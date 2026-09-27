"""Live straddle state machine - same rules as the backtest, event-driven.

Parity contract with app/backtest/engine.py:
  * entry ATM  = round(first spot tick at/after entry_time / step) * step
  * leg entry  = last premium seen at/after entry_time (backtest uses the
                 option bar's open at that minute; live cannot see the future
                 open, so latest-premium + slippage assumption is the honest
                 equivalent)
  * exits      = SoldLeg.on_bar on COMPLETED 1-min option bars (identical to
                 the backtest - same SoldLeg code path)
  * square-off = buy-back of whatever is still open at squareoff_time
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from app.data.bars import MinuteBarBuilder
from app.strategy.legs import Bar, SoldLeg
from app.strategy.params import StraddleParams

log = logging.getLogger("strategy.live")


@dataclass
class LegInstrument:
    token: str
    tradingsymbol: str
    strike: int
    right: str
    # Broker-confirmed values, never guesses: NFO (not NSE_FNO) is the segment
    # this account can trade, and lot_size is the exchange lot (live probe:
    # ls=65 for NIFTY options). Order quantity = lots * lot_size.
    exchange: str = "NFO"
    lot_size: int | None = None


@dataclass
class LiveStraddle:
    params: StraddleParams
    exec_engine: object = None          # ExecutionEngine
    risk: object = None                 # RiskManager
    date: dt.date = None
    now: dt.datetime | None = None

    phase: str = "WAIT_ENTRY"           # WAIT_ENTRY -> IN_POSITION -> DONE
    spot_ref: float | None = None
    legs: dict = field(default_factory=dict)         # right -> SoldLeg
    instruments: dict = field(default_factory=dict)  # right -> LegInstrument
    builders: dict = field(default_factory=dict)     # token -> MinuteBarBuilder
    last_price: dict = field(default_factory=dict)   # token -> float
    lots: int = 1                         # contracts in LOTS (risk-gated)
    errors: list = field(default_factory=list)
    _spot_token: str | None = None
    _order_qty: dict = field(default_factory=dict)   # right -> validated qty

    def configure(self, *, spot_token: str,
                  legs: list[tuple[SoldLeg, LegInstrument]]):
        """Called once at warm-up with the day's instruments."""
        self._spot_token = spot_token
        for leg, inst in legs:
            self.legs[leg.right] = leg
            self.instruments[inst.right] = inst
            self.builders[inst.token] = MinuteBarBuilder(on_bar=None)

    # --- sizing ---------------------------------------------------------------
    def _qty_for(self, inst: LegInstrument) -> int:
        """Order quantity in shares: lots x the broker's lot size.

        Raises if the lot size is unknown - sending qty=1 for a 65-share lot
        would be a real, silent mispricing of the whole strategy.
        """
        lot = getattr(inst, "lot_size", None)
        if not lot or int(lot) <= 0:
            raise ValueError(
                f"{inst.tradingsymbol}: no broker lot size - refusing to size order")
        return int(self.lots) * int(lot)

    # --- tick fan-in ------------------------------------------------------
    def on_tick(self, ts: dt.datetime, token: str, price: float) -> None:
        self.now = ts
        if price is None or price <= 0:
            return
        self.last_price[token] = price
        if token == self._spot_token:
            self._on_spot(ts, price)
        elif token in self.builders and self.phase == "IN_POSITION":
            completed = self.builders[token].on_tick(ts, price)
            if completed:
                self._on_leg_bar(token, completed)
        elif token in self.builders:
            self.builders[token].on_tick(ts, price)  # keep bars warm pre-entry
        if (
            self.phase == "IN_POSITION"
            and self.now.time() >= self.params.squareoff_time
        ):
            self.square_off(ts)

    # --- entry ---------------------------------------------------------------
    def _on_spot(self, ts: dt.datetime, price: float) -> None:
        if self.phase != "WAIT_ENTRY" or ts.time() < self.params.entry_time:
            return
        self.spot_ref = price  # first tick at/after entry defines ATM
        self._try_enter(ts)

    def _try_enter(self, ts: dt.datetime) -> None:
        atm = int(round(self.spot_ref / self.params.strike_step)
                  * self.params.strike_step)
        want_pe = atm + self.params.strike_offset * self.params.strike_step
        ce, pe = self.instruments.get("CE"), self.instruments.get("PE")
        if not ce or not pe or ce.strike != atm or pe.strike != want_pe:
            self.errors.append(f"strikes not resolvable at ATM {atm}")
            log.error("%s", self.errors[-1])
            self.phase = "DONE"
            return
        decision = self.risk.allow_entry(lots=self.lots, now=ts)
        if not decision:
            log.warning("entry blocked by risk: %s", decision.reason)
            self.phase = "DONE"
            return
        priced = {}
        for right in ("CE", "PE"):
            inst = self.instruments[right]
            px = self.last_price.get(inst.token)
            if px is None or px <= 0:
                self.errors.append(f"no premium for {right}; aborting entry")
                self.phase = "DONE"
                return
            try:
                qty = self._qty_for(inst)
            except ValueError as e:
                # A wrong size is worse than no trade: sizing happens BEFORE
                # anything is sent, so the day simply does not open.
                self.errors.append(f"order size refused: {e}")
                log.error("%s", self.errors[-1])
                self.phase = "DONE"
                return
            priced[right] = px
            self._order_qty[right] = qty
        self.risk.record_entry()
        for right, px in priced.items():
            inst = self.instruments[right]
            self.exec_engine.sell_to_open(
                right=right, strike=inst.strike, qty=self._order_qty[right],
                ref_price=px, token=inst.token, symbol=inst.tradingsymbol,
                exchange=inst.exchange, ts=ts,
            )
            self.legs[right].arm(px)
        self.phase = "IN_POSITION"
        log.info("ENTERED straddle CE%d/PE%d x%s @ %s", atm, want_pe,
                 dict(self._order_qty), ts.strftime("%H:%M:%S"))

    # --- exits ----------------------------------------------------------------
    def _on_leg_bar(self, token: str, bar) -> None:
        right = next((r for r, i in self.instruments.items()
                      if i.token == token), None)
        leg = self.legs.get(right) if right else None
        if leg is None or not leg.is_open:
            return
        res = leg.on_bar(Bar(open=bar.open, high=bar.high, low=bar.low,
                             close=bar.close))
        if res:
            reason, fill = res
            inst = self.instruments[right]
            self.exec_engine.buy_to_close(
                right=right, strike=inst.strike,
                qty=self._order_qty.get(right) or self._qty_for(inst),
                ref_price=fill, reason=reason, token=inst.token,
                symbol=inst.tradingsymbol, exchange=inst.exchange, ts=self.now,
            )
            self._check_day_closed()

    def square_off(self, ts: dt.datetime) -> None:
        for right, leg in self.legs.items():
            if leg.is_open:
                inst = self.instruments[right]
                # Best available: last completed close, else last raw price.
                px = leg.last_close or self.last_price.get(inst.token) or 0.0
                # Reuse the quantity actually opened; never re-derive it here,
                # or a sizing hiccup could leave the position stuck open.
                self.exec_engine.buy_to_close(
                    right=right, strike=inst.strike,
                    qty=self._order_qty.get(right) or self._qty_for(inst),
                    ref_price=px, reason="SQUAREOFF", token=inst.token,
                    symbol=inst.tradingsymbol, exchange=inst.exchange, ts=ts,
                )
                leg.is_open = False
        self.phase = "DONE"

    def _check_day_closed(self) -> None:
        if self.legs and all(not l.is_open for l in self.legs.values()):
            self.phase = "DONE"

    # --- status -----------------------------------------------------------------
    def status(self) -> dict:
        return {
            "phase": self.phase,
            "date": str(self.date),
            "spot_ref": self.spot_ref,
            "lots": self.lots,
            "order_qty": dict(self._order_qty),
            "legs": {r: l.to_dict() for r, l in self.legs.items()},
            "instruments": {
                r: {"symbol": i.tradingsymbol, "token": i.token,
                    "exchange": i.exchange, "lot_size": i.lot_size}
                for r, i in self.instruments.items()
            },
            "errors": self.errors[-5:],
        }

