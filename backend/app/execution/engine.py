"""Execution engine: paper fills + order intent, shared by live and replay.

Paper mode never touches the broker. Live mode requires the client to be
armed, and every order carries an idempotency tag (`remarks` + local tag) so a
timeout can be reconciled against the order book instead of blindly re-sent.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import uuid
import time
from dataclasses import asdict, dataclass, field

from app.broker.noren_client import PaperMode
from app.broker.noren_client import ALLOWED_LIVE_PRICE_TYPE, REJECTED_LIVE_PRICE_TYPES
from app.config import settings

log = logging.getLogger("execution")


class OrderNotSubmitted(RuntimeError):
    """The order was refused BEFORE it could reach the broker.

    This is a policy decision (paper mode, not armed, kill switch, missing
    session, incomplete instrument), never an ambiguous network state, so the
    engine must NOT leave a reconciliation marker behind for it.
    """


class PermanentOrderRefusal(OrderNotSubmitted):
    """A pre-send refusal that can NEVER succeed on retry.

    "Client not armed" is retryable - the operator may arm a minute later. A
    forbidden order type is not: the broker rejects it every single time, so
    retrying it is exactly the hammer-the-broker-forever loop this fix exists
    to stop. Subclasses OrderNotSubmitted so it still carries the "nothing was
    sent, no reconcile marker" meaning.
    """


@dataclass
class Fill:
    ts: dt.datetime
    right: str
    strike: int
    token: str | None
    action: str          # "SELL" | "BUY"
    qty: int             # shares (lots x broker lot size), never lots
    price: float
    reason: str          # ENTRY / SL / TARGET / SQUAREOFF / ...
    tag: str
    mode: str            # "paper" | "live"
    order_id: str | None = None
    symbol: str | None = None      # broker tradingsymbol, e.g. NIFTY29SEP26C23400
    exchange: str | None = None    # broker segment, e.g. NFO

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat(sep=" ", timespec="seconds")
        return d


class ExecutionEngine:
    """Turns strategy intents into fills (paper) or broker orders (live)."""

    def __init__(self, client, paper_mode: bool = True,
                 product_type: str | None = None):
        self.client = client
        self.paper_mode = paper_mode
        # Shoonya product codes are C/I/M - "MIS" is not one of them (M = the
        # intraday product). Verified against the SDK's ProductType enum.
        self.product_type = product_type or settings.product_type
        self.fills: list[Fill] = []
        self._pending: dict[str, dict] = {}

    # --- intents -------------------------------------------------------------
    def sell_to_open(self, *, right: str, strike: int, qty: int, ref_price: float,
                     token: str | None = None, symbol: str | None = None,
                     exchange: str | None = None,
                     ts: dt.datetime | None = None) -> Fill:
        return self._send(action="SELL", right=right, strike=strike, qty=qty,
                          ref_price=ref_price, token=token, symbol=symbol,
                          exchange=exchange, reason="ENTRY", ts=ts)

    def buy_to_close(self, *, right: str, strike: int, qty: int, ref_price: float,
                     reason: str, token: str | None = None,
                     symbol: str | None = None, exchange: str | None = None,
                     ts: dt.datetime | None = None) -> Fill:
        return self._send(action="BUY", right=right, strike=strike, qty=qty,
                          ref_price=ref_price, token=token, symbol=symbol,
                          exchange=exchange, reason=reason, ts=ts)

    # --- core ----------------------------------------------------------------
    def _limit_price(self, action: str, ref_price: float, is_exit: bool = False) -> float:
        """Marketable limit price for one order.

        Shoonya rejects MKT for API orders ("ALGO_CHK: MKT Order type not
        allowed for API order"), so every order is sent as a LIMIT.

        Market Order behavior for EXITS (SL / Square-off / Stop):
        - When exiting, the order MUST execute immediately at prevailing market prices.
        - For an exit BUY order (closing short option), we price through the ask
          using an aggressive exit buffer (default 10% or minimum 3 pts above ref/ask).
          Because Indian exchange matching engines follow Price-Time Priority,
          a BUY LIMIT above the market matches instantly at the best available Ask (Market Price).
          The ceiling price guarantees that fast-moving markets or wider spreads fill immediately
          without sitting unexecuted in the order book.
        - For an ENTRY order (sell to open), we use a tighter limit buffer (default 0.5%)
          to prevent excessive execution slippage.
        """
        if is_exit:
            pct = float(getattr(settings, "order_exit_buffer_pct", 10.0)) / 100.0
            min_pts = float(getattr(settings, "order_exit_min_buffer_pts", 3.0))
        else:
            pct = float(getattr(settings, "order_limit_buffer_pct", 0.5)) / 100.0
            min_pts = 0.0

        tick = float(getattr(settings, "order_tick_size", 0.05)) or 0.05
        ref = float(ref_price)
        if action == "BUY":
            raw = max(ref * (1.0 + pct), ref + min_pts)
            steps = math.ceil(raw / tick)
            px = round(steps * tick, 2)
        else:
            raw = min(ref * (1.0 - pct), ref - min_pts)
            steps = math.floor(raw / tick)
            px = round(steps * tick, 2)
            if ref > 0:
                px = min(px, ref)
        if ref > 0:
            px = max(px, tick)
        return px

    def _send(self, *, action: str, right: str, strike: int, qty: int,
              ref_price: float, token, symbol, reason: str, ts,
              exchange: str | None = None) -> Fill:
        ts = ts or dt.datetime.now()
        tag = f"{right}{strike}-{action[:2]}-{uuid.uuid4().hex[:8]}"
        is_exit = reason.upper() != "ENTRY"

        if self.paper_mode:
            fill = Fill(ts=ts, right=right, strike=strike, token=token,
                        action=action, qty=qty, price=float(ref_price),
                        reason=reason, tag=tag, mode="paper",
                        symbol=symbol, exchange=exchange)
            self.fills.append(fill)
            log.info("PAPER %s %s x%d @%.2f (%s)", action, right, qty,
                     ref_price, reason)
            return fill

        # Live path: a policy refusal must never be mistaken for an ambiguous
        # submission. Check the gates BEFORE touching the broker, so a refusal
        # raises cleanly and leaves _pending empty.
        if not getattr(self.client, "live_armed", False):
            raise OrderNotSubmitted(
                f"live order refused: client not armed for live trading "
                f"(paper_mode={self.paper_mode})"
            )

        # A guessed symbol/token is far more dangerous than a refused order:
        # an unknown contract can still be accepted by the broker at a random
        # instrument. Resolve everything up front or refuse.
        if not symbol or not token:
            raise OrderNotSubmitted(
                f"live order refused: incomplete instrument "
                f"(symbol={symbol!r} token={token!r}) - refusing to guess"
            )
        exch = exchange or settings.fno_exchange

        # For live exit orders, inspect real-time best Ask (sp1) from market depth if available
        if is_exit and action == "BUY" and self.client and hasattr(self.client, "get_quotes"):
            try:
                q = self.client.get_quotes(exch, token)
                if q and q.get("sp1"):
                    best_ask = float(q["sp1"])
                    if best_ask > 0:
                        ref_price = max(ref_price, best_ask)
            except Exception as e:
                log.debug("Quote depth lookup for %s skipped: %s", symbol, e)

        limit_px = self._limit_price(action, ref_price, is_exit=is_exit)
        price_type = ALLOWED_LIVE_PRICE_TYPE

        # Last line of defence: if a future edit ever makes this send MKT, the
        # order would be rejected by the broker every single poll - a silent,
        # endless retry loop that looks like a quote problem on the dashboard.
        # Refuse locally (still "not submitted", so no reconcile marker) rather
        # than let a guaranteed-bad request leave the process.
        if price_type.upper() in REJECTED_LIVE_PRICE_TYPES:
            raise PermanentOrderRefusal(
                f"live order refused: {price_type} is rejected for API orders "
                f"by the broker (ALGO_CHK); a LIMIT is required"
            )

        # Only exceptions at/after this point mean "the request may have left".
        try:
            resp = self.client.place_order(
                buy_or_sell="B" if action == "BUY" else "S",
                product_type=self.product_type, exchange=exch,
                tradingsymbol=symbol,
                quantity=qty, price_type=price_type, price=limit_px,
                trigger_price=0, remarks=tag,
            )
        except PaperMode as e:
            # Client refused deterministically before sending - not ambiguous.
            raise OrderNotSubmitted(str(e)) from e
        except Exception as e:  # ambiguous: order may exist at the broker
            self._pending[tag] = {"action": action, "strike": strike,
                                  "right": right, "error": str(e),
                                  "symbol": symbol, "qty": qty, "token": token}
            log.error("LIVE ORDER AMBIGUOUS %s %s x%d: %s (reconcile needed)",
                      action, symbol, qty, e)
            raise
        if not resp or not resp.get("norenordno"):
            raise RuntimeError(f"Live {action} rejected: {resp}")
        orderno = resp["norenordno"]

        fill_price = float(ref_price)
        if is_exit and hasattr(self.client, "get_order_book"):
            try:
                time.sleep(0.3)
                book = self.client.get_order_book()
                match = next((o for o in book if str(o.get("norenordno", o.get("orderno", ""))) == str(orderno)), None)
                if match:
                    st = str(match.get("status", "")).upper()
                    if st == "COMPLETE":
                        fill_price = float(match.get("avgprc", ref_price) or ref_price)
                        log.info("LIVE EXIT COMPLETE %s %s x%d @ avg %.2f", action, symbol, qty, fill_price)
                    elif st in ("OPEN", "PENDING", "TRIGGER_PENDING"):
                        tick = float(getattr(settings, "order_tick_size", 0.05)) or 0.05
                        chase_px = round(limit_px * 1.05 / tick) * tick
                        log.warning("LIVE EXIT %s %s x%d still %s at %.2f; modifying to chase fill at %.2f...", action, symbol, qty, st, limit_px, chase_px)
                        try:
                            self.client.modify_order(
                                orderno=orderno, exchange=exch, tradingsymbol=symbol,
                                newquantity=qty, newprice_type="LMT", newprice=chase_px
                            )
                            log.info("Chased exit order %s to %.2f", orderno, chase_px)
                        except Exception as ce:
                            log.error("Failed to modify exit order %s: %s", orderno, ce)
            except Exception as e:
                log.debug("Exit fill check exception: %s", e)

        fill = Fill(ts=ts, right=right, strike=strike, token=token, action=action,
                    qty=qty, price=fill_price, reason=reason, tag=tag,
                    mode="live", symbol=symbol, exchange=exch)
        fill.order_id = orderno  # type: ignore[attr-defined]
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
