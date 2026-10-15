"""Show which XBRL tags companies actually file, to fix NO_DATA cases.

Usage:
    python diagnose.py XOM                         # common financial keywords
    python diagnose.py NVDA --kw capex             # keyword search: capex, debt, interest...
    python diagnose.py NVR PCAR DE --kw debt interest

Writes output/diagnose.txt — upload that file to Claude.
Tags marked * are already used by the screener.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import config as C
from edgar import EdgarClient
from metrics import ALL_TAGS, annual_points, merge_facts
from universe import load_universe

DEFAULT_KEYWORDS = ["debt", "notes", "borrowing", "propertyplant", "productiveassets",
                    "capitalexpend", "netincome", "profitloss", "operatingincome",
                    "beforeincometaxes", "interestexpense", "stockholdersequity",
                    "revenue", "operatingactivities", "sharebased"]
ALIASES = {"capex": ["paymentstoacquire", "capital", "propertyplant", "productiveassets"],
           "debt": ["debt", "notes", "borrowing", "seniornotes", "commercialpaper"],
           "interest": ["interest"]}


def report(client, uni, ticker: str, keywords: list[str]) -> list[str]:
    match = uni[uni["ticker"].str.upper() == ticker]
    if match.empty:
        return [f"{ticker}: not in the S&P 500 list", ""]
    ciks = [int(match.iloc[0]["cik"])] + C.PREDECESSOR_CIKS.get(ticker, [])
    facts = merge_facts([client.raw_companyfacts(c) for c in ciks])
    if not facts:
        return [f"{ticker}: no company facts on EDGAR (CIKs {ciks})", ""]
    gaap = facts.get("facts", {}).get("us-gaap", {})

    lines = [f"{ticker}  CIK {ciks}  {facts.get('entityName')}",
             f"{'tag':<95} {'years':>5}  {'range':<11}  latest", "-" * 130]
    for tag in sorted(gaap):
        if not any(k in tag.lower() for k in keywords):
            continue
        rows = gaap[tag].get("units", {}).get("USD", [])
        instant = bool(rows) and "start" not in rows[0]
        pts = annual_points(facts, tag, instant=instant)
        if not pts:
            continue
        ends = sorted(pts)
        mark = "*" if tag in ALL_TAGS else " "
        lines.append(f"{mark}{tag:<94} {len(ends):>5}  {ends[0][:4]}-{ends[-1][:4]}    "
                     f"{pts[ends[-1]]:,.0f}")
    return lines + [""]


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit("Usage: python diagnose.py TICKER [TICKER ...] [--kw keyword ...]")
    ua = os.environ.get("SEC_USER_AGENT") or sys.exit("Set SEC_USER_AGENT first.")
    if "--kw" in args:
        i = args.index("--kw")
        tickers, raw_kw = args[:i], args[i + 1:]
    else:
        tickers, raw_kw = args, []
    keywords = []
    for k in raw_kw or DEFAULT_KEYWORDS:
        keywords += ALIASES.get(k.lower(), [k.lower()])

    uni = load_universe(ua)
    client = EdgarClient(ua, keep_tags=ALL_TAGS)
    lines = []
    for t in tickers:
        lines += report(client, uni, t.upper(), keywords)

    text = "\n".join(lines)
    print(text)
    out = Path("output") / "diagnose.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n")
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
