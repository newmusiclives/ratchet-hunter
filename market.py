"""Last-price lookups. Free sources, no keys. Paper trading only needs a
reasonable delayed price; a live version would use the broker's own feed."""
import csv
import io
import requests

UA = {"User-Agent": "Mozilla/5.0 RatchetHunter/1.0"}


def last_price(ticker):
    for fn in (_yahoo, _stooq):
        try:
            p = fn(ticker)
            if p and p > 0:
                return p
        except Exception:
            continue
    return None


def _yahoo(ticker):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"range": "1d", "interval": "1d"}, headers=UA, timeout=20)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return float(meta.get("regularMarketPrice") or meta.get("previousClose"))


def _stooq(ticker):
    r = requests.get("https://stooq.com/q/l/", params={"s": f"{ticker.lower()}.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    row = next(csv.DictReader(io.StringIO(r.text)))
    return float(row["Close"])


def history(ticker, start, end):
    """Daily closes {YYYY-MM-DD: close} between two dates. Yahoo first, Stooq as backup."""
    import datetime as dt
    try:
        p1 = int(dt.datetime.combine(start, dt.time()).timestamp())
        p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time()).timestamp())
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"period1": p1, "period2": p2, "interval": "1d"}, headers=UA, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        out = {dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(): c
               for ts, c in zip(res.get("timestamp", []), closes) if c}
        if out:
            return out
    except Exception:
        pass
    try:
        r = requests.get("https://stooq.com/q/d/l/", params={"s": f"{ticker.lower()}.us", "i": "d",
                         "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")}, headers=UA, timeout=30)
        r.raise_for_status()
        return {row["Date"]: float(row["Close"]) for row in csv.DictReader(io.StringIO(r.text)) if row.get("Close")}
    except Exception:
        return {}
