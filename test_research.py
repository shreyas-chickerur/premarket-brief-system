"""Tests for research.py — all against recorded fixtures, never the live API.

research.py is pure functions over already-fetched data, the same boundary
quantcore.py draws against DataFrames, which is what makes this possible.
"""

import json
from pathlib import Path

import pytest

import research as RS

FIXTURES = Path(__file__).parent / "fixtures" / "research"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


ASOF = "2026-09-03T11:00:00Z"


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
    """Rule 1: everything must attach to something. A failed item is the one
    exception -- it has nothing to attach because the feed produced
    nothing, and that is itself reportable."""
    with pytest.raises(ValueError, match="attach"):
        RS.ResearchItem(channel="news", symbol="OXY", mechanism="",
                        value=1, source="s", asof=ASOF, quality="ok")
    # does not raise:
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


# --------------------------------------------------------------- candidates

def test_candidates_includes_held_symbols():
    out = RS.candidates(held_symbols=["oxy", "sgov"])
    assert "OXY" in out and "SGOV" in out


def test_candidates_includes_watchlist_and_top_movers():
    out = RS.candidates(held_symbols=[], watchlist_symbols=["vti"], top_movers=["nvda"])
    assert "VTI" in out and "NVDA" in out


def test_candidates_adds_sector_adjacent_names():
    out = RS.candidates(held_symbols=["XOM"])
    assert "CVX" in out, "CVX shares XOM's energy sector and should be pulled in as adjacent"


def test_candidates_is_deduplicated_and_sorted():
    out = RS.candidates(held_symbols=["oxy", "OXY"], watchlist_symbols=["oxy"])
    assert out.count("OXY") == 1
    assert out == sorted(out)


# --------------------------------------------------------------- weather

def test_weather_items_only_for_mapped_symbols():
    out = RS.weather_items(["XOM", "AAPL"], {"heating_degree_days": {"value": 12}}, asof=ASOF)
    symbols = {i.symbol for i in out}
    assert symbols == {"XOM"}, "AAPL has no weather mapping and must get no weather item at all"


def test_weather_item_fails_loudly_when_variable_missing_from_payload():
    out = RS.weather_items(["XOM"], {}, asof=ASOF)
    assert len(out) == 1 and out[0].quality == "failed"


def test_weather_items_empty_for_no_mapped_symbols():
    assert RS.weather_items(["AAPL", "MSFT"], {}, asof=ASOF) == []


# --------------------------------------------------------------- news

def test_news_from_alpha_vantage_parses_the_recorded_fixture():
    raw = _load("news_sentiment_oxy.json")
    items = RS.news_items_from_alpha_vantage(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 2
    assert all(i.symbol == "OXY" and i.quality == "ok" for i in items)
    assert items[0].source == "Alpha Vantage NEWS_SENTIMENT"


def test_news_from_alpha_vantage_fails_on_empty_feed():
    items = RS.news_items_from_alpha_vantage({"feed": []}, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].quality == "failed"


def test_news_from_alpha_vantage_fails_on_none():
    items = RS.news_items_from_alpha_vantage(None, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_news_from_robinhood_parses_the_recorded_fixture():
    raw = _load("robinhood_news_oxy.json")
    items = RS.news_items_from_robinhood(raw, symbol="OXY", asof=ASOF)
    assert len(items) == 1 and items[0].source == "Robinhood get_equity_news"


def test_two_news_sources_together_satisfy_corroboration():
    """This is the mechanical version of the five-condition gate's "two
    independent corroborating sources" requirement."""
    av = RS.news_items_from_alpha_vantage(_load("news_sentiment_oxy.json"), symbol="OXY", asof=ASOF)
    rh = RS.news_items_from_robinhood(_load("robinhood_news_oxy.json"), symbol="OXY", asof=ASOF)
    bundle = _bundle_with(*av, *rh)
    assert bundle.corroborated("OXY")


# --------------------------------------------------------------- congress / insider

def test_congress_trade_items_filters_to_watched_symbols():
    raw = _load("congress_trades.json")
    meta = _load("politician_metadata.json")
    items = RS.congress_trade_items(raw, meta, held_or_candidate=["OXY", "NVDA"], asof=ASOF)
    symbols = {i.symbol for i in items}
    assert symbols == {"OXY", "NVDA"}, "ZZZZ is not watched and must be filtered out"


def test_congress_trade_item_names_the_politician_and_committee():
    raw = _load("congress_trades.json")
    meta = _load("politician_metadata.json")
    items = RS.congress_trade_items(raw, meta, held_or_candidate=["OXY"], asof=ASOF)
    assert "Jane Example" in items[0].mechanism and "Energy and Commerce" in items[0].mechanism


def test_congress_trades_fails_loudly_when_feed_unavailable():
    items = RS.congress_trade_items(None, None, held_or_candidate=["OXY"], asof=ASOF)
    assert len(items) == 1 and items[0].quality == "failed" and items[0].symbol is None


def test_insider_transaction_items_filters_to_watched_symbols():
    raw = _load("insider_transactions.json")
    items = RS.insider_transaction_items(raw, held_or_candidate=["OXY"], asof=ASOF)
    assert len(items) == 1 and items[0].symbol == "OXY"
    assert "Chief Financial Officer" in items[0].mechanism


def test_insider_transactions_fails_loudly_when_feed_unavailable():
    items = RS.insider_transaction_items(None, held_or_candidate=["OXY"], asof=ASOF)
    assert items[0].quality == "failed"


# --------------------------------------------------------------- scheduled events

def test_earnings_calendar_filters_and_names_the_date():
    raw = [{"symbol": "OXY", "reportDate": "2026-11-04", "estimate": "1.20"},
          {"symbol": "ZZZZ", "reportDate": "2026-11-05"}]
    items = RS.earnings_calendar_items(raw, held_or_candidate=["OXY"], asof=ASOF)
    assert len(items) == 1 and "2026-11-04" in items[0].mechanism


def test_earnings_estimate_items_ok_and_failed():
    ok = RS.earnings_estimate_items({"eps": "1.20"}, symbol="OXY", asof=ASOF)
    assert ok[0].quality == "ok"
    failed = RS.earnings_estimate_items(None, symbol="OXY", asof=ASOF)
    assert failed[0].quality == "failed"


def test_ipo_calendar_only_attaches_to_a_held_sector():
    raw = [{"sector": "Energy", "name": "New Driller Co"}, {"sector": "Biotech", "name": "New Bio Co"}]
    items = RS.ipo_calendar_items(raw, sector_watch=["energy"], asof=ASOF)
    assert len(items) == 1
    assert items[0].value["name"] == "New Driller Co"


def test_earnings_call_transcript_requires_explicit_horizon_reason():
    items = RS.earnings_call_transcript_items(
        {"transcript": "..."}, symbol="OXY",
        horizon_reason="OXY reports within the open thesis's 21-day horizon", asof=ASOF)
    assert items[0].mechanism == "OXY reports within the open thesis's 21-day horizon"


# --------------------------------------------------------------- filings

def test_filing_items_ok_and_failed_independently():
    items = RS.filing_items({"form": "10-Q"}, {"revenue": "123"}, symbol="OXY", asof=ASOF)
    assert len(items) == 2 and all(i.quality == "ok" for i in items)
    items2 = RS.filing_items(None, None, symbol="OXY", asof=ASOF)
    assert items2[0].quality == "failed"


# --------------------------------------------------------------- macro

def test_macro_item_rejects_unknown_channel():
    with pytest.raises(ValueError, match="macro channel"):
        RS.macro_item("NOT_A_CHANNEL", {"data": [{}]}, asof=ASOF)


def test_macro_item_ok_and_failed():
    ok = RS.macro_item("CPI", {"data": [{"value": "312.3"}]}, asof=ASOF)
    assert ok.quality == "ok" and ok.channel == "macro:CPI"
    failed = RS.macro_item("CPI", None, asof=ASOF)
    assert failed.quality == "failed"


def test_every_documented_macro_channel_is_recognised():
    for channel in RS.MACRO_CHANNELS:
        item = RS.macro_item(channel, {"data": [{"value": "1"}]}, asof=ASOF)
        assert item.quality == "ok"


# --------------------------------------------------------------- commodities

def test_commodity_items_only_for_exposed_symbols():
    out = RS.commodity_items(["XOM", "AAPL"], {"WTI": {"data": [{"value": "82.1"}]}}, asof=ASOF)
    symbols = {i.symbol for i in out}
    assert "AAPL" not in symbols, "AAPL has no commodity exposure and must get nothing"
    assert "XOM" in symbols


def test_commodity_items_reports_failed_for_exposed_symbol_with_no_data():
    out = RS.commodity_items(["XOM"], {}, asof=ASOF)
    assert any(i.quality == "failed" for i in out)


def test_commodity_items_rejects_unknown_channel_in_exposure_table(monkeypatch):
    """If COMMODITY_EXPOSURE is ever edited to reference a channel not in
    COMMODITY_CHANNELS, that must fail loudly rather than silently produce
    an item tagged with a channel name nothing else recognises."""
    monkeypatch.setitem(RS.COMMODITY_EXPOSURE, "ZZZZ", ("NOT_A_CHANNEL",))
    with pytest.raises(ValueError, match="commodity channel"):
        RS.commodity_items(["ZZZZ"], {}, asof=ASOF)


# --------------------------------------------------------------- positioning / session

def test_put_call_items_ok_with_historical_context():
    items = RS.put_call_items({"ratio": 0.8}, {"ratio": 0.75}, symbol="OXY", asof=ASOF)
    assert items[0].quality == "ok" and "0.75" in items[0].detail


def test_put_call_items_failed_when_realtime_missing():
    items = RS.put_call_items(None, {"ratio": 0.75}, symbol="OXY", asof=ASOF)
    assert items[0].quality == "failed"


def test_top_movers_items_ok_and_failed():
    assert RS.top_movers_items({"top_gainers": []}, asof=ASOF)[0].quality == "ok"
    assert RS.top_movers_items(None, asof=ASOF)[0].quality == "failed"


def test_market_status_item_ok_and_failed():
    assert RS.market_status_item({"markets": []}, asof=ASOF).quality == "ok"
    assert RS.market_status_item(None, asof=ASOF).quality == "failed"


# --------------------------------------------------------------- gather()

def test_gather_records_timing_for_every_feed_it_touches():
    raw_feeds = {
        "news_av": {"OXY": _load("news_sentiment_oxy.json")},
        "market_status": {"markets": []},
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


def test_gather_end_to_end_produces_a_usable_bundle():
    raw_feeds = {
        "news_av": {"OXY": _load("news_sentiment_oxy.json")},
        "news_rh": {"OXY": _load("robinhood_news_oxy.json")},
        "congress_trades": _load("congress_trades.json"),
        "politician_metadata": _load("politician_metadata.json"),
        "insider_transactions": _load("insider_transactions.json"),
        "macro": {"CPI": {"data": [{"value": "312.3"}]}},
        "commodities": {"WTI": {"data": [{"value": "82.1"}]}},
        "market_status": {"markets": []},
    }
    bundle = RS.gather(raw_feeds, held_or_candidate=["OXY"])
    assert bundle.corroborated("OXY")
    assert bundle.for_channel("congress_trade")
    assert bundle.for_channel("commodity:WTI")
    assert bundle.for_channel("macro:CPI")
    assert bundle.asof
