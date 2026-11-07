"""Pull deal terms out of tender-offer text with plain rules.

Rules come first because they are free and predictable. An optional AI
review (ai_review.py) can then read the same text and veto or correct.
Everything here is conservative: when in doubt, the deal is skipped.
"""
import re
import datetime as dt

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MONEY = r"(?:US)?\$\s?([0-9]{1,5}(?:,[0-9]{3})*(?:\.[0-9]{1,4})?)"

ODD = re.compile(r"odd[ -]lot|fewer than 100 shares|less than 100 shares|not more than 99 shares", re.I)
ODD_NO = re.compile(r"no special pro[ -]?ration|not have any special pro[ -]?ration|still subject to pro[ -]?ration"
                    r"|odd[ -]lot (?:tenders?|holders?|shares?)[^.]{0,80}?(?:are|will be|shall be) subject to (?:the same )?pro[ -]?ration"
                    r"|not receive priority|not provide for preferential|same proration terms|pro[ -]?ration on the same basis", re.I)
ODD_EXCEPT = re.compile(r"except|exception|priority given|preference|exceeds", re.I)
ODD_YES = re.compile(r"priority|not be subject to (?:the )?pro[ -]?ration|not subject to (?:the )?pro[ -]?ration"
                     r"|without (?:regard to )?pro[ -]?ration|accept for purchase all|purchase all shares tendered by"
                     r"|first, from|accepted in full|preference", re.I)
FIN = re.compile(r"financing condition|conditioned (?:up)?on (?:[\w-]+ ){0,6}(?:obtaining|receipt of|the receipt of) (?:[\w-]+ ){0,4}financing"
                 r"|subject to (?:[\w-]+ ){0,4}financing", re.I)
NEG = re.compile(r"\b(?:not|nor|no|without)\b", re.I)
STATED = re.compile(r"\b(?:last|closing)\b[^.$]{0,100}?price[^.$]{0,160}?(?:US)?\$\s?([0-9]+(?:\.[0-9]+)?)", re.I)


def _clauses(t):
    """Sentences, further split at 'however' / 'but' / ';' so a 'not' in one clause
    can't cancel a condition stated in the next."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", t):
        out += re.split(r";|,? however,?|,? but ", sent, flags=re.I)
    return out


def stated_prices(text):
    """Share prices the filing itself quotes ("the last reported sale price ... was $4.91")."""
    return sorted({float(v) for v in STATED.findall(re.sub(r"\s+", " ", text)) if float(v) >= 0.05})


def _num(s):
    return float(s.replace(",", ""))


def _date(s):
    try:
        return dt.datetime.strptime(re.sub(r"\s+", " ", s.strip()), "%B %d, %Y").date()
    except ValueError:
        return None


def extract_terms(text):
    t = re.sub(r"\s+", " ", text)
    low = t.lower()
    out = {"price": None, "price_high": None, "dutch_auction": False, "expiration": None,
           "odd_lot_priority": False, "financing_condition": False, "cash_offer": True,
           "terminated": False, "final_results": False, "notes": []}

    # Dutch auction: "not greater than $X nor less than $Y" (either order)
    m = re.search(r"not (?:greater|more) than " + MONEY + r"[^$]{0,40}?(?:nor|or|and not) less than " + MONEY, t, re.I) \
        or re.search(r"not less than " + MONEY + r"[^$]{0,40}?(?:nor|or|and not) (?:greater|more) than " + MONEY, t, re.I)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        out.update(dutch_auction=True, price=min(a, b), price_high=max(a, b))
        out["notes"].append("Dutch auction: using the low end of the range as the payout.")
    else:
        for pat in [r"(?:purchase|offer) price of " + MONEY + r" per (?:share|unit)",
                    r"at a price of " + MONEY + r" per (?:share|unit)",
                    MONEY + r" per (?:share|unit),? net to the seller in cash",
                    r"for " + MONEY + r" per (?:share|unit) in cash"]:
            m = re.search(pat, t, re.I)
            if m:
                out["price"] = _num(m.group(1))
                break

    # Expiration date
    for pat in [r"expire[sd]?.{0,160}?on (?:\w+day, )?(" + MONTHS + r") (\d{1,2}), (\d{4})",
                r"expiration date.{0,100}?(" + MONTHS + r") (\d{1,2}), (\d{4})"]:
        m = re.search(pat, t, re.I)
        if m:
            out["expiration"] = _date(f"{m.group(1).title()} {m.group(2)}, {m.group(3)}")
            if out["expiration"]:
                break

    # Odd-lot priority: must be stated, and any sentence denying it wins.
    odd_sents = [c for c in _clauses(t) if ODD.search(c)]
    if any(ODD_NO.search(c) and not ODD_EXCEPT.search(c) for c in odd_sents):
        out["odd_lot_priority"] = False
        out["notes"].append("Filing says odd lots are prorated like everyone else.")
    elif any(ODD_YES.search(c) for c in odd_sents):
        out["odd_lot_priority"] = True

    # Financing condition: only if a clause states it without a negation.
    out["financing_condition"] = any(FIN.search(c) and not NEG.search(c) for c in _clauses(t) if "financ" in c.lower())

    if re.search(r"exchange offer|in exchange for (?:each|shares)", low) and not re.search(r"in cash", low):
        out["cash_offer"] = False
        out["notes"].append("Looks like a share-exchange offer, not cash. Skipped in v1.")

    m = re.search(r"extend(?:ed|s|ing)? (?:the )?(?:offer|tender offer|expiration date|expiration time)[^$]{0,220}?(?:to|until) "
                  r"(?:\d{1,2}:\d{2} [ap]\.?m\.?,? [^,]{0,40}?, )?(?:on )?(?:\w+day, )?(" + MONTHS + r") (\d{1,2}), (\d{4})", t, re.I)
    out["extended_to"] = _date(f"{m.group(1).title()} {m.group(2)}, {m.group(3)}") if m else None

    if re.search(r"(?:has|have) (?:terminated|withdrawn) the (?:tender )?offer|offer (?:was|has been) (?:terminated|withdrawn)", low):
        out["terminated"] = True
    if re.search(r"final results|final proration factor|accepted for purchase an aggregate", low):
        out["final_results"] = True
    out["stated_prices"] = stated_prices(t)
    return out
