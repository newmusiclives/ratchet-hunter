"""The Rocket: S&P 500 upside with a capped loss.

Part of the risk budget buys a one-year call option on SPY (an S&P 500 fund).
The most an option can lose is what was paid for it, so the floor rule holds:
the option counts in the stress test as a deal that can lose 100%.

Paper only. Options are priced with Black-Scholes, using the VIX as the
volatility and the 13-week T-bill as the interest rate. Real fills will differ;
the cost_pct setting adds a margin for the bid/ask spread.

Two ways to size the option:
  "whole"      real-world: whole contracts of 100 shares. With SPY near $700, one
               at-the-money 1-year contract costs about $6,000, so a $2,000 budget
               buys one contract at a higher strike (out of the money).
  "fractional" theory: any amount at the money, as if contracts were divisible.
               Shows the idea; needs an account of roughly $75,000+ to do for real.
"""
import datetime as dt
import json
import math

TICKER = "SPY"


# --- pricing ------------------------------------------------------------------
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, r, q, sigma):
    """Black-Scholes price of a European call, per share."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def vol_for(vix, k_ratio, cfg):
    """Volatility for a strike. VIX (in points) for at-the-money, plus a safety margin;
    out-of-the-money calls trade at somewhat lower volatility (the S&P 'skew')."""
    atm = max(vix, 8.0) / 100 + cfg.get("rocket_iv_add", 0.01)
    skewed = atm - cfg.get("rocket_skew", 0.2) * max(0.0, k_ratio - 1)
    return max(0.07, skewed)


def rate_from_irx(irx_pct):
    return math.log(1 + (irx_pct or 0) / 100)


def price(S, k_ratio, vix, irx_pct, q, cfg, T=1.0):
    """Premium per share including the spread margin."""
    K = S * k_ratio
    prem = bs_call(S, K, T, rate_from_irx(irx_pct), q, vol_for(vix, k_ratio, cfg))
    return K, prem * (1 + cfg.get("rocket_cost_pct", 0.02))


def choose(S, vix, irx_pct, q, budget, cfg, T=1.0):
    """Pick what to buy with `budget`. Returns None if nothing fits.
    Result: strike, premium per share, units (shares of exposure), cost."""
    mode = cfg.get("rocket_mode", "whole")
    if budget <= 0:
        return None
    if mode == "fractional":
        K, prem = price(S, cfg.get("rocket_strike_pct", 100) / 100, vix, irx_pct, q, cfg, T)
        units = budget / prem
        return {"strike": K, "premium": prem, "units": units, "cost": round(units * prem, 2), "contracts": None}
    lo = int(cfg.get("rocket_strike_pct", 100))
    hi = int(cfg.get("rocket_max_strike_pct", 130))
    for pct in range(lo, hi + 1):                    # lowest strike whose one contract fits
        K, prem = price(S, pct / 100, vix, irx_pct, q, cfg, T)
        size = cfg.get("rocket_contract_shares", 100)
        per = prem * size
        if per <= budget:
            n = int(budget // per)
            return {"strike": K, "premium": prem, "units": n * size, "cost": round(n * per, 2), "contracts": n}
    return None


def payoff(units, K, S_T):
    return units * max(0.0, S_T - K)


# --- the paper engine ---------------------------------------------------------
def _vix(src):
    try:
        return src.price("^VIX")
    except Exception:
        return None


def _q(cfg):
    return cfg.get("rocket_div_yield", 0.012)


def value_per_share(pos, src, today, cfg):
    """What the option is worth today (per share), for the daily mark and for de-risking."""
    d = json.loads(pos.get("deal_json") or "{}")
    S = src.price(TICKER)
    vix = _vix(src)
    if not S:
        return None
    T = max(0.0, (dt.date.fromisoformat(pos["expiration"]) - today).days / 365)
    if T == 0 or not vix:
        return max(0.0, S - d["strike"])
    try:
        irx = src.price("^IRX")
    except Exception:
        irx = None
    k_ratio = d["strike"] / S
    return bs_call(S, d["strike"], T, rate_from_irx(irx if irx else 4.0), _q(cfg), vol_for(vix, k_ratio, cfg))


def mark_value(led, src, cfg):
    import hunter
    return hunter.mark_to_market(led, src, cfg)


def settle(led, src, cfg, notes):
    for pos in led.open_positions():
        if pos.get("engine") != "rocket":
            continue
        if led.today < dt.date.fromisoformat(pos["expiration"]):
            continue
        S = src.price(TICKER)
        if not S:
            notes.append("Rocket: SPY price unavailable at expiry; will settle tomorrow.")
            continue
        d = json.loads(pos.get("deal_json") or "{}")
        proceeds = round(payoff(pos["shares"], d["strike"], S), 2)
        led.close(pos, proceeds, "settled",
                  f"Option expired with SPY at ${S:.2f} against a ${d['strike']:.2f} strike.")
        notes.append(f"Rocket settled: ${proceeds:,.2f} back from a ${pos['cost']:,.2f} option.")


def hunt(led, src, cfg, notes):
    """Buy one year of S&P upside if none is held and the floor rule leaves room."""
    if not cfg.get("engines", {}).get("rocket"):
        return
    if cfg.get("trading_enabled") and cfg.get("broker_mode", "simulated") != "simulated":
        notes.append("Rocket: paper-only for now; skipped because a real broker is switched on.")
        return
    if any(p.get("engine") == "rocket" for p in led.open_positions()):
        return
    room = led.max_new_position(drop=1.0)
    cap = cfg.get("rocket_budget", 2000.0)
    if cfg.get("rocket_budget_share"):                      # grows with the account
        cap = cfg["rocket_budget_share"] * mark_value(led, src, cfg)
    budget = min(cap, room)
    if budget < cfg.get("rocket_min_budget", 300.0):
        notes.append(f"Rocket: waiting. The floor rule leaves ${room:,.0f} of room (needs "
                     f"${cfg.get('rocket_min_budget', 300.0):,.0f}); open deals are using the risk budget.")
        return
    S, vix = src.price(TICKER), _vix(src)
    if not S or not vix:
        notes.append("Rocket: SPY or VIX price unavailable; skipped today.")
        return
    try:
        irx = src.price("^IRX")
    except Exception:
        irx = None
    pick = choose(S, vix, irx or 4.0, _q(cfg), budget, cfg)
    if not pick:
        notes.append(f"Rocket: no 1-year SPY call up to {cfg.get('rocket_max_strike_pct', 130)}% strike fits ${budget:,.0f}.")
        return
    exp = led.today + dt.timedelta(days=365)
    units = int(round(pick["units"])) if pick["contracts"] else pick["units"]
    deal = {"ticker": f"SPY {exp:%b%y} {pick['strike']:.0f}C", "company": "SPDR S&P 500 ETF, 1-year call option",
            "accession": f"rocket-{led.today}", "url": "", "payout": pick["strike"], "expiration": exp, "dutch": False,
            "engine": "rocket", "tender_status": "not needed", "drop": 1.0, "strike": round(pick["strike"], 2),
            "spot": S, "vix": vix, "contracts": pick["contracts"],
            "note": f"S&P upside: pays SPY minus ${pick['strike']:.2f} per share on {units:,.0f} shares at expiry; most it can lose is the premium."}
    led.buy(deal, units, round(pick["premium"], 4))
    notes.append(f"Rocket bought: {deal['ticker']} for ${pick['cost']:,.2f} (SPY ${S:.2f}, VIX {vix:.1f}).")
