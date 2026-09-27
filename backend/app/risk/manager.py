"""Pre-trade risk gate. Every entry/exit intent must pass through `allow_*`."""
from __future__ import annotations

import datetime as dt
import logging

from app.config import settings

log = logging.getLogger("risk")


class RiskDecision:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


class RiskManager:
    def __init__(self, cfg=settings):
        self.cfg = cfg
        self.day: dt.date | None = None
        self.daily_pnl_pts = 0.0
        self.entries_today = 0
        self.halted_reason: str | None = None

    # --- day lifecycle ---------------------------------------------------
    def on_new_day(self, day: dt.date) -> None:
        if self.day != day:
            self.day = day
            self.daily_pnl_pts = 0.0
            self.entries_today = 0
            self.halted_reason = None

    def on_trade_close(self, pnl_pts: float) -> None:
        self.daily_pnl_pts += pnl_pts
        if self.daily_pnl_pts <= -abs(self.cfg.max_daily_loss_pts):
            self.halted_reason = (
                f"daily loss limit hit ({self.daily_pnl_pts:.1f} pts)"
            )
            log.error("RISK HALT: %s", self.halted_reason)

    # --- gates --------------------------------------------------------------
    def allow_entry(self, *, lots: int, now: dt.datetime) -> RiskDecision:
        if self.cfg.kill_switch:
            return RiskDecision(False, "kill switch engaged")
        if self.halted_reason:
            return RiskDecision(False, self.halted_reason)
        if now.time() >= self.cfg.no_new_entry_after:
            return RiskDecision(
                False, f"no new entries after {self.cfg.no_new_entry_after:%H:%M}"
            )
        if lots > self.cfg.max_lots:
            return RiskDecision(False, f"lots {lots} > max {self.cfg.max_lots}")
        if self.entries_today >= self.cfg.max_entries_per_day:
            return RiskDecision(
                False, f"max entries/day reached ({self.cfg.max_entries_per_day})"
            )
        return RiskDecision(True)

    def record_entry(self) -> None:
        self.entries_today += 1

    def allow_exit_anyway(self) -> bool:
        """Exits are ALWAYS allowed - never trap a position behind an entry gate."""
        return True

    def status(self) -> dict:
        return {
            "day": str(self.day) if self.day else None,
            "daily_pnl_pts": round(self.daily_pnl_pts, 2),
            "entries_today": self.entries_today,
            "halted_reason": self.halted_reason,
            "kill_switch": self.cfg.kill_switch,
            "max_daily_loss_pts": self.cfg.max_daily_loss_pts,
        }
