"""Tick -> 1-minute OHLC bar aggregation for the live engine.

Backtest consumes completed 1-min bars; live must too, or the shared SoldLeg
rules would see different data shapes. A bar is COMPLETE (and emitted) when the
first tick of the NEXT minute arrives, exactly like exchange bar close.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class CompletedBar:
    ts: dt.datetime      # bar open time (floored to minute)
    open: float
    high: float
    low: float
    close: float


class MinuteBarBuilder:
    """Feed ticks in time order; get a CompletedBar when each minute ends."""

    def __init__(self, on_bar):
        self.on_bar = on_bar
        self._cur: dict | None = None

    @staticmethod
    def _floor(ts: dt.datetime) -> dt.datetime:
        return ts.replace(second=0, microsecond=0)

    def on_tick(self, ts: dt.datetime, price: float) -> CompletedBar | None:
        minute = self._floor(ts)
        completed = None
        if self._cur is None or minute > self._cur["ts"]:
            if self._cur is not None:
                completed = self._emit()
            self._cur = {"ts": minute, "open": price, "high": price,
                         "low": price, "close": price}
        elif minute == self._cur["ts"]:
            self._cur["high"] = max(self._cur["high"], price)
            self._cur["low"] = min(self._cur["low"], price)
            self._cur["close"] = price
        # ticks older than the current minute are ignored (out-of-order guard)
        return completed

    def flush(self) -> CompletedBar | None:
        """Force-emit the in-progress bar (e.g. at square-off time)."""
        if self._cur is None:
            return None
        return self._emit()

    def _emit(self) -> CompletedBar:
        c = self._cur
        self._cur = None
        bar = CompletedBar(ts=c["ts"], open=c["open"], high=c["high"],
                           low=c["low"], close=c["close"])
        if self.on_bar:
            self.on_bar(bar)
        return bar