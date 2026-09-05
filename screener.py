"""screener.py — a real candidate universe, from the Robinhood scanner.

Before this module existed, `research.candidates()`'s only source of names
beyond held positions and the day's biggest movers was `SECTOR_MAP`: a
hand-typed, 31-ticker dictionary. That is why one idea cleared the
five-condition gate in four real sessions (1-4 September 2026) -- the gate
was working correctly on an input that barely varied.

The Robinhood connector has carried a real screener the whole time
(`get_scanner_filter_specs`, `create_scan`, `update_scan_filters`,
`run_scan`, `get_scans`) that nothing in this repository had ever called.
This module defines, in code, the filters that describe the universe this
system is willing to consider -- not a list of tickers, a list of
conditions a ticker must satisfy. A real scan (`CORE_UNIVERSE_SCAN_TITLE`)
was created from `CORE_UNIVERSE_FILTERS_V1` on 5 September 2026 and
matched 397 real instruments; `fixtures/scanner/run_scan_core_universe_v1_20260905.json`
is its actual, live `run_scan` response, not a hand-built fixture. **Its
scan_id is not recorded in this repository** -- like an account number, it
identifies a real object on a real account and lives only in
`HANDOFF.private.md`'s config table (`state["config"]["screener_scan_id"]`);
`get_scans` and match on `CORE_UNIVERSE_SCAN_TITLE` if it is ever lost.

**Things this module deliberately does NOT do:**

1. It does not restrict the scan to named tickers or index membership. No
   enum filter can do that (confirmed against `get_scanner_filter_specs`'s
   own guide text); the only way is an expression filter on the symbol
   field, which requires `get_scanner_datapoints` to build correctly and
   `preview_scan` to validate before saving -- **neither tool was available
   in this session**, despite both being referenced by `create_scan`'s and
   `update_scan_filters`'s own docstrings. Expression filters are avoided
   here entirely as a result; every filter in `CORE_UNIVERSE_FILTERS_V1` is
   enum-based, fully covered by the real `get_scanner_filter_specs`
   response recorded in `fixtures/scanner/get_scanner_filter_specs_20260905.json`.
2. It does not bake an account-size-dependent price ceiling into the saved
   scan. A scan's filters are a versioned, occasionally-revised definition,
   not something rewritten every morning to chase a moving account-equity
   target -- and the individual account ($42k+) and the agentic account
   (under $1k) have wildly different affordability ceilings from the same
   universe. Price is carried as a visible `Last` column instead (already a
   standard column on this scan), and `affordable_for_agentic` filters live
   scan results against whichever account's current equity and weight cap
   actually apply, computed fresh every run -- the same reasoning
   `quantcore.sector_exposure` uses for why it needs no cache of its own.
3. It does not attempt to decode the scan results' `Sector` column.
   Confirmed live, 5 September 2026: that column's real values are opaque
   numeric codes ("311", "206", "103", ...), not the human-readable
   `FILTER_TYPE_SECTOR` vocabulary the filter itself uses -- Robinhood
   Ardagh Metal Packaging comes back `"Sector": "102"`, UP Fintech comes
   back `"103"`, with no lookup table available in this session to decode
   either. `parse_scan_result` passes the raw code through unlabelled
   rather than guess at what it means; sector labelling for HELD positions
   still comes from `research.sector_from_company_overview`/
   `sector_from_etf_profile` (a different, already-decoded vocabulary),
   never from this column.
"""

from __future__ import annotations

from typing import Optional, Sequence

# --------------------------------------------------------------------------
# filters -- verified against the real get_scanner_filter_specs response,
# fixtures/scanner/get_scanner_filter_specs_20260905.json, 5 September 2026,
# and against the real filters_applied echoed back by a live create_scan/
# update_scan_filters call (fixtures/scanner/run_scan_core_universe_v1_20260905.json).
# Never hand-guessed; every filter_type/predicate/interval/length pair below
# is copied from that fixture's supported_predicates/supported_intervals/
# supported_lengths, and get_scanner_filter_specs is the tool's own
# documented authority on what is valid ("do not guess filter_type names").
# --------------------------------------------------------------------------

# Liquidity: this account cannot afford to move the market it is trying to
# exit. 500,000 shares/day is comfortably above what a sub-six-figure
# account's largest plausible position could represent, while still
# excluding genuinely illiquid names a resting stop could gap through.
LIQUIDITY_AVG_VOLUME_FLOOR = "500000"
LIQUIDITY_AVG_VOLUME_INTERVAL = "1d"
LIQUIDITY_AVG_VOLUME_LENGTH = 30

# Volatility band a stop can actually be sized against. Below the floor, a
# name barely moves and wastes a risk-budget slot for no edge; above the
# ceiling, `quantcore.stop_plan`'s floored/capped stop machinery and the
# whole-share/weight-cap rules already reject almost everything in
# practice (see PROCEDURE_RATIONALE.md's stop_quality_conflation entry, 2
# September 2026) -- screening it out here is the same judgment applied
# before research is spent, not after. 15%-80% annualised historical
# volatility brackets the real range seen across this system's actual
# candidates to date (OXY 35.65%, XOM 29.71%, GLDM ~25-26%).
VOLATILITY_FLOOR = "0.15"
VOLATILITY_CEILING = "0.80"

# Price floor: the first real run of this scan, sorted cheapest-first with
# no floor, surfaced sub-$0.35 names (PASW, PHGE, ITP, ...) that cleared
# the liquidity and volatility bands anyway -- confirmed live 5 September
# 2026. $5 is a conventional penny-stock line, not a load-bearing risk
# calculation; it exists to keep the scan's cheap end populated with real,
# recognisable names (Under Armour, Fossil, Melco all clear it) rather
# than speculative micro-caps the five-condition gate was never designed
# to reason about.
PRICE_FLOOR = "5"

# Stocks and exchange-traded funds only, matching config.asset_scope
# exactly -- no options, crypto, or futures.
CORE_INSTRUMENT_TYPES = ("STOCK", "ETF")

CORE_UNIVERSE_FILTERS_V1: list[dict] = [
    {"filter_type": "FILTER_TYPE_INSTRUMENT_TYPE", "predicate": "ANY_OF",
     "values": list(CORE_INSTRUMENT_TYPES)},
    {"filter_type": "FILTER_TYPE_AVERAGE_VOLUME", "predicate": ">",
     "values": [LIQUIDITY_AVG_VOLUME_FLOOR],
     "interval": LIQUIDITY_AVG_VOLUME_INTERVAL, "length": LIQUIDITY_AVG_VOLUME_LENGTH},
    {"filter_type": "FILTER_TYPE_HISTORICAL_VOLATILITY", "predicate": "BETWEEN",
     "values": [VOLATILITY_FLOOR, VOLATILITY_CEILING]},
    {"filter_type": "FILTER_TYPE_LAST", "predicate": ">", "values": [PRICE_FLOOR]},
]

CORE_UNIVERSE_SCAN_TITLE = "PBS Core Universe v1"

# Real Robinhood FILTER_TYPE_SECTOR options (get_scanner_filter_specs,
# verified live 5 September 2026) mapped from the GICS-style sector names
# Alpha Vantage's COMPANY_OVERVIEW/ETF_PROFILE return (also verified live
# the same day: COMPANY_OVERVIEW gives "TECHNOLOGY" for AAPL). The two
# providers do not share one vocabulary -- this is a best-effort bridge
# between them, not independently verified category-by-category beyond the
# one live example, and worth widening the moment a real held/candidate
# sector fails to find an entry here.
ALPHA_VANTAGE_TO_ROBINHOOD_SECTOR: dict[str, str] = {
    "technology": "Technology",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_discretionary": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "consumer_staples": "Consumer Defensive",
    "financial_services": "Financial Services",
    "financials": "Financial Services",
    "healthcare": "Healthcare",
    "health_care": "Healthcare",
    "energy": "Energy",
    "industrials": "Industrials",
    "basic_materials": "Basic Materials",
    "materials": "Basic Materials",
    "real_estate": "Real Estate",
    "utilities": "Utilities",
    "communication_services": "Communication Services",
}


def robinhood_sectors_for(sectors: Sequence[Optional[str]]) -> list[str]:
    """Map research.py's lower_snake_case sector labels to the real
    Robinhood FILTER_TYPE_SECTOR vocabulary, dropping anything with no
    mapping (a diversified fund's `None`, or a label this bridge does not
    yet cover) rather than guessing or raising -- a sector this cannot map
    simply does not widen the scan that run, it does not break it."""
    out = []
    for s in sectors:
        if not s:
            continue
        mapped = ALPHA_VANTAGE_TO_ROBINHOOD_SECTOR.get(s.lower())
        if mapped:
            out.append(mapped)
    return sorted(set(out))


def build_filters(held_sectors: Sequence[Optional[str]]) -> list[dict]:
    """`CORE_UNIVERSE_FILTERS_V1` plus a `FILTER_TYPE_SECTOR` ANY_OF filter
    scoped to the real sectors of currently-held positions -- this is what
    replaces `SECTOR_MAP`'s old sector-mate expansion. `update_scan_filters`
    uses REPLACE semantics (the complete filter set every call), so this
    function always returns the complete intended set, never a delta.

    Returns just `CORE_UNIVERSE_FILTERS_V1` when no held sector maps to a
    real Robinhood sector (nothing held yet, or every held sector is an
    unmapped diversified fund) -- a scan with no sector filter still screens
    on liquidity, volatility, price, and instrument type; it is a wider net,
    not a broken one.
    """
    filters = list(CORE_UNIVERSE_FILTERS_V1)
    rh_sectors = robinhood_sectors_for(held_sectors)
    if rh_sectors:
        filters.append({"filter_type": "FILTER_TYPE_SECTOR", "predicate": "ANY_OF",
                        "values": rh_sectors})
    return filters


def parse_scan_result(raw: Optional[dict]) -> list[dict]:
    """Symbols and their `Last` price from a real `run_scan` response.

    Real shape, verified live 5 September 2026 -- and NOT what either
    `create_scan`'s or `run_scan`'s own tool description says it would be
    (both describe `id`/`title`/`total`/`instruments`/`sorting`/`filters`;
    the actual top-level keys are `scan_id`, `scan_title`, `total_items`,
    `results`, `sorted_by`, `filters_applied`, `cortex_managed`):
    `{"scan_id":, "scan_title":, "sorted_by":, "filters_applied": [...],
    "total_items": int, "results": [{"ticker":, "instrument_id":,
    "instrument_type":, "columns": {"Last": "<str>", "Sector": "<code>",
    "Symbol":, "Name":, "Average volume":, "Historical volatility":, "%
    Change":, "Net change":, "Volume":, "Asset type":, ...}}]}`. See
    `fixtures/scanner/run_scan_core_universe_v1_20260905.json` for the
    actual recorded response this was built against.

    `columns` values arrive as strings (Robinhood's own numeric
    formatting, not JSON numbers, and "Average volume" arrives in
    scientific notation, e.g. `"2.384658316207e+06"`) -- `Last` and
    `Average volume` are each cast to float here; a value that will not
    cast is dropped rather than crashing the whole scan, the same "one bad
    row must not take down the rest" rule `research.py`'s parsers already
    follow. `avg_volume` exists so a caller building `research.researched_set`'s
    liquidity tiebreak has it without a second call -- the scan already
    filters on `FILTER_TYPE_AVERAGE_VOLUME`, so this is the same number the
    scan itself used, not a new fetch. `total_items` can exceed
    `len(results)` -- 397 matched the live run that produced the fixture
    above, but `run_scan` returned only the first 200 that morning, sorted
    by whichever column `update_scan_config`'s `sorting_column` last set; a
    scan sorted by `Last desc` returns the WRONG half for the agentic
    account's affordability question (its cheapest, most relevant matches
    would be in the unseen remainder) -- `PBS Core Universe v1` is kept
    sorted `Last asc` for exactly this reason, and any future rename or
    resort of this scan should preserve that.
    """
    if not raw:
        return []
    out = []
    for row in raw.get("results", []):
        ticker = row.get("ticker")
        if not ticker:
            continue
        cols = row.get("columns", {})
        last = cols.get("Last")
        try:
            last = float(last) if last is not None else None
        except (TypeError, ValueError):
            last = None
        avg_volume = cols.get("Average volume")
        try:
            avg_volume = float(avg_volume) if avg_volume is not None else None
        except (TypeError, ValueError):
            avg_volume = None
        out.append({"symbol": str(ticker).upper(), "last": last,
                    "avg_volume": avg_volume, "sector_code": cols.get("Sector")})
    return out


def affordable_for_agentic(results: Sequence[dict], *, account_equity: float,
                           max_weight_agentic: float) -> list[str]:
    """Symbols from `parse_scan_result` whose live `Last` price is cheap
    enough that ONE whole share could ever fit `max_weight_agentic` of
    `account_equity` -- the exact rule `quantcore.size_position`'s
    whole-share requirement already enforces downstream, applied here
    BEFORE Stage 1 spends any research on a name the agentic account could
    never hold at all. This is not a substitute for `size_position` (the
    real, risk-budget-derived cap is almost always tighter -- see
    PROCEDURE_RATIONALE.md's stop_quality_conflation entry), only a cheap
    pre-filter against the loosest possible version of the same rule.

    Computed fresh every call from live inputs, never cached -- account
    equity changes daily and a stale ceiling would silently drift.
    """
    if account_equity <= 0 or max_weight_agentic <= 0:
        return []
    ceiling = account_equity * max_weight_agentic
    return sorted({r["symbol"] for r in results
                  if r.get("last") is not None and 0 < r["last"] <= ceiling})
