"""Rocket tests: pricing sanity, sizing, the floor rule, and the backtest maths on made-up data."""
import datetime as dt
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hunter  # noqa: E402
import rocket  # noqa: E402
import rocket_backtest as rb  # noqa: E402
from ledger import Ledger  # noqa: E402


def cfg(tmp):
    c = hunter.load_cfg()
    c.update(ai_review=False, data_dir=str(Path(tmp) / "data"), report_dir=str(Path(tmp) / "reports"))
    return c


def test_black_scholes_known_value():
    # textbook case: S=K=100, T=1, r=5%, q=0, vol 20% -> 10.45
    assert abs(rocket.bs_call(100, 100, 1, 0.05, 0, 0.2) - 10.4506) < 0.001
    assert rocket.bs_call(100, 80, 0, 0.05, 0, 0.2) == 20


def test_whole_contract_fits_budget():
    c = {"rocket_mode": "whole", "rocket_strike_pct": 100, "rocket_max_strike_pct": 130}
    pick = rocket.choose(700, 15, 4.0, 0.012, 2000, c)
    assert pick and pick["contracts"] == 1 and pick["cost"] <= 2000
    assert pick["strike"] > 700                        # $2,000 can't buy an at-the-money SPY contract
    frac = rocket.choose(700, 15, 4.0, 0.012, 2000, dict(c, rocket_mode="fractional"))
    assert abs(frac["cost"] - 2000) < 0.01 and abs(frac["strike"] - 700) < 1e-9


class FakeSrc:
    def __init__(self, spy=700.0, vix=15.0):
        self.p = {"SPY": spy, "^VIX": vix, "^IRX": 4.0}

    def price(self, t):
        return self.p.get(t)


def test_rocket_respects_floor_and_settles():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        c["rocket_budget"] = 2000.0
        led = Ledger(Path(tmp) / "r.db", c, today=dt.date(2026, 1, 5))
        src, notes = FakeSrc(), []
        rocket.hunt(led, src, c, notes)
        pos = [p for p in led.open_positions() if p["engine"] == "rocket"]
        assert len(pos) == 1 and pos[0]["cost"] <= 2000
        assert led.stressed_value() >= led.get("floor") - 1e-6   # a total loss still leaves $8,000
        rocket.hunt(led, src, c, notes)                         # only one at a time
        assert len([p for p in led.open_positions() if p["engine"] == "rocket"]) == 1
        assert led.max_new_position() <= (2000 - pos[0]["cost"]) / 0.25 + 0.01   # only the leftover is free
        led.today = dt.date(2027, 1, 6)
        src.p["SPY"] = 840.0                                    # S&P +20%
        rocket.settle(led, src, c, notes)
        closed = [p for p in led.closed_positions() if p["engine"] == "rocket"][0]
        k = json_strike(closed)
        assert abs(closed["proceeds"] - closed["shares"] * (840 - k)) < 0.01 and closed["pnl"] > 0


def json_strike(p):
    import json
    return json.loads(p["deal_json"])["strike"]


def test_rocket_waits_when_deals_use_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        led = Ledger(Path(tmp) / "r.db", c, today=dt.date(2026, 1, 5))
        deal = {"ticker": "BIG", "company": "x", "accession": "a", "url": "", "payout": 10, "expiration": dt.date(2026, 3, 1),
                "dutch": False, "engine": "takeover", "drop": 0.5}
        led.buy(deal, 380, 9.5)       # $3,610 that could lose half = $1,805 of the $2,000 budget
        notes = []
        rocket.hunt(led, FakeSrc(), c, notes)
        assert not [p for p in led.open_positions() if p["engine"] == "rocket"]
        assert any("waiting" in n for n in notes)


def _fake_series(growth):
    """SPY growing `growth` a year, steady VIX 16 and T-bills 4%, 1% dividends, from 1993."""
    spy, vix, irx = {}, {}, {}
    d = dt.date(1993, 2, 1)
    while d < dt.date(2026, 11, 1):
        if d.weekday() < 5:
            t = (d - dt.date(1993, 2, 1)).days / 365
            spy[d.isoformat()] = (100 * (1 + growth) ** t, 100 * (1 + growth + 0.01) ** t)
            vix[d.isoformat()] = (16.0, 16.0)
            irx[d.isoformat()] = (4.0, 4.0)
        d += dt.timedelta(days=1)
    return rb.Series(spy, vix, irx)


def test_backtest_never_breaks_the_floor():
    for g in (-0.3, 0.0, 0.1, 0.25):
        ser = _fake_series(g)
        latest = ser.spy[ser.days[-1]][0]
        for m in ("whole", "fractional"):
            rows = rb.chain(ser, dt.date(2000, 1, 3), 10, rb.mode_cfg(m, ser, dt.date(2000, 1, 3), latest))
            for r in rows:
                assert r["equity1"] >= 8000 - 1e-6
                assert r["floor1"] >= 8000 and r["floor1"] <= r["equity1"] + 1e-6
            if g <= 0:
                assert rows[0]["ret"] < 0.05                     # flat or falling market: options expire worthless
            if g >= 0.25 and m == "fractional":
                assert rb.cagr(rows, "ret") > rb.cagr(rows, "spx") - 0.05


def test_hunter_run_with_rocket_offline():
    """Full offline run: the rocket must not break anything when no SPY price exists."""
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        led, eq = hunter.run(c, offline=str(ROOT / "tests" / "fixtures"), today=dt.date(2026, 11, 2),
                             hooks={"alerts": None, "broker": None})
        assert eq > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
