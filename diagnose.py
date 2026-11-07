#!/usr/bin/env python3
"""Print what the rules read in every takeover filing of the cached backtest window,
and why each was taken or skipped. Used to tune the takeover engine."""
import datetime as dt
import json
import re
from pathlib import Path

import backtest
import hunter
from extract import extract_terms

end = dt.date.today() - dt.timedelta(days=1)
start = end - dt.timedelta(days=365)
cache = backtest.CACHE
tk = json.loads((cache / f"takeovers_{start}_{end}.json").read_text())
groups = {}
for d in tk:
    groups.setdefault(d["accession"], []).append(d)
print(f"{len(tk)} takeover documents in {len(groups)} filings")
for acc, docs in sorted(groups.items(), key=lambda x: x[1][0]["file_date"]):
    d0 = docs[0]
    text = " ".join((cache / "filings" / (backtest._safe(d["doc_id"]) + ".txt")).read_text()
                    for d in docs if (cache / "filings" / (backtest._safe(d["doc_id"]) + ".txt")).exists())
    t = extract_terms(text)
    flat = re.sub(r"\s+", " ", text)
    rec = re.search(r".{0,140}recommend.{0,160}", flat, re.I)
    cash = re.search(r".{0,60}per (?:share|Share)[^.$]{0,40}?net to the [a-z]+s? in cash.{0,40}", flat, re.I)
    print(f"\n{d0['file_date']} {d0.get('ticker')} {d0['company'][:40]} docs={len(docs)}")
    print(f"   price={t['price']} cover_cash={t['cover_cash_price']} cvr={t['has_cvr']} board_ok={t['board_recommends']} "
          f"all_shares={t['all_shares']} exp={t['expiration']} fin={t['financing_condition']} odd={t['odd_lot_priority']}")
    print(f"   RECOMMEND: {rec.group(0)[:300] if rec else '-'}")
    print(f"   CASH: {cash.group(0)[:200] if cash else '-'}")

cfg = dict(hunter.load_cfg(), engines={"odd_lot": True, "takeover": True})
led, eq = backtest.run(365, offline=True, cfg=cfg, cache_dir=cache, end=end, quiet=True, write_report=False)
print(f"\nTakeover-on result: ${eq:,.2f}")
for s in led.recent_seen(2000):
    print("  ", s["ts"], s["decision"], s["reason"][:200])
