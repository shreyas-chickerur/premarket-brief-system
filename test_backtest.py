"""Tests for the retrospective backtest (EVIDENCE_ACCELERATION_PLAN.md, Phase 0).

The failure that matters here is not a crash -- it is a backtest that quietly
sees the future and reports an edge that was never there. So most of these
tests are look-ahead guards: hand the function data from AFTER the decision
date and require that it changes nothing.

Earnings and news fixtures are REAL recorded responses (fixtures/research/);
the price series are the committed synthetic fixtures, which is fine for the
mechanics under test here (no test asserts a market outcome from them).
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import backtest as B
import research as R
import runlog
import washsale as W

FIX = Path(__file__).parent / "fixtures"


def _json(name):
    return json.loads((FIX / "research" / name).read_text())


def _prices(sym):
    df = pd.read_csv(FIX / f"{sym}.csv", parse_dates=["timestamp"], comment="#")
    return df.set_index("timestamp").sort_index()


def _news(asof="2026-09-05"):
    """Both real OXY news responses, through the SAME parsers the live run uses."""
    return (R.news_items_from_alpha_vantage(_json("news_sentiment_oxy.json"), symbol="OXY", asof=asof)
            + R.news_items_from_robinhood(_json("robinhood_news_oxy.json"), symbol="OXY", asof=asof))


# --------------------------------------------------------------------------
# condition 1: catalyst windows
# --------------------------------------------------------------------------

def test_catalyst_windows_one_row_per_quarterly_report():
    rows = B.historical_catalyst_windows(_json("earnings_oxy.json"))
    assert len(rows) == 9


def test_catalyst_window_decision_date_is_horizon_days_before_the_report():
    rows = B.historical_catalyst_windows(_json("earnings_oxy.json"), horizon_days=21)
    latest = rows[-1]
    assert latest["report_date"] == date(2026, 8, 5)
    assert latest["decision_date"] == date(2026, 7, 15)
    assert latest["report_time"] == "post-market"


def test_catalyst_windows_are_oldest_first():
    rows = B.historical_catalyst_windows(_json("earnings_oxy.json"))
    dates = [r["report_date"] for r in rows]
    assert dates == sorted(dates)


def test_catalyst_windows_never_carry_the_reported_numbers():
    """Look-ahead item 4: EPS, estimate and surprise are only known AFTER the
    report, so they must not even be available to a decision made before it."""
    for row in B.historical_catalyst_windows(_json("earnings_oxy.json")):
        assert not {"reportedEPS", "estimatedEPS", "surprise", "surprisePercentage"} & set(row)


def test_catalyst_windows_refuse_a_payload_with_no_quarterly_history():
    """A symbol silently contributing zero windows is survivorship bias with
    extra steps -- it has to be loud."""
    with pytest.raises(ValueError):
        B.historical_catalyst_windows({"symbol": "OXY", "annualEarnings": []})


# --------------------------------------------------------------------------
# condition 2: the two_sources proxy
# --------------------------------------------------------------------------

def test_two_sources_clears_when_both_real_feeds_carry_a_relevant_story():
    v = B.two_sources_proxy(_news(), symbol="OXY", asof=date(2026, 9, 5))
    assert v["distinct_sources"] == 2
    assert v["cleared"] is True


def test_two_sources_ignores_every_article_published_after_the_decision_date():
    """Look-ahead item 1. The real articles are dated 3-4 September; a decision
    on 2 September must see none of them."""
    v = B.two_sources_proxy(_news(), symbol="OXY", asof=date(2026, 9, 2))
    assert v["distinct_sources"] == 0
    assert v["cleared"] is False


def test_two_sources_ignores_articles_older_than_the_lookback():
    v = B.two_sources_proxy(_news(), symbol="OXY", asof=date(2026, 12, 1), lookback_days=21)
    assert v["distinct_sources"] == 0


def test_two_sources_drops_a_passing_mention_below_min_relevance():
    """The real COP-centred article names OXY at relevance 0.09; the OXY-centred
    one at 0.54. A threshold above both leaves Alpha Vantage with nothing."""
    v = B.two_sources_proxy(_news(), symbol="OXY", asof=date(2026, 9, 5), min_relevance=0.6)
    assert "Alpha Vantage NEWS_SENTIMENT" not in v["qualifying"]
    assert v["distinct_sources"] == 1


def test_two_sources_does_not_count_one_syndicated_story_twice():
    """Two feeds carrying the same wire headline are one source, not two."""
    headline = "Occidental Petroleum raises dividend after strong quarter"
    items = [
        R.ResearchItem(channel="news", symbol="OXY", mechanism="m",
                       value={"title": headline, "relevance_score": 0.8},
                       source="Alpha Vantage NEWS_SENTIMENT", asof="2026-09-05",
                       detail="20260904T070000"),
        R.ResearchItem(channel="news", symbol="OXY", mechanism="m",
                       value={"title": headline + "!", "publisher": "Reuters"},
                       source="Robinhood get_equity_news", asof="2026-09-05",
                       detail="2026-09-04T08:00:00-04:00"),
    ]
    v = B.two_sources_proxy(items, symbol="OXY", asof=date(2026, 9, 5))
    assert v["distinct_sources"] == 1
    assert v["cleared"] is False


def test_two_sources_never_counts_a_failed_item():
    items = R.news_items_from_alpha_vantage(None, symbol="OXY", asof="2026-09-05")
    v = B.two_sources_proxy(items, symbol="OXY", asof=date(2026, 9, 5))
    assert v["distinct_sources"] == 0


def test_two_sources_records_which_items_qualified_for_audit():
    v = B.two_sources_proxy(_news(), symbol="OXY", asof=date(2026, 9, 5))
    titles = v["qualifying"]["Robinhood get_equity_news"]
    assert any("Seaport Global" in t for t in titles)


# --------------------------------------------------------------------------
# conditions 3-5 against point-in-time prices
# --------------------------------------------------------------------------

def test_point_in_time_gate_uses_only_bars_before_the_decision_session():
    """Look-ahead item 2. The live run decides pre-market, so the decision
    session's own high, low and close -- and everything after -- must not move
    the stop or the size by a cent."""
    df = _prices("F")
    decision = df.index[-40].date()
    base = B.evaluate_price_conditions("F", df, decision_date=decision,
                                       account_equity=100_000, registry=W.Registry())
    future = df.copy()
    future.loc[future.index >= pd.Timestamp(decision), ["high", "low", "close"]] *= 3.0
    future.loc[future.index > pd.Timestamp(decision), "open"] *= 3.0
    moved = B.evaluate_price_conditions("F", future, decision_date=decision,
                                        account_equity=100_000, registry=W.Registry())
    assert base == moved


def test_point_in_time_gate_sizes_off_the_prior_close_and_fills_at_the_open():
    df = _prices("F")
    decision = df.index[-40].date()
    v = B.evaluate_price_conditions("F", df, decision_date=decision,
                                    account_equity=100_000, registry=W.Registry())
    assert v["gate_failed"] is None
    assert v["session"] == decision.isoformat()
    assert v["sized_at"] == pytest.approx(float(df["close"].iloc[-41]))
    assert v["entry"] == pytest.approx(float(df["open"].iloc[-40]))
    assert v["shares"] >= 1 and float(v["shares"]).is_integer()
    assert v["stop_price"] < v["sized_at"]


def test_a_weekend_decision_date_is_taken_at_the_next_session():
    df = _prices("F")
    monday = next(ts for ts in df.index[-60:] if ts.weekday() == 0)
    v = B.evaluate_price_conditions("F", df, decision_date=(monday - pd.Timedelta(days=2)).date(),
                                    account_equity=100_000, registry=W.Registry())
    assert v["session"] == monday.date().isoformat()


def test_point_in_time_gate_fails_risk_sized_when_one_share_is_unaffordable():
    df = _prices("SPY")                       # ~$650 a share
    decision = df.index[-40].date()
    v = B.evaluate_price_conditions("SPY", df, decision_date=decision,
                                    account_equity=500, registry=W.Registry())
    assert v["gate_failed"] == "risk_sized"


def test_point_in_time_gate_blocks_a_wash_sale_buy():
    df = _prices("F")
    decision = df.index[-40].date()
    reg = W.Registry([W.Trade("F", "backtest", decision - timedelta(days=10), "sell", 5,
                              realized_pnl=-12.0)])
    v = B.evaluate_price_conditions("F", df, decision_date=decision,
                                    account_equity=100_000, registry=reg)
    assert v["gate_failed"] == "no_blocking_conflict"


def test_point_in_time_gate_refuses_a_decision_before_any_price_history():
    """A short history grades 'degraded' and the live stop_plan still accepts
    it (sized smaller) -- the backtest mirrors that rather than being stricter
    than the live gate. No history at all is a refusal."""
    df = _prices("F")
    v = B.evaluate_price_conditions("F", df, decision_date=df.index[0].date() - timedelta(days=1),
                                    account_equity=100_000, registry=W.Registry())
    assert v["gate_failed"] == "invalidation_level"


def test_point_in_time_gate_reports_anomalies_by_code():
    df = _prices("F").copy()
    decision = df.index[-40].date()
    df.loc[df.index[-45], "close"] = df["close"].iloc[-46] * 5      # a fake split-sized jump
    v = B.evaluate_price_conditions("F", df, decision_date=decision,
                                    account_equity=100_000, registry=W.Registry())
    assert v["anomalies"] and all(":" in a for a in v["anomalies"])


def test_gate_failures_are_named_from_the_live_gate_vocabulary():
    assert set(B.PRICE_CONDITIONS) <= set(runlog.GATE_CONDITIONS)


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def test_settle_scores_against_spy_at_the_first_session_on_or_after_horizon():
    f, spy = _prices("F"), _prices("SPY")
    opened = f.index[-60].date()
    out = B.settle_window("F", f, spy, opened=opened, entry=float(f.loc[:pd.Timestamp(opened), "close"].iloc[-1]),
                          horizon_days=21)
    exit_ts = f.index[f.index >= pd.Timestamp(opened + timedelta(days=21))][0]
    assert out.closed == exit_ts.date()
    assert out.exit == pytest.approx(float(f.loc[exit_ts, "close"]))
    assert out.benchmark_exit == pytest.approx(float(spy.loc[exit_ts, "close"]))


def test_settle_returns_none_when_the_horizon_has_not_matured_in_the_data():
    f, spy = _prices("F"), _prices("SPY")
    opened = f.index[-3].date()
    assert B.settle_window("F", f, spy, opened=opened, entry=10.0, horizon_days=21) is None


# --------------------------------------------------------------------------
# the universe, fixed before any result exists
# --------------------------------------------------------------------------

LISTING = pd.DataFrame({
    "symbol": ["AAA", "BBB", "CCC", "DDD-W", "EEE", "FFF", "GGG", "HHH"],
    "name": ["Alpha Inc", "Beta Corp", "Gamma Acquisition Corp", "Delta Warrant",
             "Echo Units", "Foxtrot Inc", "Golf ETF", "Hotel Rights"],
    "exchange": ["NYSE", "NASDAQ", "NYSE", "NYSE", "NASDAQ", "BATS", "NYSE ARCA", "NYSE"],
    "assetType": ["Stock", "Stock", "Stock", "Stock", "Stock", "Stock", "ETF", "Stock"],
})


def test_universe_keeps_only_operating_company_stocks_on_the_main_exchanges():
    eligible = B.eligible_listing(LISTING)
    assert eligible == ["AAA", "BBB"]


def test_universe_sample_is_reproducible_from_its_seed():
    a = B.sample_universe(LISTING, n=1, seed=7)
    assert a == B.sample_universe(LISTING, n=1, seed=7)
    assert set(a) <= {"AAA", "BBB"}


def test_universe_sample_never_exceeds_the_eligible_pool():
    assert sorted(B.sample_universe(LISTING, n=50, seed=7)) == ["AAA", "BBB"]


# --------------------------------------------------------------------------
# news by publisher, for history where Robinhood cannot be queried by date
# --------------------------------------------------------------------------

def test_av_publisher_items_name_each_article_by_its_own_publisher():
    raw = {"feed": [
        {"title": "OXY lifts payout", "time_published": "20260904T070807", "source": "Zacks",
         "overall_sentiment_score": 0.1, "ticker_sentiment": [{"ticker": "OXY", "relevance_score": "0.54"}]},
        {"title": "Seaport starts OXY at buy", "time_published": "20260903T090000", "source": "Benzinga",
         "overall_sentiment_score": 0.2, "ticker_sentiment": [{"ticker": "OXY", "relevance_score": "0.70"}]},
    ]}
    items = B.av_publisher_items(raw, symbol="OXY", asof="2026-09-05")
    assert {i.source for i in items} == {"Alpha Vantage NEWS_SENTIMENT:Zacks",
                                         "Alpha Vantage NEWS_SENTIMENT:Benzinga"}
    v = B.two_sources_proxy(items, symbol="OXY", asof=date(2026, 9, 5))
    assert v["distinct_sources"] == 2


def test_av_publisher_items_keep_the_cross_wiring_refusal():
    raw = {"feed": [{"title": "t", "time_published": "20260904T070807", "source": "X",
                     "ticker_sentiment": [{"ticker": "XOM", "relevance_score": "0.9"}]}] * 3}
    items = B.av_publisher_items(raw, symbol="OXY", asof="2026-09-05")
    assert len(items) == 1 and items[0].quality == "failed"


# --------------------------------------------------------------------------
# the portfolio simulation: a $100k book and a real $1,000 replay
# --------------------------------------------------------------------------

def _bars(start, opens, lows=None, closes=None):
    idx = pd.bdate_range(start, periods=len(opens))
    closes = closes or opens
    lows = lows or [min(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": [max(o, c) + 0.01 for o, c in zip(opens, closes)],
                         "low": lows, "close": closes, "volume": 1_000_000}, index=idx)


PLAN = {"stop_fraction": 0.10, "stop_price": 45.0, "entry": 50.0, "annual_vol": 0.3,
        "daily_vol": 0.02, "atr_fraction": 0.03, "multiple_of_daily_vol": 5.0,
        "floored": False, "capped": False, "quality": "ok", "detail": ""}


def _trial(symbol, session, sized_at=50.0, entry=50.0, plan=PLAN):
    return {"symbol": symbol, "session": session, "sized_at": sized_at, "entry": entry,
            "plan": dict(plan, entry=sized_at, stop_price=sized_at * (1 - plan["stop_fraction"]))}


def test_a_position_that_never_hits_its_stop_exits_at_the_horizon_close():
    px = _bars("2025-01-06", [50.0] * 40, closes=[50.0] * 10 + [55.0] * 30)
    spy = _bars("2025-01-06", [100.0] * 40)
    r = B.simulate([_trial("AAA", px.index[0].date())], {"AAA": px}, spy,
                   starting_cash=1000, horizon_days=21)
    t = r["trades"][0]
    assert t["exit_reason"] == "horizon"
    assert t["closed"] == px.index[px.index >= px.index[0] + pd.Timedelta(days=21)][0].date().isoformat()
    assert t["exit"] == 55.0


def test_a_stop_that_is_hit_exits_at_the_stop_and_a_gap_exits_at_the_open():
    lows = [50.0, 50.0, 44.0] + [50.0] * 30
    px = _bars("2025-01-06", [50.0] * 33, lows=lows)
    gap = _bars("2025-01-06", [50.0, 50.0, 40.0] + [40.0] * 30)
    spy = _bars("2025-01-06", [100.0] * 33)
    hit = B.simulate([_trial("AAA", px.index[0].date())], {"AAA": px}, spy, starting_cash=1000)
    gapped = B.simulate([_trial("AAA", gap.index[0].date())], {"AAA": gap}, spy, starting_cash=1000)
    assert hit["trades"][0]["exit_reason"] == "stop" and hit["trades"][0]["exit"] == 45.0
    assert gapped["trades"][0]["exit_reason"] == "stop" and gapped["trades"][0]["exit"] == 40.0


def test_the_1000_dollar_replay_runs_out_of_cash_where_the_100k_book_does_not():
    """The whole reason for two numbers: the book measures the signal, the
    replay measures what this account would actually have lived through."""
    days = pd.bdate_range("2025-01-06", periods=40)
    names = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III")
    px = {s: _bars("2025-01-06", [60.0] * 40) for s in names}
    spy = _bars("2025-01-06", [100.0] * 40)
    trials = [_trial(s, days[0].date(), sized_at=60.0, entry=60.0) for s in px]
    replay = B.simulate(trials, px, spy, starting_cash=1000)
    book = B.simulate(trials, px, spy, starting_cash=100_000, sizing_equity=1000)
    assert len(book["trades"]) == 9
    assert len(replay["trades"]) == 8            # 2 shares x $60 each; $40 left is not a share
    assert any(s["reason"] == "cash" for s in replay["skipped"])


def test_the_book_sizes_every_position_exactly_as_a_fresh_1000_dollar_account():
    px = _bars("2025-01-06", [30.0] * 40)
    spy = _bars("2025-01-06", [100.0] * 40)
    trial = _trial("AAA", px.index[0].date(), sized_at=30.0, entry=30.0)
    replay = B.simulate([trial], {"AAA": px}, spy, starting_cash=1000)
    book = B.simulate([trial], {"AAA": px}, spy, starting_cash=100_000, sizing_equity=1000)
    assert book["trades"][0]["shares"] == replay["trades"][0]["shares"]


def test_sale_proceeds_are_not_spendable_until_the_next_session():
    """Cash account, T+1: money from a stop-out today cannot fund a buy today."""
    days = pd.bdate_range("2025-01-06", periods=40)
    a = _bars("2025-01-06", [60.0] * 40, lows=[60.0, 20.0] + [60.0] * 38)
    others = {s: _bars("2025-01-06", [60.0] * 40) for s in ("BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")}
    spy = _bars("2025-01-06", [100.0] * 40)
    trials = [_trial("AAA", days[0].date(), sized_at=60.0, entry=60.0)]
    trials += [_trial(s, days[0].date(), sized_at=60.0, entry=60.0) for s in others]
    trials += [_trial("ZZZ", days[1].date(), sized_at=60.0, entry=60.0)]
    r = B.simulate(trials, {"AAA": a, "ZZZ": _bars("2025-01-06", [60.0] * 40), **others}, spy,
                   starting_cash=1000)
    assert any(t["symbol"] == "AAA" and t["exit_reason"] == "stop" for t in r["trades"])
    assert any(s["symbol"] == "ZZZ" and s["reason"] == "cash" for s in r["skipped"])


def test_a_loss_exit_blocks_rebuying_the_same_name_inside_30_days():
    days = pd.bdate_range("2025-01-06", periods=60)
    px = _bars("2025-01-06", [30.0] * 60, lows=[30.0, 20.0] + [30.0] * 58)
    spy = _bars("2025-01-06", [100.0] * 60)
    r = B.simulate([_trial("AAA", days[0].date(), 30.0, 30.0), _trial("AAA", days[10].date(), 30.0, 30.0)],
                   {"AAA": px}, spy, starting_cash=1000)
    assert [s["reason"] for s in r["skipped"]] == ["wash_sale"]


def test_final_equity_is_starting_cash_plus_every_trades_pnl():
    px = _bars("2025-01-06", [30.0] * 40, closes=[30.0] * 10 + [33.0] * 30)
    spy = _bars("2025-01-06", [100.0] * 40)
    r = B.simulate([_trial("AAA", px.index[0].date(), 30.0, 30.0)], {"AAA": px}, spy, starting_cash=1000)
    assert r["final_equity"] == pytest.approx(1000 + sum(t["pnl"] for t in r["trades"]))
    assert r["trades"][0]["pnl"] < r["trades"][0]["shares"] * 3.0      # costs are charged


# --------------------------------------------------------------------------
# data ingestion
# --------------------------------------------------------------------------

def test_news_ingest_refuses_an_article_outside_its_window():
    import backtest_data as D
    text = json.dumps({"feed": [{"time_published": "20250704T090000", "title": "t"}]})
    with pytest.raises(ValueError):
        D.check_news_window(text, "NOK", "2025-07-03")


def test_news_ingest_accepts_a_payload_inside_its_window():
    import backtest_data as D
    text = json.dumps({"feed": [{"time_published": "20250629T000000", "title": "t"},
                                {"time_published": "20250612T000000", "title": "u"}]})
    D.check_news_window(text, "NOK", "2025-07-03")


def test_a_preview_envelope_without_a_data_url_is_refused():
    import backtest_data as D
    with pytest.raises(ValueError):
        D.payload_text(json.dumps({"preview": True, "sample_data": "a,b"}))


def test_news_ingest_tolerates_alpha_vantages_inclusive_time_to_boundary():
    import backtest_data as D
    text = json.dumps({"feed": [{"time_published": "20231017T000000", "title": "t"}]})
    D.check_news_window(text, "OXY", "2023-10-17")


def test_news_requests_end_before_the_decision_day():
    import backtest_data as D
    p = D.news_window_params("OXY", date(2023, 10, 17))
    assert p["time_from"] == "20230926T0000" and p["time_to"] == "20231016T2359"


# --------------------------------------------------------------------------
# splits and dividends, point in time
# --------------------------------------------------------------------------

def _raw_with_split():
    """A 2:1 split effective 2025-01-08: raw price halves, split_coefficient 2."""
    idx = pd.to_datetime(["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09"])
    return pd.DataFrame({"open": [100.0, 102.0, 51.0, 52.0], "high": [101.0, 103.0, 52.0, 53.0],
                         "low": [99.0, 101.0, 50.0, 51.0], "close": [100.0, 102.0, 51.0, 52.0],
                         "adjusted_close": [50.0, 51.0, 51.0, 52.0], "volume": [10, 10, 20, 20],
                         "dividend_amount": [0.0, 0.0, 0.0, 0.0],
                         "split_coefficient": [1.0, 1.0, 2.0, 1.0]}, index=idx)


def test_point_in_time_frame_adjusts_only_for_splits_already_known():
    raw = _raw_with_split()
    before = B.point_in_time_frame(raw, asof=pd.Timestamp("2025-01-08"))
    after = B.point_in_time_frame(raw, asof=pd.Timestamp("2025-01-10"))
    assert list(before["close"]) == [100.0, 102.0]            # the split is not yet known
    assert list(after["close"]) == [50.0, 51.0, 51.0, 52.0]   # known: history rescaled
    assert after["close"].iloc[-1] == raw["close"].iloc[-1]   # the latest bar stays a real price


def test_total_return_frame_scales_ohlc_by_the_adjustment_ratio():
    tr = B.total_return_frame(_raw_with_split())
    assert list(tr["close"]) == [50.0, 51.0, 51.0, 52.0]
    assert tr["open"].iloc[0] == 50.0


def test_simulate_carries_a_position_through_a_split_like_a_real_account():
    raw = _raw_with_split()
    idx = pd.bdate_range("2025-01-06", periods=30)
    px = pd.DataFrame({"open": [100.0, 102.0] + [51.0] * 28, "high": [101.0, 103.0] + [52.0] * 28,
                       "low": [99.0, 101.0] + [50.0] * 28, "close": [100.0, 102.0] + [51.0] * 28,
                       "volume": 10, "dividend_amount": 0.0,
                       "split_coefficient": [1.0, 1.0, 2.0] + [1.0] * 27}, index=idx)
    spy = _bars("2025-01-06", [100.0] * 30)
    plan = dict(PLAN, stop_fraction=0.10)
    r = B.simulate([_trial("AAA", idx[0].date(), sized_at=100.0, entry=100.0, plan=plan)],
                   {"AAA": px}, spy, starting_cash=100_000, sizing_equity=100_000)
    t = r["trades"][0]
    assert t["exit_reason"] == "horizon"                     # the halving is not a stop-out
    assert t["pnl"] == pytest.approx(t["shares"] * 51.0 - t["shares"] / 2 * 100.0 - (t["shares"] / 2 * 100.0 + t["shares"] * 51.0) / 2 * 0.001, rel=1e-3)


def test_simulate_credits_dividends_on_a_held_position():
    idx = pd.bdate_range("2025-01-06", periods=30)
    px = pd.DataFrame({"open": 50.0, "high": 50.5, "low": 49.9, "close": 50.0, "volume": 10,
                       "dividend_amount": [0.0] * 5 + [1.0] + [0.0] * 24,
                       "split_coefficient": 1.0}, index=idx)
    spy = _bars("2025-01-06", [100.0] * 30)
    r = B.simulate([_trial("AAA", idx[0].date(), 50.0, 50.0)], {"AAA": px}, spy, starting_cash=1000)
    t = r["trades"][0]
    assert r["final_equity"] == pytest.approx(1000 + t["pnl"] + t["shares"] * 1.0)
