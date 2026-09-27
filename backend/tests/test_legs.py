"""Unit tests for the shared SoldLeg exit rules (backtest + live both use these)."""
import pytest

from app.strategy.legs import SL, TARGET, TRAIL_SL, Bar, SoldLeg


def make_leg(entry=100.0, sl=0.50, tgt=None, trail=None):
    leg = SoldLeg(right="CE", strike=22000, sl_pct=sl, target_pct=tgt, trail_pct=trail)
    leg.arm(entry)
    return leg


def test_arm_computes_fixed_stop():
    leg = make_leg(entry=100.0, sl=0.50)
    assert leg.entry == 100.0
    assert leg.sl_level == 150.0          # +50% of sold premium
    assert leg.is_open is True


def test_stop_hit_fills_at_stop_level():
    leg = make_leg(entry=100.0, sl=0.50)
    res = leg.on_bar(Bar(open=101, high=155, low=100, close=152))
    assert res is not None
    reason, fill = res
    assert reason == SL
    assert fill == 150.0                  # stop price, not bar close
    assert leg.is_open is False
    assert leg.on_bar(Bar(101, 160, 100, 155)) is None  # closed leg is inert


def test_stop_gap_through_uses_open_worse_fill():
    leg = make_leg(entry=100.0, sl=0.50)
    res = leg.on_bar(Bar(open=160, high=165, low=158, close=162))
    reason, fill = res
    assert reason == SL
    assert fill == 160.0                  # gapped through -> realistic open


def test_target_hit_fills_at_target():
    leg = make_leg(entry=100.0, sl=0.50, tgt=0.30)
    assert leg.target_level == 70.0
    res = leg.on_bar(Bar(open=99, high=100, low=65, close=68))
    reason, fill = res
    assert reason == TARGET
    assert fill == 70.0


def test_target_gap_better_at_open():
    leg = make_leg(entry=100.0, sl=0.50, tgt=0.30)
    reason, fill = leg.on_bar(Bar(open=60, high=62, low=55, close=58))
    assert reason == TARGET
    assert fill == 60.0


def test_trailing_arms_then_exits_with_trail_reason():
    leg = make_leg(entry=100.0, sl=0.50, trail=0.50)  # arm after 25 pts of profit
    assert leg.on_bar(Bar(open=100, high=101, low=80, close=81)) is None
    # premium fell 20 pts; needed >= 0.5 * (150-100) = 25 -> not armed yet
    assert leg.trail_sl is None
    assert leg.on_bar(Bar(open=80, high=81, low=74, close=75)) is None
    assert leg.trail_sl == 74.0           # trailed to bar low
    reason, fill = leg.on_bar(Bar(open=75, high=90, low=74, close=88))
    assert reason == TRAIL_SL
    assert fill == 90.0 if fill != 75.0 else 75.0  # stop=74; open=75 >= stop -> gap
    # entry price (open=75) is worse than trail level (74), so fill == open
    assert fill == 75.0


def test_stop_wins_over_target_on_same_bar():
    # Ambiguous bar touching both levels: conservative stop-first behaviour.
    leg = make_leg(entry=100.0, sl=0.50, tgt=0.30)
    res = leg.on_bar(Bar(open=100, high=155, low=65, close=120))
    reason, _ = res
    assert reason == SL
