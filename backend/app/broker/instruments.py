"""Resolve NIFTY weekly-expiry option contracts to tradable Shoonya tokens.

Everything here is written against the *verified live* behaviour of
api.shoonya.com (read-only probes, 2026-09-27), not the vendor docs:

  * The F&O segment for this account is ``NFO``. ``NSE_FNO`` is answered with
    HTTP 404 ``No Data :  Exchange Not enabled``.
  * ``SearchScrip`` returns rows under ``values`` (NOT ``result``), each with
    ``tsym/token/exch/seg/symname/instname/exd/optt/ls/pp/ti``.
  * ``GetOptionChain`` needs ``exch=NFO``, a *derivative* ``tsym`` root (the
    monthly futures contract, e.g. ``NIFTY29SEP26F``) and a NON-ZERO
    ``strprc``; ``strprc=0`` -> ``Invalid Input :  Invalid strprc`` and
    ``tsym='NIFTY'`` -> ``Invalid Trading Symbol``. Rows are under ``values``.
  * Option symbols look like ``NIFTY29SEP26C23400`` (root + DD + MON + YY +
    C/P + strike); NSE token ``26000`` ("Nifty 50") is the spot index.

Resolution still fails closed: if an expiry/strike pair cannot be confirmed by
an exact ``SearchScrip`` round trip, ``resolve`` raises and the bot refuses to
trade that day rather than guessing a contract.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from app.broker.noren_client import BrokerError
from app.config import settings
from app.strategy.live import LegInstrument

log = logging.getLogger("broker.instruments")

MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
EXD_RE = re.compile(r"^(\d{1,2})-(" + "|".join(MONTH_NAMES) + r")-(\d{4})$")
OPTION_RE = re.compile(
    r"^[A-Z]+?(\d{2})(" + "|".join(MONTH_NAMES) + r")(\d{2})([CP])(\d+)$")
FUTURE_RE = re.compile(
    r"^[A-Z]+?(\d{2})(" + "|".join(MONTH_NAMES) + r")(\d{2})F$")

# Scrip classification from the live SearchScrip payload.
INSTR_FUTURES = "FUTIDX"
INSTR_OPTIONS = "OPTIDX"


def parse_expiry_date(value) -> dt.date | None:
    """Parse the broker's ``exd`` field, which looks like ``29-SEP-2026``."""
    if not value:
        return None
    m = EXD_RE.match(str(value).strip().upper())
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), MONTH_NAMES.index(m.group(2)) + 1,
                       int(m.group(1)))
    except ValueError:
        return None


def expiry_from_symbol(symbol) -> dt.date | None:
    """Expiry encoded in a live Shoonya derivative symbol.

    Handles ``NIFTY29SEP26C23400`` (options) and ``NIFTY29SEP26F`` (futures);
    returns None for anything that is not a recognisable derivative symbol.
    """
    if not symbol:
        return None
    s = str(symbol).strip().upper().replace(" ", "")
    for rx in (OPTION_RE, FUTURE_RE):
        m = rx.match(s)
        if m:
            try:
                return dt.date(2000 + int(m.group(3)),
                               MONTH_NAMES.index(m.group(2)) + 1,
                               int(m.group(1)))
            except ValueError:
                return None
    return None


def pick_nearest_expiry(expiries: list[dt.date], today: dt.date,
                        min_days: int = 0) -> dt.date | None:
    """Nearest expiry >= today (+min_days). None if nothing qualifies."""
    cands = [e for e in expiries if (e - today).days >= min_days]
    return min(cands) if cands else None


class InstrumentResolver:
    """Maps a NIFTY expiry/strike/right to a tradable Shoonya token.

    Defaults come from app.config so they are overridable per deployment; the
    values themselves were confirmed against the authenticated live API.
    """

    ROW_KEYS = ("values", "result", "scrip", "scriplist", "data")

    def __init__(self, client, *, root_symbol: str | None = None,
                 exchange: str | None = None,
                 index_exchange: str | None = None,
                 index_token: str | None = None,
                 strike_step: int | None = None):
        self.client = client
        self.root_symbol = (root_symbol or settings.root_symbol).upper()
        self.exchange = exchange or settings.fno_exchange             # NFO
        self.index_exchange = index_exchange or settings.index_exchange  # NSE
        self.index_token = index_token or settings.index_token        # 26000
        self.strike_step = strike_step or 50

    # --- response normalisation --------------------------------------------
    @classmethod
    def _scrip_rows(cls, resp) -> list[dict]:
        """SearchScrip/GetOptionChain -> list of row dicts.

        The live API answers both under ``values``; documented shapes use
        ``result``, so both (and a bare list) are tolerated.
        """
        if not resp:
            return []
        if isinstance(resp, list):
            return [r for r in resp if isinstance(r, dict)]
        if isinstance(resp, dict):
            for key in cls.ROW_KEYS:
                val = resp.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
        return []

    @staticmethod
    def _row_symbol(row: dict) -> str:
        for key in ("tsym", "tradsym", "trading_symbol", "symbol"):
            val = row.get(key)
            if val:
                return str(val)
        return ""

    @staticmethod
    def _row_token(row: dict) -> str:
        for key in ("token", "scrp", "scrip", "insttoken", "instr_token"):
            val = row.get(key)
            if val not in (None, "", 0):
                return str(val)
        return ""

    @staticmethod
    def _row_lot(row: dict) -> int | None:
        """Lot size (live field ``ls``); None when absent/unparsable."""
        try:
            lot = int(float(str(row.get("ls"))))
        except (TypeError, ValueError):
            return None
        return lot if lot > 0 else None

    def _row_expiry(self, row: dict) -> dt.date | None:
        return (parse_expiry_date(row.get("exd"))
                or expiry_from_symbol(self._row_symbol(row)))

    # --- market data ---------------------------------------------------------
    def index_quote_price(self) -> float | None:
        """Spot level of the NIFTY index (used to pick ATM at entry)."""
        try:
            q = self.client.get_quotes(self.index_exchange, self.index_token) or {}
        except BrokerError as e:
            log.warning("index quote %s/%s failed: %s",
                        self.index_exchange, self.index_token, e)
            return None
        if not isinstance(q, dict):
            return None
        for key in ("lp", "rt_price", "c", "pclose"):
            try:
                val = q.get(key)
                if val is not None and float(val) > 0:
                    return float(val)
            except (TypeError, ValueError):
                continue
        return None

    def search(self, text: str, retries: int | None = None) -> list[dict]:
        """searchscrip on the F&O segment; [] when the broker has nothing.

        retries=None keeps the client default (used when resolving a contract we
        intend to trade). Probers pass 0 so a miss costs one request, not three.
        """
        try:
            kwargs = {} if retries is None else {"retries": retries}
            resp = self.client.searchscrip(self.exchange, text, **kwargs)
        except BrokerError as e:
            log.debug("searchscrip(%s, %s) failed: %s", self.exchange, text, e)
            return []
        return self._scrip_rows(resp)

    # --- expiry discovery ----------------------------------------------------
    @staticmethod
    def _candidate_expiry_dates(today: dt.date, horizon_days: int):
        """Candidate expiry dates, in ascending order, on configured weekdays.

        The live probe found NIFTY weeklies only on Tuesdays (29-SEP, 06-OCT,
        13-OCT 2026), so settings.expiry_weekdays defaults to Tuesday. If the
        exchange moves the schedule, widen SHOONYA_EXPIRY_WEEKDAYS; a date that
        is not listed resolves to "no expiry found", which makes the bot refuse
        to trade rather than pick a wrong contract.
        """
        weekdays = settings.expiry_weekdays or (1,)
        ordered = sorted(weekdays, key=lambda w: (w != 1, w))
        for off in range(0, horizon_days + 1):
            d = today + dt.timedelta(days=off)
            if d.weekday() in ordered:
                yield d

    def available_expiries(self, today: dt.date,
                           horizon_days: int = 45,
                           max_probes: int = 12) -> list[dt.date]:
        """Tradable NIFTY expiries: futures master list plus weekly probes.

        ``searchscrip(NFO, "NIFTY")`` returns the month futures whose ``exd``
        field is the real expiry calendar. Weekly contracts exist that are NOT
        in that list (only the month-end weeks carry futures), so each candidate
        weekday is confirmed with a symbol-prefix search - but only while it can
        still be nearer than the best expiry we already hold. That keeps the
        query count tiny (usually 1-3 probes) without ever missing the true
        nearest expiry, and stops us hammering a rate-limited API.
        """
        out: set[dt.date] = set()

        # 1) Master list: the root's futures carry real expiry dates.
        for row in self.search(self.root_symbol):
            if str(row.get("instname", "")).upper() != INSTR_FUTURES:
                continue
            if str(row.get("symname", "")).upper() != self.root_symbol:
                continue
            exp = self._row_expiry(row)
            if exp and exp >= today:
                out.add(exp)

        # 2) Weekly probes, in date order, bounded by the best expiry known.
        probes = 0
        for cand in self._candidate_expiry_dates(today, horizon_days):
            if cand in out:
                continue
            if out and cand >= min(out):
                break                      # cannot beat what we already have
            if probes >= max_probes:
                log.warning("expiry search hit probe budget (%d) - "
                            "returning %s", max_probes, sorted(out))
                break
            probes += 1
            prefix = self._symbol_root(cand)
            if any(
                self._row_symbol(r).upper().startswith(prefix)
                and str(r.get("symname", "")).upper() == self.root_symbol
                for r in self.search(prefix, retries=0)
            ):
                out.add(cand)

        exps = sorted(e for e in out if e >= today)
        log.info("resolved %d %s expiries on %s (%d weekly probes): %s",
                 len(exps), self.root_symbol, self.exchange, probes,
                 [str(e) for e in exps])
        return exps

        exps = sorted(e for e in out if e >= today)
        log.info("resolved %d %s expiries on %s: %s", len(exps),
                 self.root_symbol, self.exchange, [str(e) for e in exps])
        return exps

    def nearest_expiry(self, today: dt.date,
                       expiries: list[dt.date] | None = None) -> dt.date:
        """Nearest tradable expiry. Raises rather than let the bot guess.

        min_days=0 keeps the backtest's "nearest expiry" rule, which includes
        the expiry day itself - that is when a weekly short straddle earns most
        of its theta.
        """
        exps = expiries if expiries is not None else self.available_expiries(today)
        exp = pick_nearest_expiry(exps, today, min_days=0)
        if not exp:
            raise BrokerError(
                f"no tradable {self.root_symbol} expiry on/after {today} "
                f"(found {len(exps)}) - refusing to trade")
        return exp

    # --- option chain ----------------------------------------------------------
    def option_root_candidates(self, today: dt.date) -> list[str]:
        """tsym values GetOptionChain accepts, nearest-expiry futures first."""
        roots: list[str] = []
        rows = sorted(self.search(self.root_symbol),
                      key=lambda r: str(self._row_expiry(r) or dt.date.max))
        for row in rows:
            if str(row.get("instname", "")).upper() != INSTR_FUTURES:
                continue
            if str(row.get("symname", "")).upper() != self.root_symbol:
                continue
            exp = self._row_expiry(row)
            if exp is None or exp < today:
                continue
            tsym = self._row_symbol(row)
            if tsym and tsym not in roots:
                roots.append(tsym)
        return roots

    def chain_root_for(self, expiry: dt.date, today: dt.date) -> str | None:
        """A tsym whose option chain belongs to *expiry*.

        GetOptionChain needs a derivative tsym. Weekly expiries often have no
        futures contract at all (live probe: NIFTY06OCT26 returned only OPTIDX
        rows), so rooting purely on futures would silently describe a different
        month. Prefer the month future, else any option contract for that exact
        expiry - the chain endpoint accepts an option tsym as its root.
        """
        prefix = self._symbol_root(expiry)
        rows = self.search(prefix)
        fut = next((self._row_symbol(r) for r in rows
                    if self._row_symbol(r).upper().endswith("F")
                    and self._row_expiry(r) == expiry), None)
        if fut:
            return fut
        opt = next((self._row_symbol(r) for r in rows
                    if str(r.get("symname", "")).upper() == self.root_symbol
                    and self._row_symbol(r).upper().startswith(prefix)
                    and self._row_expiry(r) == expiry), None)
        if opt:
            return opt
        # Fall back to the generic futures-rooted list (nearest month).
        for root in self.option_root_candidates(today):
            if expiry_from_symbol(root) == expiry:
                return root
        return None

    def chain_rows(self, *, today: dt.date, ref_strike: float | int,
                   count: int = 3, expiry: dt.date | None = None,
                   roots: list[str] | None = None) -> list[dict]:
        """Option-chain rows around ``ref_strike`` from the first answering root.

        Live GetOptionChain rejects ``tsym='NIFTY'`` and ``strprc=0``, so the
        root is a derivative symbol and the strike is the rounded spot. Pass
        ``expiry`` to pin the chain to the contract we intend to trade; without
        it, every futures root is tried. [] means the broker gave nothing.
        """
        strike = int(round(float(ref_strike) / self.strike_step) * self.strike_step)
        if strike <= 0:
            return []
        if roots is None:
            if expiry is not None:
                root = self.chain_root_for(expiry, today)
                roots = [root] if root else []
            else:
                roots = self.option_root_candidates(today)
        for root in roots:
            try:
                resp = self.client.get_option_chain(self.exchange, root,
                                                    float(strike), count=count)
            except BrokerError as e:
                log.warning("option_chain root=%s cnt=%s failed: %s",
                            root, count, e)
                continue
            rows = self._scrip_rows(resp)
            if rows:
                return rows
        return []

    # --- symbol construction -------------------------------------------------
    def _symbol_root(self, expiry: dt.date) -> str:
        return (f"{self.root_symbol}{expiry.day:02d}"
                f"{MONTH_NAMES[expiry.month - 1]}{expiry.year % 100:02d}")

    def _symbol(self, expiry: dt.date, strike: int, right: str) -> str:
        """Live NSE derivative symbol, e.g. NIFTY29SEP26C23400."""
        right_char = "C" if str(right).upper().startswith("C") else "P"
        return f"{self._symbol_root(expiry)}{right_char}{int(strike)}"

    # --- contract resolution -------------------------------------------------
    def resolve(self, *, expiry: dt.date, atm: int, step: int | None = None,
                pe_offset_steps: int = 0,
                lot_size: int | None = None) -> list[LegInstrument]:
        """Return [CE, PE] LegInstruments for a strike+expiry, or raise.

        Every leg must survive an exact searchscrip round trip; token, exchange
        and lot size come from the broker, never from a hard-coded guess.
        """
        step = step or self.strike_step
        want = {"CE": atm, "PE": atm + pe_offset_steps * step}
        legs = []
        for right, strike in want.items():
            sym = self._symbol(expiry, strike, right)
            matches = [
                r for r in self.search(sym)
                if self._row_symbol(r).upper() == sym.upper()
                and str(r.get("symname", "")).upper() == self.root_symbol
            ]
            if not matches:
                raise BrokerError(
                    f"contract {sym} not found via searchscrip({self.exchange})"
                    f" - refusing to trade")
            row = matches[0]
            token = self._row_token(row)
            if not token:
                raise BrokerError(f"contract {sym} resolved without a token")
            row_exp = self._row_expiry(row)
            if row_exp and row_exp != expiry:
                raise BrokerError(
                    f"contract {sym} resolves to expiry {row_exp}, expected "
                    f"{expiry} - refusing to trade")
            legs.append(LegInstrument(
                token=token, tradingsymbol=sym, strike=strike, right=right,
                exchange=str(row.get("exch") or self.exchange),
                lot_size=lot_size or self._row_lot(row),
            ))
        return legs
