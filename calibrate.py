"""calibrate — plan section 6b: does the mechanical `two_sources` proxy agree
with real judgment, on the same point-in-time news?

    python calibrate.py packets     # draw the frozen sample, write blind judge packets
    python calibrate.py score       # compare judge verdicts with the proxy

`packets` writes data/bt/calibration/packets/<SYM>-<decision>.json: the
articles about the symbol published before the decision, and nothing else --
no prices, no outcome, no proxy verdict. Each judge (a separate, context-
isolated Claude session) reads one packet, applies RULE below, and writes
data/bt/calibration/verdicts/<same name>.json as
`{"cleared": bool, "sources_counted": [...], "reason": "..."}`.

`score` computes agreement and, separately, the false-clear rate -- the share
of proxy clears a judge rejected, the direction that manufactures a fake
edge. The decision rule is frozen in backtest_calibration.json.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import backtest as B
import backtest_data as D

HERE = Path(__file__).parent
CAL = D.ROOT / "calibration"

# The live condition, verbatim from HANDOFF.md section 7, plus the one
# clarification a judge needs to apply it to archived articles.
RULE = """Condition 2 of the five-condition gate: "Two independent corroborating sources.
Two outlets reprinting one wire is one source."

You are judging ONE decision window. Below are the news articles about {symbol}
that were published in the 21 days before {decision}, as they existed then.
Decide whether at least TWO INDEPENDENT sources genuinely report on {symbol}
itself -- the company, its business, its stock -- in a way that could
corroborate a thesis about it. Count a source only if:
- the article is substantively about {symbol} (not a passing mention in a list
  of many names, an ETF's holdings page, a generic market wrap or a listicle);
- it is not the same story as another counted source (one wire story reprinted
  by several outlets, or near-identical headlines, count once).
Judge only from what is below. You do not know the price, the outcome, or what
any other method decided."""


def _frozen() -> dict:
    return json.loads((HERE / "backtest_calibration.json").read_text())


def windows_on_disk() -> list[dict]:
    """Every (symbol, decision) window with news fetched, and how many articles
    about the symbol it holds before the decision."""
    out = []
    for f in sorted((D.ROOT / "news").glob("*.json")):
        sym, decision = f.stem.rsplit("-", 3)[0], "-".join(f.stem.rsplit("-", 3)[1:])
        packet = B.judge_packet(json.loads(f.read_text()), symbol=sym, decision=date.fromisoformat(decision))
        out.append({"symbol": sym, "decision_date": decision, "articles": len(packet["articles"])})
    return out


def write_packets() -> list[str]:
    fz = _frozen()
    sample = B.calibration_sample(windows_on_disk(), n=fz["n"], seed=fz["seed"])
    (CAL / "packets").mkdir(parents=True, exist_ok=True)
    names = []
    for w in sample:
        name = f"{w['symbol']}-{w['decision_date']}"
        raw = json.loads(D.path_for("news", w["symbol"], w["decision_date"]).read_text())
        packet = B.judge_packet(raw, symbol=w["symbol"], decision=date.fromisoformat(w["decision_date"]),
                                max_articles=60)
        packet["rule"] = RULE.format(symbol=w["symbol"], decision=w["decision_date"])
        (CAL / "packets" / f"{name}.json").write_text(json.dumps(packet, indent=1))
        names.append(name)
    return names


def write_population_packets() -> list[str]:
    """Plan 6c: a blind packet for every window that cleared the proxy AND
    conditions 3-5 in the last run_backtest.py results. Judges write
    data/bt/judged/verdicts/<name>.json in the same shape as calibration."""
    log = json.loads((D.ROOT / "results.json").read_text())["decision_log"]
    out_dir = D.ROOT / "judged" / "packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for row in log:
        if row["gate_failed"] is not None:
            continue
        sym, decision = row["symbol"], row["decision_date"]
        raw = json.loads(D.path_for("news", sym, decision).read_text())
        packet = B.judge_packet(raw, symbol=sym, decision=date.fromisoformat(decision), max_articles=60)
        packet["rule"] = RULE.format(symbol=sym, decision=decision)
        name = f"{sym}-{decision}"
        (out_dir / f"{name}.json").write_text(json.dumps(packet, indent=1))
        names.append(name)
    return names


def score() -> dict:
    fz = _frozen()
    ts = json.loads((HERE / "backtest_universe.json").read_text())["gate"]["two_sources"]
    proxy, judge = {}, {}
    for v in sorted((CAL / "verdicts").glob("*.json")):
        name = v.stem
        sym, decision = name.rsplit("-", 3)[0], "-".join(name.rsplit("-", 3)[1:])
        raw = json.loads(D.path_for("news", sym, decision).read_text())
        items = B.av_publisher_items(raw, symbol=sym, asof=decision)
        proxy[name] = B.two_sources_proxy(items, symbol=sym, asof=date.fromisoformat(decision),
                                          lookback_days=ts["lookback_days"],
                                          min_relevance=ts["min_relevance"])["cleared"]
        judge[name] = bool(json.loads(v.read_text())["cleared"])
    r = B.agreement(proxy, judge)
    r["passes"] = (r["n"] >= fz["min_judged"] and r["agreement"] >= fz["min_agreement"]
                   and r["false_clear_rate"] <= fz["max_false_clear_rate"])
    r["rule"] = {k: fz[k] for k in ("min_judged", "min_agreement", "max_false_clear_rate")}
    return r


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "packets":
        print("\n".join(write_packets()))
    elif cmd == "population":
        print("\n".join(write_population_packets()))
    elif cmd == "score":
        print(json.dumps(score(), indent=1))
    else:
        print(__doc__)
        sys.exit(2)
