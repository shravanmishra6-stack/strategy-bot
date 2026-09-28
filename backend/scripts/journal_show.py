"""READ-ONLY: print a journaled day as text, for a terminal check.

    python scripts/journal_show.py                 # latest day
    python scripts/journal_show.py 2026-09-28      # a specific day

Nothing is written and nothing is sent to the broker; this only reads the
workbooks back. The point is to see the same numbers the dashboard shows.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app import journal as J  # noqa: E402


def _txt(value) -> str:
    """Blank for None and for the NaN an empty Excel cell reads back as."""
    if value is None or value != value:
        return ""
    return str(value)


def show(path: Path) -> None:
    print(f"workbook: {path}")
    data = J.load_day(path)
    if not data.get("exists"):
        print("  (not written yet)")
        return

    print("\n--- Summary ---")
    for row in data.get(J.SUMMARY_SHEET) or []:
        print(f"{_txt(row.get('field')):<40} {_txt(row.get('value'))}")

    print("\n--- Positions ---")
    positions = data.get(J.POSITIONS_SHEET) or []
    if not positions:
        print("  (no positions)")
    for r in positions:
        print(f"  {r.get('right')}{r.get('strike')} x{r.get('qty')}  "
              f"in {_txt(r.get('entry_time'))}@{_txt(r.get('entry_price'))}  "
              f"out {_txt(r.get('exit_time'))}@{_txt(r.get('exit_price'))}  "
              f"pnl={_txt(r.get('pnl_inr'))} open={_txt(r.get('is_open'))} "
              f"{_txt(r.get('note'))}")

    print("\n--- Fills ---")
    fills = data.get(J.FILLS_SHEET) or []
    if not fills:
        print("  (no fills)")
    for f in fills:
        print(f"  {_txt(f.get('time'))} {_txt(f.get('action')):<4} "
              f"{_txt(f.get('right'))}{_txt(f.get('strike'))} "
              f"x{_txt(f.get('qty'))} @{_txt(f.get('price'))} "
              f"{_txt(f.get('mode'))} {_txt(f.get('reason'))} "
              f"oos={_txt(f.get('order_id'))} tag={_txt(f.get('tag'))}")

    print("\n--- Broker orders (API only) ---")
    api = [o for o in (data.get(J.ORDERS_SHEET) or [])
           if str(o.get("source", "")).upper() == "API"]
    if not api:
        print("  (none recorded)")
    for o in api:
        print(f"  {_txt(o.get('time'))} {_txt(o.get('order_id'))} "
              f"{_txt(o.get('status')):<17} {_txt(o.get('action')):<4} "
              f"{_txt(o.get('symbol'))} qty={_txt(o.get('qty'))} "
              f"fill={_txt(o.get('fill_qty'))} avg={_txt(o.get('avg_price'))} "
              f"{_txt(o.get('price_type'))} tag={_txt(o.get('tag'))} "
              f"{_txt(o.get('reject_reason'))[:44]}")


def show_master() -> None:
    print(f"\nmaster: {J.master_path()}")
    rows = J.load_master()
    if not rows:
        print("  (no days recorded)")
        return
    for r in rows:
        print(f"  {_txt(r.get('date'))} {_txt(r.get('weekday')):<4} "
              f"mode={_txt(r.get('mode'))} fills={_txt(r.get('fills'))} "
              f"pos={_txt(r.get('positions'))} "
              f"open={_txt(r.get('open_positions'))} "
              f"pnl={_txt(r.get('pnl_inr'))} "
              f"api={_txt(r.get('broker_api_orders'))}/"
              f"{_txt(r.get('broker_api_accepted'))} "
              f"matched={_txt(r.get('broker_matched'))} "
              f"orphans={_txt(r.get('broker_orphans'))} "
              f"recon={_txt(r.get('recon_matches'))} "
              f"status={_txt(r.get('session_status'))}")


def main(argv) -> int:
    day = argv[1] if len(argv) > 1 else None
    path = J.daily_path(day) if day else J.latest_workbook()
    if not path:
        print("no journal found in", J.journal_dir())
        return 1
    show(path)
    show_master()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
