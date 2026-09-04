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
        sections=[("System health", "<p>markets did things</p>")])
    assert "System health" in html and "markets did things" in html


# -------------------------------------------------- capped, canonical sections

def test_canonical_sections_is_exactly_five():
    """4 September 2026: restructured from three to five -- the original
    three had nowhere to put the prior-day track record
    (runlog.score_closed_decisions, Stage 0.6) or the diversification
    verdict (quantcore.correlation_concentration, Stage 2)."""
    assert E.MAX_SECTIONS == 5
    assert len(E.CANONICAL_SECTIONS) == 5
    assert "Prior-day review" in E.CANONICAL_SECTIONS
    assert "Diversification" in E.CANONICAL_SECTIONS


def test_all_five_canonical_sections_render_together():
    _, html = E.render_email(healthy_manifest(), sections=[
        (title, f"<p>{title} body</p>") for title in E.CANONICAL_SECTIONS
    ])
    for title in E.CANONICAL_SECTIONS:
        assert title in html and f"{title} body" in html


def test_fewer_than_five_sections_is_fine():
    """A day with nothing to say for a section omits it -- only an
    unlisted or duplicated title is an error, not an incomplete one."""
    _, html = E.render_email(healthy_manifest(),
                             sections=[("System health", "<p>ok</p>")])
    assert "System health" in html


def test_more_than_five_sections_raises():
    """The exact drift this cap exists to stop -- a caller quietly
    appending a sixth section over time, the same way the email grew to
    four sections before the 1 September 2026 cut to three."""
    six = list(E.CANONICAL_SECTIONS) + ["Extra commentary"]
    with pytest.raises(ValueError, match="at most 5 sections"):
        E.render_email(healthy_manifest(),
                       sections=[(t, "<p>x</p>") for t in six])


def test_an_unrecognised_section_title_raises():
    with pytest.raises(ValueError, match="unrecognised section title"):
        E.render_email(healthy_manifest(),
                       sections=[("What moved and why", "<p>x</p>")])


def test_a_duplicated_section_title_raises():
    with pytest.raises(ValueError, match="duplicate section title"):
        E.render_email(healthy_manifest(), sections=[
            ("System health", "<p>a</p>"),
            ("System health", "<p>b</p>"),
        ])


def test_section_cap_is_not_enforced_on_an_aborted_run():
    """Sections are dropped entirely on an aborted run (no research ran),
    so an invalid title there must not raise -- it is simply never
    rendered, same as before this cap existed."""
    _, html = E.render_email(
        aborted_manifest(),
        sections=[("not a real section", "<p>x</p>")] * 9)
    assert "not a real section" not in html


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


# ------------------------------------------------------------- idea cards

def test_idea_card_puts_symbol_action_and_quantity_up_front():
    html = E.idea_card("OXY", "buy", "2 shares", "limit 61.80, stop 58.09")
    assert "OXY" in html and "BUY" in html.upper()
    assert "2 shares" in html and "61.80" in html


def test_idea_card_bullets_carry_a_source_tag():
    html = E.idea_card("OXY", "buy", bullets=[
        ("Iran ceasefire talks stalled overnight", "Reuters"),
        ("OPEC+ meets 6 September, output cut expected", "EIA STEO"),
    ])
    assert "Iran ceasefire talks stalled overnight" in html
    assert "Reuters" in html and "EIA STEO" in html


def test_idea_card_bullet_with_no_source_still_renders():
    """A synthesis of several inputs has no single attributable source --
    it must still show up, just without a dangling attribution tag."""
    html = E.idea_card("VTI", "trim", bullets=[("Over the 15% single-name cap", "")])
    assert "Over the 15% single-name cap" in html


def test_idea_card_with_no_bullets_still_renders_the_head():
    html = E.idea_card("SGOV", "hold")
    assert "SGOV" in html and "HOLD" in html.upper()


def test_idea_card_escapes_everything():
    html = E.idea_card("<img src=x>", "buy", "1", "a & b",
                       bullets=[("<script>bad</script>", "a & b co")])
    assert "<img src=x>" not in html and "<script>bad</script>" not in html
    assert "&amp;" in html


def test_idea_cards_empty_list_says_nothing_today_not_silence():
    html = E.idea_cards([])
    assert "nothing today" in html.lower()


def test_idea_cards_renders_one_card_per_idea():
    html = E.idea_cards([
        {"symbol": "OXY", "action": "buy", "quantity": "2 shares",
         "bullets": [("Dated energy catalyst", "EIA STEO")]},
        {"symbol": "VTI", "action": "trim", "quantity": "2.29 shares",
         "bullets": [("Over the 15% cap", "")]},
    ])
    assert "OXY" in html and "VTI" in html
    assert "Dated energy catalyst" in html and "Over the 15% cap" in html


def test_idea_cards_is_part_of_the_public_api():
    assert "idea_card" in E.__all__ and "idea_cards" in E.__all__


# -------------------------------------------------------- closest-call reporting

def _closest_call(symbol="OXY", cleared=4, total=5, gate_failed="no_blocking_conflict"):
    return {"symbol": symbol, "gate_failed": gate_failed,
            "conditions_cleared": cleared, "conditions_total": total}


def test_idea_cards_reports_the_closest_call_when_nothing_cleared_the_gate():
    """4 September 2026: a day nothing clears the gate used to look
    identical whether the closest miss failed on condition 1 or condition
    5 -- both rendered bare 'Nothing today.'"""
    html = E.idea_cards([], closest_calls=[_closest_call()])
    assert "nothing today" in html.lower()
    assert "OXY" in html
    assert "cleared 4 of 5" in html
    assert "no_blocking_conflict" in html


def test_idea_cards_renders_multiple_closest_calls_in_order():
    html = E.idea_cards([], closest_calls=[
        _closest_call("AAA", cleared=4),
        _closest_call("BBB", cleared=2),
    ])
    assert html.index("AAA") < html.index("BBB")


def test_idea_cards_ignores_closest_calls_when_real_ideas_exist():
    """Closest-call reporting is only for a genuinely empty day -- it must
    never appear alongside real activity."""
    html = E.idea_cards(
        [{"symbol": "OXY", "action": "buy"}],
        closest_calls=[_closest_call("VTI")])
    assert "VTI" not in html
    assert "Closest" not in html


def test_idea_cards_closest_call_escapes_its_fields():
    html = E.idea_cards([], closest_calls=[
        _closest_call(symbol="<script>bad</script>", gate_failed="<b>x</b>")])
    assert "<script>bad</script>" not in html
    assert "<b>x</b>" not in html


# -------------------------------------------------------------- verify_email

def _decision(symbol="OXY", account="agentic", quantity=2.0, extra_inputs=None):
    inputs = {"quantity": quantity}
    if extra_inputs:
        inputs.update(extra_inputs)
    return {"symbol": symbol, "account": account, "action": "buy",
            "executed": True, "reason": "cleared the gate", "inputs": inputs}


def _manifest(decisions=()):
    return {"run_id": "run-1", "decisions": list(decisions)}


def test_verify_email_passes_a_clean_fully_traceable_case():
    ideas = {"agentic": [{
        "symbol": "OXY", "action": "buy", "quantity": "2 shares",
        "detail": "limit 61.80, stop 58.09",
        "bullets": [("Dated energy catalyst 9 Sep", "Alpha Vantage NEWS_SENTIMENT")],
    }]}
    manifest = _manifest([_decision(quantity=2.0, extra_inputs={"limit": 61.80, "stop": 58.09})])
    E.verify_email(ideas, manifest=manifest)  # must not raise


def test_verify_email_raises_on_quantity_mismatch():
    ideas = {"agentic": [{"symbol": "OXY", "action": "buy", "quantity": "3 shares"}]}
    manifest = _manifest([_decision(quantity=2.0)])
    with pytest.raises(ValueError, match="does not match"):
        E.verify_email(ideas, manifest=manifest)


def test_verify_email_raises_when_no_decision_matches_the_card_at_all():
    ideas = {"agentic": [{"symbol": "OXY", "action": "buy", "quantity": "2 shares"}]}
    with pytest.raises(ValueError, match="no matching decision"):
        E.verify_email(ideas, manifest=_manifest([]))


def test_verify_email_matches_decisions_by_account_not_just_symbol():
    """A quantity from the wrong account's decision must not satisfy the
    agentic section's claim."""
    ideas = {"agentic": [{"symbol": "OXY", "action": "buy", "quantity": "2 shares"}]}
    manifest = _manifest([_decision(account="individual", quantity=2.0)])
    with pytest.raises(ValueError, match="no matching decision"):
        E.verify_email(ideas, manifest=manifest)


def test_verify_email_raises_on_an_empty_source_bullet():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Looks fine", "")]}]}
    with pytest.raises(ValueError, match="no source"):
        E.verify_email(ideas, manifest=_manifest())


def test_verify_email_raises_on_an_unrecognised_source():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Looks fine", "gut feeling")]}]}
    with pytest.raises(ValueError, match="unrecognised source"):
        E.verify_email(ideas, manifest=_manifest())


def test_verify_email_accepts_a_source_from_known_sources():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Sourced narrowly", "EIA STEO, 9 Sep")]}]}
    E.verify_email(ideas, manifest=_manifest(), known_sources=["EIA STEO, 9 Sep"])


@pytest.mark.parametrize("source", [
    "Alpha Vantage NEWS_SENTIMENT", "Robinhood get_equity_news",
    "quantcore.stop_plan", "runlog.circuit_breaker_check",
    "ledger.reconcile_positions", "evidence.assess", "washsale.check_buy",
])
def test_verify_email_accepts_every_allowed_source_prefix(source):
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Sourced broadly", source)]}]}
    E.verify_email(ideas, manifest=_manifest())


def test_verify_email_raises_on_an_untraceable_number_in_a_bullet():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Up 47% this week", "quantcore.stop_plan")]}]}
    with pytest.raises(ValueError, match="not traceable"):
        E.verify_email(ideas, manifest=_manifest())


def test_verify_email_traces_a_number_present_in_supplied_evidence():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Up 47% this week", "quantcore.stop_plan")]}]}
    E.verify_email(ideas, manifest=_manifest(),
                   evidence=[{"weekly_change_pct": 47.0}])


def test_verify_email_traces_a_rounded_number_within_tolerance():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Weight is 12.48%", "quantcore.stop_plan")]}]}
    E.verify_email(ideas, manifest=_manifest(), evidence=[{"weight_pct": 12.483}])


def test_verify_email_rejects_a_number_outside_tolerance():
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("Weight is 12.48%", "quantcore.stop_plan")]}]}
    with pytest.raises(ValueError, match="not traceable"):
        E.verify_email(ideas, manifest=_manifest(), evidence=[{"weight_pct": 20.0}])


def test_verify_email_raises_on_untraceable_number_in_card_detail():
    ideas = {"agentic": [{"symbol": "OXY", "action": "buy", "quantity": "2 shares",
                          "detail": "limit 99.99"}]}
    manifest = _manifest([_decision(quantity=2.0)])
    with pytest.raises(ValueError, match="card detail"):
        E.verify_email(ideas, manifest=manifest)


@pytest.mark.parametrize("phrase", [
    "reports 9 Sep", "reports Sep 9", "matures in 2026",
    "the 21st of the month", "closed 2026-09-04",
])
def test_verify_email_exempts_dates_and_ordinals_from_numeric_tracing(phrase):
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [(phrase, "quantcore.stop_plan")]}]}
    E.verify_email(ideas, manifest=_manifest())  # must not raise


def test_verify_email_a_21_day_horizon_number_is_exempt_as_ordinal_like_but_bare_count_is_not():
    """A bare small integer with no date/ordinal shape at all is NOT
    exempt just because it's small -- only genuinely date-shaped numbers
    are (see the parametrised date/ordinal test above)."""
    ideas = {"agentic": [{"symbol": "OXY", "action": "hold",
                          "bullets": [("cleared 4 of 5 conditions", "runlog.circuit_breaker_check")]}]}
    with pytest.raises(ValueError, match="not traceable"):
        E.verify_email(ideas, manifest=_manifest())


def test_verify_email_only_checks_sections_actually_passed():
    """Passing only "agentic" must not require an "individual" section to
    exist or be checked."""
    ideas = {"agentic": [{"symbol": "OXY", "action": "buy", "quantity": "2 shares"}]}
    manifest = _manifest([_decision(quantity=2.0)])
    E.verify_email(ideas, manifest=manifest)  # no "individual" key at all


def test_verify_email_empty_ideas_pass_trivially():
    E.verify_email({"agentic": [], "individual": []}, manifest=_manifest())


def test_verify_email_is_part_of_the_public_api():
    assert "verify_email" in E.__all__ and "ALLOWED_SOURCE_PREFIXES" in E.__all__
