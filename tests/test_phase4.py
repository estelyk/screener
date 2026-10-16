"""Phase 4 tests: encryption, alerts, quiet public logs, failing safely."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import alerts  # noqa: E402
import build_site  # noqa: E402
import lock  # noqa: E402


def test_encrypt_roundtrip_and_wrong_passphrase():
    blob = lock.encrypt('{"hello": "LULU"}', "copper-lantern-orbit-maple", "kyle/screener")
    assert "LULU" not in json.dumps(blob)
    assert lock.decrypt(blob, "copper-lantern-orbit-maple") == '{"hello": "LULU"}'
    with pytest.raises(Exception):
        lock.decrypt(blob, "wrong")


def test_salt_is_stable_per_site_so_remembered_key_keeps_working():
    a = lock.encrypt("x", "p", "kyle/screener")
    b = lock.encrypt("x", "p", "kyle/screener")
    assert a["salt"] == b["salt"] and a["iv"] != b["iv"]   # same key, fresh nonce each day
    assert lock.encrypt("x", "p", "other/repo")["salt"] != a["salt"]


def test_published_page_contains_no_readable_results(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("PAGE_PASSPHRASE", "copper-lantern-orbit-maple")
    monkeypatch.setenv("PAGE_SALT", "kyle/screener")
    data = {"generated": "Oct 1, 2026", "settings": {}, "companies": [
        {"ticker": "LULU", "name": "Lululemon Athletica", "signal": True}]}
    html = build_site.write_site(data, tmp_path).read_text()
    assert '"locked":1' in html
    assert "LULU" not in html and "Lululemon" not in html


def test_unlocked_page_when_no_passphrase(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("PAGE_PASSPHRASE", raising=False)
    html = build_site.write_site({"generated": "x", "settings": {}, "companies": []}, tmp_path).read_text()
    assert '"locked"' not in html


def test_alert_only_for_new_signals():
    s = "secret"
    day1 = [{"ticker": "LULU", "signal": True}, {"ticker": "KO", "signal": False}]
    new, state = alerts.check(day1, set(), s)
    assert new and len(state) == 1
    assert not any("LULU" in h for h in state)               # state never holds tickers
    new, state = alerts.check(day1, state, s)
    assert not new                                            # same signal again: no alert
    day3 = day1 + [{"ticker": "DECK", "signal": True}]
    assert alerts.check(day3, state, s)[0]                    # a new one: alert


def test_quiet_phase2_report_prints_no_tickers(capsys, monkeypatch):
    import config as C
    import run_phase2
    monkeypatch.setattr(C, "QUIET", True)
    results = [{"ticker": "LULU", "signal": True, "value_per_share": 10, "price": 5, "discount": .5,
                "verify": "", "suspicious": False, "warnings": [], "to_buy_price": None, "buy_below": 7.5}]
    run_phase2.report(results)
    out = capsys.readouterr().out
    assert "LULU" not in out and "Valued 1 of 1" in out


def test_missing_prices_stop_the_run(tmp_path, monkeypatch):
    import prices
    import run_phase2
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output" / "companies").mkdir(parents=True)
    for t in ("AAA", "BBB", "CCC"):
        (tmp_path / "output" / "companies" / f"{t}.json").write_text(json.dumps(
            {"ticker": t, "name": t, "sector": "X", "status": "PASS", "annual": [], "notes": []}))
    monkeypatch.setattr(run_phase2, "OUT", tmp_path / "output")
    monkeypatch.setattr(prices, "market_data", lambda t: ({"AAA": 1.0}, {}))
    with pytest.raises(SystemExit, match="Only 1 of 3 prices"):
        run_phase2.main()
    assert not (tmp_path / "output" / "valuations.json").exists()


def test_workflow_refuses_to_publish_unencrypted_page():
    wf = (ROOT / ".github" / "workflows" / "daily.yml").read_text()
    assert "Page is not encrypted" in wf and "PAGE_PASSPHRASE" in wf


# ---------------- holdings review ----------------
from review import review_reasons  # noqa: E402


def test_review_flags_follow_buffett_sell_reasons():
    fail = {"status": "FAIL", "checks": [{"name": "debt", "passed": False}, {"name": "fcf", "passed": True}]}
    assert review_reasons(fail) == [{"kind": "quality",
                                     "text": "No longer passes the quality screen (failed: debt)"}]
    rich = {"status": "PASS", "price": 160.0, "value": 100.0, "warnings": []}
    assert [r["kind"] for r in review_reasons(rich)] == ["overvalued"]
    fine = {"status": "PASS", "price": 140.0, "value": 100.0, "warnings": []}
    assert review_reasons(fine) == []                       # 40% above value: not flagged
    cheap = {"status": "PASS", "price": 50.0, "value": 100.0, "warnings": []}
    assert review_reasons(cheap) == []                      # a price drop alone is never a flag
    warned = {"status": "PASS", "price": 90.0, "value": 100.0, "warnings": ["revenue down 12% over the last 2 years"]}
    assert review_reasons(warned)[0]["text"].startswith("Revenue down")
    assert review_reasons({"status": "EXCLUDED"}) == []


def test_holdings_alert_only_for_new_flags_and_never_stores_tickers():
    s = "secret"
    cos = [{"ticker": "KO", "review": []},
           {"ticker": "MCD", "review": [{"kind": "quality", "text": "x"}]}]
    holdings = alerts.parse_holdings(" ko, mcd ;AAPL")
    assert holdings == ["KO", "MCD", "AAPL"]
    new, state = alerts.check_holdings(cos, holdings, set(), s)
    assert new and not any("MCD" in h for h in state)
    assert not alerts.check_holdings(cos, holdings, state, s)[0]       # same flag again: quiet
    cos[0]["review"] = [{"kind": "overvalued", "text": "y"}]
    assert alerts.check_holdings(cos, holdings, state, s)[0]            # KO newly flagged
    cos[1]["review"] = []
    new, state2 = alerts.check_holdings(cos, ["MCD"], state, s)
    assert not new and state2 == set()                                  # flag cleared


def test_page_data_carries_review_flags():
    v = {"ticker": "AAA", "name": "A", "sector": "X", "price": 200.0, "value_per_share": 100.0,
         "buy_below": 75.0, "discount": -1.0, "to_buy_price": 0.6, "signal": False, "warnings": []}
    co = {"status": "PASS", "checks": [], "annual": []}
    bad = {"name": "B", "sector": "X", "status": "FAIL",
           "checks": [{"name": "returns", "passed": False}], "annual": []}
    data = build_site.page_data([v], {"AAA": co, "BBB": bad}, {}, "x")
    by = {c["ticker"]: c for c in data["companies"]}
    assert by["AAA"]["review"][0]["kind"] == "overvalued"
    assert by["BBB"]["review"][0]["kind"] == "quality"
