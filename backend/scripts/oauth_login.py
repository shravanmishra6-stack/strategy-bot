"""One-shot Shoonya OAuth login helper.

Run interactively:
    python -m scripts.oauth_login

Flow (matches Shoonya_oAuthAPI-py docs):
  1. Prints the OAuth URL built from SHOONYA_CLIENT_ID + SHOONYA_SECRET_CODE.
  2. You open it in the browser (already logged into the Shoonya web app),
     approve, and copy the returned auth_code.
  3. Paste the auth_code here; it is exchanged for an access_token via the
     SDK and validated with get_limits before being written to .env.

Token lifetime is limited (per docs the refresh_token has no usable refresh
endpoint yet) - when the bot reports SessionExpired, re-run this script.
"""
from __future__ import annotations

import getpass
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _read_env_value(name: str) -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text().splitlines():
        m = re.match(rf"^{name}=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"')
    return ""


def _write_env_values(values: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    for key, val in values.items():
        pat = re.compile(rf"^{key}=.*$")
        new = f"{key}={val}"
        for i, ln in enumerate(lines):
            if pat.match(ln.strip()):
                lines[i] = new
                break
        else:
            lines.append(new)
    ENV_PATH.write_text("\n".join(lines) + "\n")


def main() -> int:
    from app.config import settings  # after .env load

    client_id = settings.client_id or _read_env_value("SHOONYA_CLIENT_ID")
    secret = settings.secret_code or _read_env_value("SHOONYA_SECRET_CODE")
    if not client_id or not secret:
        print("Set SHOONYA_CLIENT_ID and SHOONYA_SECRET_CODE in .env first.")
        return 1

    from NorenRestApiPy.NorenApi import NorenApi

    api = NorenApi(host=settings.host, websocket=settings.websocket)
    url = api.getOAuthURL(client_id, secret)
    print("\n1) Open this URL in your browser (Shoonya web already logged in):\n\n   ", url, "\n")
    auth_code = input("2) Paste the auth_code you received: ").strip()
    if not auth_code:
        print("No auth_code supplied.")
        return 1

    print("3) Exchanging auth_code for access token ...")
    resp = api.getAccessToken(client_id, secret, auth_code)
    if not resp or resp.get("stat") != "Ok":
        print("Token exchange FAILED:", resp)
        return 2

    token = resp.get("accesstoken")
    uid = resp.get("uid") or client_id
    account = resp.get("accountId") or client_id
    refresh = resp.get("refreshtoken", "")
    print(f"   token OK (uid={uid}) - validating with get_limits ...")

    api.injectOAuthHeader(token, uid, account)
    limits = api.get_limits()
    if not limits or limits.get("stat") == "Not_Ok":
        print("   token did not validate:", limits)
        return 3
    print("   get_limits OK. Writing .env ...")

    _write_env_values({
        "SHOONYA_ACCESS_TOKEN": token,
        "SHOONYA_UID": uid,
        "SHOONYA_ACCOUNT_ID": account,
        "SHOONYA_REFRESH_TOKEN": refresh,
    })
    print("\nDone. Start the bot; when you see 'SessionExpired', re-run this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
