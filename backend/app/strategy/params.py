"""
Strategy parameters. Every knob your CSV sweep varied is exposed here so the
dashboard can edit them at runtime without touching code.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class StraddleParams:
    """Short-straddle / short-strangle seller parameters.

    Defaults reproduce `run_backtest_seller.py` exactly, so a fresh
    StrategyConfig backtest should match your existing CSV numbers.
    """

    entry_time: dt.time = dt.time(9, 20)
    squareoff_time: dt.time = dt.time(15, 15)
    expiry_mode: str = "EOD"  # "EOD" (square off) | "EXPIRY" (hold to last bar)

    sl_pct: float | None = 0.30       # stop when a leg's premium rises +30%
    target_pct: float | None = None   # take profit when premium falls -X%
    trail_pct: float | None = None    # trail once leg moves X% of SL distance
    strike_offset: int = 0            # 0 = ATM straddle, >0 = strangle (PE further OTM)

    lots: int = 1
    brokerage: float = 20.0           # Rs per order, per leg
    slippage: float = 1.0             # points of adverse fill per leg
    strike_step: int = 50             # NIFTY strike interval

    # NIFTY lot size changed over the sample; mirror the historical schedule.
    lot_schedule: tuple[tuple[dt.date, int], ...] = (
        (dt.date(2024, 4, 30), 50),
        (dt.date(2025, 5, 1), 75),
        (dt.date(2026, 1, 1), 65),
    )
    default_lot: int = 25

    def lot_size(self, d: dt.date) -> int:
        """Effective lot size for a trade date (later schedule entries win)."""
        lot = self.default_lot
        for start, size in sorted(self.lot_schedule):
            if d >= start:
                lot = size
        return lot

    def label(self) -> str:
        bits = [f"Entry {self.entry_time:%H:%M}"]
        if self.expiry_mode == "EOD":
            bits.append(f"SQO {self.squareoff_time:%H:%M}")
        if self.sl_pct:
            bits.append(f"SL {self.sl_pct:.0%}")
        if self.target_pct:
            bits.append(f"TGT {self.target_pct:.0%}")
        if self.trail_pct:
            bits.append(f"TRAIL {self.trail_pct:.0%}")
        if self.strike_offset:
            bits.append(f"Strangle +{self.strike_offset}")
        bits.append(f"{self.lots}L")
        return " | ".join(bits)

    def to_dict(self) -> dict:
        return {
            "entry_time": f"{self.entry_time:%H:%M}",
            "squareoff_time": f"{self.squareoff_time:%H:%M}",
            "expiry_mode": self.expiry_mode,
            "sl_pct": self.sl_pct,
            "target_pct": self.target_pct,
            "trail_pct": self.trail_pct,
            "strike_offset": self.strike_offset,
            "lots": self.lots,
            "brokerage": self.brokerage,
            "slippage": self.slippage,
            "strike_step": self.strike_step,
            "default_lot": self.default_lot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StraddleParams":
        def _t(key: str, default: dt.time) -> dt.time:
            raw = d.get(key)
            if not raw:
                return default
            hh, mm = str(raw).split(":")
            return dt.time(int(hh), int(mm))

        return cls(
            entry_time=_t("entry_time", cls.entry_time),
            squareoff_time=_t("squareoff_time", cls.squareoff_time),
            expiry_mode=d.get("expiry_mode", cls.expiry_mode),
            sl_pct=d.get("sl_pct", cls.sl_pct),
            target_pct=d.get("target_pct", cls.target_pct),
            trail_pct=d.get("trail_pct", cls.trail_pct),
            strike_offset=int(d.get("strike_offset", cls.strike_offset)),
            lots=int(d.get("lots", cls.lots)),
            brokerage=float(d.get("brokerage", cls.brokerage)),
            slippage=float(d.get("slippage", cls.slippage)),
            strike_step=int(d.get("strike_step", cls.strike_step)),
            default_lot=int(d.get("default_lot", cls.default_lot)),
        )
