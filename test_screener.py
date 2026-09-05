"""Tests for the real Robinhood-scanner candidate universe."""

import json

import screener as S


def _load(name):
    with open(f"fixtures/scanner/{name}") as f:
        return json.load(f)


# ---------------------------------------------------------------- filters

def test_core_universe_filters_v1_only_uses_enum_filters():
    """No expression filters -- get_scanner_datapoints and preview_scan
    were both unavailable in the session that built this, so nothing here
    could have been validated before saving."""
    for f in S.CORE_UNIVERSE_FILTERS_V1:
        assert "expression" not in f
        assert f["filter_type"].startswith("FILTER_TYPE_")


def test_core_universe_filters_v1_matches_the_real_verified_specs():
    """Every filter_type/predicate/interval/length pair must come from the
    real get_scanner_filter_specs fixture, not be hand-guessed."""
    specs = {s["filter_type"]: s for s in _load(
        "get_scanner_filter_specs_20260905.json")["data"]["filter_specs"]}
    for f in S.CORE_UNIVERSE_FILTERS_V1:
        spec = specs[f["filter_type"]]
        assert f["predicate"] in spec["supported_predicates"]
        if "interval" in f:
            assert f["interval"] in spec["supported_intervals"]
        if "length" in f:
            assert f["length"] in spec["supported_lengths"]


def test_core_universe_filters_v1_restricts_to_stocks_and_etfs():
    instrument_filter = next(f for f in S.CORE_UNIVERSE_FILTERS_V1
                             if f["filter_type"] == "FILTER_TYPE_INSTRUMENT_TYPE")
    assert set(instrument_filter["values"]) == {"STOCK", "ETF"}


def test_core_universe_filters_v1_has_a_price_floor():
    """5 September 2026: the first live run with no price floor surfaced
    sub-$0.35 penny stocks that cleared the liquidity/volatility bands
    anyway."""
    last_filter = next(f for f in S.CORE_UNIVERSE_FILTERS_V1
                       if f["filter_type"] == "FILTER_TYPE_LAST")
    assert last_filter["predicate"] == ">"
    assert float(last_filter["values"][0]) >= 5.0


# --------------------------------------------------------- sector bridging

def test_robinhood_sectors_for_maps_known_labels():
    out = S.robinhood_sectors_for(["technology", "energy"])
    assert out == ["Energy", "Technology"]


def test_robinhood_sectors_for_drops_unmapped_and_none():
    out = S.robinhood_sectors_for(["technology", None, "not_a_real_sector"])
    assert out == ["Technology"]


def test_robinhood_sectors_for_dedupes():
    out = S.robinhood_sectors_for(["technology", "consumer_discretionary", "consumer_cyclical"])
    assert out == ["Consumer Cyclical", "Technology"]


def test_robinhood_sectors_for_case_insensitive():
    assert S.robinhood_sectors_for(["TECHNOLOGY"]) == ["Technology"]


def test_build_filters_adds_sector_filter_when_sectors_known():
    filters = S.build_filters(["technology", "energy"])
    sector_filter = next(f for f in filters if f["filter_type"] == "FILTER_TYPE_SECTOR")
    assert set(sector_filter["values"]) == {"Technology", "Energy"}
    # base filters still present, complete-replacement semantics
    assert len(filters) == len(S.CORE_UNIVERSE_FILTERS_V1) + 1


def test_build_filters_omits_sector_filter_when_nothing_maps():
    """A diversified fund's None, or an unmapped label, must not produce a
    broken or empty sector filter -- just no sector filter at all."""
    filters = S.build_filters([None, "not_a_real_sector"])
    assert filters == S.CORE_UNIVERSE_FILTERS_V1
    assert not any(f["filter_type"] == "FILTER_TYPE_SECTOR" for f in filters)


def test_build_filters_with_no_held_sectors_at_all():
    assert S.build_filters([]) == S.CORE_UNIVERSE_FILTERS_V1


# --------------------------------------------------- parse_scan_result (real)

def test_parse_scan_result_parses_the_real_recorded_response():
    """fixtures/scanner/run_scan_core_universe_v1_20260905.json is the
    ACTUAL response from a real create_scan/update_scan_filters call
    against PBS Core Universe v1, not a hand-built fixture. Neither
    create_scan's nor run_scan's own tool description matches these real
    top-level keys (they claim id/title/total/instruments/sorting/filters;
    the real keys are scan_id/scan_title/total_items/results/sorted_by/
    filters_applied) -- this is exactly the kind of assumption research.py
    was rewritten to stop making, applied to a second tool."""
    raw = _load("run_scan_core_universe_v1_20260905.json")
    out = S.parse_scan_result(raw)
    assert len(out) == 5
    assert out[0] == {"symbol": "AMBP", "last": 5.01, "avg_volume": 2384658.316207,
                      "sector_code": "102"}
    assert all(r["last"] >= 5.0 for r in out)
    assert all(r["avg_volume"] > 500000 for r in out)


def test_parse_scan_result_empty_on_none():
    assert S.parse_scan_result(None) == []


def test_parse_scan_result_drops_a_row_with_no_ticker():
    raw = {"results": [{"columns": {"Last": "10.0"}}]}
    assert S.parse_scan_result(raw) == []


def test_parse_scan_result_drops_an_uncastable_last_rather_than_crashing():
    raw = {"results": [{"ticker": "XYZ", "columns": {"Last": "not_a_number"}}]}
    out = S.parse_scan_result(raw)
    assert out == [{"symbol": "XYZ", "last": None, "avg_volume": None, "sector_code": None}]


def test_parse_scan_result_drops_an_uncastable_avg_volume_rather_than_crashing():
    raw = {"results": [{"ticker": "XYZ",
                        "columns": {"Last": "10.0", "Average volume": "not_a_number"}}]}
    out = S.parse_scan_result(raw)
    assert out == [{"symbol": "XYZ", "last": 10.0, "avg_volume": None, "sector_code": None}]


def test_parse_scan_result_uppercases_symbols():
    raw = {"results": [{"ticker": "abc", "columns": {"Last": "10.0"}}]}
    assert S.parse_scan_result(raw)[0]["symbol"] == "ABC"


# ------------------------------------------------------ affordable_for_agentic

def test_affordable_for_agentic_filters_by_real_account_math():
    results = [{"symbol": "CHEAP", "last": 50.0}, {"symbol": "EXPENSIVE", "last": 500.0}]
    out = S.affordable_for_agentic(results, account_equity=1000.0, max_weight_agentic=0.18)
    # ceiling = 1000 * 0.18 = 180
    assert out == ["CHEAP"]


def test_affordable_for_agentic_empty_when_equity_or_weight_zero():
    results = [{"symbol": "CHEAP", "last": 1.0}]
    assert S.affordable_for_agentic(results, account_equity=0, max_weight_agentic=0.18) == []
    assert S.affordable_for_agentic(results, account_equity=1000, max_weight_agentic=0) == []


def test_affordable_for_agentic_skips_rows_with_no_price():
    results = [{"symbol": "NOPRICE", "last": None}]
    assert S.affordable_for_agentic(results, account_equity=1000, max_weight_agentic=0.18) == []


def test_affordable_for_agentic_never_caches_recomputes_from_live_inputs():
    """Same results, different equity -> different answer -- proving there
    is no memoisation silently reusing a stale ceiling."""
    results = [{"symbol": "MID", "last": 100.0}]
    small = S.affordable_for_agentic(results, account_equity=500, max_weight_agentic=0.18)
    large = S.affordable_for_agentic(results, account_equity=5000, max_weight_agentic=0.18)
    assert small == [] and large == ["MID"]
