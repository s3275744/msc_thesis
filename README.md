# Congressional Disclosure and Retail Order Flow

Replication code for my MSc thesis. The main sample is the
U.S. Senate over 2018 to 2024, built from TAQ trades, CRSP returns, and Quiver
congressional trading records.

The code is organised as five short notebooks that run in order. Each notebook
loads cleaned data, calls a helper function, and saves a table or figure. All the
heavy logic lives in `helpers.py`, so the notebooks stay easy to read.

The notebook outputs are deliberately not cleaned. This shows that the numbers reported in the thesis are actual computations.

## What the notebooks do

| Notebook | What it does | Main output |
|----------|--------------|-------------|
| `00_build_panel.ipynb` | Build the ticker-day panel (TAQ order flow + CRSP returns + controls). | `data/licensed/work_panel.parquet` |
| `01_returns.ipynb` | Disclosure-day return effect for Senate purchases, plus event-time and placebos. | return tables and figure |
| `02_order_flow.ipynb` | Retail and non-retail order-flow response, salience splits, top-senator channel. | order-flow tables |
| `03_committee_relevance.ipynb` | Match traded stocks to Senate committee jurisdiction (NAICS). | committee mapping |
| `04_dongxu_replication.ipynb` | Replicate the Dong and Xu (2025) House disclosure-day return effect. | `outputs/tables/dongxu_replication.csv` |

## Repository layout

```
msc_thesis/
  config.py            paths and sample dates
  helpers.py           all data loading and regression logic
  requirements.txt     Python packages
  notebooks/           00 to 04, run in order
  data/
    public/            small public files, tracked by git
    licensed/          restricted data, NOT in git (see below)
  outputs/
    tables/            saved CSV tables
    figures/           saved PNG figures
  wrds_cloud/          scripts that build the licensed data on WRDS
```

## Setup

You need Python 3.11 or newer.

```powershell
# from inside the msc_thesis folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
```

Then open the `notebooks/` folder in VS Code or Jupyter, pick the `.venv`
kernel, and run the notebooks in order from `00` to `04`.

## Data

Paths are never hardcoded. `config.py` builds every path from its own location,
so the code works wherever you clone the repository.

The data is split into two folders:

- `data/public/` holds small public files (FOMC dates, committee-to-NAICS maps,
  Senate committee membership). These are tracked by git.
- `data/licensed/` holds restricted files from CRSP, TAQ, and Quiver
  Quantitative. These are **not** shared here. `.gitignore` blocks them.

To run the notebooks you must place the licensed files in `data/licensed/`
yourself, using the exact file names listed in
[`data/licensed/README.md`](data/licensed/README.md). Full provenance for every
file is in [`DATA_AVAILABILITY.md`](DATA_AVAILABILITY.md).

If you have a WRDS account, the scripts in `wrds_cloud/` rebuild the CRSP and TAQ
inputs from scratch.

## Run order

The notebooks depend on each other, so run them in order:

1. `00_build_panel.ipynb` builds `work_panel.parquet`, which `01` and `02` read.
2. `01_returns.ipynb` and `02_order_flow.ipynb` use that panel.
3. `03_committee_relevance.ipynb` rebuilds the committee match used in `02`.
4. `04_dongxu_replication.ipynb` is a standalone replication and can run on its own.

