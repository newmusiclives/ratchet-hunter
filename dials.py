#!/usr/bin/env python3
"""Build the interactive "dials" page: the real 12-month backtest replayed under
every combination of settings, so anyone can slide the dials and see exact results.

    python3 dials.py            # after backtest.py has downloaded the data (uses the cache)

Writes published/dials.html (published to the website by the daily run).
"""
import datetime as dt
import itertools
import json
from pathlib import Path

import backtest
import hunter

HERE = Path(__file__).resolve().parent

GRID = {
    "min_premium_pct": [0.5, 1.0, 2.0, 3.0],
    "min_profit_dollars": [10.0, 15.0, 25.0, 50.0],
    "max_loss": [1000.0, 2000.0, 3000.0, 5000.0],
}


def _k(v):
    """Format numbers the way the browser does (1.0 -> "1"), so keys match."""
    return str(int(v)) if float(v).is_integer() else str(v)


def weekly(history):
    out = [h for i, h in enumerate(history) if i % 5 == 0]
    if history and out[-1] is not history[-1]:
        out.append(history[-1])
    return [[h["day"], round(h["equity"], 2), round(h["floor"], 2)] for h in out]


def build(days=365, end=None, cache_dir=backtest.CACHE, takeover_options=(False, True)):
    base = hunter.load_cfg()
    end = end or dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    has_takeovers = (Path(cache_dir) / f"takeovers_{start}_{end}.json").exists()
    takeover_options = takeover_options if has_takeovers else (False,)
    results = {}
    combos = list(itertools.product(*GRID.values(), takeover_options))
    for i, (prem, prof, loss, tko) in enumerate(combos, 1):
        cfg = dict(base, min_premium_pct=prem, min_profit_dollars=prof, max_loss=loss,
                   engines={"odd_lot": True, "takeover": tko})
        led, eq = backtest.run(days, offline=True, cfg=cfg, cache_dir=cache_dir, end=end, quiet=True, write_report=False)
        deals = [{"t": p["ticker"], "c": p["company"], "e": p.get("engine") or "odd-lot tender", "o": p["opened"],
                  "x": p["closed"], "pnl": round(p["pnl"] or 0, 2), "s": p["status"]} for p in led.closed_positions()]
        results[f"{_k(prem)}|{_k(prof)}|{_k(loss)}|{int(tko)}"] = {
            "eq": round(eq, 2), "floor": round(led.get("floor"), 2), "interest": round(led.get("interest_earned"), 2),
            "deals": deals, "curve": weekly(led.equity_history())}
        if i % 8 == 0:
            print(f"  dials: {i}/{len(combos)} combinations replayed", flush=True)
    print()
    page = TEMPLATE.replace("__DATA__", json.dumps(results)).replace("__GRID__", json.dumps(GRID)) \
        .replace("__TAKEOVER__", "true" if has_takeovers else "false") \
        .replace("__WINDOW__", f"{start} to {end}").replace("__DEFAULTS__", json.dumps(
            [base["min_premium_pct"], base["min_profit_dollars"], base["max_loss"]]))
    out = HERE / "published" / "dials.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page)
    print(f"Wrote {out}")
    return out


TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Ratchet - Hunter - Dials</title>
<style>
body{font-family:Georgia,'Iowan Old Style',serif;background:#faf8f3;color:#24221e;max-width:980px;margin:32px auto;padding:0 20px;line-height:1.45}
h1{font-size:28px;margin:0} h2{font-size:18px;margin:30px 0 10px;border-bottom:1px solid #e3dfd6;padding-bottom:6px}
.nav,.sub,label,table,.note{font-family:system-ui,sans-serif}
.nav{font-size:13px;margin:0 0 14px} .sub{color:#77736b;margin:4px 0 22px;font-size:14px} a{color:#1f5f8b}
.dials{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;background:#fff;border:1px solid #e3dfd6;border-radius:10px;padding:16px}
label{font-size:12px;color:#77736b;text-transform:uppercase;letter-spacing:.05em;display:block}
.seg{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.seg button{font:14px system-ui,sans-serif;border:1px solid #d6d3cc;background:#faf8f3;border-radius:6px;padding:6px 10px;cursor:pointer;color:#24221e}
.seg button.on{background:#1f5f8b;color:#fff;border-color:#1f5f8b}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:16px}
.tile{background:#fff;border:1px solid #e3dfd6;border-radius:10px;padding:14px 16px}
.tile .k{font:12px system-ui,sans-serif;color:#77736b;text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:24px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eeebe4} th{color:#77736b;font-weight:600}
.up{color:#2c7a4b} .down{color:#b23a2a} .note{font-size:13px;color:#77736b}
</style></head><body>
<p class="nav"><a href="index.html">Live paper run</a> &middot; <a href="backtest.html">12-month backtest</a> &middot; <b>Dials</b></p>
<h1>The Ratchet &middot; Dials</h1>
<p class="sub">Turn the dials and see exactly what the past year would have done. Every combination was replayed on the real SEC filings, __WINDOW__. Paper results, $10,000 start.</p>
<div class="dials" id="dials"></div>
<div class="tiles" id="tiles"></div>
<h2>Account value vs. floor</h2><div id="chart"></div>
<h2>Deals taken</h2><div id="deals"></div>
<p class="note">Results are a replay of past filings, not a forecast. "Max loss" sets the floor: the Hunter never risks more than this in total.</p>
<script>
const DATA=__DATA__, GRID=__GRID__, HAS_TKO=__TAKEOVER__, DEF=__DEFAULTS__;
const DIALS=[["min_premium_pct","Minimum premium",v=>v+"%"],["min_profit_dollars","Minimum profit per deal",v=>"$"+v],
             ["max_loss","Maximum total loss",v=>"$"+v.toLocaleString()]];
let st={min_premium_pct:DEF[0],min_profit_dollars:DEF[1],max_loss:DEF[2],tko:0};
const fmt=v=>"$"+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
function seg(key,label,vals,show){
  const d=document.createElement("div");d.innerHTML=`<label>${label}</label><div class="seg"></div>`;
  vals.forEach(v=>{const b=document.createElement("button");b.textContent=show(v);
    if(st[key]==v)b.className="on";b.onclick=()=>{st[key]=v;render()};d.querySelector(".seg").appendChild(b)});
  return d}
function chart(curve){
  if(curve.length<2)return"";const W=900,H=220,P=34,vals=curve.flatMap(c=>[c[1],c[2]]);
  let lo=Math.min(...vals),hi=Math.max(...vals);const pad=Math.max((hi-lo)*.15,50);lo-=pad;hi+=pad;
  const x=i=>P+(W-2*P)*i/(curve.length-1),y=v=>H-P-(H-2*P)*(v-lo)/(hi-lo);
  const pl=k=>curve.map((c,i)=>x(i).toFixed(1)+","+y(c[k]).toFixed(1)).join(" ");const L=curve[curve.length-1];
  return `<svg viewBox="0 0 ${W} ${H}" width="100%"><line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" stroke="#d6d3cc"/>
  <polyline points="${pl(2)}" fill="none" stroke="#b4532a" stroke-width="2" stroke-dasharray="5 4"/>
  <polyline points="${pl(1)}" fill="none" stroke="#1f5f8b" stroke-width="2.5"/>
  <text x="${W-P}" y="${y(L[1])-8}" text-anchor="end" fill="#1f5f8b" font-size="12">Equity ${fmt(L[1])}</text>
  <text x="${W-P}" y="${y(L[2])+16}" text-anchor="end" fill="#b4532a" font-size="12">Floor ${fmt(L[2])}</text>
  <text x="${P}" y="${H-10}" fill="#77736b" font-size="11">${curve[0][0]}</text>
  <text x="${W-P}" y="${H-10}" text-anchor="end" fill="#77736b" font-size="11">${L[0]}</text></svg>`}
function render(){
  const box=document.getElementById("dials");box.innerHTML="";
  DIALS.forEach(([k,l,s])=>box.appendChild(seg(k,l,GRID[k],s)));
  if(HAS_TKO)box.appendChild(seg("tko","Takeover engine",[0,1],v=>v?"On":"Off"));
  const r=DATA[[st.min_premium_pct,st.min_profit_dollars,st.max_loss,st.tko].join("|")];
  const ret=(r.eq/10000-1)*100,dp=r.deals.reduce((a,d)=>a+d.pnl,0),won=r.deals.filter(d=>d.pnl>0).length;
  document.getElementById("tiles").innerHTML=[["Year-end value",fmt(r.eq)],["Return",`<span class="${ret>=0?"up":"down"}">${ret>=0?"+":""}${ret.toFixed(2)}%</span>`],
    ["Deal profit",fmt(dp)],["Cash interest",fmt(r.interest)],["Deals / won",r.deals.length+" / "+won],["Final floor",fmt(r.floor)]]
    .map(([k,v])=>`<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  document.getElementById("chart").innerHTML=chart(r.curve);
  document.getElementById("deals").innerHTML=r.deals.length?`<table><tr><th>Ticker</th><th>Company</th><th>Type</th><th>Bought</th><th>Closed</th><th>Result</th><th>P&amp;L</th></tr>`+
    r.deals.map(d=>`<tr><td>${d.t}</td><td>${d.c}</td><td>${d.e}</td><td>${d.o}</td><td>${d.x}</td><td>${d.s}</td><td class="${d.pnl>=0?"up":"down"}">${d.pnl>=0?"+":""}${d.pnl.toFixed(2)}</td></tr>`).join("")+"</table>"
    :`<p class="note">No deals qualify at these settings. The money would just have earned T-bill interest.</p>`}
render();
</script></body></html>"""


if __name__ == "__main__":
    build()
