"""Review flags for companies you own. A flag means "take a look", not "sell".

Buffett sells for few reasons, and the flags follow them:
1. The business got worse   -> it no longer passes the quality screen, or it has a
                                "cheap for a reason" warning (falling revenue, etc.)
2. Price far ahead of value -> price more than OVERVALUED_ABOVE above estimated value
Price drops alone are never a flag.

The same reasons are shown in the app and used for the email alert, so they agree.
"""
from __future__ import annotations

import config as C

CHECK_LABELS = {"history": "track record", "returns": "returns on capital", "debt": "debt",
                "fcf": "free cash flow", "earnings": "earnings consistency"}


def review_reasons(entry: dict) -> list[dict]:
    """[{kind, text}] for one company entry from the page data."""
    status = entry.get("status", "PASS")
    reasons: list[dict] = []
    if status == "FAIL":
        failed = [CHECK_LABELS.get(k["name"], k["name"])
                  for k in entry.get("checks", []) if k.get("passed") is False]
        reasons.append({"kind": "quality",
                        "text": "No longer passes the quality screen"
                                + (f" (failed: {', '.join(failed)})" if failed else "")})
    elif status == "NO_DATA":
        reasons.append({"kind": "data",
                        "text": "Couldn't be checked: too little data in its latest filings"})
    elif status == "PASS":
        for w in entry.get("warnings") or []:
            reasons.append({"kind": "warning", "text": w[0].upper() + w[1:]})
        price, value = entry.get("price"), entry.get("value")
        if price and value and price > value * (1 + C.OVERVALUED_ABOVE):
            reasons.append({"kind": "overvalued",
                            "text": f"Price is {price / value - 1:.0%} above estimated value"})
    return reasons
