"""Read-only broker smoke test - SAFE: places no orders, ever.

Run after `python -m scripts.oauth_login --authcode ...` succeeds:
    python -m scripts.readonly_check

Validates, in order of risk:
  1. connect()            - injectOAuthHeader + get_limits probe
  2. get_limits           - margin/money (proves auth works end-to-end)
  3. get_positions        - the real holding book (NFO rows prove the segment)
  4. get_order_book       - order-book field naming (rk/stat/remarks)
  5. searchscrip NFO      - proves the F&O segment + scrip row shape
  6. get_quotes (index)   - proves the market-data path on the spot index
  7. expiry discovery     - the real weekly/monthly contract calendar
  8. option chain + ATM   - tradingsymbol -> token -> lot size round trip

Every step prints the broker's own words. The vendored SDK swallows `emsg`
(returns None on any Not_Ok), so DEBUG logging on NorenRestApiPy.NorenApi is
switched on here purely to surface the real reason for a failure. Nothing
here can move money: this file contains no order call.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys

from app.broker.noren_client import BrokerError, SessionExpiredError, client


def _show(label: str, value) -> None:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    if len(text) > 900:
        text = text[:900] + f"... [{len(text)} chars total]"
    print(f"  {label}: {text}")


def _sdk_debug() -> None:
    """Let the SDK's reportmsg() show the broker emsg that it discards."""
    logging.basicConfig(level=logging.INFO,
                        format="      [sdk] %(message)s", stream=sys.stdout)
    logging.getLogger("NorenRestApiPy.NorenApi").setLevel(logging.DEBUG)


def main() -> int:
    from app.broker.instruments import InstrumentResolver, expiry_from_symbol
    from app.config import settings

    print(f"host={settings.host}")
    print(f"uid={settings.uid} account={settings.account_id} "
          f"token_len={len(settings.access_token)}")
    print(f"paper_mode={settings.paper_mode} live_armed={client.live_armed}")
    print(f"segments: fno={settings.fno_exchange} "
          f"index={settings.index_exchange}/{settings.index_token} "
          f"root={settings.root_symbol}")

    if not settings.has_credentials:
        print("\nNo credentials in .env - run scripts.oauth_login first.")
        return 1
    _sdk_debug()

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
        _show("cash", (client.get_limits() or {}).get("cash"))
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[3] get_positions() ...")
    try:
        pos = client.get_positions()
        print(f"    count={len(pos)}")
        for p in pos[:5]:
            _show("  row", {k: p.get(k) for k in
                            ("exch", "tsym", "token", "prd", "netqty", "urmtom")})
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[4] get_order_book() ...")
    try:
        book = client.get_order_book()
        print(f"    count={len(book)}")
        if book:
            print("    order-book keys:", sorted(book[0].keys()))
            _show("  first", {k: book[0].get(k) for k in
                              ("exch", "tsym", "status", "orderno", "prc",
                               "qty", "rk")})
        else:
            print("    (empty - no orders today, or the broker answered Not_Ok;"
                  " see [sdk] lines above)")
    except Exception as e:
        print(f"    failed: {e}")

    res = InstrumentResolver(client)
    today = dt.date.today()
    print(f"\n[5] searchscrip({res.exchange}, {res.root_symbol}) ...")
    try:
        rows = res.search(res.root_symbol)
        print(f"    rows={len(rows)}")
        if rows:
            print("    row keys:", sorted(rows[0].keys()))
            for r in rows[:4]:
                _show("  ", {k: r.get(k) for k in
                             ("tsym", "token", "exch", "seg", "instname",
                              "symname", "exd", "optt", "ls")})
        idx = res._scrip_rows(client.searchscrip(res.index_exchange,
                                                 res.root_symbol))
        hit = [r for r in idx
               if str(r.get("symname", "")).upper().replace(" ", "")
               in (res.root_symbol, res.root_symbol + "50")]
        _show(f"index rows on {res.index_exchange}", hit[:2])
        if hit and str(hit[0].get("token")) != str(res.index_token):
            print(f"    !! index token is {hit[0].get('token')!r}, config says "
                  f"{res.index_token!r} - set SHOONYA_INDEX_TOKEN")
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[6] get_quotes on the spot index ...")
    spot = None
    try:
        _show("quote", client.get_quotes(res.index_exchange, res.index_token))
        spot = res.index_quote_price()
        _show("spot", spot)
    except Exception as e:
        print(f"    failed: {e}")

    print("\n[7] expiry discovery ...")
    try:
        exps = res.available_expiries(today)
        _show("expiries", [f"{e} ({e.strftime('%a')})" for e in exps])
    except Exception as e:
        print(f"    failed: {e}")
        exps = []

    print("\n[8] option chain + ATM CE/PE resolution ...")
    if not exps:
        print("    skipped (no expiries found - refusing to guess a contract)")
        return 3
    ref = spot or 25000.0
    atm = int(round(ref / res.strike_step) * res.strike_step)
    expiry = res.nearest_expiry(today, exps)
    print(f"    expiry={expiry} spot={spot} -> ATM strike {atm}")
    try:
        root = res.chain_root_for(expiry, today)
        _show("chain root for expiry", root)
        chain = res.chain_rows(today=today, ref_strike=ref, count=2,
                               expiry=expiry)
        _show(f"chain rows (ref={ref})", chain[:4])
        if chain:
            print("    chain keys:", sorted(chain[0].keys()))
            # The chain must describe the expiry we picked, not some other month.
            chain_exps = {expiry_from_symbol(r.get("tsym", "")) for r in chain}
            _show("chain expiries", [str(e) for e in sorted(x for x in chain_exps if x)])
    except Exception as e:
        print(f"    chain failed: {e}")

    try:
        legs = res.resolve(expiry=expiry, atm=atm, step=res.strike_step)
        for lg in legs:
            _show(f"{lg.right} {lg.strike}", vars(lg))
        lots = {lg.lot_size for lg in legs} - {None}
        if not lots:
            print("    !! no broker lot size returned - order sizing would refuse")
            return 3
        print(f"    lot size from broker: {sorted(lots)} -> qty for "
              f"{settings.max_lots} lots = "
              f"{max(lots) * settings.max_lots} (risk cap {settings.max_lots})")
    except Exception as e:
        print(f"    resolve FAILED: {e}")
        return 3

    print("\nDone. (No orders were sent.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())