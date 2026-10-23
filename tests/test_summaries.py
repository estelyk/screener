"""AI earnings summaries: found, summarised once, stored encrypted, off without a key."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import summaries  # noqa: E402

FOLDER = "https://www.sec.gov/Archives/edgar/data/1397187/000139718726000126/"
RELEASE = "<html><body><script>x()</script><p>Revenue decreased 4% to $2.4 billion.</p></body></html>"


class Resp:
    def __init__(self, body):
        self.body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeClient:
    def __init__(self):
        self.gets = []
        self.session = self

    def _throttle(self):
        pass

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url.endswith("index.json"):
            return Resp({"directory": {"item": [{"name": "lulu-20260802.htm"},
                                                {"name": "lulu-20260802xex991.htm"},
                                                {"name": "0001397187-26-000126-index.htm"}]}})
        return Resp(RELEASE)


class FakeAI:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        out = {"period": "Q2", "headline": "Sales fell 4%.", "sales": "Down 4%."}
        return SimpleNamespace(content=[SimpleNamespace(text="Here you go:\n" + json.dumps(out))])


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries, "STORE", tmp_path / "s.enc")
    monkeypatch.setattr(summaries, "STORE_PLAIN", tmp_path / "s.json")
    return tmp_path


def test_finds_the_press_release_exhibit():
    url, acc = summaries.release_url(FakeClient(), FOLDER + "0001397187-26-000126-index.htm")
    assert url == FOLDER + "lulu-20260802xex991.htm" and acc == "000139718726000126"


def test_html_to_text_drops_scripts():
    assert summaries.html_text(RELEASE) == "Revenue decreased 4% to $2.4 billion."


def test_summarises_once_and_reuses(tmp_store):
    ai, client = FakeAI(), FakeClient()
    prof = {"LULU": {"earnings": FOLDER + "x-index.htm", "earnings_date": "2026-09-03"}}
    assert summaries.add_summaries(client, prof, ["LULU"], ai=ai) == 1
    s = prof["LULU"]["ai_summary"]
    assert s["headline"] == "Sales fell 4%." and s["url"].endswith("ex991.htm") and s["profit"] is None
    assert "only facts" in ai.calls[0]["messages"][0]["content"].lower()
    prof2 = {"LULU": {"earnings": FOLDER + "x-index.htm"}}
    assert summaries.add_summaries(client, prof2, ["LULU"], ai=ai) == 0      # cached: no second call
    assert len(ai.calls) == 1 and prof2["LULU"]["ai_summary"]["headline"] == "Sales fell 4%."


def test_store_is_encrypted_with_passphrase(tmp_store, monkeypatch):
    monkeypatch.setenv("PAGE_PASSPHRASE", "secret words")
    prof = {"LULU": {"earnings": FOLDER + "x-index.htm"}}
    summaries.add_summaries(FakeClient(), prof, ["LULU"], ai=FakeAI())
    raw = (tmp_store / "s.enc").read_text()
    assert "Sales fell" not in raw and json.loads(raw)["locked"] == 1


def test_does_nothing_without_api_key(tmp_store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prof = {"LULU": {"earnings": FOLDER + "x-index.htm"}}
    assert summaries.add_summaries(FakeClient(), prof, ["LULU"]) == 0
    assert "ai_summary" not in prof["LULU"]


def test_page_shows_summary_section():
    import build_site
    v = {"ticker": "X", "name": "X", "sector": "S", "price": 1, "value_per_share": 2}
    e = build_site.company_entry(v, {"status": "PASS"}, {"ai_summary": {"headline": "h"}})
    assert e["ai_summary"]["headline"] == "h"
    assert "Latest earnings, summarised" in Path(build_site.TEMPLATE).read_text()
