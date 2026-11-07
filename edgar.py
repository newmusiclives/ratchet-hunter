"""Find tender offers on SEC EDGAR that mention an odd-lot provision.

Uses EDGAR full-text search (free, no key). The SEC requires a User-Agent
that names you and gives a contact email; set it in config.json.
SEC fair-access limit is 10 requests/second; we stay far below it.
"""
import re
import time
import datetime as dt
import requests
from bs4 import BeautifulSoup

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{fname}"


class Edgar:
    def __init__(self, user_agent, pause=0.25):
        if "yourname@" in user_agent:
            raise SystemExit("Set sec_user_agent in config.json to your name and email (SEC requirement).")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.pause = pause

    def _get(self, url, **params):
        time.sleep(self.pause)
        r = self.s.get(url, params=params or None, timeout=30)
        r.raise_for_status()
        return r

    def search_odd_lot_tenders(self, forms, lookback_days, start=None, end=None):
        end = end or dt.date.today()
        start = start or end - dt.timedelta(days=lookback_days)
        hits, seen = [], set()
        for form in forms:
            frm = 0
            while True:
                data = self._get(FTS_URL, q='"odd lot"', forms=form,
                                 dateRange="custom", startdt=start.isoformat(),
                                 enddt=end.isoformat(), **({"from": frm} if frm else {})).json()
                batch = data.get("hits", {}).get("hits", [])
                for h in batch:
                    rec = parse_hit(h)
                    if rec and rec["doc_id"] not in seen:
                        seen.add(rec["doc_id"])
                        hits.append(rec)
                total = data.get("hits", {}).get("total", {}).get("value", 0)
                frm += len(batch)
                if not batch or frm >= total or frm >= 1000:
                    break
        return hits

    def amendments(self, cik, lookback_days=120, start=None, end=None):
        """Tender-offer amendments for one company (results / extension / termination)."""
        end = end or dt.date.today()
        start = start or end - dt.timedelta(days=lookback_days)
        out = []
        for form in ("SC TO-I/A", "SC TO-T/A"):
            data = self._get(FTS_URL, q='"tender offer"', forms=form, ciks=str(cik).zfill(10),
                             dateRange="custom", startdt=start.isoformat(), enddt=end.isoformat()).json()
            want = str(int(cik))
            out += [r for r in (parse_hit(h) for h in data.get("hits", {}).get("hits", []))
                    if r and want in r["all_ciks"]]
        out.sort(key=lambda r: r.get("file_date") or "", reverse=True)
        return out

    def fetch_text(self, rec):
        html = self._get(rec["url"]).text
        return html_to_text(html)


def parse_hit(h):
    src = h.get("_source", {})
    doc_id = h.get("_id", "")
    if ":" not in doc_id:
        return None
    adsh, fname = doc_id.split(":", 1)
    ciks = src.get("ciks") or []
    names = src.get("display_names") or []
    if not ciks:
        return None
    # Pick the listed company (the one with a ticker). In third-party offers the
    # bidder is often named first and has no ticker; the target is what we buy.
    tick_re = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})(?:,\s*[A-Z0-9.\-]+)*\)")
    chosen, ticker = (names[0] if names else ""), None
    for nm in names:
        m = tick_re.search(nm)
        if m:
            chosen, ticker = nm, m.group(1)
            break
    name = chosen
    m = re.search(r"CIK\s*0*(\d+)", name)
    cik = m.group(1) if m else str(int(ciks[0]))
    return {
        "doc_id": doc_id,
        "accession": adsh,
        "form": src.get("form") or src.get("file_type"),
        "file_date": src.get("file_date"),
        "company": re.sub(r"\s*\(.*$", "", name).strip(),
        "ticker": ticker,
        "cik": cik,
        "all_ciks": [str(int(c)) for c in ciks],
        "url": ARCHIVE.format(cik=cik, adsh=adsh.replace("-", ""), fname=fname),
    }


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text)
