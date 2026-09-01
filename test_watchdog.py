"""Tests for the watchdog: the outside check on whether the daily run happened
and was healthy.

Weighted toward the two failure shapes that actually occurred in this
system's history: a run that hangs and never writes a manifest at all (31
August, a sandbox permission prompt with nobody there to answer it), and a
run that writes one but aborted (every live run before 1 September).
"""

from datetime import date

import pytest

import emailer
import runlog as R
import watchdog as W


TODAY = date(2026, 9, 2)


def _file(name, content_dict):
    import json
    return {"title": name, "content": json.dumps(content_dict)}


def _aborted_manifest(run_id="2026-09-02-dryrun"):
    log = R.RunLog(run_id, mode="dry_run")
    log.check("ledger_reconciled", False, "block", "individual: 2 symbols disagree")
    log.abort("individual account positions do not follow from its own fills")
    return log.manifest()


def _healthy_manifest(run_id="2026-09-02-dryrun"):
    log = R.RunLog(run_id, mode="dry_run")
    log.check("tools_available", True, "block", "all 14 present")
    return log.manifest()


# ------------------------------------------------------------ latest_manifest_for

def test_finds_the_manifest_for_today_among_other_dates():
    files = [
        _file("run-manifest-2026-09-01.json", {"run_id": "yesterday"}),
        _file("run-manifest-2026-09-02.json", {"run_id": "today"}),
        _file("journal-2026-09-02.json", {"entries": []}),   # not a manifest, ignored
    ]
    m = W.latest_manifest_for(files, TODAY)
    assert m == {"run_id": "today"}


def test_a_second_run_the_same_day_is_preferred_over_the_first():
    files = [
        _file("run-manifest-2026-09-02.json", {"run_id": "first"}),
        _file("run-manifest-2026-09-02-1.json", {"run_id": "second"}),
    ]
    assert W.latest_manifest_for(files, TODAY) == {"run_id": "second"}


def test_no_manifest_for_today_returns_none():
    files = [_file("run-manifest-2026-09-01.json", {"run_id": "yesterday"})]
    assert W.latest_manifest_for(files, TODAY) is None


def test_an_empty_folder_returns_none_not_an_error():
    assert W.latest_manifest_for([], TODAY) is None


def test_a_corrupt_manifest_file_is_treated_as_absent_not_a_crash():
    files = [{"title": "run-manifest-2026-09-02.json", "content": "{not json"}]
    assert W.latest_manifest_for(files, TODAY) is None


# ------------------------------------------------------------------------ assess

def test_missing_manifest_is_the_no_run_problem():
    """The 31 August shape: a run hangs and never writes anything. Nothing to
    diagnose from a manifest that does not exist, but silence itself is the
    finding."""
    a = W.assess(None)
    assert a.problem is True
    assert a.kind == "no_run"
    assert "permission-prompt" in a.detail or "hung" in a.detail


def test_an_aborted_manifest_is_a_problem_with_a_named_cause():
    a = W.assess(_aborted_manifest())
    assert a.problem is True
    assert a.kind == "aborted"
    assert a.cause is not None and a.remedy is not None
    assert a.run_id == "2026-09-02-dryrun"


def test_a_healthy_manifest_is_not_a_problem():
    a = W.assess(_healthy_manifest())
    assert a.problem is False
    assert a.kind == "healthy"


def test_a_closed_market_day_is_not_a_problem():
    """aborted=False on a closed-market day is the precise reason `aborted`
    is the signal to gate on, not 'did research happen'. A quiet Saturday
    must not wake anyone."""
    log = R.RunLog("2026-09-05-dryrun", mode="dry_run")
    log.check("market_open_today", False, "info", "weekend")
    a = W.assess(log.manifest())
    assert a.problem is False


def test_assess_reuses_emailer_diagnose_rather_than_a_second_cause_table():
    """The watchdog and the daily brief must agree on what a failure means by
    construction, not by two tables staying in sync by hand."""
    m = _aborted_manifest()
    a = W.assess(m)
    d = emailer.diagnose(m)
    assert a.cause == d["cause"]
    assert a.remedy == d["remedy"]


# -------------------------------------------------------------------- render_alert

def test_render_alert_refuses_to_run_on_a_healthy_assessment():
    """A function that only makes sense for a problem should not silently
    render a nonsensical 'alert' for a day nothing was wrong."""
    a = W.Assessment(problem=False, kind="healthy")
    with pytest.raises(ValueError):
        W.render_alert(a, today=TODAY)


def test_render_alert_includes_cause_and_remedy_when_present():
    a = W.assess(_aborted_manifest())
    subject, html = W.render_alert(a, today=TODAY)
    assert "WATCHDOG" in subject and "aborted" in subject
    assert "Likely cause" in html and "What to do" in html


def test_render_alert_omits_cause_and_remedy_for_a_no_run_problem():
    """There is no manifest to diagnose a cause from; the renderer must not
    print an empty or fabricated 'Likely cause' heading."""
    a = W.assess(None)
    _, html = W.render_alert(a, today=TODAY)
    assert "Likely cause" not in html


def test_render_alert_escapes_narrative_text():
    a = W.Assessment(problem=True, kind="aborted", detail="<script>alert(1)</script>",
                     cause="<b>bold</b>", remedy="ok")
    _, html = W.render_alert(a, today=TODAY)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_alert_is_deterministic():
    a = W.assess(_aborted_manifest())
    assert W.render_alert(a, today=TODAY) == W.render_alert(a, today=TODAY)
