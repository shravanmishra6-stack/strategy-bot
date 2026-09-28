"""Excel trade journal: one workbook per trading day, plus a running master.

    runtime/journal/
        daily/trades_YYYY-MM-DD.xlsx   Summary | Positions | Fills | Orders
        trading_journal.xlsx           Daily   (one row per trading day)

Reporting is written from the ExecutionEngine's fills, so the journal records
what the bot ACTUALLY did in both paper and live mode, tagged per row. Nothing
is reconstructed from memory: every row traces back to a fill or a broker book
entry, and any broker field that cannot be verified is left blank rather than
guessed.

Accounting mirrors app/backtest/engine.py so live and backtest numbers compare:

    pnl_inr = (entry_premium - exit_premium) * qty - total_brokerage
    pnl_pts = pnl_inr / lot_size

A sold leg profits as premium falls, which is why the difference is
entry-minus-exit. `qty` is shares (lots x broker lot size), never lots. The
backtest additionally subtracts a modelled `slippage` because it fills at the
bar's theoretical price; live fills already carry a real price, so slippage is
NOT applied again here - doing so would double-count it. Two orders exist per
closed leg (sell to open, buy to close), hence 2 x brokerage; the backtest
charges one order per closed leg, so expect journal net P&L to sit about one
order's brokerage per leg BELOW the backtest for the same fills. That is the
journal being right, not a bug to reconcile.

Writes are atomic (temp file then os.replace) and never raise into the trading
loop: a workbook left open in Excel locks its file, and losing a day's journal
is far worse than a warning in the log. When a target is locked, the fresh copy
lands beside it with a time suffix instead of failing.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import tempfile
from pathlib import Path

import pandas as pd

from app.broker.instruments import MONTH_NAMES
from app.config import settings

log = logging.getLogger("journal")

# Noren writes dates as 'dd-MMM-yyyy' (expiries) and 'dd-MM-yyyy HH:mm:ss'
# (order/exchange times). Reuse the broker module's own month table rather than
# restating it here, so a parsing change only has to happen once.
_MONTHS = {name: f"{i + 1:02d}" for i, name in enumerate(MONTH_NAMES)}

SUMMARY_SHEET = "Summary"
POSITIONS_SHEET = "Positions"
FILLS_SHEET = "Fills"
ORDERS_SHEET = "Orders"
TRADEBOOK_SHEET = "Tradebook"
MASTER_SHEET = "Daily"

SUMMARY_COLUMNS = ["field", "value"]
FILL_COLUMNS = [
    "date", "time", "mode", "action", "right", "strike", "qty", "price",
    "reason", "symbol", "exchange", "order_id", "tag",
]
POSITION_COLUMNS = [
    "date", "right", "strike", "symbol", "qty", "lots", "entry_time",
    "entry_price", "exit_time", "exit_price", "exit_reason", "is_open",
    "mark_price", "gross_pts", "costs_inr", "pnl_pts", "pnl_inr",
    "order_in", "order_out", "note",
]
ORDER_COLUMNS = [
    "date", "time", "order_id", "source", "action", "symbol", "exchange",
    "qty", "fill_qty", "limit_price", "avg_price", "trigger_price",
    "price_type", "product", "status", "reject_reason", "tag", "raw",
]
TRADEBOOK_COLUMNS = [
    "date", "time", "order_id", "fill_id", "action", "symbol", "qty",
    "price", "tag", "raw",
]
DAILY_COLUMNS = [
    "date", "weekday", "mode", "live_armed", "expiry", "atm", "spot_ref",
    "lots", "lot_size", "ce_symbol", "pe_symbol", "phase", "entries",
    "fills", "positions", "open_positions", "broker_rejections",
    "entry_deferred", "entry_deferred_reason", "gross_pts", "gross_inr",
    "costs_inr", "pnl_pts", "pnl_inr", "exit_reasons", "halted_reason",
    "errors", "session_status", "error", "started_at", "finished_at",
    "workbook", "broker_api_orders", "broker_api_accepted", "broker_matched",
    "broker_orphans", "recon_matches",
]


# --- paths -------------------------------------------------------------------
def journal_dir(base=None) -> Path:
    return Path(base or settings.journal_dir)


def daily_path(day, base=None) -> Path:
    """One workbook per trading day: daily/trades_YYYY-MM-DD.xlsx."""
    return journal_dir(base) / "daily" / f"trades_{_day_str(day)}.xlsx"


def master_path(base=None) -> Path:
    return journal_dir(base) / "trading_journal.xlsx"


def latest_workbook(base=None) -> Path | None:
    """Newest daily workbook, so /api/journal can offer a download."""
    folder = journal_dir(base) / "daily"
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.glob("trades_????-??-??.xlsx"))
    return files[-1] if files else None


# --- helpers -----------------------------------------------------------------
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_str(day) -> str:
    """Normalise a day-ish value to 'YYYY-MM-DD' (never a guess: bad input is
    truncated to the date prefix it does have)."""
    if isinstance(day, dt.datetime):
        return day.date().isoformat()
    if isinstance(day, dt.date):
        return day.isoformat()
    text = _text(day).strip().replace("T", " ")
    head = text[:10]
    return head if _DAY_RE.match(head) else (head or dt.date.today().isoformat())


def _num(value):
    """Float or None - a money column must never show a guessed zero."""
    try:
        if value is None or value == "":
            return None
        val = float(value)
    except (TypeError, ValueError):
        return None
    return None if val != val else val  # `!= self` catches NaN


def _round(value, places=2):
    val = _num(value)
    return None if val is None else round(val, places)


def _text(value) -> str:
    # A blank cell re-read from a workbook comes back as NaN, and str() of that
    # is the literal "nan" - which would then be written straight back into the
    # next version of the sheet. Treat it as empty.
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value)


def _vals(rows, key) -> list[float]:
    """Numeric values of `key` across rows, missing/NaN entries dropped.

    A workbook re-read turns an empty cell back into NaN, and NaN is not None -
    so `is not None` filters are not enough. Summing over NaN would silently
    make a rebuilt day's P&L blank, which is worse than a wrong number because
    it looks like an empty day.
    """
    out = []
    for row in rows or []:
        val = _num(row.get(key))
        if val is not None:
            out.append(val)
    return out


def _cell(value):
    """Summary values land in one mixed-type column, so a bool must not share it
    with counts: pandas (and Excel's auto-typing) coerce True/False + 0/1 into a
    boolean column, which would quietly render 'Lots 0' as 'False'. 'yes'/'no'
    reads correctly in the sheet and survives any re-read as a string."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return value


def _as_dicts(rows) -> list[dict]:
    """Accept Fill dataclasses, plain dicts, or a mix of both."""
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(dict(row))
        elif hasattr(row, "to_dict"):
            out.append(dict(row.to_dict()))
        else:  # pragma: no cover - defensive
            log.warning("journal: skipping unreadable row of type %s",
                        type(row).__name__)
    return out


def _iso_day(text: str) -> str:
    """Normalise a broker date to ISO 'YYYY-MM-DD'.

    Noren returns timestamps as 'dd-MM-yyyy HH:mm:ss' (and expiry dates as
    'dd-MMM-yyyy'), while everything this journal writes is ISO. Mixing the two
    in one column would sort and filter wrongly in Excel, so the conversion
    happens on the way in. An unrecognised shape is passed through untouched
    rather than guessed at.
    """
    text = text.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]                                  # already ISO
    parts = text[:10].split("-")
    if len(parts) == 3:
        day, month, year = parts
        if day.isdigit() and month.isdigit() and year.isdigit():
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        if day.isdigit() and year.isdigit():
            number = _MONTHS.get(month.upper())
            if number:
                return f"{year}-{number}-{day.zfill(2)}"
    return text[:10]


def _split_ts(raw) -> tuple[str, str]:
    """A broker/ISO timestamp -> (ISO date, 'HH:MM:SS').

    Noren is not consistent: GetOrderBook's `exch_tm` is 'dd-MM-yyyy HH:mm:ss'
    while `norentm` is 'HH:mm:ss dd-MM-yyyy'. Both must land in the same ISO
    column or the sheet sorts wrong, so a leading clock is detected and swapped
    rather than assumed.
    """
    if isinstance(raw, dt.datetime):
        return raw.date().isoformat(), raw.strftime("%H:%M:%S")
    text = _text(raw).strip().replace("T", " ")
    if not text:
        return "", ""
    parts = text.split(" ", 1)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    if ":" in head:                       # 'HH:mm:ss dd-MM-yyyy'
        head, tail = tail, head
    stamp = tail.split(".", 1)[0][:8]
    return _iso_day(head), stamp


def _pick(row: dict, *keys, default=None):
    """First present key from a Noren book row.

    The vendored SDK's field names differ by endpoint and version (e.g. `prc`
    vs `price`, `bs` vs `transactiontype`), so the ORDER of `keys` records what
    has actually been observed; the first non-empty hit wins and anything
    unknown stays blank instead of being guessed.
    """
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _rows(payload) -> list[dict]:
    """Normalise a broker book payload to a list of dicts.

    _unwrap() can return a dict (single row or error envelope), a list, or None
    for an empty/rejected book. None and error envelopes must not raise inside a
    live session - the journal is a record, never a dependency.
    """
    if not payload:
        return []
    if isinstance(payload, dict):
        stat = _text(payload.get("stat")).lower()
        if stat and stat != "ok":
            return []
        return [payload]
    if isinstance(payload, (list, tuple)):
        return [r for r in payload if isinstance(r, dict)]
    return []


# --- row builders ------------------------------------------------------------
def fill_rows(fills, *, day=None) -> list[dict]:
    """One journal row per fill, newest last.

    `day` only back-fills a missing timestamp; a fill that carries its own date
    always wins, so a session that runs across midnight stays on the right row.
    """
    fallback = _day_str(day or dt.date.today())
    rows = []
    for f in _as_dicts(fills):
        date, stamp = _split_ts(f.get("ts"))
        rows.append({
            "date": date or fallback,
            "time": stamp,
            "mode": _text(f.get("mode")),
            "action": _text(f.get("action")),
            "right": _text(f.get("right")),
            "strike": _num(f.get("strike")),
            "qty": _num(f.get("qty")),
            "price": _round(f.get("price"), 4),
            "reason": _text(f.get("reason")),
            "symbol": _text(f.get("symbol")),
            "exchange": _text(f.get("exchange")),
            "order_id": _text(f.get("order_id")),
            "tag": _text(f.get("tag")),
        })
    return rows


def position_rows(fills, *, day=None, lots=1, lot_size=None,
                  brokerage=0.0) -> list[dict]:
    """Pair each leg's opening SELL with its closing BUY and price the round trip.

    Pairing is FIFO *within each side* per (right, strike), by COUNT rather than by
    arrival order. Arrival order is not safe: fills reconstructed from the broker's
    book are stamped with the exchange's acknowledgement time, which can put a
    buy-back a second ahead of the sell it closes. Pairing strictly by arrival
    turned one such day into a phantom "STILL OPEN" short that did not exist -
    while the count is the position, and it says the book is flat.

    A leg opened and closed twice in a day (a re-entry) yields two rows, because
    merging them would hide the intermediate stop-out.
    """
    fallback = _day_str(day or dt.date.today())
    cost = _num(brokerage) or 0.0
    sells: dict[tuple, list[dict]] = {}
    buys: dict[tuple, list[dict]] = {}
    for f in _as_dicts(fills):
        date, stamp = _split_ts(f.get("ts"))
        side = _text(f.get("action")).upper()
        if side.startswith("S"):
            kind = sells
        elif side.startswith("B"):
            kind = buys
        else:
            continue
        key = (f.get("right"), f.get("strike"))
        kind.setdefault(key, []).append({
            "date": date or fallback, "time": stamp, "price": _num(f.get("price")),
            "qty": _num(f.get("qty")), "symbol": _text(f.get("symbol")),
            "order_id": _text(f.get("order_id")), "reason": _text(f.get("reason")),
        })

    rows: list[dict] = []
    for key in set(sells) | set(buys):
        open_stack = sells.get(key) or []
        close_stack = buys.get(key) or []
        pairs = min(len(open_stack), len(close_stack))
        for i in range(pairs):
            rows.append(_round_trip(key, open_stack[i], close_stack[i],
                                    lots=lots, lot_size=lot_size, cost=cost))
        # Leftover sells are a real short with no exit: genuinely open.
        for opener in open_stack[pairs:]:
            rows.append(_open_row(opener, key, lots=lots, lot_size=lot_size,
                                  cost=cost))
        # Leftover buys closed something this journal never saw open. It is not
        # an open position and it has no P&L: it is an unexplained exit, and the
        # row says so rather than being dropped or silently matched.
        for closer in close_stack[pairs:]:
            rows.append({
                "date": closer["date"], "right": _text(key[0]),
                "strike": _num(key[1]), "symbol": closer["symbol"],
                "qty": closer["qty"], "lots": _num(lots),
                "entry_time": "", "entry_price": None,
                "exit_time": closer["time"], "exit_price": _round(closer["price"], 4),
                "exit_reason": closer.get("reason", ""), "is_open": False,
                "mark_price": None, "gross_pts": None,
                "costs_inr": _round(cost), "pnl_pts": None, "pnl_inr": None,
                "order_in": "", "order_out": closer["order_id"],
                "note": "exit without matching entry",
            })
    return sorted(rows, key=lambda r: (r["date"], r["entry_time"], str(r["right"])))


def _round_trip(key, opener, closer, *, lots, lot_size, cost) -> dict:
    entry_px, exit_px = opener.get("price"), closer.get("price")
    qty = opener.get("qty") or closer.get("qty")
    gross_pts = None
    if entry_px is not None and exit_px is not None:
        gross_pts = round(entry_px - exit_px, 4)   # sold leg: falls = profit
    costs = 2 * cost if cost else 0.0
    pnl_inr = None
    if gross_pts is not None and qty:
        pnl_inr = round(gross_pts * qty - costs, 2)
    note = ""
    if closer["time"] and opener["time"] and closer["time"] < opener["time"]:
        # Flagged, not silently re-ordered: the reader should know the exit stamp
        # came in before the entry stamp rather than trusting a tidy-looking row.
        note = "exit stamp precedes entry (broker acknowledgement order)"
    return {
        "date": closer["date"] or opener["date"],
        "right": _text(key[0]), "strike": _num(key[1]),
        "symbol": opener.get("symbol") or closer.get("symbol"),
        "qty": qty, "lots": _num(lots),
        "entry_time": opener.get("time", ""), "entry_price": _round(entry_px, 4),
        "exit_time": closer.get("time", ""), "exit_price": _round(exit_px, 4),
        "exit_reason": closer.get("reason", ""), "is_open": False,
        "mark_price": None, "gross_pts": gross_pts, "costs_inr": _round(costs),
        "pnl_pts": None if pnl_inr is None or not lot_size
        else round(pnl_inr / float(lot_size), 2),
        "pnl_inr": pnl_inr, "order_in": opener.get("order_id", ""),
        "order_out": closer.get("order_id", ""), "note": note,
    }


def _open_row(opener, key, *, lots, lot_size, cost, note=None) -> dict:
    return {
        "date": opener["date"], "right": _text(key[0]), "strike": _num(key[1]),
        "symbol": opener["symbol"], "qty": opener["qty"], "lots": _num(lots),
        "entry_time": opener["time"], "entry_price": _round(opener["price"], 4),
        "exit_time": "", "exit_price": None, "exit_reason": "",
        "is_open": True, "mark_price": None, "gross_pts": None,
        "costs_inr": _round(cost), "pnl_pts": None, "pnl_inr": None,
        "order_in": opener["order_id"], "order_out": "",
        "note": note if note is not None else "STILL OPEN at journal time",
    }


# --- broker book -------------------------------------------------------------
def order_rows(book, *, day=None) -> list[dict]:
    """Normalise the broker's order book into journal rows.

    Read-only. This is where a rejection becomes visible in the report: the day
    the bot blamed "waiting for premiums" while the broker was answering
    'CASH shortfall', an Orders sheet holding `rejreason` would have shown it in
    one look.

    The key names below are the ones this account's live GetOrderBook actually
    returns (captured 2026-09-28: `trantype`, `exch_tm`, `remarks`, `fillshares`,
    `avgprc`), not the ones the older docs suggest - guessing here silently
    writes blanks, which is how a filled order can look like no order at all.
    `raw` keeps the untouched broker row so any field not mapped here is still
    recoverable.
    """
    fallback = _day_str(day or dt.date.today())
    rows = []
    for row in _rows(book):
        date, time_part = _split_ts(_pick(row, "exch_tm", "norentm",
                                          "ordenttm", "updatetime"))
        reject = _text(_pick(row, "rejreason", "message"))
        status = _text(_pick(row, "status", "st_intrn", "stat"))
        action = _text(_pick(row, "trantype", "bs", "transactiontype",
                             "buyorsell"))
        # Shoonya returns B/S; keep the journal readable without losing it.
        action = {"B": "BUY", "S": "SELL"}.get(action.upper(), action)
        rows.append({
            "date": date or fallback,
            "time": time_part,
            "order_id": _text(_pick(row, "norenordno", "orderno", "id")),
            "source": _text(_pick(row, "ordersource", "src", "ui_dev_code")),
            "action": action,
            "symbol": _text(_pick(row, "tsym", "tradingsymbol", "symb")),
            "exchange": _text(_pick(row, "exch", "exchange")),
            "qty": _num(_pick(row, "qty", "ordqty", "quantity")),
            "fill_qty": _num(_pick(row, "fillshares", "flqty", "fillqty")),
            "limit_price": _round(_pick(row, "prc", "ordprc", "price"), 4),
            "avg_price": _round(_pick(row, "avgprc", "fillprc"), 4),
            "trigger_price": _round(_pick(row, "trgprc"), 4),
            "price_type": _text(_pick(row, "prctyp", "pricetype",
                                      "price_type")),
            "product": _text(_pick(row, "prd", "producttype", "product_type")),
            "status": status,
            "reject_reason": reject.strip(),
            "tag": _text(_pick(row, "remarks", "rk", "remark")),
            "raw": _text(row),
        })
    return rows


def trade_book_rows(book, *, day=None) -> list[dict]:
    """Execution report rows from the broker's trade book - fills as the
    exchange saw them, kept separate from the bot's own fills for a same-day
    check. GetTradeBook returns `flqty`/`flprc` per fill against an order."""
    fallback = _day_str(day or dt.date.today())
    rows = []
    for row in _rows(book):
        date, time_part = _split_ts(_pick(row, "exch_tm", "fltm", "norentm",
                                          "tradetime"))
        action = _text(_pick(row, "trantype", "bs", "transactiontype"))
        rows.append({
            "date": date or fallback,
            "time": time_part,
            "order_id": _text(_pick(row, "norenordno", "orderno")),
            "fill_id": _text(_pick(row, "flid", "exchordid")),
            "action": {"B": "BUY", "S": "SELL"}.get(action.upper(), action),
            "symbol": _text(_pick(row, "tsym", "tradingsymbol")),
            "qty": _num(_pick(row, "flqty", "fillshares", "quantity")),
            "price": _round(_pick(row, "flprc", "avgprc", "prc"), 4),
            "tag": _text(_pick(row, "remarks", "rk")),
            "raw": _text(row),
        })
    return rows


# --- reconstruction from the broker's own record -----------------------------
# The engine stamps every order it sends with tag = f"{right}{strike}-{SE|BU}
# -{uuid8}", and place_order passes it through as `remarks`, so an order at the
# broker can always be traced back to this bot and to which leg it was.
BOT_TAG_RE = re.compile(r"^(?P<right>CE|PE)(?P<strike>\d+)-(?P<side>SE|BU)"
                        r"-[0-9a-f]{8}$")

# Only these count as executed. Anything pending/rejected/cancelled filled
# nothing, and journaled rows for them would invent trades.
EXECUTED_STATUSES = ("COMPLETE", "COMPLETED")


def reconstruct_fills(orders, *, day=None) -> list[dict]:
    """Rebuild a day's fills from the broker's order book.

    Needed when the session that traded is gone: fills live in the ExecutionEngine
    inside that process, so a restart before the journal existed lost them, and an
    operator who stops the server mid-day loses whatever was not yet written. The
    broker's own book still has every executed order, its tag, side, quantity and
    average price, which is enough to reconstruct the round trips exactly.

    Prices here are the exchange's average (`avgprc`), not the strategy's reference
    price, and `reason` says so - a reconstructed day is a broker-truth day, and
    the difference between the two is the real slippage.
    """
    rows = []
    for o in orders or []:
        tag = _text(o.get("tag"))
        match = BOT_TAG_RE.match(tag)
        if not match:
            continue                       # not this bot's order
        if "REJECT" in _text(o.get("status")).upper():
            continue                       # nothing was executed
        if not any(s in _text(o.get("status")).upper()
                   for s in EXECUTED_STATUSES):
            continue                       # pending/cancelled: no fill to book
        qty = _num(o.get("fill_qty")) or _num(o.get("qty"))
        price = _num(o.get("avg_price")) or _num(o.get("limit_price"))
        if not qty or not price:
            continue
        rows.append({
            "ts": " ".join(x for x in (_text(o.get("date")), _text(o.get("time")))
                            if x),
            "right": match.group("right"),
            "strike": int(match.group("strike")),
            "token": None,
            "action": "SELL" if match.group("side") == "SE" else "BUY",
            "qty": qty,
            "price": price,
            "reason": "BROKER",            # reconstructed, not journaled live
            "tag": tag,
            "mode": "live",
            "order_id": _text(o.get("order_id")),
            "symbol": _text(o.get("symbol")),
            "exchange": _text(o.get("exchange")),
        })
    # Sorted by order number, NOT by timestamp: norenordno is monotonic through
    # the day, while `exch_tm` reflects when the exchange acknowledged, so the two
    # can disagree. Pairing a short with its buy-back depends on submission order,
    # and sorting by the wrong one invents an open leg that does not exist.
    return sorted(rows, key=lambda f: (_text(f["order_id"]), _text(f["ts"])))


def merge_fills(*groups) -> list[dict]:
    """Union fills by tag, earlier groups winning.

    The session's own fills are the better record (they carry the strategy's
    reference price), so a reconstruction fills the gaps without overwriting them.
    """
    out: dict[str, dict] = {}
    extras: list[dict] = []
    for group in groups:
        for f in group or []:
            tag = _text(f.get("tag"))
            if not tag:
                extras.append(f)
                continue
            out.setdefault(tag, f)
    return sorted(list(out.values()) + extras,
                  key=lambda f: _text(f.get("ts")))


# --- reconciliation ----------------------------------------------------------
# A broker order is "accepted" if it is anything the exchange still holds or has
# executed: these statuses all mean the request left the process and something
# may exist at the exchange.
ACCEPTED_STATUSES = ("COMPLETE", "PENDING", "TRIGGER_PENDING", "COMPLETED")


def reconciliation(orders, fills) -> dict:
    """Do the broker's own API orders agree with the fills we journaled?

    This exists because of a real incident: an order's HTTP response raised
    while the exchange had already accepted it, so the bot believed it had sent
    nothing while it was in fact short two legs. Nothing else in the system would
    have noticed - the strategy, the risk manager and the dashboard all agreed
    with each other and disagreed with the broker.

    Orders carry the engine's `tag` in `remarks`, so a match is exact rather than
    a guess by symbol or time. Anything accepted-but-unmatched is reported as a
    possible unmanaged position.
    """
    tags = {_text(f.get("tag")) for f in fills or [] if f.get("tag")}
    api = [o for o in orders or [] if _text(o.get("source")).upper() == "API"]
    accepted = [o for o in api
                if any(s in _text(o.get("status")).upper()
                       for s in ACCEPTED_STATUSES)]
    matched = [o for o in accepted if _text(o.get("tag")) in tags]
    orphaned = [o for o in accepted if _text(o.get("tag")) not in tags]
    # A journaled fill with no accepted broker order behind it is the opposite
    # error: recorded locally, absent at the exchange (paper fills are excluded).
    order_tags = {_text(o.get("tag")) for o in accepted}
    unfilled = [f for f in fills or []
                if _text(f.get("mode")) == "live"
                and _text(f.get("tag")) not in order_tags]
    return {
        "api_orders": len(api),
        "api_accepted": len(accepted),
        "api_rejected": sum(1 for o in api
                            if "REJECT" in _text(o.get("status")).upper()),
        "matched": len(matched),
        "orphaned": orphaned,
        "unfilled": unfilled,
        # The headline: True only when both books say the same thing.
        "clean": not orphaned and not unfilled,
    }


# --- rewriting a day without losing it ---------------------------------------
# Which master-sheet columns an earlier record of the day is allowed to fill in
# for a later one. Deliberately NOT everything: P&L, fill counts and
# reconciliation are recomputed from the broker's own books on every write, so
# carrying them forward would let a stale number masquerade as today's result.
# What IS carried is the description of what the day was: the contracts it
# traded and the lot size needed to price them. How a session was *behaving*
# (phase, status, deferral) is deliberately excluded - a live run must report
# its own state, not inherit the state of the run before it.
CARRY_FIELDS = (
    "expiry", "atm", "spot_ref", "lot_size", "ce_symbol", "pe_symbol",
    "started_at",
)


def carry_forward(existing, day, *, fields=CARRY_FIELDS) -> dict:
    """Values from an earlier record of `day` worth keeping in a newer one.

    The day's contracts and lot size live in the RAM of the process that traded,
    so a writer that rebuilds the day from outside - a backfill against a server
    already sitting at `idle`, or a rescue copy written after the workbook was
    locked - knows nothing about them. Rewriting the day with such a writer
    would otherwise blank `Expiry`, `ATM pinned`, `Lot size` and `Net P&L (pts)`
    on a day that had them, silently turning a complete record into an
    incomplete one.

    Only fields that are absent in the new record are returned, so this is a
    fallback and never an override: whatever the writer genuinely observed wins.
    """
    if not existing:
        return {}
    out = {}
    for key in fields:
        value = existing.get(key)
        if value in (None, "") or value != value:            # None / NaN
            continue
        # Excel reads a date cell back as a midnight Timestamp; keep the day it
        # was written as so a carried value is not visibly worse than a live one.
        if isinstance(value, dt.datetime):
            value = (value.isoformat(sep=" ", timespec="seconds")
                     if (value.hour or value.minute or value.second)
                     else value.date().isoformat())
        elif isinstance(value, dt.date):
            value = value.isoformat()
        out[key] = value
    return out


# The Summary sheet labels the same facts by their reporting names, so a day
# whose master row was never written can still be carried from its workbook.
_SUMMARY_LABELS = {
    "expiry": "Expiry",
    "atm": "ATM pinned",
    "spot_ref": "Spot ref",
    "lot_size": "Lot size",
    "ce_symbol": "CE symbol",
    "pe_symbol": "PE symbol",
}


def day_record(day, base=None) -> dict:
    """Whatever an earlier write already recorded about `day`.

    Master row first (one small file, one read), falling back to the day's own
    Summary sheet. Returns master-shaped keys, empty when the day is new.
    """
    path = master_path(base)
    if path.is_file():
        try:
            frame = pd.read_excel(path, sheet_name=MASTER_SHEET)
            wanted = _day_str(day)
            for rec in frame.to_dict("records"):
                if _day_str(rec.get("date")) == wanted:
                    return dict(rec)
        except Exception as e:  # pragma: no cover - unreadable master is not fatal
            log.debug("journal: master unreadable for carry (%s)", e)
    book = _resolve_day_path(day, base=base)
    if book.is_file():
        try:
            frame = pd.read_excel(book, sheet_name=SUMMARY_SHEET)
            found = {str(r.get("field")).strip(): r.get("value")
                     for r in frame.to_dict("records")}
            out = {}
            for key, label in _SUMMARY_LABELS.items():
                if found.get(label) not in (None, ""):
                    out[key] = found[label]
            return out
        except Exception as e:  # pragma: no cover - a broken workbook is not fatal
            log.debug("journal: summary unreadable for carry (%s)", e)
    return {}


def summary_rows(*, day, status=None, positions=None, fills=None,
                 params=None, risk=None, orders=None, config=None,
                 recon=None, extra=None) -> list[dict]:
    """The one-glance sheet: how the day went, and whether it can be trusted."""
    status = status or {}
    params = params or {}
    risk = risk or {}
    config = config or {}
    positions = positions or []
    fills = fills or []
    resolved = status.get("resolved") or {}

    closed = [r for r in positions if not r.get("is_open")]
    still_open = [r for r in positions if r.get("is_open")]
    pnl = _vals(closed, "pnl_inr")
    gross = _vals(closed, "gross_pts")
    costs = _vals(closed, "costs_inr")
    modes = sorted({_text(f.get("mode")) for f in fills if f.get("mode")})
    reasons: dict[str, int] = {}
    for r in closed:
        reason = _text(r.get("exit_reason")) or "UNKNOWN"
        reasons[reason] = reasons.get(reason, 0) + 1
    orders = orders or []
    rejected = [o for o in orders if "reject" in _text(o.get("status")).lower()]
    lot = _num(resolved.get("lot_size")) or _num(params.get("lot_size"))

    rows = [
        ("Trading day", day),
        ("Journal written", dt.datetime.now().isoformat(sep=" ",
                                                        timespec="seconds")),
        ("Session status", status.get("status")),
        ("Strategy phase", status.get("phase")),
        ("Fill modes seen", "+".join(modes) or "no fills"),
        ("Paper mode", config.get("paper_mode")),
        ("Live armed", status.get("live_armed")),
        ("Kill switch", config.get("kill_switch")),
        ("Expiry", resolved.get("expiry")),
        ("ATM pinned", status.get("atm")),
        ("Spot ref", status.get("spot")),
        ("CE symbol", params.get("ce_symbol")),
        ("PE symbol", params.get("pe_symbol")),
        ("Lots", params.get("lots")),
        ("Lot size", lot),
        ("Fills recorded", len(fills)),
        ("Positions closed", len(closed)),
        ("Positions still open", len(still_open)),
        ("Gross pts (closed)", round(sum(gross), 2) if gross else None),
        ("Brokerage paid (Rs)", round(sum(costs), 2) if costs else None),
        ("Net P&L (Rs)", round(sum(pnl), 2) if pnl else None),
        ("Net P&L (pts)", round(sum(pnl) / float(lot), 2)
         if pnl and lot else None),
        ("Best leg (Rs)", max(pnl) if pnl else None),
        ("Worst leg (Rs)", min(pnl) if pnl else None),
        ("Exit reasons", ", ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
         or None),
        ("Broker orders", len(orders)),
        ("Broker rejections", len(rejected)),
        ("Rejection reasons", "; ".join(sorted(
            {_text(o.get("reject_reason")) for o in rejected
             if o.get("reject_reason")})[:3]) or None),
        ("Strategy entry rejections", status.get("broker_rejections")),
        ("Entry deferred", status.get("entry_deferred")),
        ("Entry deferred reason", status.get("entry_deferred_reason")),
        ("Risk halt reason", risk.get("halted_reason")),
        ("Entries today (risk)", risk.get("entries_today")),
        ("Max daily loss (pts)", risk.get("max_daily_loss_pts")
         or config.get("max_daily_loss_pts")),
        ("No entry after", config.get("no_new_entry_after")),
        ("Session error", status.get("error")),
        ("Recent strategy errors",
         " | ".join(_text(e) for e in (status.get("errors") or [])[-3:]) or None),
    ]
    for key, value in (extra or {}).items():
        rows.append((key, value))
    if recon:
        # The reconciliation block goes LAST so it sits at the bottom of the
        # sheet where a reader looks after the day's numbers - but it must be
        # impossible to miss, because an "unclean" result means the bot and the
        # broker disagree about what exists, which is a position question and not
        # a reporting one.
        rows.append(("--- reconciliation vs broker ---", None))
        rows.append(("Broker API orders", recon.get("api_orders")))
        rows.append(("Broker API accepted", recon.get("api_accepted")))
        rows.append(("Broker API rejected", recon.get("api_rejected")))
        rows.append(("Matched to journaled fills", recon.get("matched")))
        rows.append(("MATCHES", "yes" if recon.get("clean") else "NO - see note"))
        if recon.get("orphaned"):
            rows.append(("WARNING: accepted at broker, NOT journaled",
                         "; ".join(f"{o.get('order_id')} {o.get('action')} "
                                   f"{o.get('symbol')} x{o.get('qty')} "
                                   f"[{o.get('status')}]"
                                   for o in recon["orphaned"][:6])))
        if recon.get("unfilled"):
            rows.append(("WARNING: journaled live fill, no broker order",
                         "; ".join(f"{u.get('tag')} {u.get('action')} "
                                   f"{u.get('right')}{u.get('strike')}"
                                   for u in recon["unfilled"][:6])))
    return [{"field": k, "value": _cell(v)} for k, v in rows]


# --- master row --------------------------------------------------------------
def daily_row(*, day, status=None, positions=None, fills=None, params=None,
              risk=None, orders=None, recon=None, workbook=None,
              started_at=None, finished_at=None) -> dict:
    """One row for the running journal - the sheet reporting actually lives on."""
    status = status or {}
    params = params or {}
    risk = risk or {}
    resolved = status.get("resolved") or {}
    positions = positions or []
    closed = [r for r in positions if not r.get("is_open")]
    pnl = _vals(closed, "pnl_inr")
    gross = _vals(closed, "gross_pts")
    costs = _vals(closed, "costs_inr")
    modes = sorted({_text(f.get("mode")) for f in (fills or []) if f.get("mode")})
    symbols = {r.get("right"): r.get("symbol") for r in positions}
    reasons = sorted({_text(r.get("exit_reason")) for r in closed
                      if r.get("exit_reason")})
    rejected = [o for o in (orders or [])
                if "reject" in _text(o.get("status")).lower()]
    day_str = _day_str(day)
    try:
        weekday = dt.date.fromisoformat(day_str).strftime("%a")
    except ValueError:  # unparseable day keeps whatever it was
        weekday = ""
    net = round(sum(pnl), 2) if pnl else None
    lot = _num(resolved.get("lot_size")) or _num(params.get("lot_size"))
    return {
        "date": day_str,
        "weekday": weekday,
        "mode": "+".join(modes) or "no fills",
        "live_armed": bool(status.get("live_armed")),
        "expiry": resolved.get("expiry"),
        "atm": status.get("atm") or resolved.get("atm"),
        "spot_ref": status.get("spot") or resolved.get("spot"),
        "lots": params.get("lots"),
        "lot_size": lot,
        "ce_symbol": symbols.get("CE") or params.get("ce_symbol"),
        "pe_symbol": symbols.get("PE") or params.get("pe_symbol"),
        "phase": status.get("phase"),
        "entries": risk.get("entries_today"),
        "fills": len(fills or []),
        "positions": len(closed),
        "open_positions": sum(1 for r in positions if r.get("is_open")),
        "broker_rejections": status.get("broker_rejections")
        if status.get("broker_rejections") is not None else len(rejected),
        "entry_deferred": bool(status.get("entry_deferred")),
        "entry_deferred_reason": status.get("entry_deferred_reason"),
        "gross_pts": round(sum(gross), 2) if gross else None,
        "gross_inr": round(sum(gross) * float(lot), 2) if gross and lot else None,
        "costs_inr": round(sum(costs), 2) if costs else None,
        "pnl_pts": round(net / float(lot), 2) if net is not None and lot else None,
        "pnl_inr": net,
        "exit_reasons": "+".join(reasons) or None,
        "halted_reason": risk.get("halted_reason"),
        "errors": " | ".join(_text(e) for e in (status.get("errors") or [])[-3:])
        or None,
        "session_status": status.get("status"),
        "error": status.get("error"),
        "started_at": started_at,
        "finished_at": finished_at,
        "workbook": _text(workbook),
        # Reconciliation against the broker's own book. A day where these do not
        # agree is a day the bot and the exchange disagree about what exists.
        "broker_api_orders": recon.get("api_orders") if recon else None,
        "broker_api_accepted": recon.get("api_accepted") if recon else None,
        "broker_matched": recon.get("matched") if recon else None,
        "broker_orphans": len(recon.get("orphaned") or []) if recon else None,
        "recon_matches": ("" if not recon
                          else ("yes" if recon.get("clean") else "NO")),
    }


# --- writing -----------------------------------------------------------------
def _write_workbook(path: Path, sheets: dict[str, list[dict]],
                    columns: dict[str, list[str]]) -> Path | None:
    """Write sheets to `path` atomically; return the path actually written.

    Atomic because a workbook open in Excel holds an exclusive lock: writing
    straight to the target would fail mid-write and can leave a corrupt file.
    The bytes go to a temp file in the same folder first, then os.replace swaps
    them in - a single fast rename. If even the swap is refused (file still
    locked) the fresh copy lands beside it with a time suffix, so the day's
    record is never silently lost.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=".tmp_", suffix=".xlsx")
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
            for name, rows in sheets.items():
                cols = columns.get(name) or (list(rows[0]) if rows else [])
                frame = pd.DataFrame(rows, columns=cols or None)
                # Values stay native types (None renders as an empty cell, which
                # is the point: a blank means "not recorded", never "zero"), so
                # SUM/AVERAGE still work in the sheet. Display comes from
                # number_format below, not from stringifying.
                frame.to_excel(xw, sheet_name=name[:31], index=False)
                _beautify(xw.book, name, frame)
        try:
            os.replace(tmp, path)
            return path
        except OSError as e:
            rescue = path.with_name(
                f"{path.stem}-{dt.datetime.now():%H%M%S}.xlsx")
            try:
                os.replace(tmp, rescue)
                log.warning("journal: %s is locked (%s); wrote %s instead",
                            path.name, e, rescue.name)
                return rescue
            except OSError as e2:  # pragma: no cover - disk full, etc.
                log.error("journal: could not write %s: %s", path.name, e2)
                return None
    except Exception as e:  # pragma: no cover - reporting must not kill a day
        log.error("journal: write of %s failed: %s", path, e)
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _beautify(book, sheet_name: str, frame: pd.DataFrame) -> None:
    """Freeze the header, widen columns, number-format money, colour P&L.

    Cosmetic only: any failure is swallowed so a bad width never loses data.
    """
    try:
        from openpyxl.styles import Font
        ws = book[sheet_name[:31]]
    except Exception:  # pragma: no cover
        return
    try:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        red = Font(color="FF9999")
        green = Font(color="99FF99")
        wide = {"raw", "value", "errors", "note", "reject_reason", "symbol",
                "workbook", "exit_reasons", "error", "field"}
        for idx, col in enumerate(frame.columns, start=1):
            letter = ws.cell(row=1, column=idx).column_letter
            header = str(col)
            low = header.lower()
            if low in wide:
                width = 55
            elif len(header) > 14:
                width = len(header) + 2
            else:
                width = max(len(header) + 2, 10)
            ws.column_dimensions[letter].width = width
            if low.endswith(("_inr", "_pts")) or low == "price":
                for row in range(2, ws.max_row + 1):
                    cell = ws.cell(row=row, column=idx)
                    cell.number_format = "#,##0.00"
                    if low in ("pnl_inr", "pnl_pts"):
                        val = _num(cell.value)
                        if val:
                            cell.font = green if val > 0 else red
        if sheet_name == SUMMARY_SHEET:
            ws.column_dimensions["A"].width = 34
            ws.column_dimensions["B"].width = 60
            for row in range(2, ws.max_row + 1):
                ws.cell(row=row, column=1).font = Font(bold=True)
    except Exception as e:  # pragma: no cover - cosmetic
        log.debug("journal: formatting skipped for %s: %s", sheet_name, e)


def _fill_from_sheet(row: dict) -> dict:
    """Turn a Fills-sheet row back into an engine-shaped fill dict.

    Needed when a second session of the same day re-reads the morning's trades
    from the workbook: the sheet stores the stamp split across date/time, so it
    is stitched back here rather than duplicated at every call site.
    """
    out = {k: row.get(k) for k in ("mode", "action", "right", "strike", "qty",
                                  "price", "reason", "symbol", "exchange",
                                  "order_id", "tag")}
    stamp = " ".join(x for x in (_text(row.get("date")), _text(row.get("time")))
                     if x)
    out["ts"] = stamp
    return out


# --- the journal -------------------------------------------------------------
class Journal:
    """Accumulates a day's fills and writes them to Excel as they happen.

    One instance per session. `record()` is cheap to call on every poll - it
    rate-limits actual writes so a 5s loop does not thrash the disk, while a
    fill still reaches the file within one interval even if the process dies
    before the close. `finalize()` adds the broker's own order book and the
    daily master row.

    Nothing here can break a trading day: every write is wrapped and reported
    through `self.failures` instead of raising.
    """

    def __init__(self, day=None, *, client=None, params=None, lots=1,
                 lot_size=None, brokerage=None, base=None, enabled=None,
                 min_interval=30.0):
        self.day = _day_str(day or dt.date.today())
        self.client = client
        self.params = params or {}
        self.lots = _num(lots) or 1
        self.lot_size = _num(lot_size)
        if self.lot_size is None:
            self.lot_size = _num(self.params.get("lot_size"))
        if brokerage is None:
            brokerage = getattr(settings, "journal_brokerage_per_order", 20.0)
        self.brokerage = _num(brokerage) or 0.0
        self.base = Path(base) if base else None
        if enabled is None:
            enabled = bool(getattr(settings, "journal_enabled", True))
        self.enabled = bool(enabled)
        self.min_interval = float(min_interval)
        # Set via attach_day: the ExecutionEngine whose .fills we journal.
        self.engine = None
        self.fills: list[dict] = []
        self.status: dict = {}
        self.orders: list[dict] = []
        self.executions: list[dict] = []
        self.last_written: Path | None = None
        self.writes = 0
        self.failures = 0
        # What an earlier writer already recorded about this day. Read once, on
        # the first write: record() runs on every poll and must not re-open the
        # master workbook to discover nothing has changed.
        self._prior: dict | None = None
        self._prior_sheets: dict | None = None
        self._written_fills = -1
        self._last_write: dt.datetime | None = None

    # --- state ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return daily_path(self.day, base=self.base)

    def _prior_record(self) -> dict:
        """The day as an earlier write recorded it, read once and cached.

        `attach_day` is how a live session discovers lot size and contracts as
        the day unfolds; anything it never learned is taken from here rather
        than written out blank. See `carry_forward`.
        """
        if self._prior is None:
            try:
                self._prior = day_record(self.day, base=self.base)
            except Exception as e:  # pragma: no cover - never block a write
                log.warning("journal: could not read prior record: %s", e)
                self._prior = {}
        return self._prior

    def attach_day(self, *, client=None, lot_size=None, params=None,
                   lots=None, engine=None) -> None:
        """Fill in what the day turns out to be once the broker answers.

        The journal is created before connect/resolve so an aborted day
        (session expired at 09:10, resolution failed) still leaves a record
        saying why nothing traded. Anything discovered later - lot size, the
        day's contracts - is merged in here, and P&L in points only needs the
        lot size to be meaningful.

        `engine` becomes the source of fills for `record()` called without
        arguments: reading them live rather than from a snapshot the caller
        remembered to pass means a day that exits the poll loop early still
        journals every fill it made.
        """
        if client is not None:
            self.client = client
        if engine is not None:
            self.engine = engine
        size = _num(lot_size)
        if size:
            self.lot_size = size
        if params:
            incoming = params if isinstance(params, dict) else {}
            # Drop nulls from the incoming mapping: an unresolved CE symbol must
            # not overwrite (or shadow) a real one that _params_blob later takes
            # from the day's positions.
            clean = {k: v for k, v in incoming.items() if v is not None}
            self.params = {**self.params, **clean}
            if self.params.get("lot_size") and not self.lot_size:
                self.lot_size = _num(self.params["lot_size"])
        count = _num(lots)
        if count:
            self.lots = count

    def _current_fills(self) -> list[dict]:
        """The engine's fills right now, or whatever was last handed to us."""
        source = getattr(self, "engine", None)
        fills = getattr(source, "fills", None)
        if fills is not None:
            try:
                return _as_dicts(fills)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("journal: fill read failed (%s); using last copy",
                            e)
        return self.fills

    def positions(self) -> list[dict]:
        return position_rows(self.fills, day=self.day, lots=self.lots,
                             lot_size=self.lot_size,
                             brokerage=self.brokerage)

    def _status_update(self, status) -> dict:
        """Merge the latest status so an older call never blanks a newer field."""
        if status:
            self.status = {**self.status, **dict(status)}
        return self.status

    def _needs_write(self, count: int) -> bool:
        """Always write when there is new data; otherwise throttle by interval.

        The throttle exists because the session loop calls this every poll: a
        fill-count change is the signal that matters, and the interval only
        forces a periodic refresh of the live fields (phase, rejections,
        deferral reason) so a crash mid-day still leaves a current file.
        """
        if count != self._written_fills or self.last_written is None:
            return True
        if self._last_write is None:
            return True
        age = (dt.datetime.now() - self._last_write).total_seconds()
        return age >= self.min_interval

    # --- writing -------------------------------------------------------------
    def record(self, fills=None, *, status=None, force=False) -> Path | None:
        """Journal the day so far. Safe to call every poll.

        Called with no `fills`, it reads them from the attached engine, so the
        session loop only has to say "journal now" and cannot forget a fill.
        """
        self.fills = self._current_fills() if fills is None else _as_dicts(fills)
        self._status_update(status)
        if not self.enabled:
            return None
        if not force and not self._needs_write(len(self.fills)):
            return self.last_written
        path = self._write_day()
        if path:
            self._written_fills = len(self.fills)
            self._last_write = dt.datetime.now()
        return path

    def finalize(self, status=None, *, started_at=None, finished_at=None,
                 fetch_orders=True) -> Path | None:
        """Close the day: refresh the broker book, write workbook + master row.

        Always force-writes: this is the record reporting keeps, so it must not
        be skipped by the poll loop's throttle. `journal_enabled=False` is
        honoured here too - the flag has to switch off ALL writing, otherwise
        turning the journal off would still create files at the close of each
        day and the setting would be a lie.
        """
        self.fills = self._current_fills()
        self._status_update(status)
        if not self.enabled:
            return None
        if fetch_orders and self.client is not None:
            self.orders = self._fetch_orders()
            self.executions = self._fetch_executions()
        path = self._write_day(with_master=True, started_at=started_at,
                               finished_at=finished_at)
        if path:
            self._written_fills = len(self.fills)
            self._last_write = dt.datetime.now()
        return path

    def _fetch_orders(self) -> list[dict]:
        """Read-only broker books. A dead session must not hide the trade rows."""
        try:
            book = self.client.get_order_book()
        except Exception as e:
            log.warning("journal: order book unavailable: %s", e)
            return []
        try:
            return order_rows(book, day=self.day)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("journal: order book unparsable: %s", e)
            return []

    def _fetch_executions(self) -> list[dict]:
        """The broker's execution report - fills as the exchange actually gave them.

        Worth its own sheet because the journal's own P&L is built from the
        engine's fills, and a live fill is recorded at the REFERENCE price the
        order was priced off, not the exchange's average. Those two numbers
        differ by real money; the Tradebook sheet is where the difference is
        visible instead of being silently smoothed over.
        """
        try:
            book = self.client.get_trade_book()
        except Exception as e:
            log.info("journal: trade book unavailable: %s", e)
            return []
        try:
            return trade_book_rows(book, day=self.day)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("journal: trade book unparsable: %s", e)
            return []

    # --- sheets --------------------------------------------------------------
    def _params_blob(self, positions) -> dict:
        """Params + the day's resolved contracts, for the Summary sheet.

        Anything the previous write of this day recorded is used as the floor:
        `setdefault` means the live session's own values always win, so a day
        re-journaled by a writer that never resolved anything (a backfill after
        a restart) keeps its contracts and lot size instead of blanking them.
        """
        resolved = self.status.get("resolved") or {}
        blob = dict(self.params) if isinstance(self.params, dict) else {}
        for key in ("expiry", "atm", "spot", "lot_size"):
            if key in resolved:
                blob.setdefault(key, resolved[key])
        prior = carry_forward(self._prior_record(), self.day)
        blob.setdefault("expiry", prior.get("expiry"))
        blob.setdefault("atm", prior.get("atm"))
        blob.setdefault("spot", prior.get("spot_ref"))
        blob.setdefault("ce_symbol", prior.get("ce_symbol"))
        blob.setdefault("pe_symbol", prior.get("pe_symbol"))
        blob.setdefault("lots", self.lots)
        if blob.get("lot_size") is None:
            blob["lot_size"] = self.lot_size
        if blob.get("lot_size") is None:
            blob["lot_size"] = prior.get("lot_size")
        for row in positions:
            key = {"CE": "ce_symbol", "PE": "pe_symbol"}.get(_text(row.get("right")))
            if key and row.get("symbol"):
                blob.setdefault(key, row["symbol"])
        return blob

    def _merged_fills(self) -> list[dict]:
        """This session's fills plus anything already journaled for the day.

        One day can hold several sessions - the operator stops, edits .env, and
        starts again - and each run_day builds its OWN ExecutionEngine. Without
        this merge, the second session's first write would rewrite the workbook
        from an empty fill list and silently erase the morning's trades, which
        is exactly the kind of reporting hole this file exists to close.
        """
        merged = {_text(f.get("tag")): f for f in self.fills if f.get("tag")}
        untagged = [f for f in self.fills if not f.get("tag")]
        path = self.path
        if path.is_file():
            try:
                previous = load_day(path).get(FILLS_SHEET) or []
            except Exception as e:  # pragma: no cover - unreadable file
                log.warning("journal: could not re-read %s (%s)", path.name, e)
                previous = []
            for row in previous:
                tag = _text(row.get("tag"))
                if tag and tag not in merged:
                    merged[tag] = _fill_from_sheet(row)
                elif not tag:
                    untagged.append(_fill_from_sheet(row))
        out = list(merged.values()) + untagged
        return sorted(out, key=lambda f: (_text(f.get("ts")),
                                          _text(f.get("action"))))

    def _recorded_sheets(self) -> dict:
        """The day's own sheets as an earlier write left them, read once."""
        if self._prior_sheets is None:
            try:
                book = load_day(self.path, base=self.base)
                self._prior_sheets = {
                    ORDERS_SHEET: book.get(ORDERS_SHEET) or [],
                    TRADEBOOK_SHEET: book.get(TRADEBOOK_SHEET) or [],
                } if book.get("exists") else {}
            except Exception as e:  # pragma: no cover - never block a write
                log.warning("journal: could not read prior sheets: %s", e)
                self._prior_sheets = {}
        return self._prior_sheets

    @staticmethod
    def _keep_if_empty(fetched, recorded) -> list:
        """A failed or skipped fetch must not delete rows already on file.

        `fetched` is empty both when the broker has nothing to say and when the
        caller never asked (no client, --no-orders, dead session). Those are
        indistinguishable here, and the safe reading is the same: keep what the
        last successful write recorded rather than publish a thinner day.
        """
        return list(fetched) if fetched else list(recorded or [])

    def _write_day(self, *, with_master=False, started_at=None,
                   finished_at=None) -> Path | None:
        self.fills = self._merged_fills()
        positions = self.positions()
        fills = fill_rows(self.fills, day=self.day)
        params = self._params_blob(positions)
        status = self._status_with_prior()
        risk = status.get("risk") or {}
        prior = self._recorded_sheets()
        orders = self._keep_if_empty(self.orders, prior.get(ORDERS_SHEET))
        executions = self._keep_if_empty(self.executions,
                                         prior.get(TRADEBOOK_SHEET))
        recon = reconciliation(orders, self.fills) if orders else None
        sheets = {
            SUMMARY_SHEET: summary_rows(day=self.day, status=status,
                                        positions=positions, fills=fills,
                                        params=params, orders=orders,
                                        risk=risk, recon=recon,
                                        config=self._config_blob()),
            POSITIONS_SHEET: positions,
            FILLS_SHEET: fills,
            ORDERS_SHEET: orders,
            TRADEBOOK_SHEET: executions,
        }
        columns = {
            SUMMARY_SHEET: SUMMARY_COLUMNS,
            POSITIONS_SHEET: POSITION_COLUMNS,
            FILLS_SHEET: FILL_COLUMNS,
            ORDERS_SHEET: ORDER_COLUMNS,
            TRADEBOOK_SHEET: TRADEBOOK_COLUMNS,
        }
        path = _write_workbook(self.path, sheets, columns)
        if path:
            self.last_written = path
            self.writes += 1
        else:
            self.failures += 1
        if with_master:
            self._write_master(daily_row(day=self.day, status=status,
                                         positions=positions, fills=fills,
                                         params=params, risk=risk,
                                         orders=orders, recon=recon,
                                         workbook=path or self.path,
                                         started_at=started_at
                                         or self._prior_record().get("started_at"),
                                         finished_at=finished_at))
        return path

    def _status_with_prior(self) -> dict:
        """This session's status with the day context an earlier write recorded.

        The Summary sheet reads `Expiry`, `ATM pinned` and `Spot ref` out of the
        status's `resolved` block, so carrying them only into the params blob
        would leave those rows blank. Only the day-defining fields are carried:
        how a session was *behaving* (status, phase, deferral) is always its own,
        never inherited from the run before it.
        """
        status = dict(self.status)
        prior = carry_forward(self._prior_record(), self.day)
        if not prior:
            return status
        # master column -> resolved key
        targets = {"expiry": "expiry", "atm": "atm",
                   "spot_ref": "spot", "lot_size": "lot_size"}
        resolved = dict(status.get("resolved") or {})
        touched = False
        for column, key in targets.items():
            value = prior.get(column)
            if value in (None, "") or resolved.get(key) not in (None, ""):
                continue
            resolved[key] = value
            touched = True
        if touched:
            status["resolved"] = resolved
        for column, key in (("atm", "atm"), ("spot_ref", "spot")):
            if status.get(key) in (None, "") and prior.get(column) not in (None, ""):
                status[key] = prior[column]
        return status

    @staticmethod
    def _config_blob() -> dict:
        """Config values that explain a day's outcome (paper vs live, halts)."""
        return {
            "paper_mode": settings.paper_mode,
            "kill_switch": settings.kill_switch,
            "max_daily_loss_pts": settings.max_daily_loss_pts,
            "no_new_entry_after": f"{settings.no_new_entry_after:%H:%M}",
        }

    def _write_master(self, row: dict) -> Path | None:
        """Add this day's row to the master, keeping every other day intact."""
        out = write_master([row], base=self.base, prefer_new=True)
        if not out:
            self.failures += 1
        return out


def write_master(rows, *, base=None, prefer_new=True) -> Path | None:
    """Rewrite the master sheet from `rows`, merged with what is already there.

    Rewriting instead of appending keeps the file self-healing: a corrupt or
    manually mangled master is rebuilt from whatever rows it can still read, and
    re-running a day replaces that day's row instead of duplicating it - which is
    what makes the file safe to regenerate from the per-day workbooks.

    `prefer_new` decides which side wins when a date appears twice: normally the
    freshly computed row, but a rebuild passes False so the stored per-day
    numbers are not overwritten by a summary re-derived from those same numbers.
    """
    incoming = {}
    for row in rows or []:
        key = _day_str(row.get("date"))
        if key:
            row = dict(row)
            row["date"] = key
            incoming[key] = row
    if not incoming:
        return None
    path = master_path(base)
    merged = {}
    if path.is_file():
        try:
            old = pd.read_excel(path, sheet_name=MASTER_SHEET)
            for rec in old.to_dict("records"):
                key = _day_str(rec.get("date"))
                if key:
                    rec = dict(rec)
                    rec["date"] = key
                    merged[key] = rec
        except Exception as e:
            log.warning("journal: master unreadable (%s); rebuilding", e)
    for key, row in incoming.items():
        if prefer_new or key not in merged:
            merged[key] = row
    out = _write_workbook(path, {MASTER_SHEET: [merged[k] for k in sorted(merged)]},
                          {MASTER_SHEET: DAILY_COLUMNS})
    return out


# --- one-shot helpers --------------------------------------------------------
def record_fills(fills, *, day=None, status=None, client=None, params=None,
                 lots=1, lot_size=None, base=None) -> Path | None:
    """Write (or rewrite) a whole day in one call - scripts and backfills."""
    jr = Journal(day, client=client, params=params, lots=lots,
                 lot_size=lot_size, base=base)
    return jr.record(fills, status=status, force=True)


def available_days(base=None) -> list[str]:
    """Days that already have a workbook, oldest first.

    Only exact `trades_YYYY-MM-DD.xlsx` names count: the time-suffixed rescue
    copies written when a file was locked are duplicates, not extra days.
    """
    folder = journal_dir(base) / "daily"
    if not folder.is_dir():
        return []
    days = []
    for path in folder.glob("trades_*.xlsx"):
        stem = path.stem[len("trades_"):]
        if _DAY_RE.match(stem):
            days.append(stem)
    return sorted(days)


def _resolve_day_path(day, base=None) -> Path:
    """A day (date/'YYYY-MM-DD') OR an explicit workbook path.

    The explicit path matters for the time-suffixed rescue copies written when a
    workbook is locked: those are not named after their day, so the only way to
    read one back is to hand back the path the write returned.
    """
    text = str(day)
    if text.lower().endswith(".xlsx"):
        return Path(text if isinstance(day, Path) else text)
    return daily_path(day, base=base)


def load_day(day, base=None) -> dict:
    """Read one day's workbook back as {sheet: [row, ...]} for reporting."""
    path = _resolve_day_path(day, base=base)
    out = {"workbook": str(path), "exists": path.is_file()}
    if not out["exists"]:
        return out
    try:
        with pd.ExcelFile(path) as xl:
            for name in xl.sheet_names:
                frame = xl.parse(name)
                out[name] = frame.where(pd.notnull(frame), None).to_dict(
                    "records")
    except Exception as e:
        out["error"] = str(e)
    return out


def load_master(base=None) -> list[dict]:
    """Every recorded day, for the reporting summary."""
    path = master_path(base)
    if not path.is_file():
        return []
    try:
        frame = pd.read_excel(path, sheet_name=MASTER_SHEET)
        return frame.where(pd.notnull(frame), None).to_dict("records")
    except Exception as e:
        log.warning("journal: master unreadable: %s", e)
        return []
