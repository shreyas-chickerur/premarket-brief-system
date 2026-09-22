"""Look-ahead and arithmetic tests for hypotheses.py. Every signal is fed data
that only becomes true AFTER its entry and must not change."""

from datetime import date

import pandas as pd
import pytest

import evidence
import hypotheses as H


def _px(closes, start="2024-01-01", opens=None):
    idx = pd.bdate_range(start, periods=len(closes))
    opens = opens or closes
    return pd.DataFrame({"open": opens, "high": [max(o, c) for o, c in zip(opens, closes)],
                         "low": [min(o, c) for o, c in zip(opens, closes)], "close": closes,
                         "volume": 1_000_000}, index=idx)


IDX = pd.bdate_range("2024-01-01", periods=30)     # Mon 1 Jan 2024 ...


def test_a_pre_market_report_reacts_the_same_day():
    assert H.reaction_session(IDX, date(2024, 1, 3), "pre-market") == pd.Timestamp("2024-01-03")


def test_a_post_market_or_unknown_report_reacts_the_next_session():
    assert H.reaction_session(IDX, date(2024, 1, 3), "post-market") == pd.Timestamp("2024-01-04")
    assert H.reaction_session(IDX, date(2024, 1, 3), None) == pd.Timestamp("2024-01-04")


def _earn(rd, rt="post-market", surprise="12.5"):
    return {"quarterlyEarnings": [{"reportedDate": rd, "reportTime": rt, "surprisePercentage": surprise}]}


def test_pead_enters_the_session_after_the_reaction_never_before():
    px = _px([10.0] * 60)
    spy = _px([100.0] * 60)
    t = H.pead_trials("X", _earn("2024-01-03"), px, spy, horizon_days=21,
                      start=date(2024, 1, 1), end=date(2024, 12, 31), min_surprise_pct=5)
    assert t[0]["entry_session"] == pd.Timestamp("2024-01-05")      # reaction Thu 4th, entry Fri 5th


def test_pead_respects_the_surprise_threshold_and_skips_missing_surprise():
    px, spy = _px([10.0] * 60), _px([100.0] * 60)
    kw = dict(horizon_days=21, start=date(2024, 1, 1), end=date(2024, 12, 31), min_surprise_pct=5)
    assert H.pead_trials("X", _earn("2024-01-03", surprise="4.9"), px, spy, **kw) == []
    assert H.pead_trials("X", _earn("2024-01-03", surprise="None"), px, spy, **kw) == []


def test_pead_reaction_filter_uses_only_the_reaction_session():
    closes = [10.0] * 3 + [11.0] + [11.0] * 56          # +10% on the reaction session (4 Jan)
    px, spy = _px(closes), _px([100.0] * 60)
    kw = dict(horizon_days=21, start=date(2024, 1, 1), end=date(2024, 12, 31), min_reaction_excess_pct=5)
    assert len(H.pead_trials("X", _earn("2024-01-03"), px, spy, **kw)) == 1
    later = _px([10.0] * 5 + [20.0] * 55)               # a jump only AFTER entry must not qualify it
    assert H.pead_trials("X", _earn("2024-01-03"), later, spy, **kw) == []


def test_runup_exits_before_the_reaction_session():
    px = _px([10.0] * 60)
    t = H.runup_trials("X", _earn("2024-02-01"), px, lead_sessions=10,
                       start=date(2024, 1, 1), end=date(2024, 12, 31))[0]
    reaction = H.reaction_session(px.index, date(2024, 2, 1), "post-market")
    assert t["exit_session"] < reaction
    assert px.index.get_loc(reaction) - px.index.get_loc(t["entry_session"]) == 10


def test_momentum_ranks_only_on_prices_before_the_entry_session():
    n = 300
    a = _px([10.0 + i * 0.01 for i in range(n)], start="2023-01-02")   # steady riser
    b = _px([10.0] * n, start="2023-01-02")
    spy = _px([100.0] * n, start="2023-01-02")
    first = H.momentum_trials({"A": a, "B": b}, spy, top_n=1, start=date(2024, 2, 1), end=date(2024, 2, 29))
    assert first[0]["symbol"] == "A"
    b2 = b.copy()
    b2.loc[b2.index >= first[0]["entry_session"], ["open", "high", "low", "close"]] *= 10   # future only
    again = H.momentum_trials({"A": a, "B": b2}, spy, top_n=1, start=date(2024, 2, 1), end=date(2024, 2, 29))
    assert again[0]["symbol"] == "A"


def test_monthly_t_clusters_trades_by_entry_month():
    def o(d, ex):
        return evidence.Outcome("t", "X", d, d, 100.0, 100.0 + ex, 100.0, 100.0, cost_pct=0.0)
    outs = [o(date(2024, 1, 5), 2.0), o(date(2024, 1, 9), 4.0), o(date(2024, 2, 5), 1.0), o(date(2024, 3, 5), 5.0)]
    r = H.monthly_t(outs)
    assert r["months"] == 3
    assert r["mean_monthly_excess_pct"] == pytest.approx((3.0 + 1.0 + 5.0) / 3)
