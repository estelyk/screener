"""Phase 3 tests: filing links, page data, safe embedding, no private data in the page."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os  # noqa: E402
os.chdir(Path(__file__).resolve().parents[1])  # template and assets are relative paths

import build_site  # noqa: E402
from edgar import parse_filings  # noqa: E402
from valuation import warnings  # noqa: E402


def test_parse_filings_finds_latest_10k_and_earnings_8k():
    sub = {"sicDescription": "Retail-Apparel", "filings": {"recent": {
        "form": ["8-K", "8-K", "10-Q", "10-K", "10-K"],
        "accessionNumber": ["0001-26-000005", "0001-26-000004", "0001-26-000003",
                            "0001-26-000002", "0001-25-000001"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm", "lulu-10k.htm", "old.htm"],
        "filingDate": ["2026-10-01", "2026-09-04", "2026-09-05", "2026-03-26", "2025-03-27"],
        "items": ["5.07", "2.02,9.01", "", "", ""]}}}
    info = parse_filings(1397187, sub)
    assert info["ten_k"].endswith("/1397187/000126000002/lulu-10k.htm")
    assert info["ten_k_date"] == "2026-03-26"
    assert info["earnings"].endswith("0001-26-000004-index.htm")
    assert info["sic_description"] == "Retail-Apparel"


def _valuation(ticker, **kw):
    v = {"ticker": ticker, "name": f"{ticker} Inc </script><b>", "sector": "Health Care",
         "price": 50.0, "value_per_share": 100.0, "buy_below": 75.0, "discount": 0.5,
         "to_buy_price": None, "signal": True, "suspicious": False, "warnings": [],
         "verify": "", "value_note": "", "growth": 0.05, "normalized_fcf": 1e9,
         "net_debt": 0.0, "shares": 1e8}
    v.update(kw)
    return v


def _company(ticker, basis="ROIC"):
    return {"checks": [{"name": "returns", "passed": True, "detail": f"{basis} avg 30.0%"}],
            "annual": [{"fiscal_year_end": f"{2016 + i}-12-31", "revenue": 1e9, "net_income": 1e8,
                        "owner_fcf": 1e8, "long_term_debt": 0, "roe": 0.9, "roic": 0.3}
                       for i in range(10)]}


def test_page_data_uses_the_right_return_measure():
    data = build_site.page_data([_valuation("AAA")], {"AAA": _company("AAA", "ROIC")}, {}, "Oct 1, 2026")
    c = data["companies"][0]
    assert c["returns_basis"] == "ROIC" and c["annual"][0]["ret"] == 0.3 and c["annual"][0]["y"] == 2016


def test_rendered_page_embeds_data_safely(tmp_path):
    data = build_site.page_data([_valuation("AAA")], {"AAA": _company("AAA")}, {}, "Oct 1, 2026")
    page = build_site.write_site(data, tmp_path)
    html = page.read_text()
    # A company name containing </script> must not break out of the data block.
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert json.loads(m.group(1).replace("<\\/", "</"))["companies"][0]["name"].endswith("</script><b>")
    assert (tmp_path / "manifest.webmanifest").exists() and (tmp_path / "icon-180.png").exists()


def test_page_never_contains_private_portfolio(tmp_path, monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    secret = tmp_path / "my_portfolio.json"
    secret.write_text(json.dumps({"cash_reserve": 987654}))
    data = build_site.page_data([_valuation("AAA")], {"AAA": _company("AAA")}, {}, "Oct 1, 2026")
    html = build_site.write_site(data, tmp_path / "site").read_text()
    assert "987654" not in html and "cash_reserve" not in html


def test_cyclical_warning_uses_margins_not_growth():
    steady = [{"revenue": 100 * 1.2 ** i, "owner_fcf": 15 * 1.2 ** i} for i in range(10)]
    assert not any("margin" in w for w in warnings(steady))       # fast grower, stable margin
    boom = [{"revenue": 100, "owner_fcf": 8} for _ in range(7)] + [{"revenue": 120, "owner_fcf": 25}] * 3
    assert any("cyclical" in w for w in warnings(boom))
