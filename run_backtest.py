"""run_backtest — the frozen backtest (backtest_universe.json) over whatever
point-in-time data `backtest_data.py` has put in data/bt/.

    python run_backtest.py                  # every symbol with data on disk
    python run_backtest.py OXY SNAP         # just these

Writes data/bt/results.json and prints a summary. Every window is logged with
the condition it failed at, and a window with no news on disk is logged as
`news_missing` -- a gap in the data, never a gate rejection, and never
silently dropped.

Two numbers come out, on purpose (see `backtest.simulate`):
- the EVIDENCE BOOK: $100,000 of cash, every position sized as a $1,000
  account sizes it -- measures the signal, scored by `evidence.assess`;
- the $1,000 REPLAY: what the real account would have lived through.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import backtest as B
import backtest_data as D
import evidence
import washsale

HERE = Path(__file__).parent


def load_prices(symbol: str) -> pd.DataFrame | None:
    path = D.path_for("prices", symbol)
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    return df[~df.index.duplicated()]


def run(symbols: list[str] | None = None, *, judged: bool = False) -> dict:
    """`judged=True` is plan 6c: a window that clears the proxy and conditions
    3-5 counts only if its independent judge (data/bt/judged/verdicts/) also
    cleared it -- a missing verdict is `judge_missing`, never a clear."""
    frozen = json.loads((HERE / "backtest_universe.json").read_text())
    uni, win, gate, reg = frozen["universe"], frozen["windows"], frozen["gate"], frozen["preregistration"]
    start, end = date.fromisoformat(win["decision_dates_from"]), date.fromisoformat(win["decision_dates_through"])
    ts = gate["two_sources"]
    sizing_equity = frozen["accounts"]["evidence_book"]["sizing_equity"]
    symbols = symbols or uni["surfaced"]["symbols"] + uni["sampled"]["symbols"]
    stratum = {s: "surfaced" for s in uni["surfaced"]["symbols"]}
    stratum.update({s: "sampled" for s in uni["sampled"]["symbols"] if s not in stratum})

    spy_raw = load_prices("SPY")
    if spy_raw is None:
        raise SystemExit("SPY prices are required as the benchmark: fetch them first")
    spy_tr = B.total_return_frame(spy_raw)

    log, trials, outcomes, raw_prices, missing = [], [], [], {}, {}
    for sym in symbols:
        earn_path, px = D.path_for("earnings", sym), load_prices(sym)
        if not earn_path.exists() or px is None:
            missing[sym] = "no earnings or prices on disk"
            continue
        try:
            windows = [w for w in B.historical_catalyst_windows(json.loads(earn_path.read_text()))
                       if start <= w["decision_date"] <= end]
        except ValueError as e:
            missing[sym] = f"no earnings history: {e}"
            continue
        raw_prices[sym] = px
        px_tr = B.total_return_frame(px)
        for w in windows:
            d = w["decision_date"]
            row = {"symbol": sym, "stratum": stratum.get(sym, "other"), "decision_date": d.isoformat(),
                   "report_date": w["report_date"].isoformat(), "gate_failed": None}
            news_path = D.path_for("news", sym, d.isoformat())
            if not news_path.exists():
                row["gate_failed"] = "news_missing"
                log.append(row)
                continue
            items = B.av_publisher_items(json.loads(news_path.read_text()), symbol=sym, asof=d.isoformat())
            proxy = B.two_sources_proxy(items, symbol=sym, asof=d, lookback_days=ts["lookback_days"],
                                        min_relevance=ts["min_relevance"])
            row["distinct_sources"] = proxy["distinct_sources"]
            row["counted_sources"] = proxy["counted_sources"]
            if not proxy["cleared"]:
                row["gate_failed"] = "two_sources"
                log.append(row)
                continue
            v = B.evaluate_price_conditions(sym, px, decision_date=d, account_equity=sizing_equity,
                                            registry=washsale.Registry())
            row.update({k: v[k] for k in ("session", "sized_at", "entry", "stop_price", "shares", "detail")})
            row["gate_failed"] = v["gate_failed"]
            if judged and not v["gate_failed"]:
                vf = D.ROOT / "judged" / "verdicts" / f"{sym}-{d.isoformat()}.json"
                if not vf.exists():
                    row["gate_failed"] = "judge_missing"
                elif not json.loads(vf.read_text()).get("cleared"):
                    row["gate_failed"] = "two_sources"
                    row["judge_rejected"] = True
            log.append(row)
            if row["gate_failed"]:
                continue
            trials.append({"symbol": sym, "session": v["session"], "sized_at": v["sized_at"],
                           "entry": v["entry"], "plan": v["plan"]})
            session = date.fromisoformat(v["session"])
            out = B.settle_window(sym, px_tr, spy_tr, opened=session,
                                  entry=float(px_tr.loc[pd.Timestamp(session), "open"]),
                                  horizon_days=win["horizon_days"], cost_pct=reg["cost_pct"])
            if out is not None:
                outcomes.append(out)

    prereg = evidence.PreRegistration(
        hypothesis=reg["hypothesis"], target_edge_pct=reg["target_edge_pct"],
        horizon_days=reg["horizon_days"], decide_by=date(2027, 12, 31), alpha=reg["alpha"],
        power=reg["power"], assumed_sd_pct=reg["assumed_sd_pct"], cost_pct=reg["cost_pct"])
    book = frozen["accounts"]["evidence_book"]
    results = {
        "frozen_on": frozen["frozen_on"],
        "symbols_run": len(raw_prices), "symbols_missing": missing,
        "windows": len(log),
        "funnel": pd.Series([r["gate_failed"] or "cleared" for r in log]).value_counts().to_dict() if log else {},
        "evidence": evidence.assess(outcomes, prereg),
        "evidence_book": B.simulate(trials, raw_prices, spy_raw, starting_cash=book["starting_cash"],
                                    sizing_equity=book["sizing_equity"],
                                    horizon_days=win["horizon_days"], cost_pct=reg["cost_pct"]),
        "replay_1000": B.simulate(trials, raw_prices, spy_raw,
                                  starting_cash=frozen["accounts"]["replay"]["starting_cash"],
                                  horizon_days=win["horizon_days"], cost_pct=reg["cost_pct"]),
        "decision_log": log,
    }
    return results


def summary(r: dict) -> str:
    ev, bk, rp = r["evidence"], r["evidence_book"], r["replay_1000"]
    lines = [
        f"symbols run: {r['symbols_run']}   missing: {len(r['symbols_missing'])}   windows: {r['windows']}",
        f"funnel: {r['funnel']}",
        f"evidence: n={ev['n']} verdict={ev.get('verdict')} decision={ev.get('decision')}"
        + (f" mean_excess={ev.get('mean_excess_pct')}" if 'mean_excess_pct' in ev else ""),
        f"evidence book (sized as $1,000): {len(bk['trades'])} trades, "
        f"P&L ${sum(t['pnl'] for t in bk['trades']):.2f} in $1,000-account dollars",
        f"$1,000 replay: final ${rp['final_equity']:.2f} ({rp['total_return_pct']:+.2f}%) vs SPY "
        f"{rp['benchmark_return_pct']:+.2f}% over the same span, max drawdown {rp['max_drawdown_pct']:.2f}%, "
        f"{len(rp['trades'])} trades, skipped {len(rp['skipped'])}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--judged"]
    res = run(args or None, judged="--judged" in sys.argv)
    out = D.ROOT / ("results-judged.json" if "--judged" in sys.argv else "results.json")
    out.write_text(json.dumps(res, indent=1, default=str))
    print(summary(res))
    print(f"full results: {out}")
