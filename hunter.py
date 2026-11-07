#!/usr/bin/env python3
"""The Ratchet - Hunter (paper trading v1)

Run once per weekday, after the US market closes:
    python hunter.py run          # scan, settle, trade (on paper), report
    python hunter.py status       # print the account
    python hunter.py run --offline tests/fixtures   # dry run on sample filings

Paper only. Nothing in this file can place a real order.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from extract import extract_terms
from ledger import Ledger
import report

HERE = Path(__file__).resolve().parent


def load_cfg(path=None):
    return json.loads((Path(path) if path else HERE / "config.json").read_text())


# ---------------------------------------------------------------------------
class Sources:
    """Live data: EDGAR + free price feed."""
    def __init__(self, cfg):
        from edgar import Edgar
        import market
        import os
        self.edgar = Edgar(os.environ.get("SEC_USER_AGENT") or cfg["sec_user_agent"])
        self.market = market
        self.cfg = cfg

    def candidates(self):
        return self.edgar.search_odd_lot_tenders(self.cfg["edgar_forms"], self.cfg["edgar_lookback_days"])

    def text(self, rec):
        return self.edgar.fetch_text(rec)

    def price(self, ticker):
        return self.market.last_price(ticker)

    def price_before(self, ticker, day):
        series = self.market.history(ticker, day - dt.timedelta(days=14), day - dt.timedelta(days=1))
        before = [k for k in sorted(series) if k < day.isoformat()]
        return series[before[-1]] if before else None

    def updates_for(self, pos, since_days=120):
        """Recent amendment texts for a held deal (to spot termination / final results)."""
        cik = pos["url"].split("/data/")[1].split("/")[0] if "/data/" in pos["url"] else None
        if not cik:
            return []
        hits = self.edgar.amendments(cik, since_days)
        return [self.edgar.fetch_text(h) for h in hits[:4]]


class OfflineSources:
    """Sample filings from a folder, for testing without internet.
    Folder holds candidates.json, *.txt filings and prices.json."""
    def __init__(self, folder):
        self.f = Path(folder)
        self.prices = json.loads((self.f / "prices.json").read_text())

    def candidates(self):
        return json.loads((self.f / "candidates.json").read_text())

    def text(self, rec):
        return (self.f / rec["file"]).read_text()

    def price(self, ticker):
        return self.prices.get(ticker)

    def price_before(self, ticker, day):
        return None

    def updates_for(self, pos, since_days=120):
        p = self.f / f"update_{pos['ticker']}.txt"
        return [p.read_text()] if p.exists() else []


# ---------------------------------------------------------------------------
def settle(led, src, cfg, notes):
    for pos in led.open_positions():
        exp = dt.date.fromisoformat(pos["expiration"])
        try:
            updates = [extract_terms(t) for t in src.updates_for(pos)]
        except Exception as e:
            updates, _ = [], notes.append(f"Could not check updates for {pos['ticker']}: {e}")
        terminated = any(u["terminated"] for u in updates)
        final = any(u["final_results"] for u in updates)
        ext = [u["extended_to"] for u in updates if u.get("extended_to")]
        if ext and max(ext) > exp:
            exp = max(ext)
            led.set_expiration(pos, exp)
            notes.append(f"{pos['ticker']}: offer extended to {exp}")
        if pos["dutch"]:
            finals = [u["price"] for u in updates if u["final_results"] and u["price"] and not u["dutch_auction"]]
            if finals and max(finals) > pos["payout_price"]:
                led.set_payout(pos, max(finals))
                pos["payout_price"] = max(finals)
        if terminated:
            px = _safe_price(src, pos["ticker"]) or pos["price_paid"] * (1 - cfg["assumed_drop_if_deal_fails"])
            led.close(pos, round(px * pos["shares"], 2), "failed", f"Offer terminated; sold at ${px:.2f}.")
        elif led.today > exp and (final or (led.today - exp).days >= 5):
            proceeds = round(pos["payout_price"] * pos["shares"], 2)
            led.close(pos, proceeds, "settled",
                      "Tender completed; odd lot accepted in full." + (f" (Dutch auction paid ${pos['payout_price']:.2f}.)" if pos["dutch"] else ""))


def _safe_price(src, ticker):
    try:
        return src.price(ticker)
    except Exception:
        return None


def hunt(led, src, cfg, notes):
    try:
        cands = src.candidates()
    except Exception as e:
        notes.append(f"EDGAR search failed today: {e}")
        return
    groups = {}
    for r in cands:  # one SEC filing = several documents; judge them together
        groups.setdefault(r.get("accession") or r["doc_id"], []).append(r)
    notes.append(f"EDGAR returned {len(cands)} document(s) in {len(groups)} filing(s) mentioning odd lots.")
    for key, docs in groups.items():
        rec = dict(docs[0], doc_id=key, _docs=docs)
        if led.already_seen(key):
            continue
        decision, reason = evaluate(led, src, cfg, rec, notes)
        label = f"{rec.get('ticker') or '?'} {rec.get('company','')}: {reason}"
        if decision in ("watch", "retry"):
            notes.append(f"{decision.upper()} {label}")  # look again tomorrow
        else:
            led.mark_seen(rec["doc_id"], decision, label)


def evaluate(led, src, cfg, rec, notes):
    t = rec.get("ticker")
    if not t:
        return "skip", "no ticker found (not exchange-listed?)"
    if led.has_ticker(t):
        return "skip", "already holding this deal"
    try:
        text = " ".join(src.text(d) for d in rec.get("_docs", [rec]))
    except Exception as e:
        return "retry", f"could not fetch filing: {e}"
    terms = extract_terms(text)
    if terms["terminated"]:
        return "skip", "filing says the offer was terminated"
    if not terms["cash_offer"]:
        return "skip", "share-exchange offer (v1 handles cash only)"
    if not terms["odd_lot_priority"]:
        return "skip", "no clear odd-lot priority"
    if not terms["price"]:
        return "skip", "could not read the offer price"
    if not terms["expiration"]:
        return "skip", "could not read the expiration date"
    if cfg["skip_if_financing_condition"] and terms["financing_condition"]:
        return "skip", "offer depends on financing"
    days = (terms["expiration"] - led.today).days
    if days < cfg["min_days_to_expiry"]:
        return "skip", f"expires too soon ({days} days)"
    if days > cfg["max_days_to_expiry"]:
        return "watch", f"expires in {days} days; will revisit"

    px = _safe_price(src, t)
    if not px:
        return "retry", "no market price today"
    stated = terms.get("stated_prices") or []
    note_extra = ""
    if stated and rec.get("file_date"):
        fd = dt.date.fromisoformat(rec["file_date"])
        matches = lambda ref: ref and any(abs(ref / s_ - 1) <= 0.20 for s_ in stated)
        try:
            ref = src.price_before(t, fd)
        except Exception:
            ref = None
        if ref and not matches(ref):
            # Maybe the offer is for another share class (e.g. preferred) the filing names.
            alt = None
            for a in [x for x in terms.get("symbols", []) if x != t]:
                try:
                    if matches(src.price_before(a, fd)):
                        alt = a
                        break
                except Exception:
                    continue
            if not alt:
                return "skip", (f"price data (${ref:.2f}) doesn't match the price the filing quotes "
                                f"(${', $'.join(f'{x:g}' for x in stated[:3])}): wrong share class or split-adjusted data")
            if led.has_ticker(alt):
                return "skip", f"already holding this deal ({alt})"
            note_extra = f"Offer is for {alt} shares, not {t}."
            t = alt
            px = _safe_price(src, t)
            if not px:
                return "retry", f"no market price today for {t}"
    payout = terms["price"]
    premium = (payout - px) / px * 100
    if premium < cfg["min_premium_pct"]:
        return "skip", f"premium only {premium:.1f}% (price ${px:.2f} vs offer ${payout:.2f})"

    budget = led.max_new_position()
    shares = int(min(cfg["odd_lot_shares"], budget // px))
    profit = shares * (payout - px)
    if shares < 1:
        return "skip", f"floor rule: no room (max new position ${budget:,.0f})"
    if profit < cfg["min_profit_dollars"]:
        return "skip", f"only ${profit:.0f} profit possible within the floor rule ({shares} sh)"

    note = "; ".join([n for n in [note_extra] + terms["notes"] if n])
    if cfg.get("ai_review"):
        from ai_review import review
        try:
            verdict = review(text, {k: v for k, v in terms.items() if k != "notes"}, cfg["ai_model"])
        except Exception as e:
            verdict = {"approve": False, "reason": f"AI review failed ({e}); vetoed to be safe."}
        if verdict is not None:
            led.charge(cfg["ai_cost_per_review_estimate"], f"AI review of {t}")
            if not verdict.get("approve"):
                return "veto", "AI veto: " + verdict.get("reason", "")
            lo = verdict.get("price_low")
            if isinstance(lo, (int, float)) and lo < payout - 0.005:
                payout = float(lo)
                note += f"; AI corrected payout to ${lo:.2f}"
                profit = shares * (payout - px)
                if profit < cfg["min_profit_dollars"]:
                    return "skip", f"after AI correction only ${profit:.0f} profit"
            if verdict.get("red_flags"):
                note += "; flags: " + ", ".join(verdict["red_flags"])

    deal = {"ticker": t, "company": rec.get("company", ""), "accession": rec.get("accession", ""),
            "url": rec.get("url", ""), "payout": payout, "expiration": terms["expiration"],
            "dutch": terms["dutch_auction"], "note": note}
    led.buy(deal, shares, px)
    ann = premium * 365 / max(days, 1)
    return "BUY", f"{shares} sh @ ${px:.2f} -> ${payout:.2f} ({premium:.1f}%, ~{ann:.0f}% annualized), +${profit:.0f} if it completes"


def mark_to_market(led, src):
    value = led.get("cash")
    for p in led.open_positions():
        px = _safe_price(src, p["ticker"]) or p["price_paid"]
        value += px * p["shares"]
    return value


def monthly_cost(led, cfg):
    m = led.today.strftime("%Y-%m")
    if cfg.get("fixed_monthly_running_cost", 0) > 0 and led.get("last_cost_month") != m:
        led.charge(cfg["fixed_monthly_running_cost"], f"monthly running cost {m}")
        led.set("last_cost_month", m)


def run(cfg, offline=None, today=None):
    data = Path(cfg["data_dir"]) if Path(cfg["data_dir"]).is_absolute() else HERE / cfg["data_dir"]
    led = Ledger(data / ("paper_offline.db" if offline else "paper.db"), cfg, today=today)
    src = OfflineSources(offline) if offline else Sources(cfg)
    notes = []
    interest = led.accrue_cash_yield()
    monthly_cost(led, cfg)
    settle(led, src, cfg, notes)
    hunt(led, src, cfg, notes)
    lift = led.maybe_ratchet()
    equity = mark_to_market(led, src)
    led.record_equity(equity)
    rdir = Path(cfg["report_dir"]) if Path(cfg["report_dir"]).is_absolute() else HERE / cfg["report_dir"]
    path = report.write(led, cfg, equity, notes, interest, lift, rdir)
    print(report.summary_line(led, cfg, equity))
    print(f"Report: {path}")
    return led, equity


def main():
    ap = argparse.ArgumentParser(description="The Ratchet - Hunter (paper trading)")
    ap.add_argument("cmd", choices=["run", "status"])
    ap.add_argument("--offline", help="folder of sample filings for a dry run")
    ap.add_argument("--config")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD (testing)")
    a = ap.parse_args()
    cfg = load_cfg(a.config)
    today = dt.date.fromisoformat(a.date) if a.date else None
    if a.cmd == "run":
        run(cfg, a.offline, today)
    else:
        data = HERE / cfg["data_dir"] / ("paper_offline.db" if a.offline else "paper.db")
        if not data.exists():
            sys.exit("No account yet. Run: python hunter.py run")
        led = Ledger(data, cfg, today=today)
        print(report.summary_line(led, cfg, led.get("cash") + sum(p["cost"] for p in led.open_positions())))
        for p in led.open_positions():
            print(f"  OPEN {p['ticker']}: {p['shares']} sh, cost ${p['cost']:,.2f}, pays ${p['payout_price']:.2f}, expires {p['expiration']}")


if __name__ == "__main__":
    main()
