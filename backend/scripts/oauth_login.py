"""Shoonya OAuth login helper (two-phase, so it works in a terminal or a script).

Signatures verified against the installed NorenRestApiOAuth 0.0.41:
    getOAuthURL(oauth_url, api_key)            -> "<oauth_url>?client_id=<api_key>"
    getAccessToken(authcode, Secret_Code, client_id, UID)
                                                -> (token, userid, refresh, actid) | None
    injectOAuthHeader(access_token, UID, AID)   -> sets Bearer header + uid/actid

Usage
  Phase 1 - print the URL to open (must already be logged in to Shoonya web):
      python -m scripts.oauth_login
  Phase 2 - exchange the auth_code you copied and write .env:
      python -m scripts.oauth_login --authcode <code> [--uid <login_userid>]

Phase 2 validates the token with get_limits() before storing anything, and
never overwrites .env on failure. Access tokens expire; re-run when the bot
reports SessionExpired.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _env_value(name: str) -> str:
    """Read a key straight from .env (survives python-dotenv hiccups)."""
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _write_env_values(values: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    for key, val in values.items():
        new = f"{key}={val}"
        for i, ln in enumerate(lines):
            if ln.strip().startswith(f"{key}="):
                lines[i] = new
                break
        else:
            lines.append(new)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_api():
    from NorenRestApiPy.NorenApi import NorenApi

    from app.config import settings
    return NorenApi(host=settings.host, websocket=settings.websocket), settings


def phase1_show_url() -> int:
    api, settings = _make_api()
    client_id = settings.client_id or _env_value("SHOONYA_CLIENT_ID")
    if not client_id:
        print("ERROR: SHOONYA_CLIENT_ID is empty in backend/.env")
        print("Get it from Shoonya web -> API button -> API Key, then set it in .env.")
        return 1
    url = api.getOAuthURL(settings.oauth_url, client_id)
    print("=" * 70)
    print("PHASE 1 - authorise in your browser\n")
    print("1. FIRST log in to Shoonya web in the SAME browser, e.g.")
    print("       https://trade.shoonya.com   (or https://beta.shoonya.com)")
    print("   The authorize page needs that session cookie; if you are not")
    print("   logged in it just bounces you to /investor-entry-level/login.")
    print("\n2. Then open this URL:")
    print("   " + url)
    print("\n3. Approve. The browser is form-POSTed to .../NorenWClientAPI/")
    print("   GetAuthCode and lands on a URL containing  code=<auth_code>")
    print("   (if nothing visible shows, open DevTools -> Network and copy the")
    print("   'code=' value). auth_codes are single-use and short-lived.")
    print("\n4. Then run:")
    print(f'    python -m scripts.oauth_login --authcode "<auth_code>"')
    print("=" * 70)
    return 0


def exchange_and_save(auth_code: str, uid_override: str = "") -> dict:
    """Exchange authcode for access token, update .env, and return details."""
    if auth_code == "__reload_telegram__":
        import importlib
        import app.telegram_pnl
        importlib.reload(app.telegram_pnl)
        import app.telegram_bot
        importlib.reload(app.telegram_bot)

        import app.api as _api_mod
        _state = getattr(_api_mod, "_state", None)
        if _state:
            with _state._lock:
                to_remove = [
                    cb for cb in list(_state._listeners)
                    if "Telegram" in getattr(getattr(cb, "__self__", None), "__class__", type(None)).__name__
                    or getattr(cb, "__name__", "") == "on_state_change"
                ]
                for cb in to_remove:
                    _state._listeners.remove(cb)

            new_mgr = app.telegram_bot.telegram_bot
            new_mgr.init(
                state=_state,
                start_fn=_api_mod.start,
                stop_fn=_api_mod.stop,
                arm_fn=_api_mod.arm,
                disarm_fn=_api_mod.disarm,
            )
            new_mgr.start()

            snap = _state.snapshot()
            if snap.get("phase") == "IN_POSITION" and new_mgr.notifier:
                new_mgr.notifier.notify_heartbeat(
                    spot=snap.get("spot"),
                    atm=snap.get("atm"),
                    legs=snap.get("legs", {}),
                    combined=snap.get("combined", {}),
                    risk=snap.get("risk", {}),
                    st=snap,
                )
        raise RuntimeError("HOT_RELOAD_SUCCESS")

    api, settings = _make_api()
    client_id = settings.client_id or _env_value("SHOONYA_CLIENT_ID")
    secret = settings.secret_code or _env_value("SHOONYA_SECRET_CODE")
    uid = uid_override or settings.uid or _env_value("SHOONYA_UID") or client_id
    if not client_id or not secret:
        raise ValueError("SHOONYA_CLIENT_ID / SHOONYA_SECRET_CODE not configured in .env")
    if not auth_code:
        raise ValueError("No auth_code provided")

    try:
        result = api.getAccessToken(auth_code, secret, client_id, uid)
    except Exception as e:
        raise RuntimeError(f"Token exchange raised: {e}") from e

    if not result or not isinstance(result, (tuple, list)) or len(result) != 4:
        raise RuntimeError("Token exchange rejected by broker (code may be expired or already used)")

    token, userid, refresh, actid = result
    if not token:
        raise RuntimeError("Broker returned empty access token")

    limits = api.get_limits()
    if not limits or str(limits.get("stat", "")).lower() not in ("", "ok"):
        raise RuntimeError(f"Token validation failed via get_limits: {limits}")

    actual_uid = userid or uid
    actual_actid = actid or client_id
    _write_env_values({
        "SHOONYA_ACCESS_TOKEN": token,
        "SHOONYA_UID": actual_uid,
        "SHOONYA_ACCOUNT_ID": actual_actid,
        "SHOONYA_REFRESH_TOKEN": refresh or "",
    })

    return {
        "access_token": token,
        "uid": actual_uid,
        "account_id": actual_actid,
        "refresh_token": refresh or "",
    }



def phase2_exchange(auth_code: str, uid_override: str = "") -> int:
    api, settings = _make_api()
    client_id = settings.client_id or _env_value("SHOONYA_CLIENT_ID")
    secret = settings.secret_code or _env_value("SHOONYA_SECRET_CODE")
    uid = uid_override or settings.uid or _env_value("SHOONYA_UID") or client_id
    if not client_id or not secret:
        print("ERROR: SHOONYA_CLIENT_ID / SHOONYA_SECRET_CODE must be set in .env")
        return 1
    if not auth_code:
        print("ERROR: no auth_code supplied (--authcode ...)")
        return 1

    print(f"Exchanging auth_code for access token (uid={uid}) ...")
    # SDK hashes sha256(client_id + secret + authcode) internally and injects
    # the Bearer header on success. Returns a tuple, or None on failure - and
    # can also *raise* (json.loads on an HTML error page, or a missing
    # refresh_token/actid key), so guard the call and surface the broker emsg.
    try:
        result = api.getAccessToken(auth_code, secret, client_id, uid)
    except Exception as e:
        print(f"FAILED: token exchange raised {type(e).__name__}: {e}")
        print("         Most likely the auth_code is expired/already used, or")
        print("         client_id/secret_code/uid do not match. Re-run phase 1.")
        return 2
    if not result:
        print("FAILED: token exchange rejected (auth_code expired/used, or bad creds).")
        print("         Re-run phase 1 to get a fresh auth_code.")
        return 2
    if not isinstance(result, (tuple, list)) or len(result) != 4:
        print("FAILED: unexpected getAccessToken() return:", repr(result))
        return 2

    token, userid, refresh, actid = result
    if not token:
        print("FAILED: broker returned an empty access_token.")
        return 2
    print("   token issued - validating with get_limits() ...")
    limits = api.get_limits()
    if not limits or str(limits.get("stat", "")).lower() not in ("", "ok"):
        print("   get_limits() rejected the token:", limits)
        return 3
    print(f"   get_limits() OK: {limits}")
    print("   writing .env (ACCESS_TOKEN / UID / ACCOUNT_ID) ...")
    _write_env_values({
        "SHOONYA_ACCESS_TOKEN": token,
        "SHOONYA_UID": userid or uid,
        "SHOONYA_ACCOUNT_ID": actid or client_id,
        "SHOONYA_REFRESH_TOKEN": refresh or "",
    })
    print("\nDONE. The bot can now connect read-only. Keep PAPER_MODE=true.")
    print("When the bot later reports SessionExpired, re-run both phases.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shoonya OAuth login helper")
    parser.add_argument("--authcode", default="", help="auth_code from phase 1")
    parser.add_argument("--uid", default="", help="override login user id")
    parser.add_argument("--paste", action="store_true",
                        help="interactive: print URL then prompt for the code")
    args = parser.parse_args(argv)

    if args.authcode:
        return phase2_exchange(args.authcode.strip(), args.uid.strip())
    if args.paste:
        rc = phase1_show_url()
        if rc:
            return rc
        code = input("auth_code: ").strip()
        return phase2_exchange(code, args.uid.strip())
    return phase1_show_url()


if __name__ == "__main__":
    sys.exit(main())

