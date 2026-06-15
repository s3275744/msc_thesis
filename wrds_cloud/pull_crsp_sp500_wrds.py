"""
Pull CRSP daily + point-in-time S&P 500 membership for the Dong & Xu (2025)
replication. Run this ON the WRDS cloud (it needs the wrds PostgreSQL backend),
then download the two CSVs to analysis/dong_replication/ locally.

Dong & Xu universe: full daily S&P 500 stocks, House PTRs, 2014-2022. We pull
2014-01-01..2024-12-31 so the same file also covers the thesis 2018-2024 window.

Outputs (written to the current working dir on WRDS cloud):
  - crsp_sp500_daily_2014_2024.csv   analysis-ready daily panel
  - sp500_membership_2014_2024.csv   permno, start, ending (point-in-time)
"""

import os

import pandas as pd
import wrds

START = "2014-01-01"
END = "2024-12-31"

# Set WRDS_USERNAME in your environment, or leave it unset to be prompted.
_user = os.environ.get("WRDS_USERNAME")
db = wrds.Connection(wrds_username=_user) if _user else wrds.Connection()

# 1. Point-in-time S&P 500 membership intervals.
sp500 = db.raw_sql(
    f"""
    select permno, start, ending
    from crsp.dsp500list
    where ending >= '{START}' and start <= '{END}'
    """,
    date_cols=["start", "ending"],
)
sp500.to_csv("sp500_membership_2014_2024.csv", index=False)
print(f"S&P 500 member permnos: {sp500['permno'].nunique()}")

permnos = tuple(int(p) for p in sorted(sp500["permno"].unique()))

# 2. Daily stock file for those permnos.
dsf = db.raw_sql(
    f"""
    select permno, date, ret, prc, vol, shrout
    from crsp.dsf
    where permno in {permnos}
      and date between '{START}' and '{END}'
    """,
    date_cols=["date"],
)
print(f"dsf rows: {len(dsf):,}")

# 3. Time-varying identifiers (share code, exchange, ticker).
names = db.raw_sql(
    f"""
    select permno, namedt, nameendt, shrcd, exchcd, ticker, comnam
    from crsp.dsenames
    where permno in {permnos}
    """,
    date_cols=["namedt", "nameendt"],
)
db.close()

# 4. As-of merge names onto each daily row (namedt <= date <= nameendt).
dsf = dsf.sort_values(["date", "permno"]).reset_index(drop=True)
names = names.sort_values(["namedt", "permno"]).reset_index(drop=True)
m = pd.merge_asof(
    dsf, names, by="permno", left_on="date", right_on="namedt", direction="backward"
)
m = m[m["date"] <= m["nameendt"]].copy()

# 5. Build the columns the replication notebook expects.
m["ret"] = pd.to_numeric(m["ret"], errors="coerce")
m["mktcap_musd"] = (m["prc"].abs() * m["shrout"]) / 1000.0  # shrout in thousands -> $millions
out = m[["permno", "date", "ticker", "shrcd", "exchcd", "ret", "prc", "vol",
         "shrout", "mktcap_musd"]].copy()
out.to_csv("crsp_sp500_daily_2014_2024.csv", index=False)
print(f"written crsp_sp500_daily_2014_2024.csv  rows: {len(out):,}  "
      f"permnos: {out['permno'].nunique()}  "
      f"dates: {out['date'].min().date()}..{out['date'].max().date()}")
