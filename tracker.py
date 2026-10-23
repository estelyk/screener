"""Phase 5: paper-track every buy signal against the S&P 500.

The day a company becomes a buy signal, it is recorded automatically with everything
the model said that day (price, value, buy price, warnings, VERIFY flags, terminal
share, model version). Nothing is picked by hand, so the record can't be flattered.

Each entry is then "held on paper" from that day, and its total return (price change
plus dividends, from Yahoo's adjusted prices) is compared with putting the same money
into SPY (the S&P 500) and VTV (a value-stock fund) on the same day. VTV shows whether
any edge comes from this model or just from buying cheap-looking stocks in general.

Where the record lives (it holds tickers, so it is never public in plain text):
- On GitHub: encrypted with your passphrase, published next to the page as
  ledger.enc and also kept in the run's saved data. Each run reads whichever copy is
  newer. If neither can be read for a reason other than "it doesn't exist yet", the
  run stops so a good record is never replaced with an empty one.
- On your Mac: plain state/ledger.json (state/ is never committed).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config as C

STATE = Path("state")
SITE = Path("site")
OUT = Path("output")
BENCHMARKS = ("SPY", "VTV")
REACTIVATE_DAYS = 30   # a signal that returns within this many days continues its old entry


def today_et() -> str:
    """Today's date in New York (runs happen after the US close)."""
    et = timezone(timedelta(hours=-5))   # good enough: runs are at 5-6pm ET
    return datetime.now(et).strftime("%Y-%m-%d")


def empty_ledger(day: str) -> dict:
    return {"version": 1, "started": day, "updated": None, "entries": []}


# ---------------------------------------------------------------- recording
def record(ledger: dict, valuations: list[dict], day: str, model: str, code: str = "") -> int:
    """Add today's new signals; mark entries whose signal has gone. Returns # added."""
    signals = {v["ticker"]: v for v in valuations if v.get("signal")}
    latest: dict[str, dict] = {}
    for e in ledger["entries"]:
        latest[e["ticker"]] = e          # entries are in date order, so last one wins
    added = 0
    for t, v in signals.items():
        e = latest.get(t)
        if e and e["active"]:
            e["last_seen"] = day
            continue
        if e and e.get("ended") and (date.fromisoformat(day) - date.fromisoformat(e["ended"])).days <= REACTIVATE_DAYS:
            e["active"], e["ended"], e["last_seen"] = True, None, day
            continue
        ledger["entries"].append({
            "ticker": t, "name": v.get("name"), "sector": v.get("sector"),
            "start": day, "last_seen": day, "active": True, "ended": None,
            "price": v.get("price"), "value": v.get("value_per_share"),
            "value_low": v.get("value_low"), "value_high": v.get("value_high"),
            "value_no_recovery": v.get("value_no_recovery"),
            "fscore": (v.get("fscore") or {}).get("score"), "zscore": v.get("zscore"), "cash_drop_main": (v.get("cash_drop") or {}).get("main"),
            "buy_below": v.get("buy_below"), "discount": v.get("discount"),
            "terminal_share": v.get("terminal_share"), "growth": v.get("growth"),
            "warnings": v.get("warnings") or [], "verify": bool(v.get("verify")),
            "suspicious": bool(v.get("suspicious")), "model": model, "code": code,
            "conservative": bool(v.get("conservative")),
            "ret": None, "spy": None, "vtv": None, "as_of": None, "maxdd": None,
        })
        added += 1
    for t, e in latest.items():
        if e["active"] and t not in signals:
            e["active"], e["ended"] = False, day
    ledger["updated"] = day
    return added


# ---------------------------------------------------------------- returns
def _ratio(series, start: str):
    """Total return from the close on (or just before) `start` to the latest close."""
    s = series.dropna()
    if s.empty:
        return None, None
    before = s[s.index <= start]
    base = float(before.iloc[-1]) if len(before) else None
    if not base:
        return None, None
    return float(s.iloc[-1]) / base - 1, s.index[-1].strftime("%Y-%m-%d")


def _maxdd(series, start: str):
    """Worst fall from a high since the signal day (e.g. -0.18 = fell 18% at worst)."""
    s = series.dropna()
    before = s[s.index <= start]
    if not len(before):
        return None
    path = s[s.index >= before.index[-1]]
    return float((path / path.cummax() - 1).min())


SHADOW_MODELS = {
    "min": "Lower of normal & last yr",
    "blend": "Avg of normal & last yr",
    "recent3": "Avg of last 3 yrs",
}


def all_entries(ledger: dict) -> list[dict]:
    return ledger["entries"] + [e for es in (ledger.get("shadow") or {}).values() for e in es]


def record_shadow(ledger: dict, valuations: list[dict], day: str, model: str, code: str = "") -> None:
    """Record what each shadow model would have flagged, in its own list."""
    shadow = ledger.setdefault("shadow", {})
    for k in SHADOW_MODELS:
        sub = {"entries": shadow.setdefault(k, [])}
        rows = [dict(v, signal=bool((v.get("shadow_signals") or {}).get(k)),
                     value_per_share=(v.get("shadow") or {}).get(k)) for v in valuations]
        record(sub, rows, day, f"{model} shadow:{k}", code)


def update_returns(ledger: dict, history=None) -> None:
    """history: {symbol: pandas Series of adjusted closes indexed by date}. Fetched
    from Yahoo when not given. Entries with no new data keep their last return."""
    entries = all_entries(ledger)
    if not entries:
        return
    if history is None:
        history = fetch_history(sorted({e["ticker"] for e in entries}),
                                min(e["start"] for e in entries))
    for e in entries:
        sym = e["ticker"].replace(".", "-")
        r, as_of = _ratio(history[sym], e["start"]) if sym in history else (None, None)
        if r is None:
            continue
        e["ret"], e["as_of"] = r, as_of
        e["maxdd"] = _maxdd(history[sym], e["start"])
        for b in BENCHMARKS:
            if b in history:
                br, _ = _ratio(history[b][history[b].index <= as_of], e["start"])
                if br is not None:
                    e[b.lower()] = br


def fetch_history(tickers: list[str], since: str) -> dict:
    import yfinance as yf
    syms = [t.replace(".", "-") for t in tickers] + list(BENCHMARKS)
    start = (date.fromisoformat(since) - timedelta(days=10)).isoformat()
    data = yf.download(syms, start=start, auto_adjust=True, progress=False, threads=True)
    if data is None or data.empty:
        return {}
    close = data["Close"]
    if not hasattr(close, "columns"):
        close = close.to_frame(syms[0])
    return {s: close[s] for s in close.columns}


def summary(ledger: dict, holdings: list[str] | None = None) -> dict:
    """Equal-weight averages: each signal compared with SPY/VTV over its own dates."""
    def group(es):
        done = [e for e in es if e.get("ret") is not None]
        if not done:
            return {"n": len(es), "measured": 0}
        avg = lambda k: sum(e[k] for e in done if e.get(k) is not None) / max(
            1, sum(1 for e in done if e.get(k) is not None))
        return {"n": len(es), "measured": len(done), "ret": avg("ret"), "spy": avg("spy"),
                "vtv": avg("vtv"),
                "beat_spy": sum(1 for e in done if e.get("spy") is not None and e["ret"] > e["spy"]),
                "maxdd": avg("maxdd") if any(e.get("maxdd") is not None for e in done) else None,
                "days": sum((date.fromisoformat(e["as_of"]) - date.fromisoformat(e["start"])).days
                            for e in done) / len(done)}
    held = set(holdings or [])
    es = ledger["entries"]
    shadow = ledger.get("shadow") or {}
    return {"started": ledger["started"], "updated": ledger["updated"],
            "all": group(es), "held": group([e for e in es if e["ticker"] in held]),
            "conservative": group([e for e in es if e.get("conservative")]),
            "warned": group([e for e in es if e.get("warnings")]),
            "clean": group([e for e in es if not e.get("warnings")]),
            "shadow": {k: dict(group(shadow.get(k, [])), label=SHADOW_MODELS[k]) for k in SHADOW_MODELS},
            "model": C.MODEL_VERSION}


# ---------------------------------------------------------------- storage
def _pages_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/ledger.enc"


def _fetch_published(url: str):
    """(blob or None, ok). ok=False means a real failure, not just 'not there yet'."""
    try:
        with urllib.request.urlopen(url + f"?t={datetime.now().timestamp():.0f}", timeout=30) as r:
            return json.loads(r.read().decode()), True
    except urllib.error.HTTPError as e:
        return None, e.code == 404
    except Exception:
        return None, False


def load(passphrase: str | None, url: str | None = None, fetch=_fetch_published) -> dict:
    day = today_et()
    if not passphrase:                                   # local, unencrypted
        p = STATE / "ledger.json"
        return json.loads(p.read_text()) if p.exists() else empty_ledger(day)

    from lock import decrypt
    copies, problems = [], []
    cached = STATE / "ledger.enc"
    if cached.exists():
        try:
            copies.append(json.loads(decrypt(json.loads(cached.read_text()), passphrase)))
        except Exception:
            problems.append("saved copy unreadable")
    url = url or _pages_url()
    if url:
        blob, ok = fetch(url)
        if blob:
            try:
                copies.append(json.loads(decrypt(blob, passphrase)))
            except Exception:
                problems.append("published copy unreadable")
        elif not ok:
            problems.append("published copy unreachable")
    if copies:
        return max(copies, key=lambda l: (l.get("updated") or "", len(l["entries"])))
    if problems:
        raise SystemExit("Could not read the paper-tracking record (" + ", ".join(problems) +
                         "). Stopping so it isn't replaced with an empty one.")
    return empty_ledger(day)


def save(ledger: dict, passphrase: str | None) -> None:
    STATE.mkdir(exist_ok=True)
    text = json.dumps(ledger, separators=(",", ":"))
    if not passphrase:
        (STATE / "ledger.json").write_text(text)
        return
    from lock import encrypt
    blob = json.dumps(encrypt(text, passphrase, os.environ.get("PAGE_SALT") or "local"))
    (STATE / "ledger.enc").write_text(blob)
    SITE.mkdir(exist_ok=True)
    (SITE / "ledger.enc").write_text(blob)


def run(valuations: list[dict], holdings: list[str] | None = None, history=None) -> dict:
    """Load, record today's signals, update returns, save. Returns data for the page."""
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    ledger = load(passphrase)
    added = record(ledger, valuations, today_et(), C.MODEL_VERSION,
                   (os.environ.get("GITHUB_SHA") or "")[:7])
    record_shadow(ledger, valuations, today_et(), C.MODEL_VERSION)
    try:
        update_returns(ledger, history)
    except Exception as e:                                 # prices are best-effort here
        print(f"(Tracking returns not updated today: {type(e).__name__})")
    save(ledger, passphrase)
    print(f"Paper tracking: {len(ledger['entries'])} signals on record, {added} new today.")
    return {"summary": summary(ledger, holdings), "entries": ledger["entries"]}
