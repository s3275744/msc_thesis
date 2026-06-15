"""Shared helper functions for the analysis notebooks.

The goal is to keep the notebooks short and easy to read. All the repeated
work (loading data, building treatment days, running the panel regression)
lives here in small, plain functions.
"""

import numpy as np
import pandas as pd
from config import END, LICENSED_DATA, PUBLIC_DATA, START
from linearmodels.panel import PanelOLS

# --- Constants used to build the treatment -------------------------------

# Baseline stock controls used in every preferred regression.
BASE_CONTROLS = [
    "earnings_window",
    "lag_ret_1d",
    "lag_logdvol_1d",
    "lag_amihud_5d",
    "lag_rvol_5d",
    "log_mktcap",
]

# Ticker types that are not ordinary stocks (options, crypto, etc.). Dropped.
NON_EQUITY_TT = {
    "OP", "STOCK OPTION", "CRYPTOCURRENCY", "CT", "CRYPTO", "OTHER SECURITIES",
    "OT", "ET", "PS", "OI", "SA", "OL", "GS", "HN", "AB",
}

# Quiver reports trade size as a range. We use the midpoint in dollars.
SIZE_BRACKETS = {
    "$1,001 - $15,000": 8_000,
    "$15,001 - $50,000": 32_500,
    "$50,001 - $100,000": 75_000,
    "$100,001 - $250,000": 175_000,
    "$250,001 - $500,000": 375_000,
    "$500,001 - $1,000,000": 750_000,
    "$1,000,001 - $5,000,000": 3_000_000,
    "$5,000,001 - $25,000,000": 15_000_000,
    "$25,000,001 - $50,000,000": 37_500_000,
}

# The three US federal election days in the sample period.
ELECTION_DATES = [pd.Timestamp("2020-11-03"), pd.Timestamp("2022-11-08"), pd.Timestamp("2024-11-05")]


# --- Loading data --------------------------------------------------------

def load_work_panel():
    """Load the analysis panel that notebook 00 builds and saves."""
    panel = pd.read_parquet(LICENSED_DATA / "work_panel.parquet")
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    return panel.sort_values(["ticker", "date"]).reset_index(drop=True)


def load_quiver():
    """Load the Quiver congressional trading file and clean a few columns."""
    quiver = pd.read_excel(LICENSED_DATA / "congress-trading-all.xlsx")
    quiver["Ticker"] = quiver["Ticker"].astype(str).str.upper().str.strip()
    quiver["Filed"] = pd.to_datetime(quiver["Filed"], errors="coerce")
    quiver["Traded"] = pd.to_datetime(quiver["Traded"], errors="coerce")
    quiver["filed_date"] = quiver["Filed"].dt.normalize()
    quiver["TickerType_norm"] = quiver["TickerType"].astype(str).str.upper().str.strip()
    quiver["Transaction_norm"] = quiver["Transaction"].astype(str).str.upper().str.strip()
    quiver["Comments_norm"] = quiver["Comments"].astype(str).str.upper().str.strip()
    quiver["Chamber_norm"] = quiver["Chamber"].astype(str).str.upper().str.strip()
    quiver["Party_norm"] = quiver["Party"].astype(str).str.upper().str.strip().str[:1]
    quiver["handfiled"] = quiver["Comments_norm"].str.contains(r"HAND\s*-?\s*FILED", regex=True, na=False)
    quiver["size_midpoint"] = quiver["Trade_Size_USD"].map(SIZE_BRACKETS)
    quiver["lag_days"] = (quiver["Filed"] - quiver["Traded"]).dt.days
    quiver.loc[(quiver["lag_days"] < 0) | (quiver["lag_days"] > 365), "lag_days"] = np.nan
    return quiver


# --- Building the treatment ----------------------------------------------

def trading_days(panel):
    """Return the sorted list of trading days in the panel."""
    return pd.Index(sorted(panel["date"].dropna().unique()))


def next_trading_day(dates, days):
    """Map each date to itself if it is a trading day, else the next one."""
    days = pd.Index(days)

    def lookup(value):
        if pd.isna(value):
            return pd.NaT
        i = days.searchsorted(pd.Timestamp(value), side="left")
        return pd.Timestamp(days[i]) if i < len(days) else pd.NaT

    return pd.to_datetime(dates).map(lookup)


def purchase_events(quiver, chamber, days):
    """Keep qualifying purchase disclosures and map each to a trading day.

    chamber is one of "pooled", "house", "senate".
    days is the list of trading days from `trading_days`.
    """
    keep = quiver["Filed"].between(pd.Timestamp(START), pd.Timestamp(END))
    keep &= quiver["Transaction_norm"].eq("PURCHASE")
    keep &= ~quiver["handfiled"]
    keep &= ~quiver["TickerType_norm"].isin(NON_EQUITY_TT)
    keep &= quiver["Ticker"].notna() & quiver["Ticker"].ne("NAN") & quiver["Ticker"].str.len().gt(0)
    if chamber == "house":
        keep &= quiver["Chamber_norm"].eq("HOUSE")
    elif chamber == "senate":
        keep &= quiver["Chamber_norm"].eq("SENATE")

    events = quiver.loc[keep].copy()
    events = events.rename(columns={"Ticker": "ticker"})
    events["date"] = next_trading_day(events["filed_date"], days)
    events = events.dropna(subset=["date"]).copy()
    events["is_dem"] = events["Party_norm"].eq("D").astype("int8")
    events["is_rep"] = events["Party_norm"].eq("R").astype("int8")
    return events


def collapse_cells(events):
    """Collapse event rows to one row per ticker-day (a treated cell)."""
    if events.empty:
        return pd.DataFrame(columns=["ticker", "date"])
    top_members = set(events["BioGuideID"].value_counts().head(15).index)
    events = events.copy()
    events["top_member"] = events["BioGuideID"].isin(top_members).astype("int8")
    cells = events.groupby(["ticker", "date"], as_index=False).agg(
        report_n=("Filed", "size"),
        size_max=("size_midpoint", "max"),
        lag_days_min=("lag_days", "min"),
        dem_n=("is_dem", "sum"),
        rep_n=("is_rep", "sum"),
        top_member_n=("top_member", "sum"),
    )
    return cells


def event_flag(panel_index, cells, days, window=0, shift=0):
    """Return a 0/1 array marking treated ticker-days in the panel.

    window: also treat this many trading days after each event day.
    shift:  move the event by this many trading days (used for placebos
            and event-time plots). When shift is set, window is ignored.
    """
    position = {pd.Timestamp(d): i for i, d in enumerate(days)}
    treated = set()
    for ticker, event_date in zip(cells["ticker"], cells["date"]):
        i = position.get(pd.Timestamp(event_date))
        if i is None:
            continue
        if shift != 0:
            j = i + shift
            if 0 <= j < len(days):
                treated.add((ticker, pd.Timestamp(days[j])))
        else:
            for step in range(window + 1):
                j = i + step
                if j < len(days):
                    treated.add((ticker, pd.Timestamp(days[j])))
    if not treated:
        return np.zeros(len(panel_index), dtype="int8")
    treated_index = pd.MultiIndex.from_tuples(treated, names=["ticker", "date"])
    return panel_index.isin(treated_index).astype("int8")


# --- Running the regression ----------------------------------------------

def run_panel(df, y, x_cols, controls=BASE_CONTROLS, drop_zero_retail=False, entity=True, time=True):
    """Fit a panel regression with two-way clustered standard errors.

    entity/time switch the ticker and date fixed effects on or off. With both
    off we add a constant so the model still has an intercept.
    """
    data = df.copy()
    if drop_zero_retail:
        data = data[data["signed_retail_dollar"] > 0]
    needed = [y] + list(x_cols) + list(controls) + ["ticker", "date"]
    data = data.dropna(subset=needed).set_index(["ticker", "date"])
    exog = data[list(x_cols) + list(controls)].copy()
    if not entity and not time:
        exog.insert(0, "const", 1.0)
    model = PanelOLS(
        data[y], exog,
        entity_effects=entity, time_effects=time,
        drop_absorbed=True, check_rank=False,
    )
    return model.fit(cov_type="clustered", cluster_entity=True, cluster_time=True, low_memory=True)


def coef(result, term):
    """Pull one coefficient out of a fitted result as a small dict (in bps)."""
    return {
        "coef_bps": result.params[term] * 10_000,
        "se_bps": result.std_errors[term] * 10_000,
        "t": result.tstats[term],
        "p": result.pvalues[term],
        "nobs": int(result.nobs),
    }


def tidy(result, terms=None):
    """Return a tidy table of coefficients (in basis points)."""
    table = pd.DataFrame({
        "coef_bps": result.params * 10_000,
        "se_bps": result.std_errors * 10_000,
        "t": result.tstats,
        "p": result.pvalues,
    })
    if terms is not None:
        table = table.loc[terms]
    return table.round(4)


def run_event_time(panel, y, panel_index, cells, days, drop_zero_retail=False, kmin=-5, kmax=10):
    """Fit one joint event-time regression with a separate dummy for each day k.

    This is the proper event-study form: all leads and lags go into the same
    model. Returns a small table with the coefficient for each event day k.
    """
    data = panel.copy()
    day_cols = []
    for k in range(kmin, kmax + 1):
        name = f"k{k:+d}"
        data[name] = event_flag(panel_index, cells, days, shift=k)
        day_cols.append((k, name))
    result = run_panel(data, y, [c for _, c in day_cols], drop_zero_retail=drop_zero_retail)
    rows = []
    for k, name in day_cols:
        rows.append({
            "k": k,
            "coef": result.params[name],
            "se": result.std_errors[name],
            "coef_bps": result.params[name] * 10_000,
            "se_bps": result.std_errors[name] * 10_000,
            "p": result.pvalues[name],
        })
    return pd.DataFrame(rows)


def _sp500_lookup():
    """Map each ticker to its S&P 500 membership spans (start, end)."""
    path = LICENSED_DATA / "sp500_2024_list.csv"
    if not path.exists():
        return {}
    sp = pd.read_csv(path)
    sp["ticker"] = sp["ticker"].astype(str).str.upper().str.strip()
    sp["start"] = pd.to_datetime(sp["start"], errors="coerce").dt.normalize()
    sp["ending"] = pd.to_datetime(sp["ending"], errors="coerce").dt.normalize()
    return sp.groupby("ticker")[["start", "ending"]].apply(lambda g: list(zip(g["start"], g["ending"]))).to_dict()


def _in_sp500(lookup, ticker, date):
    for start, end in lookup.get(str(ticker).upper(), []):
        if start <= pd.Timestamp(date) <= end:
            return True
    return False


def senate_buckets(senate_rows, days):
    """One row per Senate treated cell, tagged with salience and moderator buckets.

    Buckets: committee relevance, disclosed trade size, filing freshness, party,
    member notoriety, election timing, and S&P 500 membership. These drive the
    salience splits and the additional moderator table.
    """
    cells = collapse_cells(senate_rows)

    # Committee relevance: did a Senate purchase fall in the senator's committee
    # jurisdiction? Matched on ticker and filing date, then collapsed to the cell.
    committee_path = LICENSED_DATA / "ticker_day_match_senate.parquet"
    if committee_path.exists():
        committee = pd.read_parquet(committee_path)
        committee["ticker"] = committee["ticker"].astype(str).str.upper().str.strip()
        committee["filed_date"] = pd.to_datetime(committee["filed_date"], errors="coerce").dt.normalize()
        rows = senate_rows[["ticker", "filed_date", "date"]].drop_duplicates()
        rows = rows.merge(committee, on=["ticker", "filed_date"], how="left")
        match = rows.groupby(["ticker", "date"], as_index=False).agg(
            committee_match=("committee_match", lambda s: pd.NA if s.dropna().empty else int(s.dropna().max())),
            naics_2=("naics_2", lambda s: str(s.dropna().iloc[0]) if not s.dropna().empty else pd.NA),
        )
        cells = cells.merge(match, on=["ticker", "date"], how="left")
    else:
        cells["committee_match"] = pd.NA
        cells["naics_2"] = pd.NA

    cells["committee_match"] = cells["committee_match"].astype("Int64")
    is_match = cells["committee_match"].eq(1).fillna(False).to_numpy(dtype=bool)
    is_nonmatch = cells["committee_match"].eq(0).fillna(False).to_numpy(dtype=bool)
    cells["committee"] = np.select([is_match, is_nonmatch], ["committee", "noncommittee"], default="unclassified")

    cells["size_bucket"] = np.select(
        [cells["size_max"].le(15_000),
         cells["size_max"].gt(15_000) & cells["size_max"].le(100_000),
         cells["size_max"].gt(100_000)],
        ["small", "mid", "large"], default="unknown")

    cells["freshness"] = np.where(cells["lag_days_min"].le(14), "fresh", "stale")
    cells.loc[cells["lag_days_min"].isna(), "freshness"] = "unknown"

    cells["party_bucket"] = np.select(
        [cells["dem_n"].gt(0) & cells["rep_n"].eq(0),
         cells["rep_n"].gt(0) & cells["dem_n"].eq(0),
         cells["dem_n"].gt(0) & cells["rep_n"].gt(0)],
        ["democrat", "republican", "both"], default="unknown")

    cells["notoriety"] = np.where(cells["top_member_n"].gt(0), "top_member", "other")

    election_dates = {d + pd.Timedelta(days=o) for d in ELECTION_DATES for o in range(-60, 61)}
    cells["election_bucket"] = np.where(cells["date"].isin(election_dates), "election", "nonelection")

    sp500 = _sp500_lookup()
    cells["sp500_bucket"] = ["sp500" if _in_sp500(sp500, t, d) else "nonsp500"
                             for t, d in zip(cells["ticker"], cells["date"])]
    return cells


def run_split(panel, panel_index, cells, days, split_col, categories, deps):
    """Run a salience or moderator split.

    For each category we build a treated-cell dummy over the [0,+1] window, then
    put all category dummies into one regression per dependent variable. `deps`
    is a list of (column, drop_zero_retail) pairs.
    """
    data = panel.copy()
    used = []
    for category in categories:
        subset = cells[cells[split_col].eq(category)]
        if subset.empty:
            continue
        data[f"f_{category}"] = event_flag(panel_index, subset, days, window=1)
        used.append(category)

    rows = []
    for dep, drop_zero in deps:
        result = run_panel(data, dep, [f"f_{c}" for c in used], drop_zero_retail=drop_zero)
        for category in used:
            term = f"f_{category}"
            rows.append({
                "split": split_col, "category": category, "outcome": dep,
                "treated_rows": int(data[term].sum()),
                "coef": round(result.params[term], 6),
                "p": round(result.pvalues[term], 4),
                "coef_bps": round(result.params[term] * 10_000, 2),
            })
    return pd.DataFrame(rows)


def _trailing(group_series, window, gap, min_periods=10):
    """Per-ticker trailing average ending `gap` days before today."""
    return group_series.transform(lambda x: x.shift(gap).rolling(window, min_periods=min(min_periods, window)).mean())


def add_orderflow_extras(panel):
    """Add the extra order-flow columns used only in the appendix robustness tables.

    Three groups: (a) OIB with different pre-disclosure baselines, (b) abnormal
    signed-activity shares, and (c) net buying in dollars scaled by normal volume.
    """
    data = panel.sort_values(["ticker", "date"]).copy()
    grp = data.groupby("ticker", sort=False)

    # (a) OIB baseline variants (window, gap).
    for suffix, window, gap in [("w20_g6", 20, 6), ("w10_g5", 10, 5), ("w30_g5", 30, 5)]:
        data[f"abn_retail_oib_{suffix}"] = data["retail_oib"] - _trailing(grp["retail_oib"], window, gap)
        data[f"abn_nonretail_oib_{suffix}"] = data["nonretail_oib"] - _trailing(grp["nonretail_oib"], window, gap)

    # (b) Signed-activity shares, then their abnormal (baseline-removed) versions.
    denom = data["dollar_volume_day"].replace(0, np.nan)
    data["retail_buy_share"] = data["retail_buy_dollar"].clip(lower=0) / denom
    data["retail_sell_share"] = data["retail_sell_dollar"].abs() / denom
    data["unsigned_retail_share"] = data["unsigned_retail_dollar"].clip(lower=0) / denom
    data["nonretail_buy_share"] = data["nonretail_buy_dollar"].clip(lower=0) / denom
    data["nonretail_sell_share"] = data["nonretail_sell_dollar"].abs() / denom
    activity_vars = ["retail_buy_share", "retail_sell_share", "unsigned_retail_share",
                     "retail_dollar_share", "nonretail_buy_share", "nonretail_sell_share",
                     "nonretail_dollar_share"]
    grp = data.groupby("ticker", sort=False)
    for var in activity_vars:
        data[f"abn_{var}"] = data[var] - _trailing(grp[var], 20, 5)

    # (c) Net signed buying in dollars scaled by normal daily dollar volume.
    norm_vol = _trailing(grp["dollar_volume_day"], 20, 5).replace(0, np.nan)
    for group, col in [("retail", "retail_net_dollar"), ("nonretail", "nonretail_net_dollar")]:
        net_base = _trailing(grp[col], 20, 5)
        data[f"abn_{group}_netbuy_scaled"] = (data[col] - net_base) / norm_vol
    return data


# --- Committee relevance (used by notebook 03) ---------------------------

# Start/end of each Congress, used to pick the right committee roster.
CONGRESS_BOUNDS = [
    (115, pd.Timestamp("2017-01-03"), pd.Timestamp("2019-01-03")),
    (116, pd.Timestamp("2019-01-03"), pd.Timestamp("2021-01-03")),
    (117, pd.Timestamp("2021-01-03"), pd.Timestamp("2023-01-03")),
    (118, pd.Timestamp("2023-01-03"), pd.Timestamp("2025-01-03")),
]


def _congress_for_date(d):
    """Return the Congress number that was sitting on date `d`."""
    for congress, start, end in CONGRESS_BOUNDS:
        if start <= pd.Timestamp(d) < end:
            return congress
    return None


def load_committee_memberships():
    """Senate committee rosters (one row per senator-committee-Congress)."""
    memb = pd.read_parquet(PUBLIC_DATA / "committee_membership_long_senate.parquet")
    memb["bioguide_id"] = memb["bioguide_id"].astype(str).str.upper().str.strip()
    return memb


def load_ticker_naics():
    """Map each ticker to its two-digit NAICS sector (from Compustat)."""
    naics = pd.read_parquet(LICENSED_DATA / "ticker_naics2.parquet")[["ticker", "naics_2"]]
    naics["ticker"] = naics["ticker"].astype(str).str.upper().str.strip()
    return naics


def load_senate_naics_map():
    """Map each Senate committee name to the NAICS sectors it oversees."""
    import yaml
    with open(PUBLIC_DATA / "senate_committee_naics_map.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)["committees"]
    return {name.strip(): {str(code) for code in codes} for name, codes in raw.items()}


def committee_mapping_table():
    """Return the committee-to-NAICS mapping for both chambers as a table."""
    import yaml
    rows = []
    sources = [("House", PUBLIC_DATA / "committee_naics_map.yaml"),
               ("Senate", PUBLIC_DATA / "senate_committee_naics_map.yaml")]
    for chamber, path in sources:
        with open(path, encoding="utf-8") as f:
            committees = yaml.safe_load(f)["committees"]
        for name, codes in committees.items():
            text = ", ".join(str(c) for c in codes) if codes else "Not mapped"
            rows.append({"chamber": chamber, "committee": str(name), "naics_2_codes": text})
    return pd.DataFrame(rows)


def build_committee_match(senate_rows):
    """Tag each Senate purchase by committee relevance, then collapse to cells.

    A purchase is a `committee_match` when the senator sat on a committee whose
    jurisdiction (Senate Rule XXV) covers the stock's two-digit NAICS sector.
    Result is one row per ticker and filing day with `committee_match` (1, 0, or
    missing) and `naics_2`. This rebuilds `ticker_day_match_senate.parquet`.
    """
    memb = load_committee_memberships()
    naics = load_ticker_naics()
    cmap = load_senate_naics_map()

    df = senate_rows[["ticker", "filed_date", "BioGuideID"]].copy()
    df["bioguide_id"] = df["BioGuideID"].astype(str).str.upper().str.strip()
    df["congress"] = df["filed_date"].map(_congress_for_date)
    df = df.dropna(subset=["congress"]).copy()
    df["congress"] = df["congress"].astype(int)
    df = df.merge(naics, on="ticker", how="left")

    roster = memb.groupby(["bioguide_id", "congress"])["committee_name"].agg(set).to_dict()

    def classify(bioguide_id, congress, stock_naics):
        committees = roster.get((bioguide_id, congress), set())
        if not committees or pd.isna(stock_naics):
            return pd.NA
        stock_code = str(stock_naics).strip()
        if stock_code.endswith(".0"):
            stock_code = stock_code[:-2]
        hits = sum(1 for c in committees if stock_code in cmap.get(c, set()))
        return int(hits > 0)

    df["committee_match"] = [classify(b, c, n) for b, c, n
                             in zip(df["bioguide_id"], df["congress"], df["naics_2"])]
    df["committee_match"] = df["committee_match"].astype("Int8")

    cells = df.groupby(["ticker", "filed_date"], as_index=False).agg(
        committee_match=("committee_match", lambda s: pd.NA if s.dropna().empty else int(s.dropna().max())),
        naics_2=("naics_2", "first"),
        n_trades=("committee_match", "size"),
    )
    cells["committee_match"] = cells["committee_match"].astype("Int8")
    return cells


# --- Building the full analysis panel (used by notebook 00) --------------

def _rolling_baseline(series, window, gap, min_periods=10):
    """Trailing average over a window that ends `gap` days before today."""
    return series.shift(gap).rolling(window, min_periods=min(min_periods, window)).mean()


def build_panel():
    """Build the full ticker-day analysis panel from the raw licensed files.

    Returns three things:
      - panel: the regression panel with returns, controls and order-flow columns
      - sample_table: the sample-construction funnel (pooled)
      - senate_table: the Senate event-sample funnel
    """
    # 1. Raw TAQ ticker-day panel. Dates are stored day-first (dd/mm/yyyy).
    taq = pd.read_csv(LICENSED_DATA / "ticker_day_panel_analysis.csv", low_memory=False)
    taq = taq.loc[:, ~taq.columns.str.startswith("Unnamed")]
    taq["ticker"] = taq["ticker"].astype(str).str.upper().str.strip()
    taq["date"] = pd.to_datetime(taq["date"], dayfirst=True, errors="coerce").dt.normalize()
    taq["nbbo_match_rate"] = taq["matched_trade_n_day"] / taq["n_trades_day"].replace(0, np.nan)
    taq["signed_retail_dollar"] = taq["retail_buy_dollar"].fillna(0) + taq["retail_sell_dollar"].fillna(0)
    days = trading_days(taq)

    # 2. Quiver purchase disclosures, pooled and Senate-only.
    quiver = load_quiver()
    pooled_cells = collapse_cells(purchase_events(quiver, "pooled", days))
    senate_cells = collapse_cells(purchase_events(quiver, "senate", days))

    # 3. CRSP returns and share codes (keep the largest match per ticker-day).
    crsp = pd.read_csv(
        LICENSED_DATA / "crsp_dsf_common_2018_2024.csv",
        usecols=["permno", "date", "ticker", "shrcd", "exchcd", "ret", "mktcap_musd"],
        low_memory=False,
    )
    crsp["ticker"] = crsp["ticker"].astype(str).str.upper().str.strip()
    crsp["date"] = pd.to_datetime(crsp["date"], errors="coerce").dt.normalize()
    crsp["ret"] = pd.to_numeric(crsp["ret"], errors="coerce")
    crsp["mktcap_musd"] = pd.to_numeric(crsp["mktcap_musd"], errors="coerce")
    crsp = (crsp.sort_values("mktcap_musd", ascending=False)
                .drop_duplicates(["ticker", "date"], keep="first")
                .rename(columns={"ret": "crsp_ret"}))
    panel = taq.merge(crsp, on=["ticker", "date"], how="left")

    # 4. Apply the activity, NBBO and common-stock filters.
    panel = panel[(panel["n_trades_day"] >= 20) & (panel["nbbo_match_rate"] >= 0.50)]
    panel = panel[panel["shrcd"].eq(11)].copy()

    # 5. Earnings, FOMC and election day flags.
    earn = pd.read_csv(LICENSED_DATA / "earnings_dates.csv", usecols=["permno", "ticker", "announce_date"])
    earn["ticker"] = earn["ticker"].astype(str).str.upper().str.strip()
    earn["announce_date"] = pd.to_datetime(earn["announce_date"], errors="coerce").dt.normalize()
    windows = pd.concat([earn.assign(date=earn["announce_date"] + pd.Timedelta(days=d)) for d in (-1, 0, 1)])
    win_permno = windows.dropna(subset=["permno"])[["permno", "date"]].drop_duplicates().assign(e1=1)
    win_ticker = windows[["ticker", "date"]].drop_duplicates().assign(e2=1)
    panel = panel.merge(win_permno, on=["permno", "date"], how="left")
    panel = panel.merge(win_ticker, on=["ticker", "date"], how="left")
    panel["earnings_window"] = ((panel["e1"].fillna(0) > 0) | (panel["e2"].fillna(0) > 0)).astype("int8")
    panel = panel.drop(columns=["e1", "e2"])

    fomc = pd.read_csv(PUBLIC_DATA / "fomc_dates.csv")
    fomc["announcement_date"] = pd.to_datetime(fomc["announcement_date"], dayfirst=True, errors="coerce").dt.normalize()
    panel_days = trading_days(panel)
    fomc_days = set()
    for day in fomc["announcement_date"].dropna():
        i = panel_days.searchsorted(day, side="left")
        for step in range(-3, 2):
            j = i + step
            if 0 <= j < len(panel_days):
                fomc_days.add(pd.Timestamp(panel_days[j]))
    panel["fomc_window"] = panel["date"].isin(fomc_days).astype("int8")

    election_days = {d + pd.Timedelta(days=s) for d in ELECTION_DATES for s in range(-60, 61)}
    panel["election_window"] = panel["date"].isin(election_days).astype("int8")

    # 6. Control variables and abnormal order-flow measures.
    panel = panel.sort_values(["ticker", "date"]).reset_index(drop=True)
    grp = panel.groupby("ticker", sort=False)
    panel["taq_ret"] = grp["price_close"].pct_change()
    panel["log_dvol"] = np.log(panel["dollar_volume_day"].replace(0, np.nan))
    panel["log_mktcap"] = np.log(panel["mktcap_musd"].replace(0, np.nan))
    panel["amihud"] = panel["taq_ret"].abs() / panel["dollar_volume_day"].replace(0, np.nan) * 1e9
    panel["lag_ret_1d"] = grp["taq_ret"].shift(1)
    panel["lag_logdvol_1d"] = grp["log_dvol"].shift(1)
    panel["lag_amihud_5d"] = grp["amihud"].transform(lambda x: x.shift(1).rolling(5, min_periods=3).mean())
    panel["lag_rvol_5d"] = grp["realized_vol_1min"].transform(lambda x: x.shift(1).rolling(5, min_periods=3).mean())
    panel["abn_retail_oib"] = panel["retail_oib"] - grp["retail_oib"].transform(lambda x: _rolling_baseline(x, 20, 5))
    panel["abn_nonretail_oib"] = panel["nonretail_oib"] - grp["nonretail_oib"].transform(lambda x: _rolling_baseline(x, 20, 5))

    # 7. Sample-construction funnel (pooled) and Senate event funnel.
    panel_set = set(zip(panel["ticker"], panel["date"]))
    taq_set = set(zip(taq["ticker"], taq["date"]))

    keep = quiver["Filed"].between(pd.Timestamp(START), pd.Timestamp(END))
    keep &= quiver["Transaction_norm"].eq("PURCHASE")
    keep &= ~quiver["handfiled"]
    keep &= ~quiver["TickerType_norm"].isin(NON_EQUITY_TT)
    keep &= quiver["Ticker"].notna() & quiver["Ticker"].ne("NAN") & quiver["Ticker"].str.len().gt(0)
    purchases = quiver[keep]
    cells_in_taq = pooled_cells[[p in taq_set for p in zip(pooled_cells["ticker"], pooled_cells["date"])]]
    pooled_treated = pooled_cells[[p in panel_set for p in zip(pooled_cells["ticker"], pooled_cells["date"])]]

    sample_table = pd.DataFrame([
        ("Raw Quiver disclosures (all dates, all asset types)", len(quiver), quiver["Ticker"].nunique()),
        (f"Filed {START} to {END}", int(quiver["Filed"].between(pd.Timestamp(START), pd.Timestamp(END)).sum()),
         quiver.loc[quiver["Filed"].between(pd.Timestamp(START), pd.Timestamp(END)), "Ticker"].nunique()),
        ("Qualifying purchase rows after filters", len(purchases), purchases["Ticker"].nunique()),
        ("Ticker-day cells after mapping Filed to next session", len(pooled_cells), pooled_cells["ticker"].nunique()),
        ("Cells that intersect the raw TAQ panel", len(cells_in_taq), cells_in_taq["ticker"].nunique()),
        ("Raw TAQ panel ticker-days", len(taq), taq["ticker"].nunique()),
        ("Final filtered TAQ/CRSP panel ticker-days", len(panel), panel["ticker"].nunique()),
        ("Of which treated purchase ticker-days", len(pooled_treated), pooled_treated["ticker"].nunique()),
    ], columns=["step", "observations", "tickers"])

    senate_keep = keep & quiver["Chamber_norm"].eq("SENATE")
    senate_treated = senate_cells[[p in panel_set for p in zip(senate_cells["ticker"], senate_cells["date"])]]
    senate_table = pd.DataFrame([
        ("Qualifying purchase rows after filters", len(purchases), purchases["Ticker"].nunique()),
        ("Senate purchase rows within this universe", int(senate_keep.sum()), quiver.loc[senate_keep, "Ticker"].nunique()),
        ("Senate ticker-event cells after filing-date mapping", len(senate_cells), senate_cells["ticker"].nunique()),
        ("Senate treated ticker-days in the final panel", len(senate_treated), senate_treated["ticker"].nunique()),
        ("Final filtered TAQ/CRSP panel ticker-days", len(panel), panel["ticker"].nunique()),
    ], columns=["step", "observations", "tickers"])

    return panel, sample_table, senate_table
