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


# --------------------------------------------------------------------------
# round 2: insiders and Congress
# --------------------------------------------------------------------------

def _ins(*rows):
    return {"data": [dict(zip(("transaction_date", "executive", "executive_title", "acquisition_or_disposal",
                               "shares", "share_price", "security_type"), r)) for r in rows]}


def test_awards_and_exercise_and_sell_are_not_open_market_buys():
    raw = _ins(("2024-01-10", "A", "CEO", "A", "1000", "0.0", "Common Stock"),          # award
               ("2024-01-10", "B", "CFO", "A", "1000", "20.0", "Common Stock"),         # exercise...
               ("2024-01-10", "B", "CFO", "D", "1000", "25.0", "Common Stock"),         # ...and sell
               ("2024-01-11", "C", "Director", "A", "500", "20.0", "Common Stock"))     # real buy
    assert [b["executive"] for b in H.open_market_buys(raw)] == ["C"]


def test_a_preview_envelope_yields_no_insider_buys():
    assert H.open_market_buys({"preview": True, "sample_data": "x"}) == []


def test_an_insider_cluster_enters_only_after_the_form4_lag():
    px = _px([10.0] * 120)
    raw = _ins(("2024-01-10", "A", "Director", "A", "500", "20.0", "Common Stock"),
               ("2024-01-15", "B", "CFO", "A", "500", "20.0", "Common Stock"))
    t = H.insider_trials("X", raw, px, mode="cluster", horizon_days=63, start=date(2024, 1, 1), end=date(2024, 12, 31))
    assert len(t) == 1
    assert t[0]["entry_session"] == pd.Timestamp("2024-01-19")      # Mon 15th + 4 business days


def test_a_single_insider_is_not_a_cluster():
    px = _px([10.0] * 120)
    raw = _ins(("2024-01-10", "A", "Director", "A", "500", "20.0", "Common Stock"),
               ("2024-01-15", "A", "Director", "A", "500", "20.0", "Common Stock"))
    assert H.insider_trials("X", raw, px, mode="cluster", horizon_days=63,
                            start=date(2024, 1, 1), end=date(2024, 12, 31)) == []


def test_large_officer_buy_needs_both_size_and_an_officer_title():
    px = _px([10.0] * 120)
    big_dir = _ins(("2024-01-10", "A", "Director", "A", "10000", "20.0", "Common Stock"))
    big_ceo = _ins(("2024-01-10", "A", "President & CEO", "A", "10000", "20.0", "Common Stock"))
    kw = dict(mode="large_officer", horizon_days=63, start=date(2024, 1, 1), end=date(2024, 12, 31))
    assert H.insider_trials("X", big_dir, px, **kw) == []
    assert len(H.insider_trials("X", big_ceo, px, **kw)) == 1


def test_congress_enters_the_session_after_the_filing_not_the_trade():
    px = _px([10.0] * 120)
    raw = {"trades": [{"transaction_type": "BUY", "transaction_date": "2024-01-02",
                       "filed_date": "2024-02-06", "amount_min": "15001.00"}]}
    t = H.congress_trials("X", raw, px, horizon_days=63, start=date(2024, 1, 1), end=date(2024, 12, 31))
    assert t[0]["entry_session"] == pd.Timestamp("2024-02-07")


def test_congress_sales_and_small_amounts_are_filtered():
    px = _px([10.0] * 120)
    raw = {"trades": [{"transaction_type": "SELL", "filed_date": "2024-02-06", "amount_min": "50001"},
                      {"transaction_type": "BUY", "filed_date": "2024-02-06", "amount_min": "1001"}]}
    assert H.congress_trials("X", raw, px, horizon_days=63, start=date(2024, 1, 1), end=date(2024, 12, 31),
                             min_amount=15000) == []


# --------------------------------------------------------------------------
# round 3
# --------------------------------------------------------------------------

def test_volatility_ranking_uses_only_prices_before_entry():
    n = 300
    calm = _px([10.0 + (i % 2) * 0.01 for i in range(n)], start="2023-01-02")
    wild = _px([10.0 + (i % 2) * 2.0 for i in range(n)], start="2023-01-02")
    spy = _px([100.0] * n, start="2023-01-02")
    t = H.ranked_monthly_trials({"C": calm, "W": wild}, spy, rank="low_vol", top_n=1,
                                start=date(2024, 2, 1), end=date(2024, 2, 29), horizon_days=21)
    assert t[0]["symbol"] == "C"
    calm2 = calm.copy()
    calm2.loc[calm2.index >= t[0]["entry_session"], "close"] *= [1 + (i % 2) for i in range((calm2.index >= t[0]["entry_session"]).sum())]
    t2 = H.ranked_monthly_trials({"C": calm2, "W": wild}, spy, rank="low_vol", top_n=1,
                                 start=date(2024, 2, 1), end=date(2024, 2, 29), horizon_days=21)
    assert t2[0]["symbol"] == "C"


def test_the_uptrend_filter_skips_months_when_spy_is_below_its_200_day_average():
    n = 300
    falling = _px([200.0 - i * 0.3 for i in range(n)], start="2023-01-02")
    a = _px([10.0] * n, start="2023-01-02")
    assert H.ranked_monthly_trials({"A": a}, falling, rank="momentum", top_n=1, start=date(2024, 2, 1),
                                   end=date(2024, 2, 29), horizon_days=21, require_uptrend=True) == []


def test_momentum_filter_keeps_only_strong_names_at_their_own_entry():
    n = 300
    strong = _px([10.0 + i * 0.05 for i in range(n)], start="2023-01-02")
    weak = _px([30.0 - i * 0.05 for i in range(n)], start="2023-01-02")
    entry = strong.index[280]
    trials = [{"symbol": "S", "entry_session": entry, "exit_session": strong.index[290], "signal": {}},
              {"symbol": "W", "entry_session": entry, "exit_session": weak.index[290], "signal": {}}]
    kept = H.with_momentum_filter(trials, {"S": strong, "W": weak}, min_percentile=0.7)
    assert [t["symbol"] for t in kept] == ["S"]


def test_a_stock_that_stops_trading_mid_hold_exits_at_its_last_trade_not_dropped():
    idx = pd.bdate_range("2024-01-01", periods=40)
    assert H.exit_after(idx[:10], idx[2], 30, data_end=idx[-1]) == idx[9]
    assert H.exit_after(idx[:10], idx[2], 30) is None                     # no market-end given: unknown
    assert H.exit_after(idx, idx[30], 30, data_end=idx[-1]) is None       # still trading, just not matured


def test_reversal_buys_last_weeks_losers_and_skips_penny_stocks():
    n, cut = 300, 280                                   # portfolio formed around session 280
    flat = _px([20.0] * n, start="2023-01-02")
    fresh = _px([20.0] * (cut - 3) + [15.0] * (n - cut + 3), start="2023-01-02")   # fell just before
    penny = _px([3.0] * (cut - 3) + [1.0] * (n - cut + 3), start="2023-01-02")     # fell more, but < $5
    spy = _px([100.0] * n, start="2023-01-02")
    pr = {"A": flat, "F": fresh, "P": penny}
    week = flat.index[cut]
    t = H.ranked_trials(pr, {k: v["close"] for k, v in pr.items()}, spy, rank="reversal5", top_n=1,
                        start=week.date(), end=(week + pd.Timedelta(days=4)).date(), horizon_days=7, freq="W")
    assert t and t[0]["symbol"] == "F"


def test_52_week_high_ranking_prefers_the_name_nearest_its_high():
    n = 300
    near = _px([10.0 + i * 0.01 for i in range(n)], start="2023-01-02")
    far = _px([20.0 - i * 0.02 for i in range(n)], start="2023-01-02")
    spy = _px([100.0] * n, start="2023-01-02")
    pr = {"N": near, "F": far}
    t = H.ranked_trials(pr, {k: v["close"] for k, v in pr.items()}, spy, rank="high52", top_n=1,
                        start=date(2024, 2, 1), end=date(2024, 2, 29), horizon_days=21)
    assert t[0]["symbol"] == "N"
