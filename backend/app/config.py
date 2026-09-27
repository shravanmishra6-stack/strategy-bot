"""
Central configuration. Secrets come ONLY from environment / .env, never from
the dashboard or source control. Defaults are safe: PAPER_MODE is on unless you
explicitly turn it off.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: .env support if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience, not a hard dep
    pass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _hhmm(name: str, default: str) -> dt.time:
    raw = (os.getenv(name) or default).strip()
    hh, mm = raw.split(":")
    return dt.time(int(hh), int(mm))


BACKEND_DIR = Path(__file__).resolve().parent.parent
# Your existing 1-min CSV dump (spot + options) lives on the Desktop.
DEFAULT_DATA_DIR = Path.home() / "Desktop" / "nifty_breeze_data"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings resolved once at import time."""

    paper_mode: bool = field(default_factory=lambda: _bool("PAPER_MODE", True))
    kill_switch: bool = field(default_factory=lambda: _bool("KILL_SWITCH", False))

    # Shoonya / Noren
    client_id: str = field(default_factory=lambda: os.getenv("SHOONYA_CLIENT_ID", ""))
    secret_code: str = field(default_factory=lambda: os.getenv("SHOONYA_SECRET_CODE", ""))
    uid: str = field(default_factory=lambda: os.getenv("SHOONYA_UID", ""))
    account_id: str = field(default_factory=lambda: os.getenv("SHOONYA_ACCOUNT_ID", ""))
    access_token: str = field(default_factory=lambda: os.getenv("SHOONYA_ACCESS_TOKEN", ""))
    # Browser OAuth page. Verified against Shoonya's own /OAuthlogin/ SPA
    # (assets/Auth-*.js): the 'authorize' route is mounted at base
    # '/OAuthlogin/' + '/authorize/oauth' and reads 'client_id' from the query
    # string, which is exactly what NorenApi.getOAuthURL() appends. The
    # 'NorenWeb/authorize/oauth' path found in the vendor sample cred.yml is
    # stale and returns 404.
    oauth_url: str = field(
        default_factory=lambda: os.getenv(
            "SHOONYA_OAUTH_URL", "https://api.shoonya.com/OAuthlogin/authorize/oauth"
        )
    )
    host: str = field(
        default_factory=lambda: os.getenv(
            "SHOONYA_HOST", "https://api.shoonya.com/NorenWClientAPI/"
        )
    )
    websocket: str = field(
        default_factory=lambda: os.getenv(
            "SHOONYA_WEBSOCKET", "wss://api.shoonya.com/NorenWSAPI/"
        )
    )

    # Historical data location (reused from the existing Breeze CSV dump)
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", str(DEFAULT_DATA_DIR)))
    )

    # Risk
    max_daily_loss_pts: float = field(
        default_factory=lambda: _float("RISK_MAX_DAILY_LOSS_PTS", 400.0)
    )
    max_lots: int = field(default_factory=lambda: _int("RISK_MAX_LOTS", 2))
    max_entries_per_day: int = field(
        default_factory=lambda: _int("RISK_MAX_ENTRIES_PER_DAY", 2)
    )
    no_new_entry_after: dt.time = field(
        default_factory=lambda: _hhmm("RISK_NO_NEW_ENTRY_AFTER", "14:45")
    )

    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _int("API_PORT", 8000))

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token and self.uid and self.account_id)

    @property
    def spot_dir(self) -> Path:
        return self.data_dir / "nifty_spot_1min"

    @property
    def options_dir(self) -> Path:
        return self.data_dir / "nifty_options_1min"


settings = Settings()
