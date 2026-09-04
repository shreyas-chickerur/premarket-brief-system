"""HTML rendering for the morning brief.

The email is the only artefact a human actually reads, so it is rendered by
tested code rather than composed freehand each morning. Two things follow from
that: the format cannot drift run to run, and a failed run cannot quietly send a
worse email than a successful one.

Design rules, in priority order:

1. **The verdict is the first thing, in words.** Not a wall of checks the reader
   has to scan for a "false".
2. **A failed run is SHORT.** What broke, the likely cause, what to do. The full
   check list and the raw manifest are noise when the answer is "nothing ran";
   the manifest is written to storage for anyone who wants it.
3. **Only failures are itemised.** Passing checks collapse to a count. Twenty
   green rows train the reader to skim, and skimming is how a red one gets
   missed.
4. **`diagnose` states a cause, not just a symptom.** "9 required tools missing"
   is a symptom. "The connectors are not attached to the routine" is the thing
   the reader can act on.

Email clients strip <style> blocks and ignore most modern CSS, so everything is
inline and the layout is tables. No external assets: images and web fonts are
blocked by default in most clients and would make the mail look broken.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Optional, Sequence

__all__ = ["diagnose", "render_email", "subject_for", "idea_card", "idea_cards",
          "CANONICAL_SECTIONS", "MAX_SECTIONS"]

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------

INK = "#1a1a1a"
MUTED = "#6b6b6b"
RULE = "#e3e3e0"
PAPER = "#ffffff"
WELL = "#f7f7f5"

STATUS = {
    "aborted":  ("#b3261e", "#fdeceb", "Run aborted"),
    "degraded": ("#8a5a00", "#fdf4e3", "Completed with warnings"),
    "nominal":  ("#1e6b3a", "#eaf5ee", "Healthy"),
}

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
        "Arial,sans-serif")
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


# --------------------------------------------------------------------------
# diagnosis
# --------------------------------------------------------------------------

# check name -> (likely cause, what to do about it)
# Keyed on the check names runlog.preflight emits. A check with no entry falls
# back to its own detail string, which is why the fallback still says something
# useful rather than "unknown error".
_CAUSES: dict[str, tuple[str, str]] = {
    "tools_available": (
        "The connectors are not attached to this scheduled routine. A routine "
        "sees only the connectors listed in its own configuration — it does "
        "not inherit the ones connected to the account.",
        "Open the routine, edit its configuration, and add the missing "
        "connectors to the 'Runs with' list. Then run it again.",
    ),
    "self_test": (
        "The committed code is broken: the test suite failed before any market "
        "data was touched.",
        "Read the failing test names below, fix them, and push. The run will "
        "keep refusing to trade until the suite is green.",
    ),
    "ledger_reconciled": (
        "The recorded ledger and the broker disagree about what is held. Every "
        "sizing decision downstream of a wrong position count is also wrong.",
        "Compare the two position lists below and correct the journal in "
        "state.json before the next run. Do not trade on a disputed ledger.",
    ),
    "fired_on_schedule": (
        "The run started well away from its intended time, which usually means "
        "a daylight-saving change moved the UTC cron relative to local time.",
        "Update the routine's cron expression for the current offset.",
    ),
    "holiday_table_current": (
        "The verified market-holiday table has passed the date it was checked "
        "through, so an unlisted date is no longer evidence of a normal session.",
        "Extend MARKET_HOLIDAYS in runlog.py from the exchange's published "
        "calendar and update HOLIDAY_TABLE_HORIZON.",
    ),
}

_GENERIC = (
    "A blocking preflight check failed, so the run stopped before acting.",
    "Read the detail below and correct the underlying condition.",
)


def diagnose(manifest: dict) -> Optional[dict]:
    """Name the likely cause of a failed run, or None if nothing blocked it.

    Reports on the FIRST blocking failure only. Later failures are usually
    consequences of the first one, and listing them side by side invites the
    reader to fix the wrong thing.
    """
    blocking = [c for c in manifest.get("checks", [])
                if not c.get("passed") and c.get("severity") == "block"]
    if not blocking:
        return None

    first = blocking[0]
    cause, remedy = _CAUSES.get(first.get("name", ""), _GENERIC)
    return {
        "check": first.get("name", "unknown"),
        "detail": first.get("detail", ""),
        "cause": cause,
        "remedy": remedy,
        "also_failed": [c.get("name") for c in blocking[1:]],
    }


# --------------------------------------------------------------------------
# subject
# --------------------------------------------------------------------------

def subject_for(manifest: dict, *, prefix: str = "") -> str:
    """One line that is useful in a notification, with no body opened.

    The reader should be able to triage from the lock screen, so the headline
    fact goes in the subject: what broke, or what was decided.
    """
    run_id = manifest.get("run_id", "unknown")
    date = run_id.split("-dry")[0] if "-dry" in run_id else run_id
    head = f"{prefix} " if prefix else ""

    d = diagnose(manifest)
    if d:
        short = {
            "tools_available": "connectors not attached",
            "self_test": "test suite failing",
            "ledger_reconciled": "ledger/broker mismatch",
        }.get(d["check"], d["check"])
        return f"{head}Pre-Market Brief {date} — ABORTED ({short})"

    if not _market_open(manifest):
        return f"{head}Pre-Market Brief {date} — market closed"

    dec = manifest.get("decisions", [])
    placed = sum(1 for x in dec if x.get("executed"))
    ideas = sum(1 for x in dec if x.get("action") in ("buy", "sell", "trim"))
    health = manifest.get("health", "nominal")
    tail = "" if health == "nominal" else f", {health}"
    return (f"{head}Pre-Market Brief {date} — "
            f"{ideas} idea{'s' if ideas != 1 else ''}, "
            f"{placed} order{'s' if placed != 1 else ''}{tail}")


def _market_open(manifest: dict) -> bool:
    for c in manifest.get("checks", []):
        if c.get("name") == "market_open_today":
            return bool(c.get("passed"))
    return True


# --------------------------------------------------------------------------
# small html helpers
# --------------------------------------------------------------------------

def _p(text: str, *, size: int = 15, color: str = INK, top: int = 0) -> str:
    return (f'<p style="margin:{top}px 0 12px;font:400 {size}px/1.55 {FONT};'
            f'color:{color};">{text}</p>')


def _h(text: str) -> str:
    return (f'<h2 style="margin:28px 0 10px;font:600 12px/1.4 {FONT};'
            f'letter-spacing:.09em;text-transform:uppercase;color:{MUTED};">'
            f'{escape(text)}</h2>')


def _well(inner: str, *, accent: str = RULE) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0" width="100%" style="background:{WELL};'
            f'border-left:3px solid {accent};margin:0 0 14px;">'
            f'<tr><td style="padding:12px 14px;">{inner}</td></tr></table>')


def _code(text: str) -> str:
    return (f'<span style="font:400 13px/1.5 {MONO};color:{INK};'
            f'word-break:break-word;">{escape(text)}</span>')


_ACTION_COLORS = {
    "buy":  ("#1e6b3a", "#eaf5ee"),
    "sell": ("#b3261e", "#fdeceb"),
    "trim": ("#8a5a00", "#fdf4e3"),
    "hold": (MUTED, WELL),
}


def _action_badge(action: str) -> str:
    fg, bg = _ACTION_COLORS.get(action.lower(), (MUTED, WELL))
    return (f'<span style="display:inline-block;padding:2px 8px;'
            f'border-radius:3px;background:{bg};color:{fg};font:700 12px/1.5 '
            f'{FONT};letter-spacing:.04em;text-transform:uppercase;">'
            f'{escape(action)}</span>')


def idea_card(symbol: str, action: str, quantity: str = "", detail: str = "",
             bullets: Sequence[tuple[str, str]] = ()) -> str:
    """One symbol, rendered so the reader sees the call before the reasoning:
    symbol and action first, then each supporting point as its own bullet,
    tagged with the source behind it. Replaces hand-written prose paragraphs
    for exactly the reason a reader complained about them: a name, an
    action, and a quantity buried in a sentence are slower to scan than the
    same three things in the first line of a card.

    `bullets` are `(text, source)` pairs — `source` names what the point
    came from (a data provider, a named report, a computed check, a
    specific tool call) so the reasoning trail is visible, not just
    asserted. Pass an empty `source` string for a bullet with no single
    attributable source (a synthesis of several); it still renders, just
    without an attribution tag.
    """
    qty_html = (f'&nbsp;<strong style="font:700 14px/1.4 {FONT};color:{INK};">'
                f'{escape(str(quantity))}</strong>' if quantity else "")
    detail_html = (f'&nbsp;<span style="font:400 13px/1.5 {FONT};'
                   f'color:{MUTED};">{escape(detail)}</span>' if detail else "")
    head = (f'<div style="margin:0 0 8px;">'
            f'<strong style="font:700 16px/1.3 {FONT};color:{INK};">'
            f'{escape(symbol)}</strong>&nbsp; {_action_badge(action)}'
            f'{qty_html}{detail_html}</div>')

    items = []
    for text, source in bullets:
        tag = (f' <span style="color:{MUTED};font-size:12px;">— '
               f'{escape(source)}</span>' if source else "")
        items.append(f'<li style="margin:0 0 5px;font:400 14px/1.55 {FONT};'
                     f'color:{INK};">{escape(text)}{tag}</li>')
    body = (f'<ul style="margin:0;padding-left:18px;">{"".join(items)}</ul>'
            if items else "")
    return _well(head + body)


def idea_cards(ideas: Sequence[dict], *, closest_calls: Sequence[dict] = ()) -> str:
    """Render a whole section (e.g. the individual account's suggestions, or
    the agentic account's activity) as one card per symbol instead of a
    paragraph. Each dict in `ideas` needs `symbol` and `action`; `quantity`,
    `detail`, and `bullets` (a list of `(text, source)` pairs) are optional.
    An empty list renders a plain "nothing today" line — a do-nothing day is
    a correct, expected output, not an omission to explain away.

    `closest_calls` (see `runlog.closest_calls`) are rejected ideas that got
    furthest through the five-condition gate before failing, rendered only
    when `ideas` is empty — a symbol that cleared 4 of 5 conditions is worth
    naming even on a day nothing actually traded, rather than the reader
    seeing the exact same "nothing today" whether the gate came close or
    was not close at all. Each dict needs `symbol`, `gate_failed`,
    `conditions_cleared`, and `conditions_total` — the shape
    `runlog.closest_calls` already returns; this module does not import
    `runlog` to render it.
    """
    if ideas:
        return "".join(
            idea_card(i["symbol"], i["action"], i.get("quantity", ""),
                     i.get("detail", ""), i.get("bullets", ()))
            for i in ideas)
    if not closest_calls:
        return _p("Nothing today.", color=MUTED)
    lines = [_p("Nothing today.", color=MUTED)]
    for c in closest_calls:
        lines.append(_p(
            f'Closest: <strong>{escape(str(c["symbol"]))}</strong> — cleared '
            f'{escape(str(c["conditions_cleared"]))} of {escape(str(c["conditions_total"]))} '
            f'conditions, failed on "{escape(str(c["gate_failed"]))}".',
            size=13, color=MUTED))
    return "".join(lines)


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

# The fixed set of sections a completed run's email may contain, in the
# order they render. Cut from an original four ("Evidence review" / "Where
# things stand" / "What moved and why" / "Risk measurement") to three on 1
# September 2026, then restructured to these five on 4 September 2026: the
# original three had nowhere to put the prior-day track record
# (`runlog.score_closed_decisions`, Stage 0.6) or the portfolio
# concentration verdict (`quantcore.correlation_concentration`, Stage 2) —
# both existed in the run's own data and had no section to appear in, so
# neither ever reached the one artefact a human actually reads. This tuple
# is enforced in `render_email` below, not just described in
# `DAILY_PROCEDURE.md` prose: the user does not want a market-commentary
# newsletter, and a caller that starts appending ad hoc sections again
# (the exact drift that produced the original four) now gets a `ValueError`
# instead of a silently growing email.
CANONICAL_SECTIONS = (
    "Agentic account — activity",
    "Individual account — suggestions",
    "Prior-day review",
    "Diversification",
    "System health",
)
MAX_SECTIONS = len(CANONICAL_SECTIONS)


def render_email(manifest: dict, *,
                 sections: Sequence[tuple[str, str]] = (),
                 prefix: str = "",
                 disclaimer: bool = True) -> tuple[str, str]:
    """Return `(subject, html)`.

    `sections` are `(title, html)` pairs holding the research narrative for a
    run that got far enough to produce one. They are DROPPED on an aborted run:
    if preflight refused to proceed, there is no research, and printing empty
    headings implies there was.

    On a completed run, `sections` may contain at most `MAX_SECTIONS` (5)
    entries, and every title must be one of `CANONICAL_SECTIONS` — a day
    that has nothing to say for a section omits it entirely (fewer than 5
    is fine; an unlisted or duplicated title is not). This is what "caps
    enforced in code" means: the section list cannot silently grow or drift
    just because a future change to `DAILY_PROCEDURE.md`'s prose forgot to
    also update this list.
    """
    d = diagnose(manifest)
    health = "aborted" if d else manifest.get("health", "nominal")
    accent, wash, label = STATUS.get(health, STATUS["degraded"])

    body: list[str] = [_banner(manifest, accent, wash, label)]

    if d:
        body.append(_failure_block(d, accent))
        body.append(_what_still_worked(manifest))
    else:
        sections = list(sections)
        if len(sections) > MAX_SECTIONS:
            raise ValueError(
                f"render_email accepts at most {MAX_SECTIONS} sections, got "
                f"{len(sections)}: {[t for t, _ in sections]}")
        titles = [t for t, _ in sections]
        unknown = [t for t in titles if t not in CANONICAL_SECTIONS]
        if unknown:
            raise ValueError(
                f"unrecognised section title(s) {unknown} — must be one of "
                f"{CANONICAL_SECTIONS}")
        if len(set(titles)) != len(titles):
            raise ValueError(f"duplicate section title(s) in {titles}")

        body.append(_health_line(manifest))
        for title, html in sections:
            body.append(_h(title))
            body.append(html)
        body.append(_decisions(manifest))

    body.append(_footer(manifest, disclaimer))

    html = (
        f'<div style="margin:0;padding:24px 12px;background:{WELL};">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' width="100%" style="max-width:640px;margin:0 auto;background:{PAPER};'
        f'border:1px solid {RULE};border-radius:6px;">'
        f'<tr><td style="padding:26px 28px 22px;">{"".join(body)}</td></tr>'
        f'</table></div>'
    )
    return subject_for(manifest, prefix=prefix), html


def _banner(manifest: dict, accent: str, wash: str, label: str) -> str:
    mode = escape(str(manifest.get("mode", "")).upper())
    run_id = escape(str(manifest.get("run_id", "")))
    tag = ""
    if mode:
        tag = (f'<span style="display:inline-block;padding:2px 7px;'
               f'border:1px solid {accent};border-radius:3px;font:600 10px/1.6 '
               f'{FONT};letter-spacing:.08em;color:{accent};">{mode}</span>')
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' width="100%" style="background:{wash};border-radius:4px;'
        f'margin:0 0 18px;"><tr><td style="padding:14px 16px;">'
        f'<div style="font:700 19px/1.3 {FONT};color:{accent};">'
        f'{escape(label)}</div>'
        f'<div style="margin-top:6px;font:400 12px/1.5 {FONT};color:{MUTED};">'
        f'{tag}&nbsp; {run_id}</div>'
        f'</td></tr></table>'
    )


def _failure_block(d: dict, accent: str) -> str:
    out = [_h("What failed")]
    out.append(_p(f'Check <strong>{escape(d["check"])}</strong> did not pass.'))
    if d["detail"]:
        out.append(_well(_code(d["detail"]), accent=accent))
    if d["also_failed"]:
        names = ", ".join(escape(str(n)) for n in d["also_failed"])
        out.append(_p(f'Also failing, likely as a consequence: {names}.',
                      size=13, color=MUTED))

    out.append(_h("Likely cause"))
    out.append(_p(escape(d["cause"])))
    out.append(_h("What to do"))
    out.append(_p(escape(d["remedy"])))
    return "".join(out)


def _what_still_worked(manifest: dict) -> str:
    """One quiet line, so a reader knows the blast radius.

    An aborted run is not necessarily a broken system, and saying what held up
    keeps a connector problem from reading like a code problem.
    """
    checks = manifest.get("checks", [])
    passed = sum(1 for c in checks if c.get("passed"))
    bits = [f"{passed} of {len(checks)} preflight checks passed"]

    for c in checks:
        if c.get("name") == "self_test" and c.get("passed"):
            bits.append(escape(str(c.get("detail", "test suite green"))))
            break

    return _p("No orders were placed and no research ran. "
              + "; ".join(bits) + ".", size=13, color=MUTED, top=18)


def _health_line(manifest: dict) -> str:
    checks = manifest.get("checks", [])
    failed = [c for c in checks if not c.get("passed")]
    dur = manifest.get("duration_ms", 0)
    anoms = len(manifest.get("anomalies", []))

    parts = [f"{len(checks) - len(failed)}/{len(checks)} checks",
             f"{dur / 1000:.1f}s",
             f"{anoms} anomal{'y' if anoms == 1 else 'ies'}"]
    line = _p(" &middot; ".join(parts), size=13, color=MUTED)

    if not failed:
        return line

    warn = "".join(
        f'<li style="margin:0 0 4px;">{escape(str(c.get("name")))}: '
        f'{escape(str(c.get("detail", "")))}</li>' for c in failed)
    return line + _well(
        f'<div style="font:600 12px/1.5 {FONT};color:{MUTED};'
        f'letter-spacing:.06em;text-transform:uppercase;">Warnings</div>'
        f'<ul style="margin:8px 0 0;padding-left:18px;font:400 13px/1.55 '
        f'{FONT};color:{INK};">{warn}</ul>')


def _decisions(manifest: dict) -> str:
    dec = manifest.get("decisions", [])
    out = [_h("Decisions")]
    if not dec:
        out.append(_p("No decisions recorded.", color=MUTED))
        return "".join(out)

    rows = []
    for x in dec:
        mark = "placed" if x.get("executed") else "not placed"
        gate = x.get("gate_failed")
        reason = escape(str(x.get("reason", "")))
        if gate:
            reason = f'<em>{escape(str(gate))}</em> — {reason}'
        rows.append(
            f'<tr><td style="padding:7px 8px 7px 0;border-top:1px solid {RULE};'
            f'font:600 13px/1.5 {MONO};color:{INK};white-space:nowrap;">'
            f'{escape(str(x.get("symbol", "")))}</td>'
            f'<td style="padding:7px 8px;border-top:1px solid {RULE};'
            f'font:400 13px/1.5 {FONT};color:{INK};white-space:nowrap;">'
            f'{escape(str(x.get("action", "")))}</td>'
            f'<td style="padding:7px 8px;border-top:1px solid {RULE};'
            f'font:400 12px/1.5 {FONT};color:{MUTED};white-space:nowrap;">'
            f'{mark}</td>'
            f'<td style="padding:7px 0 7px 8px;border-top:1px solid {RULE};'
            f'font:400 13px/1.5 {FONT};color:{INK};">{reason}</td></tr>')

    out.append(f'<table role="presentation" cellpadding="0" cellspacing="0" '
               f'border="0" width="100%" style="border-collapse:collapse;">'
               f'{"".join(rows)}</table>')
    return "".join(out)


def _footer(manifest: dict, disclaimer: bool) -> str:
    meta = " &middot; ".join(escape(str(x)) for x in [
        manifest.get("run_id", ""),
        manifest.get("mode", ""),
        manifest.get("started_at", ""),
    ] if x)
    tail = (f'<div style="margin-top:6px;">Not investment advice. Suggestions '
            f'are research output; the decision is yours.</div>'
            if disclaimer else "")
    return (f'<div style="margin-top:26px;padding-top:14px;'
            f'border-top:1px solid {RULE};font:400 11px/1.6 {FONT};'
            f'color:{MUTED};">{meta}{tail}</div>')
