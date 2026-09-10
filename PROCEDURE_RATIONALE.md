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
directly against the real fills both accounts had already produced that
morning (real fill count, real symbols — not repeated here; a blocked-
symbol list is account-specific data of the same class as an account
number, and this file is public. See `HANDOFF.private.md` if the exact
symbols matter for a future investigation). The rehearsal's rebuild
blocked several symbols; that morning's own journal `"note"` entry named
only two of them. Read on its own, that looks exactly like the failure
this system is built to prevent — `HANDOFF.md` section 11's registry that
had forgotten a loss sale and approved the repurchase that disallowed it,
recurring in a new shape.

It was not that. Every one of the missing symbols was re-verified directly
against `washsale.Registry.blocked_symbols`, called on the actual fills,
with each symbol's real split history fetched fresh (several of them had
never been split-checked before because none are currently held, so no
earlier run had reason to fetch their splits): all came back genuine,
unexpired loss sales, the registry itself never stopped blocking them. The
run-manifest's own `washsale_registry_rebuilt` check confirmed it too, on
both days — split-adjusted trades, both accounts, never read from storage
— no symbol list at all, block-severity, nothing more. The only place
either day's blocked-symbol list existed anywhere was a free-form journal
`"note"` entry, hand-composed by that morning's run for the email's
benefit. One morning wrote out the complete list; the next, for whatever
reason, wrote only the names relevant to that day's actual candidates and
never mentioned the rest, who touched nothing decided that day.

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
manual reconciliation that surfaced the note's missing symbols had itself
built its split-check list from currently HELD positions (24 symbols)
rather than from the combined fill history (70 symbols) — and several of
the missing wash-sale symbols are exactly the ones that difference
excludes: fully sold, none held that day, all still needing their own
split history for `cost_basis`, `loss_sales`, and the registry regardless.

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

## Stage 4 — the sector cap is computed, not just documented

Found 5 September 2026, same rehearsal. `sector_cap_individual` has been in
`state.json.config` and documented in `HANDOFF.md` since early on;
`DAILY_PROCEDURE.md` Stage 4 said to "flag any breach of the ... sector cap
from config" in the same sentence as the single-name cap and cash floor,
both of which real code actually checks. Nothing computed a sector
breakdown at all — no function existed to call, so the instruction could
only ever be followed by judgment, which is exactly the class of gap that
produced the wash-sale note and the `all_symbols` list above.

Unlike `vol_percentile`/`trend_state`, deliberately left unfixed because
they need a 252/200-day price history genuinely too expensive to pull for
every held-or-candidate symbol every morning, a sector breakdown needs
nothing beyond a weight per symbol — quantity times the live quote
`get_equity_quotes` already returns for every held-or-candidate name each
run, divided by account equity. There is no cost tradeoff to make here and
therefore no cache to build; `quantcore.sector_exposure` is computed fresh
every run from data the run already has.

## `STAGE_TIMING_BUDGETS_MS["preflight"]` — 180s, not 15s

The original 15-second budget was never measured against what Stage 0
actually does; it tripped `stage_budget_overruns` on every real run that
ever logged a preflight duration, which made an overrun mean nothing.
Observed real durations: 95s (4 September 2026) and 139s (1 September
2026), pulling roughly 1,500 orders combined across both accounts and
checking splits for every symbol either has ever traded (~68-70 names, see
`ledger.all_traded_symbols` above) before a single price is looked at.
180s gives headroom over both without being picked merely to stop the
check from firing — the point of a budget is that crossing it means
something, and 15s never let it.

## Stage 1 — the screener uses enum filters only, and needed a price floor added after the fact

Designed 5 September 2026, in response to the standing complaint that the
five-condition gate looks strict mainly because almost nothing is being
brought to it. `create_scan`'s and `update_scan_filters`' own docstrings
reference `get_scanner_datapoints` and `preview_scan` as the way to build
and sanity-check an expression filter (e.g. "price between what the
agentic account can size and what `stop_plan` can size against," expressed
directly rather than approximated by an enum range). Neither tool is
actually callable this session — calling them returns nothing, not an
error that could be handled. Rather than guess at an expression filter's
syntax with no way to check it against real data, `screener.CORE_UNIVERSE_FILTERS_V1`
uses only enum filters from the real, live `get_scanner_filter_specs`
catalog (recorded verbatim in `fixtures/scanner/get_scanner_filter_specs_20260905.json`,
not assumed from memory or from the tool's description text).

That catalog does not include a price filter tight enough on its own:
liquidity (`FILTER_TYPE_AVERAGE_VOLUME` > 500,000, 30-day) and volatility
(`FILTER_TYPE_HISTORICAL_VOLATILITY` between 0.15 and 0.80) alone let
sub-$0.35 names through — PASW at $0.12 among them, verified in the real
`run_scan` results before the fix. A stock at that price is not a
meaningfully sizeable idea for an account with a whole-share, position-cap
sizing rule; it is noise that would have to be filtered by hand downstream
every single day. `FILTER_TYPE_LAST > 5` was added afterward, once the
unfiltered results made the gap visible, not designed in up front — the
real result set is the thing that caught it, which is the same argument
this project has made for every other "verify against a live call, not a
docstring" decision in this file.

The scan's default sort ("Last desc") was also wrong for this use case and
caught the same way: `run_scan` caps results at 200 of the ~400 real
matches, and sorting by price descending means the 200 THROWN AWAY are the
cheap half — exactly the half relevant to "what can this account afford."
`update_scan_config` re-sorts "Last asc" so the kept 200 are the affordable
end.

One real finding is recorded as unresolved rather than guessed at: the
scan's `Sector` result column returns opaque numeric codes ("311", "206",
...) with no decoding table exposed by any available tool. `screener.parse_scan_result`
passes the code through raw as `sector_code` and does not attempt to map
it — sector data for the sizing/exposure math instead comes from Alpha
Vantage's `COMPANY_OVERVIEW`/`ETF_PROFILE`, which use real named sectors,
not from this column. Decoding it is future work, not a blocking gap,
because nothing downstream currently reads `sector_code`.

## Stage 1 — congress-discovery: committee leadership was the wrong selection criterion, tried and reverted

Designed 5 September 2026. `congress_trade_items`/`insider_transaction_items`
can only ever CONFIRM a symbol already in `held_or_candidate` — they take a
ticker and return trades in it, so they cannot widen the universe on their
own. `CONGRESS_TRADES` separately accepts a `bioguide_id` and, given one,
returns that member's entire disclosed trade history regardless of ticker —
a genuine discovery path, but only if there is a principled way to choose
whose `bioguide_id`s to track, since tracking all 1,144 members in
`POLITICIAN_METADATA` daily is neither affordable nor a meaningful signal.

The first criterion tried was financial-committee leadership — French
Hill, Maxine Waters, Tim Scott, Elizabeth Warren — chosen for looking
objective and politically neutral. Their real `bioguide_id`s were verified
live against `POLITICIAN_METADATA` (H001072, W000187, S001184, W000817),
then each was actually queried against `CONGRESS_TRADES`. All four came
back with zero disclosed trades. This is very likely genuine — committee
leadership in the chamber that regulates markets is exactly the position
most likely to sit behind a blind trust — but a selection criterion that
verifiably surfaces nothing is not a selection criterion, it is a dead
end, and the fix was to change the criterion rather than keep it and hope
a future run finds something.

The replacement is data-driven rather than hand-picked: `bioguide_id` was
added as a field on each `congress_trade_items` result (it was already in
the raw API response and simply was not being kept), so any politician who
has ALREADY disclosed a trade in a symbol this system already examines
enters the tracked set automatically. This was verified against real data,
not assumed to work: bioguide_id C001123 (Gilbert Cisneros, sourced from
the real OXY fixture already on file) returned 2,248 real disclosed
trades, including DSGX and TXRH — both symbols this project's
`held_or_candidate` universe had never seen before that call. A hand-picked
roster requires the roster-picker to already know who is worth tracking,
which is the same "wasted research" problem as the price/volatility
mismatch above; a set built from who has already shown up trading in this
system's own universe needs no such guess and grows only as the universe
itself grows.

## Stage 0 — `diagnose()` distinguishes a lapsed brokerage token from a misconfigured connector

Designed 5 September 2026. Both failure modes present identically today:
`tools_available` fails because every Robinhood-prefixed tool is missing
from the manifest. But the fix is completely different — a lapsed OAuth
token needs the user to sign in again; a routine with connectors never
attached needs a configuration change on the routine itself — and an
operator reading a generic "tools not available" cause has no way to tell
which one they are looking at without independently checking both.
`_diagnose_tools_available` reuses the missing-tools list `tools_available`'s
own check already populates and asks a narrow question: is every missing
name Robinhood-prefixed? If so, name it as a token-expiry cause and
remedy specifically; otherwise, keep the existing generic "connectors not
attached" text, since a genuinely mixed missing-tools list means something
broader than just the brokerage connection lapsed.

This only helps after the fact, at the moment a run has already failed on
it. `runlog.brokerage_token_health` (backed by `Journal.days_since_last_brokerage_success`,
itself backed by the new `run_entry` field `brokerage_ok`) is the
proactive half: the observed expiry window for this connector is roughly 4
days (`HANDOFF.md` section 12), so a `warn`-severity System-health line
fires once 3 days have passed since the last confirmed-successful
brokerage call — a day of runway before the failure a same-day operator
would otherwise discover only from a bounced run.

## Stage 1 — the researched set is a budget, ranked by the gate's own first condition

Found 5 September 2026, immediately after widening the candidate universe:
the same session that added a real scanner (hundreds of matches) and
congressional discovery (thousands of trades per tracked member) also
fixed `NEWS_SENTIMENT` to fetch strictly sequentially, one symbol per
call. Individually correct; multiplied together, they made Stage 1
unbounded and serialised on its slowest call at the same time. A rehearsal
measured roughly 25 seconds of wall-clock time per researched symbol
(~620s for ~25 symbols) BEFORE either change — an eligible universe in the
hundreds would turn that into a run unable to finish before the market
opens or before the watchdog's own 60-minute limit fires, and the actual
failure mode would not be a clean abort: it would be that morning's agent
quietly researching some improvised subset instead, which is exactly the
nondeterminism `research.py` exists to remove, just relocated one level up
and made harder to see.

The fix keeps the eligible universe exactly as wide as it is —
`research.candidates()` is a filter result, not a resource decision, and
narrowing it back down would undo the whole point of widening it — and
instead bounds a NEW, separate concept: `research.researched_set`, the
subset Stage 1 actually spends calls on today. The ranking inside it is
deliberately NOT "how attractive does this name look" — that would smuggle
investment judgment into what is supposed to be a pure budget cutoff, the
same mistake congressional discovery's first committee-leadership attempt
made in a different shape. Instead it ranks by the gate's own first
condition: a named catalyst WITH A DATE. A symbol with no dated catalyst
inside the horizon cannot clear the gate today regardless of how much
research it receives, so researching it ahead of one that could clear the
gate is waste, not diligence. `EARNINGS_CALENDAR`'s bulk pull (already
fetched for the narrower `earnings_calendar_items` call) costs nothing
extra to also check against the full eligible universe via
`symbols_with_dated_catalyst` — one bulk response, two consumers.

Congressional recency is the next tier, not because it is a weaker signal
in principle, but because it is the next-cheapest one actually available
without spending the calls the ceiling exists to avoid: `congress_recency`
comes from data `congress_trade_items_by_politician` already fetched for
discovery, at zero extra cost. Insider-activity recency is explicitly NOT
in this tier for the same reason committee leadership was abandoned as a
selection criterion for discovery itself — `INSIDER_TRANSACTIONS` has no
bulk, symbol-agnostic pull, so knowing whether an eligible-but-not-held
name has recent insider activity would require calling it per symbol
before the researched set is even decided, which is circular. Held
positions already get insider data regardless (they are always
researched, tier or no tier), so this gap costs nothing where it matters
most; it is recorded honestly in `researched_set`'s own docstring rather
than silently claimed as a signal the function does not actually see.

`21` days as `CANDIDATE_CATALYST_HORIZON_DAYS` matches an existing horizon
already in use elsewhere in this codebase (an open thesis's own horizon),
kept consistent rather than introducing a second, unrelated number for a
similar-sounding concept. `40` as `DEFAULT_RESEARCH_SET_CEILING` is a
starting point, not a tuned constant: ~25s/symbol observed, times a target
of keeping the gather stage to roughly 15-20 minutes inside the run's
60-minute total budget, landing near 40-48 symbols; 40 was chosen with a
little headroom rather than at the edge of that estimate. `cut_for_budget`
is recorded specifically so this number can be revisited from evidence —
if it stays large every day, the ceiling is wrong and should move, and
that is a fact this system should surface, not one that should require
asking.

## Stage 1 — congressional discovery needed a recency and materiality bound

Found 5 September 2026, same session. `CONGRESS_TRADES(bioguide_id=...)`
has no date-range parameter and always returns a member's ENTIRE disclosed
trading history — one real bioguide_id returned thousands of trades
spanning years. Without a bound, a purchase from years ago became a
candidate on equal footing with one disclosed last week, and
`researched_set`'s congressional-recency tier had no honest "recency" to
rank by if the underlying set was not actually recent.

`research.recent_congress_items` filters on two independent things:
`transaction_date` within `CONGRESS_DISCOVERY_RECENCY_DAYS` (90) of `asof`,
and `amount_min` at or above `CONGRESS_DISCOVERY_MIN_AMOUNT` ($15,000).
Ninety days is wide enough to survive the up-to-45-day disclosure filing
lag Congress's own STOCK Act allows without discarding a trade that is, in
practice, still recent — half the window is deliberate slack, not a number
picked to look round. $15,000 is the floor of the smallest disclosure
bracket Congress itself uses (`$1,001-$15,000`); excluding it keeps a
member's smallest, least-informative disclosures from diluting a real one.
This does NOT additionally require a second disclosure before a symbol
counts, which was considered and rejected: requiring multiplicity would
discard the single freshest possible signal (a disclosure made yesterday,
alone on file) in exchange for a corroboration test that
`researched_set`'s recency-weighted ranking is already better positioned
to handle on its own — a single very recent, above-floor disclosure
outranks an old one regardless, without needing the old one thrown away
first. This is a different concept from `ledger.CONGRESS_DISCOVERY_HORIZON_DAYS`
(the weekly cadence for how often a member's full history is RE-PULLED,
unaffected by this change) and is not a replacement for it.

## Stage 0 step 1 — a real scan_id is account-specific data, not a code constant

Found 5 September 2026: the scan created for `screener.py` had its real
scan_id committed, by name, in `screener.py`, `DAILY_PROCEDURE.md`, and
`HANDOFF.md` — all three public. This repository's own existing rule
(`HANDOFF.md` section 5) is that account numbers and storage identifiers
live only in `HANDOFF.private.md`, never the public repo; a saved-scan
identifier on the live account is exactly that class of thing and had
simply not been recognised as one when it was first created. Fixed by
moving the value to `HANDOFF.private.md`'s config table under
`screener_scan_id` and replacing all three public references with the
config key name (`get_scans` plus a title match, `CORE_UNIVERSE_SCAN_TITLE`,
recovers it if it is ever lost). The same pass also found the wash-sale
registry note-drift finding's rationale (Stage 0 step 8 above) had, while
documenting a REPORTING bug, itself reported a real blocked-symbol list —
the actual composition of a real wash-sale block is exactly as
account-specific as a scan_id or an account number, and both the
`DAILY_PROCEDURE.md` and this file's account of that finding were rewritten
to describe the shape of the bug without repeating which real symbols were
involved.

## Stage 0 / Stage 6 — a heartbeat, because a budget is not a timeout

Found 5 September 2026. `STAGE_TIMING_BUDGETS_MS` (the `preflight` and
`gather` entries above) are advisory: `stage_budget_overruns` reports after
the fact, to an email that only gets sent if the run finishes. The nine
budgets sum to roughly 35 minutes against a 60-minute watchdog offset, which
looks like 25 minutes of headroom -- but nothing stops a stage that runs
long, so the real worst case was unbounded, and the failure it leads to had
already happened once: on 2 September a 39-minute run against a
30-minute watchdog offset nearly triggered a second, duplicate trading run
(`HANDOFF.md` section 11) before the offset was widened to 60 with a single
recheck. Both of those are still finite numbers. A gather stage that stalls
on a single hung external call puts a run past 70 minutes, and the
watchdog -- unable to tell "slow" from "stuck" from a Drive folder alone --
concludes `no_run` and begins the exact same retry race, just later.

**Widening the offset again was considered and rejected.** It is the same
patch a third time: trade one unbounded wait for a longer unbounded one,
with no new information gained. The actual gap is that the watchdog has
never had anything to read except a manifest, which by definition does not
exist until the run is either finished or aborted -- there was no way to
distinguish "in progress" from "not going to happen" from outside. The fix
is for the run to say so itself, continuously, and cheaply enough that
writing it never becomes the reason a run is slow.

**Design constraint discovered while building this**: the Drive connector
can create a file but not modify one already written -- the exact
limitation `ledger.compact_journal_month` already documents for the
journal. "Update it as each stage completes" therefore cannot mean editing
one file's content in place; it means a new file each time, same as every
other dated artifact in this system (`run-manifest-*-N.json`,
`fills-cache-*-N.json`, ...). `run-heartbeat-YYYY-MM-DD-N.json` uses
`len(log.stages)` as `N` -- 0 before any stage has completed, then 1, 2,
3... -- which needs no separate counter and falls naturally out of state
`RunLog` already tracks. Old heartbeat files are never deleted, for the
same reason old daily journal files survive monthly compaction: expected
leftover clutter the fold/lookup logic already ignores by date and
sequence, not an error worth building delete support to avoid.

**Two independent mechanisms, because they catch two different failures:**

- `runlog.DEFAULT_WALL_CLOCK_DEADLINE_SECONDS` (2,700s / 45 minutes),
  checked between stages, is a proactive self-stop for a run that is
  genuinely slow but still executing normally -- past it, the run calls
  `log.abort(...)` and goes straight to Stage 6, sending an incomplete
  brief rather than continuing into the watchdog's own window at all. This
  CANNOT catch a single hung call: the deadline check is code, and code
  does not run while blocked inside a call that never returns.
- `watchdog.HEARTBEAT_STALE_AFTER_SECONDS` (2,100s / 35 minutes) is the
  backstop for exactly that case, read from OUTSIDE the run by a process
  that is not blocked on anything the run is blocked on. The heartbeat
  updates once per completed stage, so a run legitimately deep into a
  single slow stage can go quiet for up to that stage's own budget without
  being mistaken for stuck -- gather's is the largest at 1,800s, and 2,100s
  gives it real headroom (10 minutes) without waiting so long that a truly
  stuck run sits undetected for most of the watchdog's own offset.

The four numbers are deliberately ordered with real margin between each:
`STAGE_TIMING_BUDGETS_MS["gather"]` (30m) < `HEARTBEAT_STALE_AFTER_SECONDS`
(35m) < `DEFAULT_WALL_CLOCK_DEADLINE_SECONDS` (45m) < the watchdog's own
60-minute offset. Each threshold has headroom over the one before it for a
reason: a legitimately slow gather stage must never look hung; a run
correctly self-stopping at its deadline must never race the watchdog's own
recheck; and the watchdog must have real lead time to notice a genuine
stall before its own offset would otherwise fire anyway.

**What this does not prove.** A stale heartbeat is evidence a run has
stopped making progress, not proof the underlying process has actually
terminated or released whatever it was doing when it stalled. The retry
`WATCHDOG_PROCEDURE.md` starts on a `hung` verdict is a fresh attempt, not
a resumption -- if the original process were somehow still consuming
resources (a connector-level hang rather than a process-level one), a
concurrent retry could in principle still collide with it. This is a
smaller, more specific version of the same risk `no_run` already carried
before any of this existed; it is not new, and it is not eliminated by a
heartbeat that can only observe silence, never confirm death. Recorded
here plainly rather than implied to be solved.

## Stage 6 — Warnings grouped by severity; Decisions rendered as cards

Found 8 September 2026, reading the actual watchdog-retry brief the new
wall-clock deadline had just produced for real: a 41-check run failed 16
of them, and `_health_line` rendered all 16 as one flat, colon-prefixed
list -- `fills_cache_present: No fills-cache-*.json exists...` next to
`single_name_cap_individual: VTI 17.75% against a 15% cap` next to
`vol_percentile_available: FAILED on GLDM...`, in whatever order
`manifest["checks"]` happened to carry them. A reader has no way to tell
"this is a portfolio fact you need to act on" from "this is a cache-miss
note that fixed itself this run" without parsing every snake_case check
name by hand -- exactly the same complaint `idea_card`'s own docstring
already records about the old prose-paragraph account sections.

Every `Check` already carries a `severity` (`info`/`warn`/`block`); nothing
had ever used it to order the Warnings list. `_health_line` now splits
failed checks into "Needs attention" (`warn`/`block`, rendered first) and
"System notes" (`info`, rendered after, and omitted entirely with no
empty heading when there are none) -- a group with nothing in it renders
nothing, never a bare label. The raw check name is not hidden (this
system's whole point is that nothing is), it moves from a colon prefix
BEFORE the detail to a small muted tag AFTER it, matching the "-- source"
attribution `idea_card` already uses for every bullet elsewhere in the
same email -- one visual convention for "here is where this claim comes
from," not two.

`_decisions` had the same problem in a different shape: a four-column
table with `white-space:nowrap` forced on symbol/action/placed-or-not,
which has no good answer on a phone screen -- the reason column simply
gets whatever width the other three don't claim. Rebuilt to call the
existing `idea_card` per decision instead of building a table row by
hand; `gate_failed`, previously a raw snake_case token in an `<em>` tag
(`no_blocking_conflict`), now reads as plain words ahead of the reason
(`no blocking conflict: ...`) in one line, the same prose register as
everything else in this email. Nothing about `verify_email`'s contract
changes -- `_decisions` reads `manifest["decisions"]` directly and was
never part of the structured-ideas path that function checks; this is a
pure presentation change over the same fields the table already showed.

## Stage 6 — a cache-file write needs a check that it actually happened

Found 8 September 2026, root-causing why preflight was still slow the
morning after the watchdog retry had, by its own account, everything it
needed to fix that. The retry's own manifest logged `fills_ready_to_cache:
870` -- every one of the 870 derived fills was old enough to cache -- and
wrote `splits-cache-2026-09-08.json` in the same run. There is no
`create_file(fills-cache-2026-09-08.json)` call anywhere in that run's own
`calls` log. The write was computed and never made, and nothing on the
manifest said so; it surfaced only because a human, reading a slow run the
next morning, diffed the prior run's call log by hand.

This is the identical failure shape as the wash-sale note-drift bug
(Stage 0 step 8, above) and `ledger.run_entry`'s schema-pinning fix: an
instruction that produces a specific artifact, with nothing that checks
the artifact actually got produced. Both of those were fixed the same way
-- not by trying to make the underlying step more reliable (an agent
following a long, multi-stage procedure by hand will occasionally drop
one line of it; that is not a bug in the instruction so much as a fact
about the execution model), but by adding a check that catches the
OUTCOME being wrong, cheaply, the same day. `DAILY_PROCEDURE.md` Stage 6
now asks for one `log.check(f"{cache}_cache_written", ...)` per cache
file, `warn` severity, passing trivially when there was nothing to write
(matching `fills_cache_present`'s own "None passes quietly" convention)
and failing only when something was ready to cache and the matching
`create_file` call is missing from this run's own `calls` log -- a fact
the agent running the procedure already has direct visibility into,
without a second Drive round-trip to confirm. A single miss is a `warn`;
a recurring one becomes visible for free through `no_chronic_failures`,
which tallies any check name failing across the last ten runs with no
hardcoded list of which ones to watch.

No code changes underlie this entry -- `ledger.fills_ready_to_cache` and
`fold_fills_cache` computed correctly; the gap was entirely in the
procedure text having no way to notice its own instruction went unfollowed.

## Stage 0 step 1 / step 0 — two standing config warnings closed, 9 September 2026

`config_screener_scan_id_configured` and `config_run_wall_clock_deadline_seconds_present`
had been failing every run since 7 September, both `warn` severity and
both recovering silently via a documented fallback (`get_scans` + title
match for the scan id; `runlog.DEFAULT_WALL_CLOCK_DEADLINE_SECONDS` for
the deadline) -- correct behavior, but a human was meant to set both
deliberately rather than let a fallback stand in indefinitely. `state.json`
(schema 3, updated this date, superseding the 31 August version) now sets
`screener_scan_id` to the real, already-created `PBS Core Universe v1`
scan id and `run_wall_clock_deadline_seconds` to 2700 -- the same value
the fallback was already applying, made explicit rather than changed. Not
touched: whether 2700 is still the RIGHT number. That question was live
only because recent runs were taking 42-51 minutes with no fills cache to
work from; the 9 September fix (above) removes the ~880-order re-pull
that was the actual driver of that duration, so there is not yet evidence
the number itself needs to move, in either direction.

Updating this required editing the live trigger prompts (`RemoteTrigger`),
not just this repository -- `{{STATE_FILE_ID}}` is substituted into two
scheduled routines' own prompt text at trigger-creation time, per
`HANDOFF.private.md` section 8, and neither prompt re-reads this repo for
a fresher value. Both were updated to the new file id in the same pass;
the old `state.json` was renamed to `state.superseded-2026-09-09.json`
in Drive rather than left ambiguous alongside the new one.

## Stage 6 — decisions before diagnostics, and "how much" must be a number

Requested 10 September 2026: the email should lead with only what is
needed to decide -- buy/sell/hold, how much, and why -- with everything
else reachable but not competing for the top of the page. Two changes,
one rendering-only and one a real new rule.

**Rendering**: `render_email`'s body order previously ran banner ->
`_health_line` (check tally and warnings) -> the two account sections ->
`other_sections` (prior-day review, diversification, system health) ->
`_decisions`. The account sections -- the only part of the email that is
actually a decision -- sat third, after a warnings block that on a slow
Stage 0 can run to fifteen or more items. They now render immediately
after the banner; everything else moves behind a `_details_divider()`
that says outright nothing past it is hidden, only reordered, so the
"never hide a check" rule stays true in substance, not just in the raw
manifest anyone can still pull from Drive. `idea_card`'s left border is
now colored to the action (the same palette `_action_badge` already used)
instead of the neutral rule-grey every other `_well` block gets, and the
badge itself carries a directional glyph (▲ buy, ▼ sell/trim, ● hold) --
a column of cards should read as a column of colors and shapes before a
single word is read.

**The real rule**: a `buy`/`sell`/`trim` card's `quantity` must contain an
actual number. Found while reviewing a real card from 8 September that
read `quantity: "partial trim"` -- true, but not an answer to "how much,"
and the real figure ("about 2.97 shares, roughly 1,127 dollars") was
sitting unused in the matching `Decision`'s own `reason` text the whole
time. `emailer.ACTIONS_REQUIRING_QUANTITY` names the three actions that
change a position (`hold`/`skip`/`none` are not transactions and carry no
such requirement); `verify_email` raises before render, same as every
other unverifiable claim it already catches -- the fix is "put the number
that already exists into the card," never "loosen the check."
