"""Shareholder-vote cash mergers ("merger arbitrage" without a tender offer).

A company agrees to be bought for $X cash per share; shareholders vote; when
the deal closes, every remaining share is paid $X automatically. No tender
election is needed, so this engine can run fully hands-off in a real account.

Found through definitive merger proxies (SEC form DEFM14A). Completion and
termination are read from the company's later 8-K filings.
"""
import datetime as dt
import re

from extract import MONEY, MONTHS, _date, _num

QUARTER_END = {"first": (3, 31), "second": (6, 30), "third": (9, 30), "fourth": (12, 31)}
HALF_END = {"first": (6, 30), "second": (12, 31)}


def _flat(text):
    return re.sub(r"\s+", " ", text)


def cash_price(t):
    """Per-share cash merger consideration (most repeated value)."""
    pats = [r"right to receive " + MONEY + r"(?: per share)? in cash",
            r"right to receive (?:an amount in )?cash(?: equal to)? " + MONEY,
            r"merger consideration (?:of|equal to) " + MONEY + r"(?: per share)?(?: in cash)?",
            MONEY + r" per share in cash, without interest"]
    vals = [_num(v) for p in pats for v in re.findall(p, t, re.I)]
    return max(set(vals), key=vals.count) if vals else None


def has_stock_component(t):
    return bool(re.search(r"right to receive.{0,200}?(?:\d+\.\d+|one|a fraction of a) (?:validly issued,? )?(?:fully paid )?"
                          r"(?:and non-?assessable )?shares? of .{0,60}?(?:common stock|ordinary shares)", t, re.I))


def expected_close(t, filed):
    """Latest stated target for closing, as a date. Falls back to filing + 120 days."""
    found = []
    for m in re.finditer(r"(?:expect|anticipate)[^.]{0,120}?(?:complete|close|consummate)[^.]{0,80}?"
                         r"(?:(first|second|third|fourth) (?:calendar |fiscal )?quarter|(first|second) half|end) of (?:calendar |fiscal )?(\d{4})", t, re.I):
        y = int(m.group(3))
        if m.group(1):
            mo, d = QUARTER_END[m.group(1).lower()]
        elif m.group(2):
            mo, d = HALF_END[m.group(2).lower()]
        else:
            mo, d = 12, 31
        found.append(dt.date(y, mo, d))
    future = [d for d in found if d >= filed]
    return max(future) if future else filed + dt.timedelta(days=120)


def meeting_date(t):
    m = re.search(r"special meeting[^.]{0,160}?(?:will be held|to be held|scheduled)[^.]{0,40}?on (?:\w+day, )?(" + MONTHS + r") (\d{1,2}), (\d{4})", t, re.I)
    return _date(f"{m.group(1).title()} {m.group(2)}, {m.group(3)}") if m else None


def board_recommends(t):
    return bool(re.search(r"recommends? (?:unanimously )?that (?:you|(?:the )?(?:stockholders|shareholders)[^.]{0,30}?) vote [“\"']?for[”\"']? "
                          r"[^.]{0,80}?(?:merger|adoption|proposal)", t, re.I))


def terms(text, filed):
    t = _flat(text)
    return {
        "price": cash_price(t),
        "stock_component": has_stock_component(t),
        "has_cvr": bool(re.search(r"contingent value right", t, re.I)),
        "board_recommends": board_recommends(t),
        "financing_condition": bool(re.search(r"(?:merger|closing) is (?:subject to|conditioned (?:up)?on) (?:[\w-]+ ){0,4}financing", t, re.I))
                               and not re.search(r"not (?:subject to|conditioned (?:up)?on) (?:any |a )?financing", t, re.I),
        "meeting": meeting_date(t),
        "expected_close": expected_close(t, filed),
    }


def update_status(text):
    """Read a later 8-K: did the merger complete, get terminated, or get approved?"""
    t = _flat(text).lower()
    if re.search(r"(?:terminated|termination of) the (?:merger agreement|agreement and plan of merger)", t) and \
            not re.search(r"completion of|completed the merger", t):
        return "terminated"
    if re.search(r"completed (?:its|the) (?:previously announced )?(?:merger|acquisition)|consummated the merger|"
                 r"the merger became effective|completion of acquisition or disposition of assets", t):
        return "completed"
    if re.search(r"(?:stockholders|shareholders) (?:approved|adopted) the (?:merger|agreement and plan of merger|merger agreement)", t):
        return "approved"
    return None
