"""Small-cap (moonshot) screen: scoring, red flags, filing parsing, the locked page,
and quiet logs. No network: SEC and Yahoo are replaced with fake data."""
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import smallcap_screener as S  # noqa: E402
import run_smallcap  # noqa: E402

TODAY = dt.date.today()


# ---------- fake XBRL helpers ----------
def fy(val, y, form="10-K"):
    return {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": val, "form": form,
            "filed": f"{y + 1}-03-01", "accn": f"a{y}"}


def inst(val, d, form="10-Q"):
    return {"end": d, "val": val, "form": form, "filed": d, "accn": "x" + d}


def facts(rev, ni, ocf, cash, shares, debt=10e6, eq=100e6, ca=60e6, cl=20e6):
    return {"facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [fy(v, 2021 + i) for i, v in enumerate(rev[:2])]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax":
                {"units": {"USD": [fy(v, 2021 + i) for i, v in enumerate(rev)]}},
            "NetIncomeLoss": {"units": {"USD": [fy(ni, 2024)]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [fy(ocf, 2024)]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [inst(cash, "2025-06-30")]}},
            "LongTermDebt": {"units": {"USD": [inst(debt, "2025-06-30")]}},
            "StockholdersEquity": {"units": {"USD": [inst(eq, "2025-06-30")]}},
            "AssetsCurrent": {"units": {"USD": [inst(ca, "2025-06-30")]}},
            "LiabilitiesCurrent": {"units": {"USD": [inst(cl, "2025-06-30")]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding":
                {"units": {"shares": [inst(v, d) for d, v in shares.items()]}}}}}


BASE = dict(foreign=False, drop_3m=0.0, discontinued_ops=False, going_concern=False,
            runway_months=None, share_change_pct=0, share_change_years=3, reverse_split=False,
            revenue=40e6, rev_growth_pct=80, net_income=-10e6, net_income_prev=-15e6,
            gross_margin=0.6, ocf=-5e6, insider_net=0, market_cap=300e6, offerings_12m=0,
            equity=50e6, acquisition_growth=False, acq_spend_2y=0)


# ---------- metrics ----------
def test_metrics_pick_newest_revenue_tag_and_growth():
    m = S.compute_metrics(facts([80e6, 90e6, 100e6, 115e6], 8e6, 12e6, 30e6,
                                {"2022-07-01": 50e6, "2025-07-01": 49e6}), 5.0)
    assert m["revenue"] == 115e6 and round(m["rev_growth_pct"], 1) == 15.0
    assert round(m["current_ratio"], 1) == 3.0 and round(m["debt_to_equity"], 2) == 0.1


def test_market_cap_uses_all_share_classes():
    f = facts([500e6, 567e6], 78e6, 100e6, 50e6, {"2023-07-01": 6.9e6, "2025-07-01": 6.9e6})
    f["facts"]["us-gaap"]["WeightedAverageNumberOfSharesOutstandingBasic"] = \
        {"units": {"shares": [fy(600e6, 2024)]}}
    assert S.compute_metrics(f, 10.0)["market_cap"] == 6e9


def test_dilution_and_reverse_split_detected():
    m = S.compute_metrics(facts([6e6, 7e6], -20e6, -24e6, 10e6,
                                {"2022-07-01": 10e6, "2023-07-01": 80e6, "2024-07-01": 4e6,
                                 "2025-07-01": 30e6}), 2.0)
    assert m["reverse_split"] and m["share_change_pct"] > 150


def test_spinoff_detected_from_discontinued_operations():
    recent = (TODAY - dt.timedelta(days=45)).isoformat()
    f = facts([16e9, 17.4e9], 1.09e9, 2e9, 3e9, {"2023-07-01": 700e6, "2025-07-01": 680e6})
    f["facts"]["us-gaap"]["IncomeLossFromDiscontinuedOperationsNetOfTax"] = {"units": {"USD": [
        {"start": "2026-07-01", "end": recent, "val": 250e6, "form": "10-Q", "filed": recent,
         "accn": "q"}]}}
    m = S.compute_metrics(f, 9.43)
    m.update(foreign=False, drop_3m=0.05, going_concern=False, insider_net=0, offerings_12m=0)
    fl, p, n = S.score_moonshot(m)
    assert S.status_for(m, fl, p, "moonshot") == "CHECK"


def test_acquisition_growth_flagged():
    f = facts([17e6, 161e6], -34e6, -13e6, 68e6, {"2023-07-01": 240e6, "2025-07-01": 247e6})
    f["facts"]["us-gaap"]["PaymentsToAcquireBusinessesNetOfCashAcquired"] = \
        {"units": {"USD": [fy(210e6, 2021), fy(0, 2022)]}}
    m = S.compute_metrics(f, 1.83)
    assert m["acquisition_growth"]
    m.update(foreign=False, drop_3m=0, going_concern=False, insider_net=0, offerings_12m=0)
    assert any("acquisitions" in n for n in S.score_moonshot(m)[2])


# ---------- moonshot scoring ----------
def test_fast_grower_passes():
    fl, p, n = S.score_moonshot(dict(BASE, runway_months=30, insider_net=1e5))
    assert not fl and S.status_for(BASE, fl, p, "moonshot") == "PASS"


def test_slow_profitable_company_is_weak_not_pass():
    m = dict(BASE, rev_growth_pct=3, net_income=84e6, ocf=446e6, insider_net=1)
    fl, p, n = S.score_moonshot(m)
    assert p >= 5 and S.status_for(m, fl, p, "moonshot") == "WEAK"


def test_amazon_2001_style_survives_moonshot_but_fails_value():
    m = dict(BASE, revenue=3.12e9, rev_growth_pct=13, net_income=-567e6, net_income_prev=-1411e6,
             ocf=-120e6, runway_months=60, equity=-1.44e9, market_cap=2.5e9)
    assert S.score(m)[0] and not S.score_moonshot(m)[0]


@pytest.mark.parametrize("change", [
    {"going_concern": True}, {"runway_months": 5}, {"share_change_pct": 273},
    {"reverse_split": True}, {"revenue": 0.5e6}])
def test_fatal_flags_fail(change):
    m = dict(BASE, **change)
    fl, p, n = S.score_moonshot(m)
    assert fl and S.status_for(m, fl, p, "moonshot") == "FAIL"


# ---------- filings ----------
def test_going_concern_ignores_hypotheticals():
    assert S.has_going_concern("<p>These conditions raise substantial doubt about the "
                               "Company's ability to continue as a going concern.</p>")
    assert not S.has_going_concern("<p>If we fail to raise capital, this could raise substantial "
                                   "doubt about our ability to continue as a going concern.</p>")


def test_form4_buys_and_sells():
    x = """<ownershipDocument><nonDerivativeTable>
    <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>1000</value></transactionShares>
    <transactionPricePerShare><value>4.50</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>200</value></transactionShares>
    <transactionPricePerShare><value>5</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""
    assert S.parse_form4(x) == (4500.0, 1000.0)
    assert S.parse_form4("not xml") == (0.0, 0.0)


# ---------- full run with fake SEC + Yahoo ----------
U = {"fields": ["cik", "name", "ticker", "exchange"], "data": [
    [1, "BigCo", "BIGX", "NYSE"], [2, "ForeignCo", "FRNX", "NYSE"], [3, "Small A", "SMAX", "Nasdaq"],
    [4, "Small B", "SMBX", "Nasdaq"], [5, "Small C", "SMCX", "Nasdaq"]]}
SHARES = {1: 5e9, 2: 40e6, 3: 50e6, 4: 60e6, 5: 70e6}


def _fake_get(self, url, cache_key, as_json=True, max_age=None, store=True):
    if "company_tickers_exchange" in url:
        return U
    if "companyfacts" in url:
        cik = int(url.split("CIK")[1][:10])
        return facts([20e6, 30e6], 1e6, 2e6, 20e6, {"2022-07-01": SHARES[cik], "2025-07-01": SHARES[cik]})
    if "submissions" in url:
        cik = int(url.split("CIK")[1][:10])
        return {"filings": {"recent": {"form": ["20-F" if cik == 2 else "10-K"],
                                       "filingDate": [str(TODAY - dt.timedelta(days=100))],
                                       "accessionNumber": [f"000{cik}-26-1"], "primaryDocument": ["d.htm"]}}}
    if url.endswith(".htm"):
        return "<p>Annual report. Nothing alarming.</p>"
    return None


@pytest.fixture
def fake_market(monkeypatch, tmp_path):
    monkeypatch.setattr(S.Sec, "get", _fake_get)
    monkeypatch.setattr(S, "fetch_prices", lambda tickers, cache: {
        t: {"price": 5.0, "avg_volume": v, "drop_3m": 0.1}
        for t, v in zip(["BIGX", "FRNX", "SMAX", "SMBX", "SMCX"], [9e6, 8e6, 3e6, 2e6, 1e6])})
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    return tmp_path


def test_full_run_keeps_only_small_us_filers(fake_market, monkeypatch, capsys):
    monkeypatch.setattr(S, "QUIET", True)
    out = fake_market
    rows = S.main(["--max-mcap", "500e6", "--max-candidates", "2",
                   "--out", str(out / "o"), "--cache", str(out / "c")])
    assert sorted(r["ticker"] for r in rows) == ["SMAX", "SMBX"]
    logs = capsys.readouterr().out
    for t in ("BIGX", "FRNX", "SMAX", "SMBX", "SMCX"):
        assert t not in logs, "quiet mode must not print tickers (public logs)"


def test_going_concern_result_cached_not_the_10k(fake_market, monkeypatch):
    monkeypatch.setattr(S, "LEAN_CACHE", True)
    out = fake_market
    S.main(["--max-mcap", "500e6", "--max-candidates", "3", "--out", str(out / "o"),
            "--cache", str(out / "c")])
    assert list((out / "c" / "gc").glob("*.json"))
    assert not (out / "c" / "10k").exists() and not (out / "c" / "facts").exists()


# ---------- the locked page ----------
ROWS = [dict(BASE, ticker="ROCK", name="Rocket </script><b>Co", price=4.0, status="PASS", score=9,
             red_flags="", notes="insiders net buying", tenk_url="https://www.sec.gov/x",
             cash=1e6, insider_buys=2e5, insider_sells=0.0, debt_to_equity=float("nan")),
        dict(BASE, ticker="DUDX", name="Dud Inc", price=1.5, status="FAIL", score=2,
             red_flags="going-concern warning; cash runway 4 mo", notes="", tenk_url="",
             cash=1e5, insider_buys=0.0, insider_sells=0.0, debt_to_equity=None),
        dict(BASE, ticker="FORX", name="Foreign", status="SKIP", score=0, red_flags="", notes="",
             tenk_url="", price=3.0, cash=None, insider_buys=0, insider_sells=0, debt_to_equity=None)]


def test_page_data_shape():
    d = run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6)
    assert [c["ticker"] for c in d["companies"]] == ["ROCK", "DUDX"]   # foreign left out
    assert d["counts"] == {"PASS": 1, "WEAK": 0, "CHECK": 0, "FAIL": 1}
    assert d["companies"][1]["flags"] == ["going-concern warning", "cash runway 4 mo"]
    assert d["companies"][0]["debt_to_equity"] is None                  # NaN cleaned for JSON
    json.dumps(d, allow_nan=False)
    assert "&lt;" not in d["rules"] and "<" in d["rules"]


def test_page_is_locked_and_opens_with_passphrase(monkeypatch):
    monkeypatch.chdir(ROOT)
    from lock import decrypt
    d = run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6)
    html = run_smallcap.render(d, "correct horse battery staple", "owner/screener")
    blob = json.loads(html.split('type="application/json">')[1].split("</script>")[0].replace("<\\/", "</"))
    assert blob["locked"] == 1
    assert "ROCK" not in html and "Rocket" not in html               # nothing readable
    assert json.loads(decrypt(blob, "correct horse battery staple"))["companies"][0]["ticker"] == "ROCK"
    # same salt as the main page, so one remembered unlock opens both
    import build_site
    main = build_site.render({"companies": []}, "correct horse battery staple", "owner/screener")
    main_blob = json.loads(main.split('<script id="data" type="application/json">')[1].split("</script>")[0])
    assert main_blob["salt"] == blob["salt"]


def test_unlocked_page_escapes_script_and_has_styles(monkeypatch):
    monkeypatch.chdir(ROOT)
    html = run_smallcap.render(run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6))
    assert "/*__STYLE__*/" not in html and "/*__DATA__*/" not in html
    assert "--accent" in html                                       # main page CSS copied in
    data_part = html.split('type="application/json">')[1].split("</script>")[0]
    assert "</script>" not in data_part and "<\\/script>" in data_part


def test_main_page_links_to_smallcaps(monkeypatch):
    monkeypatch.chdir(ROOT)
    t = (ROOT / "site_template.html").read_text()
    assert 'href="smallcap/"' in t and "showSmallcapLink" in t and "Small-Cap Growth" in t
    assert "moonshot" not in t.lower()                                  # respectable name on the app


def test_workflows_wired(monkeypatch):
    daily = (ROOT / ".github/workflows/daily.yml").read_text()
    weekly = (ROOT / ".github/workflows/smallcap.yml").read_text()
    assert "smallcap-site-" in daily and "smallcap-site-" in weekly
    # the small-cap steps in the daily job must never block the main page
    block = daily.split("Get latest small-cap page")[1].split("configure-pages")[0]
    assert block.count("continue-on-error: true") == block.count("- name:") + 1 == 3
    assert '"locked":1' in weekly


def test_runner_keeps_last_page_when_run_is_nearly_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    site = tmp_path / "site"

    def write(d):
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(run_smallcap.render(d))
        return site / "index.html"
    monkeypatch.setattr(run_smallcap, "write_page", write)
    monkeypatch.setattr(run_smallcap.smallcap_tracker, "run", lambda rows: {"started": "x"})
    monkeypatch.setattr(run_smallcap, "descriptions", lambda rows: {})   # no network in tests
    import smallcap_prices
    monkeypatch.setattr(smallcap_prices, "run", lambda *a, **k: 0)        # no network in tests

    monkeypatch.setattr(run_smallcap.S, "main", lambda argv: ROWS[:2])
    assert run_smallcap.main() == 1 and not site.exists()          # too few: keep last week's page
    monkeypatch.setattr(run_smallcap.S, "main",
                        lambda argv: [dict(ROWS[0], ticker=f"T{i}") for i in range(25)])
    assert run_smallcap.main() == 0 and (site / "index.html").exists()


# ---------- paper track record ----------
import pandas as pd  # noqa: E402
import smallcap_tracker as T  # noqa: E402


def _series(start, values):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D"))


WEEK1 = [dict(ROWS[0], ticker="ROCK", status="PASS", price=4.0),
         dict(ROWS[1], ticker="DUDX", status="FAIL", price=1.5),
         dict(ROWS[0], ticker="MEHX", status="WEAK", price=3.0)]


def test_record_adds_pass_and_fail_once():
    led = T.empty_ledger("2026-11-15")
    assert T.record(led, WEEK1, "2026-11-15") == {"PASS": 1, "FAIL": 1}     # WEAK not tracked
    assert T.record(led, WEEK1, "2026-11-22") == {"PASS": 0, "FAIL": 0}     # no duplicates
    week3 = [dict(WEEK1[1], status="PASS")]                                  # a FAIL later passes
    assert T.record(led, week3, "2026-11-29") == {"PASS": 1, "FAIL": 0}
    assert [(e["ticker"], e["group"], e["start"]) for e in led["entries"]] == [
        ("ROCK", "PASS", "2026-11-15"), ("DUDX", "FAIL", "2026-11-15"), ("DUDX", "PASS", "2026-11-29")]


def test_returns_and_summary_against_iwm():
    led = T.empty_ledger("2026-11-01")
    T.record(led, WEEK1[:2], "2026-11-02")
    hist = {"ROCK": _series("2026-11-01", [4, 4, 6, 12]),      # 3x from Nov 2
            "DUDX": _series("2026-11-01", [2, 1.5, 1.0, 0.6]),  # -60%
            "IWM": _series("2026-11-01", [200, 200, 205, 210])}  # +5%
    assert T.update_returns(led, hist) == 2
    s = T.summary(led)
    assert round(s["pass"]["ret"], 2) == 2.0 and round(s["pass"]["bench"], 2) == 0.05
    assert s["pass"]["beat"] == 1 and s["pass"]["up_2x"] == 1
    assert round(s["fail"]["ret"], 2) == -0.6 and s["fail"]["down_half"] == 1
    assert s["picks"][0]["ticker"] == "ROCK"
    # a delisted stock keeps its last measured return
    T.update_returns(led, {"IWM": hist["IWM"], "ROCK": hist["ROCK"]})
    assert round(T.summary(led)["fail"]["ret"], 2) == -0.6


def test_storage_round_trip_encrypted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    led = T.empty_ledger("2026-11-15")
    T.record(led, WEEK1, "2026-11-15")
    T.save(led, "pass words here")
    blob = (tmp_path / "smallcap_data/site/ledger.enc").read_text()
    assert "ROCK" not in blob and json.loads(blob)["locked"] == 1
    assert (tmp_path / "smallcap_data/cache/ledger.enc").exists()
    loaded = T.load("pass words here", url="https://x/ledger.enc", fetch=lambda u: (None, True))
    assert [e["ticker"] for e in loaded["entries"]] == ["ROCK", "DUDX"]


def test_load_prefers_newer_published_copy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from lock import encrypt
    old = T.empty_ledger("2026-11-01"); old["updated"] = "2026-11-08"
    new = T.empty_ledger("2026-11-01"); new["updated"] = "2026-11-15"
    T.record(new, WEEK1, "2026-11-15")
    T.save(old, "pw")
    blob = encrypt(json.dumps(new), "pw", "local")
    assert len(T.load("pw", url="u", fetch=lambda u: (blob, True))["entries"]) == 2


@pytest.mark.parametrize("problem", ["unreachable", "unreadable"])
def test_load_stops_rather_than_start_empty(monkeypatch, tmp_path, problem):
    monkeypatch.chdir(tmp_path)
    if problem == "unreachable":
        fetch = lambda u: (None, False)                     # noqa: E731  network error
    else:
        from lock import encrypt
        bad = encrypt("{}", "other passphrase", "local")
        fetch = lambda u: (bad, True)                       # noqa: E731
    with pytest.raises(SystemExit):
        T.load("pw", url="u", fetch=fetch)
    assert T.load("pw", url="u", fetch=lambda u: (None, True))["entries"] == []   # first week: fine


def test_page_shows_track_record_and_fail_label(monkeypatch):
    monkeypatch.chdir(ROOT)
    t = (ROOT / "smallcap_template.html").read_text()
    assert "function trackRecord" in t and "DATA.tracking" in t and "failtag" in t


# ---------- company page data ----------
import random  # noqa: E402


def _random_company(rng):
    pick = lambda *xs: rng.choice(xs)  # noqa: E731
    return dict(foreign=False, drop_3m=0.0, discontinued_ops=False,
                going_concern=pick(True, False, None), runway_months=pick(None, 3.0, 8.9, 9.0, 30.0),
                share_change_pct=pick(None, -10.0, 5.0, 20.0, 21.0, 149.0, 151.0), share_change_years=3.0,
                reverse_split=pick(True, False), revenue=pick(None, 0.5e6, 1e6, 40e6),
                rev_growth_pct=pick(None, -5.0, 20.0, 21.0, 50.0, 51.0, 300.0),
                net_income=pick(None, -10e6, 0.0, 5e6), net_income_prev=pick(None, -20e6, -5e6, 1e6),
                gross_margin=pick(None, 0.2, 0.4, 0.41), ocf=pick(None, -5e6, 0.0, 5e6),
                insider_net=pick(0, -1.0, 1.0), insider_buys=1.0, insider_sells=0.0,
                market_cap=pick(None, 30e6, 50e6, 1e9, 1.5e9), offerings_12m=pick(0, 1, 3),
                equity=pick(None, -1.0, 5e6), acquisition_growth=pick(True, False), acq_spend_2y=1e6,
                cash=1e6)


def test_score_breakdown_always_matches_the_score():
    rng = random.Random(7)
    for _ in range(3000):
        m = _random_company(rng)
        flags, pts, _ = S.score_moonshot(m)
        ex = S.explain_moonshot(m)
        total = sum(p["pts"] for p in ex["points"])
        assert max(0, min(10, total)) == pts, (m, ex)
        assert any(k["passed"] is False for k in ex["checks"]) == bool(flags), (m, flags, ex["checks"])


def test_history_for_charts():
    f = facts([10e6, 20e6, 30e6, 40e6, 50e6, 60e6, 70e6], -5e6, 2e6, 9e6,
              {"2020-07-01": 10e6, "2021-03-01": 11e6, "2021-09-01": 12e6, "2025-07-01": 15e6})
    m = S.compute_metrics(f, 3.0)
    assert [r["y"] for r in m["annual"]] == [2022, 2023, 2024, 2025, 2026, 2027]   # last 6 years
    assert m["annual"][-1]["revenue"] == 70e6
    assert m["shares_by_year"] == [{"y": 2020, "v": 10e6}, {"y": 2021, "v": 12e6}, {"y": 2025, "v": 15e6}]


def test_page_data_has_company_page_fields():
    r = dict(ROWS[0], annual=[{"y": 2025, "revenue": 1e6, "net_income": float("nan")}],
             shares_by_year=[{"y": 2025, "v": 1e6}], cik=320193, industry="Services-Prepackaged Software",
             ten_k_date="2026-02-01", gross_margin=0.5, offerings_12m=0)
    d = run_smallcap.page_data([r], "Nov 15, 2026", 20e6, 500e6, descs={"ROCK": "Makes rockets."})
    c = d["companies"][0]
    assert c["description"] == "Makes rockets." and c["industry"].startswith("Services")
    assert c["annual"][0]["net_income"] is None                       # NaN cleaned
    assert "CIK=320193" in c["all_filings"]
    assert len(c["explain"]["checks"]) == 5 and c["explain"]["points"]
    json.dumps(d, allow_nan=False)


def test_descriptions_cached_and_best_effort(monkeypatch, tmp_path):
    monkeypatch.setattr(run_smallcap, "DESC_CACHE", tmp_path / "descriptions.json")
    calls = []

    def fetch(t):
        calls.append(t)
        if t == "BADX":
            raise RuntimeError("Yahoo said no")
        return f"{t} makes things. It sells them. It is growing. A fourth sentence is dropped."
    rows = [{"ticker": "AAAX", "status": "FAIL"}, {"ticker": "BADX", "status": "PASS"},
            {"ticker": "CCCX", "status": "PASS"}]
    out = run_smallcap.descriptions(rows, fetch=fetch)
    assert set(out) == {"AAAX", "CCCX"} and "fourth" not in out["CCCX"]
    assert calls[:2] == ["BADX", "CCCX"]                              # PASS first
    calls.clear()
    run_smallcap.descriptions(rows, fetch=fetch)
    assert calls == []                                                # cached, failures retried later


def test_company_pages_are_linked():
    t = (ROOT / "smallcap_template.html").read_text()
    assert "function brief" in t and "hashchange" in t and 'href="#">‹ Small-Cap Growth' in t
    assert "function barChart" in t and "Shares outstanding" in t


# ---------- price charts ----------
import numpy as np  # noqa: E402
import smallcap_prices as P  # noqa: E402


def _fake_prices(years=8):
    days = pd.bdate_range(end="2026-11-13", periods=int(252 * years))
    daily = pd.Series(np.linspace(2.0, 6.0, len(days)), index=days)
    tz = "America/New_York"
    s1 = pd.date_range("2026-11-13 09:30", "2026-11-13 15:55", freq="5min", tz=tz)
    d1 = pd.Series(np.linspace(5.9, 6.0, len(s1)), index=s1)
    s5 = pd.date_range("2026-11-09 09:30", "2026-11-13 15:30", freq="30min", tz=tz)
    d5 = pd.Series(np.linspace(5.8, 6.0, len(s5)), index=s5)
    return daily, d1, d5


def test_price_ranges_like_robinhood():
    daily, d1, d5 = _fake_prices()
    r = P.build_ranges(daily, d1, d5)
    assert list(r) == ["1D", "1W", "1M", "1Y", "5Y", "ALL"]
    for k, v in r.items():
        assert len(v["t"]) == len(v["c"]) and len(v["c"]) <= P.MAX_POINTS + 1, k
        assert v["c"][-1] == 6.0 or k in ("1D", "1W"), k              # ends on the latest close
    assert r["1D"]["base"] == round(float(daily.iloc[-2]), 3)        # vs the previous close
    one_month_ago = daily.index[-1] - pd.DateOffset(months=1)
    assert r["1M"]["base"] == round(float(daily[daily.index <= one_month_ago].iloc[-1]), 3)
    assert r["ALL"]["c"][0] == 2.0                                   # starts at the beginning


def test_short_history_and_missing_intraday():
    daily, _, _ = _fake_prices(years=0.5)
    r = P.build_ranges(daily)
    assert "1D" not in r and "1W" not in r and {"1M", "1Y", "5Y", "ALL"} <= set(r)
    assert P.build_ranges(pd.Series([], dtype=float)) == {}


def test_price_files_locked_and_unguessable(tmp_path):
    key = P.page_key("pass words here", "owner/screener")
    other = P.page_key("different words", "owner/screener")
    name = P.file_name(key, "rock")
    assert name.endswith(".enc") and "ROCK" not in name.upper() and name == P.file_name(key, "ROCK")
    assert name != P.file_name(other, "ROCK")
    daily, d1, d5 = _fake_prices()
    n = P.write({"ROCK": P.build_ranges(daily, d1, d5)}, key, tmp_path)
    f = tmp_path / "px" / name
    assert n == 1 and f.exists() and "ROCK" not in f.read_text() and "6.0" not in f.read_text()
    assert P.unseal(f.read_text(), key)["ranges"]["1Y"]["c"][-1] == 6.0
    P.write({"NEWX": {"1M": {"t": [1], "c": [1.0], "base": None}}}, key, tmp_path)
    assert not f.exists()                                            # old files replaced, not piled up


def test_price_refresh_keeps_old_charts_when_yahoo_fails(tmp_path, capsys):
    (tmp_path / "px").mkdir()
    (tmp_path / "px" / "old.enc").write_text("x")
    n = P.run(["AAAX", "BBBX", "CCCX"], "pw", "l", tmp_path, fetcher=lambda ts: {})
    assert n == 0 and (tmp_path / "px" / "old.enc").exists()
    assert "AAAX" not in capsys.readouterr().out                     # logs are public


def test_price_refresh_reads_tickers_from_locked_page(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    d = run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6)
    (tmp_path / "index.html").write_text(run_smallcap.render(d, "pw words", "owner/screener"))
    assert P.tickers_from_page(tmp_path, "pw words") == ["ROCK", "DUDX"]


def test_page_and_workflow_wired_for_charts():
    t = (ROOT / "price_chart.js").read_text()
    assert "function loadPrices" in t and "function priceChart" in t and 'crypto.subtle.digest("SHA-256"' in t
    for r in ("1D", "1W", "1M", "1Y", "5Y", "ALL"):
        assert '"' + r + '"' in t
    daily = (ROOT / ".github/workflows/daily.yml").read_text()
    step = daily.split("Refresh small-cap price charts")[1].split("- name:")[0]
    assert "continue-on-error: true" in step and "timeout-minutes" in step and "smallcap_prices.py" in step


def test_daily_refresh_rebuilds_page_with_current_design(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    d = run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6)
    old = run_smallcap.render(d, "pw words", "owner/screener").replace("function priceChart", "function oldDesign")
    (tmp_path / "index.html").write_text(old)
    P.rebuild_page(tmp_path, "pw words", "owner/screener")
    new = (tmp_path / "index.html").read_text()
    assert "function priceChart" in new and "oldDesign" not in new
    assert P.tickers_from_page(tmp_path, "pw words") == ["ROCK", "DUDX"]   # same data, still locked
    assert "ROCK" not in new


def test_value_page_gets_the_same_chart(monkeypatch, tmp_path):
    """The Value Screener page: chart code pasted in, price files keyed like small caps,
    and its own KEY (My budget settings) left alone."""
    monkeypatch.chdir(ROOT)
    import build_site
    main = build_site.render({"generated": "Nov 16, 2026", "settings": {},
                              "companies": [{"ticker": "KO", "name": "Coca-Cola"}]}, "pw words", "owner/screener")
    small = run_smallcap.render(run_smallcap.page_data(ROWS, "Nov 15, 2026", 20e6, 500e6), "pw words", "owner/screener")
    for page in (main, small):
        assert "/*__PRICE_CHART__*/" not in page and "function startPriceChart" in page
        assert page.count("var PAGE_KEY = null") == 1
    assert 'var KEY = "valueScreener.sizing.v1"' in main                # untouched
    (tmp_path / "index.html").write_text(main)
    assert P.tickers_from_page(tmp_path, "pw words") == ["KO"]
    daily = (ROOT / ".github/workflows/daily.yml").read_text()
    step = daily.split("Value page price charts")[1].split("- name:")[0]
    assert "continue-on-error: true" in step and "--site site --no-rebuild" in step
    # it runs after the main page is built and checked as encrypted, before publishing
    assert daily.index("Make sure the page is encrypted") < daily.index("Value page price charts") < daily.index("upload-pages-artifact")


def test_daily_value_chart_command_end_to_end(monkeypatch, tmp_path):
    """`python smallcap_prices.py --site site --no-rebuild`: writes one locked file per
    company and leaves the Value Screener page itself byte-for-byte unchanged."""
    monkeypatch.chdir(ROOT)
    import build_site
    page = build_site.render({"generated": "Nov 16, 2026", "settings": {},
                              "companies": [{"ticker": t, "name": t} for t in ("KO", "AAPL")]},
                             "pw words", "owner/screener")
    (tmp_path / "index.html").write_text(page)
    monkeypatch.setenv("PAGE_PASSPHRASE", "pw words"); monkeypatch.setenv("PAGE_SALT", "owner/screener")
    fake = lambda tickers: {t: {"1Y": [[1, 10.0], [2, 11.0]]} for t in tickers}
    monkeypatch.setattr(P.run, "__defaults__", (P.SITE, fake))
    assert P.main(["--site", str(tmp_path), "--no-rebuild"]) == 0
    key = P.page_key("pw words", "owner/screener")
    files = sorted(p.name for p in (tmp_path / "px").iterdir())
    assert files == sorted(P.file_name(key, t) for t in ("KO", "AAPL"))
    assert (tmp_path / "index.html").read_text() == page


def test_failed_download_keeps_previous_chart(tmp_path):
    key = P.page_key("pw words", "owner/screener")
    P.write({"CURI": {"1Y": [[1, 3.0]]}, "TSSI": {"1Y": [[1, 7.0]]}}, key, tmp_path)
    # next run: Yahoo returns TSSI only; CURI is still on the page, DROP is not
    P.write({"TSSI": {"1Y": [[2, 7.5]]}}, key, tmp_path, keep=["TSSI", "CURI"])
    names = {p.name for p in (tmp_path / "px").iterdir()}
    assert names == {P.file_name(key, "CURI"), P.file_name(key, "TSSI")}
    assert P.unseal((tmp_path / "px" / P.file_name(key, "CURI")).read_text(), key)["ranges"]["1Y"] == [[1, 3.0]]
    assert P.unseal((tmp_path / "px" / P.file_name(key, "TSSI")).read_text(), key)["ranges"]["1Y"] == [[2, 7.5]]
    # a company that left the page loses its file
    P.write({"TSSI": {"1Y": [[3, 8.0]]}}, key, tmp_path, keep=["TSSI"])
    assert {p.name for p in (tmp_path / "px").iterdir()} == {P.file_name(key, "TSSI")}


def test_fetch_retries_missed_tickers(monkeypatch):
    import sys, types
    calls = []
    def download(batch, **kw):
        calls.append((tuple(batch), kw["interval"]))
        raise RuntimeError("yahoo busy") if len(calls) <= 3 else RuntimeError("still busy")
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert P.fetch(["CURI", "TSSI"]) == {}
    assert len(calls) == 6                          # first pass + one retry pass, no more
