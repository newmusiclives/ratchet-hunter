"""Broker connections. Set "broker_mode" in config.json:

  simulated     Paper only (default). No broker, no real orders.
  assisted      Real account at any broker. The Hunter does the research, sizing and
                risk checks, then emails the exact order and tender steps; a person
                places them (about 3 minutes, a few times a year). Works with brokers
                that accept tender elections online, such as Interactive Brokers.
  alpaca-paper  Places real orders automatically in an Alpaca PAPER account
                (secrets ALPACA_KEY_ID / ALPACA_SECRET_KEY). Good for testing order flow.
  alpaca-live   Real money at Alpaca. Locked unless the secret ALLOW_REAL_MONEY is set
                to YES. Note Alpaca charges $100 per tender election, so set
                "tender_fee": 100 and expect few deals to qualify.

Every mode keeps the paper ledger as the record of what the strategy did. Real
accounts are checked against it each day (reconcile) and differences are reported.
"""
import math
import os

import requests


def connect(cfg):
    mode = cfg.get("broker_mode", "simulated")
    if mode == "assisted":
        return AssistedBroker(cfg)
    if mode in ("alpaca-paper", "alpaca-live"):
        return AlpacaBroker(cfg, live=(mode == "alpaca-live"))
    raise ValueError(f"unknown broker_mode {mode!r}")


class AssistedBroker:
    """A real account where a person places the orders the Hunter specifies."""
    real_account = True

    def __init__(self, cfg):
        self.name = cfg.get("broker_name", "the brokerage")
        self.slip = cfg.get("limit_slippage_pct", 1.0) / 100

    def buy(self, ticker, shares, px):
        return None  # the alert carries the order; see order_text()

    def order_text(self, side, ticker, shares, px):
        lim = round(px * (1 + self.slip), 2) if side == "buy" else round(px * (1 - self.slip), 2)
        return f"{side.upper()} {shares} {ticker}, limit ${lim:.2f}, day order"

    def sell(self, ticker, shares, px):
        return None

    def reconcile(self, led):
        return []

    def sweep(self, led, cfg):
        pass


class AlpacaBroker:
    real_account = True

    def __init__(self, cfg, live=False, session=None):
        key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY secrets are not set")
        if live and os.environ.get("ALLOW_REAL_MONEY") != "YES":
            raise RuntimeError("alpaca-live is locked: set the secret ALLOW_REAL_MONEY to YES to trade real money")
        self.live = live
        self.base = "https://api.alpaca.markets" if live else "https://paper-api.alpaca.markets"
        self.name = "Alpaca (LIVE)" if live else "Alpaca paper"
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        self.slip = cfg.get("limit_slippage_pct", 1.0) / 100
        self.max_order = cfg.get("max_single_deal_cost", 10000.0)

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _order(self, side, ticker, shares, px):
        if shares <= 0:
            return None
        lim = round(px * (1 + self.slip), 2) if side == "buy" else round(px * (1 - self.slip), 2)
        if side == "buy" and shares * lim > self.max_order:
            raise RuntimeError(f"order ${shares * lim:,.0f} exceeds max_single_deal_cost")
        o = self._req("POST", "/v2/orders", json={
            "symbol": ticker, "qty": str(shares), "side": side, "type": "limit",
            "limit_price": f"{lim:.2f}", "time_in_force": "day"})
        return o.get("id")

    def buy(self, ticker, shares, px):
        return self._order("buy", ticker, shares, px)

    def sell(self, ticker, shares, px):
        return self._order("sell", ticker, shares, px)

    def positions(self):
        return {p["symbol"]: int(float(p["qty"])) for p in self._req("GET", "/v2/positions")}

    def reconcile(self, led):
        """Bring the ledger in line with what really filled, and report any differences."""
        notes = []
        for p in led.open_positions():
            oid = p.get("order_id")
            if not oid or p.get("tender_status") == "filled-checked":
                continue
            o = self._req("GET", f"/v2/orders/{oid}")
            st = o.get("status")
            if st == "filled":
                fill = float(o["filled_avg_price"])
                qty = int(float(o["filled_qty"]))
                cost = round(fill * qty, 2)
                led.set("cash", led.get("cash") + p["cost"] - cost)
                led.update_position(p["id"], price_paid=fill, cost=cost, shares=qty)
                led.log("fill", f"{p['ticker']}: {qty} filled at ${fill:.2f}")
                notes.append(f"{p['ticker']} order filled: {qty} @ ${fill:.2f}")
            elif st in ("canceled", "expired", "rejected"):
                led.close(p, p["cost"], "unfilled", f"Broker order {st}; nothing was bought.")
                notes.append(f"{p['ticker']} order {st}: removed from the account")
        held = self.positions()
        fund = led.cfg.get("cash_fund", "")
        for p in led.open_positions():
            if held.get(p["ticker"], 0) != p["shares"]:
                notes.append(f"Check {p['ticker']}: ledger {p['shares']} shares, broker {held.get(p['ticker'], 0)}")
        for sym, q in held.items():
            if sym != fund and sym not in {p["ticker"] for p in led.open_positions()}:
                notes.append(f"Broker holds {q} {sym} that the Hunter doesn't track")
        return notes

    def sweep(self, led, cfg):
        """Keep idle cash in a T-bill fund (e.g. SGOV), leaving a buffer for new deals."""
        fund = cfg.get("cash_fund")
        if not fund:
            return
        acct = self._req("GET", "/v2/account")
        cash = float(acct["cash"])
        buffer = cfg.get("cash_buffer", 2000.0)
        held = self.positions()
        qty = held.get(fund, 0)
        if qty:
            px = float(self._req("GET", f"/v2/positions/{fund}").get("current_price") or 0)
        else:
            import market
            px = market.last_price(fund) or 0
        if px <= 0:
            return
        if cash > buffer + px:
            self._order("buy", fund, math.floor((cash - buffer) / px), px)
        elif cash < buffer and qty > 0:
            self._order("sell", fund, min(qty, math.ceil((buffer - cash) / px)), px)
