"""Phase 1: pull EDGAR data for the S&P 500 (minus excluded sectors) and run the screen.

Usage:
    export SEC_USER_AGENT="Your Name your@email.com"
    python run_phase1.py --tickers KO,AAPL,MSFT   # quick test
    python run_phase1.py                          # full run

Outputs:
    output/screen_results.csv        one row per company, PASS first
    output/companies/<TICKER>.json   10-yr table + check details (feeds the Phase 3 briefs)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import config as C
from edgar import EdgarClient
from metrics import ALL_TAGS, annual_table, merge_facts
from screen import screen_company
from universe import load_universe

OUT = Path("output")
FIELDS = ["ticker", "name", "sector", "status", "failed", "unjudged", "returns_basis",
          "returns_avg", "debt_years", "fcf_positive_years", "loss_years", "years",
          "latest_fy_end", "latest_revenue", "latest_net_income", "latest_owner_fcf", "notes"]
STATUS_ORDER = {"PASS": 0, "NO_DATA": 1, "FAIL": 2, "ERROR": 3}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="comma-separated subset, e.g. KO,AAPL")
    ap.add_argument("--limit", type=int, help="only screen the first N companies")
    ap.add_argument("--refresh-universe", action="store_true", help="re-download the S&P 500 list")
    args = ap.parse_args()

    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        sys.exit('Set SEC_USER_AGENT first, e.g.  export SEC_USER_AGENT="Kyle Smith kyle@example.com"')

    uni = load_universe(ua, refresh=args.refresh_universe)
    excluded = uni["sector"].isin(C.EXCLUDED_SECTORS)
    print(f"S&P 500: {len(uni)} companies, skipping {excluded.sum()} in {sorted(C.EXCLUDED_SECTORS)}")
    uni = uni[~excluded]
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        uni = uni[uni["ticker"].str.upper().isin(wanted)]
    if args.limit:
        uni = uni.head(args.limit)

    client = EdgarClient(ua, keep_tags=ALL_TAGS)
    (OUT / "companies").mkdir(parents=True, exist_ok=True)
    if not args.tickers and not args.limit:
        # Full run: clear old per-company files so dropped companies don't linger.
        for old in (OUT / "companies").glob("*.json"):
            old.unlink()
    results = []

    for i, co in enumerate(uni.itertuples(index=False), 1):
        base = {"ticker": co.ticker, "name": co.name, "sector": co.sector}
        try:
            ciks = [int(co.cik)] + C.PREDECESSOR_CIKS.get(co.ticker, [])
            facts = merge_facts([client.companyfacts(c) for c in ciks])
            if facts is None:
                raise RuntimeError("no XBRL company facts on EDGAR")
            rows, notes = annual_table(facts)
            from quarterly import quarterly_update
            try:
                q = quarterly_update(facts, rows)
            except Exception:          # quarterly data is a bonus; never fail a company on it
                q = None
            s = screen_company(rows)
            latest = rows[-1] if rows else {}
            res = {**base, **{k: v for k, v in s.items() if k != "checks"},
                   "latest_fy_end": latest.get("fiscal_year_end"),
                   "latest_revenue": latest.get("revenue"),
                   "latest_net_income": latest.get("net_income"),
                   "latest_owner_fcf": latest.get("owner_fcf"),
                   "notes": "; ".join(notes)}
            (OUT / "companies" / f"{co.ticker}.json").write_text(json.dumps(
                {**base, "cik": int(co.cik), "status": s["status"], "checks": s["checks"],
                 "notes": notes, "annual": rows, "quarterly": q}, indent=2))
        except Exception as e:  # keep going; one bad company shouldn't stop the run
            res = {**base, "status": "ERROR", "notes": f"{type(e).__name__}: {e}"}
        results.append(res)
        if C.QUIET:
            if i % 50 == 0 or i == len(uni):
                print(f"screened {i}/{len(uni)}")
        else:
            print(f"[{i:>3}/{len(uni)}] {co.ticker:<6} {res['status']:<8} {res.get('failed') or res.get('notes') or ''}")

    results.sort(key=lambda r: (STATUS_ORDER[r["status"]], -(r.get("returns_avg") or 0)))
    with open(OUT / "screen_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})

    counts = {s: sum(r["status"] == s for r in results) for s in STATUS_ORDER}
    print("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items()))
    verify = [r["ticker"] for r in results
              if r["status"] == "PASS" and "VERIFY" in (r.get("notes") or "")]
    if verify and not C.QUIET:
        print(f"Passed, but debt was assumed or estimated (confirm in 10-K): {', '.join(verify)}")
    if counts["ERROR"] > len(results) * 0.2:
        raise SystemExit(f"Too many errors ({counts['ERROR']}); SEC data may be unavailable. Stopping.")
    print(f"Results: {OUT / 'screen_results.csv'}")


if __name__ == "__main__":
    main()
