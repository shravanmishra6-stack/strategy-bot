"""Tests for the live path: bars, instruments, risk, execution, live strategy."""
import datetime as dt

import pytest

from app.broker.instruments import expiry_from_symbol, pick_nearest_expiry
from app.data.bars import MinuteBarBuilder
from app.execution.engine import ExecutionEngine, OrderNotSubmitted
from app.risk.manager import RiskManager
from app.strategy.legs import SL, SoldLeg
from app.strategy.live import LegInstrument, LiveStraddle
from app.strategy.params import StraddleParams

DAY = dt.date(2026, 1, 15)


def ts(hh, mm, ss=0):
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss)


# --- minute bars ---------------------------------------------------------
def test_bar_completes_on_next_minute_tick():
    seen = []
    b = MinuteBarBuilder(on_bar=seen.append)
    assert b.on_tick(ts(9, 20, 0), 100.0) is None
    assert b.on_tick(ts(9, 20, 30), 105.0) is None
    assert b.on_tick(ts(9, 20, 59), 95.0) is None
    bar = b.on_tick(ts(9, 21, 5), 96.0)
    assert bar is not None and len(seen) == 1
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 95.0, 95.0)
    assert b.flush().open == 96.0  # in-progress bar force-emitted
    assert b.flush() is None


def test_out_of_order_ticks_ignored():
    b = MinuteBarBuilder(on_bar=None)
    b.on_tick(ts(9, 21, 0), 100.0)
    assert b.on_tick(ts(9, 20, 0), 50.0) is None  # stale tick
    assert b._cur["open"] == 100.0


# --- instruments -----------------------------------------------------------
@pytest.mark.parametrize(
    "sym,expected",
    [
        ("NIFTY26J15CE25000", dt.date(2026, 1, 15)),
        ("NIFTY26U30PE24500", dt.date(2026, 10, 30)),   # October = U
        ("NIFTY26O06CE25000", dt.date(2026, 6, 6)),     # June    = O
        ("NIFTY26F26PE23000", dt.date(2026, 2, 26)),
        ("NOTANOPTION", None),
        ("NIFTY26Z13CE19900", dt.date(2026, 9, 13)),   # September = Z
        ("NIFTY26W24PE25500", dt.date(2026, 12, 24)),  # December  = W
    ],
)
def test_expiry_from_symbol(sym, expected):
    assert expiry_from_symbol(sym) == expected


def test_pick_nearest_expiry():
    exps = [dt.date(2026, 1, 20), dt.date(2026, 1, 16), dt.date(2026, 1, 30)]
    assert pick_nearest_expiry(exps, dt.date(2026, 1, 15)) == dt.date(2026, 1, 16)
    assert pick_nearest_expiry(exps, dt.date(2026, 1, 16), min_days=1) == dt.date(2026, 1, 20)
    assert pick_nearest_expiry(exps, dt.date(2026, 2, 1)) is None


# --- risk -------------------------------------------------------------------
def test_risk_gates():
    rm = RiskManager()
    rm.on_new_day(DAY)
    assert rm.allow_entry(lots=1, now=ts(9, 25))
    rm.record_entry()
    rm.record_entry()
    assert not rm.allow_entry(lots=1, now=ts(10, 0))  # max entries/day = 2
    rm.on_new_day(dt.date(2026, 1, 16))
    assert rm.allow_entry(lots=1, now=ts(9, 25))
    assert not rm.allow_entry(lots=5, now=ts(9, 25))   # max lots = 2
    assert not rm.allow_entry(lots=1, now=ts(15, 0))   # after cutoff 14:45
    rm.on_trade_close(-500.0)                           # default cap 400 pts
    assert rm.halted_reason
    assert not rm.allow_entry(lots=1, now=ts(10, 0))


def test_risk_day_reset():
    rm = RiskManager()
    rm.on_new_day(DAY)
    rm.on_trade_close(-500.0)
    assert rm.halted_reason
    rm.on_new_day(dt.date(2026, 1, 16))
    assert rm.halted_reason is None and rm.daily_pnl_pts == 0.0


# --- execution (paper) --------------------------------------------------------
def test_paper_engine_fills_locally():
    eng = ExecutionEngine(client=None, paper_mode=True)
    f = eng.sell_to_open(right="CE", strike=25000, qty=1, ref_price=120.5)
    assert f.mode == "paper" and f.action == "SELL" and f.price == 120.5
    assert eng.fills[-1] is f


def test_live_engine_refuses_without_armed_client():
    class NotArmed:
        live_armed = False

        def place_order(self, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    eng = ExecutionEngine(client=NotArmed(), paper_mode=False)
    with pytest.raises(OrderNotSubmitted):
        eng.sell_to_open(right="CE", strike=25000, qty=50, ref_price=100.0,
                         symbol="NIFTY26J15CE25000")
    # A refusal must not masquerade as an ambiguous submission.
    assert eng.pending_count() == 0
    assert eng.fills == []


def test_not_armed_papermode_exception_is_not_ambiguous():
    """Client that claims armed but then refuses with PaperMode."""
    from app.broker.noren_client import PaperMode

    class Conflicting:
        live_armed = True

        def place_order(self, **kw):
            raise PaperMode("REFUSED: kill switch on")

    eng = ExecutionEngine(client=Conflicting(), paper_mode=False)
    with pytest.raises(OrderNotSubmitted):
        eng.buy_to_close(right="PE", strike=25000, qty=50, ref_price=90.0,
                         reason="SQUAREOFF")
    assert eng.pending_count() == 0


def test_transport_failure_is_ambiguous_and_reconciles():
    class Flaky:
        live_armed = True

        def place_order(self, **kw):
            raise TimeoutError("no response - order may exist")

        def get_order_book(self):
            return [{"rk": None}]

    eng = ExecutionEngine(client=Flaky(), paper_mode=False)
    with pytest.raises(TimeoutError):
        eng.sell_to_open(right="CE", strike=25000, qty=50, ref_price=100.0)
    assert eng.pending_count() == 1          # genuinely ambiguous -> tracked
    assert eng.reconcile() == []             # no tag match yet -> still pending
    assert eng.pending_count() == 1


# --- live straddle -------------------------------------------------------------
class FakeRisk:
    def allow_entry(self, *, lots, now):
        class Ok:
            def __bool__(self):
                return True
        return Ok()

    def record_entry(self):
        pass


def make_live():
    p = StraddleParams(entry_time=dt.time(9, 20), squareoff_time=dt.time(15, 15),
                       sl_pct=0.50)
    ce_leg = SoldLeg(right="CE", strike=25000, sl_pct=0.50)
    pe_leg = SoldLeg(right="PE", strike=25000, sl_pct=0.50)
    ce = LegInstrument(token="1001", tradingsymbol="NIFTY26J15CE25000",
                       strike=25000, right="CE")
    pe = LegInstrument(token="1002", tradingsymbol="NIFTY26J15PE25000",
                       strike=25000, right="PE")
    eng = ExecutionEngine(client=None, paper_mode=True)
    live = LiveStraddle(params=p, exec_engine=eng, risk=FakeRisk(), date=DAY)
    live.configure(spot_token="999", legs=[(ce_leg, ce), (pe_leg, pe)])
    return live, eng


def test_live_entry_and_stop_exit():
    live, eng = make_live()
    live.on_tick(ts(9, 19), "999", 24000.0)       # before entry: ignored
    assert live.phase == "WAIT_ENTRY"
    live.on_tick(ts(9, 20), "1001", 120.0)
    live.on_tick(ts(9, 20), "1002", 130.0)
    live.on_tick(ts(9, 20, 5), "999", 25010.0)    # first spot tick at entry
    assert live.phase == "IN_POSITION"
    assert len(eng.fills) == 2                     # sold both legs
    assert live.legs["CE"].entry == 120.0

    # Drive CE premium through its stop (120*1.5=180) using completed bars.
    live.on_tick(ts(9, 21), "1001", 150.0)
    live.on_tick(ts(9, 22), "1001", 170.0)         # 9:21 bar completes H=150
    live.on_tick(ts(9, 22, 30), "1001", 185.0)     # same minute crosses stop
    assert live.legs["CE"].is_open                 # exit only on completed bar
    live.on_tick(ts(9, 23), "1001", 200.0)         # 9:22 bar completes o170/h185
    assert not live.legs["CE"].is_open
    ce_exit = [f for f in eng.fills if f.action == "BUY" and f.right == "CE"]
    assert len(ce_exit) == 1 and ce_exit[0].reason == SL
    assert ce_exit[0].price == 180.0               # open 170 < stop -> filled at 180

    live.on_tick(ts(9, 24), "1002", 100.0)
    live.on_tick(ts(9, 25), "1002", 90.0)
    assert live.phase == "IN_POSITION"             # PE still open pre-15:15
    live.on_tick(ts(15, 15, 1), "999", 25000.0)    # any tick triggers square-off
    assert live.phase == "DONE"
    pe_exit = [f for f in eng.fills if f.action == "BUY" and f.right == "PE"]
    assert len(pe_exit) == 1 and pe_exit[0].reason == "SQUAREOFF"


def test_live_aborts_when_no_premium_at_entry():
    live, eng = make_live()
    live.on_tick(ts(9, 20, 5), "999", 25010.0)     # spot, but zero option prices
    assert live.phase == "DONE" and eng.fills == []
    assert any("no premium" in e for e in live.errors)


def test_live_refuses_entry_when_strike_mismatch():
    p = StraddleParams(entry_time=dt.time(9, 20), sl_pct=0.5)
    ce_leg = SoldLeg(right="CE", strike=99999, sl_pct=0.5)   # wired wrong strike
    ce_inst = LegInstrument(token="1", tradingsymbol="X", strike=99999, right="CE")
    pe_leg = SoldLeg(right="PE", strike=25000, sl_pct=0.5)
    pe_inst = LegInstrument(token="2", tradingsymbol="Y", strike=25000, right="PE")
    eng = ExecutionEngine(client=None, paper_mode=True)
    live = LiveStraddle(params=p, exec_engine=eng, risk=FakeRisk(), date=DAY)
    live.configure(spot_token="999", legs=[(ce_leg, ce_inst), (pe_leg, pe_inst)])
    live.on_tick(ts(9, 20), "1", 100.0)
    live.on_tick(ts(9, 20), "2", 100.0)
    live.on_tick(ts(9, 20, 1), "999", 25010.0)     # ATM would be 25000
    assert live.phase == "DONE" and eng.fills == []
