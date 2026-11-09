"""Offline tests: python -m pytest tests  (or: python tests/test_hunter.py)"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hunter  # noqa: E402
from extract import extract_terms  # noqa: E402
from ledger import Ledger  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def cfg(tmp):
    c = hunter.load_cfg()
    c.update(ai_review=False, data_dir=str(Path(tmp) / "data"), report_dir=str(Path(tmp) / "reports"))
    return c


def test_extract_fixed_and_dutch():
    a = extract_terms((FIX / "alder.txt").read_text())
    assert a["price"] == 12.50 and a["odd_lot_priority"] and a["expiration"] == dt.date(2026, 11, 30)
    b = extract_terms((FIX / "birch.txt").read_text())
    assert b["dutch_auction"] and b["price"] == 20.0 and b["price_high"] == 23.0
    assert b["expiration"] == dt.date(2026, 12, 2) and b["odd_lot_priority"]
    assert not extract_terms((FIX / "cedar.txt").read_text())["odd_lot_priority"]
    assert extract_terms((FIX / "dogwood.txt").read_text())["financing_condition"]
    assert not extract_terms((FIX / "elm.txt").read_text())["cash_offer"]
    assert extract_terms((FIX / "update_BRCH.txt").read_text())["terminated"]
    assert extract_terms((FIX / "update_ALDR.txt").read_text())["final_results"]


def test_real_world_wording():
    """Sentences taken from real 2025-26 filings that fooled v2."""
    e = extract_terms
    assert not e("The Offer is not conditioned upon any minimum number of shares being tendered, nor subject to any financing condition.")["financing_condition"]
    assert not e("THE OFFERS ARE NOT CONDITIONED ON THE RECEIPT OF FINANCING OR ANY MINIMUM NUMBER OF SHARES BEING TENDERED.")["financing_condition"]
    assert e("The Offer is not conditioned on any minimum number of Shares being tendered. The Offer, however, is conditioned on the receipt of financing.")["financing_condition"]
    assert not e("This Offer will not have any special pro ration provision for odd-lot tenders, which means that all odd-lot tenders "
                 "(including stockholders who own fewer than 100 shares) are subject to pro ration and will not receive priority.")["odd_lot_priority"]
    assert not e("pro rata basis (odd-lot tenders for stockholders who own fewer than 100 shares are still subject to pro ration)")["odd_lot_priority"]
    assert e("Shares tendered will be subject to proration, except for Odd Lots as described below. "
             "Odd Lots will not be subject to proration.")["odd_lot_priority"]
    assert e("we will purchase shares (a) first, from any eligible shareholder who owns not more than 99 Shares (Odd lot Shareholders)")["odd_lot_priority"]
    t = e("a price not less than US$2.80 nor greater than US$3.20 per ADS")
    assert t["dutch_auction"] and t["price"] == 2.80
    assert 37.25 in e("Scholastic will select the lowest purchase price (in increments of $0.50). On March 20, 2026, the last "
                      "reported sale price of the Shares was $37.25 per Share.")["stated_prices"]


def test_worst_two_stress_test():
    """Worst-2 sizing: many mid-size deals allowed, but the two worst failures never exceed the allowance."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        c["stress_worst_deals"] = 2
        led = Ledger(Path(tmp) / "t.db", c, today=dt.date(2026, 11, 9))
        bought = 0
        for i in range(10):
            room = led.max_new_position(0.35)
            size = min(room, 2800.0)
            if size < 100:
                break
            led.buy({"ticker": f"T{i}", "company": "", "accession": "", "url": "", "payout": 1.0,
                     "expiration": dt.date(2026, 12, 1), "dutch": False, "drop": 0.35}, 1, size)
            bought += 1
        losses = sorted((p["cost"] * 0.35 for p in led.open_positions()), reverse=True)
        assert sum(losses[:2]) <= 2000 + 1e-6
        assert bought >= 3, bought                       # old all-fail rule allowed only ~$5,700 in total
        assert sum(p["cost"] for p in led.open_positions()) > 5714
        # With the old rule (every deal fails at once) the same book would breach the floor:
        c2 = dict(c, stress_worst_deals=None)
        led.cfg = c2
        assert led.stressed_value() < led.get("floor")


def test_floor_rule_math():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        led = Ledger(Path(tmp) / "t.db", c, today=dt.date(2026, 11, 9))
        assert led.get("floor") == 8000
        # With $10k cash, $8k floor, 25% assumed drop: max purchase is $8,000.
        assert abs(led.max_new_position() - 8000) < 1e-6


def test_full_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        day0 = dt.date(2026, 11, 9)
        led, eq = hunter.run(c, offline=str(FIX), today=day0)
        held = {p["ticker"]: p for p in led.open_positions()}
        seen = {r["reason"].split()[0]: r["decision"] for r in led.recent_seen(50)}
        assert set(held) == {"ALDR", "BRCH", "FIRP"}, held.keys()
        assert seen["CEDR"] == "skip" and seen["DOGW"] == "skip" and seen["ELMX"] == "skip" and seen["GUMT"] == "skip"
        assert held["ALDR"]["shares"] == 99 and held["BRCH"]["shares"] == 99
        # FIRP is limited by the floor rule, not by the 99-share cap.
        assert held["FIRP"]["shares"] < 99
        assert led.stressed_value() >= led.get("floor") - 1e-6
        # Worst-2 stress test: the two worst failures together stay within the $2,000 allowance
        losses = sorted((p["cost"] * 0.25 for p in led.open_positions()), reverse=True)
        assert sum(losses[:2]) <= 2000 + 1e-6

        # Two weeks later: BRCH terminates and its price falls; ALDR has final results.
        src_prices = json.loads((FIX / "prices.json").read_text())
        led2, eq2 = hunter.run(c, offline=str(FIX), today=dt.date(2026, 12, 7))
        closed = {p["ticker"]: p for p in led2.closed_positions()}
        assert closed["ALDR"]["status"] == "settled"
        assert abs(closed["ALDR"]["pnl"] - 99 * (12.50 - src_prices["ALDR"])) < 0.01
        assert closed["BRCH"]["status"] == "failed"
        assert "FIRP" not in closed  # expired Dec 4; waits 5 days for results
        assert led2.get("interest_earned") > 0

        # Friday Dec 11: FIRP settles, and the Ratchet lifts the floor by half the week's profit.
        led3, eq3 = hunter.run(c, offline=str(FIX), today=dt.date(2026, 12, 11))
        closed = {p["ticker"]: p for p in led3.closed_positions()}
        assert closed["FIRP"]["status"] == "settled"
        realized = sum(p["pnl"] for p in closed.values())
        assert led3.get("floor") > 8000
        assert abs(led3.get("floor") - (8000 + 0.5 * realized)) < 0.01
        assert not led3.open_positions()
        print(f"   end equity ${eq3:,.2f}, floor ${led3.get('floor'):,.2f}, realized ${realized:+,.2f}")


def test_failed_deal_takes_a_loss_but_floor_holds():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 9))
        orig = hunter.OfflineSources.price
        hunter.OfflineSources.price = lambda self, t: 14.25 if t == "BRCH" else orig(self, t)  # -25% after termination
        try:
            led, eq = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 16))
        finally:
            hunter.OfflineSources.price = orig
        b = [p for p in led.closed_positions() if p["ticker"] == "BRCH"][0]
        assert b["status"] == "failed" and b["pnl"] < 0
        assert led.stressed_value() >= led.get("floor") - 1e-6


def test_edgar_hit_parsing():
    from edgar import parse_hit
    hit = {"_id": "0001193125-26-245678:d812345dsctoi.htm",
           "_source": {"ciks": ["0000320193"], "display_names": ["Acme Widgets, Inc.  (ACME)  (CIK 0000320193)"],
                       "form": "SC TO-I", "file_date": "2026-11-02"}}
    r = parse_hit(hit)
    assert r["ticker"] == "ACME" and r["company"] == "Acme Widgets, Inc." and r["cik"] == "320193"
    assert r["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000119312526245678/d812345dsctoi.htm"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
