"""The list of companies to screen: current S&P 500 constituents with SEC CIK numbers.

Source: Wikipedia's constituents table (includes GICS sector and CIK).
Cached to data/sp500.csv; pass refresh=True to re-download.

Note: this is *today's* index. That's fine for forward tracking (Phase 5), but any
historical backtest using it would suffer survivorship bias.
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE = Path("data/sp500.csv")


def _fetch_from_wikipedia(user_agent: str) -> pd.DataFrame:
    html = requests.get(WIKI_URL, headers={"User-Agent": user_agent}, timeout=30).text
    df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    df = df.rename(columns={
        "Symbol": "ticker", "Security": "name", "GICS Sector": "sector",
        "GICS Sub-Industry": "sub_industry", "CIK": "cik",
    })
    if "cik" not in df.columns:
        df["cik"] = None
    return df[["ticker", "name", "sector", "sub_industry", "cik"]]


def _fill_missing_ciks(df: pd.DataFrame, user_agent: str) -> pd.DataFrame:
    if df["cik"].notna().all():
        return df
    data = requests.get(SEC_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30).json()
    lookup = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
    df["cik"] = [
        cik if pd.notna(cik) else lookup.get(str(t).replace(".", "-").upper())
        for t, cik in zip(df["ticker"], df["cik"])
    ]
    return df


MAX_AGE_DAYS = 30  # the index changes a few times a year; refresh monthly


def load_universe(user_agent: str, refresh: bool = False) -> pd.DataFrame:
    import time
    fresh = CACHE.exists() and time.time() - CACHE.stat().st_mtime < MAX_AGE_DAYS * 86400
    if fresh and not refresh:
        df = pd.read_csv(CACHE)
    else:
        df = _fill_missing_ciks(_fetch_from_wikipedia(user_agent), user_agent)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(CACHE, index=False)
    df = df.dropna(subset=["cik"]).copy()
    df["cik"] = df["cik"].astype(int)
    # Some companies have two share classes (GOOGL/GOOG, FOXA/FOX) but one set of filings.
    df = df.drop_duplicates(subset="cik", keep="first")
    return df.reset_index(drop=True)
