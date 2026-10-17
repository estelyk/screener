"""Quarterly early warning: how is the current fiscal year going so far?

Annual reports come once a year, so a business can have three bad quarters before
the yearly numbers show it. Every quarterly report (10-Q) states the year-to-date
figures AND the same period a year earlier, so this compares like with like:

    sales, profit, cash from operations, capex and owner cash flow
    for the N months since the last annual report  vs  the same N months a year before

and from those, the last-12-months owner cash flow:

    last 12 months = last fiscal year + this year so far - same period last year

Shown on the page and used for warnings only. It never changes the estimated value,
which stays on full fiscal years.
"""
from __future__ import annotations

from datetime import date, timedelta

from metrics import TAGS

QUARTERLY_FORMS = {"10-Q", "10-Q/A"}
TOL = 10                     # days of slack for 52/53-week fiscal calendars
METRICS = ("revenue", "net_income", "operating_cash_flow", "capex", "sbc")

SALES_DOWN = -0.03           # warn if sales so far are down more than 3%
PROFIT_DOWN = -0.25          # ...profit down more than 25%
CASH_DOWN = -0.25            # ...owner cash flow down more than 25%


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _year_back(d: date) -> date:
    return d - timedelta(days=365)


def _durations(facts: dict, tag: str) -> list[dict]:
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag, {})
    out = []
    for r in node.get("units", {}).get("USD", []):
        if r.get("form") in QUARTERLY_FORMS and r.get("start") and r.get("end"):
            out.append(r)
    return out


def _pick(rows: list[dict], start: date, end: date) -> float | None:
    """Value for the period matching (start, end); closest dates first, then the
    latest filing (restatements win)."""
    matches = [r for r in rows
               if abs((_d(r["start"]) - start).days) <= TOL and abs((_d(r["end"]) - end).days) <= TOL]
    if not matches:
        return None
    best = min(matches, key=lambda r: (abs((_d(r["end"]) - end).days) + abs((_d(r["start"]) - start).days),
                                       _neg(r.get("filed", ""))))
    return float(best["val"])


def _neg(filed: str) -> tuple:
    return tuple(-ord(ch) for ch in filed)        # sorts later filings first


def _latest_ytd_period(facts: dict, fy_end: date) -> tuple[date, date] | None:
    """(start, end) of the newest year-to-date period after the last annual report."""
    start_target = fy_end + timedelta(days=1)
    candidates = []
    for metric in ("revenue", "operating_cash_flow", "net_income"):
        for tag in TAGS[metric]:
            for r in _durations(facts, tag):
                s, e = _d(r["start"]), _d(r["end"])
                if abs((s - start_target).days) <= TOL and e > fy_end and 80 <= (e - s).days <= 300:
                    candidates.append((e, s))
    if not candidates:
        return None
    e, s = max(candidates)
    return s, e


def _pair(facts: dict, metric: str, start: date, end: date):
    """(this year so far, same period last year) from the first tag that has both."""
    for tag in TAGS[metric]:
        rows = _durations(facts, tag)
        cur = _pick(rows, start, end)
        prev = _pick(rows, _year_back(start), _year_back(end))
        if cur is not None and prev is not None:
            return cur, prev
    return None


def _change(cur, prev):
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1


def quarterly_update(facts: dict | None, rows: list[dict]) -> dict | None:
    if not facts or not rows or not rows[-1].get("fiscal_year_end"):
        return None
    fy_end = _d(rows[-1]["fiscal_year_end"])
    period = _latest_ytd_period(facts, fy_end)
    if not period:
        return None
    start, end = period
    items = {}
    for m in METRICS:
        p = _pair(facts, m, start, end)
        if p:
            items[m] = {"cur": p[0], "prev": p[1], "change": _change(*p)}
    if "revenue" not in items and "operating_cash_flow" not in items:
        return None

    owner = None
    if "operating_cash_flow" in items and "capex" in items:
        sbc = items.get("sbc", {"cur": 0.0, "prev": 0.0})
        cur = items["operating_cash_flow"]["cur"] - items["capex"]["cur"] - sbc["cur"]
        prev = items["operating_cash_flow"]["prev"] - items["capex"]["prev"] - sbc["prev"]
        owner = {"cur": cur, "prev": prev, "change": _change(cur, prev)}
        fy = rows[-1].get("owner_fcf")
        owner["last12"] = fy + cur - prev if fy is not None else None
    months = round((end - start).days / 30.44)
    return {"start": start.isoformat(), "through": end.isoformat(), "months": months,
            "fy_end": fy_end.isoformat(), "items": items, "owner_fcf": owner}


def warnings(q: dict | None) -> list[str]:
    if not q:
        return []
    when = f"in the {q['months']} months to {q['through']} vs a year earlier (latest quarterly report)"
    out = []
    rev = q["items"].get("revenue", {}).get("change")
    if rev is not None and rev < SALES_DOWN:
        out.append(f"sales down {-rev:.0%} {when}")
    ni = q["items"].get("net_income", {}).get("change")
    if ni is not None and ni < PROFIT_DOWN:
        out.append(f"profit down {-ni:.0%} {when}")
    o = q.get("owner_fcf") or {}
    if o.get("change") is not None and o["change"] < CASH_DOWN:
        out.append(f"owner cash flow down {-o['change']:.0%} {when}")
    elif o.get("prev") is not None and o["prev"] > 0 and o.get("cur") is not None and o["cur"] <= 0:
        out.append(f"owner cash flow negative so far this year {when}")
    return out
