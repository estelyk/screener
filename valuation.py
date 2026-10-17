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
   then a terminal value for everything after. A business growing slower than the
   long-run rate is never faded UP to it (and a shrinking one ends at 0%).
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
              tg: float | None = None) -> tuple[float, float]:
    """(PV of years 1-10, PV of everything after) for cash flow starting at fcf0."""
    r = C.DISCOUNT_RATE if r is None else r
    tg = C.TERMINAL_GROWTH if tg is None else tg
    tg = min(tg, max(growth, 0.0))   # slow or shrinking businesses don't fade upward
    hi, n = C.HIGH_GROWTH_YEARS, C.PROJECTION_YEARS
    pv, f = 0.0, fcf0
    for t in range(1, n + 1):
        g = growth if t <= hi else growth + (tg - growth) * (t - hi) / (n - hi)
        f *= 1 + g
        pv += f / (1 + r) ** t
    terminal = f * (1 + tg) / (r - tg)
    return pv, terminal / (1 + r) ** n


def dcf(fcf0: float, growth: float, r: float | None = None, tg: float | None = None) -> float:
    """Present value of all future cash flows, starting from fcf0 per year."""
    return sum(dcf_parts(fcf0, growth, r, tg))


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
    return out


def value_company(rows: list[dict], split_factor: float = 1.0) -> dict:
    """Intrinsic value per share, or a reason it can't be estimated.
    split_factor: shares multiply by this for splits since the last annual report."""
    out = {"value_per_share": None, "value_low": None, "value_high": None,
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
    near, far = dcf_parts(fcf0, growth)
    out.update(growth=growth, net_debt=net_debt, shares=shares, value_note=why)
    out["value_per_share"] = (near + far) / shares
    out["terminal_share"] = far / (near + far)
    r_lo, tg_lo = C.SCENARIOS["bear"]
    r_hi, tg_hi = C.SCENARIOS["bull"]
    out["value_low"] = dcf(fcf0, growth, r_lo, tg_lo) / shares
    out["value_high"] = dcf(fcf0, growth, r_hi, tg_hi) / shares
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
