"""Bulk TAQ pull and 1-minute aggregation, designed for WRDS Cloud.

V2: per-ticker streaming. For each (date, ticker) we pull trades+quotes,
match, classify, aggregate and write one CSV, then drop everything
from memory before moving to the next ticker. Peak memory is ~one
ticker-day's worth of raw TAQ rows, not one date's worth.

Reads an event panel CSV (ticker_day_panel.csv) and, for every
trading date in it, pulls trades + NBBO from TAQ Millisecond, signs trades
(BJZZ subpenny + Lee-Ready quote-midpoint), aggregates to 1-minute bars,
and writes one CSV per (ticker, date).

Designed to be:
- Resumable: ticker-days that already have an output CSV are skipped.
- Cloud-native: all paths come from CLI / env vars; defaults point at /scratch.
- Parallelisable: split the work via --start-date / --end-date or --year so
  multiple SGE array tasks can run concurrently without colliding.

Usage on WRDS Cloud (typical):
    python bulk_taq_pull_v2.py \
        --panel  /scratch/eur/$USER/taq_batch/ticker_day_panel.csv \
        --out    /scratch/eur/$USER/taq_batch/bars_1min \
        --logs   /scratch/eur/$USER/taq_batch/logs \
        --year   2024 \
        --workers 3
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import wrds


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--panel",  required=True, type=Path,
                   help="CSV with columns ticker,date.")
    p.add_argument("--out",    required=True, type=Path,
                   help="Directory for per-(ticker,date) 1-minute CSV files.")
    p.add_argument("--logs",   required=True, type=Path,
                   help="Directory for run logs and per-date stats.")
    p.add_argument("--wrds-user", default=os.environ.get("WRDS_USER"))
    p.add_argument("--year",       type=int, default=None,
                   help="If set, only process dates in this calendar year.")
    p.add_argument("--start-date", type=str, default=None,
                   help="Inclusive YYYY-MM-DD lower bound (overrides --year if set).")
    p.add_argument("--end-date",   type=str, default=None,
                   help="Inclusive YYYY-MM-DD upper bound.")
    p.add_argument("--workers",       type=int, default=3,
                   help="Threads (each opens its own WRDS connection). Keep <=4.")
    p.add_argument("--ticker-batch",  type=int, default=1,
                   help="Kept for CLI compatibility; ignored in V2 (always 1).")
    p.add_argument("--max-retries",   type=int, default=3)
    p.add_argument("--no-resume",     action="store_true",
                   help="Re-pull and overwrite ticker-days even if output exists.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Trade signing + aggregation helpers (identical logic to V1)
# ---------------------------------------------------------------------------
RTH_START = "09:30"
RTH_END   = "16:00"
RETRY_BACKOFF_S = [5, 30, 300]


def out_path(out_dir: Path, ticker: str, date: pd.Timestamp) -> Path:
    return out_dir / f"{ticker}_{date.strftime('%Y-%m-%d')}_1min.csv"


def build_dt(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["dt"] = pd.to_datetime(
        df["date"].astype(str) + " " + df["time_m"].astype(str), errors="coerce"
    )
    return df.dropna(subset=["dt"]).sort_values("dt")


def match_and_enrich(trades: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or quotes.empty:
        return pd.DataFrame()
    trades_dt = build_dt(trades)
    quotes_dt = build_dt(quotes)
    for c in ["sym_root", "sym_suffix"]:
        if c not in trades_dt.columns:
            trades_dt[c] = None
        if c not in quotes_dt.columns:
            quotes_dt[c] = None

    taq = pd.merge_asof(
        trades_dt,
        quotes_dt[["dt", "bid", "bidsiz", "ask", "asksiz", "sym_root", "sym_suffix"]],
        on="dt", by=["sym_root", "sym_suffix"],
        direction="backward", tolerance=pd.Timedelta("1s"),
    )
    taq["midpoint"]       = (taq["bid"] + taq["ask"]) / 2.0
    taq["spread"]         = taq["ask"] - taq["bid"]
    taq["rel_spread_bps"] = (taq["spread"] / taq["midpoint"]) * 1e4
    taq["quoted_depth"]   = (taq["bidsiz"].fillna(0) + taq["asksiz"].fillna(0)) / 2.0
    taq["valid_quote"]    = (taq["bid"] > 0) & (taq["ask"] > 0) & (taq["ask"] > taq["bid"])
    taq["zero_spread"]    = (taq["ask"] == taq["bid"]) & taq["bid"].notna()

    price = taq["price"].to_numpy(dtype="float64", na_value=np.nan)
    mid   = taq["midpoint"].to_numpy(dtype="float64", na_value=np.nan)
    taq["lr_sign"] = np.where(price > mid, 1, np.where(price < mid, -1, 0)).astype("int8")
    taq["eff_spread_bps"] = (
        2.0 * taq["lr_sign"] * (taq["price"] - taq["midpoint"]) / taq["midpoint"] * 1e4
    )
    tr_scond_stripped = taq["tr_scond"].astype("string").str.strip()
    taq["is_regular"] = tr_scond_stripped.isin([pd.NA, "", "@"])
    return taq


def classify_retail(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    valid_nbbo   = (out["bid"] > 0) & (out["ask"] > 0) & (out["ask"] > out["bid"])
    off_exchange = out["ex"].astype(str) == "D"
    price_cents  = out["price"] * 100.0
    round_penny  = (price_cents - price_cents.round()).abs() < 1e-6
    is_candidate = off_exchange & (~round_penny) & valid_nbbo

    R = pd.Series(np.nan, index=out.index, dtype="float64")
    R.loc[valid_nbbo] = (
        out.loc[valid_nbbo, "price"] - out.loc[valid_nbbo, "bid"]
    ) / (out.loc[valid_nbbo, "ask"] - out.loc[valid_nbbo, "bid"])

    retail_side = pd.Series(pd.NA, index=out.index, dtype="object")
    retail_side.loc[is_candidate & (R > 0.6)] = "buy"
    retail_side.loc[is_candidate & (R < 0.4)] = "sell"

    out["R"]              = R
    out["is_candidate"]   = is_candidate
    out["retail_side"]    = retail_side
    out["is_retail_like"] = retail_side.isin(["buy", "sell"])
    return out


def aggregate_1min(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    work = df.dropna(subset=["dt", "price", "size"]).copy()
    if work.empty:
        return pd.DataFrame()
    work["dollar"] = work["price"] * work["size"]

    retail_buy_mask    = work["retail_side"].fillna("").eq("buy")
    retail_sell_mask   = work["retail_side"].fillna("").eq("sell")
    is_signed_retail   = retail_buy_mask | retail_sell_mask
    is_unsigned_retail = work["is_candidate"].fillna(False) & ~is_signed_retail

    work["retail_buy_dollar"]      = np.where(retail_buy_mask,    work["dollar"], 0.0)
    work["retail_sell_dollar"]     = np.where(retail_sell_mask,   work["dollar"], 0.0)
    work["retail_buy_shares"]      = np.where(retail_buy_mask,    work["size"],   0)
    work["retail_sell_shares"]     = np.where(retail_sell_mask,   work["size"],   0)
    work["unsigned_retail_dollar"] = np.where(is_unsigned_retail, work["dollar"], 0.0)
    work["unsigned_retail_shares"] = np.where(is_unsigned_retail, work["size"],   0)
    work["signed_retail_flag"]     = is_signed_retail.astype(int)
    work["unsigned_retail_flag"]   = is_unsigned_retail.astype(int)

    is_nonretail        = (~is_signed_retail).astype(bool)
    nonretail_buy_mask  = (is_nonretail & (work["price"] > work["midpoint"])).fillna(False)
    nonretail_sell_mask = (is_nonretail & (work["price"] < work["midpoint"])).fillna(False)
    work["nonretail_buy_dollar"]   = np.where(nonretail_buy_mask,  work["dollar"], 0.0)
    work["nonretail_sell_dollar"]  = np.where(nonretail_sell_mask, work["dollar"], 0.0)
    work["nonretail_buy_shares"]   = np.where(nonretail_buy_mask,  work["size"],   0)
    work["nonretail_sell_shares"]  = np.where(nonretail_sell_mask, work["size"],   0)
    work["nonretail_dollar_all"]   = np.where(is_nonretail,        work["dollar"], 0.0)

    work["eff_spread_dollar"]        = work["eff_spread_bps"] * work["dollar"]
    work["retail_eff_spread_dollar"] = np.where(
        is_signed_retail, work["eff_spread_dollar"], 0.0
    )

    odd_lot_mask = work["size"] < 100
    work["odd_lot_flag"]     = odd_lot_mask.astype(int)
    work["odd_lot_dollar"]   = np.where(odd_lot_mask, work["dollar"], 0.0)
    work["regular_flag"]     = work["is_regular"].fillna(False).astype(int)
    work["matched_flag"]     = work["bid"].notna().astype(int)
    work["valid_quote_flag"] = work["valid_quote"].fillna(False).astype(int)
    work["zero_spread_flag"] = work["zero_spread"].fillna(False).astype(int)

    work = work.set_index("dt").between_time(RTH_START, RTH_END)
    if work.empty:
        return pd.DataFrame()

    bars = work.groupby(pd.Grouper(freq="1min")).agg(
        n_trades              = ("price",                "size"),
        regular_trade_n       = ("regular_flag",         "sum"),
        matched_trade_n       = ("matched_flag",         "sum"),
        share_volume          = ("size",                 "sum"),
        dollar_volume         = ("dollar",               "sum"),
        price_open            = ("price",                "first"),
        price_high            = ("price",                "max"),
        price_low             = ("price",                "min"),
        price_close           = ("price",                "last"),
        mid_open              = ("midpoint",             "first"),
        mid_high              = ("midpoint",             "max"),
        mid_low               = ("midpoint",             "min"),
        mid_close             = ("midpoint",             "last"),
        mean_spread           = ("spread",               "mean"),
        median_spread         = ("spread",               "median"),
        mean_rel_spread_bps   = ("rel_spread_bps",       "mean"),
        eff_spread_dollar_sum = ("eff_spread_dollar",    "sum"),
        mean_bid_size         = ("bidsiz",               "mean"),
        mean_ask_size         = ("asksiz",               "mean"),
        mean_quoted_depth     = ("quoted_depth",         "mean"),
        signed_retail_n       = ("signed_retail_flag",   "sum"),
        unsigned_retail_n     = ("unsigned_retail_flag", "sum"),
        retail_buy_dollar     = ("retail_buy_dollar",    "sum"),
        retail_sell_dollar    = ("retail_sell_dollar",   "sum"),
        retail_buy_shares     = ("retail_buy_shares",    "sum"),
        retail_sell_shares    = ("retail_sell_shares",   "sum"),
        unsigned_retail_dollar = ("unsigned_retail_dollar", "sum"),
        unsigned_retail_shares = ("unsigned_retail_shares", "sum"),
        nonretail_buy_dollar  = ("nonretail_buy_dollar", "sum"),
        nonretail_sell_dollar = ("nonretail_sell_dollar","sum"),
        nonretail_buy_shares  = ("nonretail_buy_shares", "sum"),
        nonretail_sell_shares = ("nonretail_sell_shares","sum"),
        nonretail_dollar      = ("nonretail_dollar_all", "sum"),
        retail_eff_spread_dollar_sum = ("retail_eff_spread_dollar", "sum"),
        odd_lot_n             = ("odd_lot_flag",         "sum"),
        odd_lot_dollar        = ("odd_lot_dollar",       "sum"),
        valid_quote_n         = ("valid_quote_flag",     "sum"),
        zero_spread_n         = ("zero_spread_flag",     "sum"),
    )
    bars = bars[bars["n_trades"] > 0].copy()
    if bars.empty:
        return pd.DataFrame()

    bars["vwap"]                 = bars["dollar_volume"] / bars["share_volume"].replace(0, np.nan)
    bars["mean_spread_cents"]    = bars["mean_spread"] * 100.0
    bars["median_spread_cents"]  = bars["median_spread"] * 100.0
    bars["mean_eff_spread_bps"]  = bars["eff_spread_dollar_sum"] / bars["dollar_volume"].replace(0, np.nan)
    retail_total_dollar_signed   = bars["retail_buy_dollar"] + bars["retail_sell_dollar"]
    bars["retail_eff_spread_bps"] = (
        bars["retail_eff_spread_dollar_sum"] / retail_total_dollar_signed.replace(0, np.nan)
    )
    bars["odd_lot_trade_share"]  = bars["odd_lot_n"] / bars["n_trades"].replace(0, np.nan)
    bars["odd_lot_dollar_share"] = bars["odd_lot_dollar"] / bars["dollar_volume"].replace(0, np.nan)
    bars = bars.drop(columns=["mean_spread", "median_spread",
                              "eff_spread_dollar_sum", "retail_eff_spread_dollar_sum"])

    retail_total_dollar    = bars["retail_buy_dollar"] + bars["retail_sell_dollar"]
    nonretail_total_dollar = bars["nonretail_buy_dollar"] + bars["nonretail_sell_dollar"]
    bars["retail_net_dollar"]        = bars["retail_buy_dollar"] - bars["retail_sell_dollar"]
    bars["nonretail_net_dollar"]     = bars["nonretail_buy_dollar"] - bars["nonretail_sell_dollar"]
    bars["retail_identified_dollar"] = retail_total_dollar + bars["unsigned_retail_dollar"]
    bars["retail_dollar_share"]      = retail_total_dollar / bars["dollar_volume"].replace(0, np.nan)
    bars["retail_trade_share"]       = bars["signed_retail_n"] / bars["n_trades"].replace(0, np.nan)
    bars["retail_oib"]               = bars["retail_net_dollar"] / retail_total_dollar.replace(0, np.nan)
    bars["nonretail_dollar_share"]   = bars["nonretail_dollar"] / bars["dollar_volume"].replace(0, np.nan)
    bars["nonretail_signed_oib"]     = bars["nonretail_net_dollar"] / nonretail_total_dollar.replace(0, np.nan)
    bars["valid_quote_share"]        = bars["valid_quote_n"] / bars["n_trades"].replace(0, np.nan)

    bars = bars.reset_index().rename(columns={"dt": "time_bin"})
    bars.insert(0, "ticker", ticker)
    bars.insert(1, "date",   bars["time_bin"].dt.normalize())
    bars["minute_idx"] = (
        (bars["time_bin"].dt.hour * 60 + bars["time_bin"].dt.minute) - (9 * 60 + 30)
    ).astype("int32")

    ordered = [
        "ticker", "date", "time_bin", "minute_idx",
        "n_trades", "regular_trade_n", "matched_trade_n",
        "share_volume", "dollar_volume", "vwap",
        "price_open", "price_high", "price_low", "price_close",
        "mid_open", "mid_high", "mid_low", "mid_close",
        "mean_spread_cents", "median_spread_cents",
        "mean_rel_spread_bps", "mean_eff_spread_bps",
        "mean_bid_size", "mean_ask_size", "mean_quoted_depth",
        "signed_retail_n", "unsigned_retail_n",
        "retail_buy_dollar", "retail_sell_dollar", "retail_net_dollar",
        "retail_buy_shares", "retail_sell_shares",
        "unsigned_retail_dollar", "unsigned_retail_shares",
        "retail_identified_dollar",
        "retail_dollar_share", "retail_trade_share", "retail_oib",
        "nonretail_buy_dollar", "nonretail_sell_dollar", "nonretail_net_dollar",
        "nonretail_buy_shares", "nonretail_sell_shares",
        "nonretail_dollar", "nonretail_dollar_share", "nonretail_signed_oib",
        "retail_eff_spread_bps",
        "odd_lot_n", "odd_lot_dollar", "odd_lot_trade_share", "odd_lot_dollar_share",
        "valid_quote_n", "zero_spread_n", "valid_quote_share",
    ]
    return bars[ordered]


# ---------------------------------------------------------------------------
# Per-thread WRDS connection management + resilient SQL
# ---------------------------------------------------------------------------
_tls = threading.local()


def _get_conn(wrds_user: str):
    conn = getattr(_tls, "db", None)
    if conn is None:
        conn = wrds.Connection(wrds_username=wrds_user) if wrds_user else wrds.Connection()
        _tls.db = conn
    return conn


def _reconnect_tls(wrds_user: str):
    conn = getattr(_tls, "db", None)
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
    _tls.db = wrds.Connection(wrds_username=wrds_user) if wrds_user else wrds.Connection()


def raw_sql_resilient(sql: str, wrds_user: str, max_retries: int) -> pd.DataFrame:
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return _get_conn(wrds_user).raw_sql(sql)
        except Exception as e:
            last_err = e
            if attempt == max_retries:
                break
            time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
            try:
                _reconnect_tls(wrds_user)
            except Exception:
                pass
    raise last_err


def pull_one_ticker_day(date: pd.Timestamp, ticker: str, cfg) -> tuple:
    """Pull trades and quotes for a single (date, ticker). No accumulation."""
    year = date.year
    ctm_table  = f"taqmsec.ctm_{year}"
    nbbo_table = f"taqmsec.complete_nbbo_{year}"
    dt_str = date.strftime("%Y-%m-%d")
    sql_t = f"""
        SELECT date, time_m, price, size, sym_root, sym_suffix, ex, tr_rf, tr_scond
        FROM {ctm_table}
        WHERE date = '{dt_str}' AND sym_root = '{ticker}'
          AND (sym_suffix IS NULL OR sym_suffix = '')
          AND time_m BETWEEN '09:30:00' AND '16:00:00'
          AND price > 0 AND size > 0
    """
    sql_q = f"""
        SELECT date, time_m,
               best_bid AS bid, best_bidsizeshares AS bidsiz,
               best_ask AS ask, best_asksizeshares AS asksiz,
               sym_root, sym_suffix
        FROM {nbbo_table}
        WHERE date = '{dt_str}' AND sym_root = '{ticker}'
          AND (sym_suffix IS NULL OR sym_suffix = '')
          AND time_m BETWEEN '09:30:00' AND '16:00:00'
    """
    trades = raw_sql_resilient(sql_t, cfg.wrds_user, cfg.max_retries)
    quotes = raw_sql_resilient(sql_q, cfg.wrds_user, cfg.max_retries)
    return trades, quotes


# ---------------------------------------------------------------------------
# Per-date worker (V2: streams ticker by ticker)
# ---------------------------------------------------------------------------
def process_date(
    date: pd.Timestamp, tickers: list, cfg, write_lock, log_f
) -> dict:
    stats = {"date": date.strftime("%Y-%m-%d"),
             "requested": len(tickers), "saved": 0, "empty": 0, "errors": 0}

    if not cfg.no_resume:
        tickers = [t for t in tickers if not out_path(cfg.out, t, date).exists()]
        if not tickers:
            stats["skipped_all"] = True
            stats["elapsed_s"] = 0.0
            return stats

    t0 = time.time()

    for ticker in tickers:
        try:
            tr, qu = pull_one_ticker_day(date, ticker, cfg)
            if tr.empty or qu.empty:
                del tr, qu
                stats["empty"] += 1
                continue

            matched = match_and_enrich(tr, qu)
            del tr, qu
            if matched.empty:
                del matched
                stats["empty"] += 1
                continue

            classified = classify_retail(matched)
            del matched
            bars = aggregate_1min(classified, ticker)
            del classified
            if bars.empty:
                del bars
                stats["empty"] += 1
                continue

            bars.to_csv(out_path(cfg.out, ticker, date), index=False)
            del bars
            stats["saved"] += 1
        except Exception as e:
            stats["errors"] += 1
            with write_lock:
                log_f.write(f"{date.date()} {ticker} process-failed: {type(e).__name__}: {e}\n")
                log_f.write(traceback.format_exc() + "\n")
                log_f.flush()
        # Periodic gc to keep allocator from holding too much.
        if (stats["saved"] + stats["empty"] + stats["errors"]) % 25 == 0:
            gc.collect()

    gc.collect()
    stats["elapsed_s"] = round(time.time() - t0, 2)
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = parse_args()
    cfg.out.mkdir(parents=True, exist_ok=True)
    cfg.logs.mkdir(parents=True, exist_ok=True)

    print(f"Panel        : {cfg.panel}",  flush=True)
    print(f"Output dir   : {cfg.out}",    flush=True)
    print(f"Logs dir     : {cfg.logs}",   flush=True)
    print(f"WRDS user    : {cfg.wrds_user}", flush=True)
    print(f"Workers      : {cfg.workers}",   flush=True)
    print(f"V2 mode      : per-ticker streaming (ticker-batch ignored)", flush=True)
    print(f"Resume mode  : {not cfg.no_resume}", flush=True)

    panel = pd.read_csv(cfg.panel)
    panel["date"]   = pd.to_datetime(panel["date"]).dt.normalize()
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()

    if cfg.year is not None:
        panel = panel[panel["date"].dt.year == cfg.year]
    if cfg.start_date:
        panel = panel[panel["date"] >= pd.Timestamp(cfg.start_date)]
    if cfg.end_date:
        panel = panel[panel["date"] <= pd.Timestamp(cfg.end_date)]

    panel = panel.sort_values(["date", "ticker"]).reset_index(drop=True)
    if panel.empty:
        print("No rows after date filtering. Exiting.", flush=True)
        return 0

    tickers_by_date = {pd.Timestamp(k): v["ticker"].tolist()
                       for k, v in panel.groupby("date", sort=True)}
    all_dates  = sorted(tickers_by_date.keys())
    total_tdays = len(panel)

    print(f"Dates        : {len(all_dates):,}",   flush=True)
    print(f"Ticker-days  : {total_tdays:,}",      flush=True)
    print(f"Date range   : {all_dates[0].date()} -> {all_dates[-1].date()}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"y{cfg.year}" if cfg.year else "all"
    log_path   = cfg.logs / f"bulkv2_{tag}_{stamp}.log"
    stats_path = cfg.logs / f"bulkv2_{tag}_{stamp}_stats.jsonl"
    log_f   = open(log_path,   "a", encoding="utf-8")
    stats_f = open(stats_path, "a", encoding="utf-8")
    write_lock = threading.Lock()

    running = {"requested": 0, "saved": 0, "empty": 0, "errors": 0}
    tdays_done = 0
    t_run = time.time()

    print(f"Launching ThreadPoolExecutor with {cfg.workers} workers...", flush=True)
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {
            pool.submit(process_date, dt, tickers_by_date[dt], cfg, write_lock, log_f): dt
            for dt in all_dates
        }
        n_done = 0
        for fut in as_completed(futures):
            dt = futures[fut]
            try:
                stats = fut.result()
            except Exception as e:
                stats = {"date": dt.strftime("%Y-%m-%d"),
                         "requested": len(tickers_by_date[dt]),
                         "saved": 0, "empty": 0, "errors": len(tickers_by_date[dt]),
                         "fatal": f"{type(e).__name__}: {e}"}
                with write_lock:
                    log_f.write(f"{dt.date()} fatal: {type(e).__name__}: {e}\n")
                    log_f.flush()
            with write_lock:
                stats_f.write(json.dumps(stats) + "\n")
                stats_f.flush()
            for k in ("requested", "saved", "empty", "errors"):
                running[k] += stats.get(k, 0)
            tdays_done += stats.get("requested", 0)
            n_done += 1
            if n_done % 5 == 0 or n_done == len(all_dates):
                elapsed = max(time.time() - t_run, 1e-9)
                tday_rate = tdays_done / elapsed
                eta_min = (total_tdays - tdays_done) / max(tday_rate, 1e-9) / 60.0
                print(f"[{n_done:5d}/{len(all_dates)}] last={dt.date()}  "
                      f"tdays={tdays_done:,}/{total_tdays:,}  "
                      f"saved={running['saved']:,}  empty={running['empty']:,}  "
                      f"errors={running['errors']:,}  "
                      f"{n_done/elapsed:.2f} d/s  {tday_rate:.1f} td/s  "
                      f"ETA={eta_min:.1f} min", flush=True)

    log_f.close()
    stats_f.close()
    print("DONE.", running, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
