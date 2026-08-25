from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.config import LOGS_DIR, MACROS_DIR, MACRO_STEP_TIMEOUT_SEC
from app.dongle.base import Station
from app.dongle.parser import advertised_uuids, flatten_characteristics, gatt_uuids, normalize_addr, resolve_handle
from app.gatt_names import lookup_name, normalize_uuid
from app.macros.builtins import builtin_macros

log = logging.getLogger(__name__)
MAX_STORED_RUNS = 80


def subst(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            key = match.group(1)
            if key in ctx and ctx[key] is not None:
                return str(ctx[key])
            return match.group(0)

        return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, value)
    if isinstance(value, list):
        return [subst(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: subst(v, ctx) for k, v in value.items()}
    return value


def load_macro_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("id", path.stem)
    data.setdefault("name", path.stem)
    data.setdefault("params", {})
    data.setdefault("steps", [])
    data["builtin"] = False
    data["path"] = str(path)
    return data


def list_user_macros() -> list[dict[str, Any]]:
    MACROS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(MACROS_DIR.glob("*.json")):
        try:
            out.append(load_macro_file(path))
        except Exception as exc:
            log.warning("skip macro %s: %s", path, exc)
    return out


def list_macros() -> list[dict[str, Any]]:
    by_id = {m["id"]: m for m in builtin_macros()}
    for m in list_user_macros():
        by_id[m["id"]] = m
    return list(by_id.values())


def get_macro(macro_id: str) -> dict[str, Any]:
    for m in list_macros():
        if m["id"] == macro_id:
            return m
    raise KeyError(macro_id)


def save_user_macro(macro_id: str, body: dict[str, Any]) -> dict[str, Any]:
    MACROS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", macro_id).strip("-") or "macro"
    data = {
        "id": safe,
        "name": body.get("name") or safe,
        "description": body.get("description") or "",
        "params": body.get("params") or {},
        "steps": body.get("steps") or [],
    }
    path = MACROS_DIR / f"{safe}.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    data["builtin"] = False
    data["path"] = str(path)
    return data


def delete_user_macro(macro_id: str) -> None:
    path = MACROS_DIR / f"{macro_id}.json"
    if path.exists():
        path.unlink()
    else:
        raise KeyError(macro_id)


def advertised_vs_gatt(station: Station) -> dict[str, Any]:
    addr = station.connected_addr
    device = station.devices.get(addr) if addr else None
    adv_ids: list[str] = []
    if device:
        adv_ids = list(device.get("uuids") or [])
        for field in (device.get("fields") or []) + (device.get("scan_rsp_fields") or []):
            for u in field.get("uuids") or []:
                if u not in adv_ids:
                    adv_ids.append(u)
            if field.get("uuid") and field["uuid"] not in adv_ids:
                adv_ids.append(field["uuid"])
    gatt_ids = gatt_uuids(station.services)
    adv_norm = {normalize_uuid(u) for u in adv_ids}
    missing_in_gatt = sorted(u for u in adv_norm if u and u not in gatt_ids)
    extra_in_gatt = sorted(
        u
        for u in gatt_ids
        if u
        and u not in adv_norm
        and normalize_uuid(u) not in {"1800", "1801"}
        and len(normalize_uuid(u)) <= 8
    )
    return {
        "ok": True,
        "advertised": [{"uuid": u, "name": lookup_name(u)} for u in sorted(adv_norm) if u],
        "gatt": [{"uuid": u, "name": lookup_name(u)} for u in sorted(gatt_ids)],
        "advertised_missing_from_gatt": [{"uuid": u, "name": lookup_name(u)} for u in missing_in_gatt],
        "gatt_not_in_advertising": [{"uuid": u, "name": lookup_name(u)} for u in extra_in_gatt],
    }


async def op_read_all(station: Station) -> dict[str, Any]:
    results = []
    for char in flatten_characteristics(station.services):
        flags = char.get("properties", {}).get("flags", {})
        if not flags.get("read"):
            results.append(
                {
                    "handle": char.get("handle"),
                    "uuid": char.get("uuid"),
                    "name": char.get("name"),
                    "skipped": True,
                    "reason": "not readable",
                }
            )
            continue
        results.append(await station.read(char["handle"]))
    return {"ok": True, "results": results}


async def op_write_all(station: Station, hex_value: str = "00", restore: bool = False) -> dict[str, Any]:
    results = []
    for char in flatten_characteristics(station.services):
        flags = char.get("properties", {}).get("flags", {})
        writable = flags.get("write") or flags.get("write_without_response")
        if not writable:
            results.append(
                {
                    "handle": char.get("handle"),
                    "uuid": char.get("uuid"),
                    "name": char.get("name"),
                    "skipped": True,
                    "reason": "not writable",
                }
            )
            continue
        original = None
        if restore and flags.get("read"):
            original = await station.read(char["handle"])
        without = bool(flags.get("write_without_response") and not flags.get("write"))
        written = await station.write(char["handle"], hex_value, as_hex=True, without_response=without)
        restored = None
        if restore and original and original.get("ok") and original.get("hex"):
            restored = await station.write(
                char["handle"], original["hex"], as_hex=True, without_response=without
            )
        results.append({"write": written, "original": original, "restored": restored})
    return {"ok": True, "results": results}


async def op_probe_permissions(station: Station, hex_value: str = "00") -> dict[str, Any]:
    results = []
    for char in flatten_characteristics(station.services):
        flags = char.get("properties", {}).get("flags", {})
        expected_r = bool(flags.get("read"))
        expected_w = bool(flags.get("write") or flags.get("write_without_response"))
        read_res = await station.read(char["handle"])
        without = bool(flags.get("write_without_response") and not flags.get("write"))
        write_res = await station.write(char["handle"], hex_value, as_hex=True, without_response=without)
        if read_res.get("ok") and read_res.get("hex") and write_res.get("ok"):
            try:
                await station.write(char["handle"], read_res["hex"], as_hex=True, without_response=without)
            except Exception:
                pass
        actual_r = bool(read_res.get("ok"))
        actual_w = bool(write_res.get("ok"))
        results.append(
            {
                "handle": char.get("handle"),
                "uuid": char.get("uuid"),
                "name": char.get("name"),
                "properties": char.get("properties"),
                "expected_read": expected_r,
                "actual_read": actual_r,
                "read": read_res,
                "expected_write": expected_w,
                "actual_write": actual_w,
                "write": write_res,
                "mismatch": (expected_r != actual_r) or (expected_w != actual_w),
            }
        )
    mismatches = [r for r in results if r.get("mismatch")]
    return {"ok": True, "results": results, "mismatch_count": len(mismatches)}


async def op_verify_adv(station: Station, step: dict[str, Any]) -> dict[str, Any]:
    addr = step.get("address") or station.connected_addr
    device = None
    if addr:
        want = normalize_addr(str(addr)) if addr else ""
        device = station.devices.get(want)
        if not device:
            for key, val in station.devices.items():
                if str(addr).upper() in key.upper():
                    device = val
                    break
    if not device and len(station.devices) == 1:
        device = next(iter(station.devices.values()))
    if not device:
        raise RuntimeError("No advertised data captured for target device — scan first")
    fields = (device.get("fields") or []) + (device.get("scan_rsp_fields") or [])
    errors = []
    expected_name = str(step.get("name") or "").strip()
    if expected_name and (device.get("name") or "").lower() != expected_name.lower():
        errors.append(f"name {device.get('name')!r} != {expected_name!r}")
    uuid_raw = step.get("uuids") or []
    if isinstance(uuid_raw, str):
        uuid_raw = [u.strip() for u in uuid_raw.split(",") if u.strip()]
    expected_uuids = [normalize_uuid(u) for u in uuid_raw]
    have = {normalize_uuid(u) for u in (device.get("uuids") or advertised_uuids(fields))}
    for u in expected_uuids:
        if u not in have:
            errors.append(f"missing advertised UUID {u}")
    company = str(step.get("company_id") or "").strip()
    if company:
        blob = json.dumps(device).upper()
        if company.replace("0x", "").upper() not in blob:
            errors.append(f"company id {company} not in advertisement")
    contains = str(step.get("contains") or "").replace(" ", "").lower()
    blob_hex = ((device.get("adv_hex") or "") + (device.get("scan_rsp_hex") or "")).lower()
    if contains and contains not in blob_hex:
        errors.append(f"adv hex does not contain {contains}")
    return {
        "ok": not errors,
        "error": "; ".join(errors) if errors else None,
        "device": {
            "addr": device.get("addr"),
            "name": device.get("name"),
            "rssi": device.get("rssi"),
            "adv_hex": device.get("adv_hex"),
            "scan_rsp_hex": device.get("scan_rsp_hex"),
            "fields": device.get("fields"),
            "uuids": device.get("uuids"),
        },
    }


class MacroRun:
    def __init__(self, run_id: str, station_id: str, macro: dict[str, Any], params: dict[str, Any]):
        self.id = run_id
        self.station_id = station_id
        self.macro_id = macro["id"]
        self.macro_name = macro.get("name") or macro["id"]
        self.status = "running"
        self.started = time.time()
        self.finished: float | None = None
        self.params = params
        self.steps: list[dict[str, Any]] = []
        self.error: str | None = None
        self.log_path = LOGS_DIR / f"{run_id}.jsonl"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "macro_id": self.macro_id,
            "macro_name": self.macro_name,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "params": self.params,
            "steps": self.steps,
            "error": self.error,
            "log_path": str(self.log_path),
        }

    def append(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


class MacroEngine:
    def __init__(self) -> None:
        self.runs: dict[str, MacroRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # station_id -> task
        self._pause: dict[str, asyncio.Event] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    def active_run(self, station_id: str) -> MacroRun | None:
        for run in self.runs.values():
            if run.station_id == station_id and run.status in {"running", "paused"}:
                return run
        return None

    async def start(
        self,
        station: Station,
        macro: dict[str, Any],
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
    ) -> MacroRun:
        existing = self.active_run(station.id)
        if existing:
            raise RuntimeError(f"Macro {existing.macro_id} already running")
        run = MacroRun(uuid.uuid4().hex[:12], station.id, macro, params)
        self.runs[run.id] = run
        station.macro_run_id = run.id
        self._pause[station.id] = asyncio.Event()
        self._cancel[station.id] = asyncio.Event()
        self._pause[station.id].set()
        task = asyncio.create_task(self._execute(station, macro, run, emit), name=f"macro-{run.id}")
        self._tasks[station.id] = task
        return run

    def request_cancel(self, station_id: str) -> None:
        ev = self._cancel.get(station_id)
        if ev:
            ev.set()
        pause = self._pause.get(station_id)
        if pause:
            pause.set()

    def request_continue(self, station_id: str) -> None:
        ev = self._pause.get(station_id)
        if ev:
            ev.set()

    def _prune_runs(self) -> None:
        if len(self.runs) <= MAX_STORED_RUNS:
            return
        active = {r.id for r in self.runs.values() if r.status in {"running", "paused"}}
        finished = sorted(
            (r for r in self.runs.values() if r.id not in active),
            key=lambda r: r.started,
        )
        excess = len(self.runs) - MAX_STORED_RUNS
        for run in finished[:excess]:
            self.runs.pop(run.id, None)

    def _emit(self, emit: Callable, event: dict[str, Any]) -> None:
        try:
            emit(event)
        except Exception:
            log.debug("macro emit failed", exc_info=True)

    async def _execute(
        self,
        station: Station,
        macro: dict[str, Any],
        run: MacroRun,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        ctx: dict[str, Any] = dict(macro.get("params") or {})
        ctx.update(run.params or {})
        if station.connected_addr and not ctx.get("address"):
            ctx["address"] = station.connected_addr
        self._emit(
            emit,
            {
                "type": "macro_started",
                "run_id": run.id,
                "macro_id": macro["id"],
                "name": macro.get("name"),
                "steps": macro.get("steps") or [],
            },
        )
        run.append({"event": "started", "macro": macro["id"], "params": ctx})
        notify_waiters: list[asyncio.Future] = []
        adv_waiters: list[asyncio.Future] = []

        def on_station_event(event: dict[str, Any]) -> None:
            if event.get("type") == "notify":
                for fut in list(notify_waiters):
                    if not fut.done():
                        fut.set_result(event)
            if event.get("type") in {"scan", "adv"}:
                for fut in list(adv_waiters):
                    if not fut.done():
                        fut.set_result(event)

        unsub = station.subscribe(on_station_event)
        try:
            steps = list(macro.get("steps") or [])
            for index, raw_step in enumerate(steps):
                if self._cancel.get(station.id) and self._cancel[station.id].is_set():
                    run.status = "cancelled"
                    break
                step = subst(raw_step, ctx)
                op = (step.get("op") or "").strip()
                rec = {"index": index, "op": op, "step": step, "status": "running"}
                run.steps.append(rec)
                self._emit(emit, {"type": "step_started", "run_id": run.id, "index": index, "step": step})
                run.append({"event": "step_started", "index": index, "step": step})
                try:
                    result = await asyncio.wait_for(
                        self._run_step(station, step, ctx, run, emit, notify_waiters, adv_waiters),
                        timeout=float(step.get("timeout_s") or MACRO_STEP_TIMEOUT_SEC),
                    )
                    rec["result"] = result
                    rec["status"] = "ok" if result.get("ok", True) else "error"
                    if result.get("error") and rec["status"] == "ok":
                        rec["status"] = "error"
                    if rec["status"] == "error" and not step.get("continue_on_error"):
                        run.status = "failed"
                        run.error = result.get("error") or f"step {op} failed"
                        self._emit(
                            emit,
                            {
                                "type": "step_result",
                                "run_id": run.id,
                                "index": index,
                                "ok": False,
                                "result": result,
                            },
                        )
                        run.append({"event": "step_result", "index": index, "result": result})
                        break
                    if step.get("save_as") and result.get("hex") is not None:
                        ctx[str(step["save_as"])] = result.get("hex")
                        ctx[str(step["save_as"]) + "_ascii"] = result.get("ascii")
                    ctx["last"] = result
                    if result.get("hex") is not None:
                        ctx["last_read"] = result.get("hex")
                    self._emit(
                        emit,
                        {
                            "type": "step_result",
                            "run_id": run.id,
                            "index": index,
                            "ok": rec["status"] != "error",
                            "result": result,
                        },
                    )
                    run.append({"event": "step_result", "index": index, "result": result})
                except Exception as exc:
                    rec["status"] = "error"
                    rec["result"] = {"ok": False, "error": str(exc)}
                    run.append({"event": "step_error", "index": index, "error": str(exc)})
                    self._emit(
                        emit,
                        {
                            "type": "step_result",
                            "run_id": run.id,
                            "index": index,
                            "ok": False,
                            "result": rec["result"],
                        },
                    )
                    if not step.get("continue_on_error"):
                        run.status = "failed"
                        run.error = str(exc)
                        break
            else:
                if run.status == "running":
                    run.status = "ok"
        finally:
            unsub()
            if run.status == "running":
                run.status = "cancelled" if (self._cancel.get(station.id) and self._cancel[station.id].is_set()) else "ok"
            run.finished = time.time()
            station.macro_run_id = None
            self._tasks.pop(station.id, None)
            self._pause.pop(station.id, None)
            self._cancel.pop(station.id, None)
            self._emit(
                emit,
                {
                    "type": "macro_finished",
                    "run_id": run.id,
                    "status": run.status,
                    "error": run.error,
                    "run": run.public(),
                },
            )
            run.append({"event": "finished", "status": run.status, "error": run.error})
            self._prune_runs()
            station.emit("station", station=station.snapshot())

    async def _run_step(
        self,
        station: Station,
        step: dict[str, Any],
        ctx: dict[str, Any],
        run: MacroRun,
        emit: Callable,
        notify_waiters: list[asyncio.Future],
        adv_waiters: list[asyncio.Future],
    ) -> dict[str, Any]:
        op = step.get("op")
        if op == "log":
            return {"ok": True, "message": step.get("message") or step.get("text") or ""}
        if op == "delay":
            ms = float(step.get("timeout_ms") or step.get("ms") or 1000)
            await asyncio.sleep(ms / 1000.0)
            return {"ok": True, "ms": ms}
        if op == "pause":
            message = step.get("message") or "Continue when ready"
            pause_ev = self._pause.get(station.id)
            cancel_ev = self._cancel.get(station.id)
            if pause_ev:
                pause_ev.clear()
            run.status = "paused"
            self._emit(
                emit,
                {
                    "type": "macro_paused",
                    "run_id": run.id,
                    "message": message,
                },
            )
            while True:
                if cancel_ev and cancel_ev.is_set():
                    return {"ok": False, "error": "cancelled"}
                if pause_ev and pause_ev.is_set():
                    break
                await asyncio.sleep(0.1)
            run.status = "running"
            return {"ok": True, "message": message}
        if op == "scan":
            duration = int(step.get("duration") or step.get("timeout_s") or 5)
            filt = str(step.get("filter") or "")
            await station.start_scan(duration=duration, filter_hex=filt)
            await asyncio.sleep(duration + 0.3)
            try:
                await station.stop_scan()
            except Exception:
                pass
            return {"ok": True, "devices": list(station.devices.values())}
        if op == "connect":
            addr = step.get("address") or ctx.get("address")
            if not addr:
                raise RuntimeError("connect requires address")
            return await station.connect(str(addr))
        if op == "disconnect":
            return await station.disconnect()
        if op == "read":
            target = step.get("uuid") or step.get("handle")
            return await station.read(str(target))
        if op in {"write", "write_cmd"}:
            target = step.get("uuid") or step.get("handle")
            as_hex = "hex" in step or not step.get("ascii")
            data = step.get("hex") if as_hex else step.get("ascii") or step.get("data") or ""
            without = op == "write_cmd" or bool(step.get("without_response"))
            return await station.write(str(target), str(data), as_hex=as_hex, without_response=without)
        if op in {"notify_on", "notify_off", "indicate_on", "indicate_off"}:
            target = step.get("uuid") or step.get("handle")
            enable = op.endswith("_on")
            indicate = op.startswith("indicate")
            return await station.set_notify(str(target), enable, indicate=indicate)
        if op == "wait_notify":
            timeout = float(step.get("timeout_ms") or 5000) / 1000.0
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            notify_waiters.append(fut)
            try:
                event = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return {"ok": False, "error": "wait_notify timed out"}
            finally:
                if fut in notify_waiters:
                    notify_waiters.remove(fut)
            want = (step.get("uuid") or step.get("handle") or "").lower()
            if want:
                got = str(event.get("uuid") or event.get("handle") or "").lower()
                if want not in got and got not in want:
                    # still accept; filtering is best-effort
                    pass
            ctx["last_notify"] = event.get("hex")
            contains = str(step.get("contains") or "").replace(" ", "").lower()
            if contains and contains not in str(event.get("hex") or ""):
                return {"ok": False, "error": "notify payload mismatch", "event": event}
            return {"ok": True, "event": event, "hex": event.get("hex"), "ascii": event.get("ascii")}
        if op == "wait_adv":
            timeout = float(step.get("timeout_ms") or 8000) / 1000.0
            contains = str(step.get("contains") or "").replace(" ", "").lower()
            loop = asyncio.get_running_loop()
            deadline = time.time() + timeout
            if not station.scanning:
                await station.start_scan(duration=0, filter_hex=contains)
            while time.time() < deadline:
                fut = loop.create_future()
                adv_waiters.append(fut)
                try:
                    event = await asyncio.wait_for(fut, timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                finally:
                    if fut in adv_waiters:
                        adv_waiters.remove(fut)
                device = event.get("device") or {}
                blob = ((device.get("adv_hex") or "") + (device.get("scan_rsp_hex") or "")).lower()
                addr_ok = True
                if step.get("address"):
                    addr_ok = str(step["address"]).upper() in str(device.get("addr") or "").upper()
                if addr_ok and (not contains or contains in blob):
                    try:
                        await station.stop_scan()
                    except Exception:
                        pass
                    return {"ok": True, "device": device}
            try:
                await station.stop_scan()
            except Exception:
                pass
            return {"ok": False, "error": "wait_adv timed out"}
        if op == "read_all":
            return await op_read_all(station)
        if op == "write_all":
            restore = step.get("restore")
            if isinstance(restore, str):
                restore = restore.strip().lower() in {"1", "true", "yes"}
            return await op_write_all(station, str(step.get("hex") or "00"), bool(restore))
        if op == "probe_permissions":
            return await op_probe_permissions(station, str(step.get("hex") or "00"))
        if op == "verify_adv":
            return await op_verify_adv(station, step)
        if op == "advertised_vs_gatt":
            return advertised_vs_gatt(station)
        if op == "assert":
            return _run_assert(station, step, ctx)
        if op == "wait_passkey":
            return {"ok": True, "message": "Enter passkey in the UI if prompted"}
        raise RuntimeError(f"Unknown macro op: {op}")


def _run_assert(station: Station, step: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    last = ctx.get("last") or {}
    if "equals" in step:
        source = step.get("source") or "last_read"
        got = str(ctx.get(source) or last.get("hex") or "")
        expect = str(step.get("equals") or "")
        ok = got.lower() == expect.lower().replace(" ", "")
        return {"ok": ok, "error": None if ok else f"{source} {got!r} != {expect!r}", "got": got}
    if "contains" in step:
        source = step.get("source") or "last_read"
        got = str(ctx.get(source) or last.get("hex") or "")
        expect = str(step.get("contains") or "").replace(" ", "").lower()
        ok = expect in got.lower()
        return {"ok": ok, "error": None if ok else f"{source} does not contain {expect}", "got": got}
    if step.get("adv_uuid"):
        info = advertised_vs_gatt(station)
        have = {normalize_uuid(x["uuid"]) for x in info.get("advertised") or []}
        u = normalize_uuid(str(step["adv_uuid"]))
        ok = u in have
        return {"ok": ok, "error": None if ok else f"advertised UUID {u} missing"}
    if step.get("properties") and (step.get("uuid") or step.get("handle")):
        char = resolve_handle(station.services, str(step.get("uuid") or step.get("handle")))
        if not char:
            return {"ok": False, "error": "characteristic not found"}
        flags = char.get("properties", {}).get("flags", {})
        need = str(step["properties"]).upper()
        mapping = {"R": "read", "W": "write", "N": "notify", "I": "indicate", "X": "write_without_response"}
        missing = [ch for ch, name in mapping.items() if ch in need and not flags.get(name)]
        return {"ok": not missing, "error": None if not missing else f"missing properties {missing}"}
    return {"ok": False, "error": "assert needs equals, contains, adv_uuid, or properties"}


def list_runs(engine: MacroEngine) -> list[dict[str, Any]]:
    return [r.public() for r in sorted(engine.runs.values(), key=lambda r: r.started, reverse=True)]


def load_run_log(run_id: str) -> list[dict[str, Any]]:
    path = LOGS_DIR / f"{run_id}.jsonl"
    if not path.exists():
        # try full id prefix match
        matches = list(LOGS_DIR.glob(f"{run_id}*.jsonl"))
        if not matches:
            raise KeyError(run_id)
        path = matches[0]
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
