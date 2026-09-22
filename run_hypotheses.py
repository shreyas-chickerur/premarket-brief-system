"""run_hypotheses — one frozen round of hypotheses_prereg.json.

    python run_hypotheses.py discovery <prereg.json>   # screen every hypothesis on the discovery period
    python run_hypotheses.py holdout <prereg.json>     # confirm ONLY the ones the screen carried

Writes data/bt/hypotheses/round<N>-<phase>.json. The holdout run refuses to
look at any hypothesis the committed discovery file did not carry.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import backtest as B
import backtest_data as D
import hypotheses as H
import run_backtest as RB

HERE = Path(__file__).parent
OUT = D.ROOT / "hypotheses"


def _z(p: float) -> float:
    return statistics.NormalDist().inv_cdf(p)


def load_all():
    uni = json.loads((HERE / "backtest_universe.json").read_text())["universe"]
    raw, tr, earn = {}, {}, {}
    for s in dict.fromkeys(uni["surfaced"]["symbols"] + uni["sampled"]["symbols"]):
        ep, px = D.path_for("earnings", s), RB.load_prices(s)
        if not ep.exists() or px is None:
            continue
        e = json.loads(ep.read_text())
        if not e.get("quarterlyEarnings"):
            continue
        raw[s], tr[s], earn[s] = px, B.total_return_frame(px), e
    return raw, tr, earn, B.total_return_frame(RB.load_prices("SPY"))


def trials_for(spec: dict, tr, earn, spy_tr, start: date, end: date) -> list[dict]:
    if spec["signal"] == "pead":
        kw = {k: spec[k] for k in ("min_surprise_pct", "min_reaction_excess_pct") if k in spec}
        return [t for s in tr for t in H.pead_trials(s, earn[s], tr[s], spy_tr, horizon_days=spec["horizon_days"],
                                                    start=start, end=end, **kw)]
    if spec["signal"] == "runup":
        return [t for s in tr for t in H.runup_trials(s, earn[s], tr[s], lead_sessions=spec["lead_sessions"],
                                                     start=start, end=end)]
    if spec["signal"] == "momentum":
        return H.momentum_trials(tr, spy_tr, top_n=spec["top_n"], start=start, end=end,
                                 horizon_days=spec["horizon_days"])
    if spec["signal"] == "insider":
        return [t for s in tr if (HERE / "data" / "bt" / "insider" / f"{s}.json").exists()
                for t in H.insider_trials(s, json.loads((D.ROOT / "insider" / f"{s}.json").read_text()), tr[s],
                                          mode=spec["mode"], horizon_days=spec["horizon_days"], start=start, end=end)]
    if spec["signal"] == "congress":
        return [t for s in tr if (D.ROOT / "congress" / f"{s}.json").exists()
                for t in H.congress_trials(s, json.loads((D.ROOT / "congress" / f"{s}.json").read_text()), tr[s],
                                           horizon_days=spec["horizon_days"], start=start, end=end,
                                           min_amount=spec.get("min_amount", 0))]
    raise ValueError(spec["signal"])


def carried_so_far(before_round: int) -> int:
    """Hypotheses carried to a holdout in earlier rounds -- the multiple-
    testing count keeps growing across rounds; it never resets."""
    n = 0
    for r in range(1, before_round):
        f = OUT / f"round{r}-discovery.json"
        if f.exists():
            n += len(json.loads(f.read_text())["carried"])
    return n


def run(phase: str, prereg: str) -> dict:
    pre = json.loads((HERE / prereg).read_text())
    rnd, (a, b) = pre["round"], pre["periods"][phase]
    start, end = date.fromisoformat(a), date.fromisoformat(b)
    names = list(pre["hypotheses"])
    if phase == "holdout":
        disc = json.loads((OUT / f"round{rnd}-discovery.json").read_text())
        names = disc["carried"]
    raw, tr, earn, spy_tr = load_all()
    res = {}
    for h in names:
        spec = pre["hypotheses"][h]
        trials = trials_for(spec, tr, earn, spy_tr, start, end)
        ev = H.evaluate(trials, raw, tr, spy_tr, account_equity=pre["account"]["sizing_equity"], cost_pct=pre["cost_pct"])
        hi_cost = [o.excess_pct - (0.30 - pre["cost_pct"]) for o in ev["outcomes"]]
        row = {k: ev[k] for k in ("candidates", "gated", "n", "mean_excess_pct", "median_excess_pct",
                                  "sd_excess_pct", "win_rate_vs_spy", "months", "mean_monthly_excess_pct", "t")}
        row["mean_excess_pct_at_0.30_cost"] = statistics.mean(hi_cost) if hi_cost else float("nan")
        row["_trials"] = ev["trials"]
        res[h] = row
    out = {"round": rnd, "phase": phase, "period": [a, b], "results": res}
    if phase == "discovery":
        screen_t = _z(1 - 0.05 / len(pre["hypotheses"]))
        out["screen_t"] = screen_t
        out["carried"] = [h for h, r in res.items()
                          if r["n"] >= 100 and r["mean_excess_pct"] >= 0.50 and r["t"] >= screen_t]
    else:
        m = max(1, len(names) + (carried_so_far(rnd) if rnd > 1 else 0))
        crit = _z(1 - 0.05 / m)
        out["critical_t"] = crit
        out["confirmed"] = [h for h, r in res.items()
                            if r["n"] >= 50 and r["t"] >= crit and r["mean_excess_pct"] > 0
                            and r["mean_excess_pct_at_0.30_cost"] > 0]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"round{rnd}-{phase}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def show(out: dict) -> None:
    print(f"round {out['round']} {out['phase']} {out['period']}")
    for h, r in out["results"].items():
        print(f"  {h}: n={r['n']:4d} mean={r['mean_excess_pct']:+.2f}% median={r['median_excess_pct']:+.2f}% "
              f"win={r['win_rate_vs_spy']:.0%} months={r['months']} t={r['t']:+.2f} "
              f"@0.30%={r['mean_excess_pct_at_0.30_cost']:+.2f}%  gated={r['gated']}")
    for k in ("screen_t", "carried", "critical_t", "confirmed"):
        if k in out:
            print(f"  {k}: {out[k]}")


if __name__ == "__main__":
    show(run(sys.argv[1], sys.argv[2]))
