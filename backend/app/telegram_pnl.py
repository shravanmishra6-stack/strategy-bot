"""Unified P&L and Position formatting helper for Strategy Bot Telegram notifications & supervisor.

Ensures Telegram bot outputs match the web dashboard logic 100%.
Handles both live engine snapshots and test mock structures seamlessly.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx

_spot_cache = {"price": None, "ts": 0.0}


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
        "entries_today": risk.get("entries_today", 0),
        "entry_spot": entry_spot,
        "live_spot": live_spot,
    }
