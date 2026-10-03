"""FastAPI app: the operator dashboard + control endpoints.

Serves the single-page frontend and exposes the session's status so the
browser can poll it. Also owns the one process-wide session thread, so the
dashboard can start/stop a trading day without shelling out.

Live arming is exposed as an explicit operator action (`POST /api/arm`), not
something the runner does implicitly - the config gate (PAPER_MODE=false) must
be set AND this button pressed before any order can leave the process.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.broker.noren_client import client
from app.config import settings, update_access_token
from app import journal as journal_mod
from app.session import SessionState, run_day
from scripts.oauth_login import exchange_and_save, _make_api
from pydantic import BaseModel
from typing import Optional

log = logging.getLogger("api")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Excel's own content type: without it browsers open the workbook as a broken
# text file instead of offering a download.
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app_inst: FastAPI):
    from app.telegram_bot import telegram_bot
    telegram_bot.init(
        state=_state,
        start_fn=start,
        stop_fn=stop,
        arm_fn=arm,
        disarm_fn=disarm,
    )
    telegram_bot.start()
    try:
        yield
    finally:
        telegram_bot.stop()


app = FastAPI(title="Strategy Bot", version="1.0", lifespan=lifespan)

_state = SessionState()
_stop = threading.Event()
_thread: threading.Thread | None = None

# --- Runtime strategy overrides (changed via dashboard, read by session.py) ---
# These override .env / Settings defaults for the NEXT session start and, where
# possible, patch the running strategy in-flight.
# Persisted to disk so they survive server restarts.
import json as _json

_OVERRIDES_FILE = Path(__file__).resolve().parent.parent / "runtime" / "overrides.json"


def _load_overrides() -> dict:
    """Load runtime overrides from disk (if any)."""
    try:
        if _OVERRIDES_FILE.is_file():
            return _json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("failed to load overrides.json: %s", e)
    return {}


def _save_overrides() -> None:
    """Persist current runtime overrides to disk."""
    try:
        _OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OVERRIDES_FILE.write_text(
            _json.dumps(runtime_overrides, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("failed to save overrides.json: %s", e)


runtime_overrides: dict = _load_overrides()


@app.get("/api/status")
def status() -> dict:
    snap = _state.snapshot()
    snap["paper_mode"] = settings.paper_mode
    snap["kill_switch"] = settings.kill_switch
    snap["has_credentials"] = settings.has_credentials
    snap["session_ok"] = client.session_ok
    snap["session_running"] = bool(_thread and _thread.is_alive())
    comb = snap.get("combined") or {}
    comb_pct = comb.get("sl_pct") if comb.get("sl_pct") is not None else settings.strategy_combined_sl_pct
    comb_ent = comb.get("entry")
    if comb_pct is not None and comb_ent is not None:
        comb_pts = round(comb_ent * comb_pct, 2)
        comb["sl_pts"] = comb_pts
        comb["sl_level"] = round(comb_ent + comb_pts, 2)
    snap["config"] = {
        "max_daily_loss_pts": settings.max_daily_loss_pts,
        "max_lots": settings.max_lots,
        "max_entries_per_day": settings.max_entries_per_day,
        "no_new_entry_after": settings.no_new_entry_after.strftime("%H:%M"),
        "product_type": settings.product_type,
        "root_symbol": settings.root_symbol,
        "fno_exchange": settings.fno_exchange,
        "sl_pct": settings.strategy_sl_pct,
        "combined_sl_pct": settings.strategy_combined_sl_pct,
    }
    # Strategy rules: merge .env defaults with any runtime overrides
    from app.strategy.params import StraddleParams
    defaults = StraddleParams(
        lots=runtime_overrides.get("lots", 1),
        sl_pct=runtime_overrides.get("sl_pct", settings.strategy_sl_pct),
        combined_sl_pct=runtime_overrides.get("combined_sl_pct", settings.strategy_combined_sl_pct),
        sl_to_cost=runtime_overrides.get("sl_to_cost", settings.strategy_sl_to_cost),
        target_pct=runtime_overrides.get("target_pct", settings.strategy_target_pct),
        strike_offset=runtime_overrides.get("strike_offset", 0),
    )
    # Overlay entry/squareoff time from runtime overrides
    import datetime as _dt
    sp_dict = defaults.to_dict()
    if "entry_time" in runtime_overrides:
        sp_dict["entry_time"] = runtime_overrides["entry_time"]
    if "squareoff_time" in runtime_overrides:
        sp_dict["squareoff_time"] = runtime_overrides["squareoff_time"]
    snap["strategy_params"] = {
        **sp_dict,
        "strategy_type": "strangle" if defaults.strike_offset > 0 else "straddle",
    }
    snap["runtime_overrides"] = dict(runtime_overrides)
    return snap


@app.post("/api/arm")
def arm() -> JSONResponse:
    """Operator action: arm live trading. Refused unless PAPER_MODE is off."""
    if settings.paper_mode:
        return JSONResponse({"ok": False,
                             "reason": "PAPER_MODE is on - set PAPER_MODE=false "
                                       "in backend/.env and restart"},
                            status_code=409)
    if settings.kill_switch:
        return JSONResponse({"ok": False, "reason": "KILL_SWITCH is engaged"},
                            status_code=409)
    if not client.session_ok:
        return JSONResponse({"ok": False,
                             "reason": "no broker session - start the session "
                                       "first"},
                            status_code=409)
    ok = client.arm_live_trading()
    _state.update(live_armed=client.live_armed)
    return JSONResponse({"ok": ok, "live_armed": client.live_armed})


@app.post("/api/disarm")
def disarm() -> dict:
    client.disarm_live_trading()
    _state.update(live_armed=False)
    return {"ok": True, "live_armed": False}


@app.post("/api/start")
def start() -> JSONResponse:
    """Begin a session WITHOUT arming. Live mode stays disarmed until the
    operator presses ARM LIVE, so a session started hours before the open
    never carries an armed client around all morning.
    """
    global _thread
    if _thread and _thread.is_alive():
        return JSONResponse({"ok": False, "reason": "session already running"},
                            status_code=409)
    _stop.clear()
    _thread = threading.Thread(target=run_day,
                               kwargs={"live": False,
                                       "state": _state,
                                       "stop_event": _stop},
                               name="session", daemon=True)
    _thread.start()
    return JSONResponse({"ok": True, "paper_mode": settings.paper_mode,
                         "armed": client.live_armed})


@app.post("/api/stop")
def stop() -> dict:
    _stop.set()
    return {"ok": True}

class LoginCodeReq(BaseModel):
    auth_code: str
    uid: str = ""


class ConfigUpdateReq(BaseModel):
    lots: Optional[int] = None
    sl_pct: Optional[float] = None
    combined_sl_pct: Optional[float] = None
    sl_to_cost: Optional[bool] = None
    target_pct: Optional[float] = None
    strike_offset: Optional[int] = None
    strategy_type: Optional[str] = None  # "straddle" | "strangle"
    entry_time: Optional[str] = None     # "HH:MM"
    squareoff_time: Optional[str] = None # "HH:MM"


@app.post("/api/config")
def update_config(req: ConfigUpdateReq) -> JSONResponse:
    """Update runtime strategy parameters from the dashboard.

    Changes take effect on the NEXT session start. If a session is already
    running, lots cannot be changed mid-position (would desync qty), but SL %
    and strategy type are noted for next start.
    """
    changes = {}
    if req.lots is not None:
        if req.lots < 1 or req.lots > settings.max_lots:
            return JSONResponse(
                {"ok": False, "reason": f"lots must be 1–{settings.max_lots}"},
                status_code=400)
        runtime_overrides["lots"] = req.lots
        changes["lots"] = req.lots

    if req.sl_pct is not None:
        # Accept 0 to disable, or a positive decimal (e.g. 0.30 = 30%)
        if req.sl_pct < 0 or req.sl_pct > 5.0:
            return JSONResponse(
                {"ok": False, "reason": "sl_pct must be 0–5.0 (0=disabled, 0.30=30%)"},
                status_code=400)
        runtime_overrides["sl_pct"] = req.sl_pct if req.sl_pct > 0 else None
        changes["sl_pct"] = req.sl_pct

    if req.combined_sl_pct is not None:
        if req.combined_sl_pct < 0 or req.combined_sl_pct > 5.0:
            return JSONResponse(
                {"ok": False, "reason": "combined_sl_pct must be 0–5.0"},
                status_code=400)
        runtime_overrides["combined_sl_pct"] = req.combined_sl_pct if req.combined_sl_pct > 0 else None
        changes["combined_sl_pct"] = req.combined_sl_pct

    if req.sl_to_cost is not None:
        runtime_overrides["sl_to_cost"] = req.sl_to_cost
        changes["sl_to_cost"] = req.sl_to_cost

    if req.target_pct is not None:
        runtime_overrides["target_pct"] = req.target_pct if req.target_pct > 0 else None
        changes["target_pct"] = req.target_pct

    if req.strategy_type is not None:
        if req.strategy_type == "strangle":
            runtime_overrides["strike_offset"] = runtime_overrides.get("strike_offset", 1) or 1
        elif req.strategy_type == "straddle":
            runtime_overrides["strike_offset"] = 0
        changes["strategy_type"] = req.strategy_type

    if req.strike_offset is not None:
        if req.strike_offset < 0 or req.strike_offset > 10:
            return JSONResponse(
                {"ok": False, "reason": "strike_offset must be 0–10"},
                status_code=400)
        runtime_overrides["strike_offset"] = req.strike_offset
        changes["strike_offset"] = req.strike_offset

    import datetime as _dt
    for _field, _key in [(req.entry_time, "entry_time"),
                         (req.squareoff_time, "squareoff_time")]:
        if _field is not None:
            try:
                hh, mm = _field.strip().split(":")
                _dt.time(int(hh), int(mm))  # validate
                runtime_overrides[_key] = _field.strip()
                changes[_key] = _field.strip()
            except (ValueError, AttributeError):
                return JSONResponse(
                    {"ok": False, "reason": f"{_key} must be HH:MM"},
                    status_code=400)

    log.info("Runtime config updated: %s", changes)
    _save_overrides()
    return JSONResponse({"ok": True, "changes": changes,
                         "overrides": dict(runtime_overrides)})


@app.get("/api/journal")
def journal_info() -> dict:
    """Where the Excel journal lives and which days are already recorded."""
    return {
        "enabled": settings.journal_enabled,
        "dir": str(settings.journal_dir),
        "master": str(journal_mod.master_path()),
        "master_exists": journal_mod.master_path().is_file(),
        "days": journal_mod.available_days(),
        "latest": (str(journal_mod.latest_workbook())
                   if journal_mod.latest_workbook() else None),
        "daily": (_state.snapshot().get("journal_path")),
        "writes": _state.snapshot().get("journal_writes", 0),
        "write_failures": _state.snapshot().get("journal_failures", 0),
    }


@app.get("/api/journal/master")
def journal_master() -> FileResponse:
    """Download the running master sheet - every recorded day, one row each."""
    path = journal_mod.master_path()
    if not path.is_file():
        return JSONResponse({"ok": False,
                             "reason": "no master sheet yet - it is written "
                                       "when a day is finalised"},
                            status_code=404)
    return FileResponse(path, filename=path.name,
                        media_type=XLSX_MIME)


@app.get("/api/journal/daily")
def journal_download() -> FileResponse:
    """Download the most recent day's workbook - the report itself."""
    path = journal_mod.latest_workbook()
    if not path:
        return JSONResponse({"ok": False,
                             "reason": "no journal written yet - start a "
                                       "session and it appears on the first "
                                       "fill"}, status_code=404)
    return FileResponse(path, filename=path.name, media_type=XLSX_MIME)


@app.post("/api/journal/rebuild")
def journal_rebuild() -> JSONResponse:
    """Rebuild the master sheet from every daily workbook on disk.

    The per-day files are the record and the master is derived from them, so a
    master that got corrupted or hand-edited can always be regenerated -
    without this, one bad file would cost every day's summary row.
    """
    rows = []
    for day in journal_mod.available_days():
        try:
            data = journal_mod.load_day(day)
        except Exception as e:  # pragma: no cover - unreadable file
            return JSONResponse({"ok": False, "reason": f"{day}: {e}"},
                                status_code=500)
        positions = data.get(journal_mod.POSITIONS_SHEET) or []
        fills = data.get(journal_mod.FILLS_SHEET) or []
        orders = data.get(journal_mod.ORDERS_SHEET) or []
        summary = {str(r.get("field")): r.get("value")
                   for r in (data.get(journal_mod.SUMMARY_SHEET) or [])}
        rows.append(journal_mod.daily_row(
            day=day,
            status={"status": summary.get("Session status"),
                    "phase": summary.get("Strategy phase"),
                    "atm": summary.get("ATM pinned"),
                    "spot": summary.get("Spot ref"),
                    "live_armed": summary.get("Live armed") == "yes",
                    "entry_deferred": summary.get("Entry deferred") == "yes",
                    "entry_deferred_reason": summary.get("Entry deferred reason"),
                    "broker_rejections": summary.get("Strategy entry rejections"),
                    "errors": [summary.get("Recent strategy errors")]
                    if summary.get("Recent strategy errors") else [],
                    "error": summary.get("Session error"),
                    "resolved": {"expiry": summary.get("Expiry"),
                                 "lot_size": summary.get("Lot size")}},
            positions=positions, fills=fills, orders=orders,
            params={"lots": summary.get("Lots"),
                    "lot_size": summary.get("Lot size")},
            workbook=data.get("workbook")))
    if not rows:
        return JSONResponse({"ok": False,
                             "reason": "no daily workbooks found in "
                                       f"{journal_mod.journal_dir()}"},
                            status_code=404)
    out = journal_mod.write_master(rows, prefer_new=False)
    if not out:
        return JSONResponse({"ok": False,
                             "reason": "master workbook is locked (close it in "
                                       "Excel) or unwritable"}, status_code=500)
    return JSONResponse({"ok": True, "master": str(out), "days": len(rows)})


@app.get("/api/login/url")
def login_url() -> dict:
    """Generate the Shoonya OAuth authorize URL."""
    api, s = _make_api()
    url = api.getOAuthURL(s.oauth_url, s.client_id)
    return {"url": url, "client_id": s.client_id}


@app.post("/api/login/code")
def login_with_code(req: LoginCodeReq) -> JSONResponse:
    """Exchange authcode, write .env, hot-reload settings & broker session."""
    code = (req.auth_code or "").strip()
    if not code:
        return JSONResponse({"ok": False, "reason": "No auth code provided"}, status_code=400)
    try:
        res = exchange_and_save(code, req.uid.strip())
        update_access_token(res["access_token"], res["uid"], res["account_id"])
        client.refresh_session(res["access_token"], res["uid"], res["account_id"])
        return JSONResponse({
            "ok": True,
            "message": "Token refreshed and validated successfully!",
            "uid": res["uid"],
            "account_id": res["account_id"],
            "session_ok": client.session_ok,
        })
    except Exception as e:
        log.exception("Token exchange failed")
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)


@app.post("/api/login/auto")
def login_auto() -> JSONResponse:
    """Run automated zero-touch login via Playwright & TOTP."""
    from app.broker.auto_login import perform_auto_login
    try:
        res = perform_auto_login()
        return JSONResponse({
            "ok": True,
            "message": "Automated login completed successfully!",
            "uid": res["uid"],
            "account_id": res["account_id"],
            "session_ok": res["session_ok"],
            "limits": res.get("limits"),
        })
    except Exception as e:
        log.exception("Automated login failed")
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)


@app.post("/api/server/restart")
def restart_server() -> JSONResponse:
    """Restarts the server process cleanly in the background."""
    import subprocess
    import os
    from pathlib import Path

    current_pid = os.getpid()
    log.warning("Server restart requested via API (PID %d)", current_pid)
    
    ps_script = Path(__file__).resolve().parent.parent / "scripts" / "restart_helper.ps1"
    
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ps_script), "-PidToKill", str(current_pid)
    ]
    subprocess.Popen(
        cmd,
        creationflags=flags,
        close_fds=True,
    )
    return JSONResponse({"ok": True, "message": "Server restarting in background..."})


@app.post("/api/server/shutdown")
def shutdown_server() -> JSONResponse:
    """Kills / terminates the server process cleanly."""
    import subprocess
    import os

    current_pid = os.getpid()
    log.warning("Server shutdown requested via API (PID %d)", current_pid)

    DETACHED_PROCESS = 0x00000008
    flags = DETACHED_PROCESS

    cmd = f"ping 127.0.0.1 -n 2 >nul & taskkill /F /PID {current_pid}"
    subprocess.Popen(["cmd.exe", "/c", cmd], creationflags=flags, close_fds=True)
    return JSONResponse({"ok": True, "message": "Server shutting down..."})


@app.get("/api/telegram/status")
def telegram_status() -> dict:
    from app.telegram_bot import telegram_bot
    return telegram_bot.status_dict()


@app.post("/api/telegram/test")
def telegram_test() -> JSONResponse:
    from app.telegram_bot import telegram_bot
    ok, msg = telegram_bot.send_test_message()
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)





@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/static/app.js")
def app_js() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "static" / "app.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

