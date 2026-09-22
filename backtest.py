"""backtest — the five-condition gate replayed against history that already
happened (EVIDENCE_ACCELERATION_PLAN.md, Phase 0 and section 6a).

Why this exists: `evidence.py`'s pre-registered claim needs ~891 closed trades,
and the live gate clears about one idea a month -- a verdict decades out. A
forward-only system cannot fix that; the 21-day horizon alone is a floor on
time. History can: "time to mature" becomes a lookup, not a wait.

The rule every function here follows: **a decision on date D sees only what
existed before D.** A backtest that peeks is worse than no backtest -- it
manufactures exactly the edge this project is trying to test for. Each
look-ahead guard below names the item of the plan's section 8 checklist it
enforces, and `test_backtest.py` proves each one by handing in future data and
requiring that nothing changes.

Nothing here re-derives the live gate. Conditions 3-5 call the same
`quantcore`/`washsale` functions the morning run calls; news goes through the
same `research` parsers (and their cross-wiring guard). What is new is only
what the live run does by judgment: the catalyst calendar as of a past date,
and a mechanical stand-in for `two_sources` that MUST be validated against
real LLM judgment (plan section 6b) before its output counts as evidence.

This module places nothing and reads no broker. It is pure functions over
data the caller already fetched.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

import pandas as pd

import evidence
import quantcore as q
import washsale

# The gate's own names for the conditions evaluated from prices, in the live
# order (runlog.GATE_CONDITIONS). A failure is reported as the FIRST of these
# that fails, the same "how far did it get" semantics closest_calls ranks by.
PRICE_CONDITIONS = ("invalidation_level", "risk_sized", "no_blocking_conflict")

DEFAULT_HORIZON_DAYS = 21
DEFAULT_MIN_RELEVANCE = 0.3          # plan 6a: a starting guess, tuned by 6b
SAME_STORY_OVERLAP = 0.6             # plan 6a: token overlap = one story


# --------------------------------------------------------------------------
# condition 1: the catalyst calendar, as it stood
# --------------------------------------------------------------------------

def historical_catalyst_windows(earnings: dict, *,
                                horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[dict]:
    """One row per quarterly report in an Alpha Vantage `EARNINGS` response:
    the date the report first entered the horizon (`decision_date`), the
    report's own date, and whether it lands pre- or post-market. Oldest first.

    The live gate evaluates an idea every day it sits in the researched set;
    a backtest collapses that to the single moment the catalyst first
    qualifies, so one report is one trial, never 21 correlated ones.

    Look-ahead item 4: `reportedEPS`, `estimatedEPS` and `surprise*` are known
    only AFTER the report, so they are not copied onto the row at all -- a
    decision made before the report cannot use what is not there.

    Raises on a payload with no quarterly history rather than returning `[]`:
    a symbol quietly contributing zero trials is survivorship bias."""
    rows = earnings.get("quarterlyEarnings") if isinstance(earnings, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("EARNINGS payload has no quarterlyEarnings; refusing to "
                         "treat the symbol as having no catalysts")
    symbol = str(earnings.get("symbol", "")).upper()
    out = []
    for row in rows:
        try:
            reported = date.fromisoformat(str(row.get("reportedDate", ""))[:10])
        except ValueError:
            continue
        out.append({
            "symbol": symbol,
            "fiscal_date_ending": row.get("fiscalDateEnding"),
            "report_date": reported,
            "report_time": row.get("reportTime"),
            "decision_date": reported - timedelta(days=horizon_days),
        })
    out.sort(key=lambda r: r["report_date"])
    return out


# --------------------------------------------------------------------------
# condition 2: a mechanical proxy for "two independent sources"
# --------------------------------------------------------------------------

def _published(item) -> Optional[date]:
    """Publication date from a parsed news item's `detail`: Alpha Vantage's
    `20260904T070807` or Robinhood's ISO `2026-09-03T08:10:06-04:00`."""
    raw = (item.detail or "").strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _tokens(title: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", title.lower()).split())


def _same_story(a: str, b: str, threshold: float = SAME_STORY_OVERLAP) -> bool:
    """Overlap coefficient of normalized title tokens. The smaller title's
    tokens are the denominator, so a feed that appends a suffix to a wire
    headline still reads as the same story."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) > threshold


def two_sources_proxy(items: Iterable, *, symbol: str, asof: date,
                      lookback_days: int = DEFAULT_HORIZON_DAYS,
                      min_relevance: float = DEFAULT_MIN_RELEVANCE) -> dict:
    """Mechanical stand-in for the live `two_sources` judgment, over items
    already parsed by `research.news_items_from_alpha_vantage` /
    `news_items_from_robinhood` (so their cross-wiring guard still applies).

    An item qualifies when it is usable, attached to `symbol`, published in
    `[asof - lookback_days, asof)`, and -- where the feed scores it (Alpha
    Vantage) -- has `relevance_score >= min_relevance`. Robinhood carries no
    relevance score; its articles are fetched per symbol, so they are taken
    as relevant.

    Look-ahead item 1: `asof` is exclusive. The live run decides before the
    open, so an article dated the decision day itself may not have existed
    yet; the conservative reading drops it.

    A source counts only if it carries at least one story no source counted
    before it carries: two feeds syndicating one wire headline are one source.

    THIS IS AN APPROXIMATION of the live judgment, not the judgment itself.
    Plan section 6b: measure its agreement with real LLM verdicts, and its
    false-clear rate especially, before trusting a population built on it."""
    sym = symbol.upper()
    start = asof - timedelta(days=lookback_days)
    by_source: dict[str, list[str]] = {}
    for it in items:
        if it.symbol != sym or not it.channel.startswith("news") or not it.usable:
            continue
        published = _published(it)
        if published is None or not (start <= published < asof):
            continue
        value = it.value if isinstance(it.value, dict) else {}
        relevance = value.get("relevance_score")
        if relevance is not None and float(relevance) < min_relevance:
            continue
        by_source.setdefault(it.source, []).append(str(value.get("title", "")))

    counted: list[str] = []
    seen: list[str] = []
    for source in sorted(by_source):
        titles = by_source[source]
        if any(not any(_same_story(t, s) for s in seen) for t in titles):
            counted.append(source)
        seen.extend(titles)

    return {
        "distinct_sources": len(counted),
        "cleared": len(counted) >= 2,
        "counted_sources": counted,
        "qualifying": by_source,
        "asof": asof.isoformat(),
        "lookback_days": lookback_days,
        "min_relevance": min_relevance,
        "method": "proxy_v1",
    }


# --------------------------------------------------------------------------
# conditions 3-5, from prices as they stood
# --------------------------------------------------------------------------

def evaluate_price_conditions(symbol: str, prices: pd.DataFrame, *,
                              decision_date: date, account_equity: float,
                              registry: washsale.Registry,
                              buying_power: Optional[float] = None,
                              vol_window: int = 60) -> dict:
    """Invalidation level, risk sizing and blocking conflicts for one symbol
    on one past date, by the live gate's own functions.

    The decision is taken pre-market on the first session on or after
    `decision_date` (a weekend date rolls forward, as the live run would).
    Look-ahead item 2: only bars strictly BEFORE that session are read --
    the stop and the size come from the prior close, exactly what the 06:20
    run can see -- and the fill is that session's open, the first price an
    order placed before the bell could actually get.

    Look-ahead item 6: `registry` must hold only the backtest's OWN earlier
    simulated trades, never the live registry.

    `account_equity` is whatever the live sizing would see: pass the real
    account's size to reproduce what it could afford (plan section 7 and the
    disclosure that goes with it)."""
    result = {"symbol": symbol.upper(), "decision_date": decision_date.isoformat(),
              "session": None, "gate_failed": None, "detail": "", "sized_at": None,
              "entry": None, "stop_price": None, "shares": None, "risk_dollars": None,
              "plan": None, "anomalies": []}
    sessions = prices.index[prices.index >= pd.Timestamp(decision_date)]
    if len(sessions) == 0:
        result.update(gate_failed="invalidation_level", detail="no session on or after decision date")
        return result
    session = sessions[0]
    result["session"] = session.date().isoformat()
    result["entry"] = float(prices.loc[session, "open"])
    cut = point_in_time_frame(prices, asof=session)
    if cut.empty:
        result.update(gate_failed="invalidation_level", detail="no prices before the decision session")
        return result

    sized_at = float(cut["close"].iloc[-1])
    result["sized_at"] = sized_at

    try:
        vol, _ = q.consensus_volatility(cut, window=vol_window)
        atr = q.average_true_range(cut)
        plan = q.stop_plan(sized_at, vol, atr)
    except ValueError as e:
        result.update(gate_failed="invalidation_level", detail=str(e))
        return result
    result["stop_price"] = float(plan.stop_price)
    result["plan"] = plan.to_dict()

    size = q.size_position(account_equity, sized_at, plan, buying_power=buying_power)
    result["shares"] = float(size.shares)
    result["risk_dollars"] = float(size.risk_dollars)
    if size.shares < 1 or not size.whole_share_ok:
        result.update(gate_failed="risk_sized", detail=size.reason)
        return result

    anoms = q.detect_anomalies(symbol, cut, asof=session)
    result["anomalies"] = sorted(f"{a.severity}:{a.code}" for a in anoms)
    if q.blocking(anoms):
        result.update(gate_failed="no_blocking_conflict", detail="blocking data anomaly")
        return result
    verdict = registry.check_buy(symbol, session.date())
    if not verdict.allowed:
        result.update(gate_failed="no_blocking_conflict", detail=verdict.reason)
        return result
    return result


# --------------------------------------------------------------------------
# prices as they stood: splits and dividends
# --------------------------------------------------------------------------

_OHLC = ["open", "high", "low", "close"]


def point_in_time_frame(raw: pd.DataFrame, *, asof: pd.Timestamp) -> pd.DataFrame:
    """Bars strictly before `asof`, adjusted only for splits that had already
    happened by then -- what a pre-market decision on `asof` could see.

    Alpha Vantage's `adjusted_close` is back-adjusted with EVERY later split
    and dividend, which silently rewrites old price levels: NVDA at ~$1,000
    before its 10:1 split reads as ~$100, a share a $1,000 account "could have
    bought" but could not. Here the latest bar is always a real traded price,
    and earlier bars are rescaled only by splits on or before it, so returns
    and volatility are continuous and the price level is the true one."""
    cut = raw.loc[raw.index < asof].copy()
    if cut.empty or "split_coefficient" not in cut:
        return cut
    coef = cut["split_coefficient"].astype(float).where(lambda c: c > 0, 1.0)
    # factor for a row = product of split coefficients strictly after it
    after = coef[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    cut[_OHLC] = cut[_OHLC].div(after, axis=0)
    cut["volume"] = cut["volume"] * after
    return cut


def total_return_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """OHLC scaled by `adjusted_close / close`: split- and dividend-adjusted.
    Its LEVELS are hindsight, but its RATIOS between any two dates are exact
    total returns -- which is all `settle_window` reads from it."""
    out = raw.copy()
    ratio = raw["adjusted_close"].astype(float) / raw["close"].astype(float)
    out[_OHLC] = raw[_OHLC].mul(ratio, axis=0)
    return out


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def settle_window(symbol: str, prices: pd.DataFrame, benchmark: pd.DataFrame, *,
                  opened: date, entry: float,
                  horizon_days: int = DEFAULT_HORIZON_DAYS,
                  cost_pct: float = 0.10) -> Optional[evidence.Outcome]:
    """Score one cleared window with `evidence.settle`, exactly as a live
    thesis is scored: exit at the first session on or after
    `opened + horizon_days`, benchmark over the same dates. `None` when that
    session is not in the data yet -- an unmatured trial is not a loss."""
    due = pd.Timestamp(opened + timedelta(days=horizon_days))
    later = prices.index[prices.index >= due]
    if len(later) == 0 or later[0] not in benchmark.index:
        return None
    exit_ts = later[0]
    bench_cut = benchmark.loc[:pd.Timestamp(opened)]
    if bench_cut.empty:
        return None
    thesis = {"thesis_id": f"bt-{symbol.upper()}-{opened.isoformat()}", "symbol": symbol.upper(),
              "opened": opened, "entry": entry,
              "benchmark_entry": float(bench_cut["close"].iloc[-1])}
    return evidence.settle(thesis, exit_price=float(prices.loc[exit_ts, "close"]),
                           benchmark_exit=float(benchmark.loc[exit_ts, "close"]),
                           closed=exit_ts.date(), cost_pct=cost_pct)


# --------------------------------------------------------------------------
# the universe, fixed before any result exists (look-ahead item 3)
# --------------------------------------------------------------------------

MAIN_EXCHANGES = ("NYSE", "NASDAQ")
# Shells and derivative listings, by name. None is an operating company whose
# earnings are a catalyst: a SPAC reports no business, a warrant or unit is a
# claim on something else.
_NOT_AN_OPERATING_COMPANY = re.compile(
    r"\b(?:acquisition|warrants?|units?|rights?|trust|fund|etf|etn|notes?|"
    r"preferred|depositary)\b", re.IGNORECASE)


def eligible_listing(listing: pd.DataFrame) -> list[str]:
    """Operating-company common stocks on NYSE/NASDAQ from an Alpha Vantage
    `LISTING_STATUS` snapshot, sorted. Taken from a snapshot AS OF a past date,
    so names delisted since are still in it -- the survivorship bias a
    universe built from today's listings would carry is exactly what this
    avoids."""
    df = listing[(listing["assetType"] == "Stock") & listing["exchange"].isin(MAIN_EXCHANGES)]
    df = df[~df["symbol"].astype(str).str.contains(r"[-./^]", regex=True)]
    df = df[~df["name"].astype(str).str.contains(_NOT_AN_OPERATING_COMPANY)]
    return sorted(df["symbol"].astype(str))


def sample_universe(listing: pd.DataFrame, *, n: int, seed: int) -> list[str]:
    """A seeded random draw of `n` names from `eligible_listing`. The seed and
    `n` are the pre-commitment: written down before any result is seen, so the
    universe cannot drift toward names that happened to work."""
    pool = eligible_listing(listing)
    if n >= len(pool):
        return pool
    return sorted(pd.Series(pool).sample(n=n, random_state=seed).tolist())


# --------------------------------------------------------------------------
# news by publisher, for history
# --------------------------------------------------------------------------

def av_publisher_items(raw: Optional[dict], *, symbol: str, asof: str) -> list:
    """Alpha Vantage `NEWS_SENTIMENT` items with each article's own publisher
    as its source (`"Alpha Vantage NEWS_SENTIMENT:<publisher>"`).

    Why: Robinhood's news cannot be queried as of a past date, so a backtest
    has one feed, and "two feeds" can never clear. Two different publishers
    inside that one feed is the nearest honest reading of "two independent
    sources" -- a DIFFERENT definition from the live run's, which is one more
    reason plan section 6b's calibration has to pass before this counts.

    Parsed by `research.news_items_from_alpha_vantage` first, so its
    cross-wiring refusal (most articles not naming `symbol`) still applies."""
    parsed = __import__("research").news_items_from_alpha_vantage(raw, symbol=symbol, asof=asof)
    if len(parsed) == 1 and parsed[0].quality == "failed":
        return parsed
    publisher = {(e.get("title", ""), str(e.get("time_published", ""))): e.get("source") or "unknown"
                 for e in (raw or {}).get("feed", [])}
    out = []
    for it in parsed:
        pub = publisher.get((it.value.get("title", ""), it.detail), "unknown")
        out.append(type(it)(channel=it.channel, symbol=it.symbol, mechanism=it.mechanism,
                            value=dict(it.value, publisher=pub),
                            source=f"{it.source}:{pub}", asof=it.asof,
                            quality=it.quality, detail=it.detail))
    return out


# --------------------------------------------------------------------------
# the portfolio: a $100k book, and the $1,000 account it stands in for
# --------------------------------------------------------------------------

def simulate(trials: Sequence[dict], prices: dict, benchmark: pd.DataFrame, *,
             starting_cash: float, sizing_equity: Optional[float] = None,
             horizon_days: int = DEFAULT_HORIZON_DAYS, cost_pct: float = 0.10,
             risk_budget_fraction: float = 0.02, max_weight: float = 0.18) -> dict:
    """Walk cleared trials through a cash account day by day, in calendar order.

    Each trial (from `evaluate_price_conditions`): `symbol`, `session`,
    `sized_at` (prior close), `entry` (session open), `plan` (a StopPlan dict).

    Two uses, one function:
    - **The $1,000 replay** (`starting_cash=1000`, no `sizing_equity`): what
      the real account would have lived through -- sized off its current
      equity, limited to settled cash, so ideas are skipped when money is tied
      up. This is the dollar answer.
    - **The evidence book** (`starting_cash=100_000, sizing_equity=1000`):
      every position sized exactly as a fresh $1,000 account would size it
      (same whole-share rounding, same 18% cap, same unaffordable names), but
      cash never runs out, so the sample measures the SIGNAL rather than this
      account's funding (plan section 7). Its dollars are $1,000-account
      dollars per trade; its total is NOT what one $1,000 account could earn,
      because it holds more positions at once than $1,000 can fund.

    Exits, as the live system trades: a stop resting good-for-day each
    session -- filled at the stop, or at the open if the day gaps through it --
    otherwise the close of the first session on or after `opened +
    horizon_days`. Stops cannot fill overnight, which is why a gap exits at
    the open. Sale proceeds settle next session (T+1). A loss exit blocks
    re-buying that name for 30 days (`washsale.Registry`, seeded only from
    this simulation's own trades). `cost_pct` (round trip, percent of
    notional) is charged on every trade.

    `prices` are RAW traded prices (with `split_coefficient` and
    `dividend_amount` when available): a split during a hold rescales shares,
    entry and stop as the broker would, and a dividend is paid in cash to a
    position held into its ex-date.

    Not modelled, and to be disclosed with any result: sector caps and the
    cash floor beyond settled cash, partial fills, and slippage past a stop
    beyond `cost_pct`."""
    by_session: dict = {}
    for t in trials:
        by_session.setdefault(pd.Timestamp(t["session"]), []).append(t)
    days = benchmark.index[benchmark.index >= min(by_session)] if by_session else benchmark.index[:0]

    cash, pending = float(starting_cash), []          # pending: (available_on, amount)
    positions: dict[str, dict] = {}
    registry = washsale.Registry()
    trades, skipped, equity_curve = [], [], []
    last_close: dict[str, float] = {}

    def _close(sym, pos, day, price, reason):
        nonlocal pending
        proceeds = pos["shares"] * price
        cost = (pos["shares"] * pos["entry"] + proceeds) / 2 * cost_pct / 100.0
        pnl = proceeds - pos["shares"] * pos["entry"] - cost
        nxt = benchmark.index[benchmark.index > day]
        pending.append((nxt[0] if len(nxt) else day, proceeds - cost))
        registry.add(washsale.Trade(sym, "agentic", day.date(), "sell", pos["shares"],
                                    realized_pnl=round(pnl, 6)))
        bench_ret = (float(benchmark.loc[day, "close"]) / pos["bench_entry"] - 1) * 100
        ret = (price / pos["entry"] - 1) * 100
        trades.append({"symbol": sym, "opened": pos["opened"].date().isoformat(),
                       "closed": day.date().isoformat(), "entry": pos["entry"], "exit": price,
                       "shares": pos["shares"], "pnl": round(pnl, 2), "exit_reason": reason,
                       "return_pct": round(ret, 4), "benchmark_return_pct": round(bench_ret, 4),
                       "excess_pct": round(ret - bench_ret - cost_pct, 4)})
        del positions[sym]

    dividends = 0.0
    for day in days:
        cash += sum(a for d, a in pending if d <= day)
        pending = [(d, a) for d, a in pending if d > day]

        # Corporate actions on held names, before anything trades: a split
        # rescales the position the way the broker does; a dividend is paid
        # to a position held into its ex-date (not one bought that morning).
        for sym, pos in positions.items():
            px = prices[sym]
            if day not in px.index:
                continue
            coef = float(px.loc[day].get("split_coefficient", 1.0) or 1.0)
            if coef > 0 and coef != 1.0:
                pos["shares"] *= coef
                pos["entry"] /= coef
                pos["stop"] /= coef
                last_close[sym] = last_close.get(sym, pos["entry"] * coef) / coef
            div = float(px.loc[day].get("dividend_amount", 0.0) or 0.0)
            if div > 0 and pos["opened"] < day:
                pending.append((day, pos["shares"] * div))
                dividends += pos["shares"] * div

        equity_open = cash + sum(a for _, a in pending) + sum(
            p["shares"] * last_close.get(s, p["entry"]) for s, p in positions.items())
        for t in sorted(by_session.get(day, []), key=lambda t: t["symbol"]):
            sym = t["symbol"].upper()
            if sym in positions:
                skipped.append({"symbol": sym, "session": t["session"], "reason": "already_held"})
                continue
            if not registry.check_buy(sym, day.date()).allowed:
                skipped.append({"symbol": sym, "session": t["session"], "reason": "wash_sale"})
                continue
            plan = q.StopPlan(**t["plan"])
            size = q.size_position(sizing_equity or equity_open, t["sized_at"], plan,
                                   risk_budget_fraction=risk_budget_fraction,
                                   max_weight=max_weight,
                                   buying_power=cash if sizing_equity is None else None)
            shares = min(int(size.shares), int(cash // t["entry"]))   # an open above the prior close
            if shares < 1:
                reason = "cash" if size.shares >= 1 or size.cash_limited else "unaffordable"
                skipped.append({"symbol": sym, "session": t["session"], "reason": reason})
                continue
            cash -= shares * t["entry"]
            registry.add(washsale.Trade(sym, "agentic", day.date(), "buy", shares))
            due = prices[sym].index[prices[sym].index >= day + pd.Timedelta(days=horizon_days)]
            positions[sym] = {"shares": shares, "entry": t["entry"], "stop": plan.stop_price,
                              "opened": day, "due": due[0] if len(due) else None,
                              "bench_entry": float(benchmark.loc[day, "open"])}

        for sym in list(positions):
            px = prices[sym]
            if day not in px.index:
                continue
            bar, pos = px.loc[day], positions[sym]
            if float(bar["low"]) <= pos["stop"]:
                _close(sym, pos, day, min(float(bar["open"]), pos["stop"]), "stop")
            elif pos["due"] is not None and day >= pos["due"]:
                _close(sym, pos, day, float(bar["close"]), "horizon")
            else:
                last_close[sym] = float(bar["close"])

        equity_curve.append((day, cash + sum(a for _, a in pending) + sum(
            p["shares"] * last_close.get(s, p["entry"]) for s, p in positions.items())))

    final = equity_curve[-1][1] if equity_curve else float(starting_cash)
    peak, max_dd = float(starting_cash), 0.0
    for _, e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    bench_ret = ((float(benchmark.loc[days[-1], "close"]) / float(benchmark.loc[days[0], "open"]) - 1) * 100
                 if len(days) else 0.0)
    return {
        "starting_cash": starting_cash, "sizing_equity": sizing_equity,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / starting_cash - 1) * 100, 4),
        "benchmark_return_pct": round(bench_ret, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "dividends": round(dividends, 2),
        "trades": trades, "skipped": skipped,
        "open_positions": sorted(positions),
        "equity_curve": [(d.date().isoformat(), round(e, 2)) for d, e in equity_curve],
    }


# --------------------------------------------------------------------------
# calibrating the two_sources proxy against real judgment (plan section 6b)
# --------------------------------------------------------------------------

def density_bucket(articles: int) -> str:
    """How much news a window holds -- the archive thins out going back in
    time, so agreement has to be measured across densities, not on the
    easy, news-rich recent windows alone."""
    if articles <= 0:
        return "none"
    if articles <= 5:
        return "sparse"
    if articles <= 30:
        return "moderate"
    return "dense"


def calibration_sample(windows: Sequence[dict], *, n: int, seed: int) -> list[dict]:
    """A seeded draw of `n` windows spread across (year, news density)
    strata, round-robin, so each stratum is represented before any repeats.
    Windows with no articles are left out: there is nothing to judge, and
    both the proxy and a reader reject them trivially."""
    strata: dict = {}
    for w in windows:
        if w.get("articles", 0) <= 0:
            continue
        strata.setdefault((str(w["decision_date"])[:4], density_bucket(w["articles"])), []).append(w)
    shuffled = {k: pd.Series(range(len(v))).sample(frac=1, random_state=seed).tolist()
                for k, v in strata.items()}
    out, round_ = [], 0
    keys = sorted(strata)
    while len(out) < n and any(round_ < len(shuffled[k]) for k in keys):
        for k in keys:
            if round_ < len(shuffled[k]) and len(out) < n:
                out.append(strata[k][shuffled[k][round_]])
        round_ += 1
    return out


def judge_packet(raw: Optional[dict], *, symbol: str, decision: date,
                 lookback_days: int = DEFAULT_HORIZON_DAYS, summary_chars: int = 400) -> dict:
    """What a judge sees for one window: the articles about `symbol`
    published in the lookback strictly before `decision` -- title, publisher,
    date, the feed's relevance score and a trimmed summary. No prices, no
    outcome, no proxy verdict: the judgment has to be made blind to all
    three, or the calibration measures agreement with hindsight."""
    sym, lo = symbol.upper(), decision - timedelta(days=lookback_days)
    arts = []
    for e in (raw or {}).get("feed", []):
        match = next((t for t in e.get("ticker_sentiment", ())
                      if str(t.get("ticker", "")).upper() == sym), None)
        try:
            published = datetime.strptime(str(e.get("time_published", ""))[:15], "%Y%m%dT%H%M%S").date()
        except ValueError:
            continue
        if match is None or not (lo <= published < decision):
            continue
        arts.append({"title": e.get("title", ""), "publisher": e.get("source", "unknown"),
                     "published": published.isoformat(),
                     "relevance_to_symbol": match.get("relevance_score"),
                     "summary": str(e.get("summary", ""))[:summary_chars]})
    arts.sort(key=lambda a: a["published"], reverse=True)
    return {"symbol": sym, "decision_date": decision.isoformat(), "articles": arts}


def agreement(proxy: dict, judge: dict) -> dict:
    """Agreement between proxy and judge verdicts over the windows both
    judged (`{window_id: cleared}`). `false_clear_rate` -- of the windows the
    proxy CLEARED, the share the judge rejected -- is reported on its own:
    it is the direction that inflates a backtest's apparent edge."""
    keys = sorted(set(proxy) & set(judge))
    agree = sum(proxy[k] == judge[k] for k in keys)
    p_clear = [k for k in keys if proxy[k]]
    p_reject = [k for k in keys if not proxy[k]]
    return {
        "n": len(keys),
        "agreement": agree / len(keys) if keys else float("nan"),
        "false_clear_rate": (sum(not judge[k] for k in p_clear) / len(p_clear)) if p_clear else float("nan"),
        "false_reject_rate": (sum(judge[k] for k in p_reject) / len(p_reject)) if p_reject else float("nan"),
        "disagreements": [k for k in keys if proxy[k] != judge[k]],
    }
