"""Shareholder-vote cash merger engine (no internet)."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hunter  # noqa: E402
import merger  # noqa: E402

PROXY = ("Each share of Company common stock will be converted into the right to receive $45.00 in cash, without interest "
         "(the merger consideration). The special meeting will be held on December 10, 2026, at 9:00 a.m. Eastern Time. "
         "The board of directors unanimously recommends that you vote FOR the merger proposal. "
         "We expect to complete the merger in the first quarter of 2027. The merger is not subject to a financing condition. "
         "Holders will receive $45.00 in cash, without interest.")
MIXED = ("Each share will be converted into the right to receive $20.00 in cash and 0.25 shares of Parent common stock. "
         "The board recommends that you vote FOR the merger proposal.")


def test_reading_merger_terms():
    t = merger.terms(PROXY, dt.date(2026, 11, 2))
    assert t["price"] == 45.00 and t["board_recommends"] and not t["stock_component"]
    assert t["meeting"] == dt.date(2026, 12, 10) and t["expected_close"] == dt.date(2027, 3, 31)
    assert not t["financing_condition"]
    assert merger.terms(MIXED, dt.date(2026, 11, 2))["stock_component"]
    assert merger.update_status("On March 3, 2027, the Company completed its previously announced merger with Parent.") == "completed"
    assert merger.update_status("The Company terminated the merger agreement with Parent.") == "terminated"
    assert merger.update_status("At the special meeting, stockholders adopted the merger agreement.") == "approved"


def _folder(tmp, final_text):
    f = Path(tmp) / "fx"
    f.mkdir()
    (f / "candidates.json").write_text(json.dumps([
        {"doc_id": "M:proxy.htm", "accession": "0000000009-26-000009", "form": "DEFM14A", "file_date": "2026-11-02",
         "company": "Merger Target Inc", "ticker": "MTGT", "cik": "99", "engine": "merger",
         "url": "https://www.sec.gov/Archives/edgar/data/99/x/proxy.htm", "file": "proxy.txt"},
        {"doc_id": "N:proxy.htm", "accession": "0000000010-26-000010", "form": "DEFM14A", "file_date": "2026-11-02",
         "company": "Mixed Pay Corp", "ticker": "MIXD", "cik": "98", "engine": "merger",
         "url": "https://www.sec.gov/Archives/edgar/data/98/x/proxy.htm", "file": "mixed.txt"}]))
    (f / "proxy.txt").write_text(PROXY)
    (f / "mixed.txt").write_text(MIXED)
    (f / "prices.json").write_text(json.dumps({"MTGT": 43.00, "MIXD": 30.0}))
    if final_text:
        (f / "update_MTGT.txt").write_text(final_text)
    return f


def test_merger_buy_and_settle():
    with tempfile.TemporaryDirectory() as tmp:
        c = hunter.load_cfg()
        c.update(ai_review=False, data_dir=str(Path(tmp) / "data"), report_dir=str(Path(tmp) / "reports"),
                 engines={"odd_lot": False, "takeover": False, "merger": True})
        f = _folder(tmp, None)
        led, _ = hunter.run(c, offline=str(f), today=dt.date(2026, 11, 9), hooks={"alerts": None, "broker": None})
        held = {p["ticker"]: p for p in led.open_positions()}
        assert set(held) == {"MTGT"}, held
        m = held["MTGT"]
        assert m["cost"] <= 4000 and m["shares"] > 99 and m["expiration"] == "2027-03-31"
        assert m["tender_status"] in ("assumed", "not needed")
        assert led.stressed_value() >= led.get("floor") - 1e-6
        (f / "update_MTGT.txt").write_text("On March 3, 2027, the Company completed its previously announced merger with Parent.")
        led, _ = hunter.run(c, offline=str(f), today=dt.date(2027, 3, 5), hooks={"alerts": None, "broker": None})
        done = {p["ticker"]: p for p in led.closed_positions()}["MTGT"]
        assert done["status"] == "settled" and abs(done["proceeds"] - done["shares"] * 45.00) < 0.01
        print(f"   merger: {done['shares']} MTGT, P&L {done['pnl']:+.2f}")


def test_merger_termination_sells():
    with tempfile.TemporaryDirectory() as tmp:
        c = hunter.load_cfg()
        c.update(ai_review=False, data_dir=str(Path(tmp) / "data"), report_dir=str(Path(tmp) / "reports"),
                 engines={"odd_lot": False, "takeover": False, "merger": True})
        f = _folder(tmp, None)
        hunter.run(c, offline=str(f), today=dt.date(2026, 11, 9), hooks={"alerts": None, "broker": None})
        (f / "update_MTGT.txt").write_text("The Company terminated the merger agreement with Parent after regulators objected.")
        (f / "prices.json").write_text(json.dumps({"MTGT": 30.00, "MIXD": 30.0}))
        led, eq = hunter.run(c, offline=str(f), today=dt.date(2026, 12, 1), hooks={"alerts": None, "broker": None})
        d = {p["ticker"]: p for p in led.closed_positions()}["MTGT"]
        assert d["status"] == "failed" and d["pnl"] < 0
        assert eq >= led.get("floor") - 1e-6   # a 32% drop is inside the 35% assumption


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
