"""Tests for Telegram Bot notifier, state observer, and command handling."""
import pytest
from unittest.mock import MagicMock, patch
from app.session import SessionState
from app.telegram_bot import (
    TelegramNotifier,
    TelegramStateObserver,
    TelegramCommandPoller,
    TelegramBotManager,
    _esc,
)


def test_esc_helper():
    assert _esc("test <script> & 'hello'") == "test &lt;script&gt; &amp; &#x27;hello&#x27;"
    assert _esc(None) == ""
    assert _esc(123) == "123"


def test_notifier_inactive_when_unconfigured():
    notif = TelegramNotifier(token="", chat_id="", enabled=True)
    assert not notif.is_configured
    assert not notif.is_active
    # Must fail gracefully without raising
    assert notif.send_message("Hello") is False


def test_notifier_active_when_configured():
    notif = TelegramNotifier(token="123456:ABC-DEF", chat_id="987654", enabled=True)
    assert notif.is_configured
    assert notif.is_active


def test_notifier_disabled_flag():
    notif = TelegramNotifier(token="123456:ABC-DEF", chat_id="987654", enabled=False)
    assert notif.is_configured
    assert not notif.is_active
    assert notif.send_message("Hello") is False


@patch("httpx.Client.post")
def test_notifier_send_message_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    notif = TelegramNotifier(token="123456:ABC-DEF", chat_id="987654", enabled=True)
    ok = notif.send_message("<b>Test Alert</b>")
    assert ok is True
    assert notif.last_sent_at is not None
    assert notif.last_error is None


@patch("httpx.Client.post")
def test_notifier_send_message_failure_handled(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": "Chat not found"}'
    mock_post.return_value = mock_resp

    notif = TelegramNotifier(token="123456:ABC-DEF", chat_id="987654", enabled=True)
    ok = notif.send_message("<b>Test Alert</b>")
    assert ok is False
    assert "Chat not found" in notif.last_error


def test_notifier_formatting_methods():
    notif = TelegramNotifier(token="fake", chat_id="fake", enabled=True)
    with patch.object(notif, "send_message", return_value=True) as mock_send:
        # 1. Server started
        notif.notify_server_started("127.0.0.1", 8000, paper_mode=True, session_ok=True)
        assert "PAPER TRADING" in mock_send.call_args[0][0]

        # 2. Session start
        notif.notify_session_start("2026-10-02", paper_mode=False, armed=True, lots=2)
        assert "ARMED" in mock_send.call_args[0][0]

        # 3. Resolved
        notif.notify_resolved({
            "spot": 25000.0,
            "atm": 25000,
            "expiry": "2026-10-08",
            "lot_size": 65,
            "legs": [{"right": "CE", "symbol": "NIFTY25000CE"}, {"right": "PE", "symbol": "NIFTY25000PE"}]
        })
        assert "25,000.00" in mock_send.call_args[0][0]

        # 4. Entry
        notif.notify_entry(
            spot=25000.0, atm=25000, lots=1,
            legs={
                "CE": {"entry_price": 100.0, "sl": 130.0},
                "PE": {"entry_price": 110.0, "sl": 143.0},
            },
            combined={"entry": 210.0, "sl_level": 378.0}
        )
        assert "STRADDLE ENTRY EXECUTED" in mock_send.call_args[0][0]

        # 5. Leg exit
        notif.notify_leg_exit("CE", "NIFTY25000CE", "SL", 130.0, -30.0, -1950.0)
        assert "LEG EXIT: 🔵 CE" in mock_send.call_args[0][0]

        # 6. Combined SL
        notif.notify_combined_sl(380.0, 378.0, -168.0, -10920.0)
        assert "COMBINED STOP-LOSS" in mock_send.call_args[0][0]

        # 7. Target hit
        notif.notify_target_hit(105.0, 6825.0)
        assert "TARGET PROFIT" in mock_send.call_args[0][0]

        # 8. Square off
        notif.notify_squareoff("EOD", 45.0, 2925.0)
        assert "SQUARE-OFF COMPLETED" in mock_send.call_args[0][0]

        # 9. Session end
        notif.notify_session_end("DONE", 45.0, 2925.0, 2, "journal.xlsx")
        assert "TRADING SESSION FINISHED" in mock_send.call_args[0][0]

        # 10. Arm changed
        notif.notify_arm_changed(True)
        assert "LIVE TRADING ARMED" in mock_send.call_args[0][0]

        # 11. Alert
        notif.notify_alert("Test Alert", "Detail info", level="ERROR")
        assert "Test Alert" in mock_send.call_args[0][0]


def test_observer_triggers_on_state_changes():
    notif = TelegramNotifier(token="fake", chat_id="fake", enabled=True)
    obs = TelegramStateObserver(notifier=notif)

    with patch.object(notif, "notify_session_start") as m_start, \
         patch.object(notif, "notify_resolved") as m_res, \
         patch.object(notif, "notify_entry") as m_entry, \
         patch.object(notif, "notify_leg_exit") as m_exit, \
         patch.object(notif, "notify_session_end") as m_end, \
         patch.object(notif, "notify_arm_changed") as m_arm, \
         patch.object(notif, "notify_alert") as m_alert:

        # 1. Starting
        obs.on_state_update(old={}, new={"status": "starting", "day": "2026-10-02"}, changes={"status": "starting"})
        assert m_start.call_count == 1

        # 2. Resolved
        obs.on_state_update(old={"status": "starting"}, new={"status": "resolved", "resolved": {"atm": 25000}, "day": "2026-10-02"}, changes={"status": "resolved"})
        assert m_res.call_count == 1

        # 3. Entry
        legs = {
            "CE": {"status": "OPEN", "entry_price": 100.0, "symbol": "NIFTY_CE"},
            "PE": {"status": "OPEN", "entry_price": 100.0, "symbol": "NIFTY_PE"},
        }
        obs.on_state_update(old={"phase": "WAIT_ENTRY"}, new={"phase": "IN_POSITION", "legs": legs, "day": "2026-10-02"}, changes={"phase": "IN_POSITION"})
        assert m_entry.call_count == 1

        # 4. Leg Exit
        legs_after = {
            "CE": {"status": "CLOSED", "exit_reason": "SL", "exit_price": 130.0, "pnl_pts": -30.0, "pnl_rupees": -1950.0, "symbol": "NIFTY_CE"},
            "PE": {"status": "OPEN", "entry_price": 100.0, "symbol": "NIFTY_PE"},
        }
        obs.on_state_update(old={"phase": "IN_POSITION"}, new={"phase": "IN_POSITION", "legs": legs_after, "day": "2026-10-02"}, changes={"legs": legs_after})
        assert m_exit.call_count == 1
        assert m_exit.call_args[0][0] == "CE"
        assert m_exit.call_args[0][2] == "SL"

        # 5. Arming change
        obs.on_state_update(old={"live_armed": False}, new={"live_armed": True}, changes={"live_armed": True})
        assert m_arm.call_count == 1
        assert m_arm.call_args[0][0] is True

        # 6. Session Expired Alert
        obs.on_state_update(old={}, new={"status": "session_expired", "error": "Token expired", "day": "2026-10-02"}, changes={"status": "session_expired"})
        assert m_alert.call_count == 1

        # 7. Session Done
        obs.on_state_update(old={"status": "running"}, new={"status": "done", "risk": {"daily_pnl_pts": 10.0, "daily_pnl_rupees": 650.0}, "fills": [], "day": "2026-10-02"}, changes={"status": "done"})
        assert m_end.call_count == 1


def test_observer_no_false_leg_exit_on_pre_entry_abort():
    """When a session finishes or aborts before entry, no spurious leg exit alerts must be emitted."""
    notif = TelegramNotifier(token="fake", chat_id="fake", enabled=True)
    obs = TelegramStateObserver(notifier=notif)

    with patch.object(notif, "notify_leg_exit") as m_exit:
        # Pre-entry state with legs that have is_open=False / status=None and NO entry
        legs_pre_entry = {
            "CE": {"is_open": False, "symbol": "NIFTY_CE", "entry": None, "entry_price": None},
            "PE": {"is_open": False, "symbol": "NIFTY_PE", "entry": None, "entry_price": None},
        }
        # Session aborts / finishes before entry
        obs.on_state_update(
            old={"status": "running", "phase": "WAIT_ENTRY"},
            new={"status": "done", "phase": "DONE", "legs": legs_pre_entry, "day": "2026-10-06"},
            changes={"status": "done", "phase": "DONE"}
        )
        assert m_exit.call_count == 0


def test_command_poller_authorization_and_dispatch():
    notif = TelegramNotifier(token="fake", chat_id="111222", enabled=True)
    get_state = MagicMock(return_value={
        "status": "running", "phase": "IN_POSITION", "spot": 25000.0, "atm": 25000,
        "paper_mode": True, "live_armed": False, "lots": 1,
        "risk": {"daily_pnl_pts": 15.0, "daily_pnl_rupees": 975.0, "entries_today": 1},
        "combined": {"pnl_pts": 15.0, "pnl_rupees": 975.0},
        "legs": {"CE": {"status": "OPEN", "entry_price": 100.0, "last_price": 85.0, "sl": 130.0, "pnl_pts": 15.0}},
    })
    start_fn = MagicMock()
    stop_fn = MagicMock()
    arm_fn = MagicMock()
    disarm_fn = MagicMock()

    poller = TelegramCommandPoller(
        notifier=notif,
        get_state_fn=get_state,
        start_session_fn=start_fn,
        stop_session_fn=stop_fn,
        arm_fn=arm_fn,
        disarm_fn=disarm_fn,
    )

    with patch.object(notif, "send_message") as m_send:
        # Unauthorized message: should not call authorized notifier
        poller._handle_message({"chat": {"id": 999999}, "text": "/status"})
        assert not m_send.called

        # Authorized /status
        poller._handle_message({"chat": {"id": 111222}, "text": "/status"})
        assert "Bot Status Overview" in m_send.call_args[0][0]

        # Authorized /pnl
        poller._handle_message({"chat": {"id": 111222}, "text": "/pnl"})
        assert "P&L Summary" in m_send.call_args[0][0]

        # Authorized /positions
        poller._handle_message({"chat": {"id": 111222}, "text": "/positions"})
        assert "Position Breakdown" in m_send.call_args[0][0]

        # Authorized /arm
        poller._handle_message({"chat": {"id": 111222}, "text": "/arm"})
        assert arm_fn.called

        # Authorized /disarm
        poller._handle_message({"chat": {"id": 111222}, "text": "/disarm"})
        assert disarm_fn.called

        # Authorized /start_session on weekday
        with patch("app.telegram_bot.dt.date") as mock_date:
            mock_date.today.return_value.weekday.return_value = 2  # Wednesday
            poller._handle_message({"chat": {"id": 111222}, "text": "/start_session"})
            assert start_fn.called

        # Authorized /squareoff
        poller._handle_message({"chat": {"id": 111222}, "text": "/squareoff"})
        assert stop_fn.called


def test_start_session_weekend_guard():
    notif = TelegramNotifier(token="fake", chat_id="111222", enabled=True)
    start_fn = MagicMock()
    poller = TelegramCommandPoller(
        notifier=notif,
        get_state_fn=MagicMock(return_value={}),
        start_session_fn=start_fn,
    )
    with patch.object(notif, "send_message") as m_send, \
         patch("app.telegram_bot.dt.date") as mock_date:
        mock_date.today.return_value.weekday.return_value = 5  # Saturday

        # 1. Plain /start_session on weekend: refuses with informative message
        poller._handle_message({"chat": {"id": 111222}, "text": "/start_session"})
        assert not start_fn.called
        assert any("No Trading Session on Weekends" in str(call) for call in m_send.call_args_list)

        # 2. Force start on weekend: proceeds
        poller._handle_message({"chat": {"id": 111222}, "text": "/start_session force"})
        assert start_fn.called


def test_cmd_restart_and_kill():
    notif = TelegramNotifier(token="fake", chat_id="111222", enabled=True)
    poller = TelegramCommandPoller(notifier=notif, get_state_fn=MagicMock())

    with patch.object(notif, "send_message") as m_send, \
         patch("subprocess.Popen") as mock_popen:

        # Test /restart
        poller._handle_message({"chat": {"id": 111222}, "text": "/restart"})
        assert any("Restarting server" in str(call) for call in m_send.call_args_list)
        assert mock_popen.called
        assert "restart_helper.ps1" in str(mock_popen.call_args[0][0])

        mock_popen.reset_mock()
        m_send.reset_mock()

        # Test /kill
        poller._handle_message({"chat": {"id": 111222}, "text": "/kill"})
        assert any("Shutting down server" in str(call) for call in m_send.call_args_list)
        assert mock_popen.called
        assert "taskkill" in str(mock_popen.call_args[0][0])



def test_session_state_listeners():
    st = SessionState()
    received = []

    def callback(old, new, changes):
        received.append((old.get("status"), new.get("status"), changes))

    st.add_listener(callback)
    st.update(status="starting")
    st.update(status="resolved")

    assert len(received) == 2
    assert received[0] == ("idle", "starting", {"status": "starting"})
    assert received[1] == ("starting", "resolved", {"status": "resolved"})

    st.remove_listener(callback)
    st.update(status="running")
    assert len(received) == 2
