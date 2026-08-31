"""Tests for the evidence framework.

Known-answer wherever possible: feed in a record with a KNOWN edge (or a known
absence of one) and require the verdict to match. A framework that returns
"promising" on noise is the exact failure this module exists to prevent, so the
tests are weighted toward proving it says no when the answer is no.
"""

import math
from datetime import date, timedelta

import numpy as np
import pytest

import evidence as E


PREREG = E.PreRegistration(
    hypothesis="The five-condition gate produces positive excess return over SPY after costs.",
    target_edge_pct=0.50,
    horizon_days=21,
    decide_by=date(2027, 6, 30),
    assumed_sd_pct=4.0,
)


def _outcomes(excesses, start=date(2026, 9, 1), played=None):
    """Build outcomes with an exact excess return, benchmark held flat."""
    out = []
    for i, ex in enumerate(excesses):
        entry = 100.0
        # excess = return - benchmark - cost; benchmark flat, so return = ex + cost
        exit_ = entry * (1 + (ex + PREREG.cost_pct) / 100.0)
        out.append(E.Outcome(
            thesis_id=f"t{i}", symbol="AAA",
            opened=start, closed=start + timedelta(days=21),
            entry=entry, exit=exit_,
            benchmark_entry=500.0, benchmark_exit=500.0,
            cost_pct=PREREG.cost_pct,
            thesis_played_out=None if played is None else played[i],
        ))
    return out


# -------------------------------------------------------- excess arithmetic

def test_excess_is_return_minus_benchmark_minus_cost():
    o = E.Outcome("t", "AAA", date(2026, 9, 1), date(2026, 9, 22),
                  entry=100.0, exit=110.0,              # +10%
                  benchmark_entry=100.0, benchmark_exit=104.0,   # +4%
                  cost_pct=0.10)
    assert o.return_pct == pytest.approx(10.0)
    assert o.benchmark_return_pct == pytest.approx(4.0)
    assert o.excess_pct == pytest.approx(5.9)


def test_beating_cash_while_losing_to_the_index_is_negative_edge():
    """A strategy that made 8% while the index made 12% did not make money in
    the sense that matters; the index was available for free."""
    o = E.Outcome("t", "AAA", date(2026, 9, 1), date(2026, 9, 22),
                  entry=100.0, exit=108.0,
                  benchmark_entry=100.0, benchmark_exit=112.0, cost_pct=0.0)
    assert o.return_pct > 0
    assert o.excess_pct == pytest.approx(-4.0)


def test_settle_refuses_a_thesis_with_no_entry_price():
    """A silently defaulted entry manufactures a return."""
    with pytest.raises(ValueError, match="entry"):
        E.settle({"thesis_id": "t", "symbol": "AAA", "opened": "2026-09-01",
                  "benchmark_entry": 500.0},
                 exit_price=110.0, benchmark_exit=505.0, closed=date(2026, 9, 22))


def test_settle_builds_a_scored_outcome_from_a_matured_thesis():
    o = E.settle({"thesis_id": "t1", "symbol": "AAA", "opened": "2026-09-01",
                  "entry": 100.0, "benchmark_entry": 500.0},
                 exit_price=110.0, benchmark_exit=500.0,
                 closed=date(2026, 9, 22), cost_pct=0.1)
    assert o.thesis_id == "t1" and o.held_days == 21
    assert o.excess_pct == pytest.approx(9.9)


# -------------------------------------------------- is the question answerable

def test_required_sample_grows_as_the_claimed_edge_shrinks():
    """The core insight: a small edge in noisy returns needs a lot of trades."""
    big = E.required_sample(2.0, 4.0)
    small = E.required_sample(0.25, 4.0)
    assert small > big * 20


def test_required_sample_matches_the_textbook_formula():
    # (z_.95 + z_.80)^2 * (sd/edge)^2 = (1.6449+0.8416)^2 * 64 ~= 396
    assert E.required_sample(0.5, 4.0, alpha=0.05, power=0.80) == pytest.approx(396, abs=2)


def test_time_to_evidence_reports_a_human_answer():
    """The number that should be computed before committing capital."""
    n = E.required_sample(0.5, 4.0)
    plan = E.time_to_evidence(n, trades_per_week=10.0, horizon_days=21)
    assert plan["weeks"] > 40
    assert "months" in plan["readable"] or "years" in plan["readable"]


def test_a_slower_trade_rate_takes_proportionally_longer():
    fast = E.time_to_evidence(400, trades_per_week=10.0)
    slow = E.time_to_evidence(400, trades_per_week=2.0)
    assert slow["weeks"] > fast["weeks"] * 4


def test_prereg_refuses_a_hypothesis_with_no_number():
    """'Beats the market' is not a hypothesis, it is a mood."""
    with pytest.raises(ValueError, match="target_edge_pct"):
        E.PreRegistration("beats the market", 0.0, 21, date(2027, 1, 1))


# ----------------------------------------------------------------- verdicts

def test_no_closed_trades_says_so_plainly():
    r = E.assess([], PREREG)
    assert r["n"] == 0 and r["decision"] == "collect"


def test_a_small_sample_establishes_nothing_however_good_it_looks():
    """Ten straight winners is what luck looks like at n=10."""
    r = E.assess(_outcomes([3.0] * 10), PREREG, asof=date(2026, 12, 1))
    assert r["verdict"] == "insufficient"
    assert r["decision"] == "collect"
    assert "establish" in r["explanation"]


def test_a_genuine_edge_is_detected_at_sufficient_size():
    rng = np.random.default_rng(1)
    ex = list(rng.normal(1.5, 4.0, 400))          # true edge 1.5% per trade
    r = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    assert r["verdict"] == "edge detected, provisional"
    assert r["decision"] == "continue"
    assert "provisional" in r["explanation"].lower()


def test_pure_noise_is_not_called_an_edge():
    """The failure that costs years: reading zero as promising."""
    rng = np.random.default_rng(2)
    ex = list(rng.normal(0.0, 4.0, 400))
    r = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    assert r["verdict"] != "edge detected, provisional"
    assert r["decision"] in ("collect", "stop")


def test_futility_stops_early_once_the_claimed_edge_is_ruled_out():
    """Ruling an edge out takes less evidence than establishing one, and being
    able to stop early is the whole point."""
    ex = [0.0] * 120                              # dead flat, zero dispersion
    r = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    assert r["verdict"] == "futile"
    assert r["decision"] == "stop"
    assert "ruled out" in r["explanation"]


def test_futility_is_checked_before_the_sample_size_gate():
    """A tight interval around zero settles the question at a size that would
    otherwise be dismissed as too small."""
    r = E.assess(_outcomes([0.01] * 12), PREREG, asof=date(2026, 12, 1))
    assert r["verdict"] == "futile"


def test_a_losing_record_is_not_reported_as_inconclusive():
    r = E.assess(_outcomes([-1.0] * 60), PREREG, asof=date(2026, 12, 1))
    assert r["decision"] == "stop"


def test_the_registered_deadline_is_honoured():
    """The rule was fixed before the data; passing the date without evidence is
    a stop, not an invitation to keep going."""
    rng = np.random.default_rng(3)
    ex = list(rng.normal(0.2, 4.0, 60))
    r = E.assess(_outcomes(ex), PREREG, asof=date(2027, 7, 1))
    assert r["decision"] == "stop"
    assert "deadline" in r["verdict"] or "deadline" in r["explanation"]


def test_more_looks_tighten_the_threshold():
    """Checking weekly and stopping the first time it looks good manufactures
    significance out of noise."""
    rng = np.random.default_rng(4)
    ex = list(rng.normal(0.9, 4.0, 300))
    once = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1), looks_taken=1)
    many = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1), looks_taken=52)
    assert many["critical_t"] > once["critical_t"]
    assert many["effective_alpha"] < once["effective_alpha"]


def test_every_verdict_carries_the_registered_claim_it_was_judged_against():
    r = E.assess(_outcomes([1.0] * 40), PREREG, asof=date(2026, 12, 1))
    assert r["pre_registration"]["target_edge_pct"] == 0.50
    assert r["pre_registration"]["decide_by"] == "2027-06-30"


def test_benchmark_and_hit_rates_are_reported_separately():
    """Hit rate flatters; beating the benchmark is the real bar."""
    r = E.assess(_outcomes([2.0, -1.0, 3.0, -0.5]), PREREG, asof=date(2026, 12, 1))
    assert r["hit_rate"] == pytest.approx(0.5)
    assert "beat_benchmark_rate" in r and "mean_benchmark_pct" in r


# ------------------------------------------------------------------ stats

def test_bootstrap_interval_brackets_a_known_mean():
    rng = np.random.default_rng(5)
    v = rng.normal(2.0, 1.0, 500)
    lo, hi = E.bootstrap_ci(v)
    assert lo < 2.0 < hi and (hi - lo) < 0.5


def test_bootstrap_is_deterministic_for_a_given_seed():
    v = [1.0, -2.0, 3.0, 0.5, 4.0]
    assert E.bootstrap_ci(v) == E.bootstrap_ci(v)


def test_inverse_normal_matches_known_quantiles():
    assert E._z(0.975) == pytest.approx(1.9600, abs=1e-3)
    assert E._z(0.95) == pytest.approx(1.6449, abs=1e-3)
    assert E._z(0.5) == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------- trading_policy

def test_futility_pauses_new_positions():
    """The point of the whole module: evidence has to change behaviour, not
    just get reported. A record nobody acts on is a diary."""
    ex = [0.0] * 120
    verdict = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    policy = E.trading_policy(verdict)
    assert policy["pause_new_positions"] is True
    assert "futile" in policy["reason"].lower() or "review" in policy["reason"].lower()


def test_a_missed_deadline_also_pauses_new_positions():
    rng = np.random.default_rng(3)
    ex = list(rng.normal(0.2, 4.0, 60))
    verdict = E.assess(_outcomes(ex), PREREG, asof=date(2027, 7, 1))
    assert E.trading_policy(verdict)["pause_new_positions"] is True


def test_an_edge_that_is_still_open_does_not_pause_anything():
    r = E.assess(_outcomes([1.0] * 10), PREREG, asof=date(2026, 10, 1))
    assert r["decision"] == "collect"
    assert E.trading_policy(r)["pause_new_positions"] is False


def test_a_detected_edge_does_not_pause_anything():
    rng = np.random.default_rng(1)
    ex = list(rng.normal(1.5, 4.0, 400))
    r = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    assert E.trading_policy(r)["pause_new_positions"] is False


def test_pausing_never_mentions_touching_existing_positions():
    """The pause opens no new exposure; it must not read as a liquidation
    order, which is a materially different and much larger action."""
    ex = [0.0] * 120
    verdict = E.assess(_outcomes(ex), PREREG, asof=date(2026, 12, 1))
    policy = E.trading_policy(verdict)
    assert "unaffected" in policy["reason"] or "untouched" in policy["reason"]
