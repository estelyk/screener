"""The quality screen. Each check returns True (pass), False (fail) or None (not
enough data to judge). A company is:
  FAIL    if any check fails,
  NO_DATA if nothing fails but some check couldn't be judged,
  PASS    if every check passes.
NO_DATA is worth reviewing: it usually means a tag mapping in metrics.py needs work.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import mean

import config as C


@dataclass
class Check:
    name: str
    passed: bool | None
    detail: str


def _vals(rows: list[dict], key: str) -> list[float]:
    return [r[key] for r in rows if r.get(key) is not None]


def check_history(rows):
    n = len(_vals(rows, "net_income"))
    ok = n >= C.MIN_YEARS_REQUIRED
    return Check("history", True if ok else None, f"{n} years of annual data")


def check_returns(rows):
    distorted = any(
        r["equity"] is not None and (r["equity"] <= 0 or (r["roe"] or 0) > C.ROE_DISTORTED_ABOVE)
        for r in rows
    )
    if not distorted:
        basis, key, avg_min, floor, max_below = "ROE", "roe", C.ROE_AVG_MIN, C.ROE_FLOOR, C.ROE_YEARS_BELOW_FLOOR_MAX
    else:
        basis, key, avg_min, floor, max_below = "ROIC", "roic", C.ROIC_AVG_MIN, C.ROIC_FLOOR, C.ROIC_YEARS_BELOW_FLOOR_MAX
    vals = _vals(rows, key)
    extra = {"returns_basis": basis, "returns_avg": None}
    if len(vals) < C.MIN_YEARS_REQUIRED:
        return Check("returns", None, f"{basis}: only {len(vals)} years computable"), extra
    avg = mean(vals)
    below = sum(v < floor for v in vals)
    extra["returns_avg"] = avg
    why = ""
    if distorted:
        methods = sorted({r["roic_method"] for r in rows if r.get("roic_method")})
        why = f" (equity distorted by buybacks, so return on capital used: {', '.join(methods)})"
    return Check("returns", avg >= avg_min and below <= max_below,
                 f"{basis} avg {avg:.1%}, {below} yr(s) below {floor:.0%}{why}"), extra


def check_debt(rows):
    debt = rows[-1]["long_term_debt"] if rows else None
    recent_ni = _vals(rows[-3:], "net_income")
    if debt is None or not recent_ni:
        return Check("debt", None, "latest debt or earnings missing"), {"debt_years": None}
    avg_ni = mean(recent_ni)
    if avg_ni <= 0:
        return Check("debt", False, "recent average earnings not positive"), {"debt_years": None}
    years = debt / avg_ni
    return (Check("debt", years <= C.DEBT_YEARS_OF_EARNINGS_MAX,
                  f"debt = {years:.1f} years of earnings"), {"debt_years": years})


def check_fcf(rows):
    key = "owner_fcf" if C.SUBTRACT_SBC else "fcf"
    vals = _vals(rows, key)
    if len(vals) < C.MIN_YEARS_REQUIRED:
        return Check("fcf", None, f"only {len(vals)} years of cash flow data"), {"fcf_positive_years": None}
    need = math.ceil(C.FCF_POSITIVE_SHARE * len(vals))
    pos = sum(v > 0 for v in vals)
    return (Check("fcf", pos >= need, f"free cash flow positive {pos}/{len(vals)} years"),
            {"fcf_positive_years": f"{pos}/{len(vals)}"})


def check_earnings(rows):
    ni = _vals(rows, "net_income")
    if len(ni) < C.MIN_YEARS_REQUIRED:
        return Check("earnings", None, f"only {len(ni)} years of earnings"), {"loss_years": None}
    losses = sum(v < 0 for v in ni)
    drops = sum(1 for a, b in zip(ni, ni[1:]) if a > 0 and b < a * (1 - C.BIG_DROP))
    ok = losses <= C.LOSS_YEARS_MAX and drops <= C.BIG_DROP_YEARS_MAX
    return (Check("earnings", ok, f"{losses} loss year(s), {drops} drop(s) over {C.BIG_DROP:.0%}"),
            {"loss_years": losses})


def screen_company(rows: list[dict]) -> dict:
    checks, summary = [check_history(rows)], {}
    for fn in (check_returns, check_debt, check_fcf, check_earnings):
        chk, extra = fn(rows)
        checks.append(chk)
        summary.update(extra)

    if any(c.passed is False for c in checks):
        status = "FAIL"
    elif any(c.passed is None for c in checks):
        status = "NO_DATA"
    else:
        status = "PASS"
    summary.update({
        "status": status,
        "failed": ";".join(c.name for c in checks if c.passed is False),
        "unjudged": ";".join(c.name for c in checks if c.passed is None),
        "years": len(rows),
        "checks": [asdict(c) for c in checks],
    })
    return summary
