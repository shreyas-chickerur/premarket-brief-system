# Pre-Market Brief System

Measurement and health layers for a daily pre-market research and trading brief
running on a broker's agentic trading account.

This repository holds **code only**. All state — the trade journal, the wash-sale
registry, open theses, run manifests, configuration values, and account
identifiers — lives in a private Google Drive folder and is never committed here.

Start with [HANDOFF.md](HANDOFF.md), the operations guide.

## Modules

| File | Purpose |
|---|---|
| `quantcore.py` | Volatility estimators, indicators, stop distance, position sizing, data-anomaly detection |
| `runlog.py` | Run manifests, staged timing, the preflight self-audit, regression review, honest scoring |
| `washsale.py` | Cross-account wash-sale registry (26 U.S.C. 1091 is taxpayer-level, not account-level) |
| `ledger.py` | Positions and the wash-sale trade list rebuilt from broker order history; the append-only journal |
| `evidence.py` | Pre-registered edge testing: sample-size planning, futility stopping, the policy that pauses trading on a ruled-out claim |
| `emailer.py` | HTML rendering of the brief, with failure diagnosis |
| `pipeline_demo.py` | End-to-end demonstration run |
| `make_fixtures.py` | Regenerates the deterministic synthetic series the demo falls back to |

## Design rules

1. Every measurement returns its value **plus** the sample size it came from and a
   quality flag: `ok`, `thin`, `degraded`, or `failed`. Nothing returns a
   plausible-looking number without saying how much to trust it.
2. A failed estimate propagates as a refusal, never as a default.
3. Every action **and every deliberate non-action** is recorded. A day with no
   trades leaves the same audit trail as a day with three.
4. The morning run audits itself before it looks at a single price: run the test
   suite, verify the brokerage tools are visible, reconcile the ledger against the
   broker, check the clock and calendar, review the last ten runs.
5. Stops are sized from each security's own volatility, then floored and capped.
   A flat percentage is wrong for every stock at once.
6. The market calendar is a verified table, not derived from the observance
   rules, and it fails loudly once past the horizon it was verified through.
7. Positions and the wash-sale registry are rebuilt from broker order history
   every run, never stored — a stale local copy is a form of drift the system
   cannot detect on its own.
8. Whether this system has an edge is a pre-registered, reviewed question, not
   a one-time claim: `evidence.py` states the sample size a claimed edge needs
   before trusting it, and stops opening new positions the moment that edge is
   ruled out.

## Broker constraints this code encodes

- **Fractional positions cannot carry stop orders.** Verified against the live API:
  a 0.5-share stop returns `Invalid trigger for fractional order`, while the same
  order for 1 whole share is accepted. The order *simulator* accepts the fractional
  version, so the simulator alone is not proof.
- **Stops do not execute outside regular hours**, so overnight gaps are unprotected
  regardless of what rests at the broker.
- **No bracket or one-cancels-other orders**, and the agent connector exposes no
  cancel tool — so stops are placed good-for-day and re-derived each morning rather
  than left resting.

## Running it

Python 3.11 or newer.

```
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # 235 tests
python pipeline_demo.py      # end-to-end run
```

The demo prefers real saved pulls in `data/` and falls back to the committed
synthetic fixtures in `fixtures/`, so a fresh clone runs with no API key. It
prints which source it used, and says plainly when the numbers are synthetic.

## Tests

Known-answer tests simulate a price path with a *known* volatility and require each
estimator to recover it. An estimator that returns a plausible number for the wrong
reason is the failure mode that matters, and only a known-answer test catches it.

The market-calendar tests are the same idea applied to a hand-maintained table:
they compare it against the exchange's published calendar, because a holiday date
that merely looks reasonable is exactly the kind of wrong that goes unnoticed.
