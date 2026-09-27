"""Read-only broker smoke test - SAFE: places no orders, ever.

Run after `python -m scripts.oauth_login --authcode ...` succeeds:
    python -m scripts.readonly_check

Validates, in order of risk:
  1. connect()            - injectOAuthHeader + get_limits probe
  2. get_limits           - margin/money (proves auth works end-to-end)
  3. get_positions        - should be empty for a fresh account
  4. get_order_book       - proves order-book field naming (rk/stat/remarks)
  5. searchscrip NIFTY    - proves the index token we build symbols on
  6. get_quotes (index)   - proves market-data path
  7. expiry discovery     - option-chain probing for real NIFTY expiries
  8. resolve ATM CE/PE    - tradingsymbol -> token round trip

Any failure prints what to fix; nothing here can move money.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from app.broker.noren_client import BrokerError, SessionExpiredError, client


def _show(label: str, value) -> None:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    if len(text) > 900:
        text = text[:900] + f"... [{len(text)} chars total]"
    print(f"  {label}: {text}")


def main() -> int:
    from app.config import settings

    print(f"host={settings.host}")
    print(f"uid={settings.uid} account={settings.account_id} "
          f"token_len={len(settings.access_token)}")
    print(f"paper_mode={settings.paper_mode} live_armed={client.live_armed}")

    if not settings.has_credentials:
        print("\nNo credentials in .env - run scripts.oauth_login first.")
        return 1

    print("\n[1] connect() ...")
    try:
        ok = client.connect()
    except SessionExpiredError as e:
        print(f"    SESSION EXPIRED: {e}\n    Re-run the OAuth flow (both phases).")
        return 2
    except BrokerError as e:
        print(f"    CONNECT FAILED: {e}")
        print("    Check SHOONYA_HOST / token validity.")
        return 2
    _show("session_ok", client.session_ok)
    if not ok:
        return 2

    print("\n[2] get_limits() ...")
    try:
        limits = client.get_limits()
        _show("limits", limits)
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[3] get_positions() ...")
    try:
        pos = client.get_positions()
        _show(f"count={len(pos)}", pos[:3])
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[4] get_order_book() ...")
    try:
        book = client.get_order_book()
        _show(f"count={len(book)}", book[:2])
        if book:
            print("    order-book keys:", sorted(book[0].keys()))
    except Exception as e:
        print(f"    failed: {e}")

    from app.broker.instruments import InstrumentResolver

    res = InstrumentResolver(client)
    today = dt.date.today()

    print("\n[5] searchscrip(NSE_FNO, NIFTY) - confirm index token ...")
    try:
        rows = res._scrip_rows(client.searchscrip(res.exchange, "NIFTY"))
        _show(f"hits={len(rows)}", rows[:6])
        for r in rows:
            if res._row_symbol(r).upper().replace(" ", "") in ("NIFTY", "NIFTY50"):
                tok = res._row_token(r)
                print(f"    -> index token: {tok!r} (resolver default "
                      f"{res.index_token!r} - override if these differ)")
                break
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[6] get_quotes on the index (market-data path) ...")
    try:
        _show("quote", client.get_quotes(res.exchange, res.index_token))
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[7] expiry discovery via get_option_chain ...")
    try:
        exps = res.available_expiries(today)
        _show("expiries", [str(e) for e in exps])
    except Exception as e:
        print(f"    failed: {e}")
        exps = []

    print("\n[8] resolve ATM CE/PE for the nearest expiry ...")
    if exps:
        spot = None
        try:
            q = client.get_quotes(res.exchange, res.index_token) or {}
            if isinstance(q, dict):
                for k in ("rt_price", "ltp", "pclose"):
                    if q.get(k):
                        spot = float(q[k])
                        break
        except Exception as e:
            print(f"    quote for ATM failed: {e}")
        atm = int(round((spot or 25000) / 50) * 50)
        print(f"    spot={spot} -> ATM strike {atm}, expiry {exps[0]}")
        try:
            legs = res.resolve(expiry=exps[0], atm=atm, step=50)
            for lg in legs:
                _show(f"{lg.right} {lg.strike}", vars(lg))
        except Exception as e:
            print(f"    resolve FAILED: {e}")
    else:
        print("    skipped (no expiries found)")

    print("\nDone. (No orders were sent.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
