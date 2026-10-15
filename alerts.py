"""Decide whether today's run produced a NEW buy signal (one that wasn't there on
the previous run), for the alert.

Yesterday's signals are remembered in state/signals.json, but only as keyed hashes
(HMAC with your passphrase), so even that file never contains a ticker.

On GitHub Actions this writes new_signal=true/false for the workflow, which then
opens a GitHub issue saying only "new signal, open your page" (GitHub emails you).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

STATE = Path("state/signals.json")
VALUATIONS = Path("output/valuations.json")


def fingerprint(ticker: str, secret: str) -> str:
    return hmac.new(secret.encode(), ticker.encode(), hashlib.sha256).hexdigest()


def check(results: list[dict], previous: set[str], secret: str) -> tuple[bool, set[str]]:
    today = {fingerprint(r["ticker"], secret) for r in results if r.get("signal")}
    return bool(today - previous), today


def main() -> None:
    secret = os.environ.get("PAGE_PASSPHRASE") or "local"
    results = json.loads(VALUATIONS.read_text())
    previous = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new, today = check(results, previous, secret)

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(sorted(today)))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"new_signal={'true' if new else 'false'}\n")
    print("New buy signal since last run." if new else "No new buy signals.")


if __name__ == "__main__":
    main()
