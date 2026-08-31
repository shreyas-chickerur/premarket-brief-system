"""
quantcore — the deterministic computation layer for the Pre-Market Brief System.

Design rules this module obeys:
  1. Every function is pure and testable. No network, no clock, no globals.
  2. Every function that can fail on bad input raises, or returns a value plus a
     quality flag. Nothing silently returns a plausible-looking number.
  3. Every estimate carries the sample size it was computed from.
  4. Nothing here decides to trade. It measures; the decision layer reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence
import math
import numpy as np
import pandas as pd

TRADING_DAYS = 252
OHLC = ("open", "high", "low", "close")


# --------------------------------------------------------------------------
# result containers
# --------------------------------------------------------------------------

@dataclass
class Estimate:
    """A number that knows how it was made and how much to trust it."""
    value: float
    method: str
    n_obs: int
    quality: str = "ok"          # ok | thin | degraded | failed
    note: str = ""

    def __post_init__(self):
        if self.quality not in ("ok", "thin", "degraded", "failed"):
            raise ValueError(f"bad quality flag: {self.quality}")

    @property
    def usable(self) -> bool:
        return self.quality != "failed" and np.isfinite(self.value)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------

def validate_ohlc(df: pd.DataFrame) -> list[str]:
    """Return a list of structural problems. Empty list means the frame is sane.

    This runs before any estimator touches the data. A bar that violates
    high >= max(open, close) is not a data quirk, it is a corrupt row, and an
    estimator fed corrupt rows returns a confident wrong answer.
    """
    problems: list[str] = []

    missing = [c for c in OHLC if c not in df.columns]
    if missing:
        return [f"missing columns: {missing}"]

    if len(df) == 0:
        return ["empty frame"]

    if not isinstance(df.index, pd.DatetimeIndex):
        problems.append("index is not a DatetimeIndex")
    else:
        if not df.index.is_monotonic_increasing:
            problems.append("index is not sorted ascending")
        if df.index.has_duplicates:
            dupes = df.index[df.index.duplicated()].tolist()[:3]
            problems.append(f"duplicate dates: {dupes}")

    sub = df[list(OHLC)]
    if sub.isna().any().any():
        cols = sub.columns[sub.isna().any()].tolist()
        problems.append(f"NaN values in: {cols}")

    nonpos = (sub <= 0).any()
    if nonpos.any():
        problems.append(f"non-positive prices in: {nonpos[nonpos].index.tolist()}")

    hi_ok = df["high"] >= df[["open", "close", "low"]].max(axis=1) - 1e-9
    lo_ok = df["low"] <= df[["open", "close", "high"]].min(axis=1) + 1e-9
    n_bad = int((~hi_ok).sum() + (~lo_ok).sum())
    if n_bad:
        bad_dates = df.index[~(hi_ok & lo_ok)][:3].tolist()
        problems.append(f"{n_bad} bar(s) violate high/low bounds, e.g. {bad_dates}")

    return problems


# --------------------------------------------------------------------------
# volatility estimators
# --------------------------------------------------------------------------

def _log(a, b):
    return np.log(np.asarray(a, dtype=float) / np.asarray(b, dtype=float))


def close_to_close_vol(df: pd.DataFrame, window: int = 60) -> Estimate:
    """Plain annualised standard deviation of daily log returns. The baseline."""
    c = df["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    r = r.iloc[-window:]
    n = len(r)
    if n < 5:
        return Estimate(float("nan"), "close_to_close", n, "failed", "fewer than 5 returns")
    v = float(r.std(ddof=1) * math.sqrt(TRADING_DAYS))
    q = "ok" if n >= 20 else "thin"
    return Estimate(v, "close_to_close", n, q)


def parkinson_vol(df: pd.DataFrame, window: int = 60) -> Estimate:
    """Uses the high-low range. Roughly 5x more efficient than close-to-close,
    but blind to overnight gaps, so it understates for gappy names."""
    d = df.iloc[-window:]
    n = len(d)
    if n < 5:
        return Estimate(float("nan"), "parkinson", n, "failed", "fewer than 5 bars")
    hl = _log(d["high"], d["low"]) ** 2
    v = float(np.sqrt(hl.mean() / (4 * math.log(2)) * TRADING_DAYS))
    return Estimate(v, "parkinson", n, "ok" if n >= 20 else "thin")


def garman_klass_vol(df: pd.DataFrame, window: int = 60) -> Estimate:
    d = df.iloc[-window:]
    n = len(d)
    if n < 5:
        return Estimate(float("nan"), "garman_klass", n, "failed", "fewer than 5 bars")
    hl = _log(d["high"], d["low"]) ** 2
    co = _log(d["close"], d["open"]) ** 2
    var = 0.5 * hl - (2 * math.log(2) - 1) * co
    m = float(np.mean(var))
    if m <= 0:
        return Estimate(float("nan"), "garman_klass", n, "failed", "non-positive variance")
    return Estimate(float(np.sqrt(m * TRADING_DAYS)), "garman_klass", n, "ok" if n >= 20 else "thin")


def rogers_satchell_vol(df: pd.DataFrame, window: int = 60) -> Estimate:
    """Drift-independent, which matters for a trending stock."""
    d = df.iloc[-window:]
    n = len(d)
    if n < 5:
        return Estimate(float("nan"), "rogers_satchell", n, "failed", "fewer than 5 bars")
    hc, ho = _log(d["high"], d["close"]), _log(d["high"], d["open"])
    lc, lo = _log(d["low"], d["close"]), _log(d["low"], d["open"])
    var = float(np.mean(hc * ho + lc * lo))
    if var <= 0:
        return Estimate(float("nan"), "rogers_satchell", n, "failed", "non-positive variance")
    return Estimate(float(np.sqrt(var * TRADING_DAYS)), "rogers_satchell", n, "ok" if n >= 20 else "thin")


def yang_zhang_vol(df: pd.DataFrame, window: int = 60) -> Estimate:
    """Handles overnight gaps AND drift. The default estimator for this system.

    Overnight risk is exactly what an unprotected stop is exposed to, so an
    estimator that ignores gaps is the wrong one for sizing our stops.
    """
    d = df.iloc[-(window + 1):]
    n = len(d)
    if n < 7:
        return Estimate(float("nan"), "yang_zhang", n, "failed", "fewer than 7 bars")

    o, h, l, c = (d[k].astype(float) for k in OHLC)
    prev_c = c.shift(1)

    on = np.log(o / prev_c).dropna()                      # overnight jump
    oc = np.log(c / o).iloc[1:]                           # open to close
    hc, ho = np.log(h / c).iloc[1:], np.log(h / o).iloc[1:]
    lc, lo = np.log(l / c).iloc[1:], np.log(l / o).iloc[1:]
    rs = (hc * ho + lc * lo)

    m = len(on)
    if m < 6:
        return Estimate(float("nan"), "yang_zhang", m, "failed", "fewer than 6 usable bars")

    v_on = float(on.var(ddof=1))
    v_oc = float(oc.var(ddof=1))
    v_rs = float(rs.mean())
    k = 0.34 / (1.34 + (m + 1) / (m - 1))
    var = v_on + k * v_oc + (1 - k) * v_rs
    if not np.isfinite(var) or var <= 0:
        return Estimate(float("nan"), "yang_zhang", m, "failed", "non-positive variance")

    q = "ok" if m >= 20 else "thin"
    return Estimate(float(np.sqrt(var * TRADING_DAYS)), "yang_zhang", m, q)


def average_true_range(df: pd.DataFrame, window: int = 14) -> Estimate:
    """ATR in price units, and as a fraction of the last close."""
    d = df.iloc[-(window + 1):]
    n = len(d)
    if n < 3:
        return Estimate(float("nan"), "atr", n, "failed", "fewer than 3 bars")
    h, l, pc = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float).shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).dropna()
    tr = tr.iloc[-window:]
    if len(tr) < 2:
        return Estimate(float("nan"), "atr", len(tr), "failed", "insufficient true ranges")
    atr_px = float(tr.mean())
    last = float(d["close"].iloc[-1])
    q = "ok" if len(tr) >= window else "thin"
    return Estimate(atr_px / last, "atr_fraction", len(tr), q, note=f"atr_price={atr_px:.4f}")


def garch_vol(returns: Sequence[float], horizon: int = 5) -> Estimate:
    """GARCH(1,1) with Student-t innovations. Optional: if `arch` is absent or the
    fit fails to converge, this returns a failed Estimate rather than a guess."""
    r = pd.Series(returns, dtype=float).dropna()
    n = len(r)
    if n < 100:
        return Estimate(float("nan"), "garch_1_1", n, "failed", "needs >=100 returns")
    try:
        from arch import arch_model
    except ImportError:
        return Estimate(float("nan"), "garch_1_1", n, "failed", "arch not installed")
    try:
        scaled = r * 100.0
        res = arch_model(scaled, vol="GARCH", p=1, q=1, dist="t", mean="Constant").fit(disp="off")
        if not getattr(res, "convergence_flag", 0) == 0:
            return Estimate(float("nan"), "garch_1_1", n, "failed", "did not converge")
        f = res.forecast(horizon=horizon, reindex=False)
        daily = float(np.sqrt(f.variance.values[0].mean())) / 100.0
        v = daily * math.sqrt(TRADING_DAYS)
        if not np.isfinite(v) or v <= 0 or v > 5.0:
            return Estimate(float("nan"), "garch_1_1", n, "failed", f"implausible output {v}")
        return Estimate(v, "garch_1_1", n, "ok", note=f"horizon={horizon}d")
    except Exception as e:                                    # noqa: BLE001
        return Estimate(float("nan"), "garch_1_1", n, "failed", f"{type(e).__name__}: {e}")


def consensus_volatility(df: pd.DataFrame, window: int = 60) -> tuple[Estimate, dict]:
    """Run every estimator, cross-check them, return the one we act on.

    Disagreement between estimators is itself a signal: if the gap-aware
    estimator far exceeds the range-based one, the name is gapping overnight,
    which is precisely the risk a resting stop cannot protect against.
    """
    ests = {
        "yang_zhang": yang_zhang_vol(df, window),
        "close_to_close": close_to_close_vol(df, window),
        "parkinson": parkinson_vol(df, window),
        "garman_klass": garman_klass_vol(df, window),
        "rogers_satchell": rogers_satchell_vol(df, window),
    }
    usable = {k: e for k, e in ests.items() if e.usable}
    if not usable:
        return Estimate(float("nan"), "consensus", 0, "failed", "no estimator produced a value"), ests

    primary = ests["yang_zhang"]
    vals = np.array([e.value for e in usable.values()])
    spread = float(vals.max() / vals.min()) if vals.min() > 0 else float("inf")

    if not primary.usable:
        med = float(np.median(vals))
        return Estimate(med, "consensus_median", int(np.median([e.n_obs for e in usable.values()])),
                        "degraded", "yang_zhang unavailable, using median of survivors"), ests

    quality = primary.quality
    note = f"estimator spread {spread:.2f}x across {len(usable)} methods"
    if spread > 2.5:
        quality = "degraded"
        note += " — WIDE, estimators disagree materially"

    gap_ratio = (ests["yang_zhang"].value / ests["parkinson"].value
                 if ests["parkinson"].usable and ests["parkinson"].value > 0 else float("nan"))
    if np.isfinite(gap_ratio):
        note += f"; overnight-gap ratio {gap_ratio:.2f}"

    return Estimate(primary.value, "consensus_yang_zhang", primary.n_obs, quality, note), ests


# --------------------------------------------------------------------------
# stop and size
# --------------------------------------------------------------------------

@dataclass
class StopPlan:
    stop_fraction: float
    stop_price: float
    entry: float
    annual_vol: float
    daily_vol: float
    atr_fraction: float
    multiple_of_daily_vol: float
    floored: bool
    capped: bool
    quality: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def stop_plan(entry: float, vol: Estimate, atr: Estimate, *,
              k_daily_sigma: float = 2.5,
              floor: float = 0.06, cap: float = 0.15) -> StopPlan:
    """Distance to stop = k daily standard deviations, cross-checked against ATR.

    A flat percentage stop is wrong for every stock at once. This sets the stop
    where ordinary daily noise will not reach it, but a genuine break will.
    """
    if entry <= 0:
        raise ValueError("entry must be positive")
    if not vol.usable:
        raise ValueError(f"unusable volatility estimate: {vol.note}")

    daily = vol.value / math.sqrt(TRADING_DAYS)
    raw = k_daily_sigma * daily

    atr_frac = atr.value if atr.usable else float("nan")
    detail = f"{k_daily_sigma}x daily sigma of {daily:.4f}"
    if np.isfinite(atr_frac):
        blended = 0.65 * raw + 0.35 * (2.0 * atr_frac)
        detail += f"; blended with 2x ATR of {atr_frac:.4f}"
        raw = blended

    floored = raw < floor
    capped = raw > cap
    frac = min(max(raw, floor), cap)

    quality = vol.quality
    if floored or capped:
        quality = "degraded" if quality == "ok" else quality
        detail += f"; {'floored' if floored else 'capped'} from {raw:.4f}"

    return StopPlan(
        stop_fraction=frac,
        stop_price=round(entry * (1 - frac), 2),
        entry=entry,
        annual_vol=vol.value,
        daily_vol=daily,
        atr_fraction=atr_frac,
        multiple_of_daily_vol=frac / daily if daily > 0 else float("nan"),
        floored=floored, capped=capped, quality=quality, detail=detail,
    )


@dataclass
class SizePlan:
    shares: int
    notional: float
    weight: float
    risk_dollars: float
    whole_share_ok: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def size_position(account_equity: float, entry: float, plan: StopPlan, *,
                  risk_budget_fraction: float = 0.02,
                  max_weight: float = 0.18,
                  require_whole_shares: bool = True) -> SizePlan:
    """Equal RISK per position, not equal dollars, then clipped by the weight cap.

    risk_budget_fraction is the fraction of the account lost if the stop fills
    at its trigger price. Slippage past the stop makes the realised loss larger,
    which is why the budget is deliberately small.
    """
    if account_equity <= 0 or entry <= 0:
        raise ValueError("account_equity and entry must be positive")

    risk_dollars = account_equity * risk_budget_fraction
    notional_by_risk = risk_dollars / plan.stop_fraction
    notional_cap = account_equity * max_weight
    notional = min(notional_by_risk, notional_cap)
    binding = "risk budget" if notional_by_risk <= notional_cap else "weight cap"

    shares = int(notional // entry)
    if require_whole_shares and shares < 1:
        return SizePlan(0, 0.0, 0.0, 0.0, False,
                        f"one share costs {entry:.2f} but cap allows {notional:.2f} "
                        f"— excluded because a resting stop needs whole shares")

    filled = shares * entry
    return SizePlan(
        shares=shares,
        notional=round(filled, 2),
        weight=filled / account_equity,
        risk_dollars=round(filled * plan.stop_fraction, 2),
        whole_share_ok=True,
        reason=f"{binding} binding; {shares} share(s) at {entry:.2f}",
    )


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def rsi(close: pd.Series, window: int = 14) -> Estimate:
    c = pd.Series(close, dtype=float).dropna()
    if len(c) < window + 1:
        return Estimate(float("nan"), "rsi", len(c), "failed", f"needs {window+1} closes")
    d = c.diff().dropna()
    gain = d.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return Estimate(100.0, "rsi", len(c), "ok", "no losses in window")
    rs = float(gain.iloc[-1]) / last_loss
    return Estimate(100 - 100 / (1 + rs), "rsi", len(c), "ok")


def trend_state(close: pd.Series, fast: int = 50, slow: int = 200) -> dict:
    c = pd.Series(close, dtype=float).dropna()
    out = {"fast_ma": None, "slow_ma": None, "state": "unknown", "n_obs": len(c)}
    if len(c) >= fast:
        out["fast_ma"] = float(c.rolling(fast).mean().iloc[-1])
    if len(c) >= slow:
        out["slow_ma"] = float(c.rolling(slow).mean().iloc[-1])
    last = float(c.iloc[-1])
    if out["slow_ma"] is not None:
        out["state"] = "above_long_trend" if last > out["slow_ma"] else "below_long_trend"
    elif out["fast_ma"] is not None:
        out["state"] = "above_short_trend" if last > out["fast_ma"] else "below_short_trend"
        out["state"] += "_no_long_history"
    return out


def vol_percentile(df: pd.DataFrame, lookback: int = 252, window: int = 20) -> Estimate:
    """Where current realised volatility sits in its own recent history.

    This is the regime signal. It replaces a hidden Markov model deliberately:
    it is stable across reruns, has no label-switching problem, and is
    explainable in one sentence at six in the morning.
    """
    c = df["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    if len(r) < window * 3:
        return Estimate(float("nan"), "vol_percentile", len(r), "failed",
                        f"needs {window*3} returns, have {len(r)}")
    rolling = r.rolling(window).std().dropna().iloc[-lookback:]
    cur = float(rolling.iloc[-1])
    pct = float((rolling < cur).mean() * 100)
    q = "ok" if len(rolling) >= 120 else "thin"
    return Estimate(pct, "vol_percentile", len(rolling), q,
                    note=f"{window}d realised vol vs trailing {len(rolling)} obs")


def correlation_concentration(returns_by_symbol: dict[str, pd.Series]) -> dict:
    """Are these N positions actually one bet?

    Ledoit-Wolf shrinkage is far better behaved than a raw sample correlation on
    short histories, but it shrinks COVARIANCE toward a single scaled identity
    whose target variance is the average of the diagonal. When the holdings have
    wildly different volatilities -- a Treasury fund near 1% sitting beside a
    semiconductor near 100% -- that target is wrong for every asset at once, and
    converting the distorted covariance to a correlation drags the off-diagonals
    toward zero.

    Measured on a known-answer portfolio of 20 names at a true pairwise
    correlation of 0.55 with volatilities spanning 1% to 105%: the covariance
    route reported 0.28 and 2.65 effective bets against a true 1.75, and called
    the book UNCONCENTRATED. Understating correlation is the one direction a
    risk measure must never fail in, because it silently licenses more exposure.

    Standardising each series to unit variance first puts the estimator in
    correlation space, where the identity target is the right one. Same shrinkage
    benefit, without the heterogeneity distortion.
    """
    syms = [s for s, r in returns_by_symbol.items() if r is not None and len(r.dropna()) > 10]
    if len(syms) < 2:
        return {"status": "insufficient", "n_symbols": len(syms)}
    frame = pd.DataFrame({s: returns_by_symbol[s] for s in syms}).dropna()
    if len(frame) < 20:
        return {"status": "insufficient", "n_symbols": len(syms), "n_obs": len(frame)}

    from sklearn.covariance import LedoitWolf

    # Standardise to unit variance BEFORE shrinking, so the shrinkage target is
    # the identity in correlation space rather than an average-variance sphere
    # that fits none of the assets.
    sd = frame.std(ddof=1).replace(0.0, np.nan)
    if sd.isna().any():
        dead = sd[sd.isna()].index.tolist()
        frame = frame.drop(columns=dead)
        syms = [s for s in syms if s not in dead]
        sd = frame.std(ddof=1)
        if len(syms) < 2:
            return {"status": "insufficient", "n_symbols": len(syms),
                    "note": f"zero-variance series dropped: {dead}"}

    z = (frame - frame.mean()) / sd

    lw = LedoitWolf().fit(z.values)
    cov = lw.covariance_
    d = np.sqrt(np.diag(cov))
    corr = cov / np.outer(d, d)
    iu = np.triu_indices_from(corr, k=1)
    off = corr[iu]

    eig = np.sort(np.linalg.eigvalsh(corr))[::-1]
    top_share = float(eig[0] / eig.sum())

    # Shrinkage pulls toward independence, so the shrunk estimate is the
    # OPTIMISTIC one: it always says the book is more diversified than the raw
    # sample does. Compute the unshrunk sample correlation too and let the
    # conservative reading drive the risk verdict. A concentration measure is
    # allowed to be wrong toward caution; it is not allowed to be wrong toward
    # "you have more independent bets than you do".
    sample = np.corrcoef(z.values, rowvar=False)
    s_eig = np.sort(np.linalg.eigvalsh(sample))[::-1]
    s_top = float(s_eig[0] / s_eig.sum())
    s_off = sample[iu]

    worst = int(np.argmax(off))
    pairs = [(syms[i], syms[j]) for i, j in zip(*iu)]
    return {
        "status": "ok",
        "n_symbols": len(syms),
        "n_obs": len(frame),
        "mean_pairwise_corr": float(off.mean()),
        "max_pairwise_corr": float(off.max()),
        "max_pair": pairs[worst],
        "first_factor_share": top_share,
        "effective_bets": float(1.0 / top_share),
        "shrinkage": float(lw.shrinkage_),
        # unshrunk view, for the reader who wants to see the raw evidence
        "mean_pairwise_corr_sample": float(s_off.mean()),
        "effective_bets_sample": float(1.0 / s_top),
        # the verdict uses whichever view is less flattering
        "concentrated": bool(max(top_share, s_top) > 0.60),
    }


# --------------------------------------------------------------------------
# anomaly detection on incoming data
# --------------------------------------------------------------------------

@dataclass
class Anomaly:
    code: str
    severity: str          # info | warn | block
    symbol: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


# Ratios a corporate action would produce. A genuine one-day move of +100% or
# -50% in a liquid name is vanishingly rare; a 2-for-1 split produces exactly
# that and produces it to the tick, which is what the tolerance keys on.
_SPLIT_FACTORS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20)


def _looks_like_a_split(ratio: float, tol: float = 0.04) -> Optional[str]:
    """Name the split a price ratio implies, or None if it looks like a move."""
    for f in _SPLIT_FACTORS:
        if abs(ratio - 1.0 / f) <= tol / f:       # forward split: price divides
            return f"{f}-for-1"
        if abs(ratio - float(f)) <= tol * f:      # reverse split: price multiplies
            return f"1-for-{f}"
    return None


def detect_anomalies(symbol: str, df: pd.DataFrame, *,
                     asof: Optional[pd.Timestamp] = None,
                     max_staleness_days: int = 5,
                     jump_sigma: float = 8.0) -> list[Anomaly]:
    """Data-integrity checks that run on every series before it is used.

    Severity 'block' means the symbol is excluded from trading decisions this
    run. The system never trades on data it has flagged as suspect.
    """
    out: list[Anomaly] = []

    for p in validate_ohlc(df):
        out.append(Anomaly("ohlc_structure", "block", symbol, p))
    if any(a.severity == "block" for a in out):
        return out

    last_date = df.index[-1]
    if asof is not None:
        stale = (asof - last_date).days
        if stale > max_staleness_days:
            out.append(Anomaly("stale_data", "block", symbol,
                               f"last bar {last_date.date()} is {stale} days before {asof.date()}"))

    c = df["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    if len(r) >= 30:
        # Scale from the median absolute deviation, not the standard deviation.
        # A split is a huge outlier, and it inflates its own sigma enough to hide
        # from a test built on that sigma. MAD barely moves.
        med = float(r.median())
        mad = float((r - med).abs().median())
        sd = 1.4826 * mad if mad > 0 else float(r.std(ddof=1))

        if sd > 0:
            # Scan the WHOLE series, not just the last bar. A split three weeks
            # back leaves today's bar perfectly ordinary while corrupting every
            # volatility estimate computed over the window -- and volatility is
            # what stop distance and position size are derived from, so a silent
            # one poisons the sizing of every trade in that name.
            zs = (r - med).abs() / sd
            worst = int(zs.values.argmax())
            zmax = float(zs.iloc[worst])

            if zmax > jump_sigma:
                when = r.index[worst]
                when = when.date() if hasattr(when, "date") else when
                ratio = float(np.exp(r.iloc[worst]))
                split = _looks_like_a_split(ratio)

                if split:
                    out.append(Anomaly(
                        "possible_split", "block", symbol,
                        f"{when}: price moved {ratio:.4g}x, which is within a "
                        f"whisker of a {split} split. Unadjusted prices make "
                        f"volatility meaningless here — refetch this series "
                        f"split-adjusted before using it for anything"))
                else:
                    where = "last bar" if worst == len(r) - 1 else f"{when}"
                    out.append(Anomaly(
                        "price_jump", "warn", symbol,
                        f"{where}: {zmax:.1f} sigma move — verify for a split, "
                        f"a bad print, or genuine news before acting"))
        # Count trailing zero returns directly. N identical closes produce N-1
        # zero returns, so testing .tail(5) here would silently require SIX
        # identical closes — an off-by-one that would let a frozen feed through.
        zeros = (r.abs() < 1e-12).values
        trailing_zeros = 0
        for flag in zeros[::-1]:
            if not flag:
                break
            trailing_zeros += 1
        if trailing_zeros >= 4:
            out.append(Anomaly("frozen_series", "block", symbol,
                               f"{trailing_zeros + 1} consecutive identical closes "
                               f"— feed likely frozen"))

    if "volume" in df.columns:
        v = pd.to_numeric(df["volume"], errors="coerce")
        if v.tail(3).fillna(0).eq(0).all():
            out.append(Anomaly("zero_volume", "block", symbol,
                               "zero volume on the last 3 bars — likely halted or delisted"))
        elif len(v.dropna()) >= 30:
            med = float(v.iloc[-30:-1].median())
            last = float(v.iloc[-1])
            if med > 0 and last > 6 * med:
                out.append(Anomaly("volume_spike", "info", symbol,
                                   f"volume {last/med:.1f}x its 30-day median"))

    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 10:
        gaps = df.index.to_series().diff().dt.days.dropna()
        big = gaps[gaps > 7]
        if len(big) > 0:
            out.append(Anomaly("calendar_gap", "info", symbol,
                               f"{len(big)} gap(s) over 7 days in the series, "
                               f"largest {int(big.max())} days"))
    return out


def blocking(anoms: Sequence[Anomaly]) -> bool:
    return any(a.severity == "block" for a in anoms)
