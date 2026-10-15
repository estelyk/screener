"""Tests on synthetic EDGAR data, covering the traps real filings contain."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import annual_points, annual_table, merge_facts  # noqa: E402
from screen import screen_company  # noqa: E402


def flow(year, val, filed=None, days=364, form="10-K"):
    end = date(year, 12, 31)
    return {"start": (end - timedelta(days=days)).isoformat(), "end": end.isoformat(),
            "val": val, "form": form, "filed": filed or f"{year + 1}-02-15"}


def inst(year, val, filed=None):
    return {"end": date(year, 12, 31).isoformat(), "val": val, "form": "10-K",
            "filed": filed or f"{year + 1}-02-15"}


def facts(**tags):
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tags.items()}}}


YEARS = range(2016, 2026)


def good_company(**overrides):
    base = dict(
        Revenues=[flow(y, 1000 + 50 * i) for i, y in enumerate(YEARS)],
        NetIncomeLoss=[flow(y, 100 + 5 * i) for i, y in enumerate(YEARS)],
        OperatingIncomeLoss=[flow(y, 150 + 5 * i) for i, y in enumerate(YEARS)],
        NetCashProvidedByUsedInOperatingActivities=[flow(y, 150) for y in YEARS],
        PaymentsToAcquirePropertyPlantAndEquipment=[flow(y, 30) for y in YEARS],
        ShareBasedCompensation=[flow(y, 10) for y in YEARS],
        StockholdersEquity=[inst(y, 500) for y in YEARS],
        LongTermDebt=[inst(y, 200) for y in YEARS],
        CashAndCashEquivalentsAtCarryingValue=[inst(y, 100) for y in YEARS],
    )
    base.update(overrides)
    return facts(**base)


def test_restatement_latest_filing_wins():
    f = facts(NetIncomeLoss=[flow(2020, 100, "2021-02-01"), flow(2020, 90, "2022-02-01")])
    assert annual_points(f, "NetIncomeLoss", instant=False) == {"2020-12-31": 90.0}


def test_quarterly_and_10q_values_ignored():
    f = facts(NetIncomeLoss=[flow(2020, 100), flow(2020, 25, days=91), flow(2020, 99, form="10-Q")])
    assert annual_points(f, "NetIncomeLoss", instant=False) == {"2020-12-31": 100.0}


def test_tag_switch_gives_continuous_revenue():
    f = good_company(
        Revenues=[flow(y, 1000) for y in range(2016, 2018)],
        RevenueFromContractWithCustomerExcludingAssessedTax=[flow(y, 2000) for y in range(2018, 2026)],
    )
    rows, _ = annual_table(f)
    assert len(rows) == 10
    assert all(r["revenue"] is not None for r in rows)
    assert rows[0]["revenue"] == 1000 and rows[-1]["revenue"] == 2000


def test_quality_company_passes():
    rows, notes = annual_table(good_company())
    s = screen_company(rows)
    assert s["status"] == "PASS", s["checks"]
    assert s["returns_basis"] == "ROE"
    assert notes == []


def test_negative_equity_switches_to_roic():
    rows, _ = annual_table(good_company(StockholdersEquity=[inst(y, -200) for y in YEARS],
                                        LongTermDebt=[inst(y, 400) for y in YEARS],
                                        CashAndCashEquivalentsAtCarryingValue=[inst(y, 0) for y in YEARS]))
    s = screen_company(rows)
    assert s["returns_basis"] == "ROIC"
    assert s["status"] == "PASS", s["checks"]


def test_heavy_debt_fails():
    rows, _ = annual_table(good_company(LongTermDebt=[inst(y, 2000) for y in YEARS]))
    s = screen_company(rows)
    assert s["status"] == "FAIL" and "debt" in s["failed"]


def test_repeated_losses_fail():
    ni = [flow(y, -50 if y in (2018, 2021) else 120) for y in YEARS]
    s = screen_company(annual_table(good_company(NetIncomeLoss=ni))[0])
    assert "earnings" in s["failed"]


def test_short_history_is_no_data():
    f = good_company(NetIncomeLoss=[flow(y, 120) for y in range(2021, 2026)])
    s = screen_company(annual_table(f)[0])
    assert s["status"] == "NO_DATA"


def test_missing_debt_tag_treated_as_zero_with_note():
    f = good_company()
    del f["facts"]["us-gaap"]["LongTermDebt"]
    rows, notes = annual_table(f)
    assert rows[-1]["long_term_debt"] == 0.0
    assert any("debt" in n for n in notes)


def test_missing_debt_but_paying_interest_is_estimated_from_interest():
    f = good_company(InterestExpense=[flow(y, 40) for y in YEARS])
    del f["facts"]["us-gaap"]["LongTermDebt"]
    rows, notes = annual_table(f)
    assert rows[-1]["long_term_debt"] == 40 / 0.04 and rows[-1]["debt_estimated"]
    assert any("estimated from interest" in n for n in notes)
    s = screen_company(rows)
    assert "debt" in s["failed"]  # ~$1,000 of debt vs ~$140 earnings: over 4 years


def test_debt_found_under_alternate_tag():
    f = good_company(SeniorNotes=[inst(y, 250) for y in YEARS])
    del f["facts"]["us-gaap"]["LongTermDebt"]
    rows, _ = annual_table(f)
    assert rows[-1]["long_term_debt"] == 250


def test_no_operating_income_uses_pretax_plus_interest():
    f = good_company(
        StockholdersEquity=[inst(y, -200) for y in YEARS],
        IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest=[
            flow(y, 130) for y in YEARS],
        InterestExpense=[flow(y, 10) for y in YEARS],
        LongTermDebt=[inst(y, 400) for y in YEARS],
        CashAndCashEquivalentsAtCarryingValue=[inst(y, 0) for y in YEARS],
    )
    del f["facts"]["us-gaap"]["OperatingIncomeLoss"]
    rows, _ = annual_table(f)
    assert all(r["roic"] is not None for r in rows)
    assert abs(rows[0]["roic"] - 140 * 0.79 / 200) < 1e-9


def test_deeply_negative_equity_uses_tangible_capital():
    f = good_company(
        StockholdersEquity=[inst(y, -2000) for y in YEARS],
        PropertyPlantAndEquipmentNet=[inst(y, 300) for y in YEARS],
        AssetsCurrent=[inst(y, 500) for y in YEARS],
        LiabilitiesCurrent=[inst(y, 400) for y in YEARS],
    )
    rows, _ = annual_table(f)
    assert {r["roic_method"] for r in rows} == {"tangible capital"}
    s = screen_company(rows)
    assert s["returns_basis"] == "ROIC" and "returns" not in s["unjudged"]


def test_capex_estimated_when_label_missing():
    # Company-specific capex label for most years: only 2016-2017 in standard data.
    f = good_company(
        PaymentsToAcquirePropertyPlantAndEquipment=[flow(y, 30) for y in (2016, 2017)],
        PropertyPlantAndEquipmentNet=[inst(y, 100 + 10 * (y - 2015)) for y in range(2015, 2026)],
        Depreciation=[flow(y, 20) for y in YEARS],
    )
    f["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(flow(2015, 90))
    rows, notes = annual_table(f)
    est = [r for r in rows if r["capex_estimated"]]
    assert len(est) == 8 and all(r["capex"] == 30 for r in est)  # +10 PP&E + 20 depreciation
    assert any("estimated for 8" in n for n in notes)
    assert screen_company(rows)["status"] == "PASS"


def test_predecessor_cik_history_is_merged():
    old = good_company(NetIncomeLoss=[flow(y, 120) for y in range(2016, 2025)])
    new = facts(NetIncomeLoss=[flow(2025, 130, "2026-02-20")])
    merged = merge_facts([new, old, None])
    rows, _ = annual_table(merged)
    assert len(rows) == 10 and rows[-1]["net_income"] == 130


def test_small_but_real_interest_blocks_zero_debt_assumption():
    # Interest of 0.25% of revenue: debt exists even though it's small relative to sales.
    f = good_company(InterestExpense=[flow(y, 3.5) for y in YEARS])
    del f["facts"]["us-gaap"]["LongTermDebt"]
    rows, notes = annual_table(f)
    assert not rows[-1]["debt_assumed_zero"] and rows[-1]["long_term_debt"] > 0
    assert any("estimated from interest" in n for n in notes)


def test_debt_note_reflects_latest_year_only():
    # Debt missing in an old year but present now: no misleading note.
    debt = [inst(y, 200) for y in YEARS if y != 2017]
    rows, notes = annual_table(good_company(LongTermDebt=debt))
    assert rows[-1]["long_term_debt"] == 200
    assert not any("debt" in n.lower() for n in notes)
