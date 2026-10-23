"""Turn raw company-facts JSON into a clean annual table (one row per fiscal year).

The hard parts this handles:
- Companies label the same number with different XBRL tags, and switch tags over
  time. Each metric has a priority list of tags; for each year we take the first
  tag that has a value, so a switch mid-history still yields a continuous series.
- 10-K filings also contain quarterly figures and prior years' comparatives. We
  keep only ~12-month periods, key them by period end date, and when a year was
  reported in several filings (restatements), keep the most recently filed value.
- Missing debt is ambiguous: debt-free, or debt under a tag we don't know. We only
  assume zero when the company also reports no meaningful interest expense.
- Return on capital has three fallbacks, because buybacks can push equity (and even
  equity + debt - cash) below zero: ROE -> ROIC -> return on tangible capital.
"""
from __future__ import annotations

from datetime import date

import config as C

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F"}
MIN_DAYS, MAX_DAYS = 350, 380          # 52/53-week fiscal years land in here
MATCH_TOLERANCE_DAYS = 7               # align dates that differ by a few days
MATERIAL_INTEREST = 0.001              # interest > 0.1% of revenue means debt exists

TAGS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
    ],
    "net_income": [
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
        "InterestPaidNet",
        "InterestPaid",
    ],
    "depreciation": [
        "Depreciation",
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "long_term_debt": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligations",
        "DebtInstrumentCarryingAmount",
        "SeniorNotes",
        "SeniorLongTermNotes",
        "LongTermNotesPayable",
        "UnsecuredLongTermDebt",
        "NotesPayable",
        "ConvertibleNotesPayable",
        "OtherLongTermDebtNoncurrent",
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebtFairValue",  # disclosed fair value; close to carrying value
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "current_assets": ["AssetsCurrent"],
    "total_assets": ["Assets"],
    # Shown in the brief's debt check (not used by the quality screen itself).
    "short_term_debt": ["ShortTermBorrowings", "CommercialPaper", "ShortTermBankLoansAndNotesPayable"],
    "lease_liabilities": ["OperatingLeaseLiability", "OperatingLeaseLiabilityNoncurrent"],
    "total_liabilities": ["Liabilities"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "net_ppe": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ],
}
TAGS["diluted_shares"] = [
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]
UNITS = {"diluted_shares": "shares"}  # everything else is USD
INSTANT_METRICS = {"equity", "long_term_debt", "cash", "current_assets",
                   "current_liabilities", "net_ppe", "total_assets", "total_liabilities",
                   "retained_earnings", "short_term_debt", "lease_liabilities"}  # balance-sheet snapshots
ALL_TAGS = sorted({t for tags in TAGS.values() for t in tags})


def _d(s: str) -> date:
    return date.fromisoformat(s)


def annual_points(facts: dict, tag: str, instant: bool, unit: str = "USD") -> dict[str, float]:
    """{period_end: value} for one tag, annual periods only, latest filing wins."""
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not node:
        return {}
    best: dict[str, tuple[str, float]] = {}
    for row in node.get("units", {}).get(unit, []):
        if row.get("form") not in ANNUAL_FORMS or not row.get("end"):
            continue
        end = row["end"]
        if not instant:
            start = row.get("start")
            if not start or not MIN_DAYS <= (_d(end) - _d(start)).days <= MAX_DAYS:
                continue
        filed = row.get("filed", "")
        if end not in best or filed > best[end][0]:
            best[end] = (filed, float(row["val"]))
    return {end: val for end, (_, val) in best.items()}


def metric_series(facts: dict, metric: str) -> dict[str, float]:
    merged: dict[str, float] = {}
    for tag in TAGS[metric]:
        unit = UNITS.get(metric, "USD")
        for end, val in annual_points(facts, tag, metric in INSTANT_METRICS, unit).items():
            merged.setdefault(end, val)
    return merged


def _lookup(series: dict[str, float], end: str) -> float | None:
    if end in series:
        return series[end]
    target, best = _d(end), None
    for e, v in series.items():
        gap = abs((_d(e) - target).days)
        if gap <= MATCH_TOLERANCE_DAYS and (best is None or gap < best[0]):
            best = (gap, v)
    return best[1] if best else None


def _fiscal_year_ends(series: dict[str, float]) -> list[str]:
    """Sorted period ends, collapsing near-duplicates (same year, dates a few days apart)."""
    ends: list[str] = []
    for e in sorted(series):
        if ends and (_d(e) - _d(ends[-1])).days < 300:
            ends[-1] = e
        else:
            ends.append(e)
    return ends


def _pays_material_interest(r: dict) -> bool:
    ie, rev = r.get("interest_expense"), r.get("revenue")
    if ie is None or ie <= 0:
        return False
    return rev is None or ie > MATERIAL_INTEREST * rev


def _return_on_capital(r: dict) -> tuple[float | None, str | None]:
    """After-tax operating return: ROIC, or return on tangible capital if ROIC's
    denominator is wiped out by buybacks."""
    ebit = r["operating_income"]
    if ebit is None and r["pretax_income"] is not None:
        ebit = r["pretax_income"] + (r["interest_expense"] or 0.0)
    if ebit is None:
        return None, None
    nopat = ebit * (1 - C.TAX_RATE)

    eq, debt, cash = r["equity"], r["long_term_debt"], r["cash"]
    if eq is not None:
        invested = eq + (debt or 0.0) - (cash or 0.0)
        if invested > 0:
            return nopat / invested, "ROIC"

    ppe, ca, cl = r["net_ppe"], r["current_assets"], r["current_liabilities"]
    if ppe is not None:
        working = max(ca - cl, 0.0) if ca is not None and cl is not None else 0.0
        tangible = ppe + working
        if tangible > 0:
            return nopat / tangible, "tangible capital"
    return None, None


def annual_table(facts: dict, years: int = C.YEARS) -> tuple[list[dict], list[str]]:
    """Return (rows oldest->newest, data-quality notes)."""
    series = {m: metric_series(facts, m) for m in TAGS}
    notes: set[str] = set()
    no_sbc_tag = not series["sbc"]
    if no_sbc_tag:
        notes.add("no stock comp reported; treated as zero")

    all_ends = _fiscal_year_ends(series["net_income"])
    window = all_ends[-years:]
    rows = []
    capex_estimated = 0
    for idx, end in enumerate(window):
        r = {"fiscal_year_end": end}
        for m in TAGS:
            r[m] = _lookup(series[m], end)
        if no_sbc_tag:
            r["sbc"] = 0.0

        # Missing debt: assume zero only if the company pays (almost) no interest.
        # If it does pay interest, estimate debt from the interest bill.
        r["debt_assumed_zero"] = r["debt_estimated"] = False
        if r["long_term_debt"] is None:
            if not _pays_material_interest(r):
                r["long_term_debt"] = 0.0
                r["debt_assumed_zero"] = True
            elif r["interest_expense"]:
                r["long_term_debt"] = r["interest_expense"] / C.IMPLIED_INTEREST_RATE
                r["debt_estimated"] = True

        # Capex filed under a company-specific label (not in SEC's free data):
        # estimate it as the change in net plant & equipment plus depreciation.
        r["capex_estimated"] = False
        if r["capex"] is None:
            pos = all_ends.index(end)
            prev_ppe = _lookup(series["net_ppe"], all_ends[pos - 1]) if pos > 0 else None
            if r["net_ppe"] is not None and prev_ppe is not None and r["depreciation"] is not None:
                r["capex"] = max(r["net_ppe"] - prev_ppe + r["depreciation"], 0.0)
                r["capex_estimated"] = True
                capex_estimated += 1

        ocf, capex = r["operating_cash_flow"], r["capex"]
        r["fcf"] = ocf - capex if ocf is not None and capex is not None else None
        r["owner_fcf"] = (r["fcf"] - (r["sbc"] or 0.0)) if r["fcf"] is not None else None

        # Many companies file total assets and equity but no "Liabilities" line.
        if r["total_liabilities"] is None and r["total_assets"] is not None and r["equity"] is not None:
            r["total_liabilities"] = r["total_assets"] - r["equity"]

        ni, eq = r["net_income"], r["equity"]
        r["roe"] = ni / eq if ni is not None and eq is not None and eq > 0 else None
        r["roic"], r["roic_method"] = _return_on_capital(r)
        # Shown, not used by the screen: a return above 100% (or below -100%) on capital
        # means the capital base is too small for the ratio to mean anything.
        r["roic_nm"] = r["roic"] is not None and abs(r["roic"]) > 1.0
        rows.append(r)

    # When the latest share count was filed: a split after this date isn't reflected
    # (reports filed after a split restate share counts for it).
    if rows:
        rows[-1]["shares_filed"] = _filed_date(facts, TAGS["diluted_shares"],
                                               rows[-1]["fiscal_year_end"], "shares")

    # Debt notes describe the latest year only, since that's what the debt check uses.
    if rows:
        latest = rows[-1]
        if latest["debt_assumed_zero"]:
            notes.add("VERIFY: no debt figure in latest filing and little interest; assumed debt-free")
        elif latest["debt_estimated"]:
            notes.add(f"VERIFY: debt estimated from interest paid "
                      f"(~${latest['long_term_debt'] / 1e9:.1f}B at {C.IMPLIED_INTEREST_RATE:.0%})")
        elif latest["long_term_debt"] is None:
            notes.add("pays interest but debt figure not found; check tags")
    if capex_estimated:
        notes.add(f"capital spending estimated for {capex_estimated} year(s) "
                  "(company-specific label not in SEC data)")
    return rows, sorted(notes)


def _filed_date(facts: dict, tags: list[str], end: str, unit: str) -> str | None:
    """Latest filing date of an annual value for this period end, across tags."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    best = None
    for tag in tags:
        for row in gaap.get(tag, {}).get("units", {}).get(unit, []):
            if (row.get("form") in ANNUAL_FORMS and row.get("end")
                    and abs((_d(row["end"]) - _d(end)).days) <= MATCH_TOLERANCE_DAYS):
                if best is None or row.get("filed", "") > best:
                    best = row.get("filed")
        if best:
            return best
    return best


def merge_facts(fact_sets: list[dict | None]) -> dict | None:
    """Combine company facts from several SEC IDs (e.g. a new holding company and
    its predecessor). Duplicate periods are resolved later by latest filing date."""
    present = [f for f in fact_sets if f]
    if not present:
        return None
    merged: dict[str, dict[str, list]] = {}
    for f in present:
        for tag, node in f.get("facts", {}).get("us-gaap", {}).items():
            for unit, rows in node.get("units", {}).items():
                merged.setdefault(tag, {}).setdefault(unit, []).extend(rows)
    return {"entityName": present[0].get("entityName"),
            "facts": {"us-gaap": {t: {"units": units} for t, units in merged.items()}}}
