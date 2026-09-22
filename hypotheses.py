"""hypotheses — candidate signals for the $1,000 agentic account, tested the
only way a search can be honest: a frozen, numbered family per round,
screened on a DISCOVERY period and confirmed on a HOLDOUT period it never saw
(hypotheses_prereg.json).

Why the ceremony: per-trade excess returns here have a spread of roughly 13%.
Try enough variants on one sample and one of them will "work" by chance --
that is the whole failure mode this project exists to avoid. A hypothesis
counts only if it survives the holdout with its threshold corrected for how
many were carried there, and significance is counted over calendar MONTHS,
not trades, because overlapping trades share the same market days.

Every signal is point-in-time: it is built only from what was public before
its entry session, and each one says so next to the line that enforces it.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

import pandas as pd

import backtest as B
import evidence
import washsale


# --------------------------------------------------------------------------
# session arithmetic
# --------------------------------------------------------------------------

def reaction_session(index: pd.DatetimeIndex, report_date: date,
                     report_time: Optional[str]) -> Optional[pd.Timestamp]:
    """The first session that trades on the report: the report day itself
    for a pre-market release, the next session otherwise. An unknown time is
    treated as post-market -- the later, conservative choice, so a signal
    can never be read before it was published."""
    rd = pd.Timestamp(report_date)
    later = index[index >= rd] if report_time == "pre-market" else index[index > rd]
    return later[0] if len(later) else None


def session_offset(index: pd.DatetimeIndex, ts: pd.Timestamp, n: int) -> Optional[pd.Timestamp]:
    i = index.get_loc(ts) + n
    return index[i] if 0 <= i < len(index) else None


def exit_after(index: pd.DatetimeIndex, entry: pd.Timestamp, horizon_days: int) -> Optional[pd.Timestamp]:
    later = index[index >= entry + pd.Timedelta(days=horizon_days)]
    return later[0] if len(later) else None


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def reaction_excess_pct(px_tr: pd.DataFrame, spy_tr: pd.DataFrame, reaction: pd.Timestamp) -> Optional[float]:
    """The stock's close-to-close move on the reaction session, minus SPY's.
    Known at that session's close -- entries using it are placed the NEXT
    session."""
    prev = session_offset(px_tr.index, reaction, -1)
    if prev is None or reaction not in spy_tr.index or prev not in spy_tr.index:
        return None
    r = px_tr.loc[reaction, "close"] / px_tr.loc[prev, "close"] - 1
    b = spy_tr.loc[reaction, "close"] / spy_tr.loc[prev, "close"] - 1
    return float((r - b) * 100)


# --------------------------------------------------------------------------
# signals -> candidate trades
# --------------------------------------------------------------------------

def pead_trials(symbol: str, earnings: dict, px_tr: pd.DataFrame, spy_tr: pd.DataFrame, *,
                horizon_days: int, start: date, end: date,
                min_surprise_pct: Optional[float] = None,
                min_reaction_excess_pct: Optional[float] = None) -> list[dict]:
    """Post-earnings drift: buy after a good report, hold `horizon_days`.

    Point in time: the surprise and the reaction are both known by the close
    of the reaction session, and the entry is the OPEN OF THE SESSION AFTER
    it. Nothing before the report is used, so the report's own numbers are
    fair game here in a way they never were for the pre-report gate."""
    out = []
    for q in earnings.get("quarterlyEarnings", []):
        try:
            rd = date.fromisoformat(str(q.get("reportedDate", ""))[:10])
        except ValueError:
            continue
        rs = reaction_session(px_tr.index, rd, q.get("reportTime"))
        entry = session_offset(px_tr.index, rs, 1) if rs is not None else None
        if entry is None or not (start <= entry.date() <= end):
            continue
        surprise = _num(q.get("surprisePercentage"))
        if min_surprise_pct is not None and (surprise is None or surprise < min_surprise_pct):
            continue
        rx = reaction_excess_pct(px_tr, spy_tr, rs)
        if min_reaction_excess_pct is not None and (rx is None or rx < min_reaction_excess_pct):
            continue
        ex = exit_after(px_tr.index, entry, horizon_days)
        if ex is None:
            continue
        out.append({"symbol": symbol.upper(), "entry_session": entry, "exit_session": ex,
                    "signal": {"report_date": rd.isoformat(), "surprise_pct": surprise,
                               "reaction_excess_pct": rx}})
    return out


def runup_trials(symbol: str, earnings: dict, px_tr: pd.DataFrame, *, lead_sessions: int,
                 start: date, end: date) -> list[dict]:
    """The pre-announcement run-up: buy `lead_sessions` before the reaction
    session, sell at the close of the session before it -- out before the
    number is known, so the trade is on attention, not on the result.

    Look-ahead disclosed: this treats the report date as known
    `lead_sessions` ahead. Companies announce dates weeks in advance, but a
    date that later moved is a small leak this cannot see."""
    out = []
    for q in earnings.get("quarterlyEarnings", []):
        try:
            rd = date.fromisoformat(str(q.get("reportedDate", ""))[:10])
        except ValueError:
            continue
        rs = reaction_session(px_tr.index, rd, q.get("reportTime"))
        if rs is None:
            continue
        entry, ex = session_offset(px_tr.index, rs, -lead_sessions), session_offset(px_tr.index, rs, -1)
        if entry is None or ex is None or not (start <= entry.date() <= end):
            continue
        out.append({"symbol": symbol.upper(), "entry_session": entry, "exit_session": ex,
                    "signal": {"report_date": rd.isoformat()}})
    return out


def momentum_trials(prices_tr: dict, spy_tr: pd.DataFrame, *, top_n: int, start: date, end: date,
                    lookback_sessions: int = 252, skip_sessions: int = 21,
                    horizon_days: int = 21) -> list[dict]:
    """12-1 momentum: on the first session of each month, buy the `top_n`
    names with the best return from ~12 months to ~1 month ago, hold
    `horizon_days`. Ranked only on closes strictly before the entry
    session."""
    months = spy_tr.index[(spy_tr.index >= pd.Timestamp(start)) & (spy_tr.index <= pd.Timestamp(end))]
    firsts = pd.Series(months, index=months).groupby([months.year, months.month]).first()
    out = []
    for entry in firsts:
        ranked = []
        for sym, px in prices_tr.items():
            past = px.loc[px.index < entry, "close"]
            if len(past) <= lookback_sessions or entry not in px.index:
                continue
            ret = past.iloc[-1 - skip_sessions] / past.iloc[-1 - lookback_sessions] - 1
            ranked.append((float(ret), sym))
        for ret, sym in sorted(ranked, reverse=True)[:top_n]:
            ex = exit_after(prices_tr[sym].index, entry, horizon_days)
            if ex is not None:
                out.append({"symbol": sym, "entry_session": entry, "exit_session": ex,
                            "signal": {"momentum_12_1": round(ret, 4)}})
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def settle_between(symbol: str, px_tr: pd.DataFrame, spy_tr: pd.DataFrame, *,
                   entry: pd.Timestamp, exit: pd.Timestamp, cost_pct: float) -> Optional[evidence.Outcome]:
    """Total-return outcome from the entry session's open to the exit
    session's close, benchmarked to SPY over exactly the same span."""
    if entry not in spy_tr.index or exit not in spy_tr.index or exit not in px_tr.index:
        return None
    return evidence.Outcome(
        thesis_id=f"h-{symbol}-{entry.date().isoformat()}", symbol=symbol, opened=entry.date(),
        closed=exit.date(), entry=float(px_tr.loc[entry, "open"]), exit=float(px_tr.loc[exit, "close"]),
        benchmark_entry=float(spy_tr.loc[entry, "open"]), benchmark_exit=float(spy_tr.loc[exit, "close"]),
        cost_pct=cost_pct)


def monthly_t(outcomes: Sequence[evidence.Outcome]) -> dict:
    """Mean excess per calendar month of entry, then a t-statistic across
    months. Trades opened the same month share the same market days; counting
    them as independent overstates confidence."""
    by: dict = {}
    for o in outcomes:
        by.setdefault((o.opened.year, o.opened.month), []).append(o.excess_pct)
    means = [statistics.mean(v) for v in by.values()]
    n = len(means)
    if n < 2:
        return {"months": n, "t": float("nan"), "mean_monthly_excess_pct": means[0] if means else float("nan")}
    sd = statistics.stdev(means)
    return {"months": n, "mean_monthly_excess_pct": statistics.mean(means),
            "t": statistics.mean(means) / (sd / math.sqrt(n)) if sd > 0 else float("nan")}


def tradable(trial: dict, px_raw: pd.DataFrame, *, account_equity: float) -> dict:
    """Would the $1,000 account take this trade? The live sizing and
    anomaly checks, point in time, at the entry session."""
    v = B.evaluate_price_conditions(trial["symbol"], px_raw, decision_date=trial["entry_session"].date(),
                                    account_equity=account_equity, registry=washsale.Registry())
    return v


def evaluate(trials: Iterable[dict], raw: dict, tr: dict, spy_tr: pd.DataFrame, *,
             account_equity: float, cost_pct: float) -> dict:
    """Score one hypothesis over one period: which trials the $1,000 account
    could take, their outcomes, per-trade and calendar-month statistics."""
    taken, gated, outcomes = [], {}, []
    for t in trials:
        v = tradable(t, raw[t["symbol"]], account_equity=account_equity)
        if v["gate_failed"]:
            gated[v["gate_failed"]] = gated.get(v["gate_failed"], 0) + 1
            continue
        o = settle_between(t["symbol"], tr[t["symbol"]], spy_tr, entry=t["entry_session"],
                           exit=t["exit_session"], cost_pct=cost_pct)
        if o is None:
            continue
        outcomes.append(o)
        taken.append({"symbol": t["symbol"], "session": v["session"], "sized_at": v["sized_at"],
                      "entry": v["entry"], "plan": v["plan"], "exit_on": t["exit_session"].date().isoformat()})
    ex = [o.excess_pct for o in outcomes]
    return {"candidates": len(taken) + sum(gated.values()), "gated": gated, "n": len(outcomes),
            "mean_excess_pct": statistics.mean(ex) if ex else float("nan"),
            "median_excess_pct": statistics.median(ex) if ex else float("nan"),
            "sd_excess_pct": statistics.stdev(ex) if len(ex) > 1 else float("nan"),
            "win_rate_vs_spy": sum(e > 0 for e in ex) / len(ex) if ex else float("nan"),
            **monthly_t(outcomes), "trials": taken, "outcomes": outcomes}


# --------------------------------------------------------------------------
# round 2: insiders and Congress
# --------------------------------------------------------------------------

import re as _re

FORM4_LAG_BUSINESS_DAYS = 4          # Form 4 is due in 2 business days; 2 more of margin
_OFFICER = _re.compile(r"\b(ceo|chief executive|cfo|chief financial|president)\b", _re.IGNORECASE)


def open_market_buys(raw: Optional[dict]) -> list[dict]:
    """Insider purchases from an Alpha Vantage INSIDER_TRANSACTIONS payload.

    The feed carries no transaction code, so an open-market buy is inferred:
    an acquisition ("A") of common stock at a real price (awards come through
    at 0), NOT matched by a disposal by the same person on the same date
    (the shape of an option exercise sold straight away). A preview envelope
    or missing payload yields nothing -- never a guess."""
    rows = (raw or {}).get("data") if isinstance(raw, dict) and not raw.get("preview") else None
    if not isinstance(rows, list):
        return []
    sold = {(r.get("executive"), r.get("transaction_date")) for r in rows
            if r.get("acquisition_or_disposal") == "D"}
    out = []
    for r in rows:
        price, shares = _num(r.get("share_price")), _num(r.get("shares"))
        if (r.get("acquisition_or_disposal") != "A" or not price or price <= 0 or not shares
                or "common" not in str(r.get("security_type", "")).lower()
                or (r.get("executive"), r.get("transaction_date")) in sold):
            continue
        try:
            d = date.fromisoformat(str(r["transaction_date"])[:10])
        except (KeyError, ValueError):
            continue
        out.append({"date": d, "executive": r.get("executive"), "title": r.get("executive_title") or "",
                    "value": shares * price})
    return sorted(out, key=lambda b: b["date"])


def _first_session_on_or_after(index: pd.DatetimeIndex, ts: pd.Timestamp) -> Optional[pd.Timestamp]:
    later = index[index >= ts]
    return later[0] if len(later) else None


def _dedupe(signals: list[tuple[date, dict]], gap_days: int) -> list[tuple[date, dict]]:
    out, last = [], None
    for d, info in sorted(signals, key=lambda x: x[0]):
        if last is None or (d - last).days >= gap_days:
            out.append((d, info))
            last = d
    return out


def insider_trials(symbol: str, raw: Optional[dict], px_tr: pd.DataFrame, *, mode: str,
                   horizon_days: int, start: date, end: date, cluster_days: int = 30,
                   min_executives: int = 2, min_value: float = 100_000, dedupe_days: int = 90) -> list[dict]:
    """`mode="cluster"`: at least `min_executives` different insiders buying
    within `cluster_days`. `mode="large_officer"`: one CEO/CFO/President buy
    worth at least `min_value`. Entry: the first session on or after the
    signal date plus FORM4_LAG_BUSINESS_DAYS -- the purchase is public by
    then, and not before."""
    buys, signals = open_market_buys(raw), []
    for b in buys:
        if mode == "cluster":
            execs = {x["executive"] for x in buys if 0 <= (b["date"] - x["date"]).days <= cluster_days}
            if len(execs) >= min_executives:
                signals.append((b["date"], {"executives": len(execs)}))
        elif mode == "large_officer":
            if b["value"] >= min_value and _OFFICER.search(b["title"]):
                signals.append((b["date"], {"value": round(b["value"]), "title": b["title"]}))
        else:
            raise ValueError(mode)
    out = []
    for d, info in _dedupe(signals, dedupe_days):
        entry = _first_session_on_or_after(px_tr.index, pd.Timestamp(d) + pd.offsets.BDay(FORM4_LAG_BUSINESS_DAYS))
        if entry is None or not (start <= entry.date() <= end):
            continue
        ex = exit_after(px_tr.index, entry, horizon_days)
        if ex is not None:
            out.append({"symbol": symbol.upper(), "entry_session": entry, "exit_session": ex,
                        "signal": {"signal_date": d.isoformat(), **info}})
    return out


def congress_trials(symbol: str, raw: Optional[dict], px_tr: pd.DataFrame, *, horizon_days: int,
                    start: date, end: date, min_amount: float = 0.0, dedupe_days: int = 30) -> list[dict]:
    """A member of Congress disclosed a purchase. Entry: the first session
    AFTER `filed_date` -- the trade itself can be 45 days older, and nobody
    could act on it before the filing was public."""
    trades = (raw or {}).get("trades") if isinstance(raw, dict) and not raw.get("preview") else None
    signals = []
    for t in trades or []:
        if str(t.get("transaction_type", "")).upper() not in ("BUY", "PURCHASE", "P"):
            continue
        if (_num(t.get("amount_min")) or 0.0) < min_amount:
            continue
        try:
            filed = date.fromisoformat(str(t.get("filed_date") or t.get("notification_date"))[:10])
        except ValueError:
            continue
        signals.append((filed, {"member": t.get("politician_canonical") or t.get("politician"),
                                "amount_min": _num(t.get("amount_min"))}))
    out = []
    for d, info in _dedupe(signals, dedupe_days):
        later = px_tr.index[px_tr.index > pd.Timestamp(d)]
        if not len(later) or not (start <= later[0].date() <= end):
            continue
        ex = exit_after(px_tr.index, later[0], horizon_days)
        if ex is not None:
            out.append({"symbol": symbol.upper(), "entry_session": later[0], "exit_session": ex,
                        "signal": {"filed_date": d.isoformat(), **info}})
    return out


# --------------------------------------------------------------------------
# round 3: rankings, a market filter, and combinations
# --------------------------------------------------------------------------

def momentum_score(px_tr: pd.DataFrame, entry: pd.Timestamp, lookback: int = 252, skip: int = 21) -> Optional[float]:
    """12-1 month return from closes strictly before `entry`."""
    past = px_tr.loc[px_tr.index < entry, "close"]
    if len(past) <= lookback:
        return None
    return float(past.iloc[-1 - skip] / past.iloc[-1 - lookback] - 1)


def volatility_score(px_tr: pd.DataFrame, entry: pd.Timestamp, lookback: int = 252) -> Optional[float]:
    """Annualised stdev of daily returns over the `lookback` sessions strictly
    before `entry`."""
    past = px_tr.loc[px_tr.index < entry, "close"]
    if len(past) <= lookback:
        return None
    r = past.iloc[-lookback - 1:].pct_change().dropna()
    return float(r.std() * math.sqrt(252))


def market_above_trend(spy_tr: pd.DataFrame, entry: pd.Timestamp, window: int = 200) -> bool:
    """SPY's last close before `entry` above its `window`-session average."""
    past = spy_tr.loc[spy_tr.index < entry, "close"]
    return len(past) >= window and float(past.iloc[-1]) > float(past.iloc[-window:].mean())


def momentum_percentile(prices_tr: dict, symbol: str, entry: pd.Timestamp) -> Optional[float]:
    """Where `symbol`'s 12-1 momentum ranks in the universe at `entry`, 0..1
    (1 = strongest), using only prices before `entry`."""
    mine = momentum_score(prices_tr[symbol], entry)
    if mine is None:
        return None
    others = [s for s in (momentum_score(px, entry) for px in prices_tr.values()) if s is not None]
    return sum(o <= mine for o in others) / len(others)


def ranked_monthly_trials(prices_tr: dict, spy_tr: pd.DataFrame, *, rank: str, top_n: int,
                          start: date, end: date, horizon_days: int,
                          require_uptrend: bool = False) -> list[dict]:
    """Monthly portfolio on the first session of each month: `rank="momentum"`
    buys the strongest 12-1 names, `rank="low_vol"` the calmest. With
    `require_uptrend`, a month is skipped unless SPY sits above its 200-day
    average going in."""
    months = spy_tr.index[(spy_tr.index >= pd.Timestamp(start)) & (spy_tr.index <= pd.Timestamp(end))]
    firsts = pd.Series(months, index=months).groupby([months.year, months.month]).first()
    out = []
    for entry in firsts:
        if require_uptrend and not market_above_trend(spy_tr, entry):
            continue
        scored = []
        for sym, px in prices_tr.items():
            if entry not in px.index:
                continue
            s = momentum_score(px, entry) if rank == "momentum" else volatility_score(px, entry)
            if s is not None:
                scored.append((s, sym))
        picks = sorted(scored, reverse=(rank == "momentum"))[:top_n]
        for s, sym in picks:
            ex = exit_after(prices_tr[sym].index, entry, horizon_days)
            if ex is not None:
                out.append({"symbol": sym, "entry_session": entry, "exit_session": ex,
                            "signal": {rank: round(s, 4)}})
    return out


def with_momentum_filter(trials: list[dict], prices_tr: dict, *, min_percentile: float) -> list[dict]:
    """Keep only trials whose symbol ranks at or above `min_percentile` on
    12-1 momentum at its own entry session."""
    out = []
    for t in trials:
        p = momentum_percentile(prices_tr, t["symbol"], t["entry_session"])
        if p is not None and p >= min_percentile:
            out.append(dict(t, signal=dict(t["signal"], momentum_percentile=round(p, 3))))
    return out
