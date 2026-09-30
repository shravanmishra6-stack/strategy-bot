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


# Shoonya refuses market orders for API-originated orders ("ALGO_CHK: MKT Order
# type not allowed for API order"). Phone/MOB orders are exempt, so this only
# ever bites the bot - and it bites silently, because the vendored SDK throws the
# broker's `emsg` away and leaves a bare `None` behind. Every live order must be
# a LIMIT priced through the spread; asking for MKT is a guaranteed rejection,
# never a fill attempt.
ALLOWED_LIVE_PRICE_TYPE = "LMT"
REJECTED_LIVE_PRICE_TYPES = ("MKT", "SL-M", "SLM")
# SL-LMT is accepted by Shoonya for API orders (trigger_price + limit_price)
ALLOWED_SL_PRICE_TYPE = "SL-LMT"


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
    """Normalise Noren responses: dict/list/None -> value or raise.

    NB: the vendored SDK converts any ``stat != Ok`` (dict) response into a bare
    ``None`` for its list-flavoured getters, so a ``None`` here usually means the
    broker rejected the call (bad segment/args, empty book, or a dead session)
    and we cannot see the ``emsg``. Callers that treat an empty list as valid
    pass ``empty_on_none`` on the wrapper method and enable DEBUG logging on the
    ``NorenRestApiPy.NorenApi`` logger to surface the real reason.
    """
    if resp is None:
        raise BrokerError(
            f"{context}: broker returned nothing (Not_Ok / empty). "
            f"Check the segment + arguments; enable DEBUG on "
            f"'NorenRestApiPy.NorenApi' to see the broker emsg.",
        )
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
        # A list of one error dict is a disguised Not_Ok; anything else (rows,
        # including an empty list) is a legitimate payload. The SDK returns the
        # parsed JSON list verbatim for books/positions, so do NOT drop it.
        if (
            len(resp) == 1
            and isinstance(resp[0], dict)
            and str(resp[0].get("stat", "")).lower() not in ("", "ok")
        ):
            return _unwrap(resp[0], context=context)
        return resp
    return resp


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

    def refresh_session(self, access_token: str, uid: str = "", account_id: str = "") -> bool:
        """Inject new token into client session and validate immediately."""
        with self._lock:
            try:
                from NorenRestApiPy.NorenApi import NorenApi
            except ImportError as e:
                raise BrokerError(f"NorenRestApiOAuth not installed: {e}")

            api = NorenApi(host=self.cfg.host, websocket=self.cfg.websocket)
            try:
                actual_uid = uid or self.cfg.uid
                actual_actid = account_id or self.cfg.account_id
                api.injectOAuthHeader(access_token, actual_uid, actual_actid)
                limits = _unwrap(api.get_limits(), context="refresh_session/get_limits")
                self._api = api
                self._session_ok = bool(limits)
                log.info("Shoonya session refreshed and verified (uid=%s)", actual_uid)
                return True
            except Exception as e:
                self._session_ok = False
                log.error("Shoonya refresh_session failed: %s", e)
                raise BrokerError(f"Token validation failed: {e}") from e


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
    def _call(self, method_name: str, *args, retries: int = 0,
              empty_on_none: bool = False, **kwargs):
        """Invoke an SDK method and unwrap it.

        empty_on_none: treat the SDK's ``None`` (which it returns both for a
        genuinely empty book AND for a Not_Ok it couldn't parse as a list) as an
        empty result instead of raising. Safe for read-only books where "no
        rows" is a normal state; never use it on trading calls.
        """
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
                if empty_on_none:
                    # The SDK hides the broker's reason; say so in our log
                    # rather than silently reporting an empty book.
                    log.warning("%s returned nothing; treating as empty: %s",
                                method_name, e)
                    return []
                last_err = e
                # Never blindly retry place_order: a timeout may still have
                # created the order. The engine reconciles via order book.
                if method_name == "place_order":
                    raise
                if attempt < retries:
                    time.sleep(0.5 * (2**attempt))
        if last_err is None:
            raise BrokerError(f"{method_name} failed with no response")
        raise last_err  # pragma: no cover - loop always returns or raises

    # --- account / books (read-only) ----------------------------------------
    def get_limits(self):
        return self._call("get_limits", retries=2)

    def get_positions(self):
        return self._call("get_positions", retries=1, empty_on_none=True) or []

    def get_order_book(self):
        return self._call("get_order_book", retries=1, empty_on_none=True) or []

    def get_trade_book(self):
        return self._call("get_trade_book", retries=1, empty_on_none=True) or []

    def get_holdings(self):
        return self._call("get_holdings", retries=1, empty_on_none=True) or []

    # --- market data --------------------------------------------------------
    def searchscrip(self, exchange: str, searchtext: str, *, retries: int = 2):
        return self._call("searchscrip", exchange, searchtext, retries=retries)

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

    def place_sl_order(self, *, buy_or_sell: str, product_type: str, exchange: str,
                       tradingsymbol: str, quantity: int,
                       trigger_price: float, limit_price: float,
                       retention: str = "DAY", remarks: str = "sl_bot") -> dict | None:
        """Place a Stop-Loss Limit (SL-LMT) order at the broker.

        For a short option SL: BUY back when price rises to trigger_price.
        The limit_price is set slightly above trigger_price to guarantee fill
        (exchange fills at best available price up to limit_price).
        Returns the broker's order dict (with norenordno) or raises.
        """
        if not self.live_armed:
            raise PaperMode(
                f"REFUSED place_sl_order {buy_or_sell} {tradingsymbol}: not armed"
            )
        log.warning(
            "LIVE SL ORDER -> %s %s %s x%d trigger=%.2f limit=%.2f (SL-LMT)",
            buy_or_sell, exchange, tradingsymbol, quantity, trigger_price, limit_price,
        )
        return self._call(
            "place_order",
            buy_or_sell=buy_or_sell,
            product_type=product_type,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            discloseqty=0,
            price_type="SL-LMT",
            price=limit_price,
            trigger_price=trigger_price,
            retention=retention,
            amo="NO",
            remarks=remarks,
        )

    def modify_sl_order(self, *, orderno: str, exchange: str, tradingsymbol: str,
                        quantity: int, trigger_price: float, limit_price: float) -> dict | None:
        """Modify an existing SL-LMT order (e.g. move SL to cost price)."""
        if not self.live_armed:
            raise PaperMode("REFUSED modify_sl_order: not armed for live trading")
        log.warning(
            "MODIFY SL ORDER %s -> trigger=%.2f limit=%.2f",
            orderno, trigger_price, limit_price,
        )
        return self._call(
            "modify_order",
            orderno=orderno,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            newquantity=quantity,
            newprice_type="SL-LMT",
            newprice=limit_price,
            newtrigger_price=trigger_price,
        )

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
