"""Tests for the health, logging, and self-audit layer."""

from datetime import datetime, timezone
import json
import pytest

import runlog as R


TOOLS = ["robinhood.get_positions", "robinhood.place_equity_order",
         "robinhood.review_equity_order", "alphavantage.TIME_SERIES_DAILY",
         "gmail.send_message"]
REQUIRED = ["robinhood.get_positions", "robinhood.place_equity_order",
            "alphavantage.TIME_SERIES_DAILY", "gmail.send_message"]
GOOD_TEST = {"passed": True, "n_passed": 45, "n_failed": 0, "duration_ms": 1600}
NORMAL_TIME = datetime(2026, 9, 1, 6, 20)          # Tuesday 06:20 local


def base_log(**kw):
    return R.RunLog("run-test", now=datetime(2026, 9, 1, 11, 20, tzinfo=timezone.utc), **kw)


# ---------------------------------------------------------------- stages

def test_stage_records_success_and_timing():
    log = base_log()
    with log.stage("gather") as s:
        s.records = 12
    assert len(log.stages) == 1
    st = log.stages[0]
    assert st.ok and st.name == "gather" and st.records == 12
    assert st.duration_ms >= 0


def test_stage_records_failure_and_reraises():
    log = base_log()
    with pytest.raises(ValueError):
        with log.stage("analyze"):
            raise ValueError("boom")
    assert log.stages[0].ok is False
    assert "ValueError: boom" in log.stages[0].error
    assert log.errors and not log.may_trade
    assert log.health() == "critical"


# ---------------------------------------------------------------- health

def test_health_nominal_when_everything_passes():
    log = base_log()
    log.check("a", True)
    log.call("alphavantage", "TIME_SERIES_DAILY", True, 120)
    with log.stage("gather"):
        pass
    assert log.health() == "nominal" and log.may_trade


def test_warning_degrades_but_still_permits_trading():
    log = base_log()
    log.check("fired_on_schedule", False, "warn", "off by 70 min")
    assert log.health() == "degraded"
    assert log.may_trade, "a warning must not silently stop trading"


def test_block_prevents_trading():
    log = base_log()
    log.check("tools_available", False, "block", "missing robinhood")
    assert log.health() == "critical" and not log.may_trade


def test_failed_external_call_degrades():
    log = base_log()
    log.call("alphavantage", "RSI", False, 5000, "timeout")
    assert log.health() == "degraded"


def test_manifest_is_json_serialisable_and_complete():
    log = base_log()
    log.check("x", True)
    log.call("s", "op", True, 10)
    log.anomaly({"code": "price_jump", "severity": "warn", "symbol": "ZZZZ", "message": "8 sigma"})
    log.decide(R.Decision("ZZZZ", "reject", "agentic", False, "failed gate 2", gate_failed="two_sources"))
    log.metric("symbols_examined", 40)
    with log.stage("s1"):
        pass
    d = json.loads(log.to_json())
    for k in ("schema", "run_id", "health", "may_trade", "checks", "stages",
              "calls", "anomalies", "decisions", "metrics", "errors"):
        assert k in d
    assert d["schema"] == R.SCHEMA_VERSION
    assert d["decisions"][0]["gate_failed"] == "two_sources"


def test_inaction_is_recorded_like_action():
    """A quiet day must leave the same audit trail as a busy one."""
    log = base_log()
    log.decide(R.Decision("-", "none", "agentic", False, "no idea cleared the gate"))
    assert json.loads(log.to_json())["decisions"][0]["action"] == "none"


# ---------------------------------------------------------------- preflight

def test_preflight_clean_run_permits_trading():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST)
    assert log.may_trade and log.health() == "nominal"


def test_preflight_aborts_when_tools_missing():
    log = R.preflight(base_log(), available_tools=["gmail.send_message"],
                      required_tools=REQUIRED, local_time=NORMAL_TIME,
                      expected_local_hhmm=(6, 20), self_test=GOOD_TEST)
    assert log.aborted and not log.may_trade
    assert "robinhood.get_positions" in log.abort_reason


def test_preflight_aborts_when_self_test_fails():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20),
                      self_test={"passed": False, "n_passed": 41, "n_failed": 4,
                                 "duration_ms": 1700, "summary": "4 failed"})
    assert log.aborted and "self test failed" in log.abort_reason


@pytest.mark.parametrize("fired,should_pass", [
    ((6, 20), True),    # exactly on time
    ((6, 45), True),    # 25 min late, inside tolerance
    ((5, 20), False),   # one hour early: daylight saving ended and nobody moved the cron
    ((7, 20), False),   # one hour late: the other direction
    ((0, 5), False),    # wildly wrong, must not wrap into passing
])
def test_preflight_schedule_drift(fired, should_pass):
    """A one-hour daylight-saving shift must always trip this check. Comparing
    against the top of the hour with a wide window previously let it pass."""
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=datetime(2026, 11, 2, *fired), expected_local_hhmm=(6, 20),
                      self_test=GOOD_TEST)
    c = next(c for c in log.checks if c.name == "fired_on_schedule")
    assert c.passed is should_pass
    assert c.severity == "warn"
    assert log.may_trade, "schedule drift warns, it never blocks trading"


def test_preflight_rejects_a_tolerance_that_cannot_detect_dst():
    with pytest.raises(ValueError):
        R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                    local_time=NORMAL_TIME, expected_local_hhmm=(6, 20),
                    tolerance_minutes=90, self_test=GOOD_TEST)


def test_preflight_rejects_invalid_expected_time():
    with pytest.raises(ValueError):
        R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                    local_time=NORMAL_TIME, expected_local_hhmm=(25, 0),
                    self_test=GOOD_TEST)


def test_preflight_detects_holiday_and_weekend():
    hol = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=datetime(2026, 9, 7, 6, 20), expected_local_hhmm=(6, 20),
                      self_test=GOOD_TEST)
    assert not next(c for c in hol.checks if c.name == "market_open_today").passed

    sat = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=datetime(2026, 9, 5, 6, 20), expected_local_hhmm=(6, 20),
                      self_test=GOOD_TEST)
    assert not next(c for c in sat.checks if c.name == "market_open_today").passed


def test_preflight_detects_early_close():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=datetime(2026, 11, 27, 6, 20), expected_local_hhmm=(6, 20),
                      self_test=GOOD_TEST)
    c = next(c for c in log.checks if c.name == "market_open_today")
    assert c.value["early_close"] is True


def test_preflight_aborts_on_ledger_broker_disagreement():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      ledger_positions={"AAA": 3, "BBB": 2},
                      broker_positions={"AAA": 3})
    assert log.aborted and "BBB" in log.abort_reason


def test_preflight_passes_when_ledger_agrees():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      ledger_positions={"AAA": 3}, broker_positions={"AAA": 3.0})
    assert log.may_trade


def test_preflight_survives_first_ever_run_with_no_history():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=[])
    assert log.may_trade
    assert next(c for c in log.checks if c.name == "history_depth").passed


# ---------------------------------------------------------------- regressions

def _hist(n=10, health="nominal", duration=60_000, checks=None, anomalies=None, calls=None):
    return [{"health": health, "duration_ms": duration,
             "checks": checks or [], "anomalies": anomalies or [],
             "calls": calls or [{"ok": True}], "decisions": [], "stages": []}
            for _ in range(n)]


def test_regression_flags_repeated_unhealthy_runs():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=_hist(8, health="degraded"))
    assert not next(c for c in log.checks if c.name == "recent_health").passed


def test_regression_flags_runtime_blowout():
    h = _hist(6, duration=60_000)
    h.append({**h[0], "duration_ms": 400_000})
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST, history=h)
    assert not next(c for c in log.checks if c.name == "duration_stable").passed


def test_regression_flags_chronic_check_failure():
    failing = [{"name": "alpha_vantage_fresh", "passed": False}]
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=_hist(8, checks=failing))
    c = next(c for c in log.checks if c.name == "no_chronic_failures")
    assert not c.passed and "alpha_vantage_fresh" in str(c.value)


def test_regression_flags_repeat_offender_symbol():
    anom = [{"symbol": "BADX", "severity": "block", "code": "stale_data"}]
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=_hist(6, anomalies=anom))
    c = next(c for c in log.checks if c.name == "no_repeat_data_faults")
    assert not c.passed and "BADX" in str(c.value)


def test_regression_flags_unreliable_external_calls():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=_hist(6, calls=[{"ok": False}, {"ok": True}]))
    assert not next(c for c in log.checks if c.name == "external_call_reliability").passed


def test_healthy_history_raises_no_regressions():
    log = R.preflight(base_log(), available_tools=TOOLS, required_tools=REQUIRED,
                      local_time=NORMAL_TIME, expected_local_hhmm=(6, 20), self_test=GOOD_TEST,
                      history=_hist(10))
    assert log.health() == "nominal"


# ---------------------------------------------------------------- optimizations

def test_optimizer_is_silent_without_enough_history():
    assert R.find_optimizations(_hist(3)) == []


def test_optimizer_spots_stops_that_are_too_tight():
    dec = [{"action": "stop_filled", "inputs": {"recovered_within_5d": True}}]
    found = R.find_optimizations([{**h, "decisions": dec} for h in _hist(8)])
    kinds = [f["kind"] for f in found]
    assert "stop_distance" in kinds
    f = next(f for f in found if f["kind"] == "stop_distance")
    assert f["confidence"] == "tentative" and f["sample"] == 8


def test_optimizer_spots_a_single_dominant_gate():
    dec = [{"action": "reject", "gate_failed": "two_sources"},
           {"action": "reject", "gate_failed": "two_sources"}]
    found = R.find_optimizations([{**h, "decisions": dec} for h in _hist(10)])
    assert any(f["kind"] == "gate_balance" for f in found)


def test_optimizer_spots_total_inactivity():
    found = R.find_optimizations([{**h, "decisions": [{"executed": False}]} for h in _hist(12)])
    assert any(f["kind"] == "throughput" for f in found)


def test_optimizer_spots_a_dominant_slow_stage():
    stages = [{"name": "web_research", "duration_ms": 90_000},
              {"name": "gather", "duration_ms": 5_000}]
    found = R.find_optimizations([{**h, "stages": stages} for h in _hist(8)])
    f = next(f for f in found if f["kind"] == "performance")
    assert "web_research" in f["finding"] and f["confidence"] == "measured"


def test_every_optimization_states_its_sample_size():
    dec = [{"action": "stop_filled", "inputs": {"recovered_within_5d": True}}]
    for f in R.find_optimizations([{**h, "decisions": dec} for h in _hist(8)]):
        assert "sample" in f and "confidence" in f and "proposal" in f


# ---------------------------------------------------------------- scoring

def test_scoring_refuses_to_certify_a_small_sample():
    closed = [{"outcome_pct": 4.0, "thesis_played_out": True, "horizon_days": 20}] * 12
    s = R.score_closed_decisions(closed)
    assert s["n"] == 12 and s["statistically_meaningful"] is False
    assert "too few" in s["verdict"]


def test_scoring_stays_provisional_even_at_larger_samples():
    closed = ([{"outcome_pct": 5.0, "thesis_played_out": True}] * 20 +
              [{"outcome_pct": -3.0, "thesis_played_out": False}] * 20)
    s = R.score_closed_decisions(closed)
    assert s["n"] == 40
    assert s["statistically_meaningful"] is False
    assert s["hit_rate"] == 0.5 and s["thesis_accuracy"] == 0.5


def test_scoring_empty():
    assert R.score_closed_decisions([])["n"] == 0


def test_scoring_computes_dispersion():
    closed = [{"outcome_pct": v, "thesis_played_out": True} for v in (10, -5, 3, 8, -2)]
    s = R.score_closed_decisions(closed)
    assert s["return_stdev_pct"] > 0 and s["best_pct"] == 10 and s["worst_pct"] == -5
