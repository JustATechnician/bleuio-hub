from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import queue
import re
import threading
import time
from typing import Any

from app.config import BLEUIO_PIDS, BLEUIO_VID
from app.dongle.base import Station
from app.dongle.parser import (
    ascii_preview,
    collect_text,
    hex_to_bytes,
    parse_gatt_browse,
    parse_read_payload,
    parse_scan_payload,
    resolve_handle,
)

log = logging.getLogger(__name__)


def _as_dict_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        out.append(parsed)
                except json.JSONDecodeError:
                    continue
        return out
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return _as_dict_list(parsed)
        except json.JSONDecodeError:
            return []
    return []


class BleuIoStation(Station):
    """Thread-backed wrapper around the official bleuio Python library."""

    def __init__(self, station_id: str, port: str):
        super().__init__(station_id, port)
        self._dongle = None
        self._cmd_q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._scan_buffer: list[str] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.bind_loop(loop)
        await loop.run_in_executor(None, self._open)

    def _open(self) -> None:
        from bleuio_lib.bleuio_funcs import BleuIO
        try:
            self._dongle = BleuIO(port=self.port, timeout=0.05, exclusive_mode=True, rx_delay=0.01)
        except TypeError:
            # Older library without exclusive_mode / rx_delay.
            self._dongle = BleuIO(port=self.port, timeout=1)

        self._dongle.register_scan_cb(self._on_scan)
        self._dongle.register_evt_cb(self._on_evt)
        try:
            self._dongle.at_dual()
        except Exception as exc:
            log.warning("at_dual failed on %s: %s", self.port, exc)
        try:
            if hasattr(self._dongle, "atassn"):
                self._dongle.atassn(True)
            if hasattr(self._dongle, "atassm"):
                self._dongle.atassm(True)
            if hasattr(self._dongle, "atsat"):
                self._dongle.atsat(True)
            if hasattr(self._dongle, "at_show_rssi"):
                self._dongle.at_show_rssi(True)
            elif hasattr(self._dongle, "atshowrssi"):
                self._dongle.atshowrssi(True)
            if hasattr(self._dongle, "atds"):
                self._dongle.atds(True)
        except Exception as exc:
            log.debug("scan option setup: %s", exc)

        self._identify()
        self._running = True
        self._thread = threading.Thread(target=self._worker, name=f"bleuio-{self.id}", daemon=True)
        self._thread.start()
        self.emit("station", station=self.snapshot())

    def _identify(self) -> None:
        dongle = self._dongle
        try:
            info = dongle.ati()
            for item in _as_dict_list(getattr(info, "Rsp", None)):
                if item.get("fwVer"):
                    self.firmware = str(item["fwVer"])
                if item.get("hw"):
                    self.hardware = str(item["hw"])
                if item.get("name"):
                    self.product = str(item["name"])
            text = collect_text(info)
            if not self.firmware:
                m = re.search(r"Firmware Version:\s*(\S+)", text)
                if m:
                    self.firmware = m.group(1)
            if not self.hardware:
                m = re.search(r"(DA14\d+[^\\n]*)", text)
                if m:
                    self.hardware = m.group(1).strip()
        except Exception as exc:
            log.warning("ATI failed on %s: %s", self.port, exc)
        try:
            mac = dongle.at_get_mac()
            for item in _as_dict_list(getattr(mac, "Rsp", None)):
                if item.get("own_mac_addr"):
                    self.mac = str(item["own_mac_addr"])
            if not self.mac:
                text = collect_text(mac)
                m = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", text)
                if m:
                    self.mac = m.group(1).upper()
        except Exception as exc:
            log.warning("GETMAC failed on %s: %s", self.port, exc)

    def _worker(self) -> None:
        while self._running:
            try:
                name, args, kwargs, fut = self._cmd_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                fn = getattr(self, f"_do_{name}")
                result = fn(*args, **kwargs)
                fut.set_result(result)
            except Exception as exc:
                log.exception("command %s failed on %s", name, self.id)
                fut.set_exception(exc)
            finally:
                self._cmd_q.task_done()

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._cmd_q.put((name, args, kwargs, fut))
        return await asyncio.wrap_future(fut)

    async def close(self) -> None:
        self._running = False
        try:
            await self._call("shutdown")
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        try:
            if self._dongle and hasattr(self._dongle, "stop"):
                self._dongle.stop()
        except Exception:
            pass

    def _do_shutdown(self) -> None:
        if not self._dongle:
            return
        self._radio_idle()

    def _mark_idle(self, *, clear_devices: bool = False, raw: str = "") -> str | None:
        addr = self.connected_addr
        self.scanning = False
        self.connected = False
        self.connected_addr = None
        self.services = []
        if clear_devices:
            self.devices = {}
        self.emit("connection", connected=False, address=addr, raw=raw)
        self.emit("station", station=self.snapshot())
        return addr

    def _radio_idle(self) -> list[str]:
        """Stop scan and drop every GAP connection. Serial stays open."""
        errors: list[str] = []
        dongle = self._dongle
        if not dongle:
            return errors
        try:
            dongle.stop_scan()
        except Exception as exc:
            errors.append(f"stop_scan: {exc}")
        try:
            if hasattr(dongle, "at_gapdisconnectall"):
                dongle.at_gapdisconnectall()
            else:
                dongle.at_gapdisconnect()
        except Exception as exc:
            errors.append(f"gapdisconnect: {exc}")
        return errors

    def _do_reset_idle(self) -> dict[str, Any]:
        errors = self._radio_idle()
        self._mark_idle(clear_devices=True)
        if errors:
            log.warning("reset_idle on %s: %s", self.id, "; ".join(errors))
        return {"ok": not errors, "errors": errors}

    def _on_scan(self, scan_input: Any) -> None:
        parsed = parse_scan_payload(scan_input)
        if not parsed:
            return
        merged = self.merge_device(parsed)
        self.emit("scan", device=merged)
        if parsed.get("adv_hex"):
            self.emit("adv", device=merged)

    def _on_evt(self, evt_input: Any) -> None:
        text = ""
        objs = _as_dict_list(evt_input)
        if objs:
            text = json.dumps(objs)
        else:
            text = str(evt_input)
        lower = text.lower()
        if "passkey" in lower:
            self.emit("passkey", needed=True, raw=text)
        if "disconnected" in lower or "gap_disconnected" in lower:
            self._mark_idle(raw=text)
        elif self.connected_addr and "connected" in lower:
            self.connected = True
            self.emit("connection", connected=True, address=self.connected_addr, raw=text)
        if "noti" in lower or "indication" in lower or "handle_evt_gattc_notification" in lower:
            handle = None
            hex_val = ""
            m = re.search(r"handle[=:]\s*([0-9a-fA-F]{4})", text, re.I)
            if not m:
                m = re.search(r"\"handle\"\s*:\s*\"?([0-9a-fA-F]{4})", text)
            if m:
                handle = m.group(1).lower()
            hm = re.search(r"\"(?:data|value|hex)\"\s*:\s*\"([0-9A-Fa-f]+)\"", text)
            if hm:
                hex_val = hm.group(1).lower()
            else:
                hm = re.search(r"\b([0-9A-Fa-f]{4,})\b", text)
                if hm:
                    hex_val = hm.group(1).lower()
            char = resolve_handle(self.services, handle) if handle else None
            self.emit(
                "notify",
                handle=handle,
                uuid=(char or {}).get("uuid"),
                name=(char or {}).get("name"),
                hex=hex_val,
                ascii=ascii_preview(hex_to_bytes(hex_val)),
                raw=text,
            )
        # GATT browse fragments sometimes arrive as events
        if " serv " in f" {text} " or " char " in f" {text} ":
            extra = parse_gatt_browse(text)
            if extra:
                self.services = extra
                self.emit("gatt", services=self.services)

    def _do_start_scan(self, duration: int, filter_hex: str) -> None:
        dongle = self._dongle
        if self.connected:
            raise RuntimeError("Cannot scan while connected")
        try:
            dongle.stop_scan()
        except Exception:
            pass
        self.scanning = True
        self.emit("station", station=self.snapshot())
        # Pass 0 so the library returns after starting; we stop from another command.
        dongle.at_findscandata(filter_hex or "", timeout=0)

    def _do_stop_scan(self) -> None:
        try:
            self._dongle.stop_scan()
        finally:
            self.scanning = False
            self.emit("scan_complete")
            self.emit("station", station=self.snapshot())

    def _do_scantarget(self, address: str, duration: int) -> None:
        if self.connected:
            raise RuntimeError("Cannot scan while connected")
        self.scanning = True
        self.emit("station", station=self.snapshot())
        addr = address
        if addr.startswith("[") and "]" in addr:
            addr = addr.split("]", 1)[1]
        self._dongle.at_scantarget(addr)

    def _do_connect(self, address: str) -> dict[str, Any]:
        try:
            self._dongle.stop_scan()
        except Exception:
            pass
        self.scanning = False
        self.connected_addr = address
        resp = self._dongle.at_gapconnect(address)
        # Allow discovery to finish
        time.sleep(1.2)
        services = []
        try:
            browse = self._dongle.at_get_services()
            time.sleep(0.8)
            services = parse_gatt_browse(collect_text(browse))
            if not services:
                services = parse_gatt_browse(str(getattr(browse, "Rsp", "")))
        except Exception as exc:
            log.warning("GETSERVICES: %s", exc)
        self.connected = True
        self.services = services
        self.emit("connection", connected=True, address=address, raw=collect_text(resp))
        self.emit("gatt", services=services)
        self.emit("station", station=self.snapshot())
        return {"ok": True, "address": address, "services": services}

    def _do_disconnect(self) -> dict[str, Any]:
        resp = None
        err = None
        try:
            resp = self._dongle.at_gapdisconnect()
        except Exception as exc:
            err = str(exc)
            try:
                if hasattr(self._dongle, "at_gapdisconnectall"):
                    self._dongle.at_gapdisconnectall()
                    err = None
            except Exception as exc2:
                err = str(exc2)
        self._mark_idle(raw=collect_text(resp) if resp is not None else err or "")
        return {"ok": not err, "error": err}

    def _handle(self, handle_or_uuid: str) -> str:
        char = resolve_handle(self.services, handle_or_uuid)
        if char and char.get("handle"):
            return char["handle"]
        cleaned = handle_or_uuid.strip().lower().replace("0x", "")
        if re.fullmatch(r"[0-9a-f]{4}", cleaned):
            return cleaned
        raise RuntimeError(f"Cannot resolve characteristic {handle_or_uuid}")

    def _do_read(self, handle_or_uuid: str) -> dict[str, Any]:
        handle = self._handle(handle_or_uuid)
        char = resolve_handle(self.services, handle)
        try:
            resp = self._dongle.at_gattcread(handle)
            parsed = parse_read_payload(resp)
            ok = True
            err = None
            ack = getattr(resp, "Ack", None) or {}
            if isinstance(ack, dict) and ack.get("err") not in (0, None, "0"):
                ok = False
                err = ack.get("errMsg") or str(ack.get("err"))
            result = {
                "ok": ok,
                "error": err,
                "handle": handle,
                "uuid": (char or {}).get("uuid"),
                "name": (char or {}).get("name"),
                "hex": parsed.get("hex") or "",
                "ascii": parsed.get("ascii") or "",
                "raw": parsed.get("raw"),
                "expected_readable": bool((char or {}).get("properties", {}).get("flags", {}).get("read")),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc),
                "handle": handle,
                "uuid": (char or {}).get("uuid"),
                "name": (char or {}).get("name"),
                "hex": "",
                "ascii": "",
                "expected_readable": bool((char or {}).get("properties", {}).get("flags", {}).get("read")),
            }
        self.emit("read", **result)
        return result

    def _do_write(self, handle_or_uuid: str, data: str, as_hex: bool, without_response: bool) -> dict[str, Any]:
        handle = self._handle(handle_or_uuid)
        char = resolve_handle(self.services, handle)
        try:
            if as_hex:
                payload = re.sub(r"[^0-9a-fA-F]", "", data)
                if without_response:
                    resp = self._dongle.at_gattcwritewrb(handle, payload)
                else:
                    resp = self._dongle.at_gattcwriteb(handle, payload)
            else:
                if without_response:
                    resp = self._dongle.at_gattcwritewr(handle, data)
                else:
                    resp = self._dongle.at_gattcwrite(handle, data)
            ack = getattr(resp, "Ack", None) or {}
            ok = True
            err = None
            if isinstance(ack, dict) and ack.get("err") not in (0, None, "0"):
                ok = False
                err = ack.get("errMsg") or str(ack.get("err"))
            hex_val = re.sub(r"[^0-9a-fA-F]", "", data).lower() if as_hex else data.encode().hex()
            result = {
                "ok": ok,
                "error": err,
                "handle": handle,
                "uuid": (char or {}).get("uuid"),
                "name": (char or {}).get("name"),
                "hex": hex_val,
                "ascii": ascii_preview(hex_to_bytes(hex_val)),
                "without_response": without_response,
                "raw": collect_text(resp),
                "expected_writable": bool(
                    (char or {}).get("properties", {}).get("flags", {}).get("write")
                    or (char or {}).get("properties", {}).get("flags", {}).get("write_without_response")
                ),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc),
                "handle": handle,
                "uuid": (char or {}).get("uuid"),
                "name": (char or {}).get("name"),
                "hex": data,
                "expected_writable": bool(
                    (char or {}).get("properties", {}).get("flags", {}).get("write")
                    or (char or {}).get("properties", {}).get("flags", {}).get("write_without_response")
                ),
            }
        self.emit("write", **result)
        return result

    def _do_set_notify(self, handle_or_uuid: str, enable: bool, indicate: bool) -> dict[str, Any]:
        handle = self._handle(handle_or_uuid)
        dongle = self._dongle
        if indicate:
            if enable:
                resp = dongle.at_set_indi(handle) if hasattr(dongle, "at_set_indi") else dongle.at_setindi(handle)
            else:
                resp = dongle.at_clearindi(handle)
        else:
            if enable:
                resp = dongle.at_set_noti(handle)
            else:
                resp = dongle.at_clearnoti(handle)
        ack = getattr(resp, "Ack", None) or {}
        ok = True
        err = None
        if isinstance(ack, dict) and ack.get("err") not in (0, None, "0"):
            ok = False
            err = ack.get("errMsg") or str(ack.get("err"))
        return {"ok": ok, "error": err, "handle": handle, "enabled": enable, "indicate": indicate}

    def _do_enter_passkey(self, passkey: str) -> dict[str, Any]:
        resp = self._dongle.at_enter_passkey(passkey)
        return {"ok": True, "raw": collect_text(resp)}

    async def _auto_stop(self, duration: int) -> None:
        await asyncio.sleep(max(duration, 1))
        if self.scanning:
            try:
                await self.stop_scan()
            except Exception:
                pass

    async def start_scan(self, duration: int = 0, filter_hex: str = "") -> None:
        await self._call("start_scan", duration, filter_hex)
        if duration:
            asyncio.create_task(self._auto_stop(duration))

    async def stop_scan(self) -> None:
        await self._call("stop_scan")

    async def scantarget(self, address: str, duration: int = 0) -> None:
        await self._call("scantarget", address, duration)
        if duration:
            asyncio.create_task(self._auto_stop(duration))

    async def connect(self, address: str) -> dict[str, Any]:
        return await self._call("connect", address)

    async def disconnect(self) -> dict[str, Any]:
        return await self._call("disconnect")

    async def reset_idle(self) -> dict[str, Any]:
        try:
            return await self._call("reset_idle")
        except Exception as exc:
            log.warning("reset_idle failed on %s: %s", self.id, exc)
            self._mark_idle(clear_devices=True, raw=str(exc))
            return {"ok": False, "error": str(exc)}

    async def read(self, handle_or_uuid: str) -> dict[str, Any]:
        return await self._call("read", handle_or_uuid)

    async def write(
        self,
        handle_or_uuid: str,
        data: str,
        *,
        as_hex: bool = True,
        without_response: bool = False,
    ) -> dict[str, Any]:
        return await self._call("write", handle_or_uuid, data, as_hex, without_response)

    async def set_notify(self, handle_or_uuid: str, enable: bool, indicate: bool = False) -> dict[str, Any]:
        return await self._call("set_notify", handle_or_uuid, enable, indicate)

    async def enter_passkey(self, passkey: str) -> dict[str, Any]:
        return await self._call("enter_passkey", passkey)


def list_candidate_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    ports = []
    for info in list_ports.comports():
        vid = getattr(info, "vid", None)
        pid = getattr(info, "pid", None)
        desc = f"{info.device} {info.description or ''} {info.manufacturer or ''}".lower()
        if vid == BLEUIO_VID or (pid in BLEUIO_PIDS) or "bleuio" in desc or "smart sensor" in desc:
            ports.append(info.device)
            continue
        # Linux/Pi CDC ACM — skip generic Windows COM ports (ambiguous without VID/name above).
        dev = info.device or ""
        if not re.search(r"COM\d+", dev, re.I) and re.search(r"ttyACM|ttyUSB|usbmodem", dev, re.I):
            ports.append(dev)
    # De-dupe, keep order
    seen = set()
    out = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
