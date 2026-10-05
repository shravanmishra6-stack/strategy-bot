"""Telegram Bot for Strategy Bot: Real-time alerts + remote control.

Capabilities:
1. Real-time Push Notifications:
   - Server lifecycle (started / stopped / offline)
   - Session lifecycle (started, instruments resolved, day completed)
   - Trade executions (Straddle/Strangle entry with exact strikes, premiums, SLs)
   - Stop-Loss & Target triggers (Leg SL, Combined SL, Target reached, EOD Square-off)
   - Risk management halts & broker session expirations
   - Periodic heartbeat during open positions (Spot, ATM, running P&L in pts and ₹)
   - Arm / Disarm live mode changes

2. Interactive Remote Commands (via Telegram long-polling - no webhook/port-forwarding required):
   - /status       - Full status overview (spot, ATM, phase, P&L, arm state, legs)
   - /pnl          - Quick P&L snapshot (points & ₹)
   - /positions    - Current open/closed legs breakdown
   - /start_session- Start the day's session remotely
   - /stop_session - Stop session & force square-off
   - /arm          - Arm live trading (real broker orders)
   - /disarm       - Disarm live trading
   - /squareoff    - Emergency square-off open positions
   - /test         - Send test message confirmation
   - /help         - List available commands

Safety & Fault-Tolerance:
- Network/Telegram API failures NEVER block or crash the trading engine.
- Only authorized users (matching TELEGRAM_CHAT_ID) can query or execute commands.
- If tokens are missing, operates silently in no-op mode without breaking startup.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import httpx

from app.telegram_pnl import compute_session_stats, format_spot_line

from app.config import settings

log = logging.getLogger("telegram")


def _esc(val: Any) -> str:
    """Escape text for Telegram HTML parse mode."""
    if val is None:
        return ""
    return html.escape(str(val))


class TelegramNotifier:
    """Sends formatted HTML messages to the configured Telegram chat."""

    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(enabled)
        self.api_base = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self._client: Optional[httpx.Client] = None
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def is_active(self) -> bool:
        return self.enabled and self.is_configured

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def send_message(self, text: str, reply_markup: Optional[dict] = None) -> bool:
        """Send an HTML-formatted message to the default chat. Thread-safe & non-raising."""
        if not self.is_active:
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            client = self._get_client()
            resp = client.post(f"{self.api_base}/sendMessage", json=payload)
            if resp.status_code == 200:
                self.last_sent_at = time.time()
                self.last_error = None
                return True
            else:
                self.last_error = f"HTTP {resp.status_code}: {resp.text}"
                log.warning("Telegram send failed: %s", self.last_error)
                return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Telegram send exception: %s", self.last_error)
            return False

    def notify_server_started(self, host: str, port: int, paper_mode: bool, session_ok: bool) -> bool:
        mode_str = "🛡️ <b>PAPER TRADING</b>" if paper_mode else "⚠️ <b>REAL MONEY (LIVE)</b>"
        auth_str = "✅ <b>Authenticated</b>" if session_ok else "⚠️ <b>Needs Login</b>"
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = (
            "🚀 <b>Strategy Bot Server Online</b>\n"
            f"⏰ <code>{now}</code>\n"
            f"📍 Endpoint: <code>http://{host}:{port}</code>\n"
            f"🛡️ Mode: {mode_str}\n"
            f"🔑 Broker Session: {auth_str}\n\n"
            "<i>Ready to trade. Use /help for remote commands.</i>"
        )
        return self.send_message(msg)

    def notify_server_stopped(self) -> bool:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"🛑 <b>Strategy Bot Server Stopped</b>\n⏰ <code>{now}</code>"
        return self.send_message(msg)

    def notify_session_start(self, day: str, paper_mode: bool, armed: bool, lots: int) -> bool:
        mode_str = "PAPER TRADING" if paper_mode else "LIVE TRADING"
        armed_str = "⚡ <b>ARMED (Live orders enabled)</b>" if armed else "🛡️ <b>DISARMED (Standby)</b>"
        now = dt.datetime.now().strftime("%H:%M:%S")

        msg = (
            "🌅 <b>Trading Session Started</b>\n"
            f"📅 Date: <code>{_esc(day)}</code> at <code>{now}</code>\n"
            f"📦 Lots: <b>{lots}</b>\n"
            f"⚙️ Mode: <b>{mode_str}</b>\n"
            f"🔒 Status: {armed_str}"
        )
        return self.send_message(msg)

    def notify_resolved(self, resolved: dict) -> bool:
        if not resolved:
            return False
        spot = resolved.get("spot")
        atm = resolved.get("atm")
        expiry = resolved.get("expiry")
        lot_size = resolved.get("lot_size")
        legs = resolved.get("legs", [])

        ce_sym = next((l.get("symbol") for l in legs if l.get("right") == "CE"), "N/A")
        pe_sym = next((l.get("symbol") for l in legs if l.get("right") == "PE"), "N/A")

        msg = (
            "🎯 <b>Instruments Resolved</b>\n"
            f"📈 NIFTY Spot: <b>{spot:,.2f}</b>\n"
            f"🎯 ATM Strike: <b>{atm}</b>\n"
            f"📅 Expiry: <code>{_esc(expiry)}</code> (Lot: {lot_size})\n\n"
            f"🔵 <b>CE:</b> <code>{_esc(ce_sym)}</code>\n"
            f"🔴 <b>PE:</b> <code>{_esc(pe_sym)}</code>\n\n"
            "<i>Awaiting entry time (09:20)...</i>"
        )
        return self.send_message(msg)

    def notify_entry(self, spot: Optional[float], atm: Optional[int], lots: int,
                     legs: dict, combined: dict) -> bool:
        ce_leg = legs.get("CE", {})
        pe_leg = legs.get("PE", {})

        ce_entry = ce_leg.get("entry") if ce_leg.get("entry") is not None else (ce_leg.get("entry_price") or 0.0)
        ce_sl = ce_leg.get("trail_sl") or ce_leg.get("sl_level") or ce_leg.get("sl")
        pe_entry = pe_leg.get("entry") if pe_leg.get("entry") is not None else (pe_leg.get("entry_price") or 0.0)
        pe_sl = pe_leg.get("trail_sl") or pe_leg.get("sl_level") or pe_leg.get("sl")

        comb_entry = combined.get("entry") or (ce_entry + pe_entry)
        comb_sl = combined.get("sl_level")

        spot_str = f"{spot:,.2f}" if spot else "N/A"
        atm_str = str(atm) if atm else "N/A"

        ce_sl_str = f"SL: <b>{ce_sl:.2f}</b>" if ce_sl else "SL: <i>Combined</i>"
        pe_sl_str = f"SL: <b>{pe_sl:.2f}</b>" if pe_sl else "SL: <i>Combined</i>"
        comb_sl_str = f"<b>{comb_sl:.2f} pts</b>" if comb_sl else "<i>Disabled</i>"

        msg = (
            "⚡ <b>STRADDLE ENTRY EXECUTED</b>\n"
            f"📈 Spot: <b>{spot_str}</b> | ATM: <b>{atm_str}</b> | Lots: <b>{lots}</b>\n\n"
            f"🔵 <b>CE Sold @</b> <code>{ce_entry:.2f}</code> | {ce_sl_str}\n"
            f"🔴 <b>PE Sold @</b> <code>{pe_entry:.2f}</code> | {pe_sl_str}\n\n"
            f"💰 Combined Premium: <b>{comb_entry:.2f} pts</b>\n"
            f"🛑 Combined SL: {comb_sl_str}\n"
            f"⏰ Entry Time: <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        )
        return self.send_message(msg)

    def notify_leg_exit(self, right: str, symbol: str, reason: str,
                        exit_px: Optional[float], pnl_pts: float, pnl_rupees: float,
                        spot: Optional[float] = None) -> bool:
        icon = "🔴" if right == "PE" else "🔵"
        pnl_icon = "🟢" if pnl_pts >= 0 else "🛑"
        sign = "+" if pnl_pts >= 0 else ""
        px_str = f"{exit_px:.2f}" if exit_px is not None else "N/A"
        spot_line = f"📈 NIFTY Spot: <b>{spot:,.2f}</b>\n" if spot else ""

        msg = (
            f"{pnl_icon} <b>LEG EXIT: {icon} {right} ({_esc(reason)})</b>\n"
            f"🏷️ Symbol: <code>{_esc(symbol)}</code>\n"
            f"{spot_line}"
            f"💵 Exit Price: <b>{px_str}</b>\n"
            f"📊 Leg P&L: <b>{sign}{pnl_pts:.2f} pts</b> ({sign}₹{pnl_rupees:,.2f})\n"
            f"⏰ Time: <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        )
        return self.send_message(msg)

    def notify_combined_sl(self, combined_val: float, sl_level: float,
                           pnl_pts: float, pnl_rupees: float,
                           spot: Optional[float] = None) -> bool:
        sign = "+" if pnl_pts >= 0 else ""
        spot_line = f"📈 NIFTY Spot: <b>{spot:,.2f}</b>\n" if spot else ""
        msg = (
            "🚨 <b>COMBINED STOP-LOSS TRIGGERED</b>\n"
            f"{spot_line}"
            f"⚠️ Premium: <b>{combined_val:.2f}</b> breached SL: <b>{sl_level:.2f}</b>\n"
            f"💥 Loss: <b>{sign}{pnl_pts:.2f} pts</b> ({sign}₹{pnl_rupees:,.2f})\n"
            "🛑 Both positions squared off immediately."
        )
        return self.send_message(msg)

    def notify_target_hit(self, pnl_pts: float, pnl_rupees: float,
                          spot: Optional[float] = None) -> bool:
        sign = "+" if pnl_pts >= 0 else ""
        spot_line = f"📈 NIFTY Spot: <b>{spot:,.2f}</b>\n" if spot else ""
        msg = (
            "🎯 <b>TARGET PROFIT ACHIEVED!</b>\n"
            f"{spot_line}"
            f"💰 Profit: <b>{sign}{pnl_pts:.2f} pts</b> ({sign}₹{pnl_rupees:,.2f})\n"
            "🔒 Position squared off and profit locked in!"
        )
        return self.send_message(msg)

    def notify_squareoff(self, reason: str, pnl_pts: float, pnl_rupees: float,
                         spot: Optional[float] = None) -> bool:
        sign = "+" if pnl_pts >= 0 else ""
        icon = "🟢" if pnl_pts >= 0 else "🛑"
        spot_line = f"📈 NIFTY Spot: <b>{spot:,.2f}</b>\n" if spot else ""
        msg = (
            f"⏱️ <b>SQUARE-OFF COMPLETED</b>\n"
            f"📌 Reason: <code>{_esc(reason)}</code>\n"
            f"{spot_line}"
            f"{icon} Realized: <b>{sign}{pnl_pts:.2f} pts</b> ({sign}₹{pnl_rupees:,.2f})\n"
            f"⏰ Time: <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        )
        return self.send_message(msg)

    def notify_session_end(self, status: str, pnl_pts: float, pnl_rupees: float,
                           trades: int, journal_file: Optional[str] = None) -> bool:
        pnl_icon = "🟢" if pnl_pts >= 0 else "🔴"
        sign = "+" if pnl_pts >= 0 else ""
        msg = (
            "🏁 <b>TRADING SESSION FINISHED</b>\n"
            f"📊 Final Status: <b>{_esc(status).upper()}</b>\n"
            f"{pnl_icon} Today's P&L: <b>{sign}{pnl_pts:.2f} pts</b> ({sign}₹{pnl_rupees:,.2f})\n"
            f"🔢 Total Trades / Fills: <b>{trades}</b>\n"
        )
        if journal_file:
            msg += f"📑 Journal: <code>{_esc(journal_file)}</code>\n"
        msg += f"⏰ Finished: <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        return self.send_message(msg)

    def notify_arm_changed(self, armed: bool) -> bool:
        if armed:
            msg = (
                "⚠️ <b>LIVE TRADING ARMED</b>\n"
                "🚨 REAL orders will now be routed directly to Shoonya exchange!\n"
                f"⏰ <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
            )
        else:
            msg = (
                "🛡️ <b>TRADING DISARMED</b>\n"
                "Bot is now in safe standby (no live orders will be sent).\n"
                f"⏰ <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
            )
        return self.send_message(msg)

    def notify_heartbeat(self, spot: Optional[float], atm: Optional[int],
                         legs: dict, combined: dict, risk: dict,
                         st: Optional[dict] = None) -> bool:
        stats = compute_session_stats(st or {"legs": legs, "combined": combined, "risk": risk, "spot": spot, "atm": atm})
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

        time_now = dt.datetime.now().strftime("%H:%M")
        spot_header = format_spot_line(stats, atm)
        msg = (
            f"📊 <b>Heartbeat Update ({time_now})</b>\n"
            f"{spot_header}\n"
            f"🔵 CE LTP: <b>{ce_str}</b> | 🔴 PE LTP: <b>{pe_str}</b>\n"
            f"{pnl_icon} Position P&L: <b>{sign}{open_pts:.2f} pts</b> ({sign}₹{open_rs:,.2f})\n"
            f"💼 Day Total P&L: <b>{tot_sign}{tot_pts:.2f} pts</b> ({tot_sign}₹{tot_rs:,.2f})"
        )
        return self.send_message(msg)

    def notify_alert(self, title: str, message: str, level: str = "WARNING") -> bool:
        icon = "🚨" if level in ("ERROR", "CRITICAL") else "⚠️"
        msg = (
            f"{icon} <b>{_esc(title)}</b>\n"
            f"<code>{_esc(message)}</code>\n"
            f"⏰ <code>{dt.datetime.now().strftime('%H:%M:%S')}</code>"
        )
        return self.send_message(msg)


class TelegramStateObserver:
    """Observes SessionState updates and triggers matching notifications."""

    def __init__(self, notifier: TelegramNotifier, heartbeat_interval_secs: float = 1800.0):
        self.notifier = notifier
        self.heartbeat_interval = heartbeat_interval_secs
        self.last_heartbeat = time.time()
        self._notified_events: set = set()
        self._last_leg_status: Dict[str, str] = {}

    def on_state_update(self, old: dict, new: dict, changes: dict) -> None:
        """Invoked by SessionState whenever st.update(...) is called."""
        if not self.notifier.is_active:
            return

        today = new.get("day") or dt.date.today().isoformat()

        # 0) Session starting
        if new.get("status") == "starting" and f"starting_{today}" not in self._notified_events:
            self._notified_events.add(f"starting_{today}")
            self.notifier.notify_session_start(
                day=today,
                paper_mode=settings.paper_mode,
                armed=bool(new.get("live_armed")),
                lots=new.get("lots", 1),
            )

        # 1) Resolved instruments
        if new.get("status") == "resolved" and f"resolved_{today}" not in self._notified_events:
            self._notified_events.add(f"resolved_{today}")
            self.notifier.notify_resolved(new.get("resolved") or {})

        # 2) Entry executed (phase changed to IN_POSITION)
        if new.get("phase") == "IN_POSITION" and f"entry_{today}" not in self._notified_events:
            self._notified_events.add(f"entry_{today}")
            self.notifier.notify_entry(
                spot=new.get("spot"),
                atm=new.get("atm"),
                lots=new.get("lots", 1),
                legs=new.get("legs", {}),
                combined=new.get("combined", {}),
            )
            # Track leg status
            for r, leg_data in new.get("legs", {}).items():
                is_open = leg_data.get("is_open", True if leg_data.get("status") == "OPEN" else False)
                self._last_leg_status[r] = "OPEN" if is_open else "CLOSED"

        # 3) Individual leg exits
        new_legs = new.get("legs", {})
        for right, leg_data in new_legs.items():
            is_open = leg_data.get("is_open", True if leg_data.get("status") == "OPEN" else False)
            current_status = "OPEN" if is_open else "CLOSED"
            prev_status = self._last_leg_status.get(right, "OPEN")
            if prev_status == "OPEN" and current_status == "CLOSED":
                leg_key = f"exit_{today}_{right}"
                if leg_key not in self._notified_events:
                    self._notified_events.add(leg_key)
                    stats = compute_session_stats(new)
                    leg_stat = stats["legs"].get(right, {})
                    sym = leg_stat.get("symbol") or leg_data.get("symbol") or right
                    exit_px = leg_stat.get("ltp") if leg_stat.get("ltp") else leg_data.get("exit_price")
                    pnl_pts = leg_stat.get("pnl_pts") if leg_stat.get("pnl_pts") is not None else leg_data.get("pnl_pts", 0.0)
                    pnl_rupees = leg_stat.get("pnl_rupees") if leg_stat.get("pnl_rupees") is not None else leg_data.get("pnl_rupees", 0.0)
                    reason = leg_data.get("exit_reason")
                    if not reason:
                        for f in reversed(new.get("fills", [])):
                            if f.get("right") == right and f.get("action") == "BUY":
                                reason = f.get("reason")
                                break
                    reason = reason or "EXIT"
                    self.notifier.notify_leg_exit(right, sym, reason, exit_px, pnl_pts, pnl_rupees, spot=new.get("spot"))
            self._last_leg_status[right] = current_status

        # 4) Session Finished
        if new.get("status") == "done" and f"done_{today}" not in self._notified_events:
            self._notified_events.add(f"done_{today}")
            risk = new.get("risk", {})
            pnl_pts = risk.get("daily_pnl_pts", 0.0)
            pnl_rs = risk.get("daily_pnl_rupees", 0.0)
            trades = len(new.get("fills", []))
            journal = new.get("journal_path")
            self.notifier.notify_session_end("DONE", pnl_pts, pnl_rs, trades, journal)

        # 5) Arming status change
        if "live_armed" in changes and old.get("live_armed") != new.get("live_armed"):
            self.notifier.notify_arm_changed(bool(new.get("live_armed")))

        # 6) Session Expired alert
        if new.get("status") == "session_expired" and f"expired_{today}" not in self._notified_events:
            self._notified_events.add(f"expired_{today}")
            err = new.get("error") or "Shoonya token expired"
            self.notifier.notify_alert("SESSION EXPIRED", f"{err}\nPlease re-authenticate via OAuth.", level="CRITICAL")

        # 7) Connect or Resolve failure
        if new.get("status") in ("connect_failed", "resolve_failed") and f"{new['status']}_{today}" not in self._notified_events:
            self._notified_events.add(f"{new['status']}_{today}")
            self.notifier.notify_alert(new["status"].upper(), str(new.get("error")), level="ERROR")

        # 8) Risk Manager halt
        risk = new.get("risk", {})
        if risk.get("halted") and f"risk_halt_{today}" not in self._notified_events:
            self._notified_events.add(f"risk_halt_{today}")
            reason = risk.get("halted_reason") or "Risk limit breached"
            self.notifier.notify_alert("TRADING HALTED BY RISK GATE", reason, level="CRITICAL")

        # 9) Periodic Heartbeat while IN_POSITION
        if new.get("phase") == "IN_POSITION":
            now_sec = time.time()
            if now_sec - self.last_heartbeat >= self.heartbeat_interval:
                self.last_heartbeat = now_sec
                self.notifier.notify_heartbeat(
                    spot=new.get("spot"),
                    atm=new.get("atm"),
                    legs=new_legs,
                    combined=new.get("combined", {}),
                    risk=risk,
                    st=new,
                )


class TelegramCommandPoller:
    """Long-polls Telegram for incoming operator commands in a background daemon thread."""

    def __init__(self, notifier: TelegramNotifier,
                 get_state_fn: Callable[[], dict],
                 start_session_fn: Optional[Callable] = None,
                 stop_session_fn: Optional[Callable] = None,
                 arm_fn: Optional[Callable] = None,
                 disarm_fn: Optional[Callable] = None):
        self.notifier = notifier
        self.get_state = get_state_fn
        self.start_session_fn = start_session_fn
        self.stop_session_fn = stop_session_fn
        self.arm_fn = arm_fn
        self.disarm_fn = disarm_fn

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.last_update_id = 0

    def start(self) -> None:
        if not self.notifier.is_active or (self._thread and self._thread.is_alive()):
            return
        import os
        if os.getenv("TELEGRAM_POLLER_DISABLED", "").strip().lower() in ("true", "1", "yes"):
            log.info("Telegram command poller disabled via TELEGRAM_POLLER_DISABLED (external supervisor active).")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="telegram_poller", daemon=True)
        self._thread.start()
        log.info("Telegram command poller started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        log.info("Telegram command poller stopped.")

    def _is_authorized(self, chat_id: Any) -> bool:
        if not self.notifier.chat_id:
            return False
        # Match configured chat id or comma-separated list
        allowed = [c.strip() for c in str(self.notifier.chat_id).split(",") if c.strip()]
        return str(chat_id) in allowed

    def _run_loop(self) -> None:
        client = httpx.Client(timeout=25.0)
        url = f"{self.notifier.api_base}/getUpdates"

        while not self._stop_event.is_set():
            try:
                params = {"offset": self.last_update_id + 1, "timeout": 15}
                resp = client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        self.last_update_id = update["update_id"]
                        msg = update.get("message")
                        if msg and "text" in msg:
                            self._handle_message(msg)
                elif resp.status_code == 409:
                    log.warning("Telegram poller conflict (409) - another instance is polling. Waiting 10s...")
                    time.sleep(10.0)
                elif resp.status_code in (401, 404):
                    log.error("Telegram bot token appears invalid (HTTP %d). Poller halting.", resp.status_code)
                    break
                else:
                    time.sleep(2.0)
            except httpx.TimeoutException:
                continue
            except Exception as e:
                log.warning("Telegram polling error: %s", e)
                time.sleep(3.0)
        try:
            client.close()
        except Exception:
            pass

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "").strip()

        if not self._is_authorized(chat_id):
            log.warning("Unauthorized Telegram message from chat_id %s: %s", chat_id, text)
            # Send unauthorized response
            try:
                httpx.post(f"{self.notifier.api_base}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "⛔ <b>Unauthorized access.</b>\nThis Strategy Bot is private.",
                    "parse_mode": "HTML"
                }, timeout=5.0)
            except Exception:
                pass
            return

        cmd = text.split()[0].lower() if text else ""
        if "@" in cmd:  # Strip bot handle if sent in groups (e.g. /status@MyBot)
            cmd = cmd.split("@")[0]

        if cmd in ("/start", "/help"):
            self._cmd_help()
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/pnl":
            self._cmd_pnl()
        elif cmd == "/positions":
            self._cmd_positions()
        elif cmd == "/start_session":
            self._cmd_start_session(text)
        elif cmd in ("/stop_session", "/squareoff"):
            self._cmd_stop_session()
        elif cmd == "/arm":
            self._cmd_arm()
        elif cmd == "/disarm":
            self._cmd_disarm()
        elif cmd == "/login":
            self._cmd_login(text)
        elif cmd in ("/start_server", "/startserver", "/boot"):
            import os
            pid = os.getpid()
            self.notifier.send_message(
                f"🟢 <b>Server is ALREADY running!</b>\n"
                f"• Address: <code>http://127.0.0.1:8000</code>\n"
                f"• Process PID: <code>{pid}</code>\n\n"
                f"<i>(To start trading, send: <code>/start_session</code>)</i>"
            )
        elif cmd == "/restart":
            self._cmd_restart()
        elif cmd in ("/kill", "/shutdown"):
            self._cmd_kill()
        elif cmd == "/test":
            self.notifier.send_message("✅ <b>Test Successful!</b> Telegram notifications and commands are fully operational.")
        else:
            self.notifier.send_message(f"❓ Unknown command: <code>{_esc(cmd)}</code>\nType /help to see available commands.")

    def _cmd_help(self) -> None:
        help_text = (
            "🤖 <b>Strategy Bot Control Menu</b>\n\n"
            "<b>📊 Monitoring Commands:</b>\n"
            "• /status - Complete market & bot status overview\n"
            "• /pnl - Today's Realized & Unrealized P&L\n"
            "• /positions - Active option legs breakdown\n"
            "• /test - Test bot communication\n\n"
            "<b>⚡ Control Commands:</b>\n"
            "• /login - Automate broker login (or /login [code])\n"
            "• /start_session - Start the trading session\n"
            "• /stop_session - Stop session & force square-off\n"
            "• /arm - Arm live broker trading\n"
            "• /disarm - Disarm live trading\n"
            "• /squareoff - Emergency exit all open legs\n\n"
            "<b>⚙️ Server Commands:</b>\n"
            "• /start_server - Boot up / check bot server\n"
            "• /restart - Restart the bot server in background\n"
            "• /kill - Shut down and stop the bot server\n"
        )
        self.notifier.send_message(help_text)

    def _cmd_status(self) -> None:
        st = self.get_state()
        status = st.get("status", "idle")
        phase = st.get("phase", "NONE")
        spot = st.get("spot")
        atm = st.get("atm")
        paper = st.get("paper_mode", True)
        armed = st.get("live_armed", False)
        lots = st.get("lots", 1)

        mode_str = "PAPER" if paper else ("LIVE (ARMED)" if armed else "LIVE (DISARMED)")
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
            f"⚙️ State: <b>{_esc(status).upper()}</b> | Phase: <b>{_esc(phase)}</b>\n"
            f"🛡️ Mode: <b>{mode_str}</b>\n"
            f"{spot_header} | Lots: <b>{lots}</b>\n\n"
            f"💰 Position P&L: <b>{pos_sign}{pos_pts:.2f} pts</b> ({pos_sign}₹{pos_rs:,.2f})\n"
            f"💼 Day Total P&L: <b>{tot_sign}{tot_pts:.2f} pts</b> ({tot_sign}₹{tot_rs:,.2f})\n\n"
            f"⏰ <code>{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        self.notifier.send_message(msg)

    def _cmd_pnl(self) -> None:
        st = self.get_state()
        spot = st.get("spot")
        atm = st.get("atm")
        spot_str = f"{spot:,.2f}" if spot else "—"
        atm_str = str(atm) if atm else "—"

        stats = compute_session_stats(st)
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
            f"🔢 Entries Today: <b>{stats.get('entries_today', 0)}</b>"
        )
        self.notifier.send_message(msg)

    def _cmd_positions(self) -> None:
        st = self.get_state()
        spot = st.get("spot")
        atm = st.get("atm")
        spot_str = f"{spot:,.2f}" if spot else "—"
        atm_str = str(atm) if atm else "—"

        stats = compute_session_stats(st)
        legs = stats.get("legs", {})
        if not legs:
            self.notifier.send_message("ℹ️ No active positions or resolved legs.")
            return

        spot_header = format_spot_line(stats, atm)
        lines = [
            f"📋 <b>Position Breakdown:</b>\n"
            f"{spot_header}\n"
        ]
        for right in ("CE", "PE"):
            leg = legs.get(right)
            if not leg:
                continue
            icon = "🔵" if right == "CE" else "🔴"
            status_str = "OPEN" if leg.get("is_open") else "CLOSED"
            entry = leg.get("entry", 0.0)
            ltp = leg.get("ltp", 0.0)
            sl = leg.get("sl")
            sl_str = f"{sl:.2f}" if sl is not None else "—"
            pnl_pts = leg.get("pnl_pts", 0.0)
            pnl_rs = leg.get("pnl_rupees", 0.0)
            pnl_sign = "+" if pnl_pts >= 0 else ""
            sym = leg.get("symbol", right)

            lines.append(
                f"{icon} <b>{right} ({status_str})</b>\n"
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
        self.notifier.send_message("\n".join(lines))

    def _cmd_start_session(self, text: str = "") -> None:
        today_name = dt.datetime.now().strftime("%A")
        is_weekend = dt.date.today().weekday() >= 5
        parts = text.strip().split()
        force = len(parts) > 1 and parts[1].lower() in ("force", "-force", "--force")

        if is_weekend and not force:
            self.notifier.send_message(
                f"📅 <b>No Trading Session on Weekends</b>\n\n"
                f"Today is <b>{today_name}</b>. Saturday and Sunday are not trading days (NSE is closed).\n\n"
                f"<i>(If you are testing and want to run anyway, send: <code>/start_session force</code>)</i>"
            )
            return

        if not self.start_session_fn:
            self.notifier.send_message("⚠️ Start session function not bound.")
            return
        try:
            res = self.start_session_fn()
            msg = "🚀 Session start command dispatched."
            if is_weekend and force:
                msg = "🚀 Weekend session start command dispatched (force mode)."
            self.notifier.send_message(msg)
        except Exception as e:
            self.notifier.send_message(f"❌ Failed to start session: <code>{_esc(e)}</code>")

    def _cmd_stop_session(self) -> None:
        if not self.stop_session_fn:
            self.notifier.send_message("⚠️ Stop session function not bound.")
            return
        try:
            self.stop_session_fn()
            self.notifier.send_message("🛑 Square-off & stop command triggered.")
        except Exception as e:
            self.notifier.send_message(f"❌ Failed to stop session: <code>{_esc(e)}</code>")

    def _cmd_arm(self) -> None:
        if not self.arm_fn:
            self.notifier.send_message("⚠️ Arm function not bound.")
            return
        try:
            res = self.arm_fn()
            # If res is a response with status or dict
            self.notifier.send_message("⚡ Live trading arm command processed.")
        except Exception as e:
            self.notifier.send_message(f"❌ Arm failed: <code>{_esc(e)}</code>")

    def _cmd_disarm(self) -> None:
        if not self.disarm_fn:
            self.notifier.send_message("⚠️ Disarm function not bound.")
            return
        try:
            self.disarm_fn()
            self.notifier.send_message("🛡️ Trading disarmed successfully.")
        except Exception as e:
            self.notifier.send_message(f"❌ Disarm failed: <code>{_esc(e)}</code>")

    def _cmd_login(self, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if arg:
            # Code or URL was provided: manual exchange
            from app.broker.auto_login import _extract_code
            code = _extract_code(arg) or arg
            self.notifier.send_message("⏳ Exchanging auth code for access token...")
            try:
                from scripts.oauth_login import exchange_and_save
                from app.config import update_access_token
                from app.broker.noren_client import client

                res = exchange_and_save(code, settings.uid)
                update_access_token(res["access_token"], res["uid"], res["account_id"])
                client.refresh_session(res["access_token"], res["uid"], res["account_id"])

                limits_str = ""
                try:
                    lim = client.get_limits()
                    if lim:
                        margin = lim.get("cash", lim.get("net", "N/A"))
                        limits_str = f"\n💵 Margin: <b>₹{_esc(margin)}</b>"
                except Exception:
                    pass

                self.notifier.send_message(
                    f"✅ <b>Broker Login Successful!</b>\n"
                    f"👤 UID: <code>{_esc(res['uid'])}</code>\n"
                    f"🏦 Account: <code>{_esc(res['account_id'])}</code>"
                    f"{limits_str}\n"
                    f"🟢 Session status: <b>Active</b>"
                )
            except Exception as e:
                self.notifier.send_message(f"❌ <b>Login Failed:</b> <code>{_esc(e)}</code>")
            return

        # No arg provided: Check if automated login credentials exist
        if not getattr(settings, "has_auto_login_credentials", False):
            api_url = f"{settings.oauth_url}?client_id={settings.client_id}"
            self.notifier.send_message(
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

        # Automated zero-touch login with TOTP in background thread
        self.notifier.send_message("🔐 <b>Starting zero-touch broker login...</b>\nSolving TOTP and generating session in background.")

        def _worker():
            try:
                from app.broker.auto_login import perform_auto_login
                res = perform_auto_login()
                limits_str = ""
                lim = res.get("limits") or {}
                if lim:
                    margin = lim.get("cash", lim.get("net", "N/A"))
                    limits_str = f"\n💵 Margin: <b>₹{_esc(margin)}</b>"

                self.notifier.send_message(
                    f"🚀 <b>Automated Broker Login Successful!</b>\n"
                    f"👤 UID: <code>{_esc(res['uid'])}</code>\n"
                    f"🏦 Account: <code>{_esc(res['account_id'])}</code>"
                    f"{limits_str}\n"
                    f"🟢 Broker Session: <b>READY</b>"
                )
            except Exception as e:
                log.exception("Automated login failed")
                api_url = f"{settings.oauth_url}?client_id={settings.client_id}"
                self.notifier.send_message(
                    f"❌ <b>Automated Login Failed:</b> <code>{_esc(e)}</code>\n\n"
                    f"<b>Fallback link:</b> <a href=\"{api_url}\">Authorize on Shoonya</a>\n"
                    "Then send: <code>/login [auth_code]</code>"
                )

        threading.Thread(target=_worker, name="auto_login", daemon=True).start()

    def _cmd_restart(self) -> None:
        import os
        import subprocess
        from pathlib import Path

        current_pid = os.getpid()
        self.notifier.send_message("🔄 <b>Restarting server...</b>\nRe-launching in background (PID will change).")

        ps_script = Path(__file__).resolve().parent.parent / "scripts" / "restart_helper.ps1"
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ps_script), "-PidToKill", str(current_pid)
        ]
        try:
            subprocess.Popen(
                cmd,
                creationflags=flags,
                close_fds=True,
            )
        except Exception as e:
            self.notifier.send_message(f"❌ <b>Restart failed to trigger:</b> <code>{_esc(e)}</code>")

    def _cmd_kill(self) -> None:
        import os
        import subprocess

        current_pid = os.getpid()
        self.notifier.send_message("🛑 <b>Shutting down server...</b>\nServer process terminating.")

        DETACHED_PROCESS = 0x00000008
        flags = DETACHED_PROCESS

        cmd = f"ping 127.0.0.1 -n 2 >nul & taskkill /F /PID {current_pid}"
        try:
            subprocess.Popen(["cmd.exe", "/c", cmd], creationflags=flags, close_fds=True)
        except Exception as e:
            self.notifier.send_message(f"❌ <b>Shutdown failed:</b> <code>{_esc(e)}</code>")


class TelegramBotManager:
    """Orchestrator for Telegram notifications, observer, and command polling."""

    def __init__(self):
        self.notifier = TelegramNotifier(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            enabled=settings.telegram_enabled,
        )
        self.observer = TelegramStateObserver(
            notifier=self.notifier,
            heartbeat_interval_secs=max(60, settings.telegram_heartbeat_mins * 60),
        )
        self.poller: Optional[TelegramCommandPoller] = None
        self._state_ref = None

    def init(self, state, start_fn=None, stop_fn=None, arm_fn=None, disarm_fn=None):
        """Bind state and API callbacks."""
        self._state_ref = state
        state.add_listener(self.observer.on_state_update)

        self.poller = TelegramCommandPoller(
            notifier=self.notifier,
            get_state_fn=state.snapshot,
            start_session_fn=start_fn,
            stop_session_fn=stop_fn,
            arm_fn=arm_fn,
            disarm_fn=disarm_fn,
        )

    def start(self) -> None:
        """Start poller and send online greeting."""
        if self.notifier.is_active:
            if self.poller:
                self.poller.start()
            self.notifier.notify_server_started(
                host=settings.api_host,
                port=settings.api_port,
                paper_mode=settings.paper_mode,
                session_ok=settings.has_credentials,
            )

    def stop(self) -> None:
        """Stop poller and send farewell."""
        if self.poller:
            self.poller.stop()
        if self.notifier.is_active:
            self.notifier.notify_server_stopped()

    def send_test_message(self) -> tuple[bool, str]:
        """Test sending a message immediately."""
        if not self.notifier.token:
            return False, "TELEGRAM_BOT_TOKEN is not configured"
        if not self.notifier.chat_id:
            return False, "TELEGRAM_CHAT_ID is not configured"

        ok = self.notifier.send_message(
            "🔔 <b>Strategy Bot Test Notification</b>\n"
            f"⏰ <code>{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
            "If you see this message, your Telegram Bot integration is working perfectly!"
        )
        if ok:
            return True, "Notification sent successfully"
        return False, self.notifier.last_error or "Unknown failure"

    def status_dict(self) -> dict:
        """Diagnostic snapshot."""
        return {
            "configured": self.notifier.is_configured,
            "enabled": self.notifier.enabled,
            "active": self.notifier.is_active,
            "chat_id": self.notifier.chat_id or None,
            "poller_running": bool(self.poller and self.poller._thread and self.poller._thread.is_alive()),
            "last_error": self.notifier.last_error,
            "last_sent_at": self.notifier.last_sent_at,
        }


# Singleton manager
telegram_bot = TelegramBotManager()
