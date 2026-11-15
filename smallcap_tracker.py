"""Paper-track the weekly small-cap screen against the Russell 2000 (IWM).

Every stock that is PASS the first time it appears is "bought" on paper at that
week's price and held from then on, even after it leaves the PASS list. That matches
the plan: own many small bets and let the winners run. FAIL stocks are recorded the
same way as a control group: if PASS beats IWM and FAIL lags it, the screen is adding
something; if both look alike, it isn't.

Returns are total returns (Yahoo adjusted prices) from the close on the start day,
compared with IWM over exactly the same dates. A stock that is delisted keeps its
last measured return (a bankruptcy may therefore look less bad than -100%).

Where the record lives (it holds tickers, so never public in plain text):
- On GitHub: encrypted with your passphrase, saved in the weekly job's data and
  published next to the Small caps page as smallcap/ledger.enc. Each run reads the
  newer copy; if a copy exists but can't be read, the run stops so a good record is
  never replaced with an empty one.
- On your Mac: plain smallcap_data/ledger.json.

Separate from tracker.py (the value model's record), which this never touches.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path("smallcap_data")
CACHE = BASE / "cache"
SITE = BASE / "site"
BENCHMARK = "IWM"
GROUPS = ("PASS", "FAIL")


def today_et() -> str:
    return datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")


def empty_ledger(day: str) -> dict:
    return {"version": 1, "started": day, "updated": None, "entries": []}


# ---------------------------------------------------------------- recording
def record(ledger: dict, rows: list[dict], day: str) -> dict:
    """Add stocks seen as PASS or FAIL for the first time. Returns {group: # added}."""
    known = {(e["ticker"], e["group"]) for e in ledger["entries"]}
    added = {g: 0 for g in GROUPS}
    for r in rows:
        g, t = r.get("status"), r.get("ticker")
        if g not in GROUPS or not t or not r.get("price") or (t, g) in known:
            continue
        ledger["entries"].append({
            "ticker": t, "name": r.get("name"), "group": g, "start": day,
            "price": r.get("price"), "score": r.get("score"),
            "market_cap": r.get("market_cap"), "rev_growth_pct": r.get("rev_growth_pct"),
            "flags": r.get("red_flags") or "", "notes": r.get("notes") or "",
            "ret": None, "bench": None, "as_of": None, "maxdd": None,
        })
        known.add((t, g))
        added[g] += 1
    ledger["updated"] = day
    return added


# ---------------------------------------------------------------- returns
def _ratio(series, start: str):
    s = series.dropna()
    if s.empty:
        return None, None
    before = s[s.index <= start]
    base = float(before.iloc[-1]) if len(before) else None
    if not base:
        return None, None
    return float(s.iloc[-1]) / base - 1, s.index[-1].strftime("%Y-%m-%d")


def _maxdd(series, start: str):
    s = series.dropna()
    before = s[s.index <= start]
    if not len(before):
        return None
    path = s[s.index >= before.index[-1]]
    return float((path / path.cummax() - 1).min())


def update_returns(ledger: dict, history: dict | None = None) -> int:
    """history: {symbol: pandas Series of adjusted closes}. Returns # entries measured."""
    es = ledger["entries"]
    if not es:
        return 0
    if history is None:
        history = fetch_history(sorted({e["ticker"] for e in es}), min(e["start"] for e in es))
    measured = 0
    bench = history.get(BENCHMARK)
    for e in es:
        sym = e["ticker"].replace(".", "-")
        if sym not in history:
            continue                      # e.g. delisted: keep the last measured return
        r, as_of = _ratio(history[sym], e["start"])
        if r is None:
            continue
        e["ret"], e["as_of"], e["maxdd"] = r, as_of, _maxdd(history[sym], e["start"])
        if bench is not None:
            br, _ = _ratio(bench[bench.index <= as_of], e["start"])
            if br is not None:
                e["bench"] = br
        measured += 1
    return measured


def fetch_history(tickers: list[str], since: str, chunk: int = 50) -> dict:
    """Adjusted closes since `since`, in small batches (big ones fail on GitHub)."""
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    start = (date.fromisoformat(since) - timedelta(days=10)).isoformat()
    syms = [t.replace(".", "-") for t in tickers] + [BENCHMARK]
    out: dict = {}
    for i in range(0, len(syms), chunk):
        batch = syms[i:i + chunk]
        try:
            data = yf.download(batch, start=start, auto_adjust=True, progress=False, threads=4)
        except Exception:  # noqa: BLE001  best-effort; missing ones keep old returns
            continue
        if data is None or data.empty:
            continue
        close = data["Close"]
        if not hasattr(close, "columns"):
            close = close.to_frame(batch[0])
        for s in close.columns:
            if close[s].notna().any():
                out[s] = close[s]
    return out


# ---------------------------------------------------------------- summary for the page
def summary(ledger: dict, top: int = 8) -> dict:
    def group(g):
        es = [e for e in ledger["entries"] if e["group"] == g]
        done = [e for e in es if e.get("ret") is not None]
        res = {"n": len(es), "measured": len(done)}
        if not done:
            return res
        with_b = [e for e in done if e.get("bench") is not None]
        rets = sorted(e["ret"] for e in done)
        res.update({
            "ret": sum(rets) / len(rets),
            "median": rets[len(rets) // 2] if len(rets) % 2 else (rets[len(rets) // 2 - 1] + rets[len(rets) // 2]) / 2,
            "bench": (sum(e["bench"] for e in with_b) / len(with_b)) if with_b else None,
            "beat": sum(1 for e in with_b if e["ret"] > e["bench"]),
            "compared": len(with_b),
            "best": max(rets), "worst": min(rets),
            "up_2x": sum(1 for r in rets if r >= 1.0),
            "down_half": sum(1 for r in rets if r <= -0.5),
            "days": sum((date.fromisoformat(e["as_of"]) - date.fromisoformat(e["start"])).days
                        for e in done) / len(done),
        })
        return res
    picks = sorted([e for e in ledger["entries"] if e["group"] == "PASS"],
                   key=lambda e: (e.get("ret") is None, -(e.get("ret") or 0)))
    rows = [{k: e.get(k) for k in ("ticker", "name", "start", "price", "ret", "bench", "maxdd", "score")}
            for e in picks]
    return {"started": ledger["started"], "updated": ledger["updated"],
            "pass": group("PASS"), "fail": group("FAIL"), "picks": rows}


# ---------------------------------------------------------------- storage
def _pages_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/smallcap/ledger.enc"


def _fetch_published(url: str):
    """(blob or None, ok). ok=False means a real failure, not just 'not there yet'."""
    try:
        with urllib.request.urlopen(url + f"?t={datetime.now().timestamp():.0f}", timeout=30) as r:
            return json.loads(r.read().decode()), True
    except urllib.error.HTTPError as e:
        return None, e.code == 404
    except Exception:  # noqa: BLE001
        return None, False


def load(passphrase: str | None, url: str | None = None, fetch=_fetch_published) -> dict:
    day = today_et()
    if not passphrase:
        p = BASE / "ledger.json"
        return json.loads(p.read_text()) if p.exists() else empty_ledger(day)
    from lock import decrypt
    copies, problems = [], []
    cached = CACHE / "ledger.enc"
    if cached.exists():
        try:
            copies.append(json.loads(decrypt(json.loads(cached.read_text()), passphrase)))
        except Exception:  # noqa: BLE001
            problems.append("saved copy unreadable")
    url = url or _pages_url()
    if url:
        blob, ok = fetch(url)
        if blob:
            try:
                copies.append(json.loads(decrypt(blob, passphrase)))
            except Exception:  # noqa: BLE001
                problems.append("published copy unreadable")
        elif not ok:
            problems.append("published copy unreachable")
    if copies:
        return max(copies, key=lambda l: (l.get("updated") or "", len(l["entries"])))
    if problems:
        raise SystemExit("Could not read the small-cap track record (" + ", ".join(problems) +
                         "). Stopping so it isn't replaced with an empty one.")
    return empty_ledger(day)


def save(ledger: dict, passphrase: str | None) -> None:
    text = json.dumps(ledger, separators=(",", ":"))
    if not passphrase:
        BASE.mkdir(parents=True, exist_ok=True)
        (BASE / "ledger.json").write_text(text)
        return
    from lock import encrypt
    blob = json.dumps(encrypt(text, passphrase, os.environ.get("PAGE_SALT") or "local"))
    for d in (CACHE, SITE):
        d.mkdir(parents=True, exist_ok=True)
        (d / "ledger.enc").write_text(blob)


def run(rows: list[dict], history: dict | None = None) -> dict:
    """Load, record this week's PASS/FAIL stocks, update returns, save. Returns page data."""
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    ledger = load(passphrase)
    added = record(ledger, rows, today_et())
    try:
        update_returns(ledger, history)
    except Exception as e:  # noqa: BLE001  prices are best-effort here
        print(f"(Track record returns not updated this week: {type(e).__name__})")
    save(ledger, passphrase)
    n = {g: sum(e["group"] == g for e in ledger["entries"]) for g in GROUPS}
    print(f"Track record: {n['PASS']} PASS picks and {n['FAIL']} FAIL controls on record "
          f"({added['PASS']} and {added['FAIL']} new this week).")
    return summary(ledger)
