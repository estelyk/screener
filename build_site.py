"""Phase 3: write the mobile web page (site/index.html) from the Phase 2 results.

The page is one self-contained file plus an icon and a manifest (so it can be
added to the phone's home screen). It contains only public data: financials,
prices and estimates. Your position-sizing numbers are entered on the phone and
stay in that browser; they are never written here.

Run automatically at the end of run_phase2.py, or on its own:
    python build_site.py
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import config as C

OUT = Path("output")
SITE = Path("site")
TEMPLATE = Path("site_template.html")
ASSETS = Path("assets")

MANIFEST = {
    "name": "Value Screener", "short_name": "Screener", "start_url": ".", "display": "standalone",
    "background_color": "#fcfcfb", "theme_color": "#fcfcfb",
    "icons": [{"src": "icon-180.png", "sizes": "180x180", "type": "image/png"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
}


def _returns_basis(checks: list[dict]) -> str:
    for k in checks:
        if k.get("name") == "returns" and str(k.get("detail", "")).startswith("ROIC"):
            return "ROIC"
    return "ROE"


def company_entry(v: dict, co: dict, profile: dict) -> dict:
    basis = _returns_basis(co.get("checks", []))
    annual = [{
        "y": int(r["fiscal_year_end"][:4]),
        "revenue": r.get("revenue"), "net_income": r.get("net_income"),
        "owner_fcf": r.get("owner_fcf"), "debt": r.get("long_term_debt"),
        "ret": r.get("roe") if basis == "ROE" else r.get("roic"),
    } for r in co.get("annual", []) if r.get("fiscal_year_end")]
    links = {k: profile.get(k) for k in ("ten_k", "ten_k_date", "earnings", "earnings_date", "all_filings")}
    if not links["all_filings"] and co.get("cik"):
        links["all_filings"] = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                                f"&CIK={co['cik']}&type=10-K&dateb=&owner=include&count=40")
    return {
        "ticker": v["ticker"], "name": v["name"], "sector": v["sector"],
        "status": co.get("status", "PASS"), "screen_notes": co.get("notes", []),
        "industry": profile.get("industry"), "description": profile.get("description"),
        "price": v.get("price"), "value": v.get("value_per_share"), "buy_below": v.get("buy_below"),
        "value_low": v.get("value_low"), "value_high": v.get("value_high"),
        "terminal_share": v.get("terminal_share"),
        "discount": v.get("discount"), "to_buy_price": v.get("to_buy_price"),
        "signal": bool(v.get("signal")), "suspicious": bool(v.get("suspicious")),
        "warnings": v.get("warnings") or [], "verify": v.get("verify") or "",
        "value_note": v.get("value_note") or "", "growth": v.get("growth"),
        "normalized_fcf": v.get("normalized_fcf"), "net_debt": v.get("net_debt"),
        "shares": v.get("shares"), "checks": co.get("checks", []),
        "returns_basis": basis, "annual": annual, "links": links,
    }


def page_data(valuations: list[dict], companies: dict[str, dict],
              profiles: dict[str, dict], generated: str,
              excluded: list[dict] | None = None) -> dict:
    """Passing companies first (already ranked: signals, then closest to buy), then every
    other screened company alphabetically, then companies excluded by sector, so that
    search covers the whole S&P 500."""
    entries = [company_entry(v, companies.get(v["ticker"], {}), profiles.get(v["ticker"], {}))
               for v in valuations]
    valued = {v["ticker"] for v in valuations}
    for t in sorted(companies):
        co = companies[t]
        if t in valued or co.get("status") == "PASS":
            continue
        base = {"ticker": t, "name": co.get("name", t), "sector": co.get("sector", "")}
        entries.append(company_entry(base, co, profiles.get(t, {})))
    for e in sorted(excluded or [], key=lambda e: e["ticker"]):
        if e["ticker"] not in companies:
            entries.append({"ticker": e["ticker"], "name": e["name"], "sector": e["sector"],
                            "status": "EXCLUDED", "checks": [], "annual": [], "links": {},
                            "warnings": [], "signal": False, "to_buy_price": None})
    from review import review_reasons
    for e in entries:
        e["review"] = review_reasons(e)
    return {
        "generated": generated,
        "settings": {"margin": C.MARGIN_OF_SAFETY, "discount_rate": C.DISCOUNT_RATE,
                     "terminal_growth": C.TERMINAL_GROWTH, "watchlist_size": C.WATCHLIST_SIZE,
                     "overvalued_above": C.OVERVALUED_ABOVE},
        "companies": entries,
    }


def excluded_companies() -> list[dict]:
    """S&P 500 members skipped because of their sector (banks, insurers, REITs, utilities)."""
    path = Path("data/sp500.csv")
    if not path.exists():
        return []
    import csv
    with open(path, newline="") as f:
        return [{"ticker": r["ticker"], "name": r["name"], "sector": r["sector"]}
                for r in csv.DictReader(f) if r.get("sector") in C.EXCLUDED_SECTORS]


def render(data: dict, passphrase: str | None = None, label: str = "local") -> str:
    """Fill the template with the data, encrypted when a passphrase is given."""
    payload = json.dumps(data, separators=(",", ":"))
    if passphrase:
        from lock import encrypt
        payload = json.dumps(encrypt(payload, passphrase, label), separators=(",", ":"))
    return TEMPLATE.read_text().replace("/*__DATA__*/", payload.replace("</", "<\\/"))


def write_site(data: dict, site: Path = SITE) -> Path:
    site.mkdir(parents=True, exist_ok=True)
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    label = os.environ.get("PAGE_SALT") or "local"
    (site / "index.html").write_text(render(data, passphrase, label))
    (site / "manifest.webmanifest").write_text(json.dumps(MANIFEST, indent=2))
    for icon in ("icon-180.png", "icon-512.png"):
        if (ASSETS / icon).exists():
            shutil.copy(ASSETS / icon, site / icon)
    return site / "index.html"


def build(profiles: dict[str, dict] | None = None) -> Path:
    if profiles is None:
        p = OUT / "profiles.json"
        profiles = json.loads(p.read_text()) if p.exists() else {}
    valuations = json.loads((OUT / "valuations.json").read_text())
    companies = {p.stem: json.loads(p.read_text()) for p in (OUT / "companies").glob("*.json")}
    generated = datetime.now().strftime("%b %-d, %Y")
    data = page_data(valuations, companies, profiles or {}, generated, excluded_companies())
    # Unencrypted copy for alerts.py; output/ is never published or uploaded.
    (OUT / "page_data.json").write_text(json.dumps(data))
    return write_site(data)


if __name__ == "__main__":
    print(f"Wrote {build()}")
