#!/usr/bin/env python3
"""
Small-cap / penny stock screener (stocks under $10) — Buffett-style red-flag checks.

Data: SEC EDGAR (free, official) for fundamentals and filings, yfinance for prices.

Pipeline
  1. Universe: every NYSE / Nasdaq ticker in SEC's ticker file.
  2. Price screen: last close between --min-price and --max-price, enough volume to trade.
  3. For the most liquid candidates, pull EDGAR data and compute:
       - revenue, revenue growth, net income, operating cash flow (latest 10-K)
       - cash runway in months (only matters if cash flow is negative)
       - debt-to-equity, current ratio, market cap
       - share-count change over ~3 years (dilution) + possible reverse split
       - going-concern language in the latest 10-K
       - stock offerings filed in the last 12 months (S-1 / S-3 / 424B)
       - insider open-market buys vs sells in Form 4s (last 180 days)
  4. Red flags = automatic FAIL. Survivors get a 0-10 quality score: PASS at 5+, WEAK below.
     CHECK = price fell 60%+ in 3 months (spin-off/split/collapse). SKIP = foreign filer.
  5. Writes results.csv and report.html (phone-friendly table).

Usage
  export SEC_USER_AGENT="Your Name your@email.com"   # SEC requires a contact
  python smallcap_screener.py                          # defaults: $1-$10, top 150 by liquidity
  python smallcap_screener.py --max-candidates 50 --min-mcap 100e6

This is a research shortlist, not a buy list. Read the filings before acting.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
EXCHANGES = {"NYSE", "Nasdaq"}
ANNUAL_FORMS = {"10-K", "10-K/A"}
PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
OFFERING_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "424B1", "424B3", "424B4", "424B5", "424B7"}

TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "SalesRevenueGoodsNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "st_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "debt": ["LongTermDebt", "DebtInstrumentCarryingAmount"],
    "debt_noncurrent": ["LongTermDebtNoncurrent"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
}

# On GitHub Actions the run logs of a public repo are public: print counts only, never
# tickers or results. LEAN_CACHE skips saving big files that are refetched anyway.
QUIET = os.environ.get("GITHUB_ACTIONS") == "true"
LEAN_CACHE = os.environ.get("SMALLCAP_LEAN_CACHE") == "1" or QUIET

# Red-flag thresholds (edit to taste)
MAX_DILUTION_PCT = 50.0      # share count up more than this over ~3 years -> FAIL
MIN_RUNWAY_MONTHS = 12.0     # burning cash with less runway than this -> FAIL
MIN_REVENUE = 5_000_000      # less annual revenue than this -> "pre-revenue" FAIL
REVERSE_SPLIT_DROP = 0.40    # share count falls >40% between filings -> likely reverse split


# ----------------------------------------------------------------------------
# SEC client: polite rate limit (SEC allows 10 req/s), retries, disk cache
# ----------------------------------------------------------------------------
class Sec:
    def __init__(self, user_agent: str, cache_dir: Path, max_age_hours: float = 20):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age_hours * 3600
        self._last = 0.0

    def get(self, url: str, cache_key: str, as_json: bool = True, max_age: float | None = None,
            store: bool = True):
        path = self.cache_dir / cache_key
        age_limit = self.max_age if max_age is None else max_age
        if path.exists() and time.time() - path.stat().st_mtime < age_limit:
            text = path.read_text(encoding="utf-8", errors="replace")
            return json.loads(text) if as_json else text
        for attempt in range(4):
            wait = 0.12 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=40)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            finally:
                self._last = time.time()
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429) or r.status_code >= 500:
                time.sleep(2 ** attempt + 1)
                continue
            r.raise_for_status()
            if store:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text, encoding="utf-8")
            return r.json() if as_json else r.text
        return None


# ----------------------------------------------------------------------------
# XBRL helpers (work on the companyfacts JSON)
# ----------------------------------------------------------------------------
def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _rows(facts: dict, tag: str, unit: str = "USD", ns: str = "us-gaap") -> list[dict]:
    try:
        return facts["facts"][ns][tag]["units"][unit]
    except (KeyError, TypeError):
        return []


def annual_series(facts: dict, tags: list[str], unit: str = "USD") -> dict[str, float]:
    """Full-year values from 10-Ks, {period_end: value}. Picks the tag with the most recent
    data (companies switch tags over time); later filings overwrite restated years."""
    best: dict[str, float] = {}
    for tag in tags:
        out: dict[str, float] = {}
        for r in sorted(_rows(facts, tag, unit), key=lambda r: r.get("filed", "")):
            if r.get("form") not in ANNUAL_FORMS or not r.get("start"):
                continue
            days = (_d(r["end"]) - _d(r["start"])).days
            if 350 <= days <= 380:
                out[r["end"]] = r["val"]
        if out and (not best or max(out) > max(best)):
            best = out
    return dict(sorted(best.items()))


def latest_instant(facts: dict, tags: list[str], unit: str = "USD") -> tuple[float | None, str | None]:
    """Most recent balance-sheet value from any 10-K/10-Q."""
    best = None
    for tag in tags:
        for r in _rows(facts, tag, unit):
            if r.get("form") not in PERIODIC_FORMS or r.get("start"):
                continue
            key = (r["end"], r.get("filed", ""))
            if best is None or key > best[0]:
                best = (key, r["val"])
    if best is None:
        return None, None
    return best[1], best[0][0]


def value_at(facts: dict, tags: list[str], end: str, unit: str = "USD") -> float | None:
    for tag in tags:
        for r in _rows(facts, tag, unit):
            if r.get("end") == end and not r.get("start") and r.get("form") in PERIODIC_FORMS:
                return r["val"]
    return None


def share_history(facts: dict) -> dict[str, float]:
    """Shares outstanding over time, {date: shares}, from the filing cover pages."""
    rows = _rows(facts, "EntityCommonStockSharesOutstanding", "shares", ns="dei")
    if not rows:
        rows = _rows(facts, "CommonStockSharesOutstanding", "shares")
    out: dict[str, float] = {}
    for r in sorted(rows, key=lambda r: r.get("filed", "")):
        if r.get("val"):
            out[r["end"]] = r["val"]
    return dict(sorted(out.items()))


def dilution(shares: dict[str, float], years: float = 3.0):
    """Returns (pct_change, years_spanned, reverse_split_suspected)."""
    if len(shares) < 2:
        return None, None, False
    dates = list(shares)
    latest_d = _d(dates[-1])
    target = latest_d - dt.timedelta(days=int(365 * years))
    past = min(dates[:-1], key=lambda d: abs((_d(d) - target).days))
    span = (latest_d - _d(past)).days / 365
    if span < 1.0:
        return None, None, False
    pct = (shares[dates[-1]] / shares[past] - 1) * 100
    vals = [shares[d] for d in dates if _d(d) >= _d(past)]
    reverse = any(b < a * (1 - REVERSE_SPLIT_DROP) for a, b in zip(vals, vals[1:]))
    return pct, span, reverse


# ----------------------------------------------------------------------------
# Filing text checks
# ----------------------------------------------------------------------------
GOING_CONCERN = re.compile(
    r"(?<!could )(?<!may )(?<!might )(?<!would )(?<!could raise )(?<!may raise )"
    r"substantial doubt (?:exists |about |regarding |as to |concerning )?"
    r"(?:the company['’]s|our|its|the group['’]s)? ?ability to continue as a going concern",
    re.I,
)
HYPOTHETICAL = re.compile(r"\b(could|may|might|would|if)\b[^.]{0,80}$", re.I)


def has_going_concern(doc_html: str | None) -> bool | None:
    if not doc_html:
        return None
    text = re.sub(r"<[^>]+>", " ", doc_html)
    text = html.unescape(re.sub(r"\s+", " ", text))
    for m in GOING_CONCERN.finditer(text):
        before = text[max(0, m.start() - 100):m.start()]
        if not HYPOTHETICAL.search(before):
            return True
    return False


def parse_form4(xml_text: str | None) -> tuple[float, float]:
    """Dollar value of open-market purchases (code P) and sales (code S) in one Form 4."""
    if not xml_text:
        return 0.0, 0.0
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return 0.0, 0.0
    buys = sells = 0.0
    for t in root.iter("nonDerivativeTransaction"):
        code = (t.findtext("transactionCoding/transactionCode") or "").strip()
        try:
            shares = float(t.findtext("transactionAmounts/transactionShares/value") or 0)
            price = float(t.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if code == "P":
            buys += shares * price
        elif code == "S":
            sells += shares * price
    return buys, sells


def recent_filings(subs: dict, forms: set[str], days: int) -> list[dict]:
    rec = (subs or {}).get("filings", {}).get("recent", {})
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for i, form in enumerate(rec.get("form", [])):
        if form in forms and rec["filingDate"][i] >= cutoff:
            out.append({"form": form, "date": rec["filingDate"][i],
                        "accn": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i]})
    return out


def latest_10k(subs: dict) -> dict | None:
    rec = (subs or {}).get("filings", {}).get("recent", {})
    for i, form in enumerate(rec.get("form", [])):  # newest first
        if form == "10-K":
            return {"accn": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i],
                    "date": rec["filingDate"][i]}
    return None


def archive_url(cik: int, accn: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"


# ----------------------------------------------------------------------------
# Metrics + scoring (pure functions: easy to test and tweak)
# ----------------------------------------------------------------------------
def compute_metrics(facts: dict, price: float) -> dict:
    m: dict = {}
    rev = annual_series(facts, TAGS["revenue"])
    ni = annual_series(facts, TAGS["net_income"])
    ocf = annual_series(facts, TAGS["ocf"])

    # Recent discontinued operations = part of the business was sold or spun off, so the
    # last 10-K may describe a bigger company than the one trading today (e.g. Corteva).
    cutoff = (dt.date.today() - dt.timedelta(days=270)).isoformat()
    m["discontinued_ops"] = any(
        r.get("end", "") >= cutoff and r.get("val") and r.get("form") in PERIODIC_FORMS
        for tag in ["IncomeLossFromDiscontinuedOperationsNetOfTax",
                    "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity",
                    "DisposalGroupIncludingDiscontinuedOperationRevenue"]
        for r in _rows(facts, tag))

    m["fy_end"] = max(rev) if rev else (max(ni) if ni else None)
    m["revenue"] = rev[max(rev)] if rev else None
    if len(rev) >= 2:
        y = sorted(rev)
        prev = rev[y[-2]]
        m["rev_growth_pct"] = (rev[y[-1]] / prev - 1) * 100 if prev > 0 else None
    else:
        m["rev_growth_pct"] = None
    # Cash spent buying other companies in the last two fiscal years. Big spend relative to
    # revenue suggests growth was bought, not organic (e.g. Gevo, Gogo).
    acq = annual_series(facts, ["PaymentsToAcquireBusinessesNetOfCashAcquired",
                                "PaymentsToAcquireBusinessesGross",
                                "BusinessCombinationConsiderationTransferred1"])
    m["acq_spend_2y"] = sum(acq[d] for d in sorted(acq)[-2:]
                            if rev and d >= sorted(rev)[-2 if len(rev) >= 2 else -1]) if acq else 0.0
    rev_gain = (rev[sorted(rev)[-1]] - rev[sorted(rev)[-2]]) if len(rev) >= 2 else None
    m["acquisition_growth"] = bool(
        m["acq_spend_2y"] and rev and rev_gain and rev_gain > 0
        and (m["acq_spend_2y"] >= 0.5 * rev_gain or m["acq_spend_2y"] >= 0.10 * rev[max(rev)]))

    m["net_income"] = ni[max(ni)] if ni else None
    m["net_income_prev"] = ni[sorted(ni)[-2]] if len(ni) >= 2 else None
    m["ocf"] = ocf[max(ocf)] if ocf else None
    gp = annual_series(facts, ["GrossProfit"])
    if gp and rev and max(gp) == max(rev) and rev[max(rev)] > 0:
        m["gross_margin"] = gp[max(gp)] / rev[max(rev)]
    else:
        m["gross_margin"] = None

    cash, cash_date = latest_instant(facts, TAGS["cash"])
    if cash is not None and cash_date:
        sti = value_at(facts, TAGS["st_investments"], cash_date)
        cash += sti or 0
    m["cash"] = cash
    if m["ocf"] is not None and m["ocf"] < 0 and cash is not None:
        m["runway_months"] = cash / (-m["ocf"]) * 12
    else:
        m["runway_months"] = None

    debt, _ = latest_instant(facts, TAGS["debt"])
    if debt is None:
        nc, nc_date = latest_instant(facts, TAGS["debt_noncurrent"])
        if nc is not None:
            debt = nc + (value_at(facts, TAGS["debt_current"], nc_date) or 0)
    equity, _ = latest_instant(facts, TAGS["equity"])
    m["debt"] = debt
    m["equity"] = equity
    m["debt_to_equity"] = (debt / equity) if (debt is not None and equity and equity > 0) else None

    ca, ca_date = latest_instant(facts, TAGS["current_assets"])
    cl = value_at(facts, TAGS["current_liabilities"], ca_date) if ca_date else None
    m["current_ratio"] = (ca / cl) if (ca is not None and cl) else None

    shares = share_history(facts)
    # Cover-page counts can cover only one share class (e.g. Blue Owl), so also check
    # the weighted-average basic count from the income statement and use the larger.
    counts = [shares[max(shares)]] if shares else []
    wavg = annual_series(facts, ["WeightedAverageNumberOfSharesOutstandingBasic"], unit="shares")
    if wavg:
        counts.append(wavg[max(wavg)])
    m["shares"] = max(counts) if counts else None
    m["market_cap"] = m["shares"] * price if m["shares"] else None
    m["share_change_pct"], m["share_change_years"], m["reverse_split"] = dilution(shares)

    # Last 6 fiscal years for the company page charts (keyed by fiscal-year-end year).
    by_year: dict[int, dict] = {}
    for key, series in (("revenue", rev), ("net_income", ni), ("ocf", ocf)):
        for end, v in series.items():
            row = by_year.setdefault(int(end[:4]), {"y": int(end[:4])})
            row[key] = v
    m["annual"] = [by_year[y] for y in sorted(by_year)[-6:]]
    per_year: dict[int, float] = {}
    for d, v in shares.items():          # dates ascending: last one in a year wins
        per_year[int(d[:4])] = v
    m["shares_by_year"] = [{"y": y, "v": per_year[y]} for y in sorted(per_year)[-6:]]
    return m


MIN_PASS_SCORE = 5          # no red flags but a lower score -> WEAK, not PASS
BIG_DROP = 0.60             # price down this much from its 3-month high -> CHECK


MOON_MIN_GROWTH_PCT = 20.0  # moonshot PASS also needs revenue growing faster than this


def status_for(m: dict, flags: list[str], pts: int, mode: str = "value") -> str:
    if m.get("foreign"):
        return "SKIP"
    if flags:
        return "FAIL"
    if (m.get("drop_3m") is not None and m["drop_3m"] >= BIG_DROP) or m.get("discontinued_ops"):
        return "CHECK"
    if mode == "moonshot" and (m.get("rev_growth_pct") is None
                               or m["rev_growth_pct"] <= MOON_MIN_GROWTH_PCT):
        return "WEAK"  # profitable but slow-growing isn't a moonshot
    return "PASS" if pts >= MIN_PASS_SCORE else "WEAK"


# Moonshot mode: only fail what tends to wipe shareholders out; score upside signals.
MOON_MAX_DILUTION_PCT = 150.0   # serial share-printers
MOON_MIN_RUNWAY_MONTHS = 9.0
MOON_MIN_REVENUE = 1_000_000    # below this it's a pure lottery ticket


def score_moonshot(m: dict) -> tuple[list[str], int, list[str]]:
    """Looser screen for high-risk, high-upside small caps. Losses, negative equity and
    debt are allowed (Amazon 2001 had all three). Returns (red_flags, score 0-10, notes)."""
    flags, notes, pts = [], [], 0
    if m.get("foreign"):
        return [], 0, ["foreign filer (20-F/40-F, IFRS) — not checked, review manually"]
    if m.get("drop_3m") is not None and m["drop_3m"] >= BIG_DROP:
        notes.append(f"price down {m['drop_3m'] * 100:.0f}% from 3-mo high — spin-off, split "
                     "or collapse? Financials may not match today's company")
    if m.get("discontinued_ops"):
        notes.append("recent discontinued operations — part of the business was sold or spun "
                     "off; the last 10-K may describe a bigger company than today's")

    # Fatal flags only
    if m.get("going_concern"):
        flags.append("going-concern warning")
    if m.get("runway_months") is not None and m["runway_months"] < MOON_MIN_RUNWAY_MONTHS:
        flags.append(f"cash runway {m['runway_months']:.0f} mo")
    if m.get("share_change_pct") is not None and m["share_change_pct"] > MOON_MAX_DILUTION_PCT:
        flags.append(f"shares +{m['share_change_pct']:.0f}% in {m['share_change_years']:.1f}y")
    if m.get("reverse_split"):
        flags.append("likely reverse split")
    if m.get("revenue") is None or m["revenue"] < MOON_MIN_REVENUE:
        flags.append("no real revenue yet")

    # Upside signals
    g = m.get("rev_growth_pct")
    if g is not None and g > 20:
        pts += 2
        if g > 50 and not m.get("acquisition_growth"):
            pts += 1  # no bonus for growth that was bought
    if m.get("acquisition_growth"):
        notes.append(f"growth may be from acquisitions ({fmt_money(m['acq_spend_2y'])} spent "
                     "buying businesses in 2 yrs)")
    ni, ni_prev = m.get("net_income"), m.get("net_income_prev")
    if ni is not None and ni > 0:
        pts += 2
    elif ni is not None and ni_prev is not None and ni > ni_prev:
        pts += 1
        notes.append("losses shrinking")
    if m.get("gross_margin") is not None and m["gross_margin"] > 0.40:
        pts += 1
    if (m.get("ocf") or 0) > 0 or (m.get("runway_months") or 0) >= 24:
        pts += 1
    if (m.get("insider_net") or 0) > 0:
        pts += 2
        notes.append("insiders net buying")
    mc = m.get("market_cap")
    if mc is not None and 50e6 <= mc <= 1e9:
        pts += 1  # small enough to multiply
    sc = m.get("share_change_pct")
    if sc is not None and sc <= 20:
        pts += 1
    if m.get("offerings_12m"):
        pts -= 1
        notes.append(f"{m['offerings_12m']} offering filing(s) in 12 mo")
    if m.get("equity") is not None and m["equity"] <= 0:
        notes.append("negative equity")
    if m.get("going_concern") is None:
        notes.append("10-K not checked")
    return flags, max(0, min(10, pts)), notes


def explain_moonshot(m: dict) -> dict:
    """Plain-English breakdown of score_moonshot for the company page:
    {"checks": [{name, passed True/False/None, detail}], "points": [{name, pts, max, detail}]}.
    Must agree with score_moonshot (tested)."""
    def money(v):
        return fmt_money(v) if v is not None else "n/a"
    checks = []
    gc = m.get("going_concern")
    checks.append({"name": "No going-concern warning", "passed": None if gc is None else not gc,
                   "detail": "Latest annual report not checked" if gc is None else
                   ("The auditor doubts the company can keep going" if gc else
                    "The auditor raised no doubt about the company surviving")})
    rw = m.get("runway_months")
    checks.append({"name": f"At least {MOON_MIN_RUNWAY_MONTHS:.0f} months of cash",
                   "passed": rw is None or rw >= MOON_MIN_RUNWAY_MONTHS,
                   "detail": (f"Cash flow is positive ({money(m.get('ocf'))} last year)" if (m.get("ocf") or 0) > 0
                              else "No cash-flow data" if rw is None
                              else f"{money(m.get('cash'))} cash ÷ {money(-(m.get('ocf') or 0))} yearly burn ≈ {rw:.0f} months")})
    sc = m.get("share_change_pct")
    checks.append({"name": f"Share count up no more than {MOON_MAX_DILUTION_PCT:.0f}% in ~3 years",
                   "passed": sc is None or sc <= MOON_MAX_DILUTION_PCT,
                   "detail": "Not enough share history" if sc is None else
                   f"{sc:+.0f}% over {m.get('share_change_years') or 0:.1f} years"})
    checks.append({"name": "No sign of a reverse split", "passed": not m.get("reverse_split"),
                   "detail": "Share count fell 40%+ between filings, usually a reverse split"
                   if m.get("reverse_split") else "No sudden drop in share count"})
    rev = m.get("revenue")
    checks.append({"name": f"Revenue of at least {fmt_money(MOON_MIN_REVENUE)}",
                   "passed": rev is not None and rev >= MOON_MIN_REVENUE,
                   "detail": f"{money(rev)} in the latest year" if rev is not None else "No revenue reported"})

    pts = []
    g = m.get("rev_growth_pct")
    acq = m.get("acquisition_growth")
    gp = (2 if g is not None and g > 20 else 0) + (1 if g is not None and g > 50 and not acq else 0)
    pts.append({"name": "Revenue growth", "pts": gp, "max": 3,
                "detail": ("No growth data" if g is None else f"{g:+.0f}% last year") +
                " · +2 if over 20%, +1 more if over 50%" +
                (" (no bonus: growth may be from acquisitions)" if acq and g is not None and g > 50 else "")})
    ni, ni_prev = m.get("net_income"), m.get("net_income_prev")
    if ni is not None and ni > 0:
        pp, d = 2, f"Profitable: {money(ni)} last year"
    elif ni is not None and ni_prev is not None and ni > ni_prev:
        pp, d = 1, f"Still losing money, but less: {money(ni)} vs {money(ni_prev)} the year before"
    else:
        pp, d = 0, "No profit data" if ni is None else f"Losing money: {money(ni)} last year"
    pts.append({"name": "Profit", "pts": pp, "max": 2, "detail": d + " · +2 if profitable, +1 if losses are shrinking"})
    gm = m.get("gross_margin")
    pts.append({"name": "Gross margin", "pts": 1 if gm is not None and gm > 0.40 else 0, "max": 1,
                "detail": ("Not reported" if gm is None else f"{gm * 100:.0f}% of sales left after direct costs") +
                " · +1 if over 40%"})
    ok_cash = (m.get("ocf") or 0) > 0 or (m.get("runway_months") or 0) >= 24
    pts.append({"name": "Cash flow or runway", "pts": 1 if ok_cash else 0, "max": 1,
                "detail": (f"Operating cash flow {money(m.get('ocf'))}" +
                           (f", about {rw:.0f} months of cash" if rw is not None else "")) +
                " · +1 if cash flow is positive or 2+ years of cash"})
    ins = m.get("insider_net") or 0
    pts.append({"name": "Insider buying", "pts": 2 if ins > 0 else 0, "max": 2,
                "detail": f"Bought {money(m.get('insider_buys') or 0)}, sold {money(m.get('insider_sells') or 0)} "
                "on the open market in 180 days · +2 if net buying"})
    mc = m.get("market_cap")
    pts.append({"name": "Room to grow", "pts": 1 if mc is not None and 50e6 <= mc <= 1e9 else 0, "max": 1,
                "detail": f"Company worth {money(mc)} · +1 if $50M–$1B"})
    pts.append({"name": "Little dilution", "pts": 1 if sc is not None and sc <= 20 else 0, "max": 1,
                "detail": ("Not enough share history" if sc is None else f"Shares {sc:+.0f}% in ~3 years") +
                " · +1 if up 20% or less"})
    off = m.get("offerings_12m") or 0
    pts.append({"name": "Recent stock-sale filings", "pts": -1 if off else 0, "max": 0,
                "detail": f"{off} filing(s) to sell new shares in 12 months · −1 if any"})
    return {"checks": checks, "points": pts}


def score(m: dict) -> tuple[list[str], int, list[str]]:
    """Value mode. Returns (red_flags, score 0-10, notes)."""
    flags, notes, pts = [], [], 0
    if m.get("foreign"):
        return [], 0, ["foreign filer (20-F/40-F, IFRS) — not checked, review manually"]
    if m.get("drop_3m") is not None and m["drop_3m"] >= BIG_DROP:
        notes.append(f"price down {m['drop_3m'] * 100:.0f}% from 3-mo high — spin-off, split "
                     "or collapse? Financials may not match today's company")
    if m.get("discontinued_ops"):
        notes.append("recent discontinued operations — part of the business was sold or spun "
                     "off; the last 10-K may describe a bigger company than today's")

    if m.get("going_concern"):
        flags.append("going-concern warning")
    if m.get("runway_months") is not None and m["runway_months"] < MIN_RUNWAY_MONTHS:
        flags.append(f"cash runway {m['runway_months']:.0f} mo")
    if m.get("share_change_pct") is not None and m["share_change_pct"] > MAX_DILUTION_PCT:
        flags.append(f"shares +{m['share_change_pct']:.0f}% in {m['share_change_years']:.1f}y")
    if m.get("reverse_split"):
        flags.append("likely reverse split")
    if m.get("revenue") is None or m["revenue"] < MIN_REVENUE:
        flags.append("little/no revenue")
    if m.get("equity") is not None and m["equity"] <= 0:
        flags.append("negative equity")

    if (m.get("net_income") or 0) > 0:
        pts += 2
    if (m.get("ocf") or 0) > 0:
        pts += 2
    g = m.get("rev_growth_pct")
    if g is not None and g > 0:
        pts += 1
        if g > 10:
            pts += 1
    if m.get("debt_to_equity") is not None and m["debt_to_equity"] < 0.5:
        pts += 1
    if m.get("current_ratio") is not None and m["current_ratio"] > 1.5:
        pts += 1
    sc = m.get("share_change_pct")
    if sc is not None and sc <= 5:
        pts += 1
    if (m.get("insider_net") or 0) > 0:
        pts += 1
        notes.append("insiders net buying")
    if m.get("acquisition_growth"):
        notes.append(f"growth may be from acquisitions ({fmt_money(m['acq_spend_2y'])} spent "
                     "buying businesses in 2 yrs)")
    if m.get("offerings_12m"):
        pts -= 1
        notes.append(f"{m['offerings_12m']} offering filing(s) in 12 mo")
    if m.get("going_concern") is None:
        notes.append("10-K not checked")
    return flags, max(0, min(10, pts)), notes


# ----------------------------------------------------------------------------
# Universe + prices
# ----------------------------------------------------------------------------
def load_universe(sec: Sec) -> list[dict]:
    data = sec.get("https://www.sec.gov/files/company_tickers_exchange.json",
                   "company_tickers_exchange.json")
    if not data:
        sys.exit("Could not download SEC ticker list. Check SEC_USER_AGENT and your connection.")
    idx = {f: i for i, f in enumerate(data["fields"])}
    seen, out = set(), []
    for row in data["data"]:
        cik, name, tkr, exch = (row[idx["cik"]], row[idx["name"]],
                                row[idx["ticker"]], row[idx["exchange"]])
        if exch not in EXCHANGES or not tkr or cik in seen:
            continue
        # skip warrants / units / rights / preferreds
        if "-" in tkr and not re.fullmatch(r"[A-Z]+-[A-Z]", tkr):
            continue
        if len(tkr) == 5 and tkr[-1] in "WUR":
            continue
        seen.add(cik)
        out.append({"cik": int(cik), "name": name, "ticker": tkr, "exchange": exch})
    return out


def _download_batch(yf, batch: list[str], threads) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        df = yf.download(batch, period="3mo", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=threads)
    except Exception as e:  # noqa: BLE001
        print(f"    batch failed: {type(e).__name__}" if QUIET else f"    batch failed: {e}")
        return out
    for t in batch:
        try:
            sub = df[t] if len(batch) > 1 else df
            close = sub["Close"].dropna()
            vol = sub["Volume"].dropna()
            if close.empty:
                continue
            last = float(close.iloc[-1])
            high = float(close.max())
            out[t] = {"price": last,
                      "avg_volume": float(vol.tail(50).mean()) if not vol.empty else 0.0,
                      "drop_3m": (1 - last / high) if high > 0 else None}
        except (KeyError, IndexError):
            continue
    return out


def fetch_prices(tickers: list[str], cache_dir: Path, chunk: int = 50) -> dict[str, dict]:
    """Small batches with limited threads; big batches crashed with out-of-memory,
    too-many-open-files and DNS errors. Missing tickers get one slower retry."""
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # hide per-ticker noise
    tz_dir = cache_dir / "yf_tz"
    tz_dir.mkdir(parents=True, exist_ok=True)
    try:
        yf.set_tz_cache_location(str(tz_dir))  # avoids "unable to open database file"
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, dict] = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        if i % 1000 == 0:
            print(f"  prices {i + 1}-{min(i + 1000, len(tickers))} of {len(tickers)}", flush=True)
        out.update(_download_batch(yf, batch, threads=4))
        time.sleep(0.3)

    missing = [t for t in tickers if t not in out]
    if missing:
        print(f"  retrying {len(missing)} tickers with no price...", flush=True)
        for i in range(0, len(missing), 20):
            out.update(_download_batch(yf, missing[i:i + 20], threads=False))
            time.sleep(0.5)
    print(f"  got prices for {len(out)} of {len(tickers)}")
    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def analyze(sec: Sec, c: dict, mcap_range: tuple | None = None) -> dict | None:
    """Returns metrics, or None for no data. Returns {"_out_of_range": True} if the market
    cap is outside mcap_range, checked before the slow 10-K and Form 4 downloads."""
    cik10 = f"{c['cik']:010d}"
    facts = sec.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json",
                    f"facts/{cik10}.json", store=not LEAN_CACHE)
    if not facts:
        return None
    m = compute_metrics(facts, c["price"])
    m["drop_3m"] = c.get("drop_3m")
    if mcap_range and m.get("market_cap") is not None:
        lo, hi = mcap_range
        if m["market_cap"] < lo or (hi and m["market_cap"] > hi):
            return {"_out_of_range": True}
    subs = sec.get(f"https://data.sec.gov/submissions/CIK{cik10}.json", f"subs/{cik10}.json",
                   store=not LEAN_CACHE)

    tenk = latest_10k(subs)
    recent_forms = set((subs or {}).get("filings", {}).get("recent", {}).get("form", []))
    m["foreign"] = tenk is None and bool(recent_forms & {"20-F", "20-F/A", "40-F", "40-F/A"})
    if m["foreign"]:
        m["market_cap"] = None  # ADR share counts don't map to the US listing
        m["going_concern"] = None
        m["tenk_url"] = ""
        return m
    m["going_concern"] = None
    m["tenk_url"] = ""
    if tenk:
        url = archive_url(c["cik"], tenk["accn"], tenk["doc"])
        m["tenk_url"] = url
        gc_path = sec.cache_dir / "gc" / f"{tenk['accn']}.json"
        if gc_path.exists():
            m["going_concern"] = json.loads(gc_path.read_text())
        else:
            doc = sec.get(url, f"10k/{tenk['accn']}.htm", as_json=False, max_age=90 * 86400,
                          store=False)
            m["going_concern"] = has_going_concern(doc)
            if doc is not None:  # don't remember a failed download
                gc_path.parent.mkdir(parents=True, exist_ok=True)
                gc_path.write_text(json.dumps(m["going_concern"]))

    m["offerings_12m"] = len(recent_filings(subs, OFFERING_FORMS, 365))
    m["industry"] = ((subs or {}).get("sicDescription") or "").strip().title() or None
    m["ten_k_date"] = tenk["date"] if tenk else None
    m["cik"] = c["cik"]

    buys = sells = 0.0
    for f in recent_filings(subs, {"4"}, 180)[:15]:
        raw_doc = f["doc"].split("/")[-1]  # strip the xsl viewer prefix to get raw XML
        xml = sec.get(archive_url(c["cik"], f["accn"], raw_doc), f"f4/{f['accn']}.xml",
                      as_json=False, max_age=365 * 86400)
        b, s = parse_form4(xml)
        buys, sells = buys + b, sells + s
    m["insider_buys"], m["insider_sells"] = buys, sells
    m["insider_net"] = buys - sells
    return m


def fmt_money(v):
    if v is None:
        return "n/a"
    a = abs(v)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.0f}K"
    return "-" + s if v < 0 else s


def fmt_num(v, suffix="", digits=1):
    return "n/a" if v is None else f"{v:.{digits}f}{suffix}"


RULES = {
    "value": ("Red flags: going concern, &lt;12 mo cash, shares +50% in 3y, reverse split, "
              "revenue &lt;$5M, negative equity. Score: profit +2, cash flow +2, revenue growth "
              "+1/+2, low debt +1, liquidity +1, no dilution +1, insider buying +1, "
              "recent offering −1."),
    "moonshot": ("Red flags (fatal only): going concern, &lt;9 mo cash, shares +150% in 3y, "
                 "reverse split, revenue &lt;$1M. Losses, debt and negative equity allowed. "
                 "Score: revenue growth &gt;20% +2 (&gt;50% +1 more), profitable +2 or losses "
                 "shrinking +1, gross margin &gt;40% +1, cash-flow positive or 2y+ runway +1, "
                 "insiders buying +2, market cap $50M–$1B +1, shares up ≤20% +1, "
                 "recent offering −1."),
}


def write_outputs(rows: list[dict], outdir: Path, mode: str = "value"):
    rules = RULES[mode]
    growth_rule = f", revenue growth over {MOON_MIN_GROWTH_PCT:.0f}%" if mode == "moonshot" else ""
    weak_extra = " or slow growth" if mode == "moonshot" else ""
    outdir.mkdir(parents=True, exist_ok=True)
    cols = ["status", "score", "ticker", "name", "price", "market_cap", "revenue", "rev_growth_pct",
            "net_income", "ocf", "cash", "runway_months", "debt_to_equity", "current_ratio",
            "share_change_pct", "share_change_years", "insider_buys", "insider_sells",
            "offerings_12m", "going_concern", "drop_3m", "discontinued_ops", "acq_spend_2y", "red_flags", "notes", "tenk_url"]
    with open(outdir / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    def tr(r):
        cls = r["status"].lower()
        flags = html.escape(r["red_flags"] or "—")
        notes = html.escape(r["notes"] or "")
        link = f'<a href="{html.escape(r["tenk_url"])}">10-K</a>' if r["tenk_url"] else ""
        return (f'<tr class="{cls}"><td><b>{html.escape(r["ticker"])}</b><br>'
                f'<small>{html.escape(r["name"][:28])}</small></td>'
                f'<td>{r["status"]}<br><b>{r["score"]}/10</b></td>'
                f'<td>${r["price"]:.2f}<br><small>{fmt_money(r["market_cap"])}</small></td>'
                f'<td>{fmt_money(r["revenue"])}<br><small>{fmt_num(r["rev_growth_pct"], "%", 0)} yoy</small></td>'
                f'<td>NI {fmt_money(r["net_income"])}<br><small>OCF {fmt_money(r["ocf"])}</small></td>'
                f'<td>D/E {fmt_num(r["debt_to_equity"], "", 2)}<br><small>CR {fmt_num(r["current_ratio"], "", 1)}</small></td>'
                f'<td>{fmt_num(r["share_change_pct"], "%", 0)}</td>'
                f'<td>{flags}<br><small>{notes}</small></td><td>{link}</td></tr>')

    counts = ", ".join(f"{sum(r['status'] == s for r in rows)} {s}"
                       for s in ("PASS", "WEAK", "CHECK", "FAIL", "SKIP"))
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Small-cap screen</title><style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:12px;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px;border-bottom:1px solid #eee;
vertical-align:top;text-align:left}}th{{position:sticky;top:0;background:#fafafa;font-size:12px}}
tr.fail,tr.skip{{color:#999}}tr.pass td:nth-child(2){{color:#0a7a32}}
tr.check td:nth-child(2){{color:#b36b00}}small{{color:#777}}
.wrap{{overflow-x:auto}}</style></head><body>
<h2>Small-cap screen ({mode} mode) — {dt.date.today():%b %d, %Y}</h2>
<p>{counts} — {len(rows)} checked.<br>
<b>PASS</b> = no red flags, score {MIN_PASS_SCORE}+{growth_rule}. <b>WEAK</b> = no red flags, but low score{weak_extra}.
<b>CHECK</b> = recent spin-off/sale of a business, or price fell {BIG_DROP * 100:.0f}%+ in 3 months, so
the last 10-K may not describe today's company. <b>FAIL</b> = red flag. <b>SKIP</b> = foreign filer.<br>
{rules}
<b>Shortlist for research, not buy signals.</b></p>
<div class="wrap"><table><tr><th>Stock</th><th>Result</th><th>Price / Cap</th><th>Revenue</th>
<th>Profit / Cash flow</th><th>Debt / Liquidity</th><th>Shares Δ ~3y</th><th>Flags</th><th></th></tr>
{''.join(tr(r) for r in rows)}</table></div></body></html>"""
    (outdir / "report.html").write_text(page, encoding="utf-8")


def main(argv: list[str] | None = None) -> list[dict]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-price", type=float, default=1.0)
    ap.add_argument("--max-price", type=float, default=10.0)
    ap.add_argument("--min-avg-volume", type=float, default=200_000, help="avg daily shares")
    ap.add_argument("--mode", choices=["moonshot", "value"], default="moonshot",
                    help="moonshot (default): only fatal red flags, scores upside signals. "
                         "value: strict Buffett-style checks")
    ap.add_argument("--min-mcap", type=float, default=None,
                    help="minimum market cap in $ (default 20M moonshot, 50M value)")
    ap.add_argument("--max-mcap", type=float, default=None,
                    help="maximum market cap in $ (default 2B moonshot, none in value mode)")
    ap.add_argument("--max-candidates", type=int, default=150,
                    help="how many US companies in the market-cap range to analyze "
                         "(most liquid first)")
    ap.add_argument("--tickers", help="comma list to analyze instead of the whole market")
    ap.add_argument("--include-foreign", action="store_true",
                    help="list foreign filers (20-F/40-F) in the report as SKIP")
    ap.add_argument("--out", default="output")
    ap.add_argument("--cache", default=".cache")
    args = ap.parse_args(argv)

    if args.min_mcap is None:
        args.min_mcap = 20e6 if args.mode == "moonshot" else 50e6
    if args.max_mcap is None and args.mode == "moonshot":
        args.max_mcap = 2e9
    scorer = score_moonshot if args.mode == "moonshot" else score
    print(f"Mode: {args.mode}")

    ua = os.environ.get("SEC_USER_AGENT")
    if not ua or "@" not in ua:
        sys.exit('Set SEC_USER_AGENT, e.g.  export SEC_USER_AGENT="Jane Doe jane@example.com"')
    sec = Sec(ua, Path(args.cache))

    print("Loading SEC ticker list...")
    universe = load_universe(sec)
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
        universe = [u for u in universe if u["ticker"] in want]
    print(f"  {len(universe)} listed companies")

    print("Fetching prices...")
    prices = fetch_prices([u["ticker"] for u in universe], Path(args.cache))
    cands = []
    for u in universe:
        p = prices.get(u["ticker"])
        if not p:
            continue
        if args.tickers or (args.min_price <= p["price"] <= args.max_price
                            and p["avg_volume"] >= args.min_avg_volume):
            cands.append({**u, **p})
    cands.sort(key=lambda c: c["price"] * c["avg_volume"], reverse=True)
    print(f"  {len(cands)} stocks pass the price/volume screen; analyzing until "
          f"{args.max_candidates} US filers in the market-cap range are found")

    rows, foreign, out_of_range = [], [], 0
    mcap_range = None if args.tickers else (args.min_mcap, args.max_mcap)
    for i, c in enumerate(cands, 1):
        if len(rows) >= args.max_candidates:
            break
        if not QUIET:
            print(f"[{len(rows) + 1}/{args.max_candidates}] {c['ticker']}", flush=True)
        elif i % 100 == 0:
            print(f"  checked {i} stocks, {len(rows)} kept", flush=True)
        try:
            m = analyze(sec, c, mcap_range)
        except Exception as e:  # noqa: BLE001  keep going on odd filers
            print(f"    skipped: {type(e).__name__}" if QUIET else f"    skipped: {e}")
            continue
        if not m:
            continue
        if m.get("_out_of_range"):
            out_of_range += 1
            continue
        if m.get("foreign"):
            foreign.append(c["ticker"])
            if not args.include_foreign:
                continue
        flags, pts, notes = scorer(m)
        rows.append({**c, **m, "status": status_for(m, flags, pts, args.mode), "score": pts,
                     "red_flags": "; ".join(flags), "notes": "; ".join(notes)})
    print(f"  (skipped {out_of_range} outside the market-cap range and {len(foreign)} "
          f"foreign filers{' — listed in report' if args.include_foreign else ''})")

    order = {"PASS": 0, "WEAK": 1, "CHECK": 2, "FAIL": 3, "SKIP": 4}
    rows.sort(key=lambda r: (order[r["status"]], -r["score"], -(r.get("market_cap") or 0)))
    write_outputs(rows, Path(args.out), args.mode)
    counts = {s: sum(r["status"] == s for r in rows) for s in order}
    print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items()) + f" of {len(rows)} analyzed.")
    shown = [r for r in rows if r["status"] in ("PASS", "WEAK", "CHECK")]
    for r in ([] if QUIET else shown[:20]):
        print(f"  {r['status']:5} {r['ticker']:6} {r['score']:>2}/10  ${r['price']:.2f}  "
              f"{fmt_money(r['market_cap'])}  {r['notes']}")
    print(f"\nWrote {args.out}/results.csv and {args.out}/report.html")
    return rows


if __name__ == "__main__":
    main()
