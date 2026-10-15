# Buffett-Style Business Screener

**Phase 1** pulls 10 years of official financials from SEC EDGAR for the S&P 500
(minus financials, REITs and utilities) and checks each company for Buffett-style quality.

**Phase 2** estimates what each passing company is worth, compares that with today's
price, flags buy signals (price at least 25% below value), ranks a watchlist, and
suggests position sizes.

**Phase 3** turns the results into a mobile web page, `site/index.html`: buy
signals, the watchlist, every quality business, and a tap-through brief for each.

**Phase 4** runs it all on GitHub Actions every weekday evening and publishes the
page, encrypted with your passphrase, to GitHub Pages. New buy signals trigger an
email alert that never names the stock. See **SETUP.md** for the one-time setup.

## Daily use

```bash
python run_phase1.py   # quality screen (SEC data refreshes weekly)
python run_phase2.py   # prices, valuation, signals, watchlist, and the web page
open site/index.html   # view it in your browser
```

### The web page (`build_site.py`, `site_template.html`)

- Contains only public data: financials, prices, estimates. Safe to publish.
- Position sizing happens in the browser: tap **Sizing**, enter your cash reserve,
  max % per position, max holdings, and tickers you own. Saved only on that device.
- Each brief: plain-English description (signals + watchlist), price vs. value,
  cautions, why it passed, how the value was estimated, 10-year charts (tap a bar
  for its value), the full numbers table, and links to the latest 10-K and
  earnings release.
- `python build_site.py` rebuilds the page from the last run without refetching.

### Your portfolio settings (private)

Copy `my_portfolio.example.json` to `my_portfolio.json` and edit it:

- `cash_reserve`: dollars set aside for this tool
- `max_position_pct`: largest single position, as a fraction (0.20 = 20%)
- `max_holdings`: most companies to own at once
- `holdings`: tickers you already own, e.g. `["KO", "COST"]`

`my_portfolio.json` is in `.gitignore`, so it is never committed or published.

### How the valuation works (`valuation.py`, settings in `config.py`)

1. Start from average owner free cash flow over the last 3 years.
2. Assume growth equal to the lower of 10-year revenue growth and cash flow growth,
   capped at 10% and never below 0.
3. Grow for 5 years, fade to 2.5% by year 10, then 2.5% forever.
4. Discount everything back to today at 10% a year.
5. Subtract net debt (no credit for extra cash), divide by shares.

Buy signal: price at or below 75% of that value.

## First run (Mac)

```bash
cd buffett-screener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q tests          # 9 tests on synthetic data, should all pass

export SEC_USER_AGENT="Kyle <your-email>"   # SEC requires a name + email
python run_phase1.py --tickers KO,AAPL,MSFT,MCD,HD   # quick sanity check
python run_phase1.py                                  # full run, a few minutes
```

Open `output/screen_results.csv`. PASS rows are at the top.

## What each file does

| File | Job |
|---|---|
| `config.py` | Every threshold. Change numbers here, not in the logic. |
| `universe.py` | S&P 500 list with sector and SEC ID (CIK), cached in `data/sp500.csv`. |
| `edgar.py` | Downloads company facts from SEC, politely, caching a trimmed copy for 7 days. |
| `metrics.py` | Turns raw filings into one clean row per fiscal year. Handles tag switches, restatements, and stray quarterly numbers. |
| `screen.py` | The five checks: history, returns, debt, free cash flow, earnings consistency. |
| `run_phase1.py` | Runs it all and writes the results. |
| `diagnose.py` | Lists the data labels companies actually file, to fix NO_DATA cases: `python diagnose.py NVR PCAR --kw debt interest`. Writes `output/diagnose.txt`. |

## Reading the results

- **PASS**: every check passed. Candidates for Phase 2 valuation.
- **FAIL**: at least one check failed; the `failed` column says which.
- **NO_DATA**: nothing failed but something couldn't be measured. Usually a company
  using an XBRL tag not yet in `metrics.py`. Worth spot-checking.
- **ERROR**: download or parsing problem; reason is in `notes`.

`returns_basis` shows whether the company was judged on ROE or, when buybacks have
made equity negative or tiny, on ROIC.

`output/companies/<TICKER>.json` holds each company's full 10-year table and check
details. Phase 3 builds the briefs from these.

## Known simplifications

- ROE uses year-end equity; ROIC uses a flat 21% tax rate. When buybacks make
  equity + debt − cash negative, return on tangible capital (plant and equipment plus
  working capital) is used instead.
- Missing debt is treated as zero only if the company also reports no meaningful
  interest expense; otherwise the debt check is left unjudged.
- Missing stock-comp tags are treated as zero and noted in `notes`.
- The universe is today's S&P 500, which is fine for tracking forward but not for backtesting.
