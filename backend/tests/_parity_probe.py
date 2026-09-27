"""Temporary parity probe: run key sweep configs through the new engine."""
import datetime as dt

from app.backtest.engine import run_backtest
from app.config import settings
from app.data.loader import OptionCache, build_day_to_expiry, list_expiries, load_spot
from app.strategy.params import StraddleParams

spot = load_spot(settings.spot_dir)
expiries = list_expiries(settings.options_dir)
cache = OptionCache(settings.options_dir)
d2e = build_day_to_expiry(spot, expiries)
print(
    f"spot {spot.index.min()} -> {spot.index.max()} | "
    f"expiries {len(expiries)} | mapped days {len(d2e)}"
)

CONFIGS = [
    ("SL 30%", dt.time(9, 20), 0.30, 0),
    ("SL 50%", dt.time(9, 20), 0.50, 0),
    ("SL 60%", dt.time(9, 20), 0.60, 0),
    ("SL 80%", dt.time(9, 20), 0.80, 0),
    ("Entry 09:15 SL50", dt.time(9, 15), 0.50, 0),
    ("Strangle +1 SL50", dt.time(9, 20), 0.50, 1),
    ("SL50 +TGT 50%", dt.time(9, 20), 0.50, 0),
]

for label, entry, sl, offset in CONFIGS:
    p = StraddleParams(entry_time=entry, sl_pct=sl, strike_offset=offset)
    if label == "SL50 +TGT 50%":
        p.target_pct = 0.50
    tdf = run_backtest(p, spot, expiries, cache, day_to_expiry=d2e)
    if tdf.empty:
        print(f"{label:20s} NO TRADES")
        continue
    wins = tdf[tdf.pnl_pts > 0].pnl_pts.sum()
    loss = abs(tdf[tdf.pnl_pts < 0].pnl_pts.sum())
    dd = (tdf.cum_pts - tdf.peak).min()
    print(
        f"{label:20s} n={len(tdf):3d} win={100*(tdf.pnl_pts>0).mean():5.1f}% "
        f"pts={tdf.pnl_pts.sum():8.1f} Rs={tdf.pnl_inr.sum():10,.0f} "
        f"dd={dd:8.1f} pf={wins/loss if loss else float('inf'):5.2f} "
        f"stop%={100*(tdf.legs_stopped>0).mean():3.0f}"
    )

