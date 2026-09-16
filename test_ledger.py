"""Tests for the rebuilt-from-broker ledger.

The order fixture is REAL: it is the agentic account's actual order history as
the broker returned it on 31 August 2026, including the cancelled SGOV stop that
blocked a correctly-sized sell. Synthetic fixtures agree with whatever the code
assumes; real payloads carry the fields and states that actually turn up.
"""

import json
from datetime import date, timedelta

import pytest

import ledger as L
import runlog as R


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


def test_all_traded_symbols_includes_a_fully_closed_position(fills):
    """The Stage 0 step 7 split-check list -- and, by the same reasoning, the
    wash-sale registry's input -- must not be built from held positions.
    XLE nets to zero here (see the test above) and would be silently
    excluded by a positions-derived list, even though its own loss or gain
    sale still needs its split history and still belongs to the registry.
    Confirmed 5 September 2026: a positions-derived split-check list missed
    MRVL, CRM, and TSLA -- all fully sold that day -- for exactly this
    reason (see PROCEDURE_RATIONALE.md)."""
    assert L.all_traded_symbols(fills) == ["GLDM", "SGOV", "VGSH", "XLE"]
    assert "XLE" not in L.positions_from_fills(fills)  # closed, but still traded


def test_all_traded_symbols_is_upper_cased_and_deduplicated():
    mixed = [
        L.Fill("nflx", "buy", 1.0, 100.0, date(2026, 1, 1), "a"),
        L.Fill("NFLX", "sell", 1.0, 110.0, date(2026, 1, 5), "b"),
        L.Fill("VTI", "buy", 1.0, 300.0, date(2026, 1, 2), "c"),
    ]
    assert L.all_traded_symbols(mixed) == ["NFLX", "VTI"]


def test_all_traded_symbols_of_no_fills_is_empty():
    assert L.all_traded_symbols([]) == []


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


# ---------------------------------------------------- closed_for_scoring

def test_closed_for_scoring_joins_outcome_and_thesis_by_id():
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t1", "opened": "2026-09-01", "horizon_days": 21}},
        {"run_id": "b", "kind": "outcome",
         "payload": {"thesis_id": "t1", "excess_pct": 4.2, "thesis_played_out": True}},
    ])])
    closed = j.closed_for_scoring()
    assert closed == [{"outcome_pct": 4.2, "thesis_played_out": True, "horizon_days": 21}]


def test_closed_for_scoring_skips_an_outcome_with_no_matching_thesis():
    """Should not happen -- the journal is append-only and a "close"/
    "outcome" pair is only ever written for an already-recorded thesis --
    but must not crash or fabricate a horizon if it does."""
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "b", "kind": "outcome",
         "payload": {"thesis_id": "ghost", "excess_pct": 4.2}},
    ])])
    assert j.closed_for_scoring() == []


def test_closed_for_scoring_skips_an_outcome_missing_excess_pct():
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t1", "opened": "2026-09-01", "horizon_days": 21}},
        {"run_id": "b", "kind": "outcome", "payload": {"thesis_id": "t1"}},
    ])])
    assert j.closed_for_scoring() == []


def test_closed_for_scoring_includes_extra_outcomes_not_yet_journaled():
    """The same "journal plus this run's fresh scores" pattern Stage 0.5
    already uses for evidence.assess -- a thesis maturing THIS run must
    count toward the prior-day review immediately, not next run."""
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": "t1", "opened": "2026-08-01", "horizon_days": 30}},
    ])])
    closed = j.closed_for_scoring(extra_outcomes=[
        {"thesis_id": "t1", "excess_pct": -1.5, "thesis_played_out": False},
    ])
    assert closed == [{"outcome_pct": -1.5, "thesis_played_out": False, "horizon_days": 30}]


def test_closed_for_scoring_feeds_score_closed_decisions_directly():
    """The guarantee that matters: the joined shape is directly consumable
    by runlog.score_closed_decisions, not just structurally similar."""
    entries = [
        {"run_id": "a", "kind": "thesis",
         "payload": {"thesis_id": f"t{i}", "opened": "2026-08-01", "horizon_days": 20}}
        for i in range(35)
    ] + [
        {"run_id": "b", "kind": "outcome",
         "payload": {"thesis_id": f"t{i}", "excess_pct": 2.0 if i % 2 == 0 else -1.0,
                    "thesis_played_out": True}}
        for i in range(35)
    ]
    j = L.fold_journal([_file("journal-2026-09-01.json", entries)])
    result = R.score_closed_decisions(j.closed_for_scoring())
    assert result["n"] == 35
    assert result["statistically_meaningful"] == "no"  # under 100, per score_closed_decisions


# ------------------------------------------------------------ run_entry

def _run_log_with(*, health="nominal", duration_ms=1234,
                  decisions=(), stages=()):
    log = R.RunLog("run-x")
    for d in decisions:
        log.decide(R.Decision(**d))
    for s in stages:
        log.stages.append(R.Stage(name=s["name"], started_at="t",
                                  duration_ms=s["duration_ms"], ok=True))
    # health/duration_ms are derived on RunLog, not settable directly;
    # patch the manifest after the fact so tests can pin an exact value
    # without needing a real failing check to produce "degraded"/"critical".
    m = log.manifest()
    m["health"] = health
    m["duration_ms"] = duration_ms
    return m  # run_entry accepts a plain manifest dict just as well as a RunLog


def test_run_entry_pins_exactly_the_fields_find_optimizations_reads():
    manifest = _run_log_with(decisions=[
        {"symbol": "OXY", "action": "hold", "account": "agentic",
         "executed": False, "reason": "no change"},
    ], stages=[{"name": "gather", "duration_ms": 500}])
    entry = L.run_entry(manifest)
    assert set(entry.keys()) == set(L.RUN_ENTRY_SCHEMA_FIELDS)
    assert entry["health"] == "nominal"
    assert entry["duration_ms"] == 1234
    assert entry["decisions"][0]["symbol"] == "OXY"
    assert entry["stages"][0]["name"] == "gather"


def test_run_entry_accepts_a_real_runlog_via_manifest():
    log = R.RunLog("run-y")
    log.decide(R.Decision("OXY", "hold", "agentic", False, "no change"))
    entry = L.run_entry(log)
    assert entry["run_id"] == "run-y"
    assert entry["decisions"][0]["symbol"] == "OXY"


def test_run_entry_defaults_missing_fields_rather_than_raising():
    entry = L.run_entry({})
    assert entry == {"run_id": "", "health": "", "duration_ms": 0,
                     "decisions": [], "stages": [], "brokerage_ok": None}


def test_run_entry_round_trips_through_the_journal_into_find_optimizations():
    """The guarantee that actually matters: a run_entry payload, folded
    back out of the journal exactly as DAILY_PROCEDURE.md's Stage 6 write
    and Stage 0 step 9 read would do it, must be directly consumable by
    runlog.find_optimizations -- proving the pinned schema and the reader
    actually agree, not just that both exist."""
    # Ten runs dominated by one rejected gate -- the exact pattern
    # find_optimizations looks for (see runlog.py "gate_balance").
    entries = []
    for i in range(10):
        manifest = _run_log_with(decisions=[
            {"symbol": "OXY", "action": "reject", "account": "agentic",
             "executed": False, "reason": "failed gate",
             "gate_failed": "two_sources"},
        ])
        entries.append(_file(f"journal-2026-08-{i+1:02d}.json",
            [{"run_id": f"r{i}", "kind": "run", "payload": L.run_entry(manifest)}]))

    journal = L.fold_journal(entries)
    findings = R.find_optimizations(journal.runs)
    assert any(f["kind"] == "gate_balance" for f in findings)


# ------------------------------------------------------------ brokerage_ok

def test_run_entry_brokerage_ok_true_when_every_robinhood_call_succeeded():
    manifest = {"calls": [{"service": "robinhood", "operation": "get_accounts", "ok": True},
                          {"service": "robinhood", "operation": "get_portfolio", "ok": True},
                          {"service": "alpha_vantage", "operation": "MARKET_STATUS", "ok": True}]}
    assert L.run_entry(manifest)["brokerage_ok"] is True


def test_run_entry_brokerage_ok_false_when_any_robinhood_call_failed():
    manifest = {"calls": [{"service": "robinhood", "operation": "get_accounts", "ok": True},
                          {"service": "robinhood", "operation": "get_portfolio", "ok": False}]}
    assert L.run_entry(manifest)["brokerage_ok"] is False


def test_run_entry_brokerage_ok_none_when_no_robinhood_call_made():
    """An abort before Stage 0 step 3 (tools_available) never reaches a
    brokerage call at all -- distinct from a call that was made and
    failed."""
    manifest = {"calls": [{"service": "alpha_vantage", "operation": "MARKET_STATUS", "ok": True}]}
    assert L.run_entry(manifest)["brokerage_ok"] is None


def test_run_entry_brokerage_ok_ignores_service_name_case():
    manifest = {"calls": [{"service": "Robinhood", "operation": "get_accounts", "ok": True}]}
    assert L.run_entry(manifest)["brokerage_ok"] is True


# ------------------------------------------ days_since_last_brokerage_success

def test_no_run_entries_means_no_brokerage_history():
    j = L.fold_journal([_file("journal-2026-09-01.json", [])])
    assert j.days_since_last_brokerage_success(date(2026, 9, 5)) is None


def test_days_since_last_brokerage_success_counts_from_the_most_recent_true():
    j = L.fold_journal([
        _file("journal-2026-09-01.json",
              [{"run_id": "a", "kind": "run", "on": "2026-09-01", "payload": {"brokerage_ok": True}}]),
        _file("journal-2026-09-04.json",
              [{"run_id": "b", "kind": "run", "on": "2026-09-04", "payload": {"brokerage_ok": True}}]),
    ])
    assert j.days_since_last_brokerage_success(date(2026, 9, 5)) == 1


def test_days_since_last_brokerage_success_ignores_false_and_none():
    """A failed or skipped brokerage call is not evidence the token still
    works -- it must not reset the count back to zero."""
    j = L.fold_journal([
        _file("journal-2026-09-01.json",
              [{"run_id": "a", "kind": "run", "on": "2026-09-01", "payload": {"brokerage_ok": True}}]),
        _file("journal-2026-09-04.json",
              [{"run_id": "b", "kind": "run", "on": "2026-09-04", "payload": {"brokerage_ok": False}}]),
        _file("journal-2026-09-05.json",
              [{"run_id": "c", "kind": "run", "on": "2026-09-05", "payload": {"brokerage_ok": None}}]),
    ])
    assert j.days_since_last_brokerage_success(date(2026, 9, 5)) == 4


# ------------------------------------------------------- journal compaction

def _daily_files_for_august():
    return [
        _file("journal-2026-08-01.json", [{"run_id": "a1", "kind": "run", "payload": {"i": 1}}]),
        _file("journal-2026-08-15.json", [{"run_id": "a2", "kind": "run", "payload": {"i": 2}}]),
        _file("journal-2026-08-15-1.json", [{"run_id": "a3", "kind": "run", "payload": {"i": 3}}]),
        _file("journal-2026-08-31.json", [{"run_id": "a4", "kind": "run", "payload": {"i": 4}}]),
    ]


def test_journal_monthly_filename_convention():
    assert L.journal_monthly_filename(2026, 8) == "journal-monthly-2026-08.json"
    assert L.journal_monthly_filename(2026, 8, 1) == "journal-monthly-2026-08-1.json"


def test_monthly_filename_regex_never_matches_a_daily_filename():
    """The exact collision this naming scheme exists to avoid: a daily
    file's day component must never be misread as a monthly file's
    sequence number."""
    assert L.MONTHLY_JOURNAL_RE.match("journal-2026-09-01.json") is None
    assert L.MONTHLY_JOURNAL_RE.match("journal-2026-09-01-1.json") is None
    assert L.JOURNAL_RE.match("journal-monthly-2026-09.json") is None


def test_compact_journal_month_is_entry_for_entry_equivalent_to_the_daily_fold():
    """The equivalence the whole feature depends on: compacting must not
    change what a fold produces, only how many files it has to read."""
    daily = _daily_files_for_august()
    direct = L.fold_journal(daily)
    compacted_body = L.compact_journal_month(daily)
    compacted = L.fold_journal([{"title": "journal-monthly-2026-08.json",
                                 "content": json.dumps(compacted_body)}])
    assert [e.to_dict() for e in compacted.entries] == [e.to_dict() for e in direct.entries]


def test_compact_journal_month_rejects_files_spanning_multiple_months():
    with pytest.raises(ValueError, match="multiple months"):
        L.compact_journal_month(_daily_files_for_august() + [
            _file("journal-2026-09-01.json", [{"run_id": "s", "kind": "run", "payload": {}}]),
        ])


def test_compact_journal_month_ignores_unrelated_files():
    body = L.compact_journal_month(_daily_files_for_august() + [
        {"title": "state.json", "content": "{}"},
    ])
    assert len(body["entries"]) == 4


def test_fold_journal_prefers_the_monthly_file_over_leftover_daily_files():
    """The connector cannot delete the old daily files once compacted --
    fold_journal must still treat the monthly file as authoritative and
    skip them, not double-count."""
    daily = _daily_files_for_august()
    compacted_body = L.compact_journal_month(daily)
    monthly_file = {"title": "journal-monthly-2026-08.json", "content": json.dumps(compacted_body)}

    mixed = L.fold_journal(daily + [monthly_file])
    monthly_only = L.fold_journal([monthly_file])
    daily_only = L.fold_journal(daily)

    assert [e.to_dict() for e in mixed.entries] == [e.to_dict() for e in monthly_only.entries]
    assert [e.to_dict() for e in mixed.entries] == [e.to_dict() for e in daily_only.entries]
    assert len(mixed.entries) == 4  # not 8 -- the daily files were not double-counted


def test_fold_journal_leaves_other_months_daily_files_alone():
    """Compacting August must not touch September's still-accumulating
    daily files."""
    august_daily = _daily_files_for_august()
    monthly_file = {"title": "journal-monthly-2026-08.json",
                    "content": json.dumps(L.compact_journal_month(august_daily))}
    september_daily = _file("journal-2026-09-01.json",
                            [{"run_id": "s1", "kind": "run", "payload": {"i": 5}}])

    j = L.fold_journal([monthly_file, september_daily])
    assert [r["i"] for r in j.runs] == [1, 2, 3, 4, 5]


def test_fold_journal_sources_reports_the_monthly_file_not_the_daily_ones():
    monthly_file = {"title": "journal-monthly-2026-08.json",
                    "content": json.dumps(L.compact_journal_month(_daily_files_for_august()))}
    j = L.fold_journal(_daily_files_for_august() + [monthly_file])
    assert j.sources == ["journal-monthly-2026-08.json"]


def test_month_is_compactable_rejects_the_current_month():
    assert L.month_is_compactable(2026, 9, today=date(2026, 9, 4)) is False


def test_month_is_compactable_accepts_a_past_month():
    assert L.month_is_compactable(2026, 8, today=date(2026, 9, 4)) is True


def test_month_is_compactable_rejects_a_future_month():
    assert L.month_is_compactable(2026, 10, today=date(2026, 9, 4)) is False


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


# --------------------------- the regression that mattered (1 September 2026)

def test_a_partially_filled_rest_cancelled_order_still_counts_its_fill():
    """The real FIG defect: the state allow-list excluded a genuine execution
    because its terminal state was 'partially_filled_rest_cancelled', a state
    that was never on the list. cumulative_quantity already says authoritatively
    what executed; the label on the unfilled remainder is not this function's
    concern."""
    orders = [{"id": "fig1", "symbol": "FIG", "side": "buy",
              "state": "partially_filled_rest_cancelled",
              "quantity": "5.000000", "cumulative_quantity": "1.000000",
              "average_price": "33.00", "created_at": "2025-07-24T14:00:00Z",
              "last_transaction_at": "2025-07-24T14:00:05Z"}]
    fills = L.fills_from_orders(orders)
    assert len(fills) == 1
    assert fills[0].quantity == pytest.approx(1.0)
    assert fills[0].price == pytest.approx(33.0)


def test_any_terminal_state_counts_if_cumulative_quantity_is_positive():
    """Not an allow-list of known-good state strings -- any state at all, as
    long as something actually executed. A broker can introduce a new terminal
    state name at any time; this must not require updating a list to match."""
    orders = [{"id": "o1", "symbol": "AAA", "side": "buy",
              "state": "some_future_state_that_does_not_exist_yet",
              "cumulative_quantity": "3.000000", "average_price": "10.00",
              "created_at": "2026-01-01T00:00:00Z"}]
    fills = L.fills_from_orders(orders)
    assert len(fills) == 1 and fills[0].quantity == pytest.approx(3.0)


def test_a_state_with_zero_cumulative_quantity_still_contributes_nothing():
    """The other direction must still hold: a cancelled order with nothing
    executed contributes no fill, regardless of what its state string is."""
    orders = [{"id": "o1", "symbol": "AAA", "side": "buy", "state": "cancelled",
              "cumulative_quantity": "0.000000", "average_price": "10.00",
              "created_at": "2026-01-01T00:00:00Z"}]
    assert L.fills_from_orders(orders) == []


def test_opening_balance_resolves_a_residual_fills_can_never_explain():
    """MBGL: 4.316287 shares sold with no matching buy anywhere in history --
    they arrived outside the order book. positions_from_fills alone reports a
    negative phantom position; the recorded opening balance corrects it."""
    fills = [L.Fill("MBGL", "sell", 4.316287, 19.93, date(2026, 8, 24), "o1")]
    without = L.positions_from_fills(fills)
    assert without["MBGL"] == pytest.approx(-4.316287)

    with_balance = L.positions_from_fills(fills, opening_balances={"MBGL": 4.316287})
    assert "MBGL" not in with_balance   # nets to zero, matching the broker's flat position


def test_reconciliation_still_aborts_on_an_unrecorded_residual():
    """The mechanism must not become a way to make reconciliation silently pass
    for anything unexplained -- only for a residual a human has actually
    recorded a reason for."""
    fills = [L.Fill("MBGL", "sell", 4.316287, 19.93, date(2026, 8, 24), "o1")]
    assert "MBGL" in L.reconcile_positions({"MBGL": 0.0}, fills)


def test_reconciliation_passes_once_the_opening_balance_is_recorded():
    fills = [L.Fill("MBGL", "sell", 4.316287, 19.93, date(2026, 8, 24), "o1")]
    drift = L.reconcile_positions({"MBGL": 0.0}, fills,
                                  opening_balances={"MBGL": 4.316287})
    assert drift == {}


def test_a_pre_history_holding_is_the_same_mechanism_as_an_off_book_transfer():
    """MSFT: the account already held shares before the earliest order the API
    will return. Economically identical to MBGL's off-book transfer -- both are
    a fact about history predating what fills can see -- so the same mechanism
    covers it without a second code path."""
    fills = [
        L.Fill("MSFT", "buy", 5.0, 250.0, date(2022, 7, 1), "o1"),
        L.Fill("MSFT", "sell", 0.021388, 250.0, date(2022, 12, 3), "o2"),
    ]
    broker = {"MSFT": 4.978612 + 2.0}          # broker holds 2.0 more than fills alone imply
    drift = L.reconcile_positions(broker, fills, opening_balances={"MSFT": 2.0})
    assert drift == {}


# -------------------------------------------- journal.opening_balances

def test_journal_folds_opening_balance_entries_into_a_symbol_map():
    j = L.fold_journal([_file("journal-2026-09-01.json", [
        {"run_id": "a", "kind": "opening_balance",
         "payload": {"symbol": "MBGL", "quantity": 4.316287,
                     "recorded_on": "2026-09-01",
                     "reason": "shares sold 2026-08-24 with no matching buy in history"}},
        {"run_id": "a", "kind": "opening_balance",
         "payload": {"symbol": "MSFT", "quantity": 2.0,
                     "recorded_on": "2026-09-01",
                     "reason": "pre-history holding; order history starts 2022-06-22"}},
    ])])
    assert j.opening_balances == {"MBGL": 4.316287, "MSFT": 2.0}


def test_a_later_opening_balance_entry_corrects_an_earlier_one_for_the_same_symbol():
    """Meant to be recorded once and rarely revised, not accumulated -- a
    correction replaces, it does not add to, the earlier value."""
    j = L.fold_journal([
        _file("journal-2026-09-01.json", [
            {"run_id": "a", "kind": "opening_balance",
             "payload": {"symbol": "MBGL", "quantity": 4.0}},
        ]),
        _file("journal-2026-09-02.json", [
            {"run_id": "b", "kind": "opening_balance",
             "payload": {"symbol": "MBGL", "quantity": 4.316287}},
        ]),
    ])
    assert j.opening_balances == {"MBGL": 4.316287}


def test_no_opening_balance_entries_gives_an_empty_map_not_an_error():
    j = L.fold_journal([_file("journal-2026-09-01.json",
                              [{"run_id": "a", "kind": "run", "payload": {}}])])
    assert j.opening_balances == {}


# ---------------------------------------------------- standing circuit breaker

def test_no_circuit_breaker_entries_means_clear():
    j = L.fold_journal([_file("journal-2026-09-01.json",
                              [{"run_id": "a", "kind": "run", "payload": {}}])])
    assert j.standing_circuit_breaker is None


def test_an_unresolved_trip_is_standing():
    j = L.fold_journal([_file("journal-2026-09-02.json", [
        {"run_id": "b", "kind": "circuit_breaker_tripped",
         "payload": {"reason": "equity below hard stop"}},
    ])])
    assert j.standing_circuit_breaker == {"reason": "equity below hard stop"}


def test_a_clear_after_a_trip_resolves_it():
    j = L.fold_journal([
        _file("journal-2026-09-02.json", [
            {"run_id": "b", "kind": "circuit_breaker_tripped",
             "payload": {"reason": "equity below hard stop"}},
        ]),
        _file("journal-2026-09-05.json", [
            {"run_id": "c", "kind": "circuit_breaker_cleared",
             "payload": {"reason": "reviewed, cause identified and fixed"}},
        ]),
    ])
    assert j.standing_circuit_breaker is None


def test_a_second_trip_after_a_clear_is_standing_again():
    """Clearing one trip does not grant blanket immunity -- a fresh trip after
    a clear must still halt the run."""
    j = L.fold_journal([
        _file("journal-2026-09-02.json", [
            {"run_id": "b", "kind": "circuit_breaker_tripped",
             "payload": {"reason": "first trip"}},
        ]),
        _file("journal-2026-09-05.json", [
            {"run_id": "c", "kind": "circuit_breaker_cleared", "payload": {}},
        ]),
        _file("journal-2026-09-10.json", [
            {"run_id": "d", "kind": "circuit_breaker_tripped",
             "payload": {"reason": "second trip"}},
        ]),
    ])
    assert j.standing_circuit_breaker == {"reason": "second trip"}


# ------------------------------------------------------ latest washsale report

def test_no_washsale_report_entries_gives_none():
    j = L.fold_journal([_file("journal-2026-09-01.json",
                              [{"run_id": "a", "kind": "run", "payload": {}}])])
    assert j.latest_washsale_report is None


def test_latest_washsale_report_is_the_most_recent():
    """Oldest-first fold means the last matching entry across ALL files is the
    most recent run's actual registry output -- not a hand-written summary of
    it (see washsale.Registry.report)."""
    j = L.fold_journal([
        _file("journal-2026-09-03.json", [
            {"run_id": "a", "kind": "washsale_report",
             "payload": {"asof": "2026-09-03", "blocked": {"XLE": {"severity": "block"}}}},
        ]),
        _file("journal-2026-09-04.json", [
            {"run_id": "b", "kind": "washsale_report",
             "payload": {"asof": "2026-09-04",
                        "blocked": {"GLDM": {"severity": "block"},
                                    "XLE": {"severity": "block"}}}},
        ]),
    ])
    assert j.latest_washsale_report == {
        "asof": "2026-09-04",
        "blocked": {"GLDM": {"severity": "block"}, "XLE": {"severity": "block"}},
    }


# --------------------------------------------------------------------------
# fills and splits caching -- never positions
# --------------------------------------------------------------------------

def _cache_file(name, rows):
    return {"title": name, "content": json.dumps(rows)}


def _fill_row(symbol, side, qty, price, on, order_id="", account="IND1"):
    return {"symbol": symbol, "side": side, "quantity": qty, "price": price,
            "on": on, "order_id": order_id, "account": account}


def test_fills_cache_filename_matches_journal_convention():
    assert L.fills_cache_filename(date(2026, 9, 1)) == "fills-cache-2026-09-01.json"
    assert L.fills_cache_filename(date(2026, 9, 1), 1) == "fills-cache-2026-09-01-1.json"


def test_fold_fills_cache_merges_and_sorts_across_dated_files():
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")]),
        _cache_file("fills-cache-2026-08-25.json",
            [_fill_row("OXY", "sell", 3, 50.0, "2026-08-22", "o2")]),
    ])
    assert bad == []
    assert [f.order_id for f in fills] == ["o1", "o2"]
    assert fills[0].on == date(2026, 8, 18)


def test_fold_fills_cache_dedupes_on_order_id():
    """The same order_id appearing in two cache files (should not happen in
    practice, but must not double-count a position if it does) keeps one
    fill, not two."""
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")]),
        _cache_file("fills-cache-2026-08-21.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")]),
    ])
    assert bad == []
    assert len(fills) == 1


def test_fold_fills_cache_dedupes_fills_with_no_order_id_by_composite_key():
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18")]),
        _cache_file("fills-cache-2026-08-21.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18")]),
    ])
    assert len(fills) == 1


def test_fold_fills_cache_ignores_non_matching_titles():
    fills, bad = L.fold_fills_cache([
        _cache_file("journal-2026-08-20.json", [{"entries": []}]),
        _cache_file("fills-cache-2026-08-21.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")]),
    ])
    assert len(fills) == 1


def test_fold_fills_cache_records_unreadable_content_rather_than_crashing():
    fills, bad = L.fold_fills_cache([
        {"title": "fills-cache-2026-08-20.json", "content": "not json"},
        _cache_file("fills-cache-2026-08-21.json",
            [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")]),
    ])
    assert len(fills) == 1
    assert len(bad) == 1


def test_fold_fills_cache_records_a_malformed_row_rather_than_crashing():
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json", [
            _fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1"),
            {"symbol": "OXY"},  # missing required fields
        ]),
    ])
    assert len(fills) == 1
    assert len(bad) == 1


def test_fold_fills_cache_empty_input():
    fills, bad = L.fold_fills_cache([])
    assert fills == [] and bad == []


# --- the account round-trip, 9 September 2026 -----------------------------
# The 9 September scheduled run aborted in Stage 0 step 7 having refused to
# write its fills cache: `Fill` carried no account, so a cache folded from
# both accounts' rows could not say which account any fill belonged to, and
# per-account reconciliation would have compared a merged total against a
# single account's broker snapshot. These pin the round trip end to end.

def test_fills_from_orders_stamps_the_account_it_was_given():
    orders = [{"symbol": "SGOV", "side": "buy", "cumulative_quantity": "4.421802",
               "average_price": "100.0", "last_transaction_at": "2026-08-01T14:00:00Z",
               "id": "o1"}]
    assert L.fills_from_orders(orders, "AGENTIC1")[0].account == "AGENTIC1"
    assert L.fills_from_orders(orders)[0].account == ""


def test_fold_fills_cache_round_trips_the_account_through_the_cache():
    """The defect itself: fills for two accounts share one cache file, and
    each must come back out attributed to the account that executed it."""
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json", [
            _fill_row("SGOV", "buy", 29.805637, 100.0, "2026-08-18", "o1",
                      account="IND1"),
            _fill_row("SGOV", "buy", 4.421802, 100.0, "2026-08-18", "o2",
                      account="AGENTIC1"),
        ]),
    ])
    assert bad == []
    assert {f.order_id: f.account for f in fills} == {"o1": "IND1", "o2": "AGENTIC1"}


def test_fills_for_account_splits_a_merged_cache_back_apart():
    """The live 9 September numbers: a merged, account-less SGOV total of
    34.227439 reconciles against neither account's broker snapshot."""
    fills, _ = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json", [
            _fill_row("SGOV", "buy", 29.805637, 100.0, "2026-08-18", "o1",
                      account="IND1"),
            _fill_row("SGOV", "buy", 4.421802, 100.0, "2026-08-18", "o2",
                      account="AGENTIC1"),
        ]),
    ])
    assert L.positions_from_fills(fills)["SGOV"] == pytest.approx(34.227439)
    ind = L.positions_from_fills(L.fills_for_account(fills, "IND1"))
    agentic = L.positions_from_fills(L.fills_for_account(fills, "AGENTIC1"))
    assert ind["SGOV"] == pytest.approx(29.805637)
    assert agentic["SGOV"] == pytest.approx(4.421802)


def test_fold_fills_cache_rejects_a_row_with_no_account():
    """Strict on purpose: an unattributable fill must stop the run loudly via
    `bad` -> preflight, never fold into a total that belongs to neither
    account."""
    row = _fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1")
    row.pop("account")
    blank = _fill_row("OXY", "buy", 1, 50.0, "2026-08-18", "o2", account="")
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json", [
            row, blank,
            _fill_row("VTI", "buy", 1, 300.0, "2026-08-18", "o3", account="IND1"),
        ]),
    ])
    assert [f.order_id for f in fills] == ["o3"]
    assert len(bad) == 2


def test_fold_fills_cache_dedup_key_does_not_collapse_across_accounts():
    """Two accounts could each carry a fill with no order id and otherwise
    identical fields; the composite fallback key must keep both."""
    fills, bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json", [
            _fill_row("XOM", "buy", 10, 100.0, "2026-08-18", account="IND1"),
            _fill_row("XOM", "buy", 10, 100.0, "2026-08-18", account="AGENTIC1"),
        ]),
    ])
    assert bad == []
    assert len(fills) == 2


def test_apply_splits_preserves_the_account():
    """Split adjustment rebuilds every Fill it touches; dropping the account
    there would lose the attribution one step after the fold recovered it."""
    fills = [L.Fill("NVDA", "buy", 12.0, 900.0, date(2023, 9, 12), "o1", "IND1")]
    adjusted = L.apply_splits(fills, {
        "NVDA": [L.SplitEvent("NVDA", date(2024, 6, 10), 10.0)]})
    assert adjusted[0].account == "IND1"
    assert adjusted[0].quantity == pytest.approx(120.0)


def test_fills_ready_to_cache_preserves_the_account():
    """The cache write path itself: what goes out must still carry what the
    fold will require on the way back in."""
    old = L.Fill("XOM", "buy", 1.0, 100.0, date(2026, 8, 1), "o1", "AGENTIC1")
    kept = L.fills_ready_to_cache([old], today=date(2026, 9, 9))
    assert [f.account for f in kept] == ["AGENTIC1"]


def test_fills_cache_watermark_none_when_nothing_cached():
    assert L.fills_cache_watermark([]) is None


def test_fills_cache_watermark_is_day_after_newest_cached_fill():
    cached = [L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 18), "o1"),
             L.Fill("OXY", "sell", 3, 50.0, date(2026, 8, 22), "o2")]
    assert L.fills_cache_watermark(cached) == date(2026, 8, 23)


def test_fills_ready_to_cache_excludes_the_mutable_horizon_window():
    today = date(2026, 9, 4)
    fresh = [
        L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 20), "old"),   # 15 days old
        L.Fill("OXY", "buy", 5, 60.0, date(2026, 9, 1), "recent"),   # 3 days old
    ]
    ready = L.fills_ready_to_cache(fresh, today=today)
    assert [f.order_id for f in ready] == ["old"]


def test_fills_ready_to_cache_boundary_is_exclusive():
    """A fill exactly `horizon_days` old is still inside the mutable window
    -- caching it one day too early is exactly the bug this boundary exists
    to prevent, so the boundary itself must not be cached yet."""
    today = date(2026, 9, 4)
    boundary_fill = L.Fill("XOM", "buy", 10, 100.0, today - timedelta(days=7), "boundary")
    assert L.fills_ready_to_cache([boundary_fill], today=today) == []


def test_fills_cache_round_trip_matches_a_direct_fetch():
    """The whole point: caching older fills and re-fetching only the
    mutable window must reconstruct the exact same fill set (and therefore
    the exact same positions) as fetching everything fresh every time."""
    all_fills = L.fills_from_orders(ORDERS)
    today = date(2026, 9, 4)

    # Simulate: everything older than the horizon is already cached.
    already_cached = L.fills_ready_to_cache(all_fills, today=today)
    cache_files = [_cache_file("fills-cache-2026-08-20.json",
        [_fill_row(f.symbol, f.side, f.quantity, f.price, f.on.isoformat(), f.order_id)
         for f in already_cached])]
    cached, bad = L.fold_fills_cache(cache_files)
    assert bad == []

    # The next run re-fetches everything from the watermark forward (here,
    # simulated by just re-using the same real order fixture -- a real
    # fetch would pass fills_cache_watermark(cached) as created_at_gte).
    fresh_since_watermark = [f for f in all_fills if f.on >= L.fills_cache_watermark(cached)]
    reconstructed = sorted(set(cached) | set(fresh_since_watermark), key=lambda f: (f.on, f.order_id))
    direct = sorted(all_fills, key=lambda f: (f.on, f.order_id))
    assert L.positions_from_fills(reconstructed) == L.positions_from_fills(direct)


def test_splits_cache_filename_matches_convention():
    assert L.splits_cache_filename(date(2026, 9, 1)) == "splits-cache-2026-09-01.json"
    assert L.splits_cache_filename(date(2026, 9, 1), 2) == "splits-cache-2026-09-01-2.json"


def _splits_row(symbol, checked_through, splits):
    return {"symbol": symbol, "checked_through": checked_through, "splits": splits}


def test_fold_splits_cache_keeps_the_latest_check_per_symbol():
    cache, bad = L.fold_splits_cache([
        _cache_file("splits-cache-2026-08-20.json",
            [_splits_row("NVDA", "2026-08-20", [])]),
        _cache_file("splits-cache-2026-08-27.json",
            [_splits_row("NVDA", "2026-08-27",
                [{"effective_date": "2026-06-10", "ratio": 4.0}])]),
    ])
    assert bad == []
    assert cache["NVDA"].checked_through == date(2026, 8, 27)
    assert cache["NVDA"].splits == [L.SplitEvent("NVDA", date(2026, 6, 10), 4.0)]


def test_fold_splits_cache_tracks_multiple_symbols_independently():
    cache, bad = L.fold_splits_cache([
        _cache_file("splits-cache-2026-08-20.json", [
            _splits_row("NVDA", "2026-08-20", []),
            _splits_row("CMG", "2026-08-20", []),
        ]),
    ])
    assert set(cache) == {"NVDA", "CMG"}


def test_fold_splits_cache_records_unreadable_content():
    cache, bad = L.fold_splits_cache([
        {"title": "splits-cache-2026-08-20.json", "content": "not json"},
    ])
    assert cache == {} and len(bad) == 1


def test_fold_splits_cache_records_a_malformed_row():
    cache, bad = L.fold_splits_cache([
        _cache_file("splits-cache-2026-08-20.json", [
            {"symbol": "NVDA"},  # missing checked_through
        ]),
    ])
    assert cache == {} and len(bad) == 1


def test_symbols_needing_split_check_includes_never_checked_symbols():
    out = L.symbols_needing_split_check(["NVDA", "OXY"], {}, today=date(2026, 9, 4))
    assert out == ["NVDA", "OXY"]


def test_symbols_needing_split_check_skips_recently_checked_symbols():
    cache = {"NVDA": L.SplitsCacheEntry("NVDA", date(2026, 9, 2), [])}
    out = L.symbols_needing_split_check(["NVDA", "OXY"], cache, today=date(2026, 9, 4))
    assert out == ["OXY"]


def test_symbols_needing_split_check_rechecks_stale_symbols():
    """A symbol checked more than the horizon ago must be rechecked -- a
    real split could have been announced since, and staying cached forever
    would let it silently fall out of reconciliation."""
    cache = {"NVDA": L.SplitsCacheEntry("NVDA", date(2026, 8, 20), [])}
    out = L.symbols_needing_split_check(["NVDA"], cache, today=date(2026, 9, 4))
    assert out == ["NVDA"]


def test_symbols_needing_split_check_dedupes_and_uppercases():
    out = L.symbols_needing_split_check(["nvda", "NVDA"], {}, today=date(2026, 9, 4))
    assert out == ["NVDA"]


def test_splits_cache_round_trip_feeds_apply_splits_directly():
    """The cached SplitEvents must be usable as-is by apply_splits, with no
    extra conversion step -- proving the cache and the existing split
    machinery actually compose."""
    cache, bad = L.fold_splits_cache([
        _cache_file("splits-cache-2026-08-20.json",
            [_splits_row("NVDA", "2026-08-20",
                [{"effective_date": "2026-06-10", "ratio": 4.0}])]),
    ])
    splits_by_symbol = {sym: entry.splits for sym, entry in cache.items()}
    fills = [L.Fill("NVDA", "buy", 25, 400.0, date(2026, 5, 1), "o1")]
    adjusted = L.apply_splits(fills, splits_by_symbol)
    assert adjusted[0].quantity == 100.0
    assert adjusted[0].price == 100.0


# -------------------------------------------------------------- sector cache

def test_sector_cache_filename_matches_convention():
    assert L.sector_cache_filename(date(2026, 9, 5)) == "sector-cache-2026-09-05.json"
    assert L.sector_cache_filename(date(2026, 9, 5), 2) == "sector-cache-2026-09-05-2.json"


def _sector_row(symbol, checked_through, sector):
    return {"symbol": symbol, "checked_through": checked_through, "sector": sector}


def test_fold_sector_cache_keeps_the_latest_check_per_symbol():
    cache, bad = L.fold_sector_cache([
        _cache_file("sector-cache-2026-08-20.json", [_sector_row("AAPL", "2026-08-20", "old_label")]),
        _cache_file("sector-cache-2026-09-05.json", [_sector_row("AAPL", "2026-09-05", "technology")]),
    ])
    assert bad == []
    assert cache["AAPL"] == {"sector": "technology", "checked_through": date(2026, 9, 5)}


def test_fold_sector_cache_stores_none_for_a_diversified_fund():
    """A fund with no single dominant sector is cached as sector=None -- a
    real, useful answer (checked and found diversified), not a gap."""
    cache, bad = L.fold_sector_cache([
        _cache_file("sector-cache-2026-09-05.json", [_sector_row("VTI", "2026-09-05", None)]),
    ])
    assert cache["VTI"] == {"sector": None, "checked_through": date(2026, 9, 5)}


def test_fold_sector_cache_records_unreadable_content():
    cache, bad = L.fold_sector_cache([{"title": "sector-cache-2026-09-05.json", "content": "not json"}])
    assert cache == {} and len(bad) == 1


def test_fold_sector_cache_records_a_malformed_row():
    cache, bad = L.fold_sector_cache([
        _cache_file("sector-cache-2026-09-05.json", [{"symbol": "AAPL"}]),  # missing checked_through
    ])
    assert cache == {} and len(bad) == 1


def test_symbols_needing_sector_check_includes_never_checked_symbols():
    assert L.symbols_needing_sector_check(["AAPL", "VTI"], {}, today=date(2026, 9, 5)) == ["AAPL", "VTI"]


def test_symbols_needing_sector_check_skips_recently_checked_symbols():
    cache = {"AAPL": {"sector": "technology", "checked_through": date(2026, 9, 1)}}
    assert L.symbols_needing_sector_check(["AAPL", "VTI"], cache, today=date(2026, 9, 5)) == ["VTI"]


def test_symbols_needing_sector_check_rechecks_after_the_long_horizon():
    cache = {"AAPL": {"sector": "technology", "checked_through": date(2026, 1, 1)}}
    out = L.symbols_needing_sector_check(["AAPL"], cache, today=date(2026, 9, 5))
    assert out == ["AAPL"]


def test_symbols_needing_sector_check_horizon_is_much_longer_than_splits():
    """The whole point: a sector classification is stable for months, so
    this cache must not re-fetch on the same cadence as splits."""
    assert L.SECTOR_CACHE_HORIZON_DAYS > L.SPLITS_CACHE_HORIZON_DAYS * 10


# -------------------------------------------------------- congress discovery cache

def test_congress_discovery_cache_filename_matches_convention():
    assert (L.congress_discovery_cache_filename(date(2026, 9, 5))
            == "congress-discovery-cache-2026-09-05.json")
    assert (L.congress_discovery_cache_filename(date(2026, 9, 5), 2)
            == "congress-discovery-cache-2026-09-05-2.json")


def _congress_row(bioguide_id, checked_through, symbols):
    return {"bioguide_id": bioguide_id, "checked_through": checked_through, "symbols": symbols}


def test_fold_congress_discovery_cache_keeps_latest_symbols_per_member():
    cache, bad = L.fold_congress_discovery_cache([
        _cache_file("congress-discovery-cache-2026-08-29.json",
            [_congress_row("C001123", "2026-08-29", ["OXY"])]),
        _cache_file("congress-discovery-cache-2026-09-05.json",
            [_congress_row("C001123", "2026-09-05", ["OXY", "DSGX", "TXRH"])]),
    ])
    assert bad == []
    assert cache["C001123"]["symbols"] == ["DSGX", "OXY", "TXRH"]
    assert cache["C001123"]["checked_through"] == date(2026, 9, 5)


def test_fold_congress_discovery_cache_records_unreadable_content():
    cache, bad = L.fold_congress_discovery_cache(
        [{"title": "congress-discovery-cache-2026-09-05.json", "content": "not json"}])
    assert cache == {} and len(bad) == 1


def test_fold_congress_discovery_cache_records_a_malformed_row():
    cache, bad = L.fold_congress_discovery_cache([
        _cache_file("congress-discovery-cache-2026-09-05.json", [{"bioguide_id": "C001123"}]),
    ])
    assert cache == {} and len(bad) == 1


def test_bioguides_needing_discovery_check_includes_never_checked():
    out = L.bioguides_needing_discovery_check(["C001123", "W000817"], {}, today=date(2026, 9, 5))
    assert out == ["C001123", "W000817"]


def test_bioguides_needing_discovery_check_skips_recently_checked():
    cache = {"C001123": {"symbols": ["OXY"], "checked_through": date(2026, 9, 3)}}
    out = L.bioguides_needing_discovery_check(["C001123", "W000817"], cache, today=date(2026, 9, 5))
    assert out == ["W000817"]


def test_bioguides_needing_discovery_check_rechecks_after_a_week():
    cache = {"C001123": {"symbols": ["OXY"], "checked_through": date(2026, 8, 20)}}
    out = L.bioguides_needing_discovery_check(["C001123"], cache, today=date(2026, 9, 5))
    assert out == ["C001123"]


# ---------------------------------------------------------------- state bundle
# 11 September 2026: collapses the ~20-file Stage 0 read path into one file.
# The central promise under test is that `unpack_state_bundle` reconstructs
# EXACTLY what folding the dated files it replaces would have produced --
# there is no second parser to drift out of sync with the first.

def test_state_bundle_filename_matches_convention():
    assert L.state_bundle_filename(date(2026, 9, 11)) == "state-bundle-2026-09-11.json"
    assert L.state_bundle_filename(date(2026, 9, 11), 1) == "state-bundle-2026-09-11-1.json"


def _bundle_fixture():
    journal_entries = [
        L.JournalEntry(run_id="r1", on="2026-09-01", kind="run", payload={"i": 1}),
        L.JournalEntry(run_id="r2", on="2026-09-02", kind="thesis",
                       payload={"thesis_id": "t1", "opened": "2026-09-02", "horizon_days": 21}),
    ]
    fills = [
        L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 18), "o1", "IND1"),
        L.Fill("SGOV", "buy", 4.421802, 100.47, date(2026, 8, 24), "o2", "AGENTIC1"),
    ]
    splits_by_symbol = {
        "XOM": L.SplitsCacheEntry("XOM", date(2026, 9, 8),
                                  [L.SplitEvent("XOM", date(2020, 8, 10), 4.0)]),
    }
    sector_by_symbol = {
        "XOM": {"sector": "energy", "checked_through": date(2026, 9, 1)},
        "SGOV": {"sector": None, "checked_through": date(2026, 9, 1)},
    }
    congress_discovery = {
        "C001123": {"symbols": ["OXY", "XOM"], "checked_through": date(2026, 9, 5)},
    }
    return journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery


def test_build_state_bundle_is_json_serialisable():
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 11))
    reloaded = json.loads(json.dumps(bundle))
    assert reloaded["schema"] == L.STATE_BUNDLE_SCHEMA_VERSION
    assert reloaded["as_of"] == "2026-09-11"


def test_unpack_state_bundle_round_trips_journal_entries():
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 11))
    out = L.unpack_state_bundle(bundle)
    assert out["bad"] == []
    assert [e.run_id for e in out["journal"].entries] == ["r1", "r2"]
    assert out["journal"].open_theses(date(2026, 9, 10)) != []


def test_unpack_state_bundle_round_trips_fills_with_account():
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 11))
    out = L.unpack_state_bundle(bundle)
    by_account = {f.order_id: f.account for f in out["fills"]}
    assert by_account == {"o1": "IND1", "o2": "AGENTIC1"}


def test_unpack_state_bundle_round_trips_splits_and_sector_and_congress():
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 11))
    out = L.unpack_state_bundle(bundle)
    assert out["splits_by_symbol"]["XOM"].checked_through == date(2026, 9, 8)
    assert out["splits_by_symbol"]["XOM"].splits[0].ratio == 4.0
    assert out["sector_by_symbol"]["XOM"]["sector"] == "energy"
    assert out["sector_by_symbol"]["SGOV"]["sector"] is None
    assert out["congress_discovery"]["C001123"]["symbols"] == ["OXY", "XOM"]


def test_unpacking_a_bundle_matches_folding_the_dated_files_it_replaces():
    """The central promise: building a bundle from a fold's results and then
    unpacking it must reproduce that exact fold, since Stage 0 will read
    ONLY the bundle from here on -- if this ever drifted, a bundle-reading
    run would silently see different state than a dated-file-reading one
    did."""
    fills_direct, fills_bad = L.fold_fills_cache([
        _cache_file("fills-cache-2026-08-20.json",
                    [_fill_row("XOM", "buy", 10, 100.0, "2026-08-18", "o1", "IND1")]),
    ])
    splits_direct, splits_bad = L.fold_splits_cache([
        _cache_file("splits-cache-2026-09-08.json",
                    [_splits_row("XOM", "2026-09-08", [{"effective_date": "2020-08-10", "ratio": 4.0}])]),
    ])
    sector_direct, sector_bad = L.fold_sector_cache([
        _cache_file("sector-cache-2026-09-01.json", [_sector_row("XOM", "2026-09-01", "energy")]),
    ])
    congress_direct, congress_bad = L.fold_congress_discovery_cache([
        _cache_file("congress-discovery-cache-2026-09-05.json",
                    [_congress_row("C001123", "2026-09-05", ["OXY", "XOM"])]),
    ])
    journal_direct = L.fold_journal([
        _file("journal-2026-09-01.json", [{"run_id": "a", "kind": "run", "payload": {"i": 1}}]),
    ])
    assert fills_bad == splits_bad == sector_bad == congress_bad == journal_direct.unreadable == []

    bundle = L.build_state_bundle(
        journal_entries=journal_direct.entries, fills=fills_direct,
        splits_by_symbol=splits_direct, sector_by_symbol=sector_direct,
        congress_discovery=congress_direct, as_of=date(2026, 9, 11))
    out = L.unpack_state_bundle(bundle)

    assert out["bad"] == []
    assert [f.order_id for f in out["fills"]] == [f.order_id for f in fills_direct]
    assert out["splits_by_symbol"].keys() == splits_direct.keys()
    assert out["sector_by_symbol"] == sector_direct
    assert out["congress_discovery"] == congress_direct
    assert [e.run_id for e in out["journal"].entries] == [e.run_id for e in journal_direct.entries]


def test_unpack_state_bundle_reports_bad_rows_prefixed_bundle():
    bundle = {"schema": 1, "as_of": "2026-09-11",
              "journal_entries": [], "fills": [{"symbol": "OXY"}],  # missing required fields
              "splits_by_symbol": [], "sector_by_symbol": [], "congress_discovery": []}
    out = L.unpack_state_bundle(bundle)
    assert out["fills"] == []
    assert len(out["bad"]) == 1
    assert out["bad"][0].startswith("bundle:")


def test_unpack_state_bundle_defaults_as_of_to_today_when_missing():
    out = L.unpack_state_bundle({})
    assert out["bad"] == []
    assert out["fills"] == []
    assert out["journal"].entries == []


def test_unpack_state_bundle_folds_in_extra_journal_files_after_the_bundle():
    """15 September 2026: a bundle written early (right after Stage 0, not
    at Stage 6) cannot yet include entries THIS run adds later. Those still
    land in the ordinary dated journal file, and must fold in on TOP of the
    bundle's own entries, not replace or precede them."""
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 15))
    extra = [_file("journal-2026-09-15.json",
                   [{"run_id": "r3", "kind": "run", "payload": {"i": 3}}])]
    out = L.unpack_state_bundle(bundle, extra_journal_files=extra)
    assert out["bad"] == []
    assert [e.run_id for e in out["journal"].entries] == ["r1", "r2", "r3"]


def test_unpack_state_bundle_extra_journal_files_default_to_none():
    journal_entries, fills, splits_by_symbol, sector_by_symbol, congress_discovery = _bundle_fixture()
    bundle = L.build_state_bundle(
        journal_entries=journal_entries, fills=fills, splits_by_symbol=splits_by_symbol,
        sector_by_symbol=sector_by_symbol, congress_discovery=congress_discovery,
        as_of=date(2026, 9, 15))
    out = L.unpack_state_bundle(bundle)
    assert [e.run_id for e in out["journal"].entries] == ["r1", "r2"]


# --------------------------------------------------- fills_cache_matches_fresh
# 14 September 2026: apply_splits is deliberately not idempotent, so caching
# its OUTPUT instead of its input compounds every run. These pin the
# cross-check that catches it for anything still inside the re-fetch window.

def test_fills_cache_matches_fresh_agrees_when_quantities_match():
    cached = [L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 18), "o1", "IND1")]
    fresh = [L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 18), "o1", "IND1")]
    assert L.fills_cache_matches_fresh(cached, fresh) == []


def test_fills_cache_matches_fresh_flags_a_split_adjusted_cache_entry():
    """The actual 14 September shape: the cache holds NVDA post-split (a
    10:1 split applied), but a fresh re-fetch of the same order returns the
    broker's raw, as-executed (pre-split) quantity."""
    cached = [L.Fill("NVDA", "buy", 100, 12.0, date(2024, 6, 1), "o1", "IND1")]
    fresh = [L.Fill("NVDA", "buy", 10, 120.0, date(2024, 6, 1), "o1", "IND1")]
    assert L.fills_cache_matches_fresh(cached, fresh) == ["o1"]


def test_fills_cache_matches_fresh_ignores_fills_outside_the_overlap():
    """A cached fill with no matching fresh order_id (outside the
    watermark-forward re-fetch window) cannot be cross-checked -- absence of
    evidence, not evidence of correctness."""
    cached = [L.Fill("XOM", "buy", 10, 100.0, date(2022, 1, 1), "old1", "IND1")]
    fresh = [L.Fill("SGOV", "buy", 5, 100.0, date(2026, 9, 1), "new1", "IND1")]
    assert L.fills_cache_matches_fresh(cached, fresh) == []


def test_fills_cache_matches_fresh_respects_qty_tolerance():
    cached = [L.Fill("XOM", "buy", 10.0000001, 100.0, date(2026, 8, 18), "o1", "IND1")]
    fresh = [L.Fill("XOM", "buy", 10.0, 100.0, date(2026, 8, 18), "o1", "IND1")]
    assert L.fills_cache_matches_fresh(cached, fresh) == []


def test_fills_cache_matches_fresh_ignores_fills_with_no_order_id():
    cached = [L.Fill("XOM", "buy", 10, 100.0, date(2026, 8, 18), "", "IND1")]
    fresh = [L.Fill("XOM", "buy", 999, 100.0, date(2026, 8, 18), "", "IND1")]
    assert L.fills_cache_matches_fresh(cached, fresh) == []


# --- chunked fills-cache writes (16 September 2026) ---------------------------
#
# The failure these reproduce: `fills_ready_to_cache` returned 870 real rows
# (~136 KB) on 8, 15 and 16 September 2026 and no `fills-cache-*.json` was
# written on any of them, because the Drive connector takes content inline
# only and the whole payload could not pass through one `create_file` call.


def _many_fills(n: int) -> list[L.Fill]:
    return [L.Fill(f"SYM{i % 97:04d}", "buy" if i % 2 else "sell",
                   1.0 + i / 1000.0, 100.0 + i / 100.0,
                   date(2026, 1, 1) + timedelta(days=i % 200),
                   f"order-id-{i:08d}", "AGENTIC1" if i % 3 else "INDIVIDUAL1")
            for i in range(n)]


def test_a_full_fills_history_does_not_fit_one_inline_write():
    """The premise. 870 rows is what 8/15/16 September actually computed."""
    fills = _many_fills(870)
    one_file = json.dumps([L._fill_cache_row(f) for f in fills], separators=(",", ":"))
    assert len(one_file.encode("utf-8")) > L.CACHE_FILE_MAX_INLINE_BYTES


def test_fills_cache_chunks_each_fit_the_inline_write_budget():
    chunks = L.fills_cache_chunks(_many_fills(870), date(2026, 9, 16))
    assert len(chunks) > 1, "a payload this size should need more than one file"
    for title, content in chunks:
        assert len(content.encode("utf-8")) <= L.CACHE_FILE_MAX_INLINE_BYTES, title


def test_fills_cache_chunks_fold_back_to_exactly_the_single_file_result():
    """The whole safety argument: N chunks and the one file they replace are
    the same fills, because `fold_fills_cache` already sorts and dedupes
    across every `-N` file for a date."""
    fills = _many_fills(870)
    run_date = date(2026, 9, 16)

    chunked, chunk_bad = L.fold_fills_cache(
        [{"title": t, "content": c} for t, c in L.fills_cache_chunks(fills, run_date)])
    single, single_bad = L.fold_fills_cache([{
        "title": L.fills_cache_filename(run_date),
        "content": json.dumps([L._fill_cache_row(f) for f in fills]),
    }])

    assert chunk_bad == [] and single_bad == []
    assert chunked == single
    assert len(chunked) == len({(f.account, f.order_id) for f in fills})


def test_fills_cache_chunks_titles_follow_the_dated_sequence_convention():
    chunks = L.fills_cache_chunks(_many_fills(870), date(2026, 9, 16))
    titles = [t for t, _ in chunks]
    assert titles[0] == "fills-cache-2026-09-16.json"
    assert titles[1] == "fills-cache-2026-09-16-1.json"
    assert all(L.FILLS_CACHE_RE.match(t) for t in titles)
    assert len(set(titles)) == len(titles)


def test_fills_cache_chunks_can_start_after_an_earlier_run_today():
    """A watchdog retry writing after the 06:20 routine already wrote -0
    must not collide with it."""
    chunks = L.fills_cache_chunks(_many_fills(50), date(2026, 9, 16), start_seq=3)
    assert [t for t, _ in chunks] == ["fills-cache-2026-09-16-3.json"]


def test_fills_cache_chunks_is_empty_when_there_is_nothing_to_cache():
    """Unchanged rule: never write an empty cache file."""
    assert L.fills_cache_chunks([], date(2026, 9, 16)) == []


def test_fills_cache_chunks_never_drops_a_row_too_big_for_its_own_file():
    """Better an oversized write that fails loudly than a fill silently
    discarded to fit a budget."""
    fills = _many_fills(3)
    chunks = L.fills_cache_chunks(fills, date(2026, 9, 16), max_bytes=1)
    assert len(chunks) == 3
    folded, bad = L.fold_fills_cache(
        [{"title": t, "content": c} for t, c in chunks])
    assert bad == [] and len(folded) == 3


def test_state_bundle_fills_rows_match_the_dated_cache_rows():
    """The bundle and the dated files share one row shape by construction."""
    fills = _many_fills(5)
    bundle = L.build_state_bundle(journal_entries=[], fills=fills,
                                  splits_by_symbol={}, sector_by_symbol={},
                                  congress_discovery={}, as_of=date(2026, 9, 16))
    chunk_rows = [r for _, c in L.fills_cache_chunks(fills, date(2026, 9, 16))
                  for r in json.loads(c)]
    assert bundle["fills"] == chunk_rows
