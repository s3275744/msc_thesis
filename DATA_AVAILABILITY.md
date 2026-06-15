# Data availability

This file lists every data file the notebooks use, where it comes from, and
whether it can be shared. The code reads two folders: `data/public/` (small,
shared) and `data/licensed/` (restricted, not shared).

## Public data (in this repository)

These files are small and carry no license restriction, so they are tracked by
git in `data/public/`.

| File | What it is | Source | Used by |
|------|-----------|--------|---------|
| `fomc_dates.csv` | FOMC announcement days, 2018 to 2024. | Federal Reserve FOMC calendars. | helpers (event exclusions) |
| `fomc_dates_2014_2024.csv` | FOMC announcement days, 2014 to 2024 (88 dates). | Federal Reserve FOMC calendars. | nb04 |
| `committee_naics_map.yaml` | House committee to two-digit NAICS map. | Dong and Xu (2025), Appendix Table A5. | helpers (nb03) |
| `senate_committee_naics_map.yaml` | Senate committee to two-digit NAICS map. | Author construction from Senate Rule XXV. | helpers (nb03) |
| `committee_membership_long_senate.parquet` | Senate committee membership, 115th to 118th Congress. | `unitedstates/congress-legislators` (public GitHub). | helpers (nb03) |

## Licensed data (not in this repository)

These files come from licensed providers and are blocked by `.gitignore`. To run
the notebooks, place them in `data/licensed/` with the exact names below. You
need your own access to CRSP, TAQ, and Quiver Quantitative.

| File | What it is | Source | Used by |
|------|-----------|--------|---------|
| `congress-trading-all.xlsx` | Congressional trading disclosures (PTRs). | Quiver Quantitative. | nb00, nb01, nb02, nb04 |
| `ticker_day_panel_analysis.csv` | Ticker-day order-flow panel (retail and non-retail). | TAQ, built on WRDS Cloud (`wrds_cloud/`). | nb00 |
| `crsp_dsf_common_2018_2024.csv` | CRSP daily returns and market cap, 2018 to 2024. | CRSP Daily Stock File (WRDS). | nb00 |
| `earnings_dates.csv` | Quarterly earnings announcement dates. | Compustat / I/B/E/S (WRDS). | nb00 |
| `sp500_2024_list.csv` | S&P 500 membership with `permno`. | CRSP (WRDS). | nb02 |
| `crsp_sp500_daily_2014_2024.csv` | CRSP daily S&P 500 panel, 2014 to 2024. | CRSP (WRDS). | nb04 |
| `sp500_membership_2014_2024.csv` | Point-in-time S&P 500 membership spells. | CRSP `dsp500list` (WRDS). | nb04 |
| `ticker_naics2.parquet` | Ticker to two-digit NAICS sector. | Compustat (WRDS). | nb03 |

## Cached intermediates

A few files are outputs of earlier steps, cached so the notebooks run quickly.
They are reproducible from the licensed inputs.

| File | Folder | How it is built |
|------|--------|-----------------|
| `work_panel.parquet` | licensed | Built by `00_build_panel.ipynb` from the TAQ panel, CRSP, and earnings dates. |
| `ticker_day_match_senate.parquet` | licensed | Rebuilt by `03_committee_relevance.ipynb` (Senate committee to stock NAICS match). |
| `senator_return_influence.csv` | licensed | Cached leave-one-out ranking of senators by disclosure-day return contribution. nb02 reads it to pick the top senators. |

`work_panel.parquet` is created when you run nb00, so you do not need to copy it.
`ticker_day_match_senate.parquet` is read by nb02 and rebuilt by nb03; the
shipped copy lets nb02 run before nb03.

## Rebuilding the licensed data on WRDS

If you have a WRDS account, the scripts in `wrds_cloud/` rebuild the CRSP and TAQ
inputs from scratch. See `wrds_cloud/README.md` for the order to run them.
