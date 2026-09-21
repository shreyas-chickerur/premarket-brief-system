"""Tests for research.py — every parser against a recorded REAL response.

4 September 2026: the first version of this module shipped with fixtures
the author wrote by hand, not responses the live API actually returned.
A live check against three feeds found two parsers reading field names
that do not exist; every fixture in fixtures/research/ was replaced with
an actual recorded response (or a genuine truncated slice of one) before
this test file was rewritten against them. Never hand-write a fixture here
again -- if a new feed needs a test, call it live once, save what comes
back, and write the parser against that.
"""

import json
from pathlib import Path

import pytest

import research as RS

FIXTURES = Path(__file__).parent / "fixtures" / "research"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


ASOF = "2026-09-04T13:00:00Z"


# --------------------------------------------------------------- ResearchItem

def test_item_rejects_bad_quality():
    with pytest.raises(ValueError, match="quality"):
        RS.ResearchItem(channel="news", symbol="OXY", mechanism="x",
                        value=1, source="s", asof=ASOF, quality="excellent")


def test_item_requires_a_channel():
    with pytest.raises(ValueError, match="channel"):
        RS.ResearchItem(channel="", symbol="OXY", mechanism="x",
                        value=1, source="s", asof=ASOF)


def test_item_requires_a_mechanism_unless_failed():
    with pytest.raises(ValueError, match="attach"):
        RS.ResearchItem(channel="news", symbol="OXY", mechanism="",
                        value=1, source="s", asof=ASOF, quality="ok")
    RS.ResearchItem(channel="news", symbol="OXY", mechanism="",
                    value=None, source="s", asof=ASOF, quality="failed")


def test_item_uppercases_symbol():
    item = RS.ResearchItem(channel="news", symbol="oxy", mechanism="x",
                           value=1, source="s", asof=ASOF)
    assert item.symbol == "OXY"


def test_item_usable_reflects_quality():
    ok = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="s", asof=ASOF)
    failed = RS.ResearchItem(channel="news", symbol="OXY", mechanism="", value=None, source="s", asof=ASOF, quality="failed")
    assert ok.usable and not failed.usable


# --------------------------------------------------------------- ResearchBundle

def _bundle_with(*items):
    b = RS.ResearchBundle(asof=ASOF)
    b.items.extend(items)
    return b


def test_for_symbol_and_for_channel_filter_correctly():
    a = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="s1", asof=ASOF)
    b = RS.ResearchItem(channel="macro:CPI", symbol=None, mechanism="x", value=1, source="s2", asof=ASOF)
    bundle = _bundle_with(a, b)
    assert bundle.for_symbol("oxy") == [a]
    assert bundle.for_channel("macro:CPI") == [b]
    assert bundle.for_channel("macro") == [b]


def test_sources_for_counts_distinct_usable_news_sources():
    a = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="Alpha Vantage", asof=ASOF)
    b = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="Robinhood", asof=ASOF)
    failed = RS.ResearchItem(channel="news", symbol="OXY", mechanism="", value=None, source="Robinhood", asof=ASOF, quality="failed")
    bundle = _bundle_with(a, b, failed)
    assert bundle.sources_for("OXY") == {"Alpha Vantage", "Robinhood"}


def test_corroborated_requires_the_minimum_distinct_sources():
    a = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="Alpha Vantage", asof=ASOF)
    assert not _bundle_with(a).corroborated("OXY")
    b = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="Robinhood", asof=ASOF)
    assert _bundle_with(a, b).corroborated("OXY")


def test_corroborated_does_not_count_two_items_from_the_same_source():
    a1 = RS.ResearchItem(channel="news", symbol="OXY", mechanism="x", value=1, source="Alpha Vantage", asof=ASOF)
    a2 = RS.ResearchItem(channel="news", symbol="OXY", mechanism="y", value=2, source="Alpha Vantage", asof=ASOF)
    assert not _bundle_with(a1, a2).corroborated("OXY")


def test_coverage_issues_flags_rows_seen_but_zero_items_produced():
    """This is the exact defect the 4 September audit found: a feed that
    was fetched and parsed but produced nothing distinguishable from a
    genuinely quiet day."""
    bundle = RS.ResearchBundle(asof=ASOF)
    bundle.record_coverage("congress_trades:OXY", rows_in=58, items_out=0)
    bundle.record_coverage("insider_transactions:OXY", rows_in=12, items_out=12)
    assert bundle.coverage_issues() == ["congress_trades:OXY"]


def test_coverage_issues_empty_when_zero_rows_and_zero_items():
    """A feed with nothing to report is not a coverage issue -- only rows
    seen with nothing produced is."""
    bundle = RS.ResearchBundle(asof=ASOF)
    bundle.record_coverage("earnings_calendar", rows_in=0, items_out=0)
    assert bundle.coverage_issues() == []


# --------------------------------------------------------------- shape guard

def test_shape_guard_raises_on_missing_key():
    with pytest.raises(RS.ResearchShapeError, match="missing expected key"):
        RS._shape_guard({"foo": 1}, ("bar",), "TESTFEED")


def test_shape_guard_passes_when_key_present():
    RS._shape_guard({"bar": 1}, ("bar",), "TESTFEED")  # does not raise


# --------------------------------------------------------------- candidates

def test_candidates_includes_held_symbols():
    out = RS.candidates(held_symbols=["oxy", "sgov"])
    assert "OXY" in out and "SGOV" in out


def test_candidates_includes_watchlist_and_top_movers():
    out = RS.candidates(held_symbols=[], watchlist_symbols=["vti"], top_movers=["nvda"])
    assert "VTI" in out and "NVDA" in out


def test_candidates_includes_screener_symbols():
    """5 September 2026: the hand-typed 31-symbol SECTOR_MAP is gone.
    Sector-adjacent candidates now come from a real scan (screener.py),
    passed in as screener_symbols -- candidates() itself no longer knows
    anything about sectors."""
    out = RS.candidates(held_symbols=["XOM"], screener_symbols=["CVX", "SLB"])
    assert "CVX" in out and "SLB" in out


def test_candidates_includes_congress_discovered():
    out = RS.candidates(held_symbols=["OXY"], congress_discovered=["DSGX", "TXRH"])
    assert "DSGX" in out and "TXRH" in out


def test_candidates_is_deduplicated_and_sorted():
    out = RS.candidates(held_symbols=["oxy", "OXY"], watchlist_symbols=["oxy"],
                        screener_symbols=["oxy"], congress_discovered=["OXY"])
    assert out.count("OXY") == 1
    assert out == sorted(out)


def test_candidates_has_no_sector_map_left():
    assert not hasattr(RS, "SECTOR_MAP")


# --------------------------------------------------------- sector lookup (real)

def test_sector_from_company_overview_parses_the_real_aapl_response():
    """Verified live, 5 September 2026: raw['Sector'] == 'TECHNOLOGY'."""
    assert RS.sector_from_company_overview({"Sector": "TECHNOLOGY"}) == "technology"


def test_sector_from_company_overview_normalises_multi_word_sectors():
    assert RS.sector_from_company_overview({"Sector": "Consumer Cyclical"}) == "consumer_cyclical"


def test_sector_from_company_overview_none_when_missing():
    assert RS.sector_from_company_overview({}) is None
    assert RS.sector_from_company_overview(None) is None


def test_sector_from_etf_profile_none_for_a_diversified_fund():
    """Real VTI shape, verified live 5 September 2026: eleven sectors, the
    largest (Information Technology) at 35% -- a total-market fund doing
    exactly what it should, not a technology fund."""
    raw = {"sectors": [{"sector": "INFORMATION TECHNOLOGY", "weight": "0.35"},
                       {"sector": "FINANCIALS", "weight": "0.102"}]}
    assert RS.sector_from_etf_profile(raw) is None


def test_sector_from_etf_profile_returns_the_dominant_sector_for_a_concentrated_fund():
    raw = {"sectors": [{"sector": "ENERGY", "weight": "0.97"},
                       {"sector": "UTILITIES", "weight": "0.03"}]}
    assert RS.sector_from_etf_profile(raw) == "energy"


def test_sector_from_etf_profile_none_when_no_sectors():
    assert RS.sector_from_etf_profile({}) is None
    assert RS.sector_from_etf_profile(None) is None


def test_sector_from_etf_profile_threshold_is_documented_and_used():
    raw = {"sectors": [{"sector": "ENERGY", "weight": str(RS.ETF_SECTOR_CONCENTRATION_THRESHOLD)}]}
    assert RS.sector_from_etf_profile(raw) == "energy"
    raw_below = {"sectors": [{"sector": "ENERGY",
                              "weight": str(RS.ETF_SECTOR_CONCENTRATION_THRESHOLD - 0.01)}]}
    assert RS.sector_from_etf_profile(raw_below) is None


# ------------------------------------------------- congress discovery (real)

def test_congress_trade_items_carries_bioguide_id():
    raw = {"trades": [{"symbol": "OXY", "bioguide_id": "C001123",
                       "politician_canonical": "Gilbert Ray Cisneros, Jr.",
                       "transaction_type": "BUY"}]}
    items = RS.congress_trade_items(raw, symbol="OXY", asof=ASOF)
    assert items[0].value["bioguide_id"] == "C001123"


def test_bioguide_ids_from_congress_items_bootstraps_from_symbol_keyed_calls():
    raw = {"trades": [{"symbol": "OXY", "bioguide_id": "C001123",
                       "transaction_type": "BUY"}]}
    items = RS.congress_trade_items(raw, symbol="OXY", asof=ASOF)
    assert RS.bioguide_ids_from_congress_items(items) == ["C001123"]


def test_bioguide_ids_from_congress_items_ignores_failed_items():
    items = RS.congress_trade_items(None, symbol="OXY", asof=ASOF)
    assert RS.bioguide_ids_from_congress_items(items) == []


def test_congress_trade_items_by_politician_parses_the_real_bioguide_response():
    """Verified live, 5 September 2026, bioguide_id C001123 (Gilbert
    Cisneros): two of his real disclosed trades, neither OXY -- DSGX and
    TXRH, surfaced only because this call has no symbol filter at all."""
    raw = {"symbol": "", "bioguide_id": "C001123", "trades": [
        {"symbol": "DSGX", "politician_canonical": "Gilbert Ray Cisneros, Jr.",
         "bioguide_id": "C001123", "transaction_type": "BUY", "party": "D", "state": "CA"},
        {"symbol": "TXRH", "politician_canonical": "Gilbert Ray Cisneros, Jr.",
         "bioguide_id": "C001123", "transaction_type": "BUY", "party": "D", "state": "CA"},
    ]}
    items = RS.congress_trade_items_by_politician(raw, bioguide_id="C001123", asof=ASOF)
    symbols = {i.symbol for i in items}
    assert symbols == {"DSGX", "TXRH"}
    assert "OXY" not in symbols
    assert all(i.value["bioguide_id"] == "C001123" for i in items)


def test_congress_trade_items_by_politician_handles_a_preview_envelope():
    """This endpoint has no date-range parameter, so any member with
    real trading history returns their entire disclosed record -- always a
    preview in practice, confirmed live 5 September 2026 (2,248 trades for
    one moderately active member)."""
    fake_preview = {"preview": True, "total_lines": 44966, "full_data_tokens": 437439,
                    "data_url": "https://example.test/x.json", "message": "truncated"}
    items = RS.congress_trade_items_by_politician(fake_preview, bioguide_id="C001123", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "degraded"


def test_congress_trade_items_by_politician_failed_on_none():
    items = RS.congress_trade_items_by_politician(None, bioguide_id="C001123", asof=ASOF)
    assert items[0].quality == "failed"


def test_symbols_from_congress_items_dedupes_across_rows():
    raw = {"symbol": "", "bioguide_id": "C001123", "trades": [
        {"symbol": "DSGX", "transaction_type": "BUY"},
        {"symbol": "DSGX", "transaction_type": "SELL"},
        {"symbol": "TXRH", "transaction_type": "BUY"},
    ]}
    items = RS.congress_trade_items_by_politician(raw, bioguide_id="C001123", asof=ASOF)
    assert RS.symbols_from_congress_items(items) == ["DSGX", "TXRH"]


# ------------------------------------------------- congress recency bound

def _congress_item(symbol, *, transaction_date, amount_min=50000):
    return RS.ResearchItem(
        channel="congress_trade", symbol=symbol,
        mechanism=f"a member disclosed a trade in {symbol}",
        value={"politician": "test", "bioguide_id": "C000000",
              "transaction_type": "BUY", "amount_min": amount_min,
              "amount_max": amount_min * 2, "transaction_date": transaction_date},
        source="Alpha Vantage CONGRESS_TRADES", asof=ASOF, quality="ok")


def test_recent_congress_items_keeps_a_disclosure_inside_the_window():
    items = [_congress_item("DSGX", transaction_date="2026-08-20")]
    out = RS.recent_congress_items(items, asof="2026-09-04")
    assert [i.symbol for i in out] == ["DSGX"]


def test_recent_congress_items_drops_a_disclosure_outside_the_window():
    """The real finding this guards against: one bioguide_id returned 2,248
    trades spanning years -- a 2021 purchase must not count as fresh."""
    items = [_congress_item("TXRH", transaction_date="2021-03-01")]
    assert RS.recent_congress_items(items, asof="2026-09-04") == []


def test_recent_congress_items_boundary_is_inclusive():
    items = [_congress_item("DSGX", transaction_date="2026-06-06")]  # exactly 90 days before
    out = RS.recent_congress_items(items, asof="2026-09-04", window_days=90)
    assert [i.symbol for i in out] == ["DSGX"]


def test_recent_congress_items_drops_a_disclosure_one_day_past_the_boundary():
    items = [_congress_item("DSGX", transaction_date="2026-06-05")]
    assert RS.recent_congress_items(items, asof="2026-09-04", window_days=90) == []


def test_recent_congress_items_drops_below_the_minimum_amount():
    items = [_congress_item("DSGX", transaction_date="2026-08-20", amount_min=1001)]
    assert RS.recent_congress_items(items, asof="2026-09-04", min_amount=15000) == []


def test_recent_congress_items_keeps_at_the_minimum_amount():
    items = [_congress_item("DSGX", transaction_date="2026-08-20", amount_min=15000)]
    out = RS.recent_congress_items(items, asof="2026-09-04", min_amount=15000)
    assert [i.symbol for i in out] == ["DSGX"]


def test_recent_congress_items_drops_a_row_with_no_transaction_date():
    item = RS.ResearchItem(channel="congress_trade", symbol="DSGX", mechanism="m",
                           value={"amount_min": 50000}, source="s", asof=ASOF, quality="ok")
    assert RS.recent_congress_items([item], asof="2026-09-04") == []


def test_recent_congress_items_ignores_a_failed_item():
    item = RS.ResearchItem(channel="congress_trade", symbol="DSGX", mechanism="",
                           value=None, source="s", asof=ASOF, quality="failed")
    assert RS.recent_congress_items([item], asof="2026-09-04") == []


def test_most_recent_congress_activity_keeps_the_latest_date_per_symbol():
    items = [_congress_item("DSGX", transaction_date="2026-07-01"),
            _congress_item("DSGX", transaction_date="2026-08-20"),
            _congress_item("TXRH", transaction_date="2026-08-01")]
    out = RS.most_recent_congress_activity(items)
    assert out == {"DSGX": "2026-08-20", "TXRH": "2026-08-01"}


def test_most_recent_congress_activity_empty_for_no_items():
    assert RS.most_recent_congress_activity([]) == {}


# ----------------------------------------------------------- universe funnel

def test_universe_funnel_reports_source_counts_and_final_size():
    out = RS.universe_funnel(held_symbols=["OXY", "SGOV"], watchlist_symbols=["XOM"],
                             top_movers=["NVDA"], screener_symbols=["CVX"],
                             congress_discovered=["DSGX"])
    assert out["source_counts"] == {"held": 2, "watchlist": 1, "top_movers": 1,
                                     "screener": 1, "congress_discovered": 1}
    assert out["universe_size"] == 6


def test_universe_funnel_deduplicates_overlapping_sources():
    out = RS.universe_funnel(held_symbols=["OXY"], watchlist_symbols=["OXY"],
                             screener_symbols=["OXY"])
    assert out["universe_size"] == 1


# --------------------------------------------------- symbols_with_dated_catalyst

def _earnings_raw(rows):
    header = "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay"
    lines = [header] + [f"{s},NAME,{d},2026-09-30,1.0,USD," for s, d in rows]
    return {"result": "\r\n".join(lines) + "\r\n"}


def test_symbols_with_dated_catalyst_parses_the_real_fixture():
    """Real AAPL fixture reports 2026-10-29 -- 55 days after this ASOF, so
    it needs a horizon wide enough to see it; this is the same real
    CSV-wrapped shape earnings_calendar_items already parses, read for a
    different purpose (ranking the wider eligible universe, not just
    held-or-candidate)."""
    raw = _load("earnings_calendar_aapl.json")
    out = RS.symbols_with_dated_catalyst(raw, symbols=["AAPL"], asof="2026-09-04",
                                         horizon_days=60)
    assert out == {"AAPL": "2026-10-29"}


def test_symbols_with_dated_catalyst_excludes_outside_the_horizon():
    raw = _earnings_raw([("AAPL", "2026-10-29")])
    out = RS.symbols_with_dated_catalyst(raw, symbols=["AAPL"], asof="2026-09-04",
                                         horizon_days=21)
    assert out == {}


def test_symbols_with_dated_catalyst_boundary_is_inclusive():
    raw = _earnings_raw([("AAPL", "2026-09-25")])  # exactly 21 days out
    out = RS.symbols_with_dated_catalyst(raw, symbols=["AAPL"], asof="2026-09-04",
                                         horizon_days=21)
    assert out == {"AAPL": "2026-09-25"}


def test_symbols_with_dated_catalyst_filters_to_the_eligible_set():
    raw = _earnings_raw([("AAPL", "2026-09-10"), ("MSFT", "2026-09-10")])
    out = RS.symbols_with_dated_catalyst(raw, symbols=["AAPL"], asof="2026-09-04")
    assert out == {"AAPL": "2026-09-10"}


def test_symbols_with_dated_catalyst_drops_unparseable_dates():
    raw = _earnings_raw([("AAPL", "not-a-date")])
    assert RS.symbols_with_dated_catalyst(raw, symbols=["AAPL"], asof="2026-09-04") == {}


def test_symbols_with_dated_catalyst_empty_for_no_response():
    assert RS.symbols_with_dated_catalyst(None, symbols=["AAPL"], asof="2026-09-04") == {}


# ----------------------------------------------------------- researched_set

def test_researched_set_always_keeps_every_held_symbol():
    out = RS.researched_set(held_symbols=["OXY", "XOM"], eligible_symbols=["OXY", "XOM", "CVX"],
                            ceiling=1)
    assert set(out["researched"]) >= {"OXY", "XOM"}
    assert out["tiers"]["held"] == ["OXY", "XOM"]


def test_researched_set_prioritises_dated_catalyst_over_congress_over_liquidity():
    out = RS.researched_set(
        held_symbols=[],
        eligible_symbols=["CATA", "CONG", "LIQA", "LIQB"],
        catalyst_dates={"CATA": "2026-09-10"},
        congress_recency={"CONG": "2026-09-01"},
        liquidity_by_symbol={"LIQA": 100.0, "LIQB": 50.0},
        ceiling=2)
    assert out["researched"] == ["CATA", "CONG"]
    assert out["tiers"] == {"held": [], "dated_catalyst": ["CATA"],
                            "congress_recency": ["CONG"], "liquidity": []}


def test_researched_set_liquidity_tiebreak_ranks_higher_volume_first():
    out = RS.researched_set(held_symbols=[], eligible_symbols=["LIQA", "LIQB"],
                            liquidity_by_symbol={"LIQA": 100.0, "LIQB": 500.0}, ceiling=1)
    assert out["researched"] == ["LIQB"]


def test_researched_set_a_catalyst_symbol_is_not_double_counted_in_congress_tier():
    out = RS.researched_set(held_symbols=[], eligible_symbols=["BOTH"],
                            catalyst_dates={"BOTH": "2026-09-10"},
                            congress_recency={"BOTH": "2026-09-01"}, ceiling=5)
    assert out["tiers"]["dated_catalyst"] == ["BOTH"]
    assert out["tiers"]["congress_recency"] == []


def test_researched_set_reports_eligible_researched_and_cut_for_budget():
    out = RS.researched_set(held_symbols=["A"], eligible_symbols=["A", "B", "C", "D"], ceiling=2)
    assert out["eligible_count"] == 4
    assert out["researched_count"] == 2
    assert out["cut_for_budget"] == 2


def test_researched_set_no_cut_when_ceiling_covers_everything():
    out = RS.researched_set(held_symbols=["A"], eligible_symbols=["A", "B"], ceiling=10)
    assert out["cut_for_budget"] == 0
    assert out["researched_count"] == 2


def test_researched_set_held_count_exceeding_ceiling_is_never_truncated():
    """Held positions are a standing obligation, not a budget decision --
    if there are more of them than the ceiling, every one is still
    researched and the ceiling is exceeded, not the other way around."""
    out = RS.researched_set(held_symbols=["A", "B", "C"], eligible_symbols=["A", "B", "C", "D"],
                            ceiling=1)
    assert set(out["researched"]) == {"A", "B", "C"}
    assert out["researched_count"] == 3


def test_researched_set_default_ceiling_is_the_documented_constant():
    out = RS.researched_set(held_symbols=[], eligible_symbols=[f"S{i}" for i in range(100)])
    assert out["researched_count"] == RS.DEFAULT_RESEARCH_SET_CEILING


def test_top_movers_symbols_flattens_the_real_response():
    raw = _load("top_gainers_losers.json")
    out = RS.top_movers_symbols(raw)
    assert "CHPT" in out and "NCT" in out and "NVDA" in out
    assert all(s == s.upper() for s in out)


def test_top_movers_symbols_empty_for_no_response():
    assert RS.top_movers_symbols(None) == []


# --------------------------------------------------------------- weather

def test_weather_items_only_for_mapped_symbols():
    out = RS.weather_items(["XOM", "AAPL"], {"heating_degree_days": {"value": 12}}, asof=ASOF)
    symbols = {i.symbol for i in out}
    assert symbols == {"XOM"}


def test_weather_item_fails_loudly_when_variable_missing_from_payload():
    out = RS.weather_items(["XOM"], {}, asof=ASOF)
    assert len(out) == 1 and out[0].quality == "failed"


def test_weather_items_empty_for_no_mapped_symbols():
    assert RS.weather_items(["AAPL", "MSFT"], {}, asof=ASOF) == []


# --------------------------------------------------------------- news (real fixtures)

def test_news_from_alpha_vantage_parses_the_real_recorded_response():
    """Fixture corrected 5 September 2026 to include the real `ticker_sentiment`
    array -- the original capture predated the discovery that it exists, and
    an earlier version of this test would have passed even with no filtering
    on it at all. Article 2 is a ConocoPhillips story that names OXY only as a
    lower-relevance secondary ticker; it is correctly kept, with that lower
    relevance score carried through, not dropped just for being about a peer."""
    raw = _load("news_sentiment_oxy.json")
    items = RS.news_items_from_alpha_vantage(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 2
    assert all(i.symbol == "OXY" and i.quality == "ok" for i in items)
    assert items[0].source == "Alpha Vantage NEWS_SENTIMENT"
    assert "Occidental" in items[0].value["title"]
    assert items[0].value["relevance_score"] == pytest.approx(0.542931)
    assert items[1].value["relevance_score"] == pytest.approx(0.091072)


def test_news_from_alpha_vantage_fails_on_empty_feed():
    items = RS.news_items_from_alpha_vantage({"feed": []}, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "failed"


def test_news_from_alpha_vantage_fails_on_none():
    items = RS.news_items_from_alpha_vantage(None, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_news_from_alpha_vantage_rejects_a_cross_wired_response():
    """The 5 September 2026 regression: batched concurrent NEWS_SENTIMENT
    calls for different tickers came back with each other's content --
    real articles, real sentiment scores, wrong ticker entirely. This fixture
    is what an XOM request actually returned that day: three articles, all
    genuinely about gold/GLDM, none naming XOM in their own ticker_sentiment.
    Silently returning them as XOM news would have fed fabricated
    corroboration into the five-condition gate; the majority-mismatch must
    surface as one failed item, not three that happen to be wrong."""
    raw = _load("news_sentiment_cross_wired_xom.json")
    items = RS.news_items_from_alpha_vantage(raw, symbol="XOM", asof=ASOF)
    assert len(items) == 1
    assert items[0].quality == "failed"
    assert "cross-wired" in items[0].detail
    assert items[0].value == {"total": 3, "matching": 0, "dropped": 3}


def test_news_from_alpha_vantage_drops_a_minority_mismatch_quietly():
    """A single article genuinely not about the requested symbol -- ordinary
    Alpha Vantage tagging noise, not the batching bug -- is dropped and the
    rest returned normally; this is not the majority-mismatch failure case."""
    raw = {"feed": [
        {"title": "OXY story", "time_published": "t1", "overall_sentiment_score": 0.1,
         "overall_sentiment_label": "Neutral",
         "ticker_sentiment": [{"ticker": "OXY", "relevance_score": "0.5"}]},
        {"title": "Unrelated story", "time_published": "t2", "overall_sentiment_score": 0.2,
         "overall_sentiment_label": "Neutral",
         "ticker_sentiment": [{"ticker": "CVX", "relevance_score": "0.6"}]},
    ]}
    items = RS.news_items_from_alpha_vantage(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 1
    assert items[0].quality == "ok" and items[0].value["title"] == "OXY story"


def test_news_from_alpha_vantage_symbol_match_is_case_insensitive():
    raw = {"feed": [
        {"title": "story", "time_published": "t1", "overall_sentiment_score": 0.1,
         "overall_sentiment_label": "Neutral",
         "ticker_sentiment": [{"ticker": "oxy", "relevance_score": "0.5"}]},
    ]}
    items = RS.news_items_from_alpha_vantage(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "ok"


def test_news_from_alpha_vantage_missing_relevance_score_does_not_crash():
    raw = {"feed": [
        {"title": "story", "time_published": "t1", "overall_sentiment_score": 0.1,
         "overall_sentiment_label": "Neutral",
         "ticker_sentiment": [{"ticker": "OXY"}]},
    ]}
    items = RS.news_items_from_alpha_vantage(raw, symbol="OXY", asof=ASOF)
    assert items[0].quality == "ok" and items[0].value["relevance_score"] is None


def test_news_from_robinhood_parses_the_real_recorded_response():
    """4 September 2026: the real key is data.articles, not data.news --
    the original parser read the wrong key and would have reported
    'failed' on every real Robinhood response, including this one."""
    raw = _load("robinhood_news_oxy.json")
    items = RS.news_items_from_robinhood(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 2
    assert items[0].quality == "ok"
    assert items[0].value["publisher"] == "Benzinga"


def test_news_from_robinhood_fails_on_the_old_wrong_key_shape():
    """Guards the exact regression: a response shaped like the one this
    parser used to assume (data.news) must not be silently accepted."""
    items = RS.news_items_from_robinhood({"data": {"news": [{"title": "x"}]}}, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


@pytest.mark.parametrize("wrap", ["full", "data_only", "bare_list", "json_string", "articles_key"])
def test_news_from_robinhood_tolerates_every_way_the_response_can_be_handed_over(wrap):
    """21 September 2026: eleven real BB articles from two publishers came out
    of gather as zero usable items, costing the one candidate its second source.
    The parser was right for the real shape, so the hand-off was the variable."""
    import json as _json
    full = _load("robinhood_news_oxy.json")
    raw = {"full": full, "data_only": full["data"], "bare_list": full["data"]["articles"],
           "json_string": _json.dumps(full),
           "articles_key": {"articles": full["data"]["articles"]}}[wrap]
    items = RS.news_items_from_robinhood(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 2 and all(i.quality == "ok" for i in items)


def test_gather_row_count_matches_items_for_every_robinhood_hand_off_shape():
    full = _load("robinhood_news_oxy.json")
    for raw in (full, full["data"], full["data"]["articles"]):
        bundle = RS.gather({"news_rh": {"OXY": raw}}, held_or_candidate=["OXY"], asof=ASOF)
        assert bundle.coverage_issues() == []
        assert bundle.coverage["news_rh:OXY"] == {"rows_in": 2, "items_out": 2}


def test_two_news_sources_together_satisfy_corroboration():
    av = RS.news_items_from_alpha_vantage(_load("news_sentiment_oxy.json"), symbol="OXY", asof=ASOF)
    rh = RS.news_items_from_robinhood(_load("robinhood_news_oxy.json"), symbol="OXY", asof=ASOF)
    bundle = _bundle_with(*av, *rh)
    assert bundle.corroborated("OXY")


# --------------------------------------------------------------- congress (real, per-symbol)

def test_congress_trade_items_parses_the_real_recorded_response():
    """4 September 2026: CONGRESS_TRADES is a dict with a `trades` list,
    not a bulk list across symbols; rows key on `symbol`, `transaction_type`,
    `amount_min`/`amount_max` -- an earlier version of this parser read
    `ticker`, `transaction`, and `amount`, none of which exist, so every
    row was filtered out."""
    raw = _load("congress_trades_oxy.json")
    items = RS.congress_trade_items(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 4
    assert all(i.symbol == "OXY" and i.quality == "ok" for i in items)
    assert "Gilbert" in items[0].mechanism or "Cisneros" in items[0].mechanism


def test_congress_trade_items_uses_party_and_state_already_on_the_row():
    """No POLITICIAN_METADATA join is needed for these fields -- they are
    already present on each trade row."""
    raw = _load("congress_trades_oxy.json")
    items = RS.congress_trade_items(raw, symbol="OXY", asof=ASOF)
    assert "D" in items[0].mechanism or "R" in items[0].mechanism
    assert "CA" in items[0].mechanism or any(
        s in items[0].mechanism for s in ("CA", "DE", "PA"))


def test_congress_trade_items_handles_a_row_with_null_politician_metadata():
    """One real row (Rob Bresnahan) has null bioguide_id/party/state --
    must not crash, and must fall back to a readable "unrecorded" label."""
    raw = _load("congress_trades_oxy.json")
    items = RS.congress_trade_items(raw, symbol="OXY", asof=ASOF)
    bresnahan = [i for i in items if "Bresnahan" in i.mechanism]
    assert bresnahan and "unrecorded" in bresnahan[0].mechanism


def test_congress_trades_fails_loudly_when_feed_unavailable():
    items = RS.congress_trade_items(None, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "failed"


def test_congress_trades_handles_a_preview_envelope():
    """Modeled on the real envelope shape confirmed live via
    INSIDER_TRANSACTIONS -- CONGRESS_TRADES never actually returned a
    preview for OXY (58 rows is well under the token limit), so this
    exercises the same code path with a constructed envelope."""
    fake_preview = {"preview": True, "total_lines": 500, "full_data_tokens": 40000,
                    "data_url": "https://example.test/x.json", "message": "truncated"}
    items = RS.congress_trade_items(fake_preview, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "degraded"
    assert items[0].value["total_lines"] == 500


def test_congress_trades_raises_a_shape_error_on_a_missing_trades_key():
    with pytest.raises(RS.ResearchShapeError):
        RS.congress_trade_items({"symbol": "OXY"}, symbol="OXY", asof=ASOF)


# --------------------------------------------------------------- insider (real, per-symbol)

def test_insider_transaction_items_parses_the_real_full_data_response():
    """4 September 2026: rows key on `ticker`, not `symbol` -- swapped
    with CONGRESS_TRADES's field name in the original parser, so every row
    was filtered out in both directions."""
    raw = _load("insider_transactions_oxy_full.json")
    items = RS.insider_transaction_items(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 5
    assert all(i.symbol == "OXY" and i.quality == "ok" for i in items)


def test_insider_transaction_items_distinguishes_acquisitions_from_disposals():
    raw = _load("insider_transactions_oxy_full.json")
    items = RS.insider_transaction_items(raw, symbol="OXY", asof=ASOF)
    assert any("acquired" in i.mechanism for i in items)
    assert any("disposed" in i.mechanism for i in items)


def test_insider_transactions_handles_the_real_preview_envelope():
    """The exact response OXY returned live: 27,944 total_lines, 248,328
    full_data_tokens, truncated to a 26-line sample_data string this
    parser must never parse as though it were the whole response. An
    earlier version of this function assumed data_total_count/
    data_truncated keys that don't exist in the real envelope -- both
    always read as None."""
    raw = _load("insider_transactions_oxy_preview.json")
    items = RS.insider_transaction_items(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 1
    assert items[0].quality == "degraded"
    assert items[0].value["total_lines"] == 27944
    assert items[0].value["full_data_tokens"] == 248328
    assert "return_full_data" in items[0].mechanism


def test_insider_transactions_fails_loudly_when_feed_unavailable():
    items = RS.insider_transaction_items(None, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_insider_transactions_raises_a_shape_error_on_a_missing_data_key():
    with pytest.raises(RS.ResearchShapeError):
        RS.insider_transaction_items({"unexpected": []}, symbol="OXY", asof=ASOF)


# --------------------------------------------------------------- earnings calendar (real, CSV-wrapped)

def test_earnings_calendar_parses_the_real_csv_wrapped_response():
    """4 September 2026: EARNINGS_CALENDAR has no datatype=json option --
    always {"result": "<CSV text>"}. An earlier version of this parser
    assumed a bare list of dicts and would have crashed or matched nothing
    against the real response."""
    raw = _load("earnings_calendar_aapl.json")
    items = RS.earnings_calendar_items(raw, held_or_candidate=["AAPL"], asof=ASOF)
    assert len(items) == 1
    assert items[0].symbol == "AAPL"
    assert "2026-10-29" in items[0].mechanism


def test_earnings_calendar_real_empty_response_produces_no_items():
    """OXY's real response for its own horizon was header-only (no
    earnings due) -- a correct empty result, not a parse failure."""
    raw = _load("earnings_calendar_oxy.json")
    items = RS.earnings_calendar_items(raw, held_or_candidate=["OXY"], asof=ASOF)
    assert items == []


def test_earnings_calendar_filters_to_watched_symbols():
    raw = _load("earnings_calendar_aapl.json")
    items = RS.earnings_calendar_items(raw, held_or_candidate=["OXY"], asof=ASOF)
    assert items == [], "AAPL's row must not attach when AAPL isn't held or candidate"


def test_earnings_calendar_fails_loudly_when_feed_unavailable():
    items = RS.earnings_calendar_items(None, held_or_candidate=["OXY"], asof=ASOF)
    assert items[0].quality == "failed"


# --------------------------------------------------------------- earnings estimates (real)

def test_earnings_estimate_items_parses_the_real_response():
    raw = _load("earnings_estimates_oxy.json")
    items = RS.earnings_estimate_items(raw, symbol="OXY", asof=ASOF)
    assert items[0].quality == "ok"
    assert items[0].value[0]["date"] == "2026-12-31"


def test_earnings_estimate_items_failed_on_none():
    assert RS.earnings_estimate_items(None, symbol="OXY", asof=ASOF)[0].quality == "failed"


# --------------------------------------------------------------- IPO calendar (dropped -- see below)

def test_ipo_calendar_real_response_has_no_sector_key():
    """`IPO_CALENDAR` was dropped from `gather()` entirely (4 September
    2026): its real schema (symbol,name,ipoDate,priceRangeLow,
    priceRangeHigh,currency,exchange) has no `sector` field, so it
    structurally cannot satisfy Rule 1 (attach to something). This test
    documents the real shape so a future response adding a usable field
    is caught -- see HANDOFF.md section 11 for why the feed was removed
    rather than kept as a permanently-empty call."""
    raw = _load("ipo_calendar.json")
    rows = RS._parse_av_csv_result(raw, "IPO_CALENDAR")
    assert "sector" not in rows[0]
    assert set(rows[0].keys()) == {"symbol", "name", "ipoDate", "priceRangeLow",
                                    "priceRangeHigh", "currency", "exchange"}


# --------------------------------------------------------------- filings / transcripts (not live-verified)

def test_earnings_call_transcript_requires_explicit_horizon_reason():
    items = RS.earnings_call_transcript_items(
        {"transcript": "..."}, symbol="OXY",
        horizon_reason="OXY reports within the open thesis's 21-day horizon", asof=ASOF)
    assert items[0].mechanism == "OXY reports within the open thesis's 21-day horizon"


def test_filing_items_ok_and_failed_independently():
    items = RS.filing_items({"form": "10-Q"}, {"revenue": "123"}, symbol="OXY", asof=ASOF)
    assert len(items) == 2 and all(i.quality == "ok" for i in items)
    items2 = RS.filing_items(None, None, symbol="OXY", asof=ASOF)
    assert items2[0].quality == "failed"


# --------------------------------------------------------------- macro (real, CSV + JSON shapes)

def test_macro_item_rejects_unknown_channel():
    with pytest.raises(ValueError, match="macro channel"):
        RS.macro_item("NOT_A_CHANNEL", {"data": [{}]}, asof=ASOF)


def test_macro_item_parses_the_real_csv_wrapped_response():
    """4 September 2026: CPI's default response is {"result": "<CSV
    text>"}, not {"data": [...]} -- an earlier version of this parser
    would have raised a KeyError on every real macro response."""
    raw = _load("cpi_csv.json")
    item = RS.macro_item("CPI", raw, asof=ASOF)
    assert item.quality == "ok"
    assert item.value["date"] == "2026-07-01"
    assert item.value["value"] == "333.918"


def test_macro_item_flags_a_real_malformed_value_as_degraded():
    """The real CPI response contains a genuine malformed row,
    `2025-10-01,.` -- Alpha Vantage's own placeholder for a not-yet-final
    print. Only the LATEST row is used, so build a response whose latest
    row is the malformed one to exercise this path."""
    raw = {"result": "timestamp,value\r\n2025-10-01,.\r\n2025-09-01,324.800\r\n"}
    item = RS.macro_item("CPI", raw, asof=ASOF)
    assert item.quality == "degraded"
    assert "not a usable number" in item.detail


def test_macro_item_parses_the_real_json_shape():
    """WTI is a commodity channel, not a macro one (see commodity tests
    below) -- but it shares the same `datatype=json` response family
    handled by `_rows_from_series_response`, which is what this checks."""
    raw = _load("wti_json.json")
    rows = RS._rows_from_series_response(raw, "WTI")
    assert rows[0] == {"date": "2026-08-01", "value": "83.9"}


def test_macro_item_failed_on_none():
    assert RS.macro_item("CPI", None, asof=ASOF).quality == "failed"


def test_macro_item_handles_a_preview_envelope():
    """5 September 2026: a macro channel requested with too wide a lookback
    truncates to a preview envelope, which `_rows_from_series_response`
    cannot distinguish from a genuine field-drift -- both used to raise the
    identical ResearchShapeError. Must report degraded/truncated, not
    'failed: neither data nor result', so the two causes stay distinguishable
    in System health."""
    fake_preview = {"preview": True, "total_lines": 9000, "full_data_tokens": 120000,
                    "data_url": "https://example.test/x.json", "message": "truncated"}
    item = RS.macro_item("TREASURY_YIELD", fake_preview, asof=ASOF)
    assert item.quality == "degraded"
    assert item.value["total_lines"] == 9000


def test_every_documented_macro_channel_is_recognised():
    for channel in RS.MACRO_CHANNELS:
        item = RS.macro_item(channel, {"data": [{"date": "2026-01-01", "value": "1"}]}, asof=ASOF)
        assert item.quality == "ok"


def test_series_response_raises_shape_error_on_neither_shape():
    with pytest.raises(RS.ResearchShapeError):
        RS._rows_from_series_response({"unexpected": 1}, "CPI")


# --------------------------------------------------------------- commodities (real WTI)

def test_commodity_items_parses_the_real_wti_response():
    raw = _load("wti_json.json")
    out = RS.commodity_items(["XOM"], {"WTI": raw, "BRENT": None, "NATURAL_GAS": None}, asof=ASOF)
    wti_item = [i for i in out if i.channel == "commodity:WTI"][0]
    assert wti_item.quality == "ok"
    assert wti_item.value["date"] == "2026-08-01"


def test_commodity_items_only_for_exposed_symbols():
    raw = _load("wti_json.json")
    out = RS.commodity_items(["XOM", "AAPL"], {"WTI": raw}, asof=ASOF)
    symbols = {i.symbol for i in out}
    assert "AAPL" not in symbols
    assert "XOM" in symbols


def test_commodity_items_reports_failed_for_exposed_symbol_with_no_data():
    out = RS.commodity_items(["XOM"], {}, asof=ASOF)
    assert any(i.quality == "failed" for i in out)


def test_commodity_items_handles_a_preview_envelope():
    """Same reasoning as macro_item's preview test -- an oversized
    commodity response must report degraded/truncated, not a generic
    shape-error failure, so the two causes are distinguishable."""
    fake_preview = {"preview": True, "total_lines": 9000, "full_data_tokens": 120000,
                    "data_url": "https://example.test/x.json", "message": "truncated"}
    out = RS.commodity_items(["XOM"], {"WTI": fake_preview}, asof=ASOF)
    wti_item = [i for i in out if i.channel == "commodity:WTI"][0]
    assert wti_item.quality == "degraded"
    assert wti_item.symbol == "XOM"


def test_commodity_items_isolates_a_shape_error_to_its_own_channel():
    """A shape drift in one commodity channel's response must not take
    down the other channels sharing this same call -- XOM has WTI/BRENT/
    NATURAL_GAS exposure, all gathered through a single commodity_items()
    call in gather()."""
    good = _load("wti_json.json")
    broken = {"unexpected_key": []}  # missing both "result" and "data"
    out = RS.commodity_items(["XOM"], {"WTI": good, "BRENT": broken, "NATURAL_GAS": None}, asof=ASOF)
    by_channel = {i.channel: i for i in out}
    assert by_channel["commodity:WTI"].quality == "ok"
    assert by_channel["commodity:BRENT"].quality == "failed"
    assert by_channel["commodity:NATURAL_GAS"].quality == "failed"


def test_gold_silver_spot_parses_the_real_scalar_quote():
    """4 September 2026: GOLD_SILVER_SPOT is a live scalar quote
    ({"nominal":, "timestamp":, "price":}), not a time series -- the
    first version of this parser routed it through
    _rows_from_series_response like every other commodity channel and
    raised ResearchShapeError on every single call, since a scalar quote
    has neither a "result" nor a "data" key. GLDM/GLD/IAU are the only
    real holders of this exposure (COMMODITY_EXPOSURE), all gold ETFs."""
    raw = _load("gold_silver_spot_gold.json")
    out = RS.commodity_items(["GLDM"], {"GOLD_SILVER_SPOT": raw}, asof=ASOF)
    assert len(out) == 1
    item = out[0]
    assert item.channel == "commodity:GOLD_SILVER_SPOT"
    assert item.quality == "ok"
    assert item.value["value"] == "4424.4537790587"
    assert item.value["nominal"] == "XAUUSD"
    assert "gold-spot" in item.mechanism


def test_gold_silver_spot_raises_a_shape_error_on_missing_price_key():
    with pytest.raises(RS.ResearchShapeError):
        RS._gold_silver_spot_row({"nominal": "XAUUSD"})


def test_gold_silver_spot_shape_error_is_isolated_within_commodity_items():
    """A malformed gold-spot response must not take down WTI/BRENT for
    the same symbol -- same isolation guarantee as any other channel."""
    good_wti = _load("wti_json.json")
    broken_gold = {"nominal": "XAUUSD"}  # missing "price"
    out = RS.commodity_items(["XOM"], {"WTI": good_wti, "GOLD_SILVER_SPOT": broken_gold}, asof=ASOF)
    by_channel = {i.channel: i for i in out}
    assert by_channel["commodity:WTI"].quality == "ok"
    # XOM has no GOLD_SILVER_SPOT exposure per COMMODITY_EXPOSURE, so this
    # also confirms the broken fixture is simply never touched for XOM.
    assert "commodity:GOLD_SILVER_SPOT" not in by_channel

    out2 = RS.commodity_items(["GLDM"], {"GOLD_SILVER_SPOT": broken_gold}, asof=ASOF)
    assert out2[0].quality == "failed"


def test_commodity_items_rejects_unknown_channel_in_exposure_table(monkeypatch):
    monkeypatch.setitem(RS.COMMODITY_EXPOSURE, "ZZZZ", ("NOT_A_CHANNEL",))
    with pytest.raises(ValueError, match="commodity channel"):
        RS.commodity_items(["ZZZZ"], {}, asof=ASOF)


# --------------------------------------------------------------- positioning (real)

def test_put_call_items_parses_the_real_response():
    """4 September 2026: the real key is put_call_ratio_full_chain, a
    string -- an earlier version of this parser read `ratio`, which does
    not exist, so it always reported 'failed' regardless of whether the
    feed actually succeeded."""
    raw = _load("put_call_realtime_oxy.json")
    items = RS.put_call_items(raw, None, symbol="OXY", asof=ASOF)
    assert items[0].quality == "ok"
    assert items[0].value == "0.46"


def test_put_call_items_with_historical_context():
    raw = _load("put_call_realtime_oxy.json")
    items = RS.put_call_items(raw, {"put_call_ratio_full_chain": "0.75"}, symbol="OXY", asof=ASOF)
    assert "0.75" in items[0].detail


def test_put_call_items_failed_on_the_old_wrong_key():
    """Guards the exact regression."""
    items = RS.put_call_items({"ratio": 0.8}, None, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_put_call_items_failed_when_realtime_missing():
    items = RS.put_call_items(None, {"put_call_ratio_full_chain": "0.75"}, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_put_call_items_carries_no_near_term_signal_when_not_divergent():
    """Real recorded data end to end: the realtime full-chain ratio is
    0.46, and OXY's real nearest expiration (2026-09-04) was 0.45 -- a
    2% difference, not a signal. This must not manufacture a near-term
    item out of routine noise."""
    realtime = _load("put_call_realtime_oxy.json")
    historical = _load("put_call_historical_oxy.json")
    items = RS.put_call_items(realtime, historical, symbol="OXY", asof=ASOF)
    assert len(items) == 1
    assert items[0].channel == "positioning:put_call"


def test_put_call_items_carries_the_near_term_signal_when_genuinely_divergent():
    """4 September 2026: HISTORICAL_PUT_CALL_RATIO carries
    put_call_ratio_by_expiration, real information the first version of
    this parser discarded entirely. Reusing OXY's real recorded
    2026-09-11 row (value 2.07 against a 0.58 full-chain ratio, a 257%
    real divergence) as the nearest entry to exercise the signal path --
    every value here was genuinely observed live, only resequenced."""
    real_rows = _load("put_call_historical_oxy.json")["put_call_ratio_by_expiration"]
    divergent_first = {"put_call_ratio_full_chain": "0.58",
                       "put_call_ratio_by_expiration": [real_rows[1]] + real_rows}
    realtime = {"put_call_ratio_full_chain": "0.58"}
    items = RS.put_call_items(realtime, divergent_first, symbol="OXY", asof=ASOF)
    assert len(items) == 2
    near_term = [i for i in items if i.channel == "positioning:put_call_nearterm"][0]
    assert near_term.quality == "ok"
    assert near_term.value["date"] == "2026-09-11"
    assert "257%" in near_term.mechanism


def test_near_term_put_call_signal_returns_none_without_by_expiration():
    assert RS._near_term_put_call_signal("OXY", {"put_call_ratio_full_chain": "0.5"}, "0.5", asof=ASOF) is None


def test_near_term_put_call_signal_returns_none_on_empty_list():
    assert RS._near_term_put_call_signal(
        "OXY", {"put_call_ratio_by_expiration": []}, "0.5", asof=ASOF) is None


# --------------------------------------------------------------- top movers / market status (real)

def test_top_movers_items_parses_the_real_response():
    raw = _load("top_gainers_losers.json")
    items = RS.top_movers_items(raw, asof=ASOF)
    assert items[0].quality == "ok"


def test_top_movers_items_failed_on_none():
    assert RS.top_movers_items(None, asof=ASOF)[0].quality == "failed"


def test_market_status_item_parses_the_real_response():
    raw = _load("market_status.json")
    item = RS.market_status_item(raw, asof=ASOF)
    assert item.quality == "ok"


def test_market_status_item_failed_on_none():
    assert RS.market_status_item(None, asof=ASOF).quality == "failed"


# --------------------------------------------------------------- _row_count

def test_row_count_handles_the_real_robinhood_news_shape():
    """{"data": {"articles": [...]}} has a dict, not a list, under "data"
    -- the generic list-valued-key scan misses it, so this needs its own
    branch or every Robinhood news coverage count silently reads as 1
    regardless of how many articles actually came back."""
    raw = _load("robinhood_news_oxy.json")
    assert RS._row_count(raw) == 2


def test_row_count_zero_for_none():
    assert RS._row_count(None) == 0


# --------------------------------------------------------------- gather()

def test_gather_records_timing_for_every_feed_it_touches():
    raw_feeds = {
        "news_av": {"OXY": _load("news_sentiment_oxy.json")},
        "market_status": _load("market_status.json"),
    }
    bundle = RS.gather(raw_feeds, held_or_candidate=["OXY"])
    assert "news_av:OXY" in bundle.timings_ms
    assert "market_status" in bundle.timings_ms
    assert all(ms >= 0 for ms in bundle.timings_ms.values())


def test_gather_records_skipped_feeds_rather_than_silently_omitting_them():
    bundle = RS.gather({}, held_or_candidate=["OXY"])
    assert any("news_av:OXY" in s for s in bundle.skipped)
    assert any("congress_trades" in s for s in bundle.skipped)


def test_gather_skips_weather_cleanly_when_nothing_maps():
    bundle = RS.gather({}, held_or_candidate=["AAPL"])
    assert any("no held/candidate symbol maps to a weather variable" in s for s in bundle.skipped)


def test_gather_uses_per_symbol_congress_and_insider_maps():
    raw_feeds = {
        "congress_trades": {"OXY": _load("congress_trades_oxy.json")},
        "insider_transactions": {"OXY": _load("insider_transactions_oxy_full.json")},
    }
    bundle = RS.gather(raw_feeds, held_or_candidate=["OXY"])
    assert bundle.for_channel("congress_trade")
    assert bundle.for_channel("insider_transaction")
    assert bundle.coverage["congress_trades:OXY"]["rows_in"] == 4
    assert bundle.coverage["insider_transactions:OXY"]["rows_in"] == 5


def test_gather_catches_a_shape_error_from_one_feed_without_aborting_others():
    """A single feed's shape drift must not take down the whole gather --
    it becomes one loud failed item, and everything else still runs."""
    raw_feeds = {
        "congress_trades": {"OXY": {"unexpected_key": []}},  # missing "trades"
        "news_av": {"OXY": _load("news_sentiment_oxy.json")},
    }
    bundle = RS.gather(raw_feeds, held_or_candidate=["OXY"])
    congress_items = bundle.for_channel("congress_trade")
    assert congress_items and congress_items[0].quality == "failed"
    assert bundle.for_symbol("OXY"), "the news feed must still have produced items"


def test_gather_end_to_end_produces_a_usable_bundle_from_real_fixtures():
    raw_feeds = {
        "news_av": {"OXY": _load("news_sentiment_oxy.json")},
        "news_rh": {"OXY": _load("robinhood_news_oxy.json")},
        "congress_trades": {"OXY": _load("congress_trades_oxy.json")},
        "insider_transactions": {"OXY": _load("insider_transactions_oxy_full.json")},
        "macro": {"CPI": _load("cpi_csv.json")},
        "commodities": {"WTI": _load("wti_json.json")},
        "market_status": _load("market_status.json"),
        "top_movers": _load("top_gainers_losers.json"),
        "put_call_realtime": {"OXY": _load("put_call_realtime_oxy.json")},
    }
    bundle = RS.gather(raw_feeds, held_or_candidate=["OXY"])
    assert bundle.corroborated("OXY")
    assert bundle.for_channel("congress_trade")
    assert bundle.for_channel("insider_transaction")
    assert bundle.for_channel("commodity:WTI")
    assert bundle.for_channel("macro:CPI")
    assert not bundle.coverage_issues(), f"unexpected coverage issues: {bundle.coverage_issues()}"
    assert bundle.asof
