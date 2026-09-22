"""backtest_data — getting point-in-time history onto disk for backtest.py.

Two ways in, one layout out (`data/bt/`, gitignored like every other pull):

    data/bt/earnings/<SYM>.json      Alpha Vantage EARNINGS
    data/bt/prices/<SYM>.csv         TIME_SERIES_DAILY_ADJUSTED, outputsize=full
    data/bt/news/<SYM>-<decision>.json   NEWS_SENTIMENT for one decision window

1. **Direct** (`ALPHAVANTAGE_API_KEY` set): `fetch_symbol` calls the REST API
   itself, paced under a per-minute cap, and asks for news one decision
   window at a time -- only the lookback before each decision, bounded by
   `time_to`, exactly as plan section 8 item 1 asks. (One unbounded page of
   1,000 articles reached back only six months for OXY, so "fetch it all and
   filter" does not scale to large caps.) This is the path meant for the
   full universe -- a few thousand calls, no model in the loop.
2. **Connector** (no key): a Claude session calls the Alpha Vantage MCP tools
   and hands the saved result file to `ingest`. An oversized result arrives
   as a preview envelope whose `data_url` is fetched here, so the bytes never
   pass through the model -- the same lesson as `drive_snippets.py`.

Nothing here judges the data; `backtest.py` and `research.py` do that.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent / "data" / "bt"
API = "https://www.alphavantage.co/query"
KINDS = ("earnings", "prices", "news", "insider", "congress")


def path_for(kind: str, symbol: str, window: str = "") -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    sym = symbol.upper()
    if kind == "prices":
        return ROOT / "prices" / f"{sym}.csv"
    if kind == "news":
        return ROOT / "news" / f"{sym}-{window}.json"
    if kind in ("insider", "congress"):
        return ROOT / kind / f"{sym}.json"
    if kind in ("insider", "congress"):
        return ROOT / kind / f"{sym}.json"
    return ROOT / "earnings" / f"{sym}.json"


def _download(url: str) -> str:
    # The response CDN refuses urllib's default User-Agent with a 403.
    req = urllib.request.Request(url, headers={"User-Agent": "premarket-brief-backtest/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def payload_text(result: str) -> str:
    """The full payload behind one Alpha Vantage MCP tool result. A preview
    envelope (`{"preview": true, ..., "data_url": ...}`) is replaced by the
    file at its `data_url`; anything else is already the payload. A preview
    without a `data_url` is refused -- its `sample_data` is a truncated sample,
    never the data (see `research._preview_item`)."""
    try:
        obj = json.loads(result)
    except ValueError:
        return result
    if isinstance(obj, dict) and obj.get("preview") is True:
        if not obj.get("data_url"):
            raise ValueError("preview envelope with no data_url; the sample is not the data")
        return _download(obj["data_url"])
    return result


def check_news_window(text: str, symbol: str, window: str, lookback_days: int = 21) -> None:
    """Refuse a news payload that is not the window it claims to be. Parallel
    NEWS_SENTIMENT calls have come back cross-wired before (research.py, 5
    September 2026); here every article must fall inside the requested
    `[decision - lookback, decision)` range. `research.news_items_from_alpha_vantage`
    separately refuses one that is about the wrong ticker."""
    decision = date.fromisoformat(window)
    lo = (decision - timedelta(days=lookback_days)).strftime("%Y%m%dT0000")
    hi = decision.strftime("%Y%m%dT0000")
    for e in json.loads(text).get("feed", []):
        t = str(e.get("time_published", ""))[:13]
        # `<= hi`: a window fetched with time_to at midnight gets that midnight
        # article back (inclusive bound). It is still dated the decision day,
        # which two_sources_proxy's exclusive `asof` drops.
        if not (lo <= t <= hi):
            raise ValueError(f"{symbol} news for window {window} holds an article from {t}, "
                             f"outside [{lo}, {hi}) -- cross-wired or mis-requested")


def ingest(kind: str, symbol: str, result_file: str, window: str = "") -> Path:
    """Store one connector result (the file the harness saved it to)."""
    text = payload_text(Path(result_file).read_text())
    if kind == "news":
        check_news_window(text, symbol, window)
    out = path_for(kind, symbol, window)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


# --------------------------------------------------------------------------
# direct REST path
# --------------------------------------------------------------------------

_last_call = [0.0]
RATE_LIMIT_WORDS = ("rate limit", "burst pattern", "per minute", "requests per")


class RateLimited(RuntimeError):
    pass


def _get(params: dict, *, key: str, per_minute: int, retries: int = 6,
         backoff_seconds: float = 61.0, sleep=time.sleep) -> str:
    """One paced call. Alpha Vantage refuses both bursts (more than 5 a
    second) and the plan's per-minute cap with a 200 and a one-key JSON
    message; a rate-limit refusal is waited out and retried, anything else
    is raised. A refusal is never returned as data, so it can never be
    written to disk in place of a real response."""
    for attempt in range(retries + 1):
        wait = 60.0 / per_minute - (time.monotonic() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = time.monotonic()
        text = _download(API + "?" + urllib.parse.urlencode(dict(params, apikey=key)))
        if not text.lstrip().startswith("{"):
            return text
        obj = json.loads(text)
        refusal = next((obj[k] for k in ("Note", "Information", "Error Message")
                        if k in obj and len(obj) == 1), None)
        if refusal is None:
            return text
        if any(w in str(refusal).lower() for w in RATE_LIMIT_WORDS) and attempt < retries:
            sleep(backoff_seconds)
            continue
        raise (RateLimited if any(w in str(refusal).lower() for w in RATE_LIMIT_WORDS)
               else RuntimeError)(f"Alpha Vantage refused {params.get('function')}: {refusal}")
    raise RateLimited("unreachable")


def news_window_params(symbol: str, decision: date, lookback_days: int = 21) -> dict:
    """The NEWS_SENTIMENT request for one decision: the `lookback_days` before
    the decision date, ending at midnight that morning -- nothing published
    on or after the decision day is even requested."""
    return {"function": "NEWS_SENTIMENT", "tickers": symbol.upper(),
            "time_from": (decision - timedelta(days=lookback_days)).strftime("%Y%m%dT0000"),
            # time_to is INCLUSIVE at Alpha Vantage (an article stamped exactly
            # 00:00 on the decision day came back), so end a minute before it.
            "time_to": (decision - timedelta(days=1)).strftime("%Y%m%dT2359"),
            "limit": 1000, "sort": "LATEST"}


def decision_windows(symbol: str, *, start: date, end: date) -> list[date]:
    """Decision dates for `symbol` inside the pre-registered range, from the
    earnings file already on disk."""
    import backtest
    earnings = json.loads(path_for("earnings", symbol).read_text())
    return [w["decision_date"] for w in backtest.historical_catalyst_windows(earnings)
            if start <= w["decision_date"] <= end]


def fetch_symbol(symbol: str, *, key: str, start: date, end: date, per_minute: int = 60) -> dict:
    """Earnings, full adjusted prices, and one news window per decision for
    one symbol, skipping whatever is already on disk."""
    sym, done = symbol.upper(), {}
    for kind, params in (("earnings", {"function": "EARNINGS", "symbol": sym}),
                         ("prices", {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": sym,
                                     "outputsize": "full", "datatype": "csv"})):
        out = path_for(kind, sym)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_get(params, key=key, per_minute=per_minute))
        done[kind] = str(out)
    windows = decision_windows(sym, start=start, end=end)
    for d in windows:
        out = path_for("news", sym, d.isoformat())
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_get(news_window_params(sym, d), key=key, per_minute=per_minute))
    done["news_windows"] = len(windows)
    return done


def fetch_activity(symbol: str, *, key: str, per_minute: int = 70) -> dict:
    """Insider (Form 4) and congressional (STOCK Act) trading history for one
    symbol -- the full history in one call each, skipping what is on disk."""
    done = {}
    for kind, fn in (("insider", "INSIDER_TRANSACTIONS"), ("congress", "CONGRESS_TRADES")):
        out = path_for(kind, symbol)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_get({"function": fn, "symbol": symbol.upper()}, key=key, per_minute=per_minute))
        done[kind] = str(out)
    return done


def fetch_activity(symbol: str, *, key: str, per_minute: int = 70) -> dict:
    """Insider (Form 4) and congressional (STOCK Act) trading history for one
    symbol -- the full history in one call each, skipping what is on disk."""
    done = {}
    for kind, fn in (("insider", "INSIDER_TRANSACTIONS"), ("congress", "CONGRESS_TRADES")):
        out = path_for(kind, symbol)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_get({"function": fn, "symbol": symbol.upper()}, key=key, per_minute=per_minute))
        done[kind] = str(out)
    return done


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "ingest":
        print(ingest(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else ""))
        return 0
    if argv and argv[0] == "fetch-activity":
        key = os.environ.get("ALPHAVANTAGE_API_KEY")
        if not key:
            print("ALPHAVANTAGE_API_KEY is not set", file=sys.stderr)
            return 2
        for s in sorted(p.stem for p in (ROOT / "earnings").glob("*.json")):
            try:
                print(s, fetch_activity(s, key=key, per_minute=int(os.environ.get("AV_PER_MINUTE", "70"))), flush=True)
            except Exception as e:
                print(s, "FAILED", str(e).replace(key, "<key>"), file=sys.stderr, flush=True)
        return 0
    if argv and argv[0] == "fetch-activity":
        key = os.environ.get("ALPHAVANTAGE_API_KEY")
        if not key:
            print("ALPHAVANTAGE_API_KEY is not set", file=sys.stderr)
            return 2
        for s in sorted(p.stem for p in (ROOT / "earnings").glob("*.json")):
            try:
                print(s, fetch_activity(s, key=key, per_minute=int(os.environ.get("AV_PER_MINUTE", "70"))), flush=True)
            except Exception as e:
                print(s, "FAILED", str(e).replace(key, "<key>"), file=sys.stderr, flush=True)
        return 0
    if argv and argv[0] == "fetch":
        key = os.environ.get("ALPHAVANTAGE_API_KEY")
        if not key:
            print("ALPHAVANTAGE_API_KEY is not set", file=sys.stderr)
            return 2
        frozen = json.loads((Path(__file__).parent / "backtest_universe.json").read_text())
        universe, win = frozen["universe"], frozen["windows"]
        start = date.fromisoformat(win["decision_dates_from"])
        end = date.fromisoformat(win["decision_dates_through"])
        symbols = argv[1:] or universe["surfaced"]["symbols"] + universe["sampled"]["symbols"]
        for s in symbols:
            try:
                print(s, fetch_symbol(s, key=key, start=start, end=end,
                                      per_minute=int(os.environ.get("AV_PER_MINUTE", "70"))), flush=True)
            except Exception as e:           # one bad symbol must not stop the rest
                print(s, "FAILED", str(e).replace(key, "<key>"), file=sys.stderr)
        return 0
    print("usage: backtest_data.py ingest <earnings|prices|news> <SYM> <result-file> [decision-date]\n"
          "       backtest_data.py fetch [SYM ...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
