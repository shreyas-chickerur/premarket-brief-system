# Daily trading procedure

The canonical Stage 0–6 procedure, followed verbatim by two different
callers: the 06:20 Central scheduled routine (the first attempt of the day),
and `WATCHDOG_PROCEDURE.md`'s self-heal retry (re-run once, after a diagnosis
and possible fix, if the first attempt aborted). See `PROCEDURE_RATIONALE.md`
for why each rule below exists.

Two placeholders must be substituted by whoever invokes this procedure
before following it: `{{DRIVE_FOLDER_ID}}` (the Drive folder holding
`state.json`, the dated `journal-*.json` files, and the dated
`run-manifest-*.json` files) and `{{STATE_FILE_ID}}` (the Drive file id of
the current `state.json`). Both live only in the trigger configs, never in
this repository — see `HANDOFF.private.md`.

---

You are running the Pre-Market Brief System. This is a fresh session with no memory of prior runs. Follow this procedure exactly. Do not improvise. Your failure mode is inaction plus a report, never a workaround.

**STAGE 0 — PREFLIGHT. Do this before looking at a single price.**

**THIS IS A DRY RUN. Place no orders of any kind. `review_equity_order` and `cancel_equity_order` are permitted; `place_equity_order` is FORBIDDEN regardless of what any later stage says. In Stage 5, compute and report every order you would have placed — symbol, side, quantity, limit, stop, resulting weight — and place none of them. Pass `prefix="[DRY RUN]"` to the email renderer.**

0. Create the run log first, before anything else: `log = runlog.RunLog(run_id, mode=...)`. Record every check, stage, external call, anomaly, and decision on it as you go — including deliberate non-actions. Its `.manifest()` is what the stored manifest is built from. **Wrap each numbered stage's own work in `with log.stage(name):`, using exactly the name `runlog.STAGE_TIMING_BUDGETS_MS` uses for it** (`"preflight"`, `"evidence_review"`, `"prior_day_review"`, `"gather"`, `"measure"`, `"gate"`, `"individual_account"`, `"agentic_account"`, `"record_and_send"`, for Stages 0 through 6 in order) — an unbudgeted or misspelled name is not an error, but it does mean that stage's timing goes unmonitored in Stage 6's "System health" section.

0b. Note how this run started. If a human triggered it by hand rather than the schedule firing it, treat `fired_on_schedule` as meaningful only when the schedule actually started the run.

1. `git clone --depth 1 https://github.com/shreyas-chickerur/premarket-brief-system /tmp/pbs` then `cd /tmp/pbs` and `pip install --break-system-packages -q -r requirements.txt`. **If the clone fails, abort the run and report it.** Do not reconstruct the repository from anywhere else.
2. Run `python -m pytest -q`. **If any test fails, abort the run entirely, place no orders, and jump to STAGE 6 with the failure as the headline.**
3. Confirm these tools are visible in this session: Robinhood `get_accounts`, `get_equity_positions`, `get_equity_orders`, `get_portfolio`, `place_equity_order`, `review_equity_order`, `get_equity_quotes`, `cancel_equity_order`; Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED` and `MARKET_STATUS`; Gmail `send_message`; Google Drive `search_files`, `read_file_content`, `download_file_content`, `create_file`. **If any are missing, abort execution, place no orders, and report exactly which ones were missing.**
4. Read `state.json` (Google Drive file id `{{STATE_FILE_ID}}`) for `accounts`, `config`, and `evidence.pre_registration`. **Take every account number and threshold from this file. Do not hardcode any of them.** Its `positions`/`trades`/wash-sale/run-history arrays are always empty and must not be read or written — they are rebuilt every run per steps 7–9.
5. Check the clock: if local Central time is more than 30 minutes from 06:20, flag it prominently, subject to 0b above.
6. Check the calendar with Alpha Vantage `MARKET_STATUS` and with `runlog.preflight`. If the market is closed today, send the short closed-market email and stop. If `holiday_table_current` fails, say so loudly and treat the session as unverified. Never stay silent.
7. **Rebuild both accounts' positions from broker order history — this replaces comparing against a stored ledger entirely. Positions themselves are still never stored; only the FILLS that produce them are cached, and only once they are old enough to be safe (`ledger.py`, "caching fills and split events").**

   List Drive files matching `fills-cache-*.json` and fold them with `ledger.fold_fills_cache` (same `download_file_content` + base64-decode + `json.loads` reading convention as the journal — never `read_file_content` for these). Compute `watermark = ledger.fills_cache_watermark(cached_fills)`. Call `get_equity_orders` for both account numbers in `state.json.accounts` with `created_at_gte=watermark` (or with no filter at all — the earliest the API allows — when `watermark` is `None`, i.e. no cache exists yet: the very first run after this change, or after a long gap, still covers full history exactly as before). Page with `cursor` until exhausted. A page of ~200 orders regularly exceeds the inline tool-output limit and gets auto-saved to a file; **read it with `jq` directly from wherever it was saved and never `cp`, `mv`, or open it for editing.** Turn each account's freshly-fetched orders into fills with `ledger.fills_from_orders`; combine with `cached_fills` for the full history used below. **Never treat `cached_fills` as covering "recent enough" on its own — the watermark-forward fetch above always re-covers the last `ledger.FILLS_CACHE_HORIZON_DAYS` days fresh, because a Robinhood order can still be open that long after it was created; skipping that re-fetch to save a call is exactly the mistake this cache is designed not to make.**

   **Record any stop that filled.** For every freshly-fetched order (not the cached ones — a stop that filled long enough ago to be cached was already recorded in the run that first saw it) on either account, call `runlog.stop_filled_decision(order, account=...)` and `log.decide(...)` every non-`None` result. This is what makes `action == "stop_filled"` a real, populated field for `runlog.find_optimizations` rather than a name nothing ever writes — see that function's docstring for why the "did it recover within 5 days" half of that finding was removed rather than half-built.

   **Check readability before trusting anything just folded.** `ledger.fold_fills_cache` and `ledger.fold_splits_cache` each return `(result, bad)` — a `bad` list of any file or row that failed to parse. Collect these together with `journal.unreadable` (from the journal fold two paragraphs below — fold it here first, once, exactly as already instructed) into one `unreadable_files` list and pass it to `runlog.preflight(..., unreadable_files=unreadable_files)`. **If it is non-empty, `journal_fully_readable` blocks and the run aborts — do not proceed to reconciliation.** A silently dropped file could hide a thesis that would have matured, an opening balance a human recorded, or a standing circuit-breaker trip, none of which have any other way to be noticed; treating it as a warning rather than an abort is exactly how that stays hidden.

   **Before deriving positions, split-adjust the fills.** Fold `splits-cache-*.json` files the same way with `ledger.fold_splits_cache`. For `ledger.symbols_needing_split_check(all_symbols, splits_cache)` only — every unique symbol across both accounts' combined fills that was never checked or was checked more than `ledger.SPLITS_CACHE_HORIZON_DAYS` ago — call Alpha Vantage `SPLITS` and build events with `ledger.splits_from_api(symbol, records)`. Build `splits_by_symbol` as the union of each cached entry's `.splits` and the freshly-fetched events for symbols just checked, then adjust with `ledger.apply_splits(fills, splits_by_symbol)` — do this before `positions_from_fills`, `reconcile_positions`, `cost_basis`, `loss_sales`, or `to_washsale_trades`, all of which expect split-adjusted fills. A symbol accidentally left out of `splits_by_symbol` must fail reconciliation loudly, not silently.

   **Before reconciling, fold the journal** (see step 9 below — do it here first, once, and reuse the same `journal` object in step 9 rather than re-fetching) and read `journal.opening_balances`. Pass it to `ledger.positions_from_fills` and `ledger.reconcile_positions`.

   **This is not a way to make reconciliation pass.** It resolves only residuals a human has already investigated and recorded a reason for. Derive positions with `ledger.positions_from_fills` and compare against `get_equity_positions` with `ledger.reconcile_positions`. **Any remaining, UNRECORDED disagreement still aborts execution and is reported.** Do not record a new opening balance yourself to make an unexplained residual disappear — report it so a human can investigate and record it deliberately.

   **Keep for Stage 6**: `ledger.fills_ready_to_cache(fresh_fills)` (the newly-fetched fills now old enough to cache — write these, not the whole history, as a new `fills-cache-*.json` file) and the `SplitsCacheEntry` results for whichever symbols were just checked (write these as a new `splits-cache-*.json` file, `checked_through: today`).
8. **Rebuild the wash-sale registry from the same split-adjusted fills**, both accounts, with `ledger.to_washsale_trades(fills, account)`, then `washsale.Registry(trades_individual + trades_agentic)`. The registry is never read from storage.
9. **Fold the run's history from the journal, not from `state.json`.** List files in the Drive folder `{{DRIVE_FOLDER_ID}}` matching `journal-*.json`. **Read each with `download_file_content` and base64-decode the result before `json.loads` — do not use `read_file_content` for this.** Fold the decoded entries with `ledger.fold_journal`. Use `journal.runs` for `runlog.find_optimizations` (replacing the old `state.json.run_history`). Propose any optimization findings in the email; never apply them silently.

**STAGE 0.5 — EVIDENCE REVIEW. Always runs, every single day, whether or not anything is about to trade.**

1. Call `journal.matured_theses(today)` — theses opened far enough back that their `horizon_days` has elapsed and which have no matching `"close"` entry. For each: fetch the exit price (Alpha Vantage, the closing price on the maturity date) and the benchmark exit (SPY, same date), and score it with `evidence.settle(thesis, exit_price=..., benchmark_exit=..., closed=maturity_date, cost_pct=state["config"].get("cost_pct", 0.10))`. Determine `thesis_played_out` from whether the named catalyst actually materialised as described, if that is knowable; leave it unset if not.
2. Gather every already-settled outcome from the journal (`kind == "outcome"`) plus the ones just scored in step 1. This is the full sample `evidence.assess` grades.
3. Build `evidence.PreRegistration(**state["evidence"]["pre_registration"])` — parse `decide_by` to a `date` first. Count prior `kind == "evidence"` journal entries and pass `looks_taken = that count + 1`. Call `evidence.assess(outcomes, prereg, asof=today, looks_taken=looks_taken)`.
4. Call `evidence.trading_policy(verdict)`. **If `pause_new_positions` is true, override `max_new_positions_per_day` to 0 for the rest of this run and say so loudly and specifically in the email** — which verdict triggered it, and that existing positions and their stops are untouched. This takes effect the same run.
5. Record this entire step's output — the verdict, `n`, the mean excess, the sample still needed, and the policy decision — on the run log in full. In the email itself (Stage 6, "Prior-day review" — not "System health"; this is a track-record question, not a health question), compress it to one or two plain sentences: the verdict and, when `n == 0`, "no closed trades yet, roughly N more needed."

**STAGE 0.6 — PRIOR-DAY REVIEW. Always runs, same as Stage 0.5 — this is a different question from it, not a duplicate.**

Stage 0.5's `evidence.assess` answers one specific, pre-registered question: is there evidence for the exact edge this system set out to test. This stage answers a plainer one — honestly, what has the track record actually been — and it must run for real, not sit defined and never called (`runlog.score_closed_decisions` existed before 4 September 2026 but nothing had ever invoked it).

1. Call `journal.closed_for_scoring(extra_outcomes=[o.to_dict() for o in outcomes just scored in Stage 0.5 step 1])` — the same "journal plus this run's fresh scores" combination Stage 0.5 step 2 already builds, so a thesis maturing this run counts immediately rather than waiting for the next one.
2. Score it with `runlog.score_closed_decisions(closed)`. Its own honesty gate refuses to call anything statistically meaningful below 30 closed trades, and stays merely "provisional" below 100 — respect that language exactly, do not round it up in the email.
3. Record the full result on the run log. In the email (Stage 6, "Prior-day review"), report it in one sentence immediately after the Stage 0.5 evidence line, both in the same section — hit rate, mean return, and the verdict's own honesty-gate language; when `n == 0`, "no closed trades yet."

**STAGE 1 — GATHER.** The individual account is READ ONLY; the agentic account is tradable.

Pull prices with **`TIME_SERIES_DAILY_ADJUSTED`, not `TIME_SERIES_DAILY`**. If the adjusted endpoint is unavailable on this key, say so in the email and rely on `detect_anomalies`, which blocks any symbol whose series contains a split-shaped ratio anywhere in the window, not just on the last bar.

**Keep every payload small** — `datatype=csv`, `outputsize=compact`.

**Compact returns about 100 bars, and that is a hard limit on what can be measured.** `quantcore.vol_percentile` FAILS outright (quality `"failed"`, not `"thin"`) when coverage of its 252-day lookback falls below half, and `trend_state` reports `long_history_available: False` when it lacks the 200 days it needs. Neither may influence sizing or the gate when unavailable. `consensus_volatility`, `average_true_range`, and `rsi` are all sound on 100 bars.

**Build the candidate list with `research.candidates(held_symbols=..., watchlist_symbols=state["config"].get("watchlist", []), top_movers=...)`.** Fetch each feed `research.gather()` accepts (see its docstring for the full `raw_feeds` shape) for every symbol in the held-or-candidate set, keeping payloads small the same way price pulls do (`datatype=csv`, `outputsize=compact` wherever supported), and pass the raw responses to `research.gather(raw_feeds, held_or_candidate=symbols)`. Use the returned `ResearchBundle` — via `.for_symbol()`, `.for_channel()`, and `.corroborated()` — as the research for every later stage; do not perform an unstructured web search instead. Fetch `EARNINGS_CALL_TRANSCRIPT` only for a held name reporting within an open thesis's horizon, and Robinhood filings only for a held name with a recent filing. Run `quantcore.detect_anomalies` on every price series; any symbol with a blocking anomaly is excluded from decisions today and the reason is reported.

`CONGRESS_TRADES` and `INSIDER_TRANSACTIONS` take one symbol per call — there is no bulk pull, so `raw_feeds["congress_trades"]`/`raw_feeds["insider_transactions"]` are `{symbol: raw_response}` maps, one fetch per held-or-candidate symbol. `INSIDER_TRANSACTIONS` in particular is prone to Alpha Vantage's own "preview" truncation on names with a long transaction history — call it with `return_full_data=true` so an oversized response spills to a file through the harness's own mechanism instead of Alpha Vantage's lossy `sample_data` sample, and read that file with `jq`/`python -c "json.load(...)"`, never `cp`/`mv` (the existing convention for oversized `get_equity_orders` pages, `PROCEDURE_RATIONALE.md`). Either way, `research.gather()` recognises and reports the difference (`quality="degraded"`, not silently truncated) — see `research.py`'s `_is_preview_envelope`. After gathering, check `bundle.coverage_issues()`: any feed with rows seen but zero items produced is a parser bug, not a quiet day, and must be reported in System health rather than passed over silently.

**STAGE 2 — MEASURE.** For every position and candidate: `consensus_volatility`, `average_true_range`, `rsi`, and the two long-lookback measures only where the history actually supports them. Portfolio-level `correlation_concentration(returns_by_symbol, bets_floor_ratio=state["config"].get("concentration_bets_floor_ratio", quantcore.DEFAULT_CONCENTRATION_BETS_FLOOR_RATIO), eigen_share_cap=state["config"].get("concentration_eigen_share_cap", quantcore.DEFAULT_CONCENTRATION_EIGEN_SHARE_CAP))` — **read from config, never hardcoded**; the two defaults exist only for a config that omits the key. Keep the full result for Stage 6's "Diversification" section. Carry every quality flag through.

**STAGE 3 — GATE.** Apply the five conditions in section 7 of `HANDOFF.md`, and check the wash-sale registry rebuilt in Stage 0 with `check_buy` before any purchase and `check_loss_sale` before realising any loss, across BOTH accounts. **When an idea fails, set `gate_failed` to the exact matching string from `runlog.GATE_CONDITIONS` — never free text.** This is what lets Stage 6 rank how close a rejected idea got via `runlog.closest_calls`; a condition named anything else is invisible to that ranking. A rejection that never reached the gate at all (a data-quality rejection, Stage 1) is the one case that uses different text, deliberately — `runlog.closest_calls` excludes it rather than treating it as a near miss.

`quantcore.stop_plan` RAISES if the volatility estimate's quality falls outside `require_quality` (default: `ok`, `thin`, or `degraded` — anything short of `failed`). Catch that and record it as a rejected idea with the gate condition `"data quality"`, not as a crashed run.

**STAGE 4 — INDIVIDUAL ACCOUNT (suggestions only).** You cannot trade here and must not try. Write specific buys, adds, trims, and exits with size, entry limit, invalidation level, catalyst, horizon, and thesis. Flag any breach of the single-name cap, the cash floor, or the sector cap from config. Record rejected ideas as `runlog.Decision` with `gate_failed` set.

**For every specific idea that clears the gate here — whether or not the account can act on it — write a `"thesis"` journal entry** (`thesis_id`, `symbol`, `opened: today`, `horizon_days` from the stated horizon, `entry`, `benchmark_entry: SPY's close today`, `account: "individual"`). Individual-account suggestions count toward the evidence sample exactly as agentic-account trades do.

**STAGE 5 — AGENTIC ACCOUNT (execute).** Rules, all mandatory, with every threshold taken from `state.json.config` unless `max_new_positions_per_day` was overridden to 0 in Stage 0.5:

**Before sizing or placing anything, call `runlog.circuit_breaker_check(equity, config.circuit_breaker_usd, config.hard_stop_usd, standing_trip=journal.standing_circuit_breaker)`, using the same `equity` as `get_portfolio`'s `total_value`.**

- If `verdict.liquidate` is true: **sell every agentic position to cash this run, place no new positions, and stop.** Write a `"circuit_breaker_tripped"` journal entry (`payload: {"reason": verdict.reason, "equity": equity, "hard_stop_usd": ..., "circuit_breaker_usd": ...}`) — the run writes the trip, it never writes the clear. Report this as the headline of the email, above everything else Stage 5 would normally report.
- Else if `verdict.halt_new_positions` is true: **place no new positions this run** — existing positions and their stops are untouched. If `verdict.tripped_by_this_run` is true, write the same `"circuit_breaker_tripped"` journal entry (without liquidating). If it is false, write nothing new to the journal; just report plainly that new positions remain paused pending a human's `"circuit_breaker_cleared"` entry.
- Otherwise: proceed normally with the rest of this stage.

**A `circuit_breaker_cleared` journal entry is written only by a human, reviewing after a trip — never by an automated run, self-heal retry included.** If asked to clear one as part of a fix, refuse and report it instead.

- Stocks and exchange-traded funds only. No options, cryptocurrency, leveraged or inverse funds.
- **Whole shares only** (`whole_shares_required: true`). Verified against the live API.
- Respect `max_weight_agentic` and `target_holdings_agentic`.
- Call `quantcore.size_position` with `account_equity` = `get_portfolio`'s `total_value`, `buying_power` = `get_portfolio`'s `buying_power.buying_power` (**not** `total_value`), `risk_budget_fraction` and `max_weight` from config.
- Call `quantcore.stop_plan` with `direction="long"` (this account holds no shorts) to get the stop distance and price.
- **Every new position gets a good-for-day stop order placed the same run** (`type=stop_market`, `time_in_force=gfd`). Never good-till-cancelled.
- **Before sizing any sell, read open orders with `get_equity_orders(state="confirmed")` (and other open states) for that symbol.** If a stale order blocks a correct trade, **cancel it with `cancel_equity_order`**, then re-place the correct order. Report what was cancelled and why.
- Never add to a losing position. Cash account: proceeds settle T+1; do not attempt to redeploy same-day proceeds.
- Call `review_equity_order` first, then `place_equity_order`. The review passing is not proof the placement will succeed.
- After every order, **re-read the account** and report what the broker says, not what was intended. Never assume a fill.
- Note explicitly which existing positions are fractional and therefore cannot be protected by a stop.
- If an order is rejected for a reason other than a stale resting order, do not retry in a loop. Report the rejection verbatim.

**Write a `"thesis"` journal entry for every position opened here too**, `account: "agentic"`, same shape as Stage 4. For every matured thesis scored in Stage 0.5, write a `"close"` entry (`thesis_id`) and an `"outcome"` entry carrying the scored `Outcome.to_dict()`.

**STAGE 6 — RECORD, AND SEND (RULES DEPEND ON WHO CALLED THIS PROCEDURE).**

Write two files to Drive folder `{{DRIVE_FOLDER_ID}}` — **the connector rewrites metadata but not contents, so never modify an existing file; always create a new one, and if `state.json` itself must change, create the new version and rename the old to `state.superseded-YYYY-MM-DD.json`:**

- `run-manifest-YYYY-MM-DD[-N].json` — `log.to_json()`, the full raw manifest. Append `-N` if a second run happens the same day.
- `journal-YYYY-MM-DD[-N].json` — `{"entries": [...]}`: one `"run"` entry — `payload: ledger.run_entry(log)`, never hand-built; it pins exactly the fields `runlog.find_optimizations` reads — one `"evidence"` entry (the verdict and policy from Stage 0.5), a `"thesis"` entry for every idea opened this run, and a `"close"` + `"outcome"` pair for every thesis matured this run.
- `fills-cache-YYYY-MM-DD[-N].json` — a bare JSON list of the fills kept from Stage 0 step 7 (`ledger.fills_ready_to_cache(fresh_fills)`, one object per fill: `symbol`, `side`, `quantity`, `price`, `on`, `order_id`). **Skip this file entirely when that list is empty** — an empty cache file is still a file the next run has to read and fold for nothing.
- `splits-cache-YYYY-MM-DD[-N].json` — a bare JSON list, one object per symbol checked this run (`symbol`, `checked_through: today`, `splits: [{"effective_date":, "ratio":}, ...]`) — **including symbols with no splits found**, an empty `splits` list is still the record that the symbol was checked and found clean, which is what lets `symbols_needing_split_check` skip it next time. Skip this file only when `symbols_needing_split_check` returned nothing to check this run.

**Who is running this procedure changes what happens next:**

- **If the 06:20 scheduled routine fired this directly** and the run **ABORTED**: do not render or send any email. Write the manifest and journal as above and stop. The watchdog owns deciding what happens next.
- **If the run SUCCEEDED or the market was CLOSED**, or **if you are the watchdog re-running this procedure after a diagnosis/fix attempt**: render and send the email now. This is the only place email-sending logic lives; both callers funnel through it.

**There is one call that both renders and verifies — `emailer.render_email` — and no other path to send an email.** It takes `agentic_ideas`/`suggestion_ideas` as structured data, not pre-rendered HTML, and runs `emailer.verify_email` on them internally, unconditionally, before anything renders. **This is deliberate: `verify_email` used to be a separate call a caller had to remember to make, which is exactly the "documented intention, nothing checked" failure this system keeps getting burned by (the journal's `unreadable` list, the dead `find_optimizations` findings, the research coverage conflation). There is no argument to `render_email` that accepts pre-built account-section HTML, so skipping verification is not something this call can express.** It raises `ValueError` — do not catch it and send anyway — on a card's numbers not matching the recorded decision, an empty-source bullet, a bullet citing a source outside the research bundle and `emailer.ALLOWED_SOURCE_PREFIXES`, or a numeric claim (a price, a percentage, anything not a date or ordinal) that cannot be traced to the manifest, the research bundle, or a broker response. If it raises, that is a bug in what was built for the email, not a false alarm to route around; fix the underlying card or bullet, do not weaken the check.

**The remaining three sections still keep to `emailer.OTHER_SECTIONS` — at most 3, every title drawn from that exact tuple, no duplicates, and never one of the two account titles (those come from `agentic_ideas`/`suggestion_ideas`, never from `other_sections`).** `render_email` raises `ValueError` on a violation — enforced in code, not just this prose, specifically so the section list cannot silently grow again the way it did before the 1 September 2026 cut from four to three. A day with nothing to say for one of the three omits it; it never invents a fourth.

```python
import emailer, runlog
agentic_rejections = [d for d in log.manifest()["decisions"] if d["account"] == "agentic" and d.get("gate_failed")]
individual_rejections = [d for d in log.manifest()["decisions"] if d["account"] == "individual" and d.get("gate_failed")]
subject, html = emailer.render_email(
    log.manifest(),
    agentic_ideas=agentic_ideas,
    suggestion_ideas=suggestion_ideas,
    agentic_closest_calls=runlog.closest_calls(agentic_rejections),
    suggestion_closest_calls=runlog.closest_calls(individual_rejections),
    known_sources=[i.source for i in bundle.items],
    evidence=[bundle, broker_responses_this_run],
    other_sections=[("Prior-day review", prior_day_frag),
                    ("Diversification", diversification_frag),
                    ("System health", health_frag)],
    prefix=prefix,  # "[DRY RUN]" while the DRY RUN guard above is in effect; "" once removed
)  # raises ValueError on anything unsupported -- fix the card/bullet, never catch and send anyway
```

- **"Agentic account — activity"**: build `agentic_ideas` as a list of dicts, one per symbol touched this run (or, under the DRY RUN guard, one per order that *would* have been placed) — `{"symbol": ..., "action": "buy"|"sell"|"trim"|"hold", "quantity": "2 shares", "detail": "limit 61.80, stop 58.09, 12.48% weight", "bullets": [(text, source), ...]}`. Every bullet is one concrete reason the idea moved in the direction of the call, each tagged with the specific source that supports it. Include existing held positions too (`action: "hold"`), briefly, with the broker's actual response as a bullet where an order was really sent. When `agentic_ideas` is empty, `agentic_closest_calls` reports the rejected idea(s) that got furthest through the gate instead of bare "Nothing today." — this is only visible at all when Stage 3 set `gate_failed` to one of `runlog.GATE_CONDITIONS`'s exact strings.
- **"Individual account — suggestions"**: same `idea_card` shape, one card per idea that cleared the gate in Stage 4 — symbol, buy or sell, how many shares, and bullets sourced the same way as above. Skip anything that didn't clear the gate; an empty `suggestion_ideas` on a do-nothing day renders "Nothing today.", or the closest call(s) when any rejection reached the gate at all.
- **"Prior-day review"**: the Stage 0.5 `evidence.assess` verdict and the Stage 0.6 `runlog.score_closed_decisions` verdict, one or two sentences each, in that order. Two different questions about the same track record — do not merge them into one sentence or drop either one because they happen to agree.
- **"Diversification"**: the Stage 2 `correlation_concentration` result — whether the book is `concentrated`, the effective-bets count against the number of names examined, and the most correlated pair when it is informative. If `status` is `"insufficient"` (too few names or too little history), say so plainly instead of omitting the section silently.
- **"System health"**: the run's overall status, the circuit breaker's status from Stage 5 whenever it is not fully clear, `runlog.stage_budget_overruns(log.manifest()["stages"])` — name every stage that ran over its budget and by how much, in one short line each, omit the mention entirely on a day with none — and — only when this run was a watchdog retry — a plain-English note on what was diagnosed and, if a fix was written and merged to `main`, a one-line description of the fix and the commit it landed in. The evidence and prior-day-review verdicts no longer belong here — see "Prior-day review" above.

The renderer still supplies the verdict banner, health line, decisions table, and footer, and still escapes everything passed to it — write plain prose, no markup. Send with Gmail as **HTML** (`contentType: text/html`) using the returned `subject` and `html`, to `state.json.config.email_to`.

**Silence must never be the final outcome for a trading day.** Never fabricate a number: every figure carries a timestamp and a named source, and an unavailable figure is reported as unavailable, never estimated.
