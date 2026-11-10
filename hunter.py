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
import re
import sys
from pathlib import Path

from extract import extract_terms
from ledger import Ledger
import report

HERE = Path(__file__).resolve().parent

# Each filing is read once per process; re-checks (e.g. "watch" deals) reuse the result.
TERMS_MEMO = {}
# Board stance per company, learned from any of its filings (amendments rarely repeat it).
STANCE = {}

# Live-run helpers, set in run(). Backtests leave these empty.
HOOKS = {"alerts": None, "broker": None}


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
        eng = self.cfg.get("engines", {"odd_lot": True})
        out = []
        if eng.get("odd_lot", True):
            out += self.edgar.search_odd_lot_tenders(self.cfg["edgar_forms"], self.cfg["edgar_lookback_days"])
        if eng.get("takeover"):
            seen = {r["accession"] for r in out}
            out += [r for r in self.edgar.search_takeovers(self.cfg["edgar_lookback_days"]) if r["accession"] not in seen]
        if eng.get("merger"):
            seen = {r["accession"] for r in out}
            out += [r for r in self.edgar.search_mergers(self.cfg.get("merger_lookback_days", 120)) if r["accession"] not in seen]
        return out

    def text(self, rec):
        return self.edgar.fetch_text(rec)

    def price(self, ticker):
        return self.market.last_price(ticker)

    def tbill_rate(self):
        return self.market.tbill_rate()

    def price_before(self, ticker, day):
        series = self.market.history(ticker, day - dt.timedelta(days=14), day - dt.timedelta(days=1))
        before = [k for k in sorted(series) if k < day.isoformat()]
        return series[before[-1]] if before else None

    def updates_for(self, pos, since_days=120):
        """Recent amendment texts for a held deal (to spot termination / final results)."""
        cik = pos["url"].split("/data/")[1].split("/")[0] if "/data/" in pos["url"] else None
        if not cik:
            return []
        if pos.get("engine") == "merger":
            hits = [h for h in self.edgar.merger_updates(cik) if (h.get("file_date") or "") >= pos["opened"]]
            return [self.edgar.fetch_text(h) for h in hits[-4:]]
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

    def tbill_rate(self):
        return None

    def updates_for(self, pos, since_days=120):
        p = self.f / f"update_{pos['ticker']}.txt"
        return [p.read_text()] if p.exists() else []


# ---------------------------------------------------------------------------
def cached_terms(text):
    key = ("text", len(text), hash(text))
    if key not in TERMS_MEMO:
        TERMS_MEMO[key] = extract_terms(text)
    return TERMS_MEMO[key]


def settle_merger(led, src, cfg, pos, notes):
    import merger
    try:
        statuses = [merger.update_status(t) for t in src.updates_for(pos)]
    except Exception as e:
        notes.append(f"Could not check merger news for {pos['ticker']}: {e}")
        return
    if "terminated" in statuses:
        px = _safe_price(src, pos["ticker"]) or pos["price_paid"] * (1 - cfg.get("merger_assumed_drop", 0.35))
        sell_shares(pos, px, notes)
        led.close(pos, round(px * pos["shares"], 2), "failed", f"Merger agreement terminated; sold at ${px:.2f}.")
    elif "completed" in statuses:
        led.close(pos, round(pos["payout_price"] * pos["shares"], 2), "settled",
                  "Merger completed; cash paid automatically at the merger price.")
    else:
        exp = dt.date.fromisoformat(pos["expiration"])
        if "approved" in statuses and not led.get(f"approved_{pos['id']}"):
            led.set(f"approved_{pos['id']}", True)
            notes.append(f"{pos['ticker']}: shareholders approved the merger")
        if (led.today - exp).days > cfg.get("merger_max_overdue_days", 180):
            px = _safe_price(src, pos["ticker"]) or pos["price_paid"]
            sell_shares(pos, px, notes)
            led.close(pos, round(px * pos["shares"], 2), "unresolved",
                      f"Still not closed {cfg.get('merger_max_overdue_days', 180)} days after the expected date; sold at ${px:.2f}.")


def settle(led, src, cfg, notes):
    import rocket
    rocket.settle(led, src, cfg, notes)
    for pos in led.open_positions():
        if pos.get("engine") == "rocket":
            continue
        if pos.get("engine") == "merger":
            settle_merger(led, src, cfg, pos, notes)
            continue
        exp = dt.date.fromisoformat(pos["expiration"])
        try:
            updates = [cached_terms(t) for t in src.updates_for(pos)]
        except Exception as e:
            updates, _ = [], notes.append(f"Could not check updates for {pos['ticker']}: {e}")
        terminated = any(u["terminated"] for u in updates)
        final = any(u["final_results"] for u in updates)
        ext = [u["extended_to"] for u in updates if u.get("extended_to")]
        if ext and max(ext) > exp:
            exp = max(ext)
            led.set_expiration(pos, exp)
            notes.append(f"{pos['ticker']}: offer extended to {exp}")
        changes = [u["price_change_to"] for u in updates if u.get("price_change_to")]
        if changes and abs(changes[-1] - pos["payout_price"]) > 0.005:
            led.set_payout(pos, changes[-1])
            notes.append(f"{pos['ticker']}: offer price changed from ${pos['payout_price']:.2f} to ${changes[-1]:.2f}")
            pos["payout_price"] = changes[-1]
        if pos["dutch"]:
            finals = [u["price"] for u in updates if u["final_results"] and u["price"] and not u["dutch_auction"]]
            if finals and max(finals) > pos["payout_price"]:
                led.set_payout(pos, max(finals))
                pos["payout_price"] = max(finals)
        track_tender(led, pos, exp, notes)
        if pos.get("tender_status") == "pending" and led.today > exp and not terminated:
            px = _safe_price(src, pos["ticker"]) or pos["price_paid"] * (1 - cfg["assumed_drop_if_deal_fails"])
            sell_shares(pos, px, notes)
            led.close(pos, round(px * pos["shares"], 2), "not tendered",
                      f"Tender was never confirmed, so the shares were sold at ${px:.2f} after the offer closed.")
            continue
        if terminated:
            px = _safe_price(src, pos["ticker"]) or pos["price_paid"] * (1 - cfg["assumed_drop_if_deal_fails"])
            sell_shares(pos, px, notes)
            led.close(pos, round(px * pos["shares"], 2), "failed", f"Offer terminated; sold at ${px:.2f}.")
        elif pos.get("engine") == "takeover" and led.today > exp and not final:
            if (led.today - exp).days >= 30:   # no proof it completed: don't assume the payout
                px = _safe_price(src, pos["ticker"]) or pos["price_paid"] * (1 - cfg["takeover_assumed_drop"])
                sell_shares(pos, px, notes)
                led.close(pos, round(px * pos["shares"], 2), "unresolved",
                          f"No completion notice 30 days after the deadline; sold at ${px:.2f}.")
            continue
        elif led.today > exp and (final or (led.today - exp).days >= 5):
            fee = json.loads(pos.get("deal_json") or "{}").get("fee", 0) or 0
            proceeds = round(pos["payout_price"] * pos["shares"] - fee, 2)
            led.close(pos, proceeds, "settled",
                      ("Takeover completed at the offer price." if (pos.get("engine") == "takeover") else "Tender completed; odd lot accepted in full.") + (f" (Dutch auction paid ${pos['payout_price']:.2f}.)" if pos["dutch"] else ""))


def after_buy(led, cfg, deal, shares, px, pos_id, notes):
    """Place the real order (if a broker is connected) and tell a human."""
    broker, alerts = HOOKS["broker"], HOOKS["alerts"]
    if broker:
        try:
            oid = broker.buy(deal["ticker"], shares, px)
            led.update_position(pos_id, order_id=oid or "")
        except Exception as e:
            notes.append(f"Broker order for {deal['ticker']} failed: {e}")
            if alerts:
                alerts.open(f"Broker order failed: {deal['ticker']}", f"The Hunter could not place the order: {e}")
    if not alerts:
        return
    try:
        if deal.get("engine") == "merger" and broker and broker.real_account:
            body = (f"The Hunter is buying **{shares} {deal['ticker']}** for a cash merger at **${deal['payout']:.2f}** per share.\n\n"
                    + (f"**Place this order:** `{broker.order_text('buy', deal['ticker'], shares, px)}`\n\n" if hasattr(broker, 'order_text') else "")
                    + f"No tender is needed: the cash arrives automatically when the merger closes (expected {deal['expiration']}).\n\n"
                      f"Filing: {deal.get('url', '')}")
            ref = alerts.open(f"{'ACTION: buy' if hasattr(broker, 'order_text') else 'Bought'} {shares} {deal['ticker']} (cash merger)",
                              body, labels=("hunter", "merger"))
            if not hasattr(broker, "order_text"):
                alerts.close(ref)
            led.update_position(pos_id, alert_ref=ref)
        elif deal["tender_status"] == "pending":
            from alerts import tender_instructions
            body = tender_instructions(deal, shares, broker.name if broker else "the brokerage",
                                       deadline_days=cfg.get("tender_deadline_days_early", 1))
            if broker and hasattr(broker, "order_text"):
                body = (f"**Step 1: place this order now:** `{broker.order_text('buy', deal['ticker'], shares, px)}`\n\n"
                        f"(If the price has moved above the limit, skip the deal and close this issue.)\n\n**Step 2:**\n\n" + body)
            ref = alerts.open(f"ACTION: buy and tender {shares} {deal['ticker']}",
                              body,
                              labels=("hunter", "action-needed"))
            led.update_position(pos_id, alert_ref=ref)
        elif cfg.get("alert_on_paper_deals", True) and not (broker and broker.real_account):
            ref = alerts.open(f"Paper deal: bought {shares} {deal['ticker']} @ ${px:.2f} (tender pays ${deal['payout']:.2f})",
                              f"The Hunter found an odd-lot tender offer and bought it **on paper**.\n\n"
                              f"- Company: {deal['company']} ({deal['ticker']})\n- Paid: ${px:.2f} x {shares}\n"
                              f"- Tender price: ${deal['payout']:.2f}\n- Offer ends: {deal['expiration']}\n"
                              f"- Filing: {deal.get('url', '')}\n\nNo action needed. This is for information.",
                              labels=("hunter", "paper-deal"))
            alerts.close(ref)
    except Exception as e:
        notes.append(f"Could not send alert: {e}")


def track_tender(led, pos, exp, notes):
    """For real accounts: the tender counts as done once the person closes the alert."""
    alerts = HOOKS["alerts"]
    if pos.get("tender_status") != "pending" or not alerts:
        return
    try:
        if alerts.is_closed(pos.get("alert_ref")):
            led.update_position(pos["id"], tender_status="confirmed")
            pos["tender_status"] = "confirmed"
            led.log("tender", f"{pos['ticker']} tender confirmed")
        elif 0 <= (exp - led.today).days <= 3 and not led.get(f"reminded_{pos['id']}"):
            alerts.comment(pos.get("alert_ref"), f"Reminder: the {pos['ticker']} offer ends {exp}. Please tender and close this issue.")
            led.set(f"reminded_{pos['id']}", True)
    except Exception as e:
        notes.append(f"Could not check tender status for {pos['ticker']}: {e}")


def sell_shares(pos, px, notes):
    broker = HOOKS["broker"]
    if broker:
        try:
            broker.sell(pos["ticker"], pos["shares"], px)
            if hasattr(broker, "order_text") and HOOKS["alerts"]:
                HOOKS["alerts"].open(f"ACTION: sell {pos['shares']} {pos['ticker']}",
                                     f"The {pos['ticker']} tender didn't happen, so sell the shares: "
                                     f"`{broker.order_text('sell', pos['ticker'], pos['shares'], px)}`. Close this issue when done.",
                                     labels=("hunter", "action-needed"))
        except Exception as e:
            notes.append(f"Broker sell for {pos['ticker']} failed: {e}")


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
    memo_key = (rec.get("doc_id"), len(rec.get("_docs", [rec])))
    if memo_key in TERMS_MEMO:
        terms = TERMS_MEMO[memo_key]
        text = None   # only needed for the AI reviewer; fetched again below if so
    else:
        try:
            text = " ".join(src.text(d) for d in rec.get("_docs", [rec]))
        except Exception as e:
            return "retry", f"could not fetch filing: {e}"
        terms = extract_terms(text)
        TERMS_MEMO[memo_key] = terms
    if rec.get("engine") == "merger":
        return evaluate_merger(led, src, cfg, rec, text, notes)
    takeover = rec.get("engine") == "takeover"
    if terms["terminated"]:
        return "skip", "filing says the offer was terminated"
    if takeover and not terms["all_shares"]:
        return "skip", "takeover search hit, but not an offer for all shares"
    if takeover and terms["odd_lot_priority"]:
        takeover = False   # it's really a partial tender with odd-lot priority: handle it the odd-lot way
    if takeover:
        cik = rec.get("cik")
        if terms.get("board_recommends"):
            STANCE.setdefault(cik, "agreed")
        hostile_words = re.search(r"recommend(?:s|ed)?\s+that\s+[^.]{0,80}?(?:reject|not tender)", text or "", re.I) if text else None
        if hostile_words:
            STANCE[cik] = "hostile"
        if STANCE.get(cik) != "agreed":
            return "skip", "takeover is hostile or not recommended by the target's board"
    if takeover and terms.get("cover_cash_price"):
        terms = dict(terms, price=terms["cover_cash_price"])
    if not takeover and t in [x.upper() for x in cfg.get("excluded_tickers", [])]:
        return "skip", "investor already owns this stock elsewhere (excluded_tickers), so odd-lot priority could fail"
    cut = terms.get("ownership_cutoff")
    if not takeover and cut and cut < led.today:
        return "skip", f"odd-lot priority only for shareholders who already owned shares on {cut}"
    if not terms["cash_offer"]:
        if terms["odd_lot_priority"] and HOOKS["alerts"] and cfg.get("flag_share_swaps", True):
            try:
                ref = HOOKS["alerts"].open(f"Share-swap odd-lot offer spotted: {t}",
                                           f"**{rec.get('company', '')} ({t})** has an exchange offer with odd-lot priority. "
                                           f"The Hunter doesn't trade these automatically yet (it would need to value the shares you "
                                           f"receive), but they can be profitable for holders of fewer than 100 shares.\n\n"
                                           f"Filing: {rec.get('url', '')}", labels=("hunter", "share-swap"))
                HOOKS["alerts"].close(ref)
            except Exception as e:
                notes.append(f"Could not send share-swap alert: {e}")
            return "flag", "share-swap offer with odd-lot priority: alert sent, not traded automatically"
        return "skip", "share-exchange offer (v1 handles cash only)"
    if not takeover and not terms["odd_lot_priority"]:
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
        offer = terms["price"] or 0
        # Takeover stocks jump on the news, so near the offer price also counts as a match.
        matches = lambda ref: ref and (any(abs(ref / s_ - 1) <= 0.20 for s_ in stated)
                                       or (takeover and 0.6 * offer <= ref <= 1.05 * offer))
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
    min_prem = cfg.get("takeover_min_premium_pct", 1.0) if takeover else cfg["min_premium_pct"]
    if premium < min_prem:
        return "skip", f"premium only {premium:.1f}% (price ${px:.2f} vs offer ${payout:.2f})"
    if takeover and premium > cfg.get("takeover_max_spread_pct", 15.0):
        return "skip", (f"spread of {premium:.0f}% is too wide for a normal agreed takeover"
                        f"{' (offer includes a CVR)' if terms.get('has_cvr') else ''}: price or terms may be misread")
    if takeover and premium * 365 / max(days, 1) < cfg.get("takeover_min_annualized_pct", 10.0):
        return "skip", f"takeover spread {premium:.1f}% over {days} days is too thin"
    drop = cfg.get("takeover_assumed_drop", 0.35) if takeover else cfg["assumed_drop_if_deal_fails"]
    budget = led.max_new_position(drop)
    if takeover:
        shares = int(min(budget, cfg.get("takeover_max_position", 4000.0)) // px)
    else:
        shares = int(min(cfg["odd_lot_shares"], budget // px))
    fee = cfg.get("tender_fee", 0.0)
    profit = shares * (payout - px) - fee
    if shares < 1:
        return "skip", f"floor rule: no room (max new position ${budget:,.0f})"
    if profit < cfg["min_profit_dollars"]:
        return "skip", f"only ${profit:.0f} profit possible within the floor rule ({shares} sh{', after the $%g tender fee' % fee if fee else ''})"

    note = "; ".join([n for n in [note_extra] + terms["notes"] if n])
    if cfg.get("ai_review") and text is None:
        try:
            text = " ".join(src.text(d) for d in rec.get("_docs", [rec]))
        except Exception as e:
            return "retry", f"could not fetch filing: {e}"
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
                profit = shares * (payout - px) - fee
                if profit < cfg["min_profit_dollars"]:
                    return "skip", f"after AI correction only ${profit:.0f} profit"
            if verdict.get("red_flags"):
                note += "; flags: " + ", ".join(verdict["red_flags"])

    if fee:
        note += f"; ${fee:g} tender fee"
    deal = {"ticker": t, "company": rec.get("company", ""), "fee": fee, "accession": rec.get("accession", ""),
            "url": rec.get("url", ""), "payout": payout, "expiration": terms["expiration"],
            "dutch": terms["dutch_auction"], "note": note, "drop": drop,
            "engine": "takeover" if takeover else "odd-lot tender", "cvr": terms.get("has_cvr", False)}
    broker = HOOKS["broker"]
    deal["tender_status"] = "pending" if (broker and broker.real_account) else "assumed"
    pos_id = led.buy(deal, shares, px)
    after_buy(led, cfg, deal, shares, px, pos_id, notes)
    ann = premium * 365 / max(days, 1)
    return "BUY", f"{shares} sh @ ${px:.2f} -> ${payout:.2f} ({premium:.1f}%, ~{ann:.0f}% annualized), +${profit:.0f} if it completes"


def evaluate_merger(led, src, cfg, rec, text, notes):
    """Cash merger decided by a shareholder vote: buy below the merger price, get paid at closing."""
    import merger
    t = rec.get("ticker")
    if text is None:
        try:
            text = " ".join(src.text(d) for d in rec.get("_docs", [rec]))
        except Exception as e:
            return "retry", f"could not fetch filing: {e}"
    filed = dt.date.fromisoformat(rec["file_date"]) if rec.get("file_date") else led.today
    key = ("merger", rec.get("doc_id"))
    m = TERMS_MEMO.get(key) or TERMS_MEMO.setdefault(key, merger.terms(text, filed))
    if not m["price"]:
        return "skip", "could not read a cash merger price"
    if m["stock_component"]:
        return "skip", "part of the payment is in shares (v1 handles all-cash mergers only)"
    if not m["board_recommends"]:
        return "skip", "board does not recommend the merger (or the wording wasn't recognised)"
    if m["financing_condition"]:
        return "skip", "merger depends on financing"
    close = m["expected_close"]
    days = max((close - led.today).days, 14)
    if days > cfg.get("merger_max_days_to_close", 240):
        return "watch", f"expected to close {close}, too far away for now"
    px = _safe_price(src, t)
    if not px:
        return "retry", "no market price today"
    payout = m["price"]
    premium = (payout - px) / px * 100
    if premium < cfg.get("merger_min_premium_pct", 1.0):
        return "skip", f"spread only {premium:.1f}% (price ${px:.2f} vs ${payout:.2f} merger price)"
    if premium > cfg.get("merger_max_spread_pct", 15.0):
        return "skip", f"spread of {premium:.0f}% is too wide for an agreed merger: risk or terms may be misread"
    ann = premium * 365 / days
    if ann < cfg.get("merger_min_annualized_pct", 8.0):
        return "skip", f"{premium:.1f}% over ~{days} days (~{ann:.0f}%/yr) is too thin"
    drop = cfg.get("merger_assumed_drop", 0.35)
    budget = min(led.max_new_position(drop), cfg.get("merger_max_position", 4000.0))
    shares = int(budget // px)
    profit = shares * (payout - px)
    if shares < 1 or profit < cfg["min_profit_dollars"]:
        return "skip", f"only ${profit:.0f} profit possible within the floor rule"
    deal = {"ticker": t, "company": rec.get("company", ""), "accession": rec.get("accession", ""), "url": rec.get("url", ""),
            "payout": payout, "expiration": close, "dutch": False, "drop": drop, "engine": "merger", "fee": 0,
            "cvr": m["has_cvr"], "tender_status": "not needed",
            "note": f"Shareholder vote {m['meeting'] or 'date not stated'}; expected close {close}"
                    + ("; plus a CVR (not counted)" if m["has_cvr"] else "")}
    pos_id = led.buy(deal, shares, px)
    after_buy(led, cfg, deal, shares, px, pos_id, notes)
    return "BUY", f"{shares} sh @ ${px:.2f} -> ${payout:.2f} at closing ({premium:.1f}%, ~{ann:.0f}%/yr), +${profit:.0f} if it completes"


def derisk(led, src, cfg, notes):
    """If the stress test fails (usually right after a deal has failed), sell the open deal with the
    biggest potential loss at market until it passes again. Keeps the $2,000 promise intact."""
    guard = 0
    while led.open_positions() and led.stressed_value() < led.get("floor") - 1e-6 and guard < 20:
        guard += 1
        pos = max(led.open_positions(), key=lambda p: p["cost"] * led._drop(p))
        px = _pos_price(led, src, cfg, pos)
        if pos.get("engine") != "rocket":          # the rocket is paper-only: nothing at the broker
            sell_shares(pos, px, notes)
        led.close(pos, round(px * pos["shares"], 2), "reduced",
                  f"Sold at ${px:.2f} to keep the worst-case stress test above the floor after an earlier loss.")
        notes.append(f"De-risked: sold {pos['ticker']} to restore the safety margin.")


def _pos_price(led, src, cfg, pos):
    if pos.get("engine") == "rocket":
        import rocket
        try:
            v = rocket.value_per_share(pos, src, led.today, cfg)
        except Exception:
            v = None
        return pos["price_paid"] if v is None else v
    return _safe_price(src, pos["ticker"]) or pos["price_paid"]


def mark_to_market(led, src, cfg=None):
    value = led.get("cash")
    for p in led.open_positions():
        value += _pos_price(led, src, cfg or led.cfg, p) * p["shares"]
    return value


def monthly_cost(led, cfg):
    m = led.today.strftime("%Y-%m")
    if cfg.get("fixed_monthly_running_cost", 0) > 0 and led.get("last_cost_month") != m:
        led.charge(cfg["fixed_monthly_running_cost"], f"monthly running cost {m}")
        led.set("last_cost_month", m)


def run(cfg, offline=None, today=None, hooks=None):
    data = Path(cfg["data_dir"]) if Path(cfg["data_dir"]).is_absolute() else HERE / cfg["data_dir"]
    led = Ledger(data / ("paper_offline.db" if offline else "paper.db"), cfg, today=today)
    src = OfflineSources(offline) if offline else Sources(cfg)
    TERMS_MEMO.clear()
    STANCE.clear()
    notes = []
    rdir = Path(cfg["report_dir"]) if Path(cfg["report_dir"]).is_absolute() else HERE / cfg["report_dir"]
    from alerts import Alerts
    if hooks is not None:          # tests inject fakes
        HOOKS.update(hooks)
    else:
        HOOKS["alerts"] = None if offline else Alerts(cfg, rdir)
        HOOKS["broker"] = None if offline else make_broker(cfg, notes)
    try:
        rate = src.tbill_rate()
    except Exception:
        rate = None
    if rate is None:
        notes.append(f"Live T-bill rate unavailable today; used {cfg['cash_yield_annual'] * 100:.2f}%.")
    interest = led.accrue_cash_yield(rate)
    monthly_cost(led, cfg)
    settle(led, src, cfg, notes)
    new_floor = led.maybe_reset_floor(mark_to_market(led, src, cfg))
    if new_floor is not None:
        notes.append(f"New account year: the floor is now ${new_floor:,.2f}, "
                     f"{cfg.get('max_loss_pct', 0.20) * 100:.0f}% below the account's value today.")
    derisk(led, src, cfg, notes)
    hunt(led, src, cfg, notes)
    import rocket
    try:
        rocket.hunt(led, src, cfg, notes)
    except Exception as e:
        notes.append(f"Rocket step failed: {e}")
    lift = led.maybe_ratchet()
    equity = mark_to_market(led, src)
    led.record_equity(equity)
    import platform_features as pf
    try:
        pf.maybe_weekly_letter(led, cfg, equity, HOOKS["alerts"], notes)
        pf.maybe_monthly_review(led, cfg, src, HOOKS["alerts"], notes)
    except Exception as e:
        notes.append(f"Letter/review step failed: {e}")
    if HOOKS["broker"]:
        try:
            notes += HOOKS["broker"].reconcile(led)
            HOOKS["broker"].sweep(led, cfg)
        except Exception as e:
            notes.append(f"Broker check failed: {e}")
    path = report.write(led, cfg, equity, notes, interest, lift, rdir)
    report.write_trades(led, rdir)
    print(report.summary_line(led, cfg, equity))
    print(f"Report: {path}")
    return led, equity


def make_broker(cfg, notes):
    """'simulated' (default) means paper only, no broker. See broker.py for the others."""
    mode = cfg.get("broker_mode", "simulated")
    if mode == "simulated":
        return None
    if not cfg.get("trading_enabled", False):
        notes.append(f"Real-money trading is switched OFF (trading_enabled is false): running on paper. Broker set up: {mode}.")
        return None
    try:
        from broker import connect
        return connect(cfg)
    except Exception as e:
        notes.append(f"Broker not connected ({mode}): {e}. Running on paper only.")
        return None


def run_accounts(cfg, offline=None, today=None):
    """Run once per account in cfg["accounts"] (each with its own ledger, reports and settings)."""
    accounts = cfg.get("accounts") or [{"name": ""}]
    results = []
    for acc in accounts:
        c = {k: v for k, v in cfg.items() if k != "accounts"}
        c.update({k: v for k, v in acc.items() if k != "name"})
        name = acc.get("name", "")
        if name:
            c["data_dir"] = str(Path(cfg["data_dir"]) / name)
            c["report_dir"] = str(Path(cfg["report_dir"]) / name)
            print(f"== account: {name}")
        results.append(run(c, offline, today))
    return results


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
        run_accounts(cfg, a.offline, today)
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
