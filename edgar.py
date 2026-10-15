"""Download SEC EDGAR 'company facts' (every XBRL number a company has filed).

- SEC requires a User-Agent with your name and email; requests without it get blocked.
- SEC allows ~10 requests/second; we stay under that.
- Only the tags we use are kept on disk, so the cache stays small (a few MB total
  instead of ~1 GB of raw files). Financials change quarterly, so cached files are
  reused for a week before re-downloading.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
RETRY_STATUS = {429, 500, 502, 503, 504}


class EdgarClient:
    def __init__(self, user_agent: str, keep_tags: list[str],
                 cache_dir: str = "data/cache/companyfacts",
                 max_age_days: float = 7, min_interval: float = 0.12):
        self.keep_tags = keep_tags
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age_days * 86400
        self.min_interval = min_interval
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _trim(self, data: dict) -> dict:
        gaap = data.get("facts", {}).get("us-gaap", {})
        kept = {}
        for tag in self.keep_tags:
            units = gaap.get(tag, {}).get("units", {})
            keep = {u: rows for u, rows in units.items() if u in ("USD", "shares")}
            if keep:
                kept[tag] = {"units": keep}
        return {"cik": data.get("cik"), "entityName": data.get("entityName"),
                "_tags": list(self.keep_tags), "facts": {"us-gaap": kept}}

    def filings(self, cik: int) -> dict:
        """Industry and links to the latest 10-K and latest earnings release (8-K item 2.02).
        Cached for a week like company facts."""
        path = self.cache_dir.parent / "filings" / f"CIK{cik:010d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            return json.loads(path.read_text())

        self._throttle()
        r = self.session.get(SUBMISSIONS_URL.format(cik=cik), timeout=60)
        r.raise_for_status()
        info = parse_filings(cik, r.json())
        path.write_text(json.dumps(info))
        return info

    def raw_companyfacts(self, cik: int) -> dict | None:
        """Untrimmed download (used by diagnose.py); not cached."""
        self._throttle()
        r = self.session.get(COMPANYFACTS_URL.format(cik=cik), timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def companyfacts(self, cik: int) -> dict | None:
        path = self.cache_dir / f"CIK{cik:010d}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            cached = json.loads(path.read_text())
            # Re-download if the tag list changed since this file was saved.
            if cached.get("_tags") == list(self.keep_tags):
                return cached

        url = COMPANYFACTS_URL.format(cik=cik)
        for attempt in range(4):
            self._throttle()
            r = self.session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code in RETRY_STATUS:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = self._trim(r.json())
            path.write_text(json.dumps(data))
            return data
        r.raise_for_status()
        return None


def parse_filings(cik: int, sub: dict) -> dict:
    """Pull what the brief needs out of an SEC 'submissions' document."""
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    info = {"sic_description": sub.get("sicDescription"),
            "all_filings": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                           f"&CIK={cik}&type=10-K&dateb=&owner=include&count=40",
            "ten_k": None, "ten_k_date": None, "earnings": None, "earnings_date": None}

    def link(i, index_page=False):
        acc = recent["accessionNumber"][i]
        base = ARCHIVE.format(cik=cik, acc_nodash=acc.replace("-", ""))
        return base + (f"{acc}-index.htm" if index_page else recent["primaryDocument"][i])

    for i, form in enumerate(forms):  # newest first
        if info["ten_k"] is None and form in ("10-K", "20-F", "40-F"):
            info["ten_k"], info["ten_k_date"] = link(i), recent["filingDate"][i]
        items = (recent.get("items") or [""] * len(forms))[i] or ""
        if info["earnings"] is None and form == "8-K" and "2.02" in items:
            info["earnings"], info["earnings_date"] = link(i, index_page=True), recent["filingDate"][i]
        if info["ten_k"] and info["earnings"]:
            break
    return info
