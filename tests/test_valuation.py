"""Phase 2 tests: valuation math, growth caps, buy signals, sizing, ranking."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as C  # noqa: E402
from run_phase2 import build  # noqa: E402
from sizing import suggest  # noqa: E402
from valuation import dcf, estimate_growth, price_check, value_company  # noqa: E402


def rows(fcf=100.0, growth=0.05, n=10, shares=10.0, debt=0.0, cash=0.0, rev0=1000.0):
    out = []
    for i in range(n):
        out.append({"revenue": rev0 * (1 + growth) ** i, "owner_fcf": fcf * (1 + growth) ** i,
                    "diluted_shares": shares, "long_term_debt": debt, "cash": cash})
    return out


def test_dcf_matches_perpetuity_formula_when_growth_is_terminal_rate():
    # Constant growth g forever is worth fcf*(1+g)/(r-g): the textbook formula.
    g = C.TERMINAL_GROWTH
    assert dcf(100, g) == pytest.approx(100 * (1 + g) / (C.DISCOUNT_RATE - g))


def test_dcf_rises_with_growth():
    assert dcf(100, 0.0) < dcf(100, 0.05) < dcf(100, 0.10)


def test_growth_uses_lower_of_revenue_and_fcf_and_is_capped():
    r = rows(growth=0.30)
    g, why = estimate_growth(r)
    assert g == C.MAX_GROWTH and "limited" in why
    shrinking = rows(growth=-0.05)
    assert estimate_growth(shrinking)[0] == 0.0


def test_net_debt_subtracted_but_cash_gets_no_credit():
    base = value_company(rows())["value_per_share"]
    with_cash = value_company(rows(cash=500))["value_per_share"]
    with_debt = value_company(rows(debt=500))["value_per_share"]
    assert with_cash == pytest.approx(base)
    assert with_debt == pytest.approx(base - 500 / 10)


def test_negative_cash_flow_or_missing_shares_gives_no_value():
    assert value_company(rows(fcf=-10))["value_per_share"] is None
    r = rows()
    r[-1]["diluted_shares"] = None
    v = value_company(r)
    assert v["value_per_share"] is None and "share" in v["value_note"]


def test_buy_signal_needs_full_margin_of_safety():
    assert price_check(100, 74)["signal"] is True     # 26% below value
    assert price_check(100, 76)["signal"] is False    # only 24% below
    pc = price_check(100, 100)
    assert pc["buy_below"] == 75 and pc["to_buy_price"] == pytest.approx(0.25)


def test_sizing_respects_cap_slots_and_holdings():
    sigs = [{"ticker": "AAA", "discount": 0.40}, {"ticker": "BBB", "discount": 0.30},
            {"ticker": "CCC", "discount": 0.35}, {"ticker": "DDD", "discount": 0.50}]
    pf = {"cash_reserve": 10000, "max_position_pct": 0.2, "max_holdings": 3, "holdings": ["DDD"]}
    out = {s["ticker"]: s for s in suggest(sigs, pf)}
    assert out["DDD"]["sizing_note"] == "already held"
    assert out["AAA"]["suggested_amount"] == 2000 and out["CCC"]["suggested_amount"] == 2000
    assert out["BBB"]["sizing_note"] == "max holdings reached"  # only 2 open slots


def test_sizing_stops_when_cash_runs_out():
    sigs = [{"ticker": t, "discount": 0.3} for t in "ABC"]
    pf = {"cash_reserve": 1000, "max_position_pct": 0.5, "max_holdings": 10}
    amounts = [s["suggested_amount"] for s in suggest(sigs, pf)]
    assert amounts == [500, 500, 0]


def test_build_ranks_signals_then_watchlist():
    cos = [{"ticker": t, "name": t, "sector": "X", "notes": [], "annual": rows()} for t in "ABCD"]
    value = value_company(rows())["value_per_share"]
    prices = {"A": value * 0.9, "B": value * 0.6, "C": value * 1.5}  # D has no price
    out = build(cos, prices)
    assert [r["ticker"] for r in out] == ["B", "A", "C", "D"]
    assert out[0]["signal"] and not out[1]["signal"]
    assert "no price" in out[3]["value_note"]


def test_split_after_annual_report_adjusts_value_per_share():
    cos = [{"ticker": "BKNG", "name": "B", "sector": "X", "notes": [],
            "annual": [dict(r, fiscal_year_end=f"{2016 + i}-12-31") for i, r in enumerate(rows())]}]
    pre = value_company(rows())["value_per_share"]
    splits = {"BKNG": [("2024-06-01", 3.0), ("2026-06-08", 25.0)]}  # only the 2026 one is after FY2025
    out = build(cos, {"BKNG": pre / 25}, splits)[0]
    assert out["value_per_share"] == pytest.approx(pre / 25)
    assert not out["signal"] and "25-for-1" in out["value_note"]


def test_recent_slowdown_lowers_growth():
    r = rows(growth=0.10)
    for x in r[-3:]:
        x["revenue"] = r[-4]["revenue"]  # flat for the last 3 years
    g, why = estimate_growth(r)
    assert g == 0.0 and "recent revenue" in why


def test_warnings_flag_cheap_for_a_reason():
    r = rows(growth=0.05)
    r[-1]["revenue"] = r[-3]["revenue"] * 0.8
    for x in r:
        x["net_income"] = 100
    r[-1]["net_income"] = 50
    w = value_company(r)["warnings"]
    assert any("revenue down" in s for s in w) and any("below their 10-year peak" in s for s in w)


def test_huge_discount_is_marked_suspicious():
    assert price_check(100, 30)["suspicious"] is True
    assert price_check(100, 70)["suspicious"] is False


def test_market_data_reads_prices_and_splits(monkeypatch):
    import pandas as pd
    import yfinance as yf
    import prices
    idx = pd.to_datetime(["2026-06-05", "2026-06-08", "2026-10-13"])
    cols = pd.MultiIndex.from_product([["Close", "Stock Splits"], ["BKNG", "KO"]])
    df = pd.DataFrame([[4000, 70, 0, 0], [160, 71, 25, 0], [157.5, 72, 0, 0]], index=idx, columns=cols)
    monkeypatch.setattr(yf, "download", lambda *a, **k: df)
    p, s = prices.market_data(["BKNG", "KO"])
    assert p == {"BKNG": 157.5, "KO": 72.0}
    assert s == {"BKNG": [("2026-06-08", 25.0)]}


def test_split_already_in_annual_report_is_not_double_counted():
    r = [dict(x, fiscal_year_end=f"{2016 + i}-12-31") for i, x in enumerate(rows())]
    r[-1]["shares_filed"] = "2026-02-20"           # report filed after a Feb 1 split
    cos = [{"ticker": "X", "name": "X", "sector": "X", "notes": [], "annual": r}]
    out = build(cos, {"X": 1.0}, {"X": [("2026-02-01", 4.0)]})[0]
    assert out["value_per_share"] == pytest.approx(value_company(rows())["value_per_share"])


def test_shares_filed_date_recorded():
    from test_pipeline import flow, good_company
    from metrics import annual_table
    f = good_company()
    f["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": [flow(2025, 50, "2026-02-14"), flow(2025, 49, "2026-03-01")]}}
    rows_, _ = annual_table(f)
    assert rows_[-1]["diluted_shares"] == 49 and rows_[-1]["shares_filed"] == "2026-03-01"
