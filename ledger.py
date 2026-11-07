"""Paper account: cash, positions, the floor, and the rules that guard it.

The Floor Rule (the heart of the system):
    If every open deal failed and its stock fell by `assumed_drop_if_deal_fails`,
    the account must still be worth at least the floor.
    cash + sum(position cost * (1 - drop)) >= floor
The floor starts at starting_capital - max_loss ($8,000) and only ever rises
(the Ratchet). No margin, no shorting, no options: cash only.
"""
import json
import sqlite3
import datetime as dt
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, ticker TEXT, company TEXT, accession TEXT, url TEXT,
  shares INTEGER, price_paid REAL, cost REAL, payout_price REAL, expiration TEXT,
  dutch INTEGER, status TEXT, opened TEXT, closed TEXT, proceeds REAL, pnl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS events (ts TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS seen (doc_id TEXT PRIMARY KEY, ts TEXT, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (day TEXT PRIMARY KEY, equity REAL, floor REAL, cash REAL);
"""


class Ledger:
    def __init__(self, path, cfg, today=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.cfg = cfg
        self.today = today or dt.date.today()
        if self.get("cash") is None:
            start = cfg["starting_capital"]
            self.set("cash", start)
            self.set("floor", start - cfg["max_loss"])
            self.set("start_day", self.today.isoformat())
            self.set("last_accrual", self.today.isoformat())
            self.set("realized_since_ratchet", 0.0)
            self.set("last_ratchet", self.today.isoformat())
            self.set("running_costs", 0.0)
            self.set("interest_earned", 0.0)
            self.log("open", f"Paper account opened with ${start:,.2f}; floor ${start - cfg['max_loss']:,.2f}")

    def _migrate(self):
        """Add newer columns to an existing account without losing anything."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(positions)")}
        for col, typ in [("tender_status", "TEXT"), ("alert_ref", "TEXT"), ("engine", "TEXT"),
                         ("order_id", "TEXT"), ("deal_json", "TEXT")]:
            if col not in have:
                self.db.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
        self.db.commit()

    def update_position(self, pos_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE positions SET {cols} WHERE id=?", (*fields.values(), pos_id))
        self.db.commit()

    # --- key/value state -------------------------------------------------
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    def log(self, kind, detail):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (self.today.isoformat(), kind, detail))
        self.db.commit()

    # --- views -------------------------------------------------------------
    def open_positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='open' ORDER BY expiration")]

    def closed_positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status!='open' ORDER BY closed")]

    def has_ticker(self, ticker):
        return self.db.execute("SELECT 1 FROM positions WHERE ticker=? AND status='open'", (ticker,)).fetchone() is not None

    def already_seen(self, doc_id):
        return self.db.execute("SELECT 1 FROM seen WHERE doc_id=?", (doc_id,)).fetchone() is not None

    def mark_seen(self, doc_id, decision, reason):
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", (doc_id, self.today.isoformat(), decision, reason))
        self.db.commit()

    # --- the Floor Rule ----------------------------------------------------
    def stressed_value(self, extra_cost=0.0):
        d = self.cfg["assumed_drop_if_deal_fails"]
        held = sum(p["cost"] for p in self.open_positions()) + extra_cost
        return self.get("cash") - extra_cost + held * (1 - d)

    def max_new_position(self):
        """Largest purchase that keeps the stressed value at or above the floor."""
        d = self.cfg["assumed_drop_if_deal_fails"]
        room = (self.stressed_value() - self.get("floor")) / d
        return max(0.0, min(room, self.get("cash"), self.cfg["max_single_deal_cost"]))

    # --- money movements ---------------------------------------------------
    def accrue_cash_yield(self, rate=None):
        """Interest on idle cash. `rate` is the live T-bill yield when known."""
        last = dt.date.fromisoformat(self.get("last_accrual"))
        days = (self.today - last).days
        if days <= 0:
            return 0.0
        rate = rate if rate is not None else self.cfg["cash_yield_annual"]
        self.set("cash_rate", rate)
        cash = self.get("cash")
        interest = cash * ((1 + rate) ** (days / 365) - 1)
        self.set("cash", cash + interest)
        self.set("interest_earned", self.get("interest_earned") + interest)
        self.set("last_accrual", self.today.isoformat())
        return interest

    def charge(self, amount, why):
        if amount <= 0:
            return
        self.set("cash", self.get("cash") - amount)
        self.set("running_costs", self.get("running_costs") + amount)
        self.log("cost", f"${amount:.2f} {why}")

    def buy(self, deal, shares, price):
        cost = round(shares * price, 2)
        assert cost <= self.get("cash") + 1e-6, "no margin allowed"
        assert self.stressed_value(cost) >= self.get("floor") - 1e-6, "floor rule"
        self.set("cash", self.get("cash") - cost)
        cur = self.db.execute(
            "INSERT INTO positions (ticker,company,accession,url,shares,price_paid,cost,payout_price,expiration,dutch,status,opened,note,"
            "engine,tender_status,deal_json) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?)",
            (deal["ticker"], deal["company"], deal["accession"], deal["url"], shares, price, cost,
             deal["payout"], deal["expiration"].isoformat(), int(deal["dutch"]), self.today.isoformat(), deal.get("note", ""),
             deal.get("engine", "odd-lot tender"), deal.get("tender_status", "assumed"),
             json.dumps({k: v for k, v in deal.items() if k not in ("expiration",)}, default=str)))
        self.db.commit()
        self.log("buy", f"{shares} {deal['ticker']} @ ${price:.2f} = ${cost:,.2f}; tender pays ${deal['payout']:.2f}, expires {deal['expiration']}")
        return cur.lastrowid

    def set_expiration(self, pos, new_exp):
        self.db.execute("UPDATE positions SET expiration=? WHERE id=?", (new_exp.isoformat(), pos["id"]))
        self.db.commit()
        self.log("extended", f"{pos['ticker']} offer extended to {new_exp}")

    def set_payout(self, pos, price):
        self.db.execute("UPDATE positions SET payout_price=? WHERE id=?", (price, pos["id"]))
        self.db.commit()
        self.log("final price", f"{pos['ticker']} Dutch auction final price ${price:.2f}")

    def close(self, pos, proceeds, status, note):
        pnl = proceeds - pos["cost"]
        self.db.execute("UPDATE positions SET status=?, closed=?, proceeds=?, pnl=?, note=? WHERE id=?",
                        (status, self.today.isoformat(), proceeds, pnl, note, pos["id"]))
        self.db.commit()
        self.set("cash", self.get("cash") + proceeds)
        self.set("realized_since_ratchet", self.get("realized_since_ratchet") + pnl)
        self.log(status, f"{pos['ticker']}: proceeds ${proceeds:,.2f}, P&L ${pnl:+,.2f}. {note}")
        return pnl

    # --- the Ratchet ---------------------------------------------------------
    def maybe_ratchet(self):
        if self.today.strftime("%A") != self.cfg["ratchet_day"]:
            return 0.0
        if self.get("last_ratchet") == self.today.isoformat():
            return 0.0
        gain = self.get("realized_since_ratchet")
        lift = max(0.0, gain) * self.cfg["ratchet_share_of_profit"]
        if lift > 0:
            self.set("floor", self.get("floor") + lift)
            self.log("ratchet", f"Floor raised by ${lift:,.2f} to ${self.get('floor'):,.2f} (half of ${gain:,.2f} realized this week)")
        self.set("realized_since_ratchet", 0.0)
        self.set("last_ratchet", self.today.isoformat())
        return lift

    def record_equity(self, equity):
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
                        (self.today.isoformat(), equity, self.get("floor"), self.get("cash")))
        self.db.commit()

    def equity_history(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM equity ORDER BY day")]

    def recent_events(self, n=25):
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY rowid DESC LIMIT ?", (n,))]

    def recent_seen(self, n=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM seen ORDER BY ts DESC, rowid DESC LIMIT ?", (n,))]
