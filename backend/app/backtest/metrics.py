"""Performance metrics computed from a backtest trade log."""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(tdf: pd.DataFrame) -> dict:
    """Headline stats, matching the columns in sell_sweep_results.csv."""
    if tdf.empty or "pnl_pts" not in tdf:
        return {}

    pnl = tdf["pnl_pts"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    cum = pnl.cumsum()
    max_dd = float((cum - cum.cummax()).min()) if len(cum) else 0.0
    win_sum = float(wins.sum())
    loss_sum = abs(float(losses.sum()))

    return {
        "trades": int(len(tdf)),
        "win_rate": round(float((pnl > 0).mean() * 100), 1),
        "total_pts": round(float(pnl.sum()), 1),
        "total_inr": round(float(tdf["pnl_inr"].sum()), 0)
        if "pnl_inr" in tdf
        else None,
        "avg_pts": round(float(pnl.mean()), 2),
        "max_dd": round(max_dd, 1),
        "pf": round(win_sum / loss_sum, 2) if loss_sum else float("inf"),
        "avg_win": round(float(wins.mean()), 1) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 1) if len(losses) else 0.0,
        "best_pts": round(float(pnl.max()), 1),
        "worst_pts": round(float(pnl.min()), 1),
        "leg_stop_pct": round(float((tdf["legs_stopped"] > 0).mean() * 100), 0)
        if "legs_stopped" in tdf
        else None,
        "sharpe_like": round(
            float(pnl.mean() / pnl.std()), 3
        )
        if len(pnl) > 1 and not np.isclose(float(pnl.std()), 0.0)
        else None,
    }
