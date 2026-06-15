# Building the licensed data on WRDS

The notebooks read licensed CRSP, TAQ, and Compustat files that are too
restricted to share. This folder holds the scripts that build those files. You
run them with your own WRDS account, then copy the output into
`../data/licensed/`.

All scripts read your WRDS username from the `WRDS_USERNAME` environment
variable. If you leave it unset, the `wrds` package asks for it the first time
you connect.

## Scripts

| Script | Builds | Goes to |
|--------|--------|---------|
| `pull_crsp_dsf.py` | CRSP daily file for common stock, one CSV per quarter. | `crsp_dsf_common_2018_2024.csv` (nb00) |
| `pull_crsp_sp500_wrds.py` | CRSP daily S&P 500 panel + point-in-time membership, 2014 to 2024. | `crsp_sp500_daily_2014_2024.csv`, `sp500_membership_2014_2024.csv` (nb04) |
| `pull_earnings_dates.ipynb` | Quarterly earnings announcement dates. | `earnings_dates.csv` (nb00) |
| `bulk_taq_pull_v2.py` | One-minute TAQ bars with retail and non-retail order flow. | raw `bars_1min/` CSVs |
| `build_ticker_day_panel.py` | Aggregate the one-minute bars to a ticker-day panel. | `ticker_day_panel_analysis.csv` (nb00) |
| `submit_taq.sh`, `submit_crsp_pull.sh` | SGE job scripts to run the pulls on the WRDS cluster. | — |

## Order

1. `pull_crsp_dsf.py` and `pull_earnings_dates.ipynb` build the CRSP and earnings
   inputs for the main panel.
2. `bulk_taq_pull_v2.py` pulls the one-minute TAQ bars (this is the slow step;
   run it as an array job with `submit_taq.sh`).
3. `build_ticker_day_panel.py` turns those bars into `ticker_day_panel_analysis.csv`.
4. `pull_crsp_sp500_wrds.py` builds the separate S&P 500 panel used by the
   Dong and Xu replication (nb04).

## Why run on WRDS Cloud

The TAQ pull moves a lot of data. Running it next to the WRDS database, with one
array task per year, is far faster than pulling to a laptop. The scripts write to
`/scratch`, which has the space the pull needs.

## Example: TAQ array job

```bash
# on WRDS Cloud
export WRDS_USERNAME=your_user
cd /scratch/<institution>/$USER/taq_batch/code
chmod +x submit_taq.sh
qsub -t 2018-2024 submit_taq.sh
```

Each task writes one-minute bar CSVs to `bars_1min/`. When all years are done,
run `build_ticker_day_panel.py` to collapse them into the ticker-day panel, then
copy that file to `../data/licensed/`.

## Other inputs

Two licensed files are built outside this folder:

- `ticker_naics2.parquet` (ticker to two-digit NAICS) comes from Compustat
  (`comp.company`, `comp.security`) on WRDS.
- The congressional trading file `congress-trading-all.xlsx` is downloaded from
  Quiver Quantitative.

See `../DATA_AVAILABILITY.md` for the full list.
