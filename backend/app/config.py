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
    shoonya_password: str = field(
        default_factory=lambda: os.getenv("SHOONYA_PASSWORD", "").strip()
    )
    shoonya_totp_key: str = field(
        default_factory=lambda: os.getenv("SHOONYA_TOTP_KEY", "").strip()
    )

    # --- Broker identities discovered from the LIVE API (read-only probes) -----
    # These were NOT guessed: 'NSE_FNO' is rejected by this account with
    # 404 "Exchange Not enabled", while 'NFO' returns NIFTY option scrips.
    # Likewise the NIFTY spot index quotes on exchange NSE under token 26000
    # ("Nifty 50"); the legacy token '13' returns no data.
    fno_exchange: str = field(
        default_factory=lambda: os.getenv("SHOONYA_FNO_EXCHANGE", "NFO")
    )
    index_exchange: str = field(
        default_factory=lambda: os.getenv("SHOONYA_INDEX_EXCHANGE", "NSE")
    )
    index_token: str = field(
        default_factory=lambda: os.getenv("SHOONYA_INDEX_TOKEN", "26000")
    )
    root_symbol: str = field(
        default_factory=lambda: os.getenv("SHOONYA_ROOT_SYMBOL", "NIFTY")
    )
    # Weekdays (Mon=0..Sun=6) that can host a NIFTY weekly expiry. The live
    # probe listed weeklies on 29-SEP, 06-OCT and 13-OCT 2026 - all Tuesdays -
    # with no Thursday/Friday contracts at all, so only Tuesday is probed.
    # Override via SHOONYA_EXPIRY_WEEKDAYS (e.g. "1,3,4") if the exchange
    # changes its weekly-expiry schedule.
    expiry_weekdays: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(x) for x in os.getenv("SHOONYA_EXPIRY_WEEKDAYS", "1").split(",")
            if x.strip().lstrip("-").isdigit()
        )
    )
    # Product type used for live option orders (M = MIS intraday).
    product_type: str = field(
        default_factory=lambda: os.getenv("SHOONYA_PRODUCT_TYPE", "M")
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
    # A broker rejection that is not a policy refusal must not be retried
    # forever: after this many the strategy abandons the entry and closes the
    # day, instead of hammering the broker every poll.
    max_broker_entry_retries: int = field(
        default_factory=lambda: _int("RISK_MAX_ENTRY_RETRIES", 5)
    )

    # Strategy settings
    strategy_sl_pct: float | None = field(
        default_factory=lambda: (
            None if os.getenv("STRATEGY_SL_PCT", "").strip().lower() in ("none", "null", "false", "0")
            else (
                _float("STRATEGY_SL_PCT", 0.30)
                if os.getenv("STRATEGY_SL_PCT") is not None
                else (None if os.getenv("STRATEGY_COMBINED_SL_PCT") else 0.30)
            )
        )
    )
    strategy_combined_sl_pct: float | None = field(
        default_factory=lambda: (
            None if os.getenv("STRATEGY_COMBINED_SL_PCT", "").strip().lower() in ("none", "null", "false", "0", "")
            else _float("STRATEGY_COMBINED_SL_PCT", 0.80)
        )
    )
    strategy_sl_to_cost: bool = field(
        default_factory=lambda: _bool("STRATEGY_SL_TO_COST", True)
    )
    # When False (default), stop-loss is kept strictly in-memory and NOT placed to the broker.
    broker_sl_enabled: bool = field(
        default_factory=lambda: _bool("BROKER_SL_ENABLED", False)
    )
    strategy_target_pct: float | None = field(
        default_factory=lambda: (
            _float("STRATEGY_TARGET_PCT", 0.0)
            if os.getenv("STRATEGY_TARGET_PCT") not in (None, "", "none", "null")
            else None
        )
    )

    # --- order pricing ---------------------------------------------------------
    # Shoonya refuses MKT for API orders: "Rejected : ALGO_CHK: MKT Order type
    # not allowed for API order" (verified live on 2026-09-28). Phone-app
    # orders are exempt, so this only ever shows up on orders the bot sends.
    # Every API order therefore goes out as a LIMIT priced this far through the
    # spread, which fills promptly while the limit still caps the worst price.
    order_limit_buffer_pct: float = field(
        default_factory=lambda: _float("ORDER_LIMIT_BUFFER_PCT", 0.5)
    )
    # Aggressive buffer specifically for EXITS (SL / Square-off / Stop).
    # Since API rejects MKT orders ("ALGO_CHK"), exit orders are sent as
    # marketable LIMIT orders priced aggressively above the Ask (for BUY)
    # so the exchange matching engine fills them instantly at the best market price.
    order_exit_buffer_pct: float = field(
        default_factory=lambda: _float("ORDER_EXIT_BUFFER_PCT", 10.0)
    )
    order_exit_min_buffer_pts: float = field(
        default_factory=lambda: _float("ORDER_EXIT_MIN_BUFFER_PTS", 3.0)
    )
    order_tick_size: float = field(
        default_factory=lambda: _float("ORDER_TICK_SIZE", 0.05)
    )

    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _int("API_PORT", 8000))

    # --- trade journal (Excel) ------------------------------------------------
    # One workbook per trading day plus a running master, so day trades are
    # reportable without scraping logs. Lives under runtime/ (gitignored);
    # override with JOURNAL_DIR for a folder that syncs to backup/drive.
    journal_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("JOURNAL_DIR", str(BACKEND_DIR / "runtime" / "journal"))
        )
    )
    journal_enabled: bool = field(
        default_factory=lambda: _bool("JOURNAL_ENABLED", True)
    )
    # Brokerage charged per order, used for the journal's net P&L. Kept separate
    # from StraddleParams.brokerage (a backtest model input) because the live
    # number is whatever the broker actually bills - and paper-mode P&L should
    # be reported against the real cost.
    journal_brokerage_per_order: float = field(
        default_factory=lambda: _float("JOURNAL_BROKERAGE_PER_ORDER", 20.0)
    )

    # --- Telegram Bot ---------------------------------------------------------
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    telegram_enabled: bool = field(
        default_factory=lambda: _bool("TELEGRAM_ENABLED", True)
    )
    telegram_heartbeat_mins: int = field(
        default_factory=lambda: _int("TELEGRAM_HEARTBEAT_MINS", 30)
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token and self.uid and self.account_id)

    @property
    def has_auto_login_credentials(self) -> bool:
        return bool(
            (self.uid or self.client_id)
            and self.shoonya_password
            and self.shoonya_totp_key
            and self.client_id
            and self.secret_code
        )

    @property
    def spot_dir(self) -> Path:
        return self.data_dir / "nifty_spot_1min"

    @property
    def options_dir(self) -> Path:
        return self.data_dir / "nifty_options_1min"



def update_access_token(token: str, uid: str = "", account_id: str = "") -> None:
    """Dynamically update credentials on the singleton settings without restarting."""
    object.__setattr__(settings, "access_token", token)
    if uid:
        object.__setattr__(settings, "uid", uid)
    if account_id:
        object.__setattr__(settings, "account_id", account_id)

settings = Settings()
