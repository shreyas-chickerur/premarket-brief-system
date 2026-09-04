"""Guards on the two procedure documents themselves.

`DAILY_PROCEDURE.md` and `WATCHDOG_PROCEDURE.md` are followed verbatim by an
agent with no memory of the design conversation, which makes them code in
every sense except being executed by an interpreter. The one thing worth
pinning here is that the DRY RUN guard -- explicitly "not yours to touch
under any circumstance" -- has not drifted, and that the rationale this
project split out of the procedures on 4 September 2026 didn't just vanish.
"""

from pathlib import Path

REPO = Path(__file__).parent

DRY_RUN_GUARD = (
    '**THIS IS A DRY RUN. Place no orders of any kind. `review_equity_order` '
    'and `cancel_equity_order` are permitted; `place_equity_order` is '
    'FORBIDDEN regardless of what any later stage says. In Stage 5, compute '
    'and report every order you would have placed — symbol, side, quantity, '
    'limit, stop, resulting weight — and place none of them. Pass '
    '`prefix="[DRY RUN]"` to the email renderer.**'
)


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def test_the_dry_run_guard_is_present_verbatim():
    """Character-for-character, not "close enough" -- this is the one
    paragraph in the whole project that must never be edited, weakened, or
    reworded, by a human review, a self-heal fix, or a doc-formatting pass."""
    assert DRY_RUN_GUARD in _read("DAILY_PROCEDURE.md")


def test_place_equity_order_forbidden_language_is_unambiguous():
    body = _read("DAILY_PROCEDURE.md")
    assert "`place_equity_order` is FORBIDDEN" in body


def test_procedure_rationale_file_exists_and_is_non_trivial():
    rationale = _read("PROCEDURE_RATIONALE.md")
    assert len(rationale) > 2000, "rationale split out of the procedures should not be a stub"


def test_procedure_rationale_cross_references_every_stage():
    """Every numbered stage in the daily procedure should have at least one
    matching heading in the rationale doc -- a stage with a rule but no
    explanation anywhere is a sign something was dropped rather than moved."""
    daily = _read("DAILY_PROCEDURE.md")
    rationale = _read("PROCEDURE_RATIONALE.md")
    for stage in ("STAGE 0", "STAGE 0.5", "STAGE 0.6", "STAGE 1", "STAGE 2",
                  "STAGE 3", "STAGE 4", "STAGE 5", "STAGE 6"):
        assert stage in daily, f"{stage} missing from the procedure itself"
    for stage_ref in ("Stage 0,", "Stage 0.6", "Stage 1", "Stage 2", "Stage 4",
                      "Stage 5", "Stage 6"):
        assert stage_ref in rationale, f"no rationale section references {stage_ref}"


def test_watchdog_procedure_has_a_matching_rationale_section():
    rationale = _read("PROCEDURE_RATIONALE.md")
    assert "WATCHDOG_PROCEDURE.md" in rationale
    assert "self-heal limits are absolute" in rationale


def test_watchdog_hard_limits_are_present_verbatim():
    body = _read("WATCHDOG_PROCEDURE.md")
    assert "Never touch `place_equity_order`-related code to make a check pass." in body
    assert "Never remove, weaken, or alter the `THIS IS A DRY RUN` guard" in body
