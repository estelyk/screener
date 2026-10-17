"""Quarterly early warning: this year so far vs the same months a year earlier."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quarterly import quarterly_update, warnings  # noqa: E402


def fact(start, end, val, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def facts(rev_cur=900, rev_prev=1000, ocf_cur=150, ocf_prev=300, capex=(50, 50), ni=(80, 100)):
    """Fiscal year ends Feb 1, 2026; 10-Q for the 6 months to Aug 2, 2026 (52-week calendar)."""
    def pair(cur, prev):
        return [fact("2026-02-02", "2026-08-02", cur, "2026-09-05"),
                fact("2025-02-03", "2025-08-03", prev, "2026-09-05"),
                fact("2026-05-04", "2026-08-02", cur / 2, "2026-09-05"),          # quarter only: ignored
                fact("2025-02-03", "2026-02-01", 9999, "2026-03-20", "10-K")]       # annual: ignored
    g = {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": pair(rev_cur, rev_prev)}},
         "NetIncomeLoss": {"units": {"USD": pair(*ni)}},
         "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": pair(ocf_cur, ocf_prev)}},
         "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": pair(*capex)}}}
    return {"facts": {"us-gaap": g}}


ROWS = [{"fiscal_year_end": "2026-02-01", "owner_fcf": 800.0}]


def test_compares_year_to_date_with_same_period_last_year():
    q = quarterly_update(facts(), ROWS)
    assert q["through"] == "2026-08-02" and q["months"] == 6
    assert q["items"]["revenue"]["change"] == pytest.approx(-0.10)
    o = q["owner_fcf"]
    assert (o["cur"], o["prev"]) == (100, 250)
    assert o["last12"] == pytest.approx(800 + 100 - 250)


def test_warnings_for_falling_sales_profit_and_cash():
    w = " | ".join(warnings(quarterly_update(facts(), ROWS)))
    assert "sales down 10%" in w and "owner cash flow down 60%" in w and "6 months to 2026-08-02" in w
    assert "profit down 20%" not in w                    # 20% is under the 25% threshold


def test_no_warning_when_the_year_is_going_well():
    assert warnings(quarterly_update(facts(rev_cur=1100, ocf_cur=330, ni=(120, 100)), ROWS)) == []


def test_restated_figure_wins():
    f = facts()
    f["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"].append(
        fact("2025-02-03", "2025-08-03", 950, "2026-10-01"))
    assert quarterly_update(f, ROWS)["items"]["revenue"]["prev"] == 950


def test_nothing_when_no_quarter_since_annual_report():
    assert quarterly_update(facts(), [{"fiscal_year_end": "2026-09-30", "owner_fcf": 1}]) is None
    assert quarterly_update(None, ROWS) is None


def test_quarterly_warnings_reach_valuation():
    from run_phase2 import build
    from test_valuation import rows
    r = [dict(x, fiscal_year_end=f"{2016 + i}-12-31") for i, x in enumerate(rows())]
    q = quarterly_update(facts(), ROWS)
    out = build([{"ticker": "X", "name": "X", "sector": "S", "notes": [], "annual": r, "quarterly": q}], {"X": 1.0})[0]
    assert any("latest quarterly report" in w for w in out["warnings"])
