#!/usr/bin/env python3
"""Backtest the Rocket over every year since 1994, against the S&P 500 with dividends.

Each cycle lasts one year: keep the floor in T-bills, spend the rest of the risk budget
on a 1-year SPY call priced from that day's VIX and T-bill rate, collect the payoff at
expiry, raise the floor by half of any profit, repeat.

Two sizings are tested (see rocket.py):
  whole       real contracts of 100 shares, scaled so one contract costs what it costs
              today against a $10,000 account. This is what a $10,000 account can do.
  fractional  divisible at-the-money exposure (the idea in theory; needs ~$75k+ for real).

Run on GitHub (needs internet):  python rocket_backtest.py
Writes published/rocket.html and reports/rocket_*.csv.
Data: Yahoo daily closes for SPY (price and dividend-adjusted), ^VIX and ^IRX.
"""
import bisect
import csv
import datetime as dt
import json
import math
import statistics as st
import sys
from pathlib import Path

import rocket

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "rocket_cache"
START = dt.date(1993, 2, 1)
UA = {"User-Agent": "Mozilla/5.0 RatchetHunter/1.0"}

BASE = {"rocket_iv_add": 0.01, "rocket_skew": 0.2, "rocket_cost_pct": 0.02,
        "rocket_strike_pct": 100, "rocket_max_strike_pct": 130}


# --- data -----------------------------------------------------------------------
def fetch(ticker, end):
    """{date: (close, adjclose)} from Yahoo, cached on disk."""
    import requests
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{ticker.replace('^', '_')}.json"
    if f.exists() and json.loads(f.read_text()).get("end") == end.isoformat():
        return {k: tuple(v) for k, v in json.loads(f.read_text())["data"].items()}
    p1 = int(dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc).timestamp())
    p2 = int(dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc).timestamp()) + 86400
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", headers=UA, timeout=60,
                     params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,split",
                             "includeAdjustedClose": "true"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or closes
    out = {}
    for ts, c, a in zip(res["timestamp"], closes, adj):
        if c:
            out[dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()] = (float(c), float(a or c))
    f.write_text(json.dumps({"end": end.isoformat(), "data": out}))
    return out


class Series:
    def __init__(self, spy, vix, irx):
        self.days = sorted(spy)
        self.spy, self.vix, self.irx = spy, vix, irx
        self.vdays, self.idays = sorted(vix), sorted(irx)

    def on_or_before(self, day):
        i = bisect.bisect_right(self.days, day.isoformat()) - 1
        return self.days[i] if i >= 0 else None

    def _last(self, days, table, day):
        i = bisect.bisect_right(days, day) - 1
        return table[days[i]][0] if i >= 0 else None

    def snap(self, day):
        """Prices on the last trading day on/before `day`."""
        d = self.on_or_before(day)
        close, adj = self.spy[d]
        return d, close, adj, self._last(self.vdays, self.vix, d), self._last(self.idays, self.irx, d)

    def div_yield(self, d):
        """Trailing 1-year dividend yield of SPY from price vs dividend-adjusted closes."""
        prev = self.on_or_before(dt.date.fromisoformat(d) - dt.timedelta(days=365))
        if not prev or prev == d:
            return 0.015
        c0, a0 = self.spy[prev]
        c1, a1 = self.spy[d]
        q = math.log((a1 / a0) / (c1 / c0))
        return min(0.04, max(0.0, q))


# --- one year, and a chain of years -----------------------------------------------
def one_year(ser, start, equity, floor, cfg):
    d0, S0, A0, vix, irx = ser.snap(start)
    end = dt.date.fromisoformat(d0) + dt.timedelta(days=365)
    d1, S1, A1, _, _ = ser.snap(end)
    budget = max(0.0, min(equity - floor, cfg["budget_share"] * equity))
    pick = rocket.choose(S0, vix, irx, ser.div_yield(d0), budget, cfg)
    cost = pick["cost"] if pick else 0.0
    cash = (equity - cost) * (1 + (irx or 0) / 100)
    got = rocket.payoff(pick["units"], pick["strike"], S1) if pick else 0.0
    new = cash + got
    if new > equity:
        floor = min(new, floor + cfg.get("ratchet_share", 0.5) * (new - equity))
    return {"start": d0, "end": d1, "equity0": equity, "equity1": new, "floor1": floor, "cost": cost,
            "strike_pct": (pick["strike"] / S0 * 100) if pick else None, "payoff": got,
            "ret": new / equity - 1, "spx": A1 / A0 - 1, "vix": vix, "irx": irx}


def chain(ser, start, years, cfg, capital=10000.0, max_loss=2000.0):
    eq, floor, rows = capital, capital - max_loss, []
    day = start
    for _ in range(years):
        r = one_year(ser, day, eq, floor, cfg)
        rows.append(r)
        eq, floor = r["equity1"], r["floor1"]
        day = dt.date.fromisoformat(r["end"]) + dt.timedelta(days=1)
    return rows


def cagr(rows, key):
    g = 1.0
    for r in rows:
        g *= 1 + r[key]
    return g ** (1 / len(rows)) - 1


def mode_cfg(name, ser, start, latest_spy):
    c = dict(BASE, budget_share=0.2, ratchet_share=0.5)
    if name == "fractional":
        c["rocket_mode"] = "fractional"
    else:
        c["rocket_mode"] = "whole"
        S0 = ser.snap(start)[1]
        c["rocket_contract_shares"] = 100 * latest_spy / S0   # one contract costs, vs $10k, what it costs today
    return c


def month_starts(first, last):
    d = dt.date(first.year, first.month, 1)
    while d <= last:
        yield d
        d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


# --- report -------------------------------------------------------------------
def pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def svg_lines(series, w=760, h=300):
    """series: [(label, colour, [(date, value)])]."""
    allv = [v for _, _, pts in series for _, v in pts]
    lo, hi = math.log(min(allv)), math.log(max(allv))
    d0 = dt.date.fromisoformat(series[0][2][0][0]).toordinal()
    d1 = dt.date.fromisoformat(series[0][2][-1][0]).toordinal()
    px = lambda d: 50 + (dt.date.fromisoformat(d).toordinal() - d0) / (d1 - d0) * (w - 70)
    py = lambda v: h - 30 - (math.log(v) - lo) / (hi - lo) * (h - 50)
    out = [f"<svg viewBox='0 0 {w} {h}' style='width:100%;max-width:{w}px;font:12px system-ui'>"]
    for v in [10000, 20000, 40000, 80000, 160000, 320000]:
        if math.log(v) >= lo - 0.01 and math.log(v) <= hi + 0.01:
            out.append(f"<line x1='50' x2='{w - 20}' y1='{py(v):.1f}' y2='{py(v):.1f}' stroke='#eee'/>"
                       f"<text x='4' y='{py(v) + 4:.1f}' fill='#888'>${v // 1000}k</text>")
    for y in range(int(series[0][2][0][0][:4]) + 1, int(series[0][2][-1][0][:4]) + 1, 4):
        x = px(f"{y}-01-01")
        out.append(f"<text x='{x - 14:.1f}' y='{h - 8}' fill='#888'>{y}</text>")
    for i, (label, col, pts) in enumerate(series):
        path = " ".join(f"{'M' if j == 0 else 'L'}{px(d):.1f},{py(v):.1f}" for j, (d, v) in enumerate(pts))
        out.append(f"<path d='{path}' fill='none' stroke='{col}' stroke-width='2.2'/>")
        lx, ly = px(pts[-1][0]), py(pts[-1][1])
        out.append(f"<text x='{lx - 6:.1f}' y='{ly - 6 - i * 0:.1f}' text-anchor='end' fill='{col}'>{label}</text>")
    out.append("</svg>")
    return "".join(out)


def table(head, rows):
    h = "".join(f"<th>{x}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{b}</table>"


def main():
    today = dt.date.today()
    spy, vix, irx = fetch("SPY", today), fetch("^VIX", today), fetch("^IRX", today)
    ser = Series(spy, vix, irx)
    latest = ser.spy[ser.days[-1]][0]
    last_full = dt.date.fromisoformat(ser.days[-1]) - dt.timedelta(days=366)
    first = dt.date(1994, 2, 1)
    modes = ["whole", "fractional"]
    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)

    # 1) every single year (monthly start dates)
    single = {m: [] for m in modes}
    for s in month_starts(first, last_full):
        for m in modes:
            single[m].append(one_year(ser, s, 10000.0, 8000.0, mode_cfg(m, ser, s, latest)))
    # 2) every 10-year stretch, chained with the ratchet
    tens = {m: [] for m in modes}
    for s in month_starts(first, dt.date.fromisoformat(ser.days[-1]) - dt.timedelta(days=3653)):
        for m in modes:
            rows = chain(ser, s, 10, mode_cfg(m, ser, s, latest))
            tens[m].append({"start": rows[0]["start"], "cagr": cagr(rows, "ret"), "spx": cagr(rows, "spx"),
                            "worst": min(r["ret"] for r in rows), "spx_worst": min(r["spx"] for r in rows),
                            "end": rows[-1]["equity1"]})
    # 3) the most recent ten years, and the long run since 2000
    recent_start = dt.date.fromisoformat(ser.days[-1]) - dt.timedelta(days=3653)
    recent = {m: chain(ser, recent_start, 10, mode_cfg(m, ser, recent_start, latest)) for m in modes}
    long_start = dt.date(2000, 1, 3)
    n_long = (dt.date.fromisoformat(ser.days[-1]) - long_start).days // 365
    longrun = {m: chain(ser, long_start, n_long, mode_cfg(m, ser, long_start, latest)) for m in modes}

    for m in modes:
        with open(out_dir / f"rocket_years_{m}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(single[m][0]))
            w.writeheader()
            w.writerows(single[m])

    # summary lines (also printed for the run log)
    lines, summ = [], []
    for m in modes:
        ys = single[m]
        rets = [y["ret"] for y in ys]
        beat = sum(y["ret"] > y["spx"] for y in ys) / len(ys)
        hit13 = sum(y["ret"] >= 0.13 for y in ys) / len(ys)
        t = tens[m]
        beat10 = sum(x["cagr"] > x["spx"] for x in t) / len(t)
        over13 = sum(x["cagr"] >= 0.13 for x in t) / len(t)
        rc = cagr(recent[m], "ret")
        lc = cagr(longrun[m], "ret")
        summ.append([m, pct(st.median(rets)), pct(min(rets)), pct(max(rets)), f"{beat * 100:.0f}%", f"{hit13 * 100:.0f}%",
                     pct(st.median([x["cagr"] for x in t])), f"{beat10 * 100:.0f}%", f"{over13 * 100:.0f}%", pct(rc), pct(lc)])
        lines.append(f"[{m}] 1-yr median {pct(st.median(rets))}, worst {pct(min(rets))}, best {pct(max(rets))}, "
                     f"beat S&P in {beat * 100:.0f}% of years, >=13% in {hit13 * 100:.0f}% | 10-yr median CAGR "
                     f"{pct(st.median([x['cagr'] for x in t]))}, beat S&P in {beat10 * 100:.0f}% of 10-yr spans, "
                     f">=13% in {over13 * 100:.0f}% | last 10 yrs {pct(rc)}/yr | since 2000 {pct(lc)}/yr")
    spx_years = [y["spx"] for y in single["whole"]]
    spx_t = tens["whole"]
    spx_line = [ "S&P 500 (with dividends)", pct(st.median(spx_years)), pct(min(spx_years)), pct(max(spx_years)), "",
                 f"{sum(y >= 0.13 for y in spx_years) / len(spx_years) * 100:.0f}%",
                 pct(st.median([x['spx'] for x in spx_t])), "",
                 f"{sum(x['spx'] >= 0.13 for x in spx_t) / len(spx_t) * 100:.0f}%",
                 pct(cagr(recent['whole'], 'spx')), pct(cagr(longrun['whole'], 'spx'))]
    lines.append(f"[S&P 500 TR] 1-yr median {spx_line[1]}, worst {spx_line[2]} | 10-yr median {spx_line[6]} | "
                 f"last 10 yrs {spx_line[9]}/yr | since 2000 {spx_line[10]}/yr")
    for l in lines:
        print(l, flush=True)

    # growth chart since 2000
    def pts(rows, key):
        v, out = 10000.0, [(rows[0]["start"], 10000.0)]
        for r in rows:
            v *= 1 + r[key]
            out.append((r["end"], v))
        return out
    chart = svg_lines([("Rocket, whole contracts", "#c2410c", pts(longrun["whole"], "ret")),
                       ("Rocket, theory", "#7c3aed", pts(longrun["fractional"], "ret")),
                       ("S&P 500", "#2563eb", pts(longrun["whole"], "spx"))])
    yr_rows = [[r["start"], pct(r["spx"]), pct(r["ret"]), f"${r['cost']:,.0f}",
                f"{r['strike_pct']:.0f}%" if r["strike_pct"] else "none", f"${r['equity1']:,.0f}", f"${r['floor1']:,.0f}"]
               for r in recent["whole"]]
    html = f"""<!doctype html><meta charset='utf-8'><title>The Rocket: backtest</title>
<style>body{{font:15px/1.5 system-ui;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
table{{border-collapse:collapse;margin:10px 0 22px;font-size:13px}}td,th{{border:1px solid #ddd;padding:4px 8px;text-align:right}}
th{{background:#f6f4ef}}td:first-child,th:first-child{{text-align:left}}.note{{color:#666;font-size:13px}}</style>
<p><a href='index.html'>Live paper run</a> &middot; <a href='backtest.html'>12-month backtest</a> &middot; <a href='dials.html'>Dials</a> &middot; <b>Rocket</b></p>
<h1>The Rocket: S&amp;P upside with a $2,000 loss limit</h1>
<p>Each year: keep the $8,000 floor (it only ratchets up) in T-bills, spend up to the rest of the risk budget
(at most 20% of the account) on a 1-year SPY call option, collect what it pays, raise the floor by half of any profit.
Replayed on every month since 1994 with real SPY, VIX and T-bill data up to {ser.days[-1]}. <b>Model results, not real trades.</b></p>
<h2>Growth of $10,000 since January 2000</h2>{chart}
<h2>Summary</h2>
{table(["", "1-yr median", "worst year", "best year", "beat S&P (yrs)", "years ≥13%", "10-yr median /yr", "beat S&P (10-yr spans)", "10-yr spans ≥13%/yr", "last 10 yrs /yr", "since 2000 /yr"],
       summ + [spx_line])}
<p class='note'><b>whole</b> = real contracts of 100 shares, scaled so one contract costs against a $10,000 account what it costs today
(SPY ${latest:,.2f}); with $2,000 that means one contract at a strike above today's price. <b>fractional</b> = the idea in theory,
at-the-money and divisible; doing it for real needs an account of roughly $75,000 or more.</p>
<h2>The last ten years, year by year (whole contracts)</h2>
{table(["year starting", "S&P 500", "Rocket account", "option cost", "strike vs SPY", "account at end", "floor at end"], yr_rows)}
<h2>How the options are priced</h2>
<p class='note'>Black-Scholes, 1-year expiry. Volatility = that day's VIX + 1 point, a little lower for higher strikes (S&amp;P skew).
Interest = that day's 13-week T-bill. Dividends = SPY's trailing yield. Plus 2% on every premium for the bid/ask spread.
Real option prices differ from any model; check live quotes before trading. The S&amp;P line includes reinvested dividends.
The Hunter's deal profits are <b>not</b> included here; they come on top of the T-bill interest.</p>
"""
    pub = HERE / "published"
    pub.mkdir(exist_ok=True)
    (pub / "rocket.html").write_text(html)
    print("Wrote published/rocket.html")


if __name__ == "__main__":
    sys.exit(main())
