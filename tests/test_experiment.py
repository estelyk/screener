"""Shadow models, conservative checks, ROIC safeguard, drawdown and group results."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tracker  # noqa: E402
from run_phase2 import build, conservative_checks  # noqa: E402
from test_valuation import lulu_like, rows  # noqa: E402
from valuation import dcf, long_run_trend, value_company  # noqa: E402


def test_shadow_values_never_change_the_live_value():
    r = lulu_like()
    v = value_company(r)
    last, recent = r[-1]["owner_fcf"], [x["owner_fcf"] for x in r[-3:]]
    sh = v["shadow"]
    assert sh["min"] == pytest.approx(dcf(min(v["normalized_fcf"], last), v["growth"], trend=long_run_trend(r)) / 119.1e6)
    assert sh["blend"] == pytest.approx(dcf((v["normalized_fcf"] + last) / 2, v["growth"], trend=long_run_trend(r)) / 119.1e6)
    assert sh["recent3"] == pytest.approx(dcf(sum(recent) / 3, v["growth"], trend=long_run_trend(r)) / 119.1e6)
    assert sh["min"] < sh["blend"] < v["value_per_share"]


def test_shadow_signals_and_conservative_badge():
    r = [dict(x, fiscal_year_end=f"{2016 + i}-12-31") for i, x in enumerate(lulu_like())]
    price = value_company(lulu_like())["value_per_share"] * 0.6      # live pre-buy signal
    out = build([{"ticker": "L", "name": "L", "sector": "S", "notes": [], "annual": r}], {"L": price})[0]
    assert out["signal"] and not out["shadow_signals"]["min"]       # too dear under the lower cash flow
    assert out["conservative"] is False
    assert "price above the no-recovery value" in out["conservative_fails"]


def test_clean_cheap_stock_passes_conservative_checks():
    ok = {"signal": True, "price": 50, "value_low": 60, "value_no_recovery": None,
          "fscore": {"score": 7, "out_of": 9}, "warnings": [], "suspicious": False, "verify": ""}
    assert conservative_checks(ok) == (True, [])
    assert conservative_checks(dict(ok, signal=False)) == (False, [])
    assert conservative_checks(dict(ok, warnings=["x"]))[0] is False


def test_roic_not_meaningful_warning():
    r = rows()
    for x in r:
        x.update(net_income=100, roe=None)
    for x in r[-4:]:
        x["roic_nm"] = True
    assert any("isn't meaningful" in w for w in value_company(r)["warnings"])
    for x in r:
        x["roic_nm"] = False
    assert not any("isn't meaningful" in w for w in value_company(r)["warnings"])


def test_drawdown_and_groups():
    s = pd.Series([10, 12, 9, 11], index=pd.bdate_range("2026-10-16", periods=4))
    assert tracker._maxdd(s, "2026-10-16") == pytest.approx(9 / 12 - 1)
    led = tracker.empty_ledger("2026-10-16")
    base = {"name": "x", "sector": "s", "price": 10, "value_per_share": 20}
    vals = [dict(base, ticker="A", signal=True, conservative=True, warnings=[], shadow_signals={"min": True}, shadow={"min": 15}),
            dict(base, ticker="B", signal=True, conservative=False, warnings=["w"], shadow_signals={"min": False}, shadow={"min": 9})]
    tracker.record(led, vals, "2026-10-16", "v1")
    tracker.record_shadow(led, vals, "2026-10-16", "v1")
    assert [e["ticker"] for e in led["shadow"]["min"]] == ["A"] and led["shadow"]["blend"] == []
    tracker.update_returns(led, {"A": s, "B": s, "SPY": s, "VTV": s})
    assert led["shadow"]["min"][0]["ret"] == pytest.approx(0.1)          # shadow entries are tracked too
    sm = tracker.summary(led)
    assert sm["conservative"]["n"] == 1 and sm["warned"]["n"] == 1 and sm["clean"]["n"] == 1
    assert sm["shadow"]["min"]["n"] == 1 and sm["all"]["maxdd"] == pytest.approx(9 / 12 - 1)
