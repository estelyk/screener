"""F-score and Z-score: classic 'getting worse?' checks, shown as warnings only."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riskscores import f_warning, fscore, z_warning, zscore  # noqa: E402


def yr(ni, cfo, assets, debt, ca, cl, op, rev, shares, re=500, tl=400):
    return {"net_income": ni, "operating_cash_flow": cfo, "total_assets": assets, "long_term_debt": debt,
            "current_assets": ca, "current_liabilities": cl, "operating_income": op, "revenue": rev,
            "diluted_shares": shares, "retained_earnings": re, "total_liabilities": tl}


def test_improving_business_scores_nine():
    rows = [yr(80, 90, 1000, 300, 300, 200, 100, 1000, 100),
            yr(100, 120, 1000, 250, 320, 200, 130, 1100, 99),
            yr(130, 150, 1000, 200, 350, 200, 160, 1200, 98)]
    f = fscore(rows)
    assert (f["score"], f["out_of"]) == (9, 9) and f_warning(f) is None


def test_deteriorating_business_scores_low_and_warns():
    rows = [yr(130, 150, 1000, 200, 350, 200, 160, 1200, 98),
            yr(100, 120, 1000, 250, 320, 200, 130, 1100, 99),
            yr(-20, 10, 1100, 400, 280, 260, 40, 1050, 105)]
    f = fscore(rows)
    assert f["score"] <= 2 and "weakening" in f_warning(f)


def test_stock_split_is_not_counted_as_issuing_shares():
    rows = [yr(80, 90, 1000, 300, 300, 200, 100, 1000, 100),
            yr(100, 120, 1000, 250, 320, 200, 130, 1100, 100),
            yr(130, 150, 1000, 200, 350, 200, 160, 1200, 1000)]   # 10-for-1 split
    assert dict(fscore(rows)["checks"])["Didn't issue new shares"] is True


def test_too_little_data_gives_no_score():
    assert fscore([{"net_income": 1}] * 3) is None
    assert fscore([]) is None


def test_zscore_formula_and_zones():
    r = [yr(100, 120, 1000, 200, 400, 200, 150, 1200, 100, re=300, tl=500)]
    # 1.2*0.2 + 1.4*0.3 + 3.3*0.15 + 0.6*(2000/500) + 1.0*1.2
    assert zscore(r, 2000) == pytest.approx(0.24 + 0.42 + 0.495 + 2.4 + 1.2)
    weak = [yr(-50, 0, 1000, 600, 150, 300, -20, 500, 100, re=50, tl=900)]
    z = zscore(weak, 100)
    assert z < 1.81 and "distress" in z_warning(z, weak)


def test_buyback_company_low_z_is_not_a_warning():
    rows = [yr(100, 120, 1000, 600, 200, 300, 150, 900, 100, re=-400, tl=1200)]
    z = zscore(rows, 300)
    assert z < 1.81 and z_warning(z, rows) is None


def test_missing_balance_sheet_gives_no_z():
    assert zscore([{"revenue": 1}], 100) is None
    assert zscore([yr(1, 1, 1000, 0, 1, 1, 1, 1, 1)], None) is None


def test_scores_reach_the_valuation_and_page():
    from run_phase2 import build
    rows = [dict(yr(100 + i, 120 + i, 1000, 200, 350, 200, 150, 1000 * 1.05 ** i, 10, re=300, tl=500),
                 owner_fcf=100 * 1.05 ** i, fiscal_year_end=f"{2016 + i}-12-31") for i in range(10)]
    out = build([{"ticker": "X", "name": "X", "sector": "S", "notes": [], "annual": rows}], {"X": 50.0})[0]
    assert out["fscore"]["out_of"] == 9 and out["zscore"] is not None
