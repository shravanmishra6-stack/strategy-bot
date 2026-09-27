"""Resolve NIFTY weekly-expiry option contracts to tradable Shoonya tokens.

Shoonya's API has no clean "list all expiries" endpoint, so we triangulate:
  1. get_option_chain on the NIFTY index token returns available strike/expiry
     combinations (per docs; validated against what the SDK actually returns).
  2. searchscrip confirms each candidate tradingsymbol -> token.
  3. Historical dump folder names (expiry_YYYY-MM-DD) are used ONLY as a sanity
     check / tie-breaker, never as the source of truth for a live trade.

If the nearest expiry cannot be resolved with confidence, resolution RAISES -
the bot then refuses to trade that day rather than guessing a contract.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from app.broker.noren_client import BrokerError
from app.strategy.live import LegInstrument

log = logging.getLogger("broker.instruments")

NIFTY_SYMBOL_RE = re.compile(
    r"^NIFTY(?P<y>\d{2})(?P<m>[JFMASONQZUVW])(?P<d>\d{2})(?P<right>CE|PE)(?P<strike>\d{3,5})$",
    re.IGNORECASE,
)
MONTH_CODES = {  # F&O month codes used by Indian exchanges
    "J": 1, "F": 2, "M": 3, "A": 4, "S": 5, "O": 6,
    "N": 7, "Q": 8, "Z": 9, "U": 10, "V": 11, "W": 12,
}


def expiry_from_symbol(symbol: str) -> dt.date | None:
    m = NIFTY_SYMBOL_RE.match(symbol.strip())
    if not m:
        return None
    y = 2000 + int(m.group("y"))
    mo = MONTH_CODES.get(m.group("m").upper())
    day = int(m.group("d"))
    if not mo:
        return None
    try:
        return dt.date(y, mo, day)
    except ValueError:
        return None


def pick_nearest_expiry(expiries: list[dt.date], today: dt.date,
                        min_days: int = 0) -> dt.date | None:
    """Nearest expiry >= today (+min_days). None if nothing qualifies."""
    cands = [e for e in expiries if (e - today).days >= min_days]
    return min(cands) if cands else None


class InstrumentResolver:
    """Maps a NIFTY expiry/strike/right to a tradable Shoonya token.

    index_symbol is what get_option_chain wants (it is sent as `tsym`);
    index_token is the numeric instrument token used for websocket quotes.
    Both default to the usual NSE_FNO NIFTY values but are overridable once
    the read-only smoke test confirms them for this account.
    """

    def __init__(self, client, index_symbol: str = "NIFTY",
                 index_token: str = "13", exchange: str = "NSE_FNO"):
        self.client = client
        self.index_symbol = index_symbol
        self.index_token = index_token
        self.exchange = exchange

    def _chain_rows(self, resp) -> list[dict]:
        """Normalise get_option_chain into [{symbol|CE/PE}, ...] tolerantly.

        Verified against SDK 0.0.41: get_option_chain(exchange, tradingsymbol,
        strikeprice, count) -> {"stat":"Ok", ...} or None on Not_Ok. The nested
        payload shape is undocumented, so we walk it for anything that looks
        like an option tradingsymbol and read expiry off the symbol itself.
        """
        out: list[dict] = []
        seen_syms: set[str] = set()

        def walk(node) -> None:
            if isinstance(node, dict):
                for key, val in node.items():
                    if isinstance(val, (dict, list)):
                        walk(val)
                    # some shapes put the symbol as a dict key
                for skey in ("trading_symbol", "tradingsymbol", "tsym", "sym"):
                    sym = node.get(skey)
                    if isinstance(sym, str) and sym.upper().startswith("NIFTY") \
                            and sym not in seen_syms:
                        seen_syms.add(sym)
                        out.append({"symbol": sym})
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str) and node.upper().startswith("NIFTY"):
                if node not in seen_syms:
                    seen_syms.add(node)
                    out.append({"symbol": node})

        walk(resp)
        return out

    def available_expiries(self, today: dt.date) -> list[dt.date]:
        """Query the option chain for the index and collect expiry dates.

        NB: the SDK's get_option_chain takes a TRADING SYMBOL (it is sent as
        `tsym`), not an instrument token.
        """
        rows: list[dict] = []
        probes = [self.index_symbol] if self.index_symbol else []
        probes += [self.index_token] if self.index_token else []
        for probe in probes:
            for count in (50, 5):
                try:
                    resp = self.client.get_option_chain(self.exchange, str(probe),
                                                       0.0, count=count)
                except BrokerError as e:
                    log.warning("option_chain probe %s cnt=%s failed: %s", probe, count, e)
                    continue
                if not resp:
                    continue
                found = self._chain_rows(resp)
                log.info("option_chain probe %s cnt=%s -> %d symbols", probe, count, len(found))
                if found:
                    rows.extend(found)
                    break
            if rows:
                break
        out: set[dt.date] = set()
        for r in rows:
            exp = expiry_from_symbol(r.get("symbol") or "")
            if exp:
                out.add(exp)
        return sorted(e for e in out if e >= today)

    @staticmethod
    def _scrip_rows(resp) -> list[dict]:
        """searchscrip -> list of scrip dicts across SDK/response variants."""
        if not resp:
            return []
        if isinstance(resp, list):
            return [r for r in resp if isinstance(r, dict)]
        if isinstance(resp, dict):
            for key in ("result", "scrip", "scriplist", "data"):
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

    def resolve(self, *, expiry: dt.date, atm: int, step: int = 50,
                pe_offset_steps: int = 0) -> list[LegInstrument]:
        """Return [CE, PE] LegInstruments for a strike+expiry, or raise."""
        want = {
            "CE": atm,
            "PE": atm + pe_offset_steps * step,
        }
        legs = []
        for right, strike in want.items():
            sym = self._symbol(expiry, strike, right)
            matches = [
                r for r in self._scrip_rows(self.client.searchscrip(self.exchange, sym))
                if self._row_symbol(r).upper() == sym.upper()
            ]
            if not matches:
                raise BrokerError(
                    f"contract {sym} not found via searchscrip - refusing to trade"
                )
            token = self._row_token(matches[0])
            if not token:
                raise BrokerError(f"contract {sym} resolved without a token - refusing")
            legs.append(LegInstrument(
                token=token, tradingsymbol=sym, strike=strike, right=right,
            ))
        return legs


    @staticmethod
    def _symbol(expiry: dt.date, strike: int, right: str) -> str:
        code = {v: k for k, v in MONTH_CODES.items()}[expiry.month]
        return f"NIFTY{expiry.year % 100:02d}{code}{expiry.day:02d}{right}{strike}"
