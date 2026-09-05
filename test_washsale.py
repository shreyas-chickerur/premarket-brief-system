"""Tests for the cross-account wash sale registry."""

from datetime import date, timedelta
import pytest

import washsale as W

TODAY = date(2026, 9, 15)


def loss_sale(sym, acct, on, qty=1.0, pnl=-100.0):
    return W.Trade(sym, acct, on, "sell", qty, pnl)


def gain_sale(sym, acct, on, qty=1.0, pnl=250.0):
    return W.Trade(sym, acct, on, "sell", qty, pnl)


def buy(sym, acct, on, qty=1.0):
    return W.Trade(sym, acct, on, "buy", qty)


# ---------------------------------------------------------------- the core case

def test_cross_account_block_is_the_whole_point():
    """Loss sale in the individual account blocks a buy in the agentic account.
    This is the case the broker will not report and the tax form will not show."""
    r = W.Registry([loss_sale("NFLX", "individual", TODAY - timedelta(days=5))])
    v = r.check_buy("NFLX", TODAY)
    assert not v.allowed and v.severity == "block"
    assert "individual" in v.reason
    assert v.clears_on == TODAY - timedelta(days=5) + timedelta(days=31)


def test_block_works_in_the_other_direction_too():
    r = W.Registry([loss_sale("META", "agentic", TODAY - timedelta(days=1))])
    assert not r.check_buy("META", TODAY).allowed


def test_same_account_still_blocked():
    r = W.Registry([loss_sale("IBM", "individual", TODAY - timedelta(days=10))])
    assert not r.check_buy("IBM", TODAY).allowed


# ---------------------------------------------------------------- the window

@pytest.mark.parametrize("days_ago,blocked", [
    (0, True), (1, True), (15, True), (29, True), (30, True),
    (31, False), (45, False), (400, False),
])
def test_thirty_day_window_boundary(days_ago, blocked):
    r = W.Registry([loss_sale("SPOT", "individual", TODAY - timedelta(days=days_ago))])
    assert r.check_buy("SPOT", TODAY).allowed is (not blocked)


def test_block_clears_the_day_after_the_window():
    sold = TODAY - timedelta(days=30)
    r = W.Registry([loss_sale("WMT", "agentic", sold)])
    v = r.check_buy("WMT", TODAY)
    assert not v.allowed
    assert r.check_buy("WMT", v.clears_on).allowed


def test_multiple_loss_sales_use_the_latest_clear_date():
    r = W.Registry([loss_sale("MU", "individual", TODAY - timedelta(days=25)),
                    loss_sale("MU", "agentic", TODAY - timedelta(days=3))])
    v = r.check_buy("MU", TODAY)
    assert v.clears_on == TODAY - timedelta(days=3) + timedelta(days=31)


# ---------------------------------------------------------------- what is NOT blocked

def test_a_gain_sale_never_blocks():
    r = W.Registry([gain_sale("NVDA", "individual", TODAY - timedelta(days=2))])
    assert r.check_buy("NVDA", TODAY).allowed


def test_a_prior_buy_does_not_block_another_buy():
    r = W.Registry([buy("AAPL", "agentic", TODAY - timedelta(days=2))])
    assert r.check_buy("AAPL", TODAY).allowed


def test_unrelated_symbol_is_free():
    r = W.Registry([loss_sale("NFLX", "individual", TODAY - timedelta(days=2))])
    assert r.check_buy("AMZN", TODAY).allowed


def test_empty_registry_allows_everything():
    assert W.Registry().check_buy("VTI", TODAY).allowed


# ---------------------------------------------------------------- proxies

def test_close_proxy_warns_rather_than_blocking_silently():
    """Two funds tracking one index is legally unsettled. Warn, never wave through."""
    r = W.Registry([loss_sale("VOO", "individual", TODAY - timedelta(days=4))])
    v = r.check_buy("SPY", TODAY)
    assert not v.allowed and v.severity == "warn"
    assert "substantially" in v.reason


def test_exact_match_outranks_proxy_match():
    r = W.Registry([loss_sale("SPY", "individual", TODAY - timedelta(days=4)),
                    loss_sale("VOO", "agentic", TODAY - timedelta(days=4))])
    assert r.check_buy("SPY", TODAY).severity == "block"


def test_the_users_actual_overlap_sgov():
    """Both accounts hold SGOV today. This is the live exposure, not a hypothetical."""
    r = W.Registry([loss_sale("SGOV", "individual", TODAY - timedelta(days=7))])
    assert not r.check_buy("SGOV", TODAY).allowed
    assert r.check_buy("BIL", TODAY).severity == "warn"


def test_proxy_lookup_is_symmetric_and_excludes_self():
    assert "GLD" in W.proxies_for("GLDM")
    assert "GLDM" in W.proxies_for("GLD")
    assert "GLDM" not in W.proxies_for("GLDM")
    assert W.proxies_for("NFLX") == frozenset()


def test_symbols_are_case_insensitive():
    r = W.Registry([loss_sale("nflx", "individual", TODAY - timedelta(days=3))])
    assert not r.check_buy("NFLX", TODAY).allowed
    assert not r.check_buy("nFlX", TODAY).allowed


# ---------------------------------------------------------------- the reverse direction

def test_buying_before_a_loss_sale_also_washes_it():
    """The direction people forget: a purchase inside the 30 days BEFORE the
    loss sale triggers the rule just as a purchase after does."""
    r = W.Registry([buy("META", "agentic", TODAY - timedelta(days=10), qty=3)])
    v = r.check_loss_sale("META", TODAY)
    assert not v.allowed and v.severity == "warn"
    assert "3 share" in v.reason and "agentic" in v.reason


def test_old_purchase_does_not_wash_a_loss_sale():
    r = W.Registry([buy("META", "individual", TODAY - timedelta(days=90))])
    assert r.check_loss_sale("META", TODAY).allowed


def test_loss_sale_check_spans_accounts():
    r = W.Registry([buy("IBM", "individual", TODAY - timedelta(days=5)),
                    buy("IBM", "agentic", TODAY - timedelta(days=6))])
    v = r.check_loss_sale("IBM", TODAY)
    assert "agentic and individual" in v.reason or "individual and agentic" in v.reason


# ---------------------------------------------------------------- reporting

def test_blocked_symbols_lists_the_block_and_its_proxies():
    r = W.Registry([loss_sale("VTI", "individual", TODAY - timedelta(days=2))])
    b = r.blocked_symbols(TODAY)
    assert b["VTI"]["severity"] == "block"
    assert b["ITOT"]["severity"] == "warn"
    assert b["VTI"]["clears_on"] == TODAY - timedelta(days=2) + timedelta(days=31)


def test_blocked_symbols_empty_when_clean():
    r = W.Registry([gain_sale("NVDA", "agentic", TODAY - timedelta(days=1))])
    assert r.blocked_symbols(TODAY) == {}


def test_verdict_serialises():
    r = W.Registry([loss_sale("NFLX", "individual", TODAY - timedelta(days=5))])
    d = r.check_buy("NFLX", TODAY).to_dict()
    assert d["allowed"] is False and d["clears_on"] == "2026-10-11"
    assert d["triggering"][0]["symbol"] == "NFLX"


# ---------------------------------------------------------------- input contract

def test_a_sell_without_realized_pnl_is_rejected():
    with pytest.raises(ValueError):
        W.Trade("NFLX", "individual", TODAY, "sell", 1.0)


def test_bad_side_and_quantity_rejected():
    with pytest.raises(ValueError):
        W.Trade("NFLX", "individual", TODAY, "short", 1.0, -5.0)
    with pytest.raises(ValueError):
        W.Trade("NFLX", "individual", TODAY, "buy", 0)


def test_zero_pnl_sale_is_not_a_loss():
    r = W.Registry([W.Trade("SGOV", "agentic", TODAY - timedelta(days=1), "sell", 1.0, 0.0)])
    assert r.check_buy("SGOV", TODAY).allowed


# ---------------------------------------------------------------- seeding

def test_seed_finds_positions_currently_underwater():
    ind = [{"symbol": "NFLX", "avg": 100.25, "last": 81.73},
           {"symbol": "NVDA", "avg": 181.65, "last": 217.54}]
    ag = [{"symbol": "GLDM", "avg": 91.77, "last": 88.19}]
    seeded = W.seed_from_positions(ind, ag)
    assert "NFLX (individual)" in seeded
    assert "GLDM (agentic)" in seeded
    assert not any("NVDA" in s for s in seeded)


# --------------------------------------------------------- registry.report()

def test_report_matches_blocked_symbols_exactly():
    """`report()` must be a pure re-shaping of `blocked_symbols()` -- same
    symbols, same severities, same reasons, dates as ISO strings instead of
    `date` objects. Regression case: 5 September 2026, two runs against the
    identical 870 fills wrote wash-sale journal notes seven names apart
    (CMG, CRM, GLDM, MRVL, MU, TSLA, XLE vs. just GLDM, XLE) because the
    note was hand-composed prose, not this method's output. All five of the
    symbols the narrower note dropped -- CMG, CRM, MRVL, MU, TSLA -- were
    real, unexpired loss sales the registry itself never stopped blocking;
    only the retelling of it varied. This pins `report()` so a future
    caller has no hand-composed step left to vary."""
    r = W.Registry([
        loss_sale("CMG", "individual", TODAY - timedelta(days=19)),
        loss_sale("CRM", "individual", TODAY - timedelta(days=22)),
        loss_sale("GLDM", "agentic", TODAY - timedelta(days=19)),
        loss_sale("MRVL", "individual", TODAY - timedelta(days=22)),
        loss_sale("MU", "individual", TODAY - timedelta(days=19)),
        loss_sale("TSLA", "individual", TODAY - timedelta(days=20)),
        loss_sale("XLE", "agentic", TODAY - timedelta(days=19)),
    ])
    blocked = r.blocked_symbols(TODAY)
    report = r.report(TODAY)

    assert report["asof"] == TODAY.isoformat()
    assert set(report["blocked"]) == set(blocked)
    for sym, v in blocked.items():
        assert report["blocked"][sym]["severity"] == v["severity"]
        assert report["blocked"][sym]["reason"] == v["reason"]
        assert report["blocked"][sym]["clears_on"] == v["clears_on"].isoformat()
    # every block-severity symbol from the regression's narrower note is present
    for sym in ("CMG", "CRM", "MRVL", "MU", "TSLA", "GLDM", "XLE"):
        assert report["blocked"][sym]["severity"] == "block"


def test_report_empty_registry():
    r = W.Registry([])
    assert r.report(TODAY) == {"asof": TODAY.isoformat(), "blocked": {}}
