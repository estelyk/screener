"""AI summaries of the latest earnings release, for pre-buy watch stocks and your holdings.

How it works, once a day:
1. For each of those companies, find its latest earnings release on SEC EDGAR (the
   "EX-99.1" press release attached to the 8-K the app already links to).
2. If it hasn't been summarised before, send its text to Claude (Anthropic's API) with
   instructions to explain it in plain English, using only figures in the document,
   flagging one-off items, and never saying whether to buy or sell.
3. Store the result so each release is summarised only once (about 4 times a year per
   company). The store is encrypted with your passphrase, like the page.

Switched on only when the ANTHROPIC_API_KEY secret is set; otherwise it does nothing.
AI summaries can contain mistakes, so the page labels them and links to the original.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import config as C

STORE = Path("state/summaries.enc")
STORE_PLAIN = Path("state/summaries.json")     # local runs without a passphrase
MAX_CHARS = 120_000                            # an earnings release is usually far shorter

PROMPT = """You are helping a long-term investor who is NOT a finance professional understand a company's latest earnings release. Read the document below and summarise it in plain English.

Rules:
- Use ONLY facts and figures stated in the document. If something isn't in it, say so; never guess or use outside knowledge.
- Explain any finance term in a few words the first time you use it.
- Point out one-time items (refunds, legal settlements, write-downs, gains on sales) and say what profit would look like without them, only if the document gives enough to work it out.
- Compare with the same period last year where the document allows.
- Do NOT recommend buying, selling or holding, and do not predict the share price.

Return ONLY a JSON object with these keys, each a short string (1-3 sentences) or null if the document doesn't cover it:
"period"        - which quarter/period and its end date
"headline"      - the single most important takeaway
"sales"         - sales and same-store/comparable sales, with % changes and where they grew or shrank
"profit"        - profit and margins vs last year, and why
"one_offs"      - one-time items and their effect
"cash_debt"     - cash, debt, inventory, buybacks/dividends
"outlook"       - management's forecast and whether it was raised, lowered or kept
"management"    - leadership changes or notable comments
"watch"         - 1-3 things worth checking in the next report

DOCUMENT:
"""

KEYS = ["period", "headline", "sales", "profit", "one_offs", "cash_debt", "outlook", "management", "watch"]


# ---------------------------------------------------------------- finding the release
def release_url(client, earnings_index: str | None) -> tuple[str | None, str | None]:
    """(document URL, filing ID) of the press release inside an 8-K earnings filing."""
    if not earnings_index:
        return None, None
    folder = earnings_index.rsplit("/", 1)[0] + "/"
    acc = folder.rstrip("/").rsplit("/", 1)[-1]
    client._throttle()
    r = client.session.get(folder + "index.json", timeout=60)
    r.raise_for_status()
    items = r.json().get("directory", {}).get("item", [])
    names = [i["name"] for i in items if i.get("name", "").lower().endswith((".htm", ".html", ".txt"))]
    ex = [n for n in names if re.search(r"ex[-_]?99[-_.]?0?1|ex991|exhibit99", n.lower())]
    ex = ex or [n for n in names if "99" in n and "index" not in n.lower()]
    return (folder + ex[0], acc) if ex else (None, acc)


def html_text(html: str) -> str:
    import lxml.html
    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style"):
        bad.drop_tree()
    text = doc.text_content()
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


# ---------------------------------------------------------------- calling Claude
def ask_claude(text: str, api_key: str, ai=None) -> dict:
    if ai is None:
        import anthropic
        ai = anthropic.Anthropic(api_key=api_key)
    msg = ai.messages.create(model=C.SUMMARY_MODEL, max_tokens=1500,
                             messages=[{"role": "user", "content": PROMPT + text[:MAX_CHARS]}])
    raw = "".join(getattr(b, "text", "") for b in msg.content).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    out = {k: (str(data[k]).strip() if data.get(k) else None) for k in KEYS}
    if not any(out.values()):
        out["headline"] = raw[:1500]           # fall back to whatever came back
    return out


# ---------------------------------------------------------------- storage
def _load(passphrase: str | None) -> dict:
    try:
        if passphrase and STORE.exists():
            from lock import decrypt
            return json.loads(decrypt(json.loads(STORE.read_text()), passphrase))
        if not passphrase and STORE_PLAIN.exists():
            return json.loads(STORE_PLAIN.read_text())
    except Exception:
        pass
    return {}


def _save(store: dict, passphrase: str | None) -> None:
    STORE.parent.mkdir(exist_ok=True)
    text = json.dumps(store)
    if passphrase:
        from lock import encrypt
        STORE.write_text(json.dumps(encrypt(text, passphrase, os.environ.get("PAGE_SALT") or "local")))
    else:
        STORE_PLAIN.write_text(text)


# ---------------------------------------------------------------- main entry
def add_summaries(client, profiles: dict[str, dict], tickers: list[str], ai=None) -> int:
    """Attach profiles[t]["ai_summary"] for each ticker; returns how many were new.
    Does nothing without ANTHROPIC_API_KEY (unless a test passes `ai`)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and ai is None:
        return 0
    passphrase = os.environ.get("PAGE_PASSPHRASE") or None
    store = _load(passphrase)
    new = 0
    for t in dict.fromkeys(tickers):                    # unique, keep order
        p = profiles.get(t)
        if not p or not p.get("earnings"):
            continue
        try:
            url, acc = release_url(client, p["earnings"])
            if not url:
                continue
            if acc in store:
                p["ai_summary"] = store[acc]
                continue
            if new >= C.SUMMARY_MAX_NEW_PER_RUN:
                continue
            client._throttle()
            r = client.session.get(url, timeout=60)
            r.raise_for_status()
            s = ask_claude(html_text(r.text), api_key, ai)
            s.update(url=url, date=p.get("earnings_date"), model=C.SUMMARY_MODEL)
            store[acc] = p["ai_summary"] = s
            new += 1
        except Exception as e:                           # one failure shouldn't stop the run
            if not C.QUIET:
                print(f"  (summary skipped for {t}: {type(e).__name__})")
    _save(store, passphrase)
    return new
