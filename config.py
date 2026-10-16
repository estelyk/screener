"""Screen settings. Every threshold lives here so you can tune without touching logic."""
import os

# On GitHub Actions the run logs of a public repository are public, so the scripts
# print only counts there, never tickers or results.
QUIET = os.environ.get("GITHUB_ACTIONS") == "true"
MIN_PRICE_COVERAGE = 0.8   # fail the run (keep yesterday's page) if fewer prices arrive

# --- Universe ---------------------------------------------------------------
# Sectors skipped in v1 because the standard metrics don't fit them:
# banks/insurers (debt IS the business), REITs and utilities (leverage is structural).
EXCLUDED_SECTORS = {"Financials", "Real Estate", "Utilities"}

# Companies that reorganized under a new holding company keep their history under
# the old SEC ID (CIK). List the old IDs here and both are combined.
PREDECESSOR_CIKS = {
    "XOM": [34088],   # Exxon Mobil Corp, before ExxonMobil Holdings Corp
    "APA": [6769],    # Apache Corp, before APA Corp (2021)
}

# --- History ----------------------------------------------------------------
YEARS = 10               # look-back window, in fiscal years
MIN_YEARS_REQUIRED = 8   # fewer annual periods than this -> NO_DATA, not a pass or fail

# --- Returns on capital -----------------------------------------------------
# ROE = net income / shareholders' equity
ROE_AVG_MIN = 0.15            # 10-yr average must be at least 15%
ROE_FLOOR = 0.10              # "rarely below 10%"...
ROE_YEARS_BELOW_FLOOR_MAX = 2 # ...means at most this many years under the floor

# If equity is negative or tiny (usually from heavy buybacks), ROE is meaningless.
# Any year with ROE above this, or equity <= 0, switches the test to ROIC instead.
ROE_DISTORTED_ABOVE = 1.00

# ROIC = after-tax operating income / (equity + debt - cash)
ROIC_AVG_MIN = 0.12
ROIC_FLOOR = 0.08
ROIC_YEARS_BELOW_FLOOR_MAX = 2
TAX_RATE = 0.21               # flat assumption used for after-tax operating income

# --- Debt -------------------------------------------------------------------
# Latest long-term debt divided by average net income of the last 3 years.
DEBT_YEARS_OF_EARNINGS_MAX = 4.0

# When a company pays interest but files no standard debt figure, debt is estimated
# as interest / this rate. A lowish rate errs toward MORE debt (the cautious side).
IMPLIED_INTEREST_RATE = 0.04

# --- Free cash flow ---------------------------------------------------------
FCF_POSITIVE_SHARE = 0.8      # positive in at least 80% of years (8 of 10)
SUBTRACT_SBC = True           # subtract stock-based compensation ("owner" free cash flow)

# --- Earnings consistency ---------------------------------------------------
LOSS_YEARS_MAX = 1            # at most one year with a net loss
BIG_DROP = 0.50               # a year-over-year earnings fall of more than 50%...
BIG_DROP_YEARS_MAX = 2        # ...may happen at most this many times

# =============================== PHASE 2: VALUATION ===============================
# Intrinsic value = today's value of all future owner free cash flow.

NORMALIZE_YEARS = 3       # starting cash flow = average of the last 3 years (smooths one-offs)
DISCOUNT_RATE = 0.10      # the return you demand; future cash is shrunk by this each year
MAX_GROWTH = 0.10         # growth is never assumed above this, however fast the past was
PROJECTION_YEARS = 10     # years projected individually
HIGH_GROWTH_YEARS = 5     # years 1-5 grow at the estimated rate, then fade...
TERMINAL_GROWTH = 0.025   # ...to this long-run rate (about inflation) for all years after 10

# No credit is given for cash on hand, but net debt (debt above cash) is subtracted.
# That errs on the cautious side.

MARGIN_OF_SAFETY = 0.25   # buy signal only when price is at least 25% below value
WATCHLIST_SIZE = 15       # how many closest-to-buy companies to show
SUSPICIOUS_DISCOUNT = 0.60  # a discount bigger than this is flagged "check the data"

# ================================ HOLDINGS REVIEW ================================
# Companies you own get a "review" flag (not a sell order) when the business looks
# worse or the price runs far ahead of value. See review.py.
OVERVALUED_ABOVE = 0.50   # flag when price is more than 50% above estimated value
