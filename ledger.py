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
    "Fill", "fills_from_orders", "positions_from_fills", "cost_basis",
    "loss_sales", "reconcile_positions", "to_washsale_trades",
    "JournalEntry", "Journal", "fold_journal", "journal_filename", "JOURNAL_RE",
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

    Only `filled` and `partially_filled` orders contribute, and the quantity
    used is `cumulative_quantity`, never `quantity`. An order's requested size
    is an intention; a position is built from what actually executed. Conflating
    the two is how a cancelled or rejected order becomes a phantom holding.
    """
    out: list[Fill] = []
    for o in orders:
        state = str(o.get("state", "")).lower()
        if state not in ("filled", "partially_filled"):
            continue

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


def positions_from_fills(fills: Sequence[Fill]) -> dict[str, float]:
    """Net position per symbol. Dust below the tolerance is dropped, because a
    residue of 1e-9 shares is a rounding artefact, not a holding."""
    pos: dict[str, float] = {}
    for f in fills:
        pos[f.symbol] = pos.get(f.symbol, 0.0) + f.signed_quantity
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
                        tol: float = QTY_TOL) -> dict:
    """Do the broker's positions follow from the broker's own fills?

    This replaces comparing the broker against a local file. A local file can be
    stale for a hundred boring reasons; a position that does not follow from the
    executions that produced it is a genuine anomaly -- a transfer, a corporate
    action, a manual trade outside this system, or a bug here.

    Returns the disagreements only. Empty means the two views agree.
    """
    derived = positions_from_fills(fills)
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


def fold_journal(files: Iterable[dict]) -> Journal:
    """Fold dated journal files into one view, oldest first.

    `files` are `{"title": str, "content": str}`. A file that will not parse is
    recorded in `unreadable` and skipped rather than killing the fold: losing
    one day of theses is bad, losing the whole history because of one bad write
    is much worse.
    """
    dated: list[tuple[str, int, dict]] = []
    bad: list[str] = []

    for f in files:
        title = str(f.get("title", ""))
        m = JOURNAL_RE.match(title)
        if not m:
            continue
        try:
            body = json.loads(f.get("content") or "{}")
        except (ValueError, TypeError):
            bad.append(title)
            continue
        dated.append((m.group(1), int(m.group(2) or 0), body))

    dated.sort(key=lambda t: (t[0], t[1]))

    entries: list[JournalEntry] = []
    sources: list[str] = []
    for iso, seq, body in dated:
        sources.append(journal_filename(date.fromisoformat(iso), seq))
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
