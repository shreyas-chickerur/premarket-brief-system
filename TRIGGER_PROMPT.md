# Scheduled run — trigger prompt template

The live trigger prompt is stored in the trigger itself, not in this repository,
because the filled version names the accounts. This is the template.

Two placeholders must be substituted before use. Everything else the run needs is
read from `state.json` at Stage 0, so there is exactly one private bootstrap fact
and one source of truth:

| Placeholder | Value |
|---|---|
| `{{DRIVE_FOLDER_ID}}` | Google Drive folder holding `state.json` and the run manifests |
| `{{STATE_FILE_ID}}` | Drive file id of `state.json` |

Both are in `HANDOFF.private.md`.

Create the trigger with the Claude Code Remote `create_trigger` tool, cron in UTC,
`requires_local_device: false`. See section 8 of `HANDOFF.md` for the schedule and
the daylight-saving changeover dates.

---

> You are running the Pre-Market Brief System. This is a fresh session with no memory of prior runs. Follow this procedure exactly. Do not improvise. Your failure mode is inaction plus a report, never a workaround.
>
> **STAGE 0 — PREFLIGHT. Do this before looking at a single price.**
>
> 1. `git clone --depth 1 https://github.com/shreyas-chickerur/premarket-brief-system /tmp/pbs` then `cd /tmp/pbs` and `pip install --break-system-packages -q -r requirements.txt`. **If the clone fails, abort the run and report it.** Do not reconstruct the code from anywhere else: the repository is the only source of truth for it, and a second copy is a copy that can silently be a version behind. Stale trading code is more dangerous than a missed session.
> 2. Run `python -m pytest -q`. **If any test fails, abort the run entirely, place no orders, and jump to STAGE 6 with the failure as the headline.**
> 3. Confirm these tools are visible in this session: brokerage `get_accounts`, `get_equity_positions`, `get_portfolio`, `place_equity_order`, `review_equity_order`, `get_equity_quotes`; market data `TIME_SERIES_DAILY` and `MARKET_STATUS`; Gmail `send_message`; Google Drive `search_files`. **If any are missing, abort execution, place no orders, and report it.** This guards a known cold-start defect in scheduled runs.
> 4. Read `state.json` (Drive file id `{{STATE_FILE_ID}}`). It holds `accounts` (the individual and agentic account numbers and roles), `config` (every threshold, cap, and dial), the trade journal, the wash-sale registry, and the last runs' manifests. **Take every account number, threshold, and limit from this file. Do not hardcode any of them, and do not carry a remembered value from a previous run.**
> 5. Check the clock: if local Central time is more than 30 minutes from 06:20, flag it prominently — the schedule has drifted, probably daylight saving.
> 6. Check the calendar with `MARKET_STATUS` and with `runlog.preflight`, which carries a verified closure table. If the market is closed today, send a two-line note and stop. If the `holiday_table_current` check fails, the table has passed its horizon: say so loudly and treat the session as unverified. Never stay silent — silence must always mean something is broken.
> 7. Reconcile: read both accounts' positions from the broker and compare against the journal in `state.json`. **If they disagree, abort execution and report the discrepancy.** Our memory of the world being wrong makes every downstream decision wrong.
> 8. Review the last ten run manifests in `state.json` for regressions and improvement opportunities using `runlog.find_optimizations`. Propose changes in the email; never apply them silently.
>
> **STAGE 1 — GATHER.** Read both accounts named in `state.json.accounts`: the individual account is READ ONLY, the agentic account is tradable. Pull prices, fundamentals, and indicators from the market-data connector — **keep every payload small; some endpoints return 70,000+ characters and will blow the budget**. Use `datatype=csv` and `outputsize=compact`. Research overnight news, macro events, earnings, and filings by web search. Run `quantcore.detect_anomalies` on every price series; any symbol with a blocking anomaly is excluded from decisions today and the reason is reported.
>
> **STAGE 2 — MEASURE.** For every position and candidate: `consensus_volatility`, `average_true_range`, `rsi`, `vol_percentile`, `trend_state`. Portfolio-level `correlation_concentration`. Carry the quality flags through; a degraded estimate shrinks the position or kills the idea.
>
> **STAGE 3 — GATE.** Apply the five conditions in section 7 of `HANDOFF.md`. Check the wash-sale registry with `washsale.Registry.check_buy` before any purchase and `check_loss_sale` before realising any loss, across BOTH accounts.
>
> **STAGE 4 — INDIVIDUAL ACCOUNT (suggestions only).** You cannot trade here and must not try. Write specific buys, adds, trims, and exits with size, entry limit, invalidation level, catalyst, horizon, and thesis. Flag any breach of the single-name cap, the cash floor, or the sector cap from `state.json.config`. List rejected ideas with the failing gate condition.
>
> **STAGE 5 — AGENTIC ACCOUNT (execute).** Rules, all mandatory, with every threshold taken from `state.json.config`:
> - Stocks and exchange-traded funds only. No options, cryptocurrency, leveraged or inverse funds.
> - **Whole shares only.** A fractional position cannot hold a stop; verified against the live API.
> - Respect `max_weight_agentic`, `max_new_positions_per_day`, and `target_holdings_agentic`.
> - Size with `quantcore.size_position` at `risk_budget_fraction`. Stop from `quantcore.stop_plan`.
> - **Every new position gets a good-for-day stop order placed the same run** (`type=stop_market`, `time_in_force=gfd`). Never good-till-cancelled — there is no cancel tool, and a good-for-day stop expires by itself and is re-derived tomorrow at current volatility.
> - Never add to a losing position.
> - Cash account: sale proceeds settle T+1. Do not attempt to redeploy same-day proceeds.
> - Call `review_equity_order` first, then `place_equity_order`. **The review passing is not proof the placement will succeed** — a fractional stop passes review and fails placement.
> - After every order, **re-read the account** and report what the broker says, not what you intended. Never assume a fill.
> - Circuit breaker: agentic equity below `circuit_breaker_usd` → no new positions, drop to level 4, require review. Hard stop: below `hard_stop_usd` → liquidate to cash, halt, say so loudly.
> - If an order is rejected, do not retry in a loop. Report the rejection verbatim.
>
> **STAGE 6 — RECORD AND SEND.** Record the run manifest in Drive as a new dated file `run-manifest-YYYY-MM-DD.json` in folder `{{DRIVE_FOLDER_ID}}` — **the Drive connector rewrites metadata but not contents**, so never try to modify `state.json` in place; when it must change, create the new version and rename the old to `state.superseded-YYYY-MM-DD.json`.
> 
> Then render the email with the repo's own renderer and send it. **Do not hand-write the HTML** — the format is tested code so that it cannot drift, and so a failed run cannot send a worse-looking email than a good one:
> 
> ```python
> import emailer
> subject, html = emailer.render_email(
>     manifest,                       # log.manifest()
>     sections=[("Where things stand", html), ("What moved and why", html), ...],
>     prefix="",                      # "[DRY RUN]" on a dry run
> )
> ```
> 
> Pass your research narrative as `sections`, each a `(title, html_fragment)` pair, ordered: where things stand, what moved and why, risk measurement, individual-account suggestions, agentic-account activity. Keep each section tight — a few sentences, every factual claim source-tagged and timestamped, single-source items marked unconfirmed. The renderer adds the verdict banner, the health line, the decisions table, and the footer; it drops `sections` entirely on an aborted run, and it escapes everything you pass, so write plain prose and let it handle the markup. Send with Gmail using the returned `subject` and `html` (`contentType: text/html`) to the address in `state.json.config.email_to`.
>
> **THE EMAIL ALWAYS SENDS.** If the run aborted, the email says so and explains why. Silence must never be the outcome.
>
> Never fabricate a number. Every figure carries a timestamp and a named source. If a figure cannot be retrieved, say it was unavailable rather than estimating it. An empty suggestions section and a do-nothing day are correct and expected outputs.

---

## Dry-run variant

For the proving run (item 5 in section 10 of `HANDOFF.md`), use the prompt above
with this paragraph inserted immediately after the Stage 0 heading:

> **THIS IS A DRY RUN. Place no orders of any kind. `review_equity_order` is permitted; `place_equity_order` is forbidden regardless of what any later stage says. In Stage 5, compute and report every order you would have placed — symbol, side, quantity, limit, stop, resulting weight — and place none of them. Label the email subject `[DRY RUN]`.**

The dry run proves authentication, data retrieval, computation, Drive writes, and
email delivery end to end without risking a fill. Do not skip it.
