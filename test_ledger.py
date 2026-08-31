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


# ------------------------------------------------------------- split adjustment

def test_a_pre_split_fill_is_rescaled_to_current_share_terms():
    """A share bought the day before a 4-for-1 split is one pre-split share,
    not the four post-split shares it became. apply_splits re-expresses it."""
    ev = L.SplitEvent("XYZ", date(2026, 6, 10), 4.0)
    fills = [L.Fill("XYZ", "buy", 10.0, 400.0, date(2026, 6, 1), "o1")]
    out = L.apply_splits(fills, {"XYZ": [ev]})
    assert out[0].quantity == pytest.approx(40.0)
    assert out[0].price == pytest.approx(100.0)


def test_notional_value_is_preserved_by_a_split():
    """A split changes the share-count convention, not what was actually paid."""
    ev = L.SplitEvent("XYZ", date(2026, 6, 10), 4.0)
    fills = [L.Fill("XYZ", "buy", 7.0, 250.0, date(2026, 5, 1), "o1")]
    out = L.apply_splits(fills, {"XYZ": [ev]})
    before = fills[0].quantity * fills[0].price
    after = out[0].quantity * out[0].price
    assert after == pytest.approx(before)


def test_a_fill_on_or_after_the_effective_date_is_left_alone():
    ev = L.SplitEvent("XYZ", date(2026, 6, 10), 4.0)
    on_date = L.Fill("XYZ", "buy", 10.0, 100.0, date(2026, 6, 10), "o1")
    after = L.Fill("XYZ", "buy", 10.0, 100.0, date(2026, 6, 15), "o2")
    out = L.apply_splits([on_date, after], {"XYZ": [ev]})
    assert out[0].quantity == 10.0 and out[1].quantity == 10.0


def test_a_reverse_split_divides_shares_and_multiplies_price():
    """1-for-10: ratio 0.1, shares fall, price rises, notional unchanged."""
    ev = L.SplitEvent("XYZ", date(2026, 6, 10), 0.1)
    fills = [L.Fill("XYZ", "buy", 100.0, 5.0, date(2026, 6, 1), "o1")]
    out = L.apply_splits(fills, {"XYZ": [ev]})
    assert out[0].quantity == pytest.approx(10.0)
    assert out[0].price == pytest.approx(50.0)


def test_multiple_splits_on_one_symbol_compound_correctly():
    """A fill before BOTH a 4-for-1 and a later 2-for-1 picks up both factors
    (x8 total); a fill between them picks up only the second (x2)."""
    splits = {"XYZ": [
        L.SplitEvent("XYZ", date(2027, 1, 1), 2.0),   # deliberately out of
        L.SplitEvent("XYZ", date(2026, 6, 10), 4.0),  # chronological order --
    ]}                                                  # must not matter
    before_both = L.Fill("XYZ", "buy", 1.0, 800.0, date(2026, 1, 1), "o1")
    between = L.Fill("XYZ", "buy", 1.0, 200.0, date(2026, 8, 1), "o2")
    after_both = L.Fill("XYZ", "buy", 1.0, 100.0, date(2027, 2, 1), "o3")
    out = L.apply_splits([before_both, between, after_both], splits)
    assert out[0].quantity == pytest.approx(8.0)   # 1 * 4 * 2
    assert out[1].quantity == pytest.approx(2.0)   # 1 * 2 only
    assert out[2].quantity == pytest.approx(1.0)   # untouched


def test_a_symbol_with_no_split_data_passes_through_unchanged():
    """A deliberate no-op, not a silent skip: a caller that forgot to look up a
    symbol's splits gets a loud reconciliation failure, not a quiet wrong number."""
    fills = [L.Fill("ABC", "buy", 5.0, 50.0, date(2026, 1, 1), "o1")]
    out = L.apply_splits(fills, {"XYZ": [L.SplitEvent("XYZ", date(2026, 6, 10), 4.0)]})
    assert out[0].quantity == 5.0 and out[0].price == 50.0


def test_apply_splits_does_not_mutate_the_input_list():
    ev = L.SplitEvent("XYZ", date(2026, 6, 10), 4.0)
    original = [L.Fill("XYZ", "buy", 10.0, 400.0, date(2026, 6, 1), "o1")]
    snapshot = list(original)
    L.apply_splits(original, {"XYZ": [ev]})
    assert original == snapshot


def test_splits_from_api_parses_alpha_vantage_style_records():
    records = [
        {"effective_date": "2026-06-10", "split_factor": "4"},
        {"effective_date": "2025-01-05", "split_factor": "0.1"},
    ]
    events = L.splits_from_api("XYZ", records)
    assert len(events) == 2
    assert {e.ratio for e in events} == {4.0, 0.1}
    assert all(e.symbol == "XYZ" for e in events)


def test_splits_from_api_skips_malformed_records_rather_than_raising():
    records = [
        {"effective_date": "2026-06-10", "split_factor": "4"},
        {"effective_date": None, "split_factor": "4"},
        {"effective_date": "2026-01-01", "split_factor": "not-a-number"},
        {},
    ]
    events = L.splits_from_api("XYZ", records)
    assert len(events) == 1


def test_split_event_rejects_a_non_positive_ratio():
    with pytest.raises(ValueError, match="ratio"):
        L.SplitEvent("XYZ", date(2026, 6, 10), 0.0)
    with pytest.raises(ValueError, match="ratio"):
        L.SplitEvent("XYZ", date(2026, 6, 10), -2.0)


# ---------------------------------- the regression that mattered (31 Aug 2026)

def test_reconciliation_fails_without_split_adjustment_and_passes_with_it():
    """Mirrors the actual production failure on 31 August 2026: a real
    individual account's positions failed to reconcile against its own order
    history, and every one of the ten disagreeing symbols turned out to have
    split at some point in a multi-year history pulled with no date floor.
    The scenario here is synthetic (the real trade sizes are not put in a
    public test file), but the failure shape is exact: raw fill quantities
    from BEFORE a real split disagree with a broker snapshot taken AFTER it,
    and applying the split is what makes reconciliation possible at all."""
    # A position built from three years of raw fills, spanning a real 4-for-1
    # split. Bought 12 shares pre-split, bought 3 more post-split: broker shows
    # 12*4 + 3 = 51 shares today. Unadjusted fills sum to 12 + 3 = 15 -- looks
    # like a 36-share discrepancy that has nothing to do with any bad trade.
    fills = [
        L.Fill("NVDA", "buy", 12.0, 900.0, date(2023, 9, 12), "o1"),
        L.Fill("NVDA", "buy", 3.0, 130.0, date(2024, 8, 1), "o2"),
    ]
    broker_today = {"NVDA": 51.0}
    split = {"NVDA": [L.SplitEvent("NVDA", date(2024, 6, 10), 4.0)]}

    unadjusted_drift = L.reconcile_positions(broker_today, fills)
    assert "NVDA" in unadjusted_drift
    assert unadjusted_drift["NVDA"]["difference"] == pytest.approx(51.0 - 15.0)

    adjusted = L.apply_splits(fills, split)
    assert L.reconcile_positions(broker_today, adjusted) == {}


def test_a_symbol_that_never_split_still_reconciles_normally_in_a_mixed_batch():
    """Splits are applied per-symbol; a stock that never split in the same
    batch as one that did must not be affected."""
    fills = [
        L.Fill("NVDA", "buy", 12.0, 900.0, date(2023, 9, 12), "o1"),
        L.Fill("SGOV", "buy", 5.0, 100.0, date(2023, 9, 12), "o2"),
    ]
    splits = {"NVDA": [L.SplitEvent("NVDA", date(2024, 6, 10), 4.0)]}
    out = L.apply_splits(fills, splits)
    sgov = next(f for f in out if f.symbol == "SGOV")
    assert sgov.quantity == 5.0 and sgov.price == 100.0


def test_unadjusted_splits_corrupt_cost_basis_and_loss_sale_detection():
    """Worse than a reconciliation failure: on a synthetic NVDA-shaped position
    (buy pre-split, sell post-split), unadjusted FIFO thinks the position is
    fully closed when 28 shares remain, and reports the average cost off by
    exactly the split factor. Split-adjusting fixes both."""
    fills = [
        L.Fill("NVDA", "buy", 12.0, 900.0, date(2023, 9, 12), "o1"),
        L.Fill("NVDA", "sell", 20.0, 130.0, date(2024, 8, 1), "o2"),
    ]
    unadjusted = L.cost_basis(fills, "NVDA")
    assert unadjusted["quantity"] == 0.0          # WRONG: 28 shares actually remain

    split = {"NVDA": [L.SplitEvent("NVDA", date(2024, 6, 10), 4.0)]}
    adjusted = L.cost_basis(L.apply_splits(fills, split), "NVDA")
    assert adjusted["quantity"] == pytest.approx(28.0)
    assert adjusted["average_cost"] == pytest.approx(225.0)   # 900 / 4
