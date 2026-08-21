from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import CLAIM_COOKIE
from app.dongle.manager import DongleManager
from app.macros.engine import (
    MacroEngine,
    delete_user_macro,
    get_macro,
    list_macros,
    list_runs,
    load_run_log,
    save_user_macro,
)
from app.sessions import Session, SessionStore

log = logging.getLogger(__name__)
router = APIRouter()


class Hub:
    def __init__(self) -> None:
        self.manager = DongleManager()
        self.sessions = SessionStore()
        self.macros = MacroEngine()
        self.sockets: dict[str, list[WebSocket]] = {}

    def session_from_request(self, request: Request) -> tuple[Session, bool]:
        return self.sessions.get_or_create(request.cookies.get(CLAIM_COOKIE))

    def attach_cookie(self, response: JSONResponse, session: Session, created: bool) -> JSONResponse:
        if created:
            response.set_cookie(CLAIM_COOKIE, session.id, httponly=False, samesite="lax")
        return response

    def require_owner(self, station_id: str, session: Session) -> None:
        if not self.sessions.is_owner(station_id, session.id):
            raise HTTPException(status_code=403, detail="Claim this station first")

    def station(self, station_id: str):
        try:
            return self.manager.get(station_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown station")

    def station_public(self, station, session: Session | None) -> dict[str, Any]:
        snap = station.snapshot()
        snap["claim"] = self.sessions.public_claim(station.id, session)
        snap["devices"] = list(station.devices.values())
        return snap

    async def broadcast(self, station_id: str, event: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.sockets.get(station_id) or []):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        if dead:
            self.sockets[station_id] = [w for w in self.sockets.get(station_id, []) if w not in dead]


hub = Hub()


def _json(request: Request, payload: Any, status: int = 200) -> JSONResponse:
    session, created = hub.session_from_request(request)
    resp = JSONResponse(payload, status_code=status)
    return hub.attach_cookie(resp, session, created)


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "mock": hub.manager.mock, "stations": len(hub.manager.stations)}


@router.get("/api/stations")
async def stations(request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    payload = {
        "mock": hub.manager.mock,
        "session_id": session.id,
        "claimed": session.station_id,
        "stations": [hub.station_public(s, session) for s in hub.manager.all()],
    }
    return hub.attach_cookie(JSONResponse(payload), session, created)


@router.post("/api/stations/refresh")
async def refresh(request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    result = await hub.manager.refresh()
    result["stations"] = [hub.station_public(s, session) for s in hub.manager.all()]
    return hub.attach_cookie(JSONResponse(result), session, created)


@router.post("/api/heartbeat")
async def heartbeat(request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    return hub.attach_cookie(JSONResponse({"ok": True, "station": session.station_id}), session, created)


async def _idle_station(station_id: str) -> None:
    try:
        station = hub.manager.get(station_id)
    except KeyError:
        return
    try:
        if station.macro_run_id:
            hub.macros.request_cancel(station_id)
        await station.reset_idle()
    except Exception:
        log.exception("failed to idle station %s", station_id)


@router.post("/api/stations/{station_id}/claim")
async def claim(station_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    hub.station(station_id)
    previous = session.station_id
    try:
        hub.sessions.claim(station_id, session)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if previous and previous != station_id:
        await _idle_station(previous)
    return hub.attach_cookie(
        JSONResponse({"ok": True, "station": hub.station_public(hub.station(station_id), session)}),
        session,
        created,
    )


@router.post("/api/stations/{station_id}/release")
async def release(station_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    station = hub.station(station_id)
    try:
        hub.sessions.release(station_id, session)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    await _idle_station(station_id)
    return hub.attach_cookie(
        JSONResponse({"ok": True, "station": hub.station_public(station, session)}),
        session,
        created,
    )


def _owner_station(request: Request, station_id: str):
    session, _created = hub.session_from_request(request)
    station = hub.station(station_id)
    hub.require_owner(station_id, session)
    if station.macro_run_id:
        raise HTTPException(status_code=409, detail="Macro is running; cancel it first")
    return session, station


@router.post("/api/stations/{station_id}/scan/start")
async def scan_start(station_id: str, request: Request) -> JSONResponse:
    session, station = _owner_station(request, station_id)
    body = await _body(request)
    duration = int(body.get("duration") or 0)
    filt = str(body.get("filter") or "")
    await station.start_scan(duration=duration, filter_hex=filt)
    return _json(request, {"ok": True, "scanning": True})


@router.post("/api/stations/{station_id}/scan/stop")
async def scan_stop(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    await station.stop_scan()
    return _json(request, {"ok": True, "scanning": False})


@router.post("/api/stations/{station_id}/scantarget")
async def scantarget(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    addr = body.get("address")
    if not addr:
        raise HTTPException(status_code=400, detail="address required")
    await station.scantarget(str(addr), int(body.get("duration") or 0))
    return _json(request, {"ok": True})


@router.post("/api/stations/{station_id}/connect")
async def connect(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    addr = body.get("address")
    if not addr:
        raise HTTPException(status_code=400, detail="address required")
    try:
        result = await station.connect(str(addr))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _json(request, result)


@router.post("/api/stations/{station_id}/disconnect")
async def disconnect(station_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    station = hub.station(station_id)
    hub.require_owner(station_id, session)
    result = await station.disconnect()
    return hub.attach_cookie(JSONResponse(result), session, created)


@router.post("/api/stations/{station_id}/read")
async def read_char(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    target = body.get("handle") or body.get("uuid")
    if not target:
        raise HTTPException(status_code=400, detail="handle or uuid required")
    result = await station.read(str(target))
    return _json(request, result)


@router.post("/api/stations/{station_id}/write")
async def write_char(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    target = body.get("handle") or body.get("uuid")
    if not target:
        raise HTTPException(status_code=400, detail="handle or uuid required")
    as_hex = bool(body.get("hex") is not None or body.get("as_hex", True))
    data = body.get("hex") if body.get("hex") is not None else body.get("ascii") or body.get("data") or ""
    if body.get("ascii") and body.get("hex") is None:
        as_hex = False
    result = await station.write(
        str(target),
        str(data),
        as_hex=as_hex,
        without_response=bool(body.get("without_response")),
    )
    return _json(request, result)


@router.post("/api/stations/{station_id}/notify")
async def notify_char(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    target = body.get("handle") or body.get("uuid")
    if not target:
        raise HTTPException(status_code=400, detail="handle or uuid required")
    result = await station.set_notify(
        str(target),
        bool(body.get("enable", True)),
        indicate=bool(body.get("indicate")),
    )
    return _json(request, result)


@router.post("/api/stations/{station_id}/passkey")
async def passkey(station_id: str, request: Request) -> JSONResponse:
    _session, station = _owner_station(request, station_id)
    body = await _body(request)
    key = str(body.get("passkey") or "")
    if len(key) != 6:
        raise HTTPException(status_code=400, detail="6-digit passkey required")
    result = await station.enter_passkey(key)
    return _json(request, result)


@router.get("/api/macros")
async def macros() -> dict[str, Any]:
    return {"macros": list_macros()}


@router.get("/api/macros/{macro_id}")
async def macro_get(macro_id: str) -> dict[str, Any]:
    try:
        return get_macro(macro_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown macro")


@router.put("/api/macros/{macro_id}")
async def macro_put(macro_id: str, request: Request) -> dict[str, Any]:
    body = await _body(request)
    return save_user_macro(macro_id, body)


@router.delete("/api/macros/{macro_id}")
async def macro_delete(macro_id: str) -> dict[str, Any]:
    try:
        delete_user_macro(macro_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown macro")
    return {"ok": True}


@router.post("/api/stations/{station_id}/macros/{macro_id}/run")
async def macro_run(station_id: str, macro_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    station = hub.station(station_id)
    hub.require_owner(station_id, session)
    try:
        macro = get_macro(macro_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown macro")
    body = await _body(request)
    params = body.get("params") if isinstance(body.get("params"), dict) else body

    def emit(event: dict[str, Any]) -> None:
        event.setdefault("station_id", station_id)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(hub.broadcast(station_id, event))

    try:
        run = await hub.macros.start(station, macro, params or {}, emit)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return hub.attach_cookie(JSONResponse(run.public()), session, created)


@router.post("/api/stations/{station_id}/macros/cancel")
async def macro_cancel(station_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    hub.require_owner(station_id, session)
    hub.macros.request_cancel(station_id)
    return hub.attach_cookie(JSONResponse({"ok": True}), session, created)


@router.post("/api/stations/{station_id}/macros/continue")
async def macro_continue(station_id: str, request: Request) -> JSONResponse:
    session, created = hub.session_from_request(request)
    hub.require_owner(station_id, session)
    hub.macros.request_continue(station_id)
    return hub.attach_cookie(JSONResponse({"ok": True}), session, created)


@router.get("/api/runs")
async def runs() -> dict[str, Any]:
    return {"runs": list_runs(hub.macros)}


@router.get("/api/runs/{run_id}")
async def run_get(run_id: str) -> dict[str, Any]:
    run = hub.macros.runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown run")
    try:
        log_records = load_run_log(run_id)
    except KeyError:
        log_records = []
    payload = run.public()
    payload["log"] = log_records
    return payload


@router.get("/api/runs/{run_id}/log")
async def run_log(run_id: str) -> PlainTextResponse:
    try:
        records = load_run_log(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown run log")
    text = "\n".join(json_line(r) for r in records) + "\n"
    return PlainTextResponse(text, media_type="application/jsonl")


def json_line(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


@router.websocket("/api/stations/{station_id}/ws")
async def station_ws(websocket: WebSocket, station_id: str) -> None:
    await websocket.accept()
    session_id = websocket.cookies.get(CLAIM_COOKIE)
    session = hub.sessions.get(session_id) if session_id else None
    if not session or not hub.sessions.is_owner(station_id, session.id):
        await websocket.send_json({"type": "error", "message": "Claim this station first"})
        await websocket.close(code=4403)
        return
    station = hub.station(station_id)
    hub.sockets.setdefault(station_id, []).append(websocket)
    session.touch()

    async def on_event(event: dict[str, Any]) -> None:
        try:
            await websocket.send_json(event)
        except Exception:
            pass

    unsub = station.subscribe(on_event)
    try:
        await websocket.send_json({"type": "hello", "station": hub.station_public(station, session)})
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                session.touch()
                await websocket.send_json({"type": "ping"})
                continue
            session.touch()
            if isinstance(msg, dict) and msg.get("type") == "pong":
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws closed", exc_info=True)
    finally:
        unsub()
        hub.sockets[station_id] = [w for w in hub.sockets.get(station_id, []) if w is not websocket]


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


async def idle_reaper() -> None:
    while True:
        await asyncio.sleep(15)
        released = hub.sessions.expire_idle()
        for station_id in released:
            try:
                station = hub.manager.get(station_id)
            except KeyError:
                continue
            try:
                await _idle_station(station_id)
            except Exception:
                pass
            await hub.broadcast(station_id, {"type": "released", "reason": "idle"})
            await hub.broadcast(station_id, {"type": "station", "station": station.snapshot()})


def mount(app: FastAPI) -> None:
    app.include_router(router)
