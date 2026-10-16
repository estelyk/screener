"""What each company does, plus links to its filings, for the briefs.

- Filing links (latest 10-K, latest earnings release) come from SEC for every
  screened company (cached a week).
- The plain-English description comes from Yahoo. Priority companies (buy signals,
  the watchlist and your HOLDINGS) are always fetched; every other company is filled
  in gradually, DESC_BATCH per run, so the whole S&P 500 is covered within a few
  weeks without slowing any single run. Descriptions are kept for 6 months.
- Every fetch is best-effort: if anything fails, the page still builds without it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd

DESC_CACHE = Path("data/cache/descriptions.json")
DESC_MAX_AGE = 180 * 86400     # businesses rarely change what they do
RETRY_AFTER = 7 * 86400        # a failed fetch is retried a week later
DESC_BATCH = 60                # non-priority descriptions fetched per run
UNIVERSE = Path("data/sp500.csv")


def _short(text: str, sentences: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return " ".join(parts[:sentences])


def _needs_fetch(entry: dict | None, now: float) -> bool:
    if not entry:
        return True
    if entry.get("text"):
        return now - entry.get("at", 0) > DESC_MAX_AGE
    return now - entry.get("tried", 0) > RETRY_AFTER


def plan_fetch(cache: dict, priority: list[str], others: list[str], now: float,
               batch: int | None = None) -> list[str]:
    """Which tickers to fetch this run: all stale priority ones, then up to `batch`
    others, oldest-missing first."""
    batch = DESC_BATCH if batch is None else batch
    seen, todo = set(), []
    for t in priority:
        if t not in seen and _needs_fetch(cache.get(t), now):
            todo.append(t)
        seen.add(t)
    rest = [t for t in others if t not in seen and _needs_fetch(cache.get(t), now)]
    rest.sort(key=lambda t: (cache.get(t) or {}).get("tried", 0))
    return todo + rest[:batch]


def _fetch_one(ticker: str) -> str:
    import yfinance as yf
    try:
        return yf.Ticker(ticker.replace(".", "-")).info.get("longBusinessSummary") or ""
    except Exception:
        return ""


def descriptions(priority: list[str], others: list[str] = (), fetch=_fetch_one) -> dict[str, str]:
    cache = json.loads(DESC_CACHE.read_text()) if DESC_CACHE.exists() else {}
    now = time.time()
    todo = plan_fetch(cache, list(priority), list(others), now)
    for t in todo:
        text = fetch(t)
        if text:
            cache[t] = {"at": now, "text": _short(text)}
        else:  # keep any older text; just remember we tried
            cache[t] = {**(cache.get(t) or {}), "tried": now}
    if todo:
        DESC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DESC_CACHE.write_text(json.dumps(cache))
    return {t: e["text"] for t, e in cache.items() if e.get("text")}


def sub_industries() -> dict[str, str]:
    if not UNIVERSE.exists():
        return {}
    df = pd.read_csv(UNIVERSE)
    return dict(zip(df["ticker"], df["sub_industry"]))


def build_profiles(client, companies: list[dict], priority: list[str]) -> dict[str, dict]:
    """{ticker: {description, industry, ten_k, earnings, ...}} for every company given."""
    desc = descriptions(priority, [c["ticker"] for c in companies])
    subs = sub_industries()
    out = {}
    for c in companies:
        t = c["ticker"]
        try:
            links = client.filings(int(c["cik"]))
        except Exception:
            links = {}
        out[t] = {"industry": subs.get(t) or links.get("sic_description"),
                  "description": desc.get(t), **links}
    return out
