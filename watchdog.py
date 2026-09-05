"""The meta-check: did the daily run itself happen, and was it healthy?

Every fix so far in this system's history was found by a person asking "how
did today's run go?" and reading the log by hand. That does not scale, and it
has a specific blind spot the rest of the system cannot cover on its own: on
31 August a run hung indefinitely on a sandbox permission prompt meant for an
interactive human, and because it never reached Stage 6, it sent no email --
the one outcome this system is built never to produce, reached anyway, from
outside the run's own control.

A run that is stuck cannot report on itself. Something has to check from the
outside whether it ran at all.

This module is deliberately small and reuses `emailer.diagnose` rather than
re-deriving cause attribution -- one place decides what a failure means, and
the watchdog and the daily brief agree by construction, not by convention.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import emailer

__all__ = ["Assessment", "latest_manifest_for", "latest_heartbeat_for",
          "heartbeat_status", "HEARTBEAT_STALE_AFTER_SECONDS", "assess", "render_alert"]

MANIFEST_RE = re.compile(r"^run-manifest-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")
# Same (?:-(\d+))? sequence suffix as MANIFEST_RE, and for the identical
# reason: the Drive connector can create a file but not modify one already
# written (ledger.py's compact_journal_month has the same constraint) --
# "update it as each stage completes" cannot mean editing one file's
# content in place, only writing a new file each time and letting the
# highest sequence number for today be the current one. Old heartbeat
# files are never deleted for the same reason old daily journal files
# survive compaction: expected leftover clutter, not an error, since
# `latest_heartbeat_for` only ever looks at today's highest sequence.
HEARTBEAT_RE = re.compile(r"^run-heartbeat-(\d{4}-\d{2}-\d{2})-(\d+)\.json$")

# A stage's own budget (runlog.STAGE_TIMING_BUDGETS_MS) bounds the longest
# legitimate gap between two heartbeat writes -- the heartbeat updates once
# per completed stage, so a run genuinely still working can go quiet for up
# to its CURRENT stage's own budget without being stuck. The largest single
# budget is "gather" at 1,800s (30 minutes); 2,100s (35 minutes) gives that
# real headroom without waiting so long that a truly stuck run sits
# undetected for most of the watchdog's own 60-minute offset. See
# PROCEDURE_RATIONALE.md, 5 September 2026, for the full ordering this sits
# in: gather budget (30m) < this (35m) < runlog.DEFAULT_WALL_CLOCK_DEADLINE_SECONDS
# (45m) < the watchdog's own 60-minute offset.
HEARTBEAT_STALE_AFTER_SECONDS = 2_100


@dataclass(frozen=True)
class Assessment:
    """What the watchdog concluded, and whether it is worth waking anyone for."""
    problem: bool
    kind: str            # "no_run" | "aborted" | "healthy" | "alive" | "hung"
    detail: str = ""
    cause: Optional[str] = None
    remedy: Optional[str] = None
    run_id: Optional[str] = None


def latest_manifest_for(files: Iterable[dict], today: date) -> Optional[dict]:
    """Pick the latest run-manifest-YYYY-MM-DD[-N].json for `today`, if any.

    `files` are `{"title": str, "content": str}` pairs, the same shape
    `ledger.fold_journal` expects -- deliberately, so a caller that already
    listed the Drive folder for the journal fold can reuse the same listing
    here instead of a second round trip.

    A file that will not parse as JSON is treated as absent rather than
    raising: a corrupt manifest is itself worth flagging as a no_run-shaped
    problem, not a reason to crash the watchdog that exists to catch problems.
    """
    iso = today.isoformat()
    best: tuple[int, dict] | None = None
    for f in files:
        m = MANIFEST_RE.match(str(f.get("title", "")))
        if not m or m.group(1) != iso:
            continue
        seq = int(m.group(2) or 0)
        try:
            content = json.loads(f.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        if best is None or seq > best[0]:
            best = (seq, content)
    return best[1] if best else None


def latest_heartbeat_for(files: Iterable[dict], today: date) -> Optional[dict]:
    """Pick the highest-sequence `run-heartbeat-YYYY-MM-DD-N.json` for
    `today`, if any -- exactly `latest_manifest_for`'s own logic, because
    it faces the identical constraint: the Drive connector can create a
    file but not modify one already written, so "update the heartbeat"
    means writing a new, higher-numbered file each time, never editing the
    last one in place.

    A file that will not parse as JSON, same as a manifest, is treated as
    absent rather than raising -- a corrupt heartbeat is not proof of
    anything, and `heartbeat_status` already treats "no readable heartbeat"
    and "no heartbeat at all" the same way (`"absent"`).
    """
    iso = today.isoformat()
    best: tuple[int, dict] | None = None
    for f in files:
        m = HEARTBEAT_RE.match(str(f.get("title", "")))
        if not m or m.group(1) != iso:
            continue
        seq = int(m.group(2))
        try:
            content = json.loads(f.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        if best is None or seq > best[0]:
            best = (seq, content)
    return best[1] if best else None


def heartbeat_status(heartbeat: Optional[dict], *, now: datetime,
                     stale_after_seconds: int = HEARTBEAT_STALE_AFTER_SECONDS) -> str:
    """`"alive"`, `"hung"`, or `"absent"` -- the three things a heartbeat can
    tell a watchdog that a manifest alone cannot, because a manifest only
    exists once a run has already finished (successfully or not).

    `"absent"` means what it means today: no heartbeat file, same as no
    manifest -- the run may never have started, or hung before Stage 0
    could write anything at all (still possible; the heartbeat's first
    write is not instantaneous with the run's own start).

    `"hung"` also covers a heartbeat that parsed but is missing or cannot
    parse its own `updated_at` -- a malformed liveness signal must never
    resolve in the run's favour; only a readable, sufficiently recent
    timestamp counts as proof of life.

    A gap up to `stale_after_seconds` is `"alive"`: the heartbeat updates
    once per completed stage, so a run legitimately deep into a single slow
    stage (gather's own budget is the largest, 1,800s) can go quiet for a
    while without being stuck -- see `HEARTBEAT_STALE_AFTER_SECONDS`'s own
    comment for why 2,100s was chosen relative to that.
    """
    if heartbeat is None:
        return "absent"
    try:
        updated_at = datetime.fromisoformat(str(heartbeat["updated_at"]))
    except (KeyError, ValueError):
        return "hung"
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_seconds = (now - updated_at).total_seconds()
    return "hung" if age_seconds > stale_after_seconds else "alive"


def assess(manifest: Optional[dict], *, heartbeat: Optional[dict] = None,
          now: Optional[datetime] = None,
          stale_after_seconds: int = HEARTBEAT_STALE_AFTER_SECONDS) -> Assessment:
    """The core judgment: is there a problem, and if so, what kind.

    `aborted` is the precise signal for "something is actually wrong" -- a
    closed-market day is `aborted: false` (nothing broke, there was simply
    nothing to do), so gating on `aborted` rather than "did research happen"
    means the watchdog stays quiet on every ordinary weekend-adjacent day and
    speaks up only when a blocking check actually failed.

    A completed `manifest` is always authoritative -- it is the run's own
    final word, and a heartbeat (necessarily earlier, and necessarily less
    complete) has nothing to add once it exists. `heartbeat` only matters
    when `manifest is None`: before 5 September 2026 that always meant
    `"no_run"`, the SAME conclusion whether the run had never started or
    was still working normally two minutes from finishing. A recent
    heartbeat now distinguishes those -- `kind="alive"`, not a problem, the
    watchdog says so and leaves the run alone rather than beginning a retry
    against a run that is still in progress (the near-miss this was built
    to close: 2 September, a 39-minute run against a 30-minute watchdog
    offset nearly triggered a second, duplicate run). A stale heartbeat is
    `kind="hung"` -- a specific, actionable finding ("stuck after stage X",
    not just "nothing found") this watchdog could never make before.
    """
    if manifest is None:
        status = heartbeat_status(heartbeat, now=now or datetime.now(timezone.utc),
                                  stale_after_seconds=stale_after_seconds)
        if status == "absent":
            return Assessment(
                problem=True, kind="no_run",
                detail="No run-manifest found for today. The scheduled run may "
                      "have failed to fire, or hung before it could write one -- "
                      "the same shape as the 31 August permission-prompt stall, "
                      "where a run froze waiting on a prompt nobody was present "
                      "to answer.",
            )
        last_stage = heartbeat.get("last_stage_completed") if heartbeat else None
        updated_at = heartbeat.get("updated_at") if heartbeat else None
        run_id = heartbeat.get("run_id") if heartbeat else None
        if status == "alive":
            return Assessment(
                problem=False, kind="alive",
                detail=(f"No run-manifest yet, but the heartbeat is current "
                        f"(last stage completed: {last_stage or 'none yet'}, "
                        f"updated {updated_at}). The run appears to still be "
                        f"in progress; leaving it alone."),
                run_id=run_id,
            )
        return Assessment(
            problem=True, kind="hung",
            detail=(f"The heartbeat has not updated since {updated_at} "
                    f"(last stage completed: {last_stage or 'none yet'}), past "
                    f"the {stale_after_seconds}s staleness threshold. No "
                    f"run-manifest exists, so this is not an abort -- the run "
                    f"appears to be stuck, most likely inside a hung external "
                    f"call that never returned."),
            run_id=run_id,
        )

    if manifest.get("aborted"):
        d = emailer.diagnose(manifest)
        return Assessment(
            problem=True, kind="aborted",
            detail=str(manifest.get("abort_reason", "")),
            cause=d["cause"] if d else None,
            remedy=d["remedy"] if d else None,
            run_id=manifest.get("run_id"),
        )

    return Assessment(problem=False, kind="healthy", run_id=manifest.get("run_id"))


def render_alert(a: Assessment, *, today: date) -> tuple[str, str]:
    """Build the watchdog's own email. Reuses emailer's palette and escaping
    so this reads as part of one system, not a mismatched second voice.

    Only ever called when `a.problem` is true -- a healthy day is silent by
    design. Twenty routine "all clear" emails train the reader to stop
    reading watchdog mail at all, which defeats the one day it matters.
    """
    if not a.problem:
        raise ValueError("render_alert should only be called when a.problem is True")

    subject = f"[WATCHDOG] Pre-Market Brief {today.isoformat()} — {a.kind.replace('_', ' ')}"

    body = [emailer._p(f"<strong>{emailer.escape(a.kind.replace('_', ' ').upper())}</strong>")]
    body.append(emailer._well(emailer._code(a.detail)))
    if a.cause:
        body.append(emailer._h("Likely cause"))
        body.append(emailer._p(emailer.escape(a.cause)))
    if a.remedy:
        body.append(emailer._h("What to do"))
        body.append(emailer._p(emailer.escape(a.remedy)))
    if a.run_id:
        body.append(emailer._p(f"Run: {emailer.escape(a.run_id)}", size=12, color=emailer.MUTED))

    html = (
        f'<div style="margin:0;padding:24px 12px;background:{emailer.WELL};">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' width="100%" style="max-width:640px;margin:0 auto;background:{emailer.PAPER};'
        f'border:1px solid {emailer.RULE};border-radius:6px;">'
        f'<tr><td style="padding:26px 28px 22px;">'
        f'<div style="font:700 19px/1.3 {emailer.FONT};color:{emailer.STATUS["aborted"][0]};">'
        f'Watchdog alert</div>'
        f'{"".join(body)}'
        f'</td></tr></table></div>'
    )
    return subject, html
