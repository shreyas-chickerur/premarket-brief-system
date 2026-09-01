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
from datetime import date
from typing import Any, Iterable, Optional

import emailer

__all__ = ["Assessment", "latest_manifest_for", "assess", "render_alert"]

MANIFEST_RE = re.compile(r"^run-manifest-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


@dataclass(frozen=True)
class Assessment:
    """What the watchdog concluded, and whether it is worth waking anyone for."""
    problem: bool
    kind: str            # "no_run" | "aborted" | "healthy"
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


def assess(manifest: Optional[dict]) -> Assessment:
    """The core judgment: is there a problem, and if so, what kind.

    `aborted` is the precise signal for "something is actually wrong" -- a
    closed-market day is `aborted: false` (nothing broke, there was simply
    nothing to do), so gating on `aborted` rather than "did research happen"
    means the watchdog stays quiet on every ordinary weekend-adjacent day and
    speaks up only when a blocking check actually failed.
    """
    if manifest is None:
        return Assessment(
            problem=True, kind="no_run",
            detail="No run-manifest found for today. The scheduled run may "
                  "have failed to fire, or hung before it could write one -- "
                  "the same shape as the 31 August permission-prompt stall, "
                  "where a run froze waiting on a prompt nobody was present "
                  "to answer.",
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
