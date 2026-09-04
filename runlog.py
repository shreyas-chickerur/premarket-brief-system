"""
runlog — health logging, run manifests, and the morning self-audit.

Every run writes one manifest. Every morning's first act is to read the last N
manifests and audit itself against them before it looks at a single price.

The ordering is deliberate: a system that researches first and checks itself
afterwards has already wasted the run by the time it discovers it was broken.
"""

from __future__ import annotations

import json
import statistics
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from typing import Any, Optional, Sequence

SCHEMA_VERSION = 3

Severity = str  # info | warn | block


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    passed: bool
    severity: Severity          # severity IF it fails
    detail: str = ""
    value: Any = None

    def to_dict(self): return asdict(self)


@dataclass
class Stage:
    name: str
    started_at: str
    duration_ms: int
    ok: bool
    error: str = ""
    records: int = 0

    def to_dict(self): return asdict(self)


@dataclass
class ExternalCall:
    service: str
    operation: str
    ok: bool
    latency_ms: int
    detail: str = ""

    def to_dict(self): return asdict(self)


@dataclass
class Decision:
    """Every action AND every deliberate non-action is recorded. A day with no
    trades must leave the same audit trail as a day with three."""
    symbol: str
    action: str                 # buy | sell | trim | hold | reject | skip | none
    account: str                # individual | agentic
    executed: bool
    reason: str
    gate_failed: Optional[str] = None
    inputs: dict = field(default_factory=dict)

    def to_dict(self): return asdict(self)


def stop_filled_decision(order: dict, *, account: str) -> Optional["Decision"]:
    """A `Decision` for a stop-market order that actually filled, or
    `None` if this order was not a filled stop.

    Before 4 September 2026, nothing in this codebase distinguished a
    stop-loss fill from any other sell fill at the Decision-recording
    layer — `find_optimizations`'s "are stops too tight" check
    (`action == "stop_filled"`) could therefore never find a single
    matching decision; it was dead code keyed on a field nothing ever
    wrote. This fixes that half: every real, filled `stop_market` order
    from `get_equity_orders` now becomes a real `stop_filled` decision.
    It deliberately does NOT attempt the companion "did the price recover
    within 5 days" check that finding also depended on — see
    `find_optimizations`'s docstring for why that half needs a design
    this fix does not make on its own.

    `order` is a raw Robinhood order dict (the same shape
    `ledger.fills_from_orders` reads); call this once per order while
    rebuilding fills in `DAILY_PROCEDURE.md` Stage 0 step 7, for both
    accounts, and record every non-`None` result with `log.decide(...)`.
    """
    if str(order.get("type", "")) != "stop_market":
        return None
    qty = float(order.get("cumulative_quantity") or 0.0)
    if qty <= 0:
        return None

    price = order.get("average_price")
    if price in (None, ""):
        price = order.get("price")
    stop_price = order.get("stop_price")

    return Decision(
        symbol=str(order.get("symbol", "")).upper(),
        action="stop_filled",
        account=account,
        executed=True,
        reason=(f"stop order filled at {price}" if price not in (None, "")
               else "stop order filled"),
        inputs={
            "quantity": qty,
            "fill_price": float(price) if price not in (None, "") else None,
            "stop_price": float(stop_price) if stop_price not in (None, "") else None,
            "order_id": str(order.get("id", "")),
        },
    )


# The five-condition confidence gate, HANDOFF.md section 7, in the fixed
# order they are evaluated. Pinned here as the single canonical name for
# each condition -- `runlog.Decision.gate_failed` must be set to one of
# these exact strings (never free text) for `closest_calls` below to be
# able to rank a rejection by how far it got. A rejection recorded before
# even reaching the gate (e.g. a data-quality rejection, DAILY_PROCEDURE.md
# Stage 2) uses a different string and is deliberately not one of these.
GATE_CONDITIONS = (
    "catalyst",              # 1. a named catalyst with a date or window
    "two_sources",           # 2. two independent corroborating sources
    "invalidation_level",    # 3. a stated invalidation level
    "risk_sized",            # 4. size derived from the risk dial
    "no_blocking_conflict",  # 5. no wash sale / adding to a loser /
                             #    concentration / cash floor / whole-share /
                             #    anomaly conflict
)


def closest_calls(decisions: Sequence[dict], *, top: int = 3) -> list[dict]:
    """Which rejected ideas got furthest through the five-condition gate
    before failing -- worth surfacing on a day nothing clears it, instead
    of the email reporting bare silence (see `HANDOFF.md` section 7:
    "failing one demotes it to the watchlist with the failing condition
    named" -- that name is exactly what this ranks by).

    `decisions` are expected in `runlog.Decision.to_dict()` shape.
    `gate_failed`'s position in `GATE_CONDITIONS` is how many conditions a
    rejection cleared before it failed — later position, closer call. A
    decision whose `gate_failed` is not one of the five canonical
    condition names (never reached the gate at all -- a data-quality
    rejection, for instance) is excluded rather than ranked as an
    infinitely close miss.

    Returns up to `top` decisions closest first (ties broken by input
    order), each with `conditions_cleared`/`conditions_total` added so a
    caller can render "cleared N of 5" without needing `GATE_CONDITIONS`
    itself. Empty input, or a day with no gate rejections at all, returns
    `[]`.
    """
    total = len(GATE_CONDITIONS)
    ranked = []
    for d in decisions:
        g = d.get("gate_failed")
        if g in GATE_CONDITIONS:
            idx = GATE_CONDITIONS.index(g)
            ranked.append((idx, {**d, "conditions_cleared": idx, "conditions_total": total}))
    ranked.sort(key=lambda t: -t[0])
    return [d for _, d in ranked[:top]]


@dataclass
class CircuitBreakerVerdict:
    """What `circuit_breaker_usd` / `hard_stop_usd` actually do, enforced
    rather than merely reported. Before this existed, the daily procedure's
    only instruction was to "report where equity sits relative to" these two
    numbers -- nothing in the codebase ever stopped an order because of them,
    which is not a circuit breaker, it is a status line."""
    halt_new_positions: bool
    liquidate: bool
    reason: str
    tripped_by_this_run: bool   # False when a prior, still-unresolved trip is
                                 # why -- distinguishes "this crossed a line
                                 # just now" from "someone still needs to clear
                                 # yesterday's trip", which reads very
                                 # differently in an email.

    def to_dict(self): return asdict(self)


def circuit_breaker_check(equity: float, circuit_breaker_usd: float,
                           hard_stop_usd: float, *,
                           standing_trip: Optional[dict] = None
                           ) -> CircuitBreakerVerdict:
    """Two distinct thresholds, two distinct actions -- never conflate them.

    `circuit_breaker_usd` (the higher, softer threshold): stop opening NEW
    positions. Existing positions and their stops are untouched -- this is
    the same "pause, don't unwind" shape as `evidence.trading_policy`'s
    `stop` verdict, and for the same reason: a false alarm that liquidates a
    healthy book is worse than a few missed days.

    `hard_stop_usd` (the lower, harder threshold): liquidate the agentic
    account to cash and halt entirely. This is not a suggestion and not
    reversible by inaction -- it is the catastrophe backstop for a mistake
    everything upstream of this function failed to catch.

    **Both require a human to clear them before the run resumes normal
    operation, even after equity recovers above the threshold that tripped
    it** -- `standing_trip` is the payload of an unresolved
    `circuit_breaker_tripped` journal entry (see `ledger.Journal`), or None
    if the last such entry was a `circuit_breaker_cleared`. A V-shaped bounce
    the very next morning is exactly the case a human should look at once,
    not the case that should silently un-halt itself.
    """
    if hard_stop_usd > circuit_breaker_usd:
        raise ValueError(
            f"hard_stop_usd ({hard_stop_usd}) must be <= circuit_breaker_usd "
            f"({circuit_breaker_usd}) -- the hard stop is the lower, harder "
            f"threshold, checked first")

    if equity < hard_stop_usd:
        return CircuitBreakerVerdict(
            halt_new_positions=True, liquidate=True, tripped_by_this_run=True,
            reason=(f"equity {equity:.2f} is below the hard stop "
                    f"{hard_stop_usd:.2f} -- liquidating the agentic account "
                    f"to cash and halting entirely. This is not a suggestion. "
                    f"A human must review and record a "
                    f"'circuit_breaker_cleared' journal entry before any "
                    f"future run resumes trading."))
    if equity < circuit_breaker_usd:
        return CircuitBreakerVerdict(
            halt_new_positions=True, liquidate=False, tripped_by_this_run=True,
            reason=(f"equity {equity:.2f} is below the circuit breaker "
                    f"{circuit_breaker_usd:.2f} -- no new positions until a "
                    f"human reviews and records a 'circuit_breaker_cleared' "
                    f"journal entry. Existing positions and their stops are "
                    f"untouched."))
    if standing_trip:
        prior_reason = standing_trip.get("reason", "no reason recorded")
        return CircuitBreakerVerdict(
            halt_new_positions=True, liquidate=False, tripped_by_this_run=False,
            reason=(f"equity has recovered to {equity:.2f}, but a prior trip "
                    f"was never cleared ({prior_reason}) -- new positions "
                    f"stay paused until a human records a "
                    f"'circuit_breaker_cleared' entry, "
                    f"recovered equity is not itself a clearance."))
    return CircuitBreakerVerdict(
        halt_new_positions=False, liquidate=False, tripped_by_this_run=False,
        reason="")


# --------------------------------------------------------------------------
# the run log
# --------------------------------------------------------------------------

class RunLog:
    def __init__(self, run_id: str, *, now: Optional[datetime] = None,
                 mode: str = "live"):
        self.schema = SCHEMA_VERSION
        self.run_id = run_id
        self.mode = mode                       # live | dry_run | verification
        self.started_at = (now or datetime.now(timezone.utc)).isoformat()
        self.checks: list[Check] = []
        self.stages: list[Stage] = []
        self.calls: list[ExternalCall] = []
        self.anomalies: list[dict] = []
        self.decisions: list[Decision] = []
        self.metrics: dict[str, Any] = {}
        self.errors: list[str] = []
        self.aborted: bool = False
        self.abort_reason: str = ""
        self._t0 = time.monotonic()

    # -- recording -------------------------------------------------------
    def check(self, name: str, passed: bool, severity: Severity = "warn",
              detail: str = "", value: Any = None) -> Check:
        c = Check(name, passed, severity, detail, value)
        self.checks.append(c)
        return c

    def call(self, service: str, operation: str, ok: bool,
             latency_ms: int, detail: str = "") -> None:
        self.calls.append(ExternalCall(service, operation, ok, latency_ms, detail))

    def anomaly(self, a) -> None:
        self.anomalies.append(a if isinstance(a, dict) else a.to_dict())

    def decide(self, d: Decision) -> None:
        self.decisions.append(d)

    def metric(self, key: str, value: Any) -> None:
        self.metrics[key] = value

    def abort(self, reason: str) -> None:
        """Record why the run stopped. The FIRST call wins -- `preflight`
        runs its blocking checks in a deliberate order (e.g. journal
        readability before ledger reconciliation, so a hidden opening
        balance is named directly rather than surfacing as a confusing
        downstream reconciliation failure), and a later block is very
        often a consequence of an earlier one rather than an independent
        second cause. Overwriting the reason on every call would report
        whichever check happened to run last, not the actual root cause."""
        self.aborted = True
        if not self.abort_reason:
            self.abort_reason = reason

    def stage(self, name: str):
        return _StageCtx(self, name)

    # -- derived ---------------------------------------------------------
    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "block"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warn"]

    @property
    def may_trade(self) -> bool:
        return not self.aborted and not self.blocking_failures and not self.errors

    def health(self) -> str:
        if self.aborted or self.blocking_failures or self.errors:
            return "critical"
        if self.warnings or any(not c.ok for c in self.stages) or any(not c.ok for c in self.calls):
            return "degraded"
        return "nominal"

    def manifest(self) -> dict:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "duration_ms": int((time.monotonic() - self._t0) * 1000),
            "health": self.health(),
            "may_trade": self.may_trade,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "checks": [c.to_dict() for c in self.checks],
            "stages": [s.to_dict() for s in self.stages],
            "calls": [c.to_dict() for c in self.calls],
            "anomalies": self.anomalies,
            "decisions": [d.to_dict() for d in self.decisions],
            "metrics": self.metrics,
            "errors": self.errors,
        }

    def to_json(self) -> str:
        return json.dumps(self.manifest(), indent=2, sort_keys=True, default=str)


class _StageCtx:
    def __init__(self, log: RunLog, name: str):
        self.log, self.name, self.records = log, name, 0

    def __enter__(self):
        self._start = time.monotonic()
        self._started_at = datetime.now(timezone.utc).isoformat()
        return self

    def __exit__(self, exc_type, exc, tb):
        dur = int((time.monotonic() - self._start) * 1000)
        ok = exc_type is None
        err = ""
        if not ok:
            err = f"{exc_type.__name__}: {exc}"
            self.log.errors.append(f"[{self.name}] {err}")
        self.log.stages.append(Stage(self.name, self._started_at, dur, ok, err, self.records))
        return False        # never swallow


# --------------------------------------------------------------------------
# the morning self-audit
# --------------------------------------------------------------------------

# PROVENANCE: cross-checked 28 Aug 2026 against the exchange's own published
# holiday calendar at https://www.nyse.com/markets/hours-calendars, not merely
# derived from the observance rules. A wrong date here makes the system think a
# closed market is open, so the table is verified rather than computed.
#
# Table covers 28 Aug 2026 through 31 Dec 2027. `holiday_table_covers` below is
# the expiry guard: once the run date passes the horizon the preflight fails
# loudly instead of silently trusting an exhausted table.
#
# One subtlety the observance rules alone get wrong, and which cost this table a
# bug: when a holiday falls on a Saturday the preceding Friday normally closes,
# BUT the exchange does not apply that to New Year's Day when the substitute
# Friday would be the last trading day of the year. 1 Jan 2028 is a Saturday and
# 31 Dec 2027 is therefore a REGULAR SESSION, not a holiday. Precedent:
# 31 Dec 2010 and 31 Dec 2021 were both full trading days.
MARKET_HOLIDAYS_2026_2027 = {
    # 2026 (remaining after this system was written)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-01",  # New Year's Day
    "2027-01-18",  # Martin Luther King Jr. Day
    "2027-02-15",  # Washington's Birthday
    "2027-03-26",  # Good Friday
    "2027-05-31",  # Memorial Day
    "2027-06-18",  # Juneteenth (observed; 19 Jun falls on a Saturday)
    "2027-07-05",  # Independence Day (observed; 4 Jul falls on a Sunday)
    "2027-09-06",  # Labor Day
    "2027-11-25",  # Thanksgiving
    "2027-12-24",  # Christmas (observed; 25 Dec falls on a Saturday)
}

# 1:00 p.m. ET closes.
EARLY_CLOSE_2026_2027 = {
    "2026-11-27",  # day after Thanksgiving
    "2026-12-24",  # Christmas Eve
    "2027-11-26",  # day after Thanksgiving
}

# Last date the tables above are known-good for. Past this, the calendar check
# must fail rather than assume an unlisted date is a normal trading day.
HOLIDAY_TABLE_HORIZON = "2027-12-31"


def preflight(log: RunLog, *,
              available_tools: Sequence[str],
              required_tools: Sequence[str],
              local_time: datetime,
              expected_local_hhmm: tuple[int, int],
              tolerance_minutes: int = 30,
              history: Sequence[dict] = (),
              self_test: Optional[dict] = None,
              ledger_positions: Optional[dict] = None,
              broker_positions: Optional[dict] = None,
              unreadable_files: Sequence[str] = ()) -> RunLog:
    """Run before any research. Fills `log` with checks and may abort the run.

    Ordered cheapest-and-most-fatal first, so a broken run dies in milliseconds
    rather than after twenty minutes of research it cannot act on.
    """

    # 1. Are the tools we need actually here? The documented cold-start defect
    #    in scheduled runs makes this the single most important check.
    missing = [t for t in required_tools if t not in set(available_tools)]
    log.check("tools_available", not missing, "block",
              f"missing: {missing}" if missing else f"all {len(required_tools)} present",
              value=missing)
    if missing:
        log.abort(f"required tools not visible: {missing}")

    # 2. Did our own code survive the night? Cheap, and it is literally the
    #    "check for bugs first" requirement.
    if self_test is not None:
        passed = bool(self_test.get("passed"))
        log.check("self_test", passed, "block",
                  f"{self_test.get('n_passed', 0)} passed, "
                  f"{self_test.get('n_failed', 0)} failed in "
                  f"{self_test.get('duration_ms', 0)}ms",
                  value=self_test)
        if not passed:
            log.abort(f"self test failed: {self_test.get('summary', 'unknown')}")

    # 3. Did we fire when we meant to? This guards daylight-saving drift, so it
    #    must compare against the exact intended minute and hold a tolerance
    #    well under 60 — comparing to the top of the hour with a wide window
    #    would let a full one-hour shift pass unnoticed, which is precisely the
    #    failure this check exists to catch.
    exp_h, exp_m = expected_local_hhmm
    if not (0 <= exp_h < 24 and 0 <= exp_m < 60):
        raise ValueError(f"invalid expected_local_hhmm: {expected_local_hhmm}")
    if tolerance_minutes >= 60:
        raise ValueError("tolerance_minutes must be under 60 to detect a DST shift")
    delta = abs((local_time.hour * 60 + local_time.minute) - (exp_h * 60 + exp_m))
    delta = min(delta, 1440 - delta)          # wrap around midnight
    log.check("fired_on_schedule", delta <= tolerance_minutes, "warn",
              f"fired {local_time:%H:%M} local, expected {exp_h:02d}:{exp_m:02d} "
              f"(off by {delta} min)", value=delta)

    # 4. Is the market even open today?
    #    The holiday table is a hand-verified list with a finite horizon. Once
    #    the run date runs past that horizon an unlisted date is no longer
    #    evidence of a normal session, it is evidence the table expired -- so
    #    say so loudly rather than trading a closed market.
    iso = local_time.date().isoformat()
    table_live = iso <= HOLIDAY_TABLE_HORIZON
    log.check("holiday_table_current", table_live, "warn",
              f"verified through {HOLIDAY_TABLE_HORIZON}"
              if table_live else
              f"table expired {HOLIDAY_TABLE_HORIZON}; extend it from the "
              f"exchange's published calendar before trusting the session check",
              value=HOLIDAY_TABLE_HORIZON)

    is_weekend = local_time.weekday() >= 5
    is_holiday = iso in MARKET_HOLIDAYS_2026_2027
    log.check("market_open_today", not (is_weekend or is_holiday), "info",
              "weekend" if is_weekend else ("holiday" if is_holiday else "regular session"),
              value={"weekend": is_weekend, "holiday": is_holiday,
                     "early_close": iso in EARLY_CLOSE_2026_2027})

    # 5. Was every journal, fills-cache, and splits-cache file actually
    #    readable? Before 4 September 2026 a file that failed to parse was
    #    silently recorded in `Journal.unreadable` (or the fills/splits
    #    cache fold's `bad` list) and dropped without ever aborting the
    #    run -- a corrupt or truncated write could hide a thesis that
    #    would have matured, an opening balance a human recorded, or a
    #    standing circuit-breaker trip, none of which have any other way
    #    to be noticed. This runs BEFORE the ledger-reconciliation check
    #    below on purpose: if the hidden file was the one carrying an
    #    opening balance, reconciliation would otherwise misdiagnose a
    #    real, explained gap as spurious drift instead of naming the
    #    actual cause.
    unreadable_files = list(unreadable_files)
    log.check("journal_fully_readable", not unreadable_files, "block",
              "every journal/fills-cache/splits-cache file parsed"
              if not unreadable_files else
              f"{len(unreadable_files)} unreadable file(s): {unreadable_files}",
              value=unreadable_files)
    if unreadable_files:
        log.abort(f"unreadable journal/cache file(s): {unreadable_files}")

    # 6. Does the ledger agree with the broker? Disagreement means our memory of
    #    the world is wrong, and every sizing decision downstream is wrong too.
    if ledger_positions is not None and broker_positions is not None:
        drift = _reconcile(ledger_positions, broker_positions)
        log.check("ledger_reconciled", not drift, "block",
                  "in agreement" if not drift else f"{len(drift)} discrepancy: {drift}",
                  value=drift)
        if drift:
            log.abort(f"ledger and broker disagree on {list(drift)}")

    # 7. Regression against recent runs.
    for c in _regressions(history):
        log.checks.append(c)

    # 8. Standing improvement opportunities from the record.
    log.metric("optimizations", find_optimizations(history))
    return log


def _reconcile(ledger: dict, broker: dict, tol: float = 1e-4) -> dict:
    """Symbol -> (ledger_qty, broker_qty) for anything that disagrees.

    tol was 1e-6. Broker payloads carry six decimal places and round-trip
    through JSON and float arithmetic without preserving the last digit, so
    1e-6 was tight enough to flag positions that actually agreed -- a false
    drift abort waiting to happen. 1e-4 matches ledger.QTY_TOL, which was
    derived the same way against real broker data.
    """
    out = {}
    for sym in set(ledger) | set(broker):
        a, b = float(ledger.get(sym, 0)), float(broker.get(sym, 0))
        if abs(a - b) > tol:
            out[sym] = {"ledger": a, "broker": b}
    return out


def _regressions(history: Sequence[dict]) -> list[Check]:
    """Compare the recent record against itself and flag what is trending wrong."""
    checks: list[Check] = []
    if len(history) < 3:
        checks.append(Check("history_depth", True, "info",
                            f"only {len(history)} prior run(s); "
                            "regression checks need 3+", value=len(history)))
        return checks

    recent = list(history)[-10:]

    healths = [h.get("health") for h in recent]
    bad = sum(1 for h in healths if h in ("degraded", "critical"))
    checks.append(Check("recent_health", bad <= len(recent) // 2, "warn",
                        f"{bad} of {len(recent)} recent runs not nominal", value=healths))

    durs = [h.get("duration_ms", 0) for h in recent if h.get("duration_ms")]
    if len(durs) >= 3:
        med = statistics.median(durs[:-1]) or 1
        latest = durs[-1]
        checks.append(Check("duration_stable", latest <= med * 2.5, "warn",
                            f"last run {latest}ms vs median {med:.0f}ms",
                            value={"latest": latest, "median": med}))

    # A check that fails on most days is either a real standing problem or a
    # badly written check. Either way it needs a human's attention, not silence.
    tally: dict[str, int] = {}
    for h in recent:
        for c in h.get("checks", []):
            if not c.get("passed"):
                tally[c["name"]] = tally.get(c["name"], 0) + 1
    chronic = {k: v for k, v in tally.items() if v >= max(3, len(recent) // 2)}
    checks.append(Check("no_chronic_failures", not chronic, "warn",
                        f"chronic: {chronic}" if chronic else "none", value=chronic))

    # Repeated data anomalies on one symbol means the source is unreliable there.
    sym_tally: dict[str, int] = {}
    for h in recent:
        for a in h.get("anomalies", []):
            if a.get("severity") in ("block", "warn"):
                sym_tally[a.get("symbol", "?")] = sym_tally.get(a.get("symbol", "?"), 0) + 1
    repeat = {k: v for k, v in sym_tally.items() if v >= 3}
    checks.append(Check("no_repeat_data_faults", not repeat, "warn",
                        f"repeat offenders: {repeat}" if repeat else "none", value=repeat))

    fail_calls = [c for h in recent for c in h.get("calls", []) if not c.get("ok")]
    rate = len(fail_calls) / max(1, sum(len(h.get("calls", [])) for h in recent))
    checks.append(Check("external_call_reliability", rate < 0.15, "warn",
                        f"{rate:.0%} of recent external calls failed", value=round(rate, 4)))
    return checks


# Per-stage timing budgets, in milliseconds. Initial estimates -- judgment
# calls, not measurements, the same category as `quantcore.gap_risk_haircut`
# -- since this system has not yet accumulated enough real per-stage timing
# history to calibrate them against (see `HANDOFF.md` section 12). Revisit
# once `history` (section 8) has enough runs to know what "slow" actually
# looks like for each stage. Keys match the `name` passed to
# `RunLog.stage(name)` and are expected to line up with `DAILY_PROCEDURE.md`'s
# own Stage 0 through Stage 6 numbering -- a name used there that is not a
# key here is not budgeted (see `stage_budget_overruns`), not an error.
STAGE_TIMING_BUDGETS_MS = {
    "preflight": 15_000,          # Stage 0
    "evidence_review": 10_000,    # Stage 0.5
    "prior_day_review": 5_000,    # Stage 0.6
    "gather": 120_000,            # Stage 1 -- the most external calls by far
    "measure": 30_000,            # Stage 2
    "gate": 5_000,                # Stage 3 -- pure computation, no external calls
    "individual_account": 15_000, # Stage 4
    "agentic_account": 30_000,    # Stage 5 -- places real orders
    "record_and_send": 15_000,    # Stage 6
}


def stage_budget_overruns(stages: Sequence[dict], *,
                          budgets: dict[str, int] = STAGE_TIMING_BUDGETS_MS
                          ) -> list[dict]:
    """Which of this run's stages took longer than their budget.

    Surfaced in the email's "System health" section (Stage 6) rather than
    left buried in the raw manifest, so a stage that has quietly grown
    slow over time is visible on the one artefact a human actually reads
    every single day — not only in `find_optimizations`'s eventual
    "performance" finding, which needs 5+ runs of history before it can
    say anything and only ever names the single slowest stage, not every
    stage currently over budget.

    `stages` are expected in `Stage.to_dict()` shape (`manifest()["stages"]`).
    A stage name with no entry in `budgets` is not flagged — an unbudgeted
    name signals a naming mismatch to go fix, not a performance problem,
    and is silently skipped rather than raising, since a manifest from
    before this existed (or a stage name that has not been budgeted yet)
    must not break `System health` rendering.
    """
    out = []
    for s in stages:
        name = s.get("name")
        budget = budgets.get(name)
        dur = s.get("duration_ms", 0)
        if budget is not None and dur > budget:
            out.append({"name": name, "duration_ms": dur, "budget_ms": budget,
                        "over_by_ms": dur - budget})
    return out


def find_optimizations(history: Sequence[dict]) -> list[dict]:
    """Look for things worth changing. Proposals only — nothing self-modifies.

    Each finding names the evidence and the sample size, so a pattern seen four
    times is never dressed up as a validated conclusion.

    **No "stops too tight" finding.** An earlier version of this function
    looked for `action == "stop_filled"` decisions whose `inputs` carried
    `recovered_within_5d`, on the theory that a stop that filled and then
    saw the price promptly recover suggests the volatility multiple is too
    tight. Confirmed 4 September 2026: nothing in this codebase had ever
    written either field — `stop_filled` is now emitted for real (see
    `stop_filled_decision`), but `recovered_within_5d` cannot be. It
    describes what happens AFTER a decision is recorded, and the
    append-only journal has no way to retroactively enrich an
    already-written entry — the same reason theses use a separate
    `"close"`/`"outcome"` pair days later rather than editing the original
    `"thesis"` entry. Building this finding properly needs either that same
    separate-entry pattern (a real design decision, not implied by this
    one) or a live price lookup this otherwise pure, data-free function
    does not have. Rather than keep a finding permanently silent behind a
    field nothing could ever populate, it was removed; the raw
    `stop_filled` decisions are still in the journal for a human to review
    by hand, or for a future version of this function built around one of
    those two designs. See `HANDOFF.md` section 12.
    """
    out: list[dict] = []
    if len(history) < 5:
        return out
    recent = list(history)[-30:]
    n = len(recent)

    # Which gate condition is doing the rejecting?
    gates: dict[str, int] = {}
    for h in recent:
        for d in h.get("decisions", []):
            g = d.get("gate_failed")
            if g:
                gates[g] = gates.get(g, 0) + 1
    if gates:
        top, cnt = max(gates.items(), key=lambda kv: kv[1])
        total = sum(gates.values())
        if cnt / total > 0.6 and total >= 10:
            out.append({"kind": "gate_balance", "confidence": "observational",
                        "sample": total,
                        "finding": f"{cnt/total:.0%} of rejections come from '{top}' alone",
                        "proposal": "confirm this gate is discriminating rather than "
                                    "just hard to satisfy mechanically"})

    # Are we producing anything at all?
    idle = sum(1 for h in recent
               if not any(d.get("executed") for d in h.get("decisions", [])))
    if n >= 10 and idle / n > 0.85:
        out.append({"kind": "throughput", "confidence": "observational", "sample": n,
                    "finding": f"no action taken on {idle} of {n} runs",
                    "proposal": "verify the gate is calibrated, not merely unreachable"})

    # Slowest stage, if it dominates.
    agg: dict[str, list[int]] = {}
    for h in recent:
        for s in h.get("stages", []):
            agg.setdefault(s["name"], []).append(s.get("duration_ms", 0))
    if agg:
        means = {k: statistics.mean(v) for k, v in agg.items()}
        slow = max(means, key=means.get)
        total = sum(means.values()) or 1
        if means[slow] / total > 0.5:
            out.append({"kind": "performance", "confidence": "measured", "sample": n,
                        "finding": f"stage '{slow}' is {means[slow]/total:.0%} of runtime "
                                   f"({means[slow]:.0f}ms average)",
                        "proposal": "narrow or cache this stage's inputs"})
    return out


# --------------------------------------------------------------------------
# scoring past suggestions
# --------------------------------------------------------------------------

def score_closed_decisions(closed: Sequence[dict]) -> dict:
    """Grade the record honestly, including refusing to grade a small sample.

    `closed` items need: outcome_pct, thesis_played_out (bool), horizon_days.
    """
    n = len(closed)
    if n == 0:
        return {"n": 0, "verdict": "no closed positions yet"}

    rets = [float(c["outcome_pct"]) for c in closed]
    wins = [r for r in rets if r > 0]
    played = sum(1 for c in closed if c.get("thesis_played_out"))

    res = {
        "n": n,
        "hit_rate": len(wins) / n,
        "mean_return_pct": statistics.mean(rets),
        "median_return_pct": statistics.median(rets),
        "best_pct": max(rets), "worst_pct": min(rets),
        "thesis_accuracy": played / n,
    }
    if n >= 2:
        sd = statistics.stdev(rets)
        res["return_stdev_pct"] = sd
        res["mean_over_sd"] = statistics.mean(rets) / sd if sd > 0 else None

    # The honesty gate. Below roughly 30 closed trades, per-trade noise swamps
    # any plausible edge, and reporting a Sharpe-like number would be theatre.
    #
    # `statistically_meaningful` is a three-valued string, never a bool. It used
    # to return False below 100 trades and the STRING "provisional" from 100 up
    # -- any `if result["statistically_meaningful"]:` check treated "provisional"
    # as truthy, silently certifying a sample the verdict text next to it calls
    # provisional. A caller that wants a yes/no should compare the string, not
    # branch on it as a bool.
    if n < 30:
        res["verdict"] = (f"{n} closed trades is too few to distinguish skill from luck. "
                          f"These figures describe what happened; they do not "
                          f"establish that the process works.")
        res["statistically_meaningful"] = "no"
    elif n < 100:
        res["verdict"] = (f"{n} closed trades — still a small sample. Treat any edge "
                          f"as provisional and check it against a deflated Sharpe ratio "
                          f"before acting on it.")
        res["statistically_meaningful"] = "no"
    else:
        res["verdict"] = (f"{n} closed trades. Treat any edge as provisional and check "
                          f"it against a deflated Sharpe ratio before acting on it.")
        res["statistically_meaningful"] = "provisional"
    return res
