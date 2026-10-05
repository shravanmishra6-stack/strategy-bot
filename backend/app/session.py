"""Daily trading session: connect -> resolve live instruments -> run the straddle.

One process runs one weekday (Mon-Fri) end to end:

    1. refuse weekends outright (NIFTY has no weekend session)
    2. authenticate against Shoonya (hard-stop on SessionExpiredError)
    3. resolve the day's contracts through InstrumentResolver - spot index,
       nearest expiry, ATM CE/PE - so every token/exchange/lot comes from the
       broker, never from a guess
    4. drive LiveStraddle with ticks
    5. square off at params.squareoff_time

Market data rides the verified REST ``get_quotes`` path rather than the
websocket: the subscription payload has not been validated live yet, and a
mis-parsed tick would silently drive stop-loss decisions. Polling costs ~3
requests per loop against a 0.35s rate limiter, which is well inside the
broker's comfort zone.

Nothing here arms live trading by itself - that stays with the operator
(``arm_live_trading()``), and ``settings.paper_mode`` must be off first.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from app.broker.instruments import InstrumentResolver
from app.broker.noren_client import BrokerError, SessionExpiredError, client
from app.config import settings
from app.execution.engine import ExecutionEngine, OrderNotSubmitted
from app.journal import Journal
from app.risk.manager import RiskManager
from app.strategy.live import LiveStraddle, LegInstrument
from app.strategy.legs import SoldLeg
from app.strategy.params import StraddleParams

log = logging.getLogger("session")

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)


def _safe_journal(action, *args, **kwargs):
    """Journaling must never end a trading day.

    A report is worth having, but not at the cost of an unmanaged exception
    inside the poll loop - and the loop is what holds a live position's stops.
    Anything the journal cannot do is logged and skipped; the next successful
    write catches the file up, because every write rewrites the whole day.
    """
    try:
        return action(*args, **kwargs)
    except Exception as e:  # pragma: no cover - depends on the filesystem
        log.error("journal %s failed: %s", type(e).__name__, e)
        return None



class SessionState:
    """Thread-safe status blob the dashboard polls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list = []
        self._data: dict = {
            "status": "idle",
            "day": None,
            "live_mode": not settings.paper_mode,
            "live_armed": False,
            "lots": 1,
            "error": None,
            "resolved": None,
            "spot": None,
            "atm": None,
            "entry_deferred": False,
            "entry_deferred_reason": None,
            "broker_rejections": 0,
            "phase": None,
            "legs": {},
            "combined": {},
            "fills": [],
            "risk": {},
            "journal_path": None,
            "journal_writes": 0,
            "journal_failures": 0,
            "updated_at": None,
            "pnl_history": [],
        }

    def add_listener(self, cb) -> None:
        with self._lock:
            if cb not in self._listeners:
                self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def update(self, **kw) -> None:
        with self._lock:
            old_snap = dict(self._data)
            self._data.update(kw)
            self._data["updated_at"] = dt.datetime.now().isoformat(
                sep=" ", timespec="seconds")
            new_snap = dict(self._data)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(old_snap, new_snap, kw)
            except Exception as e:
                log.warning("SessionState listener error: %s", e)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


def _quote_price(res: InstrumentResolver, exchange: str, token: str):
    """Last traded price for one instrument via the verified quotes endpoint."""
    q = res.client.get_quotes(exchange, token) or {}
    if not isinstance(q, dict):
        return None
    # Crucial safety check: ensure the response matches the requested token and exchange.
    # A TCP/HTTP pipelining glitch must never accept an index spot quote for an option!
    resp_token = str(q.get("token", "")).strip()
    if resp_token and resp_token != str(token).strip():
        log.warning("get_quotes token mismatch: requested %s, got %s - ignoring", token, resp_token)
        return None
    resp_exch = str(q.get("exch", "")).strip().upper()
    if exchange and resp_exch and resp_exch != str(exchange).strip().upper():
        log.warning("get_quotes exch mismatch: requested %s, got %s - ignoring", exchange, resp_exch)
        return None

    uc = None
    try:
        if q.get("uc"):
            uc = float(q.get("uc"))
    except (TypeError, ValueError):
        pass

    for key in ("lp", "rt_price", "c"):
        try:
            val = q.get(key)
            if val is not None and float(val) > 0:
                price = float(val)
                # Option sanity check: F&O option premium cannot exceed its upper circuit or 2000 pts
                if exchange == res.exchange:
                    if uc is not None and price > uc:
                        log.warning("Price %.2f exceeds upper circuit %.2f for %s - ignoring", price, uc, token)
                        continue
                    if price > 2000.0:
                        log.warning("Absurd option price %.2f for %s - ignoring", price, token)
                        continue
                return price
        except (TypeError, ValueError):
            continue
    return None


def resolve_day(res: InstrumentResolver, today: dt.date) -> dict:
    """Spot + nearest expiry + ATM CE/PE legs, all broker-confirmed.

    Raises rather than returning a half-built instrument set: the strategy
    refuses to trade a leg it cannot size, and an unresolvable day must not
    degrade into a guess.
    """
    spot = res.index_quote_price()
    if not spot:
        raise BrokerError("no spot quote on "
                          f"{res.index_exchange}/{res.index_token}")
    expiry = res.nearest_expiry(today)
    atm = int(round(spot / res.strike_step) * res.strike_step)
    legs = res.resolve(expiry=expiry, atm=atm, step=res.strike_step)
    lots = {lg.lot_size for lg in legs} - {None}
    if not lots:
        raise BrokerError("broker returned no lot size - refusing to size")
    return {"spot": spot, "expiry": expiry, "atm": atm, "legs": legs,
            "lot_size": max(lots)}
def _stamp(value) -> str:
    """Second-precision clock string for the journal's start/finish columns."""
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except AttributeError:
        return "" if value is None else str(value)


def run_day(*, live: bool = False, poll_seconds: float = 5.0,
            state: SessionState | None = None,
            stop_event: threading.Event | None = None,
            journal: Journal | None = None,
            today: dt.date | None = None,
            params: StraddleParams | None = None) -> dict:
    """Run one weekday session to completion. Returns the final status dict.

    ``live`` requests arming; it is still refused unless ``PAPER_MODE`` is off
    and the kill switch is up, because arm_live_trading() enforces both.
    ``today`` defaults to the real date and exists for tests/backfills - a day
    can only be replayed if it can be named.

    The Excel journal is built here, before anything can fail, and closed in
    ``finally``: a day that dies at 09:10 on an expired broker session is exactly
    the kind of day reporting has to explain, and fills made before a crash must
    already be on disk instead of only in RAM. ``journal`` is injectable so
    tests can point the day at a temp folder.
    """
    st = state or SessionState()
    stop = stop_event or threading.Event()
    today = today or dt.date.today()
    jr = journal or Journal(today)
    started = dt.datetime.now()
    # A journal pointed at a different day than the one being traded would write
    # today's fills into yesterday's file, so the session's date wins - unless
    # the caller already accumulated fills under its own date, which must not be
    # silently re-stamped.
    if not jr.fills:
        jr.day = today.isoformat()

    status = None
    try:
        status = _run_day(st=st, stop=stop, today=today, journal=jr,
                          live=live, poll_seconds=poll_seconds, params=params)
        return status
    finally:
        snapshot = st.snapshot()
        snapshot.setdefault("day", str(today))
        # A skipped weekend never reached connect(), so there is no session to
        # ask for an order book - asking anyway would log a broker error that
        # means nothing.
        path = _safe_journal(jr.finalize, snapshot, fetch_orders=(
                                 snapshot.get("status") != "skipped"),
                             started_at=_stamp(started),
                             finished_at=_stamp(dt.datetime.now()))
        if path:
            log.info("journal written: %s", path)
        elif jr.enabled:
            log.error("journal NOT written for %s (see errors above)", today)
        stats = {"journal_path": str(path) if path else None,
                 "journal_writes": jr.writes,
                 "journal_failures": jr.failures}
        st.update(**stats)
        # `return` above already captured the dict object, so the caller only
        # sees the journal stats if this same mapping is updated in place -
        # otherwise a run that finished cleanly could not report where its
        # workbook went.
        if isinstance(status, dict):
            status.update(stats)


def _run_day(*, st: SessionState, stop: threading.Event, today: dt.date,
             journal: Journal, live: bool, poll_seconds: float,
             params: StraddleParams | None = None) -> dict:
    """The session itself, once run_day has opened the journal."""
    # 1) weekend guard - kept here too so _run_day is safe on its own.
    if today.weekday() >= 5:
        st.update(status="skipped", day=str(today),
                  error=f"{today.strftime('%A')} is not a trading day")
        return st.snapshot()

    st.update(status="starting", day=str(today), error=None)
    # The broker client and the day's lot size are discovered later, but the
    # journal must exist NOW: every early return below is a day that produced
    # no fills, and "why did nothing trade" is a reporting question.
    journal.attach_day(client=client)
    _safe_journal(journal.record, status=st.snapshot(), force=True)

    # 2) authenticate.
    try:
        if not client.connect():
            raise BrokerError("connect() returned no session")
    except SessionExpiredError as e:
        st.update(status="session_expired", error=str(e))
        log.error("session expired: %s - re-run scripts.oauth_login", e)
        return st.snapshot()
    except BrokerError as e:
        st.update(status="connect_failed", error=str(e))
        return st.snapshot()

    if live:
        if client.arm_live_trading():
            log.warning("armed: orders will reach the broker")
        else:
            st.update(error="live arm refused - check PAPER_MODE / KILL_SWITCH")

    # 3) resolve the day's contracts from the broker.
    res = InstrumentResolver(client)
    try:
        day = resolve_day(res, today)
    except Exception as e:
        st.update(status="resolve_failed", error=str(e))
        log.error("instrument resolution failed: %s", e)
        return st.snapshot()

    legs: list[LegInstrument] = day["legs"]
    resolved = {
        "expiry": str(day["expiry"]),
        "atm": day["atm"],
        "spot": day["spot"],
        "lot_size": day["lot_size"],
        "legs": [{"right": l.right, "symbol": l.tradingsymbol,
                  "token": l.token, "strike": l.strike,
                  "exchange": l.exchange, "lot_size": l.lot_size,
                  "order_qty": None} for l in legs],  # set after params
    }
    st.update(resolved=resolved, spot=day["spot"], status="resolved",
              live_armed=client.live_armed)
    log.info("resolved %s ATM %s: %s", day["expiry"], day["atm"],
             [(l.tradingsymbol, l.token) for l in legs])

    # 4) assemble the strategy exactly as the backtest does.
    # Dashboard runtime overrides (lots, SL%, straddle/strangle) win over .env.
    if params is None:
        try:
            from app.api import runtime_overrides
        except ImportError:
            runtime_overrides = {}
        _lots = runtime_overrides.get("lots", 1)
        # Parse optional HH:MM time overrides
        def _parse_time(key, default):
            raw = runtime_overrides.get(key)
            if raw:
                try:
                    hh, mm = raw.strip().split(":")
                    return dt.time(int(hh), int(mm))
                except (ValueError, AttributeError):
                    pass
            return default
        params = StraddleParams(
            lots=_lots,
            sl_pct=runtime_overrides.get("sl_pct", settings.strategy_sl_pct),
            combined_sl_pct=runtime_overrides.get(
                "combined_sl_pct", settings.strategy_combined_sl_pct),
            sl_to_cost=runtime_overrides.get(
                "sl_to_cost", settings.strategy_sl_to_cost),
            target_pct=runtime_overrides.get(
                "target_pct", settings.strategy_target_pct),
            strike_offset=runtime_overrides.get("strike_offset", 0),
            entry_time=_parse_time("entry_time", dt.time(9, 20)),
            squareoff_time=_parse_time("squareoff_time", dt.time(15, 15)),
        )
    else:
        _lots = params.lots
    risk = RiskManager()
    # Register the trading day so daily_pnl_pts/entries_today/halted_reason are
    # anchored to it. Without this call the gate kept whatever it had picked up
    # earlier in the process lifetime, so a halted day could poison the next one
    # on a server that was never restarted.
    risk.on_new_day(today)
    engine = ExecutionEngine(client, paper_mode=settings.paper_mode)
    strat = LiveStraddle(params=params, exec_engine=engine, risk=risk,
                         date=today, lots=_lots)
    pairs = [(SoldLeg(right=inst.right, strike=inst.strike, token=inst.token,
                      sl_pct=params.sl_pct, target_pct=params.target_pct,
                      trail_pct=params.trail_pct), inst) for inst in legs]
    strat.configure(spot_token=res.index_token, legs=pairs)
    # Backfill resolved order_qty now that lots is known
    for rleg in resolved["legs"]:
        rleg["order_qty"] = _lots * (rleg.get("lot_size") or 0)
    st.update(phase=strat.phase, risk=risk.status(),
              combined=strat.status().get("combined", {}),
              lots=_lots, resolved=resolved)

    # The journal now knows everything needed to price a round trip: the live
    # fill list (read straight off the engine, so a fill can never be missed)
    # and the broker-confirmed lot size, which converts points to rupees.
    journal.attach_day(engine=engine, lot_size=day["lot_size"], lots=_lots,
                       params={**params.to_dict(),
                               "ce_symbol": next((l.tradingsymbol for l in legs
                                                  if l.right == "CE"), None),
                               "pe_symbol": next((l.tradingsymbol for l in legs
                                                  if l.right == "PE"), None)})
    _safe_journal(journal.record, status=st.snapshot(), force=True)

    # 5) poll verified quotes and feed the strategy until the day closes.
    while not stop.is_set():
        now = dt.datetime.now()
        if now.time() > MARKET_CLOSE or strat.phase == "DONE":
            break
        if now.time() < MARKET_OPEN:
            st.update(status="awaiting_open")
            stop.wait(15.0)
            continue
        st.update(status="running")
        try:
            # Legs FIRST, spot last. _on_spot() fires the entry attempt as soon
            # as the clock passes entry_time, so feeding spot first meant the
            # very first loop at/after 09:20 always ran the entry gate with no
            # option premium cached (the "no premium ... aborting entry" bug).
            # LegInstrument carries its own exchange, so each leg is priced
            # independently rather than all legs sharing one exchange.
            for inst in legs:
                px = _quote_price(res, inst.exchange, inst.token)
                if px:
                    strat.on_tick(now, inst.token, px)
            spot = _quote_price(res, res.index_exchange, res.index_token)
            if spot:
                strat.on_tick(now, res.index_token, spot)
        except SessionExpiredError as e:
            st.update(status="session_expired", error=str(e),
                      phase=strat.phase)
            log.error("session expired mid-session: %s", e)
            return st.snapshot()
        except OrderNotSubmitted as e:
            # Policy refusal (not armed / incomplete instrument). Paper mode
            # still records the intent; surface it rather than swallow it.
            st.update(error=str(e))
            log.warning("order refused: %s", e)
        except BrokerError as e:
            st.update(error=str(e))
            log.warning("quote poll failed: %s", e)

        if strat.params.combined_sl_pct is not None and getattr(strat, "combined_entry", None):
            comb_pts = round(strat.combined_entry * float(strat.params.combined_sl_pct), 2)
            strat.combined_sl_level = round(strat.combined_entry + comb_pts, 2)

        st.update(phase=strat.phase,
                  spot=spot or strat.spot_ref or day["spot"],
                  spot_entry=strat.spot_ref or day["spot"],
                  atm=strat.status()["atm"],
                  entry_deferred=strat.entry_deferred,
                  entry_deferred_reason=strat.entry_deferred_reason,
                  broker_rejections=strat.broker_rejections,
                  legs={r: l.to_dict() for r, l in strat.legs.items()},
                  combined=strat.status().get("combined", {}),
                  fills=[f.to_dict() for f in engine.fills],
                  risk=risk.status(), errors=list(strat.errors))
        # Journal every poll. record() only touches the disk when the fill
        # count moved or the refresh interval passed, so this costs almost
        # nothing - but it means a fill is never stranded in RAM if the
        # process dies, and the day's file always explains itself.
        _safe_journal(journal.record, status=st.snapshot())
        try:
            engine.reconcile()  # clear anything ambiguous from a hiccup
        except Exception:  # pragma: no cover - book read must not kill the day
            pass
        stop.wait(poll_seconds)

    # 6) never leave a position open: force square-off on any exit path.
    if strat.phase == "IN_POSITION":
        try:
            strat.square_off(dt.datetime.now())
        except Exception as e:  # pragma: no cover
            log.error("square-off failed: %s", e)
            st.update(error=f"square-off failed: {e}")

    st.update(status="done", phase=strat.phase,
              legs={r: l.to_dict() for r, l in strat.legs.items()},
              combined=strat.status().get("combined", {}),
              fills=[f.to_dict() for f in engine.fills],
              risk=risk.status())
    log.info("session finished: phase=%s fills=%d", strat.phase,
             len(engine.fills))
    return st.snapshot()

