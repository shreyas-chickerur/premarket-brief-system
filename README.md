# Pre-Market Brief System

Measurement and health layers for a daily pre-market research and trading brief
running on a broker's agentic trading account.

This repository holds **code only**. All state — the trade journal, the wash-sale
registry, open theses, run manifests, and account identifiers — lives in a private
Google Drive folder and is never committed here.

## Modules

| File | Purpose |
|---|---|
| `quantcore.py` | Volatility estimators, indicators, stop distance, position sizing, data-anomaly detection |
| `runlog.py` | Run manifests, staged timing, the preflight self-audit, regression review, honest scoring |
| `washsale.py` | Cross-account wash-sale registry (26 U.S.C. 1091 is taxpayer-level, not account-level) |
| `pipeline_demo.py` | End-to-end demonstration run against saved market data |

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
5. Stops are sized from each security's own volatility, floored at 6% and capped
   at 15%. A flat percentage is wrong for every stock at once.

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

## Tests

```
pip install --break-system-packages arch statsmodels pytest
python -m pytest -q
```

Known-answer tests simulate a price path with a *known* volatility and require each
estimator to recover it. An estimator that returns a plausible number for the wrong
reason is the failure mode that matters, and only a known-answer test catches it.
