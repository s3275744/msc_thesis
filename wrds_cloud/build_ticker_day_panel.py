"""
Aggregate per-ticker-day 1-minute CSV bars into a single ticker-day panel.

Inputs
------
A root directory containing one or more subfolders named bars_1min_YYYY,
each holding files named {TICKER}_{YYYY-MM-DD}_1min.csv with the schema
produced by cloud/bulk_taq_pull_v2.py.

Output
------
A single CSV file at <root>/ticker_day_panel_analysis.csv with one
row per (ticker, date), columns matching the day-level variables described
in proposals/methodology_draft.md sections 5.4.3-5.4.4.

Usage
-----
    python wrds_cloud/build_ticker_day_panel.py \
        --root "data/bars_1min" \
        --out  "data/ticker_day_panel_analysis.csv"

Notes
-----
- "Abnormal" retail/non-retail OIB (the 20-day rolling de-meaning from
  methodology 5.4.3) is *not* computed here; it requires a within-ticker
  time-series operation that is cleaner to run once on the final panel.
- Minute bins with valid_quote_share < 0.5 are flagged but not dropped at
  this stage; if you want them excluded, set --min-valid-quote-share 0.5.
- Each input file is one ticker-day, so per-file aggregation is trivially
  parallel. We use a process pool.
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

# Columns we actually need from the minute CSV files.
NEEDED_COLS = [
    "ticker", "date", "minute_idx",
    "n_trades", "regular_trade_n", "matched_trade_n",
    "share_volume", "dollar_volume", "vwap",
    "price_open", "price_high", "price_low", "price_close",
    "mean_spread_cents", "mean_rel_spread_bps", "mean_quoted_depth",
    "mean_eff_spread_bps", "retail_eff_spread_bps",
    "retail_buy_dollar", "retail_sell_dollar",
    "retail_buy_shares", "retail_sell_shares",
    "unsigned_retail_dollar",
    "nonretail_buy_dollar", "nonretail_sell_dollar",
    "nonretail_buy_shares", "nonretail_sell_shares",
    "nonretail_dollar",
    "valid_quote_n", "valid_quote_share",
]


def _safe_div(num, den):
    return num / den if den not in (0, None) and not pd.isna(den) else np.nan


def aggregate_one(path: str, min_valid_quote_share: float = 0.0) -> dict | None:
    """Aggregate one ticker-day minute file to a single row dict."""
    try:
        df = pd.read_csv(path, usecols=lambda column: column in NEEDED_COLS)
    except Exception as e:
        return {"_error": f"{path}: {e}"}

    if df.empty:
        return None

    n_total_min = len(df)
    if min_valid_quote_share > 0.0 and "valid_quote_share" in df.columns:
        df = df[df["valid_quote_share"] >= min_valid_quote_share]
    n_kept_min = len(df)
    if n_kept_min == 0:
        return None

    ticker = df["ticker"].iloc[0]
    date   = pd.Timestamp(df["date"].iloc[0]).normalize()

    # Day-level dollar/share volume and prices.
    n_trades_day         = int(df["n_trades"].sum())
    regular_trade_n_day  = int(df["regular_trade_n"].sum()) if "regular_trade_n" in df.columns else 0
    matched_trade_n_day  = int(df["matched_trade_n"].sum()) if "matched_trade_n" in df.columns else 0
    share_volume_day     = float(df["share_volume"].sum())
    dollar_volume_day    = float(df["dollar_volume"].sum())
    vwap_day             = _safe_div(dollar_volume_day, share_volume_day)

    # Day-level OHLC across minute bins (use minute_idx for ordering).
    df_sorted   = df.sort_values("minute_idx")
    price_open  = float(df_sorted["price_open"].iloc[0])
    price_close = float(df_sorted["price_close"].iloc[-1])
    price_high  = float(df["price_high"].max())
    price_low   = float(df["price_low"].min())

    # Retail / non-retail dollar flows (sums of minute sums).
    retail_buy_dollar     = float(df["retail_buy_dollar"].sum())
    retail_sell_dollar    = float(df["retail_sell_dollar"].sum())
    retail_buy_shares     = float(df["retail_buy_shares"].sum())
    retail_sell_shares    = float(df["retail_sell_shares"].sum())
    unsigned_retail_dollar = float(df.get("unsigned_retail_dollar", pd.Series([0])).sum())

    nonretail_buy_dollar  = float(df["nonretail_buy_dollar"].sum())
    nonretail_sell_dollar = float(df["nonretail_sell_dollar"].sum())
    nonretail_buy_shares  = float(df["nonretail_buy_shares"].sum())
    nonretail_sell_shares = float(df["nonretail_sell_shares"].sum())
    nonretail_dollar      = float(df["nonretail_dollar"].sum())

    retail_total_dollar    = retail_buy_dollar + retail_sell_dollar
    nonretail_total_dollar = nonretail_buy_dollar + nonretail_sell_dollar

    retail_net_dollar      = retail_buy_dollar - retail_sell_dollar
    nonretail_net_dollar   = nonretail_buy_dollar - nonretail_sell_dollar

    retail_oib       = _safe_div(retail_net_dollar, retail_total_dollar)
    nonretail_oib    = _safe_div(nonretail_net_dollar, nonretail_total_dollar)

    retail_dollar_share    = _safe_div(retail_total_dollar, dollar_volume_day)
    nonretail_dollar_share = _safe_div(nonretail_dollar, dollar_volume_day)

    # Methodology 5.4.3:
    #   rel_quoted_spread_minute = mean_spread_cents / vwap   (per minute)
    #   day value = dollar-volume-weighted average across minutes
    # Convert spread (cents) and vwap (dollars) -> dimensionless, then to bps.
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_spread_min_bps = (df["mean_spread_cents"] / 100.0) / df["vwap"] * 1e4
    w = df["dollar_volume"].to_numpy()
    s = rel_spread_min_bps.to_numpy()
    m = np.isfinite(s) & np.isfinite(w) & (w > 0)
    rel_quoted_spread_bps_dwap = (
        float(np.average(s[m], weights=w[m])) if m.any() else np.nan
    )

    # Equal-weight (time-weighted) alternative, kept for robustness.
    rel_quoted_spread_bps_twap = (
        float(np.nanmean(rel_spread_min_bps)) if np.isfinite(rel_spread_min_bps).any() else np.nan
    )

    # Quoted depth, dollar-volume-weighted.
    d = df["mean_quoted_depth"].to_numpy()
    md = np.isfinite(d) & np.isfinite(w) & (w > 0)
    quoted_depth_dwap = float(np.average(d[md], weights=w[md])) if md.any() else np.nan

    # Effective spreads, dollar-volume-weighted. mean_eff_spread_bps is already in bps.
    def _dwap(series_name: str) -> float:
        if series_name not in df.columns:
            return np.nan
        x = df[series_name].to_numpy()
        mm = np.isfinite(x) & np.isfinite(w) & (w > 0)
        return float(np.average(x[mm], weights=w[mm])) if mm.any() else np.nan

    effective_spread_bps_dwap        = _dwap("mean_eff_spread_bps")
    retail_effective_spread_bps_dwap = _dwap("retail_eff_spread_bps")

    # 1-minute realised volatility (sqrt of sum of squared log returns on close-to-close).
    pc = df_sorted["price_close"].to_numpy(dtype=float)
    valid = (pc > 0) & np.isfinite(pc)
    pc = pc[valid]
    if pc.size >= 2:
        logret = np.diff(np.log(pc))
        realized_vol_1min = float(np.sqrt(np.sum(logret ** 2)))
    else:
        realized_vol_1min = np.nan

    # Output order matches the analytical role of each block:
    #   keys -> volume/price -> retail flow -> non-retail flow -> liquidity
    #   -> volatility -> quality. Within each block, headline DV first.
    return {
        # Keys
        "ticker": ticker,
        "date":   date,
        # Volume & price
        "n_trades_day":         n_trades_day,
        "regular_trade_n_day":  regular_trade_n_day,
        "matched_trade_n_day":  matched_trade_n_day,
        "share_volume_day":     share_volume_day,
        "dollar_volume_day":    dollar_volume_day,
        "vwap_day":             vwap_day,
        "price_open":  price_open,
        "price_high":  price_high,
        "price_low":   price_low,
        "price_close": price_close,
        # Retail order flow (H1 main)
        "retail_oib":            retail_oib,
        "retail_dollar_share":   retail_dollar_share,
        "retail_net_dollar":     retail_net_dollar,
        "retail_buy_dollar":     retail_buy_dollar,
        "retail_sell_dollar":    retail_sell_dollar,
        "retail_buy_shares":     retail_buy_shares,
        "retail_sell_shares":    retail_sell_shares,
        "unsigned_retail_dollar": unsigned_retail_dollar,
        # Non-retail order flow (supervisor symmetry test)
        "nonretail_oib":          nonretail_oib,
        "nonretail_dollar_share": nonretail_dollar_share,
        "nonretail_net_dollar":   nonretail_net_dollar,
        "nonretail_buy_dollar":   nonretail_buy_dollar,
        "nonretail_sell_dollar":  nonretail_sell_dollar,
        "nonretail_buy_shares":   nonretail_buy_shares,
        "nonretail_sell_shares":  nonretail_sell_shares,
        "nonretail_dollar":       nonretail_dollar,
        # Liquidity (H2 main)
        "rel_quoted_spread_bps_dwap":       rel_quoted_spread_bps_dwap,
        "quoted_depth_dwap":                quoted_depth_dwap,
        "effective_spread_bps_dwap":        effective_spread_bps_dwap,
        "retail_effective_spread_bps_dwap": retail_effective_spread_bps_dwap,
        "rel_quoted_spread_bps_twap":       rel_quoted_spread_bps_twap,
        # Volatility (H2 robustness)
        "realized_vol_1min": realized_vol_1min,
        # Quality
        "n_minutes_raw":  int(n_total_min),
        "n_minutes_kept": int(n_kept_min),
    }


def discover_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for sub in sorted(root.iterdir()):
        if sub.is_dir() and sub.name.startswith("bars_1min_"):
            files.extend(sub.glob("*_1min.csv"))
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Folder containing bars_1min_YYYY subfolders.")
    ap.add_argument("--out",  required=True, help="Output CSV path.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--min-valid-quote-share", type=float, default=0.0,
                    help="Drop minute bins with valid_quote_share below this threshold (0 = keep all).")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N files (debug).")
    args = ap.parse_args()

    root = Path(args.root)
    out  = Path(args.out)
    files = discover_files(root)
    if args.limit:
        files = files[: args.limit]

    print(f"Found {len(files):,} minute-bar files under {root}", flush=True)
    print(f"Workers: {args.workers}", flush=True)

    rows: list[dict] = []
    errors: list[str] = []

    worker = partial(aggregate_one, min_valid_quote_share=args.min_valid_quote_share)
    paths = [str(p) for p in files]
    # Larger chunksize amortises IPC overhead; each task is tiny.
    chunksize = max(1, min(256, len(paths) // (args.workers * 8) or 1))

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        with tqdm(total=len(paths), desc="aggregating", unit="file",
                  smoothing=0.05, dynamic_ncols=True, mininterval=0.5) as pbar:
            for r in ex.map(worker, paths, chunksize=chunksize):
                if r is None:
                    pass
                elif "_error" in r:
                    errors.append(r["_error"])
                else:
                    rows.append(r)
                pbar.update(1)
                if pbar.n % 2000 == 0:
                    pbar.set_postfix(rows=len(rows), errors=len(errors))

    if not rows:
        print("No rows produced. Aborting.", file=sys.stderr)
        sys.exit(1)

    panel = pd.DataFrame(rows).sort_values(["ticker", "date"]).reset_index(drop=True)
    panel["date"] = pd.to_datetime(panel["date"]).dt.tz_localize(None)

    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out, index=False)
    print(f"\nWrote {len(panel):,} rows to {out}")
    print(f"Tickers: {panel['ticker'].nunique():,}   "
          f"Date range: {panel['date'].min().date()} -> {panel['date'].max().date()}")
    if errors:
        log = out.with_suffix(".errors.log")
        log.write_text("\n".join(errors), encoding="utf-8")
        print(f"{len(errors)} errors written to {log}")


if __name__ == "__main__":
    main()
