"""Decide whether today's run needs an email alert:

1. A NEW buy signal (one that wasn't there on the previous run).
2. A NEW review flag on one of your holdings (HOLDINGS secret, e.g. "KO, COST").

What was flagged last time is remembered in state/, but only as keyed hashes (HMAC
with your passphrase), so no file ever contains a ticker. On GitHub Actions this
writes new_signal=... and review_needed=... for the workflow, which opens a GitHub
issue that never names the stock (GitHub emails you).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

STATE = Path("state/signals.json")
REVIEW_STATE = Path("state/reviews.json")
VALUATIONS = Path("output/valuations.json")
PAGE_DATA = Path("output/page_data.json")


def fingerprint(text: str, secret: str) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def check(results: list[dict], previous: set[str], secret: str) -> tuple[bool, set[str]]:
    today = {fingerprint(r["ticker"], secret) for r in results if r.get("signal")}
    return bool(today - previous), today


def parse_holdings(raw: str | None) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", (raw or "").upper()) if t]


def check_holdings(companies: list[dict], holdings: list[str], previous: set[str],
                   secret: str) -> tuple[bool, set[str]]:
    """A holding counts as newly flagged when it gains a kind of flag it didn't have
    last run (e.g. it was fine, or only overvalued, and now fails the quality screen)."""
    by = {c["ticker"]: c for c in companies}
    today = set()
    for t in holdings:
        for r in (by.get(t) or {}).get("review", []):
            today.add(fingerprint(f"{t}|{r['kind']}", secret))
    return bool(today - previous), today


def _load(path: Path) -> set[str]:
    return set(json.loads(path.read_text())) if path.exists() else set()


def _save(path: Path, items: set[str]) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(sorted(items)))


def main() -> None:
    secret = os.environ.get("PAGE_PASSPHRASE") or "local"
    outputs = {}

    results = json.loads(VALUATIONS.read_text())
    new_signal, today = check(results, _load(STATE), secret)
    _save(STATE, today)
    outputs["new_signal"] = new_signal
    print("New buy signal since last run." if new_signal else "No new buy signals.")

    holdings = parse_holdings(os.environ.get("HOLDINGS"))
    review_needed = False
    if holdings and PAGE_DATA.exists():
        companies = json.loads(PAGE_DATA.read_text())["companies"]
        review_needed, flagged = check_holdings(companies, holdings, _load(REVIEW_STATE), secret)
        _save(REVIEW_STATE, flagged)
        print("A holding has a new review flag." if review_needed else "No new review flags on holdings.")
    elif not holdings:
        print("No HOLDINGS secret set; skipping holdings review.")
    outputs["review_needed"] = review_needed

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            for k, v in outputs.items():
                f.write(f"{k}={'true' if v else 'false'}\n")


if __name__ == "__main__":
    main()
