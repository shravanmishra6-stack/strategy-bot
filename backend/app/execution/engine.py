"""Execution engine: paper fills + order intent, shared by live and replay.

Paper mode never touches the broker. Live mode requires the client to be
armed, and every order carries an idempotency tag (`remarks` + local tag) so a
timeout can be reconciled against the order book instead of blindly re-sent.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import asdict, dataclass, field

log = logging.getLogger("execution")


@dataclass
class Fill:
    ts: dt.datetime
    right: str
    strike: int
    token: str | None
    action: str          # "SELL" | "BUY"
    qty: int
    price: float
    reason: str          # ENTRY / SL / TARGET / SQUAREOFF / ...
    tag: str
    mode: str            # "paper" | "live"
    order_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat(sep=" ", timespec="seconds")
        return d


class ExecutionEngine:
    """Turns strategy intents into fills (paper) or broker orders (live)."""

    def __init__(self, client, paper_mode: bool = True):
        self.client = client
        self.paper_mode = paper_mode
        self.fills: list[Fill] = []
        self._pending: dict[str, dict] = {}

    # --- intents -------------------------------------------------------------
    def sell_to_open(self, *, right: str, strike: int, qty: int, ref_price: float,
                     token: str | None = None, symbol: str | None = None,
                     ts: dt.datetime | None = None) -> Fill:
        return self._send(action="SELL", right=right, strike=strike, qty=qty,
                          ref_price=ref_price, token=token, symbol=symbol,
                          reason="ENTRY", ts=ts)

    def buy_to_close(self, *, right: str, strike: int, qty: int, ref_price: float,
                     reason: str, token: str | None = None,
                     symbol: str | None = None,
                     ts: dt.datetime | None = None) -> Fill:
        return self._send(action="BUY", right=right, strike=strike, qty=qty,
                          ref_price=ref_price, token=token, symbol=symbol,
                          reason=reason, ts=ts)

    # --- core ----------------------------------------------------------------
    def _send(self, *, action: str, right: str, strike: int, qty: int,
              ref_price: float, token, symbol, reason: str,
              ts) -> Fill:
        ts = ts or dt.datetime.now()
        tag = f"{right}{strike}-{action[:2]}-{uuid.uuid4().hex[:8]}"
        if self.paper_mode:
            fill = Fill(ts=ts, right=right, strike=strike, token=token,
                        action=action, qty=qty, price=float(ref_price),
                        reason=reason, tag=tag, mode="paper")
            self.fills.append(fill)
            log.info("PAPER %s %s x%d @%.2f (%s)", action, right, qty,
                     ref_price, reason)
            return fill

        # Live path: place, then confirm via the response; on any ambiguity the
        # tag is left in _pending for reconciliation and NO retry is issued.
        try:
            resp = self.client.place_order(
                buy_or_sell="B" if action == "BUY" else "S",
                product_type="MIS", exchange="NSE_FNO",
                tradingsymbol=symbol or f"NIFTY{strike}{right}",
                quantity=qty, price_type="MKT", price=0,
                trigger_price=0, remarks=tag,
            )
        except Exception as e:  # ambiguous: order may exist at the broker
            self._pending[tag] = {"action": action, "strike": strike,
                                  "right": right, "error": str(e)}
            log.error("LIVE ORDER AMBIGUOUS %s x%d %s: %s (reconcile needed)",
                      action, qty, strike, e)
            raise
        if not resp or not resp.get("norenordno"):
            raise RuntimeError(f"Live {action} rejected: {resp}")
        fill = Fill(ts=ts, right=right, strike=strike, token=token, action=action,
                    qty=qty, price=float(ref_price), reason=reason, tag=tag,
                    mode="live")
        fill.order_id = resp["norenordno"]  # type: ignore[attr-defined]
        self.fills.append(fill)
        return fill

    def reconcile(self) -> list[dict]:
        """Match ambiguous live orders against the broker order book by tag."""
        if not self._pending:
            return []
        try:
            book = self.client.get_order_book()
        except Exception as e:
            log.error("Reconcile failed (no order book): %s", e)
            return []
        resolved = []
        for tag, info in list(self._pending.items()):
            match = [o for o in book if tag in str(o.get("rk", o.get("remarks", "")))]
            if match:
                resolved.append({"tag": tag, "orders": match, **info})
                self._pending.pop(tag)
        if resolved:
            log.warning("Reconciled %d ambiguous orders: %s", len(resolved),
                        [r["tag"] for r in resolved])
        return resolved

    def pending_count(self) -> int:
        return len(self._pending)
