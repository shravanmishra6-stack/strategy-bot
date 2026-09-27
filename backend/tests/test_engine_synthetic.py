"""Engine wiring tests on a tiny synthetic dataset (fast, no disk dependency)."""
import datetime as dt

import pandas as pd
import pytest

from app.backtest.engine import backtest_one_day
from app.strategy.params import StraddleParams

DAY = dt.date(2025, 6, 24)
EXP = dt.date(2025, 6, 24)
STRIKE = 24000


class FakeCache:
    def __init__(self, frames):
        self.frames = frames

    def get(self, expiry, strike, right):
        return self.frames.get((strike, right))


def minute_ts(hh, mm):
    return pd.Timestamp(f"{DAY} {hh:02d}:{mm:02d}:00")


def frame(rows):
    return pd.DataFrame(
        rows, columns=["open", "high", "low", "close"]
    ).set_index(pd.to_datetime([r[0] for r in rows]))[["open", "high", "low", "close"]]


def base_spot():
    return pd.DataFrame(
        {"open": [24000.0]}, index=[minute_ts(9, 20)]
    ).assign(high=24000.0, low=24000.0, close=24000.0)


def test_one_leg_stops_other_squares_off():
    rows = [("09:20:00", 100.0, 101.0, 99.0, 100.0), ("09:21:00", 100.0, 155.0, 99.0, 152.0)]
    ce = pd.DataFrame(
        [[o, h, l, c] for _, o, h, l, c in rows],
        index=pd.to_datetime([f"{DAY} {t}" for t, *_ in rows]),
        columns=["open", "high", "low", "close"],
    )
    pe = ce.copy()
    cache = FakeCache({(STRIKE, "CE"): ce, (STRIKE, "PE"): pe})
    p = StraddleParams(entry_time=dt.time(9, 20), sl_pct=0.50, brokerage=20.0, slippage=1.0)
    trade = backtest_one_day(DAY, EXP, base_spot(), cache, p)
    assert trade is not None
    assert trade["legs_stopped"] == 2  # both legs identical -> both stop at 150
    # net per leg: (100-150) - 1 slip - 20/lot brokerage
    lot = p.lot_size(DAY)  # 75 from schedule (>= 2025-05-01)
    assert lot == 75
    per_leg = (100 - 150) - 1.0 - 20.0 / lot
    assert trade["pnl_pts"] == round(2 * per_leg, 2)
    # Matches the original script: pnl_pts is rounded for display, but pnl_inr
    # is derived from the *unrounded* point total. Assert both, with the money
    # figure taken from the raw sum (not the pre-rounded points).
    assert trade["pnl_inr"] == round(2 * per_leg * lot, 2)
    assert abs(trade["pnl_inr"] - trade["pnl_pts"] * lot) < 1.0


def test_missing_entry_bar_returns_none():
    cache = FakeCache({})
    p = StraddleParams()
    assert backtest_one_day(DAY, EXP, base_spot(), cache, p) is None


def test_entry_time_missing_in_spot_returns_none():
    spot = base_spot()
    p = StraddleParams(entry_time=dt.time(9, 15))  # not in index
    cache = FakeCache({})
    assert backtest_one_day(DAY, EXP, spot, cache, p) is None


def test_lot_schedule_boundaries():
    p = StraddleParams()
    assert p.lot_size(dt.date(2024, 4, 29)) == 25
    assert p.lot_size(dt.date(2024, 4, 30)) == 50
    assert p.lot_size(dt.date(2025, 4, 30)) == 50
    assert p.lot_size(dt.date(2025, 5, 1)) == 75
    assert p.lot_size(dt.date(2026, 1, 1)) == 65


def test_params_roundtrip():
    p = StraddleParams(sl_pct=0.5, target_pct=0.3, strike_offset=2)
    q = StraddleParams.from_dict(p.to_dict())
    assert q.sl_pct == 0.5 and q.target_pct == 0.3 and q.strike_offset == 2
    assert q.entry_time == p.entry_time
