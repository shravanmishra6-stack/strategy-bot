"""Lightweight Telegram Watchdog / Supervisor for Strategy Bot.

Runs 24/7 independently of the trading engine, enabling complete remote management:
  /start_server  - Boots up the uvicorn trading server in the background
  /kill          - Shuts down the trading server (supervisor stays alive!)
  /restart       - Restarts the server cleanly
  /status        - Market & server status overview
  /pnl           - Realized & Unrealized P&L
  /positions     - Option legs breakdown
  /start_session - Start trading session (guards against weekends unless force)
  /stop_session  - Emergency exit & stop session
  /arm           - Arm live broker trading
  /disarm        - Disarm live trading
  /login         - Zero-touch or interactive broker login
  /help          - Command menu
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings
from app.telegram_pnl import compute_session_stats, format_spot_line

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot_supervisor")

PORT = int(os.getenv("API_PORT", "8000"))
BASE_URL = f"http://127.0.0.1:{PORT}"


def _esc(val: Any) -> str:
    if val is None:
        return ""
    return html.escape(str(val))


class TelegramSupervisor:
    def __init__(self):
        self.token = settings.telegram_bot_token.strip()
        self.chat_id = settings.telegram_chat_id.strip()
        self.api_base = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.last_update_id = 0
        self._last_heartbeat = time.time()
        self._http = httpx.Client(timeout=10.0)

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def is_authorized(self, chat_id: Any) -> bool:
        if not self.chat_id:
            return False
        allowed = [c.strip() for c in self.chat_id.split(",") if c.strip()]
        return str(chat_id) in allowed

    def send_message(self, text: str) -> bool:
        if not self.is_configured():
            return False
        try:
            resp = self._http.post(f"{self.api_base}/sendMessage", json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            return resp.status_code == 200
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False

    def check_heartbeat(self) -> None:
        """Send periodic heartbeat while IN_POSITION."""
        mins = getattr(settings, "telegram_heartbeat_mins", 15) or 15
        interval_secs = max(60, int(mins) * 60)
        now = time.time()
        if now - self._last_heartbeat < interval_secs:
            return

        st = self.get_server_status()
        if not st or st.get("phase") != "IN_POSITION":
            return

        self._last_heartbeat = now
        stats = compute_session_stats(st)
        ce = stats["legs"].get("CE", {})
        pe = stats["legs"].get("PE", {})
        ce_ltp = ce.get("ltp")
        pe_ltp = pe.get("ltp")
        ce_str = f"{ce_ltp:.2f}" if ce_ltp is not None else "-"
        pe_str = f"{pe_ltp:.2f}" if pe_ltp is not None else "-"

        open_pts = stats["open_pts"]
        open_rs = stats["open_rs"]
        tot_pts = stats["total_pts"]
        tot_rs = stats["total_rs"]
        pnl_icon = "🟢" if open_pts >= 0 else "🔴"
        sign = "+" if open_pts >= 0 else ""
        tot_sign = "+" if tot_pts >= 0 else ""

        spot = st.get("spot")
        atm = st.get("atm")
        spot_str = f"{spot:,.2f}" if spot else "-"
        atm_str = str(atm) if atm else "-"

        time_now = dt.datetime.now().strftime("%H:%M")
        spot_header = format_spot_line(stats, atm)
        msg = (
            f"📊 <b>Heartbeat Update ({time_now})</b>\n"
            f"{spot_header}\n"
            f"🔵 CE LTP: <b>{ce_str}</b> | 🔴 PE LTP: <b>{pe_str}</b>\n"
            f"{pnl_icon} Position P&L: <b>{sign}{open_pts:.2f} pts</b> ({sign}₹{open_rs:,.2f})\n"
            f"💼 Day Total P&L: <b>{tot_sign}{tot_pts:.2f} pts</b> ({tot_sign}₹{tot_rs:,.2f})"
        )
        self.send_message(msg)

    # --- Server Process Helpers ------------------------------------------------

    def get_server_pid(self) -> Optional[int]:
        """Check if port is listening and return PID."""
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            out = res.stdout.strip()
            if out and out.isdigit():
                return int(out)
        except Exception:
            pass
        return None

    def get_server_status(self) -> Optional[Dict[str, Any]]:
        """Query /api/status. Returns status dict or None if offline."""
        try:
            r = self._http.get(f"{BASE_URL}/api/status", timeout=2.5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def start_server(self) -> str:
        """Start uvicorn server in background."""
        pid = self.get_server_pid()
        if pid:
            return f"🟢 Server is ALREADY running at {BASE_URL} (PID: <code>{pid}</code>)."

        python_bin = "python"
        venv_py = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_py.exists():
            python_bin = str(venv_py)

        runtime_dir = BACKEND_DIR / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.date.today().isoformat()
        stdout_file = runtime_dir / f"server-{stamp}.log"
        stderr_file = runtime_dir / f"server-{stamp}.err"

        # Start with TELEGRAM_POLLER_DISABLED=true so supervisor owns polling
        env = os.environ.copy()
        env["TELEGRAM_POLLER_DISABLED"] = "true"

        DETACHED = 0x00000008
        CREATE_NEW = 0x00000200
        flags = DETACHED | CREATE_NEW

        with open(stdout_file, "a", encoding="utf-8") as out_f, \
             open(stderr_file, "a", encoding="utf-8") as err_f:
            subprocess.Popen(
                [python_bin, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=out_f,
                stderr=err_f,
                creationflags=flags,
                close_fds=True,
            )

        # Wait up to 20s for socket
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(1.0)
            pid = self.get_server_pid()
            if pid and self.get_server_status():
                return f"🚀 <b>Server is ONLINE!</b>\n• Address: <code>{BASE_URL}</code>\n• Process PID: <code>{pid}</code>\n\nReady for commands (/status, /login, /start_session)."

        return "⚠️ Server process spawned, but port 8000 is not responding yet. Check runtime/server logs."

    def stop_server(self) -> str:
        """Terminate the server process."""
        pid = self.get_server_pid()
        if not pid:
            return "ℹ️ Server is already offline."

        cmd = f"taskkill /F /PID {pid}"
        subprocess.run(["cmd.exe", "/c", cmd], capture_output=True)
        time.sleep(1.0)

        # Confirm port is free
        if not self.get_server_pid():
            return "🛑 <b>Server Stopped Successfully.</b>\nPort 8000 is free.\nSupervisor remains online — send <code>/start_server</code> anytime to restart."
        return f"⚠️ Attempted to stop PID {pid}, but port {PORT} still appears busy."

    # --- Command Handling -----------------------------------------------------

    def handle_command(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            self._cmd_help()
        elif cmd in ("/start_server", "/startserver", "/boot"):
            self.send_message("⏳ <b>Starting Strategy Bot server...</b>")
            msg = self.start_server()
            self.send_message(msg)
        elif cmd in ("/kill", "/stop_server", "/stopserver", "/shutdown"):
            self.send_message("⏳ <b>Stopping server...</b>")
            msg = self.stop_server()
            self.send_message(msg)
        elif cmd == "/restart":
            self.send_message("🔄 <b>Restarting server...</b>")
            self.stop_server()
            time.sleep(1.5)
            msg = self.start_server()
            self.send_message(msg)
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/pnl":
            self._cmd_pnl()
        elif cmd == "/positions":
            self._cmd_positions()
        elif cmd == "/start_session":
            self._cmd_start_session(text)
        elif cmd in ("/stop_session", "/squareoff"):
            self._forward_post("/api/stop", "🛑 Emergency stop & square-off triggered.")
        elif cmd == "/arm":
            self._forward_post("/api/arm", "⚡ Live broker trading ARMED.")
        elif cmd == "/disarm":
            self._forward_post("/api/disarm", "🛡️ Trading DISARMED.")
        elif cmd == "/login":
            self._cmd_login(arg)
        elif cmd == "/test":
            self.send_message("✅ <b>Supervisor Test OK!</b> Bot supervisor is operational.")
        else:
            self.send_message(f"❓ Unknown command: <code>{_esc(cmd)}</code>\nType /help to see available commands.")

    def _cmd_help(self) -> None:
        help_text = (
            "🤖 <b>Strategy Bot Supervisor Menu</b>\n\n"
            "<b>🖥️ Server Management (24/7):</b>\n"
            "• /start_server - Boot up the bot server\n"
            "• /kill - Shut down the server (supervisor stays alive)\n"
            "• /restart - Restart the server\n\n"
            "<b>📊 Monitoring Commands:</b>\n"
            "• /status - Server & live trading status\n"
            "• /pnl - Today's Realized & Unrealized P&L\n"
            "• /positions - Active option legs breakdown\n"
            "• /test - Test supervisor communication\n\n"
            "<b>⚡ Trading Control:</b>\n"
            "• /login - Automate broker login (or /login [code])\n"
            "• /start_session - Start trading session (or /start_session force)\n"
            "• /stop_session - Stop session & force square-off\n"
            "• /arm - Arm live broker trading\n"
            "• /disarm - Disarm live trading\n"
            "• /squareoff - Emergency exit all open legs\n"
        )
        self.send_message(help_text)

    def _cmd_status(self) -> None:
        st = self.get_server_status()
        if not st:
            pid = self.get_server_pid()
            pid_str = f" (PID: {pid})" if pid else ""
            self.send_message(f"🔴 <b>Server Status: OFFLINE{pid_str}</b>\nSupervisor is active.\nType <code>/start_server</code> to turn the server on.")
            return

        status = st.get("status", "idle")
        phase = st.get("phase", "NONE")
        spot = st.get("spot")
        atm = st.get("atm")
        paper = st.get("paper_mode", True)
        armed = st.get("live_armed", False)
        session_ok = bool(st.get("session_ok"))
        lots = st.get("lots", 1)

        mode_str = "PAPER" if paper else ("LIVE (ARMED)" if armed else "LIVE (DISARMED)")
        broker_str = "🟢 CONNECTED" if session_ok else "🔴 OFFLINE (/login)"
        spot_str = f"{spot:,.2f}" if spot else "N/A"
        atm_str = str(atm) if atm else "N/A"

        stats = compute_session_stats(st)
        pos_pts = stats["open_pts"]
        pos_rs = stats["open_rs"]
        tot_pts = stats["total_pts"]
        tot_rs = stats["total_rs"]
        pos_sign = "+" if pos_pts >= 0 else ""
        tot_sign = "+" if tot_pts >= 0 else ""
        spot_header = format_spot_line(stats, atm)

        msg = (
            "📊 <b>Bot Status Overview</b>\n"
            f"🟢 Server: <b>ONLINE</b> (Port {PORT})\n"
            f"⚙️ State: <b>{_esc(status).upper()}</b> | Phase: <b>{_esc(phase)}</b>\n"
            f"🛡️ Mode: <b>{mode_str}</b> | Broker: <b>{broker_str}</b>\n"
            f"{spot_header} | Lots: <b>{lots}</b>\n\n"
            f"💰 Position P&L: <b>{pos_sign}{pos_pts:.2f} pts</b> ({pos_sign}₹{pos_rs:,.2f})\n"
            f"💼 Day Total P&L: <b>{tot_sign}{tot_pts:.2f} pts</b> ({tot_sign}₹{tot_rs:,.2f})\n\n"
            f"⏰ <code>{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        self.send_message(msg)

    def _cmd_pnl(self) -> None:
        st = self.get_server_status()
        is_offline = not st

        spot = st.get("spot") if st else None
        atm = st.get("atm") if st else None
        spot_str = f"{spot:,.2f}" if spot else "—"
        atm_str = str(atm) if atm else "—"

        stats = compute_session_stats(st)
        if is_offline and stats.get("realized_pts") == 0.0 and stats.get("realized_rs") == 0.0 and not stats.get("entries_today"):
            self.send_message("🔴 <b>Server is offline and no trades were recorded in today's journal.</b>\nStart the server with <code>/start_server</code>.")
            return
        tot_pts = stats["total_pts"]
        tot_rs = stats["total_rs"]
        tot_sign = "+" if tot_pts >= 0 else ""
        tot_icon = "🟢" if tot_pts >= 0 else "🛑"

        open_pts = stats["open_pts"]
        open_rs = stats["open_rs"]
        open_sign = "+" if open_pts >= 0 else ""

        real_pts = stats["realized_pts"]
        real_rs = stats["realized_rs"]
        real_sign = "+" if real_pts >= 0 else ""

        spot_header = format_spot_line(stats, atm)

        msg = (
            f"{tot_icon} <b>Today's P&L Summary</b>\n\n"
            f"{spot_header}\n\n"
            f"📊 Total Realized & Running:\n"
            f"<b>{tot_sign}{tot_pts:.2f} pts</b> ({tot_sign}₹{tot_rs:,.2f})\n\n"
            f"🎯 Open Position P&L:\n"
            f"<b>{open_sign}{open_pts:.2f} pts</b> ({open_sign}₹{open_rs:,.2f})\n\n"
            f"💰 Closed / Realized P&L:\n"
            f"<b>{real_sign}{real_pts:.2f} pts</b> ({real_sign}₹{real_rs:,.2f})\n"
            f"🔢 Entries Today: <b>{stats.get('entries_today', 0)}</b>\n\n"
            f"⏰ <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        )
        self.send_message(msg)

    def _cmd_positions(self) -> None:
        st = self.get_server_status()
        is_offline = not st

        spot = st.get("spot") if st else None
        atm = st.get("atm") if st else None
        spot_str = f"{spot:,.2f}" if spot else "—"
        atm_str = str(atm) if atm else "—"

        stats = compute_session_stats(st)
        legs = stats.get("legs", {})
        if not legs:
            if is_offline:
                self.send_message("🔴 Server is offline and no active or recorded positions were found in today's journal.")
            else:
                self.send_message("ℹ️ No active positions currently open.")
            return

        spot_header = format_spot_line(stats, atm)
        lines = [
            f"📋 <b>Active Option Legs:</b>\n"
            f"{spot_header}\n"
        ]
        for right in ("CE", "PE"):
            leg = legs.get(right)
            if not leg:
                continue
            sym = leg.get("symbol", right)
            entry = leg.get("entry", 0.0)
            ltp = leg.get("ltp", 0.0)
            sl = leg.get("sl")
            sl_str = f"{sl:.2f}" if sl is not None else "—"
            pnl_pts = leg.get("pnl_pts", 0.0)
            pnl_rs = leg.get("pnl_rupees", 0.0)
            pnl_sign = "+" if pnl_pts >= 0 else ""
            icon = "🔵" if right == "CE" else "🔴"
            status_str = "OPEN" if leg.get("is_open") else "CLOSED"
            lines.append(
                f"{icon} <b>{right}</b> ({status_str})\n"
                f"🏷️ <code>{_esc(sym)}</code>\n"
                f"Entry: <b>{entry:.2f}</b> | LTP: <b>{ltp:.2f}</b> | SL: <b>{sl_str}</b>\n"
                f"P&L: <b>{pnl_sign}{pnl_pts:.2f} pts</b> ({pnl_sign}₹{pnl_rs:,.2f})\n"
            )
        if stats.get("comb_entry") is not None and stats.get("comb_current") is not None:
            open_sign = "+" if stats["open_pts"] >= 0 else ""
            lines.append(
                f"⚖️ Combined: <b>{stats['comb_entry']:.2f}</b> → <b>{stats['comb_current']:.2f}</b> "
                f"({open_sign}{stats['open_pts']:.2f} pts)"
            )
        self.send_message("\n".join(lines))

    def _cmd_start_session(self, text: str) -> None:
        today_name = dt.datetime.now().strftime("%A")
        is_weekend = dt.date.today().weekday() >= 5
        parts = text.strip().split()
        force = len(parts) > 1 and parts[1].lower() in ("force", "-force", "--force")

        if is_weekend and not force:
            self.send_message(
                f"📅 <b>No Trading Session on Weekends</b>\n\n"
                f"Today is <b>{today_name}</b>. Saturday and Sunday are not trading days (NSE is closed).\n\n"
                f"<i>(If you are testing and want to run anyway, send: <code>/start_session force</code>)</i>"
            )
            return

        st = self.get_server_status()
        if not st:
            self.send_message("⏳ Server is offline. Auto-starting server first...")
            self.start_server()
            time.sleep(2.0)

        self._forward_post("/api/start", "🚀 Trading session start command dispatched.")

    def _cmd_login(self, arg: str) -> None:
        st = self.get_server_status()
        if not st:
            self.send_message("🔴 Server is offline. Start it first with /start_server.")
            return

        if arg:
            from app.broker.auto_login import _extract_code
            code = _extract_code(arg) or arg
            self.send_message("⏳ Exchanging auth code for access token...")
            try:
                r = self._http.post(f"{BASE_URL}/api/login/code", json={"auth_code": code})
                if r.status_code == 200:
                    d = r.json()
                    self.send_message(
                        f"✅ <b>Broker Login Successful!</b>\n"
                        f"👤 UID: <code>{_esc(d.get('uid'))}</code>\n"
                        f"🏦 Account: <code>{_esc(d.get('account_id'))}</code>\n"
                        f"🟢 Session: <b>Active</b>"
                    )
                else:
                    self.send_message(f"❌ Login failed: {r.text}")
            except Exception as e:
                self.send_message(f"❌ Error communicating with server: {_esc(e)}")
            return

        # Auto login
        if not getattr(settings, "has_auto_login_credentials", False):
            api_url = f"{settings.oauth_url}?client_id={settings.client_id}"
            self.send_message(
                "ℹ️ <b>Automated Broker Login</b>\n\n"
                "To enable 100% zero-touch automated login without clicking links, "
                "add these to <code>backend/.env</code>:\n"
                "• <code>SHOONYA_PASSWORD=your_password</code>\n"
                "• <code>SHOONYA_TOTP_KEY=your_totp_secret_key</code>\n\n"
                "<b>Manual Option:</b>\n"
                f"1. Open: <a href=\"{api_url}\">Shoonya Authorization Link</a>\n"
                "2. Copy the redirect code and send: <code>/login [auth_code]</code>"
            )
            return

        self.send_message("🔐 <b>Starting zero-touch broker login...</b>\nSolving TOTP and generating session in background.")
        try:
            r = self._http.post(f"{BASE_URL}/api/login/auto", timeout=45.0)
            if r.status_code == 200:
                d = r.json()
                lim = d.get("limits") or {}
                margin_str = f"\n💵 Margin: <b>₹{_esc(lim.get('cash', lim.get('net', 'N/A')))}</b>" if lim else ""
                self.send_message(
                    f"🚀 <b>Automated Broker Login Successful!</b>\n"
                    f"👤 UID: <code>{_esc(d.get('uid'))}</code>\n"
                    f"🏦 Account: <code>{_esc(d.get('account_id'))}</code>"
                    f"{margin_str}\n"
                    f"🟢 Broker Session: <b>READY</b>"
                )
            else:
                self.send_message(f"❌ Auto-login failed: {r.text}")
        except Exception as e:
            self.send_message(f"❌ Auto-login request error: {_esc(e)}")

    def _forward_post(self, endpoint: str, success_msg: str) -> None:
        try:
            r = self._http.post(f"{BASE_URL}{endpoint}", timeout=10.0)
            if r.status_code in (200, 409):
                if r.status_code == 409:
                    self.send_message(f"ℹ️ {r.json().get('reason', 'Already in requested state')}")
                else:
                    self.send_message(success_msg)
            else:
                self.send_message(f"⚠️ Command returned HTTP {r.status_code}: {r.text}")
        except Exception as e:
            self.send_message(f"❌ Failed to reach server: {_esc(e)}\nIs server running? Try /start_server.")

    # --- Polling Loop ---------------------------------------------------------

    def run(self) -> None:
        if not self.is_configured():
            log.error("Telegram bot token or chat ID is missing in .env. Exiting.")
            sys.exit(1)

        log.info("Telegram Bot Supervisor starting. Listening for commands...")
        self.send_message("🛡️ <b>Bot Supervisor Online</b>\nListening 24/7 for server & trading commands.\nType /help for options.")

        while True:
            self.check_heartbeat()
            try:
                url = f"{self.api_base}/getUpdates"
                params = {"offset": self.last_update_id + 1, "timeout": 20}
                resp = self._http.get(url, params=params, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        self.last_update_id = update["update_id"]
                        msg = update.get("message")
                        if msg and "text" in msg:
                            chat_id = msg.get("chat", {}).get("id")
                            if not self.is_authorized(chat_id):
                                log.warning("Unauthorized message from %s: %s", chat_id, msg.get("text"))
                                continue
                            self.handle_command(msg["text"])
                elif resp.status_code == 409:
                    log.warning("Telegram conflict (409). Backing off 10s...")
                    time.sleep(10.0)
                else:
                    time.sleep(2.0)
            except httpx.TimeoutException:
                continue
            except Exception as e:
                log.warning("Supervisor poll exception: %s", e)
                time.sleep(3.0)


if __name__ == "__main__":
    sup = TelegramSupervisor()
    sup.run()
