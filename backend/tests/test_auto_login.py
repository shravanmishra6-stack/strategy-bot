from unittest.mock import MagicMock, patch
import pytest

from app.broker.auto_login import _extract_code, perform_auto_login
from app.config import settings
from app.telegram_bot import TelegramCommandPoller, TelegramNotifier


def test_extract_code():
    assert _extract_code("https://api.shoonya.com/NorenWClientAPI/GetAuthCode?code=MY_AUTH_CODE_123") == "MY_AUTH_CODE_123"
    assert _extract_code("https://example.com/redirect?state=1&code=SECRET_CODE_456&uid=FA123") == "SECRET_CODE_456"
    assert _extract_code("code=SIMPLE_CODE_789") == "SIMPLE_CODE_789"
    assert _extract_code("?code=PREFIXED_CODE") == "PREFIXED_CODE"
    assert _extract_code("") is None
    assert _extract_code("invalid_text_without_code") is None


def test_perform_auto_login_missing_credentials(monkeypatch):
    old_pwd = settings.shoonya_password
    old_totp = settings.shoonya_totp_key
    monkeypatch.setenv("SHOONYA_PASSWORD", "")
    monkeypatch.setenv("SHOONYA_TOTP_KEY", "")
    try:
        object.__setattr__(settings, "shoonya_password", "")
        object.__setattr__(settings, "shoonya_totp_key", "")
        with pytest.raises(ValueError, match="Missing required login credentials"):
            perform_auto_login()
    finally:
        object.__setattr__(settings, "shoonya_password", old_pwd)
        object.__setattr__(settings, "shoonya_totp_key", old_totp)


def test_telegram_login_with_explicit_code():
    notifier = TelegramNotifier(token="fake", chat_id="12345", enabled=True)
    messages = []
    notifier.send_message = lambda text, **kwargs: messages.append(text)

    poller = TelegramCommandPoller(notifier=notifier, get_state_fn=MagicMock())

    with patch("scripts.oauth_login.exchange_and_save") as mock_exchange, \
         patch("app.broker.noren_client.client.refresh_session") as mock_refresh, \
         patch("app.broker.noren_client.client.get_limits", return_value={"cash": "100000"}):

        mock_exchange.return_value = {
            "access_token": "tok_123",
            "uid": "USER1",
            "account_id": "ACT1"
        }

        orig_uid = settings.uid
        # User sends code directly
        poller._handle_message({
            "chat": {"id": 12345},
            "text": "/login https://trade.shoonya.com/redirect?code=TEST_AUTH_CODE"
        })

        mock_exchange.assert_called_once_with("TEST_AUTH_CODE", orig_uid)
        mock_refresh.assert_called_once_with("tok_123", "USER1", "ACT1")
        assert any("Broker Login Successful" in m for m in messages)
        assert any("100000" in m for m in messages)


def test_telegram_login_missing_auto_credentials():
    notifier = TelegramNotifier(token="fake", chat_id="12345", enabled=True)
    messages = []
    notifier.send_message = lambda text, **kwargs: messages.append(text)

    poller = TelegramCommandPoller(notifier=notifier, get_state_fn=MagicMock())
    old_pwd = settings.shoonya_password
    old_totp = settings.shoonya_totp_key
    try:
        object.__setattr__(settings, "shoonya_password", "")
        object.__setattr__(settings, "shoonya_totp_key", "")

        poller._handle_message({
            "chat": {"id": 12345},
            "text": "/login"
        })

        assert any("SHOONYA_PASSWORD" in m for m in messages)
        assert any("SHOONYA_TOTP_KEY" in m for m in messages)
    finally:
        object.__setattr__(settings, "shoonya_password", old_pwd)
        object.__setattr__(settings, "shoonya_totp_key", old_totp)
