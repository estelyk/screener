"""Watchlist files (smallcap_watchlist.txt, value_watchlist.txt): one ticker per line,
commas fine, # starts a comment. The app edits them through GitHub (watchlist_edit.js)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def read_watchlist(path: Path, limit: int = 50) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text().splitlines():
        for w in line.split("#")[0].replace(",", " ").split():
            w = w.strip().upper().lstrip("$")
            if TICKER.fullmatch(w) and w not in out:
                out.append(w)
    return out[:limit]
