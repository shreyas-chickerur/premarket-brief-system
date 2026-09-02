# Scheduled routines — trigger prompt templates

The actual trading and reporting procedure lives in two files in this
repository, not in the trigger prompts themselves:

- [`DAILY_PROCEDURE.md`](DAILY_PROCEDURE.md) — Stage 0 through 6, followed by
  the 06:20 Central scheduled run and, on a retry, by the watchdog.
- [`WATCHDOG_PROCEDURE.md`](WATCHDOG_PROCEDURE.md) — the 07:20 Central check:
  did today's run happen and was it healthy, and if not, diagnose, attempt a
  fix, merge it, and re-run `DAILY_PROCEDURE.md` once.

Each trigger's own prompt is now just a short pointer that clones the repo
and tells the agent which procedure file to follow — a single canonical copy
of the actual logic instead of two independently-edited, multi-thousand-word
prompts drifting apart from each other, which is exactly the kind of bug
this project has hit before.

Two placeholders must be substituted in both trigger prompts before use:

| Placeholder | Value |
|---|---|
| `{{DRIVE_FOLDER_ID}}` | Google Drive folder holding `state.json`, the dated `journal-*.json` files, and the dated `run-manifest-*.json` files |
| `{{STATE_FILE_ID}}` | Drive file id of the current `state.json` (schema 3 as of 31 August 2026) |

Both are in `HANDOFF.private.md`. Neither is ever committed to this public
repository — everything else the run needs is read from `state.json` or
rebuilt from the broker at Stage 0.

Create each trigger with the Claude Code Remote `create_trigger` tool, cron
in UTC, `requires_local_device: false`. See section 8 of `HANDOFF.md` for the
schedule and the daylight-saving changeover dates. **Attach the Robinhood,
Alpha Vantage, and Google Drive connectors to the main routine, and Gmail +
Google Drive to the watchdog** — a scheduled routine sees only the
connectors listed in its own configuration, not the ones connected to the
account. This was the cause of the first scheduled fire aborting with 9 of
10 tools missing. The watchdog carries neither the Robinhood nor Alpha
Vantage connector directly — it gets both only by way of following
`DAILY_PROCEDURE.md`'s own Stage 0 step 3, which checks for them the same way
the main routine does, so a missing connector on a watchdog retry fails the
same loud, honest way it would on the main run.

---

## Main routine trigger prompt

```
You are running the Pre-Market Brief System's daily trading procedure.

git clone --depth 1 https://github.com/shreyas-chickerur/premarket-brief-system /tmp/pbs
cd /tmp/pbs
pip install --break-system-packages -q -r requirements.txt

Read and follow DAILY_PROCEDURE.md exactly, substituting:
  {{DRIVE_FOLDER_ID}} = <the real folder id>
  {{STATE_FILE_ID}}   = <the real file id>
wherever the document uses those placeholders. You are the 06:20 scheduled
routine invoking this procedure directly — follow its Stage 6 branch for
that caller (stay silent on an ABORTED run; the watchdog owns the retry).
```

## Watchdog trigger prompt

```
You are the Watchdog for the Pre-Market Brief System.

git clone --depth 1 https://github.com/shreyas-chickerur/premarket-brief-system /tmp/pbswd
cd /tmp/pbswd
pip install --break-system-packages -q -r requirements.txt

Read and follow WATCHDOG_PROCEDURE.md exactly, substituting:
  {{DRIVE_FOLDER_ID}} = <the real folder id>
wherever the document uses that placeholder. When it directs you to follow
DAILY_PROCEDURE.md yourself (Stage 5), substitute both placeholders there too
and identify yourself as "the watchdog retry" as that document describes.
```

---

## Dry-run vs. live

`DAILY_PROCEDURE.md`'s Stage 0 currently opens with a `THIS IS A DRY RUN`
paragraph that forbids `place_equity_order` outright. That single paragraph
is the entire gate between this system and real order placement on the
agentic account — removing it is a deliberate, one-time, human decision, not
something either routine (including the watchdog's self-heal merges) is ever
allowed to touch. See `HANDOFF.md`'s "Path to live trading" section for what
should be true before that paragraph comes out, and exactly what to change
when it's time.
