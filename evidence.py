"""Does this system have an edge, and could it ever prove it?

The failure mode this module exists to prevent is not losing money. It is
spending a year on a platform that was never capable of producing evidence
either way, and mistaking the absence of a verdict for a promising start.

Three commitments, in the order they matter:

**Ask whether the question is answerable before answering it.** With a two
position per day cap, a plausible per-trade edge, and the noise of daily equity
returns, `required_sample` and `time_to_evidence` say up front how many closed
trades and how many months it would take to distinguish that edge from luck. If
the answer is longer than anyone is willing to wait, that is the single most
useful thing this system can report, and it can report it on day one.

**Pre-register the claim and the stopping rule.** A `PreRegistration` fixes the
hypothesis, the edge being claimed, the significance level, and the date by
which a verdict is due -- before the data arrives. Deciding what counts as
success after seeing results is how a flat equity curve becomes "still early".

**Allow the answer to be no, and say so early.** `assess` reports futility: once
the confidence interval's upper bound falls below the edge being claimed, that
edge has been ruled out and continuing cannot rediscover it. A framework that
can only ever return "inconclusive, keep going" is the thing that wastes years.

Everything is measured as EXCESS return over a benchmark, after costs. A
strategy that made 8% while the index made 12% did not make money in the sense
that matters, and holding the index was free.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

import numpy as np

__all__ = [
    "PreRegistration", "Outcome", "settle", "assess", "trading_policy",
    "required_sample", "time_to_evidence", "bootstrap_ci",
]

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# the claim, fixed in advance
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PreRegistration:
    """What is being claimed, and what would settle it. Written before the data.

    `target_edge_pct` is the per-trade excess return over the benchmark, after
    costs, that the process claims to produce. It has to be stated as a number:
    "beats the market" is not a hypothesis, it is a mood.
    """
    hypothesis: str
    target_edge_pct: float
    horizon_days: int
    decide_by: date
    alpha: float = 0.05
    power: float = 0.80
    min_sample: int = 30
    assumed_sd_pct: float = 4.0        # per-trade dispersion of excess returns
    cost_pct: float = 0.10             # round-trip spread and slippage

    def __post_init__(self):
        if self.target_edge_pct <= 0:
            raise ValueError("target_edge_pct must be positive — state the edge being claimed")
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be between 0 and 1")
        if self.assumed_sd_pct <= 0:
            raise ValueError("assumed_sd_pct must be positive")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decide_by"] = self.decide_by.isoformat()
        return d


# --------------------------------------------------------------------------
# a settled prediction
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """One prediction, scored against the benchmark over the same window."""
    thesis_id: str
    symbol: str
    opened: date
    closed: date
    entry: float
    exit: float
    benchmark_entry: float
    benchmark_exit: float
    cost_pct: float = 0.10
    thesis_played_out: Optional[bool] = None

    @property
    def return_pct(self) -> float:
        return (self.exit / self.entry - 1.0) * 100.0

    @property
    def benchmark_return_pct(self) -> float:
        return (self.benchmark_exit / self.benchmark_entry - 1.0) * 100.0

    @property
    def excess_pct(self) -> float:
        """The only number that means anything. Beating cash is not an edge when
        the index was available for free."""
        return self.return_pct - self.benchmark_return_pct - self.cost_pct

    @property
    def held_days(self) -> int:
        return (self.closed - self.opened).days

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opened"] = self.opened.isoformat()
        d["closed"] = self.closed.isoformat()
        d.update(return_pct=self.return_pct,
                 benchmark_return_pct=self.benchmark_return_pct,
                 excess_pct=self.excess_pct)
        return d


def settle(thesis: dict, *, exit_price: float, benchmark_exit: float,
           closed: date, cost_pct: float = 0.10,
           thesis_played_out: Optional[bool] = None) -> Outcome:
    """Turn a matured thesis into a scored outcome.

    Raises rather than guessing if the thesis is missing the prices it was
    opened at. A silently defaulted entry price manufactures a return.
    """
    for key in ("thesis_id", "symbol", "opened", "entry", "benchmark_entry"):
        if thesis.get(key) in (None, ""):
            raise ValueError(f"thesis is missing {key!r}; cannot be scored honestly")

    opened = thesis["opened"]
    if isinstance(opened, str):
        opened = date.fromisoformat(opened[:10])

    return Outcome(
        thesis_id=str(thesis["thesis_id"]),
        symbol=str(thesis["symbol"]),
        opened=opened,
        closed=closed,
        entry=float(thesis["entry"]),
        exit=float(exit_price),
        benchmark_entry=float(thesis["benchmark_entry"]),
        benchmark_exit=float(benchmark_exit),
        cost_pct=cost_pct,
        thesis_played_out=thesis_played_out,
    )


# --------------------------------------------------------------------------
# can this question be answered at all?
# --------------------------------------------------------------------------

def required_sample(target_edge_pct: float, assumed_sd_pct: float,
                    alpha: float = 0.05, power: float = 0.80) -> int:
    """Closed trades needed to detect `target_edge_pct` if it is really there.

    A one-sample, one-sided test. Normal approximation, which is close enough
    when the answer is "about two hundred" and the point is the order of
    magnitude, not the third digit.

    This is the number that should be computed BEFORE committing capital. If it
    exceeds what the trade rate can deliver in a tolerable time, the design
    cannot be validated, and no amount of patience changes that.
    """
    if target_edge_pct <= 0 or assumed_sd_pct <= 0:
        raise ValueError("target edge and dispersion must both be positive")

    # inverse normal CDF without scipy, via the standard rational approximation
    z_alpha = _z(1.0 - alpha)
    z_power = _z(power)
    n = ((z_alpha + z_power) * assumed_sd_pct / target_edge_pct) ** 2
    return int(math.ceil(n))


def _z(p: float) -> float:
    """Inverse standard normal CDF (Acklam's approximation, ~1e-9 accurate)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > hi:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def time_to_evidence(required_n: int, trades_per_week: float,
                     horizon_days: int = 0) -> dict:
    """How long until a verdict is even possible, in plain units.

    Includes the horizon: a trade opened in the final week cannot be scored, so
    the wait is the time to open `required_n` positions plus the time for the
    last one to mature.
    """
    if trades_per_week <= 0:
        raise ValueError("trades_per_week must be positive")
    weeks = required_n / trades_per_week + horizon_days / 7.0
    return {
        "required_n": required_n,
        "trades_per_week": trades_per_week,
        "weeks": weeks,
        "months": weeks / 4.345,
        "years": weeks / 52.0,
        "readable": _readable(weeks),
    }


def trading_policy(verdict: dict) -> dict:
    """Turn a verdict into an action, so the evidence review is not a diary
    entry nobody acts on.

    This is the difference between "producing evidence" and "producing a
    report about evidence". A verdict of futile or no-edge-by-deadline
    disables new positions on the very next run -- reported loudly, always
    reversible by a person, never silent. Existing positions and their stops
    are untouched: this pauses opening new exposure, it does not liquidate.
    """
    decision = verdict.get("decision")
    n = verdict.get("n", 0)

    if decision in ("stop",):
        return {
            "pause_new_positions": True,
            "reason": (f"Evidence review: {verdict.get('verdict')}. "
                      f"{verdict.get('explanation', '')} New positions are "
                      f"paused pending human review. Existing positions and "
                      f"their stops are unaffected."),
        }
    return {
        "pause_new_positions": False,
        "reason": (f"Evidence review: {verdict.get('verdict')} ({n} closed "
                  f"trades). No policy change."),
    }


def _readable(weeks: float) -> str:
    if weeks < 8:
        return f"about {weeks:.0f} weeks"
    if weeks < 104:
        return f"about {weeks / 4.345:.0f} months"
    return f"about {weeks / 52:.1f} years"


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------

def bootstrap_ci(values: Sequence[float], alpha: float = 0.05,
                 draws: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean.

    Non-parametric on purpose: trade returns are skewed and fat-tailed, and a
    normal interval flatters a record containing one lucky outlier.
    """
    v = np.asarray(list(values), dtype=float)
    if len(v) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(draws, len(v)), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi)


def assess(outcomes: Sequence[Outcome], prereg: PreRegistration, *,
           asof: Optional[date] = None, looks_taken: int = 1) -> dict:
    """Grade the record against the claim that was registered in advance.

    `looks_taken` matters. Checking a growing record every week and stopping the
    first time it looks good is a way to manufacture significance out of noise,
    so the threshold is tightened by the number of looks. Bonferroni is
    conservative, which is the correct direction for a test whose false positive
    costs real money.
    """
    asof = asof or date.today()
    n = len(outcomes)

    plan = time_to_evidence(
        required_sample(prereg.target_edge_pct, prereg.assumed_sd_pct,
                        prereg.alpha, prereg.power),
        trades_per_week=10.0, horizon_days=prereg.horizon_days)

    base = {
        "n": n,
        "asof": asof.isoformat(),
        "pre_registration": prereg.to_dict(),
        "sample_needed": plan["required_n"],
        "looks_taken": looks_taken,
        "effective_alpha": prereg.alpha / max(1, looks_taken),
    }

    if n == 0:
        return {**base, "verdict": "no closed trades yet",
                "decision": "collect",
                "explanation": (f"Nothing has matured. On the registered claim this "
                                f"needs about {plan['required_n']} closed trades to settle.")}

    excess = [o.excess_pct for o in outcomes]
    mean = statistics.mean(excess)
    sd = statistics.stdev(excess) if n > 1 else float("nan")
    lo, hi = bootstrap_ci(excess, prereg.alpha) if n > 1 else (float("nan"), float("nan"))

    t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    crit = _z(1.0 - base["effective_alpha"])

    res = {
        **base,
        "mean_excess_pct": mean,
        "median_excess_pct": statistics.median(excess),
        "sd_excess_pct": sd,
        "ci_low_pct": lo,
        "ci_high_pct": hi,
        "t_stat": t,
        "critical_t": crit,
        "hit_rate": sum(1 for e in excess if e > 0) / n,
        "beat_benchmark_rate": sum(1 for o in outcomes
                                   if o.return_pct > o.benchmark_return_pct) / n,
        "mean_benchmark_pct": statistics.mean(o.benchmark_return_pct for o in outcomes),
        "thesis_accuracy": (sum(1 for o in outcomes if o.thesis_played_out) / n
                            if any(o.thesis_played_out is not None for o in outcomes) else None),
    }

    # --- futility: has the claimed edge already been ruled out? --------------
    # Checked BEFORE the sample-size gate on purpose. Ruling an edge out takes
    # less evidence than establishing one, and the whole point of this module is
    # to be able to stop early.
    if n > 1 and math.isfinite(hi) and hi < prereg.target_edge_pct:
        res.update(
            verdict="futile",
            decision="stop",
            explanation=(
                f"The claimed edge of {prereg.target_edge_pct:.2f}% per trade is "
                f"outside the confidence interval on {n} closed trades "
                f"(interval {lo:.2f}% to {hi:.2f}%). It has been ruled out at this "
                f"size, not merely unproven. Collecting more of the same will not "
                f"bring it back."),
        )
        return res

    if n < max(prereg.min_sample, 2):
        res.update(
            verdict="insufficient",
            decision="collect",
            explanation=(
                f"{n} closed trades is too few to separate skill from luck; the "
                f"registered claim needs about {plan['required_n']}. These figures "
                f"describe what happened and establish nothing."),
        )
        return res

    detected = math.isfinite(t) and t > crit and mean > 0
    if detected:
        res.update(
            verdict="edge detected, provisional",
            decision="continue",
            explanation=(
                f"Mean excess {mean:.2f}% per trade over {n} trades, t={t:.2f} "
                f"against a {crit:.2f} threshold tightened for {looks_taken} look(s). "
                f"Provisional: this is one strategy tested on one sample, and the "
                f"honest next step is out-of-sample confirmation, not more capital."),
        )
        return res

    if asof >= prereg.decide_by:
        res.update(
            verdict="no edge by the registered deadline",
            decision="stop",
            explanation=(
                f"The deadline registered in advance ({prereg.decide_by.isoformat()}) "
                f"has passed with mean excess {mean:.2f}% and t={t:.2f}, short of "
                f"the {crit:.2f} threshold. The rule was fixed before the data; "
                f"honour it."),
        )
        return res

    res.update(
        verdict="inconclusive",
        decision="collect",
        explanation=(
            f"Mean excess {mean:.2f}% over {n} trades, t={t:.2f} against {crit:.2f}. "
            f"Neither established nor ruled out. Deadline "
            f"{prereg.decide_by.isoformat()}."),
    )
    return res
