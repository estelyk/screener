"""Weekly small-cap ("moonshot") screen -> passphrase-locked page at site/smallcap/.

Runs smallcap_screener.py (stocks $1-$10, companies $20M-$500M, US filers), updates
the paper track record (smallcap_tracker.py), then
writes smallcap_data/site/index.html, encrypted with the same passphrase and salt as
the Value Screener page so one unlock opens both. The daily workflow copies that page
into the published site; this script never touches the value model or its data.

On GitHub Actions (see .github/workflows/smallcap.yml) logs show counts only.

Run locally (unlocked page, for a look):
    python run_smallcap.py
    open smallcap_data/site/index.html
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import smallcap_screener as S
import smallcap_tracker

BASE = Path("smallcap_data")
SITE = BASE / "site"
TEMPLATE = Path("smallcap_template.html")
MAIN_TEMPLATE = Path("site_template.html")

FIELDS = ["ticker", "name", "status", "score", "price", "market_cap", "revenue", "rev_growth_pct",
          "net_income", "ocf", "cash", "runway_months", "debt_to_equity", "share_change_pct",
          "insider_buys", "insider_sells", "tenk_url", "ten_k_date", "industry", "gross_margin",
          "offerings_12m"]
DESC_CACHE = BASE / "cache" / "descriptions.json"
DESC_BATCH = 120   # new descriptions fetched per run (PASS and CHECK first); kept 6 months


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _clean_deep(v):
    if isinstance(v, dict):
        return {k: _clean_deep(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_deep(x) for x in v]
    return _clean(v)


def descriptions(rows: list[dict], fetch=None) -> dict[str, str]:
    """What each company does, from Yahoo (best-effort, cached 6 months)."""
    import time
    import profiles
    fetch = fetch or profiles._fetch_one
    cache = json.loads(DESC_CACHE.read_text()) if DESC_CACHE.exists() else {}
    order = {"PASS": 0, "CHECK": 1, "WEAK": 2, "FAIL": 3}
    ranked = [r["ticker"] for r in sorted(rows, key=lambda r: order.get(r.get("status"), 9))]
    priority = [r["ticker"] for r in rows if r.get("status") in ("PASS", "CHECK")]
    now = time.time()
    todo = profiles.plan_fetch(cache, priority, ranked, now, batch=DESC_BATCH)
    for t in todo:
        try:
            text = fetch(t)
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            cache[t] = {"at": now, "text": profiles._short(text)}
        else:
            cache[t] = {**(cache.get(t) or {}), "tried": now}
    if todo:
        DESC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DESC_CACHE.write_text(json.dumps(cache))
    return {t: e["text"] for t, e in cache.items() if e.get("text")}


def page_data(rows: list[dict], generated: str, min_mcap: float, max_mcap: float | None,
              mode: str = "moonshot", descs: dict | None = None) -> dict:
    companies = []
    for r in rows:
        if r.get("status") == "SKIP":
            continue
        e = {k: _clean(r.get(k)) for k in FIELDS}
        e["flags"] = [f for f in (r.get("red_flags") or "").split("; ") if f]
        e["notes"] = [n for n in (r.get("notes") or "").split("; ") if n]
        e["annual"] = _clean_deep(r.get("annual") or [])
        e["shares_by_year"] = _clean_deep(r.get("shares_by_year") or [])
        e["explain"] = _clean_deep(S.explain_moonshot(r)) if mode == "moonshot" else None
        e["description"] = (descs or {}).get(r.get("ticker"))
        if r.get("cik"):
            e["all_filings"] = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                                f"&CIK={int(r['cik'])}&type=&dateb=&owner=include&count=40")
        companies.append(e)
    counts = {s: sum(c["status"] == s for c in companies) for s in ("PASS", "WEAK", "CHECK", "FAIL")}
    def short(v):
        return f"${v / 1e9:g}B" if v >= 1e9 else f"${v / 1e6:g}M"
    scope = f"stocks $1–$10, companies {short(min_mcap)}–{short(max_mcap) if max_mcap else 'any size'}"
    rules = re.sub(r"<[^>]+>", "", S.RULES[mode])
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        rules = rules.replace(a, b)
    rules += f" PASS also requires revenue growth over {S.MOON_MIN_GROWTH_PCT:.0f}%." if mode == "moonshot" else ""
    return {"generated": generated, "scope": scope, "counts": counts, "rules": rules,
            "companies": companies}


def main_styles() -> str:
    """The Value Screener page's CSS, so the two pages look the same."""
    m = re.search(r"<style>(.*?)</style>", MAIN_TEMPLATE.read_text(), re.S)
    return m.group(1) if m else ""


def render(data: dict, passphrase: str | None = None, label: str = "local") -> str:
    payload = json.dumps(data, separators=(",", ":"))
    if passphrase:
        from lock import encrypt
        payload = json.dumps(encrypt(payload, passphrase, label), separators=(",", ":"))
    html = TEMPLATE.read_text().replace("/*__STYLE__*/", main_styles())
    html = html.replace("/*__PRICE_CHART__*/", (Path(__file__).resolve().parent / "price_chart.js").read_text())
    return html.replace("/*__DATA__*/", payload.replace("</", "<\\/"))


def write_page(data: dict, site: Path = SITE) -> Path:
    site.mkdir(parents=True, exist_ok=True)
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    label = os.environ.get("PAGE_SALT") or "local"
    out = site / "index.html"
    out.write_text(render(data, passphrase, label))
    return out


def main() -> int:
    min_mcap = float(os.environ.get("SMALLCAP_MIN_MCAP", "20e6"))
    max_mcap = float(os.environ.get("SMALLCAP_MAX_MCAP", "500e6"))
    count = os.environ.get("SMALLCAP_CANDIDATES", "300")
    rows = S.main(["--mode", "moonshot", "--min-mcap", str(min_mcap), "--max-mcap", str(max_mcap),
                   "--max-candidates", count,
                   "--out", str(BASE / "output"), "--cache", str(BASE / "cache")])
    if len(rows) < 20:
        # Something went wrong (e.g. Yahoo or SEC blocked us): keep last week's page.
        print(f"Only {len(rows)} companies analyzed; not replacing the page.")
        return 1
    try:
        descs = descriptions(rows)
    except Exception as e:  # noqa: BLE001  the page still builds without them
        print(f"(Descriptions not updated: {type(e).__name__})")
        descs = {}
    data = page_data(rows, datetime.now().strftime("%b %-d, %Y"), min_mcap, max_mcap, descs=descs)
    data["tracking"] = smallcap_tracker.run(rows)
    path = write_page(data)
    print(f"Wrote {path} ({len(data['companies'])} companies)")
    try:  # price charts are best-effort; the daily job refreshes them on weekdays
        import smallcap_prices
        smallcap_prices.run([c["ticker"] for c in data["companies"]],
                            os.environ.get("PAGE_PASSPHRASE") or None,
                            os.environ.get("PAGE_SALT") or "local", SITE)
    except Exception as e:  # noqa: BLE001
        print(f"(Price charts not built: {type(e).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
