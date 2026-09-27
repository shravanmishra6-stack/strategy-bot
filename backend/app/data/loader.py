"""
Historical data access for the local 1-min CSV dump (Breeze-era files, reused
as-is). Kept separate from the engine so a live broker feed can replace it.

Notes learned from inspecting the dataset:
  * Spot CSVs: nifty_spot_1min/nifty_spot_1min_<year>.csv
  * Options:   nifty_options_1min/expiry_<YYYY-MM-DD>/NIFTY_<expiry>_<strike>_<CE|PE>.csv
  * Some expiry_* directories are EMPTY placeholders -> must be filtered out.
  * Files carry a header + rows; anything <= 100 bytes is an empty stub.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import threading
from functools import lru_cache

import pandas as pd

MIN_VALID_BYTES = 100
OHLC = ["open", "high", "low", "close"]


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates("datetime").sort_values("datetime")
    return df.set_index("datetime")[OHLC]


def load_spot(spot_dir: str | os.PathLike) -> pd.DataFrame:
    """Concatenated 1-min NIFTY spot bars, indexed by datetime (IST)."""
    files = sorted(glob.glob(os.path.join(str(spot_dir), "*.csv")))
    if not files:
        raise FileNotFoundError(f"No spot CSVs in {spot_dir}")
    frames = []
    for f in files:
        if os.path.getsize(f) <= MIN_VALID_BYTES:
            continue
        df = pd.read_csv(f)
        if "datetime" not in df.columns:
            continue
        frames.append(_tidy(df))
    if not frames:
        raise FileNotFoundError(f"No usable spot CSVs in {spot_dir}")
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out


def list_expiries(options_dir: str | os.PathLike) -> list[dt.date]:
    """Expiry dates that actually contain at least one real contract CSV."""
    out: list[dt.date] = []
    for path in sorted(glob.glob(os.path.join(str(options_dir), "expiry_*"))):
        tag = os.path.basename(path).replace("expiry_", "")
        try:
            date = dt.date.fromisoformat(tag)
        except ValueError:
            continue
        has_data = any(
            os.path.getsize(c) > MIN_VALID_BYTES
            for c in glob.glob(os.path.join(path, "*.csv"))
        )
        if has_data:
            out.append(date)
    return sorted(out)


def option_path(options_dir: str | os.PathLike, expiry: dt.date, strike: int, right: str):
    fname = f"NIFTY_{expiry}_{strike}_{right}.csv"
    return os.path.join(str(options_dir), f"expiry_{expiry}", fname)


def load_option(
    options_dir: str | os.PathLike, expiry: dt.date, strike: int, right: str
) -> pd.DataFrame | None:
    """1-min premium bars for one contract, or None if unavailable."""
    p = option_path(options_dir, expiry, strike, right)
    if not os.path.exists(p) or os.path.getsize(p) <= MIN_VALID_BYTES:
        return None
    df = pd.read_csv(p)
    if "datetime" not in df.columns or len(df) < 2:
        return None
    return _tidy(df)


class OptionCache:
    """Thread-safe memoised contract loader (backs both backtest and live)."""

    def __init__(self, options_dir: str | os.PathLike):
        self.options_dir = str(options_dir)
        self._store: dict[tuple, pd.DataFrame | None] = {}
        self._lock = threading.Lock()

    def get(self, expiry: dt.date, strike: int, right: str) -> pd.DataFrame | None:
        key = (expiry, strike, right)
        with self._lock:
            if key in self._store:
                return self._store[key]
        df = load_option(self.options_dir, expiry, strike, right)
        with self._lock:
            self._store[key] = df
        return df

    def __len__(self) -> int:
        return len(self._store)


@lru_cache(maxsize=1)
def _noop():  # keeps lru_cache import meaningful for future caching needs
    return None


def build_day_to_expiry(
    spot: pd.DataFrame, expiries: list[dt.date]
) -> dict[dt.date, dt.date]:
    """Map each trading day to the nearest expiry on/after it (T+0 roll rule)."""
    trade_days = sorted({d.date() for d in spot.index.normalize().unique()})
    mapping: dict[dt.date, dt.date] = {}
    for d in trade_days:
        for e in expiries:
            if e >= d:
                mapping[d] = e
                break
    return mapping
