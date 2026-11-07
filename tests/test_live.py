"""Live-account behaviour with fake alerts and a fake broker (no internet)."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hunter  # noqa: E402
from extract import extract_terms  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


class FakeAlerts:
    def __init__(self):
        self.opened, self.closed_refs, self.comments, self.n = [], set(), [], 0

    def open(self, title, body, labels=()):
        self.n += 1
        self.opened.append((str(self.n), title, labels))
        return str(self.n)

    def close(self, ref, comment=None):
        self.closed_refs.add(ref)

    def comment(self, ref, body):
        self.comments.append((ref, body))

    def is_closed(self, ref):
        return ref in self.closed_refs


class FakeBroker:
    name = "FakeBroker"
    real_account = True

    def __init__(self):
        self.orders = []

    def buy(self, t, n, px):
        self.orders.append(("buy", t, n))
        return f"o{len(self.orders)}"

    def sell(self, t, n, px):
        self.orders.append(("sell", t, n))

    def reconcile(self, led):
        return []

    def sweep(self, led, cfg):
        pass


def cfg(tmp):
    c = hunter.load_cfg()
    c.update(ai_review=False, data_dir=str(Path(tmp) / "data"), report_dir=str(Path(tmp) / "reports"))
    return c


def test_paper_deals_send_info_alerts():
    with tempfile.TemporaryDirectory() as tmp:
        al = FakeAlerts()
        led, _ = hunter.run(cfg(tmp), offline=str(FIX), today=dt.date(2026, 11, 9), hooks={"alerts": al, "broker": None})
        titles = [t for _, t, _ in al.opened]
        assert any("Paper deal" in t and "ALDR" in t for t in titles), titles
        assert all(r in al.closed_refs for r, _, _ in al.opened)  # info only, auto-closed
        assert (Path(tmp) / "reports" / "trades.csv").exists()


def test_real_account_tender_must_be_confirmed():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        al, br = FakeAlerts(), FakeBroker()
        hooks = {"alerts": al, "broker": br}
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 9), hooks=hooks)
        assert ("buy", "ALDR", 99) in br.orders
        actions = {t.split()[3]: r for r, t, labels in al.opened if "action-needed" in labels}
        assert set(actions) >= {"ALDR", "BRCH"}, actions
        # Person confirms ALDR (closes the issue) but never confirms FIRP.
        al.closed_refs.add(actions["ALDR"])
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 10), hooks=hooks)
        aldr = [p for p in led.open_positions() if p["ticker"] == "ALDR"][0]
        assert aldr["tender_status"] == "confirmed"
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 12, 2), hooks=hooks)   # 2 days before FIRP ends
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 12, 11), hooks=hooks)
        closed = {p["ticker"]: p for p in led.closed_positions()}
        assert closed["ALDR"]["status"] == "settled"
        assert closed["FIRP"]["status"] == "not tendered"
        assert ("sell", "FIRP", closed["FIRP"]["shares"]) in br.orders
        assert any("Reminder" in b for _, b in al.comments)


def test_ownership_cutoff():
    t = extract_terms("ODD LOTS. Odd Lots shall mean all Shares tendered by any person who owned beneficially as of the "
                      "close of business on September 21, 2026, and continues to own as of the Expiration Date, fewer than 100 Shares.")
    assert t["ownership_cutoff"] == dt.date(2026, 9, 21)
    assert extract_terms("There were 5,000,000 shares outstanding as of the close of business on May 1, 2026.")["ownership_cutoff"] is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
