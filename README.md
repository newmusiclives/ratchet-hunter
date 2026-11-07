# The Ratchet · Hunter (paper trading v1)

An autonomous scanner that hunts **odd-lot tender offers** on SEC EDGAR and trades them on paper, under a hard loss limit. It runs once a day by itself and writes a report you open in a browser.

**Nothing here can place a real order.** This is the 30-day proving run.

---

## What it does every day

1. **Earns interest.** Idle paper cash grows at the T-bill rate (4.3%, set in `config.json`).
2. **Checks open deals.** For each deal it holds, it reads the company's latest SEC amendments.
   - If the offer was **terminated**, it sells at the market price and books the loss.
   - Once the offer has **expired** (final results filed, or 5 days have passed), it is paid the tender price for all its shares. Odd lots are not prorated.
3. **Hunts.** It searches EDGAR for tender-offer filings that mention "odd lot", then reads each one and pulls out:
   cash price (or the low end of a Dutch-auction range), expiry date, odd-lot priority, financing conditions, exchange offers.
4. **Filters.** It skips anything that is unclear, not cash, dependent on financing, expiring too soon, under a 3% premium, or worth less than $25.
5. **AI review (optional).** If an Anthropic API key is set, Claude reads the key passages of the filing. Claude can **veto** a trade or lower the payout, but it can never force one. Its cost is charged to the account.
6. **Sizes the trade under the Floor Rule** (below), then buys up to 99 shares on paper.
7. **Ratchet.** Every Friday, half of the week's realized profit is added to the floor permanently.
8. **Reports.** `reports/latest.html` shows account value against the floor, open and closed deals, and every filing it looked at, with the reason for each decision.

## The Floor Rule ($2,000 maximum loss)

```
Floor starts at $10,000 − $2,000 = $8,000, and only ever rises.
Before any purchase:  cash + every open position × (1 − 25%) ≥ floor
```

In plain terms: if every open deal collapsed and its stock fell 25%, the account would still be worth at least the floor. The engine uses no margin, no shorting and no options; it trades with cash only.
*The limit:* a stock that falls more than 25% after a failed deal could take the account below the floor. Raise `assumed_drop_if_deal_fails` to 0.35 for a stricter limit; the Hunter will then take smaller positions.

---

## Setup (about 10 minutes)

You need **Python 3.10+**.

```bash
cd hunter
pip install requests beautifulsoup4
```

**1. Required.** Open `config.json` and set `sec_user_agent` to a name and email, for example
`"RatchetHunter jane@example.com"`. The SEC blocks requests that don't identify the sender.

**2. Optional.** Turn on the AI reviewer:
```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Windows: setx ANTHROPIC_API_KEY sk-ant-...
```
Without a key, the rules decide alone and the AI step is skipped.

**3. Run it once by hand:**
```bash
python hunter.py run
python hunter.py status
```
Then open `reports/latest.html`.

**4. Schedule it** to run every weekday at 3:30 pm Mountain time, after the US market closes:

*Mac / Linux* (`crontab -e`):
```
30 15 * * 1-5 cd /path/to/hunter && /usr/bin/python3 hunter.py run >> data/run.log 2>&1
```
*Windows:* in Task Scheduler, create a task that runs Mon–Fri at 3:30 PM.
Action: `python` · Arguments: `hunter.py run` · Start in: `C:\path\to\hunter`

The computer must be on at that time. Any always-on machine or a $5/month cloud server works.

## Live 30-day paper run on GitHub (no computer needed)

The workflow in `.github/workflows/hunter.yml` runs the Hunter every weekday at 3:17 pm Arizona/New Mexico time on GitHub's servers. It saves the paper account (`data/paper.db`) back into the repository and publishes the report to the repository's GitHub Pages site:
- `index.html`: the live paper run, updated daily
- `backtest.html`: the 12-month backtest on real SEC filings

One-time setup in the repository on github.com:
1. **Settings → Secrets and variables → Actions → New repository secret.** Name: `SEC_USER_AGENT`. Value: `RatchetHunter your@email.com`. Optionally add `ANTHROPIC_API_KEY` to switch on the AI reviewer.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Actions tab → "Hunter daily paper run" → Run workflow** to do the first run now.

## Test it without internet

```bash
python tests/test_hunter.py
python hunter.py run --offline tests/fixtures --date 2026-11-09
```
The sample filings cover a fixed-price deal, a Dutch auction, a deal without odd-lot priority, a deal with a financing condition, an exchange offer, an expensive stock that the Floor Rule limits, a premium that is too thin, and a terminated deal. Offline runs use a separate database (`data/paper_offline.db`).

## Backtest: replay the past 12 months of real deals

```bash
python3 backtest.py
```
The first run downloads every real odd-lot tender offer the SEC has on file for the past year, along with each company's later amendments and daily prices. That takes 5–15 minutes. It then replays the year one trading day at a time, using the same rules, Floor Rule and Ratchet, and only "sees" what was public on each day.
Open `reports/backtest_report.html` when it finishes.

- Run it again with no internet: `python3 backtest.py --cache-only`. Downloaded data is kept in `data/backtest_cache`.
- Look further back: `python3 backtest.py --days 730`
- The AI reviewer is off in backtests, so the results come from the rules alone.

**What the rules check (v3):** odd-lot priority must be stated, and any sentence saying odd lots are prorated like everyone else overrides it. A financing condition counts only if the filing says the offer depends on it, so "not subject to a financing condition" is read correctly. All documents in one SEC filing are read together. The price data must match the share price the filing itself quotes (within 20%). That catches the wrong share class (for example, a tender for preferred shares) and price histories distorted by reverse splits.

**Backtest limits:** trades are filled at that day's closing price. Companies that no longer trade may have no price history and are skipped. A small number of filings may be worded in ways the rules can't read, and those are skipped too (the report says why). Treat the result as a careful estimate, not a promise.

## Settings worth knowing (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `max_loss` | 2000 | The most the account may lose in total |
| `assumed_drop_if_deal_fails` | 0.25 | Stress-test drop used by the Floor Rule |
| `min_premium_pct` | 3.0 | Smallest premium (tender price over market price) worth taking |
| `min_profit_dollars` | 25 | Skip deals worth less than this |
| `max_days_to_expiry` | 60 | Deals further out are watched, not bought yet |
| `skip_if_financing_condition` | true | Avoid offers that depend on borrowed money |
| `ratchet_share_of_profit` | 0.5 | Share of each week's profit that is locked into the floor |
| `fixed_monthly_running_cost` | 0 | Add server or data costs here so returns are truly net |

---

## How paper results can differ from real ones

- **Dutch auctions** are counted at the low end of the price range. Real payouts are often higher.
- **Completion is assumed** unless a filing says the offer was terminated. Real deals can be extended, which ties up cash for longer.
- **Prices** come from free delayed feeds. Real fills may be a few cents worse.

## Before any real money: the go-live checklist

1. **The account must be in the New Mexico investor's name**, and they must switch the bot on themselves.
2. **Odd-lot priority usually counts every share the investor owns, across all their accounts.** If they already own 100+ shares of that company anywhere, the deal does not work for them. Ask them for a list of their holdings, and the Hunter will skip those tickers.
3. **Tendering is the one step that may not be automatic.** Buying shares through a broker's API is easy. Electing to tender is a "voluntary corporate action", and many brokers only accept it on their website or by phone. Choose a broker that accepts tender elections online or through its API, or accept a 1-minute human click per deal (roughly 1–3 a month).
4. **Taxes:** gains are short-term and taxed as ordinary income (federal plus New Mexico). Keep the reports; they double as a trade log.
5. **Legal:** if you (not the investor) control the trading or are paid on performance, speak to a securities lawyer about investment adviser rules first.

*Paper trading tool for research. Not investment advice.*
