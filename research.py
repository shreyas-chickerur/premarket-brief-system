"""research.py — deterministic research gathering for Stage 1.

Before this module existed, Stage 1 was one sentence: "research overnight
news, macro events, earnings, and filings by web search." Every other stage
in this system is tested code that returns a value, a sample size, and a
quality flag; this one was improvisation, and the slowest stage on record
because of it. Two runs researched under that sentence are not comparable to
each other, which matters a great deal for a system whose evidence framework
is supposed to be grading a strategy that holds still.

**4 September 2026 — the first version of this module shipped with parsers
written against hand-invented fixtures, not real responses.** A live check
against three feeds found two parsers reading field names the API does not
use (`ticker` where the response uses `symbol` and vice versa between
`CONGRESS_TRADES` and `INSIDER_TRANSACTIONS`), a bulk-call assumption
`CONGRESS_TRADES` does not support (it requires one symbol or BioGuide ID
per call), and two oversized-payload shapes (a truncated "preview" envelope
and a harness file-spill) neither parser recognised at all. Every parser in
this file has since been rewritten against an actual recorded response — see
`fixtures/research/live_raw/` — and the ones that could not be re-verified
live within the time available are marked as such below, not silently
assumed correct.

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
`outputsize=compact` wherever the endpoint supports it -- **except the nine
macro and eleven commodity channels, where `datatype=json` should be
requested instead** (see `_rows_from_series_response`): their CSV default
returns `{"result": "<CSV text>"}`, a JSON envelope around a CSV string, not
a parseable data shape, while `datatype=json` returns a clean
`{"data": [{"date":, "value":}]}`. `EARNINGS_CALENDAR` and `IPO_CALENDAR`
have no `datatype` parameter at all and always return the CSV-wrapped shape.

**Rule 3 -- time every feed and track its coverage.** `gather()` records
wall-clock milliseconds per feed, and `ResearchBundle.coverage` records rows
seen versus items produced per feed. A feed that returned rows but produced
zero items is a field-name mismatch or a shape change wearing the same face
as a genuinely quiet day -- `ResearchBundle.coverage_issues()` is what tells
them apart.
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence

QUALITY = ("ok", "thin", "degraded", "failed")


class ResearchShapeError(ValueError):
    """A feed's response is missing a key its parser depends on. Raised,
    not filtered past silently: a missing key means the provider's response
    shape drifted from what the parser was written against, exactly the
    class of bug (a wrapper shape, a renamed field) this module has already
    shipped once. `gather()` catches this per-feed and turns it into a
    highly visible `quality="failed"` item rather than letting one feed's
    drift abort the whole run."""


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
    coverage: dict[str, dict] = field(default_factory=dict)  # feed -> {rows_in, items_out}
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

    def record_coverage(self, feed: str, rows_in: int, items_out: int) -> None:
        self.coverage[feed] = {"rows_in": rows_in, "items_out": items_out}

    def coverage_issues(self) -> list[str]:
        """Feeds that saw rows but produced zero items -- the exact shape a
        field-name mismatch takes, indistinguishable from a genuinely quiet
        day unless checked explicitly. This is what would have caught the
        4 September 2026 CONGRESS_TRADES/INSIDER_TRANSACTIONS bugs before a
        live run did."""
        return [feed for feed, c in self.coverage.items()
                if c["rows_in"] > 0 and c["items_out"] == 0]

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "skipped": list(self.skipped),
            "timings_ms": dict(self.timings_ms),
            "coverage": dict(self.coverage),
            "asof": self.asof,
        }


# --------------------------------------------------------------------------
# shape guards and oversized-payload handling
# --------------------------------------------------------------------------

def _is_preview_envelope(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("preview") is True


def _preview_item(channel: str, symbol: Optional[str], source: str, raw: dict,
                   asof: str) -> ResearchItem:
    """A parser handed a preview envelope must never parse `sample_data` as
    though it were the whole response -- it is a truncated, lossy sample.
    Real shape, verified live 4 September 2026 (INSIDER_TRANSACTIONS for
    OXY, 248,328 tokens): `{"preview": true, "data_type":, "total_lines":,
    "sample_lines":, "sample_data": <JSON string>, "headers":,
    "full_data_tokens":, "max_tokens_exceeded": true, "content_type":,
    "message":, "return_full_data_note":, "data_url":}`. Earlier versions
    of this function assumed `data_total_count`/`data_truncated` keys that
    do not exist in the real response -- both always read as None."""
    total_lines = raw.get("total_lines")
    tokens = raw.get("full_data_tokens")
    truncated_desc = (f"{total_lines} total lines, {tokens} tokens, only a preview sample was returned"
                      if total_lines is not None else "response was truncated to a preview")
    return ResearchItem(
        channel=channel, symbol=symbol,
        mechanism=f"{source} returned a preview only ({truncated_desc}); "
                 f"re-fetch with return_full_data=true rather than parsing the sample",
        value={"total_lines": total_lines, "full_data_tokens": tokens,
               "data_url": raw.get("data_url")},
        source=source, asof=asof, quality="degraded",
        detail=str(raw.get("message", "")))


def _shape_guard(raw: dict, required_keys: Sequence[str], feed_name: str) -> None:
    missing = [k for k in required_keys if k not in raw]
    if missing:
        raise ResearchShapeError(
            f"{feed_name} response is missing expected key(s) {missing} -- "
            f"got keys {sorted(raw.keys())}. Do not guess: fix the parser "
            f"against a fresh recorded response.")


def _parse_av_csv_result(raw: dict, feed_name: str) -> list[dict]:
    """Several Alpha Vantage endpoints return `{"result": "<CSV text>"}` --
    a JSON envelope around a CSV string, not a `{"data": [...]}` shape.
    Verified live, 4 September 2026, for `CPI` (default `datatype=csv`),
    `EARNINGS_CALENDAR`, and `IPO_CALENDAR` (neither of which has a
    `datatype` parameter at all, so this is their only shape)."""
    _shape_guard(raw, ("result",), feed_name)
    return list(csv.DictReader(io.StringIO(raw["result"])))


def _rows_from_series_response(raw: dict, feed_name: str) -> list[dict]:
    """The nine macro and eleven commodity channels can return either shape:
    `datatype=json` gives `{"name":, "interval":, "unit":, "data":
    [{"date":, "value":}]}` (verified live against `WTI`); the `datatype=csv`
    default gives `{"result": "<CSV text>"}` with columns `timestamp,value`
    (verified live against `CPI`, including a real malformed row,
    `2025-10-01,.`). Callers should request `datatype=json` (see
    `PROCEDURE_RATIONALE.md`); this function tolerates either shape so a
    caller that forgot still gets a correct parse rather than a silently
    empty one, and normalises the CSV column name `timestamp` to `date` so
    downstream code has one field name to read regardless of which shape
    arrived."""
    if "data" in raw:
        return list(raw["data"])
    if "result" in raw:
        rows = _parse_av_csv_result(raw, feed_name)
        return [{"date": r.get("timestamp", r.get("date")), "value": r.get("value")}
                for r in rows]
    raise ResearchShapeError(
        f"{feed_name} response has neither 'data' nor 'result' -- got keys {sorted(raw.keys())}")


def _numeric_quality(value: Any) -> str:
    """A malformed or missing latest value (Alpha Vantage's own placeholder
    for a not-yet-published print is a literal '.') must not be reported as
    an ordinary number -- degraded, not a silent pass-through."""
    if value in (None, ".", ""):
        return "degraded"
    try:
        float(value)
    except (TypeError, ValueError):
        return "degraded"
    return "ok"


# --------------------------------------------------------------------------
# candidate generation -- the other half of an undefined research process
# --------------------------------------------------------------------------

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
    """The candidate universe, defined once, in code. Four sources, unioned
    and deduplicated: held positions; `state.json.config.watchlist`;
    today's `TOP_GAINERS_LOSERS` (flatten with `top_movers_symbols()` first);
    names sharing a sector (via `sector_map`, default `SECTOR_MAP`) with a
    held position."""
    sector_map = SECTOR_MAP if sector_map is None else sector_map
    held = {s.upper() for s in held_symbols}
    out = set(held) | {s.upper() for s in watchlist_symbols} | {s.upper() for s in top_movers}

    held_sectors = {sector_map[s] for s in held if s in sector_map}
    for sym, sec in sector_map.items():
        if sec in held_sectors:
            out.add(sym)

    return sorted(out)


def top_movers_symbols(raw: Optional[dict]) -> list[str]:
    """Flatten `TOP_GAINERS_LOSERS`'s real shape into the flat ticker list
    `candidates()`'s `top_movers` argument expects. Real shape (verified
    live, 4 September 2026): `{"metadata":, "last_updated":,
    "top_gainers": [{"ticker":, ...}], "top_losers": [...],
    "most_actively_traded": [...]}`."""
    if not raw:
        return []
    out: list[str] = []
    for bucket in ("top_gainers", "top_losers", "most_actively_traded"):
        for row in raw.get(bucket, []):
            t = row.get("ticker")
            if t:
                out.append(str(t).upper())
    return out


# --------------------------------------------------------------------------
# weather -- the one path, explicit and testable, never a general narrative
# --------------------------------------------------------------------------

WEATHER_MAP: dict[str, tuple[str, str]] = {
    "XOM": ("heating_degree_days", "refiner and heating-fuel demand rises with cold snaps"),
    "CVX": ("heating_degree_days", "refiner and heating-fuel demand rises with cold snaps"),
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
    """`NEWS_SENTIMENT`, already fetched. Verified live, 4 September 2026,
    against a real OXY response -- `raw["feed"]`, and each entry's `title`,
    `overall_sentiment_score`, `overall_sentiment_label`, `time_published`,
    all match exactly what this parser reads. Note for the caller: even a
    `limit=2` request has been observed to spill to a file (77KB+) -- read
    it with `jq`, per `PROCEDURE_RATIONALE.md`.

    **Filters on each article's own `ticker_sentiment`; never trusts that a
    response fetched for `symbol` is actually about `symbol`.** Confirmed
    live, 5 September 2026: fetching `NEWS_SENTIMENT` for several different
    tickers in ONE PARALLEL BATCH of calls returned real, well-formed
    responses whose content belonged to a DIFFERENT ticker than the one
    requested -- a call for XOM came back 100% GLDM-tagged articles, a call
    for GLDM came back AAPL articles, and so on for four of five concurrent
    calls; only a fifth, made alone, came back correct. Nothing downstream
    -- this parser as it stood, `verify_email`, a human reading the brief --
    had any way to catch it: the source is real, the headline is real, and
    any number quoted from it traces, which is exactly what makes it a
    fabrication path that survives every guard built so far. It is
    detectable at all only because every article carries its own
    `ticker_sentiment` array naming the tickers it is actually about, each
    with a `relevance_score`. An article is kept only if `symbol` appears in
    its OWN `ticker_sentiment`, and the surviving items carry that
    relevance score -- a name mentioned in passing alongside three others is
    weaker corroboration than a name the article is centrally about, and the
    five-condition gate's `corroborated()` check should be able to tell the
    difference even though it does not weight by it yet. A response where
    MOST articles fail this check is reported as one `quality="failed"` item
    naming the cross-wiring, not a quietly shorter list -- indistinguishable
    from a genuinely quiet day otherwise. See PROCEDURE_RATIONALE.md for why
    the fix on the calling side is to fetch this endpoint sequentially, one
    symbol per call, never batched -- this filter is the second line of
    defence, not a replacement for that."""
    if not raw or not raw.get("feed"):
        return [ResearchItem(channel="news", symbol=symbol,
                             mechanism="no company/market news with sentiment retrieved",
                             value=None, source="Alpha Vantage NEWS_SENTIMENT",
                             asof=asof, quality="failed",
                             detail="empty or missing feed")]
    sym = symbol.upper()
    feed = raw["feed"]
    kept: list[tuple[dict, Optional[float]]] = []
    dropped = 0
    for entry in feed:
        match = next((t for t in entry.get("ticker_sentiment", ())
                     if str(t.get("ticker", "")).upper() == sym), None)
        if match is None:
            dropped += 1
            continue
        try:
            relevance = float(match.get("relevance_score"))
        except (TypeError, ValueError):
            relevance = None
        kept.append((entry, relevance))

    if dropped > len(feed) // 2:
        return [ResearchItem(
            channel="news", symbol=symbol, mechanism="",
            value={"total": len(feed), "matching": len(kept), "dropped": dropped},
            source="Alpha Vantage NEWS_SENTIMENT", asof=asof, quality="failed",
            detail=(f"{dropped} of {len(feed)} articles returned for a {sym} request do not "
                   f"name {sym} in their own ticker_sentiment -- this response is cross-wired "
                   f"to a different ticker, not a quiet news day; do not use it"))]

    out = []
    for entry, relevance in kept:
        title = entry.get("title", "")
        sentiment = entry.get("overall_sentiment_score")
        out.append(ResearchItem(
            channel="news", symbol=symbol,
            mechanism=f"news item scored for sentiment toward {symbol}",
            value={"title": title, "sentiment_score": sentiment,
                  "sentiment_label": entry.get("overall_sentiment_label"),
                  "relevance_score": relevance},
            source="Alpha Vantage NEWS_SENTIMENT", asof=asof, quality="ok",
            detail=str(entry.get("time_published", ""))))
    return out


def news_items_from_robinhood(raw: Optional[dict], *, symbol: str,
                               asof: str) -> list[ResearchItem]:
    """Robinhood `get_equity_news`, already fetched — the second,
    independent source `sources_for`/`corroborated` need. Real shape
    (verified live, 4 September 2026, against a real OXY response):
    `{"data": {"symbol":, "articles": [{"title":, "publisher":,
    "published_at":, ...}], "next_cursor":}, "guide":}` — an earlier
    version of this parser read `data.news`, a key that does not exist;
    the real list is `data.articles`, and each entry also carries a
    `publisher`, which is itself worth keeping since it is a second,
    finer-grained attribution than "Robinhood" alone."""
    articles = (raw or {}).get("data", {}).get("articles")
    if not articles:
        return [ResearchItem(channel="news", symbol=symbol,
                             mechanism="no Robinhood news retrieved",
                             value=None, source="Robinhood get_equity_news",
                             asof=asof, quality="failed", detail="empty result")]
    out = []
    for entry in articles:
        out.append(ResearchItem(
            channel="news", symbol=symbol,
            mechanism=f"independent news source for {symbol}",
            value={"title": entry.get("title", ""), "publisher": entry.get("publisher")},
            source="Robinhood get_equity_news", asof=asof, quality="ok",
            detail=str(entry.get("published_at", ""))))
    return out


# --------------------------------------------------------------------------
# congressional and insider activity -- both genuinely per-symbol
# --------------------------------------------------------------------------

def congress_trade_items(raw: Optional[dict], *, symbol: str,
                          asof: str) -> list[ResearchItem]:
    """`CONGRESS_TRADES` for ONE symbol. The endpoint requires a `symbol`
    or a `bioguide_id` -- there is no bulk pull across a watch list, unlike
    an earlier version of this parser assumed. Real shape (verified live,
    4 September 2026, against 58 real OXY disclosures): `{"symbol":,
    "bioguide_id":, "trades": [{"symbol":, "transaction_type":,
    "amount_min":, "amount_max":, "party":, "state":, "politician_canonical":,
    ...}]}`. `party`/`state`/`state_district` are already on each row --
    some rows have them `null` (redacted upstream, not every discloser's
    metadata is complete), but `POLITICIAN_METADATA` is not needed to fill
    them in and is not called here."""
    if raw is None:
        return [ResearchItem(channel="congress_trade", symbol=symbol,
                             mechanism=f"CONGRESS_TRADES unavailable for {symbol}",
                             value=None, source="Alpha Vantage CONGRESS_TRADES",
                             asof=asof, quality="failed")]
    if _is_preview_envelope(raw):
        return [_preview_item("congress_trade", symbol, "Alpha Vantage CONGRESS_TRADES", raw, asof)]
    _shape_guard(raw, ("trades",), "CONGRESS_TRADES")
    out = []
    for row in raw["trades"]:
        politician = row.get("politician_canonical") or row.get("politician") or "an unnamed member"
        party = row.get("party") or "party unrecorded"
        state = row.get("state") or "state unrecorded"
        txn = row.get("transaction_type", "transaction")
        out.append(ResearchItem(
            channel="congress_trade", symbol=symbol,
            mechanism=f"{politician} ({party}, {state}) disclosed a {txn} in {symbol}",
            value={"politician": politician, "transaction_type": txn,
                  "amount_min": row.get("amount_min"), "amount_max": row.get("amount_max"),
                  "transaction_date": row.get("transaction_date")},
            source="Alpha Vantage CONGRESS_TRADES", asof=asof, quality="ok"))
    return out


def insider_transaction_items(raw: Optional[dict], *, symbol: str,
                               asof: str) -> list[ResearchItem]:
    """`INSIDER_TRANSACTIONS` for one symbol. Real shape (verified live,
    4 September 2026, against 2,794 real OXY rows): full data is
    `{"data": [{"ticker":, "executive":, "executive_title":,
    "acquisition_or_disposal": "A"|"D", "shares":, "share_price":,
    "transaction_date":}]}` -- rows key on `ticker`, not `symbol` (this was
    swapped with `CONGRESS_TRADES`'s field name in an earlier version of
    this parser, which is why every row was filtered out in both
    directions). Oversized responses return a preview envelope instead;
    see `_preview_item`."""
    if raw is None:
        return [ResearchItem(channel="insider_transaction", symbol=symbol,
                             mechanism=f"INSIDER_TRANSACTIONS unavailable for {symbol}",
                             value=None, source="Alpha Vantage INSIDER_TRANSACTIONS",
                             asof=asof, quality="failed")]
    if _is_preview_envelope(raw):
        return [_preview_item("insider_transaction", symbol, "Alpha Vantage INSIDER_TRANSACTIONS", raw, asof)]
    _shape_guard(raw, ("data",), "INSIDER_TRANSACTIONS")
    out = []
    for row in raw["data"]:
        if str(row.get("ticker", "")).upper() != symbol.upper():
            continue
        verb = "acquired" if row.get("acquisition_or_disposal") == "A" else "disposed of"
        out.append(ResearchItem(
            channel="insider_transaction", symbol=symbol,
            mechanism=(f"{row.get('executive', 'an insider')} "
                      f"({row.get('executive_title', 'role unrecorded')}) {verb} shares"),
            value={"shares": row.get("shares"), "share_price": row.get("share_price"),
                  "date": row.get("transaction_date")},
            source="Alpha Vantage INSIDER_TRANSACTIONS", asof=asof, quality="ok"))
    return out


# --------------------------------------------------------------------------
# scheduled events
# --------------------------------------------------------------------------

def earnings_calendar_items(raw: Optional[dict], *,
                             held_or_candidate: Sequence[str],
                             asof: str) -> list[ResearchItem]:
    """`EARNINGS_CALENDAR` has no `datatype=json` option -- always
    `{"result": "<CSV text>"}`, columns `symbol,name,reportDate,
    fiscalDateEnding,estimate,currency,timeOfTheDay` (verified live,
    4 September 2026, against both an empty and a populated real
    response)."""
    watch = {s.upper() for s in held_or_candidate}
    if raw is None:
        return [ResearchItem(channel="earnings_calendar", symbol=None,
                             mechanism="EARNINGS_CALENDAR unavailable this run",
                             value=None, source="Alpha Vantage EARNINGS_CALENDAR",
                             asof=asof, quality="failed")]
    rows = _parse_av_csv_result(raw, "EARNINGS_CALENDAR")
    out = []
    for row in rows:
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
    """`EARNINGS_ESTIMATES` for one symbol. Real shape (verified live,
    4 September 2026): `{"symbol":, "estimates": [...]}`."""
    if not raw:
        return [ResearchItem(channel="earnings_estimate", symbol=symbol,
                             mechanism="EARNINGS_ESTIMATES unavailable this run",
                             value=None, source="Alpha Vantage EARNINGS_ESTIMATES",
                             asof=asof, quality="failed")]
    _shape_guard(raw, ("estimates",), "EARNINGS_ESTIMATES")
    return [ResearchItem(
        channel="earnings_estimate", symbol=symbol,
        mechanism=f"consensus estimate context for {symbol}'s next report",
        value=raw["estimates"][:1], source="Alpha Vantage EARNINGS_ESTIMATES",
        asof=asof, quality="ok")]


def earnings_call_transcript_items(raw: Optional[dict], *, symbol: str,
                                    horizon_reason: str,
                                    asof: str) -> list[ResearchItem]:
    """`EARNINGS_CALL_TRANSCRIPT` for the prior quarter — only called at
    all for a held name reporting within an open thesis's horizon. Not
    independently re-verified live in the 4 September 2026 audit; treat the
    shape assumption below as unconfirmed until it is."""
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
    """Robinhood `get_sec_filing` + `get_sec_filing_facts`. Not
    independently re-verified live in the 4 September 2026 audit."""
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
    """One of the nine macro series. Verified live, 4 September 2026:
    `CPI` on the `datatype=csv` default (`{"result": "<CSV text>"}`,
    including a real malformed row) and all nine channels
    (`TREASURY_YIELD`, `FEDERAL_FUNDS_RATE`, `CPI`, `INFLATION`,
    `UNEMPLOYMENT` -- which also has a real malformed `"."` row --
    `NONFARM_PAYROLL`, `RETAIL_SALES`, `REAL_GDP`, `DURABLES`) on
    `datatype=json` (`{"data": [{"date":, "value":}]}`, see
    `_rows_from_series_response`).

    **Checks for a preview envelope first.** Confirmed 5 September 2026: a
    macro channel requested with a lookback wide enough to trigger the
    harness's own oversized-response truncation comes back as a preview
    envelope (`{"preview": true, ...}`, no `"data"`/`"result"` key), which
    `_rows_from_series_response` cannot tell apart from a genuine field-name
    drift — both raised the identical `ResearchShapeError`. `CONGRESS_TRADES`
    and `INSIDER_TRANSACTIONS` already had this branch; macro and
    commodities did not, so a caller who asked for too much history read a
    truncation as a parser bug."""
    if channel not in MACRO_CHANNELS:
        raise ValueError(f"unrecognised macro channel: {channel!r}")
    if not raw:
        return ResearchItem(channel=f"macro:{channel}", symbol=None,
                            mechanism=f"{channel} unavailable this run",
                            value=None, source=f"Alpha Vantage {channel}",
                            asof=asof, quality="failed")
    if _is_preview_envelope(raw):
        return _preview_item(f"macro:{channel}", None, f"Alpha Vantage {channel}", raw, asof)
    rows = _rows_from_series_response(raw, channel)
    if not rows:
        return ResearchItem(channel=f"macro:{channel}", symbol=None,
                            mechanism=f"{channel} returned no data points",
                            value=None, source=f"Alpha Vantage {channel}",
                            asof=asof, quality="failed")
    latest = rows[0]
    quality = _numeric_quality(latest.get("value"))
    return ResearchItem(
        channel=f"macro:{channel}", symbol=None,
        mechanism=f"latest {channel} print, standing macro backdrop",
        value=latest, source=f"Alpha Vantage {channel}", asof=asof, quality=quality,
        detail="" if quality == "ok" else f"latest reported value is {latest.get('value')!r}, not a usable number")


# --------------------------------------------------------------------------
# commodities -- one shape, eleven channels, gated on actual exposure
# --------------------------------------------------------------------------

COMMODITY_CHANNELS = ("WTI", "BRENT", "NATURAL_GAS", "COPPER", "ALUMINUM",
                      "WHEAT", "CORN", "COFFEE", "SUGAR", "COTTON",
                      "ALL_COMMODITIES", "GOLD_SILVER_SPOT")

COMMODITY_EXPOSURE: dict[str, tuple[str, ...]] = {
    "XOM": ("WTI", "BRENT", "NATURAL_GAS"), "CVX": ("WTI", "BRENT", "NATURAL_GAS"),
    "COP": ("WTI", "BRENT"), "SLB": ("WTI", "BRENT"), "HAL": ("WTI", "BRENT"),
    "OXY": ("WTI", "BRENT"), "XLE": ("WTI", "BRENT"), "USO": ("WTI",),
    "VDE": ("WTI", "BRENT"), "XOP": ("WTI", "BRENT"), "OIH": ("WTI", "BRENT"),
    "GLDM": ("GOLD_SILVER_SPOT",), "GLD": ("GOLD_SILVER_SPOT",), "IAU": ("GOLD_SILVER_SPOT",),
    "DE": ("CORN", "WHEAT"),
}


def _gold_silver_spot_row(raw: dict) -> dict:
    """`GOLD_SILVER_SPOT` is a live scalar quote, not a time series --
    verified live 4 September 2026: `{"nominal": "XAUUSD", "timestamp":
    "2026-09-04 18:34:58", "price": "4423.7080671183"}`. No `result`, no
    `data`, no rows -- routing it through `_rows_from_series_response`
    (as the first version of this parser did, since it sits in
    `COMMODITY_CHANNELS` alongside every series channel) raised
    `ResearchShapeError` on every single call. Normalised to the same
    `{"date":, "value":}` shape the series channels use so downstream
    code (`_numeric_quality`, the email cards) doesn't need to special-case
    it."""
    _shape_guard(raw, ("nominal", "price"), "GOLD_SILVER_SPOT")
    return {"date": raw.get("timestamp"), "value": raw.get("price"), "nominal": raw.get("nominal")}


def commodity_items(symbols: Sequence[str], raw_by_channel: dict[str, Optional[dict]],
                     *, asof: str) -> list[ResearchItem]:
    """One item per (symbol, channel) pair actually exposed per
    `COMMODITY_EXPOSURE`. All eleven series channels (`WTI`, `BRENT`,
    `NATURAL_GAS`, `COPPER`, `ALUMINUM`, `WHEAT`, `CORN`, `COFFEE`,
    `SUGAR`, `COTTON`, `ALL_COMMODITIES`) were verified live 4 September
    2026 to share `WTI`'s `datatype=json` response family (see
    `_rows_from_series_response`). `GOLD_SILVER_SPOT` is not a series and
    is parsed separately -- see `_gold_silver_spot_row`.

    **Checks for a preview envelope before parsing.** Same reasoning as
    `macro_item`: an oversized commodity response truncates to a preview
    envelope that `_rows_from_series_response` cannot distinguish from a
    genuine shape drift, and the `ResearchShapeError` catch below would
    otherwise report the correct symptom (failed) with the wrong cause."""
    out = []
    for sym in symbols:
        for channel in COMMODITY_EXPOSURE.get(sym.upper(), ()):
            if channel not in COMMODITY_CHANNELS:
                raise ValueError(f"unrecognised commodity channel: {channel!r}")
            raw = raw_by_channel.get(channel)
            if not raw:
                out.append(ResearchItem(
                    channel=f"commodity:{channel}", symbol=sym,
                    mechanism=f"{sym} has {channel} exposure but the feed was unavailable",
                    value=None, source=f"Alpha Vantage {channel}",
                    asof=asof, quality="failed"))
                continue
            if _is_preview_envelope(raw):
                out.append(_preview_item(f"commodity:{channel}", sym,
                                         f"Alpha Vantage {channel}", raw, asof))
                continue
            try:
                if channel == "GOLD_SILVER_SPOT":
                    rows = [_gold_silver_spot_row(raw)]
                else:
                    rows = _rows_from_series_response(raw, channel)
            except ResearchShapeError as e:
                # A shape drift in one commodity channel must not take
                # down every other symbol/channel pair in this call --
                # they all share one `timed()` invocation in gather().
                out.append(ResearchItem(
                    channel=f"commodity:{channel}", symbol=sym,
                    mechanism="", value=None, source=f"Alpha Vantage {channel}",
                    asof=asof, quality="failed", detail=str(e)))
                continue
            if not rows:
                out.append(ResearchItem(
                    channel=f"commodity:{channel}", symbol=sym,
                    mechanism=f"{sym} has {channel} exposure but no data points were returned",
                    value=None, source=f"Alpha Vantage {channel}",
                    asof=asof, quality="failed"))
                continue
            latest = rows[0]
            quality = _numeric_quality(latest.get("value"))
            mechanism = (f"{sym} has direct gold-spot price exposure" if channel == "GOLD_SILVER_SPOT"
                        else f"{sym} has direct {channel} price exposure")
            out.append(ResearchItem(
                channel=f"commodity:{channel}", symbol=sym,
                mechanism=mechanism,
                value=latest, source=f"Alpha Vantage {channel}", asof=asof, quality=quality,
                detail="" if quality == "ok" else f"latest reported value is {latest.get('value')!r}, not a usable number"))
    return out


# --------------------------------------------------------------------------
# positioning and session state
# --------------------------------------------------------------------------

def _near_term_put_call_signal(symbol: str, historical: Optional[dict],
                                full_chain_ratio, *, asof: str,
                                threshold: float = 0.5) -> Optional[ResearchItem]:
    """`HISTORICAL_PUT_CALL_RATIO`'s real shape (verified live 4 September
    2026, OXY) carries `put_call_ratio_by_expiration`: `[{"date":
    "YYYY-MM-DD", "value": "R"}, ...]`, nearest expiration first -- real
    information the first version of this parser discarded entirely. Only
    the single nearest expiration is checked (this is a near-term signal,
    not a scan for any divergent date out the chain), and it is only
    carried as its own item when it diverges from the full-chain ratio by
    more than `threshold` (50% relative) -- a routine near-dated wobble
    should not manufacture a signal that is not really there. Returns
    None when there is nothing clean to attach, per Rule 1."""
    if not historical or not isinstance(historical.get("put_call_ratio_by_expiration"), list):
        return None
    rows = historical["put_call_ratio_by_expiration"]
    if not rows:
        return None
    try:
        full_chain_val = float(full_chain_ratio)
    except (TypeError, ValueError):
        return None
    date, raw_value = rows[0].get("date"), rows[0].get("value")
    if not date or raw_value is None or full_chain_val == 0:
        return None
    try:
        near_val = float(raw_value)
    except (TypeError, ValueError):
        return None
    divergence = abs(near_val - full_chain_val) / full_chain_val
    if divergence < threshold:
        return None
    return ResearchItem(
        channel="positioning:put_call_nearterm", symbol=symbol,
        mechanism=(f"{date} expiration put/call ratio of {near_val} diverges "
                  f"{divergence:.0%} from {symbol}'s {full_chain_val} full-chain ratio"),
        value={"date": date, "value": raw_value, "full_chain_ratio": full_chain_ratio},
        source="Alpha Vantage HISTORICAL_PUT_CALL_RATIO", asof=asof, quality="ok")


def put_call_items(realtime: Optional[dict], historical: Optional[dict], *,
                    symbol: str, asof: str) -> list[ResearchItem]:
    """Real key (verified live, 4 September 2026, against a real OXY
    response) is `put_call_ratio_full_chain` -- a string. An earlier
    version of this parser read `ratio`, a key that does not exist in the
    response at all, so this always reported `failed` regardless of
    whether the feed actually succeeded. See `_near_term_put_call_signal`
    for the near-dated expiration signal this now also carries when it's
    genuinely divergent."""
    if not realtime or "put_call_ratio_full_chain" not in realtime:
        return [ResearchItem(channel="positioning:put_call", symbol=symbol,
                             mechanism=f"put/call positioning for {symbol} unavailable this run",
                             value=None, source="Alpha Vantage REALTIME_PUT_CALL_RATIO",
                             asof=asof, quality="failed")]
    full_chain_ratio = realtime.get("put_call_ratio_full_chain")
    hist_ratio = (historical or {}).get("put_call_ratio_full_chain")
    items = [ResearchItem(
        channel="positioning:put_call", symbol=symbol,
        mechanism=f"current put/call ratio for {symbol}",
        value=full_chain_ratio,
        source="Alpha Vantage REALTIME_PUT_CALL_RATIO", asof=asof, quality="ok",
        detail=(f"historical context: {hist_ratio}" if hist_ratio is not None
               else "no historical context available"))]
    near_term = _near_term_put_call_signal(symbol, historical, full_chain_ratio, asof=asof)
    if near_term is not None:
        items.append(near_term)
    return items


def top_movers_items(raw: Optional[dict], *, asof: str) -> list[ResearchItem]:
    """`TOP_GAINERS_LOSERS`, verified live 4 September 2026 — real shape
    matches what this parser already stored wholesale; see
    `top_movers_symbols()` for the flattened ticker list `candidates()`
    consumes."""
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
    """`MARKET_STATUS`, verified live 4 September 2026 — real shape
    (`{"endpoint":, "markets": [...]}`) matches what this parser already
    stored wholesale."""
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

def _row_count(raw: Any) -> int:
    """Best-effort "rows seen" for coverage tracking, across the shapes
    this module's feeds actually return."""
    if raw is None:
        return 0
    if isinstance(raw, dict):
        for key in ("trades", "data", "estimates", "feed"):
            if key in raw and isinstance(raw[key], list):
                return len(raw[key])
        # Robinhood get_equity_news: {"data": {"articles": [...]}} -- "data"
        # is a dict here, not a list, so the generic check above misses it.
        articles = raw.get("data", {})
        if isinstance(articles, dict) and isinstance(articles.get("articles"), list):
            return len(articles["articles"])
        if "result" in raw and isinstance(raw["result"], str):
            return max(len(raw["result"].strip().splitlines()) - 1, 0)
        return 1 if raw else 0
    if isinstance(raw, list):
        return len(raw)
    return 0


def gather(raw_feeds: dict[str, Any], *, held_or_candidate: Sequence[str],
           asof: Optional[str] = None,
           timer: Callable[[], float] = time.monotonic) -> ResearchBundle:
    """Assemble a `ResearchBundle` from already-fetched raw responses.

    `raw_feeds` keys (all optional; a missing key produces no items for
    that feed and is recorded in `skipped` rather than as an error):

    - `"news_av"`, `"news_rh"`: `{symbol: raw_response}`
    - `"congress_trades"`, `"insider_transactions"`: `{symbol: raw_response}`
      -- both endpoints require a symbol per call, there is no bulk pull
    - `"earnings_calendar"` (one bulk response), `"earnings_estimates"`
      (`{symbol: raw}`)
    - `"earnings_call_transcripts"`: `{symbol: (raw, horizon_reason)}`
    - `"sec_filings"`, `"sec_filing_facts"`: `{symbol: raw}`
    - `"macro"`: `{channel: raw}` for any of `MACRO_CHANNELS`, requested
      with `datatype=json`
    - `"commodities"`: `{channel: raw}` for any of `COMMODITY_CHANNELS`
      (including `GOLD_SILVER_SPOT`, a scalar quote requested with
      `symbol=GOLD` -- not a `datatype=json` series; see
      `_gold_silver_spot_row`); the other ten channels requested with
      `datatype=json`
    - `"weather"`: `{variable: raw}`
    - `"put_call_realtime"`, `"put_call_historical"`: `{symbol: raw}` --
      `put_call_historical` additionally carries a near-dated expiration
      signal when genuinely divergent, see `_near_term_put_call_signal`.
      `IPO_CALENDAR` is not fetched at all: its real schema has no
      `sector` field and cannot satisfy Rule 1 (see
      `fixtures/research/ipo_calendar.json` and `HANDOFF.md` section 11)
    - `"top_movers"`, `"market_status"`

    Every feed's wall-clock cost is recorded on `timings_ms`; rows-seen vs
    items-produced is recorded on `coverage` so a field-name mismatch
    cannot masquerade as a quiet day (see `ResearchBundle.coverage_issues`).
    A `ResearchShapeError` from any single feed is caught here and turned
    into one `quality="failed"` item naming the mismatch, rather than
    aborting the rest of gathering.
    """
    asof = asof or datetime.utcnow().isoformat()
    bundle = ResearchBundle(asof=asof)
    symbols = list(dict.fromkeys(s.upper() for s in held_or_candidate))

    def timed(name: str, fn, *, rows_in: int = None, channel: str = None):
        """`channel` names the ResearchItem.channel a shape-error fallback
        item gets, and MUST match the channel the feed's real parser uses
        (see the `channel="..."` literals throughout this module) -- not
        just derived from `name`, which is a feed/symbol key, not an item
        channel. A mismatch here is exactly the kind of silent-mismatch
        bug this module exists to catch: a caller filtering
        `bundle.for_channel("congress_trade")` must find a shape failure
        for congress trades, not lose it under a "congress_trades" that
        no real item ever uses."""
        t0 = timer()
        try:
            result = fn()
        except ResearchShapeError as e:
            result = [ResearchItem(channel=channel or name.split(":")[0], symbol=None,
                                   mechanism="", value=None, source=name,
                                   asof=asof, quality="failed", detail=str(e))]
        finally:
            bundle.timings_ms[name] = int((timer() - t0) * 1000)
        items = result if isinstance(result, list) else [result]
        n_in = rows_in if rows_in is not None else len(items)
        bundle.record_coverage(name, n_in, sum(1 for i in items if i.usable))
        return items

    news_av = raw_feeds.get("news_av", {})
    news_rh = raw_feeds.get("news_rh", {})
    for sym in symbols:
        if sym in news_av:
            bundle.items.extend(timed(f"news_av:{sym}",
                lambda sym=sym: news_items_from_alpha_vantage(news_av.get(sym), symbol=sym, asof=asof),
                rows_in=_row_count(news_av.get(sym)), channel="news"))
        else:
            bundle.skipped.append(f"news_av:{sym} — not in raw_feeds")
        if sym in news_rh:
            bundle.items.extend(timed(f"news_rh:{sym}",
                lambda sym=sym: news_items_from_robinhood(news_rh.get(sym), symbol=sym, asof=asof),
                rows_in=_row_count(news_rh.get(sym)), channel="news"))
        else:
            bundle.skipped.append(f"news_rh:{sym} — not in raw_feeds")

    congress = raw_feeds.get("congress_trades", {})
    for sym, raw in congress.items():
        bundle.items.extend(timed(f"congress_trades:{sym}",
            lambda raw=raw, sym=sym: congress_trade_items(raw, symbol=sym, asof=asof),
            rows_in=_row_count(raw), channel="congress_trade"))
    if not congress:
        bundle.skipped.append("congress_trades — not in raw_feeds")

    insiders = raw_feeds.get("insider_transactions", {})
    for sym, raw in insiders.items():
        bundle.items.extend(timed(f"insider_transactions:{sym}",
            lambda raw=raw, sym=sym: insider_transaction_items(raw, symbol=sym, asof=asof),
            rows_in=_row_count(raw), channel="insider_transaction"))
    if not insiders:
        bundle.skipped.append("insider_transactions — not in raw_feeds")

    if "earnings_calendar" in raw_feeds:
        raw = raw_feeds.get("earnings_calendar")
        bundle.items.extend(timed("earnings_calendar", lambda: earnings_calendar_items(
            raw, held_or_candidate=symbols, asof=asof), rows_in=_row_count(raw)))
    else:
        bundle.skipped.append("earnings_calendar — not in raw_feeds")

    for sym, raw in raw_feeds.get("earnings_estimates", {}).items():
        bundle.items.extend(timed(f"earnings_estimates:{sym}",
            lambda raw=raw, sym=sym: earnings_estimate_items(raw, symbol=sym, asof=asof),
            channel="earnings_estimate"))

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
        bundle.items.extend(timed(f"macro:{channel}",
            lambda channel=channel, raw=raw: [macro_item(channel, raw, asof=asof)],
            rows_in=_row_count(raw)))

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
            lambda sym=sym: put_call_items(pc_rt.get(sym), pc_hist.get(sym), symbol=sym, asof=asof),
            channel="positioning:put_call"))

    if "top_movers" in raw_feeds:
        bundle.items.extend(timed("top_movers", lambda: top_movers_items(raw_feeds.get("top_movers"), asof=asof)))
    if "market_status" in raw_feeds:
        bundle.items.extend(timed("market_status", lambda: [market_status_item(raw_feeds.get("market_status"), asof=asof)]))

    return bundle
