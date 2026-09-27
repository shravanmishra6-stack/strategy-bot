"""
Defensive wrapper around Shoonya's Noren API (NorenRestApiOAuth).

Design rules:
  * Every response goes through `_unwrap`: Shoonya signals errors with
    {"stat": "Not_Ok", "emsg": ...} *and* sometimes returns Python None.
  * "Session Expired" raises SessionExpiredError -> callers alert and STOP;
    we never blindly retry an order (it may already exist).
  * place_order is refused unless live trading is explicitly armed
    (arm_live_trading() called by the operator) AND PAPER_MODE=false AND the
    kill switch is off. Belt and braces.
  * A simple rate limiter guards against ban-hammering (Shoonya limits are low;
    ~3 req/s sustained is considered safe).
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings

log = logging.getLogger("broker.noren")

SESSION_EXPIRED_MARKERS = ("session expired", "invalid session", "not logged in")


class BrokerError(RuntimeError):
    """Any Not_Ok / transport failure from the broker."""

    def __init__(self, message: str, *, emsg: str | None = None):
        super().__init__(message)
        self.emsg = emsg


class SessionExpiredError(BrokerError):
    """Auth died. Human re-login required; never auto-retry orders on this."""


class PaperMode(RuntimeError):
    """Order refused because live trading is not armed - engine simulates fills."""


class RateLimiter:
    """Minimum interval between outbound requests (thread safe)."""

    def __init__(self, min_interval: float = 0.35):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


def _unwrap(resp, *, context: str):
    """Normalise Noren responses: dict/list/None -> value or raise."""
    if resp is None:
        raise BrokerError(f"{context}: empty response (check logs/session)")
    if isinstance(resp, dict):
        stat = str(resp.get("stat", "")).lower()
        emsg = str(resp.get("emsg", "")) or None
        if stat and stat != "ok":
            if any(m in (emsg or "").lower() for m in SESSION_EXPIRED_MARKERS):
                raise SessionExpiredError(f"{context}: {emsg}", emsg=emsg)
            if "no data" in (emsg or "").lower():
                return None
            raise BrokerError(f"{context}: {emsg or 'Not_Ok'}", emsg=emsg)
        return resp
    if isinstance(resp, list):
        if (
            len(resp) == 1
            and isinstance(resp[0], dict)
            and str(resp[0].get("stat", "")).lower() not in ("", "ok")
        ):
            return _unwrap(resp[0], context=context)


class ShoonyaClient:
    """Single shared broker client; construct once at app startup."""

    def __init__(self, cfg=settings):
        self.cfg = cfg
        self._api = None
        self._limiter = RateLimiter()
        self._lock = threading.RLock()
        self._session_ok = False
        # Set True only via the dashboard's explicit operator action.
        self._live_armed = False

    # --- connection --------------------------------------------------------
    def connect(self) -> bool:
        """Establish the OAuth session and validate it. Idempotent."""
        if not self.cfg.has_credentials:
            log.warning("No Shoonya credentials configured; broker stays offline.")
            self._session_ok = False
            return False
        try:
            from NorenRestApiPy.NorenApi import NorenApi
        except ImportError as e:  # pragma: no cover
            raise BrokerError(f"NorenRestApiOAuth not installed: {e}")

        with self._lock:
            api = NorenApi(host=self.cfg.host, websocket=self.cfg.websocket)
            try:
                api.injectOAuthHeader(
                    self.cfg.access_token, self.cfg.uid, self.cfg.account_id
                )
                # Cheap authenticated probe to validate the token before use.
                limits = _unwrap(api.get_limits(), context="connect/get_limits")
                self._api = api
                self._session_ok = bool(limits)
                log.info("Shoonya session established (uid=%s)", self.cfg.uid)
                return True
            except SessionExpiredError:
                self._session_ok = False
                log.error("Shoonya access token EXPIRED - re-run the OAuth flow.")
                raise
            except Exception as e:
                self._session_ok = False
                log.error("Shoonya connect failed: %s", e)
                raise BrokerError(f"connect failed: {e}") from e

    @property
    def session_ok(self) -> bool:
        return self._session_ok and self._api is not None

    @property
    def live_armed(self) -> bool:
        return bool(
            self._live_armed and not self.cfg.paper_mode and not self.cfg.kill_switch
        )

    def arm_live_trading(self) -> bool:
        if self.cfg.paper_mode:
            log.warning("arm_live_trading ignored: PAPER_MODE is on.")
            return False
        if self.cfg.kill_switch:
            log.warning("arm_live_trading ignored: KILL_SWITCH is on.")
            return False
        self._live_armed = True
        log.warning("LIVE TRADING ARMED - orders will reach the broker!")
        return True

    def disarm_live_trading(self) -> None:
        self._live_armed = False
        log.info("Live trading disarmed (paper only).")

    # --- generic call wrapper ----------------------------------------------
    def _call(self, method_name: str, *args, retries: int = 0, **kwargs):
        if not self.session_ok:
            raise BrokerError("Broker session not established")
        func = getattr(self._api, method_name)
        last_err = None
        for attempt in range(retries + 1):
            self._limiter.wait()
            try:
                return _unwrap(func(*args, **kwargs), context=method_name)
            except SessionExpiredError:
                self._session_ok = False  # never retry an auth-dead session
                raise
            except BrokerError as e:
                last_err = e
                # Never blindly retry place_order: a timeout may still have
                # created the order. The engine reconciles via order book.
                if method_name == "place_order":
                    raise
                if attempt < retries:
                    time.sleep(0.5 * (2**attempt))
        raise last_err  # pragma: no cover - loop always returns or raises

    # --- account / books (read-only) ----------------------------------------
    def get_limits(self):
        return self._call("get_limits", retries=2)

    def get_positions(self):
        return self._call("get_positions", retries=1) or []

    def get_order_book(self):
        return self._call("get_order_book", retries=1) or []

    def get_trade_book(self):
        return self._call("get_trade_book", retries=1) or []

    def get_holdings(self):
        return self._call("get_holdings", retries=1) or []

    # --- market data --------------------------------------------------------
    def searchscrip(self, exchange: str, searchtext: str):
        return self._call("searchscrip", exchange, searchtext, retries=2)

    def get_security_info(self, exchange: str, token: str):
        return self._call("get_security_info", exchange, token, retries=2)

    def get_quotes(self, exchange: str, token: str):
        return self._call("get_quotes", exchange, token, retries=2)

    def get_option_chain(self, exchange: str, tradingsymbol: str,
                         strikeprice: float, count: int = 5):
        return self._call(
            "get_option_chain", exchange, tradingsymbol, strikeprice, count,
            retries=2,
        )

    def get_time_price_series(self, exchange: str, token: str, starttime=None,
                              endtime=None, interval=None):
        return self._call(
            "get_time_price_series", exchange, token, starttime=starttime,
            endtime=endtime, interval=interval, retries=2,
        ) or []

    # --- trading -------------------------------------------------------------
    def place_order(self, *, buy_or_sell: str, product_type: str, exchange: str,
                    tradingsymbol: str, quantity: int, price_type: str = "LMT",
                    price: float = 0.0, trigger_price: float | None = None,
                    discloseqty: int = 0, retention: str = "DAY",
                    amo: str = "NO", remarks: str = "bot"):
        """Send an order. Refused (PaperMode) unless live trading is armed."""
        if not self.live_armed:
            raise PaperMode(
                f"REFUSED live order {buy_or_sell} {tradingsymbol} x{quantity}: "
                f"paper={self.cfg.paper_mode} armed={self._live_armed} "
                f"kill={self.cfg.kill_switch}"
            )
        log.warning(
            "LIVE ORDER -> %s %s %s x%d @%s (%s)", buy_or_sell, exchange,
            tradingsymbol, quantity, price, price_type,
        )
        return self._call(
            "place_order",
            buy_or_sell=buy_or_sell, product_type=product_type, exchange=exchange,
            tradingsymbol=tradingsymbol, quantity=quantity, discloseqty=discloseqty,
            price_type=price_type, price=price, trigger_price=trigger_price,
            retention=retention, amo=amo, remarks=remarks,
        )

    def modify_order(self, **kwargs):
        if not self.live_armed:
            raise PaperMode("REFUSED modify_order: not armed for live trading")
        return self._call("modify_order", **kwargs)

    def cancel_order(self, orderno: str):
        if not self.live_armed:
            raise PaperMode("REFUSED cancel_order: not armed for live trading")
        return self._call("cancel_order", orderno)

    # --- websocket -----------------------------------------------------------
    def start_websocket(self, *, on_quote, on_order_update, on_open=None,
                        on_close=None):
        """Start the Noren WS thread. Requires a valid session + credentials."""
        if not self.session_ok:
            raise BrokerError("Cannot start websocket: session not established")
        self._api.set_credentials(
            self.cfg.access_token, self.cfg.uid, self.cfg.account_id
        )
        return self._api.start_websocket(
            subscribe_callback=on_quote,
            order_update_callback=on_order_update,
            socket_open_callback=on_open,
            socket_close_callback=on_close,
        )

    def subscribe(self, instruments):
        if not self.session_ok:
            raise BrokerError("Cannot subscribe: session not established")
        return self._api.subscribe(instruments)

    def unsubscribe(self, instruments):
        if not self.session_ok:
            raise BrokerError("Cannot unsubscribe: session not established")
        return self._api.unsubscribe(instruments)

    def subscribe_orders(self):
        if not self.session_ok:
            raise BrokerError("Cannot subscribe orders: session not established")
        return self._api.subscribe_orders()

    def logout(self):
        if self._api is None:
            return None
        try:
            return self._api.logout()
        finally:
            self._session_ok = False
            self._live_armed = False


# One client instance for the whole process.
client = ShoonyaClient()
