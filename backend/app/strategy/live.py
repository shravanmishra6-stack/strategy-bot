"""Live straddle state machine - same rules as the backtest, event-driven.

Parity contract with app/backtest/engine.py:
  * entry ATM  = round(first spot tick at/after entry_time / step) * step. The
                 strike is PINNED by that first tick: a retry after a deferral
                 prices the same strike, so the strike check and the risk gate
                 cannot churn between polls.
  * leg entry  = last premium seen at/after entry_time (backtest uses the
                 option bar's open at that minute; live cannot see the future
                 open, so latest-premium + slippage assumption is the honest
                 equivalent)
  * exits      = SoldLeg.on_bar on COMPLETED 1-min option bars (identical to
                 the backtest - same SoldLeg code path)
  * square-off = buy-back of whatever is still open at squareoff_time

A day started at/after entry_time is supported: the first poll caches the leg
premiums and the entry fires on the same loop. An entry whose order is refused
(the operator has not armed yet) parks in WAIT_ENTRY and retries every tick
until the risk gate's no_new_entry_after cutoff, so arming late still trades.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from app.data.bars import MinuteBarBuilder
from app.config import settings
from app.execution.engine import OrderNotSubmitted, PermanentOrderRefusal
from app.strategy.legs import Bar, COMBINED_SL, SL, SoldLeg
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
    combined_entry: float | None = None
    combined_sl_level: float | None = None
    _spot_token: str | None = None
    _order_qty: dict = field(default_factory=dict)   # right -> validated qty
    # Set when entry was attempted while a leg premium had not been seen yet, or
    # when the order itself was refused and can safely be retried. Keeps the
    # day in WAIT_ENTRY instead of ending it; the flag also gates the retry so
    # an entry can never be committed twice.
    entry_deferred: bool = False
    # WHY the entry is parked, so the dashboard can stop claiming "waiting for
    # premiums" when the real cause is a refused/rejected submit. Short label
    # only; the full detail stays in `errors`.
    entry_deferred_reason: str | None = None
    entry_recorded: bool = False                     # risk booked exactly once
    broker_rejections: int = 0                       # non-policy submit refusals
    # Realized points already reported to the risk gate, one entry per leg, so a
    # leg can never be counted twice (SL exit + a later square-off sweep).
    _closed_legs: set = field(default_factory=set)
    _atm_pin: int | None = None                      # ATM from first post-entry spot
    # Conditions already surfaced in `errors`, so a retrying entry reports each
    # one once instead of flooding the dashboard every poll. Holds leg rights
    # ("CE"/"PE") and the pseudo-key "submit".
    _waiting: set = field(default_factory=set)
    # Broker-side SL-LMT order IDs keyed by right ("CE"/"PE").
    # Placed immediately after entry so the exchange enforces the stop;
    # cancelled/modified as position state changes.
    _broker_sl_orders: dict = field(default_factory=dict)
    # Flag: broker SL orders have already been placed for this entry.
    _broker_sl_placed: bool = False

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
            if self._check_combined_sl(ts):
                return
            completed = self.builders[token].on_tick(ts, price)
            if completed:
                self._on_leg_bar(token, completed)
        elif token in self.builders:
            self.builders[token].on_tick(ts, price)  # keep bars warm pre-entry
            # A deferred entry completes as soon as the missing leg premium
            # shows up. Without this the day dies on the first tick whenever
            # the session starts at/after entry_time.
            if self.entry_deferred and self.phase == "WAIT_ENTRY":
                self._try_enter(ts)
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
        if self._atm_pin is None:
            self._atm_pin = int(
                round(price / self.params.strike_step) * self.params.strike_step)
        self._try_enter(ts)

    def _try_enter(self, ts: dt.datetime) -> None:
        atm = self._atm_pin
        if atm is None:
            return  # no post-entry spot yet - nothing to price against
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
        armed = True
        for right in ("CE", "PE"):
            inst = self.instruments[right]
            px = self.last_price.get(inst.token)
            if px is None or px <= 0:
                # The poll loop prices the legs before the index, but the very
                # first loop of a session started at/after entry_time can still
                # race a missing/zero quote. Defer and retry on the next tick -
                # treating it as fatal here is what killed late starts.
                self.entry_deferred = True
                self.entry_deferred_reason = "quote"
                armed = False
                if right not in self._waiting:
                    self._waiting.add(right)
                    self.errors.append(
                        f"no premium for {right} yet; entry deferred")
                    log.warning("entry deferred: no %s premium yet at %s",
                                right, ts.strftime("%H:%M:%S"))
                continue
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
        if not armed:
            return  # still WAIT_ENTRY; the leg-tick retry will finish the job
        # Probe the FIRST order before booking or trusting the second: a policy
        # refusal (client not armed - e.g. the operator arms after 09:20) must
        # leave the day recoverable instead of half-opened. Nothing is recorded
        # in `priced` that has not actually been sent, so the retry is the same
        # straddle, only later.
        first_right = "CE"
        try:
            self._send_leg(first_right, priced[first_right], ts)
        except PermanentOrderRefusal as e:
            # Nothing was sent and nothing ever will be (a forbidden order
            # type, an unresolvable instrument). Retrying would loop forever
            # without the broker ever being asked, so end the day instead of
            # parking it in WAIT_ENTRY like the late-arm case below does.
            self.errors.append(f"entry permanently refused: {e}")
            log.error("%s", self.errors[-1])
            self.entry_deferred = False
            self.entry_deferred_reason = None
            self.phase = "DONE"
            return
        except OrderNotSubmitted as e:
            # Retryable policy refusal with nothing sent (client not armed
            # - e.g. the operator arms after 09:20). Keep the day alive and
            # retry: arming late must still trade.
            self._defer("submit", f"entry not sent ({e}); will retry")
            return
        except (RuntimeError, ValueError) as e:
            # A broker-side rejection: no order exists, but retrying forever
            # would hammer the broker every poll, so give up after a few tries
            # and let the normal square-off close the day.
            self.broker_rejections += 1
            limit = int(getattr(settings, "max_broker_entry_retries", 5))
            if self.broker_rejections >= limit:
                self.errors.append(
                    f"entry rejected by broker {self.broker_rejections}x ({e})"
                    f" - abandoning entry")
                log.error("%s", self.errors[-1])
                self.entry_deferred = False
                self.entry_deferred_reason = None
                self.phase = "DONE"
                return
            self._defer("submit",
                        f"entry rejected by broker ({e}); retry "
                        f"{self.broker_rejections}/{limit}", reason="rejected")
            return
        if not self.entry_recorded:
            self.risk.record_entry()
            self.entry_recorded = True
        for right in ("CE", "PE"):
            if right == first_right:
                continue
            px = priced[right]
            try:
                self._send_leg(right, px, ts)
            except Exception as e:
                # The first leg is already live, so the straddle is incomplete -
                # unwind rather than hold a naked short leg.
                log.error("second leg %s failed (%s) - unwinding %s",
                          right, e, first_right)
                self.errors.append(f"second leg {right} failed: {e}")
                try:
                    first_inst = self.instruments[first_right]
                    self.exec_engine.buy_to_close(
                        right=first_right, strike=first_inst.strike,
                        qty=self._order_qty[first_right],
                        ref_price=self.legs[first_right].entry or px,
                        reason="ENTRY_UNWIND", token=first_inst.token,
                        symbol=first_inst.tradingsymbol,
                        exchange=first_inst.exchange, ts=ts)
                except Exception as un:
                    log.error("unwind of %s also failed: %s", first_right, un)
                    self.errors.append(f"unwind failed: {un}")
                # Mirror the square-off path: the buy-back closed the leg, so
                # the local state must not keep claiming it is open.
                self.legs[first_right].is_open = False
                # Book the unwind too: it was bought back at its own entry, so
                # it contributes only costs. Counting it keeps the day's points
                # honest instead of silently dropping a completed round trip.
                self._report_leg_close(
                    first_right, self.legs[first_right].entry or px)
                if not self.entry_recorded:
                    self.risk.record_entry()
                    self.entry_recorded = True
                self.phase = "DONE"
                return
        self.entry_deferred = False
        self.entry_deferred_reason = None
        self._waiting.clear()
        self.phase = "IN_POSITION"
        ce_leg, pe_leg = self.legs.get("CE"), self.legs.get("PE")
        if ce_leg and pe_leg and ce_leg.entry is not None and pe_leg.entry is not None:
            self.combined_entry = round(float(ce_leg.entry + pe_leg.entry), 4)
            if self.params.combined_sl_pct is not None:
                comb_pts = round(self.combined_entry * float(self.params.combined_sl_pct), 2)
                self.combined_sl_level = round(self.combined_entry + comb_pts, 2)
        log.info("ENTERED straddle CE%d/PE%d x%s @ %s (combined entry=%.2f, combined SL level=%s)",
                 atm, want_pe, dict(self._order_qty), ts.strftime("%H:%M:%S"),
                 self.combined_entry or 0.0,
                 f"{self.combined_sl_level:.2f}" if self.combined_sl_level else "none")
        # Stop-loss is enforced in memory by default. Broker SL orders are only placed
        # if broker_sl_enabled is True.
        if getattr(settings, "broker_sl_enabled", False):
            self._place_broker_sl_orders()
        else:
            log.info("SL kept in memory only (not placed to broker) for %s",
                     f"combined SL {self.combined_sl_level:.2f}" if self.combined_sl_level else "legs")

    def _send_leg(self, right: str, px: float, ts: dt.datetime) -> None:
        """Submit one leg's opening sell and arm its exit rules."""
        inst = self.instruments[right]
        self.exec_engine.sell_to_open(
            right=right, strike=inst.strike, qty=self._order_qty[right],
            ref_price=px, token=inst.token, symbol=inst.tradingsymbol,
            exchange=inst.exchange, ts=ts,
        )
        self.legs[right].arm(px)

    def _place_broker_sl_orders(self) -> None:
        """Place SL-LMT BUY orders at the broker for every open leg.

        By default, SL is kept strictly in-memory and not sent to broker.
        """
        if not getattr(settings, "broker_sl_enabled", False):
            log.info("SL kept in memory only; skipping broker SL placement")
            return
        if self._broker_sl_placed:
            return
        sl_level = self.combined_sl_level
        if sl_level is None:
            log.info("No combined SL level set; skipping broker SL orders")
            return
        placed_any = False
        for right, leg in self.legs.items():
            if not leg.is_open or leg.entry is None:
                continue
            inst = self.instruments.get(right)
            if inst is None:
                continue
            qty = self._order_qty.get(right) or self._qty_for(inst)
            try:
                orderno = self.exec_engine.place_broker_sl(
                    right=right,
                    strike=inst.strike,
                    qty=qty,
                    sl_trigger=sl_level,
                    token=inst.token,
                    symbol=inst.tradingsymbol,
                    exchange=inst.exchange,
                )
                if orderno:
                    self._broker_sl_orders[right] = orderno
                    log.info("Broker SL order placed for %s: orderno=%s trigger=%.2f",
                             right, orderno, sl_level)
                placed_any = True
            except Exception as e:
                log.error("Failed to place broker SL for %s: %s", right, e)
        if placed_any:
            self._broker_sl_placed = True

    def _cancel_broker_sl(self, right: str) -> None:
        """Cancel broker SL order for one leg (called when leg exits by any path)."""
        orderno = self._broker_sl_orders.pop(right, None)
        if orderno and getattr(settings, "broker_sl_enabled", False):
            inst = self.instruments.get(right)
            self.exec_engine.cancel_broker_sl(
                orderno, symbol=inst.tradingsymbol if inst else "")

    def _modify_broker_sl_to_cost(self, right: str, cost: float) -> None:
        """Modify surviving leg's broker SL order to cost price (SL-to-cost)."""
        if not getattr(settings, "broker_sl_enabled", False):
            return
        orderno = self._broker_sl_orders.get(right)
        if not orderno:
            return
        inst = self.instruments.get(right)
        qty = self._order_qty.get(right) or (self._qty_for(inst) if inst else 0)
        self.exec_engine.modify_broker_sl(
            orderno=orderno,
            right=right,
            qty=qty,
            new_trigger=cost,
            symbol=inst.tradingsymbol if inst else None,
            exchange=inst.exchange if inst else None,
        )
        log.info("Broker SL for %s modified to cost %.2f (orderno=%s)",
                 right, cost, orderno)

    def _report_leg_close(self, right: str, exit_px: float) -> None:
        """Feed one closed leg's realized points to the risk gate.

        Without this the daily loss limit was unreachable in production:
        `RiskManager.on_trade_close()` was only ever called from tests, so
        `daily_pnl_pts` stayed at 0.0, `halted_reason` never fired, and no
        configured `RISK_MAX_DAILY_LOSS_PTS` could stop anything.

        Mirrors the backtest exactly (app/backtest/engine.py:128):
            net = (entry - exit) - slippage - brokerage / lot
        so the live gate compares against the same number the sweep tuned.
        Idempotent per leg, because a square-off sweep can revisit a leg that
        already exited on its stop.
        """
        if self.risk is None or right in self._closed_legs:
            return
        try:
            exit_px = float(exit_px)
        except (TypeError, ValueError):
            return
        if exit_px <= 0:
            # square_off falls back to 0.0 when it has no price. Trusting that
            # would book entry-0 as a large PROFIT and keep the bot trading on a
            # day that may actually be losing. Leave the leg unreported instead.
            return
        leg = self.legs.get(right)
        inst = self.instruments.get(right)
        if leg is None or leg.entry is None:
            return
        lot = int(getattr(inst, "lot_size", 0) or 0) if inst else 0
        p = self.params
        net = (leg.entry - float(exit_px)) - p.slippage
        if lot > 0:
            net -= p.brokerage / lot
        self._closed_legs.add(right)
        try:
            self.risk.on_trade_close(net)
        except AttributeError:
            # Test doubles and older risk stubs may not implement the hook;
            # never let bookkeeping break an open position's exit.
            pass

    def _defer(self, key: str, message: str, reason: str = "submit") -> None:
        """Park the entry, keeping WAIT_ENTRY so the next tick retries.

        `reason` distinguishes a missing quote ("quote") from a refused or
        rejected order ("submit") so the UI never misattributes one to the
        other - the dashboard said "waiting for premiums" for a day whose real
        problem was a broker rejection.
        """
        self.entry_deferred = True
        self.entry_deferred_reason = reason
        if key not in self._waiting:
            self._waiting.add(key)
            self.errors.append(message)
            log.warning("%s", message)

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
            leg.last_close = fill   # stamp exact exit fill so PnL is correct
            inst = self.instruments[right]
            self.exec_engine.buy_to_close(
                right=right, strike=inst.strike,
                qty=self._order_qty.get(right) or self._qty_for(inst),
                ref_price=fill, reason=reason, token=inst.token,
                symbol=inst.tradingsymbol, exchange=inst.exchange, ts=self.now,
            )
            # Cancel the broker SL order — leg already exited via bar-SL
            self._cancel_broker_sl(right)
            self._report_leg_close(right, fill)
            if reason in (SL, COMBINED_SL) and self.params.sl_to_cost:
                self._move_surviving_legs_to_cost()
            # 0DTE safeguard: if the leg that just closed pushed the day past
            # the daily loss cap, the *other* leg must not be allowed to run on
            # until square-off time. On expiry day a short premium can grow
            # without bound, so a halted day means flatten now, not at 15:15.
            if self.risk is not None and getattr(self.risk, "halted_reason", None) \
                    and any(l.is_open for l in self.legs.values()):
                log.error("daily loss cap hit (%s) - flattening open leg(s) now",
                          self.risk.halted_reason)
                self.square_off(self.now or dt.datetime.now())
                return
            self._check_day_closed()

    def _move_surviving_legs_to_cost(self) -> None:
        """When one leg hits SL, move remaining open leg(s) SL to their entry price (cost)."""
        self._sl_to_cost_active = True
        for r, l in self.legs.items():
            if l.is_open and l.entry is not None:
                l.sl_level = round(float(l.entry), 2)
                l.trail_sl = None
                log.info("One leg hit SL -> Moved surviving %s SL to cost (entry price): %.2f", r, l.sl_level)
                # Also modify the broker-side SL order to the new cost trigger
                self._modify_broker_sl_to_cost(r, l.sl_level)

    def _check_combined_sl(self, ts: dt.datetime) -> bool:
        if self.phase != "IN_POSITION" or self.params.combined_sl_pct is None:
            return False
        comb_entry = getattr(self, "combined_entry", None)
        if comb_entry is None:
            return False
        comb_pts = round(comb_entry * float(self.params.combined_sl_pct), 2)
        comb_sl = round(comb_entry + comb_pts, 2)
        self.combined_sl_level = comb_sl

        # 1) Track Total Premium across all legs (open live price + closed leg exit fill)
        open_legs = [l for l in self.legs.values() if l.is_open]
        if not open_legs:
            return False

        total_curr = 0.0
        for right, leg in self.legs.items():
            if leg.is_open:
                inst = self.instruments.get(right)
                px = (self.last_price.get(inst.token) if inst else None) or leg.last_close or leg.entry
                if px is None or px <= 0:
                    return False
                # Sanity guard: an option premium cannot be an index spot quote (> 2000 pts or > 10x entry)
                if px > 2000.0 or (leg.entry and px > leg.entry * 10):
                    log.error("Rejecting corrupt %s price %.2f (entry %.2f) in SL check", right, px, leg.entry or 0)
                    return False
                total_curr += px
            else:
                total_curr += leg.last_close or leg.entry or 0.0

        total_curr = round(total_curr, 2)
        if total_curr >= comb_sl:
            log.warning(
                "COMBINED SL HIT: total premium %.2f >= SL %.2f (entry %.2f + %.2f pts SL)",
                total_curr, comb_sl, comb_entry, comb_pts
            )
            for right, leg in list(self.legs.items()):
                if leg.is_open:
                    inst = self.instruments[right]
                    px = (self.last_price.get(inst.token) if inst else None) or leg.last_close or leg.entry
                    self.exec_engine.buy_to_close(
                        right=right, strike=inst.strike,
                        qty=self._order_qty.get(right) or self._qty_for(inst),
                        ref_price=px, reason=COMBINED_SL, token=inst.token,
                        symbol=inst.tradingsymbol, exchange=inst.exchange, ts=ts,
                    )
                    leg.is_open = False
                    leg.last_close = px   # stamp exit fill so PnL is correct
                    self._cancel_broker_sl(right)
                    self._report_leg_close(right, px)
            self._check_day_closed()
            return True

        # 2) If an individual leg had exited earlier and SL-to-cost is active on surviving leg:
        if getattr(self, "_sl_to_cost_active", False):
            for right, leg in list(self.legs.items()):
                if not leg.is_open or leg.entry is None:
                    continue
                inst = self.instruments.get(right)
                px = (self.last_price.get(inst.token) if inst else None) or leg.last_close or leg.entry
                cost_stop = round(float(leg.entry), 2)
                # Must actually rise above cost (with 0.05 tick margin to avoid firing at entry tick)
                if px is not None and px > cost_stop + 0.05:
                    log.warning("SURVIVING LEG SL-TO-COST HIT: %s price %.2f > cost %.2f", right, px, cost_stop)
                    self.exec_engine.buy_to_close(
                        right=right, strike=inst.strike,
                        qty=self._order_qty.get(right) or self._qty_for(inst),
                        ref_price=px, reason=SL, token=inst.token,
                        symbol=inst.tradingsymbol, exchange=inst.exchange, ts=ts,
                    )
                    leg.is_open = False
                    leg.last_close = px
                    self._cancel_broker_sl(right)
                    self._report_leg_close(right, px)
                    self._check_day_closed()
                    return True

        return False

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
                leg.last_close = px   # stamp exit fill so PnL is correct
                # Cancel broker SL — we are squaring off manually
                self._cancel_broker_sl(right)
                self._report_leg_close(right, px)
        self.phase = "DONE"

    def _check_day_closed(self) -> None:
        if self.legs and all(not l.is_open for l in self.legs.values()):
            self.phase = "DONE"

    # --- status -----------------------------------------------------------------
    def status(self) -> dict:
        comb_entry = getattr(self, "combined_entry", None)
        comb_sl = getattr(self, "combined_sl_level", None)
        comb_sl_pts = (
            round(comb_entry * self.params.combined_sl_pct, 2)
            if comb_entry is not None and self.params.combined_sl_pct is not None
            else None
        )
        comb_curr = None
        # Include ALL legs (open + closed) so PnL reflects realized losses too.
        # For open legs: use latest live tick; for closed legs: last_close holds
        # the stamped exit fill price set at closure time.
        if self.legs:
            any_priced = any(
                l.entry is not None for l in self.legs.values()
            )
            if any_priced:
                total = 0.0
                for r, l in self.legs.items():
                    inst = self.instruments.get(r)
                    if l.is_open:
                        px = (self.last_price.get(inst.token) if inst else None) \
                             or l.last_close or l.entry or 0.0
                    else:
                        # Closed leg: last_close was stamped to exit fill at closure
                        px = l.last_close or l.entry or 0.0
                    total += px
                comb_curr = round(total, 2)
        return {
            "phase": self.phase,
            "date": str(self.date),
            "spot_ref": self.spot_ref,
            "atm": self._atm_pin,
            "lots": self.lots,
            "entry_deferred": self.entry_deferred,
            "entry_deferred_reason": self.entry_deferred_reason,
            "order_qty": dict(self._order_qty),
            "legs": {r: l.to_dict() for r, l in self.legs.items()},
            "combined": {
                "sl_pct": self.params.combined_sl_pct,
                "entry": comb_entry,
                "sl_pts": comb_sl_pts,
                "sl_level": comb_sl,
                "current": round(comb_curr, 2) if comb_curr is not None else None,
            },
            "instruments": {
                r: {"symbol": i.tradingsymbol, "token": i.token,
                    "exchange": i.exchange, "lot_size": i.lot_size}
                for r, i in self.instruments.items()
            },
            "broker_sl_orders": dict(self._broker_sl_orders),
            "errors": self.errors[-5:],
        }

