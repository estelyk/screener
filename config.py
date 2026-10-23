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

# Starting cash flow = typical (median) owner-FCF margin over the last 5 years x the
# latest year's revenue. The median ignores one-off years (e.g. a big tax deposit or
# acquisition payment); scaling by latest revenue keeps growing companies at today's size.
NORMALIZE_YEARS = 5
MIN_MARGIN_YEARS = 3      # need at least this many years with both cash flow and revenue
DISCOUNT_RATE = 0.10      # the return you demand; future cash is shrunk by this each year
MAX_GROWTH = 0.10         # growth is never assumed above this, however fast the past was
MIN_GROWTH = -0.05        # shrinking businesses may be projected to keep shrinking
PROJECTION_YEARS = 10     # years projected individually
HIGH_GROWTH_YEARS = 5     # years 1-5 grow at the estimated rate, then fade...
TERMINAL_GROWTH = 0.025   # ...to this long-run rate (about inflation) for all years after 10

# Owner cash flow is already AFTER interest payments, so it is cash available to
# shareholders: it is discounted at the shareholders' required return and debt is
# NOT subtracted again (that would count it twice). The quality screen separately
# rejects companies with too much debt.

# Bear / base / bull range shown in each brief: (discount rate, long-run growth).
SCENARIOS = {"bear": (0.12, 0.020), "base": (0.10, 0.025), "bull": (0.08, 0.030)}

MARGIN_OF_SAFETY = 0.25   # buy signal only when price is at least 25% below value
WATCHLIST_SIZE = 15       # how many closest-to-buy companies to show
SUSPICIOUS_DISCOUNT = 0.60  # a discount bigger than this is flagged "check the data"
DILUTION_WARN = 1.10      # warn if the share count grew more than 10% over 5 years
CASH_BACKING_MIN = 0.80   # warn if 5-yr operating cash flow < 80% of 5-yr reported earnings
RECOVERY_GAP_WARN = 0.30  # warn if the normal cash flow used is >30% above last year's actual

# ================================ HOLDINGS REVIEW ================================
# Companies you own get a "review" flag (not a sell order) when the business looks
# worse or the price runs far ahead of value. See review.py.
OVERVALUED_ABOVE = 0.50   # flag when price is more than 50% above estimated value

# ================================ AI EARNINGS SUMMARIES ================================
# Used only when the ANTHROPIC_API_KEY secret is set (see summaries.py).
SUMMARY_MODEL = "claude-sonnet-5-5"
SUMMARY_MAX_NEW_PER_RUN = 25      # cap on new summaries per daily run (cost guard)

# ================================ PAPER TRACKING ================================
# Recorded with every buy signal, so results can be tied to the exact rules used.
# Change this whenever the model's rules change (frozen for the 3-month trial).
MODEL_VERSION = "v1.0 (frozen Oct 16, 2026)"
