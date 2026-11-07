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
        actions = {t.split()[-1]: r for r, t, labels in al.opened if "action-needed" in labels}
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


class FakeResp:
    def __init__(self, data, code=200):
        self.data, self.status_code, self.content = data, code, b"x"

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeAlpaca:
    """Pretends to be Alpaca's API."""
    def __init__(self):
        self.headers, self.orders, self.calls = {}, {}, []

    def request(self, method, url, json=None, timeout=None):
        path = url.split(".markets")[1]
        self.calls.append((method, path, json))
        if method == "POST" and path == "/v2/orders":
            oid = f"id{len(self.orders) + 1}"
            self.orders[oid] = {"status": "filled", "filled_avg_price": str(float(json["limit_price"]) - 0.05),
                                "filled_qty": json["qty"], "symbol": json["symbol"]}
            return FakeResp({"id": oid})
        if method == "GET" and path.startswith("/v2/orders/"):
            return FakeResp(self.orders[path.rsplit("/", 1)[1]])
        if path == "/v2/positions":
            return FakeResp([{"symbol": o["symbol"], "qty": o["filled_qty"]} for o in self.orders.values()])
        if path == "/v2/account":
            return FakeResp({"cash": "1000"})
        return FakeResp({})


def test_alpaca_orders_and_reconcile():
    import os
    import broker
    os.environ.update(ALPACA_KEY_ID="k", ALPACA_SECRET_KEY="s")
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        c["cash_fund"] = ""
        fake = FakeAlpaca()
        b = broker.AlpacaBroker(c, live=False, session=fake)
        assert b.base.startswith("https://paper-api")
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 9), hooks={"alerts": FakeAlerts(), "broker": b})
        posts = [j for m, p, j in fake.calls if m == "POST"]
        assert any(j["symbol"] == "ALDR" and j["qty"] == "99" and j["side"] == "buy" for j in posts)
        notes = b.reconcile(led)
        aldr = [p for p in led.open_positions() if p["ticker"] == "ALDR"][0]
        assert abs(aldr["price_paid"] - (round(11.20 * 1.01, 2) - 0.05)) < 1e-6, aldr["price_paid"]
        assert any("filled" in n for n in notes)


def test_real_money_is_locked():
    import os
    import broker
    os.environ.update(ALPACA_KEY_ID="k", ALPACA_SECRET_KEY="s")
    os.environ.pop("ALLOW_REAL_MONEY", None)
    try:
        broker.AlpacaBroker({"max_single_deal_cost": 10000}, live=True, session=FakeAlpaca())
        raise AssertionError("live trading should be locked")
    except RuntimeError as e:
        assert "locked" in str(e)


def test_assisted_mode_sends_the_order():
    import broker
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        al = FakeAlerts()
        bodies = []
        orig = al.open
        al.open = lambda title, body, labels=(): (bodies.append(body), orig(title, body, labels))[1]
        hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 9), hooks={"alerts": al, "broker": broker.AssistedBroker(c)})
        b = [x for x in bodies if "ALDR" in x][0]
        assert "BUY 99 ALDR, limit $11.31" in b and "noon Eastern on 2026-11-27" in b, b[:400]


def test_tender_fee_blocks_small_deals():
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        c["tender_fee"] = 100.0   # Alpaca's fee
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 9), hooks={"alerts": None, "broker": None})
        held = {p["ticker"] for p in led.open_positions()}
        assert "BRCH" not in held       # $99 gross < $100 fee
        assert "ALDR" in held or "FIRP" in held


def test_weekly_letter_and_monthly_review():
    import os
    os.environ.pop("ANTHROPIC_API_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        c = cfg(tmp)
        al = FakeAlerts()
        hooks = {"alerts": al, "broker": None}
        hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 2), hooks=hooks)   # Monday, early in month: review
        led, _ = hunter.run(c, offline=str(FIX), today=dt.date(2026, 11, 6), hooks=hooks)  # Friday: letter
        titles = [t for _, t, _ in al.opened]
        assert any(t.startswith("Monthly review") for t in titles), titles
        assert any(t.startswith("Weekly letter") for t in titles), titles
        assert "account is worth" in led.get("latest_letter")["text"]
        page = (Path(tmp) / "reports" / "latest.html").read_text()
        assert "This week's letter" in page


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
