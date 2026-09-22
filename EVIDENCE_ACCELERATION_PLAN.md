# Evidence acceleration plan — getting a verdict in weeks, not decades

**Written**: 22 September 2026. **Author**: Claude session (this document is the
handoff — a *different* Claude Code session should be able to pick this up and
build from it with no other context). **Status**: design, not yet implemented.
Nothing in this document authorizes placing an order; the DRY RUN guard in
`DAILY_PROCEDURE.md` is untouched and stays untouched by everything below.

## 1. The number that started this

`evidence.py`'s pre-registered plan needs **891 closed trades** (80% power,
detecting a 0.5%-per-trade excess-return edge, assumed 6% per-trade SD, 21-day
horizon). It was designed assuming 10 trades/week, giving its registered
`decide_by` of **2028-02-28** (~21 months out).

Real observed rate through 22 September 2026: **one** idea has cleared the
five-condition gate in 22 daily runs — about **0.23 trades/week**. At that
rate:

```
required_n = 891
observed_rate = 1 / (22 trading days / 5) = 0.227 trades/week
weeks = 891 / 0.227 + 21/7 ≈ 3923 weeks ≈ 75.5 years
```

(`evidence.time_to_evidence(891, trades_per_week=0.227, horizon_days=21)` —
run it yourself, `evidence.py` is unchanged and this is real output, not a
hand estimate.)

**The user's instruction: get real evidence, significantly faster. A few
weeks is acceptable. Figure out how.** This document is that plan.

## 2. Why forward-only trading cannot be sped up into "weeks"

Two independent hard floors, neither fixable by trading more aggressively:

1. **The 21-day horizon is a floor on time, not on trade count.** A thesis
   opened today cannot be scored until `opened + horizon_days` — see
   `evidence.settle()` and `DAILY_PROCEDURE.md` Stage 0.5 step 1
   (`journal.matured_theses`). Even a gate that cleared *every* eligible
   symbol *every* day would still need 3 weeks for the very first trade to
   mature. This alone rules out "a few weeks" for a forward-only design
   whose evidence needs hundreds of *matured* trades.
2. **The gate is, and has always been, extremely selective by design** — the
   watchdog has now flagged "no action taken on N of N runs" on **22 of 22**
   runs without anyone acting on it (`runlog.py`'s `optimizations` list, every
   recent manifest). Even a generous re-calibration would plausibly get to a
   few clears per week, not tens.

**Conclusion: forward-only cannot deliver n≈891 in weeks under any
realistic gate-strictness change.** The only lever big enough is retrospective:
evaluate the exact same gate against *history that already happened*, where
"time to mature" is not a wait — it is a lookup into data that already
exists.

## 3. The complication that changes the whole design: the gate is not code

This is the load-bearing finding of this investigation, and it was **not**
obvious going in.

`runlog.GATE_CONDITIONS` names five conditions:

```python
GATE_CONDITIONS = (
    "catalyst",              # 1. a named catalyst with a date or window
    "two_sources",           # 2. two independent corroborating sources
    "invalidation_level",    # 3. a stated invalidation level
    "risk_sized",            # 4. size derived from the risk dial
    "no_blocking_conflict",  # 5. no wash sale / adding to a loser /
                             #    concentration / cash floor / whole-share /
                             #    anomaly conflict
)
```

There is **no single function that evaluates this gate**. It is applied by
the Claude Code session running `DAILY_PROCEDURE.md` Stages 1-3 each morning,
reading research output and writing a `runlog.Decision` with `gate_failed`
set by *judgment*. Confirmed by grep: `distinct_sources` (the count behind
`two_sources`) appears **nowhere** in `research.py` as a computed field — it
is typed into the `Decision.inputs` dict by the executing session after
reading the actual article content and judging genuine independence (real
example, BB, 22 Sept: "Two independent sources: Alpha Vantage
NEWS_SENTIMENT and Robinhood get_equity_news, the latter carrying MT
Newswires and Benzinga reporting...").

The good news: **three of the five conditions already are pure, tested,
reusable functions** — nobody has to re-derive these, they already exist and
are exactly what the live system calls:

| Condition | Live implementation | Backtest-ready? |
|---|---|---|
| 1. `catalyst` | date math against `EARNINGS`/`EARNINGS_CALENDAR` | **Yes** — trivial, see §5 |
| 2. `two_sources` | **LLM judgment**, reading article content for genuine independence | **No** — needs §6 |
| 3. `invalidation_level` | `quantcore.stop_plan(entry, vol, atr, ...)` | **Yes** — pure function, already tested (`test_quantcore.py`) |
| 4. `risk_sized` | `quantcore.size_position(account_equity, entry, plan, ...)` | **Yes** — pure function, already tested |
| 5. `no_blocking_conflict` | `washsale.Registry`/`Trade` (wash-sale), `quantcore.detect_anomalies`/`blocking` (price anomaly), plus cash-floor/concentration/whole-share bookkeeping done in prose | **Mostly** — wash-sale and anomaly checks are pure functions; cash-floor/concentration need a simulated paper portfolio, see §7 |

So the actual gap to mechanize is **one condition** (`two_sources`) plus
**portfolio-state bookkeeping** for condition 5's account-specific sub-checks
— not a from-scratch reimplementation of the whole gate. That is what makes
"weeks" realistic.

## 4. Data feasibility — verified live, not assumed

Checked directly against the real Alpha Vantage tools this session, 22
September 2026 (not from memory, not from documentation — actual calls):

- **`NEWS_SENTIMENT` supports point-in-time queries** (`time_from`/`time_to`,
  format `YYYYMMDDTHHMM`). Verified: a query for `AAPL` bounded to
  1–15 Feb 2024 returned 27 real articles with real `source_domain`,
  `relevance_score`, `ticker_sentiment_score`, `time_published` — genuine
  historical archive, not a current-news endpoint faking a date filter. This
  is exactly what a look-ahead-safe backtest needs: news *as it existed* on a
  historical date, not news that later became available.
- **`EARNINGS` returns the complete historical `reportedDate` +
  `reportTime` (pre-market/post-market) record per symbol, one call, no
  date-range limit.** Verified against `BB`: clean quarterly earnings dates
  back to 1999. This reconstructs the exact historical catalyst calendar for
  any symbol in a single call — **not** one call per catalyst instance.
- **`TIME_SERIES_DAILY_ADJUSTED` with `outputsize=full`** gives 20+ years of
  OHLCV per symbol, one call. This is all that condition 3/4 (invalidation,
  sizing) and evidence scoring (`settle()`'s `exit_price` = close price at
  `opened + horizon_days`) need.
- **`INSIDER_TRANSACTIONS`** takes `from_date`, and **`CONGRESS_TRADES`**
  returns full history per symbol/bioguide — both already point-in-time
  capable, useful if the backtest universe is later widened to include the
  congress-discovery source.

No look-ahead-bias blocker exists in the data layer. The risk is entirely in
how the backtest *uses* these calls (see §8's look-ahead checklist).

## 5. Mechanizing condition 1 (`catalyst`) — trivial

For a symbol with `EARNINGS` pulled once:

```python
def historical_catalyst_windows(earnings: dict, *, horizon_days: int = 21) -> list[dict]:
    """One row per quarterly report: the date the catalyst FIRST entered the
    21-day horizon (reportedDate - horizon_days) and the report's own date/time.
    A backtest evaluates the gate once per window, at the moment it enters
    the horizon -- exactly mirroring how the live gate is evaluated once per
    day an idea sits in the researched set, but collapsed to the single
    correct evaluation point instead of one row per day."""
```

This is new code but has no design risk — it is date arithmetic over data
already fetched.

## 6. Mechanizing condition 2 (`two_sources`) — the real work

This cannot be perfectly replicated without an LLM reading article content
for genuine independence (not just two different API calls surfacing the same
wire story twice). Two paths, recommended as a **combination**, not either/or:

### 6a. A codified proxy (fast, cheap, no LLM — build this first)

```python
def two_sources_proxy(av_news: list[dict], rh_news: list[dict], *,
                      catalyst_date: date, window_days: int = 21,
                      min_relevance: float = 0.3) -> dict:
    """Mechanical stand-in for the live judgment call. Counts a SOURCE as
    present when at least one item from that feed:
      - falls within [catalyst_date - window_days, catalyst_date]
      - has ticker_sentiment relevance_score >= min_relevance for the symbol
      - is not purely a syndication of the other feed's identical headline
        (dedupe by normalized title similarity -- see note below)
    distinct_sources = count of {alpha_vantage_news_sentiment, robinhood_news}
    that have >=1 qualifying item. Returns the same shape
    `Decision.inputs` records live (`distinct_sources`, plus which items
    qualified, for auditability) so a human can spot-check any backtest row
    against what a live run would have recorded.

    THIS IS AN APPROXIMATION, not the live judgment. It must be validated
    (see 6b) before its output is trusted as the evidence sample.
    """
```

Design notes for whoever builds this:
- Use `av_news`'s own `relevance_score` field (already computed by Alpha
  Vantage) rather than inventing a new relevance metric.
- The dedupe-by-title-similarity step matters: two feeds both carrying the
  same Reuters wire story is not two independent sources, and the live
  system's own reasoning traces (e.g. the real BB clearance: "RBC modeling
  revenue... and a separate MT Newswires report...") show genuine
  independence checking, not just a count. A cheap first pass: normalize
  titles (lowercase, strip punctuation) and treat >0.6 token-overlap as the
  same story regardless of which feed carried it.
- `min_relevance=0.3` is a starting guess, not a validated threshold — tune
  it against 6b's calibration set.

### 6b. Validation against real LLM judgment (do this before trusting 6a's output)

Before the mechanized backtest's population is treated as evidence, measure
how often the proxy agrees with what an actual Claude Code session applying
`DAILY_PROCEDURE.md` Stage 1-3 judgment would decide, on the **same
point-in-time data**:

1. Assemble a calibration set: the real historical `two_sources`
   clearances/rejections already on record (BB 9/18 reject, BB 9/21 reject,
   BB 9/22 clear — three real, already-graded examples) plus ~30-50
   additional historical catalyst windows picked to cover a range of
   coverage density (mega-cap with abundant news through microcap with
   sparse news).
2. For each, run an actual Claude Code invocation fed *only* the
   point-in-time `NEWS_SENTIMENT`/Robinhood-news data for that window (no
   later information), asking it to apply exactly the `two_sources`
   condition as `DAILY_PROCEDURE.md` states it, and record its verdict.
3. Compare to the proxy's verdict on the identical input. Report agreement
   rate, and specifically the **false-clear rate** (proxy says cleared, LLM
   says rejected) — this is the dangerous direction, since it would inflate
   the backtest's apparent edge.
4. **Decision rule**: if agreement ≥ ~85-90% with a low false-clear rate,
   trust the proxy for the full backtest population, and disclose the
   validated agreement rate in the eventual evidence writeup as a stated
   methodological limitation. If agreement is materially lower, either
   refine the proxy and re-validate, or fall back to running the full
   backtest population through the LLM directly (§6c) — slower per-instance
   but still tractable in weeks at the expected population size (§9).

### 6c. Fallback: LLM-in-the-loop for the full population

If 6a/6b doesn't reach an acceptable agreement rate, the backtest can run
every catalyst-window instance through an actual Claude Code session (batched,
parallel, e.g. via this project's own subagent/cloud-session tooling) instead
of a mechanized proxy. Slower and has a real compute/API cost, but every
individual evaluation is cheap (one point-in-time news read, one judgment) and
this is exactly the kind of task that parallelizes — hundreds of independent,
context-isolated evaluations, no shared state between them. Budget this as
the fallback plan, not the default, given 6a is far cheaper if it validates.

## 7. Simulating condition 5's portfolio-state sub-checks

Wash-sale timing (`washsale.Registry`/`Trade`, `proxies_for`) and price
anomaly blocking (`quantcore.detect_anomalies`, `blocking`) are already pure
functions — reuse them directly, no new code.

Cash floor / single-name cap / sector cap / whole-share are bookkeeping
against a running paper portfolio that does not exist yet for a backtest (the
live agentic account is a specific $989 real account; a backtest needs its
own simulated ledger). Recommend:

- **Do not size the backtest's simulated capital to match the real ~$989
  agentic account.** The real account's own near-total cash exhaustion
  (`agentic_max_weight`/`individual_cash_floor` warnings recur on nearly
  every run) means a chunk of the live system's low throughput is a
  **capital-starvation artifact**, not a gate-strictness artifact — worth
  separately flagging to a human (see §10) since it is a real, fixable, fast
  lever independent of this whole plan. Size the backtest's simulated
  account large enough (e.g. $100k+) that cash floor rarely binds, so the
  backtest measures the *signal's* edge, not this account's funding history.
  **This must be disclosed explicitly wherever the backtest's verdict is
  reported** — it is evidence about a well-capitalized version of the
  strategy, not literally about the $989 account.
- Track the simulated portfolio chronologically (theses open and close over
  simulated time, in real calendar order) so concentration/sector caps and
  wash-sale windows are evaluated against genuinely prior state, not the
  final backtest-wide portfolio — a look-ahead trap, see §8.

## 8. Look-ahead-bias checklist (read this before writing a line of backtest code)

Every one of these is a way a backtest silently cheats and reports a fake
edge. Check the design against each before trusting any output:

1. **News**: only ever query `NEWS_SENTIMENT` with `time_to` = the
   catalyst-window's own start (never the full history including what was
   published after). Verified feasible in §4.
2. **Price at entry**: use the close/open on the actual decision date, never
   a later, better-informed price.
3. **Universe selection**: do not build "the eligible universe on date D"
   from a symbol list assembled using *current* (2026) criteria (e.g.
   today's screener results, today's held positions, today's S&P
   membership). This is survivorship/hindsight bias — a name that was a
   micro-cap disaster in 2024 and got delisted would never appear if the
   universe is built from what exists *today*. Recommended mitigation:
   restrict the backtest universe to symbols this system's *own real recent
   runs* have actually surfaced (held + watchlist + the real screener/
   congress-discovery output logged in journal files over the system's live
   history) plus a fixed, pre-committed list decided *before* looking at any
   backtest result (e.g. current S&P 500 membership as of a fixed date is
   still survivorship-biased but is a known, disclosable simplification —
   state it plainly rather than pretending otherwise).
4. **Earnings surprise / analyst revisions**: `EARNINGS`'s `estimatedEPS`/
   `surprise` fields are only known *after* the report — never use them to
   help decide whether to have opened a position *before* that date's
   report.
5. **Splits/dividends**: `TIME_SERIES_DAILY_ADJUSTED`'s adjusted close
   already handles this, but confirm the same adjustment-ratio-ordering
   fix documented in `PROCEDURE_RATIONALE.md` (Stage 19, sort-ascending
   before applying adjustment ratios) is honored — that was a real, found
   bug in this exact kind of computation.
6. **Wash-sale registry**: must be seeded only from the backtest's own prior
   simulated trades in chronological order, never from the real live
   registry (which reflects real 2026 trades irrelevant to, say, a 2023
   backtest date).

## 9. Phased plan — fits inside "a few weeks"

Rough estimate, assuming one engineer/session-equivalent working
continuously; parallelizable where noted.

**Phase 0 (days 1-2): mechanize condition 1, wire up existing pure
functions.** New file `backtest.py`: `historical_catalyst_windows()` (§5),
thin wrappers calling `quantcore.stop_plan`/`size_position`/
`detect_anomalies`/`blocking` and `washsale.Registry` against backtest-supplied
data instead of live data. Unit tests against known real values (e.g. BB's
real 22 Sept `stop_price=8.29`/`shares=17` should be reproducible by calling
the exact same functions with the exact same inputs — a parity test, direct
analogue of what the sibling `investor-mimic-bot` repo calls its "parity
backtester").

**Phase 1 (days 2-5, parallelizable with Phase 0): point-in-time data
build.** For the chosen universe (see §8.3's caution), one `EARNINGS` call
and one `TIME_SERIES_DAILY_ADJUSTED(outputsize=full)` call per symbol —
cheap, a few hundred calls total regardless of window length. Build
`historical_catalyst_windows()` output for every symbol. This produces the
**full candidate population size** before any gate is applied — know this
number before committing further engineering (if it's too small even before
filtering, that's worth knowing on day 5, not day 20).

**Phase 2 (days 4-8, needs Phase 0+1): build and run the `two_sources`
proxy (§6a).** This is the API-call-heavy phase — one `NEWS_SENTIMENT`
(and one Robinhood `get_equity_news`, if it supports historical — **verify
this**, it was not checked this session and the live system's own comment
says Robinhood's is not obviously date-bounded) call per catalyst-window
instance. Budget and rate-limit this explicitly — Alpha Vantage's per-minute
cap for this project's plan was **not found documented anywhere in this
repo** and should be confirmed empirically (send a burst, watch for
429/rate-limit responses) before assuming a throughput number.

**Phase 3 (days 6-10, overlaps Phase 2): calibration (§6b).** Run the
~30-50-instance calibration set through both the proxy and a real LLM
judgment pass. This is the gating decision for whether Phase 4 uses the
cheap proxy or falls back to §6c.

**Phase 4 (days 8-12): compute outcomes.** For every instance clearing the
full simulated gate, `evidence.settle()` needs `entry`/`benchmark_entry`
(price on the decision date) and `exit`/`benchmark_exit` (price on
`opened + horizon_days`, both symbol and SPY) — all already in the Phase 1
data. This step is cheap and fast once entries exist; no new API calls.

**Phase 5 (days 10-14): run `evidence.assess()` on the resulting
population, write the verdict up, reconcile against the live 22-run record
as a sanity check** (the real BB clearances/rejections should appear in the
backtest's own catalyst-window list for BB, and the proxy's verdict on them
should match what's already known — a free extra parity check).

**Total: roughly 2-3 weeks**, with Phases 0/1 and parts of 2/3
parallelizable, comfortably inside the user's "a few weeks" ceiling — and
substantially faster if the universe is kept modest (a few hundred symbols,
not thousands) for the first pass, which is the right choice anyway: better
to get a real, honestly-caveated verdict on a moderate universe in 2 weeks
than an uncalibrated one on a huge universe in 4.

## 10. A separate, faster, complementary lever — don't skip this

Two changes that cost nothing to build (they're policy, not engineering) and
start contributing real forward evidence *immediately*, in parallel with the
backtest:

1. **Record a thesis for every idea that clears conditions 1-4, regardless
   of condition 5's account-funding sub-checks.** `DAILY_PROCEDURE.md`
   already establishes the precedent that individual-account suggestions
   (which can never execute) still count toward the evidence sample "exactly
   as agentic-account trades do." Extend that same logic: an idea that
   clears catalyst + two-sources + invalidation + sizing but is only
   rejected because *this account's cash floor* blocked it is a **funding**
   rejection, not a **signal-quality** rejection, and the pre-registered
   hypothesis is about signal quality. Recording these as a clearly
   *separate, labeled* evidence stream (never silently merged into the
   primary agentic-account sample, since that would change what's being
   measured) roughly multiplies the live sample rate — BB alone would have
   contributed on 21 September too, not just 22 September, under this rule.
2. **Recapitalize the agentic account, or raise its position cap, as an
   independent decision.** This is not evidence-methodology — it is a real
   business decision (more capital at risk) — but §7 already found that a
   meaningful share of the live system's low clearance rate is capital
   starvation, not gate strictness. Flagging this explicitly rather than
   letting it hide inside "the gate is too strict."

Neither of these requires waiting on the backtest; both should start the
next trading day if a human approves them.

## 11. Open decisions that need a human, not a Claude session

- **Backtest universe**: how large, and exactly which symbols (§8.3).
  Recommend starting narrow (a few hundred) and widening only if the
  narrow pass's population turns out too small.
- **Simulated backtest capital** (§7): confirm $100k+ (or pick a number) and
  confirm the explicit-disclosure requirement is acceptable.
- **§10's two levers**: both are policy decisions (what counts as evidence;
  how much real capital is at risk), not engineering ones. Needs explicit
  sign-off before the next session implements either.
- **§6b's calibration threshold** (85-90% agreement, low false-clear rate):
  reasonable default, but a human should confirm before a session spends
  Phase 2-3's API budget building toward it.
- **Alpha Vantage rate limit / plan tier**: not documented anywhere in this
  repo (checked `HANDOFF.md`, `HANDOFF.private.md`, `research.py` — none
  mention it). A session picking this plan up should check
  `HANDOFF.private.md`'s config table or ask before assuming a throughput
  number for Phase 2's budget.

## 12. What NOT to do

- Do not lower `PreRegistration.target_edge_pct` or `min_sample` after
  seeing early backtest results to make a verdict arrive faster — that is
  exactly the p-hacking `evidence.py`'s own docstring exists to prevent
  ("Pre-register the claim and the stopping rule... before the data
  arrives"). If the backtest population turns out small, that is itself the
  answer (the design cannot be validated at this universe size), not a
  reason to move the goalposts.
- Do not merge the §10 "cleared 1-4, blocked by funding" stream into the
  primary registered sample without a new, explicit pre-registration
  covering exactly that population. It is legitimate evidence about
  something slightly different from the original hypothesis; label it as
  such.
- Do not skip §6b's calibration to save time. An uncalibrated mechanical
  proxy for `two_sources` is the single most likely way this entire plan
  quietly manufactures a fake edge — this is the one step worth the extra
  days it costs.
