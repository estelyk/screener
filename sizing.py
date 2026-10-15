"""Suggest a dollar amount for each buy signal.

Your numbers live in my_portfolio.json, which is never committed or published
(it's in .gitignore). Copy my_portfolio.example.json to start.

Rules:
- No position larger than max_position_pct of the cash reserve.
- No more holdings in total than max_holdings.
- Signals are funded in order of biggest discount first, until cash or slots run out.
- Companies you already hold are skipped.
"""
from __future__ import annotations

import json
from pathlib import Path

PORTFOLIO_FILE = Path("my_portfolio.json")


def load_portfolio() -> dict | None:
    if not PORTFOLIO_FILE.exists():
        return None
    p = json.loads(PORTFOLIO_FILE.read_text())
    p.setdefault("holdings", [])
    return p


def suggest(signals: list[dict], portfolio: dict) -> list[dict]:
    """signals: dicts with 'ticker' and 'discount'. Returns them with 'suggested_amount'."""
    cash = float(portfolio["cash_reserve"])
    cap = cash * float(portfolio["max_position_pct"])
    held = {t.upper() for t in portfolio.get("holdings", [])}
    slots = int(portfolio["max_holdings"]) - len(held)

    out = []
    for s in sorted(signals, key=lambda s: -(s.get("discount") or 0)):
        s = dict(s)
        if s["ticker"].upper() in held:
            s["suggested_amount"], s["sizing_note"] = 0.0, "already held"
        elif slots <= 0:
            s["suggested_amount"], s["sizing_note"] = 0.0, "max holdings reached"
        elif cash <= 0:
            s["suggested_amount"], s["sizing_note"] = 0.0, "cash reserve used up"
        else:
            amt = min(cap, cash)
            s["suggested_amount"], s["sizing_note"] = round(amt, 2), ""
            cash -= amt
            slots -= 1
        out.append(s)
    return out
