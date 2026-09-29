"""
Backtest engine for the short-straddle seller.

Faithful port of `nifty_breeze_data/Backtest/run_backtest_seller.py`, with the
per-leg exit rules delegated to app.strategy.legs.SoldLeg so that backtest and
live share one rule implementation. Exit-price convention, cost model
(brokerage + slippage) and lot schedule are preserved unchanged so results stay
comparable with sell_sweep_results.csv.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from app.backtest.metrics import summarize
from app.strategy.legs import (
    COMBINED_SL,
    EOD_FINAL,
    HOLD_TO_EXPIRY,
    SL,
    SQUAREOFF,
    TRAIL_SL,
    Bar,
    SoldLeg,
)
from app.strategy.params import StraddleParams

STOP_REASONS = (SL, COMBINED_SL, TRAIL_SL)


def _clean_price(val, fallback: float) -> float:
    try:
        px = float(val)
    except (TypeError, ValueError):
        return fallback
    if np.isnan(px) or px <= 0:
        return fallback
    return px


def backtest_one_day(
    day: dt.date,
    expiry: dt.date,
    spot: pd.DataFrame,
    cache,
    p: StraddleParams,
) -> dict | None:
    """Simulate one straddle/strangle sold on `day` for `expiry`."""
    day_str = day.strftime("%Y-%m-%d")
    entry_dt = pd.Timestamp(f"{day_str} {p.entry_time}")
    if entry_dt not in spot.index:
        return None
    spot_at_entry = float(spot.loc[entry_dt, "open"])

    atm = int(round(spot_at_entry / p.strike_step) * p.strike_step)
    # Mirrors the original: CE at ATM, PE shifted further OTM by strike_offset.
    strikes = {"CE": atm, "PE": atm + p.strike_offset * p.strike_step}

    legs: dict[str, dict] = {}
    for right, strike in strikes.items():
        df = cache.get(expiry, strike, right)
        if df is None or entry_dt not in df.index:
            return None
        entry = _clean_price(df.loc[entry_dt, "open"], 0.0)
        if entry <= 0:
            return None
        leg = SoldLeg(
            right=right,
            strike=strike,
            sl_pct=p.sl_pct,
            target_pct=p.target_pct,
            trail_pct=p.trail_pct,
        )
        leg.arm(entry)
        legs[right] = {"leg": leg, "df": df}

    day_start = pd.Timestamp(day_str)
    timeline = legs["CE"]["df"].index[legs["CE"]["df"].index >= day_start].union(
        legs["PE"]["df"].index[legs["PE"]["df"].index >= day_start]
    )
    timeline = timeline[timeline >= entry_dt]
    if timeline.empty:
        return None

    if p.expiry_mode == "EOD":
        so_dt = pd.Timestamp(f"{day_str} {p.squareoff_time}")
    else:
        so_dt = timeline.max() + pd.Timedelta(minutes=1)

    comb_sl = None
    if p.combined_sl_pct is not None:
        entries = [box["leg"].entry for box in legs.values() if box["leg"].entry is not None]
        if len(entries) == len(legs):
            comb_sl = round(sum(entries) * p.combined_sl_pct, 2)
            for box in legs.values():
                box["leg"].sl_level = comb_sl

    exits = []
    for ts in timeline:
        if ts >= so_dt:
            reason = SQUAREOFF if p.expiry_mode == "EOD" else HOLD_TO_EXPIRY
            for right, box in legs.items():
                leg, df = box["leg"], box["df"]
                if leg.is_open:
                    px = (
                        _clean_price(df.loc[ts, "open"], leg.last_close)
                        if ts in df.index
                        else leg.last_close
                    )
                    exits.append((right, ts, px, reason))
                    leg.is_open = False
            break


        for right, box in legs.items():
            leg, df = box["leg"], box["df"]
            if not leg.is_open or ts not in df.index:
                continue
            res = leg.on_bar(Bar.from_row(df.loc[ts]))
            if res:
                exits.append((right, ts, res[1], res[0]))
                if res[0] == SL and p.sl_to_cost:
                    for other_right, other_box in legs.items():
                        if other_box["leg"].is_open and other_box["leg"].entry is not None:
                            other_box["leg"].sl_level = other_box["leg"].entry

    for right, box in legs.items():
        leg = box["leg"]
        if leg.is_open:
            exits.append((right, timeline.max(), leg.last_close, EOD_FINAL))
            leg.is_open = False

    lot = p.lot_size(day)
    total_pts = 0.0
    leg_rows = []
    for right, ts, px, reason in exits:
        leg = legs[right]["leg"]
        # Sold leg profits as premium falls; costs are per-leg slippage plus
        # per-order brokerage expressed in points (brokerage / lot).
        net = (leg.entry - px) - p.slippage - (p.brokerage / lot)
        total_pts += net
        leg_rows.append(
            (right, leg.strike, leg.entry, round(px, 2), round(net, 2), reason)
        )

    if not leg_rows:
        return None

    return {
        "date": day_str,
        "expiry": str(expiry),
        "spot": round(spot_at_entry, 2),
        "ce_strike": legs["CE"]["leg"].strike,
        "pe_strike": legs["PE"]["leg"].strike,
        "entry_time": entry_dt.strftime("%H:%M"),
        "exit_time": max(r[1] for r in exits).strftime("%H:%M"),
        "ce_entry": round(legs["CE"]["leg"].entry, 2),
        "pe_entry": round(legs["PE"]["leg"].entry, 2),
        "legs_stopped": sum(1 for r in leg_rows if r[5] in STOP_REASONS),
        "exit_reason": "+".join(sorted({r[5] for r in leg_rows})),
        "pnl_pts": round(total_pts, 2),
        "lot": lot,
        "pnl_inr": round(total_pts * lot, 2),
        "leg_details": leg_rows,
    }


def run_backtest(
    p: StraddleParams,
    spot: pd.DataFrame,
    expiries: list[dt.date],
    cache,
    day_to_expiry: dict[dt.date, dt.date] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> pd.DataFrame:
    """Run `p` across every mapped trading day; returns the trade log frame."""
    if day_to_expiry is None:
        from app.data.loader import build_day_to_expiry

        day_to_expiry = build_day_to_expiry(spot, expiries)

    trade_days = sorted(day_to_expiry)
    if start:
        trade_days = [d for d in trade_days if d >= start]
    if end:
        trade_days = [d for d in trade_days if d <= end]

    rows = []
    for d in trade_days:
        e = day_to_expiry.get(d)
        if e is None:
            continue
        t = backtest_one_day(d, e, spot, cache, p)
        if t:
            rows.append(t)

    if not rows:
        return pd.DataFrame()

    tdf = pd.DataFrame(
        [{k: v for k, v in t.items() if k != "leg_details"} for t in rows]
    )
    tdf["cum_pts"] = tdf["pnl_pts"].cumsum().round(2)
    tdf["cum_inr"] = tdf["pnl_inr"].cumsum().round(2)
    tdf["peak"] = tdf["cum_pts"].cummax()
    tdf["dd_pts"] = (tdf["cum_pts"] - tdf["peak"]).round(2)
    return tdf


def backtest_report(p: StraddleParams, tdf: pd.DataFrame) -> dict:
    """Metrics dict shaped for the API / dashboard."""
    if tdf.empty:
        return {"config": p.label(), "params": p.to_dict(), "trades": 0, "metrics": {}}
    return {
        "config": p.label(),
        "params": p.to_dict(),
        "trades": int(len(tdf)),
        "first_date": str(tdf["date"].iloc[0]),
        "last_date": str(tdf["date"].iloc[-1]),
        "metrics": summarize(tdf),
    }
