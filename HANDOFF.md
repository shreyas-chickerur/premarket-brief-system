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
| **The connector exposes no cancel-order tool** | Tool inventory. Orders can be placed and read, not cancelled. |
| **Cash account, T+1 settlement** | Sale proceeds unusable until the next trading day. Fine at a once-daily cadence. |
| **Pattern day trader rule no longer exists** | Eliminated by the regulator effective 4 June 2026. Not a constraint. |
| **GitHub REST API blocked; `git clone` works** | Tested. The daily code pull is free; pushing requires the repo be attached to the session. |
| **Container has no direct internet except the package index** | Tested. All data arrives via connectors or WebFetch. |
| **Some market-data endpoints return enormous payloads** | One treasury endpoint returned 72,407 characters and blew the tool output limit. Constrain every pull: `datatype=csv`, `outputsize=compact`. |

### The consequences these force

- **Whole shares only in the agentic account.** Any name priced above the position
  cap is untradable, however good the idea.
- **Stops are good-for-day, re-derived every morning.** This turns the missing
  cancel tool into a non-issue — the order expires by itself — and makes stops
  adapt to changing volatility. It costs nothing real, because a resting overnight
  stop was never protecting anything outside regular hours anyway.
- **Pre-existing fractional positions cannot be stopped.** The plan is to keep the
  cash-equivalent holdings as a yield-bearing reserve and close the one carrying
  real directional risk, since it is unstoppable. That sale realises a loss and
  opens the first 31-day wash-sale block, including proxy warnings on
  substantially identical funds.

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
| State | private Google Drive folder | Account numbers, config, journal, registry, manifests |

`state.json` is the single source of truth for everything private. The folder
and file identifiers are in `HANDOFF.private.md`.

Two properties of the Drive connector shape how state is written:

- **It can rewrite a file's metadata but not its contents.** Updating
  `state.json` therefore means creating the new version and renaming the old to
  `state.superseded-YYYY-MM-DD.json`. Run history accumulates as dated
  `run-manifest-YYYY-MM-DD.json` files rather than by appending to one document.
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
| `quantcore.py` | Five volatility estimators, ATR, GARCH, RSI, trend, volatility percentile, Ledoit-Wolf concentration, stop derivation, risk-parity sizing, eight anomaly classes |
| `runlog.py` | Run manifests, staged timing, preflight self-audit, verified market calendar, regression review, optimization proposals, honest scoring |
| `washsale.py` | Cross-account registry, both directions, proxy warnings |
| `emailer.py` | HTML brief rendering, failure diagnosis, subject lines |
| `pipeline_demo.py` | End-to-end demonstration run |
| `make_fixtures.py` | Regenerates the deterministic synthetic fixtures the demo falls back to |
| `test_quantcore.py` | 56 tests, including known-answer volatility and correlation recovery |
| `test_runlog.py` | 45 tests, including daylight-saving drift and market-calendar integrity |
| `test_washsale.py` | 32 tests, including the live overlapping-holding case |
| `test_emailer.py` | 26 tests, including escaping and no-research-on-abort |

Key API surface:

```python
import quantcore as q, runlog as R, washsale as W

vol, parts = q.consensus_volatility(df, window=60)   # Estimate + per-method dict
atr  = q.average_true_range(df)
plan = q.stop_plan(entry, vol, atr)                  # StopPlan
size = q.size_position(equity, entry, plan,
                       risk_budget_fraction=...,
                       max_weight=...,
                       require_whole_shares=True)    # SizePlan
anoms = q.detect_anomalies(symbol, df, asof=today)   # list[Anomaly]
if q.blocking(anoms): ...                            # exclude this symbol

log = R.RunLog(run_id, mode="live")
R.preflight(log, available_tools=..., required_tools=...,
            local_time=..., expected_local_hhmm=(6, 20),
            self_test=..., history=...,
            ledger_positions=..., broker_positions=...)
if not log.may_trade: ...                            # abort path

reg = W.Registry(trades)
v  = reg.check_buy("XYZ", today)                     # Verdict
v2 = reg.check_loss_sale("XYZ", today)               # the reverse direction
```

**Every estimate carries `.value`, `.n_obs`, and `.quality`** — `ok`, `thin`,
`degraded`, or `failed`. A failed estimate propagates as a refusal, never a default.

### Running it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # 159 tests
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

A completed run keeps the full brief: health line, the research narrative
sections, then the decisions table with the failing gate named on every
rejected idea.

## 10. Remaining work

1. ~~Push the repository.~~ Done.
2. ~~Verify the market holiday table against the published exchange calendar.~~
   Done — one wrong date found and fixed, regression tests added, expiry guard
   added.
3. ~~Make `pipeline_demo.py` runnable on a fresh clone.~~ Done via `fixtures/`.
4. ~~Create the scheduled task.~~ Done — it runs weekdays at 06:20 Central in
   **dry-run mode**, where order placement is forbidden outright rather than
   merely declined by a later stage.
5. **Prove the dry run end to end.** The first fire confirmed the clone, the
   dependency install, and all 122 tests passing in the cloud environment. What
   still needs proving on a weekday, when the market is open, is the brokerage
   read path, the market-data pulls, and email delivery.
6. **Attach the brokerage connector to the scheduled run.** Until it is visible,
   preflight aborts by design and no research happens. See section 11.
7. **Only then, switch to live.** Replace the dry-run prompt with the standard
   one from `TRIGGER_PROMPT.md`. This is a deliberate, reversible decision and
   should be made by a person, not inherited by a routine.
8. **First live run:** close the unstoppable directional position, and act on the
   concentration and cash-floor breaches if and only if they clear the gate.
9. **Extend the holiday table** before `HOLIDAY_TABLE_HORIZON`.

## 10b. What the first live dry run found

The 31 August dry run was the first to reach real market data. It placed nothing
and surfaced four defects, which is what the exercise was for.

**Fixed:**

- **Unadjusted prices made volatility meaningless.** `TIME_SERIES_DAILY` returns
  raw prices, so CRWD's 4-for-1 split read as 293% volatility. Worse,
  `detect_anomalies` only ever tested the FINAL bar, so a split weeks back left
  today's bar ordinary while corrupting every estimate over the window. The
  scan now covers the whole series, uses a median-absolute-deviation scale so a
  split cannot hide inside the volatility it creates, and **blocks** a symbol
  whose move matches a split ratio. Stage 1 now pulls the adjusted endpoint.
- **Concentration was understated.** `correlation_concentration` shrank
  covariance toward one average variance, which is wrong for every asset at once
  when a 1%-vol Treasury fund sits beside a 105%-vol semiconductor. On a
  known-answer book at a true 0.55 correlation it reported 0.28, claimed 2.65
  effective bets against a true 1.75, and called the book unconcentrated.
  Returns are now standardised before shrinking, and the verdict uses the less
  flattering of the shrunk and sample views — a risk measure may err toward
  caution, never toward "you have more independent bets than you do".
- **The wash-sale registry was empty while the broker showed two real loss
  sales.** It is now rebuilt from broker history every run, with the stored copy
  treated as a cache to cross-check rather than a source of truth.
- **`vol_percentile` and `trend_state` were degraded for all 23 symbols.**
  Compact payloads return ~100 bars; they need 252 and 200. They are now
  reported as unavailable rather than as a weak estimate, because a percentile
  computed over a third of its window is a different statistic wearing the same
  name.

**Still open — a real operational one:** a stale good-for-day stop from an
earlier session holds 1 share of SGOV, so the correct 4-share sell is rejected
with `EQUITY_MAX_SELL_SHARES_EXCEEDED`, and there is no cancel tool. See
section 11.

## 11. Open and unverified

- **A stale good-for-day stop is holding shares hostage.** A leftover stop from
  an earlier session reserves 1 share of SGOV, so a correctly-sized 4-share sell
  is rejected outright rather than partially filled. There is no cancel tool, so
  the only remedies are to wait for it to expire, place the smaller order that
  fits, or cancel it by hand in the broker's own app. Worth watching: if
  good-for-day stops are outliving their day, the whole "stops expire by
  themselves" design needs revisiting.
- Whether the connector broker refreshes the brokerage token indefinitely without
  a fresh desktop-browser sign-in. The token expires roughly every four days;
  refresh is supported but unconfirmed for this server.
- ~~Whether a scheduled cloud run reliably sees the brokerage connector.~~
  **Resolved.** A routine sees only the connectors attached to its own
  configuration, not those connected to the account. With all four attached, the
  31 August run saw all ten required tools. Originally observed as: The market-data, mail,
  and storage connectors resolved; the brokerage connector was not present in
  the run's tool set. Preflight caught it and refused to proceed, which is the
  designed behaviour, but it means the routine cannot do useful work until the
  connector is attached to the routine itself rather than only to the
  interactive account. This is the top open item.

## 12. Standing honesty rules

These are not decoration; they are the reason to trust the output.

- No process makes market calls reliably correct. The system guarantees
  **process** — no fabricated numbers, sourced and timestamped figures, a
  confidence threshold, and "do nothing" as a first-class output — not
  **outcomes**.
- At a two-position daily cap the agentic account produces perhaps 20–40 closed
  trades a quarter. That is far too few to separate skill from luck.
  `runlog.score_closed_decisions` refuses to certify a sample under 30 and stays
  provisional under 100. Post-mortems and rule corrections, never parameter
  tuning on a dozen data points.
- If the system ever reports that a strategy is working based on a handful of
  trades, that is overfitting and should be called out.
