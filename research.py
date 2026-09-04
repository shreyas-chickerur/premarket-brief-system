"""research.py — deterministic research gathering for Stage 1.

Before this module existed, Stage 1 was one sentence: "research overnight
news, macro events, earnings, and filings by web search." Every other stage
in this system is tested code that returns a value, a sample size, and a
quality flag; this one was improvisation, and the slowest stage on record
because of it. Two runs researched under that sentence are not comparable to
each other, which matters a great deal for a system whose evidence framework
is supposed to be grading a strategy that holds still.

This module does not call any API itself. It takes ALREADY-FETCHED raw
responses (whatever the Alpha Vantage / Robinhood connectors returned) and
turns them into `ResearchItem`s: a value, a source, an as-of timestamp, and a
quality flag from the same vocabulary `quantcore.Estimate` uses. A feed that
failed, or returned nothing usable, propagates as `quality="failed"` and is
reported as unavailable -- never defaulted, never estimated, never quietly
skipped. Keeping every parser a pure function of already-fetched data is what
makes this testable against recorded fixtures rather than the live API, the
same boundary `quantcore.py` draws against DataFrames.

**Rule 1 -- everything must attach to something.** Every item names the
symbol or the macro channel it bears on, plus the mechanism in one clause.
An item that attaches to nothing is dropped before it ever reaches the
brief -- research exists to inform decisions about real holdings and
real candidates, not to describe the market in general.

**Rule 2 -- keep payloads small.** Callers are told to use `datatype=csv`,
`outputsize=compact` wherever the endpoint supports it, matching the
discipline `DAILY_PROCEDURE.md` already imposes elsewhere.

**Rule 3 -- time every feed.** `gather()` records wall-clock milliseconds
per feed on the bundle, because the performance finding in
`runlog.find_optimizations` has nothing to measure otherwise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence

QUALITY = ("ok", "thin", "degraded", "failed")


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------

@dataclass
class ResearchItem:
    """One fact, attached to something, graded for trust.

    `channel` names the kind of thing this is (`"news"`, `"congress_trade"`,
    `"insider_transaction"`, `"earnings_calendar"`, `"macro:CPI"`,
    `"commodity:WTI"`, `"positioning:put_call"`, `"market_status"`, ...).
    `symbol` is the held or candidate symbol this bears on; `None` is only
    valid for a genuinely portfolio-wide or macro-wide channel (e.g. a CPI
    print with no single-symbol attachment), and even then the item must
    still name which held/candidate names it is relevant to via `mechanism`
    -- construction refuses an item with neither.
    """
    channel: str
    symbol: Optional[str]
    mechanism: str          # one clause: how this bears on the symbol/channel
    value: Any
    source: str
    asof: str                # ISO date or datetime string
    quality: str = "ok"
    detail: str = ""

    def __post_init__(self):
        if self.quality not in QUALITY:
            raise ValueError(f"bad quality flag: {self.quality!r}")
        if not self.channel:
            raise ValueError("a research item must name a channel")
        if not self.mechanism and self.quality != "failed":
            raise ValueError(
                "an item must attach to something: state the mechanism, or "
                "drop the item before it reaches the brief")
        if self.symbol is not None:
            self.symbol = self.symbol.upper()

    @property
    def usable(self) -> bool:
        return self.quality != "failed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchBundle:
    """Everything gathered for one run. The morning agent interprets this;
    it does not decide what belongs in it."""
    items: list[ResearchItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # feed name + why
    timings_ms: dict[str, int] = field(default_factory=dict)
    asof: str = ""

    def for_symbol(self, symbol: str) -> list[ResearchItem]:
        s = symbol.upper()
        return [i for i in self.items if i.symbol == s]

    def for_channel(self, channel: str) -> list[ResearchItem]:
        return [i for i in self.items if i.channel == channel or
                i.channel.startswith(channel + ":")]

    def sources_for(self, symbol: str, *, channel_prefix: str = "news") -> set[str]:
        """Distinct sources among the news-type items attached to `symbol`.
        This is what makes "two independent corroborating sources" (the
        five-condition gate's requirement) a mechanical count instead of a
        judgment call."""
        s = symbol.upper()
        return {i.source for i in self.items
                if i.symbol == s and i.channel.startswith(channel_prefix)
                and i.usable}

    def corroborated(self, symbol: str, *, minimum: int = 2) -> bool:
        return len(self.sources_for(symbol)) >= minimum

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "skipped": list(self.skipped),
            "timings_ms": dict(self.timings_ms),
            "asof": self.asof,
        }


# --------------------------------------------------------------------------
# candidate generation -- the other half of an undefined research process
# --------------------------------------------------------------------------

# Same data-as-code pattern as washsale.PROXY_GROUPS: a hardcoded reference
# table, not a live lookup, because the alternative (an unstated, ad hoc
# notion of "sector") is exactly the kind of undefined universe this
# function exists to replace. Extend as new symbols are actually held.
SECTOR_MAP: dict[str, str] = {
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    "HAL": "energy", "OXY": "energy", "XLE": "energy", "USO": "energy",
    "VDE": "energy", "XOP": "energy", "OIH": "energy",
    "AAPL": "technology", "MSFT": "technology", "GOOGL": "technology",
    "META": "technology", "NVDA": "technology", "CRWD": "technology",
    "AMZN": "consumer_discretionary", "WMT": "consumer_staples",
    "SPGI": "financials", "JPM": "financials", "IBM": "technology",
    "GLDM": "commodities", "GLD": "commodities", "IAU": "commodities",
    "SGOV": "cash_equivalent", "VGSH": "cash_equivalent",
    "VTI": "broad_market", "VOO": "broad_market", "SPY": "broad_market",
    "VXUS": "international",
}


def candidates(*, held_symbols: Sequence[str],
                watchlist_symbols: Sequence[str] = (),
                top_movers: Sequence[str] = (),
                sector_map: dict[str, str] = None) -> list[str]:
    """The candidate universe, defined once, in code, rather than left to
    whatever a morning happens to think of.

    Four sources, unioned and deduplicated:
    1. Held positions, both accounts — always candidates for sizing or exit
       review, not just new entries.
    2. `state.json.config.watchlist` (if present) — an explicit,
       human-curated list.
    3. Today's `TOP_GAINERS_LOSERS` — unusual dispersion is exactly what a
       five-condition gate might catch, and what the old unstructured
       research step had no systematic way of noticing.
    4. Names sharing a sector (via `sector_map`, default `SECTOR_MAP`) with
       a held position — adjacency to existing exposure, not a blind screen.
    """
    sector_map = SECTOR_MAP if sector_map is None else sector_map
    held = {s.upper() for s in held_symbols}
    out = set(held) | {s.upper() for s in watchlist_symbols} | {s.upper() for s in top_movers}

    held_sectors = {sector_map[s] for s in held if s in sector_map}
    for sym, sec in sector_map.items():
        if sec in held_sectors:
            out.add(sym)

    return sorted(out)


# --------------------------------------------------------------------------
# weather -- the one path, explicit and testable, never a general narrative
# --------------------------------------------------------------------------

# symbol -> (weather_variable, mechanism). This IS the only path by which
# weather may enter a decision -- a symbol not listed here gets no weather
# item, ever, regardless of how newsworthy the weather is generally.
WEATHER_MAP: dict[str, tuple[str, str]] = {
    "XOM": ("heating_degree_days", "refiner and heating-fuel demand rises with cold snaps"),
    "CVX": ("heating_degree_days", "refiner and heating-fuel demand rises with cold snaps"),
    "NATGAS-exposed": ("heating_degree_days", "placeholder key, replace with real natgas-levered symbols as held"),
    "WMT": ("named_storms", "regional storm exposure disrupts retail foot traffic and supply chains"),
    "DE": ("drought_index", "agricultural equipment demand tracks planting-season conditions"),
    "CORN": ("precipitation", "row-crop yields track growing-season rainfall"),
    "WHEAT": ("precipitation", "wheat yields track growing-season rainfall"),
    "COTTON": ("drought_index", "cotton is unusually drought-sensitive relative to other row crops"),
    "SUGAR": ("precipitation", "cane yields track growing-season rainfall in producing regions"),
    "COFFEE": ("named_storms", "frost and storm events in producing regions move futures sharply"),
}


def weather_items(symbols: Sequence[str], weather_by_variable: dict[str, dict],
                   *, source: str = "weather-mapping", asof: str = "") -> list[ResearchItem]:
    """Build weather items ONLY for symbols in `WEATHER_MAP`, using values
    already fetched into `weather_by_variable` (keyed by the same variable
    names `WEATHER_MAP` uses). A symbol not in the map gets nothing -- no
    general weather narrative reaches the brief through any other path."""
    out: list[ResearchItem] = []
    for sym in symbols:
        entry = WEATHER_MAP.get(sym.upper())
        if entry is None:
            continue
        variable, mechanism = entry
        payload = weather_by_variable.get(variable)
        if payload is None:
            out.append(ResearchItem(
                channel="weather", symbol=sym, mechanism=mechanism,
                value=None, source=source, asof=asof, quality="failed",
                detail=f"no data supplied for weather variable {variable!r}"))
            continue
        out.append(ResearchItem(
            channel="weather", symbol=sym, mechanism=mechanism,
            value=payload, source=source, asof=asof, quality="ok"))
    return out


# --------------------------------------------------------------------------
# news and sentiment -- the dual-source corroboration check
# --------------------------------------------------------------------------

def news_items_from_alpha_vantage(raw: Optional[dict], *, symbol: str,
                                   asof: str) -> list[ResearchItem]:
    """`NEWS_SENTIMENT`, already fetched. `raw` is the parsed JSON response
    (or `None`/empty on a failed call)."""
    if not raw or not raw.get("feed"):
        return [ResearchItem(channel="news", symbol=symbol,
                             mechanism="no company/market news with sentiment retrieved",
                             value=None, source="Alpha Vantage NEWS_SENTIMENT",
                             asof=asof, quality="failed",
                             detail="empty or missing feed")]
    out = []
    for entry in raw["feed"]:
        title = entry.get("title", "")
        sentiment = entry.get("overall_sentiment_score")
        out.append(ResearchItem(
            channel="news", symbol=symbol,
            mechanism=f"news item scored for sentiment toward {symbol}",
            value={"title": title, "sentiment_score": sentiment,
                  "sentiment_label": entry.get("overall_sentiment_label")},
            source="Alpha Vantage NEWS_SENTIMENT", asof=asof, quality="ok",
            detail=str(entry.get("time_published", ""))))
    return out


def news_items_from_robinhood(raw: Optional[dict], *, symbol: str,
                               asof: str) -> list[ResearchItem]:
    """Robinhood `get_equity_news`, already fetched — the second,
    independent source `sources_for`/`corroborated` need to make "two
    independent corroborating sources" mechanical rather than a judgment
    call."""
    if not raw or not raw.get("data", {}).get("news"):
        return [ResearchItem(channel="news", symbol=symbol,
                             mechanism="no Robinhood news retrieved",
                             value=None, source="Robinhood get_equity_news",
                             asof=asof, quality="failed", detail="empty result")]
    out = []
    for entry in raw["data"]["news"]:
        out.append(ResearchItem(
            channel="news", symbol=symbol,
            mechanism=f"independent news source for {symbol}",
            value={"title": entry.get("title", "")},
            source="Robinhood get_equity_news", asof=asof, quality="ok",
            detail=str(entry.get("published_at", ""))))
    return out


# --------------------------------------------------------------------------
# congressional and insider activity
# --------------------------------------------------------------------------

def congress_trade_items(raw: Optional[list], politician_meta: Optional[dict], *,
                          held_or_candidate: Sequence[str], asof: str) -> list[ResearchItem]:
    """`CONGRESS_TRADES` joined against `POLITICIAN_METADATA`, filtered to
    `held_or_candidate`. Both already fetched."""
    watch = {s.upper() for s in held_or_candidate}
    if raw is None:
        return [ResearchItem(channel="congress_trade", symbol=None,
                             mechanism="CONGRESS_TRADES unavailable this run",
                             value=None, source="Alpha Vantage CONGRESS_TRADES",
                             asof=asof, quality="failed")]
    out = []
    meta = politician_meta or {}
    for row in raw:
        sym = str(row.get("ticker", "")).upper()
        if sym not in watch:
            continue
        politician = row.get("representative") or row.get("senator") or "unknown"
        bio = meta.get(politician, {})
        out.append(ResearchItem(
            channel="congress_trade", symbol=sym,
            mechanism=f"{politician} ({bio.get('party', 'party unknown')}, "
                     f"{bio.get('committee', 'committee unknown')}) disclosed a trade in {sym}",
            value={"politician": politician, "transaction": row.get("transaction"),
                  "amount": row.get("amount"), "date": row.get("transaction_date")},
            source="Alpha Vantage CONGRESS_TRADES + POLITICIAN_METADATA",
            asof=asof, quality="ok"))
    return out


def insider_transaction_items(raw: Optional[list], *,
                               held_or_candidate: Sequence[str],
                               asof: str) -> list[ResearchItem]:
    """`INSIDER_TRANSACTIONS`, already fetched, filtered the same way as
    congressional trades."""
    watch = {s.upper() for s in held_or_candidate}
    if raw is None:
        return [ResearchItem(channel="insider_transaction", symbol=None,
                             mechanism="INSIDER_TRANSACTIONS unavailable this run",
                             value=None, source="Alpha Vantage INSIDER_TRANSACTIONS",
                             asof=asof, quality="failed")]
    out = []
    for row in raw:
        sym = str(row.get("symbol", "")).upper()
        if sym not in watch:
            continue
        out.append(ResearchItem(
            channel="insider_transaction", symbol=sym,
            mechanism=(f"{row.get('executive', 'insider')} "
                      f"({row.get('executive_title', 'role unknown')}) "
                      f"{row.get('acquisition_or_disposal', 'transacted')} shares"),
            value={"shares": row.get("shares"), "share_price": row.get("share_price"),
                  "date": row.get("transaction_date")},
            source="Alpha Vantage INSIDER_TRANSACTIONS", asof=asof, quality="ok"))
    return out


# --------------------------------------------------------------------------
# scheduled events
# --------------------------------------------------------------------------

def earnings_calendar_items(raw: Optional[list], *,
                             held_or_candidate: Sequence[str],
                             asof: str) -> list[ResearchItem]:
    """`EARNINGS_CALENDAR`, filtered to held/candidate symbols."""
    watch = {s.upper() for s in held_or_candidate}
    if raw is None:
        return [ResearchItem(channel="earnings_calendar", symbol=None,
                             mechanism="EARNINGS_CALENDAR unavailable this run",
                             value=None, source="Alpha Vantage EARNINGS_CALENDAR",
                             asof=asof, quality="failed")]
    out = []
    for row in raw:
        sym = str(row.get("symbol", "")).upper()
        if sym not in watch:
            continue
        out.append(ResearchItem(
            channel="earnings_calendar", symbol=sym,
            mechanism=f"{sym} reports earnings on {row.get('reportDate', 'an unspecified date')}",
            value={"report_date": row.get("reportDate"), "estimate": row.get("estimate")},
            source="Alpha Vantage EARNINGS_CALENDAR", asof=asof, quality="ok"))
    return out


def earnings_estimate_items(raw: Optional[dict], *, symbol: str,
                             asof: str) -> list[ResearchItem]:
    """`EARNINGS_ESTIMATES` for one symbol."""
    if not raw:
        return [ResearchItem(channel="earnings_estimate", symbol=symbol,
                             mechanism="EARNINGS_ESTIMATES unavailable this run",
                             value=None, source="Alpha Vantage EARNINGS_ESTIMATES",
                             asof=asof, quality="failed")]
    return [ResearchItem(
        channel="earnings_estimate", symbol=symbol,
        mechanism=f"consensus estimate context for {symbol}'s next report",
        value=raw, source="Alpha Vantage EARNINGS_ESTIMATES", asof=asof, quality="ok")]


def ipo_calendar_items(raw: Optional[list], *, sector_watch: Sequence[str],
                        asof: str) -> list[ResearchItem]:
    """`IPO_CALENDAR` — attaches only to a named sector already represented
    among held/candidate names, per Rule 1; a new IPO with no such
    connection is dropped, not narrated."""
    if raw is None:
        return [ResearchItem(channel="ipo_calendar", symbol=None,
                             mechanism="IPO_CALENDAR unavailable this run",
                             value=None, source="Alpha Vantage IPO_CALENDAR",
                             asof=asof, quality="failed")]
    watch = {s.lower() for s in sector_watch}
    out = []
    for row in raw:
        sector = str(row.get("sector", "")).lower()
        if sector not in watch:
            continue
        out.append(ResearchItem(
            channel="ipo_calendar", symbol=None,
            mechanism=f"upcoming IPO in the {sector} sector, already represented in the book",
            value=row, source="Alpha Vantage IPO_CALENDAR", asof=asof, quality="ok"))
    return out


def earnings_call_transcript_items(raw: Optional[dict], *, symbol: str,
                                    horizon_reason: str,
                                    asof: str) -> list[ResearchItem]:
    """`EARNINGS_CALL_TRANSCRIPT` for the prior quarter — only called at
    all for a held name reporting within an open thesis's horizon."""
    if not raw:
        return [ResearchItem(channel="earnings_call_transcript", symbol=symbol,
                             mechanism=horizon_reason, value=None,
                             source="Alpha Vantage EARNINGS_CALL_TRANSCRIPT",
                             asof=asof, quality="failed")]
    return [ResearchItem(
        channel="earnings_call_transcript", symbol=symbol,
        mechanism=horizon_reason, value=raw,
        source="Alpha Vantage EARNINGS_CALL_TRANSCRIPT", asof=asof, quality="ok")]


# --------------------------------------------------------------------------
# filings
# --------------------------------------------------------------------------

def filing_items(sec_filing: Optional[dict], sec_facts: Optional[dict], *,
                  symbol: str, asof: str) -> list[ResearchItem]:
    """Robinhood `get_sec_filing` + `get_sec_filing_facts`, for a held name
    with a recent filing."""
    out = []
    if sec_filing:
        out.append(ResearchItem(
            channel="filing", symbol=symbol,
            mechanism=f"recent SEC filing for {symbol}",
            value=sec_filing, source="Robinhood get_sec_filing",
            asof=asof, quality="ok"))
    else:
        out.append(ResearchItem(
            channel="filing", symbol=symbol,
            mechanism=f"no recent SEC filing retrieved for {symbol}",
            value=None, source="Robinhood get_sec_filing",
            asof=asof, quality="failed"))
    if sec_facts:
        out.append(ResearchItem(
            channel="filing_facts", symbol=symbol,
            mechanism=f"structured facts from {symbol}'s recent filing",
            value=sec_facts, source="Robinhood get_sec_filing_facts",
            asof=asof, quality="ok"))
    return out


# --------------------------------------------------------------------------
# macro -- one shape, nine channels
# --------------------------------------------------------------------------

MACRO_CHANNELS = ("TREASURY_YIELD", "FEDERAL_FUNDS_RATE", "CPI", "INFLATION",
                   "UNEMPLOYMENT", "NONFARM_PAYROLL", "RETAIL_SALES",
                   "REAL_GDP", "DURABLES")


def macro_item(channel: str, raw: Optional[dict], *, asof: str) -> ResearchItem:
    """One of the nine macro series, already fetched. Macro items attach to
    the named channel, not a symbol — `mechanism` says which held/candidate
    exposure the channel actually bears on, supplied by the caller since
    only the caller knows which names are in play this run."""
    if channel not in MACRO_CHANNELS:
        raise ValueError(f"unrecognised macro channel: {channel!r}")
    if not raw or not raw.get("data"):
        return ResearchItem(channel=f"macro:{channel}", symbol=None,
                            mechanism=f"{channel} unavailable this run",
                            value=None, source=f"Alpha Vantage {channel}",
                            asof=asof, quality="failed")
    latest = raw["data"][0] if isinstance(raw["data"], list) else raw["data"]
    return ResearchItem(
        channel=f"macro:{channel}", symbol=None,
        mechanism=f"latest {channel} print, standing macro backdrop",
        value=latest, source=f"Alpha Vantage {channel}", asof=asof, quality="ok")


# --------------------------------------------------------------------------
# commodities -- one shape, eleven channels, gated on actual exposure
# --------------------------------------------------------------------------

COMMODITY_CHANNELS = ("WTI", "BRENT", "NATURAL_GAS", "COPPER", "ALUMINUM",
                      "WHEAT", "CORN", "COFFEE", "SUGAR", "COTTON",
                      "ALL_COMMODITIES", "GOLD_SILVER_SPOT")

# symbol -> commodity channel(s) it has real exposure to. Same data-as-code
# pattern as SECTOR_MAP and WEATHER_MAP: a commodity feed is only fetched
# and attached for a symbol actually listed here.
COMMODITY_EXPOSURE: dict[str, tuple[str, ...]] = {
    "XOM": ("WTI", "BRENT", "NATURAL_GAS"), "CVX": ("WTI", "BRENT", "NATURAL_GAS"),
    "COP": ("WTI", "BRENT"), "SLB": ("WTI", "BRENT"), "HAL": ("WTI", "BRENT"),
    "OXY": ("WTI", "BRENT"), "XLE": ("WTI", "BRENT"), "USO": ("WTI",),
    "VDE": ("WTI", "BRENT"), "XOP": ("WTI", "BRENT"), "OIH": ("WTI", "BRENT"),
    "GLDM": ("GOLD_SILVER_SPOT",), "GLD": ("GOLD_SILVER_SPOT",), "IAU": ("GOLD_SILVER_SPOT",),
    "DE": ("CORN", "WHEAT"),
}


def commodity_items(symbols: Sequence[str], raw_by_channel: dict[str, Optional[dict]],
                     *, asof: str) -> list[ResearchItem]:
    """One item per (symbol, channel) pair actually exposed per
    `COMMODITY_EXPOSURE`, using already-fetched data keyed by channel name.
    A symbol with no listed exposure gets nothing — Rule 1 again."""
    out = []
    for sym in symbols:
        for channel in COMMODITY_EXPOSURE.get(sym.upper(), ()):
            if channel not in COMMODITY_CHANNELS:
                raise ValueError(f"unrecognised commodity channel: {channel!r}")
            raw = raw_by_channel.get(channel)
            if not raw or not raw.get("data"):
                out.append(ResearchItem(
                    channel=f"commodity:{channel}", symbol=sym,
                    mechanism=f"{sym} has {channel} exposure but the feed was unavailable",
                    value=None, source=f"Alpha Vantage {channel}",
                    asof=asof, quality="failed"))
                continue
            latest = raw["data"][0] if isinstance(raw["data"], list) else raw["data"]
            out.append(ResearchItem(
                channel=f"commodity:{channel}", symbol=sym,
                mechanism=f"{sym} has direct {channel} price exposure",
                value=latest, source=f"Alpha Vantage {channel}",
                asof=asof, quality="ok"))
    return out


# --------------------------------------------------------------------------
# positioning and session state
# --------------------------------------------------------------------------

def put_call_items(realtime: Optional[dict], historical: Optional[dict], *,
                    symbol: str, asof: str) -> list[ResearchItem]:
    """`REALTIME_PUT_CALL_RATIO` against `HISTORICAL_PUT_CALL_RATIO` for
    context — the historical series is context only, never treated as a
    signal in its own right."""
    out = []
    if realtime and realtime.get("ratio") is not None:
        out.append(ResearchItem(
            channel="positioning:put_call", symbol=symbol,
            mechanism=f"current put/call ratio for {symbol}",
            value=realtime.get("ratio"),
            source="Alpha Vantage REALTIME_PUT_CALL_RATIO",
            asof=asof, quality="ok",
            detail=(f"historical context: {historical.get('ratio')}"
                   if historical and historical.get("ratio") is not None
                   else "no historical context available")))
    else:
        out.append(ResearchItem(
            channel="positioning:put_call", symbol=symbol,
            mechanism=f"put/call positioning for {symbol} unavailable this run",
            value=None, source="Alpha Vantage REALTIME_PUT_CALL_RATIO",
            asof=asof, quality="failed"))
    return out


def top_movers_items(raw: Optional[dict], *, asof: str) -> list[ResearchItem]:
    """`TOP_GAINERS_LOSERS` — attaches to the broad-market channel; also
    feeds `candidates()`'s `top_movers` argument upstream of this call."""
    if not raw:
        return [ResearchItem(channel="positioning:dispersion", symbol=None,
                             mechanism="TOP_GAINERS_LOSERS unavailable this run",
                             value=None, source="Alpha Vantage TOP_GAINERS_LOSERS",
                             asof=asof, quality="failed")]
    return [ResearchItem(
        channel="positioning:dispersion", symbol=None,
        mechanism="today's dispersion — largest gainers and losers, market-wide",
        value=raw, source="Alpha Vantage TOP_GAINERS_LOSERS", asof=asof, quality="ok")]


def market_status_item(raw: Optional[dict], *, asof: str) -> ResearchItem:
    """`MARKET_STATUS` — session state, already required by
    `DAILY_PROCEDURE.md` Stage 0 step 6; recorded here too so it is part of
    the same timed, graded bundle rather than a separate untimed call."""
    if not raw:
        return ResearchItem(channel="market_status", symbol=None,
                            mechanism="MARKET_STATUS unavailable this run",
                            value=None, source="Alpha Vantage MARKET_STATUS",
                            asof=asof, quality="failed")
    return ResearchItem(channel="market_status", symbol=None,
                        mechanism="session state for today",
                        value=raw, source="Alpha Vantage MARKET_STATUS",
                        asof=asof, quality="ok")


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def gather(raw_feeds: dict[str, Any], *, held_or_candidate: Sequence[str],
           asof: Optional[str] = None,
           timer: Callable[[], float] = time.monotonic) -> ResearchBundle:
    """Assemble a `ResearchBundle` from already-fetched raw responses.

    `raw_feeds` keys (all optional; a missing key is treated as "not
    fetched this run" and produces no items for that feed rather than an
    error — the caller decides which feeds to fetch based on what is
    actually held or candidate):

    - `"news_av"`, `"news_rh"`: `{symbol: raw_response}`
    - `"congress_trades"`, `"politician_metadata"`, `"insider_transactions"`
    - `"earnings_calendar"`, `"earnings_estimates"` (`{symbol: raw}`),
      `"ipo_calendar"`
    - `"earnings_call_transcripts"`: `{symbol: (raw, horizon_reason)}`
    - `"sec_filings"`, `"sec_filing_facts"`: `{symbol: raw}`
    - `"macro"`: `{channel: raw}` for any of `MACRO_CHANNELS`
    - `"commodities"`: `{channel: raw}` for any of `COMMODITY_CHANNELS`
    - `"weather"`: `{variable: raw}`
    - `"put_call_realtime"`, `"put_call_historical"`: `{symbol: raw}`
    - `"top_movers"`, `"market_status"`

    Every feed's wall-clock cost is recorded on `timings_ms`, keyed by feed
    name, whether or not it was actually present in `raw_feeds` (a feed
    that was skipped entirely costs 0ms and is recorded in `skipped`, which
    is itself information the performance review can use).
    """
    asof = asof or datetime.utcnow().isoformat()
    bundle = ResearchBundle(asof=asof)
    symbols = list(dict.fromkeys(s.upper() for s in held_or_candidate))

    def timed(name: str, fn):
        t0 = timer()
        try:
            result = fn()
        finally:
            bundle.timings_ms[name] = int((timer() - t0) * 1000)
        return result

    news_av = raw_feeds.get("news_av", {})
    news_rh = raw_feeds.get("news_rh", {})
    for sym in symbols:
        if sym in news_av:
            bundle.items.extend(timed(f"news_av:{sym}",
                lambda sym=sym: news_items_from_alpha_vantage(news_av.get(sym), symbol=sym, asof=asof)))
        else:
            bundle.skipped.append(f"news_av:{sym} — not in raw_feeds")
        if sym in news_rh:
            bundle.items.extend(timed(f"news_rh:{sym}",
                lambda sym=sym: news_items_from_robinhood(news_rh.get(sym), symbol=sym, asof=asof)))
        else:
            bundle.skipped.append(f"news_rh:{sym} — not in raw_feeds")

    if "congress_trades" in raw_feeds:
        bundle.items.extend(timed("congress_trades", lambda: congress_trade_items(
            raw_feeds.get("congress_trades"), raw_feeds.get("politician_metadata"),
            held_or_candidate=symbols, asof=asof)))
    else:
        bundle.skipped.append("congress_trades — not in raw_feeds")

    if "insider_transactions" in raw_feeds:
        bundle.items.extend(timed("insider_transactions", lambda: insider_transaction_items(
            raw_feeds.get("insider_transactions"), held_or_candidate=symbols, asof=asof)))
    else:
        bundle.skipped.append("insider_transactions — not in raw_feeds")

    if "earnings_calendar" in raw_feeds:
        bundle.items.extend(timed("earnings_calendar", lambda: earnings_calendar_items(
            raw_feeds.get("earnings_calendar"), held_or_candidate=symbols, asof=asof)))
    else:
        bundle.skipped.append("earnings_calendar — not in raw_feeds")

    for sym, raw in raw_feeds.get("earnings_estimates", {}).items():
        bundle.items.extend(timed(f"earnings_estimates:{sym}",
            lambda raw=raw, sym=sym: earnings_estimate_items(raw, symbol=sym, asof=asof)))

    if "ipo_calendar" in raw_feeds:
        held_sectors = [SECTOR_MAP[s] for s in symbols if s in SECTOR_MAP]
        bundle.items.extend(timed("ipo_calendar", lambda: ipo_calendar_items(
            raw_feeds.get("ipo_calendar"), sector_watch=held_sectors, asof=asof)))
    else:
        bundle.skipped.append("ipo_calendar — not in raw_feeds")

    for sym, pair in raw_feeds.get("earnings_call_transcripts", {}).items():
        raw, reason = pair
        bundle.items.extend(timed(f"earnings_call_transcript:{sym}",
            lambda raw=raw, reason=reason, sym=sym:
                earnings_call_transcript_items(raw, symbol=sym, horizon_reason=reason, asof=asof)))

    sec_filings = raw_feeds.get("sec_filings", {})
    sec_facts = raw_feeds.get("sec_filing_facts", {})
    for sym in set(sec_filings) | set(sec_facts):
        bundle.items.extend(timed(f"filing:{sym}",
            lambda sym=sym: filing_items(sec_filings.get(sym), sec_facts.get(sym), symbol=sym, asof=asof)))

    for channel, raw in raw_feeds.get("macro", {}).items():
        bundle.items.append(timed(f"macro:{channel}",
            lambda channel=channel, raw=raw: macro_item(channel, raw, asof=asof)))

    commodity_raw = raw_feeds.get("commodities", {})
    if commodity_raw:
        bundle.items.extend(timed("commodities",
            lambda: commodity_items(symbols, commodity_raw, asof=asof)))

    weather_raw = raw_feeds.get("weather")
    if weather_raw is not None:
        bundle.items.extend(timed("weather",
            lambda: weather_items(symbols, weather_raw, asof=asof)))
    else:
        relevant = [s for s in symbols if s in WEATHER_MAP]
        if not relevant:
            bundle.skipped.append("weather — no held/candidate symbol maps to a weather variable")
        else:
            bundle.skipped.append(f"weather — not in raw_feeds (relevant: {relevant})")

    pc_rt = raw_feeds.get("put_call_realtime", {})
    pc_hist = raw_feeds.get("put_call_historical", {})
    for sym in set(pc_rt) | set(pc_hist):
        bundle.items.extend(timed(f"put_call:{sym}",
            lambda sym=sym: put_call_items(pc_rt.get(sym), pc_hist.get(sym), symbol=sym, asof=asof)))

    if "top_movers" in raw_feeds:
        bundle.items.extend(timed("top_movers", lambda: top_movers_items(raw_feeds.get("top_movers"), asof=asof)))
    if "market_status" in raw_feeds:
        bundle.items.append(timed("market_status", lambda: market_status_item(raw_feeds.get("market_status"), asof=asof)))

    return bundle
