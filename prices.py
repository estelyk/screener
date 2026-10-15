"""Latest share prices and recent stock splits. yfinance is an unofficial Yahoo
scraper that occasionally breaks, so everything goes through this file: swapping the
source later means changing only this file.

Splits matter: share counts come from the last annual report, so a split since then
(e.g. Booking's 25-for-1 in 2026) would make value per share look 25x too high.
"""
from __future__ import annotations

import pandas as pd


def _frame(data: pd.DataFrame, field: str, first_symbol: str) -> pd.DataFrame | None:
    if field not in data.columns.get_level_values(0):
        return None
    df = data[field]
    return df.to_frame(first_symbol) if isinstance(df, pd.Series) else df


def market_data(tickers: list[str]) -> tuple[dict[str, float], dict[str, list[tuple[str, float]]]]:
    """Return (latest price per ticker, splits per ticker as [(date, ratio)]) over ~2 years."""
    import yfinance as yf

    yf_symbol = {t: t.replace(".", "-") for t in tickers}  # BF.B -> BF-B on Yahoo
    data = yf.download(list(yf_symbol.values()), period="2y", auto_adjust=False,
                       actions=True, progress=False, threads=True)
    if data is None or data.empty:
        return {}, {}
    first = next(iter(yf_symbol.values()))
    close = _frame(data, "Close", first)
    split_df = _frame(data, "Stock Splits", first)

    prices, splits = {}, {}
    for t, sym in yf_symbol.items():
        if close is not None and sym in close.columns:
            s = close[sym].dropna()
            if len(s):
                prices[t] = float(s.iloc[-1])
        if split_df is not None and sym in split_df.columns:
            sp = split_df[sym].fillna(0)
            events = [(d.strftime("%Y-%m-%d"), float(r)) for d, r in sp[sp > 0].items()]
            if events:
                splits[t] = events
    return prices, splits


def latest_prices(tickers: list[str]) -> dict[str, float]:
    return market_data(tickers)[0]
