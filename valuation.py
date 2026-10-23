"""Conservative intrinsic value from owner free cash flow (a discounted cash flow model).

Steps for one company:
1. Starting cash flow: owner FCF (cash from operations - capex - stock comp),
   normalized as the median owner-FCF margin of the last 5 years x the latest
   revenue. One-off years (a tax deposit, an acquisition payment) are ignored by
   the median, and growing companies are valued at today's size.
2. Growth: the LOWEST of 10-year revenue growth, recent (3-year) revenue growth and
   owner-FCF growth, kept between MIN_GROWTH and MAX_GROWTH. The recent rate stops
   a slowing business from being valued on its glory years.
3. Project 10 years: growth for 5 years, fading to the long-run rate by year 10,
   then a terminal value for everything after. The long-run rate is capped by the
   company's own 10-year revenue trend (floored at 0%), so a temporary slump doesn't
   lower it forever, but a business in long-term decline isn't faded up to 2.5%.
4. Discount every year back to today at the discount rate and add it up.
5. Divide by diluted shares. Debt is NOT subtracted: owner cash flow is already
   after interest, so it is cash to shareholders (subtracting debt would count it
   twice). Excess cash is not added either; the interest it earns is already in.
Each company is valued under bear / base / bull assumptions (config.SCENARIOS);
the base case drives the buy signal.
"""
from __future__ import annotations

from statistics import mean, median

import config as C
from riskscores import f_warning, fscore


def _vals(rows, key):
    return [r.get(key) for r in rows if r.get(key) is not None]


def _cagr(start: float, end: float, years: float) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0 or years <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def normalized_fcf(rows: list[dict]) -> float | None:
    """Median owner-FCF margin over the last NORMALIZE_YEARS years x latest revenue.
    Falls back to the median owner FCF when revenue is missing."""
    window = rows[-C.NORMALIZE_YEARS:]
    margins = [r["owner_fcf"] / r["revenue"] for r in window
               if r.get("owner_fcf") is not None and r.get("revenue")]
    latest_rev = next((r["revenue"] for r in reversed(window) if r.get("revenue")), None)
    if len(margins) >= C.MIN_MARGIN_YEARS and latest_rev:
        return median(margins) * latest_rev
    fcf = _vals(window, "owner_fcf")
    return median(fcf) if len(fcf) >= C.MIN_MARGIN_YEARS else None


def long_run_trend(rows: list[dict]) -> float | None:
    """Revenue growth per year across the whole history (usually 10 years)."""
    rev = [(i, r["revenue"]) for i, r in enumerate(rows) if r.get("revenue")]
    if len(rev) < 2:
        return None
    return _cagr(rev[0][1], rev[-1][1], rev[-1][0] - rev[0][0])


def estimate_growth(rows: list[dict]) -> tuple[float, str]:
    """Return (growth rate, explanation)."""
    candidates = {}
    rev = [(i, r["revenue"]) for i, r in enumerate(rows) if r.get("revenue")]
    if len(rev) >= 2:
        g = _cagr(rev[0][1], rev[-1][1], rev[-1][0] - rev[0][0])
        if g is not None:
            candidates["10-yr revenue"] = g
    if len(rev) >= 4:
        g = _cagr(rev[-4][1], rev[-1][1], rev[-1][0] - rev[-4][0])
        if g is not None:
            candidates["recent revenue"] = g

    # Cash-flow growth: normalized cash flow at the start vs the end of the history,
    # so one-off years at either end don't distort it.
    n = C.NORMALIZE_YEARS
    if len(rows) >= n + 3:
        start, end = normalized_fcf(rows[:n]), normalized_fcf(rows)
        g = _cagr(start, end, len(rows) - n)
        if g is not None:
            candidates["owner FCF"] = g

    if not candidates:
        return 0.0, "no reliable growth history; assumed 0%"
    basis, g = min(candidates.items(), key=lambda kv: kv[1])
    capped = max(C.MIN_GROWTH, min(g, C.MAX_GROWTH))
    why = f"{basis} growth {g:.1%}/yr"
    if capped != g:
        why += f", limited to {capped:.1%}"
    return capped, why


def dcf_parts(fcf0: float, growth: float, r: float | None = None,
              tg: float | None = None, trend: float | None = None) -> tuple[float, float]:
    """(PV of years 1-10, PV of everything after) for cash flow starting at fcf0.
    trend: long-run revenue growth; caps the terminal rate (defaults to `growth`)."""
    r = C.DISCOUNT_RATE if r is None else r
    tg = C.TERMINAL_GROWTH if tg is None else tg
    tg = min(tg, max(growth if trend is None else trend, 0.0))
    hi, n = C.HIGH_GROWTH_YEARS, C.PROJECTION_YEARS
    pv, f = 0.0, fcf0
    for t in range(1, n + 1):
        g = growth if t <= hi else growth + (tg - growth) * (t - hi) / (n - hi)
        f *= 1 + g
        pv += f / (1 + r) ** t
    terminal = f * (1 + tg) / (r - tg)
    return pv, terminal / (1 + r) ** n


def dcf(fcf0: float, growth: float, r: float | None = None, tg: float | None = None,
        trend: float | None = None) -> float:
    """Present value of all future cash flows, starting from fcf0 per year."""
    return sum(dcf_parts(fcf0, growth, r, tg, trend))


# What to look up for each source of a cash-flow drop. Deliberately no verdict on
# whether it will last: that needs the cash-flow statement and management's account.
CHECK = {
    "operating": ("lower cash from operations",
                  "Look at the cash-flow statement and the earnings release: was it lower profit, "
                  "or more cash tied up in inventory, unpaid bills, taxes or other timing items?"),
    "capex": ("higher spending on stores and equipment",
              "Look at what the spending is for (new stores, distribution centres, technology) "
              "and whether management expects it to stay this high."),
    "stock_pay": ("more pay in stock", "Look at whether stock-based pay keeps rising as a share of sales."),
}


def cash_drop(rows: list[dict]) -> dict | None:
    """Where did the fall in owner cash flow come from? Facts only, last year vs the
    year before:  owner FCF = operating cash flow - capex - stock pay, and
    operating cash flow = profit + everything else (inventory, bills, taxes, timing).
    Returns None unless owner cash flow fell by more than 10%."""
    if len(rows) < 2:
        return None
    t, p = rows[-1], rows[-2]
    keys = ("owner_fcf", "operating_cash_flow", "capex")
    if any(r.get(k) is None for r in (t, p) for k in keys):
        return None
    before, last = p["owner_fcf"], t["owner_fcf"]
    if before <= 0 or last >= 0.9 * before:
        return None
    d_ocf = t["operating_cash_flow"] - p["operating_cash_flow"]
    d_capex = t["capex"] - p["capex"]
    d_sbc = (t.get("sbc") or 0.0) - (p.get("sbc") or 0.0)
    d_ni = (t["net_income"] - p["net_income"]
            if t.get("net_income") is not None and p.get("net_income") is not None else None)
    effects = {"operating": d_ocf, "capex": -d_capex, "stock_pay": -d_sbc}   # effect on owner FCF
    main = min(effects, key=effects.get)
    return {"last": last, "before": before, "change": last - before,
            "ocf_change": d_ocf, "capex_change": d_capex, "sbc_change": d_sbc,
            "profit_change": d_ni, "other_change": None if d_ni is None else d_ocf - d_ni,
            "main": main, "cause": CHECK[main][0], "check": CHECK[main][1]}


def warnings(rows: list[dict]) -> list[str]:
    """Plain-English reasons a stock might be cheap for a good reason."""
    out = []
    rev = _vals(rows, "revenue")
    if len(rev) >= 3 and rev[-1] < rev[-3]:
        out.append(f"revenue down {1 - rev[-1] / rev[-3]:.0%} over the last 2 years")
    ni = _vals(rows, "net_income")
    if len(ni) >= 3 and max(ni) > 0 and ni[-1] < 0.7 * max(ni):
        out.append(f"earnings {1 - ni[-1] / max(ni):.0%} below their 10-year peak")
    # Compare cash-flow MARGINS, not raw cash flow, so steady growers don't trip it:
    # only a jump in how much cash each dollar of sales produces suggests a boom.
    paired = [(r["owner_fcf"], r["revenue"]) for r in rows
              if r.get("owner_fcf") is not None and r.get("revenue")]
    if len(paired) >= 6:
        recent = sum(f for f, _ in paired[-3:]) / sum(s for _, s in paired[-3:])
        earlier = sum(f for f, _ in paired[:-3]) / sum(s for _, s in paired[:-3])
        if earlier > 0 and recent > 1.5 * earlier and recent - earlier > 0.05:
            out.append(f"cash flow margin {recent:.0%} recently vs {earlier:.0%} before "
                       "(could be a cyclical peak)")

    # Dilution: share count growing more than ~2% a year over 5 years. A jump of 1.8x
    # or more is almost always an unadjusted stock split in old filings, so skip it.
    sh = _vals(rows, "diluted_shares")
    if len(sh) >= 6 and sh[-6] > 0:
        ratio = sh[-1] / sh[-6]
        if C.DILUTION_WARN < ratio < 1.8:
            out.append(f"share count up {ratio - 1:.0%} over 5 years (dilution)")

    # Recovery assumed: the normal (median-margin) cash flow is well above what the
    # company actually produced last year. Fine for a one-off bad year; risky if the
    # business is getting worse, so it is shown, not hidden.
    f0, last = normalized_fcf(rows), (rows[-1].get("owner_fcf") if rows else None)
    if f0 and f0 > 0 and last is not None:
        if last <= 0:
            out.append("last year's owner cash flow was negative; the value assumes a recovery")
        elif f0 > (1 + C.RECOVERY_GAP_WARN) * last:
            out.append(f"value assumes cash flow recovers: it uses {f0 / last - 1:.0%} more than "
                       "last year's actual (see the no-recovery value)")

    dc = debt_check(rows)
    if dc and dc["years_loans"] is not None and dc["years_loans"] > dc["limit"] and dc["short_term"]:
        out.append(f"with short-term borrowings included, debt is {dc['years_loans']:.1f} years of "
                   f"profit (limit {dc['limit']:g}); see the debt check")
    nm = sum(1 for r in rows if r.get("roic_nm"))
    roe_distorted = any(r.get("roe") is None or (r.get("roe") or 0) > 1.0 for r in rows if r.get("net_income") is not None)
    if roe_distorted and nm >= 3:
        out.append(f"return on capital isn't meaningful in {nm} of {len(rows)} years (very small capital base "
                   "after buybacks), so the returns test may overstate quality")
    fw = f_warning(fscore(rows))
    if fw:
        out.append(fw)

    # Earnings not backed by cash: operating cash flow well below reported profit.
    recent5 = [(r["operating_cash_flow"], r["net_income"]) for r in rows[-5:]
               if r.get("operating_cash_flow") is not None and r.get("net_income") is not None]
    if len(recent5) >= 4:
        ocf, ni = sum(o for o, _ in recent5), sum(n for _, n in recent5)
        if ni > 0 and ocf < C.CASH_BACKING_MIN * ni:
            out.append(f"reported earnings not fully backed by cash "
                       f"(operating cash flow {ocf / ni:.0%} of earnings over 5 years)")
    return out


def debt_check(rows: list[dict]) -> dict | None:
    """The debt test, worked out in full so you don't have to check it by hand:
    loans (long-term debt + short-term borrowings) and, separately, store/office
    leases, each divided by average profit over the last 3 years."""
    if not rows:
        return None
    t = rows[-1]
    ni = [r.get("net_income") for r in rows[-3:] if r.get("net_income") is not None]
    avg = sum(ni) / len(ni) if ni else None
    lt = t.get("long_term_debt")
    st = t.get("short_term_debt") or 0.0
    leases = t.get("lease_liabilities")
    loans = (lt or 0.0) + st

    def years(x):
        return x / avg if avg and avg > 0 else None

    return {"long_term": lt, "short_term": st, "loans": loans, "leases": leases,
            "avg_profit": avg, "years_loans": years(loans),
            "years_with_leases": years(loans + (leases or 0.0)) if leases else None,
            "limit": C.DEBT_YEARS_OF_EARNINGS_MAX, "interest": t.get("interest_expense"),
            "assumed_zero": bool(t.get("debt_assumed_zero")), "estimated": bool(t.get("debt_estimated"))}


def value_company(rows: list[dict], split_factor: float = 1.0) -> dict:
    """Intrinsic value per share, or a reason it can't be estimated.
    split_factor: shares multiply by this for splits since the last annual report."""
    out = {"value_per_share": None, "value_low": None, "value_high": None, "long_run_growth": None,
           "value_no_recovery": None, "last_fcf": None, "last_growth": None,
           "fscore": fscore(rows), "zscore": None, "cash_drop": cash_drop(rows),
           "debt_check": debt_check(rows), "shadow": {},
           "terminal_share": None, "growth": None, "normalized_fcf": None,
           "net_debt": None, "shares": None, "value_note": "", "warnings": warnings(rows)}
    fcf0 = normalized_fcf(rows)
    if fcf0 is None:
        out["value_note"] = "not enough recent cash flow data"
        return out
    out["normalized_fcf"] = fcf0
    if fcf0 <= 0:
        out["value_note"] = "recent owner free cash flow not positive"
        return out

    latest = rows[-1]
    shares = latest.get("diluted_shares")
    if not shares or shares <= 0:
        out["value_note"] = "share count not found"
        return out
    shares *= split_factor

    growth, why = estimate_growth(rows)
    debt = latest.get("long_term_debt") or 0.0
    cash = latest.get("cash") or 0.0
    net_debt = debt - cash   # shown for information only; not subtracted (see above)
    trend = long_run_trend(rows)
    out["long_run_growth"] = min(C.TERMINAL_GROWTH, max(trend if trend is not None else growth, 0.0))
    near, far = dcf_parts(fcf0, growth, trend=trend)
    out.update(growth=growth, net_debt=net_debt, shares=shares, value_note=why)
    out["value_per_share"] = (near + far) / shares
    out["terminal_share"] = far / (near + far)
    r_lo, tg_lo = C.SCENARIOS["bear"]
    r_hi, tg_hi = C.SCENARIOS["bull"]
    out["value_low"] = dcf(fcf0, growth, r_lo, tg_lo, trend) / shares
    out["value_high"] = dcf(fcf0, growth, r_hi, tg_hi, trend) / shares
    # "No recovery" check (shown only, never used for the signal): start from last
    # year's actual cash flow and grow at no more than last year's sales growth.
    last = latest.get("owner_fcf")
    out["last_fcf"] = last
    rev = [r.get("revenue") for r in rows[-2:]]
    g1 = rev[1] / rev[0] - 1 if len(rev) == 2 and rev[0] and rev[1] else None
    out["last_growth"] = g1
    if last is not None and last > 0 and last < fcf0:
        g_low = max(C.MIN_GROWTH, min(growth, g1 if g1 is not None else growth, C.MAX_GROWTH))
        out["value_no_recovery"] = dcf(last, g_low, trend=trend) / shares

    # Shadow models (recorded for the experiment, never used for the pre-buy watch):
    # three other ways to pick the starting cash flow, same growth and discounting.
    recent = [r.get("owner_fcf") for r in rows[-3:] if r.get("owner_fcf") is not None]
    starts = {
        "min": min(fcf0, last) if last is not None else None,
        "blend": (fcf0 + last) / 2 if last is not None else None,
        "recent3": sum(recent) / len(recent) if len(recent) == 3 else None,
    }
    out["shadow"] = {k: (dcf(f, growth, trend=trend) / shares if f is not None and f > 0 else None)
                     for k, f in starts.items()}
    if split_factor != 1.0:
        out["value_note"] += f"; shares adjusted for a {split_factor:g}-for-1 split"
    return out


def price_check(value_per_share: float | None, price: float | None) -> dict:
    """Compare price to value. discount > 0 means price is below value."""
    if not value_per_share or not price:
        return {"buy_below": None, "discount": None, "signal": False, "to_buy_price": None,
                "suspicious": False}
    buy_below = value_per_share * (1 - C.MARGIN_OF_SAFETY)
    discount = 1 - price / value_per_share
    return {
        "buy_below": buy_below,
        "discount": discount,                            # how far below value the price is
        "signal": price <= buy_below,
        "to_buy_price": 1 - buy_below / price,           # how far price must fall to signal
        # Markets rarely misprice a quality business this much; usually it's a data issue.
        "suspicious": discount > C.SUSPICIOUS_DISCOUNT,
    }
