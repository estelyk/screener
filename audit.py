"""Print every valuation input for one company, using the screener's own code, so the
numbers can be checked by hand or by a second reviewer. Changes nothing.

    python audit.py LULU
"""
from __future__ import annotations

import os
import sys

import config as C
from edgar import EdgarClient
from metrics import ALL_TAGS, annual_table, merge_facts
from run_phase2 import build
from screen import screen_company
from universe import load_universe
from valuation import _cagr, dcf_parts, estimate_growth, long_run_trend, normalized_fcf


def m(x):
    return "—" if x is None else f"${x / 1e6:,.0f}M"


def p(x):
    return "—" if x is None else f"{x:.2%}"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python audit.py TICKER")
    t = sys.argv[1].upper()
    ua = os.environ.get("SEC_USER_AGENT") or sys.exit('First run: export SEC_USER_AGENT="Your Name you@email.com"')
    uni = load_universe(ua)
    hit = uni[uni["ticker"].str.upper() == t]
    if hit.empty:
        sys.exit(f"{t} is not in the S&P 500 list.")
    co = next(hit.itertuples(index=False))
    client = EdgarClient(ua, keep_tags=ALL_TAGS)
    facts = merge_facts([client.companyfacts(c) for c in [int(co.cik)] + C.PREDECESSOR_CIKS.get(t, [])])
    rows, notes = annual_table(facts)
    s = screen_company(rows)

    print(f"\n{t} ({co.name})  quality screen: {s['status']}  model {C.MODEL_VERSION}")
    for n in notes:
        print(f"  note: {n}")
    print("\nFY end       Revenue    Op. cash   Capex      Stock pay  Owner FCF  FCF margin  Diluted shares")
    for r in rows:
        rev, f = r.get("revenue"), r.get("owner_fcf")
        print(f"{r['fiscal_year_end']}  {m(rev):>9}  {m(r.get('operating_cash_flow')):>9}  {m(r.get('capex')):>9}  "
              f"{m(r.get('sbc')):>9}  {m(f):>9}  {p(f / rev if f is not None and rev else None):>10}  "
              f"{(r.get('diluted_shares') or 0) / 1e6:,.1f}M")

    # The three growth candidates, computed exactly as estimate_growth does.
    rev = [(i, r["revenue"]) for i, r in enumerate(rows) if r.get("revenue")]
    A = _cagr(rev[0][1], rev[-1][1], rev[-1][0] - rev[0][0]) if len(rev) >= 2 else None
    B = _cagr(rev[-4][1], rev[-1][1], rev[-1][0] - rev[-4][0]) if len(rev) >= 4 else None
    n = C.NORMALIZE_YEARS
    Cg = _cagr(normalized_fcf(rows[:n]), normalized_fcf(rows), len(rows) - n) if len(rows) >= n + 3 else None
    g, why = estimate_growth(rows)
    F0 = normalized_fcf(rows)
    trend = long_run_trend(rows)
    gT = min(C.TERMINAL_GROWTH, max(trend if trend is not None else g, 0.0))

    print("\nVALUATION INPUTS")
    print(f"  F0  starting owner FCF (median 5-yr margin x latest revenue): {m(F0)}")
    print(f"  A   ten-year revenue growth per year:   {p(A)}  ({rows[0]['fiscal_year_end'][:4]}-{rows[-1]['fiscal_year_end'][:4]})")
    print(f"  B   three-year revenue growth per year: {p(B)}")
    print(f"  C   normalized FCF growth per year:     {p(Cg)}")
    print(f"  g   growth used for years 1-5:          {p(g)}   ({why})")
    print(f"  gT  long-run growth after year 10:      {p(gT)}")
    if F0 and F0 > 0:
        near, far = dcf_parts(F0, g, trend=trend)
        print(f"  Value of years 1-10 today:   {m(near)}")
        print(f"  Value of years 11+ today:    {m(far)}")
        print(f"  Terminal value share:        {p(far / (near + far))}")

    try:
        from prices import market_data
        prices, splits = market_data([t])
        v = build([{"ticker": t, "name": co.name, "sector": co.sector, "notes": notes, "annual": rows}],
                  prices, splits)[0]
        print(f"\n  Shares used: {(v.get('shares') or 0) / 1e6:,.1f}M   {v.get('value_note')}")
        d = lambda x: "—" if x is None else f"${x:,.2f}"
        print(f"  Value per share: base {d(v['value_per_share'])}  (range {d(v['value_low'])} to {d(v['value_high'])})")
        print(f"  No-recovery value (last year's FCF {m(v.get('last_fcf'))}, growth <= last year's sales "
              f"{p(v.get('last_growth'))}): {d(v.get('value_no_recovery'))}")
        print(f"  Price: {d(v['price'])}   buy below: {d(v['buy_below'])}   discount: {p(v['discount'])}"
              f"   signal: {v['signal']}")
        cd = v.get("cash_drop")
        if cd:
            print(f"  Why cash fell: {m(cd['before'])} (avg of the 2 years before) -> {m(cd['last'])}")
            for k, label in [("profit", "profit"), ("timing", "inventory/bills/timing"),
                             ("spending", "stores & equipment"), ("stock_pay", "stock pay")]:
                print(f"     {label:<24} {'+' if cd['parts'][k] >= 0 else '-'}{m(abs(cd['parts'][k]))}")
            print(f"     mainly: {cd['cause']} ({cd['meaning']})")
        f = v.get("fscore")
        if f:
            print(f"  Piotroski F-score: {f['score']} of {f['out_of']}")
            for label, ok in f["checks"]:
                print(f"     {'yes' if ok else 'no ' if ok is False else ' - '}  {label}")
        z = v.get("zscore")
        print(f"  Altman Z-score: {'—' if z is None else f'{z:.2f}'}")
        for w in v["warnings"]:
            print(f"  warning: {w}")
    except Exception as e:
        print(f"\n  (price step skipped: {type(e).__name__}: {e})")


if __name__ == "__main__":
    main()
