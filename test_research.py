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


def test_candidates_adds_sector_adjacent_names():
    out = RS.candidates(held_symbols=["XOM"])
    assert "CVX" in out


def test_candidates_is_deduplicated_and_sorted():
    out = RS.candidates(held_symbols=["oxy", "OXY"], watchlist_symbols=["oxy"])
    assert out.count("OXY") == 1
    assert out == sorted(out)


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
