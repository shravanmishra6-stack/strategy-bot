"""Automated zero-touch broker login for Shoonya using TOTP and headless browser.

Flow:
1. Reads Shoonya credentials (UID, Password, TOTP Secret Key, Client ID, Secret Code) from settings/.env.
2. Computes the current 6-digit TOTP via pyotp.
3. Launches a lightweight headless browser (Edge/Chrome/Chromium) to access the OAuth authorization page.
4. Fills #lgnusrid, #lgnpwd, #lgnotp and submits the login form.
5. Handles any OAuth consent prompt and captures the resulting auth_code from the redirect URL.
6. Calls exchange_and_save() to exchange auth_code for access_token, writes to .env, and refreshes the live broker session.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Any, Dict, Optional

import pyotp

from app.config import settings, update_access_token

log = logging.getLogger("broker.auto_login")


def _extract_code(text: str) -> Optional[str]:
    """Extract auth_code from a URL or raw text containing 'code='."""
    if not text:
        return None
    # Check if text is a URL with query params
    try:
        parsed = urllib.parse.urlparse(text)
        query = urllib.parse.parse_qs(parsed.query)
        if "code" in query and query["code"]:
            return query["code"][0].strip()
    except Exception:
        pass
    # Regex fallback
    m = re.search(r"(?:[?&]|^)code=([A-Za-z0-9_.\-]+)", text)
    if m:
        return m.group(1).strip()
    return None


def perform_auto_login() -> Dict[str, Any]:
    """Execute the full zero-touch headless login and session refresh.

    Returns dict with keys:
        - ok: bool
        - uid: str
        - account_id: str
        - access_token: str
        - limits: dict
    Raises RuntimeError or ValueError on configuration or login failure.
    """
    from scripts.oauth_login import exchange_and_save

    # 1. Validate required credentials
    uid = (settings.uid or settings.client_id or os.getenv("SHOONYA_UID", "")).strip()
    password = (getattr(settings, "shoonya_password", "") or os.getenv("SHOONYA_PASSWORD", "")).strip()
    totp_key = (getattr(settings, "shoonya_totp_key", "") or os.getenv("SHOONYA_TOTP_KEY", "")).strip()
    client_id = (settings.client_id or os.getenv("SHOONYA_CLIENT_ID", "")).strip()
    secret_code = (settings.secret_code or os.getenv("SHOONYA_SECRET_CODE", "")).strip()
    oauth_url = (settings.oauth_url or "https://api.shoonya.com/OAuthlogin/authorize/oauth").strip()

    missing = []
    if not uid:
        missing.append("SHOONYA_UID (or SHOONYA_CLIENT_ID)")
    if not password:
        missing.append("SHOONYA_PASSWORD")
    if not totp_key:
        missing.append("SHOONYA_TOTP_KEY")
    if not client_id:
        missing.append("SHOONYA_CLIENT_ID")
    if not secret_code:
        missing.append("SHOONYA_SECRET_CODE")

    if missing:
        raise ValueError(
            f"Missing required login credentials in .env: {', '.join(missing)}. "
            "Please configure SHOONYA_PASSWORD and SHOONYA_TOTP_KEY."
        )

    # 2. Compute TOTP token
    clean_totp_key = totp_key.replace(" ", "").upper()
    try:
        totp_gen = pyotp.TOTP(clean_totp_key)
        totp_code = totp_gen.now()
        log.info("Generated 6-digit TOTP for %s", uid)
    except Exception as e:
        raise ValueError(f"Invalid SHOONYA_TOTP_KEY: {e}") from e

    # 3. Launch headless browser via Playwright
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(f"Playwright not installed in virtual environment: {e}") from e

    auth_url = f"{oauth_url}?client_id={urllib.parse.quote(client_id)}"
    log.info("Starting headless OAuth flow at %s", auth_url)

    captured_code: Optional[str] = None

    with sync_playwright() as p:
        browser = None
        # Try available system browsers: Edge (built into Windows), Chrome, then bundled Chromium
        for channel in ("msedge", "chrome", None):
            try:
                launch_kwargs: Dict[str, Any] = {"headless": True}
                if channel:
                    launch_kwargs["channel"] = channel
                browser = p.chromium.launch(**launch_kwargs)
                log.info("Launched headless browser using channel=%s", channel or "chromium")
                break
            except Exception as e:
                log.debug("Could not launch browser with channel %s: %s", channel, e)

        if not browser:
            raise RuntimeError("Failed to launch headless browser (Edge/Chrome/Chromium).")

        context = browser.new_context()
        page = context.new_page()

        def _on_request(request):
            nonlocal captured_code
            code = _extract_code(request.url)
            if code:
                captured_code = code

        page.on("request", _on_request)

        try:
            page.goto(auth_url, wait_until="networkidle", timeout=30000)

            # Check if current URL already has code
            captured_code = captured_code or _extract_code(page.url)

            if not captured_code:
                # Wait for login inputs
                try:
                    page.wait_for_selector("#lgnusrid", timeout=12000)
                except Exception:
                    # Check if already redirected or code present
                    captured_code = captured_code or _extract_code(page.url) or _extract_code(page.content())
                    if not captured_code:
                        raise RuntimeError(f"Login page did not render expected inputs. Current URL: {page.url}")

                # Fill credentials and TOTP
                page.fill("#lgnusrid", uid)
                page.fill("#lgnpwd", password)
                page.fill("#lgnotp", totp_code)

                log.info("Submitted credentials and TOTP on Shoonya login page")

                # Submit form
                login_btn = page.get_by_role("button", name="LOGIN")
                if login_btn.count() > 0:
                    login_btn.first.click()
                else:
                    page.click('button[type="submit"]')

                # Wait for redirect or code
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    captured_code = captured_code or _extract_code(page.url) or _extract_code(page.content())
                    if captured_code:
                        break

                    # Check for an Authorize / Approve button if consent screen appears
                    for btn_name in ("AUTHORIZE", "APPROVE", "ALLOW", "CONTINUE", "SUBMIT"):
                        auth_btn = page.get_by_role("button", name=re.compile(btn_name, re.I))
                        if auth_btn.count() > 0 and auth_btn.first.is_visible():
                            log.info("Clicking '%s' consent button", btn_name)
                            auth_btn.first.click()
                            page.wait_for_timeout(1000)
                            break

                    # Check for visible error messages on the page
                    err_elem = page.locator(".error, .alert, .text-danger, #errorMsg")
                    if err_elem.count() > 0 and err_elem.first.is_visible():
                        err_text = err_elem.first.inner_text().strip()
                        if err_text:
                            raise RuntimeError(f"Shoonya login error: {err_text}")

            if not captured_code:
                # Final check on page URL and content
                captured_code = _extract_code(page.url) or _extract_code(page.content())

        finally:
            browser.close()

    if not captured_code:
        raise RuntimeError("Login completed but no authorization code was received from Shoonya.")

    log.info("Captured Shoonya auth_code: %s... (len=%d)", captured_code[:6], len(captured_code))

    # 4. Exchange auth_code for access_token and update .env
    res = exchange_and_save(captured_code, uid)
    access_token = res["access_token"]
    actual_uid = res.get("uid") or uid
    account_id = res.get("account_id") or ""

    # 5. Hot-reload config and refresh active broker client
    update_access_token(access_token, actual_uid, account_id)
    from app.broker.noren_client import client
    client.refresh_session(access_token, actual_uid, account_id)

    # 6. Retrieve account limits to verify live trading readiness
    limits_info = {}
    try:
        limits = client.get_limits()
        if limits:
            limits_info = limits
    except Exception as e:
        log.warning("Could not fetch limits after login: %s", e)

    return {
        "ok": True,
        "uid": actual_uid,
        "account_id": account_id,
        "access_token": access_token,
        "limits": limits_info,
        "session_ok": client.session_ok,
    }
