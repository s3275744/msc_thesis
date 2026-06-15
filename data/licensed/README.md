# Licensed data (not in git)

This folder holds the **restricted data files** the notebooks need. These files
come from licensed providers and are **not** shared in this repository. If you
have access to the same sources, place the files here with the exact names
below and the notebooks will run.

| File | Source | Used by |
|------|--------|---------|
| `congress-trading-all.xlsx` | Quiver Quantitative (congressional trading) | nb00, nb01, nb02 |
| `ticker_day_panel_analysis.csv` | TAQ, built on WRDS Cloud (see `wrds_cloud/`) | nb00 |
| `crsp_dsf_common_2018_2024.csv` | CRSP Daily Stock File (WRDS) | nb00 |
| `earnings_dates.csv` | Compustat / I/B/E/S earnings dates (WRDS) | nb00 |
| `sp500_2024_list.csv` | CRSP S&P 500 membership (has `permno`) | nb02 |
| `senator_return_influence.csv` | Cached senator return-influence ranking (built from Quiver + CRSP) | nb02 |
| `ticker_day_match_senate.parquet` | Senate committee–NAICS match (nb03 output) | nb02 |
| `ticker_naics2.parquet` | Ticker to two-digit NAICS map (Compustat, WRDS) | nb03 |
| `crsp_sp500_daily_2014_2024.csv` | CRSP daily returns, 2014–2024 (Dong-Xu) | nb04 |
| `sp500_membership_2014_2024.csv` | CRSP S&P 500 membership, 2014–2024 | nb04 |
| `work_panel.parquet` | Built by nb00, read by nb01/nb02 | nb01, nb02 |

`work_panel.parquet` is produced when you run `notebooks/00_build_panel.ipynb`.
You do not need to copy it; it is created locally.

See `../../DATA_AVAILABILITY.md` for the full provenance of each file.
