"""Tests for the live path: bars, instruments, risk, execution, live strategy."""
import datetime as dt
import re

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
# Symbol grammar verified against the live API (searchscrip on segment NFO):
# NIFTY29SEP26C23400 = root + DD + MON + YY + C/P + strike. The old
# "NIFTY26J15CE25000" month-code form never existed on this broker.
@pytest.mark.parametrize(
    "sym,expected",
    [
        ("NIFTY29SEP26C23400", dt.date(2026, 9, 29)),
        ("NIFTY29SEP26P23100", dt.date(2026, 9, 29)),
        ("NIFTY27OCT26C24000", dt.date(2026, 10, 27)),
        ("NIFTY23NOV26P25000", dt.date(2026, 11, 23)),
        ("NIFTY29SEP26F", dt.date(2026, 9, 29)),      # futures root
        ("MIDCPNIFTY06OCT26C12000", dt.date(2026, 10, 6)),
        ("NOTANOPTION", None),
        ("NIFTY26J15CE25000", None),                  # stale vendor form
        ("NIFTY31FEB26C23400", None),                 # impossible date
    ],
)
def test_expiry_from_symbol(sym, expected):
    assert expiry_from_symbol(sym) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("29-SEP-2026", dt.date(2026, 9, 29)),
     ("31-DEC-2027", dt.date(2027, 12, 31)),
     ("29sep26", None),
     (None, None),
     ("", None)],
)
def test_parse_expiry_date(raw, expected):
    from app.broker.instruments import parse_expiry_date
    assert parse_expiry_date(raw) == expected


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
        eng.sell_to_open(right="CE", strike=25000, qty=50, ref_price=100.0,
                         token="73927", symbol="NIFTY29SEP26C25000")
    assert eng.pending_count() == 1          # genuinely ambiguous -> tracked
    assert eng.reconcile() == []             # no tag match yet -> still pending
    assert eng.pending_count() == 1


def test_live_refuses_incomplete_instrument_without_touching_broker():
    """A guessed symbol is far more dangerous than a missed trade."""
    class Armed:
        live_armed = True

        def place_order(self, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    eng = ExecutionEngine(client=Armed(), paper_mode=False)
    with pytest.raises(OrderNotSubmitted):
        eng.sell_to_open(right="CE", strike=25000, qty=50, ref_price=100.0)
    assert eng.pending_count() == 0
    assert eng.fills == []


def test_live_sends_broker_product_and_segment():
    sent = {}

    class Armed:
        live_armed = True

        def place_order(self, **kw):
            sent.update(kw)
            return {"stat": "Ok", "norenordno": "900001"}

    eng = ExecutionEngine(client=Armed(), paper_mode=False, product_type="M")
    f = eng.sell_to_open(right="CE", strike=25000, qty=65, ref_price=100.0,
                         token="73927", symbol="NIFTY29SEP26C25000",
                         exchange="NFO")
    assert sent["exchange"] == "NFO" and sent["product_type"] == "M"
    assert sent["tradingsymbol"] == "NIFTY29SEP26C25000"
    assert sent["quantity"] == 65
    assert f.order_id == "900001" and f.mode == "live"


def test_live_sizing_refuses_without_lot_size():
    live, eng = make_live(lot_size=None)
    live.on_tick(ts(9, 20), "1001", 120.0)
    live.on_tick(ts(9, 20), "1002", 130.0)
    live.on_tick(ts(9, 20, 5), "999", 25010.0)
    assert live.phase == "DONE"
    assert eng.fills == []
    assert any("lot size" in e for e in live.errors)


def test_live_qty_is_lots_times_lot_size():
    live, eng = make_live(lots=2)
    live.on_tick(ts(9, 20), "1001", 120.0)
    live.on_tick(ts(9, 20), "1002", 130.0)
    live.on_tick(ts(9, 20, 5), "999", 25010.0)
    assert live.phase == "IN_POSITION"
    assert [f.qty for f in eng.fills] == [130, 130]
    live.on_tick(ts(15, 15, 1), "999", 25000.0)
    assert {f.qty for f in eng.fills if f.action == "BUY"} == {130}


# --- live straddle -------------------------------------------------------------
class FakeRisk:
    def allow_entry(self, *, lots, now):
        class Ok:
            def __bool__(self):
                return True
        return Ok()

    def record_entry(self):
        pass


LOT = 65  # NIFTY option lot size returned by the live broker (ls field)


def make_live(lots=1, lot_size=LOT):
    p = StraddleParams(entry_time=dt.time(9, 20), squareoff_time=dt.time(15, 15),
                       sl_pct=0.50)
    ce_leg = SoldLeg(right="CE", strike=25000, sl_pct=0.50)
    pe_leg = SoldLeg(right="PE", strike=25000, sl_pct=0.50)
    ce = LegInstrument(token="1001", tradingsymbol="NIFTY29SEP26C25000",
                       strike=25000, right="CE", exchange="NFO",
                       lot_size=lot_size)
    pe = LegInstrument(token="1002", tradingsymbol="NIFTY29SEP26P25000",
                       strike=25000, right="PE", exchange="NFO",
                       lot_size=lot_size)
    eng = ExecutionEngine(client=None, paper_mode=True)
    live = LiveStraddle(params=p, exec_engine=eng, risk=FakeRisk(), date=DAY,
                        lots=lots)
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
    assert {f.qty for f in eng.fills} == {LOT}     # lots x broker lot size
    assert {f.exchange for f in eng.fills} == {"NFO"}
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


# --- instrument resolution against replayed LIVE payloads ------------------
# FakeBroker below replays verbatim shapes captured from api.shoonya.com on
# 2026-09-27. They pin three things the code previously guessed wrong: the NFO
# segment, rows under "values", and chain roots that must be a derivative tsym
# with a non-zero strike.
def _opt_row(tsym, token, optt, strike, exd="29-SEP-2026"):
    return {"tsym": tsym, "token": token, "exch": "NFO", "seg": "DER",
            "instname": "OPTIDX", "symname": "NIFTY", "exd": exd,
            "optt": optt, "strprc": f"{strike}.00", "ls": "65", "pp": "2",
            "ti": "0.05"}


class FakeBroker:
    """Answers like the live API for exactly the queries the resolver makes."""

    # Only the month-end contracts carry futures; weekly chains do not.
    FUT_ROWS = [
        {"tsym": "NIFTY29SEP26F", "token": "68407", "exch": "NFO", "seg": "DER",
         "instname": "FUTIDX", "symname": "NIFTY", "exd": "29-SEP-2026",
         "optt": "XX", "ls": "65", "pp": "2", "ti": "0.10"},
        {"tsym": "NIFTY27OCT26F", "token": "48704", "exch": "NFO", "seg": "DER",
         "instname": "FUTIDX", "symname": "NIFTY", "exd": "27-OCT-2026",
         "optt": "XX", "ls": "65", "pp": "2", "ti": "0.10"},
    ]
    # Weeklies the broker actually lists for this account (all Tuesdays).
    WEEKLIES = ("NIFTY29SEP26", "NIFTY06OCT26", "NIFTY13OCT26")

    def __init__(self):
        self.search_calls = []
        self.chain_calls = []
        self.quote_calls = []

    def searchscrip(self, exchange, searchtext, *, retries=2):
        self.search_calls.append((exchange, searchtext))
        if exchange != "NFO":
            # Live behaviour: a segment this account cannot use answers
            # Not_Ok, which the SDK flattens to None.
            return None
        text = searchtext.upper()
        if text == "NIFTY":
            return {"stat": "Ok", "values": list(self.FUT_ROWS)}
        rows = []
        for root, tok, right in (("NIFTY29SEP26C23150", "73908", "CE"),
                                 ("NIFTY29SEP26P23150", "73909", "PE")):
            if root.upper().startswith(text):
                rows.append(_opt_row(root, tok, right, 23150))
        for frow in self.FUT_ROWS:               # monthly prefix also returns its future
            if frow["tsym"].upper().startswith(text):
                rows.append(frow)
        if not rows and text in self.WEEKLIES:      # weekly chain probe
            exd = (text[5:7] + "-" + text[7:10] + "-20" + text[10:12])
            rows.append(_opt_row(text + "C23150", "70001", "CE", 23150, exd=exd))
        return {"stat": "Ok", "values": rows} if rows else None

    def get_quotes(self, exchange, token):
        self.quote_calls.append((exchange, token))
        if (exchange, str(token)) == ("NSE", "26000"):
            return {"stat": "Ok", "exch": "NSE", "token": "26000",
                    "lp": "23140.50", "pp": "2", "ls": "1"}
        return None

    def get_option_chain(self, exchange, tradingsymbol, strikeprice, count=5):
        self.chain_calls.append((exchange, tradingsymbol, strikeprice, count))
        if exchange != "NFO" or float(strikeprice) <= 0:
            return None
        # Live behaviour: a derivative tsym works as the root - the monthly
        # future or (for weekly-only expiries) an option contract.
        m = re.match(r"^(NIFTY\d{2}[A-Z]{3}\d{2})", tradingsymbol.upper())
        if not m:
            return None
        prefix = m.group(1)
        return {"stat": "Ok", "values": [
            _opt_row(prefix + "C23150", "73908", "CE", 23150),
            _opt_row(prefix + "P23150", "73909", "PE", 23150),
        ]}


def test_resolver_uses_live_segment_and_index_identity():
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    assert res.exchange == "NFO"
    assert (res.index_exchange, res.index_token) == ("NSE", "26000")
    assert res.root_symbol == "NIFTY"


def test_resolver_reads_rows_from_values_key():
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    assert res._scrip_rows({"stat": "Ok", "values": [{"tsym": "X"}]}) \
        == [{"tsym": "X"}]
    assert res._scrip_rows({"stat": "Ok", "result": [{"tsym": "Y"}]}) \
        == [{"tsym": "Y"}]
    assert res._scrip_rows(None) == []
    assert res._scrip_rows([{"tsym": "Z"}]) == [{"tsym": "Z"}]


def test_available_expiries_from_futures_master_list():
    from app.broker.instruments import InstrumentResolver

    broker = FakeBroker()
    res = InstrumentResolver(broker)
    exps = res.available_expiries(dt.date(2026, 9, 28))
    assert dt.date(2026, 9, 29) in exps          # from the futures master list
    assert dt.date(2026, 10, 27) in exps
    assert all(e >= dt.date(2026, 9, 28) for e in exps)
    # Every probe must use the enabled segment, never NSE_FNO.
    assert {exch for exch, _ in broker.search_calls} == {"NFO"}
    assert res.nearest_expiry(dt.date(2026, 9, 28), exps) == dt.date(2026, 9, 29)


def test_weekly_only_expiry_is_found_by_probe():
    """The nearest weekly has no futures row, so only a probe can find it.

    On 30-SEP the 29-SEP month contract has expired and the futures master list
    offers only 27-OCT. The real nearest expiry is the 06-OCT weekly, which the
    broker lists with no futures row at all.
    """
    from app.broker.instruments import InstrumentResolver

    broker = FakeBroker()
    res = InstrumentResolver(broker)
    exps = res.available_expiries(dt.date(2026, 9, 30))
    assert exps[0] == dt.date(2026, 10, 6), exps
    assert res.nearest_expiry(dt.date(2026, 9, 30), exps) == dt.date(2026, 10, 6)
    # Bounded search: one master query plus only the probes that can win.
    assert len(broker.search_calls) <= 4, broker.search_calls


def test_expiry_probe_budget_is_bounded():
    """An empty master list must not turn into one call per calendar day."""
    from app.broker.instruments import InstrumentResolver

    broker = FakeBroker()
    res = InstrumentResolver(broker)
    res.available_expiries(dt.date(2026, 12, 1), horizon_days=90, max_probes=3)
    assert len(broker.search_calls) <= 4, broker.search_calls


def test_symbol_grammar_matches_live_contracts():
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    assert res._symbol(dt.date(2026, 9, 29), 23400, "CE") == "NIFTY29SEP26C23400"
    assert res._symbol(dt.date(2026, 10, 6), 23150, "PE") == "NIFTY06OCT26P23150"


def test_resolve_returns_broker_token_and_lot():
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    legs = res.resolve(expiry=dt.date(2026, 9, 29), atm=23150, step=50)
    assert [(l.right, l.token, l.lot_size, l.exchange) for l in legs] == [
        ("CE", "73908", 65, "NFO"), ("PE", "73909", 65, "NFO")]
    assert [l.tradingsymbol for l in legs] == ["NIFTY29SEP26C23150",
                                               "NIFTY29SEP26P23150"]


def test_resolve_refuses_unknown_contract():
    from app.broker.instruments import InstrumentResolver
    from app.broker.noren_client import BrokerError

    res = InstrumentResolver(FakeBroker())
    with pytest.raises(BrokerError) as exc:
        res.resolve(expiry=dt.date(2026, 12, 31), atm=99999, step=50)
    assert "refusing to trade" in str(exc.value)


def test_chain_rows_only_uses_derivative_root_and_nonzero_strike():
    from app.broker.instruments import InstrumentResolver

    broker = FakeBroker()
    res = InstrumentResolver(broker)
    rows = res.chain_rows(today=dt.date(2026, 9, 28), ref_strike=23140.5,
                          count=2)
    assert [r["tsym"] for r in rows] == ["NIFTY29SEP26C23150",
                                         "NIFTY29SEP26P23150"]
    assert broker.chain_calls, "chain should have been queried"
    for exch, tsym, strprc, _cnt in broker.chain_calls:
        assert exch == "NFO"
        assert tsym.endswith("F")
        assert float(strprc) > 0


def test_chain_root_prefers_future_and_falls_back_to_option():
    import datetime as _dt
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    today = _dt.date(2026, 9, 28)
    # Monthly expiry has a futures contract: use it as the chain root.
    assert res.chain_root_for(_dt.date(2026, 9, 29), today) == "NIFTY29SEP26F"
    # Weekly-only expiry has no futures contract, so an option contract of the
    # SAME expiry must be used - never a different month's future.
    assert res.chain_root_for(_dt.date(2026, 10, 6), today) == "NIFTY06OCT26C23150"
    # An expiry the broker does not list yields no root (we refuse, not guess).
    assert res.chain_root_for(_dt.date(2026, 12, 31), today) is None


def test_chain_rows_pinned_to_expiry_describe_that_expiry():
    import datetime as _dt
    from app.broker.instruments import InstrumentResolver, expiry_from_symbol

    res = InstrumentResolver(FakeBroker())
    today = _dt.date(2026, 9, 28)
    weekly = _dt.date(2026, 10, 6)
    rows = res.chain_rows(today=today, ref_strike=23140.5, count=2,
                          expiry=weekly)
    assert rows, "weekly chain must resolve"
    assert {expiry_from_symbol(r["tsym"]) for r in rows} == {weekly}


def test_index_quote_price_reads_lp():
    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(FakeBroker())
    assert res.index_quote_price() == 23140.50


def test_nearest_expiry_raises_when_calendar_empty():
    from app.broker.instruments import InstrumentResolver
    from app.broker.noren_client import BrokerError

    res = InstrumentResolver(FakeBroker())
    with pytest.raises(BrokerError) as exc:
        res.nearest_expiry(dt.date(2030, 1, 1), [])
    assert "refusing to trade" in str(exc.value)


def test_unwrap_passes_through_row_lists():
    """Regression: the SDK returns books as plain lists.

    _unwrap used to fall off the end for any multi-row list and return None,
    which silently turned a real 4-row position book into count=0.
    """
    from app.broker.noren_client import BrokerError, _unwrap

    rows = [{"stat": "Ok", "tsym": "A", "netqty": "65"},
            {"stat": "Ok", "tsym": "B", "netqty": "-65"}]
    assert _unwrap(rows, context="get_positions") == rows
    assert _unwrap([], context="get_order_book") == []
    assert _unwrap([rows[0]], context="get_positions") == [rows[0]]
    # A single-row list carrying a Not_Ok is still a disguised error.
    assert _unwrap([{"stat": "Not_Ok", "emsg": "no data"}],
                   context="get_order_book") is None
    # Session death must stay distinguishable from an empty book.
    import pytest as _pytest
    with _pytest.raises(BrokerError):
        _unwrap(None, context="searchscrip")
    from app.broker.noren_client import SessionExpiredError
    with _pytest.raises(SessionExpiredError):
        _unwrap({"stat": "Not_Ok", "emsg": "Session Expired"}, context="x")


def test_book_getters_tolerate_empty_broker_answer():
    """The SDK flattens 'no data' to None; books must return [] not raise."""
    from app.broker.noren_client import ShoonyaClient

    class Api:
        def get_order_book(self):
            return None

        def get_positions(self):
            return None

    c = ShoonyaClient()
    c._api = Api()
    c._session_ok = True
    assert c.get_order_book() == []
    assert c.get_positions() == []


# --- OAuth login URL -------------------------------------------------------
# Shoonya's own /OAuthlogin/ SPA (assets/Auth-*.js) reads client_id from the
# query string, and the vendor sample's "NorenWeb/authorize/oauth" path 404s.
# Pin the shape so the stale URL cannot creep back in.

def test_oauth_url_is_the_live_authorize_page():
    from app.config import settings

    assert settings.oauth_url.startswith("https://api.shoonya.com/")
    assert settings.oauth_url.endswith("/OAuthlogin/authorize/oauth")
    assert "NorenWeb/authorize" not in settings.oauth_url


def test_getoauth_url_appends_client_id():
    from NorenRestApiPy.NorenApi import NorenApi

    from app.config import settings

    api = NorenApi(host=settings.host, websocket=settings.websocket)
    url = api.getOAuthURL(settings.oauth_url, "CLIENT123")
    assert url == settings.oauth_url + "?client_id=CLIENT123"


def test_token_exchange_host_matches_oauth_host():
    """GenAcsTok must sit on the same host as the authorize page, otherwise an
    auth_code minted in the browser can never be redeemed."""
    from urllib.parse import urlparse

    from app.config import settings

    assert urlparse(settings.oauth_url).netloc == urlparse(settings.host).netloc
    assert settings.host.rstrip("/").endswith("NorenWClientAPI")


def test_phase2_reports_broker_error_without_crashing(monkeypatch, capsys):
    """getAccessToken() raises (HTML error page) instead of returning None."""
    import scripts.oauth_login as ol

    class BoomApi:
        def getAccessToken(self, *a, **k):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    fake_settings = type("S", (), {
        "client_id": "CID", "secret_code": "SEC", "uid": "UID"})()
    monkeypatch.setattr(ol, "_make_api", lambda: (BoomApi(), fake_settings))
    monkeypatch.setattr(ol, "_env_value", lambda name: "")

    rc = ol.phase2_exchange("an-auth-code")
    assert rc == 2
    out = capsys.readouterr().out
    assert "token exchange raised ValueError" in out
    assert "auth_code is expired" in out


def test_phase2_never_writes_env_on_rejection(monkeypatch):
    import scripts.oauth_login as ol

    written = {}
    monkeypatch.setattr(
        ol, "_write_env_values", lambda vals: written.update(vals))

    class NullApi:
        def getAccessToken(self, *a, **k):
            return None

    fake_settings = type("S", (), {
        "client_id": "CID", "secret_code": "SEC", "uid": "UID"})()
    monkeypatch.setattr(ol, "_make_api", lambda: (NullApi(), fake_settings))
    monkeypatch.setattr(ol, "_env_value", lambda name: "")

    assert ol.phase2_exchange("bad-code") == 2
    assert written == {}

