"""Persistent state, rebuilt from evidence rather than remembered.

The first two live runs exposed that this system had no memory at all. The
storage connector can create a file and rename it but cannot rewrite its
contents, so `state.json` was read every morning and never written. The journal
stayed empty, and the preflight reconciliation compared an empty journal against
two dozen real broker positions -- a check that could only ever pass by
exemption or abort by default.

The fix is to stop trying to remember two different things the same way.

**Positions and trades are rebuilt from the broker every run.** The broker's
order history is the authoritative record of what happened; a local copy of it
can only ever be a stale duplicate that drifts. Rebuilding makes drift
structurally impossible rather than merely detected, and reconciliation becomes
a real integrity check -- do the positions follow from the fills? -- instead of
a comparison against a file we hope is current.

**Theses, gate decisions, and run records go in an append-only journal**, one
dated file per run, folded together on read. The broker cannot know why a trade
was made, what would have invalidated it, or what was deliberately not done.
That is the only state worth persisting, and appending never needs a rewrite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "Fill", "fills_from_orders", "SplitEvent", "splits_from_api", "apply_splits",
    "positions_from_fills", "cost_basis", "loss_sales", "reconcile_positions",
    "to_washsale_trades", "JournalEntry", "Journal", "fold_journal",
    "journal_filename", "JOURNAL_RE", "run_entry", "RUN_ENTRY_SCHEMA_FIELDS",
    "journal_monthly_filename", "MONTHLY_JOURNAL_RE", "month_is_compactable",
    "compact_journal_month",
    "FILLS_CACHE_HORIZON_DAYS", "fills_cache_filename", "FILLS_CACHE_RE",
    "fold_fills_cache", "fills_cache_watermark", "fills_ready_to_cache",
    "SPLITS_CACHE_HORIZON_DAYS", "splits_cache_filename", "SPLITS_CACHE_RE",
    "SplitsCacheEntry", "fold_splits_cache", "symbols_needing_split_check",
]

# Quantities are compared with a tolerance because broker payloads carry six
# decimal places and round-tripping through JSON and float arithmetic does not
# preserve the last digit. 1e-6 -- the previous tolerance -- is exactly the
# magnitude of that noise, so it produced false drift on positions that agreed.
QTY_TOL = 1e-4


# --------------------------------------------------------------------------
# broker-derived state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    """One executed trade, as the broker reports it."""
    symbol: str
    side: str                   # buy | sell
    quantity: float
    price: float
    on: date
    order_id: str = ""

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        txt = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(txt).date()
        except ValueError:
            try:
                return date.fromisoformat(txt[:10])
            except ValueError:
                return None
    return None


def fills_from_orders(orders: Iterable[dict]) -> list[Fill]:
    """Extract actual executions from broker order payloads.

    The quantity used is `cumulative_quantity`, never `quantity`. An order's
    requested size is an intention; a position is built from what actually
    executed. Conflating the two is how a cancelled or rejected order becomes
    a phantom holding -- but the reverse mistake is just as real: allow-listing
    a fixed set of terminal states (`filled`, `partially_filled`) silently
    discarded a genuine execution on 1 September 2026, because the broker's
    actual terminal state for a partial fill whose remainder got cancelled is
    `partially_filled_rest_cancelled`, a state that was never on the list. The
    order still filled 1.0 share at $33.00 -- `cumulative_quantity` already
    says so authoritatively regardless of what the label on the rest of the
    order is. Any order with a positive `cumulative_quantity` contributed a
    real fill; the state name describes what happened to the UNFILLED
    remainder, which is not this function's concern.
    """
    out: list[Fill] = []
    for o in orders:
        qty = float(o.get("cumulative_quantity") or 0.0)
        if qty <= 0:
            continue

        price = o.get("average_price")
        if price in (None, ""):
            price = o.get("price")
        if price in (None, ""):
            continue

        when = _as_date(o.get("last_transaction_at")) or _as_date(o.get("created_at"))
        if when is None:
            continue

        out.append(Fill(
            symbol=str(o.get("symbol", "")).upper(),
            side=str(o.get("side", "")).lower(),
            quantity=qty,
            price=float(price),
            on=when,
            order_id=str(o.get("id", "")),
        ))
    out.sort(key=lambda f: (f.on, f.order_id))
    return out


# --------------------------------------------------------------------------
# split adjustment
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SplitEvent:
    """One corporate split. `ratio` is post-split shares per pre-split share:
    4.0 for a 4-for-1 forward split, 0.1 for a 1-for-10 reverse split. This is
    the convention Alpha Vantage's SPLITS endpoint uses for `split_factor`."""
    symbol: str
    effective_date: date
    ratio: float

    def __post_init__(self):
        if self.ratio <= 0:
            raise ValueError(f"split ratio must be positive, got {self.ratio}")


def splits_from_api(symbol: str, records: Iterable[dict]) -> list[SplitEvent]:
    """Build SplitEvent objects from Alpha Vantage SPLITS response records.

    Each record is expected to carry `effective_date` (YYYY-MM-DD) and
    `split_factor` (e.g. "4" for 4-for-1, "0.1" for 1-for-10). Malformed
    records are skipped rather than raising, since one bad record from an
    external API should not take down reconciliation for every symbol.
    """
    out = []
    for r in records:
        d = _as_date(r.get("effective_date"))
        try:
            ratio = float(r.get("split_factor"))
        except (TypeError, ValueError):
            ratio = None
        if d is not None and ratio is not None and ratio > 0:
            out.append(SplitEvent(symbol, d, ratio))
    return out


def apply_splits(fills: Sequence[Fill],
                 splits: dict[str, Sequence[SplitEvent]]) -> list[Fill]:
    """Re-express every historical fill in CURRENT, post-split share terms.

    Fills are reported by the broker as executed at the time: a share bought
    the day before a 4-for-1 split is one pre-split share, not the four
    post-split shares it became. Summing those raw quantities against a
    broker position snapshot taken today -- which is always in current,
    post-split terms -- makes a symbol that has ever split look like a
    reconciliation failure no matter how correct the trading was. This is
    exactly what happened on 31 August 2026: NVDA, CMG, NFLX, VUG, CRWD, and
    five other symbols in a real account all disagreed by amounts that
    matched known split ratios once investigated, and nothing had been
    wrong with a single trade.

    For a fill dated strictly before a split's effective date, quantity is
    multiplied by the ratio and price is divided by it, so the fill's
    notional value is unchanged -- only the share-count convention shifts to
    match today's. A fill on or after the effective date is already quoted in
    post-split terms and is left alone. Multiple splits on the same symbol
    compound correctly regardless of the order `splits` lists them in,
    because each is applied only to fills strictly before ITS OWN date, and
    they are processed earliest-first so a fill before two splits picks up
    both factors.

    Symbols with no entry in `splits` pass through completely unchanged --
    this is a deliberate no-op, not a silent skip, so a caller that forgot to
    look up a symbol's splits gets a reconciliation failure loud enough to
    notice rather than a quietly wrong number.
    """
    out = list(fills)
    for symbol, events in splits.items():
        for ev in sorted(events, key=lambda e: e.effective_date):
            out = [
                Fill(f.symbol, f.side, f.quantity * ev.ratio, f.price / ev.ratio,
                    f.on, f.order_id)
                if f.symbol == symbol and f.on < ev.effective_date else f
                for f in out
            ]
    return out


# --------------------------------------------------------------------------
# caching fills and split events -- never positions
# --------------------------------------------------------------------------
#
# Section 5's storage rule stays exactly as it is: positions and the
# wash-sale registry are never stored, only rebuilt from fills every run.
# What was actually expensive was pulling the ENTIRE order history with no
# `created_at_gte` every single morning, and calling `SPLITS` for every
# symbol every single run, even though a fill from a year ago and a split
# check from last week are both already-settled facts about the past that
# do not need re-fetching from the broker to be trusted again today.
#
# The cache below holds Fills and SplitEvents -- append-only dated files,
# the same pattern the journal already uses, because the Drive connector
# can create a file but not rewrite one. It does NOT hold positions, and it
# does not let a run skip fetching the LAST `*_HORIZON_DAYS` of history
# fresh from the broker: a Robinhood order can still be open (unfilled
# remainder, still eligible to fill or be cancelled) for a few days after
# it was created, so trusting a fill as permanent before it has had time to
# reach a terminal state would let a caching optimization quietly corrupt
# the exact reconciliation invariant the redesign in this file exists to
# protect. Only fills strictly OLDER than the horizon are ever written to
# the cache; positions are still rebuilt fresh every run from the full
# cached-plus-fresh fill set, and still reconciled against the live broker
# snapshot every run, exactly as before.

FILLS_CACHE_HORIZON_DAYS = 7
FILLS_CACHE_RE = re.compile(r"^fills-cache-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def fills_cache_filename(run_date: date, seq: int = 0) -> str:
    """One file per run, same convention as `journal_filename` -- the
    connector cannot overwrite, and a second run on the same day gets its
    own sequence number rather than colliding with the first."""
    stem = f"fills-cache-{run_date.isoformat()}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


def fold_fills_cache(files: Iterable[dict]) -> tuple[list[Fill], list[str]]:
    """Fold dated fills-cache files into one deduplicated, sorted `Fill`
    list, plus any files/rows that would not parse.

    `files` are `{"title": str, "content": str}`, same shape as
    `fold_journal`. Deduplicated on `order_id` (falling back to a
    symbol/date/price/quantity key for the rare fill with no id) so a
    caller that accidentally re-caches an already-cached fill does not
    double it into the position count.
    """
    seen: dict[str, Fill] = {}
    bad: list[str] = []
    dated: list[tuple[str, int, Any]] = []

    for f in files:
        title = str(f.get("title", ""))
        if not FILLS_CACHE_RE.match(title):
            continue
        try:
            rows = json.loads(f.get("content") or "[]")
        except (ValueError, TypeError):
            bad.append(title)
            continue
        m = FILLS_CACHE_RE.match(title)
        dated.append((m.group(1), int(m.group(2) or 0), rows))

    dated.sort(key=lambda t: (t[0], t[1]))

    for iso, seq, rows in dated:
        for r in rows:
            try:
                fill = Fill(
                    symbol=str(r["symbol"]).upper(),
                    side=str(r["side"]),
                    quantity=float(r["quantity"]),
                    price=float(r["price"]),
                    on=_as_date(r["on"]),
                    order_id=str(r.get("order_id", "")),
                )
                if fill.on is None:
                    raise ValueError("unparseable date")
            except (KeyError, TypeError, ValueError):
                bad.append(f"{fills_cache_filename(date.fromisoformat(iso), seq)} fill")
                continue
            key = fill.order_id or f"{fill.symbol}:{fill.on}:{fill.price}:{fill.quantity}"
            seen[key] = fill

    out = sorted(seen.values(), key=lambda f: (f.on, f.order_id))
    return out, bad


def fills_cache_watermark(cached_fills: Sequence[Fill]) -> Optional[date]:
    """The earliest date the next broker fetch needs to cover: the day
    after the newest cached fill, or `None` (fetch full history, exactly
    as every run does today) if nothing is cached yet.

    Fetching from here forward always re-covers the entire mutable horizon
    window fresh, since `fills_ready_to_cache` never lets a fill inside
    that window into the cache in the first place -- there is no gap to
    account for separately here.
    """
    if not cached_fills:
        return None
    return max(f.on for f in cached_fills) + timedelta(days=1)


def fills_ready_to_cache(fresh_fills: Sequence[Fill], *,
                         horizon_days: int = FILLS_CACHE_HORIZON_DAYS,
                         today: Optional[date] = None) -> list[Fill]:
    """Fills old enough to be safely written to the cache: strictly older
    than `horizon_days` ago. Every fill that goes in has therefore been
    re-fetched fresh from the broker at least once after its order had
    `horizon_days` to reach a terminal state -- filled, cancelled, or
    expired -- so caching it can no longer under-count a still-open order.
    Fills inside the window are used for today's rebuild but stay
    uncached until a later run ages them out naturally.
    """
    today = today or date.today()
    boundary = today - timedelta(days=horizon_days)
    return [f for f in fresh_fills if f.on < boundary]


SPLITS_CACHE_HORIZON_DAYS = 7
SPLITS_CACHE_RE = re.compile(r"^splits-cache-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def splits_cache_filename(run_date: date, seq: int = 0) -> str:
    """Same append-only convention as `fills_cache_filename`."""
    stem = f"splits-cache-{run_date.isoformat()}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


@dataclass
class SplitsCacheEntry:
    """What this system knew about one symbol's splits, as of `checked_through`."""
    symbol: str
    checked_through: date
    splits: list[SplitEvent] = field(default_factory=list)


def fold_splits_cache(files: Iterable[dict]) -> tuple[dict[str, SplitsCacheEntry], list[str]]:
    """Fold dated splits-cache files into one entry per symbol, plus any
    files/rows that would not parse.

    Unlike fills, a symbol can appear in many cache files over time as it
    gets periodically rechecked; the entry with the LATEST `checked_through`
    wins per symbol, folded oldest-file-first so a later file's entry for a
    symbol supersedes an earlier one rather than the reverse.
    """
    by_symbol: dict[str, SplitsCacheEntry] = {}
    bad: list[str] = []
    dated: list[tuple[str, int, Any]] = []

    for f in files:
        title = str(f.get("title", ""))
        m = SPLITS_CACHE_RE.match(title)
        if not m:
            continue
        try:
            body = json.loads(f.get("content") or "[]")
        except (ValueError, TypeError):
            bad.append(title)
            continue
        dated.append((m.group(1), int(m.group(2) or 0), body))

    dated.sort(key=lambda t: (t[0], t[1]))

    for iso, seq, body in dated:
        for row in body:
            try:
                sym = str(row["symbol"]).upper()
                checked_through = _as_date(row["checked_through"])
                if checked_through is None:
                    raise ValueError("unparseable checked_through")
                events = [
                    SplitEvent(sym, _as_date(e["effective_date"]), float(e["ratio"]))
                    for e in row.get("splits", [])
                ]
            except (KeyError, TypeError, ValueError):
                bad.append(f"{splits_cache_filename(date.fromisoformat(iso), seq)} entry")
                continue
            existing = by_symbol.get(sym)
            if existing is None or checked_through >= existing.checked_through:
                by_symbol[sym] = SplitsCacheEntry(sym, checked_through, events)

    return by_symbol, bad


def symbols_needing_split_check(symbols: Sequence[str],
                                cache: dict[str, SplitsCacheEntry], *,
                                horizon_days: int = SPLITS_CACHE_HORIZON_DAYS,
                                today: Optional[date] = None) -> list[str]:
    """Which symbols need a fresh `SPLITS` call this run: never checked
    before, or checked more than `horizon_days` ago. Every other symbol
    reuses its cached events. This bounds a real split's detection latency
    to `horizon_days` rather than requiring `SPLITS` to run for every held
    and candidate symbol on every single run regardless of whether
    anything could plausibly have changed since the last check.
    """
    today = today or date.today()
    boundary = today - timedelta(days=horizon_days)
    out = set()
    for sym in symbols:
        entry = cache.get(sym.upper())
        if entry is None or entry.checked_through < boundary:
            out.add(sym.upper())
    return sorted(out)


def positions_from_fills(fills: Sequence[Fill],
                         opening_balances: Optional[dict[str, float]] = None
                         ) -> dict[str, float]:
    """Net position per symbol. Dust below the tolerance is dropped, because a
    residue of 1e-9 shares is a rounding artefact, not a holding.

    Expects SPLIT-ADJUSTED fills (see `apply_splits`) for any symbol that has
    ever split -- a broker's current position snapshot is always in current
    share terms, and comparing it against unadjusted historical fills is the
    reconciliation failure this function cannot see from the inside.

    `opening_balances` covers the two situations fills can never explain:
    shares that arrived outside the order book entirely (a transfer, a
    spin-off distribution, a DRIP conversion with no corresponding buy order),
    and shares bought before the earliest order the broker's API will return.
    Both were found on 1 September 2026 (MBGL and MSFT) and both would
    otherwise fail reconciliation on every single future run, forever, for a
    fact about history that fills can never contain.

    This is deliberately NOT inferred. A missing explanation is reported as a
    residual by `reconcile_positions`, and stays a hard abort, until a human
    has recorded why with a `journal.opening_balance` entry -- an explicit,
    dated, auditable fact, the same shape as the pre-registered evidence claim
    or the gap-risk haircut: a judgment call written down, never a silent
    guess standing in for one.
    """
    pos: dict[str, float] = {}
    for f in fills:
        pos[f.symbol] = pos.get(f.symbol, 0.0) + f.signed_quantity
    for sym, qty in (opening_balances or {}).items():
        pos[sym] = pos.get(sym, 0.0) + qty
    return {s: q for s, q in pos.items() if abs(q) > QTY_TOL}


def cost_basis(fills: Sequence[Fill], symbol: str,
               asof: Optional[date] = None) -> dict:
    """Average cost and the highest price paid for shares still held.

    Two numbers because they answer different questions. Average cost is what
    the position cost overall. The highest price still held is what matters for
    a wash sale: the broker disposes of lots FIFO, so a sale can realise a loss
    on an expensive lot while the average shows a gain. Using the average alone
    would miss exactly those loss sales, and a wash-sale registry that misses a
    loss sale approves the repurchase that disallows it.

    Expects SPLIT-ADJUSTED fills for any symbol that has ever split, same as
    `positions_from_fills`. FIFO lot accounting sums quantities across fills as
    if they share one unit; a pre-split buy and a post-split sell do not, and
    mixing them makes a partially-sold, still-open position look fully closed
    while reporting an average cost off by the split factor -- verified on a
    synthetic NVDA-shaped scenario in the tests.
    """
    sym = symbol.upper()
    lots: list[list[float]] = []          # [quantity, price], FIFO
    for f in fills:
        if f.symbol != sym:
            continue
        if asof is not None and f.on > asof:
            continue
        if f.side == "buy":
            lots.append([f.quantity, f.price])
        else:
            remaining = f.quantity
            while remaining > QTY_TOL and lots:
                take = min(remaining, lots[0][0])
                lots[0][0] -= take
                remaining -= take
                if lots[0][0] <= QTY_TOL:
                    lots.pop(0)

    qty = sum(l[0] for l in lots)
    if qty <= QTY_TOL:
        return {"quantity": 0.0, "average_cost": None, "highest_lot": None}
    return {
        "quantity": qty,
        "average_cost": sum(l[0] * l[1] for l in lots) / qty,
        "highest_lot": max(l[1] for l in lots),
    }


def loss_sales(fills: Sequence[Fill]) -> list[dict]:
    """Every sale that realised a loss on any lot, for the wash-sale registry.

    Deliberately conservative: a sale counts as a loss sale if it is below the
    average cost OR below the highest lot still held at the time. Over-reporting
    a loss sale costs a delayed repurchase; under-reporting it silently
    disallows a deduction the owner believes they have.

    Expects SPLIT-ADJUSTED fills, for the same reason as `cost_basis`, which
    this calls directly.
    """
    out = []
    for i, f in enumerate(fills):
        if f.side != "sell":
            continue
        prior = fills[:i]
        basis = cost_basis(prior, f.symbol, asof=f.on)
        if basis["average_cost"] is None:
            continue
        by_avg = f.price < basis["average_cost"]
        by_lot = f.price < basis["highest_lot"]
        if by_avg or by_lot:
            out.append({
                "symbol": f.symbol,
                "on": f.on,
                "quantity": f.quantity,
                "price": f.price,
                "average_cost": basis["average_cost"],
                "highest_lot": basis["highest_lot"],
                "basis": "average" if by_avg else "fifo_lot",
            })
    return out


def reconcile_positions(broker: dict[str, float],
                        fills: Sequence[Fill],
                        tol: float = QTY_TOL,
                        opening_balances: Optional[dict[str, float]] = None
                        ) -> dict:
    """Do the broker's positions follow from the broker's own fills?

    This replaces comparing the broker against a local file. A local file can be
    stale for a hundred boring reasons; a position that does not follow from the
    executions that produced it is a genuine anomaly -- a transfer, a corporate
    action, a manual trade outside this system, or a bug here.

    `opening_balances` (see `positions_from_fills`) covers only the specific,
    dated, human-recorded facts about history fills cannot explain -- an
    UNEXPLAINED residual must still abort. This parameter is not a way to make
    reconciliation pass; it is a way to stop re-litigating the same already-
    understood gap in the order history every single day.

    Returns the disagreements only. Empty means the two views agree.
    """
    derived = positions_from_fills(fills, opening_balances)
    out: dict[str, dict] = {}
    for sym in set(broker) | set(derived):
        b = float(broker.get(sym, 0.0))
        d = float(derived.get(sym, 0.0))
        if abs(b - d) > tol:
            out[sym] = {
                "broker": b,
                "derived_from_fills": d,
                "difference": b - d,
                "likely": _explain(sym, b, d, fills),
            }
    return out


def _explain(symbol: str, broker_qty: float, derived_qty: float,
             fills: Sequence[Fill]) -> str:
    if not any(f.symbol == symbol for f in fills):
        return ("no fills for this symbol in the window examined — most likely "
                "opened before it, or transferred in")
    if derived_qty == 0 and broker_qty > 0:
        return "fills net to nothing but the broker holds shares — check for a split or transfer"
    if broker_qty == 0 and derived_qty > 0:
        return "fills imply a holding the broker does not show — check for a manual sale"
    return "partial disagreement — check for a corporate action or a trade placed outside this system"


# --------------------------------------------------------------------------
# bridge to the wash-sale registry
# --------------------------------------------------------------------------

def to_washsale_trades(fills: Sequence[Fill], account: str):
    """Turn rebuilt fills into `washsale.Trade` objects, computing realised P&L
    per sell so the registry's `is_loss_sale` has the number it requires.

    This is the piece that made the registry's first live read come up empty:
    a `washsale.Trade` on a sell REQUIRES `realized_pnl`, and nothing had ever
    computed it from broker history. FIFO cost basis, same lot accounting as
    `cost_basis` and `loss_sales` above, so a wash sale detected here and a
    loss sale detected there can never disagree about which lot was consumed.

    Expects SPLIT-ADJUSTED fills for the same reason `cost_basis` does: its
    own FIFO lot matching here would otherwise mix pre- and post-split share
    counts as one unit and compute a realised P&L off by the split factor.
    """
    import washsale as W       # deferred: washsale imports nothing from here,
                               # avoids a cycle since this module is the one
                               # doing the bridging

    lots: dict[str, list[list[float]]] = {}
    out = []
    for f in fills:
        if f.side == "buy":
            lots.setdefault(f.symbol, []).append([f.quantity, f.price])
            out.append(W.Trade(f.symbol, account, f.on, "buy", f.quantity))
            continue

        remaining = f.quantity
        cost = 0.0
        book = lots.get(f.symbol, [])
        while remaining > QTY_TOL and book:
            take = min(remaining, book[0][0])
            cost += take * book[0][1]
            book[0][0] -= take
            remaining -= take
            if book[0][0] <= QTY_TOL:
                book.pop(0)
        matched = f.quantity - remaining
        proceeds = matched * f.price
        pnl = proceeds - cost if matched > QTY_TOL else 0.0
        out.append(W.Trade(f.symbol, account, f.on, "sell", f.quantity,
                           realized_pnl=round(pnl, 6)))
    return out


# --------------------------------------------------------------------------
# the append-only journal
# --------------------------------------------------------------------------

JOURNAL_RE = re.compile(r"^journal-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def journal_filename(run_date: date, seq: int = 0) -> str:
    """One file per run. A second run on the same day gets its own sequence
    number rather than overwriting the first, because the connector cannot
    overwrite and a silently dropped run is worse than a duplicate."""
    stem = f"journal-{run_date.isoformat()}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


# Fields runlog._regressions and runlog.find_optimizations actually read
# from one history entry -- see runlog.py. Every field run_entry() emits
# exists because one of those two functions consumes it; nothing else
# belongs in a "run" journal entry, which is why it stays a small, pinned
# projection of the full manifest rather than the manifest itself (that
# already gets written whole to run-manifest-YYYY-MM-DD[-N].json).
RUN_ENTRY_SCHEMA_FIELDS = ("run_id", "health", "duration_ms", "decisions", "stages")


def run_entry(log: Any) -> dict:
    """The payload for a journal `"run"` entry.

    Before this existed, `DAILY_PROCEDURE.md` described it only as "a
    compact summary for `find_optimizations`" — prose, not a pinned
    contract, with nothing stopping the fields actually written from
    drifting out of sync with the fields `runlog._regressions` and
    `runlog.find_optimizations` read (`health`, `duration_ms`,
    `decisions[].action`/`.inputs.recovered_within_5d`/`.gate_failed`/
    `.executed`, `stages[].name`/`.duration_ms`). A mismatch there would
    not raise — `dict.get(..., default)` throughout both functions means
    a missing or renamed field just silently stops contributing to the
    optimization findings, exactly the kind of failure that never gets
    noticed until someone asks why a known pattern stopped showing up.

    Accepts either a `runlog.RunLog` (or anything else with a
    `.manifest()` method shaped the same way — duck-typed rather than
    importing `runlog`, the same reason `to_washsale_trades` imports
    `washsale` locally rather than at module level: avoiding an import
    cycle) or a plain manifest `dict` directly, for testing without a
    `RunLog` instance.
    """
    m = log.manifest() if hasattr(log, "manifest") else dict(log)
    return {
        "run_id": m.get("run_id", ""),
        "health": m.get("health", ""),
        "duration_ms": m.get("duration_ms", 0),
        "decisions": m.get("decisions", []),
        "stages": m.get("stages", []),
    }


@dataclass
class JournalEntry:
    """One recorded fact from one run."""
    run_id: str
    on: str                        # ISO date
    kind: str                      # thesis | decision | run | note
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Journal:
    entries: list[JournalEntry] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[JournalEntry]:
        return [e for e in self.entries if e.kind == kind]

    @property
    def runs(self) -> list[dict]:
        """Past run manifests, oldest first — what find_optimizations reads."""
        return [e.payload for e in self.of_kind("run")]

    @property
    def opening_balances(self) -> dict[str, float]:
        """Symbol -> quantity for shares a human has explicitly attested arrived
        outside the order book or before the API's history horizon (see
        `ledger.positions_from_fills`). Folded oldest-first, so a later entry
        for the same symbol corrects an earlier one rather than doubling it —
        this is meant to be recorded once per symbol and rarely revised, not
        accumulated."""
        out: dict[str, float] = {}
        for e in self.of_kind("opening_balance"):
            sym = e.payload.get("symbol")
            qty = e.payload.get("quantity")
            if sym is not None and qty is not None:
                out[str(sym).upper()] = float(qty)
        return out

    @property
    def standing_circuit_breaker(self) -> Optional[dict]:
        """The payload of the most recent unresolved `circuit_breaker_tripped`
        entry, or `None` if clear. `self.entries` is already folded
        oldest-first, so the last matching entry — trip or clear — is
        chronologically the most recent; a trip with no later clear (or a
        clear with no later trip) is exactly what determines the current
        state. Only a human ever writes `circuit_breaker_cleared` — the
        automated run itself never does, by design (see
        `runlog.circuit_breaker_check`)."""
        latest = None
        for e in self.entries:
            if e.kind in ("circuit_breaker_tripped", "circuit_breaker_cleared"):
                latest = e
        if latest is None or latest.kind == "circuit_breaker_cleared":
            return None
        return latest.payload

    @property
    def latest_washsale_report(self) -> Optional[dict]:
        """The most recent `washsale_report` entry's payload (see
        `washsale.Registry.report`), or `None` if no run has ever recorded
        one. `self.entries` is already folded oldest-first, so the last
        matching entry is the most recent run's actual, complete
        `blocked_symbols` output — not a hand-written summary of it. Feeds
        `runlog.washsale_registry_stable`, which compares this run's fresh
        report against it."""
        latest = None
        for e in self.entries:
            if e.kind == "washsale_report":
                latest = e
        return latest.payload if latest is not None else None

    def open_theses(self, asof: date) -> list[dict]:
        """Theses whose horizon has not yet elapsed and which are not closed."""
        closed = {e.payload.get("thesis_id") for e in self.of_kind("close")}
        out = []
        for e in self.of_kind("thesis"):
            p = e.payload
            if p.get("thesis_id") in closed:
                continue
            opened = _as_date(p.get("opened"))
            horizon = int(p.get("horizon_days", 0) or 0)
            # Strictly the complement of matured_theses. When these two used
            # different comparisons a thesis was simultaneously open and due for
            # scoring on its maturity date, which would have let the same
            # prediction be counted as evidence twice.
            if opened and horizon and opened + timedelta(days=horizon) <= asof:
                continue
            out.append(p)
        return out

    def matured_theses(self, asof: date) -> list[dict]:
        """Theses whose horizon has elapsed and which still need scoring.

        This is what makes the evidence record accumulate on its own. A
        prediction nobody goes back to settle is not evidence, it is a diary.
        """
        closed = {e.payload.get("thesis_id") for e in self.of_kind("close")}
        out = []
        for e in self.of_kind("thesis"):
            p = e.payload
            if p.get("thesis_id") in closed:
                continue
            opened = _as_date(p.get("opened"))
            horizon = int(p.get("horizon_days", 0) or 0)
            if opened and horizon and opened + timedelta(days=horizon) <= asof:
                out.append(p)
        return out

    def closed_for_scoring(self, extra_outcomes: Sequence[dict] = ()) -> list[dict]:
        """Every settled thesis, shaped for `runlog.score_closed_decisions`
        — which needs `outcome_pct`, `thesis_played_out`, `horizon_days`.

        `evidence.Outcome.to_dict()`, what a `"outcome"` journal entry's
        payload actually carries, has neither field under those exact
        names: `excess_pct` — the only number `Outcome` itself calls
        meaningful, see its own docstring — is the honest choice for
        `outcome_pct`, and `horizon_days` lives on the original `"thesis"`
        entry, not the outcome, so this joins the two by `thesis_id`. An
        outcome with no matching thesis entry, or missing `excess_pct`
        entirely, is skipped rather than guessed at.

        `extra_outcomes` lets a caller pass in outcomes scored THIS run
        (`Outcome.to_dict()` results not yet written to the journal)
        alongside the already-journaled ones — the same "journal plus this
        run's fresh scores" pattern Stage 0.5 already uses when building
        `evidence.assess`'s input.
        """
        theses_by_id = {e.payload.get("thesis_id"): e.payload for e in self.of_kind("thesis")}
        all_outcomes = [e.payload for e in self.of_kind("outcome")] + list(extra_outcomes)
        out = []
        for p in all_outcomes:
            thesis = theses_by_id.get(p.get("thesis_id"))
            if thesis is None or "excess_pct" not in p:
                continue
            out.append({
                "outcome_pct": p["excess_pct"],
                "thesis_played_out": p.get("thesis_played_out"),
                "horizon_days": int(thesis.get("horizon_days", 0) or 0),
            })
        return out


def fold_journal(files: Iterable[dict]) -> Journal:
    """Fold dated journal files into one view, oldest first.

    `files` are `{"title": str, "content": str}`. A file that will not parse is
    recorded in `unreadable` and skipped rather than killing the fold: losing
    one day of theses is bad, losing the whole history because of one bad write
    is much worse.

    A monthly-compacted file (`journal-monthly-YYYY-MM[-N].json`, see
    `compact_journal_month`) is the sole source for its calendar month —
    any daily `journal-YYYY-MM-DD[-N].json` file for that same month is
    skipped rather than folded, even if it is still sitting in Drive (the
    connector can create a file but not delete one, so old daily files
    left behind after compaction are expected, not an error). This is what
    makes compaction reduce file COUNT without changing what a fold
    produces: `fold_journal(daily_files_for_a_month)` and
    `fold_journal([the_monthly_compacted_file])` are exactly equivalent,
    and mixing both in one call still returns the monthly result, never a
    doubled one.
    """
    files = list(files)
    compacted_months: set[str] = set()
    for f in files:
        m = MONTHLY_JOURNAL_RE.match(str(f.get("title", "")))
        if m:
            compacted_months.add(f"{m.group(1)}-{m.group(2)}")

    # (sort_key, tie_break, body, source_filename) -- tie_break -1 for a
    # monthly file guarantees it sorts before any same-month daily file
    # that might otherwise share its "YYYY-MM-01" sort key; in practice
    # this never matters since a compacted month's daily files are already
    # excluded above, but it keeps the ordering well-defined regardless.
    dated: list[tuple[str, int, dict, str]] = []
    bad: list[str] = []

    for f in files:
        title = str(f.get("title", ""))
        dm = JOURNAL_RE.match(title)
        mm = MONTHLY_JOURNAL_RE.match(title)
        if dm:
            iso, seq = dm.group(1), int(dm.group(2) or 0)
            if iso[:7] in compacted_months:
                continue
            try:
                body = json.loads(f.get("content") or "{}")
            except (ValueError, TypeError):
                bad.append(title)
                continue
            dated.append((iso, seq, body, journal_filename(date.fromisoformat(iso), seq)))
        elif mm:
            year, month, seq = int(mm.group(1)), int(mm.group(2)), int(mm.group(3) or 0)
            try:
                body = json.loads(f.get("content") or "{}")
            except (ValueError, TypeError):
                bad.append(title)
                continue
            dated.append((f"{year:04d}-{month:02d}-01", -1, body,
                          journal_monthly_filename(year, month, seq)))

    dated.sort(key=lambda t: (t[0], t[1]))

    entries: list[JournalEntry] = []
    sources: list[str] = []
    for iso, seq, body, source in dated:
        sources.append(source)
        for raw in body.get("entries", []):
            try:
                entries.append(JournalEntry(
                    run_id=str(raw.get("run_id", "")),
                    on=str(raw.get("on", iso)),
                    kind=str(raw.get("kind", "note")),
                    payload=dict(raw.get("payload", {})),
                ))
            except (TypeError, ValueError):
                bad.append(f"{iso} entry")
    return Journal(entries=entries, sources=sources, unreadable=bad)


def journal_monthly_filename(year: int, month: int, seq: int = 0) -> str:
    """One compacted file per calendar month, same create-only convention
    as `journal_filename`. Named `journal-monthly-` rather than reusing the
    daily `journal-YYYY-MM-DD` prefix with an omitted day so the two can
    never be mistaken for each other by a regex — `journal-2026-09.json`
    would otherwise be ambiguous with a daily file whose day happens to
    look like a sequence number (`journal-2026-09-01.json`)."""
    stem = f"journal-monthly-{year:04d}-{month:02d}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


MONTHLY_JOURNAL_RE = re.compile(r"^journal-monthly-(\d{4})-(\d{2})(?:-(\d+))?\.json$")


def month_is_compactable(year: int, month: int, *, today: Optional[date] = None) -> bool:
    """Whether a calendar month is safe to compact: strictly before the
    CURRENT month, never today's still-accumulating one. Compacting a
    month that could still receive a new daily file would let that later
    file silently disappear from the fold once the monthly file exists —
    `fold_journal` treats a compacted month as complete and skips any
    daily file for it, including one written after compaction ran. This
    is the guard against compacting too early, not a scheduling mechanism;
    compaction is an occasional maintenance step, not part of the daily
    routine."""
    today = today or date.today()
    return (year, month) < (today.year, today.month)


def compact_journal_month(daily_files: Iterable[dict]) -> dict:
    """Fold a set of daily journal files — expected to be every daily file
    for exactly one calendar month — into the body of one monthly-compacted
    file. Entry order and content are unchanged; compaction reduces file
    COUNT, never information, which is what makes it exactly equivalent to
    folding the daily files it replaces (see `fold_journal`'s docstring).

    Raises `ValueError` if the given files span more than one calendar
    month — `fold_journal`'s supersede-by-month logic requires a monthly
    file to be a complete, exact replacement for exactly the days it
    claims to cover, never a partial one.
    """
    daily_files = list(daily_files)
    months = {
        m.group(1)[:7]
        for f in daily_files
        if (m := JOURNAL_RE.match(str(f.get("title", ""))))
    }
    if len(months) > 1:
        raise ValueError(
            f"compact_journal_month given files spanning multiple months: {sorted(months)} "
            "-- compact one month at a time")
    j = fold_journal(daily_files)
    return {"entries": [e.to_dict() for e in j.entries]}
