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

log = logging.getLogger("api")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Excel's own content type: without it browsers open the workbook as a broken
# text file instead of offering a download.
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")

app = FastAPI(title="Strategy Bot", version="1.0")

_state = SessionState()
_stop = threading.Event()
_thread: threading.Thread | None = None


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
        target_sl = round(comb_ent * comb_pct, 2)
        comb["sl_level"] = target_sl
        for leg in (snap.get("legs") or {}).values():
            if leg.get("sl_level") is None:
                leg["sl_level"] = target_sl
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




@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/static/app.js")
def app_js() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "static" / "app.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

