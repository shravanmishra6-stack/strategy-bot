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
    def __init__(self, client, index_token: str = "13", exchange: str = "NSE_FNO"):
        self.client = client
        self.index_token = index_token      # NIFTY 50 index in NSE_FNO
        self.exchange = exchange

    def available_expiries(self, today: dt.date) -> list[dt.date]:
        """Query the option chain for a mid strike and collect expiry dates."""
        rows = []
        # A chain call with any sane strike returns the whole strike ladder for
        # that expiry set; probe a couple of strikes to cover weeklies.
        for probe in (25000, 24000, 26000):
            try:
                resp = self.client.get_option_chain(self.exchange, str(self.index_token),
                                                    float(probe), count=5)
            except BrokerError as e:
                log.warning("option_chain probe @%s failed: %s", probe, e)
                continue
            if not resp:
                continue
            oc = resp.get("oc") if isinstance(resp, dict) else None
            if oc:
                rows.extend(oc)
        out: set[dt.date] = set()
        for r in rows:
            for key in ("CE", "PE"):
                item = r.get(key) or {}
                sym = item.get("trading_symbol") or item.get("symbol") or ""
                exp = expiry_from_symbol(sym)
                if exp:
                    out.add(exp)
        return sorted(e for e in out if e >= today)

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
                s for s in self.client.searchscrip(self.exchange, sym)
                or [] if s.get("tradsym") == sym
            ]
            if not matches:
                raise BrokerError(
                    f"contract {sym} not found via searchscrip - refusing to trade"
                )
            m = matches[0]
            legs.append(LegInstrument(
                token=str(m.get("token")), tradingsymbol=sym,
                strike=strike, right=right,
            ))
        return legs

    @staticmethod
    def _symbol(expiry: dt.date, strike: int, right: str) -> str:
        code = {v: k for k, v in MONTH_CODES.items()}[expiry.month]
        return f"NIFTY{expiry.year % 100:02d}{code}{expiry.day:02d}{right}{strike}"
