# Why `DAILY_PROCEDURE.md` says what it says

`DAILY_PROCEDURE.md` is meant to be followed exactly, by an agent with no
memory of the design conversation. That means it has to be scannable — one
rule per line, no dates, no story. This file holds the explanation the rules
themselves used to carry: why each one exists, what happened the day it was
added, and what it would cost to get wrong. Nothing here overrides the
procedure; if the two ever disagree, the procedure is correct and this file
is stale and needs fixing.

Cross-referenced by stage and step number, in the same order as the
procedure itself.

## Header — one procedure, two callers

Keeping one copy of the procedure instead of two independent trigger prompts
is deliberate — the two drifting out of sync is exactly the kind of bug this
project has hit before (see `HANDOFF.md` section 11, 2 September 2026: a
Drive-connector escaping bug had to be fixed in two places before the two
documents were merged into one).

## Stage 0, step 0 — construct the run log first

`RunLog` timestamps itself on construction. Building it later makes every
stage timing read as zero and silently destroys the performance record the
regression review (`runlog.find_optimizations`) depends on.

**Why each stage must use `runlog.STAGE_TIMING_BUDGETS_MS`'s exact name.**
Before 4 September 2026, `log.stage(name)` existed and recorded a
duration, but nothing ever compared that duration against anything, and
`DAILY_PROCEDURE.md` never even named which stages to wrap. A stage that
quietly grows slower run over run had no way to surface until
`find_optimizations`'s "performance" finding fired — which needs 5+ runs
of history, and even then only ever names the single slowest stage, not
every stage currently over budget. `runlog.stage_budget_overruns` fixes
both gaps: pinned canonical names close the "which name" question, and
Stage 6's "System health" section reports every overrun on the SAME run
it happened, not five runs later. A stage whose name is not in
`STAGE_TIMING_BUDGETS_MS` is silently unmonitored, not an error — the
budgets themselves are initial estimates, the same judgment-call category
as `quantcore.gap_risk_haircut`, since this system does not yet have
enough real per-stage history to calibrate them against (`HANDOFF.md`
section 12).

## Stage 0, step 0b — a manual fire is not a schedule defect

If a human triggered the run by hand rather than the schedule firing it, the
`fired_on_schedule` check will fail by however far the manual run sits from
06:20. That is expected and is not a defect: say so plainly rather than
recommending a fix for a schedule that is not broken.

## Stage 0, step 1 — the repository is the only source of truth

Stale trading code is more dangerous than a missed session. Do not
reconstruct the repository from anywhere else if the clone fails — abort and
report instead.

## Stage 0, step 3 — a routine sees only its own connectors

A routine sees only the connectors listed in its own configuration, not the
ones connected to the account — this is not a transient cold-start defect,
it will recur if a connector is ever detached. This was the cause of the
first scheduled fire aborting with 9 of 10 tools missing (`HANDOFF.md`
section 11, 31 August 2026).

## Stage 0, step 4 — never hardcode an account number or threshold

`state.json` no longer holds positions, trades, the wash-sale registry, or
run history — those arrays are present only for backward compatibility, are
always empty, and must not be read or written. They are rebuilt every run
per steps 7–9.

## Stage 0, step 6 — never stay silent on an unverified calendar

If `holiday_table_current` fails, the table has passed its horizon it was
verified through — silence must always mean something is broken, so this
gets flagged loudly rather than assumed fine.

## Stage 0, step 7 — split-adjustment and the reconciliation design

**Why split-adjust before anything else.** A multi-year, no-date-floor pull
WILL cross a real corporate split — this is not a hypothetical: on 31 August
2026, 10 of 21 symbols in the individual account failed reconciliation for
exactly this reason (NVDA, CMG, NFLX, VUG, CRWD, and others), because a fill
recorded before a split is one pre-split share, not the several post-split
shares it became, and the broker's current position snapshot is always in
today's post-split terms. A symbol accidentally left out of
`splits_by_symbol` is a no-op, not silently wrong — it will simply fail
reconciliation loudly if it turns out to have split, which is the correct
failure mode.

**Why the jq-not-cp rule for oversized order pages.** A page of ~200 orders
regularly exceeds the inline tool-output limit and gets auto-saved to a
file. On 31 August 2026, a `cp` of exactly such a file triggered a sandbox
permission prompt meant for an interactive human, and a scheduled run has
nobody present to answer it — the run hung indefinitely. A pure read (`jq`,
`cat`, `python -c "json.load(open(...))"`) has not been observed to trigger
this.

**Why opening balances exist, and why they cannot make a residual
disappear.** `journal.opening_balances` is a small, dated, human-recorded
map of shares that arrived outside the order book or before the API's
history horizon — 1 September 2026: MBGL and MSFT, both documented in
`HANDOFF.md` section 11. Any remaining, unrecorded disagreement still aborts
execution: once splits and recorded opening balances are accounted for, the
broker's own positions failing to follow from the broker's own fills means a
transfer, a different corporate action, or a bug, and every downstream
number depends on knowing which. Recording a new opening balance to make an
unexplained residual disappear would defeat the entire check.

## Stage 0, step 7 — why fills and split checks are cached, and positions still are not

**The problem the cache fixes.** Before 4 September 2026, every single run
pulled BOTH accounts' entire order history with no `created_at_gte` and
called `SPLITS` for every unique symbol, every day, forever — a fixed,
growing cost paid in full each morning for history that, past a short
window, cannot change. This is exactly what a live review flagged as
findings worth confirming, and both confirmed true.

**Why the cache holds fills and split events, not positions.**
`HANDOFF.md` section 5's storage rule is that positions are rebuilt every
run, never stored — that is what makes drift structurally impossible
rather than merely detected. Fills and split events are a different kind
of fact: once an order has reached a terminal state and a split's
effective date has passed, both are permanently fixed history, in exactly
the same sense `journal-*.json` entries are — the fold-on-read pattern
already proven safe for the journal applies to them just as well, and
`positions_from_fills` is still called fresh on the full cached-plus-new
fill set every run, still reconciled against the live broker snapshot
every run. Nothing about what gets trusted changes; only how much has to
be re-fetched from the broker to reconstruct it does.

**Why a horizon window, not "cache anything older than the last fetch."**
A Robinhood order is not guaranteed to be terminal the moment it is first
seen — a partially filled order can still complete, or its remainder can
still be cancelled, for some days after it was created. Caching a fill the
first time it is observed would risk permanently under-counting an order
that later filled more, silently corrupting `positions_from_fills` in a
way `reconcile_positions` might never catch (the broker's own snapshot
would simply always disagree by the same missed amount). `ledger.
FILLS_CACHE_HORIZON_DAYS` (7 days) is the bound on how long an order is
trusted to still possibly be open; `fills_ready_to_cache` only writes a
fill to the cache once it is strictly older than that, and the watermark
fetch in Stage 0 step 7 always re-covers that entire trailing window fresh
regardless of what is cached, so a still-mutable order is never trusted
from the cache before it has had time to settle.

**Why splits are cached per-symbol with their own staleness check, not
once and forever.** A split event, once its effective date has passed, will
never change — but a company can announce and execute a NEW split at any
future point while a symbol is still held or a candidate, so "checked once,
trust forever" would let a real split silently escape detection.
`symbols_needing_split_check` rechecks a symbol only after
`SPLITS_CACHE_HORIZON_DAYS` (7 days, matching the fills horizon) have
passed since its last check, bounding detection latency to a known, small
window instead of requiring the endpoint to run for every symbol every day
regardless of whether anything could plausibly have changed.

## Stage 0, step 7 — recording a stop fill for real, and what was deliberately not built

Before 4 September 2026, nothing distinguished a stop-loss fill from any
other sell fill at the Decision-recording layer — `runlog.
find_optimizations`'s "are stops too tight" finding (keyed on `action ==
"stop_filled"` and `inputs.recovered_within_5d`) could therefore never
find a single matching decision. It was dead code keyed on fields nothing
ever wrote.

`runlog.stop_filled_decision` fixes the half that can be fixed cleanly:
every real, filled `stop_market` order becomes a real `stop_filled`
`Decision`, called once per freshly-fetched order in Stage 0 step 7 —
never against a cached fill, since that fill was already recorded as a
decision in whichever earlier run first fetched it fresh; recording it
again on every later run it happens to still be cached would duplicate
the same fill into the journal repeatedly.

**The "did it recover within 5 days" half was deliberately not built.**
That is information about what happens AFTER the decision is recorded,
and the append-only journal has no way to retroactively enrich an
already-written entry — the same constraint that makes a thesis's
maturity outcome a separate `"close"`/`"outcome"` entry days later rather
than an edit to the original `"thesis"` entry. Building it properly needs
either that same separate-entry pattern, applied here, or a live price
lookup `find_optimizations` does not currently take as an input — both
real design decisions, neither implied by "record the fill." Rather than
half-build a finding that can never actually fire, the whole
`stop_distance` finding was removed from `find_optimizations`; the real
`stop_filled` decisions are still in the journal for a human to review
by hand, or for whichever of those two designs a future change picks.

## Stage 0, step 7 — why an unreadable file blocks rather than warns

Before 4 September 2026, `ledger.fold_journal` already recorded a file
that failed to parse in `Journal.unreadable` — but nothing ever read that
list. The run proceeded as if the file had never existed, silently. The
fix is `runlog.preflight`'s `journal_fully_readable` check, `"block"`
severity, fed by the union of `journal.unreadable` and the fills-/splits-
cache folds' own `bad` lists (the same risk, extended to the caches Task
3 introduced the same day).

**Why this must block rather than warn.** A dropped file could hide a
thesis that would have matured (silently corrupting the evidence sample),
an opening balance a human recorded (making a real, already-explained
reconciliation gap look like fresh drift), or a standing circuit-breaker
trip (letting a halted account resume trading nobody actually cleared).
None of these have any other way to be noticed — a warning that a human
might not read carefully enough is not a meaningful safeguard against any
of them.

**Why it must run before ledger reconciliation, not after.** If the
hidden file was the one carrying an opening balance, running
reconciliation anyway would report a confusing, misleading "drift"
failure instead of the actual, nameable cause — a human debugging the
wrong symptom. `RunLog.abort` was changed the same day to keep the FIRST
reason given rather than the last, specifically so this ordering is not
undone by a later, consequential check also calling `abort()`.

## Stage 0, step 8 — the wash-sale registry is rebuilt, never stored

A stored copy that has forgotten a loss sale approves the repurchase that
disallows it — which is exactly what happened on the first live run.

## Stage 0, step 9 — why `download_file_content`, not `read_file_content`

The watchdog's first verification run (1 September 2026) found
`read_file_content` markdown-escapes JSON text (backslash-escaping
underscores and brackets), which breaks `json.loads` silently:
`ledger.fold_journal` catches the parse failure and drops the file into
`unreadable` rather than raising, so a whole day's theses/opening-balance
history can vanish from the fold with no visible error. `download_file_content`
returns raw base64 and does not have this problem.

## Stage 0.6 — a different question from Stage 0.5, and why it must actually run

`runlog.score_closed_decisions` existed before 4 September 2026 and was
never called from anywhere — a defined but permanently dormant function,
exactly the "no prior-day review" gap an early review of this system
flagged. Stage 0.6 fixes that by calling it for real, every run.

**Why this is not a duplicate of Stage 0.5.** `evidence.assess` grades
the record against ONE specific, pre-registered claim (`target_edge_pct`,
`assumed_sd_pct`), with a Bonferroni correction for repeated looks — a
strict statistical test whose answer is "is there evidence for the exact
thing we set out to test." `score_closed_decisions` asks a plainer
question with no pre-registered claim behind it at all: honestly, what
has the hit rate and mean return actually been. Both matter, and they can
disagree — a small sample can show a positive mean return
(`score_closed_decisions`) while still being nowhere near enough evidence
to clear the pre-registered bar (`evidence.assess`), and reporting only
one would hide the other's answer.

**Why `journal.closed_for_scoring` joins two entry kinds rather than
reading `"outcome"` alone.** `evidence.Outcome.to_dict()` — what an
`"outcome"` entry's payload actually carries — has no field named
`outcome_pct` or `horizon_days`; `runlog.score_closed_decisions` needs
both. `excess_pct` (the only number `Outcome` itself calls meaningful) is
the honest stand-in for `outcome_pct`, and `horizon_days` only exists on
the original `"thesis"` entry, so the two have to be joined by
`thesis_id`. This is the same class of schema-drift risk `ledger.run_entry`
(section 6) exists to pin down elsewhere — a function whose caller and
reader disagree about field names fails silently, not loudly.

## Stage 1 — why the adjusted endpoint, and why compact payloads

**Adjusted, not raw, prices.** The unadjusted endpoint returns raw prices, so
a stock that split inside the window shows a cliff that is not a price
move: CRWD's 4-for-1 read as 293% volatility, and the sizing that flows from
that number would have been wrong for as long as the split sat in the
window.

**Why `datatype=csv`, `outputsize=compact`.** Some endpoints return 70,000+
characters uncompacted and will blow the tool output budget.

**Why compact's ~100-bar limit matters.** `quantcore.vol_percentile` and
`trend_state` need 252 and 200 days respectively; below half coverage,
`vol_percentile` fails outright rather than reporting a number flagged only
`"thin"`, because a number this system itself cannot verify must not
influence sizing or the gate.

**Why `research.py` calls `CONGRESS_TRADES`/`INSIDER_TRANSACTIONS` per
symbol, and why `INSIDER_TRANSACTIONS` needs `return_full_data=true`.**
`research.py`'s first version was written against fixtures the author
hand-wrote, not responses either API had actually returned, and a live
check on 4 September 2026 found both endpoints do not support a bulk
multi-symbol pull the way the first version assumed — one call per symbol,
full stop (`HANDOFF.md` section 11, "the fixtures were fabricated"). The
same live check hit `INSIDER_TRANSACTIONS`'s real preview truncation on the
very first call for a name with a long transaction history (OXY: 27,944
lines, 248,328 tokens) — `return_full_data=true` converts that into a
genuine harness file-spill instead of Alpha Vantage's own lossy
`sample_data` sample, which is why Stage 1 says to read it back with
`jq`/`json.load`, the same convention already established for oversized
`get_equity_orders` pages, rather than treating the preview as the answer.

**Why `bundle.coverage_issues()` gets checked after every gather.** The
same live check showed `ResearchBundle.skipped` alone cannot tell "feed not
fetched" apart from "feed fetched, parsed to zero items because a field
name was wrong" — OXY had 58 real congressional trades that a
field-name mismatch would have silently rendered as nothing, with every
check staying green. `coverage_issues()` (rows seen, zero items produced)
is the check that catches that class of bug instead of a human catching it
by accident.

## Stage 2 — why the concentration recalibration

`correlation_concentration` reports both a shrunk and an unshrunk view and
flags `concentrated` when effective bets fall under half the number of
names examined, or the eigen-share exceeds 0.45 — the old 0.60
eigen-share-only cutoff never fired for a realistically correlated equity
book (see `HANDOFF.md` section 11, 31 August 2026: a known-answer portfolio
at a true 0.55 correlation was called unconcentrated).

**Why the two thresholds are read from `config`, not hardcoded.** Both
numbers were parameters of `correlation_concentration` only as of 4
September 2026 — before that, `state.json`'s config already documented
`concentration_bets_floor_ratio`/`concentration_eigen_share_cap` as
though they were live inputs, but the function never accepted them, so
changing either value in config silently did nothing. The function
defaults to the same 0.5/0.45 recalibrated values when config omits the
key, so an unconfigured run behaves exactly as before.

## Stage 3 — why `gate_failed` must be one of `runlog.GATE_CONDITIONS`, exactly

Before 4 September 2026, `gate_failed` was free text — whatever string
the run happened to write. `runlog.closest_calls` (Stage 6, "report the
closest call when nothing clears the gate" — an early review flagged the
missing capability directly) ranks a rejection by where its `gate_failed`
value falls in the five conditions' fixed, published order (`HANDOFF.md`
section 7): the later the failing condition, the more of the gate the
idea actually cleared before it failed. A misspelled or freely-worded
condition name is invisible to that ranking — it cannot be placed in the
order, so it is silently excluded rather than ranked, which would make
"nothing today" look identical whether the closest miss failed on
condition 1 or condition 5. Pinning the five exact strings in code, not
prose, is what makes the ranking possible at all — the same reason
`ledger.run_entry` pins the `"run"` entry's schema (section 6) rather than
trusting a description to stay in sync with what reads it.

A rejection recorded before the idea even reached the gate — a
data-quality rejection, Stage 1 — deliberately uses different text
(`"data quality"`, not one of the five). It is excluded from
`closest_calls` for the same reason a `gate_failed` typo would be: it
never reached the gate, so ranking it as a near miss would be wrong in
the opposite direction — reporting a fundamentally unusable idea as
almost having cleared everything.

## Stage 4 — why individual-account suggestions count as evidence

The pre-registered evidence claim is about whether the five-condition gate
itself has an edge, not about which account executes on it, so
individual-account suggestions count toward the sample exactly as
agentic-account trades do — and given how few ideas clear the gate on a
typical day, this roughly doubles the rate evidence accumulates.

## Stage 5 — the circuit breaker is enforcement, not a status line

Before 4 September 2026, the procedure only said to "report where equity
sits relative to" `circuit_breaker_usd` and `hard_stop_usd` — nothing in the
codebase ever stopped an order because of them, which is not what a circuit
breaker is for. `runlog.circuit_breaker_check` fixes that; see `HANDOFF.md`
section 11 for the full account. A `circuit_breaker_cleared` entry is
written only by a human because a V-shaped bounce the very next morning
must not silently resume trading on its own — recovered equity is not
itself a clearance.

`WATCHDOG_PROCEDURE.md`'s hard limits already forbid the self-heal path from
writing a clearance entry or touching this check to make a run pass; if a
"fix" would require either, that is the signal to stop and report it, not
route around it.

**Why the sizing call structure.** `buying_power`, not `total_value`, is
what a cash account with T+1 settlement can actually spend — they are very
different numbers, and sizing against equity alone can produce an order the
broker rejects outright. `gap_risk_haircut` shrinks the effective risk
budget by default because stops cannot execute outside regular hours and a
gap can pass straight through one.

**Why `cancel_equity_order` is now part of the stale-order path.** A stale
resting stop reserves shares and the broker rejects the whole order rather
than filling what it can (`EQUITY_MAX_SELL_SHARES_EXCEEDED`). This tool did
not exist when the system was first designed; it was confirmed working 31
August 2026, so a stuck order no longer has to be waited out.

## Stage 6 — why the schema and the five-section cap

**Why `emailer.verify_email` runs before `render_email`, and why it
raises rather than warns.** The email is the one artefact a human
actually reads and the one place a fabricated number or an unsupported
claim would do real damage — silently trusted because it came from the
system's own report. `verify_email` checks, before anything renders: a
card's `quantity` against the actual recorded `Decision`; that every
bullet in the two account sections has a source; that the source is
either a real research-bundle source or matches
`ALLOWED_SOURCE_PREFIXES` (this repo's own data-provider and module
naming conventions); and that every other number in a bullet or a card's
`detail` can be traced, exactly or within a stated tolerance, to the
manifest, the research bundle, or a broker response — dates and ordinals
exempted, since "reports 9 Sep" is not a financial claim needing a
source the way "up 47% this week" is. It raises `ValueError` rather than
logging a warning specifically so a run cannot accidentally catch the
exception and send anyway; if it fires, the fix is the card or bullet
that produced the untraceable claim, never the check.

**Why `render_email` calls `verify_email` itself, rather than the
procedure calling both.** For one day, `verify_email` was a second call a
caller had to remember to make before `render_email` — an instruction,
not an enforcement, and this system had already been burned three times
by exactly that shape of gap: the journal's `unreadable` list nothing
read, the `find_optimizations` findings keyed on fields nothing wrote,
the research `coverage_issues()` conflation. A run that simply forgot the
`verify_email` call would render an unverified email and nothing would
complain — the one check where that failure is least acceptable would
have been the one check in `emailer.py` that was optional. The fix closes
the seam rather than warning about it harder: `render_email` now takes
`agentic_ideas`/`suggestion_ideas` as structured data, not pre-rendered
HTML, builds the two account sections itself, and runs `verify_email` on
them first, unconditionally. There is no parameter that accepts
pre-built account HTML, so a caller cannot express "render without
verifying" even by mistake. `verify_email` stays independently callable
for tests, but production code has exactly one path to a sent email, and
it always passes through the check.

**Why "always create a new file, never modify one."** The Drive connector
rewrites metadata but not contents.

**Why the `"run"` entry's payload is `ledger.run_entry(log)`, never
hand-built.** Before 4 September 2026, the only instruction was to write
"a compact summary for `find_optimizations`" — prose, not a pinned
contract. `runlog._regressions` and `runlog.find_optimizations` read
specific fields (`health`, `duration_ms`, `decisions[].action`/
`.inputs.recovered_within_5d`/`.gate_failed`/`.executed`,
`stages[].name`/`.duration_ms`) via `dict.get(..., default)` throughout,
which means a field that drifted out of sync between what got written
and what those functions expect would not raise — it would just silently
stop contributing to the optimization findings, a failure nobody would
notice until a known pattern stopped showing up for no visible reason.
`ledger.run_entry` pins the exact field set in code, tested end to end by
folding a `run_entry` payload back out of a journal and feeding it
straight to `runlog.find_optimizations`.

**Why the 06:20 routine stays silent on an abort.** The watchdog (fires 60
minutes later, reads `WATCHDOG_PROCEDURE.md`) owns deciding what happens
next — diagnosing, attempting a fix, and re-running this same procedure.
Sending a partial "sorry, broken" email from the direct routine would just
be a second email nobody asked for once the watchdog's retry lands.

**Why at most five sections, as cards, and why enforced in code.** The
user does not want a market-commentary newsletter. The section list was
"Evidence review" / "Where things stand" / "What moved and why" / "Risk
measurement" originally, cut to three (agentic activity, individual
suggestions, system health) on 1 September 2026 — and restructured to
five on 4 September 2026, adding "Prior-day review" and "Diversification"
back in, because both existed in the run's own data
(`runlog.score_closed_decisions`, `quantcore.correlation_concentration`)
with nowhere to appear: the original three-section cut had quietly
dropped real information along with the newsletter tone, not just the
tone. `emailer.CANONICAL_SECTIONS`/`MAX_SECTIONS` enforce this in
`render_email` itself now, not only in this prose — a caller that starts
appending a sixth section gets a `ValueError`, the same drift-back-to-
four risk the 1 September cut only guarded against by asking nicely. The
two account sections are cards, one per symbol, never a paragraph, since 4
September 2026: a name, an action, and a quantity buried in a sentence
are slower to scan than the same three things in a card's first line.
Every bullet is tagged with the specific source that supports it — a
named data provider (`"Alpha Vantage"`), a specific report (`"EIA STEO, 9
Sep"`), a computed check (`"quantcore.stop_plan"`), a tool result
(`"review_equity_order"`) — not a vague "research suggests".

**Why "Prior-day review" and "Diversification" are separate sections, not
folded into "System health".** `evidence.assess` and
`runlog.score_closed_decisions` (Stage 0.5, Stage 0.6) both answer a
track-record question — is there evidence for the pre-registered edge,
and honestly what has the hit rate been — which is a different kind of
question from "did the run complete without breaking." Folding both into
"System health" is how `score_closed_decisions` stayed uncalled for as
long as it did: a function whose output has no section to appear in is
easy to leave uncalled indefinitely. `correlation_concentration` gets the
same treatment for the same reason — it was always computed in Stage 2
and never had anywhere in the email to show up.

**Why the "System health" section never omits a watchdog note.** The user
reads this section specifically to know the system stopped itself or fixed
itself; omitting the note when one applies defeats the point of building
the self-heal loop at all.

**Why silence is never the outcome.** The 06:20 routine may stay quiet on an
abort, but by the time the watchdog's pass is done — whether it fixed
something, couldn't find a confident fix, or its own retry also aborted —
exactly one email always goes out.

---

# Why `WATCHDOG_PROCEDURE.md` says what it says

## Why the watchdog exists at all

On 31 August 2026, a run froze indefinitely on a sandbox permission prompt
meant for an interactive human (the same `cp`-vs-`jq` issue documented
above), and because it never reached Stage 6, it sent no email at all —
the one outcome the daily procedure is built never to produce, reached
anyway, from the outside, by a run that could not know it had failed.
Nobody noticed until asked to check by hand.

## Stage 2 — why `download_file_content`, again

Same root cause as `DAILY_PROCEDURE.md` Stage 0 step 9: `read_file_content`
markdown-escapes JSON, discovered by the watchdog's own first verification
run, 1 September 2026.

## The recheck before concluding `no_run`

A research-heavy morning has taken as long as 39 minutes end to end; the
watchdog's cron offset is sized with that in mind but is not a guarantee.
On 2 September 2026, skipping the recheck would have started a full retry
of the day's trading — cloning the repo, pulling both accounts' full order
history — while the original 06:20 run was still writing its own files,
exactly the kind of same-day duplicate run that has caused real damage
before. That run caught it by chance, re-listing Drive on its own
initiative after noticing a suspiciously-timed file appear mid-check; the
recheck step makes that a designed behavior instead of a lucky one.

## Stage 5 — why the self-heal limits are absolute

The user explicitly authorized the watchdog to merge its own fixes directly
to `main` without a human reviewing a PR first (1 September 2026: "I don't
care if you merge into main... this is your money to play with... I want
this system to be automated and self functioning/healing"). The three hard
limits (never touch `place_equity_order` code, never weaken a Stage 0
safety check, never alter the `THIS IS A DRY RUN` guard) are deliberately
carved out of that authorization: an incorrect or over-eager autonomous fix
to a system that places real orders is a worse outcome than one day's
trading not happening. Going live is a separate, one-time human decision,
documented in `HANDOFF.md`'s "Path to live trading" section — not something
this authorization extends to.

## Why the watchdog carries the trading connectors

The watchdog carries Robinhood and Alpha Vantage, not just Gmail and Drive,
specifically so a same-day retry after a fix can actually finish the day's
trading (or, under the DRY RUN guard, simulate it) rather than waiting for
tomorrow — a deliberate choice made 2 September 2026, trading a larger
technical surface (two sessions instead of one holding the trading
connector) for the ability to fix and finish the same day.

## Stage 0 step 8 — why the wash-sale report is a pinned schema, not a note

Discovered 5 September 2026, during a rehearsal that reran the real Stage 0
directly against the real 870 fills both accounts had already produced that
morning. The rehearsal's rebuild blocked seven symbols (CMG, CRM, GLDM,
MRVL, MU, TSLA, XLE); that morning's own journal `"note"` entry named only
two (GLDM, XLE). Read on its own, that looks exactly like the failure this
system is built to prevent — `HANDOFF.md` section 11's registry that had
forgotten a loss sale and approved the repurchase that disallowed it,
recurring in a new shape.

It was not that. Every one of the five missing symbols was re-verified
directly against `washsale.Registry.blocked_symbols`, called on the actual
fills, with each symbol's real split history fetched fresh (three of the
five — MRVL, CRM, TSLA — had never been split-checked before because
none of them are currently held, so no earlier run had reason to fetch
their splits): all five came back genuine, unexpired loss sales, the
registry itself never stopped blocking them. The run-manifest's own
`washsale_registry_rebuilt` check confirmed it too, on both days: "870
split-adjusted trades, both accounts, never read from storage" — no symbol
list at all, block-severity, nothing more. The only place either day's
blocked-symbol list existed anywhere was a free-form journal `"note"`
entry, hand-composed by that morning's run for the email's benefit. One
morning wrote out the complete list; the next, for whatever reason,
wrote only the two names relevant to that day's actual candidates (GLDM
was a held position being reconsidered, XLE a rejected one) and never
mentioned the other five, who touched nothing decided that day.

The registry was correct and identical both times. Nothing had ever
required a run to log what it actually returned, so two truthful summaries
of the same correct computation looked, side by side, like the computation
disagreeing with itself — indistinguishable, from a Drive folder, from the
real thing this file exists to guard against. `washsale.Registry.report`
now IS the journal record (never a hand-typed summary of it), and
`runlog.washsale_registry_stable` compares this run's report against the
last one on file, block-severity only, exempting a symbol only if its OWN
recorded `clears_on` date has actually passed — treating any other
disappearance as the genuine, blocking regression it would be if it ever
really happened. This mirrors `ledger.run_entry`'s existing fix for the
identical shape of problem in run-history reporting.

## Stage 0 step 7 — `all_symbols` is derived from fills, in code, not assembled

Found while investigating the wash-sale note above, 5 September 2026. The
manual reconciliation that surfaced the note's missing five symbols had
itself built its split-check list from currently HELD positions (24
symbols) rather than from the combined fill history (70 symbols) — and
three of the five missing wash-sale symbols, MRVL, CRM, and TSLA, are
exactly the ones that difference excludes: all fully sold, none held that
day, all still needing their own split history for `cost_basis`,
`loss_sales`, and the registry regardless.

Whether the live daily runs themselves ever made the same mistake could not
be settled directly — each morning's run is a fresh session with no
persisted trace of how it built the list, only the final "SPLITS x68" call
count. That count sits far closer to `all_traded_symbols`'s ~70 than to the
~24 held positions across both accounts, which is real evidence the daily
runs had been deriving it from fills correctly all along — but evidence
from a call count is not the same guarantee as a function whose contract IS
the correct set. `DAILY_PROCEDURE.md` step 7 previously described
`all_symbols` only in prose ("every unique symbol across both accounts'
combined fills") with no function that actually built it — an instruction
a careful reader follows and a rushed one quietly narrows to whatever is
already at hand, which for a reconciliation task is positions. It is now
`ledger.all_traded_symbols(fills)`: a symbol either account has ever traded
enters the set the day it appears in a fill and never leaves it, closed
position or not.

## Stage 1 — `NEWS_SENTIMENT` fetched one symbol at a time, never batched

Found 5 September 2026, during the same rehearsal as the wash-sale note
above. Five `NEWS_SENTIMENT` calls, one per ticker (OXY, XOM, GLDM, AAPL,
SGOV), were issued in a single parallel batch. Only the first (OXY) came
back correctly filtered — fifty articles, all fifty naming OXY in their own
`ticker_sentiment`. The other four came back real, well-formed, on-topic
responses about a DIFFERENT ticker than the one requested: the "XOM" call
returned 100% GLDM-tagged articles, the "GLDM" call returned AAPL articles,
and so on. Retrying the identical four calls sequentially, one at a time,
fixed all of them immediately.

This is worse than a shape error. The source is real, the headline is real,
every number in it traces to something — it is a fabrication path that
reaches the five-condition gate's `two_sources` corroboration check and
survives `verify_email` intact, because nothing about the article itself is
false; only its attribution is. It is detectable at all only because every
article carries its own `ticker_sentiment` array naming the tickers it is
actually about, each with a `relevance_score` — a fact not previously used
by this parser. `research.news_items_from_alpha_vantage` now filters on
that field and fails loudly (one `quality="failed"` item, not a quietly
shorter list) when most of a response's articles do not name the requested
symbol. That filter is the second line of defence. The first, cheaper one
is this rule: fetch `NEWS_SENTIMENT` one symbol per call, never batched —
whatever causes the cross-wiring (almost certainly a parameter-binding or
response-caching fault on the tool side, triggered by concurrent calls with
different arguments) has no chance to fire if there is never more than one
such call in flight.
