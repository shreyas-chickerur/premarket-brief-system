# Exercising the self-heal loop deliberately — a proposal, not an implementation

Written 5 September 2026. Nothing in this document is implemented. It exists
to let a human decide which of several ways to deliberately trigger a
diagnose-fix-merge-retry cycle is worth running, and to be explicit about
what each one would and would not prove — because that is the actual
question, not "does the watchdog work."

## Why this is needed

The self-heal loop (`WATCHDOG_PROCEDURE.md`, `HANDOFF.md` section 8) has
never completed a real diagnose-fix-merge-retry cycle against a genuinely
`aborted` run. It has been open on the live-trading status snapshot since
before this session started, and after this session it carries more surface
area than before: `washsale_registry_stable`, the `NEWS_SENTIMENT` filter,
and the macro/commodity preview branches can all now abort or fail a run
where nothing could before. Waiting for a real failure to happen on its own,
on an account that trades roughly once a week when it trades at all, is not
a plan with a visible timeline.

## What is already true regardless of which option is chosen

- **`place_equity_order` cannot fire.** The `THIS IS A DRY RUN` guard is
  untouched by every option below and by the watchdog's own hard limits
  (`WATCHDOG_PROCEDURE.md` Stage 5 step 2: never touch
  `place_equity_order`-related code, never weaken a Stage 0 safety check,
  never alter the DRY RUN guard). No option here changes that. The
  downside of any of these exercises is a bad merge to a codebase that still
  cannot place a real order, not a bad trade.
- **A bad merge is still a real risk**, the same one the watchdog's existing
  self-heal authorization already accepts for production use (1 September
  2026: "I don't care if you merge into main... this is your money to play
  with"). Running an exercise is not a reason to accept a WORSE version of
  that risk than production already carries — see "Where this runs" below.

## Three options, and what each would prove

### Option A — plant a fresh, familiar-shaped bug on a branch

Reintroduce a bug in the exact shape `WATCHDOG_PROCEDURE.md` already
authorizes fixing on its own ("a missing allow-list entry, a data-shape
mismatch, a new corporate-action type") — for example, narrowing
`fills_from_orders`'s accepted order states back to `filled`/`partially_filled`
only, silently reproducing the real FIG bug from 1 September. Point a
throwaway watchdog run at the branch carrying this regression and let it
diagnose, fix, and retry.

**Proves:** the mechanics work end to end — `emailer.diagnose()` names a
real cause, a fix gets written, the suite passes, the merge happens, the
retry runs `DAILY_PROCEDURE.md` again successfully.

**Does not prove:** judgment under a genuinely unfamiliar failure. I would
be choosing the bug, in a shape the watchdog's own authorization already
anticipates, with the correct fix already known to whoever plants it. This
is close to grading an open-book exam with the answer key taped to the
inside cover — a real exercise of the pipeline's mechanics, not of its
judgment.

### Option B — force an abort without touching any code

Add a temporary, clearly-marked escape hatch (an environment variable or a
single conditional in `runlog.preflight`) that fails one named check on
command — no real bug, no fix to write. The watchdog sees `aborted: true`,
diagnoses it, and would need to recognize there is nothing to patch (or
that removing the escape hatch itself IS the fix).

**Proves:** the detection and diagnosis path — the watchdog correctly
identifies which check failed and produces the specific, actionable cause
`emailer.diagnose()` is supposed to name (this is also the cheapest way to
confirm the new `brokerage_token_health`/token-expiry distinction actually
reads correctly end to end, not just in unit tests).

**Does not prove:** anything about the fix-and-merge half of self-heal at
all. This is a strictly narrower rehearsal than Option A, useful mainly as
a fast, near-zero-risk warm-up before attempting A or C.

### Option C — revert a real, past fix, blind

Pick one commit from this repository's actual history that fixed a genuine
production bug (there is no shortage: the FIG allow-list fix, the
`GOLD_SILVER_SPOT` parser, the kill-switch-style unit mismatches this
project's sibling repo hit, the `stop_quality_conflation` fix from 2
September). Revert exactly that commit on a branch, **without re-reading
the original fix or its rationale immediately beforehand**, and let the
watchdog diagnose and re-fix it as though it were new.

**Proves:** genuine independent diagnosis and a genuine independent fix —
the watchdog has to reconstruct the reasoning, not pattern-match against a
bug someone just handed it with the fix in mind. This is the closest of the
three to "a failure that surprises it," while still being bounded and
reversible, because the correct fix is known in advance (it is sitting in
git history) even though the watchdog does not get to see it.

**Does not prove:** a truly novel failure mode this codebase has never seen
before — by definition, every option that stays safe and reversible has to
choose from problems already understood well enough to plant deliberately.
A genuinely unprecedented failure cannot be rehearsed; it can only be
survived when it happens.

## Where this runs

None of these should run against the real trading Drive folder, the real
`state.json`, or by pointing the live cron-triggered watchdog/main routines
at a branch — that is an infrastructure change (editing cron-job.org's
target or the routine's own config) with its own real risk of being left
misconfigured after the exercise ends. The lower-risk shape: clone the repo
to a throwaway branch and a throwaway Drive folder seeded with a copy of
real (or realistic synthetic) `state.json`/journal files, run
`DAILY_PROCEDURE.md` and `WATCHDOG_PROCEDURE.md` manually against that
branch and folder, and treat the resulting merge as a normal PR reviewed by
a human before it ever reaches `main` — even though a real self-heal event
going forward auto-merges, this is a rehearsal, and the one thing worth
spending a review on is confirming the fix it produces is one you would
have wanted merged unsupervised.

## Recommendation

Run B once, first — it is nearly free and it is the only way to confirm the
token-expiry diagnosis actually reads correctly in the one place that
matters (the rendered email), not just in a unit test. Then run C, not A —
A proves the pipeline can execute a script it was already handed; C is the
version that actually tests whether the diagnosis step does real work. A is
worth keeping in reserve only if C turns up something the watchdog cannot
reason about at all, to isolate whether the failure is in diagnosis or in
the fix-and-merge mechanics.

This is a recommendation, not a decision. Say which of B, C, or A (in
whatever order) to actually run, and against what branch and Drive folder,
and that becomes a separate, scoped follow-up — not something this document
or this session should just go ahead and do.
