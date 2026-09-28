"""Backfill the Excel journal from a running server's /api/status.

Written for the day the journal code itself was deployed: a session already in
progress holds its fills only in the old process's RAM, so restarting without
this would lose the day. It is read-only against the broker - one GET of the
dashboard's own status endpoint, then the same Journal the session uses does the
writing. Run it before restarting the server, or any time afterwards to rebuild
a day from whatever the server still remembers.

    python scripts/journal_from_status.py                     # today, live URL
    python scripts/journal_from_status.py --url http://host:8000
    python scripts/journal_from_status.py --day 2026-09-28
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import journal as J  # noqa: E402
from app.broker.instruments import expiry_from_symbol  # noqa: E402
from app.config import settings  # noqa: E402

# /api/status keys the journal reads, mapped to the names SessionState uses.
STATUS_KEYS = ("status", "day", "phase", "spot", "atm", "lots", "live_armed",
               "live_mode", "error", "resolved", "legs", "fills", "risk",
               "errors", "entry_deferred", "entry_deferred_reason",
               "broker_rejections")


def fetch(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/api/status",
                                headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def to_status(snapshot: dict, day: str) -> dict:
    out = {k: snapshot[k] for k in STATUS_KEYS if k in snapshot}
    out["day"] = day
    # `errors` lives at the top level of the session blob, and the journal's
    # strategy-error column expects a list.
    errors = out.get("errors")
    out["errors"] = list(errors) if isinstance(errors, list) else (
        [str(errors)] if errors else [])
    return out


def day_context(fills, *, lots=1) -> dict:
    """The day's contracts, recovered from the broker's own records.

    A server sitting at `idle` has already forgotten what the day resolved to -
    expiry, ATM, lot size and the two symbols live in the trading process's RAM
    - so backfilling after a restart would otherwise write those columns blank
    over a day that had them. Every value here is read off a fill the broker
    actually reported, not guessed: the symbol carries its own expiry and
    strike, and quantity per lot is the lot size.

    Returns nothing it cannot establish, so an unresolvable day just stays blank
    - which is the truthful answer, not a failure.
    """
    out: dict = {}
    strikes = sorted({int(f["strike"]) for f in fills or []
                      if str(f.get("right", "")).upper() in ("CE", "PE")
                      and f.get("strike")})
    # The strategy trades one ATM strike; if the day spans several, the pinned
    # ATM is not knowable from the fills and is left out rather than invented.
    if len(strikes) == 1:
        out["atm"] = strikes[0]
    for f in fills or []:
        symbol = str(f.get("symbol") or "")
        right = str(f.get("right", "")).upper()
        if right in ("CE", "PE") and symbol and not out.get(f"{right.lower()}_symbol"):
            out[f"{right.lower()}_symbol"] = symbol
        expiry = out.get("expiry") or expiry_from_symbol(symbol)
        if expiry:
            # As text: Excel types a bare date cell as a datetime and it reads
            # back "2026-09-29 00:00:00", which is noise in a report column.
            out["expiry"] = J._day_str(expiry)
    qty = {int(f["qty"]) for f in fills or [] if f.get("qty")}
    if len(qty) == 1 and lots:
        per_lot = next(iter(qty)) / lots
        if per_lot >= 1 and float(per_lot).is_integer():
            out["lot_size"] = int(per_lot)
    return out


# Day facts an operator can hand to the backfill, for the ones a restarted
# server has forgotten and that cannot be re-derived from a broker record.
# The first group describes what the day traded; the second describes how the
# session ended, which only the process that ran it ever knew.
CONTEXT_KEYS = ("expiry", "atm", "spot", "lot_size", "ce_symbol", "pe_symbol")
STATE_KEYS = ("status", "phase", "entry_deferred", "entry_deferred_reason",
              "broker_rejections", "error")
SETTABLE = CONTEXT_KEYS + STATE_KEYS


def _kv(text: str):
    """Parse a `--set key=value` pair, turning numbers into numbers."""
    key, sep, value = text.partition("=")
    key = key.strip().replace("-", "_")
    if key == "session_status":                   # the master sheet's name for it
        key = "status"
    if not sep or key not in SETTABLE:
        raise argparse.ArgumentTypeError(
            f"expected --set KEY=VALUE with KEY one of: {', '.join(SETTABLE)}")
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError(f"--set {key}: needs a value")
    if key in ("entry_deferred",):
        low = value.lower()
        if low in ("1", "true", "yes", "y", "on"):
            return key, True
        if low in ("0", "false", "no", "n", "off"):
            return key, False
        raise argparse.ArgumentTypeError(f"--set {key}: not a yes/no value")
    try:
        num = float(value)
    except ValueError:
        return key, value
    return key, (int(num) if num.is_integer() else num)


def prior_session(day, base=None) -> dict:
    """How the day ENDED, as an earlier write recorded it.

    `/api/status` describes the session the server is running *now*. Backfilling
    a day it is not trading means its vocabulary - `idle`, `phase=None` - is a
    fact about the tool, not about that day, and writing it over a day that ran
    and closed would make a completed day read as one the bot never touched.
    """
    row = J.day_record(day, base=base)
    out = {}
    for column, key in (("session_status", "status"), ("phase", "phase"),
                        ("entry_deferred", "entry_deferred"),
                        ("entry_deferred_reason", "entry_deferred_reason"),
                        ("broker_rejections", "broker_rejections")):
        value = row.get(column)
        if value not in (None, "") and value == value:      # skip None and NaN
            out[key] = value
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=f"http://{settings.api_host}:{settings.api_port}")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: server's day)")
    ap.add_argument("--dir", default=None,
                    help=f"journal folder (default: {settings.journal_dir})")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing workbook for that day")
    ap.add_argument("--no-orders", dest="orders", action="store_false",
                    help="skip the read-only broker order book (it fills the "
                         "Orders sheet, which is where a rejection reason and "
                         "any order the bot did get accepted becomes visible)")
    ap.add_argument("--set", dest="set", type=_kv, action="append",
                    metavar="KEY=VALUE", default=[],
                    help="supply a day fact the server no longer has, e.g. "
                         "--set lot_size=65 --set spot=22824.45. Repeatable; "
                         "overrides anything derived here.")
    args = ap.parse_args(argv)

    try:
        snapshot = fetch(args.url)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"cannot read {args.url}/api/status: {e}")
        return 2

    day = args.day or J._day_str(snapshot.get("day") or dt.date.today())
    base = Path(args.dir) if args.dir else None
    fills = snapshot.get("fills") or []

    if not args.force:
        existing = J.daily_path(day, base=base)
        if existing.exists():
            print(f"{existing.name} already exists - pass --force to rewrite it")
            return 0

    lots = snapshot.get("lots") or 1
    jr = J.Journal(day, base=base, lots=lots,
                   lot_size=(snapshot.get("resolved") or {}).get("lot_size"))

    if args.orders:
        # A SEPARATE read-only client: the running server is never asked to
        # place anything, and this fails soft - no session simply means no
        # Orders sheet, not a lost trade record.
        try:
            from app.broker.noren_client import ShoonyaClient

            probe = ShoonyaClient()
            if probe.connect():
                jr.client = probe
                jr.orders = jr._fetch_orders()
                jr.executions = jr._fetch_executions()
                print(f"broker books fetched: {len(jr.orders)} order(s), "
                      f"{len(jr.executions)} execution(s)")
            else:
                print("no broker session - writing without the Orders sheet")
        except Exception as e:
            print(f"broker books unavailable ({e}) - writing without them")

    # The broker's own record of what this bot executed. Needed whenever the
    # session that traded is gone: fills live in that process's ExecutionEngine,
    # so a restart loses them from /api/status while the broker still remembers.
    # Matched by `remarks` tag, so the bot's own orders are identified exactly
    # rather than guessed at from a timestamp.
    reconstructed = J.reconstruct_fills(jr.orders)
    if reconstructed and not fills:
        print(f"reconstructed {len(reconstructed)} executed bot fill(s) from the "
              f"broker book - the server reported {len(fills)}")
        fills = reconstructed
    elif reconstructed:
        fills = J.merge_fills(fills, reconstructed)

    # The server only knows what the CURRENT session resolved. Backfilling a day
    # it is not trading (status `idle`) means `resolved` is empty, and without
    # this the day's expiry / ATM / lot size would be rewritten blank over the
    # values an earlier write had recorded. Recovery is from the fills the broker
    # reported, then from whatever the journal already held for the day.
    status = to_status(snapshot, day)
    supplied = dict(args.set)
    # The server's own state only describes a day it is CURRENTLY trading. Asked
    # to rebuild a past one, its `idle` status, empty phase and zeroed counters
    # are facts about the tool, not about that day: writing them over a day the
    # bot ran and closed would make it read as one the bot never touched, and a
    # copied `broker_rejections=0` would mask the count the order book itself
    # proves. So for a past day each of these survives only if the operator
    # states it or an earlier write of that day recorded it; otherwise it is
    # dropped and the journal falls back to what is evidenced on disk.
    if J._day_str(snapshot.get("day") or "") != day or status.get("status") == "idle":
        carried = prior_session(day, base=base)
        for key in (*STATE_KEYS, "errors"):
            if key in supplied:
                status[key] = supplied[key]
            elif key in carried:
                status[key] = carried[key]
            else:
                status.pop(key, None)
        print("day-end state (this server's own values not trusted): " + ", ".join(
            f"{k}={status.get(k)}" for k in sorted(STATE_KEYS)))

    recovered = day_context(fills, lots=lots)
    # Operator-supplied facts the server has already forgotten and that cannot be
    # re-derived from a broker record - a spot reference is a market tick, not a
    # property of any order. Wins over anything derived, and is repeated back on
    # the console so the provenance of the number is on the record.
    recovered.update({k: v for k, v in supplied.items() if k in CONTEXT_KEYS})
    if recovered:
        resolved = dict(status.get("resolved") or {})
        for key, value in recovered.items():
            resolved.setdefault(key, value)
            # The Summary sheet reads ATM and spot off the top level of the
            # status, so an empty one has to be filled here as well.
            if key in ("atm", "spot") and status.get(key) in (None, ""):
                status[key] = value
        status["resolved"] = resolved
        jr.attach_day(lot_size=recovered.get("lot_size"),
                      params={k: v for k, v in recovered.items()
                              if k in ("expiry", "lot_size",
                                       "ce_symbol", "pe_symbol")
                              and v not in (None, "")})
        print("recovered day context: " + ", ".join(
            f"{k}={v}" for k, v in sorted(recovered.items())))

    path = jr.record(fills, status=status, force=True)
    if not path:
        print("journal write failed (see errors above)")
        return 1

    path = jr.finalize(status, fetch_orders=False) or path
    if not path:
        print("journal finalize failed (see errors above)")
        return 1
    print(f"journaled {day}: {len(jr.fills)} fill(s), "
          f"{len(jr.orders)} broker order(s) -> {path}")
    print(f"master sheet: {J.master_path(base)}")
    positions = jr.positions()
    if positions:
        import pandas as pd
        frame = pd.DataFrame(positions)
        cols = [c for c in ("right", "strike", "entry_time", "entry_price",
                           "exit_time", "exit_price", "exit_reason",
                           "pnl_inr", "is_open") if c in frame.columns]
        print("\n--- reconstructed positions ---")
        print(frame[cols].to_string(index=False))
        net = [v for v in (frame["pnl_inr"].tolist() or []) if v == v]
        if net:
            print(f"\nday net (broker prices): Rs {sum(net):,.2f}")
    open_legs = [p for p in positions if p.get("is_open")]
    if open_legs:
        print(f"\nWARNING: {len(open_legs)} leg(s) still open per the broker:")
        for p in open_legs:
            print(f"  {p['right']}{p['strike']} x{p['qty']} "
                  f"entered {p['entry_time']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
