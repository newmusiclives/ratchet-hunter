#!/usr/bin/env python3
"""The Ratchet - Hunter: 12-month backtest on REAL past deals.

    python backtest.py                 # last 365 days
    python backtest.py --days 540      # longer look-back
    python backtest.py --cache-only    # re-run from saved data, no internet

Step 1 (download): every odd-lot tender offer filed on SEC EDGAR in the window,
    the company's later amendments (extensions, final results, terminations),
    and daily closing prices. Everything is saved in data/backtest_cache so
    later runs are instant and the SEC is only asked once.
Step 2 (replay): walks forward one weekday at a time using the SAME rules,
    Floor Rule and Ratchet as the live Hunter. On each day it only "sees"
    filings and prices that existed on that day - no peeking ahead.
"""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

import hunter
import report
from ledger import Ledger

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "backtest_cache"


def _safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


class Cache:
    def __init__(self, folder, offline):
        self.d = Path(folder)
        (self.d / "filings").mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._edgar = None

    def edgar(self, cfg):
        if self.offline:
            raise RuntimeError("offline: not in cache")
        if not self._edgar:
            from edgar import Edgar
            import os
            self._edgar = Edgar(os.environ.get("SEC_USER_AGENT") or cfg["sec_user_agent"])
        return self._edgar

    def _json(self, name, make):
        p = self.d / name
        if p.exists():
            return json.loads(p.read_text())
        val = make()
        p.write_text(json.dumps(val, indent=1, default=str))
        return val

    def deals(self, cfg, start, end):
        forms = [f for f in cfg["edgar_forms"] if not f.endswith("/A")]
        eng = cfg.get("engines", {"odd_lot": True})
        out = []
        if eng.get("odd_lot", True):
            out += self._json(f"deals_{start}_{end}.json",
                              lambda: self.edgar(cfg).search_odd_lot_tenders(forms, 0, start=start, end=end))
        if eng.get("takeover"):
            try:
                tk = self._json(f"takeovers_{start}_{end}.json",
                                lambda: self.edgar(cfg).search_takeovers(0, start=start, end=end))
            except RuntimeError:   # offline and never downloaded
                tk = []
            seen = {d["accession"] for d in out}
            out += [d for d in tk if d["accession"] not in seen]
        return out

    def amendments(self, cfg, cik, start, end):
        # One file per company covering the whole window, so replay and download agree.
        return self._json(f"amend_{cik}.json",
                          lambda: self.edgar(cfg).amendments(cik, start=start, end=end))

    def text(self, cfg, rec):
        p = self.d / "filings" / (_safe(rec["doc_id"]) + ".txt")
        if p.exists():
            return p.read_text()
        t = self.edgar(cfg).fetch_text(rec)
        p.write_text(t)
        return t

    def prices(self, ticker, start, end):
        def make():
            if self.offline:
                return {}
            import market
            return market.history(ticker, start, end)
        return self._json(f"prices_{_safe(ticker)}.json", make)


class ReplaySource:
    """Looks like the live data source, but frozen at `self.day`."""
    def __init__(self, cache, cfg, deals, start, end, log):
        self.c, self.cfg, self.start, self.end, self.log = cache, cfg, start, end, log
        self.day = start
        self.deals = deals
        self.px = {}

    def candidates(self):
        # A filing becomes visible the trading day after it was filed.
        return [d for d in self.deals if dt.date.fromisoformat(d["file_date"]) < self.day]

    def text(self, rec):
        return self.c.text(self.cfg, rec)

    def price(self, ticker):
        if ticker not in self.px:
            self.px[ticker] = self.c.prices(ticker, self.start - dt.timedelta(days=10), self.end)
        series = self.px[ticker]
        best = None
        for d in sorted(series):
            if d <= self.day.isoformat():
                best = series[d]
            else:
                break
        return best

    def tbill_rate(self):
        p = self.price("^IRX")
        return p / 100 if p and 0 < p < 20 else None

    def price_before(self, ticker, day):
        if ticker not in self.px:
            self.px[ticker] = self.c.prices(ticker, self.start - dt.timedelta(days=10), self.end)
        before = [k for k in sorted(self.px[ticker]) if k < day.isoformat()]
        return self.px[ticker][before[-1]] if before else None

    def updates_for(self, pos, since_days=None):
        cik = pos["url"].split("/data/")[1].split("/")[0] if "/data/" in pos["url"] else None
        if not cik:
            return []
        recs = self.c.amendments(self.cfg, cik, self.start - dt.timedelta(days=30), self.end)
        texts = []
        for r in recs:
            if r.get("file_date") and dt.date.fromisoformat(r["file_date"]) < self.day:
                try:
                    texts.append(self.c.text(self.cfg, r))
                except Exception as e:
                    self.log(f"could not read amendment for {pos['ticker']}: {e}")
        return texts


def weekdays(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def run(days=365, offline=False, cfg=None, cache_dir=CACHE, end=None, quiet=False, write_report=True):
    cfg = dict(cfg or hunter.load_cfg())
    cfg["ai_review"] = False  # rules only: free, repeatable, no hindsight from a model
    end = end or dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    say = (lambda *a: None) if quiet else print
    cache = Cache(cache_dir, offline)

    say(f"Backtest window: {start} to {end}")
    say("Step 1/2: collecting real odd-lot tender offers from SEC EDGAR (first run takes a few minutes)...")
    deals = cache.deals(cfg, start, end)
    say(f"  found {len(deals)} original filing(s) mentioning odd lots")
    problems = []
    for i, d in enumerate(deals, 1):
        if not d.get("ticker"):
            continue
        try:
            cache.text(cfg, d)
            cache.amendments(cfg, d["cik"], start - dt.timedelta(days=30), end)
            cache.prices(d["ticker"], start - dt.timedelta(days=10), end)
        except Exception as e:
            problems.append(f"{d.get('ticker')}: {e}")
        if not quiet and i % 5 == 0:
            print(f"  downloaded {i}/{len(deals)}")

    try:
        cache.prices("^IRX", start - dt.timedelta(days=10), end)  # T-bill rate history
    except Exception:
        pass
    say("Step 2/2: replaying the year one trading day at a time...")
    db = HERE / "data" / (f"backtest_{start}_{end}.db" if write_report else f"backtest_grid_{start}_{end}.db")
    if db.exists():
        db.unlink()
    src = ReplaySource(cache, cfg, deals, start, end, problems.append)
    led = None
    equity = cfg["starting_capital"]
    for day in weekdays(start, end):
        src.day = day
        if led is None:
            led = Ledger(db, cfg, today=day)
        led.today = day
        notes = []
        led.accrue_cash_yield(src.tbill_rate())
        hunter.monthly_cost(led, cfg)
        hunter.settle(led, src, cfg, notes)
        hunter.hunt(led, src, cfg, notes)
        led.maybe_ratchet()
        equity = hunter.mark_to_market(led, src)
        led.record_equity(equity)

    if not write_report:
        return led, equity
    out = HERE / "reports"
    notes = [f"BACKTEST of {len(deals)} real SEC filings, {start} to {end}. Rules only (AI reviewer off).",
             "Prices are daily closes; trades assume you bought at the close the day after the filing."]
    notes += [f"Data problem: {p}" for p in list(dict.fromkeys(problems))[:15]]
    path = report.write(led, cfg, equity, notes, 0.0, 0.0, out, title="12-month backtest",
                        filename="backtest_report.html", seen_limit=500)
    say("")
    say(report.summary_line(led, cfg, equity))
    say(f"Open this file to see the year: {path}")
    return led, equity


def main():
    ap = argparse.ArgumentParser(description="Replay the past year's real odd-lot tenders")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--cache-only", action="store_true", help="use saved data only (no internet)")
    a = ap.parse_args()
    run(a.days, a.cache_only)


if __name__ == "__main__":
    main()
