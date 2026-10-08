"""Unified P&L and Position formatting helper for Strategy Bot Telegram notifications & supervisor.

Ensures Telegram bot outputs match the web dashboard logic 100%.
Handles both live engine snapshots and test mock structures seamlessly.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from typing import Any, Dict, Optional

import httpx

import glob
import os
from pathlib import Path

from app import journal as journal_mod

_spot_cache = {"price": None, "ts": 0.0}


def get_previous_journal_day(ref_date: Optional[str] = None) -> Optional[str]:
    """Find the most recent trading journal date strictly prior to ref_date."""
    if not ref_date:
        ref_date = dt.date.today().isoformat()
    daily_dir = Path(journal_mod.daily_path(ref_date)).parent
    if not daily_dir.is_dir():
        return None
    matches = []
    for p in daily_dir.glob("trades_*.xlsx"):
        name = p.stem.replace("trades_", "")
        if name < ref_date:
            matches.append(name)
    matches.sort()
    return matches[-1] if matches else None


def load_journal_day_stats(day_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load official P&L and trade metrics for a day directly from the Excel journal.
    
    Checks the daily workbook (trades_YYYY-MM-DD.xlsx) first, then falls back to
    the master trading_journal.xlsx sheet.
    """
    if not day_str:
        day_str = dt.date.today().isoformat()

    def _to_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) else f
        except Exception:
            return None

    # 1. Attempt daily workbook
    try:
        day_data = journal_mod.load_day(day_str)
    except Exception:
        day_data = None

    if day_data and day_data.get("exists"):
        summary = {
            r.get("field"): r.get("value")
            for r in (day_data.get("Summary") or [])
            if isinstance(r, dict)
        }
        positions = day_data.get("Positions") or []
        fills = day_data.get("Fills") or []

        net_rs = _to_float(summary.get("Net P&L (Rs)"))
        net_pts = _to_float(summary.get("Net P&L (pts)"))
        gross_pts = _to_float(summary.get("Gross pts (closed)"))
        costs_rs = _to_float(summary.get("Brokerage paid (Rs)"))

        if net_rs is None and positions:
            net_rs = sum((_to_float(p.get("pnl_inr")) or 0.0) for p in positions)
        if net_pts is None and positions:
            net_pts = sum((_to_float(p.get("pnl_pts")) or 0.0) for p in positions)
        if gross_pts is None and positions:
            gross_pts = sum((_to_float(p.get("gross_pts")) or 0.0) for p in positions)
        if costs_rs is None and positions:
            costs_rs = sum((_to_float(p.get("costs_inr")) or 0.0) for p in positions)

        entries_today = summary.get("Entries today (risk)")
        if entries_today is None or entries_today == 0:
            sell_times = set(
                f.get("time") for f in fills if f.get("action") == "SELL" and f.get("time")
            )
            if sell_times:
                entries_today = len(sell_times)
            elif positions:
                entries_today = (len(positions) + 1) // 2

        legs: Dict[str, Any] = {}
        for p in reversed(positions):
            r = p.get("right")
            if r and r not in legs:
                is_open = bool(p.get("is_open"))
                entry_px = _to_float(p.get("entry_price")) or 0.0
                exit_px = _to_float(p.get("exit_price"))
                ltp = _to_float(p.get("mark_price")) or exit_px or entry_px
                p_pts = _to_float(p.get("pnl_pts")) or 0.0
                p_rs = _to_float(p.get("pnl_inr")) or 0.0
                legs[r] = {
                    "right": r,
                    "symbol": p.get("symbol") or r,
                    "strike": p.get("strike"),
                    "is_open": is_open,
                    "status": "OPEN" if is_open else "CLOSED",
                    "entry": entry_px,
                    "ltp": ltp,
                    "sl": None,
                    "pnl_pts": round(p_pts, 2),
                    "pnl_rupees": round(p_rs, 2),
                }

        spot_ref = _to_float(summary.get("Spot ref"))
        lot_size = summary.get("Lot size") or 65
        lots = summary.get("Lots") or 1

        # Calculate P&L strictly for positions/orders placed by our strategy (from Orders tab)
        # Identifies all symbols touched by the strategy engine (API/tagged), and sums the
        # completed trades for those symbols (including manual intervention exits for those legs).
        orders_data = day_data.get("Orders") or []
        orders_strat_pnl = None
        orders_strat_count = 0
        if orders_data:
            strat_symbols = set()
            for o in orders_data:
                if not isinstance(o, dict):
                    continue
                if str(o.get("status") or "").upper() != "COMPLETE":
                    continue
                if bool(o.get("tag")) or (str(o.get("source") or "").upper() == "API"):
                    sym = o.get("symbol")
                    if sym:
                        strat_symbols.add(sym)

            if strat_symbols:
                s_buy, s_sell = 0.0, 0.0
                for o in orders_data:
                    if not isinstance(o, dict):
                        continue
                    if str(o.get("status") or "").upper() != "COMPLETE":
                        continue
                    sym = o.get("symbol")
                    if sym not in strat_symbols:
                        continue
                    act = str(o.get("action") or "").upper()
                    q = _to_float(o.get("fill_qty")) or _to_float(o.get("qty")) or 0.0
                    px = _to_float(o.get("avg_price")) or _to_float(o.get("price")) or 0.0
                    val = q * px
                    orders_strat_count += 1
                    if act == "BUY":
                        s_buy += val
                    elif act == "SELL":
                        s_sell += val
                if orders_strat_count > 0:
                    orders_strat_pnl = round(s_sell - s_buy, 2)

        return {
            "day": str(day_str),
            "realized_pts": round(net_pts or 0.0, 2),
            "realized_rs": round(net_rs or 0.0, 2),
            "open_pts": 0.0,
            "open_rs": 0.0,
            "total_pts": round(net_pts or 0.0, 2),
            "total_rs": round(net_rs or 0.0, 2),
            "gross_pts": round(gross_pts or 0.0, 2),
            "costs_inr": round(costs_rs or 0.0, 2),
            "lot_size": int(lot_size),
            "lots": int(lots),
            "qty": int(lot_size) * int(lots),
            "entries_today": int(entries_today or 0),
            "entry_spot": spot_ref,
            "live_spot": spot_ref,
            "legs": legs,
            "comb_entry": None,
            "comb_current": None,
            "comb_sl_level": None,
            "source": "journal_daily",
            "session_status": summary.get("Session status") or "done",
            "orders_strat_pnl": orders_strat_pnl,
            "orders_strat_count": orders_strat_count,
        }

    # 2. Fallback to master sheet
    try:
        rows = journal_mod.load_master()
    except Exception:
        rows = []
    target = [r for r in rows if str(r.get("date")) == str(day_str)]
    if target:
        row = target[-1]
        pnl_pts = _to_float(row.get("pnl_pts")) or 0.0
        pnl_rs = _to_float(row.get("pnl_inr")) or 0.0
        lot_size = row.get("lot_size") or 65
        lots = row.get("lots") or 1
        spot_ref = _to_float(row.get("spot_ref"))
        return {
            "day": str(day_str),
            "realized_pts": round(pnl_pts, 2),
            "realized_rs": round(pnl_rs, 2),
            "open_pts": 0.0,
            "open_rs": 0.0,
            "total_pts": round(pnl_pts, 2),
            "total_rs": round(pnl_rs, 2),
            "gross_pts": round(_to_float(row.get("gross_pts")) or 0.0, 2),
            "costs_inr": round(_to_float(row.get("costs_inr")) or 0.0, 2),
            "lot_size": int(lot_size),
            "lots": int(lots),
            "qty": int(lot_size) * int(lots),
            "entries_today": int(_to_float(row.get("entries")) or 0),
            "entry_spot": spot_ref,
            "live_spot": spot_ref,
            "legs": {},
            "comb_entry": None,
            "comb_current": None,
            "comb_sl_level": None,
            "source": "journal_master",
            "session_status": row.get("session_status") or "done",
        }

    return None


def get_live_nifty_spot() -> Optional[float]:
    """Fetch live real-time LTP of NIFTY 50 spot with a 3-second cache."""
    now = time.time()
    if now - _spot_cache["ts"] < 3.0 and _spot_cache["price"] is not None:
        return _spot_cache["price"]
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1m"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = httpx.get(url, headers=headers, timeout=2.5)
        if resp.status_code == 200:
            meta = resp.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            px = meta.get("regularMarketPrice")
            if px and float(px) > 0:
                _spot_cache["price"] = round(float(px), 2)
                _spot_cache["ts"] = now
                return _spot_cache["price"]
    except Exception:
        pass
    return _spot_cache.get("price")


def format_spot_line(stats: Dict[str, Any], atm: Any = None) -> str:
    """Format dual NIFTY Spot display: Live LTP + Entry Spot."""
    live_px = stats.get("live_spot")
    entry_px = stats.get("entry_spot")
    atm_str = str(atm) if atm else "—"

    live_str = f"{live_px:,.2f}" if live_px else "—"
    entry_str = f"{entry_px:,.2f}" if entry_px else "—"

    if entry_px and live_px and abs(live_px - entry_px) > 0.05:
        chg = live_px - entry_px
        chg_sign = "+" if chg >= 0 else ""
        return (
            f"📈 <b>NIFTY Spot LTP:</b> <code>{live_str}</code> "
            f"({chg_sign}{chg:.2f} vs entry <code>{entry_str}</code> | ATM <b>{atm_str}</b>)"
        )
    return f"📈 <b>NIFTY Spot LTP:</b> <code>{live_str}</code> | ATM <b>{atm_str}</b>"


def compute_session_stats(st: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not st or not isinstance(st, dict):
        j_stats = load_journal_day_stats()
        if j_stats:
            live_spot = get_live_nifty_spot() or j_stats.get("live_spot")
            j_stats["live_spot"] = live_spot
            return j_stats
        live_spot = get_live_nifty_spot()
        return {
            "lot_size": 65,
            "lots": 1,
            "qty": 65,
            "realized_pts": 0.0,
            "realized_rs": 0.0,
            "open_pts": 0.0,
            "open_rs": 0.0,
            "total_pts": 0.0,
            "total_rs": 0.0,
            "legs": {},
            "comb_entry": None,
            "comb_current": None,
            "comb_sl_level": None,
            "entries_today": 0,
            "entry_spot": None,
            "live_spot": live_spot,
        }

    resolved = st.get("resolved") or {}
    lot_size = (
        resolved.get("lot_size")
        or st.get("lot_size")
        or st.get("strategy_params", {}).get("default_lot")
        or 65
    )
    lots = st.get("lots") or st.get("strategy_params", {}).get("lots") or 1
    qty = int(lot_size) * int(lots)

    fills = st.get("fills") or []
    legs = st.get("legs") or {}
    comb = st.get("combined") or {}
    risk = st.get("risk") or {}

    # Map resolved symbols by right
    res_symbols = {}
    for rlg in resolved.get("legs", []):
        if isinstance(rlg, dict) and rlg.get("right") and rlg.get("symbol"):
            res_symbols[rlg["right"]] = rlg["symbol"]

    # Fills by right
    sells_by_right = {}
    buys_by_right = {}
    for f in fills:
        if not isinstance(f, dict):
            continue
        r = f.get("right")
        if not r:
            continue
        if f.get("action") == "SELL":
            sells_by_right.setdefault(r, []).append(f)
        elif f.get("action") == "BUY":
            buys_by_right.setdefault(r, []).append(f)

    comb_entry = comb.get("entry")
    comb_curr = comb.get("current")
    comb_sl_level = comb.get("sl_level")
    if comb_sl_level is None and comb_entry is not None:
        sl_pct = comb.get("sl_pct")
        if sl_pct is not None:
            comb_sl_level = round(comb_entry * (1.0 + float(sl_pct)), 2)

    legs_summary = {}
    realized_from_fills = 0.0
    unrealized_mtm = 0.0

    for right, leg in legs.items():
        if not isinstance(leg, dict):
            continue

        # Status: handle both boolean 'is_open' and string 'status'
        if "is_open" in leg:
            is_open = bool(leg["is_open"])
        elif "status" in leg:
            is_open = (str(leg["status"]).upper() == "OPEN")
        else:
            is_open = True

        entry = leg.get("entry") if leg.get("entry") is not None else leg.get("entry_price")
        if entry is not None:
            entry = float(entry)

        last_close = leg.get("last_close") if leg.get("last_close") is not None else leg.get("last_price")
        if last_close is not None:
            last_close = float(last_close)

        sells = sells_by_right.get(right, [])
        buys = buys_by_right.get(right, [])

        sym = (
            leg.get("tradingsymbol")
            or leg.get("symbol")
            or res_symbols.get(right)
            or (sells[0].get("symbol") if sells else None)
            or (buys[0].get("symbol") if buys else None)
            or right
        )

        # SL priority: leg trailing stop -> leg stop loss -> combined stop loss
        sl = leg.get("trail_sl") or leg.get("sl_level") or leg.get("sl") or comb_sl_level

        # If pnl_pts was explicitly precomputed in leg (e.g. in legacy tests), respect it
        if "pnl_pts" in leg and leg["pnl_pts"] is not None and not fills:
            leg_pnl = float(leg["pnl_pts"])
            exit_px = last_close if last_close is not None else (entry or 0.0)
            if is_open:
                unrealized_mtm += leg_pnl
            else:
                realized_from_fills += leg_pnl
        else:
            if not is_open and sells and buys:
                avg_sell = sum(float(f["price"]) for f in sells) / len(sells)
                avg_buy = sum(float(f["price"]) for f in buys) / len(buys)
                leg_pnl = avg_sell - avg_buy
                realized_from_fills += leg_pnl
                exit_px = avg_buy
            elif is_open and entry is not None:
                curr_px = last_close if last_close is not None else entry
                leg_pnl = entry - curr_px
                unrealized_mtm += leg_pnl
                exit_px = curr_px
            else:
                leg_pnl = 0.0
                exit_px = last_close or entry or 0.0

        legs_summary[right] = {
            "right": right,
            "symbol": sym,
            "strike": leg.get("strike"),
            "is_open": is_open,
            "status": "OPEN" if is_open else "CLOSED",
            "entry": entry if entry is not None else 0.0,
            "ltp": exit_px if exit_px is not None else 0.0,
            "sl": float(sl) if sl is not None else None,
            "pnl_pts": round(leg_pnl, 2),
            "pnl_rupees": round(leg_pnl * qty, 2),
        }

    # Combined open position PnL fallback/cross-check:
    # If both legs are open and comb has entry & current, comb_entry - comb_curr matches unrealized_mtm
    if comb_entry is not None and comb_curr is not None and len(legs_summary) >= 2 and all(l["is_open"] for l in legs_summary.values()):
        open_pts = round(comb_entry - comb_curr, 2)
    elif "pnl_pts" in comb and comb["pnl_pts"] is not None and not fills and not legs:
        open_pts = float(comb["pnl_pts"])
    else:
        open_pts = round(unrealized_mtm, 2)

    open_rs = round(open_pts * qty, 2)

    # Realized risk points
    realized_pts = round((risk.get("daily_pnl_pts") or 0.0) + realized_from_fills, 2)
    realized_rs = round(
        risk.get("daily_pnl_rupees") if risk.get("daily_pnl_rupees") is not None else (realized_pts * qty),
        2
    )

    entries_today = risk.get("entries_today", 0)

    orders_strat_pnl = None
    orders_strat_count = 0

    # Check journal day stats
    day_str = st.get("day") or dt.date.today().isoformat()
    j_stats = load_journal_day_stats(day_str)
    if j_stats:
        orders_strat_pnl = j_stats.get("orders_strat_pnl")
        orders_strat_count = j_stats.get("orders_strat_count", 0)

        # If in-memory state has no fills or no realized P&L recorded, or session is complete
        if (not fills and realized_pts == 0.0 and realized_rs == 0.0) or st.get("status") in ("done", "stopped", "halted", "idle", "session_expired"):
            if j_stats.get("realized_pts") or j_stats.get("realized_rs") or j_stats.get("entries_today"):
                if realized_pts == 0.0 and realized_rs == 0.0:
                    realized_pts = j_stats["realized_pts"]
                    realized_rs = j_stats["realized_rs"]
                if entries_today == 0 and j_stats.get("entries_today"):
                    entries_today = j_stats["entries_today"]
                # If legs in memory are empty or have 0 PnL, supplement from journal legs
                if j_stats.get("legs"):
                    for r, jl in j_stats["legs"].items():
                        if r not in legs_summary or (legs_summary[r].get("entry", 0.0) == 0.0 and legs_summary[r].get("pnl_pts", 0.0) == 0.0):
                            legs_summary[r] = jl

    total_pts = round(realized_pts + open_pts, 2)
    total_rs = round(realized_rs + open_rs, 2)

    entry_spot = st.get("spot")
    live_spot = get_live_nifty_spot() or entry_spot

    return {
        "lot_size": lot_size,
        "lots": lots,
        "qty": qty,
        "realized_pts": realized_pts,
        "realized_rs": realized_rs,
        "open_pts": open_pts,
        "open_rs": open_rs,
        "total_pts": total_pts,
        "total_rs": total_rs,
        "legs": legs_summary,
        "comb_entry": comb_entry,
        "comb_current": comb_curr,
        "comb_sl_level": comb_sl_level,
        "entries_today": entries_today,
        "entry_spot": entry_spot,
        "live_spot": live_spot,
        "orders_strat_pnl": orders_strat_pnl,
        "orders_strat_count": orders_strat_count,
    }
