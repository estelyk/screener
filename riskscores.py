"""Two classic "is this business getting worse?" scores, shown as warnings only.
They never change the estimated value or the buy signal.

Piotroski F-score (2000): nine yes/no checks comparing the latest year with the year
before. 8-9 = strong and improving, 0-3 = weakening on most measures. Built for
telling real bargains from value traps among cheap-looking stocks.
  Gross margin isn't in SEC's standard data for many companies, so check 8 uses the
  operating margin (profit from the business before interest and tax) instead.

Altman Z-score (1968): a weighted mix of five ratios that historically separated
companies heading for bankruptcy. Below 1.8 = distress zone. Designed for
manufacturers; it often reads low for retailers, software and companies that have
bought back a lot of stock, so it is a prompt to look, not a verdict.
"""
from __future__ import annotations

import config as C

F_WARN_AT = 3          # warn when the F-score is this or lower (out of 9)
Z_DISTRESS = 1.81
Z_SAFE = 2.99


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def fscore(rows: list[dict]) -> dict | None:
    """{"score", "out_of", "checks": [(label, passed)]} or None if too little data."""
    if len(rows) < 3:
        return None
    p2, p1, t = rows[-3], rows[-2], rows[-1]
    ni, cfo, assets_prev = t.get("net_income"), t.get("operating_cash_flow"), p1.get("total_assets")
    roa = _div(ni, assets_prev)
    roa_prev = _div(p1.get("net_income"), p2.get("total_assets"))

    def lev(r):
        return _div(r.get("long_term_debt"), r.get("total_assets"))

    def cur(r):
        return _div(r.get("current_assets"), r.get("current_liabilities"))

    def margin(r):
        return _div(r.get("operating_income"), r.get("revenue"))

    def turnover(r, prev):
        return _div(r.get("revenue"), prev.get("total_assets"))

    sh, sh_prev = t.get("diluted_shares"), p1.get("diluted_shares")
    no_issue = None
    if sh and sh_prev:
        ratio = sh / sh_prev
        no_issue = ratio <= 1.0 or ratio >= 1.8          # a jump of 1.8x+ is a stock split

    def cmp(a, b):
        return None if a is None or b is None else a > b

    lv, lv_prev = lev(t), lev(p1)
    checks = [
        ("Profitable last year", None if roa is None else roa > 0),
        ("Brought in cash from operations", None if cfo is None else cfo > 0),
        ("Return on assets improved", cmp(roa, roa_prev)),
        ("Cash from operations exceeded reported profit", cmp(cfo, ni)),
        ("Debt fell relative to assets", None if lv is None or lv_prev is None else lv <= lv_prev),
        ("Short-term bill-paying ability improved", cmp(cur(t), cur(p1))),
        ("Didn't issue new shares", no_issue),
        ("Operating margin improved", cmp(margin(t), margin(p1))),
        ("Sales per dollar of assets improved", cmp(turnover(t, p1), turnover(p1, p2))),
    ]
    known = [(label, ok) for label, ok in checks if ok is not None]
    if len(known) < 7:
        return None
    return {"score": sum(1 for _, ok in known if ok), "out_of": len(known),
            "checks": [[label, ok] for label, ok in checks]}


def zscore(rows: list[dict], market_cap: float | None) -> float | None:
    if not rows or not market_cap:
        return None
    r = rows[-1]
    ta, tl = r.get("total_assets"), r.get("total_liabilities")
    parts = [r.get("current_assets"), r.get("current_liabilities"), r.get("retained_earnings"),
             r.get("operating_income"), r.get("revenue")]
    if not ta or ta <= 0 or not tl or tl <= 0 or any(x is None for x in parts):
        return None
    ca, cl, re, ebit, sales = parts
    return (1.2 * (ca - cl) / ta + 1.4 * re / ta + 3.3 * ebit / ta
            + 0.6 * market_cap / tl + 1.0 * sales / ta)


def f_warning(f: dict | None) -> str | None:
    if not f:
        return None
    weak = f["score"] <= F_WARN_AT if f["out_of"] == 9 else f["score"] / f["out_of"] <= F_WARN_AT / 9
    if weak:
        return (f"business weakening on most measures (Piotroski F-score {f['score']} of "
                f"{f['out_of']}; see the brief)")
    return None


def z_warning(z: float | None, rows: list[dict] | None = None) -> str | None:
    # Negative retained earnings in a company that passed the quality screen almost
    # always means years of buybacks and dividends, not losses, which drags Z down
    # without signalling distress. The score is still shown, but not as a warning.
    re = (rows or [{}])[-1].get("retained_earnings") if rows else None
    if re is not None and re < 0:
        return None
    if z is not None and z < Z_DISTRESS:
        return (f"financial-distress score is low (Altman Z {z:.1f}, below 1.8); often reads low "
                "for retailers, software and big buyback companies, so check debt and cash")
    return None
