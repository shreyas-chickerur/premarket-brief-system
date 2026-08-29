"""Tests for the brief renderer.

The email is the deliverable, so the properties worth pinning are the ones a
reader depends on: a failed run says why in words, a failed run does not print
research headings it has no research for, and nothing a model wrote can inject
markup into the message.
"""

import re

import pytest

import emailer as E
import runlog as R


TOOLS = ["a", "b"]
GOOD_TEST = {"passed": True, "n_passed": 122, "n_failed": 0, "duration_ms": 1900}


def aborted_manifest(missing=("get_accounts", "get_portfolio")):
    log = R.RunLog("2026-08-31-dryrun", mode="dry_run")
    log.check("tools_available", False, "block", f"missing: {list(missing)}")
    log.check("self_test", True, "block", "122 passed, 0 failed in 1900ms")
    log.abort("required tools not visible")
    return log.manifest()


def healthy_manifest():
    log = R.RunLog("2026-08-31", mode="live")
    log.check("tools_available", True, "block", "all 10 present")
    log.check("self_test", True, "block", "122 passed, 0 failed in 1900ms")
    log.check("market_open_today", True, "info", "regular session")
    log.decide(R.Decision("F", "buy", "agentic", True, "26 shares at 6.80"))
    log.decide(R.Decision("NFLX", "reject", "individual", False,
                          "no dated catalyst", gate_failed="catalyst"))
    return log.manifest()


# ------------------------------------------------------------- diagnosis

def test_diagnose_names_a_cause_not_just_the_symptom():
    d = E.diagnose(aborted_manifest())
    assert d["check"] == "tools_available"
    # the symptom is "tools missing"; the cause is that the routine does not
    # inherit account connectors.
    assert "not attached" in d["cause"]
    assert "Runs with" in d["remedy"]


def test_diagnose_reports_only_the_first_blocking_failure():
    log = R.RunLog("r", mode="live")
    log.check("tools_available", False, "block", "missing: ['x']")
    log.check("ledger_reconciled", False, "block", "AAA differs")
    d = E.diagnose(log.manifest())
    assert d["check"] == "tools_available"
    assert d["also_failed"] == ["ledger_reconciled"]


def test_diagnose_returns_none_on_a_clean_run():
    assert E.diagnose(healthy_manifest()) is None


def test_unknown_check_still_gets_an_actionable_fallback():
    log = R.RunLog("r", mode="live")
    log.check("something_new", False, "block", "boom")
    d = E.diagnose(log.manifest())
    assert d["cause"] and d["remedy"]


def test_a_warning_does_not_count_as_an_abort():
    log = R.RunLog("r", mode="live")
    log.check("fired_on_schedule", False, "warn", "off by 41 min")
    assert E.diagnose(log.manifest()) is None


# ------------------------------------------------------------- subject

def test_subject_carries_the_headline_fact_for_triage():
    s, _ = E.render_email(aborted_manifest(), prefix="[DRY RUN]")
    assert s.startswith("[DRY RUN]")
    assert "ABORTED" in s and "connectors not attached" in s


def test_subject_of_a_good_run_counts_ideas_and_orders():
    s, _ = E.render_email(healthy_manifest())
    assert "ABORTED" not in s
    assert "1 idea" in s and "1 order" in s


def test_subject_says_so_when_the_market_is_closed():
    log = R.RunLog("2026-09-07", mode="live")
    log.check("market_open_today", False, "info", "holiday")
    s, _ = E.render_email(log.manifest())
    assert "market closed" in s


# ------------------------------------------------------------- body

def test_failed_run_states_failure_cause_and_remedy():
    _, html = E.render_email(aborted_manifest())
    for heading in ("What failed", "Likely cause", "What to do"):
        assert heading in html


def test_failed_run_drops_research_sections_entirely():
    """Empty headings imply research happened. None did."""
    _, html = E.render_email(
        aborted_manifest(),
        sections=[("What moved and why", "<p>markets did things</p>")])
    assert "What moved and why" not in html
    assert "markets did things" not in html


def test_failed_run_says_no_orders_were_placed():
    _, html = E.render_email(aborted_manifest())
    assert "No orders were placed" in html


def test_failed_run_reports_what_still_worked():
    _, html = E.render_email(aborted_manifest())
    assert "122 passed" in html


def test_healthy_run_keeps_its_research_sections():
    _, html = E.render_email(
        healthy_manifest(),
        sections=[("What moved and why", "<p>markets did things</p>")])
    assert "What moved and why" in html and "markets did things" in html


def test_passing_checks_are_counted_not_itemised():
    """Twenty green rows train the reader to skim past a red one."""
    _, html = E.render_email(healthy_manifest())
    assert "3/3 checks" in html
    assert "all 10 present" not in html


def test_warnings_are_itemised_on_a_completed_run():
    log = R.RunLog("r", mode="live")
    log.check("tools_available", True, "block", "ok")
    log.check("fired_on_schedule", False, "warn", "off by 41 min")
    _, html = E.render_email(log.manifest())
    assert "Warnings" in html and "off by 41 min" in html


def test_rejected_ideas_show_the_gate_that_failed():
    _, html = E.render_email(healthy_manifest())
    assert "catalyst" in html and "no dated catalyst" in html


def test_disclaimer_present_by_default():
    _, html = E.render_email(healthy_manifest())
    assert "Not investment advice" in html


# ------------------------------------------------------------- format

def test_output_is_html_not_plain_text():
    _, html = E.render_email(aborted_manifest())
    assert html.lstrip().startswith("<div") and "</table>" in html


def test_styles_are_inline_because_clients_strip_style_blocks():
    _, html = E.render_email(healthy_manifest())
    assert "<style" not in html.lower()
    assert 'style="' in html


def test_no_external_assets_are_referenced():
    """Remote images and web fonts are blocked by default and render broken."""
    _, html = E.render_email(healthy_manifest())
    assert "http://" not in html
    assert "<img" not in html.lower()
    assert "https://fonts." not in html


@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    'x" onload="alert(1)',
    "Ford & Co <b>bold</b>",
])
def test_model_written_text_cannot_inject_markup(payload):
    log = R.RunLog("r", mode="live")
    log.check("tools_available", False, "block", payload)
    _, html = E.render_email(log.manifest())
    assert payload not in html
    assert "<script>" not in html


def test_symbol_and_reason_are_escaped_in_the_decisions_table():
    log = R.RunLog("r", mode="live")
    log.check("tools_available", True, "block", "ok")
    log.decide(R.Decision("<img src=x>", "buy", "agentic", True, "a & b"))
    _, html = E.render_email(log.manifest())
    assert "<img src=x>" not in html
    assert "&amp;" in html


def test_render_is_deterministic_for_the_same_manifest():
    m = aborted_manifest()
    assert E.render_email(m) == E.render_email(m)


def test_body_stays_small_enough_to_read():
    """Gmail clips messages past ~102KB; a failure note has no excuse to be big."""
    _, html = E.render_email(aborted_manifest())
    assert len(html.encode()) < 12_000
