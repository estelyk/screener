"""Price charts for the company pages (1D, 1W, 1M, 1Y, 5Y, ALL), for both the
Small-Cap Growth page (smallcap_data/site) and the Value Screener page (site, with
--site site --no-rebuild). Display only: nothing here feeds the value model.

Writes one small file per company into smallcap_data/site/px/, which is published
next to the page. On GitHub each file is encrypted with the same key as the page,
and its name is a hash of that key plus the ticker, so nobody without the
passphrase can read the prices or tell which tickers are on the list.
(Locally, with no passphrase, files are plain px/<TICKER>.json.)

Runs in two places:
- the weekly small-cap job, right after the page is built;
- the daily job (weekday evenings), to refresh the charts. That step is allowed to
  fail: the previous charts stay and the main page is never affected.

Logs print counts only (the repo is public).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

SITE = Path("smallcap_data/site")
RANGES = ("1D", "1W", "1M", "1Y", "5Y", "ALL")
MAX_POINTS = 400


# ---------------------------------------------------------------- keys and files
def page_key(passphrase: str, label: str) -> bytes:
    """The page's AES key (same derivation as lock.py and the browser)."""
    from lock import derive_key, salt_for
    return derive_key(passphrase, salt_for(label))


def file_name(key: bytes | None, ticker: str) -> str:
    """Scrambled when locked, so the file list doesn't reveal tickers.
    The browser computes the same: SHA-256(raw key bytes + ticker), first 24 hex."""
    if key is None:
        return ticker.upper() + ".json"
    return hashlib.sha256(key + ticker.upper().encode()).hexdigest()[:24] + ".enc"


def seal(payload: dict, key: bytes | None) -> str:
    text = json.dumps(payload, separators=(",", ":"))
    if key is None:
        return text
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, text.encode(), None)
    return json.dumps({"iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()})


def unseal(blob: str, key: bytes | None) -> dict:
    if key is None:
        return json.loads(blob)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    b = json.loads(blob)
    return json.loads(AESGCM(key).decrypt(base64.b64decode(b["iv"]), base64.b64decode(b["ct"]), None))


# ---------------------------------------------------------------- building ranges
def _num(v: float) -> float:
    return round(float(v), 4 if v < 1 else 3 if v < 100 else 2)


def _pack(series, base: float | None) -> dict | None:
    """series: pandas Series of closes with a DatetimeIndex."""
    s = series.dropna()
    if s.empty:
        return None
    if len(s) > MAX_POINTS:                      # thin out evenly, always keep the last point
        step = len(s) / MAX_POINTS
        keep = sorted({int(i * step) for i in range(MAX_POINTS)} | {len(s) - 1})
        s = s.iloc[keep]
    ts = [int(t.timestamp()) for t in s.index]
    return {"t": ts, "c": [_num(v) for v in s.values], "base": _num(base) if base else None}


def _before(daily, when):
    prior = daily[daily.index < when]
    return float(prior.iloc[-1]) if len(prior) else None


def build_ranges(daily, intraday_1d=None, intraday_5d=None) -> dict:
    """daily: Series of daily closes (split-adjusted, like Robinhood), DatetimeIndex.
    intraday_1d / intraday_5d: Series of 5-minute / 30-minute closes, tz-aware.
    Each range starts from the close just before the range ('base'), so the change
    shown matches what Robinhood shows for that period."""
    import pandas as pd
    daily = daily.dropna()
    if daily.empty:
        return {}
    if daily.index.tz is not None:
        daily.index = daily.index.tz_localize(None)
    last = daily.index[-1]
    out: dict = {}

    for name, s in (("1D", intraday_1d), ("1W", intraday_5d)):
        if s is not None and len(s.dropna()):
            s = s.dropna()
            first_day = pd.Timestamp(s.index[0].date())
            out[name] = _pack(s, _before(daily, first_day))

    def daily_range(since):
        part = daily[daily.index > since]
        return _pack(part, _before(daily, part.index[0]) if len(part) else None)

    out["1M"] = daily_range(last - pd.DateOffset(months=1))
    out["1Y"] = daily_range(last - pd.DateOffset(years=1))
    five = daily[daily.index > last - pd.DateOffset(years=5)]
    weekly = five.resample("W-FRI").last().dropna()
    if len(weekly) and weekly.index[-1] != last:      # end exactly on the latest close
        weekly.index = list(weekly.index[:-1]) + [last]
    out["5Y"] = _pack(weekly, _before(daily, five.index[0]) if len(five) else None)
    if len(daily) <= MAX_POINTS:
        out["ALL"] = _pack(daily, None)
    else:
        rule = "W-FRI" if len(daily) / 5 <= MAX_POINTS else "ME"
        coarse = daily.resample(rule).last().dropna()
        if len(coarse) and coarse.index[-1] != last:
            coarse.index = list(coarse.index[:-1]) + [last]
        coarse = pd.concat([daily.iloc[:1], coarse])      # start on the very first close
        out["ALL"] = _pack(coarse, None)
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- downloading
def _closes(data, ticker, many):
    try:
        s = (data[ticker] if many else data)["Close"].dropna()
    except (KeyError, TypeError):
        return None
    return s if len(s) else None


def fetch(tickers: list[str], chunk: int = 25, _retry: bool = True) -> dict:
    """{ticker: ranges}. Best-effort: tickers Yahoo doesn't return are retried once in
    small batches, then skipped (write() keeps their previous chart)."""
    import logging
    import time
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    out = {}
    for i in range(0, len(tickers), chunk):
        batch = [t.replace(".", "-") for t in tickers[i:i + chunk]]
        many = len(batch) > 1
        got = {}
        for key, kw in (("daily", dict(period="max", interval="1d")),
                        ("d1", dict(period="1d", interval="5m")),
                        ("d5", dict(period="5d", interval="30m"))):
            try:
                got[key] = yf.download(batch, group_by="ticker", auto_adjust=False, progress=False,
                                       threads=4, prepost=False, **kw)
            except Exception:  # noqa: BLE001
                got[key] = None
            time.sleep(0.5)
        for orig, sym in zip(tickers[i:i + chunk], batch):
            daily = _closes(got["daily"], sym, many) if got["daily"] is not None else None
            if daily is None:
                continue
            d1 = _closes(got["d1"], sym, many) if got["d1"] is not None else None
            d5 = _closes(got["d5"], sym, many) if got["d5"] is not None else None
            r = build_ranges(daily, d1, d5)
            if r:
                out[orig] = r
    missed = [t for t in tickers if t not in out]
    if _retry and missed:
        time.sleep(5)                       # Yahoo often drops a batch when busy
        out.update(fetch(missed, chunk=5, _retry=False))
    return out


# ---------------------------------------------------------------- writing
def write(charts: dict, key: bytes | None, site: Path = SITE, keep: list[str] = ()) -> int:
    """New charts replace old ones. A ticker in `keep` (still on the page) that failed to
    download this time keeps its previous chart instead of losing it."""
    px = site / "px"
    tmp = site / "px.new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for t, ranges in charts.items():
        (tmp / file_name(key, t)).write_text(seal({"ticker": t, "ranges": ranges}, key))
    for t in keep:
        old = px / file_name(key, t)
        if t not in charts and old.exists():
            shutil.copy2(old, tmp / old.name)
    shutil.rmtree(px, ignore_errors=True)
    tmp.rename(px)
    return len(charts)


def page_data_from_site(site: Path = SITE, passphrase: str | None = None) -> dict:
    """Read the data back out of the (locked) small-cap page."""
    html = (site / "index.html").read_text()
    raw = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
    blob = json.loads(raw.replace("<\\/", "</"))
    if blob.get("locked"):
        from lock import decrypt
        blob = json.loads(decrypt(blob, passphrase))
    return blob


def tickers_from_page(site: Path = SITE, passphrase: str | None = None) -> list[str]:
    return [c["ticker"] for c in page_data_from_site(site, passphrase).get("companies", [])]


def rebuild_page(site: Path = SITE, passphrase: str | None = None, label: str = "local") -> None:
    """Re-render last week's data with the current page design, so design changes
    show up after the next daily run instead of waiting for Sunday."""
    import run_smallcap
    data = page_data_from_site(site, passphrase)
    (site / "index.html").write_text(run_smallcap.render(data, passphrase, label))


def run(tickers: list[str], passphrase: str | None, label: str, site: Path = SITE,
        fetcher=fetch) -> int:
    key = page_key(passphrase, label) if passphrase else None
    charts = fetcher(tickers)
    if len(charts) < max(1, len(tickers) // 2):
        print(f"Price charts: only {len(charts)} of {len(tickers)} downloaded; keeping the previous ones.")
        return 0
    n = write(charts, key, site, keep=tickers)
    missed = [t for t in tickers if t not in charts]
    if missed:
        print(f"Price charts: no new prices for {', '.join(missed[:20])}"
              f"{' …' if len(missed) > 20 else ''} (kept previous chart if there was one).")
    print(f"Price charts: wrote {n} of {len(tickers)}.")
    return n


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Write locked price-chart files next to a page.")
    ap.add_argument("--site", default=str(SITE), help="folder with index.html (default: small-cap page)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="don't re-render the page (use for the Value Screener page)")
    args = ap.parse_args(argv)
    site = Path(args.site)
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    label = os.environ.get("PAGE_SALT") or "local"
    if not (site / "index.html").exists():
        print("No page yet; skipping price charts.")
        return 0
    if not args.no_rebuild:
        try:
            rebuild_page(site, passphrase, label)
        except Exception as e:  # noqa: BLE001  keep last week's page as it was
            print(f"(Page design not refreshed: {type(e).__name__})")
    tickers = tickers_from_page(site, passphrase)
    run(tickers, passphrase, label, site)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
