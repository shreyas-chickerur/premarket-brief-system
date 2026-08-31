"""
washsale — the cross-account repurchase registry.

Why this exists: 26 U.S.C. 1091 is written at the TAXPAYER level, not the
account level. A loss sale in the individual account and a repurchase in the
agentic account within thirty days is a wash sale, and the broker is only
required to report one when the sale and the repurchase happen in the SAME
account with the same security. Nothing else in the chain catches the
cross-account case, so this does.

Two directions, both of which matter:
  1. BUY blocked   — we sold it at a loss inside the last 30 days.
  2. SELL warned   — we bought it inside the last 30 days, so realising a loss
                     now washes against that purchase.

Deliberately conservative: it blocks rather than optimises, because the cost of
a false block is waiting a few weeks and the cost of a miss is a disallowed
loss the tax form never mentions.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

WINDOW_DAYS = 30                     # each side of the sale; 61 days inclusive

# Funds that track the same underlying index closely enough that the Internal
# Revenue Service has never ruled them safely distinct. No authority says these
# ARE substantially identical, and none says they are not — so the system warns
# on them and never silently treats a swap as clean.
PROXY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"SPY", "VOO", "IVV", "SPLG"}),          # S&P 500
    frozenset({"VTI", "ITOT", "SCHB", "VTSAX"}),       # total US market
    frozenset({"QQQ", "QQQM"}),                        # Nasdaq 100
    frozenset({"VXUS", "IXUS", "VTIAX"}),              # total international
    frozenset({"VGSH", "SCHO", "SHY"}),                # short Treasury
    frozenset({"SGOV", "BIL", "TBIL"}),                # Treasury bills
    frozenset({"GLDM", "GLD", "IAU"}),                 # gold
    frozenset({"VTV", "IVE", "SCHV"}),                 # large value
)


def proxies_for(symbol: str) -> frozenset[str]:
    s = symbol.upper()
    for g in PROXY_GROUPS:
        if s in g:
            return g - {s}
    return frozenset()


@dataclass(frozen=True)
class Trade:
    symbol: str
    account: str            # "individual" | "agentic"
    on: date
    side: str               # "buy" | "sell"
    quantity: float
    realized_pnl: Optional[float] = None   # required and negative for a loss sale

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError(f"bad side: {self.side}")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.side == "sell" and self.realized_pnl is None:
            raise ValueError("a sell must carry realized_pnl to be classified")

    @property
    def is_loss_sale(self) -> bool:
        return self.side == "sell" and (self.realized_pnl or 0) < 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["on"] = self.on.isoformat()
        return d


@dataclass
class Verdict:
    allowed: bool
    severity: str           # ok | warn | block
    reason: str
    clears_on: Optional[date] = None
    triggering: tuple = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["clears_on"] = self.clears_on.isoformat() if self.clears_on else None
        d["triggering"] = [t.to_dict() if isinstance(t, Trade) else t for t in self.triggering]
        return d


class Registry:
    """Holds every trade in BOTH accounts. Order of insertion does not matter."""

    def __init__(self, trades: Iterable[Trade] = ()):
        self._trades: list[Trade] = list(trades)

    def add(self, t: Trade) -> None:
        self._trades.append(t)

    def __len__(self) -> int:
        return len(self._trades)

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    # ---------------------------------------------------------------- buys

    def check_buy(self, symbol: str, asof: date) -> Verdict:
        """May we buy `symbol` today without disallowing an earlier loss?"""
        sym = symbol.upper()
        prox = proxies_for(sym)
        cutoff = asof - timedelta(days=WINDOW_DAYS)

        exact = [t for t in self._trades
                 if t.symbol.upper() == sym and t.is_loss_sale and cutoff <= t.on <= asof]
        if exact:
            clears = max(t.on for t in exact) + timedelta(days=WINDOW_DAYS + 1)
            accts = sorted({t.account for t in exact})
            return Verdict(
                False, "block",
                f"{sym} was sold at a loss on "
                f"{', '.join(sorted(t.on.isoformat() for t in exact))} "
                f"in the {' and '.join(accts)} account"
                f"{'s' if len(accts) > 1 else ''}; buying now disallows that loss",
                clears, tuple(exact))

        near = [t for t in self._trades
                if t.symbol.upper() in prox and t.is_loss_sale and cutoff <= t.on <= asof]
        if near:
            clears = max(t.on for t in near) + timedelta(days=WINDOW_DAYS + 1)
            names = sorted({t.symbol.upper() for t in near})
            return Verdict(
                False, "warn",
                f"{sym} closely tracks {', '.join(names)}, sold at a loss inside the "
                f"window. No ruling settles whether these are substantially "
                f"identical, so this is treated as unsafe rather than clean",
                clears, tuple(near))

        return Verdict(True, "ok", f"no loss sale of {sym} or a close proxy in the last "
                                   f"{WINDOW_DAYS} days")

    # --------------------------------------------------------------- sells

    def check_loss_sale(self, symbol: str, asof: date) -> Verdict:
        """If we realise a loss on `symbol` today, does an earlier purchase wash it?

        This is the direction people forget: buying inside the thirty days
        BEFORE a loss sale triggers the rule just as buying after does.
        """
        sym = symbol.upper()
        cutoff = asof - timedelta(days=WINDOW_DAYS)
        buys = [t for t in self._trades
                if t.symbol.upper() == sym and t.side == "buy" and cutoff <= t.on <= asof]
        if buys:
            qty = sum(t.quantity for t in buys)
            accts = sorted({t.account for t in buys})
            return Verdict(
                False, "warn",
                f"{qty:g} share(s) of {sym} were bought on "
                f"{', '.join(sorted(t.on.isoformat() for t in buys))} in the "
                f"{' and '.join(accts)} account{'s' if len(accts) > 1 else ''}; "
                f"realising a loss now washes against those shares. "
                f"Selling the whole position and staying out {WINDOW_DAYS + 1} days "
                f"is the clean path",
                max(t.on for t in buys) + timedelta(days=WINDOW_DAYS + 1), tuple(buys))
        return Verdict(True, "ok", f"no purchase of {sym} in the last {WINDOW_DAYS} days")

    # ------------------------------------------------------------ reporting

    def blocked_symbols(self, asof: date) -> dict:
        """Everything currently unbuyable, for the email's rejected list."""
        out = {}
        for t in self._trades:
            if not t.is_loss_sale:
                continue
            if not (asof - timedelta(days=WINDOW_DAYS) <= t.on <= asof):
                continue
            for s in {t.symbol.upper()} | proxies_for(t.symbol.upper()):
                v = self.check_buy(s, asof)
                if not v.allowed:
                    prev = out.get(s)
                    if prev is None or (v.clears_on and v.clears_on > prev["clears_on"]):
                        out[s] = {"severity": v.severity, "reason": v.reason,
                                  "clears_on": v.clears_on}
        return out


def seed_from_positions(individual: Sequence[dict], agentic: Sequence[dict]) -> list[str]:
    """Names currently held at a loss in either account.

    These are not blocks — nothing has been sold. They are the watchlist: the
    moment any of them is sold, a thirty-one day block opens in BOTH accounts.
    """
    out = []
    for rows, acct in ((individual, "individual"), (agentic, "agentic")):
        for r in rows:
            if float(r["last"]) < float(r["avg"]):
                out.append(f"{r['symbol'].upper()} ({acct})")
    return out
