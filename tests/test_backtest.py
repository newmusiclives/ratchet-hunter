"""Backtest replay test on a synthetic saved cache (no internet)."""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backtest  # noqa: E402
import hunter  # noqa: E402

END = dt.date(2026, 10, 30)
DAYS = 120
START = END - dt.timedelta(days=DAYS)


def rec(i, ticker, cik, date, form="SC TO-I"):
    return {"doc_id": f"000000000{i}-26-00000{i}:doc{i}.htm", "accession": f"000000000{i}-26-00000{i}", "form": form,
            "file_date": date, "company": f"{ticker} Corp", "ticker": ticker, "cik": cik,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/x/doc{i}.htm"}


def build(folder):
    f = Path(folder)
    (f / "filings").mkdir(parents=True)
    deals = [rec(1, "AAA", "11", "2026-08-03"), rec(2, "BBB", "22", "2026-08-10"),
             rec(3, "CCC", "33", "2026-08-20"), {**rec(4, "X", "44", "2026-08-21"), "ticker": None}]
    texts = {
        deals[0]["doc_id"]: "Offer to purchase at a purchase price of $12.50 per Share, net to the seller in cash. The Offer will expire at 5:00 P.M., New York City time, on September 2, 2026. Odd lots will not be subject to proration.",
        deals[1]["doc_id"]: "Offer to purchase shares at a price not greater than $23.00 nor less than $20.00 per share. The Offer will expire at midnight on September 10, 2026. Odd lot holders receive priority.",
        deals[2]["doc_id"]: "Offer to purchase at a purchase price of $18.00 per share, net to the seller in cash. The Offer will expire on October 1, 2026. Odd lots will receive priority.",
        deals[3]["doc_id"]: "Offer to purchase at a purchase price of $5.00 per share. Expires on September 30, 2026. Odd lots receive priority.",
    }
    amends = {
        "11": [(5, "2026-09-04", "Final results: the Company accepted for purchase an aggregate of 1,000,000 Shares at $12.50 per Share. Odd lots accepted in full.")],
        "22": [(6, "2026-09-08", "The Company has extended the Offer. The Expiration Date has been extended to 5:00 p.m., New York City time, on September 24, 2026."),
               (7, "2026-09-26", "Final results: the Company accepted for purchase an aggregate of 2,000,000 shares at a purchase price of $21.75 per share.")],
        "33": [(8, "2026-09-15", "The Company has terminated the tender offer. No shares will be purchased.")],
    }
    for d in deals:
        (f / "filings" / (backtest._safe(d["doc_id"]) + ".txt")).write_text(texts[d["doc_id"]])
    for cik, items in amends.items():
        recs = []
        for i, date, text in items:
            r = rec(i, "?", cik, date, "SC TO-I/A")
            recs.append(r)
            (f / "filings" / (backtest._safe(r["doc_id"]) + ".txt")).write_text(text)
        (f / f"amend_{cik}.json").write_text(json.dumps(recs))
    (f / f"deals_{START}_{END}.json").write_text(json.dumps(deals))

    def series(fn):
        out, d = {}, START - dt.timedelta(days=10)
        while d <= END:
            if d.weekday() < 5:
                out[d.isoformat()] = fn(d)
            d += dt.timedelta(days=1)
        return out
    (f / "prices_AAA.json").write_text(json.dumps(series(lambda d: 11.20)))
    (f / "prices_BBB.json").write_text(json.dumps(series(lambda d: 19.00)))
    (f / "prices_CCC.json").write_text(json.dumps(series(lambda d: 17.00 if d < dt.date(2026, 9, 15) else 13.00)))


def test_backtest_replay():
    with tempfile.TemporaryDirectory() as tmp:
        build(Path(tmp) / "cache")
        cfg = hunter.load_cfg()
        led, eq = backtest.run(DAYS, offline=True, cfg=cfg, cache_dir=Path(tmp) / "cache", end=END, quiet=True)
        closed = {p["ticker"]: p for p in led.closed_positions()}
        assert closed["AAA"]["status"] == "settled" and abs(closed["AAA"]["pnl"] - 99 * 1.30) < 0.01
        b = closed["BBB"]
        assert b["status"] == "settled" and b["closed"] > "2026-09-24", b  # waited for the extension
        assert abs(b["proceeds"] - 99 * 21.75) < 0.01, b  # paid the Dutch-auction final price
        c = closed["CCC"]
        assert c["status"] == "failed" and c["pnl"] < 0 and c["closed"] >= "2026-09-16"
        bought = [p["ticker"] for p in led.closed_positions() + led.open_positions()]
        assert "X" not in bought and None not in bought
        assert led.get("floor") > 8000
        assert led.stressed_value() >= led.get("floor") - 1e-6
        print(f"   backtest end equity ${eq:,.2f}, floor ${led.get('floor'):,.2f}")
        for p in led.closed_positions():
            print(f"   {p['ticker']}: {p['status']} {p['pnl']:+.2f} on {p['closed']}")





def test_switches_to_preferred_share_class():
    """Offer is for preferred shares; common stock price doesn't match -> trade the preferred."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "cache"
        (f / "filings").mkdir(parents=True)
        d = rec(1, "PFD", "55", "2026-08-03")
        (f / "filings" / (backtest._safe(d["doc_id"]) + ".txt")).write_text(
            "Offer to purchase Series A Preferred Shares at a purchase price of $22.50 per share, net to the seller in cash. "
            "The Offer will expire on September 2, 2026. Odd lots will not be subject to proration. "
            "The Series A Shares are traded on Nasdaq under the symbol “PFDAP.” "
            "On July 31, 2026, the last reported sale price of the Series A Shares was $21.65 per share.")
        (f / f"deals_{START}_{END}.json").write_text(json.dumps([d]))
        (f / "amend_55.json").write_text("[]")

        def series(v):
            out, x = {}, START - dt.timedelta(days=10)
            while x <= END:
                if x.weekday() < 5:
                    out[x.isoformat()] = v
                x += dt.timedelta(days=1)
            return out
        (f / "prices_PFD.json").write_text(json.dumps(series(14.90)))    # common stock
        (f / "prices_PFDAP.json").write_text(json.dumps(series(21.65)))  # the preferred
        led, eq = backtest.run(DAYS, offline=True, cfg=hunter.load_cfg(), cache_dir=f, end=END, quiet=True)
        held = led.closed_positions() + led.open_positions()
        assert [p["ticker"] for p in held] == ["PFDAP"], held
        assert abs(held[0]["pnl"] - 99 * (22.50 - 21.65)) < 0.01


def test_takeover_engine():
    """Cash takeovers: bigger positions, a price bump, and a broken deal that the floor survives."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "cache"
        (f / "filings").mkdir(parents=True)
        good = dict(rec(1, "TGTA", "61", "2026-08-03", "SC TO-T"), engine="takeover")
        bust = dict(rec(2, "TGTB", "62", "2026-08-10", "SC TO-T"), engine="takeover")
        texts = {
            good["doc_id"]: "Buyer Co is offering to purchase all of the outstanding shares of common stock of TGTA at a price of $20.00 per share, "
                            "net to the seller in cash. The Offer will expire on September 4, 2026. The Offer is not subject to any financing condition.",
            bust["doc_id"]: "Acquirer Inc is offering to purchase all of the outstanding shares of TGTB for $10.00 per share in cash. "
                            "The Offer will expire on September 30, 2026. The Offer is not conditioned on obtaining financing.",
        }
        for d in (good, bust):
            (f / "filings" / (backtest._safe(d["doc_id"]) + ".txt")).write_text(texts[d["doc_id"]])
        amends = {"61": [(5, "2026-08-25", "Buyer Co has increased the offer price to $20.50 per share."),
                         (6, "2026-09-08", "Final results: Buyer Co accepted for purchase an aggregate of 9,000,000 shares.")],
                  "62": [(7, "2026-09-14", "Acquirer Inc has terminated the tender offer.")]}
        for cik, items in amends.items():
            recs = []
            for i, date, text in items:
                r = rec(i, "?", cik, date, "SC TO-T/A")
                recs.append(r)
                (f / "filings" / (backtest._safe(r["doc_id"]) + ".txt")).write_text(text)
            (f / f"amend_{cik}.json").write_text(json.dumps(recs))
        (f / f"deals_{START}_{END}.json").write_text("[]")
        (f / f"takeovers_{START}_{END}.json").write_text(json.dumps([good, bust]))

        def series(fn):
            out, x = {}, START - dt.timedelta(days=10)
            while x <= END:
                if x.weekday() < 5:
                    out[x.isoformat()] = fn(x)
                x += dt.timedelta(days=1)
            return out
        (f / "prices_TGTA.json").write_text(json.dumps(series(lambda x: 19.40)))
        (f / "prices_TGTB.json").write_text(json.dumps(series(lambda x: 9.70 if x < dt.date(2026, 9, 14) else 6.30)))
        led, eq = backtest.run(DAYS, offline=True, cfg=hunter.load_cfg(), cache_dir=f, end=END, quiet=True)
        closed = {p["ticker"]: p for p in led.closed_positions()}
        a, b = closed["TGTA"], closed["TGTB"]
        assert a["shares"] > 99 and a["cost"] <= 4000, a          # not limited to an odd lot, capped per deal
        assert a["status"] == "settled" and abs(a["proceeds"] - a["shares"] * 20.50) < 0.01  # paid the raised price
        assert b["status"] == "failed" and b["pnl"] < 0
        assert eq > led.get("floor")
        print(f"   takeovers: TGTA {a['pnl']:+.2f}, TGTB {b['pnl']:+.2f}, end ${eq:,.2f}")


if __name__ == "__main__":
    test_backtest_replay()
    print("ok  test_backtest_replay")
    test_switches_to_preferred_share_class()
    print("ok  test_switches_to_preferred_share_class")
    test_takeover_engine()
    print("ok  test_takeover_engine")
