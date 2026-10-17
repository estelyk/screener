"""Phase 2: value every company that passed the Phase 1 screen, compare with today's
price, flag buy signals, and suggest position sizes.

Run Phase 1 first (it writes output/companies/*.json). Then:
    python run_phase2.py

Outputs:
    output/valuations.csv    every passing company: value, price, discount, signal
    output/valuations.json   the same, for the Phase 3 web page
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import config as C
from sizing import load_portfolio, suggest
from valuation import price_check, value_company

OUT = Path("output")
FIELDS = ["ticker", "name", "sector", "signal", "price", "value_per_share", "value_low",
          "value_high", "value_no_recovery", "last_fcf", "zscore", "terminal_share", "buy_below",
          "discount", "to_buy_price", "growth", "normalized_fcf", "net_debt", "shares",
          "verify", "suspicious", "warnings", "value_note"]


def load_passing() -> list[dict]:
    companies = []
    for path in sorted((OUT / "companies").glob("*.json")):
        c = json.loads(path.read_text())
        if c.get("status") == "PASS":
            companies.append(c)
    return companies


def split_factor(splits: list[tuple[str, float]], since: str | None) -> float:
    """Combined ratio of splits after the last annual report's period end."""
    f = 1.0
    for day, ratio in splits:
        if since is None or day > since:
            f *= ratio
    return f


def build(companies: list[dict], prices: dict[str, float],
          splits: dict[str, list] | None = None) -> list[dict]:
    splits = splits or {}
    results = []
    for c in companies:
        latest = c["annual"][-1] if c["annual"] else {}
        since = latest.get("shares_filed") or latest.get("fiscal_year_end")
        factor = split_factor(splits.get(c["ticker"], []), since)
        v = value_company(c["annual"], split_factor=factor)
        if "diluted_shares" not in (c["annual"][-1] if c["annual"] else {}):
            v["value_note"] = "rerun Phase 1 to pick up share counts"
        price = prices.get(c["ticker"])
        if price is None and not v["value_note"].startswith("rerun"):
            v["value_note"] = (v["value_note"] + "; " if v["value_note"] else "") + "no price"
        # Altman Z needs the stock market value, so it is added here, once the price is known.
        if price is not None:
            from riskscores import z_warning, zscore
            shares = v.get("shares") or ((latest.get("diluted_shares") or 0) * factor)
            v["zscore"] = zscore(c["annual"], price * shares if shares else None)
            zw = z_warning(v["zscore"], c["annual"])
            if zw:
                v["warnings"] = v["warnings"] + [zw]
        verify = "; ".join(n for n in c.get("notes", []) if n.startswith("VERIFY"))
        results.append({"ticker": c["ticker"], "name": c["name"], "sector": c["sector"],
                        "price": price, "verify": verify, **v,
                        **price_check(v["value_per_share"], price)})

    # Signals first (biggest discount first), then closest to their buy price.
    def rank(r):
        if r["signal"]:
            return (0, -r["discount"])
        if r["to_buy_price"] is not None:
            return (1, r["to_buy_price"])
        return (2, 0)
    return sorted(results, key=rank)


def _money(x):
    return "—" if x is None else f"${x:,.2f}"


def report(results: list[dict]) -> None:
    signals = [r for r in results if r["signal"]]
    valued = [r for r in results if r["value_per_share"]]
    print(f"\nValued {len(valued)} of {len(results)} passing companies "
          f"(margin of safety {C.MARGIN_OF_SAFETY:.0%}, discount rate {C.DISCOUNT_RATE:.0%}).")
    if C.QUIET:
        return  # public logs: no tickers, no signals

    if signals:
        print(f"\nBUY SIGNALS ({len(signals)}): price at least {C.MARGIN_OF_SAFETY:.0%} below value")
        portfolio = load_portfolio()
        sized = suggest(signals, portfolio) if portfolio else signals
        for s in sized:
            amt = s.get("suggested_amount")
            size = f"  suggest {_money(amt)}" if amt else (f"  ({s['sizing_note']})" if s.get("sizing_note") else "")
            flag = "  [VERIFY debt]" if s["verify"] else ""
            if s["suspicious"]:
                flag += "  [CHECK DATA: discount unusually large]"
            print(f"  {s['ticker']:<6} price {_money(s['price'])}  value {_money(s['value_per_share'])}  "
                  f"discount {s['discount']:.0%}{size}{flag}")
            for w in s["warnings"]:
                print(f"           ! {w}")
        if not portfolio:
            print("  (copy my_portfolio.example.json to my_portfolio.json for suggested amounts)")
    else:
        print("\nNo buy signals today. That's normal; the screen is meant to be strict.")

    watch = [r for r in results if not r["signal"] and r["to_buy_price"] is not None][:C.WATCHLIST_SIZE]
    if watch:
        print(f"\nWATCHLIST: closest to their buy price")
        for r in watch:
            print(f"  {r['ticker']:<6} price {_money(r['price'])}  buy below {_money(r['buy_below'])}  "
                  f"needs a {r['to_buy_price']:.0%} drop")


def main() -> None:
    companies = load_passing()
    if not companies:
        raise SystemExit("No Phase 1 results found. Run: python run_phase1.py")
    print(f"Fetching prices for {len(companies)} companies...")
    from prices import market_data
    prices, splits = market_data([c["ticker"] for c in companies])
    print(f"Got {len(prices)} prices.")
    if len(prices) < C.MIN_PRICE_COVERAGE * len(companies):
        # Better no update than a page built on missing prices. On GitHub this fails
        # the run, which emails you and leaves yesterday's page in place.
        raise SystemExit(f"Only {len(prices)} of {len(companies)} prices arrived; Yahoo may be "
                         "blocking requests. Stopping without updating the page.")

    results = build(companies, prices, splits)
    with open(OUT / "valuations.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: (round(v, 4) if isinstance(v, float)
                            else "; ".join(v) if isinstance(v, list) else v)
                        for k, v in r.items()})
    (OUT / "valuations.json").write_text(json.dumps(results, indent=2))
    report(results)
    print(f"\nFull table: {OUT / 'valuations.csv'}")

    # Phase 3: the mobile page. Profiles (descriptions + filing links) are best-effort.
    import build_site
    profiles = {}
    ua = os.environ.get("SEC_USER_AGENT")
    if ua:
        try:
            from edgar import EdgarClient
            from metrics import ALL_TAGS
            from profiles import build_profiles
            from alerts import parse_holdings
            # Descriptions: your holdings and today's signals/watchlist first.
            priority = parse_holdings(os.environ.get("HOLDINGS"))
            priority += [r["ticker"] for r in results if r["signal"]]
            priority += [r["ticker"] for r in results
                         if not r["signal"] and r["to_buy_price"] is not None][:C.WATCHLIST_SIZE]
            screened = [json.loads(p.read_text()) for p in sorted((OUT / "companies").glob("*.json"))]
            print(f"\nFetching filing links and company descriptions...")
            profiles = build_profiles(EdgarClient(ua, keep_tags=ALL_TAGS), screened, priority)
            (OUT / "profiles.json").write_text(json.dumps(profiles))
        except Exception as e:
            print(f"(Skipped profiles: {type(e).__name__}: {e})")
    else:
        print("\n(Set SEC_USER_AGENT to add filing links to the briefs.)")
    # Phase 5: record today's signals and update their paper returns.
    import tracker
    from alerts import parse_holdings
    tracking = tracker.run(results, parse_holdings(os.environ.get("HOLDINGS")))
    page = build_site.build(profiles, tracking)
    print(f"Web page: {page}   (double-click to open it in your browser)")


if __name__ == "__main__":
    main()
