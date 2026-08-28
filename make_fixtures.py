"""Generate the deterministic fixture series `pipeline_demo.py` falls back to.

The demo needs price history to demonstrate anything, but `data/` holds saved
Alpha Vantage pulls and is deliberately not committed. Without a fallback the
demo is unrunnable for anyone who clones the repo, which makes it useless as the
thing it exists to be: a way to see the pipeline work before trusting it.

So the fixtures are SYNTHETIC and generated from a fixed seed. They are built
from a known annual volatility per symbol, which means they double as a
known-answer check: run the demo and the consensus estimate should land near the
`target_vol` printed in the header of each file. A fixture whose recovered
volatility drifts from its target is evidence of a real regression.

They are not market data and must never be presented as market data. The demo
labels its output accordingly, and the run manifest records which source it used.

Regenerate with:  python make_fixtures.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("fixtures")

# symbol -> (start price, target annualised vol, annual drift, seed)
# Levels and volatilities are chosen to resemble the real names loosely enough
# to be readable, and are not claimed to match them.
SPEC = {
    "SPY":  (640.0, 0.14, 0.08, 20260828),
    "F":     (11.5, 0.32, 0.01, 20260829),
    "INTC":  (24.0, 0.42, 0.00, 20260830),
}

N_BARS = 420                       # enough for the 252-day volatility percentile
LAST_SESSION = pd.Timestamp("2026-08-28")
TRADING_DAY = 252


def sessions(n: int, last: pd.Timestamp) -> pd.DatetimeIndex:
    """`n` weekday sessions ending on `last`.

    Weekdays only, so the calendar-gap anomaly check sees a clean series.
    Exchange holidays are not removed: a one-day gap is indistinguishable from a
    weekend to that check, and inventing holiday logic here would duplicate the
    verified table in runlog.py for no gain.
    """
    idx = pd.bdate_range(end=last, periods=n)
    return idx


def series(start: float, target_vol: float, drift: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    daily_sigma = target_vol / np.sqrt(TRADING_DAY)
    daily_mu = drift / TRADING_DAY - 0.5 * daily_sigma ** 2

    steps = rng.normal(daily_mu, daily_sigma, N_BARS)
    close = start * np.exp(np.cumsum(steps))

    # Open gaps modestly from the prior close; the intrabar range is drawn from
    # the same volatility so Parkinson and Garman-Klass see a coherent bar
    # rather than a close-only series with decorative wicks.
    prev = np.concatenate([[start], close[:-1]])
    open_ = prev * np.exp(rng.normal(0.0, daily_sigma * 0.3, N_BARS))

    hi_body = np.maximum(open_, close)
    lo_body = np.minimum(open_, close)
    high = hi_body * np.exp(np.abs(rng.normal(0.0, daily_sigma * 0.6, N_BARS)))
    low = lo_body * np.exp(-np.abs(rng.normal(0.0, daily_sigma * 0.6, N_BARS)))

    volume = rng.lognormal(mean=16.0, sigma=0.35, size=N_BARS).round().astype("int64")

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=sessions(N_BARS, LAST_SESSION),
    ).rename_axis("timestamp")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for sym, (start, vol, drift, seed) in SPEC.items():
        df = series(start, vol, drift, seed)

        realised = float(np.log(df["close"] / df["close"].shift(1)).dropna().std(ddof=1)
                         * np.sqrt(TRADING_DAY))
        path = OUT / f"{sym}.csv"
        with path.open("w") as fh:
            fh.write(f"# SYNTHETIC fixture -- not market data. seed={seed} "
                     f"target_vol={vol:.2f} realised_vol={realised:.3f}\n")
            df.round(4).to_csv(fh)
        print(f"{sym:<6} {len(df)} bars  {df.index[0].date()} -> {df.index[-1].date()}  "
              f"target_vol={vol:.2f} realised={realised:.3f}  {path}")


if __name__ == "__main__":
    main()
