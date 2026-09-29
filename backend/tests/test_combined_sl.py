"""Tests for Combined Stop Loss (Total Premium SL)."""
import datetime as dt
import pandas as pd
import pytest

from app.backtest.engine import backtest_one_day
from app.execution.engine import ExecutionEngine
from app.risk.manager import RiskManager
from app.strategy.legs import COMBINED_SL, SoldLeg
from app.strategy.live import LegInstrument, LiveStraddle
from app.strategy.params import StraddleParams


class DummyClient:
    def __init__(self):
        self.orders = []

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"stat": "Ok", "norenordno": f"dummy_{len(self.orders)}"}

    def get_order_book(self):
        return []

    def get_trade_book(self):
        return []


def test_straddle_params_combined_sl():
    p = StraddleParams(sl_pct=None, combined_sl_pct=0.80)
    assert p.sl_pct is None
    assert p.combined_sl_pct == 0.80
    assert "CombSL 80%" in p.label()

    d = p.to_dict()
    assert d["combined_sl_pct"] == 0.80
    assert d["sl_pct"] is None

    p2 = StraddleParams.from_dict(d)
    assert p2.combined_sl_pct == 0.80
    assert p2.sl_pct is None


def test_live_straddle_combined_sl_triggers_individual_legs():
    params = StraddleParams(sl_pct=None, combined_sl_pct=0.80, sl_to_cost=True, lots=1)
    client = DummyClient()
    engine = ExecutionEngine(client, paper_mode=True)
    today = dt.date(2026, 9, 29)
    risk = RiskManager()
    risk.on_new_day(today)

    strat = LiveStraddle(params=params, exec_engine=engine, risk=risk, date=today, lots=1)
    ce_inst = LegInstrument(token="101", tradingsymbol="NIFTY22700CE", strike=22700, right="CE", lot_size=65)
    pe_inst = LegInstrument(token="102", tradingsymbol="NIFTY22700PE", strike=22700, right="PE", lot_size=65)

    pairs = [
        (SoldLeg(right="CE", strike=22700, token="101", sl_pct=params.sl_pct), ce_inst),
        (SoldLeg(right="PE", strike=22700, token="102", sl_pct=params.sl_pct), pe_inst),
    ]
    strat.configure(spot_token="26000", legs=pairs)

    now = dt.datetime(2026, 9, 29, 9, 20, 0)
    # Spot arrives -> triggers ATM pinning and entry
    strat.on_tick(now, "101", 60.0)
    strat.on_tick(now, "102", 60.0)
    strat.on_tick(now, "26000", 22700.0)

    assert strat.phase == "IN_POSITION"
    assert strat.combined_entry == 120.0
    # Combined SL at 80% of 120 = 96.0
    assert strat.combined_sl_level == 96.0
    assert strat.legs["CE"].sl_level == 96.0
    assert strat.legs["PE"].sl_level == 96.0
    assert strat.legs["CE"].is_open is True
    assert strat.legs["PE"].is_open is True

    # Tick 1: CE surges to 85 (< 96), PE drops to 50
    # Neither leg crossed 96 -> both stay open
    tick1 = dt.datetime(2026, 9, 29, 9, 21, 0)
    strat.on_tick(tick1, "101", 85.0)
    strat.on_tick(tick1, "102", 50.0)
    assert strat.phase == "IN_POSITION"
    assert strat.legs["CE"].is_open is True
    assert strat.legs["PE"].is_open is True

    # Tick 2: CE crosses 96 -> reaches 100!
    # CE must trigger SL and exit! PE is still open at 50.
    tick2 = dt.datetime(2026, 9, 29, 9, 22, 0)
    strat.on_tick(tick2, "101", 100.0)
    assert strat.legs["CE"].is_open is False
    assert strat.legs["PE"].is_open is True
    assert strat.phase == "IN_POSITION"
    # Surviving PE leg's SL must now be moved to its entry cost (60.0)!
    assert strat.legs["PE"].sl_level == 60.0

    # Tick 3: Later, PE rises to 60.50 (crosses its entry cost of 60.0!)
    # PE now triggers SL at cost and exits.
    tick3 = dt.datetime(2026, 9, 29, 9, 23, 0)
    strat.on_tick(tick3, "102", 60.50)
    assert strat.legs["PE"].is_open is False
    assert strat.phase == "DONE"

    fills = [f for f in engine.fills if f.action == "BUY"]
    assert len(fills) == 2
    assert all(f.reason == "SL" for f in fills)


def test_backtest_combined_sl():
    day = dt.date(2025, 6, 24)
    exp = dt.date(2025, 6, 24)

    spot = pd.DataFrame(
        {"open": [24000.0], "high": [24000.0], "low": [24000.0], "close": [24000.0]},
        index=pd.to_datetime([f"{day} 09:20:00"]),
    )

    # 09:20: CE=60, PE=60 (total=120, 80% SL level = 96 on each leg)
    # 09:21: CE=85 (< 96), PE=50 (< 96) -> no exit
    # 09:22: CE=100 (>= 96) -> CE SL exit! PE stays open
    # 09:23: PE=98 (>= 96) -> PE SL exit!
    rows_ce = [
        ("09:20:00", 60.0, 61.0, 59.0, 60.0),
        ("09:21:00", 60.0, 86.0, 59.0, 85.0),
        ("09:22:00", 85.0, 102.0, 84.0, 100.0),
        ("09:23:00", 100.0, 101.0, 99.0, 100.0),
    ]
    rows_pe = [
        ("09:20:00", 60.0, 61.0, 59.0, 60.0),
        ("09:21:00", 60.0, 62.0, 49.0, 50.0),
        ("09:22:00", 50.0, 55.0, 48.0, 52.0),
        ("09:23:00", 52.0, 99.0, 51.0, 98.0),
    ]
    ce_df = pd.DataFrame(
        [[o, h, l, c] for _, o, h, l, c in rows_ce],
        index=pd.to_datetime([f"{day} {t}" for t, *_ in rows_ce]),
        columns=["open", "high", "low", "close"],
    )
    pe_df = pd.DataFrame(
        [[o, h, l, c] for _, o, h, l, c in rows_pe],
        index=pd.to_datetime([f"{day} {t}" for t, *_ in rows_pe]),
        columns=["open", "high", "low", "close"],
    )

    class Cache:
        def get(self, exp, strk, right):
            return ce_df if right == "CE" else pe_df

    params = StraddleParams(
        entry_time=dt.time(9, 20),
        sl_pct=None,
        combined_sl_pct=0.80,
        sl_to_cost=True,
    )

    res = backtest_one_day(day, exp, spot, Cache(), params)
    assert res is not None
    assert res["legs_stopped"] == 2
    reasons = [row[5] for row in res["leg_details"]]
    assert all(r == "SL" for r in reasons)
