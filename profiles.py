"""What each company does, plus links to its filings, for the briefs.

- Filing links (latest 10-K, latest earnings release) come from SEC for every
  passing company.
- The plain-English description comes from Yahoo, fetched only for buy signals and
  the watchlist (the companies whose briefs you'll actually read), cached 30 days.
- Every fetch is best-effort: if anything fails, the page still builds without it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd

DESC_CACHE = Path("data/cache/descriptions.json")
DESC_MAX_AGE = 30 * 86400
UNIVERSE = Path("data/sp500.csv")


def _short(text: str, sentences: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return " ".join(parts[:sentences])


def descriptions(tickers: list[str]) -> dict[str, str]:
    cache = json.loads(DESC_CACHE.read_text()) if DESC_CACHE.exists() else {}
    now = time.time()
    stale = [t for t in tickers if now - cache.get(t, {}).get("at", 0) > DESC_MAX_AGE]
    if stale:
        try:
            import yfinance as yf
            for t in stale:
                try:
                    text = yf.Ticker(t.replace(".", "-")).info.get("longBusinessSummary") or ""
                except Exception:
                    text = ""
                if text:
                    cache[t] = {"at": now, "text": _short(text)}
        except Exception:
            pass
        DESC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DESC_CACHE.write_text(json.dumps(cache))
    return {t: cache[t]["text"] for t in tickers if t in cache}


def sub_industries() -> dict[str, str]:
    if not UNIVERSE.exists():
        return {}
    df = pd.read_csv(UNIVERSE)
    return dict(zip(df["ticker"], df["sub_industry"]))


def build_profiles(client, companies: list[dict], shortlist: list[str]) -> dict[str, dict]:
    """{ticker: {description, industry, ten_k, earnings, ...}}"""
    desc = descriptions(shortlist)
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
