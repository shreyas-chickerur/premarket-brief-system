"""End-to-end demonstration of the morning pipeline.

Executes the same sequence a live morning would: preflight self-audit, data
validation, anomaly detection, volatility and indicator computation, stop and
size derivation, portfolio concentration, then writes a run manifest.

Data source, in order of preference:

  1. `data/<SYM>.csv`     -- saved Alpha Vantage pulls, real market data, not
                             committed (see .gitignore).
  2. `fixtures/<SYM>.csv` -- deterministic SYNTHETIC series built from a known
                             volatility by `make_fixtures.py`, committed so the
                             demo runs on a fresh clone with no API key.

Which one was used is printed in the header and recorded in the run manifest
under `data_source`. Synthetic output is never presented as market data: the
whole point of this system is that a number always says where it came from.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import quantcore as q
import runlog as R

SYMBOLS = ["SPY", "F", "INTC"]
AGENTIC_EQUITY = 1000.00
MAX_WEIGHT = 0.18
RISK_BUDGET = 0.02
DATA_ASOF = pd.Timestamp("2026-08-28")


REAL_DIR = Path("data")
FIXTURE_DIR = Path("fixtures")


def data_source() -> tuple[Path, str]:
    """Pick the data directory and name it honestly."""
    if all((REAL_DIR / f"{s}.csv").exists() for s in SYMBOLS):
        return REAL_DIR, "real"
    if all((FIXTURE_DIR / f"{s}.csv").exists() for s in SYMBOLS):
        return FIXTURE_DIR, "synthetic"
    raise SystemExit(
        f"no complete series for {SYMBOLS} in ./{REAL_DIR} or ./{FIXTURE_DIR}. "
        f"Run `python make_fixtures.py` to build the synthetic fallback."
    )


def load(sym: str, root: Path) -> pd.DataFrame:
    # comment="#" so the fixture provenance header does not become a data row.
    df = pd.read_csv(root / f"{sym}.csv", parse_dates=["timestamp"], comment="#")
    return df.set_index("timestamp").sort_index()


def run_self_test() -> dict:
    """The literal 'check for bugs before doing anything' step."""
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no"],
                       capture_output=True, text=True)
    dur = int((time.monotonic() - t0) * 1000)
    tail = (p.stdout.strip().splitlines() or ["no output"])[-1]
    n_pass = n_fail = 0
    for tok in tail.replace(",", " ").split():
        if tok.isdigit():
            continue
    import re
    m = re.search(r"(\d+) passed", tail)
    if m:
        n_pass = int(m.group(1))
    m = re.search(r"(\d+) failed", tail)
    if m:
        n_fail = int(m.group(1))
    return {"passed": p.returncode == 0, "n_passed": n_pass, "n_failed": n_fail,
            "duration_ms": dur, "summary": tail}


def main():
    log = R.RunLog(f"demo-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}", mode="dry_run")

    root, kind = data_source()
    log.metric("data_source", {"path": str(root), "kind": kind})
    banner = ("REAL saved market data" if kind == "real" else
              "SYNTHETIC fixtures -- illustrative only, NOT market data")
    print(f"data source      : ./{root}  ({banner})")

    # ---------------- preflight ----------------
    with log.stage("preflight"):
        st = run_self_test()
        R.preflight(
            log,
            available_tools=["robinhood.get_positions", "robinhood.place_equity_order",
                             "alphavantage.TIME_SERIES_DAILY", "gmail.send_message"],
            required_tools=["robinhood.get_positions", "robinhood.place_equity_order",
                            "alphavantage.TIME_SERIES_DAILY", "gmail.send_message"],
            local_time=datetime(2026, 8, 28, 6, 22),
            expected_local_hhmm=(6, 20),
            self_test=st,
            history=[],
        )
    print(f"self test        : {st['summary']}")
    print(f"preflight health : {log.health()}  may_trade={log.may_trade}\n")
    if not log.may_trade:
        print("ABORTED:", log.abort_reason)
        return log

    # ---------------- gather + validate ----------------
    frames, returns = {}, {}
    with log.stage("gather_and_validate") as s:
        for sym in SYMBOLS:
            t0 = time.monotonic()
            df = load(sym, root)
            log.call("alphavantage", f"TIME_SERIES_DAILY:{sym}", True,
                     int((time.monotonic() - t0) * 1000), f"{len(df)} bars")
            anoms = q.detect_anomalies(sym, df, asof=DATA_ASOF)
            for a in anoms:
                log.anomaly(a)
            if q.blocking(anoms):
                log.decide(R.Decision(sym, "skip", "agentic", False,
                                      f"blocked by data anomaly: {anoms[0].message}"))
                print(f"{sym}: BLOCKED — {anoms[0].message}")
                continue
            frames[sym] = df
            c = df["close"].astype(float)
            returns[sym] = np.log(c / c.shift(1)).dropna()
            s.records += 1

    print(f"{'sym':<6}{'last':>9}{'vol_yz':>9}{'spread':>8}{'atr':>8}"
          f"{'rsi':>7}{'volpct':>8}  {'trend':<22}{'quality':<10}")
    print("-" * 96)

    rows = []
    with log.stage("compute"):
        for sym, df in frames.items():
            vol, parts = q.consensus_volatility(df, window=60)
            atr = q.average_true_range(df)
            rsi = q.rsi(df["close"])
            vp = q.vol_percentile(df, window=20)
            tr = q.trend_state(df["close"])
            last = float(df["close"].iloc[-1])
            spread = (max(p.value for p in parts.values() if p.usable) /
                      min(p.value for p in parts.values() if p.usable))
            rows.append(dict(sym=sym, last=last, vol=vol, atr=atr, rsi=rsi,
                             vp=vp, trend=tr, parts=parts, spread=spread))
            print(f"{sym:<6}{last:>9.2f}{vol.value:>9.3f}{spread:>8.2f}"
                  f"{atr.value:>8.3f}{rsi.value:>7.1f}{vp.value:>8.1f}  "
                  f"{tr['state']:<22}{vol.quality:<10}")

    # ---------------- stops and sizing ----------------
    print(f"\n{'sym':<6}{'stop%':>8}{'stop$':>10}{'xdaily':>8}{'shares':>8}"
          f"{'notional':>10}{'weight':>8}{'risk$':>8}  reason")
    print("-" * 104)
    plans = []
    with log.stage("size"):
        for r in rows:
            plan = q.stop_plan(r["last"], r["vol"], r["atr"])
            size = q.size_position(AGENTIC_EQUITY, r["last"], plan,
                                   risk_budget_fraction=RISK_BUDGET,
                                   max_weight=MAX_WEIGHT, require_whole_shares=True)
            plans.append((r, plan, size))
            log.decide(R.Decision(
                r["sym"], "buy" if size.shares else "skip", "agentic", False,
                size.reason,
                inputs={"stop_fraction": round(plan.stop_fraction, 4),
                        "annual_vol": round(r["vol"].value, 4),
                        "vol_quality": r["vol"].quality}))
            print(f"{r['sym']:<6}{plan.stop_fraction*100:>7.2f}%{plan.stop_price:>10.2f}"
                  f"{plan.multiple_of_daily_vol:>8.2f}{size.shares:>8}"
                  f"{size.notional:>10.2f}{size.weight*100:>7.1f}%"
                  f"{size.risk_dollars:>8.2f}  {size.reason}")

    # ---------------- concentration ----------------
    with log.stage("concentration"):
        conc = q.correlation_concentration(returns)
    print(f"\nconcentration: mean pairwise correlation "
          f"{conc['mean_pairwise_corr']:.3f}, max {conc['max_pairwise_corr']:.3f} "
          f"{conc['max_pair']}, effective bets {conc['effective_bets']:.2f}, "
          f"concentrated={conc['concentrated']}")
    log.metric("concentration", conc)
    log.metric("symbols_examined", len(SYMBOLS))
    log.metric("symbols_tradable", sum(1 for _, _, s in plans if s.shares > 0))

    # ---------------- estimator detail ----------------
    print("\nestimator cross-check (annualised):")
    for r in rows:
        detail = "  ".join(f"{k[:4]}={v.value:.3f}" for k, v in r["parts"].items() if v.usable)
        print(f"  {r['sym']:<5} {detail}   spread={r['spread']:.2f}x")

    print(f"\nrun health: {log.health()}   anomalies: {len(log.anomalies)}   "
          f"decisions: {len(log.decisions)}")
    for a in log.anomalies:
        print(f"  [{a['severity']:<5}] {a['symbol']:<5} {a['code']:<16} {a['message']}")

    Path("run_manifest.json").write_text(log.to_json())
    print(f"\nmanifest written: {len(log.to_json())} bytes")
    if kind == "synthetic":
        print("\nNOTE: every figure above was computed from synthetic fixtures. "
              "It demonstrates that the pipeline runs, not what any real security did.")
    return log


if __name__ == "__main__":
    main()
