"""The Excel trade journal: accounting, resilience, and the session hook.

Three promises these tests pin down:

  1. the money is right - a sold leg's P&L is (entry - exit) x qty less
     brokerage, in rupees AND in points via the broker's own lot size;
  2. reporting can never break a trading day - every failure mode (unwritable
     file, dead broker session, full disk) is logged, not raised;
  3. one file per day, re-runnable - re-journaling a day replaces it instead of
     duplicating rows or spawning a second workbook.
"""
import datetime as dt
import threading
import types

import pandas as pd
import pytest

from app import journal as J
from app.execution.engine import Fill

DAY = dt.date(2026, 9, 28)
LOT = 65
BROKERAGE = 20.0


def fill(action, right, price, reason, when, mode="live", order_id=None,
         strike=25000, qty=LOT):
    return Fill(ts=when, right=right, strike=strike, token=f"T{right}",
                action=action, qty=qty, price=price, reason=reason,
                tag=f"{right}{strike}-{action[:2]}-abc", mode=mode,
                order_id=order_id, symbol=f"NIFTY29SEP26{right}{strike}",
                exchange="NFO")


def straddle(mode="live"):
    """One ATM straddle: sell both legs at 09:20, exit CE up PE down."""
    return [
        fill("SELL", "CE", 120.0, "ENTRY", dt.datetime(2026, 9, 28, 9, 20, 1),
             mode=mode, order_id="1001" if mode == "live" else None),
        fill("SELL", "PE", 100.0, "ENTRY", dt.datetime(2026, 9, 28, 9, 20, 2),
             mode=mode, order_id="1002" if mode == "live" else None),
        fill("BUY", "CE", 90.0, "TARGET", dt.datetime(2026, 9, 28, 11, 0),
             mode=mode, order_id="1003" if mode == "live" else None),
        fill("BUY", "PE", 130.0, "SL", dt.datetime(2026, 9, 28, 12, 0),
             mode=mode, order_id="1004" if mode == "live" else None),
    ]


# --- P&L ---------------------------------------------------------------------
def test_positions_pair_each_leg_and_price_the_round_trip():
    rows = {r["right"]: r for r in J.position_rows(
        straddle(), day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)}
    assert set(rows) == {"CE", "PE"}
    ce = rows["CE"]
    assert ce["entry_price"] == 120.0 and ce["exit_price"] == 90.0
    assert ce["gross_pts"] == 30.0
    assert ce["costs_inr"] == 40.0
    assert ce["pnl_inr"] == pytest.approx(30.0 * LOT - 40.0)
    # Points are the derived figure, rounded to 2dp for the sheet.
    assert ce["pnl_pts"] == pytest.approx((30.0 * LOT - 40.0) / LOT, abs=0.01)
    assert ce["is_open"] is False
    assert ce["order_in"] == "1001" and ce["order_out"] == "1003"
    # A leg stopped out at a HIGHER premium than it was sold for loses money.
    pe = rows["PE"]
    assert pe["pnl_inr"] == pytest.approx((100.0 - 130.0) * LOT - 40.0)
    assert pe["pnl_inr"] < 0


def test_day_net_is_the_sum_of_both_legs():
    rows = J.position_rows(straddle(), day=DAY, lots=1, lot_size=LOT,
                           brokerage=BROKERAGE)
    assert sum(r["pnl_inr"] for r in rows) == pytest.approx(-80.0)
    # Two orders per closed leg, two legs: brokerage is the whole day's loss.
    assert sum(r["costs_inr"] for r in rows) == 80.0


def test_points_column_is_blank_without_a_lot_size_not_guessed():
    """No broker lot size means no points figure: 1 lot is not always 65."""
    rows = J.position_rows(straddle(), day=DAY, lots=1, lot_size=None,
                           brokerage=BROKERAGE)
    assert all(r["pnl_pts"] is None for r in rows)
    assert all(r["pnl_inr"] is not None for r in rows)


def test_unmatched_exit_is_recorded_and_flagged():
    """A BUY with no opening SELL must still appear, marked, never hidden."""
    rows = J.position_rows(
        [fill("BUY", "CE", 50.0, "SQUAREOFF", dt.datetime(2026, 9, 28, 15, 15))],
        day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(rows) == 1
    assert rows[0]["pnl_inr"] is None
    assert "without matching entry" in rows[0]["note"]


def test_open_position_is_not_valued_at_the_entry_price():
    """An unclosed leg has no P&L yet - writing 0.0 would misstate the day."""
    rows = J.position_rows(
        [fill("SELL", "CE", 120.0, "ENTRY", dt.datetime(2026, 9, 28, 9, 20))],
        day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(rows) == 1 and rows[0]["is_open"] is True
    assert rows[0]["pnl_inr"] is None and rows[0]["pnl_pts"] is None
    assert "STILL OPEN" in rows[0]["note"]


def test_two_entries_on_the_same_strike_pair_fifo():
    """A re-entry must pair with its own exit, not with the day's first sell."""
    rows = J.position_rows([
        fill("SELL", "CE", 100.0, "ENTRY", dt.datetime(2026, 9, 28, 9, 20)),
        fill("BUY", "CE", 80.0, "SL", dt.datetime(2026, 9, 28, 10, 0)),
        fill("SELL", "CE", 90.0, "ENTRY", dt.datetime(2026, 9, 28, 10, 30)),
        fill("BUY", "CE", 70.0, "TARGET", dt.datetime(2026, 9, 28, 14, 0)),
    ], day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(rows) == 2
    assert {r["gross_pts"] for r in rows} == {20.0}
    assert len({(r["entry_time"], r["exit_time"]) for r in rows}) == 2


def test_paper_fills_are_journaled_too_and_tagged():
    """Paper days are the dry run reporting reads, so they must not be skipped."""
    rows = J.fill_rows(straddle(mode="paper"), day=DAY)
    assert {r["mode"] for r in rows} == {"paper"}
    assert all(r["order_id"] == "" for r in rows)   # paper has no broker id
    assert rows[0]["date"] == "2026-09-28" and rows[0]["time"] == "09:20:01"


# --- files -------------------------------------------------------------------
def test_daily_workbook_has_every_sheet(tmp_path):
    jr = J.Journal(DAY, params={"lots": 1}, lots=1, lot_size=LOT,
                   brokerage=BROKERAGE, base=tmp_path)
    path = jr.record(straddle(), status={"status": "running",
                                         "phase": "IN_POSITION"})
    assert path is not None and path.exists()
    assert path.name == f"trades_{DAY}.xlsx"
    with pd.ExcelFile(path) as xl:
        assert xl.sheet_names == ["Summary", "Positions", "Fills", "Orders",
                                 "Tradebook"]
    loaded = J.load_day(DAY, base=tmp_path)
    assert len(loaded["Fills"]) == 4 and len(loaded["Positions"]) == 2


def test_recording_the_same_day_twice_rewrites_one_file(tmp_path):
    """Reruns and restarts must not leave a second copy of the day behind."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle()[:2], status={"status": "running"})
    jr.record(straddle(), status={"status": "running"})
    jr.record(straddle(), status={"status": "done"}, force=True)
    assert [p.name for p in (tmp_path / "daily").glob("*.xlsx")] == \
        [f"trades_{DAY}.xlsx"]
    assert len(J.load_day(DAY, base=tmp_path)["Fills"]) == 4
    assert jr.writes == 3 and jr.failures == 0


def test_finalize_writes_one_master_row_and_a_rerun_does_not_duplicate_it(tmp_path):
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "running"})
    path = jr.finalize({"status": "done", "phase": "DONE"},
                       started_at="2026-09-28 09:15:00",
                       finished_at="2026-09-28 15:16:00")
    assert path is not None and J.master_path(tmp_path).exists()
    master = J.load_master(base=tmp_path)
    assert len(master) == 1
    assert master[0]["date"] == "2026-09-28"
    assert master[0]["weekday"] == "Mon"
    assert master[0]["pnl_inr"] == pytest.approx(-80.0)
    assert master[0]["positions"] == 2
    assert master[0]["open_positions"] == 0
    # Re-finalize (restart, or stop pressed twice): still exactly one row.
    jr.finalize({"status": "done", "error": "stopped by operator"},
                finished_at="2026-09-28 15:20:00")
    master = J.load_master(base=tmp_path)
    assert len(master) == 1
    assert master[0]["finished_at"] == "2026-09-28 15:20:00"
    assert master[0]["error"] == "stopped by operator"


def test_master_keeps_every_other_day_when_one_day_is_rewritten(tmp_path):
    for day, exit_px in ((dt.date(2026, 9, 28), 90.0),
                         (dt.date(2026, 9, 29), 60.0)):
        jr = J.Journal(day, lots=1, lot_size=LOT, brokerage=BROKERAGE,
                       base=tmp_path)
        jr.record([fill("SELL", "CE", 100.0, "ENTRY",
                        dt.datetime(day.year, day.month, day.day, 9, 20)),
                   fill("BUY", "CE", exit_px, "SQUAREOFF",
                        dt.datetime(day.year, day.month, day.day, 15, 15))],
                  status={"status": "running"})
        jr.finalize({"status": "done"})
    assert J.available_days(base=tmp_path) == ["2026-09-28", "2026-09-29"]
    master = J.load_master(base=tmp_path)
    assert [m["date"] for m in master] == ["2026-09-28", "2026-09-29"]
    # (100-90)*65 - 40 and (100-60)*65 - 40
    assert [m["pnl_inr"] for m in master] == [pytest.approx(610.0),
                                              pytest.approx(2560.0)]


def test_write_master_can_preserve_stored_rows(tmp_path):
    """A rebuild passes prefer_new=False so stored numbers win."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done"})
    jr.finalize({"status": "done"})
    J.write_master([{"date": "2026-09-28", "pnl_inr": 999999.0}],
                   base=tmp_path, prefer_new=False)
    assert J.load_master(base=tmp_path)[0]["pnl_inr"] == pytest.approx(-80.0)
    J.write_master([{"date": "2026-09-28", "pnl_inr": 999999.0}],
                   base=tmp_path, prefer_new=True)
    assert J.load_master(base=tmp_path)[0]["pnl_inr"] == pytest.approx(999999.0)


def test_write_master_of_nothing_writes_nothing(tmp_path):
    assert J.write_master([], base=tmp_path) is None
    assert J.available_days(base=tmp_path) == []


def test_load_day_of_an_absent_day_says_so(tmp_path):
    out = J.load_day(DAY, base=tmp_path)
    assert out["exists"] is False and "Positions" not in out
    assert J.load_master(base=tmp_path) == []
    assert J.latest_workbook(base=tmp_path) is None


# --- resilience --------------------------------------------------------------
def test_unwritable_workbook_is_reported_not_raised(tmp_path, monkeypatch):
    """The journal must never be the reason a trading day dies."""
    def boom(*a, **k):
        raise PermissionError("file is open in Excel")

    monkeypatch.setattr(J.pd, "ExcelWriter", boom)
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    assert jr.record(straddle(), status={"status": "done"}) is None
    assert jr.finalize({"status": "done"}) is None
    assert jr.writes == 0
    # record()'s workbook, finalize()'s workbook, finalize()'s master sheet.
    assert jr.failures == 3
    assert list(tmp_path.rglob(".tmp_*")) == []     # no litter from failures


def test_corrupt_master_is_rebuilt_not_fatal(tmp_path, monkeypatch):
    """An unreadable master costs no data: it is regenerated from scratch."""
    def boom(*a, **k):
        raise ValueError("File is not a zip file")

    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done"})
    monkeypatch.setattr(J.pd, "read_excel", boom)
    assert jr.finalize({"status": "done"}) is not None
    monkeypatch.undo()
    assert len(J.load_master(base=tmp_path)) == 1


def test_disabled_journal_writes_nothing(tmp_path):
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE,
                   base=tmp_path, enabled=False)
    assert jr.record(straddle(), status={"status": "done"}) is None
    assert jr.finalize({"status": "done"}) is None
    assert list(tmp_path.rglob("*.xlsx")) == []


def test_dead_broker_session_still_journals_the_fills(tmp_path):
    """A session that dies at square-off must not cost the day's trade record."""
    class DeadClient:
        live_armed = False

        def get_order_book(self):
            raise RuntimeError("Not_Ok: session expired")

    jr = J.Journal(DAY, client=DeadClient(), lots=1, lot_size=LOT,
                   brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "running"})
    path = jr.finalize({"status": "done"}, fetch_orders=True)
    assert path is not None and path.exists()
    assert jr.orders == []                       # no orders sheet data, but...
    loaded = J.load_day(DAY, base=tmp_path)
    assert len(loaded["Positions"]) == 2          # ... the trades are all there


def test_safe_journal_wrapper_swallows_everything(tmp_path):
    """run_day only ever touches the journal through _safe_journal."""
    import app.session as session

    broken = J.Journal(DAY, lots=1, lot_size=LOT, base=tmp_path)
    broken.record = types.MethodType(
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk gone")), broken)
    assert session._safe_journal(broken.record, []) is None


# --- fills come from the engine, not from memory ------------------------------
def test_record_reads_fills_live_off_the_engine(tmp_path):
    """Attaching the engine means a fill cannot be forgotten by the caller."""
    engine = types.SimpleNamespace(fills=[])
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.attach_day(engine=engine, lot_size=LOT)
    jr.record(status={"status": "running"})
    assert len(J.load_day(DAY, base=tmp_path)["Fills"]) == 0
    engine.fills.append(fill("SELL", "CE", 120.0, "ENTRY",
                             dt.datetime(2026, 9, 28, 9, 20)))
    jr.record(status={"status": "running"})
    assert len(J.load_day(DAY, base=tmp_path)["Fills"]) == 1
    jr.finalize({"status": "done"})
    master = J.load_master(base=tmp_path)
    assert master[0]["fills"] == 1
    assert master[0]["open_positions"] == 1
    assert master[0]["lot_size"] == LOT


def test_record_throttles_but_new_data_always_writes(tmp_path):
    """The poll loop calls record() every few seconds; the disk is spared."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE,
                   base=tmp_path, min_interval=3600.0)
    rows = straddle()
    jr.record(rows[:2], status={"status": "running"})
    assert jr.writes == 1
    jr.record(rows[:2], status={"status": "running"})     # unchanged: no rewrite
    assert jr.writes == 1
    jr.record(rows, status={"status": "running"})         # new fills: rewrite
    assert jr.writes == 2
    jr.record(rows, status={"status": "done"}, force=True)  # forced: rewrite
    assert jr.writes == 3


def test_attach_day_does_not_let_placeholders_shadow_real_values(tmp_path):
    jr = J.Journal(DAY, lots=1, lot_size=LOT, base=tmp_path)
    jr.attach_day(params={"ce_symbol": None, "lots": 1})
    assert jr.params == {"lots": 1}
    jr.attach_day(client="c", params={"lot_size": LOT})
    assert jr.client == "c" and jr.lot_size == LOT


def test_locked_target_saves_a_rescue_copy_instead_of_losing_the_day(
        tmp_path, monkeypatch):
    """A workbook open in Excel locks its file; the data must still be written."""
    real = J.os.replace
    state = {"n": 0}

    def flaky(src, dst):
        state["n"] += 1
        if state["n"] == 1:                    # the day workbook is "locked"
            raise PermissionError("busy")
        return real(src, dst)

    monkeypatch.setattr(J.os, "replace", flaky)
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    path = jr.record(straddle(), status={"status": "done"})
    assert path is not None
    assert path.name.startswith(f"trades_{DAY}-") and path.exists()
    assert len(J.load_day(path)["Fills"]) == 4
    # A rescue copy is not counted as a second trading day.
    assert J.available_days(base=tmp_path) == []
    # And no temp files are left behind.
    assert not [p for p in (tmp_path / "daily").glob(".tmp_*")]


# --- broker book -------------------------------------------------------------
# Field names below are copied from this account's live GetOrderBook response
# (captured 2026-09-28): `trantype`/`exch_tm`/`remarks`/`fillshares`/`avgprc`,
# not the `bs`/`exchtime`/`rk` names the older docs use. They are pinned here so
# a mapping that silently writes blanks gets caught.
LIVE_ORDER_ROWS = [
    {"stat": "Ok", "norenordno": "26092800216190", "exch": "NFO",
     "tsym": "NIFTY29SEP26C22850", "trantype": "B", "prctyp": "LMT",
     "qty": "65", "prc": "135.90", "avgprc": "136.25", "fillshares": "65",
     "prd": "M", "status": "COMPLETE", "ordersource": "API",
     "exch_tm": "28-09-2026 12:44:05", "norentm": "12:44:05 28-09-2026",
     "remarks": "CE22850-BU-57581e4e", "rejreason": " "},
    {"stat": "Ok", "norenordno": "26092800213887", "exch": "NFO",
     "tsym": "NIFTY29SEP26P22850", "trantype": "S", "prctyp": "MKT",
     "qty": "65", "prc": "68.45", "avgprc": "", "fillshares": "",
     "prd": "M", "status": "REJECTED", "ordersource": "API",
     "exch_tm": "28-09-2026 12:41:12",
     "rejreason": "RED:RULE:{Check CASH Ratio}Shortfall:INR 5848.92"},
    {"stat": "Ok", "norenordno": "26092800220603", "exch": "NFO",
     "tsym": "NIFTY29SEP26P22850", "trantype": "B", "prctyp": "SL-LMT",
     "qty": "65", "prc": "140.00", "trgprc": "136.00", "prd": "M",
     "status": "TRIGGER_PENDING", "ordersource": "MOB", "exch_tm": ""},
]


def test_order_rows_use_the_real_noren_field_names():
    rows = J.order_rows(LIVE_ORDER_ROWS, day=DAY)
    assert len(rows) == 3
    first = rows[0]
    # 'dd-MM-yyyy HH:mm:ss' -> ISO date + clock, so the sheet sorts correctly.
    assert first["date"] == "2026-09-28" and first["time"] == "12:44:05"
    assert first["order_id"] == "26092800216190"
    assert first["action"] == "BUY"                 # trantype B, expanded
    assert first["symbol"] == "NIFTY29SEP26C22850"
    assert first["source"] == "API"
    assert first["qty"] == 65 and first["fill_qty"] == 65
    assert first["limit_price"] == 135.9 and first["avg_price"] == 136.25
    assert first["price_type"] == "LMT" and first["product"] == "M"
    assert first["status"] == "COMPLETE"
    # The engine's tag rides in `remarks` - the only exact link between an
    # order at the broker and a fill in this journal.
    assert first["tag"] == "CE22850-BU-57581e4e"
    # A rejected order's rejreason is a single space, not a message.
    assert first["reject_reason"] == ""


def test_order_rows_expose_the_rejection_reason():
    """The report must answer 'why did nothing fill' without calling the broker."""
    rows = J.order_rows(LIVE_ORDER_ROWS, day=DAY)
    assert "CASH Ratio" in rows[1]["reject_reason"]
    assert rows[1]["price_type"] == "MKT"     # the order type the broker refuses
    assert rows[1]["fill_qty"] is None        # rejected: nothing executed
    assert rows[2]["source"] == "MOB"         # manual orders stay visible
    assert rows[2]["trigger_price"] == 136.0
    assert rows[2]["date"] == "2026-09-28"    # blank stamp falls back to the day
    assert "norenordno" in rows[2]["raw"]     # ... and every raw field is kept


def test_norentm_time_first_stamp_is_parsed_too():
    """GetOrderBook mixes 'dd-MM-yyyy HH:mm:ss' and 'HH:mm:ss dd-MM-yyyy'."""
    rows = J.order_rows([{"norenordno": "1", "ordersource": "API",
                          "norentm": "12:44:05 28-09-2026",
                          "status": "COMPLETE"}], day=DAY)
    assert rows[0]["date"] == "2026-09-28" and rows[0]["time"] == "12:44:05"


def test_order_rows_survive_empty_and_error_broker_payloads():
    """_unwrap returns None / dict / list depending on the call; none may raise."""
    assert J.order_rows(None, day=DAY) == []
    assert J.order_rows([], day=DAY) == []
    assert J.order_rows({"stat": "Not_Ok", "emsg": "Invalid data"},
                        day=DAY) == []
    assert J.order_rows(["junk", {"norenordno": "8"}], day=DAY)[0][
        "order_id"] == "8"
    assert J.order_rows({"stat": "Ok", "norenordno": "7"}, day=DAY)[0][
        "order_id"] == "7"


def test_trade_book_rows_are_kept_separate_from_the_bots_fills():
    """Live fills are recorded at the REFERENCE price, so the exchange's own
    average has to be visible somewhere or the difference is invisible."""
    rows = J.trade_book_rows([
        {"stat": "Ok", "norenordno": "26092800107328", "flid": "1764245",
         "trantype": "B", "tsym": "NIFTY29SEP26C23100", "flqty": "65",
         "flprc": "24.55", "prc": "0.00", "prctyp": "MKT",
         "exch_tm": "28-09-2026 10:25:22", "remarks": "CE23100-BU-aa11bb22"},
    ], day=DAY)
    assert rows[0]["action"] == "BUY" and rows[0]["qty"] == 65
    assert rows[0]["price"] == 24.55           # the exchange's fill, not the ref
    assert rows[0]["fill_id"] == "1764245"
    assert rows[0]["tag"] == "CE23100-BU-aa11bb22"
    assert J.trade_book_rows(None, day=DAY) == []


# --- summary -----------------------------------------------------------------
def test_summary_counts_rejections_from_the_brokers_own_book():
    orders = J.order_rows(
        [{"norenordno": "1", "status": "Rejected",
          "rejreason": "CASH shortfall"},
         {"norenordno": "2", "status": "Complete"}], day=DAY)
    rows = {r["field"]: r["value"] for r in J.summary_rows(
        day=str(DAY), status={"status": "running"}, positions=[], fills=[],
        params={}, risk={}, config={}, orders=orders)}
    assert rows["Broker orders"] == 2
    assert rows["Broker rejections"] == 1
    assert rows["Rejection reasons"] == "CASH shortfall"


def test_summary_booleans_do_not_corrupt_the_integers_beside_them(tmp_path):
    """A mixed column of True/False and 0/1 reads back as ALL booleans, which
    would silently render 'Lots 0' as 'False'. Booleans are written as yes/no."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done", "live_armed": True,
                                  "entry_deferred": False,
                                  "resolved": {"lot_size": LOT,
                                               "expiry": "2026-09-29"}})
    frame = pd.read_excel(jr.path, sheet_name="Summary")
    values = dict(zip(frame["field"], frame["value"]))
    assert values["Lots"] == 1
    assert values["Fills recorded"] == 4
    assert values["Positions closed"] == 2
    assert values["Live armed"] == "yes"
    assert values["Entry deferred"] == "no"
    assert values["Expiry"] == "2026-09-29"


def test_summary_carries_the_entry_deferral_cause():
    """The report must not repeat the bug of blaming a rejection on quotes."""
    rows = {r["field"]: r["value"] for r in J.summary_rows(
        day=str(DAY),
        status={"status": "running", "entry_deferred": True,
                "entry_deferred_reason": "rejected", "broker_rejections": 5},
        positions=[], fills=[], params={}, risk={}, config={}, orders=[])}
    assert rows["Entry deferred"] == "yes"
    assert rows["Entry deferred reason"] == "rejected"
    assert rows["Strategy entry rejections"] == 5


def test_master_booleans_stay_real_booleans(tmp_path):
    """The master has one column per flag, so TRUE/FALSE there are unambiguous."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done", "live_armed": False})
    jr.finalize({"status": "done", "live_armed": False})
    frame = pd.read_excel(J.master_path(tmp_path), sheet_name=J.MASTER_SHEET)
    assert bool(frame["live_armed"].iloc[0]) is False
    assert frame["pnl_inr"].iloc[0] == pytest.approx(-80.0)


# --- reconstruction from the broker's own record ------------------------------
# Rows shaped exactly as today's live GetOrderBook, mapped through order_rows()
# so the reconstruction is exercised against the real column names.
BOT_ORDER_ROWS = J.order_rows([
    {"norenordno": "213876", "tsym": "NIFTY29SEP26C22850", "exch": "NFO",
     "trantype": "S", "prctyp": "LMT", "qty": "65", "prc": "103.80",
     "avgprc": "104.10", "fillshares": "65", "prd": "M", "status": "COMPLETE",
     "ordersource": "API", "exch_tm": "28-09-2026 12:40:18",
     "remarks": "CE22850-SE-00e30285"},
    {"norenordno": "214380", "tsym": "NIFTY29SEP26C22850", "exch": "NFO",
     "trantype": "B", "prctyp": "LMT", "qty": "65", "prc": "136.30",
     "avgprc": "102.90", "fillshares": "65", "prd": "M", "status": "COMPLETE",
     "ordersource": "API", "exch_tm": "28-09-2026 12:41:04",
     "remarks": "CE22850-BU-277289aa"},
    {"norenordno": "213887", "tsym": "NIFTY29SEP26P22850", "exch": "NFO",
     "trantype": "S", "prctyp": "LMT", "qty": "65", "prc": "68.45",
     "prd": "M", "status": "REJECTED", "ordersource": "API",
     "exch_tm": "28-09-2026 12:41:12", "remarks": "PE22850-SE-9eb5eb49",
     "rejreason": "RED:RULE:{Check CASH Ratio}Shortfall:INR 5848.92"},
    {"norenordno": "214182", "tsym": "NIFTY29SEP26P22850", "exch": "NFO",
     "trantype": "S", "prctyp": "LMT", "qty": "65", "prc": "68.45",
     "avgprc": "68.90", "fillshares": "65", "prd": "M", "status": "COMPLETE",
     "ordersource": "MOB", "exch_tm": "28-09-2026 12:35:00"},
], day=DAY)


def test_reconstruct_fills_reads_only_the_bots_executed_orders():
    rows = J.reconstruct_fills(BOT_ORDER_ROWS)
    tags = {r["tag"] for r in rows}
    assert tags == {"CE22850-SE-00e30285", "CE22850-BU-277289aa"}
    # The rejected order executed nothing and the manual MOB order is not the
    # bot's; jouralling either would invent trades that never happened.
    assert "PE22850-SE-9eb5eb49" not in tags
    assert all(r["mode"] == "live" and r["reason"] == "BROKER" for r in rows)
    sold = [r for r in rows if r["action"] == "SELL"][0]
    assert sold["right"] == "CE" and sold["strike"] == 22850
    assert sold["qty"] == 65.0
    # The exchange's average, not the limit: this is broker truth, not intent.
    assert sold["price"] == 104.1
    assert sold["ts"] == "2026-09-28 12:40:18"


def test_reconstruct_fills_ignores_junk_and_unowned_tags():
    assert J.reconstruct_fills([]) == []
    assert J.reconstruct_fills(None) == []
    for junk in ({"tag": "some-random-remark", "status": "COMPLETE",
                  "qty": 10, "avg_price": 5},
                 {"tag": "CE100-SE-zzzzzzzz", "status": "COMPLETE",
                  "qty": 10, "avg_price": 5},          # suffix not hex
                 {"tag": "CE100-SE-abcdef01", "status": "PENDING",
                  "qty": 65, "avg_price": 5},          # not executed
                 {"tag": "CE100-SE-abcdef01", "status": "COMPLETE",
                  "qty": 65, "avg_price": None, "limit_price": None}):
        assert J.reconstruct_fills([junk]) == [], junk["tag"]


def test_reconstruction_pairs_into_a_real_position_view():
    positions = J.position_rows(J.reconstruct_fills(BOT_ORDER_ROWS), day=DAY,
                               lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(positions) == 1 and positions[0]["is_open"] is False
    # Sold at 104.10, bought at 102.90 -> 1.2 pts x 65 less two Rs20 orders.
    assert positions[0]["pnl_inr"] == pytest.approx(1.2 * 65 - 40)
    assert positions[0]["note"] == ""


def test_merge_fills_keeps_the_better_record():
    """The session's own fill carries the strategy's reference price, so a
    reconstruction fills gaps without overwriting what was seen live."""
    live = {"tag": "CE100-SE-aaaa1111", "action": "SELL", "right": "CE",
            "strike": 100, "qty": 65, "price": 103.8,
            "ts": "2026-09-28 09:20:01"}
    rebuilt = dict(live, price=104.1, tag="PE100-BU-bbbb2222")
    merged = {m["tag"]: m for m in J.merge_fills([live],
                                                 [rebuilt, dict(live, price=999)])}
    assert len(merged) == 2
    assert merged["CE100-SE-aaaa1111"]["price"] == 103.8   # session wins
    assert merged["PE100-BU-bbbb2222"]["price"] == 104.1   # gap filled


def test_merge_fills_keeps_untagged_rows():
    a = {"tag": "", "action": "SELL", "ts": "x"}
    b = {"tag": "", "action": "BUY", "ts": "y"}
    assert len(J.merge_fills([a], [b])) == 2
    assert J.merge_fills() == []


# --- pairing is by count, not by arrival -------------------------------------
def test_a_buy_stamped_before_its_sell_still_pairs_and_is_flagged():
    """Noren stamps the exchange's acknowledgement, not the submission, so a
    buy-back can land a few seconds ahead of the sell it closes. Pairing by
    arrival order reported that day as holding an open short that did not exist."""
    rows = [
        {"ts": "2026-09-28 12:41:28", "action": "BUY", "right": "PE",
         "strike": 22850, "qty": 65, "price": 69.65, "reason": "BROKER",
         "order_id": "214599"},
        {"ts": "2026-09-28 12:41:35", "action": "SELL", "right": "PE",
         "strike": 22850, "qty": 65, "price": 68.90, "reason": "BROKER",
         "order_id": "214648"},
    ]
    positions = J.position_rows(rows, day=DAY, lots=1, lot_size=LOT,
                               brokerage=BROKERAGE)
    assert len(positions) == 1
    leg = positions[0]
    assert leg["is_open"] is False            # the book is flat, so it reads flat
    assert "precedes entry" in leg["note"]    # ... and the oddity is still flagged
    assert leg["pnl_inr"] == pytest.approx((68.90 - 69.65) * 65 - 40)


def test_a_genuine_open_short_is_still_reported_open():
    """Pairing by count must not hide a short that really has no exit."""
    positions = J.position_rows(
        [{"ts": "2026-09-28 09:20:01", "action": "SELL", "right": "CE",
          "strike": 25000, "qty": 65, "price": 120.0, "reason": "ENTRY"}],
        day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(positions) == 1
    assert positions[0]["is_open"] is True
    assert "STILL OPEN" in positions[0]["note"]
    assert positions[0]["pnl_inr"] is None


def test_an_orphan_buy_is_flagged_not_paired_with_nothing():
    positions = J.position_rows(
        [{"ts": "2026-09-28 09:20:01", "action": "BUY", "right": "CE",
          "strike": 25000, "qty": 65, "price": 120.0, "reason": "SL",
          "order_id": "9"}],
        day=DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(positions) == 1
    assert "without matching entry" in positions[0]["note"]
    assert positions[0]["is_open"] is False
    assert positions[0]["pnl_inr"] is None


def test_re_entry_still_yields_two_rows_under_count_pairing():
    sell_a = {"ts": "2026-09-28 09:20:01", "action": "SELL", "right": "CE",
              "strike": 25000, "qty": 65, "price": 100.0, "reason": "ENTRY"}
    buy_a = {"ts": "2026-09-28 10:00:00", "action": "BUY", "right": "CE",
             "strike": 25000, "qty": 65, "price": 80.0, "reason": "SL"}
    sell_b = dict(sell_a, ts="2026-09-28 10:30:00", price=90.0)
    buy_b = dict(buy_a, ts="2026-09-28 14:00:00", price=70.0)
    positions = J.position_rows([sell_a, buy_a, sell_b, buy_b], day=DAY,
                               lots=1, lot_size=LOT, brokerage=BROKERAGE)
    assert len(positions) == 2
    assert {p["gross_pts"] for p in positions} == {20.0, 20.0}
    assert all(not p["is_open"] for p in positions)


# --- reconciliation ------------------------------------------------------------
def test_reconciliation_flags_orders_accepted_but_not_journaled():
    """The real incident: an order's response raised while the exchange had
    already accepted it, so the bot believed it had sent nothing. Only the
    broker's own book contradicts that."""
    fills = [{"tag": "CE22850-SE-00e30285", "action": "SELL", "right": "CE",
              "strike": 22850, "qty": 65, "price": 104.1, "mode": "live"}]
    recon = J.reconciliation(BOT_ORDER_ROWS, fills)
    assert recon["api_orders"] == 3                  # the MOB row is not API
    assert recon["api_accepted"] == 2
    assert recon["api_rejected"] == 1
    assert recon["matched"] == 1
    assert [o["order_id"] for o in recon["orphaned"]] == ["214380"]
    assert recon["clean"] is False


def test_reconciliation_is_clean_when_both_books_agree():
    recon = J.reconciliation(BOT_ORDER_ROWS,
                             J.reconstruct_fills(BOT_ORDER_ROWS))
    assert recon["clean"] is True
    assert recon["orphaned"] == [] and recon["unfilled"] == []
    assert recon["matched"] == 2


def test_reconciliation_flags_a_journaled_live_fill_the_broker_denies():
    fills = [{"tag": "CE99999-SE-deadbeef", "action": "SELL", "right": "CE",
              "strike": 99999, "qty": 65, "price": 1.0, "mode": "live"}]
    recon = J.reconciliation(BOT_ORDER_ROWS, fills)
    assert [f["tag"] for f in recon["unfilled"]] == ["CE99999-SE-deadbeef"]
    assert recon["clean"] is False


def test_paper_fills_are_never_counted_as_missing_at_the_broker():
    """Paper mode sends nothing, so "no broker order behind this fill" is the
    expected state for a paper row, not a defect.

    Two directions are pinned: a paper day whose book is empty is clean, and a
    paper row is not flagged as missing even when the book does hold API orders
    belonging to some other (live) session.
    """
    fills = [{"tag": "CE100-SE-aaaa1111", "action": "SELL", "mode": "paper",
              "right": "CE", "strike": 100, "qty": 65, "price": 1.0}]
    empty = J.reconciliation([], fills)
    assert empty["clean"] is True and empty["unfilled"] == []
    mixed = J.reconciliation(BOT_ORDER_ROWS, fills)
    assert mixed["unfilled"] == []          # paper excluded
    assert mixed["api_accepted"] == 2       # those orders are still reported


def test_summary_and_master_surface_an_unclean_reconciliation(tmp_path):
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.orders = [dict(o) for o in BOT_ORDER_ROWS]
    jr.record([f for f in J.reconstruct_fills(BOT_ORDER_ROWS)
               if f["tag"] == "CE22850-SE-00e30285"],
              status={"status": "done"})
    path = jr.finalize({"status": "done"})
    summary = {r["field"]: r["value"] for r in J.load_day(path)["Summary"]}
    assert summary["MATCHES"] == "NO - see note"
    assert "214380" in str(summary["WARNING: accepted at broker, NOT journaled"])
    master = J.load_master(base=tmp_path)[0]
    assert master["recon_matches"] == "NO"
    assert master["broker_orphans"] == 1
    assert master["broker_api_accepted"] == 2


# --- session hook -----------------------------------------------------------
class RefusingClient:
    """A broker that will not even let the session start."""

    live_armed = False
    session_ok = False

    def connect(self):
        return False

    def get_order_book(self):
        return []


def test_run_day_journals_a_day_that_never_reaches_the_market(tmp_path,
                                                             monkeypatch):
    """Connect fails -> the day still gets a workbook and a master row.

    This is why the journal is built before anything can fail: "why did nothing
    trade today" has to be answerable from the report even for a day that never
    got as far as an order.
    """
    import app.session as session

    monkeypatch.setattr(session, "client", RefusingClient())
    st = session.SessionState()
    out = session.run_day(state=st, poll_seconds=0.01, today=DAY,
                          journal=J.Journal(DAY, lots=1, lot_size=LOT,
                                            brokerage=BROKERAGE, base=tmp_path))
    assert out["status"] == "connect_failed"
    days = J.available_days(base=tmp_path)
    assert days == ["2026-09-28"]                # exactly one file for the day
    stored = J.load_day(DAY, base=tmp_path)
    summary = {r["field"]: r["value"] for r in stored["Summary"]}
    assert summary["Session status"] == "connect_failed"
    assert "no session" in str(summary["Session error"])
    assert summary["Fills recorded"] == 0
    master = J.load_master(base=tmp_path)
    assert len(master) == 1
    assert master[0]["session_status"] == "connect_failed"
    assert master[0]["fills"] == 0
    assert out["journal_path"]                   # surfaced to the dashboard
    assert out["journal_writes"] >= 1
    assert out["journal_failures"] == 0


def test_run_day_still_returns_status_when_the_journal_cannot_write(
        tmp_path, monkeypatch):
    """A full disk is a reporting problem, not a trading halt."""
    import app.session as session

    broken = J.Journal(DAY, lots=1, lot_size=LOT, base=tmp_path)
    broken.record = types.MethodType(
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")), broken)
    broken.finalize = types.MethodType(
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")), broken)
    monkeypatch.setattr(session, "client", RefusingClient())
    out = session.run_day(state=session.SessionState(), poll_seconds=0.01,
                          today=DAY, journal=broken)
    assert out["status"] == "connect_failed"
    assert out["journal_path"] is None


def test_weekend_is_journaled_as_a_skipped_day(tmp_path):
    """A non-trading day is recorded as skipped, not missing from the report."""
    import app.session as session

    saturday = dt.date(2026, 10, 3)
    out = session.run_day(state=session.SessionState(), poll_seconds=0.01,
                          today=saturday,
                          journal=J.Journal(saturday, lots=1, lot_size=LOT,
                                            brokerage=BROKERAGE, base=tmp_path))
    assert out["status"] == "skipped"
    assert J.available_days(base=tmp_path) == ["2026-10-03"]
    master = J.load_master(base=tmp_path)
    assert master[0]["session_status"] == "skipped"
    assert master[0]["weekday"] == "Sat"
    assert "not a trading day" in str(master[0]["error"])


class QuoteClient:
    """Answers quotes, then asks to stop so the loop runs a bounded number of polls."""

    live_armed = False
    session_ok = True

    def __init__(self, stop_after=3):
        self.stop_after = stop_after
        self.calls = 0
        self.stop_event = None

    def connect(self):
        return True

    def get_quotes(self, exchange, token):
        self.calls += 1
        if self.stop_event and self.calls >= self.stop_after:
            self.stop_event.set()
        return {"lp": "120.0"}

    def get_order_book(self):
        return []


def _instrument_legs():
    from app.strategy.live import LegInstrument

    return [LegInstrument(token="1001", tradingsymbol="NIFTY29SEP26C25000",
                          strike=25000, right="CE", exchange="NFO",
                          lot_size=LOT),
            LegInstrument(token="1002", tradingsymbol="NIFTY29SEP26P25000",
                          strike=25050, right="PE", exchange="NFO",
                          lot_size=LOT)]


def test_the_journal_is_updated_inside_the_poll_loop(tmp_path, monkeypatch):
    """A fill must not wait until 15:30 to reach the disk.

    The process died mid-day once already. If journaling only happened at the
    close, that day's record would have died with it, so record() is called from
    inside the loop. This pins the call - moving journaling back to the finally
    block alone would fail here.
    """
    import app.session as session

    client = QuoteClient()
    monkeypatch.setattr(session, "client", client)
    # Widened so the test passes whenever it runs, not only between 09:15-15:30.
    monkeypatch.setattr(session, "MARKET_OPEN", dt.time(0, 0))
    monkeypatch.setattr(session, "MARKET_CLOSE", dt.time(23, 59))
    monkeypatch.setattr(session, "resolve_day",
                        lambda res, today: {"spot": 25010.05,
                                            "expiry": dt.date(2026, 9, 29),
                                            "atm": 25000, "lot_size": LOT,
                                            "legs": _instrument_legs()})
    calls = []
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    original = jr.record

    def spy(*a, **k):
        calls.append(len(jr._current_fills()))
        return original(*a, **k)

    jr.record = spy
    stop = threading.Event()
    client.stop_event = stop
    out = session.run_day(state=session.SessionState(), poll_seconds=0.01,
                          today=DAY, stop_event=stop, journal=jr)
    assert "starting" != out["status"]
    # One write right after resolution, then at least one more from the loop.
    assert len(calls) >= 2, calls
    assert jr.last_written is not None and jr.last_written.exists()
    loaded = J.load_day(DAY, base=tmp_path)
    summary = {r["field"]: r["value"] for r in loaded["Summary"]}
    assert summary["Session status"] == "done"
    assert summary["Lot size"] == LOT


# --- round trip / rebuild ----------------------------------------------------
def test_rebuild_recomputes_the_master_from_daily_workbooks(tmp_path):
    """Losing the master must cost nothing: the per-day files rebuild it."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done", "phase": "DONE",
                                 "resolved": {"lot_size": LOT,
                                              "expiry": "2026-09-29"}})
    jr.finalize({"status": "done"}, started_at="09:15", finished_at="15:16")
    before = J.load_master(base=tmp_path)
    assert len(before) == 1

    path = J.master_path(tmp_path)
    path.unlink()
    assert J.load_master(base=tmp_path) == []

    rows = []
    for day in J.available_days(base=tmp_path):
        data = J.load_day(day, base=tmp_path)
        summary = {str(r["field"]): r["value"]
                   for r in data[J.SUMMARY_SHEET]}
        rows.append(J.daily_row(
            day=day,
            status={"status": summary.get("Session status"),
                    "phase": summary.get("Strategy phase"),
                    "resolved": {"expiry": summary.get("Expiry"),
                                 "lot_size": summary.get("Lot size")}},
            positions=data[J.POSITIONS_SHEET], fills=data[J.FILLS_SHEET],
            orders=data[J.ORDERS_SHEET],
            params={"lots": summary.get("Lots")},
            workbook=data["workbook"]))
    assert J.write_master(rows, base=tmp_path) is not None
    after = J.load_master(base=tmp_path)
    assert len(after) == 1
    # Empty cells come back as NaN; the rebuild must not turn them into 0.0 or
    # poison the sum, so the money columns must still match the original run.
    assert after[0]["pnl_inr"] == pytest.approx(before[0]["pnl_inr"])
    assert after[0]["date"] == before[0]["date"]


def test_missing_cells_stay_blank_instead_of_becoming_zero(tmp_path):
    """An unopened leg has no P&L. A 0.0 there would overstate the day.

    A blank cell is how the sheet says "not recorded", and pandas reads it back
    as NaN - which is correct here, because NaN is visibly absent while 0.0
    silently claims a flat leg.
    """
    fills = [fill("SELL", "CE", 120.0, "ENTRY", dt.datetime(2026, 9, 28, 9, 20))]
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(fills, status={"status": "running", "phase": "IN_POSITION"})
    jr.finalize({"status": "done"})
    frame = pd.read_excel(jr.path, sheet_name="Positions")
    assert frame["pnl_inr"].isna().all()
    assert frame["pnl_pts"].isna().all()
    # The day's master row carries the same absence, never a fabricated zero.
    master = pd.DataFrame(J.load_master(base=tmp_path))
    assert pd.isna(master["pnl_inr"].iloc[0])
    assert master["open_positions"].iloc[0] == 1
    assert master["fills"].iloc[0] == 1


# --- API ---------------------------------------------------------------------
@pytest.fixture()
def client_api(tmp_path):
    """A TestClient with the journal pointed at tmp_path.

    Settings is a frozen dataclass and journal_dir is read at call time, so the
    redirect has to go through object.__setattr__ and be restored afterwards -
    otherwise these tests would write workbooks into the real runtime folder.
    """
    from fastapi.testclient import TestClient

    from app import api as api_mod
    from app.config import settings

    original = settings.journal_dir
    object.__setattr__(settings, "journal_dir", tmp_path)
    try:
        yield TestClient(api_mod.app)
    finally:
        object.__setattr__(settings, "journal_dir", original)


def _write_a_day(tmp_path, client_api):
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE, base=tmp_path)
    jr.record(straddle(), status={"status": "done", "phase": "DONE"})
    jr.finalize({"status": "done"})


def test_journal_endpoint_reports_where_the_files_are(client_api, tmp_path):
    info = client_api.get("/api/journal").json()
    assert info["enabled"] is True
    assert info["days"] == [] and info["latest"] is None
    _write_a_day(tmp_path, client_api)
    info = client_api.get("/api/journal").json()
    assert info["days"] == ["2026-09-28"]
    assert info["master_exists"] is True
    assert info["latest"].endswith("trades_2026-09-28.xlsx")


def test_daily_download_is_a_real_workbook(client_api, tmp_path):
    assert client_api.get("/api/journal/daily").status_code == 404
    _write_a_day(tmp_path, client_api)
    res = client_api.get("/api/journal/daily")
    assert res.status_code == 200
    assert "spreadsheetml" in res.headers["content-type"]
    assert "trades_2026-09-28" in res.headers["content-disposition"]
    assert res.content[:2] == b"PK"                 # xlsx is a zip container


def test_master_download(client_api, tmp_path):
    assert client_api.get("/api/journal/master").status_code == 404
    _write_a_day(tmp_path, client_api)
    res = client_api.get("/api/journal/master")
    assert res.status_code == 200
    assert b"trading_journal" in res.headers["content-disposition"].encode()


def test_rebuild_endpoint_recreates_the_master(client_api, tmp_path):
    _write_a_day(tmp_path, client_api)
    assert J.load_master(base=tmp_path)
    J.master_path(tmp_path).unlink()
    res = client_api.post("/api/journal/rebuild")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["days"] == 1
    assert len(J.load_master(base=tmp_path)) == 1


def test_rebuild_endpoint_says_why_when_there_is_nothing_to_rebuild(
        client_api, tmp_path):
    res = client_api.post("/api/journal/rebuild")
    assert res.status_code == 404
    assert "no daily workbooks" in res.json()["reason"]


# --- rewriting a day without losing what an earlier write knew ---------------
# The day's contracts (expiry, ATM, lot size) live in the RAM of whichever
# process traded it. A writer that rebuilds the day from outside - a backfill
# against a server already sitting at `idle`, or a rescue copy written after the
# workbook was locked - has never seen them, and without a carry-forward it
# overwrites a complete record with a thinner one. That is a reporting loss on
# exactly the days most worth reading about.

def _day_with_context(tmp_path, orders=None):
    """A day journaled by the session that actually traded it."""
    jr = J.Journal(DAY, lots=1, lot_size=LOT, brokerage=BROKERAGE,
                   base=tmp_path)
    jr.attach_day(lot_size=LOT, params={"ce_symbol": "NIFTY29SEP26CE25000",
                                        "pe_symbol": "NIFTY29SEP26PE25000"})
    jr.orders = orders if orders is not None else []
    status = {"status": "done", "phase": "WAIT_ENTRY", "atm": 25000,
              "spot": 24990.5, "resolved": {"expiry": "2026-09-29",
                                            "lot_size": LOT}}
    jr.record(straddle(), status=status, force=True)
    jr.executions = [{"date": str(DAY), "time": "11:00:00", "order_id": "1",
                      "fill_id": "f1", "action": "BUY", "symbol": "X",
                      "qty": LOT, "price": 90.0, "tag": "CE25000-BU-abc"}]
    return jr.finalize(status)


def test_a_writer_that_never_resolved_anything_keeps_the_day_context(tmp_path):
    _day_with_context(tmp_path)
    blind = J.Journal(DAY, base=tmp_path)          # no params, no lot size
    blind.record(straddle(), status={"status": "idle"}, force=True)
    summary = {r["field"]: r["value"]
               for r in J.load_day(DAY, base=tmp_path)["Summary"]}
    assert summary["Expiry"] == "2026-09-29"
    assert summary["ATM pinned"] == 25000
    assert summary["Spot ref"] == 24990.5
    assert summary["Lot size"] == LOT
    assert summary["Net P&L (pts)"]               # needs the lot size to exist
    assert summary["CE symbol"] == "NIFTY29SEP26CE25000"


def test_carry_forward_does_not_borrow_a_previous_run_behaviour(tmp_path):
    """What a session was DOING is never inherited; only what the day WAS."""
    _day_with_context(tmp_path)
    blind = J.Journal(DAY, base=tmp_path)
    blind.record(straddle(), status={"status": "idle"}, force=True)
    summary = {r["field"]: r["value"]
               for r in J.load_day(DAY, base=tmp_path)["Summary"]}
    assert summary["Session status"] == "idle"    # this writer's own state
    assert summary["Strategy phase"] is None      # not the earlier run's


def test_an_empty_fetch_does_not_erase_a_recorded_order_book(tmp_path):
    orders = [{"date": str(DAY), "time": "12:40:18", "order_id": "1",
               "source": "API", "action": "SELL", "symbol": "NIFTY29SEP26C25000",
               "exchange": "NFO", "qty": LOT, "fill_qty": LOT,
               "limit_price": 120.0, "avg_price": 120.0, "trigger_price": None,
               "price_type": "LMT", "product": "N", "status": "COMPLETE",
               "reject_reason": None, "tag": "CE25000-SE-abc", "raw": {}}]
    _day_with_context(tmp_path, orders=orders)
    blind = J.Journal(DAY, base=tmp_path)         # no client: nothing fetched
    blind.record(straddle(), status={"status": "idle"}, force=True)
    stored = J.load_day(DAY, base=tmp_path)
    assert len(stored["Orders"]) == 1
    assert len(stored["Tradebook"]) == 1
    summary = {r["field"]: r["value"] for r in stored["Summary"]}
    assert summary["Broker orders"] == 1


def test_carry_forward_reads_the_disk_once_per_journal(tmp_path, monkeypatch):
    """`record()` runs every poll; the prior read must not follow it there."""
    _day_with_context(tmp_path)
    calls = []
    real = J.day_record
    monkeypatch.setattr(J, "day_record",
                        lambda day, base=None: (calls.append(day),
                                                real(day, base=base))[1])
    blind = J.Journal(DAY, base=tmp_path)
    for _ in range(5):
        blind.record(straddle(), status={"status": "idle"}, force=True)
    assert len(calls) <= 1


def test_carry_forward_skips_blanks_and_normalises_dates():
    assert J.carry_forward({}, "2026-09-28") == {}
    assert J.carry_forward(None, "2026-09-28") == {}
    row = {"expiry": dt.datetime(2026, 9, 29), "atm": 22850, "spot_ref": None,
           "lot_size": float("nan"), "started_at": "2026-09-28 09:15:00"}
    out = J.carry_forward(row, "2026-09-28")
    assert out["expiry"] == "2026-09-29"          # midnight Timestamp -> a day
    assert out["atm"] == 22850
    assert out["started_at"] == "2026-09-28 09:15:00"   # a real time is kept
    assert "spot_ref" not in out                  # None is absent, not a value
    assert "lot_size" not in out                  # so is NaN
