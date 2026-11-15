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
    assert 'href="smallcap/"' in t and "showSmallcapLink" in t


def test_workflows_wired(monkeypatch):
    daily = (ROOT / ".github/workflows/daily.yml").read_text()
    weekly = (ROOT / ".github/workflows/smallcap.yml").read_text()
    assert "smallcap-site-" in daily and "smallcap-site-" in weekly
    # the small-cap steps in the daily job must never block the main page
    block = daily.split("Get latest small-cap page")[1].split("configure-pages")[0]
    assert block.count("continue-on-error: true") == 2
    assert '"locked":1' in weekly


def test_runner_keeps_last_page_when_run_is_nearly_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    site = tmp_path / "site"

    def write(d):
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text(run_smallcap.render(d))
        return site / "index.html"
    monkeypatch.setattr(run_smallcap, "write_page", write)

    monkeypatch.setattr(run_smallcap.S, "main", lambda argv: ROWS[:2])
    assert run_smallcap.main() == 1 and not site.exists()          # too few: keep last week's page
    monkeypatch.setattr(run_smallcap.S, "main",
                        lambda argv: [dict(ROWS[0], ticker=f"T{i}") for i in range(25)])
    assert run_smallcap.main() == 0 and (site / "index.html").exists()
