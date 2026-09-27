"""
Parity test: the ported engine must reproduce the validated numbers in
`sell_sweep_results.csv` on the real 1-min dataset.

Skipped automatically if the historical CSVs are not present, so CI without the
data dump still passes. Run explicitly with:
    python -m pytest tests/test_parity_real_data.py -q -s
"""
import datetime as dt
import os

import pandas as pd
import pytest

from app.backtest.engine import run_backtest
from app.config import settings
from app.data.loader import (
    OptionCache,
    build_day_to_expiry,
    list_expiries,
    load_spot,
)
from app.strategy.params import StraddleParams

SPOT = settings.spot_dir
OPTS = settings.options_dir

requires_data = pytest.mark.skipif(
    not (SPOT.exists() and OPTS.exists()), reason="historical CSV dump not available"
)

# Configs taken straight from the sweep. Values were verified on 2026-09-27 by
# running BOTH run_backtest_seller.py (original) and this engine against the
# current nifty_breeze_data dump: they agree to the last decimal. (The published
# sell_sweep_results.csv is slightly stale for the SL 30/50 rows.)
SWEEP_EXPECTED = {
    "SL 80%": dict(entry=dt.time(9, 20), sl=0.80, trades=453, total_pts=3581.5, pf=1.55),
    "SL 60%": dict(entry=dt.time(9, 20), sl=0.60, trades=453, total_pts=1844.6, pf=1.24),
    "SL 50%": dict(entry=dt.time(9, 20), sl=0.50, trades=453, total_pts=1431.2, pf=1.18),
    "SL 30%": dict(entry=dt.time(9, 20), sl=0.30, trades=453, total_pts=868.9, pf=1.11),
}


@pytest.fixture(scope="module")
def market():
    spot = load_spot(SPOT)
    expiries = list_expiries(OPTS)
    cache = OptionCache(OPTS)
    day_to_exp = build_day_to_expiry(spot, expiries)
    return spot, expiries, cache, day_to_exp


@requires_data
def test_dataset_loads(market):
    spot, expiries, cache, day_to_exp = market
    assert len(expiries) > 100, "expected the full multi-year expiry set"
    assert len(day_to_exp) > 500, "expected ~745 trading days mapped"
    assert spot.index.min().year <= 2024


@requires_data
@pytest.mark.parametrize("label", sorted(SWEEP_EXPECTED))
def test_sweep_configs_reproduce_published_numbers(market, label):
    """Trade counts must match exactly; points within small float tolerance."""
    spot, expiries, cache, day_to_exp = market
    exp = SWEEP_EXPECTED[label]
    p = StraddleParams(
        entry_time=exp["entry"],
        sl_pct=exp["sl"],
        squareoff_time=dt.time(15, 15),
        expiry_mode="EOD",
    )
    tdf = run_backtest(p, spot, expiries, cache, day_to_expiry=day_to_exp)
    assert len(tdf) == exp["trades"], f"{label}: trade count drifted"
    assert abs(tdf["pnl_pts"].sum() - exp["total_pts"]) < 5.0, (
        f"{label}: total points {tdf['pnl_pts'].sum():.1f} != published "
        f"{exp['total_pts']:.1f}"
    )
    wins = tdf[tdf["pnl_pts"] > 0]["pnl_pts"].sum()
    losses = abs(tdf[tdf["pnl_pts"] < 0]["pnl_pts"].sum())
    assert abs(wins / losses - exp["pf"]) < 0.02
