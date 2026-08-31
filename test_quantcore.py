"""
Correctness tests for quantcore.

The important ones are the recovery tests: we simulate a price path with a KNOWN
volatility and require each estimator to recover it. An estimator that returns a
plausible-looking number for the wrong reason is the failure mode that matters,
and only a known-answer test catches it.
"""

import math
import numpy as np
import pandas as pd
import pytest

import quantcore as q


# ---------------------------------------------------------------- fixtures

def synth_ohlc(n_days=400, annual_vol=0.32, overnight_share=0.35,
               drift=0.08, seed=7, start=100.0, intraday_steps=78):
    """Simulate a price path with known total volatility, split between an
    overnight gap and an intraday path, then build true OHLC bars from it."""
    rng = np.random.default_rng(seed)
    daily = annual_vol / math.sqrt(q.TRADING_DAYS)
    sig_on = daily * math.sqrt(overnight_share)
    sig_id = daily * math.sqrt(1 - overnight_share)
    mu = drift / q.TRADING_DAYS

    rows, close = [], start
    for _ in range(n_days):
        o = close * math.exp(rng.normal(0, sig_on))
        steps = rng.normal(mu / intraday_steps, sig_id / math.sqrt(intraday_steps), intraday_steps)
        path = o * np.exp(np.cumsum(steps))
        h, l, c = float(path.max()), float(path.min()), float(path[-1])
        h, l = max(h, o, c), min(l, o, c)
        rows.append((o, h, l, c, rng.integers(1_000_000, 5_000_000)))
        close = c

    idx = pd.bdate_range("2024-01-02", periods=n_days)
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


@pytest.fixture(scope="module")
def df():
    return synth_ohlc()


# ---------------------------------------------------------------- validation

def test_clean_frame_has_no_problems(df):
    assert q.validate_ohlc(df) == []


def test_catches_high_below_close(df):
    bad = df.copy()
    bad.iloc[50, bad.columns.get_loc("high")] = bad["close"].iloc[50] * 0.5
    probs = q.validate_ohlc(bad)
    assert any("high/low bounds" in p for p in probs)


def test_catches_negative_price(df):
    bad = df.copy()
    bad.iloc[10, bad.columns.get_loc("low")] = -1.0
    assert any("non-positive" in p for p in q.validate_ohlc(bad))


def test_catches_unsorted_and_duplicate_index(df):
    shuffled = df.iloc[::-1]
    assert any("sorted" in p for p in q.validate_ohlc(shuffled))
    dup = pd.concat([df, df.iloc[[-1]]])
    assert any("duplicate" in p for p in q.validate_ohlc(dup))


def test_catches_nan(df):
    bad = df.copy()
    bad.iloc[5, bad.columns.get_loc("close")] = np.nan
    assert any("NaN" in p for p in q.validate_ohlc(bad))


def test_empty_and_missing_columns():
    assert q.validate_ohlc(pd.DataFrame()) != []
    assert any("missing columns" in p for p in q.validate_ohlc(pd.DataFrame({"open": [1.0]})))


# ------------------------------------------------- known-answer recovery

TRUE_VOL = 0.32

@pytest.mark.parametrize("fn,tol", [
    (q.close_to_close_vol, 0.30),   # noisiest estimator, widest tolerance
    (q.yang_zhang_vol,     0.15),
    (q.garman_klass_vol,   0.30),   # ignores overnight, expected to understate
    (q.rogers_satchell_vol,0.30),
    (q.parkinson_vol,      0.35),   # ignores overnight, understates most
])
def test_estimators_recover_known_volatility(df, fn, tol):
    est = fn(df, window=250)
    assert est.usable, est.note
    rel = abs(est.value - TRUE_VOL) / TRUE_VOL
    assert rel < tol, f"{est.method} gave {est.value:.4f} vs true {TRUE_VOL} ({rel:.1%} off)"


def test_yang_zhang_is_the_most_accurate_on_gappy_data(df):
    """The reason yang_zhang is the system default: it alone accounts for the
    overnight gap, which is a third of the variance here."""
    err = {}
    for fn in (q.yang_zhang_vol, q.parkinson_vol, q.garman_klass_vol, q.rogers_satchell_vol):
        e = fn(df, window=250)
        err[e.method] = abs(e.value - TRUE_VOL)
    assert min(err, key=err.get) == "yang_zhang", err


def test_gap_blind_estimators_understate(df):
    """Sanity check on direction: estimators that ignore overnight moves must
    come in BELOW the true total volatility on a series with real gaps."""
    assert q.parkinson_vol(df, 250).value < TRUE_VOL
    assert q.garman_klass_vol(df, 250).value < TRUE_VOL


def test_higher_vol_input_gives_higher_estimate():
    lo = q.yang_zhang_vol(synth_ohlc(annual_vol=0.15, seed=3), 250).value
    hi = q.yang_zhang_vol(synth_ohlc(annual_vol=0.60, seed=3), 250).value
    assert hi > lo * 2.5


def test_thin_data_is_flagged_not_hidden(df):
    e = q.yang_zhang_vol(df.iloc[-12:], window=12)
    assert e.quality == "thin"
    e2 = q.yang_zhang_vol(df.iloc[-4:], window=4)
    assert e2.quality == "failed" and not e2.usable


# ---------------------------------------------------------------- consensus

def test_consensus_reports_spread_and_prefers_yang_zhang(df):
    est, parts = q.consensus_volatility(df, window=250)
    assert est.usable
    assert est.method == "consensus_yang_zhang"
    assert "spread" in est.note and "gap ratio" in est.note.replace("-", " ")
    assert set(parts) == {"yang_zhang", "close_to_close", "parkinson",
                          "garman_klass", "rogers_satchell"}


# ---------------------------------------------------------------- ATR / RSI

def test_atr_is_positive_and_sane(df):
    a = q.average_true_range(df)
    assert a.usable and 0 < a.value < 0.25


def test_rsi_bounded_and_directional(df):
    v = q.rsi(df["close"]).value
    assert 0 <= v <= 100
    rising = pd.Series(np.linspace(100, 200, 60))
    falling = pd.Series(np.linspace(200, 100, 60))
    assert q.rsi(rising).value > 95
    assert q.rsi(falling).value < 5


def test_rsi_insufficient_data_fails_loudly():
    e = q.rsi(pd.Series([1.0, 2.0, 3.0]))
    assert e.quality == "failed" and not e.usable


# ---------------------------------------------------------------- stops

def test_stop_scales_with_volatility():
    quiet = synth_ohlc(annual_vol=0.14, seed=11)
    wild = synth_ohlc(annual_vol=0.75, seed=11)
    sq, _ = q.consensus_volatility(quiet, 250)
    sw, _ = q.consensus_volatility(wild, 250)
    pq = q.stop_plan(100.0, sq, q.average_true_range(quiet))
    pw = q.stop_plan(100.0, sw, q.average_true_range(wild))
    assert pw.stop_fraction > pq.stop_fraction * 1.8
    assert pq.floored, "a very quiet name should hit the 6% floor"
    assert not pw.floored
    # 75% annualised volatility lands near but under the 15% cap; the cap itself
    # is exercised directly in test_stop_respects_floor_and_cap.
    assert 0.10 < pw.stop_fraction <= 0.15


def test_stop_respects_floor_and_cap():
    for vol in (0.05, 0.20, 0.45, 2.0):
        e = q.Estimate(vol, "test", 200)
        p = q.stop_plan(50.0, e, q.Estimate(float("nan"), "atr_fraction", 0, "failed"))
        assert 0.06 - 1e-12 <= p.stop_fraction <= 0.15 + 1e-12
        assert p.stop_price == round(50.0 * (1 - p.stop_fraction), 2)


def test_stop_refuses_unusable_volatility():
    bad = q.Estimate(float("nan"), "x", 0, "failed", "no data")
    with pytest.raises(ValueError):
        q.stop_plan(100.0, bad, q.Estimate(0.02, "atr_fraction", 20))


def test_stop_rejects_bad_entry():
    with pytest.raises(ValueError):
        q.stop_plan(0.0, q.Estimate(0.3, "x", 200), q.Estimate(0.02, "atr_fraction", 20))


# ---------------------------------------------------------------- sizing

def test_size_is_inverse_to_stop_distance():
    tight = q.StopPlan(0.06, 94.0, 100.0, .2, .012, .01, 5, False, False, "ok", "")
    loose = q.StopPlan(0.15, 85.0, 100.0, .5, .031, .02, 5, False, False, "ok", "")
    a = q.size_position(10_000, 100.0, tight)
    b = q.size_position(10_000, 100.0, loose)
    assert a.notional > b.notional

    # Equal risk holds only while the RISK budget is the binding constraint.
    # For a tight stop the weight cap binds first and deliberately overrides
    # risk parity, so the quiet name carries LESS than the full risk budget.
    # That is concentration control winning, by design.
    assert "weight cap" in a.reason and "risk budget" in b.reason
    assert a.risk_dollars < b.risk_dollars

    # With the cap lifted, the two converge on equal risk as intended.
    a2 = q.size_position(10_000, 100.0, tight, max_weight=1.0)
    b2 = q.size_position(10_000, 100.0, loose, max_weight=1.0)
    assert a2.risk_dollars == pytest.approx(b2.risk_dollars, rel=0.05)


def test_weight_cap_binds_for_tight_stops():
    tight = q.StopPlan(0.06, 94.0, 100.0, .2, .012, .01, 5, False, False, "ok", "")
    s = q.size_position(10_000, 100.0, tight, risk_budget_fraction=0.05, max_weight=0.18)
    assert s.weight <= 0.18 + 1e-9
    assert "weight cap" in s.reason


def test_expensive_share_excluded_when_whole_shares_required():
    plan = q.StopPlan(0.10, 900.0, 1000.0, .3, .019, .02, 5, False, False, "ok", "")
    s = q.size_position(1_000, 1000.0, plan, max_weight=0.18)
    assert s.shares == 0 and not s.whole_share_ok
    assert "whole shares" in s.reason


def test_agentic_account_realistic_case():
    """$1,000 account, $60 share, 10% stop: must produce a real, stop-able position."""
    plan = q.StopPlan(0.10, 54.0, 60.0, .35, .022, .02, 4.5, False, False, "ok", "")
    s = q.size_position(1_000, 60.0, plan, risk_budget_fraction=0.02, max_weight=0.18)
    assert s.shares >= 1 and s.whole_share_ok
    assert s.notional <= 180.0 + 1e-9


def test_size_rejects_bad_inputs():
    plan = q.StopPlan(0.10, 90.0, 100.0, .3, .019, .02, 5, False, False, "ok", "")
    with pytest.raises(ValueError):
        q.size_position(0, 100.0, plan)
    with pytest.raises(ValueError):
        q.size_position(1000, -5.0, plan)


# ---------------------------------------------------------------- regime

def test_vol_percentile_bounds_and_direction(df):
    e = q.vol_percentile(df)
    assert e.usable and 0 <= e.value <= 100


def test_vol_percentile_high_after_vol_spike():
    calm = synth_ohlc(n_days=300, annual_vol=0.15, seed=5)
    storm = synth_ohlc(n_days=40, annual_vol=0.90, seed=6, start=float(calm["close"].iloc[-1]))
    storm.index = pd.bdate_range(calm.index[-1] + pd.Timedelta(days=1), periods=40)
    e = q.vol_percentile(pd.concat([calm, storm]))
    assert e.value > 90, f"expected a high percentile after a vol spike, got {e.value}"


def test_trend_state(df):
    t = q.trend_state(df["close"])
    assert t["state"] in ("above_long_trend", "below_long_trend")
    short = q.trend_state(df["close"].iloc[-60:])
    assert "no_long_history" in short["state"]


# ---------------------------------------------------------------- correlation

def test_correlation_detects_one_bet_in_disguise():
    rng = np.random.default_rng(1)
    factor = rng.normal(0, 0.012, 300)
    same = {f"S{i}": pd.Series(factor + rng.normal(0, 0.002, 300)) for i in range(5)}
    r = q.correlation_concentration(same)
    assert r["status"] == "ok" and r["concentrated"]
    assert r["effective_bets"] < 1.6

    indep = {f"I{i}": pd.Series(rng.normal(0, 0.012, 300)) for i in range(5)}
    r2 = q.correlation_concentration(indep)
    assert not r2["concentrated"] and r2["effective_bets"] > 3.0


def test_correlation_insufficient_input():
    assert q.correlation_concentration({"A": pd.Series([0.1, 0.2])})["status"] == "insufficient"


# ---------------------------------------------------------------- anomalies

def test_no_anomalies_on_clean_data(df):
    a = q.detect_anomalies("CLEAN", df, asof=df.index[-1])
    assert not q.blocking(a)


def test_stale_data_blocks(df):
    a = q.detect_anomalies("OLD", df, asof=df.index[-1] + pd.Timedelta(days=20))
    assert q.blocking(a) and any(x.code == "stale_data" for x in a)


@pytest.mark.parametrize("n_frozen", [5, 6, 9])
def test_frozen_series_blocks(df, n_frozen):
    """Five identical closes is four zero returns. Regression test for an
    off-by-one that previously required six identical closes to trip."""
    bad = df.copy()
    last = float(bad["close"].iloc[-1])
    for col in ("open", "high", "low", "close"):
        bad.iloc[-n_frozen:, bad.columns.get_loc(col)] = last
    a = q.detect_anomalies("FROZE", bad, asof=bad.index[-1])
    assert q.blocking(a) and any(x.code == "frozen_series" for x in a)


def test_three_identical_closes_does_not_trip_frozen(df):
    """A genuine quiet patch must not be misread as a dead feed."""
    bad = df.copy()
    last = float(bad["close"].iloc[-1])
    for col in ("open", "high", "low", "close"):
        bad.iloc[-3:, bad.columns.get_loc(col)] = last
    a = q.detect_anomalies("QUIET", bad, asof=bad.index[-1])
    assert not any(x.code == "frozen_series" for x in a)


def test_zero_volume_blocks(df):
    bad = df.copy()
    bad.iloc[-3:, bad.columns.get_loc("volume")] = 0
    a = q.detect_anomalies("HALT", bad, asof=bad.index[-1])
    assert q.blocking(a) and any(x.code == "zero_volume" for x in a)


def _rescale(frame, at, factor):
    """Apply `factor` to every price from bar `at` onward, as a split would."""
    bad = frame.copy()
    for col in ("open", "high", "low", "close"):
        j = bad.columns.get_loc(col)
        bad.iloc[at:, j] = bad.iloc[at:, j].astype(float) * factor
    return bad


def test_price_jump_warns_but_does_not_block(df):
    """A large move that is NOT a clean split ratio is news, not a corporate
    action: worth flagging, not worth refusing to trade on."""
    bad = df.copy()
    for col in ("open", "high", "low", "close"):
        bad.iloc[-1, bad.columns.get_loc(col)] = float(bad[col].iloc[-1]) * 1.55
    a = q.detect_anomalies("NEWS", bad, asof=bad.index[-1])
    assert any(x.code == "price_jump" and x.severity == "warn" for x in a)
    assert not q.blocking(a)


def test_a_clean_split_ratio_blocks_rather_than_warns(df):
    """Unadjusted prices make volatility meaningless, and volatility is what
    stop distance and position size are derived from."""
    a = q.detect_anomalies("RSPLIT", _rescale(df, len(df) - 1, 2.0),
                           asof=df.index[-1])
    assert q.blocking(a)
    hit = next(x for x in a if x.code == "possible_split")
    assert "1-for-2" in hit.message


def test_a_forward_split_is_caught_too(df):
    """CRWD's 4-for-1 divides the price by four; the ratio is 0.25."""
    a = q.detect_anomalies("CRWD", _rescale(df, len(df) - 1, 0.25),
                           asof=df.index[-1])
    hit = next(x for x in a if x.code == "possible_split")
    assert "4-for-1" in hit.message and hit.severity == "block"


def test_a_split_in_the_MIDDLE_of_the_series_is_caught(df):
    """The regression that mattered. The old check tested only the final bar, so
    a split weeks back left today's bar ordinary while corrupting every estimate
    computed over the window. CRWD read 293% volatility and nothing objected."""
    bad = _rescale(df, len(df) // 2, 0.25)
    a = q.detect_anomalies("CRWD", bad, asof=bad.index[-1])
    assert q.blocking(a)
    assert any(x.code == "possible_split" for x in a)


def test_a_split_cannot_hide_inside_the_volatility_it_creates(df):
    """A split is a huge outlier and inflates a standard deviation enough to
    stop being an outlier by that measure. The scale is a MAD for this reason."""
    bad = _rescale(df, len(df) // 3, 0.1)          # 10-for-1, a violent one
    a = q.detect_anomalies("TENFOR1", bad, asof=bad.index[-1])
    assert any(x.code == "possible_split" for x in a)


def test_an_ordinary_series_raises_no_split(df):
    assert not any(x.code == "possible_split"
                   for x in q.detect_anomalies("CALM", df, asof=df.index[-1]))


def test_volume_spike_is_informational(df):
    bad = df.copy()
    bad.iloc[-1, bad.columns.get_loc("volume")] = int(bad["volume"].median() * 20)
    a = q.detect_anomalies("BUZZ", bad, asof=bad.index[-1])
    assert any(x.code == "volume_spike" and x.severity == "info" for x in a)
    assert not q.blocking(a)


def test_corrupt_bar_blocks_before_estimators_run(df):
    bad = df.copy()
    bad.iloc[-2, bad.columns.get_loc("high")] = 0.01
    a = q.detect_anomalies("CORRUPT", bad, asof=bad.index[-1])
    assert q.blocking(a)


# ---------------------------------------------------------------- estimate contract

def test_estimate_rejects_bad_quality_flag():
    with pytest.raises(ValueError):
        q.Estimate(1.0, "x", 10, "probably fine")


def test_nan_value_is_never_usable():
    assert not q.Estimate(float("nan"), "x", 10, "ok").usable


# ------------------------------------------------- concentration, known-answer

def _correlated_book(rho, vols, n=150, seed=11):
    """A book whose TRUE pairwise correlation is exactly `rho`, with the given
    per-asset volatilities. Known-answer input: the estimator has to recover a
    number we already know."""
    rng = np.random.default_rng(seed)
    common = rng.normal(size=n)
    out = {}
    for i, v in enumerate(vols):
        idio = rng.normal(size=n)
        z = np.sqrt(rho) * common + np.sqrt(1 - rho) * idio
        out[f"S{i}"] = pd.Series(z * v)
    return out


def test_concentration_recovers_a_known_correlation():
    book = _correlated_book(0.55, [0.02] * 12)
    out = q.correlation_concentration(book)
    assert abs(out["mean_pairwise_corr"] - 0.55) < 0.12


def test_heterogeneous_volatility_does_not_deflate_the_correlation():
    """The regression that mattered. Shrinking COVARIANCE toward one average
    variance is wrong for every asset when a 1%-vol Treasury fund sits beside a
    105%-vol semiconductor; it dragged a true 0.55 down to 0.28 and called a
    concentrated book diversified. Standardising first fixes it."""
    vols = [0.01] * 4 + list(np.linspace(0.15, 1.05, 16))
    mixed = q.correlation_concentration(_correlated_book(0.55, vols))
    even = q.correlation_concentration(_correlated_book(0.55, [0.02] * 20))

    # the estimate must not depend on how spread out the volatilities are
    assert abs(mixed["mean_pairwise_corr"] - even["mean_pairwise_corr"]) < 0.10
    assert mixed["mean_pairwise_corr"] > 0.40


def test_effective_bets_is_not_overstated_under_mixed_volatility():
    vols = [0.01] * 4 + list(np.linspace(0.15, 1.05, 16))
    out = q.correlation_concentration(_correlated_book(0.55, vols))
    true_bets = 1.0 / ((1 + 19 * 0.55) / 20)          # ~1.75
    assert out["effective_bets"] < true_bets + 0.6


def test_the_verdict_uses_the_less_flattering_of_the_two_views():
    """Shrinkage always says the book is more diversified than the raw sample
    does. A risk measure may err toward caution, never toward 'you have more
    independent bets than you do'."""
    out = q.correlation_concentration(_correlated_book(0.90, [0.02] * 15))
    assert out["concentrated"] is True
    assert out["effective_bets_sample"] <= out["effective_bets"] + 1e-6


def test_independent_series_are_not_called_concentrated():
    rng = np.random.default_rng(3)
    book = {f"S{i}": pd.Series(rng.normal(size=200) * 0.02) for i in range(8)}
    out = q.correlation_concentration(book)
    assert out["concentrated"] is False
    assert abs(out["mean_pairwise_corr"]) < 0.15


def test_a_zero_variance_series_is_dropped_not_crashed_on():
    """A halted or fully frozen series has no correlation with anything, and
    dividing by its zero standard deviation would poison the whole matrix."""
    book = _correlated_book(0.5, [0.02] * 5)
    book["DEAD"] = pd.Series([0.0] * len(book["S0"]))
    out = q.correlation_concentration(book)
    assert out["status"] == "ok"
    assert "DEAD" not in str(out.get("max_pair", ""))
