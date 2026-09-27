"""Run the ORIGINAL run_backtest_seller.py at SL 30% to test whether the
published CSV row is stale vs the current data dump. Read-only wrt the repo."""
import datetime as dt
import os
import sys

sys.path.insert(0, r"C:\Users\sharv\Desktop\nifty_breeze_data\Backtest")
import run_backtest_seller as m  # noqa: E402

m.ENTRY_TIME = dt.time(9, 20)
m.SQUAREOFF_TIME = dt.time(15, 15)
m.EXPIRY_MODE = "EOD"
m.TRAIL_PCT = None
m.TARGET_PCT = None
m.STRIKE_OFFSET = 0
m.SL_PCT = 0.50

spot = m.load_spot()
expiries = m.active_expiries()
trade_days = sorted({d.date() for d in spot.index.normalize().unique()})
day_to_exp = {}
for d in trade_days:
    for e in expiries:
        if e >= d:
            day_to_exp[d] = e
            break

CACHE = {}
rows = []
for d in trade_days:
    e = day_to_exp.get(d)
    if e is None:
        continue
    t = m.backtest_one_day(d, e, spot, CACHE)
    if t:
        t.pop("leg_details", None)
        rows.append(t)

import pandas as pd  # noqa: E402

df = pd.DataFrame(rows)
wins = df[df.pnl_pts > 0].pnl_pts.sum()
loss = abs(df[df.pnl_pts < 0].pnl_pts.sum())
cum = df.pnl_pts.cumsum()
print("ORIGINAL SCRIPT, SL 30%, current data dump:")
print(f"  n={len(df)} win={100*(df.pnl_pts>0).mean():.1f}% "
      f"pts={df.pnl_pts.sum():.1f} Rs={df.pnl_inr.sum():,.0f} "
      f"dd={(cum-cum.cummax()).min():.1f} pf={wins/loss:.2f}")

