"""Daily report: one HTML page you can open in any browser."""
import datetime as dt
import html
import os
from pathlib import Path


def summary_line(led, cfg, equity):
    start = cfg["starting_capital"]
    floor = led.get("floor")
    days = (led.today - dt.date.fromisoformat(led.get("start_day"))).days
    return (f"[{led.today}] Day {days}: equity ${equity:,.2f} ({(equity / start - 1) * 100:+.2f}%), "
            f"floor ${floor:,.2f}, stressed value ${led.stressed_value():,.2f}, "
            f"open deals {len(led.open_positions())}")


def _svg(history):
    if len(history) < 2:
        return "<p class='muted'>The chart appears after the second day.</p>"
    W, H, P = 760, 220, 34
    vals = [h["equity"] for h in history] + [h["floor"] for h in history]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.15, 50)
    lo, hi = lo - pad, hi + pad
    n = len(history)
    x = lambda i: P + (W - 2 * P) * i / (n - 1)
    y = lambda v: H - P - (H - 2 * P) * (v - lo) / (hi - lo)
    eq = " ".join(f"{x(i):.1f},{y(h['equity']):.1f}" for i, h in enumerate(history))
    fl = " ".join(f"{x(i):.1f},{y(h['floor']):.1f}" for i, h in enumerate(history))
    last = history[-1]
    return f"""<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Equity and floor over time">
  <line x1="{P}" y1="{H-P}" x2="{W-P}" y2="{H-P}" stroke="#d6d3cc"/>
  <polyline points="{fl}" fill="none" stroke="#b4532a" stroke-width="2" stroke-dasharray="5 4"/>
  <polyline points="{eq}" fill="none" stroke="#1f5f8b" stroke-width="2.5"/>
  <text x="{W-P}" y="{y(last['equity'])-8:.1f}" text-anchor="end" fill="#1f5f8b" font-size="12">Equity ${last['equity']:,.0f}</text>
  <text x="{W-P}" y="{y(last['floor'])+16:.1f}" text-anchor="end" fill="#b4532a" font-size="12">Floor ${last['floor']:,.0f}</text>
  <text x="{P}" y="{H-10}" fill="#77736b" font-size="11">{history[0]['day']}</text>
  <text x="{W-P}" y="{H-10}" text-anchor="end" fill="#77736b" font-size="11">{last['day']}</text>
</svg>"""


def _table(rows, cols):
    if not rows:
        return "<p class='muted'>None yet.</p>"
    head = "".join(f"<th>{html.escape(c[0])}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c[1](r)}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def write(led, cfg, equity, notes, interest, lift, outdir, title=None, filename=None, seen_limit=40):
    outdir.mkdir(parents=True, exist_ok=True)
    start = cfg["starting_capital"]
    floor = led.get("floor")
    stressed = led.stressed_value()
    days = (led.today - dt.date.fromisoformat(led.get("start_day"))).days
    closed = led.closed_positions()
    realized = sum(p["pnl"] for p in closed)
    wins = sum(1 for p in closed if p["pnl"] > 0)
    e = html.escape
    money = lambda v: f"${v:,.2f}"

    open_tbl = _table(led.open_positions(), [
        ("Ticker", lambda p: f"<a href='{e(p['url'])}'>{e(p['ticker'])}</a>"),
        ("Company", lambda p: e(p["company"])),
        ("Type", lambda p: e(p.get("engine") or "odd-lot tender") + (" <b style='color:#b4532a'>(experimental)</b>"
                            if (p.get("engine") or "") in cfg.get("experimental_engines", []) else "")),
        ("Shares", lambda p: p["shares"]),
        ("Paid", lambda p: money(p["price_paid"])),
        ("Tender pays", lambda p: money(p["payout_price"])),
        ("Expected profit", lambda p: money(p["shares"] * (p["payout_price"] - p["price_paid"]))),
        ("Expires", lambda p: p["expiration"]),
        ("Notes", lambda p: e(p["note"] or "")),
    ])
    closed_tbl = _table(list(reversed(closed)), [
        ("Ticker", lambda p: e(p["ticker"])),
        ("Type", lambda p: e(p.get("engine") or "odd-lot tender") + (" (experimental)"
                            if (p.get("engine") or "") in cfg.get("experimental_engines", []) else "")),
        ("Result", lambda p: e(p["status"])),
        ("Cost", lambda p: money(p["cost"])),
        ("Proceeds", lambda p: money(p["proceeds"])),
        ("P&L", lambda p: f"<b class='{'up' if p['pnl'] >= 0 else 'down'}'>{p['pnl']:+,.2f}</b>"),
        ("Closed", lambda p: p["closed"]),
    ])
    seen_tbl = _table(led.recent_seen(seen_limit), [
        ("Seen", lambda s: s["ts"]),
        ("Decision", lambda s: f"<b>{e(s['decision'])}</b>"),
        ("Why", lambda s: e(s["reason"])),
    ])
    events = "".join(f"<li><span class='muted'>{e(ev['ts'])}</span> <b>{e(ev['kind'])}</b> {e(ev['detail'])}</li>"
                     for ev in led.recent_events(20))
    today_notes = "".join(f"<li>{e(n)}</li>" for n in notes) or "<li>Nothing unusual.</li>"

    lt = led.get("latest_letter")
    LETTER = (f"<h2>This week's letter <span style='font:13px system-ui;color:#8a867d'>&middot; {e(lt['date'])}</span></h2>"
              f"<div class='tile' style='font-size:16px;line-height:1.6'>{'<br><br>'.join(e(p) for p in lt['text'].split(chr(10) + chr(10)))}</div>"
              if lt else "")
    NAV = ("<p class='nav'><a href='index.html'>Live paper run</a> &middot; <a href='backtest.html'>12-month backtest</a> &middot; <a href='dials.html'>Dials</a> &middot; <a href='rocket.html'>Rocket</a></p>"
           if os.environ.get("HUNTER_SITE") else "")
    live_mode = cfg.get("broker_mode", "simulated") != "simulated"
    MODE = ("" if not live_mode else
            f"<p class='banner' style='margin:0 0 12px'><b>{'LIVE ACCOUNT: real-money trading is ON' if cfg.get('trading_enabled') else 'LIVE ACCOUNT, still on paper: real-money trading is switched OFF'}</b>"
            f" &middot; broker: {e(cfg.get('broker_name', ''))} ({e(cfg.get('broker_mode'))})</p>")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Ratchet - Hunter - {e(title or str(led.today))}</title>
<style>
body{{font-family:Georgia,'Iowan Old Style',serif;background:#faf8f3;color:#24221e;max-width:980px;margin:32px auto;padding:0 20px;line-height:1.45}}
h1{{font-size:28px;margin:0}} h2{{font-size:18px;margin:34px 0 10px;border-bottom:1px solid #e3dfd6;padding-bottom:6px}}
.sub{{color:#77736b;margin:4px 0 22px;font-family:system-ui,sans-serif;font-size:14px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.tile{{background:#fff;border:1px solid #e3dfd6;border-radius:10px;padding:14px 16px}}
.tile .k{{font-family:system-ui,sans-serif;font-size:12px;color:#77736b;text-transform:uppercase;letter-spacing:.05em}}
.tile .v{{font-size:24px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;font-family:system-ui,sans-serif;font-size:13px;background:#fff}}
th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid #eeebe4;vertical-align:top}} th{{color:#77736b;font-weight:600}}
.muted{{color:#8a867d}} .up{{color:#2c7a4b}} .down{{color:#b23a2a}}
ul{{font-family:system-ui,sans-serif;font-size:13px;padding-left:18px}} a{{color:#1f5f8b}}
.nav{{font-family:system-ui,sans-serif;font-size:13px;margin:0 0 14px}}
.banner{{background:#fff4e5;border:1px solid #f0d9b5;border-radius:8px;padding:10px 14px;font-family:system-ui,sans-serif;font-size:13px}}
</style></head><body>
{NAV}{MODE}<h1>The Ratchet &middot; Hunter</h1>
<p class="sub">{e(title) + ' &middot; ' if title else ''}Paper trading report for {led.today} &middot; day {days} &middot; no real money is used</p>
<div class="tiles">
 <div class="tile"><div class="k">Account value</div><div class="v">{money(equity)}</div></div>
 <div class="tile"><div class="k">Return so far</div><div class="v {'up' if equity >= start else 'down'}">{(equity / start - 1) * 100:+.2f}%</div></div>
 <div class="tile"><div class="k">Floor (never below)</div><div class="v">{money(floor)}</div></div>
 <div class="tile"><div class="k">Worst case if every deal fails</div><div class="v">{money(stressed)}</div></div>
 <div class="tile"><div class="k">Deals closed / won</div><div class="v">{len(closed)} / {wins}</div></div>
 <div class="tile"><div class="k">Realized deal profit</div><div class="v">{money(realized)}</div></div>
 <div class="tile"><div class="k">Cash interest earned</div><div class="v">{money(led.get('interest_earned'))}</div></div>
 <div class="tile"><div class="k">AI &amp; running costs</div><div class="v">{money(led.get('running_costs'))}</div></div>
</div>
{LETTER}<h2>Account value vs. floor</h2>
{_svg(led.equity_history())}
<h2>{'About this run' if title else 'Today'}</h2>
<ul>{today_notes}{'' if title else f'<li>Cash interest added today: {money(interest)}</li>'}{f'<li>Ratchet raised the floor by {money(lift)}</li>' if lift else ''}
<li>Room for a new deal under the floor rule: {money(led.max_new_position())}</li>
<li>Cash earns the T-bill rate: {(led.get('cash_rate') or cfg['cash_yield_annual']) * 100:.2f}% a year</li></ul>
<h2>Open deals</h2>{open_tbl}
<h2>Closed deals <span style="font:13px system-ui;color:#8a867d">&middot; <a href="trades.csv">download all trades (CSV)</a></span></h2>{closed_tbl}
<h2>Every filing the Hunter looked at</h2>{seen_tbl}
<h2>Activity log</h2><ul>{events}</ul>
<p class="banner">Paper results assume odd-lot tenders complete at the stated price once the offer expires,
unless a filing shows the offer was terminated. Dutch auctions are counted at the low end of the range.
Real results can differ.</p>
</body></html>"""
    if filename:
        path = outdir / filename
        path.write_text(page)
        return path
    path = outdir / f"report_{led.today}.html"
    path.write_text(page)
    (outdir / "latest.html").write_text(page)
    return path


def write_trades(led, outdir):
    """trades.csv: every position, for the accountant and for checking."""
    import csv
    outdir.mkdir(parents=True, exist_ok=True)
    rows = led.closed_positions() + led.open_positions()
    cols = ["ticker", "company", "engine", "shares", "price_paid", "cost", "payout_price", "opened", "closed",
            "proceeds", "pnl", "status", "tender_status", "expiration", "url", "note"]
    with (outdir / "trades.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})
    return outdir / "trades.csv"
