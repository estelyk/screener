"""Phase 5 tests: recording signals, paper returns vs benchmarks, safe storage."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tracker  # noqa: E402


def sig(t, price=50.0, signal=True, **kw):
    return {"ticker": t, "name": t, "sector": "X", "price": price, "value_per_share": 100.0,
            "buy_below": 75.0, "discount": 0.5, "signal": signal, "warnings": [], "verify": "", **kw}


def test_every_signal_is_recorded_once_with_what_the_model_said():
    led = tracker.empty_ledger("2026-10-16")
    assert tracker.record(led, [sig("KO", warnings=["w"], verify="VERIFY debt"), sig("PEP", signal=False)],
                          "2026-10-16", "v1") == 1
    e = led["entries"][0]
    assert (e["ticker"], e["price"], e["value"], e["model"], e["verify"]) == ("KO", 50.0, 100.0, "v1", True)
    assert e["warnings"] == ["w"]
    assert tracker.record(led, [sig("KO", price=40)], "2026-10-19", "v1") == 0   # same signal: no duplicate
    assert led["entries"][0]["price"] == 50.0 and led["entries"][0]["last_seen"] == "2026-10-19"


def test_ended_signals_stay_tracked_and_a_quick_return_continues_the_entry():
    led = tracker.empty_ledger("2026-10-01")
    tracker.record(led, [sig("KO")], "2026-10-01", "v1")
    tracker.record(led, [], "2026-10-02", "v1")
    assert led["entries"][0]["active"] is False and led["entries"][0]["ended"] == "2026-10-02"
    tracker.record(led, [sig("KO")], "2026-10-20", "v1")          # back within 30 days
    assert len(led["entries"]) == 1 and led["entries"][0]["active"]
    tracker.record(led, [], "2026-10-21", "v1")
    tracker.record(led, [sig("KO")], "2027-01-15", "v1")          # months later: a new entry
    assert len(led["entries"]) == 2


def series(vals, start="2026-10-14"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)))


def test_returns_compare_each_signal_with_benchmarks_over_the_same_days():
    led = tracker.empty_ledger("2026-10-16")
    tracker.record(led, [sig("KO"), sig("BF.B")], "2026-10-16", "v1")
    hist = {"KO": series([9, 9, 10, 11, 12]),          # signal day close 10 -> 12 = +20%
            "SPY": series([100, 100, 100, 105, 110]),  # +10% over the same days
            "VTV": series([50, 50, 50, 50, 55])}       # +10%
    tracker.update_returns(led, hist)
    ko, bf = led["entries"]
    assert ko["ret"] == pytest.approx(0.20) and ko["spy"] == pytest.approx(0.10)
    assert ko["vtv"] == pytest.approx(0.10) and ko["as_of"] == "2026-10-20"
    assert bf["ret"] is None                            # no price data: left empty, not zero
    s = tracker.summary(led, ["KO"])
    assert s["all"]["measured"] == 1 and s["all"]["beat_spy"] == 1
    assert s["held"]["n"] == 1


def test_missing_data_keeps_the_last_known_return():
    led = tracker.empty_ledger("2026-10-16")
    tracker.record(led, [sig("KO")], "2026-10-16", "v1")
    led["entries"][0]["ret"] = 0.05
    tracker.update_returns(led, {"SPY": series([1, 1, 1])})
    assert led["entries"][0]["ret"] == 0.05


def test_signal_on_a_holiday_uses_the_previous_close():
    led = tracker.empty_ledger("2026-10-18")
    tracker.record(led, [sig("KO")], "2026-10-18", "v1")          # a Sunday
    tracker.update_returns(led, {"KO": series([10, 10, 10, 12])})  # Fri 16th close = 10
    assert led["entries"][0]["ret"] == pytest.approx(0.20)


# ---------------- storage ----------------
@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tracker, "STATE", tmp_path / "state")
    monkeypatch.setattr(tracker, "SITE", tmp_path / "site")
    return tmp_path


def test_encrypted_record_round_trips_and_never_contains_tickers(in_tmp):
    led = tracker.empty_ledger("2026-10-16")
    tracker.record(led, [sig("KO")], "2026-10-16", "v1")
    tracker.save(led, "pass phrase")
    for p in (in_tmp / "state" / "ledger.enc", in_tmp / "site" / "ledger.enc"):
        assert "KO" not in p.read_text()
    got = tracker.load("pass phrase", url="https://x/ledger.enc", fetch=lambda u: (None, True))
    assert got["entries"][0]["ticker"] == "KO"


def test_newer_copy_wins(in_tmp):
    from lock import encrypt
    old = tracker.empty_ledger("2026-10-16"); old["updated"] = "2026-10-16"
    tracker.save(old, "p")
    new = tracker.empty_ledger("2026-10-16"); new["updated"] = "2026-10-20"
    tracker.record(new, [sig("KO")], "2026-10-20", "v1")
    blob = encrypt(json.dumps(new), "p", "x")
    assert len(tracker.load("p", url="u", fetch=lambda u: (blob, True))["entries"]) == 1


def test_never_starts_over_when_the_record_cant_be_read(in_tmp):
    with pytest.raises(SystemExit, match="paper-tracking record"):
        tracker.load("p", url="u", fetch=lambda u: (None, False))   # network failure
    assert tracker.load("p", url="u", fetch=lambda u: (None, True))["entries"] == []   # first run


def test_local_runs_use_plain_file(in_tmp):
    out = tracker.run([sig("KO")], ["KO"], history={})
    assert (in_tmp / "state" / "ledger.json").exists()
    assert out["entries"][0]["ticker"] == "KO" and out["summary"]["all"]["n"] == 1


def test_page_includes_track_record():
    import build_site
    data = build_site.page_data([], {}, {}, "x")
    data["tracking"] = {"summary": {"started": "2026-10-16", "model": "v1"}, "entries": []}
    html = build_site.render(data)
    assert "Track record" in Path(build_site.TEMPLATE).read_text() and '"tracking"' in html
