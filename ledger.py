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
    "fills_for_account", "CACHE_FILE_MAX_INLINE_BYTES", "fills_cache_chunks",
    "SPLITS_CACHE_HORIZON_DAYS", "splits_cache_filename", "SPLITS_CACHE_RE",
    "SplitsCacheEntry", "fold_splits_cache", "symbols_needing_split_check",
    "SECTOR_CACHE_HORIZON_DAYS", "sector_cache_filename", "SECTOR_CACHE_RE",
    "fold_sector_cache", "symbols_needing_sector_check",
    "CONGRESS_DISCOVERY_HORIZON_DAYS", "congress_discovery_cache_filename",
    "CONGRESS_DISCOVERY_CACHE_RE", "fold_congress_discovery_cache",
    "bioguides_needing_discovery_check",
    "STATE_BUNDLE_SCHEMA_VERSION", "STATE_BUNDLE_RE",
    "state_bundle_chunk_filename", "state_bundle_chunks",
    "next_state_bundle_run_seq", "group_state_bundle_titles",
    "merge_state_bundle_chunks", "BUNDLE_EXCLUDED_JOURNAL_KINDS",
    "journal_files_to_supplement",
    "BUNDLE_MAX_AGE_DAYS", "state_bundle_needs_rewrite", "state_bundle_age_days",
    "build_state_bundle", "unpack_state_bundle",
    "fills_cache_matches_fresh",
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
    """One executed trade, as the broker reports it.

    `account` is which brokerage account executed it. It exists because
    everything downstream of a fill is PER ACCOUNT -- `DAILY_PROCEDURE.md`
    Stage 0 step 7 reconciles each account against its own
    `get_equity_positions` snapshot, and step 8 calls
    `to_washsale_trades(fills, account)` once per account -- while
    `fold_fills_cache` returns one flat list folded from files that mix both.
    Without the field, a cached fill's account is unrecoverable and the merge
    is silently wrong: demonstrated against live broker data on 9 September
    2026, where SGOV is held in both accounts (29.805637 derived for one,
    4.421802 for the other) and an account-less merged cache yields 34.227439,
    which reconciles against neither. That run refused to write its cache
    at all rather than corrupt the next run's reconciliation, and aborted in
    Stage 0 step 7 having spent its wall-clock budget re-pulling 880 orders
    it could not cache. The default is empty only so the field can sit last
    without reordering every existing call site; `fold_fills_cache` requires
    a real one on every cached row.
    """
    symbol: str
    side: str                   # buy | sell
    quantity: float
    price: float
    on: date
    order_id: str = ""
    account: str = ""

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


def fills_from_orders(orders: Iterable[dict], account: str = "") -> list[Fill]:
    """Extract actual executions from broker order payloads.

    `account` is stamped onto every fill produced. The caller already knows
    it -- `get_equity_orders` is called once per account number -- and it is
    the only place that knowledge exists, since an order payload does not
    carry the account it was placed in. Passing it here is what lets a fill
    survive a round trip through the fills cache still knowing which
    account's position it belongs to (see `Fill.account`).

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
            account=str(account),
        ))
    out.sort(key=lambda f: (f.on, f.order_id))
    return out


def fills_for_account(fills: Sequence[Fill], account: str) -> list[Fill]:
    """Just the fills belonging to `account`, order preserved.

    `fold_fills_cache` deliberately returns one flat list across both
    accounts -- the cache files are dated, not per-account -- but every
    consumer that reconciles or builds wash-sale trades works on exactly one
    account at a time. This is that split, in code, so it is not re-derived
    by hand each morning as a comprehension that could quietly compare the
    wrong side.
    """
    return [f for f in fills if f.account == account]


def all_traded_symbols(fills: Sequence[Fill]) -> list[str]:
    """Every unique symbol either account has EVER traded, current positions
    or not -- the `all_symbols` `DAILY_PROCEDURE.md` Stage 0 step 7 names for
    `symbols_needing_split_check`, built here in code instead of assembled by
    whoever is running that morning.

    A symbol sold down to zero drops out of `get_equity_positions` forever,
    but its loss sale is exactly what the wash-sale registry still has to
    reason about, and `cost_basis`/`loss_sales`/`to_washsale_trades` need its
    fills split-adjusted just as much as a currently-held name's do -- the
    registry does not stop caring about a position the day it closes.
    Deriving this list from held positions instead of fills would silently
    exclude every closed-out symbol from the split check. Confirmed 5
    September 2026: a manual reconciliation built its split-check list from
    held positions and missed MRVL, CRM, and TSLA (all fully sold, none held
    that day) as a result -- see PROCEDURE_RATIONALE.md for the investigation
    and for why the live production runs' own reported "68 symbols" figure
    (close to `len(all_traded_symbols(...))`'s ~70, nowhere near the ~24 held
    positions) is evidence, though not proof, that the daily runs themselves
    had been deriving this correctly from fills all along. Either way,
    nothing had ever pinned that derivation in code before this function.
    """
    return sorted({f.symbol.upper() for f in fills})


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
                    f.on, f.order_id, f.account)
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
    `fold_journal`. Deduplicated on `account` + `order_id` (falling back to a
    symbol/date/price/quantity key for the rare fill with no id) so a
    caller that accidentally re-caches an already-cached fill does not
    double it into the position count. The key is account-scoped because
    the rest of the pipeline is: two accounts' rows live in the same file,
    and a key that ignored the account could collapse one account's fill
    into the other's.

    **A row without a non-empty `account` is recorded in `bad`, not
    folded.** That is deliberately strict, and it is the whole point of the
    9 September 2026 fix: an account-less row cannot be attributed to
    either account, and folding it anyway would put a fill into a merged
    total that reconciles against neither one -- the exact corruption the
    9 September run refused to write its cache to avoid. A `bad` entry
    reaches `runlog.preflight(unreadable_files=...)` and aborts the run
    with a readable reason, which is the correct outcome: better a loud
    stop than a position count that is quietly wrong. No fills-cache file
    written before this change exists, so nothing legacy is being rejected.
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
                    account=str(r["account"]),
                )
                if fill.on is None:
                    raise ValueError("unparseable date")
                if not fill.account:
                    raise ValueError("fill has no account")
            except (KeyError, TypeError, ValueError):
                bad.append(f"{fills_cache_filename(date.fromisoformat(iso), seq)} fill")
                continue
            ident = fill.order_id or f"{fill.symbol}:{fill.on}:{fill.price}:{fill.quantity}"
            seen[f"{fill.account}:{ident}"] = fill

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


def _fill_cache_row(f: Fill) -> dict:
    """The one row shape every fills-cache file and the state bundle's own
    `fills` list use. Defined once so the two cannot drift apart --
    `fold_fills_cache` is the only reader of either."""
    return {"symbol": f.symbol, "side": f.side, "quantity": f.quantity,
            "price": f.price, "on": f.on.isoformat(), "order_id": f.order_id,
            "account": f.account}


# The Drive connector takes file content INLINE only (`create_file`'s
# `textContent`/`base64Content`; `update_file` is metadata-only), so a cache
# file's ENTIRE payload has to pass back out through the caller inside a
# single tool call. That caps one write at roughly the harness's inline
# argument budget -- and the fills cache outgrew it. 8, 15 and 16 September
# 2026 each computed a real 870-row, ~136 KB `fills_ready_to_cache` list and
# then wrote no file at all, so the next run found no cache, re-fetched the
# full history, and arrived at the same wall the next day: the same
# bytes-scaling deadlock `drive_snippets` broke on the READ side, still open
# on the write side.
#
# The dated-file convention already carries the fix. `FILLS_CACHE_RE` accepts
# a `-N` sequence suffix and `fold_fills_cache` folds every matching file for
# a date together, sorted and deduplicated, so N small files for one day fold
# to exactly what one large file would have -- see
# `fills_cache_chunks`. 60,000 bytes leaves real headroom under the limit
# without making the daily file count grow faster than it needs to; the
# steady state after one full write lands is a handful of rows a day, well
# inside a single chunk.
CACHE_FILE_MAX_INLINE_BYTES = 60_000


def fills_cache_chunks(fills: Sequence[Fill], run_date: date, *,
                       max_bytes: int = CACHE_FILE_MAX_INLINE_BYTES,
                       start_seq: int = 0) -> list[tuple[str, str]]:
    """Split one run's cache-ready fills into `(title, content)` pairs small
    enough to each be written by a single `create_file` call.

    Returns `[]` for an empty `fills` -- the existing "skip this file
    entirely when the list is empty" rule, unchanged: an empty cache file is
    still a file the next run has to read and fold for nothing.

    Titles run `fills-cache-{run_date}.json`, `-1.json`, `-2.json`, ...
    from `start_seq` (pass the next free sequence number if an earlier run
    today already wrote some). Each `content` is a JSON list of
    `_fill_cache_row` objects -- byte-for-byte the shape
    `fold_fills_cache` already reads, so folding the chunks is folding the
    file they replace.

    A single row larger than `max_bytes` on its own still gets its own
    chunk rather than being dropped: an oversized write that fails loudly
    is recoverable, a silently discarded fill is the corruption this
    module exists to prevent.
    """
    if not fills:
        return []

    encoded = [json.dumps(_fill_cache_row(f), separators=(",", ":")) for f in fills]
    out: list[tuple[str, str]] = []
    seq = start_seq
    batch: list[str] = []
    size = 2  # the enclosing "[" and "]"

    def flush() -> None:
        nonlocal batch, size, seq
        if not batch:
            return
        out.append((fills_cache_filename(run_date, seq), "[" + ",".join(batch) + "]"))
        seq += 1
        batch = []
        size = 2

    for row in encoded:
        cost = len(row.encode("utf-8")) + (1 if batch else 0)
        if batch and size + cost > max_bytes:
            flush()
            cost = len(row.encode("utf-8"))
        batch.append(row)
        size += cost
    flush()
    return out


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

    `account` is STAMPED onto every trade produced. This call never filters by
    it -- pass one account's fills, via `fills_for_account(fills, account)`,
    not the combined history. Handing it everything labels both accounts'
    trades with one account number, which leaves the blocked set, severities
    and `clears_on` dates untouched (every trade is just present twice under
    two labels) and so slips past `washsale_registry_stable`, while silently
    corrupting the `reason` prose a human reads to decide whether a block is
    real. Observed live 18 September 2026; see `DAILY_PROCEDURE.md` Stage 0
    step 8.

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
RUN_ENTRY_SCHEMA_FIELDS = ("run_id", "health", "duration_ms", "decisions", "stages", "brokerage_ok")


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

    # brokerage_ok (added 5 September 2026): a single, cheap summary of
    # whether this run's Robinhood calls succeeded, so
    # `Journal.days_since_last_brokerage_success` has something to walk
    # back through without the full journal needing to carry every call's
    # detail (`calls` itself is NOT part of this pinned schema, same reason
    # `decisions`/`stages` are trimmed projections rather than the full
    # manifest). `None` when the run made no brokerage call at all --
    # distinct from `False`, which means a call was actually attempted and
    # failed. See runlog.brokerage_token_health and
    # PROCEDURE_RATIONALE.md, 5 September 2026.
    brokerage_calls = [c for c in m.get("calls", [])
                       if str(c.get("service", "")).lower() == "robinhood"]
    brokerage_ok = all(c.get("ok") for c in brokerage_calls) if brokerage_calls else None

    return {
        "run_id": m.get("run_id", ""),
        "health": m.get("health", ""),
        "duration_ms": m.get("duration_ms", 0),
        "decisions": m.get("decisions", []),
        "stages": m.get("stages", []),
        "brokerage_ok": brokerage_ok,
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

    def days_since_last_brokerage_success(self, asof: date) -> Optional[int]:
        """How many days since a run last recorded `brokerage_ok: true` in
        its `"run"` journal entry, or `None` if no run ever has.

        Feeds `runlog.brokerage_token_health`, which warns before the
        brokerage token's observed ~4-day expiry window is reached
        (`HANDOFF.md` section 12) rather than only after a run aborts at
        `tools_available` with no warning at all. `self.entries` is already
        folded oldest-first, so the last matching entry is the most recent
        real success -- a run with `brokerage_ok: False` or `None` (no
        brokerage call attempted, e.g. an abort before Stage 0 step 3) does
        not reset this count, since neither one is evidence the token is
        still good.
        """
        last_ok: Optional[date] = None
        for e in self.of_kind("run"):
            if e.payload.get("brokerage_ok") is True:
                d = _as_date(e.on)
                if d is not None:
                    last_ok = d
        if last_ok is None:
            return None
        return (asof - last_ok).days

    @property
    def opening_balances(self) -> dict[str, float]:
        """Symbol -> quantity for shares a human has explicitly attested arrived
        outside the order book or before the API's history horizon (see
        `ledger.positions_from_fills`). Folded oldest-first, so a later entry
        for the same symbol corrects an earlier one rather than doubling it —
        this is meant to be recorded once per symbol and rarely revised, not
        accumulated.

        **Account-UNAWARE — merges every recorded entry regardless of which
        account it is actually about.** Kept only for backward compatibility;
        `DAILY_PROCEDURE.md` step 7 calls `opening_balances_for_account`
        instead (18 September 2026) precisely because passing this merged
        map into the wrong account's `reconcile_positions` invents a phantom
        residual. See that method's docstring.
        """
        out: dict[str, float] = {}
        for e in self.of_kind("opening_balance"):
            sym = e.payload.get("symbol")
            qty = e.payload.get("quantity")
            if sym is not None and qty is not None:
                out[str(sym).upper()] = float(qty)
        return out

    def opening_balances_for_account(self, account: str) -> dict[str, float]:
        """Symbol -> quantity for opening balances recorded AGAINST THIS
        ACCOUNT specifically — the account-scoped counterpart of
        `opening_balances`, and the one `DAILY_PROCEDURE.md` step 7 actually
        calls, once per account, before that account's own
        `reconcile_positions`. `account` is the same raw account-number
        string `Fill.account`/`fills_for_account` use throughout this
        module, e.g. `"792805129"` — not a role name like `"individual"`.

        18 September 2026: the opening-balance payload schema had no
        `account` field at all when MBGL and MSFT were first recorded
        (1 September 2026), so `opening_balances` could only ever merge
        every entry across both accounts. Passing that unscoped map into an
        account it does NOT belong to invents a phantom residual —
        `positions_from_fills` adds the quantity to a symbol that account's
        own fills and broker positions never mention, and
        `reconcile_positions` then reports a disagreement against nothing,
        aborting a genuinely healthy day over a fact recorded correctly,
        just for the wrong account.

        **An entry with no `account` field at all is skipped here entirely,
        never guessed into either account.** The two original MBGL/MSFT
        entries are exactly that shape, which is why fresh, account-tagged
        entries were recorded for both the same day this method was added
        (see `PROCEDURE_RATIONALE.md`) — this method deliberately does not
        special-case the old, unscoped entries, so there is exactly one
        rule to reason about rather than a legacy exception living forever.
        """
        out: dict[str, float] = {}
        for e in self.of_kind("opening_balance"):
            if e.payload.get("account") != account:
                continue
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


# --------------------------------------------------------------------------
# sector cache -- replacing the hand-maintained research.SECTOR_MAP
# --------------------------------------------------------------------------
#
# A company's sector classification changes on the order of years, not
# days -- nothing like a split, which can happen with a week's notice.
# SECTOR_CACHE_HORIZON_DAYS is deliberately long (see PROCEDURE_RATIONALE.md,
# 5 September 2026) so a symbol's sector is fetched from Alpha Vantage
# (`research.sector_from_company_overview` / `sector_from_etf_profile`)
# roughly once and reused for months, the same create-only, dated-file
# pattern the fills and splits caches already use.

SECTOR_CACHE_HORIZON_DAYS = 180
SECTOR_CACHE_RE = re.compile(r"^sector-cache-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def sector_cache_filename(run_date: date, seq: int = 0) -> str:
    """Same create-only convention as `splits_cache_filename`."""
    stem = f"sector-cache-{run_date.isoformat()}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


def fold_sector_cache(files: Iterable[dict]) -> tuple[dict[str, dict], list[str]]:
    """Fold dated sector-cache files into one entry per symbol, plus any
    files/rows that would not parse.

    `files` are `{"title": str, "content": str}`, same shape as
    `fold_splits_cache`. A symbol can be rechecked over time (a genuine
    reclassification, however rare); the entry with the LATEST
    `checked_through` wins per symbol, folded oldest-file-first exactly like
    `fold_splits_cache`. Each returned entry is `{"sector": Optional[str],
    "checked_through": date}` -- `sector` is `None` when the symbol was
    checked and found to have no single sector (a diversified fund; see
    `research.sector_from_etf_profile`), which is itself worth caching so
    the next run does not re-fetch it hoping for a different answer.
    """
    by_symbol: dict[str, dict] = {}
    bad: list[str] = []
    dated: list[tuple[str, int, Any]] = []

    for f in files:
        title = str(f.get("title", ""))
        m = SECTOR_CACHE_RE.match(title)
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
                sector = row.get("sector")
                sector = str(sector) if sector is not None else None
            except (KeyError, TypeError, ValueError):
                bad.append(f"{sector_cache_filename(date.fromisoformat(iso), seq)} entry")
                continue
            existing = by_symbol.get(sym)
            if existing is None or checked_through >= existing["checked_through"]:
                by_symbol[sym] = {"sector": sector, "checked_through": checked_through}

    return by_symbol, bad


def symbols_needing_sector_check(symbols: Sequence[str],
                                 cache: dict[str, dict], *,
                                 horizon_days: int = SECTOR_CACHE_HORIZON_DAYS,
                                 today: Optional[date] = None) -> list[str]:
    """Which symbols need a fresh COMPANY_OVERVIEW/ETF_PROFILE call this
    run: never checked before, or checked more than `horizon_days` ago.
    Mirrors `symbols_needing_split_check` exactly, at a far longer horizon
    because a sector classification is far more stable than a split
    calendar."""
    today = today or date.today()
    boundary = today - timedelta(days=horizon_days)
    out = set()
    for sym in symbols:
        entry = cache.get(sym.upper())
        if entry is None or entry["checked_through"] < boundary:
            out.add(sym.upper())
    return sorted(out)


# --------------------------------------------------------------------------
# congressional-discovery cache -- bioguide_id -> symbols they have traded
# --------------------------------------------------------------------------
#
# CONGRESS_TRADES by bioguide_id returns a member's ENTIRE disclosed trading
# history every call (no date-range parameter exists) -- often thousands of
# rows, always a preview envelope in practice. Re-fetching that for the same
# member every day to find maybe one new disclosure is the same mistake the
# fills/splits caches exist to avoid, at a much worse ratio. This cache
# stores only the DISCOVERED SYMBOLS per member, not the trade rows
# themselves (the trade detail is not needed once a symbol has been fed into
# `research.candidates()` -- from there it is treated exactly like any other
# candidate and re-researched normally), at a weekly horizon matching the
# fills/splits caches' cadence.

CONGRESS_DISCOVERY_HORIZON_DAYS = 7
CONGRESS_DISCOVERY_CACHE_RE = re.compile(
    r"^congress-discovery-cache-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def congress_discovery_cache_filename(run_date: date, seq: int = 0) -> str:
    """Same create-only convention as the fills/splits caches."""
    stem = f"congress-discovery-cache-{run_date.isoformat()}"
    return f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json"


def fold_congress_discovery_cache(files: Iterable[dict]
                                  ) -> tuple[dict[str, dict], list[str]]:
    """Fold dated congress-discovery-cache files into one entry per
    bioguide_id, plus any files/rows that would not parse. Same shape and
    same latest-`checked_through`-wins fold as `fold_sector_cache`. Each
    entry is `{"symbols": list[str], "checked_through": date}`.
    """
    by_id: dict[str, dict] = {}
    bad: list[str] = []
    dated: list[tuple[str, int, Any]] = []

    for f in files:
        title = str(f.get("title", ""))
        m = CONGRESS_DISCOVERY_CACHE_RE.match(title)
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
                bioguide_id = str(row["bioguide_id"])
                checked_through = _as_date(row["checked_through"])
                if checked_through is None:
                    raise ValueError("unparseable checked_through")
                symbols = sorted({str(s).upper() for s in row.get("symbols", [])})
            except (KeyError, TypeError, ValueError):
                bad.append(f"{congress_discovery_cache_filename(date.fromisoformat(iso), seq)} entry")
                continue
            existing = by_id.get(bioguide_id)
            if existing is None or checked_through >= existing["checked_through"]:
                by_id[bioguide_id] = {"symbols": symbols, "checked_through": checked_through}

    return by_id, bad


def bioguides_needing_discovery_check(bioguide_ids: Sequence[str],
                                      cache: dict[str, dict], *,
                                      horizon_days: int = CONGRESS_DISCOVERY_HORIZON_DAYS,
                                      today: Optional[date] = None) -> list[str]:
    """Which tracked bioguide_ids need a fresh CONGRESS_TRADES(bioguide_id=)
    pull this run. Mirrors `symbols_needing_split_check` exactly."""
    today = today or date.today()
    boundary = today - timedelta(days=horizon_days)
    out = set()
    for bid in bioguide_ids:
        entry = cache.get(bid)
        if entry is None or entry["checked_through"] < boundary:
            out.add(bid)
    return sorted(out)


# --------------------------------------------------------------------------
# state bundle -- collapses Stage 0's ~20-file read path into one file
# --------------------------------------------------------------------------
#
# 7-11 September 2026: Stage 0 grew a separate append-only Drive file per
# concern -- journal, fills cache, splits cache, sector cache, congress-
# discovery cache -- each reasonable alone. Together they became a store
# where every read costs a real, measured tens-of-seconds-per-file
# write-back cost (`download_file_content`'s base64 payload has to be
# materialised locally before `json.loads` can read it, not the download
# itself -- see PROCEDURE_RATIONALE.md), and the file count grows by at
# least one every trading day with no bound reachable before 1 October
# (`month_is_compactable` forbids the current month). By 11 September the
# required set was 21 files and ~299 KB, and two independent same-day
# measurements clocked the connector at 23-39 bytes/second -- not
# bandwidth, per-file latency, paid once per file before any work starts.
#
# `build_state_bundle`/`unpack_state_bundle` collapse that into ONE file,
# written once at Stage 6 and read once at Stage 0: one Drive round trip
# replacing twenty-one. The dated files are NOT replaced or superseded --
# they remain exactly what they always were, the append-only audit trail.
# The bundle is a CACHE of that trail, not a second source of truth:
# `unpack_state_bundle` round-trips a bundle's stored rows back through the
# SAME `fold_journal`/`fold_fills_cache`/`fold_splits_cache`/
# `fold_sector_cache`/`fold_congress_discovery_cache` functions the dated
# files use, by handing each its stored rows as one synthetic same-shaped
# file. Building a bundle and folding the dated files it summarises
# therefore produce identical results BY CONSTRUCTION -- there is no second
# parser to keep in sync with the first, and a bundle is exactly as
# trustworthy as the fold it stands in for.

STATE_BUNDLE_SCHEMA_VERSION = 1

# The bundle collapses Stage 0's ~20-file read into one, but its own content
# -- the FULL current journal, fills, and every cache -- routinely exceeds
# CACHE_FILE_MAX_INLINE_BYTES by 5-6x (observed 361,643 bytes on 18 September
# 2026). `create_file` takes content inline only, so a bundle this size can
# never be written as a single file -- confirmed empty in the Drive folder
# every day since 11 September regardless of what any run computed. This is
# the exact bytes-proportional wall the fills cache hit on 8/15/16 September
# and chunked its way past (`fills_cache_chunks`); the bundle needs the same
# fix, but with one difference: a bundle is a full-state SNAPSHOT, not an
# append-only accumulation like fills or journal entries, so a later write
# must REPLACE an earlier one's chunks rather than fold alongside them, and
# a run that aborts partway through writing one must not leave a half-
# written group masquerading as a complete, readable bundle.
#
# The filename carries three numbers, always all three, never an optional
# suffix: `state-bundle-{date}-{run_seq}-{chunk_seq}-of-{chunk_count}.json`.
# `run_seq` distinguishes separate bundle WRITES the same day (0 for the
# first, 1 for a watchdog retry's fresher one, ...) -- the role the old
# single `-N` suffix played before chunking existed. `chunk_seq`/
# `chunk_count` are new: which piece of THAT write this file is, out of how
# many total. A group is usable only once every `chunk_seq` from 0 to
# `chunk_count - 1` is actually present for its `(date, run_seq)` --
# verifiable from the file LISTING alone, no download needed, which is what
# lets an aborted partial write be silently skipped in favour of an older
# complete group instead of being read as truth.
STATE_BUNDLE_RE = re.compile(
    r"^state-bundle-(\d{4}-\d{2}-\d{2})-(\d+)-(\d+)-of-(\d+)\.json$")


def state_bundle_chunk_filename(run_date: date, run_seq: int, chunk_seq: int,
                                chunk_count: int) -> str:
    """One file per chunk of one bundle write. `chunk_count` is repeated in
    every chunk's own filename (not just its content) so completeness is
    checkable from a Drive listing alone -- see the module note above."""
    return (f"state-bundle-{run_date.isoformat()}-{run_seq}-{chunk_seq}"
            f"-of-{chunk_count}.json")


def next_state_bundle_run_seq(titles: Sequence[str], run_date: date) -> int:
    """The `run_seq` to use when writing a fresh bundle for `run_date`: one
    past the highest `run_seq` already present for that date among
    `titles` (a plain Drive listing of filenames, no download needed),
    complete or not. An incomplete group left by an earlier aborted write
    must never be reused -- its declared `chunk_count` may not match this
    write's, and mixing the two would silently corrupt both."""
    iso = run_date.isoformat()
    seen = []
    for title in titles:
        m = STATE_BUNDLE_RE.match(title)
        if m and m.group(1) == iso:
            seen.append(int(m.group(2)))
    return max(seen, default=-1) + 1


# 21 September 2026: the first complete bundle (382,057 bytes, 7 chunks) cost
# ~23 minutes of Stage 0 just to WRITE -- `create_file` takes content inline, so
# every byte is model output -- and a step that rewrote it every day would have
# made the "fast bundle path" a 23-minute daily tax on exactly the budget it
# exists to protect. The dated files are already the daily deltas, so the bundle
# only needs refreshing occasionally: readers fold anything dated on/after its
# `as_of` on top (see `unpack_state_bundle`'s `extra_*_files`).
BUNDLE_MAX_AGE_DAYS = 7

# `note` entries are free-text audit narrative (108 of the bundle's 382 KB) that
# no code path reads -- they stay in the dated journal files, which remain the
# audit trail, but carrying them in the bundle only makes every rewrite slower.
BUNDLE_EXCLUDED_JOURNAL_KINDS = frozenset({"note"})


def state_bundle_age_days(titles: Sequence[str], today: date) -> Optional[int]:
    """Days between `today` and the freshest COMPLETE bundle group's `as_of`,
    or `None` if there is no complete group (the bootstrap case)."""
    group = group_state_bundle_titles(titles)
    if group is None:
        return None
    return (today - date.fromisoformat(group[0])).days


def state_bundle_needs_rewrite(titles: Sequence[str], today: date, *,
                               max_age_days: int = BUNDLE_MAX_AGE_DAYS) -> bool:
    """Should this run write a fresh bundle? True when no complete group exists
    at all, or the freshest one is `max_age_days` or more old. Anything newer
    is served by the dated files written since (folded on top at read time), so
    rewriting it would spend Stage 0's budget to re-record what is already
    recorded."""
    age = state_bundle_age_days(titles, today)
    return age is None or age >= max_age_days


def build_state_bundle(*, journal_entries: Sequence[JournalEntry],
                       fills: Sequence[Fill],
                       splits_by_symbol: dict[str, SplitsCacheEntry],
                       sector_by_symbol: dict[str, dict],
                       congress_discovery: dict[str, dict],
                       as_of: date,
                       journal_sources: Sequence[str] = ()) -> dict:
    """Package everything Stage 0 needs into one JSON-serialisable object,
    passed to `state_bundle_chunks` to be split and written as one or more
    `state-bundle-*.json` files.

    `journal_sources` is the list of `journal-*.json` filenames whose entries
    `journal_entries` already contains (`Journal.sources`, plus the file(s) this
    run wrote). A reader folds every OTHER journal file on top
    (`journal_files_to_supplement`); without the list it could not tell which
    same-day file a rewritten bundle already absorbed, and would double-count it.

    Entries whose kind is in `BUNDLE_EXCLUDED_JOURNAL_KINDS` (`note`) are
    left out: nothing reads them, and the dated journal files keep them.

    `journal_entries` should be the FULL history -- the previously unpacked
    bundle's `journal.entries` plus every entry this run added -- not just
    this run's own; same for `fills`/`splits_by_symbol`/`sector_by_symbol`/
    `congress_discovery`, each the complete current cache, not a delta.
    Row shapes are IDENTICAL to the existing dated-cache file formats on
    purpose (see the module note above), which is what lets
    `unpack_state_bundle` reuse the existing fold functions unchanged.
    """
    return {
        "schema": STATE_BUNDLE_SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "journal_sources": sorted(set(journal_sources)),
        "journal_entries": [e.to_dict() for e in journal_entries
                            if e.kind not in BUNDLE_EXCLUDED_JOURNAL_KINDS],
        "fills": [_fill_cache_row(f) for f in fills],
        "splits_by_symbol": [
            {"symbol": e.symbol, "checked_through": e.checked_through.isoformat(),
             "splits": [{"effective_date": s.effective_date.isoformat(), "ratio": s.ratio}
                        for s in e.splits]}
            for e in splits_by_symbol.values()
        ],
        "sector_by_symbol": [
            {"symbol": sym, "sector": v["sector"],
             "checked_through": v["checked_through"].isoformat()}
            for sym, v in sector_by_symbol.items()
        ],
        "congress_discovery": [
            {"bioguide_id": bid, "symbols": v["symbols"],
             "checked_through": v["checked_through"].isoformat()}
            for bid, v in congress_discovery.items()
        ],
    }


_STATE_BUNDLE_LIST_FIELDS = (
    "journal_sources", "journal_entries", "fills", "splits_by_symbol", "sector_by_symbol",
    "congress_discovery",
)


def state_bundle_chunks(bundle: dict, run_date: date, *,
                        run_seq: int = 0,
                        max_bytes: int = CACHE_FILE_MAX_INLINE_BYTES
                        ) -> list[tuple[str, str]]:
    """Split one `build_state_bundle` result into `(title, content)` pairs
    small enough to each be written by a single `create_file` call -- the
    bundle-side counterpart of `fills_cache_chunks`.

    Every chunk is a JSON object carrying `schema`, `as_of`, `part`
    (0-based), `of` (the total chunk count), and a SLICE of each of the
    five list-valued fields (`journal_entries`, `fills`,
    `splits_by_symbol`, `sector_by_symbol`, `congress_discovery`) --
    concatenating those five lists across every chunk, in `part` order,
    reconstructs exactly the lists `build_state_bundle` produced.
    `merge_state_bundle_chunks` is the reverse of this function, the same
    relationship `unpack_state_bundle` has to `build_state_bundle`.

    Unlike `fills_cache_chunks`, there is no "nothing to write" case --
    always returns at least one chunk, even for a bundle whose lists are
    all empty, because `schema`/`as_of` alone are the whole point of a
    bundle existing (see `DAILY_PROCEDURE.md` step 7b: `state_bundle_written`
    is never skippable). `run_seq` should be `next_state_bundle_run_seq`'s
    result for this date so a same-day second write never collides with
    (or gets mistaken for part of) an earlier one.
    """
    flat: list[tuple[str, str]] = [
        (name, json.dumps(row, separators=(",", ":")))
        for name in _STATE_BUNDLE_LIST_FIELDS
        for row in bundle.get(name, [])
    ]

    header = {"schema": bundle.get("schema", STATE_BUNDLE_SCHEMA_VERSION),
              "as_of": bundle["as_of"]}
    empty_part_cost = len(json.dumps(
        {**header, "part": 0, "of": 1,
         **{name: [] for name in _STATE_BUNDLE_LIST_FIELDS}},
        separators=(",", ":")).encode("utf-8"))

    parts: list[list[tuple[str, str]]] = []
    batch: list[tuple[str, str]] = []
    size = empty_part_cost

    def flush() -> None:
        nonlocal batch, size
        parts.append(batch)
        batch = []
        size = empty_part_cost

    for name, row in flat:
        cost = len(row.encode("utf-8")) + 1
        if batch and size + cost > max_bytes:
            flush()
        batch.append((name, row))
        size += cost
    parts.append(batch)  # always at least one part, even if flat was empty

    total = len(parts)
    out: list[tuple[str, str]] = []
    for i, part_rows in enumerate(parts):
        payload: dict[str, Any] = {"schema": header["schema"],
                                    "as_of": header["as_of"],
                                    "part": i, "of": total}
        for name in _STATE_BUNDLE_LIST_FIELDS:
            payload[name] = []
        for name, row in part_rows:
            payload[name].append(json.loads(row))
        title = state_bundle_chunk_filename(run_date, run_seq, i, total)
        out.append((title, json.dumps(payload, separators=(",", ":"))))
    return out


def group_state_bundle_titles(titles: Sequence[str]
                              ) -> Optional[tuple[str, int, list[str]]]:
    """Given every `state-bundle-*.json` filename present in the Drive
    folder (titles only -- no download needed), find the freshest COMPLETE
    write and return `(as_of, run_seq, ordered_filenames)` to download, or
    `None` if no complete group exists at all (the bootstrap path).

    A write is a `(date, run_seq)` group; it counts as complete only when
    every `chunk_seq` from 0 up to (but not including) its own declared
    `chunk_count` is actually present among `titles` -- a run that aborted
    partway through writing leaves a partial group, and this treats that
    exactly like the group never existed rather than reading it as a
    truncated truth. Preferred group: the latest `date`, then within that
    date the highest `run_seq` among the COMPLETE ones only -- an
    incomplete fresher write never masks an older complete one.
    """
    groups: dict[tuple[str, int], dict[int, str]] = {}
    chunk_counts: dict[tuple[str, int], int] = {}
    for title in titles:
        m = STATE_BUNDLE_RE.match(title)
        if not m:
            continue
        as_of, run_seq, chunk_seq, chunk_count = (
            m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        key = (as_of, run_seq)
        groups.setdefault(key, {})[chunk_seq] = title
        # A chunk_count that disagrees across files in the same group is
        # itself a reason to treat the group as harder to complete, never
        # easier -- keep the smallest one seen.
        chunk_counts[key] = min(chunk_counts.get(key, chunk_count), chunk_count)

    complete = [
        key for key, files in groups.items()
        if len(files) == chunk_counts[key]
        and set(files) == set(range(chunk_counts[key]))
    ]
    if not complete:
        return None

    best = max(complete, key=lambda key: (key[0], key[1]))
    as_of, run_seq = best
    files = groups[best]
    ordered = [files[i] for i in range(chunk_counts[best])]
    return as_of, run_seq, ordered


def merge_state_bundle_chunks(files: Iterable[dict]
                              ) -> tuple[Optional[dict], list[str]]:
    """Reassemble one bundle from its chunk files (`{"title", "content"}`,
    the same shape `fold_journal`/`fold_fills_cache`/etc. all take) -- the
    reverse of `state_bundle_chunks`. `files` should already be exactly one
    group's files (`group_state_bundle_titles` gives you that), but this
    function re-sorts by each chunk's own declared `part` regardless, so
    passing them out of order is harmless.

    Returns `(bundle, bad)`: `bundle` is the same shape `build_state_bundle`
    produces (feed it straight to `unpack_state_bundle`), or `None` if
    every chunk was unreadable. `bad` names any chunk that failed to parse
    or whose `as_of` disagreed with the rest -- a bundle assembled from
    mismatched chunks is worse than none, so a disagreeing chunk's rows are
    never merged in, only reported.
    """
    parsed: list[tuple[int, dict]] = []
    bad: list[str] = []
    for f in files:
        title = str(f.get("title", ""))
        try:
            payload = json.loads(f.get("content") or "{}")
            part = int(payload["part"])
        except (ValueError, TypeError, KeyError):
            bad.append(title)
            continue
        parsed.append((part, payload))

    if not parsed:
        return None, bad

    parsed.sort(key=lambda t: t[0])
    schema = parsed[0][1].get("schema", STATE_BUNDLE_SCHEMA_VERSION)
    as_of = parsed[0][1].get("as_of")

    merged: dict[str, Any] = {"schema": schema, "as_of": as_of}
    for name in _STATE_BUNDLE_LIST_FIELDS:
        merged[name] = []

    for _, payload in parsed:
        if payload.get("as_of") != as_of:
            bad.append(f"as_of mismatch in part {payload.get('part')}")
            continue
        for name in _STATE_BUNDLE_LIST_FIELDS:
            merged[name].extend(payload.get(name, []))

    return merged, bad


def journal_files_to_supplement(bundle: dict, titles: Sequence[str]) -> list[str]:
    """Which journal files in the folder still need folding on top of `bundle`.

    With a `journal_sources` list (every bundle written from 21 September 2026)
    the answer is exact: every journal file not named in it. A legacy bundle
    without the list is a snapshot taken before that run's own journal file
    existed, so every file dated on or after its `as_of` is the supplement --
    which is also why a same-day file is not skipped: it was written after."""
    sources = bundle.get("journal_sources")
    journal_titles = [t for t in titles
                      if JOURNAL_RE.match(t) or MONTHLY_JOURNAL_RE.match(t)]
    if sources is not None:
        have = set(sources)
        return sorted(t for t in journal_titles if t not in have)
    as_of = bundle.get("as_of") or ""
    out = []
    for t in journal_titles:
        m = JOURNAL_RE.match(t)
        if m and m.group(1) >= as_of:
            out.append(t)
    return sorted(out)


def unpack_state_bundle(bundle: dict, *,
                        extra_journal_files: Sequence[dict] = (),
                        extra_fills_cache_files: Sequence[dict] = (),
                        extra_splits_cache_files: Sequence[dict] = (),
                        extra_sector_cache_files: Sequence[dict] = (),
                        extra_congress_discovery_cache_files: Sequence[dict] = ()
                        ) -> dict:
    """Reverse of `build_state_bundle`: reconstruct the `Journal` and the
    four fold results by feeding the bundle's stored rows back through the
    exact fold functions the dated files use, each as one synthetic
    same-shaped file dated `bundle["as_of"]`.

    `extra_journal_files` (15 September 2026) covers the gap a bundle
    written mid-run leaves: once Stage 0 writes the bundle as soon as its
    own data is ready, rather than waiting for Stage 6 when the wall-clock
    budget is tightest, the bundle's `journal_entries` cannot yet include
    entries THIS run adds later (theses, decisions, the run entry itself)
    -- those still land in the ordinary `journal-YYYY-MM-DD[-N].json` file
    Stage 6 always writes. Pass any such files dated on or after the
    bundle's own `as_of` (same `{"title", "content"}` shape as everywhere
    else) and they fold in alongside the bundle's own synthetic file, in
    the same oldest-first order `fold_journal` already guarantees --
    there is normally at most one or two of these (today's own run, and
    a same-day watchdog retry's), never the unbounded backlog the bundle
    exists to avoid reading.

    The other four `extra_*_files` parameters (21 September 2026) do the same
    for the fills, splits, sector and congress-discovery dated files: since a
    bundle is now refreshed only about weekly (`BUNDLE_MAX_AGE_DAYS`), the
    small dated files written since its `as_of` ARE the deltas, and folding
    them on top gives exactly the state a fresh bundle would have held.

    Returns `{"journal": Journal, "fills": list[Fill],
    "splits_by_symbol": dict[str, SplitsCacheEntry],
    "sector_by_symbol": dict[str, dict], "congress_discovery": dict[str, dict],
    "bad": list[str]}`. `bad` collects anything any of the five folds could
    not parse, prefixed `bundle:` -- feed it straight into
    `runlog.preflight(unreadable_files=...)` exactly like the dated-file
    reading path did, so a corrupt bundle aborts loudly instead of silently
    dropping state.
    """
    as_of = bundle.get("as_of") or date.today().isoformat()
    bad: list[str] = []

    journal = fold_journal([{
        "title": journal_filename(date.fromisoformat(as_of)),
        "content": json.dumps({"entries": bundle.get("journal_entries", [])}),
    }, *extra_journal_files])
    bad.extend(f"bundle:{x}" for x in journal.unreadable)

    fills, fills_bad = fold_fills_cache([{
        "title": fills_cache_filename(date.fromisoformat(as_of)),
        "content": json.dumps(bundle.get("fills", [])),
    }, *extra_fills_cache_files])
    bad.extend(f"bundle:{x}" for x in fills_bad)

    splits_by_symbol, splits_bad = fold_splits_cache([{
        "title": splits_cache_filename(date.fromisoformat(as_of)),
        "content": json.dumps(bundle.get("splits_by_symbol", [])),
    }, *extra_splits_cache_files])
    bad.extend(f"bundle:{x}" for x in splits_bad)

    sector_by_symbol, sector_bad = fold_sector_cache([{
        "title": sector_cache_filename(date.fromisoformat(as_of)),
        "content": json.dumps(bundle.get("sector_by_symbol", [])),
    }, *extra_sector_cache_files])
    bad.extend(f"bundle:{x}" for x in sector_bad)

    congress_discovery, congress_bad = fold_congress_discovery_cache([{
        "title": congress_discovery_cache_filename(date.fromisoformat(as_of)),
        "content": json.dumps(bundle.get("congress_discovery", [])),
    }, *extra_congress_discovery_cache_files])
    bad.extend(f"bundle:{x}" for x in congress_bad)

    return {
        "journal": journal,
        "fills": fills,
        "splits_by_symbol": splits_by_symbol,
        "sector_by_symbol": sector_by_symbol,
        "congress_discovery": congress_discovery,
        "bad": bad,
    }


# --------------------------------------------------------------------------
# cache-vs-broker cross-check -- catching a cache that holds ADJUSTED fills
# --------------------------------------------------------------------------
#
# 14 September 2026: a prior run cached `fills_ready_to_cache`'s output built
# from the SPLIT-ADJUSTED `fills` variable instead of the raw `fresh_fills`
# one `DAILY_PROCEDURE.md` step 7 actually names -- an execution mistake, not
# a defect in this file. `apply_splits` is deliberately NOT idempotent (see
# its own docstring: a fill dated before a split's effective date is always
# multiplied by the ratio, with no memory of whether that already happened),
# so every run since re-adjusted the corrupted rows a second time. It went
# undetected for five sessions because every affected symbol (NVDA, GOOGL,
# CMG, CRWD, NFLX, VUG) was already fully closed out: a net-zero position
# scaled by any constant still reconciles to zero against the broker, so
# `ledger_reconciled` had nothing to catch. Only cost basis, loss-sale
# amounts, and the wash-sale registry for those closed symbols were wrong,
# silently, every day.

def fills_cache_matches_fresh(cached_fills: Sequence[Fill],
                              fresh_fills: Sequence[Fill]) -> list[str]:
    """Cross-check cached fills against a fresh, authoritative re-fetch of
    the same orders for disagreement -- direct evidence the cache holds
    altered (most likely split-adjusted) quantities instead of the broker's
    raw, as-executed ones.

    `fresh_fills` should be `ledger.fills_from_orders`'s UNADJUSTED output,
    the same object `DAILY_PROCEDURE.md` step 7 caches via
    `fills_ready_to_cache` -- never the `apply_splits` result. The
    watermark-forward re-fetch (`fills_cache_watermark`) always re-covers the
    last `FILLS_CACHE_HORIZON_DAYS`, so cached and fresh rows for that window
    overlap by construction; a real disagreement there is not a race
    condition, it is corruption. Returns the mismatched `order_id`s, sorted,
    empty if everything agrees (within `QTY_TOL`).

    An empty result does NOT prove the whole cache is clean -- it can only
    see the horizon-window overlap. A fill older than that already left the
    re-fetch window and is invisible here regardless of its condition; only
    a full rebuild (`fills_cache_watermark` returning `None`, i.e. the cache
    was reset) re-verifies fills older than the horizon.
    """
    fresh_by_id = {f.order_id: f for f in fresh_fills if f.order_id}
    mismatched = []
    for c in cached_fills:
        f = fresh_by_id.get(c.order_id)
        if f is not None and abs(f.quantity - c.quantity) > QTY_TOL:
            mismatched.append(c.order_id)
    return sorted(mismatched)
