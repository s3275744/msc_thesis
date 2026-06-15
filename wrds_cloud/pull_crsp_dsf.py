"""
Pull CRSP daily file for U.S. common stock (SHRCD 10/11) over the thesis
sample period and write CSV files to /scratch.

Run on WRDS (head node or Jupyter), with the `wrds` package available:

    python pull_crsp_dsf.py \
        --start 2018-10-01 \
        --end   2024-12-31 \
        --out   /scratch/eur/$USER/crsp

Output columns:
    permno, date, ticker, shrcd, exchcd,
    ret, prc, openprc, askhi, bidlo,
    shrout, vol, cfacpr, cfacshr
plus derived:
    mktcap_musd  = |prc| * shrout / 1e3      (millions of USD)
    price_abs    = |prc|                     (NBBO midpoint when no last trade)

Notes
-----
- Joins CRSP dsf to msenames to get ticker, shrcd, exchcd that were in
  effect on each trading day (handles ticker reuse correctly).
- Keeps only SHRCD in (10, 11): ordinary common stock incorporated in the US.
  Drops ADRs, ETFs, units, closed-end funds, REIT class B shares, etc.
- Does NOT filter by exchange. Keep NYSE/AMEX/NASDAQ (exchcd 1,2,3) at
  the analysis stage if you want.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import wrds

SQL = """
SELECT  a.permno,
        a.date,
        b.ticker,
        b.shrcd,
        b.exchcd,
        a.ret,
        a.prc,
        a.openprc,
        a.askhi,
        a.bidlo,
        a.shrout,
        a.vol,
        a.cfacpr,
        a.cfacshr
FROM    crsp.dsf  AS a
JOIN    crsp.msenames AS b
  ON    a.permno = b.permno
 AND    a.date BETWEEN b.namedt AND b.nameendt
WHERE   a.date BETWEEN %(start)s AND %(end)s
  AND   b.shrcd IN (10, 11, 12)
  AND   b.exchcd IN (1, 2, 3)
"""


def quarter_chunks(start: str, end: str):
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    cur = pd.Timestamp(s.year, ((s.month - 1) // 3) * 3 + 1, 1)
    while cur <= e:
        q = (cur.month - 1) // 3 + 1
        q_end = (cur + pd.offsets.QuarterEnd(0)).normalize()
        cs = max(cur, s)
        ce = min(q_end, e)
        yield f"{cur.year}Q{q}", cs.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")
        cur = q_end + pd.Timedelta(days=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-10-01")
    ap.add_argument("--end",   default="2024-12-31")
    ap.add_argument("--out",   required=True,
                    help="Output directory. One CSV per quarter is written inside.")
    args = ap.parse_args()

    out_dir = Path(args.out)
    # If user passed a .csv path, drop the suffix and treat as directory.
    if out_dir.suffix == ".csv":
        out_dir = out_dir.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to WRDS as {os.environ.get('USER', '?')}", flush=True)
    db = wrds.Connection()

    print(f"Pulling CRSP dsf  {args.start} -> {args.end}  (SHRCD 10/11), "
          f"one CSV per quarter -> {out_dir}", flush=True)

    for tag, cs, ce in quarter_chunks(args.start, args.end):
        out_path = out_dir / f"crsp_dsf_common_{tag}.csv"
        if out_path.exists():
            print(f"  {tag}: exists, skipping", flush=True)
            continue
        print(f"  {tag}: pulling {cs} -> {ce} ...", flush=True)
        df = db.raw_sql(SQL, params={"start": cs, "end": ce},
                        date_cols=["date"]).copy()
        print(f"     rows={len(df):,}  permnos={df['permno'].nunique():,}",
              flush=True)

        df["price_abs"]   = df["prc"].abs()
        df["mktcap_musd"] = df["price_abs"] * df["shrout"] / 1_000.0
        for c in ("permno", "shrcd", "exchcd"):
            df[c] = df[c].astype("int32")
        df["ticker"] = df["ticker"].astype("string")

        df = df.sort_values(["permno", "date"]).reset_index(drop=True)
        df.to_csv(out_path, index=False)
        print(f"     wrote {out_path}  ({out_path.stat().st_size / 1e6:.1f} MB)",
              flush=True)
        del df

    db.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
