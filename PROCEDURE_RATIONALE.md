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

## Stage 2 — why the concentration recalibration

`correlation_concentration` reports both a shrunk and an unshrunk view and
flags `concentrated` when effective bets fall under half the number of
names examined, or the eigen-share exceeds 0.45 — the old 0.60
eigen-share-only cutoff never fired for a realistically correlated equity
book (see `HANDOFF.md` section 11, 31 August 2026: a known-answer portfolio
at a true 0.55 correlation was called unconcentrated).

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

## Stage 6 — why the schema and the three-section cap

**Why "always create a new file, never modify one."** The Drive connector
rewrites metadata but not contents.

**Why the 06:20 routine stays silent on an abort.** The watchdog (fires 60
minutes later, reads `WATCHDOG_PROCEDURE.md`) owns deciding what happens
next — diagnosing, attempting a fix, and re-running this same procedure.
Sending a partial "sorry, broken" email from the direct routine would just
be a second email nobody asked for once the watchdog's retry lands.

**Why exactly three sections, as cards.** The user does not want a
market-commentary newsletter, only what the agentic account did, what to
consider for the individual account, and whether the system is healthy — cut
down from "Evidence review" / "Where things stand" / "What moved and why" /
"Risk measurement" on 1 September 2026. The two account sections are cards,
one per symbol, never a paragraph, since 4 September 2026: a name, an
action, and a quantity buried in a sentence are slower to scan than the
same three things in a card's first line. Every bullet is tagged with the
specific source that supports it — a named data provider (`"Alpha Vantage"`),
a specific report (`"EIA STEO, 9 Sep"`), a computed check
(`"quantcore.stop_plan"`), a tool result (`"review_equity_order"`) — not a
vague "research suggests".

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
