# Pre-Market Brief System — Operations Guide

Everything needed to understand, run, and extend this system. Written to be handed
to an agent or a person with no memory of the design conversation.

> **This repository is public and holds code only.** Account numbers, balances,
> holdings, configuration values, the trade journal, the wash-sale registry, run
> manifests, and storage identifiers live in `state.json` in a private Google
> Drive folder and are never committed here. Wherever this document needs one of
> those, it names the `state.json` key instead of the value.
>
> The private companion document, `HANDOFF.private.md`, carries the concrete
> values. It is gitignored and lives alongside `state.json` in Drive.

**Status:** measurement, health, and wash-sale layers are built and tested
(159 tests passing). The market calendar is verified against the exchange's
published table. Nothing is scheduled. No research pass has ever run. The agentic
account has not been traded except for one deliberate throwaway test order.

---

## 1. What this system does

Every weekday morning before the United States market opens, a cloud-scheduled run:

1. Audits itself (test suite, tool visibility, ledger reconciliation, clock, calendar).
2. Gathers market data, news, and both brokerage account states.
3. Measures volatility, indicators, stops, sizes, and portfolio concentration.
4. Passes candidate ideas through a five-condition confidence gate.
5. Writes **suggestions** for the individual account (it cannot trade there) and
   **executes trades** in the agentic account.
6. Writes a run manifest and updated ledger to Google Drive.
7. Emails a brief to the owner — **always**, even on total failure.

The owner is not a professional investor. This is research output, not investment
advice, and the individual-account decisions remain the owner's.

---

## 2. The two accounts

The system spans two brokerage accounts with different permissions. Their numbers
and balances live in `state.json` under `accounts`.

| | Individual | Agentic |
|---|---|---|
| `state.json` key | `accounts.individual` | `accounts.agentic` |
| Broker type | margin | **cash** |
| Agent may trade | **No — read only, enforced by the broker** | Yes |
| Role | long-term holdings; the system only suggests | the system executes here |
| Positions | many, all fractional | few, all fractional at baseline |

Two facts drive most of the design:

- **The agentic account is small.** Combined with an 18% position cap and the
  whole-share rule below, the tradable universe is bounded to names priced under
  roughly `agentic_equity x max_weight_agentic` per share.
- **Positions in both accounts are fractional at baseline**, and fractional
  positions cannot carry stop orders. Anything the system opens must be whole
  shares or it cannot be protected.

Baseline rule breaches and the loss-carrying positions that seed the wash-sale
watchlist are recorded in `state.json`, not here — they change with the market.
A breach is never automatically a trade; it must clear the gate in section 7
like any other idea.

---

## 3. Broker constraints — all verified against the live API, not assumed

| Constraint | Evidence |
|---|---|
| **Fractional positions cannot carry stop orders** | Live order, 0.5 sh stop → `Invalid trigger for fractional order`. Same order at 1 whole share → accepted, `queued`. |
| **The order simulator is not proof** | `review_equity_order` accepted the fractional stop with zero alerts. Only the live placement rejected it. Never trust review alone. |
| **Stops do not execute outside regular hours** | Broker documentation. Overnight gaps are unprotected regardless of what rests. |
| **No bracket / one-cancels-other orders** | Broker documentation. A stop and a target cannot both rest. |
| **The connector now exposes a cancel-order tool** (`cancel_equity_order`) | Confirmed working 31 August 2026 — used to cancel a stale good-for-day stop that was blocking a correctly-sized sell. This was NOT available when the system was first designed; the good-for-day-and-let-it-expire pattern below predates it and remains the default, but a stuck order no longer has to be waited out. |
| **Cash account, T+1 settlement** | Sale proceeds unusable until the next trading day. Fine at a once-daily cadence. |
| **Pattern day trader rule no longer exists** | Eliminated by the regulator effective 4 June 2026. Not a constraint. |
| **GitHub REST API blocked; `git clone` works** | Tested. The daily code pull is free; pushing requires the repo be attached to the session. |
| **Container has no direct internet except the package index** | Tested. All data arrives via connectors or WebFetch. |
| **Some market-data endpoints return enormous payloads** | One treasury endpoint returned 72,407 characters and blew the tool output limit. Constrain every pull: `datatype=csv`, `outputsize=compact`. |

### The consequences these force

- **Whole shares only in the agentic account.** Any name priced above the position
  cap is untradable, however good the idea.
- **Stops are good-for-day, re-derived every morning.** This lets an order
  expire by itself and makes stops adapt to changing volatility, and it now has
  a second line of defence: if a stale one is ever found still resting and
  blocking a trade, `cancel_equity_order` removes it directly rather than
  waiting it out.
- **Pre-existing fractional positions cannot be stopped.** GLDM, the one
  carrying real directional risk while unstoppable, was closed on 27 August
  2026 at a small loss, opening the wash-sale block that runs through late
  September (see section 10c). SGOV and VGSH remain as the yield-bearing
  reserve.
- **Overnight gap risk cannot be eliminated, only sized for.** Since stops
  cannot execute outside regular hours, `size_position` applies
  `config.gap_risk_haircut` (25% by default) to the risk budget on every
  position — the real risk per position is larger than the stated
  `risk_budget_fraction` alone would suggest, and sizing now assumes that
  rather than pretending the stop covers what it cannot.

---

## 4. Configuration

The live values are in `state.json` under `config`. The keys and their meaning:

| Key | Meaning |
|---|---|
| `individual_level`, `agentic_level` | Aggressiveness dial, 1–10, per account. See the scale below. |
| `max_weight_individual`, `max_weight_agentic` | Single-name cap as a fraction of account equity. |
| `cash_floor_individual` | Minimum cash fraction. |
| `sector_cap_individual` | Maximum fraction in one sector. |
| `target_holdings_individual`, `target_holdings_agentic` | `[min, max]` position counts. |
| `max_new_positions_per_day` | Hard cap on new opens per run. |
| `risk_budget_fraction` | Fraction of equity risked per position, between entry and stop. |
| `k_daily_sigma` | Stop distance in daily standard deviations before floor and cap. |
| `stop_floor`, `stop_cap` | Bounds on stop distance as a fraction of entry. |
| `stop_time_in_force` | `gfd`. Never `gtc` — there is no cancel tool. |
| `whole_shares_required` | `true`. A fractional position cannot hold a stop. |
| `circuit_breaker_usd` | Agentic equity below this: stop opening positions, drop to level 4, require review before resuming. |
| `hard_stop_usd` | Agentic equity below this: liquidate to cash, halt entirely, email. Not a suggestion. |
| `wash_sale_block_enabled`, `wash_sale_window_days` | Cross-account wash-sale enforcement, 30-day window each side. |
| `asset_scope` | Stocks and exchange-traded funds only. |
| `tax_optimization`, `scheduled_contributions` | Off. |
| `suggestions_per_day_cap` | Unset — the gate is the limiter, not a quota. |
| `email_to` | Brief recipient. |
| `gap_risk_haircut` | Shrinks the effective risk budget to account for stops that cannot execute outside regular hours (default 0.25). See section 10c. |
| `concentration_bets_floor_ratio`, `concentration_eigen_share_cap` | The recalibrated concentration thresholds (0.5 and 0.45) — see section 10c. Read by convention; the actual gate values live in `quantcore.correlation_concentration`, not read from config at runtime yet. |

A separate top-level `evidence.pre_registration` object (not under `config`)
holds the pre-registered claim this system is being tested against — see
section 10c. It is the one piece of state written once and deliberately never
touched by an automated run; changing a hypothesis after seeing the data is
exactly what pre-registration exists to prevent.

**Aggressiveness scale**, for reference when a level is changed:

| Level | Max position | Cash reserve | Holdings | Character |
|---|---|---|---|---|
| 1–2 | 5% | 40%+ | 15–25 | Index funds, dividend payers, minimal turnover |
| 3–4 | 8% | 25% | 12–18 | Quality large companies, long horizon |
| 5–6 | 12% | 15% | 8–14 | Balanced; rotation and earnings positions allowed |
| 7–8 | 18% | 5–10% | 6–10 | Concentrated conviction, momentum, higher volatility |
| 9–10 | 25%+ | 0–5% | 4–8 | Aggressive; 30%+ drawdowns expected and normal |

Note honestly: while most of the agentic account sits in treasury funds and gold,
the circuit breaker is far away. It becomes meaningful as capital is deployed.

---

## 5. Storage

| What | Where | Why |
|---|---|---|
| Code | this public repository | `git clone --depth 1` each morning: no credentials, no context cost |
| Configuration and the pre-registered evidence claim | `state.json`, private Google Drive folder | Account numbers, thresholds, the one hypothesis being tested |
| Everything that changes run to run | rebuilt from the broker + dated `journal-*.json` files, same folder | See below — this is a deliberate redesign, not the original plan |

The folder and file identifiers are in `HANDOFF.private.md`.

**`state.json` went through a redesign on 31 August 2026, after the first two
live runs.** It originally also held the trade journal, the wash-sale registry,
and run history as arrays inside it, and neither run ever wrote to them because
the Drive connector cannot rewrite a file's contents — only its metadata. The
journal stayed permanently empty while the broker held two dozen real positions,
and preflight's reconciliation check could only ever pass by exemption or abort
by default. Two properties of the connector, and the fix each one forced:

- **It can rewrite metadata but not contents.** So nothing that changes every
  run lives inside one file that gets edited in place. `state.json` now holds
  only configuration and the pre-registered evidence claim, which change rarely
  and deliberately; when they do change, the fix is the same as before —
  create the new version, rename the old to `state.superseded-YYYY-MM-DD.json`.
  Everything that changes every run goes in a **new dated file**:
  `run-manifest-YYYY-MM-DD[-N].json` (the raw manifest, for a human) and
  `journal-YYYY-MM-DD[-N].json` (structured entries, for the code to fold back
  in). `ledger.fold_journal` reads every `journal-*.json` file in the folder and
  merges them into one view, oldest first.
- **Positions and the wash-sale registry are never stored at all — they are
  rebuilt from the broker's own order history every run**, via `ledger.py`. A
  local copy of a broker's positions can only ever be a stale duplicate that
  drifts; rebuilding from the fills that produced them makes drift structurally
  impossible rather than merely detected, and turns "does the ledger agree with
  the broker" into "do the broker's own positions follow from the broker's own
  fills" — a check that can actually fail for a reason worth aborting on
  (a transfer, a corporate action, a bug) rather than failing by construction.
- **Drive is not a code fallback.** It briefly held copies of the Python
  modules; they went stale the first time the code changed, and a stale copy of
  the market calendar is exactly the failure this system is built to avoid. The
  repository is the only source of truth for code, and a failed clone aborts the
  run rather than reaching for a second copy.

**Never commit `state.json`, `HANDOFF.private.md`, or anything from `data/`.**
`.gitignore` covers all three; that is a backstop, not a substitute for care.

---

## 6. Code

| File | What |
|---|---|
| `quantcore.py` | Five volatility estimators, ATR, GARCH, RSI, trend, volatility percentile, Ledoit-Wolf concentration, direction-aware stop derivation, cash- and quality-aware sizing, eight anomaly classes including split detection |
| `runlog.py` | Run manifests, staged timing, preflight self-audit, verified market calendar, regression review, optimization proposals, honest scoring |
| `washsale.py` | Cross-account registry, both directions, proxy warnings |
| `ledger.py` | Positions and the wash-sale trade list rebuilt from broker order history; the append-only journal fold |
| `evidence.py` | Pre-registered hypothesis testing, sample-size planning, futility stopping, and the policy that pauses new positions when the claimed edge is ruled out |
| `emailer.py` | HTML brief rendering, failure diagnosis, subject lines |
| `pipeline_demo.py` | End-to-end demonstration run |
| `make_fixtures.py` | Regenerates the deterministic synthetic fixtures the demo falls back to |
| `test_quantcore.py` | 76 tests, including known-answer volatility and correlation recovery |
| `test_runlog.py` | 45 tests, including daylight-saving drift and market-calendar integrity |
| `test_washsale.py` | 32 tests |
| `test_ledger.py` | 23 tests, several against a real broker order-history fixture |
| `test_evidence.py` | 24 tests, including known-answer edge detection and futility |
| `test_emailer.py` | 26 tests, including escaping and no-research-on-abort |

Key API surface:

```python
import quantcore as q, runlog as R, washsale as W, ledger as L, evidence as E

vol, parts = q.consensus_volatility(df, window=60)   # Estimate + per-method dict
atr  = q.average_true_range(df)
plan = q.stop_plan(entry, vol, atr, direction="long")           # StopPlan; raises on
                                                                  # a quality outside
                                                                  # require_quality
size = q.size_position(equity, entry, plan,
                       risk_budget_fraction=..., max_weight=...,
                       buying_power=portfolio["buying_power"],   # NOT equity —
                                                                  # cash accounts settle
                                                                  # T+1
                       require_whole_shares=True)    # SizePlan; quality and the
                                                       # gap-risk haircut both shrink it
anoms = q.detect_anomalies(symbol, df, asof=today)   # list[Anomaly]
if q.blocking(anoms): ...                            # exclude this symbol

log = R.RunLog(run_id, mode="live")
R.preflight(log, available_tools=..., required_tools=..., local_time=...,
            expected_local_hhmm=(6, 20), self_test=..., history=journal.runs)
if not log.may_trade: ...                            # abort path

fills = L.fills_from_orders(broker_orders)                  # cancelled orders excluded
positions = L.positions_from_fills(fills)                   # rebuilt, never stored
drift = L.reconcile_positions(broker_positions, fills)       # do positions follow fills?
trades = L.to_washsale_trades(fills, account="agentic")
reg = W.Registry(trades_individual + trades_agentic)         # rebuilt every run
v = reg.check_buy("XYZ", today)

journal = L.fold_journal(drive_files)                        # every journal-*.json
matured = journal.matured_theses(today)                       # ready to be scored
outcome = E.settle(thesis, exit_price=..., benchmark_exit=..., closed=today)
prereg = E.PreRegistration(**state["evidence"]["pre_registration"])
verdict = E.assess(outcomes, prereg, asof=today)              # "collect"/"continue"/"stop"
policy = E.trading_policy(verdict)                            # {"pause_new_positions": bool, ...}
```

**Every estimate carries `.value`, `.n_obs`, and `.quality`** — `ok`, `thin`,
`degraded`, or `failed`. A failed estimate propagates as a refusal, never a
default, and `stop_plan` now enforces this itself: it raises if the quality it
is handed falls outside an explicit allow-list, rather than relying on every
caller to check.

### Running it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # 235 tests
python pipeline_demo.py      # end-to-end run
```

The demo prefers real saved pulls in `data/` and falls back to the committed
synthetic fixtures in `fixtures/`, so a fresh clone runs with no API key. It
prints which source it used and says plainly when the numbers are synthetic.

### The market calendar

`runlog.py` holds a hand-verified table of exchange closures and early closes,
cross-checked on 28 August 2026 against the exchange's published calendar rather
than derived from the observance rules. Deriving it is not safe: the rule that a
Saturday holiday moves to the preceding Friday does **not** apply to New Year's
Day when the substitute would be the last trading day of the year, which makes
31 December 2027 a regular session. An earlier version of the table had it wrong.

`HOLIDAY_TABLE_HORIZON` is the expiry date. Past it, preflight raises
`holiday_table_current` rather than treating an unlisted date as a normal
session. **Extend the table from the published calendar before that date.**

---

## 7. The five-condition confidence gate

An idea is not a trade until it clears all five. Failing one demotes it to the
watchlist with the failing condition named.

1. **A named catalyst with a date or window.** Not "well-run company."
2. **Two independent corroborating sources.** Two outlets reprinting one wire is
   one source.
3. **A stated invalidation level** — the price or event that proves it wrong.
4. **Size derived from the risk dial**, not conviction.
5. **No blocking conflict** — wash sale, adding to a loser, concentration, cash
   floor, whole-share rule, or a data anomaly on that symbol.

Empty days are expected and correct. A system producing a confident trade every
morning is manufacturing them.

---

## 8. The scheduled run

Created with the Claude Code Remote `create_trigger` tool — **not** local cron,
which dies with the session. Cron is evaluated in UTC, so the expression changes
with daylight saving. Target fire time is 06:20 Central.

| Period | Cron |
|---|---|
| Daylight time | `20 11 * * 1-5` |
| From 1 Nov 2026 (standard time) | `20 12 * * 1-5` |
| From 14 Mar 2027 | `20 11 * * 1-5` |

`requires_local_device: false` — everything runs in the cloud.

The trigger prompt itself is stored in the trigger, not here, because it names
the accounts. The template with placeholders is `TRIGGER_PROMPT.md`; the filled
version is in `HANDOFF.private.md`. The prompt's shape:

- **Stage 0 — Preflight.** Clone, install, run the test suite (any failure aborts
  the run entirely), confirm every required tool is visible (this guards a known
  cold-start defect), read `state.json`, check the clock for schedule drift,
  check the market calendar, reconcile ledger against broker, review the last ten
  manifests for regressions.
- **Stage 1 — Gather.** Both accounts, prices, news, filings. Small payloads.
  Anomaly detection on every series; blocking anomalies exclude the symbol.
- **Stage 2 — Measure.** Volatility consensus, ATR, RSI, percentile, trend,
  concentration. Quality flags carried through.
- **Stage 3 — Gate.** The five conditions, plus the wash-sale registry in both
  directions across both accounts.
- **Stage 4 — Individual account.** Suggestions only; the agent cannot trade here
  and must not try.
- **Stage 5 — Agentic account.** Execute, under every rule in sections 3 and 4.
  Review then place; re-read the account after every order and report what the
  broker says, not what was intended.
- **Stage 6 — Record and send.** Manifest and journal to Drive, then the email.

**The email always sends.** If the run aborted, it says so and explains why.
Silence must never be the outcome.

---

## 9. The email

The email is the only artefact a human reads, so it is rendered by tested code
(`emailer.py`), not composed freehand each morning. That keeps the format from
drifting between runs and stops a failed run from sending a worse email than a
successful one.

```python
import emailer
subject, html = emailer.render_email(log.manifest(),
                                     sections=[("What moved and why", html)],
                                     prefix="[DRY RUN]")
```

**It is always HTML**, inline-styled, table-laid-out, with no external assets —
clients strip `<style>` blocks and block remote images by default.

Rules the renderer enforces, each because the opposite is a real failure mode:

- **The verdict comes first, in words.** Not a wall of checks to scan for a
  "false".
- **A failed run is short**: what failed, the likely cause, what to do, and one
  line on what still worked so a connector problem does not read like a code
  problem. The full manifest goes to storage for whoever wants it.
- **`diagnose()` names a cause, not a symptom.** "9 required tools missing" is a
  symptom; "the connectors are not attached to the routine" is actionable. It
  reports the first blocking failure only — later ones are usually its
  consequences, and listing them invites fixing the wrong thing.
- **Passing checks collapse to a count.** Twenty green rows train the reader to
  skim, and skimming is how a red one gets missed. Warnings are itemised.
- **An aborted run prints no research headings.** Empty sections imply research
  happened.
- **Everything the model writes is escaped.** Narrative text is untrusted input
  as far as the renderer is concerned.
- **The subject carries the headline fact**, so the run can be triaged from a
  lock screen: `Pre-Market Brief 2026-08-29 — ABORTED (connectors not attached)`
  or `Pre-Market Brief 2026-09-02 — 2 ideas, 2 orders`.

A completed run keeps the full brief: health line, then the research narrative sections in order — **Evidence review first**, ahead of the day's market research, because the standing question of whether this system has an edge should never be buried under whatever happened to be interesting that morning — then the decisions table with the failing gate named on every rejected idea.

## 10. Remaining work

1. ~~Push the repository.~~ Done.
2. ~~Verify the market holiday table against the published exchange calendar.~~
   Done — one wrong date found and fixed, regression tests added, expiry guard
   added.
3. ~~Make `pipeline_demo.py` runnable on a fresh clone.~~ Done via `fixtures/`.
4. ~~Create the scheduled task.~~ Done — it runs weekdays at 06:20 Central in
   **dry-run mode**, where order placement is forbidden outright rather than
   merely declined by a later stage.
5. ~~Attach the connectors to the scheduled routine.~~ Done — Robinhood, Alpha
   Vantage, and Google Drive were attached 31 August 2026 (see section 10c);
   the routine now sees all required tools.
6. ~~Redesign state persistence so it is not permanently empty.~~ Done — see
   section 5 and section 10c. Positions and the wash-sale registry are rebuilt
   from broker history every run; a dated, append-only journal replaces the
   arrays inside `state.json` that the connector could never actually update.
7. ~~Give the "does this have an edge" question an answerable, reviewed
   process.~~ Done — see section 10c and `evidence.py`. A hypothesis is
   pre-registered with a target edge, a sample-size plan, and a decision
   deadline; every run reviews the accumulated evidence, and a verdict of
   futile or no-edge-by-deadline pauses new positions automatically.
8. **Prove the redesigned pipeline on two or three consecutive clean weekday
   dry runs** — the memory rebuild, the journal fold, and the evidence review
   are all new as of 31 August and have not yet run against a live weekday.
9. **Only then, switch to live.** Replace the dry-run prompt with the standard
   one from `TRIGGER_PROMPT.md`. This is a deliberate, reversible decision.
10. **First live run:** act on the concentration and cash-floor breaches in the
    individual account if and only if they clear the gate. (The one unstoppable
    directional position, GLDM, was already closed — see section 10c.)
11. **Extend the holiday table** before `HOLIDAY_TABLE_HORIZON` (2027-12-31).
12. **Revisit `gap_risk_haircut` and the concentration thresholds** once real
    trading history exists to check them against, rather than the judgment
    calls they currently are (section 10c).

## 10b. What the first live dry run found (31 August, morning fire)

The first scheduled fire aborted with 9 of 10 required tools missing. Root
cause: a scheduled routine sees only the connectors listed in its own
configuration, not the ones connected to the account. Resolved same day by
attaching Robinhood, Alpha Vantage, and Google Drive to the routine directly.

The second fire that day reached real market data for the first time and
surfaced four defects, all fixed the same day:

- **Unadjusted prices made volatility meaningless.** `TIME_SERIES_DAILY`
  returns raw prices, so CRWD's 4-for-1 split read as 293% volatility, and
  `detect_anomalies` tested only the final bar, so a split weeks back left
  today's bar ordinary while corrupting every estimate over the window. Fixed:
  the scan covers the whole series with a median-absolute-deviation scale, and
  a split-shaped ratio now **blocks** the symbol. Stage 1 pulls the adjusted
  endpoint.
- **Concentration was understated.** Shrinking covariance toward one average
  variance is wrong for every asset at once when a 1%-vol Treasury fund sits
  beside a 105%-vol semiconductor; on a known-answer book at a true 0.55
  correlation it reported 0.28 and called the book unconcentrated. Fixed by
  standardising before shrinking and taking the less flattering of the shrunk
  and sample views.
- **The wash-sale registry was empty** while the broker showed two real loss
  sales. Fixed by rebuilding it from broker history every run (section 10c).
- **`vol_percentile` and `trend_state` were reported `thin`/degraded for all 23
  symbols** instead of unavailable, when compact payloads (~100 bars) fall far
  short of their 252- and 200-day requirements. Fixed: `vol_percentile` now
  fails outright below 50% coverage.

A stale good-for-day stop was also found holding 1 share of SGOV hostage,
rejecting a correct 4-share sell whole rather than partially. The owner
cancelled it by hand and enabled the full Robinhood tool set, which turned out
to include `cancel_equity_order` — not available when this system was
designed. See section 10c.

## 10c. The second pass — memory, evidence, sizing, and the rest of the code audit

Prompted directly: "what is EVERYTHING that needs to be fixed", followed by
explicit instructions to fix the memory problem with real tests, fix cash-aware
sizing, fix every code defect, and — the one that mattered most — build a real
way to answer whether this system has an edge, because an earlier, unrelated
project "did not provide clear evidence" and the time and money spent on it were
wasted for exactly that reason. Everything below was built and tested the same
session, 31 August 2026.

### Memory — `ledger.py` (new)

`state.json`'s journal, wash-sale registry, and run history had been empty
since the system was designed, because the Drive connector cannot rewrite a
file's contents. Preflight's reconciliation compared an empty journal against
two dozen real positions every run — a check that could only pass by exemption
or abort by default.

`ledger.py` rebuilds positions and the wash-sale trade list from broker order
history every run instead of storing them, which makes drift structurally
impossible rather than merely detected. `fills_from_orders` takes only
`filled`/`partially_filled` orders and only their `cumulative_quantity` — a
cancelled order (like the stale SGOV stop) contributes nothing.
`positions_from_fills`, `cost_basis` (FIFO, tracking both the average cost and
the highest lot still held — a sale can realise a loss on an expensive lot
while the average shows a gain), `loss_sales`, and `reconcile_positions` build
on that. `to_washsale_trades` bridges rebuilt fills into `washsale.Trade`
objects with realised P&L computed the same way, closing the gap that left the
registry empty: a `Trade` on a sell requires `realized_pnl`, and nothing had
ever computed it from broker history before.

Run-to-run history moves to dated, append-only `journal-YYYY-MM-DD[-N].json`
files, folded by `fold_journal` — oldest first, tolerant of one corrupt file,
never overwriting a same-day second run. 23 tests, several run directly against
the agentic account's real order history from 31 August 2026, including the
cancelled SGOV stop as a fixture.

`reconcile_positions`'s tolerance is 1e-4, matching a real defect: the previous
1e-6 tolerance was tighter than the rounding noise in a six-decimal broker
payload round-tripped through JSON, so it could have false-positived on
positions that actually agreed.

### Evidence — `evidence.py` (new)

The direct instruction was to build a way to answer whether this system has an
edge, and to keep reviewing it — not to answer it once and move on.

**The number that matters most, computed before anything else:**
`evidence.required_sample` says how many closed trades are needed to detect a
claimed edge against noise, and `time_to_evidence` converts that into a
timeline. At the pre-registered claim (0.50% excess return per trade, 6%
assumed dispersion — realistic for single-name 21-day returns, not the 4%
textbook figure for a diversified index), that is roughly 900 closed trades.
At the trade rate actually observed on 31 August — 1 idea, 0 orders — that is
measured in years. **This is the finding.** Knowing the wait before committing
to it is the entire point; the previous project this was compared against
never produced this number at all.

`PreRegistration` fixes the hypothesis, the claimed edge, the significance
level, and a decision deadline *before* data arrives — refusing a hypothesis
with no stated number, because "beats the market" is a mood, not a claim.
`Outcome.excess_pct` is return minus the SPY benchmark minus a cost assumption:
beating cash while losing to the index is negative edge, because the index was
free. `assess` grades the accumulated record against the pre-registered claim
and returns one of five verdicts — `insufficient` (too few trades to say
anything), `edge detected, provisional`, `inconclusive`, `no edge by the
registered deadline`, or **`futile`** (checked *before* the sample-size gate,
because ruling an edge out takes less evidence than establishing one: once the
bootstrap confidence interval's upper bound falls below the claimed edge, that
edge is ruled out and more of the same data will not bring it back — the
ability to stop early is the point). More "looks" at a growing record tighten
the significance threshold, because checking weekly and stopping the first time
it looks good is how noise becomes a false discovery.

`trading_policy(verdict)` is the piece that makes this an evidence *system*
rather than an evidence *report*: a `stop` decision — futility or a missed
deadline — pauses new positions the very next run, loudly, always reversible,
never touching existing positions or their stops. A verdict nobody acts on is a
diary. 24 tests, including several that require the framework to correctly say
no on pure-noise and dead-flat records — the failure mode that costs years is
reading zero as promising.

### Sizing — the cash bug, quality enforcement, direction, fractional shares

`size_position` sized against account *equity*. The agentic account is a *cash*
account with T+1 settlement, where equity and buying power are very different
numbers — nothing prevented sizing an order the broker would reject for lack of
settled funds. It now takes `buying_power` explicitly and reports
`cash_limited` and which constraint actually bound.

An `Estimate`'s `quality` flag was carried through by convention and never
enforced. `stop_plan` now raises if handed a quality outside an explicit
allow-list, and `size_position` scales the position down for `thin` (65%) and
`degraded` (40%) quality rather than sizing them identically to `ok`.

`stop_plan` gained a `direction` parameter (`"long"`/`"short"`) — nothing
previously checked this, so a short position would have silently received a
long-side stop, the wrong side of the market rather than merely a worse number.
Unused today (the account holds no shorts) but no longer a silent gap.

`require_whole_shares=False` was accepted and had no effect — shares were
always floored to an integer regardless. It now genuinely sizes fractionally,
gated at a $1 minimum notional, for whichever account eventually needs it.

### The rest of the code audit

- **`vol_percentile`** now fails outright (quality `"failed"`) below 50%
  coverage of its 252-day lookback, rather than returning a number flagged only
  `"thin"`. **`trend_state`** reports `long_history_available` explicitly
  rather than only a suffix on the state string.
- **Concentration threshold recalibrated.** The eigen-share cutoff at 0.60
  never fires for a realistically correlated equity book — 20 names at a true
  0.55 correlation land around 0.57. The primary trigger is now
  ratio-based: `concentrated` when effective bets fall under half the number
  of names examined (an absolute "fewer than 5 bets" floor was tried first and
  rejected — on a genuinely independent 5-name book, sampling noise alone
  regularly dips the estimate under 5). The eigen-share cutoff is kept as a
  second path at 0.45.
- **`score_closed_decisions`'s `statistically_meaningful` field mixed a bool
  and the string `"provisional"`.** Any `if result[...]:` check treated
  `"provisional"` as truthy, silently certifying a sample the verdict text
  next to it calls provisional. It is now always one of exactly `"no"` or
  `"provisional"`, never a bare bool.
- **`runlog._reconcile`'s tolerance** was `1e-6`, tighter than real broker
  rounding noise; raised to `1e-4` to match `ledger.QTY_TOL`, which was derived
  from the same real payloads.
- A dead, unused variable in `washsale.Registry.check_buy` was removed.
- **`GAP_RISK_HAIRCUT = 0.25`** — a judgment call, documented in `quantcore.py`
  and section 3: since stops cannot execute outside regular hours, sizing now
  assumes a smaller effective risk budget than requested rather than pretending
  the stop covers a risk it structurally cannot.

### Robinhood tool set — the previous constraints were partly stale

Enabling the full Robinhood MCP tool set on 31 August surfaced
`cancel_equity_order`, which did not exist when this system's "no cancel tool"
design (good-for-day stops, re-derived every morning) was written. That design
remains the default — it costs nothing and adapts to volatility daily — but a
stuck order no longer has to be waited out; it can be cancelled directly. The
stale SGOV stop was cancelled this way (confirmed via live order history:
`state: cancelled`), and `TRIGGER_PROMPT.md` now instructs cancelling a stale
order that blocks a correct trade rather than only reporting it.

The three-position GLDM/VGSH/SGOV baseline changed too: GLDM (the unstoppable
directional holding) was sold 27 August 2026 at 90.79, a small loss against a
91.77 cost basis, opening a wash-sale block on GLDM/GLD/IAU through late
September. XLE was also opened and closed at a small loss the same window. Both
are exactly the loss sales the registry's first live read missed entirely.

### 235 tests, up from 116 at handoff

`test_ledger.py` (23) and `test_evidence.py` (24) are new. `test_quantcore.py`
grew from 45 to 76 for the sizing, quality-enforcement, direction, fractional,
and concentration-recalibration coverage.

## 11. Open and unverified

- Whether the connector broker refreshes the brokerage token indefinitely
  without a fresh desktop-browser sign-in. The token expires roughly every four
  days; refresh is supported but unconfirmed for this server. If it lapses, runs
  will abort at the tool-visibility check until re-authorised.
- **The redesigned memory and evidence pipeline is untested against a live
  weekday.** Everything in section 10c was built and unit-tested the same
  session it was designed in; it has not yet run against real broker order
  history inside the scheduled routine on a day the market is open.
- **`gap_risk_haircut` (0.25) and the concentration thresholds (0.5 ratio, 0.45
  eigen-share) are judgment calls, not measurements.** They should be revisited
  once enough real trading history exists to check them against actual
  overnight gap behaviour and actual portfolio correlation, respectively.
- **The pre-registered evidence claim** (0.50% edge, 6% assumed dispersion,
  decide-by 2027-06-30 -- see the earlier note in this file for the current
  parameters and always defer to `state.json.evidence.pre_registration` for
  the live values) implies roughly 900 closed trades to settle at the observed
  trade rate. That is measured in years. Whether to accept that timeline,
  relax the position-count caps to trade more often, or lower the claimed edge
  the system is willing to accept is a decision for a person, not this file.

## 12. Standing honesty rules

These are not decoration; they are the reason to trust the output.

- No process makes market calls reliably correct. The system guarantees
  **process** — no fabricated numbers, sourced and timestamped figures, a
  confidence threshold, and "do nothing" as a first-class output — not
  **outcomes**.
- The evidence framework in section 10c exists because a process guarantee is
  not the same claim as an edge, and conflating them is exactly how a project
  can run for a year on nothing. `evidence.required_sample` and
  `time_to_evidence` are meant to be read on day one, not discovered on year two.
- At a two-position daily cap plus whatever the individual account's
  suggestions add, the sample accumulates slowly. `evidence.assess` refuses to
  call anything an edge below the registered sample size, and `trading_policy`
  pauses new positions automatically the moment the claimed edge is ruled out
  rather than waiting for a person to notice.
- If the system ever reports that a strategy is working based on a handful of
  trades, that is overfitting and should be called out.
