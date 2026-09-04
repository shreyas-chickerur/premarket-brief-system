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

**Status (4 September 2026):** 343 tests passing. The system is scheduled and
running daily in dry-run mode (weekdays, 06:20 Central) — see section 8. It has
reached every stage on real order history three times in a row, every time
with both accounts reconciling to zero residuals — see section 11 for the
path to that point, including a corporate-split bug, a missed order state, a
too-tight watchdog offset that nearly started a duplicate trading run (fixed
and validated live the next morning), and a sizing bug that conflated a
mechanically-bounded stop with untrustworthy data. That last fix produced the
system's first-ever idea to clear all five gate conditions the very next
session — buy 2 OXY, matures 24 September 2026 — verified against live data,
not assumed. A watchdog routine (section 8) now diagnoses, fixes, and retries
a broken day automatically. The agentic account has not been traded except
for one deliberate throwaway test order and the 27 August GLDM/XLE loss sales
recorded in section 11; live order placement remains gated behind the
`THIS IS A DRY RUN` guard described in
section 8's "Path to live trading."

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
- **Pre-existing fractional positions cannot be stopped.** GLDM is the one
  carrying real directional risk while unstoppable; **only half the position**
  (1.089681 of 2.179363 shares) was sold on 27 August 2026 at a small loss,
  which opened the wash-sale block that runs through late September (see
  section 11) — the earlier version of this document said the position was
  fully closed, which was wrong. 1.089682 shares remain held, still
  unstoppable, still marked below its 91.77 cost basis. SGOV and VGSH remain
  as the yield-bearing reserve.
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
| `circuit_breaker_usd` | Agentic equity below this: stop opening new positions, existing positions and stops untouched, require a human `circuit_breaker_cleared` journal entry before resuming — enforced by `runlog.circuit_breaker_check` (section 3), not just reported. The "drop to level 4" refinement this line used to describe is not yet built; today it is a flat halt, not a demotion. |
| `hard_stop_usd` | Agentic equity below this: liquidate the agentic account to cash, halt entirely, email — same enforcement function, the harder of its two thresholds. Not a suggestion. |
| `wash_sale_block_enabled`, `wash_sale_window_days` | Cross-account wash-sale enforcement, 30-day window each side. |
| `asset_scope` | Stocks and exchange-traded funds only. |
| `tax_optimization`, `scheduled_contributions` | Off. |
| `suggestions_per_day_cap` | Unset — the gate is the limiter, not a quota. |
| `email_to` | Brief recipient. |
| `gap_risk_haircut` | Shrinks the effective risk budget to account for stops that cannot execute outside regular hours (default 0.25). See section 11. |
| `concentration_bets_floor_ratio`, `concentration_eigen_share_cap` | The recalibrated concentration thresholds (0.5 and 0.45) — see section 11. Read by convention; the actual gate values live in `quantcore.correlation_concentration`, not read from config at runtime yet. |

A separate top-level `evidence.pre_registration` object (not under `config`)
holds the pre-registered claim this system is being tested against — see
section 11. It is the one piece of state written once and deliberately never
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
| Fills and split events (never positions) | dated `fills-cache-*.json` / `splits-cache-*.json` files, same folder | Bounded-staleness cache so the full order history and every symbol's `SPLITS` don't get re-pulled every run — see below |

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
- **That rule stays exactly as it is — but the FILLS and SPLIT EVENTS
  positions are rebuilt from can now themselves be cached, once they are old
  enough to be safe (4 September 2026).** Before this, the full order
  history was re-pulled with no `created_at_gte` and `SPLITS` was called for
  every symbol, both on every single run, forever. `ledger.
  fills_ready_to_cache` only writes a fill to `fills-cache-*.json` once it
  is strictly older than `FILLS_CACHE_HORIZON_DAYS` (7 days) — long enough
  that a Robinhood order can no longer plausibly still be open — and Stage
  0 still always re-fetches that entire trailing window fresh from the
  broker every run regardless of what is cached, so a still-mutable order
  is never trusted from the cache before it has settled. `ledger.
  symbols_needing_split_check` skips `SPLITS` only for a symbol checked
  within `SPLITS_CACHE_HORIZON_DAYS`, so a real future split is still caught
  within a bounded window rather than requiring a daily check forever.
  Positions are still rebuilt fresh from the full cached-plus-new fill set
  every run and still reconciled against the live broker snapshot every
  run — nothing about what is trusted changes, only how much has to be
  re-fetched to reconstruct it. See `PROCEDURE_RATIONALE.md`'s "why fills
  and split checks are cached" and `HANDOFF.md` section 11.
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
| `DAILY_PROCEDURE.md` | The canonical Stage 0–6 trading procedure, followed by both the main routine and the watchdog's retry — not code, but the single source of truth for what either one actually does |
| `WATCHDOG_PROCEDURE.md` | The watchdog's own procedure: assess today's manifest, and on a real problem, diagnose, attempt a fix, merge it, and re-run `DAILY_PROCEDURE.md` once |
| `research.py` | Deterministic Stage 1 research — replaces "research by web search" with a defined candidate universe and one parser per feed (news, congressional/insider activity, scheduled events, filings, macro, commodities, positioning), every item graded `ok`/`thin`/`degraded`/`failed` and required to attach to a symbol or channel |
| `quantcore.py` | Five volatility estimators, ATR, GARCH, RSI, trend, volatility percentile, Ledoit-Wolf concentration, direction-aware stop derivation, cash- and quality-aware sizing, eight anomaly classes including split detection |
| `runlog.py` | Run manifests, staged timing, preflight self-audit (including a blocking `journal_fully_readable` check), verified market calendar, regression review, optimization proposals, honest scoring |
| `washsale.py` | Cross-account registry, both directions, proxy warnings |
| `ledger.py` | Positions and the wash-sale trade list rebuilt from broker order history; the append-only journal fold; the bounded-staleness fills/split-events cache (positions themselves are still never cached); optional monthly journal compaction, exactly equivalent to the daily fold it replaces; `run_entry(log)` pins the `"run"` journal entry's schema to exactly what `find_optimizations` reads |
| `evidence.py` | Pre-registered hypothesis testing, sample-size planning, futility stopping, and the policy that pauses new positions when the claimed edge is ruled out |
| `emailer.py` | HTML brief rendering, failure diagnosis, subject lines |
| `watchdog.py` | The outside check: did the daily run happen at all, and was it healthy — catches a hung run that never reached its own email |
| `pipeline_demo.py` | End-to-end demonstration run |
| `make_fixtures.py` | Regenerates the deterministic synthetic fixtures the demo falls back to |
| `test_quantcore.py` | 81 tests, including known-answer volatility and correlation recovery, and the floor/cap quality-conflation regression |
| `test_runlog.py` | 57 tests, including daylight-saving drift, market-calendar integrity, circuit-breaker enforcement, the blocking `journal_fully_readable` check, and `abort()`'s first-reason-wins semantics |
| `test_washsale.py` | 32 tests |
| `test_ledger.py` | 88 tests: real broker order-history fixtures, split adjustment, the partially-filled-rest-cancelled fix, the opening-balance mechanism, the standing circuit-breaker fold, the fills/split-events cache (fold, watermark, horizon boundary, round-trip equivalence to a direct fetch), monthly journal compaction (exact equivalence to the daily fold, the monthly-file-supersedes-leftover-daily-files guarantee), and `run_entry` (schema pinning, round-trip through a folded journal directly into `runlog.find_optimizations`) |
| `test_evidence.py` | 28 tests, including known-answer edge detection and futility |
| `test_watchdog.py` | 15 tests |
| `test_research.py` | 45 tests, against recorded fixtures in `fixtures/research/`, never the live API |
| `test_procedure_docs.py` | 6 tests: the DRY RUN guard's exact text, and that every stage has a matching rationale section |
| `test_emailer.py` | 34 tests, including escaping, no-research-on-abort, and the `idea_card`/`idea_cards` bulleted format |

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
python -m pytest -q          # 343 tests
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
   one source. Mechanical, not a judgment call, since `research.py` (section 11,
   4 September 2026): `bundle.corroborated(symbol)` counts distinct sources
   among a symbol's usable news items.
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

The trigger prompt itself is a short pointer stored in the trigger (it names
the accounts' Drive IDs, so it isn't committed here) — it clones the repo and
tells the agent to follow `DAILY_PROCEDURE.md`, the canonical Stage 0–6
procedure that lives in this repository and is the actual source of truth.
`TRIGGER_PROMPT.md` is the template for the short pointer itself; the filled
version (with real IDs) is in `HANDOFF.private.md`. The procedure's shape:

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
- **Stage 6 — Record, and send only some of the time.** Manifest and journal
  to Drive always. The email itself only sends here if this run succeeded or
  the market was closed; on an ABORT this stage writes the record and stays
  quiet, deliberately — see "The watchdog" below for why.

**Exactly one email per trading day, always, eventually.** If this run
succeeds outright, it sends immediately. If it aborts, it sends nothing and
the watchdog (60 minutes later) owns diagnosing, retrying, and sending the
one final email — see "The watchdog" below and `WATCHDOG_PROCEDURE.md`.
Silence is never the outcome by the time the watchdog's pass is done, even
on a day nothing could be fixed.

### The watchdog — the check the run cannot perform on itself

A second, separate routine, "Pre-Market Brief System — Watchdog," fires 60
minutes after the main run (07:20 Central) and reads `watchdog.py`. It exists
for one failure the main run structurally cannot report on its own: on 31
August a run hung indefinitely on a sandbox permission prompt meant for an
interactive human, and because it never reached Stage 6, it sent no email —
the one outcome this system is built never to produce, reached anyway, from
the outside, by a run that could not know it had failed.

Each fire, the watchdog:

1. Lists the Drive folder for a `run-manifest-YYYY-MM-DD[-N].json` dated
   today. **None found does not immediately mean the main run is missing** —
   it re-checks once after a short wait before concluding that, because the
   main run can legitimately still be in progress (see the 2 September 2026
   entry in section 11). Only genuinely absent after that recheck counts as
   `no_run`.
2. If found, reads the latest one and checks `aborted`. **`aborted`, not
   "did research happen," is the signal** — a closed-market day is
   `aborted: false` and correctly produces no alert; only a genuine blocking
   failure does.
3. On an aborted run (or a genuine `no_run`), diagnoses with `emailer.diagnose()`
   — the same function the daily brief itself uses — attempts a fix when
   confident, and re-runs `DAILY_PROCEDURE.md` itself once, which is what
   actually sends the day's email (see "One consolidated email" below).
4. **A healthy day sends nothing.** Twenty routine "all clear" emails train
   the reader to stop reading watchdog mail at all, which defeats the one day
   it matters.

The watchdog only reads Drive and sends mail directly — it carries neither
the Robinhood nor Alpha Vantage connector on its own configuration, so it
cannot place an order except by way of following `DAILY_PROCEDURE.md`
itself, the same code path with the same DRY RUN guard.

**Self-heal, with a deliberate merge authorization (1 September 2026): the
watchdog may commit its own fixes straight to `main`, no review gate, in
service of the goal that this system stay automated and self-healing without
day-to-day intervention.** `WATCHDOG_PROCEDURE.md` is the actual logic; in
outline, on a real problem it diagnoses with `emailer.diagnose()` (same
function the daily brief itself uses), and — only for a concrete, narrow,
well-understood fix matching the kind this repo's history shows (a missing
allow-list entry, a data-shape mismatch, a new corporate-action type) — writes
it, adds tests, and **if the full suite passes, commits directly to `main`
and pushes, with no PR review gate.** It then re-runs `DAILY_PROCEDURE.md`
once, itself, so the day's trading is retried on the fixed code rather than
left broken. Three things stay off-limits regardless of that authorization,
stated as absolutes in `WATCHDOG_PROCEDURE.md` Stage 5 step 2: never touch
`place_equity_order`-related code, never weaken a Stage 0 safety or
reconciliation check, and never remove or alter the `THIS IS A DRY RUN`
guard anywhere. Deciding to go live is a separate, one-time human decision —
see "Path to live trading" below — not something either routine's ordinary
self-heal authority extends to.

**One consolidated email, not two.** The 06:20 routine sends nothing on an
abort — it just writes the manifest and stops. The watchdog owns everything
that happens next and `DAILY_PROCEDURE.md`'s own Stage 6 is the only place
that sends mail: immediately, if the 06:20 run actually succeeded; otherwise
once the watchdog's single retry is done, whether that retry fixed the day
or is still reporting a failure. Either way exactly one email goes out per
day, trimmed to exactly three sections — **"Agentic
account — activity," "Individual account — suggestions,"** and **"System
health"** (which includes a plain note on what was diagnosed and fixed, when
the watchdog's retry did that) — dropping the older "Evidence review" /
"Where things stand" / "What moved and why" / "Risk measurement" sections
from the email body itself (that detail still lives in the run log and
journal, just not narrated in the inbox every morning).

**First verification run (1 September 2026) — a real bug found and fixed
same-day, before any of the above existed.** Fired by hand to test the
newly built watchdog (at the time, alert-only with a never-merged optional
PR), it correctly picked the day's *latest* manifest (a run at 19:11Z that
reconciled cleanly, superseding an 11:32Z run that had aborted on
MBGL/MSFT/FIG before opening balances were recorded) and correctly stayed
silent. Along the way it discovered that Google Drive's `read_file_content`
markdown-escapes JSON text (backslash-escaping underscores and brackets),
which breaks `json.loads` outright — for the watchdog this would silently
look like `no_run` if it were the only manifest of the day, and for the main
routine's journal fold (`ledger.fold_journal`, step 9) it fails quietly into
`unreadable`, dropping a day's theses/opening-balances with no visible
error. The run worked around it live by falling back to
`download_file_content` (raw base64) and decoding by hand; `DAILY_PROCEDURE.md`
and `WATCHDOG_PROCEDURE.md` both use `download_file_content` for every JSON
read from Drive as a result, rather than relying on every future run to
rediscover the same workaround.

Same cron pattern and DST changeover as the main run, offset by 60 minutes
(widened from 30 on 2 September 2026 — see section 11 for why):

| Period | Cron |
|---|---|
| Daylight time | `20 12 * * 1-5` |
| From 1 Nov 2026 (standard time) | `20 13 * * 1-5` |
| From 14 Mar 2027 | `20 12 * * 1-5` |

### Path to live trading

The agentic account is a **real Robinhood brokerage account**, not a paper
sandbox — the only thing standing between today's dry run and real orders is
the `THIS IS A DRY RUN` paragraph at the top of `DAILY_PROCEDURE.md` Stage 0.
Removing it is a one-time, human decision (see `TRIGGER_PROMPT.md`,
"Dry-run vs. live") — deliberately left out of the self-heal authorization
above, and out of scope for either routine to do on its own, regardless of
how the merge authorization for ordinary bug fixes is worded.

What should be true before that paragraph comes out, roughly in order:

1. **Let the self-heal loop actually prove itself first.** It was built and
   verified once (1 September 2026) against real Drive data, but has not yet
   had to diagnose-fix-merge-retry a real `aborted` day end to end. Watch a
   few weeks of daily emails — including at least one day it had to heal
   itself — before trusting it unattended with real orders.
2. **Watch the qualitative call quality, not just "no crashes."** Read the
   "Individual account — suggestions" section for a stretch of days: are the
   picks and sizing explainable and sane day after day? A system with zero
   bugs can still have no edge; a system with an edge can still have bugs.
   These are different questions and both matter.
3. **Do not wait for the pre-registered evidence claim to reach full
   statistical power before going live** — at the current trade rate,
   reaching the ~891-trade sample the registered claim (`target_edge_pct
   0.50%`, `decide_by 2028-02-28`) needs could take years. `evidence.assess`
   exists to catch **futility** early (rule the claimed edge out fast if the
   data says so) and to pause new positions if that happens — not to be a
   gate you wait years to clear. Go live once 1 and 2 above hold, and keep
   watching the evidence section after that; `trading_policy` will pause new
   positions on its own if the data turns against it.
4. **Shrink the risk budget below what `state.json.config` currently allows**
   — revisit at the moment of going live, not before. On an account this
   small (~$1,000), `risk_budget_fraction` (2%) and `max_weight_agentic`
   (18%) are already tiny in absolute dollars (~$20 risked, ~$180 capped per
   position); cutting them further risks pricing the whole-share constraint
   out of nearly every position, the same failure mode the 2 September
   sizing bug caused for a different reason. The lever that actually matters
   at this size is `circuit_breaker_usd` / `hard_stop_usd` (currently 700 /
   500 — roughly 30% / 50% down from today's equity): decide how much of
   the account you're willing to risk before flipping the guard, and set
   these two numbers to that, not to an arbitrary fraction of the current
   ones.
5. **Confirm the circuit breaker and hard stop actually halt trading** — done
   4 September 2026. `runlog.circuit_breaker_check` is real enforcement, not
   a status line: `DAILY_PROCEDURE.md` Stage 5 calls it before sizing or
   placing anything, halts new positions or liquidates as the two
   thresholds require, and requires a human-written
   `circuit_breaker_cleared` journal entry before a future run resumes —
   equity recovering on its own is explicitly not enough. See section 11.
6. **When ready, the mechanical change is small:** in the live main-routine
   trigger, delete the `THIS IS A DRY RUN` paragraph (and the `prefix`
   argument in Stage 6's email call) and rename the trigger away from
   "DRY RUN." Do this yourself, in the trigger config — it is the one edit
   this system will not make to itself, self-heal authorization or not.
   Watch the first several live days closely by hand rather than trusting
   the watchdog's silence-on-healthy immediately; a quiet watchdog on day one
   of real trading is worth a manual double-check regardless.

#### Status snapshot (updated after each material change; latest 4 September 2026)

| # | Item | Status |
|---|---|---|
| 1 | Self-heal proves itself, including a real fix-and-retry cycle | **Not yet.** One near-miss caught and fixed (2 September — a too-tight watchdog offset), but that was found in a dev session, not by the watchdog completing a real diagnose-fix-merge-retry against a genuinely `aborted` run. 3 September validated the *healthy* path only: the widened 60-minute offset gave the watchdog a clean first-listing read, no recheck or retry needed. |
| 2 | Call quality over a stretch of days | **Improving, still early.** 3 research sessions now (1, 2, 3 September). 3 September produced the **first idea ever to clear all five gate conditions** — buy 2 OXY, limit 61.80, stop 58.09 — with a genuinely two-sided read (it flagged Brent falling on the same Iran-diplomacy news it was buying into, rather than one-sided reasoning). Three days is not a track record yet. |
| 3 | Don't wait for full statistical power | **On track, unchanged.** Still `n=0`, correctly not gating on this. The OXY thesis opened 3 September is the first real open position the evidence framework has ever tracked; it matures **24 September 2026**, which will be the first genuine test of the settlement/scoring path end to end — worth a checkpoint on that date, not because one trade proves anything. |
| 4 | Shrink the risk budget for the first live stretch | **Reframed, not applied.** At this account size the sizing knobs are already near their practical floor; the meaningful decision is the two dollar thresholds in item 5, made at the moment of going live — see the item's own text above. |
| 5 | Confirm circuit breaker / hard stop actually halt trading | **Done, 4 September 2026.** Real enforcement (`runlog.circuit_breaker_check`), tested, wired into Stage 5, human-clear-only by design. See section 11. |
| 6 | The mechanical flip | **Not done, as it shouldn't be yet.** |

Net: item 5 is now genuinely done, and item 2 has one real data point in its favor (found for a traceable reason, not luck). Items 1, 4 (in its reframed form), and 6 are still open — a real self-heal cycle has never happened, the go-live risk thresholds haven't been decided, and the guard itself is untouched. **Next scheduled reassessment: 3 weeks out (around 25 September 2026), after the OXY thesis has settled.**

---

## 9. The email

The email is the only artefact a human reads, so it is rendered by tested code
(`emailer.py`), not composed freehand each morning. That keeps the format from
drifting between runs and stops a failed run from sending a worse email than a
successful one.

```python
import emailer
subject, html = emailer.render_email(
    log.manifest(),
    sections=[("Agentic account — activity", emailer.idea_cards(ideas))],
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
- **A suggestion or activity item is a card, never a paragraph** (`emailer.idea_card`
  / `idea_cards`, 4 September 2026). Symbol, action, and quantity on one line;
  reasoning as bullets below it, each tagged with the specific source that
  backs it. A name and a number buried in a sentence are slower to scan than
  the same two things in a card's first line — this replaced hand-written
  prose after exactly that complaint.

A completed run keeps the full brief: health line, then exactly three
sections — **"Agentic account — activity," "Individual account —
suggestions," "System health"** (trimmed to just those three, 1 September
2026, on the view that a daily trading email should read like a status
report, not a market-commentary newsletter; the older "Evidence review" /
"Where things stand" / "What moved and why" / "Risk measurement" sections
still get computed and logged, just not narrated in the inbox) — then the
decisions table with the failing gate named on every rejected idea.

## 10. Remaining work

Everything from initial build-out through the first complete dry run —
pushing the repo, the holiday-calendar fix, the scheduled task, connector
attachment, rebuilding state from broker history instead of storing it,
the evidence framework, and proving the pipeline against real order
history — is done; section 11 has the record of how. What is actually left:

1. **Let the self-heal loop prove itself over more real trading days**
   before trusting it fully unattended — see section 8's "Path to live
   trading" for the full checklist.
2. **Decide what to do about the remaining half of the unstoppable GLDM
   position** (section 11) once it clears the gate.
3. **Extend the holiday table** before `HOLIDAY_TABLE_HORIZON` (2027-12-31).
4. **Revisit `gap_risk_haircut` and the concentration thresholds** once real
   trading history exists to check them against, rather than the judgment
   calls they are today.
5. **Go live** — a deliberate, one-time, human decision. See section 8,
   "Path to live trading."

## 11. Build history

A record of what this system found and fixed against itself. Kept because
this kind of institutional memory is worth more archived than
rediscovered — several of these entries are exactly the class of bug that
recurs once documentation forgets it happened.

### 31 August 2026 — connectors, then four defects on first contact with live data

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
  sales. Fixed by rebuilding it from broker history every run (section 11).
- **`vol_percentile` and `trend_state` were reported `thin`/degraded for all 23
  symbols** instead of unavailable, when compact payloads (~100 bars) fall far
  short of their 252- and 200-day requirements. Fixed: `vol_percentile` now
  fails outright below 50% coverage.

A stale good-for-day stop was also found holding 1 share of SGOV hostage,
rejecting a correct 4-share sell whole rather than partially. The owner
cancelled it by hand and enabled the full Robinhood tool set, which turned out
to include `cancel_equity_order` — not available when this system was
designed. See section 11.

### 31 August 2026 — memory, evidence, sizing, and a full code audit

A full pass the same day fixed the memory problem with real tests, added
cash-aware sizing, and fixed every other known code defect — and, the one
that mattered most, built a real, ongoing way to answer whether this system
has an edge. That last piece exists because an earlier, unrelated project
never produced clear evidence either way, and the time and money spent on it
were wasted for exactly that reason. Everything below was built and tested
the same day.

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

The goal was a way to answer whether this system has an edge, reviewed on
a standing cadence — not a one-time claim to make once and move on from.

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

The three-position GLDM/VGSH/SGOV baseline changed too: **half** of the
GLDM position (1.089681 of 2.179363 shares; verified against live order
history, order `6a8f4ba5`) was sold 27 August 2026 at 90.79, a small loss
against a 91.77 cost basis, opening a wash-sale block on GLDM/GLD/IAU through
late September. **1.089682 shares of GLDM remain held** — this document
previously said the position was closed, which the 1 September live run's own
reconciliation output caught as wrong; corrected here. XLE was also opened and
closed at a small loss the same window. Both loss sales are exactly what the
registry's first live read missed entirely.

### 273 tests, up from 116 at handoff

`test_ledger.py` (45, including split adjustment, the FIG fix, and opening balances) and `test_evidence.py` (24) are new. `test_quantcore.py`
grew from 45 to 76 for the sizing, quality-enforcement, direction, fractional,
and concentration-recalibration coverage.

### 1 September 2026 — the redesigned pipeline's first live run, a real bug correctly caught

The memory rebuild from section 11 was proven the same day it was written, on
the actual account, during market hours. It found a genuine defect and stopped
rather than working around it, which is what a proving run is for.

**What happened.** Stage 0 step 7 pulled full order history with no date
floor, as designed — 10 orders on the agentic account, 880 on the individual
account spanning June 2022 to August 2026. The agentic account reconciled
exactly: 3 positions, 10 fills, zero drift. The individual account did not: 10
of 21 symbols disagreed with the broker, e.g. NVDA off by 19.83 shares, CMG off
by 1.51. The run correctly diagnosed every discrepancy as a corporate-action
artifact from the fill price ranges alone (NVDA fills spanning \$113.67 to
\$1,237.68 is not one stock's ordinary price history) and **aborted rather than
guessing** — no research, no sizing, no orders, an aborted-run email sent, both
Drive files written. Exactly the designed behaviour: inaction plus a report.

**Root cause.** `ledger.fills_from_orders` reports fills the way the broker
recorded them: a share bought the day before a 4-for-1 split is one pre-split
share, not the four post-split shares it became. `positions_from_fills` summed
those raw quantities and compared them against `get_equity_positions`, which is
always in TODAY's post-split terms. A multi-year, no-date-floor history pull —
exactly what step 7 calls for — was guaranteed to cross a real split sooner or
later; it is not a hypothetical, it happened on the very first live run.

**Fix.** `ledger.py` gained `SplitEvent`, `splits_from_api` (parses Alpha
Vantage's `SPLITS` response), and `apply_splits`, which re-expresses every fill
strictly before a split's effective date in current share terms — quantity
multiplied by the ratio, price divided by it, so the fill's notional value is
unchanged and only the share-count convention shifts. Multiple splits on one
symbol compound correctly regardless of the order they are supplied in. A
symbol with no split data passes through as a deliberate no-op, so a symbol
someone forgot to look up fails LOUDLY (a reconciliation mismatch) rather than
silently.

**It was worse than a reconciliation failure.** `cost_basis`, `loss_sales`, and
`to_washsale_trades` all do FIFO lot accounting that sums quantities across
fills as one unit. On a synthetic NVDA-shaped position (12 shares bought
pre-split, 20 sold post-split), the unadjusted version doesn't just mis-count —
it reports the position as **fully closed** when 28 shares actually remain, and
the average cost is off by exactly the split factor. A wash-sale registry built
on that would misjudge every loss sale on a split-affected symbol. All three
functions' docstrings now say explicitly that they expect split-adjusted fills.

Stage 0 step 7 now calls Alpha Vantage `SPLITS` for every symbol across both
accounts' fills and applies the adjustment before deriving positions,
reconciling, or building the wash-sale registry. 12 new tests in
`test_ledger.py`, including a reconstruction of the exact failure shape (not
the real account's actual trade sizes, which stay out of the public repo) that
fails without the fix and passes with it.

### 1 September 2026 — the split fix's first live test, down to three, and a second real defect

The routine fired again live on 1 September 2026, after the split fix from
10d. It worked: 10 disagreeing symbols became 3. The email itself said so —
"Split-adjusting the fills against Alpha Vantage SPLITS resolved 7 of them
(WMT, CMG, VUG, CRWD, NFLX, GOOGL, NVDA)" — and it investigated the remaining
three rather than reporting a bare number, correctly separating a second code
defect from two facts about history that no code fix could ever produce.

**FIG — a second real bug, now fixed.** `fills_from_orders` allow-listed
exactly two order states, `filled` and `partially_filled`. A real FIG order
from 24 July 2025 has the terminal state `partially_filled_rest_cancelled` —
1.0 share genuinely executed at $33.00, with the unfilled remainder cancelled
— and that state was never on the list, so the fill was silently discarded.
Fixed by dropping the allow-list entirely: any order with a positive
`cumulative_quantity` contributed a real fill, regardless of what happened to
the rest of it. `cumulative_quantity` already says authoritatively what
executed; a state-name allow-list was always going to miss a state nobody had
enumerated yet, and it did, on the very next attempt.

**MBGL and MSFT — not bugs, gaps in what fills can ever explain.** MBGL: 4.32
shares sold 24 August 2026 with no matching buy anywhere in the pulled
history — they arrived outside the order book (a transfer, a spin-off, a DRIP
conversion; which one is not yet known). MSFT: 0.021 shares sold in December
2022 with no prior buy, because the account already held MSFT before the
order history the API returns even starts. Both are the same shape of fact:
something true about history that fills structurally cannot contain, and no
amount of correct code will make them reconcile.

`ledger.py` gained an `opening_balances` mechanism for exactly this — a
`{symbol: quantity}` map that `positions_from_fills` and `reconcile_positions`
add in before comparing to the broker, sourced from a new `journal.opening_balance`
entry kind rather than invented, inferred, or silently accepted. **This is not
a way to make reconciliation pass** — an unrecorded residual is still a hard
abort, by test (`test_reconciliation_still_aborts_on_an_unrecorded_residual`).
It is a way to stop re-litigating the same already-understood 4.32 shares
every single morning once a human has actually recorded why. The MBGL and MSFT
entries are recorded in the journal folder for exactly this reason; if the
true cause of the MBGL transfer ever comes to light, correct the entry rather
than leaving a placeholder reason standing.

**A documentation error this run's output caught.** Section 10c said GLDM
"was sold" on 27 August, implying the whole position closed. The broker
disagrees: only half the position (1.089681 of 2.179363 shares) was sold that
day; **1.089682 shares of GLDM remain held**, still fractional, still
unstoppable, still marked below cost. Corrected in sections 3 and 10c. The run
found this itself, in its "where things stand" section, by comparing its own
rebuilt state against what earlier documentation claimed — which is precisely
the kind of self-check the regression review in `runlog.find_optimizations`
exists to eventually automate.

**Also observed and not yet acted on:** the 31 August verification run
stalled indefinitely on a sandbox permission prompt, meant for an interactive
human to click, that nobody was there to click — triggered by copying an
oversized `get_equity_orders` page (~200 orders, 147KB) that had been
auto-saved to disk rather than reading it in place. The 1 September run did
NOT hit this same stall, having read the equivalent oversized page in place
with `jq` instead of copying it — so it may be intermittent, tied to a
specific access pattern, or genuinely resolved by that change; not yet
understood well enough to call fixed, only that it has not recurred. See
section 12.

### 1 September 2026 — first complete run, every stage, first live brief

A second live fire the same day reached every stage for the first time: 28
checks, 0 blocking failures, `may_trade: true`, `aborted: false`. Both accounts
reconciled with zero residuals — individual (21 positions, 860 split-adjusted
fills plus the two recorded opening balances) and agentic (3 positions, 10
fills) — confirming the split fix, the FIG state fix, and the MBGL/MSFT
opening balances (10d, 10e) all hold together under a real run, not just under
test. The permission-prompt mitigation from 10e also held: the individual
account's ~1,000-order history spilled to disk exactly as expected, and the
run read it with `jq` with no stall.

The brief itself: one dated catalyst on each side of the wash-sale/whole-share
line (AAPL, blocked mechanically at the cash floor and the risk-budget/
whole-share rule; XLE, blocked by the 27 August loss sale), a VTI cap-breach
trim suggested for the read-only individual account, and every agentic holding
correctly held with its reasoning stated. Every number in it was independently
re-derived by hand afterward against the real formulas -- the AAPL risk-budget
figure, the VTI trim size, the cash-floor percentage, the multiplicity-adjusted
alpha -- and all matched exactly, which is the strongest evidence yet that the
system computes rather than narrates.

One cosmetic defect survived to the sent email: the literal word "False"
appeared twice as a Unicode replacement character followed by "lse" (e.g.
`pause_new_positions�lse`), in two terse key=value debug fragments mixed into
otherwise-normal prose. Every other figure in the email — including several
independently re-verified above — was unaffected. The cause was disposable,
run-specific formatting code that assembled those two lines differently from
the rest of the narrative (which reads as ordinary sentences, not debug
output); since that code never touched the repository, there is nothing here
to patch, but the daily procedure now says explicitly to render every fact as
prose through the same path, never as a separate hand-rolled key=value
fragment.

### 2 September 2026 — a watchdog near-miss, and a sizing bug

Two real problems surfaced from the second consecutive complete run, both
fixed the same day.

**The watchdog nearly started a duplicate trading run.** The main run took
about 39 minutes end to end that morning — a research-heavy day — but the
watchdog's offset was only 30 minutes. It listed Drive at the 30-minute
mark, found no manifest yet because the main run was still in progress, and
its procedure at the time treated that as `no_run`: no manifest by the
deadline means the main run is missing. It began the prescribed retry —
cloned the repo, pulled both accounts' full order history — before, partway
through Stage 0, noticing a journal file appear in a Drive listing with a
timestamp *after* its own check had started. It re-listed the folder on its
own initiative, found the real manifest, confirmed the main run had
completed normally (`aborted: false`), and abandoned the retry: no orders,
no Drive writes, no repo pushes. It reported the near-miss directly rather
than treating "turned out fine" as nothing to mention.

Left alone, a slower day would not have gotten the same lucky catch, and two
full trading attempts running back-to-back is exactly the same failure
shape as a same-day duplicate run that has caused real damage before. Fixed
two ways: the offset widened from 30 to 60 minutes (section 8's cron table),
and `WATCHDOG_PROCEDURE.md` Stage 2 now requires a single recheck — wait
roughly 10 minutes and re-list Drive once more — before a missing manifest
counts as `no_run` at all, turning what the watchdog improvised into a
designed step.

**A sizing bug cut positions to 40% for a reason that had nothing to do with
data quality.** `quantcore.stop_plan` downgraded a `StopPlan`'s `quality` to
`"degraded"` whenever the stop distance was floored or capped — a
deliberate risk-management bound — even when the underlying volatility
estimate was itself `"ok"`. `size_position` then read that as a genuine
data-quality problem and applied the 40% `degraded` scaler. Since floor and
cap trigger on nothing more than "unusually quiet" or "unusually wild"
volatility, this quietly gutted sizing for exactly the stocks it fires on
most often: XOM (1 September and 2 September) and AAPL (1 September) both
sized toward zero shares for this reason, not because their data was
untrustworthy. Fixed by leaving `quality` alone when floor/cap fires —
`floored`/`capped` already carry that fact on their own dedicated fields and
never needed to double up onto `quality` — with a regression test proving
an `"ok"`-quality estimate stays `"ok"`, and sizes identically, whether or
not its stop got floored or capped.

### 3 September 2026 — both fixes proved themselves on the first real morning after

**The watchdog offset fix held.** The 06:20 run finished and wrote its
manifest at 12:07:43 UTC; the watchdog fired at 12:24:23 UTC (the new 60-minute
gap, drifted 4 minutes) and found that manifest on its very first Drive
listing — no recheck-wait needed, no premature `no_run`, no unnecessary
retry. The boring, correct outcome.

**The sizing fix produced the system's first-ever gate-clearing idea.** Third
consecutive zero-residual reconciliation, then OXY sized to 2 shares — buy,
limit 61.80, stop 58.09, 12.48% weight, $7.42 at risk, `review_equity_order`
clean — where the same inputs would have sized to 0 the day before the fix.
The run did not take this on faith: it re-derived the sizing by hand against
live data and confirmed the difference traced to commit `583d027`, not to a
change in market conditions. No order was placed (DRY RUN held). The thesis
opened the same run and matures 24 September 2026 — the first real position
the evidence framework has ever tracked end to end, and the first date worth
checking back on for that reason specifically.

### 4 September 2026 — the circuit breaker went from reported to enforced, and the email from prose to cards

Two pieces of prep work for the eventual live decision, neither touching the
`THIS IS A DRY RUN` guard itself.

**The circuit breaker and hard stop are now real.** Every prior version of
this system only ever *reported* where equity sat relative to
`circuit_breaker_usd` and `hard_stop_usd` — nothing in the codebase actually
stopped an order because of them. `runlog.circuit_breaker_check` fixes that:
called before any sizing or placement in Stage 5, it returns
`halt_new_positions` and `liquidate` as separate, correctly-ordered
thresholds (the hard stop is checked first and liquidates; the softer
circuit breaker only pauses new positions, existing ones and their stops
untouched), and — deliberately — **neither one un-halts on its own once
equity recovers.** A `circuit_breaker_tripped` journal entry is written
automatically when either threshold is crossed; only a human ever writes the
matching `circuit_breaker_cleared` entry (`ledger.Journal.standing_circuit_breaker`
reads whichever came later), and `WATCHDOG_PROCEDURE.md`'s existing hard
limits already forbid the self-heal path from writing one on its own. 10 new
tests.

**The suggestion and activity sections moved from prose to cards.** A
complaint about the email: paragraphs buried the one thing worth scanning
for — which symbol, which action, how many shares — inside sentences.
`emailer.idea_card` / `idea_cards` render each idea as symbol, action badge,
and quantity on one line, then the reasoning as bullets, **each tagged with
the specific source that supports it** (a data provider, a named report, a
computed check) rather than an unattributed paragraph of synthesis.
`DAILY_PROCEDURE.md` Stage 6 now builds both the "Agentic account —
activity" and "Individual account — suggestions" sections this way. 8 new
tests.

### 4 September 2026 — the procedure documents split from their own rationale

An external review of this project (list of twelve findings, independently
verified against the code before any of them were acted on) flagged that
`DAILY_PROCEDURE.md` mixed load-bearing rules with the story of the morning
that produced them — a rule an agent must follow exactly is harder to spot
in the middle of a paragraph about 31 August 2026 than on its own line.
`DAILY_PROCEDURE.md` and `WATCHDOG_PROCEDURE.md` now hold only imperative
rules, one per line or short paragraph, no dates, no history. The
explanation moved to the new `PROCEDURE_RATIONALE.md`, cross-referenced by
stage and step number — nothing was dropped, only relocated. This is Task 1
of that review's ordered work list; the remaining eleven follow in their own
commits.

### 4 September 2026 — `research.py`, a deterministic Stage 1

Task 2 of the same review, and the largest: "research overnight news, macro
events, earnings, and filings by web search" was Stage 1's entire
specification — improvised fresh each morning, not reproducible run to run,
and the slowest stage on record for exactly that reason. `research.py`
replaces it. It calls no API itself — every parser is a pure function of an
already-fetched raw response, the same boundary `quantcore.py` draws against
DataFrames, which is what makes it testable against recorded fixtures
(`fixtures/research/`) instead of the live API.

Every `ResearchItem` carries a value, a source, an as-of timestamp, and a
quality flag from the same vocabulary `quantcore.Estimate` uses; a failed
feed reports `quality="failed"` rather than being defaulted or skipped. Every
item must attach to a symbol or a named macro/commodity channel plus a
one-clause mechanism, or it is refused at construction — research exists to
inform decisions about real holdings and candidates, not to narrate the
market. `bundle.corroborated(symbol)` makes the five-condition gate's "two
independent corroborating sources" a mechanical count (`research.py` pulls
Alpha Vantage `NEWS_SENTIMENT` and Robinhood `get_equity_news` specifically
so there are two sources to count) instead of a judgment call.

The candidate universe was the other half of the same problem — undefined
anywhere in the codebase before this. `research.candidates()` unions four
sources: held positions, `state.json.config.watchlist`, today's
`TOP_GAINERS_LOSERS`, and names sharing a sector (`SECTOR_MAP`, the same
data-as-code pattern as `washsale.PROXY_GROUPS`) with a held position.
Weather enters through exactly one path, `WEATHER_MAP`: a symbol not listed
gets no weather item, regardless of how newsworthy the weather is generally
— a general weather narrative was explicitly the thing to keep out of the
brief.

45 tests, all against recorded fixtures. `DAILY_PROCEDURE.md` Stage 1 now
calls `research.candidates()` and `research.gather()` instead of describing
an unstructured web search. **Not yet run against the live Alpha Vantage or
Robinhood connectors** — see section 12.

### 4 September 2026 — the fixtures were fabricated, and live checking found what that hid

The prediction in section 12 above came true within a day: a live check of
three feeds against the real Alpha Vantage and Robinhood connectors found two
parsers reading field names that do not exist, because every fixture
`research.py`'s first version shipped with was hand-written, not a response
either API had ever actually returned.

- **`CONGRESS_TRADES` and `INSIDER_TRANSACTIONS` had their field names
  swapped against each other.** The real congress response is `{"trades":
  [...]}` with `symbol`/`transaction_type`/`amount_min`/`amount_max` already
  on each row (no `POLITICIAN_METADATA` join needed) and no bulk pull — one
  call per symbol. The real insider response keys rows on `ticker`, not
  `symbol`. The first version of each parser used the other one's real field
  name, so both always produced zero items against genuine data — OXY alone
  has 58 real congressional trades that would have rendered as "nothing
  here" every single morning, with every check still green, because
  `ResearchBundle.skipped` could not tell "not fetched" apart from "fetched,
  parsed to zero." Fixed by rewriting both to the confirmed real per-symbol
  shape, and by adding `ResearchBundle.coverage`/`coverage_issues()`:
  rows-seen vs. items-produced per feed, so a field-name mismatch shows up
  as a coverage issue rather than a quiet day.
- **Oversized payloads were entirely unhandled**, and both mechanisms
  reproduced live on the first real call: Alpha Vantage's own "preview"
  envelope (triggered on `INSIDER_TRANSACTIONS` for OXY — 27,944 lines,
  248,328 tokens) and the harness's separate file-spill (triggered on
  `NEWS_SENTIMENT(tickers="OXY", limit=2)` at 77,376 characters). Fixed with
  `_is_preview_envelope`/`_preview_item`, which reports what was truncated
  and where the full data lives rather than parsing the lossy
  `sample_data` sample — and this needed a second correction mid-fix: the
  real envelope keys are `total_lines`/`full_data_tokens`, not the
  `data_total_count`/`data_truncated` keys the first attempt assumed, which
  would have made every degraded item report `None` for the one number that
  makes it useful.
- **No shape-drift guard existed anywhere.** Added `ResearchShapeError` and
  `_shape_guard`, raised on any missing required key and caught per-feed
  inside `gather()`, converted to one loud `quality="failed"` item rather
  than crashing the whole gather. A parallel gap in `commodity_items` — one
  bad channel's response could abort every other symbol/channel pair sharing
  its single `gather()` call — got the same per-channel isolation.
  `_row_count`'s generic list-valued-key scan also missed the Robinhood news
  shape (`{"data": {"articles": [...]}}}`, a dict under `"data"`, not a
  list), silently under-counting rows-seen without ever being able to
  produce a false coverage pass; fixed for accuracy regardless.
- **Macro and commodity channels assumed the wrong response family
  entirely.** The real default for `CPI`/`WTI`/etc. is `{"result": "<CSV
  text>"}`, not `{"data": [...]}` — confirmed live, including a genuine
  malformed value (`"."` for a not-yet-published CPI month) now handled as
  `degraded` rather than crashing or silently passing through.
  `EARNINGS_CALENDAR` has the same CSV-wrapper shape and no `datatype`
  parameter at all. `IPO_CALENDAR`'s real schema has no `sector` field, so
  the sector-based attachment this parser tried to do was never actually
  possible — first fixed by making `ipo_calendar_items` always return `[]`,
  then removed from `gather()` entirely later the same day once that
  turned out to just be a permanently-empty call and a `skipped` noise
  line rather than a working feature (see the entry below).
  `REALTIME_PUT_CALL_RATIO`'s real key is `put_call_ratio_full_chain`, not
  `ratio`. Robinhood's real news key is `data.articles`, not `data.news`.
- Every fixture in `fixtures/research/` was replaced with a genuinely
  recorded response (or a real, representative truncated slice of one) —
  the fabricated `congress_trades.json`, `insider_transactions.json`, and
  `politician_metadata.json` are gone. `research.py` grew from 45 to 76
  tests, every one against a recorded real fixture, none hand-written.
  Full suite: 376, up from 292 at handoff.
- **Still not independently live-verified as of this entry**: 8 of the 9
  macro channels and 10 of the 11 commodity channels beyond the one
  representative example each (`CPI`, `WTI`) — assumed to share the same
  response family by provider and documented `datatype` toggle, not
  individually confirmed; `HISTORICAL_PUT_CALL_RATIO`; `EARNINGS_CALL_TRANSCRIPT`;
  Robinhood `get_sec_filing`/`get_sec_filing_facts`. **Update, same day**:
  the remaining macro/commodity channels, `HISTORICAL_PUT_CALL_RATIO`, and
  `GOLD_SILVER_SPOT` were verified live and fixed — see the entry directly
  below. `EARNINGS_CALL_TRANSCRIPT` and the Robinhood filing tools remain
  unverified; see section 12.

### 4 September 2026 — the rest of the feeds verified live, and one more wrong parser found

A second live check, the same day as the entry above, targeted exactly the
feeds that entry had flagged as unverified: the eight remaining macro
channels, the ten remaining commodity channels, `HISTORICAL_PUT_CALL_RATIO`,
`EARNINGS_CALL_TRANSCRIPT`, and both Robinhood filing tools. This found one
more wrong parser and produced two deliberate design changes.

- **`GOLD_SILVER_SPOT` was wrong.** It sits in `COMMODITY_CHANNELS`
  alongside ten genuine time-series channels, and `commodity_items` routed
  every channel through `_rows_from_series_response` uniformly. The real
  response is a live scalar quote — `{"nominal": "XAUUSD", "timestamp":
  "2026-09-04 18:34:58", "price": "4423.7080671183"}` — with no `result`
  and no `data` key, so it raised `ResearchShapeError` on every single
  call. This mattered more than the other unverified channels: `GLDM` is a
  real individual-account holding with no protective stop (section 3), and
  `GOLD_SILVER_SPOT` is its only commodity feed — the one channel covering
  the riskiest holding was the one that never worked. Fixed with a
  dedicated `_gold_silver_spot_row` parser, still isolated per-channel so
  a shape drift there can't take down `WTI`/`BRENT` for the same symbol.
- **The other eighteen channels checked out.** All eight remaining macro
  channels (`TREASURY_YIELD`, `FEDERAL_FUNDS_RATE`, `INFLATION`,
  `UNEMPLOYMENT`, `NONFARM_PAYROLL`, `RETAIL_SALES`, `REAL_GDP`,
  `DURABLES`) and all ten remaining series commodity channels (`BRENT`,
  `NATURAL_GAS`, `COPPER`, `ALUMINUM`, `WHEAT`, `CORN`, `COFFEE`, `SUGAR`,
  `COTTON`, `ALL_COMMODITIES`) share `CPI`/`WTI`'s response family exactly
  as assumed, confirmed by a live `datatype=json` call to each. A second
  real malformed value turned up along the way — `UNEMPLOYMENT`'s
  2025-10-01 print is also `"."` — handled by the same `_numeric_quality`
  path that already covered CPI's.
- **`HISTORICAL_PUT_CALL_RATIO` was correct, and was carrying unused
  information.** The real response includes `put_call_ratio_by_expiration`,
  a per-expiration array, which the parser discarded entirely. A near-dated
  ratio far above the full-chain number is exactly the kind of dated,
  specific catalyst the five-condition gate's catalyst condition is meant
  to catch, so `_near_term_put_call_signal` now carries the nearest
  expiration as its own item — but only when it diverges from the
  full-chain ratio by more than 50%, so a routine near-dated wobble can't
  manufacture a signal that isn't really there (Rule 1 still applies: no
  forced attachment).
- **`IPO_CALENDAR` was removed from `gather()` entirely.** It had already
  been confirmed dead on arrival (no `sector` field in the real schema,
  so `ipo_calendar_items` could only ever return `[]`), but `gather()` was
  still fetching it, calling it, and appending a permanent `skipped` line
  every run — a fetch, a call, and a noise line for a guaranteed zero, which
  reads as a working feature when it structurally cannot be one. The
  function is deleted; the real captured shape stays in
  `fixtures/research/ipo_calendar.json` and in this entry as the documented
  reason, so re-adding it is a small, deliberate change if a future
  response ever adds a usable field.
- 83 tests in `research.py` (up from 76), full suite 381 (up from 376).
  `EARNINGS_CALL_TRANSCRIPT` and Robinhood `get_sec_filing`/
  `get_sec_filing_facts` remain unverified — see section 12.

### 4 September 2026 — fills and split events are cached; positions are still never stored

Findings from the original review of this system were confirmed and fixed:
the full order history was pulled with no `created_at_gte` every single
morning, and `SPLITS` was called for every unique symbol on every single
run — both a fixed, growing cost paid daily for history that, past a short
window, cannot change.

`ledger.py` gained a bounded-staleness cache for fills and split events —
**not** positions, which the storage rule in section 5 above still forbids
storing at all. `fills_ready_to_cache` only writes a fill to
`fills-cache-*.json` once it is strictly older than
`FILLS_CACHE_HORIZON_DAYS` (7 days, generously longer than a Robinhood
order can plausibly stay open), and `DAILY_PROCEDURE.md` Stage 0 step 7
still always re-fetches that entire trailing window fresh from the broker
every run via `fills_cache_watermark`, regardless of what is cached — a
still-mutable order is never trusted from the cache before it has had time
to settle. `symbols_needing_split_check` skips `SPLITS` only for a symbol
checked within `SPLITS_CACHE_HORIZON_DAYS`, bounding a real future split's
detection latency to a known window rather than eliminating the check
outright. Both caches use the same append-only dated-file, fold-on-read
pattern the journal already established, for the same reason: the Drive
connector can create a file but not rewrite one.

`fills_cache_round_trip_matches_a_direct_fetch` is the test that matters
most here: reconstructing history from cached-plus-fresh fills must
produce the exact same derived positions as fetching everything fresh
every time, proven against the real 31 August 2026 order fixture.
`ledger.py`: 50 → 73 tests. Full suite: 381 → 404.

### 4 September 2026 — monthly journal compaction

As the journal accumulates one dated file per run indefinitely, folding it
means listing and reading every file that has ever existed, every run,
forever. `ledger.compact_journal_month` folds a calendar month's worth of
daily `journal-YYYY-MM-DD[-N].json` files into one
`journal-monthly-YYYY-MM[-N].json` file, entry order and content
unchanged — compaction reduces file COUNT, never information.

`fold_journal` treats a monthly-compacted file as the sole source for its
calendar month: any daily file left over from before compaction is skipped
rather than folded, which matters because the connector can create a file
but not delete one, so the old daily files stay in the folder forever
after compaction runs. `test_fold_journal_prefers_the_monthly_file_over_leftover_daily_files`
proves this directly — folding the daily files, the monthly file, and both
together all produce the exact same entry list, never a doubled one.

The monthly file is named `journal-monthly-YYYY-MM`, not
`journal-YYYY-MM` with the day omitted, specifically to avoid a regex
collision: `journal-2026-09.json` would be genuinely ambiguous against
a daily file whose day happens to look like a sequence number
(`journal-2026-09-01.json`) — `test_monthly_filename_regex_never_matches_a_daily_filename`
guards this directly.

`month_is_compactable` guards against compacting the current, still-
accumulating month or a future one — compacting a month that could still
receive a new daily file would let that later file silently disappear
from every future fold. **Compaction is a deliberate, occasional
maintenance operation, not part of the automated daily routine** — nothing
in `DAILY_PROCEDURE.md` calls it, and it is not scheduled anywhere. It is
meant to be run by hand (or by a future dedicated maintenance task) on a
closed month, e.g. once a quarter, not wired into every run's own logic.

`ledger.py`: 73 → 84 tests. Full suite: 404 → 415.

### 4 September 2026 — an unreadable journal file now aborts the run instead of vanishing

`ledger.fold_journal` has recorded a file that failed to parse in
`Journal.unreadable` since 31 August 2026 — but nothing ever read that
list. The run proceeded as if the file had never existed. A dropped file
could hide a thesis that would have matured, an opening balance a human
recorded, or a standing circuit-breaker trip, none of which have any
other way to be noticed.

`runlog.preflight` gained a new blocking check, `journal_fully_readable`,
fed by `unreadable_files` — the union of `journal.unreadable` and the
`bad` lists Task 3's `fold_fills_cache`/`fold_splits_cache` already
return. It runs before `ledger_reconciled` deliberately: if the hidden
file carried an opening balance, running reconciliation anyway would
report confusing spurious drift instead of the real, nameable cause.
`RunLog.abort` was changed to keep the FIRST reason given rather than the
last, so this ordering actually survives a later check also calling
`abort()` — before this, whichever blocking check happened to run last
decided what the run reported, not the root cause.

`runlog.py`/`test_runlog.py`: 52 → 57 tests. Full suite: 415 → 420.

### 4 September 2026 — the `"run"` journal entry's schema is pinned in code

Stage 6's only instruction for the `"run"` journal entry used to be "a
compact summary for `find_optimizations`" — prose, not a pinned contract.
`runlog._regressions` and `runlog.find_optimizations` read specific
fields (`health`, `duration_ms`, `decisions[].action`/
`.inputs.recovered_within_5d`/`.gate_failed`/`.executed`,
`stages[].name`/`.duration_ms`) via `dict.get(..., default)` throughout —
a field that drifted out of sync between what got hand-written and what
those functions expect would not raise, it would just silently stop
contributing to the optimization findings.

`ledger.run_entry(log)` pins the exact five-field schema
(`RUN_ENTRY_SCHEMA_FIELDS`) and is the only sanctioned way to build the
payload now — `DAILY_PROCEDURE.md` Stage 6 calls it directly rather than
describing the shape in prose. Duck-typed to accept either a
`runlog.RunLog` (via `.manifest()`) or a plain manifest `dict`, matching
the same import-cycle-avoidance pattern `to_washsale_trades` already
uses for `washsale`.

`test_run_entry_round_trips_through_the_journal_into_find_optimizations`
is the test that matters most here: it builds real `run_entry` payloads
into journal entries, folds them, and feeds the result straight into
`runlog.find_optimizations`, proving the schema and the reader actually
agree — not just that both exist.

`ledger.py`/`test_ledger.py`: 84 → 88 tests. Full suite: 420 → 424.

## 12. Open and unverified

- **`research.py` has now been live-checked feed by feed for every channel
  it calls, but never run end to end inside a real scheduled brief.** As
  of 4 September 2026, every one of: `NEWS_SENTIMENT`, Robinhood
  `get_equity_news`, `CONGRESS_TRADES`, `INSIDER_TRANSACTIONS` (both the
  full-data and preview-envelope paths), all nine `MACRO_CHANNELS`
  (`CPI` on its `datatype=csv` default, the other eight on
  `datatype=json`), all eleven `COMMODITY_CHANNELS` (`GOLD_SILVER_SPOT`'s
  scalar-quote shape separately from the other ten series channels),
  `EARNINGS_CALENDAR` (both an empty and a populated response),
  `EARNINGS_ESTIMATES`, `HISTORICAL_PUT_CALL_RATIO`,
  `REALTIME_PUT_CALL_RATIO`, `MARKET_STATUS`, and `TOP_GAINERS_LOSERS`
  have each been called live at least once and their parsers rewritten
  against the real response — a double-digit count of wrong field names,
  wrong response shapes, and unhandled payload cases were found and fixed
  across the two 4 September entries above, out of roughly fifteen feeds
  checked. `IPO_CALENDAR` was checked, confirmed structurally unable to
  satisfy Rule 1, and removed rather than kept as dead code. **Still not
  independently live-verified**: `EARNINGS_CALL_TRANSCRIPT` and Robinhood
  `get_sec_filing`/`get_sec_filing_facts` — that defect rate is reason
  enough not to assume these three are fine because they resemble an
  already-fixed family; verify them the same way before relying on them.
  And even the verified feeds have never all run together inside one real
  `DAILY_PROCEDURE.md`
  execution — first real contact is the next scheduled trading day.
- Whether the connector broker refreshes the brokerage token indefinitely
  without a fresh desktop-browser sign-in. The token expires roughly every four
  days; refresh is supported but unconfirmed for this server. If it lapses, runs
  will abort at the tool-visibility check until re-authorised.
- **The self-heal loop (section 8) has been exercised twice, not battle-tested.**
  The redesigned memory and evidence pipeline has reached every stage, on real
  order history, with both accounts reconciling to zero residuals across two
  consecutive sessions — see section 11. The watchdog's diagnose-fix-merge-retry
  path has caught one real Drive-connector bug and one real near-miss (a
  too-tight offset that nearly started a duplicate trading run, section 11),
  but has never yet had to complete a full diagnose-fix-merge-retry cycle
  against an actual `aborted` run. Watch it handle one before trusting it
  fully unattended.
- **A scheduled routine can stall indefinitely on a sandbox permission
  prompt with nobody present to answer it.** Observed once (31 August, an
  oversized `get_equity_orders` page), not observed since. Not understood well
  enough to call fixed — only that a human has to notice a stuck run manually
  if it recurs, since a hung session sends no email and looks, from outside,
  identical to "still running." Worth instrumenting a timeout check if it
  recurs.
- **`gap_risk_haircut` (0.25) and the concentration thresholds (0.5 ratio, 0.45
  eigen-share) are judgment calls, not measurements.** They should be revisited
  once enough real trading history exists to check them against actual
  overnight gap behaviour and actual portfolio correlation, respectively.
- **The pre-registered evidence claim** (0.50% edge, 6% assumed dispersion,
  decide-by 2028-02-28 — always defer to `state.json.evidence.pre_registration`
  for the live values) implies roughly 900 closed trades to settle at the
  observed trade rate. That is measured in years — see section 8's "Path to
  live trading" for why that is not, on its own, a reason to delay going live.
- **The fills/split-events cache (section 5, section 11's 4 September entry)
  has never run against a real Drive folder.** No `fills-cache-*.json` or
  `splits-cache-*.json` file exists yet, so the first real run after this
  change takes the cold-start path (`watermark=None`, full history fetched
  exactly as before) and should behave identically to today — the caching
  benefit only shows up starting the SECOND real run, once a cache file
  exists to fold. `test_fills_cache_round_trip_matches_a_direct_fetch`
  proves the reconstruction logic against the real 31 August order fixture,
  but nothing has proven the actual dated-file read/write cycle against a
  live Drive folder yet.
- **Monthly journal compaction has never been run, live or otherwise, by a
  human or by any procedure.** The logic is tested to exact equivalence
  against synthetic multi-file journals, but no `journal-monthly-*.json`
  file has ever actually been written to the real Drive folder, and since
  it is a deliberate manual/occasional operation (not part of the daily
  routine), it may not be for some time — see section 11's 4 September
  entry.

## 13. Standing honesty rules

These are not decoration; they are the reason to trust the output.

- No process makes market calls reliably correct. The system guarantees
  **process** — no fabricated numbers, sourced and timestamped figures, a
  confidence threshold, and "do nothing" as a first-class output — not
  **outcomes**.
- The evidence framework in section 11 exists because a process guarantee is
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
