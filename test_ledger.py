"""Tests for the rebuilt-from-broker ledger.

The order fixture is REAL: it is the agentic account's actual order history as
the broker returned it on 31 August 2026, including the cancelled SGOV stop that
blocked a correctly-sized sell. Synthetic fixtures agree with whatever the code
assumes; real payloads carry the fields and states that actually turn up.
"""

import json
from datetime import date

import pytest

import ledger as L


# --- real payloads, agentic account, 2026-08-31 -------------------------------

ORDERS = [
    # the stale stop the owner cancelled by hand: requested 1 share, filled none
    {"id": "6a920a00", "symbol": "SGOV", "side": "sell", "type": "market",
     "state": "cancelled", "quantity": "1.000000", "cumulative_quantity": "0.000000",
     "price": None, "stop_price": "90.000000", "average_price": None,
     "created_at": "2026-08-28T22:21:52.670594Z",
     "last_transaction_at": "2026-08-31T17:40:02.532Z"},
    {"id": "6a8f4ba5", "symbol": "GLDM", "side": "sell", "type": "market",
     "state": "filled", "quantity": "1.089681", "cumulative_quantity": "1.089681",
     "average_price": "90.790000", "created_at": "2026-08-26T20:25:09.24896Z",
     "last_transaction_at": "2026-08-27T13:30:00.616Z"},
    {"id": "6a8f4ba1", "symbol": "XLE", "side": "sell", "type": "market",
     "state": "filled", "quantity": "2.380578", "cumulative_quantity": "2.380578",
     "average_price": "62.230000", "created_at": "2026-08-26T20:25:05.108967Z",
     "last_transaction_at": "2026-08-27T13:30:00.124Z"},
    {"id": "6a8c87ed", "symbol": "XLE", "side": "buy", "type": "market",
     "state": "filled", "quantity": "2.380578", "cumulative_quantity": "2.380578",
     "price": "63.000000", "average_price": "63.009900",
     "created_at": "2026-08-24T18:05:33.306262Z",
     "last_transaction_at": "2026-08-24T18:05:33.464Z"},
    {"id": "6a8c87ec", "symbol": "GLDM", "side": "buy", "type": "market",
     "state": "filled", "quantity": "2.179363", "cumulative_quantity": "2.179363",
     "average_price": "91.769900", "created_at": "2026-08-24T18:05:32.238632Z",
     "last_transaction_at": "2026-08-24T18:05:32.362Z"},
    {"id": "6a8c87ea", "symbol": "VGSH", "side": "buy", "type": "market",
     "state": "filled", "quantity": "3.440007", "cumulative_quantity": "3.440007",
     "average_price": "58.139400", "created_at": "2026-08-24T18:05:30.131287Z",
     "last_transaction_at": "2026-08-24T18:05:30.31Z"},
    {"id": "6a8c87e7", "symbol": "SGOV", "side": "buy", "type": "market",
     "state": "filled", "quantity": "4.421802", "cumulative_quantity": "4.421802",
     "average_price": "100.637700", "created_at": "2026-08-24T18:05:28.01235Z",
     "last_transaction_at": "2026-08-24T18:05:28.185Z"},
]

# what the broker independently reported holding that morning
BROKER_POSITIONS = {"SGOV": 4.421802, "VGSH": 3.440007, "GLDM": 1.089682}


@pytest.fixture
def fills():
    return L.fills_from_orders(ORDERS)


# ---------------------------------------------------------------- fills

def test_cancelled_orders_never_become_positions(fills):
    """The regression this fixture exists for. A requested-but-unfilled order is
    an intention; treating it as a holding invents shares that do not exist."""
    assert not any(f.order_id == "6a920a00" for f in fills)
    assert len(fills) == 6


def test_quantity_comes_from_what_executed_not_what_was_asked(fills):
    gldm_sell = next(f for f in fills if f.symbol == "GLDM" and f.side == "sell")
    assert gldm_sell.quantity == pytest.approx(1.089681)


def test_positions_rebuilt_from_fills_match_the_broker(fills):
    """The whole design rests on this: if positions follow from fills, a local
    copy of them is redundant and cannot drift."""
    derived = L.positions_from_fills(fills)
    assert derived.keys() == BROKER_POSITIONS.keys()
    for sym, qty in BROKER_POSITIONS.items():
        assert derived[sym] == pytest.approx(qty, abs=1e-6)


def test_a_fully_closed_position_does_not_linger(fills):
    """XLE was bought and sold in full; it must not appear as dust."""
    assert "XLE" not in L.positions_from_fills(fills)


def test_fills_are_ordered_oldest_first(fills):
    assert [f.on for f in fills] == sorted(f.on for f in fills)


# ---------------------------------------------------------------- basis

def test_cost_basis_tracks_fifo_lots(fills):
    """GLDM: bought 2.179363 at 91.7699, sold half. The remaining half was
    bought at the same price, so both measures agree here."""
    b = L.cost_basis(fills, "GLDM")
    assert b["quantity"] == pytest.approx(1.089682, abs=1e-6)
    assert b["average_cost"] == pytest.approx(91.7699, abs=1e-4)


def test_cost_basis_of_a_closed_position_is_empty(fills):
    assert L.cost_basis(fills, "XLE")["average_cost"] is None


def test_highest_lot_differs_from_average_when_lots_differ():
    """Why both numbers exist: FIFO can realise a loss on an expensive lot while
    the average still shows a gain."""
    f = [L.Fill("AAA", "buy", 1, 100.0, date(2026, 1, 1)),
         L.Fill("AAA", "buy", 1, 50.0, date(2026, 1, 2))]
    b = L.cost_basis(f, "AAA")
    assert b["average_cost"] == pytest.approx(75.0)
    assert b["highest_lot"] == pytest.approx(100.0)


# ---------------------------------------------------------------- loss sales

def test_the_two_real_loss_sales_are_found(fills):
    """These are the sales the first live run discovered were missing from the
    registry entirely. GLDM 91.77 -> 90.79 and XLE 63.01 -> 62.23."""
    found = {ls["symbol"]: ls for ls in L.loss_sales(fills)}
    assert set(found) == {"GLDM", "XLE"}
    assert found["GLDM"]["on"] == date(2026, 8, 27)
    assert found["XLE"]["on"] == date(2026, 8, 27)


def test_a_profitable_sale_is_not_a_loss_sale():
    f = [L.Fill("AAA", "buy", 1, 50.0, date(2026, 1, 1)),
         L.Fill("AAA", "sell", 1, 60.0, date(2026, 1, 2))]
    assert L.loss_sales(f) == []


def test_a_fifo_loss_counts_even_when_the_average_shows_a_gain():
    """Conservative by design: under-reporting a loss sale silently disallows a
    deduction the owner believes they have."""
    f = [L.Fill("AAA", "buy", 1, 100.0, date(2026, 1, 1)),
         L.Fill("AAA", "buy", 1, 20.0, date(2026, 1, 2)),
         L.Fill("AAA", "sell", 1, 70.0, date(2026, 1, 3))]     # avg 60, lot 100
    hits = L.loss_sales(f)
    assert len(hits) == 1 and hits[0]["basis"] == "fifo_lot"


def test_a_sale_with_no_prior_purchase_is_skipped():
    f = [L.Fill("AAA", "sell", 1, 70.0, date(2026, 1, 3))]
    assert L.loss_sales(f) == []


# ---------------------------------------------------------------- reconcile

def test_reconciliation_passes_when_positions_follow_from_fills(fills):
    assert L.reconcile_positions(BROKER_POSITIONS, fills) == {}


def test_reconciliation_tolerates_last_digit_noise(fills):
    """1e-6 was the old tolerance and is exactly the size of the rounding noise
    in a six-decimal broker payload, so it flagged positions that agreed."""
    jittered = {k: v + 5e-7 for k, v in BROKER_POSITIONS.items()}
    assert L.reconcile_positions(jittered, fills) == {}


def test_reconciliation_catches_a_holding_that_no_fill_explains(fills):
    sneaky = dict(BROKER_POSITIONS, AAPL=3.0)
    drift = L.reconcile_positions(sneaky, fills)
    assert "AAPL" in drift
    assert drift["AAPL"]["broker"] == 3.0
    assert "no fills" in drift["AAPL"]["likely"]


def test_reconciliation_catches_a_manual_sale_outside_the_system(fills):
    gone = {k: v for k, v in BROKER_POSITIONS.items() if k != "VGSH"}
    drift = L.reconcile_positions(gone, fills)
    assert "VGSH" in drift and "manual sale" in drift["VGSH"]["likely"]


# ---------------------------------------------------------------- journal

def _file(name, entries):
    return {"title": name, "content": json.dumps({"entries": entries})}


def test_journal_folds_dated_files_oldest_first():
    j = L.fold_journal([
        _file("journal-2026-09-02.json", [{"run_id": "b", "kind": "run", "payload": {"i": 2}}]),
        _file("journal-2026-09-01.json", [{"run_id": "a", "kind": "run", "payload": {"i": 1}}]),
    ])
    assert [r["i"] for r in j.runs] == [1, 2]


def test_a_second_run_on_one_day_does_not_overwrite_the_first():
    """The connector cannot overwrite, and a silently dropped run is worse than
    a duplicate."""
    assert L.journal_filename(date(2026, 9, 1)) == "journal-2026-09-01.json"
    assert L.journal_filename(date(2026, 9, 1), 1) == "journal-2026-09-01-1.json"
    j = L.fold_journal([
        _file("journal-2026-09-01-1.json", [{"run_id": "b", "kind": "run", "payload": {"i": 2}}]),
        _file("journal-2026-09-01.json", [{"run_id": "a", "kind": "run", "payload": {"i": 1}}]),
    ])
    assert [r["i"] for r in j.runs] == [1, 2]


def test_one_corrupt_file_does_not_destroy_the_whole_history():
    j = L.fold_journal([
        _file("journal-2026-09-01.json", [{"run_id": "a", "kind": "run", "payload": {"i": 1}}]),
        {"title": "journal-2026-09-02.json", "content": "{not json"},
    ])
    assert len(j.runs) == 1
    assert "journal-2026-09-02.json" in j.unreadable


def test_unrelated_files_in_the_folder_are_ignored():
    j = L.fold_journal([
        _file("journal-2026-09-01.json", [{"run_id": "a", "kind": "run", "payload": {}}]),
        {"title": "state.json", "content": "{}"},
        {"title": "run-manifest-2026-09-01.json", "content": "{}"},
    ])
    assert j.sources == ["journal-2026-09-01.json"]


def test_matured_theses_come_back_for_scoring():
    """A prediction nobody settles is a diary, not evidence."""
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t1", "opened": "2026-09-01", "horizon_days": 10}},
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t2", "opened": "2026-09-01", "horizon_days": 90}},
    ])])
    matured = [t["thesis_id"] for t in j.matured_theses(date(2026, 9, 11))]
    assert matured == ["t1"]
    assert [t["thesis_id"] for t in j.open_theses(date(2026, 9, 11))] == ["t2"]


def test_a_settled_thesis_is_not_offered_for_scoring_twice():
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t1", "opened": "2026-09-01", "horizon_days": 10}},
        {"run_id": "b", "kind": "close", "payload": {"thesis_id": "t1"}},
    ])])
    assert j.matured_theses(date(2026, 9, 30)) == []


def test_an_empty_folder_folds_to_an_empty_journal():
    j = L.fold_journal([])
    assert j.runs == [] and j.entries == [] and j.unreadable == []
