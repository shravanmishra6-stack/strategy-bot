"""
Per-leg state machine for sold option legs.

This is the ONLY place stop-loss / target / trailing rules live. The backtester
feeds it historical bars; the live engine feeds it websocket-built bars. Keeping
one implementation is what makes backtest numbers meaningful for live trading.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bar:
    """A single completed OHLC bar (premium price for option legs)."""

    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_row(cls, row) -> "Bar":
        return cls(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )


# Exit reasons, shared with the backtest trade log and the dashboard UI.
SL = "SL"
TRAIL_SL = "TRAIL_SL"
TARGET = "TARGET"
SQUAREOFF = "SQUAREOFF"
HOLD_TO_EXPIRY = "HOLD_TO_EXPIRY"
EOD_FINAL = "EOD_FINAL"


@dataclass
class SoldLeg:
    """One sold (short) option leg with its entry premium and live stop level."""

    right: str                 # "CE" | "PE"
    strike: int
    token: str | None = None   # Shoonya contract token (live only)
    tradingsymbol: str | None = None
    entry: float | None = None
    sl_pct: float | None = None
    target_pct: float | None = None
    trail_pct: float | None = None
    is_open: bool = False
    sl_level: float | None = None      # fixed stop, computed at entry
    trail_sl: float | None = None      # active trailing stop, once armed
    last_close: float | None = None
    order_id: str | None = None        # broker norenordno (live only)

    # --- lifecycle ---------------------------------------------------------
    def arm(self, entry_premium: float) -> None:
        """Call once the short fill is known: sets stops and marks open."""
        self.entry = round(float(entry_premium), 4)
        self.is_open = True
        self.last_close = self.entry
        self.sl_level = entry_premium * (1 + self.sl_pct) if self.sl_pct else None
        self.target_level = (
            entry_premium * (1 - self.target_pct) if self.target_pct else None
        )
        self.trail_sl = None

    @property
    def active_stop(self) -> float | None:
        """Trailing stop wins over the fixed stop once armed."""
        if self.trail_sl is not None:
            return self.trail_sl
        return self.sl_level

    def disarm(self) -> None:
        self.is_open = False

    # --- decision ----------------------------------------------------------
    def on_bar(self, bar: Bar) -> tuple[str, float] | None:
        """Evaluate one bar. Returns (reason, fill_price) if the leg exits.

        Fill convention (matches run_backtest_seller.py):
          * stop hit -> fill at the stop, unless the bar gapped through it, in
            which case the open is the realistic fill (worse).
          * target hit -> fill at the target, unless it gapped better at open.
        """
        if not self.is_open:
            return None

        self.last_close = bar.close

        stop = self.active_stop
        if stop is not None and bar.high >= stop:
            fill = stop if bar.open < stop else bar.open
            reason = TRAIL_SL if self.trail_sl is not None else SL
            self.is_open = False
            return reason, float(fill)

        tgt = getattr(self, "target_level", None)
        if tgt is not None and bar.low <= tgt:
            fill = tgt if bar.open > tgt else bar.open
            self.is_open = False
            return TARGET, float(fill)

        # Arm the trailing stop once price has moved `trail_pct` of the SL
        # distance in our favour. A sold leg profits as premium falls, so the
        # move is measured downwards from entry.
        if self.trail_pct and self.sl_level and self.entry:
            if (self.entry - bar.low) >= self.trail_pct * (self.sl_level - self.entry):
                self.trail_sl = bar.low

        return None

    def to_dict(self) -> dict:
        return {
            "right": self.right,
            "strike": self.strike,
            "token": self.token,
            "tradingsymbol": self.tradingsymbol,
            "entry": self.entry,
            "is_open": self.is_open,
            "sl_level": self.sl_level,
            "target_level": getattr(self, "target_level", None),
            "trail_sl": self.trail_sl,
            "last_close": self.last_close,
            "order_id": self.order_id,
        }
