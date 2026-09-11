from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.config import (
    BLEUIO_PIDS,
    BLEUIO_VID,
    GATT_BROWSE_IDLE_SEC,
    GATT_BROWSE_TIMEOUT_SEC,
    SCAN_DEFAULT_SEC,
    SERIAL_READ_TIMEOUT,
    SERIAL_WRITE_TIMEOUT,
)
from app.gatt_discovery import atds_enabled_on_open, auto_browse_on_connect, get_gatt_discovery_mode
from app.dongle.base import Station
from app.dongle.zephyr_map import get_expected_gatt_catalog, merge_discovered_with_expected
from app.dongle.parser import (
    ascii_preview,
    collect_text,
    gatt_json_to_line,
    hex_to_bytes,
    parse_gatt_browse,
    parse_gatt_response,
    parse_read_payload,
    parse_scan_payload,
    public_services,
    resolve_handle,
)

log = logging.getLogger(__name__)

_AGENT_DEBUG_LOG = Path(__file__).resolve().parent.parent.parent / "debug-4e7605.log"


def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict[str, Any] | None = None) -> None:
    # #region agent log
    try:
        entry = {
            "sessionId": "4e7605",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
            "runId": "serial-recovery",
        }
        with _AGENT_DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # #endregion


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
        self._gatt_lines: list[str] = []
        self._gatt_ready = threading.Event()
        self._connect_ready = threading.Event()
        self._scan_timer_task: asyncio.Task | None = None
        self._serial_failed = False
        self._connect_browse_pending = False
        self._gatt_dongle_browse_active = False

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.bind_loop(loop)
        await loop.run_in_executor(None, self._open)

    def _open(self) -> None:
        from bleuio_lib.bleuio_funcs import BleuIO
        read_timeout = SERIAL_READ_TIMEOUT
        write_timeout = SERIAL_WRITE_TIMEOUT
        try:
            self._dongle = BleuIO(
                port=self.port,
                timeout=read_timeout,
                w_timeout=write_timeout,
                exclusive_mode=True,
                rx_delay=0,
            )
        except TypeError:
            # Older library without exclusive_mode / rx_delay / w_timeout.
            try:
                self._dongle = BleuIO(port=self.port, timeout=read_timeout, w_timeout=write_timeout)
            except TypeError:
                self._dongle = BleuIO(port=self.port, timeout=max(read_timeout, 1.0))

        self._dongle.register_scan_cb(self._on_scan)
        self._dongle.register_evt_cb(self._on_evt)
        self._install_bleuio_hooks()
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
                self._dongle.atds(atds_enabled_on_open())
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

    def _serial_healthy(self) -> bool:
        dongle = self._dongle
        if dongle is None:
            return False
        serial = getattr(dongle, "_serial", None)
        if serial is None or not serial.is_open:
            return False
        return bool(getattr(dongle, "_reader_alive", False))

    def _stop_reader_safe(self, dongle: Any) -> None:
        if getattr(dongle, "_reader_alive", False):
            dongle._stop_reader()
            return
        reader = getattr(dongle, "receiver_thread", None)
        serial = getattr(dongle, "_serial", None)
        if reader is not None and reader.is_alive():
            dongle._reader_alive = False
            try:
                if serial is not None and hasattr(serial, "cancel_read"):
                    serial.cancel_read()
            except Exception:
                pass
            reader.join(timeout=2.0)

    def _reopen_serial_port(self) -> bool:
        dongle = self._dongle
        if dongle is None:
            return False
        # #region agent log
        _agent_dbg(
            "S1",
            "worker.py:_reopen_serial_port",
            "reopening serial port",
            {
                "reader_alive": bool(getattr(dongle, "_reader_alive", False)),
                "serial_open": bool(getattr(getattr(dongle, "_serial", None), "is_open", False)),
            },
        )
        # #endregion
        try:
            import serial

            self._stop_reader_safe(dongle)
            serial_obj = getattr(dongle, "_serial", None)
            if serial_obj is not None:
                try:
                    if serial_obj.is_open:
                        serial_obj.close()
                except Exception:
                    pass
            dongle._serial = serial.Serial(
                port=self.port,
                baudrate=dongle.baud,
                parity="N",
                stopbits=1,
                bytesize=8,
                timeout=dongle.timeout,
                write_timeout=dongle.w_timeout,
                exclusive=dongle.exclusive_mode,
            )
            dongle.rx_buffer = b""
            dongle._start_reader()
            self._serial_failed = False
            ok = self._serial_healthy()
            # #region agent log
            _agent_dbg("S1", "worker.py:_reopen_serial_port", "reopen result", {"ok": ok})
            # #endregion
            return ok
        except Exception as exc:
            log.error("Serial reopen failed on %s: %s", self.id, exc)
            # #region agent log
            _agent_dbg("S1", "worker.py:_reopen_serial_port", "reopen failed", {"error": str(exc)})
            # #endregion
            return False

    def _recover_serial(self) -> bool:
        if self._reopen_serial_port():
            return True
        dongle = self._dongle
        if dongle is None:
            return False
        # #region agent log
        _agent_dbg("S1", "worker.py:_recover_serial", "soft reopen failed; full init", {})
        # #endregion
        try:
            self._stop_reader_safe(dongle)
            serial_obj = getattr(dongle, "_serial", None)
            if serial_obj is not None:
                try:
                    if serial_obj.is_open:
                        serial_obj.close()
                except Exception:
                    pass
            dongle._serial = None
            dongle.dongle_reconnect_retry_cnt = 0
            dongle._BleuIO__init_serial()
            self._serial_failed = False
            ok = self._serial_healthy()
            # #region agent log
            _agent_dbg("S1", "worker.py:_recover_serial", "full init result", {"ok": ok})
            # #endregion
            return ok
        except Exception as exc:
            log.error("Serial recovery failed on %s: %s", self.id, exc)
            # #region agent log
            _agent_dbg("S1", "worker.py:_recover_serial", "full init failed", {"error": str(exc)})
            # #endregion
            return False

    def _ensure_serial(self, context: str) -> bool:
        healthy = self._serial_healthy()
        # #region agent log
        _agent_dbg("S2", "worker.py:_ensure_serial", context, {"healthy": healthy})
        # #endregion
        if healthy:
            return True
        log.warning("Serial link unhealthy on %s (%s); recovering", self.id, context)
        return self._recover_serial()

    def _install_bleuio_hooks(self) -> None:
        """BleuIO routes scan-complete actions outside the evt callback; hook RX handlers."""
        dongle = self._dongle
        if dongle is None:
            return
        station = self
        orig_scan = dongle._BleuIO__process_scan_result
        orig_action = dongle._BleuIO__process_action
        orig_cmd = dongle._BleuIO__process_command_response

        def cmd_hook(line: str) -> None:
            orig_cmd(line)
            if station.connected_addr is not None and station._ingest_dongle_gatt():
                station._ingest_gatt_serial_line(line)

        def scan_hook(line: str) -> None:
            orig_scan(line)
            if not dongle.__saveScanRsp or dongle._scan_cb is None:
                return
            if '{"S' in line:
                return
            if "Device Data" in line or re.search(r"Device:\s*\[", line, re.I):
                try:
                    dongle._scan_cb([line])
                except Exception as exc:
                    log.warning("scan text callback on %s: %s", station.id, exc)

        def action_hook(line: str) -> None:
            orig_action(line)
            if "scan completed" in line.lower() and "action" in line.lower():
                station._finish_scan()
            if re.search(r'"action"\s*:\s*"connected"', line, re.I):
                station._note_connected(line)

        dongle._BleuIO__process_scan_result = scan_hook
        dongle._BleuIO__process_action = action_hook
        dongle._BleuIO__process_command_response = cmd_hook

        orig_poll = dongle._BleuIO__poll_serial

        def poll_hook() -> None:
            orig_poll()
            if not station._serial_healthy():
                station._serial_failed = True
                # #region agent log
                _agent_dbg("S3", "worker.py:poll_hook", "serial poll thread ended", {"context": "rx_dead"})
                # #endregion

        dongle._BleuIO__poll_serial = poll_hook

    def _maybe_recover_serial(self, context: str) -> bool:
        if self._serial_healthy():
            return True
        log.warning("Serial link dropped on %s (%s); reopening", self.id, context)
        return self._recover_serial()

    def _ingest_dongle_gatt(self) -> bool:
        if self._gatt_dongle_browse_active:
            return True
        return auto_browse_on_connect()

    def _request_gatt_browse(self) -> Any | None:
        if not self._maybe_recover_serial("gatt_browse"):
            return None
        try:
            return self._dongle.at_get_services()
        except Exception as exc:
            log.warning("GETSERVICES: %s", exc)
            return None

    def _ingest_gatt_serial_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        lower = stripped.lower()
        if "gattc_browse_completed" in lower or "gattc_discover_completed" in lower:
            status_ok = bool(re.search(r"status\s*=\s*0\b", stripped, re.I)) or '"status":0' in stripped.replace(" ", "")
            # #region agent log
            _agent_dbg(
                "G1",
                "worker.py:_ingest_gatt_serial_line",
                "browse completed line",
                {"status_ok": status_ok, "gatt_lines": len(self._gatt_lines), "line_head": stripped[:120]},
            )
            # #endregion
            if status_ok:
                self._finalize_gatt()
            else:
                self._gatt_ready.set()
            return
        if "handle_evt" in lower:
            return
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                converted = gatt_json_to_line(obj)
                if converted:
                    if converted not in self._gatt_lines:
                        self._gatt_lines.append(converted)
                    return
                evt = obj.get("evt")
                if isinstance(evt, dict):
                    converted = gatt_json_to_line(evt)
                    if converted and converted not in self._gatt_lines:
                        self._gatt_lines.append(converted)
                        return
        if re.search(r"\b(serv|char|desc|----)\b", stripped, re.I):
            self._ingest_gatt_fragment(stripped)

    def _ingest_gatt_from_objs(self, objs: list[dict[str, Any]]) -> None:
        for obj in objs:
            candidates: list[Any] = [obj]
            evt = obj.get("evt")
            if isinstance(evt, dict):
                candidates.append(evt)
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                converted = gatt_json_to_line(cand)
                if converted and converted not in self._gatt_lines:
                    self._gatt_lines.append(converted)

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
        self._cancel_scan_timer()
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
        if not self._maybe_recover_serial("radio_idle"):
            errors.append("serial: link unavailable")
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

    def _ingest_gatt_fragment(self, text: str) -> None:
        for raw_line in text.replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line or "handle_evt" in line.lower():
                continue
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        converted = gatt_json_to_line(obj)
                        if converted:
                            line = converted
                        else:
                            continue
                except json.JSONDecodeError:
                    continue
            if re.search(r"\b(serv|char|desc|----)\b", line, re.I):
                if line not in self._gatt_lines:
                    self._gatt_lines.append(line)

    def _finalize_gatt(self, *, emit: bool = True) -> list[dict[str, Any]]:
        services = parse_gatt_browse("\n".join(self._gatt_lines))
        if services:
            self.services = services
            if emit and not self._connect_browse_pending:
                self.emit("gatt", services=public_services(self.services))
        self._gatt_ready.set()
        return services

    def _do_reset_idle(self) -> dict[str, Any]:
        errors = self._radio_idle()
        self._mark_idle(clear_devices=True)
        if errors:
            log.warning("reset_idle on %s: %s", self.id, "; ".join(errors))
        return {"ok": not errors, "errors": errors}

    def _is_connected_action(self, text: str) -> bool:
        return bool(re.search(r'"action"\s*:\s*"connected"', text, re.I))

    def _note_connected(self, raw: str = "") -> None:
        if not self.connected_addr:
            return
        self.connected = True
        self._connect_ready.set()
        self.emit("connection", connected=True, address=self.connected_addr, raw=raw)

    def _wait_for_connection(self, timeout: float = 20.0) -> bool:
        dongle = self._dongle
        if dongle is not None and getattr(dongle.status, "isConnected", False):
            self._note_connected()
            return True
        if self._connect_ready.wait(timeout=timeout):
            return True
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if dongle is not None and getattr(dongle.status, "isConnected", False):
                self._note_connected()
                return True
            if not self.connected_addr:
                return False
            if not self._serial_healthy():
                self._maybe_recover_serial("connect_wait")
            time.sleep(0.1)
        return bool(dongle is not None and getattr(dongle.status, "isConnected", False))

    def _dongle_gatt_browse(self) -> list[dict[str, Any]]:
        """Run GETSERVICES and wait for serial GATT dump (heavy — avoid on connect for large GATT)."""
        self._gatt_dongle_browse_active = True
        self._connect_browse_pending = True
        self._gatt_lines = []
        self._gatt_ready.clear()
        try:
            browse = None
            if not self._gatt_ready.is_set() and not self._gatt_lines:
                browse = self._request_gatt_browse()
            if not self._wait_for_gatt_browse():
                log.warning(
                    "GATT browse timed out on %s (%d lines buffered)",
                    self.id,
                    len(self._gatt_lines),
                )
            services = parse_gatt_browse("\n".join(self._gatt_lines))
            if not services and browse is not None:
                services = parse_gatt_response(browse)
            return services or []
        finally:
            self._gatt_dongle_browse_active = False

    def _populate_gatt_after_connect(self) -> None:
        mode = get_gatt_discovery_mode()
        expected = get_expected_gatt_catalog()
        if mode == "dongle" or (mode == "merge" and not expected):
            discovered = self._dongle_gatt_browse()
            if discovered:
                self.services = discovered
            if expected:
                self.services = merge_discovered_with_expected(self.services, expected)
        elif expected:
            self.services = copy.deepcopy(expected)
        else:
            discovered = self._dongle_gatt_browse()
            if discovered:
                self.services = discovered

    def _do_refresh_gatt(self) -> dict[str, Any]:
        if not self.connected or not self.connected_addr:
            return {"ok": False, "error": "Not connected", "services": []}
        if not self._ensure_serial("gatt_refresh"):
            return {"ok": False, "error": "Dongle serial link unavailable", "services": []}
        discovered = self._dongle_gatt_browse()
        expected = get_expected_gatt_catalog()
        if expected:
            self.services = merge_discovered_with_expected(discovered, expected)
        elif discovered:
            self.services = discovered
        self._connect_browse_pending = False
        pub = public_services(self.services)
        self.emit("gatt", services=pub)
        self.emit("station", station=self.snapshot())
        return {
            "ok": True,
            "services": pub,
            "gatt_discovery": get_gatt_discovery_mode(),
            "dongle_lines": len(self._gatt_lines),
        }

    def _wait_for_gatt_browse(
        self,
        timeout: float | None = None,
        idle_grace: float | None = None,
    ) -> bool:
        timeout = timeout if timeout is not None else GATT_BROWSE_TIMEOUT_SEC
        idle_grace = idle_grace if idle_grace is not None else GATT_BROWSE_IDLE_SEC
        if self._gatt_ready.is_set():
            return True
        deadline = time.time() + timeout
        last_count = 0
        idle_since: float | None = None
        while time.time() < deadline:
            if self._gatt_ready.is_set():
                # #region agent log
                _agent_dbg(
                    "G2",
                    "worker.py:_wait_for_gatt_browse",
                    "browse ready event",
                    {"gatt_lines": len(self._gatt_lines), "reason": "event"},
                )
                # #endregion
                return True
            count = len(self._gatt_lines)
            if count != last_count:
                last_count = count
                idle_since = time.time()
            elif count > 0 and idle_since is not None and time.time() - idle_since >= idle_grace:
                # #region agent log
                _agent_dbg(
                    "G2",
                    "worker.py:_wait_for_gatt_browse",
                    "browse idle grace",
                    {"gatt_lines": count, "reason": "idle"},
                )
                # #endregion
                return True
            if not self._serial_healthy():
                if self._maybe_recover_serial("gatt_wait") and self.connected_addr:
                    self._request_gatt_browse()
                    idle_since = None
            time.sleep(0.1)
        # #region agent log
        _agent_dbg(
            "G2",
            "worker.py:_wait_for_gatt_browse",
            "browse timeout",
            {"gatt_lines": len(self._gatt_lines), "reason": "timeout"},
        )
        # #endregion
        return bool(self._gatt_lines)

    def _try_ingest_scan_evt(self, objs: list[dict[str, Any]]) -> None:
        if not self.scanning:
            return
        for obj in objs:
            candidates: list[Any] = [obj]
            evt = obj.get("evt")
            if isinstance(evt, dict):
                candidates.append(evt)
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                action = str(cand.get("action") or "").lower()
                if action in {"scanning", "scan completed"}:
                    continue
                if not (cand.get("addr") or cand.get("data") or cand.get("adv") or cand.get("scandata") or "SF" in cand or "ST" in cand):
                    continue
                parsed = parse_scan_payload(cand)
                if not parsed:
                    continue
                merged = self.merge_device(parsed)
                self.emit("scan", device=merged)
                if parsed.get("adv_hex"):
                    self.emit("adv", device=merged)

    def _on_scan(self, scan_input: Any) -> None:
        parsed = parse_scan_payload(scan_input)
        if not parsed:
            log.debug("scan parse miss on %s: %s", self.id, str(scan_input)[:200])
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
            if self._ingest_dongle_gatt():
                self._ingest_gatt_from_objs([o for o in objs if isinstance(o, dict)])
            self._try_ingest_scan_evt([o for o in objs if isinstance(o, dict)])
        else:
            text = str(evt_input)
        lower = text.lower()
        if "passkey" in lower:
            self.emit("passkey", needed=True, raw=text)
        if '"action":"scan completed"' in lower or "scan completed" in lower:
            self._finish_scan()
        if "disconnected" in lower or "gap_disconnected" in lower:
            self._mark_idle(raw=text)
        elif self._is_connected_action(text):
            self._note_connected(text)
        if "gattc_browse_completed" in lower or "gattc_discover_completed" in lower:
            if not self._ingest_dongle_gatt():
                return
            status_ok = bool(re.search(r"status\s*=\s*0\b", text, re.I)) or '"status":0' in text.replace(" ", "")
            if status_ok:
                self._finalize_gatt()
            else:
                log.warning("GATT browse failed on %s: %s", self.id, text[:200])
                self._gatt_ready.set()
        elif " serv " in f" {text} " or " char " in f" {text} " or " ---- " in f" {text} " or " desc " in f" {text} ":
            if not self._ingest_dongle_gatt():
                return
            self._ingest_gatt_fragment(text)
            if not self._connect_browse_pending:
                partial = parse_gatt_browse("\n".join(self._gatt_lines))
                if partial:
                    self.services = partial
                    self.emit("gatt", services=public_services(self.services))
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
        # GATT browse fragments sometimes arrive as events (handled above)

    def _finish_scan(self) -> None:
        if not self.scanning:
            return
        dongle = self._dongle
        if dongle is not None:
            try:
                dongle.__saveScanRsp = False
            except Exception:
                pass
        self.scanning = False
        self.emit("scan_complete")
        self.emit("station", station=self.snapshot())

    def _start_findscandata(self, filter_hex: str) -> None:
        """Start infinite FINDSCANDATA via library (emits SF lines); duration handled by watchdog."""
        dongle = self._dongle
        scandata = (filter_hex or "").upper()
        dongle.rx_scanning_results = []
        dongle.__saveScanRsp = True
        dongle.at_findscandata(scandata, timeout=0)

    def _do_start_scan(self, duration: int, filter_hex: str) -> None:
        if not self._ensure_serial("scan_start"):
            raise RuntimeError("Dongle serial link unavailable")
        dongle = self._dongle
        if self.connected:
            raise RuntimeError("Cannot scan while connected")
        try:
            dongle.stop_scan()
        except Exception:
            pass
        self.scanning = True
        self.emit("station", station=self.snapshot())
        self._start_findscandata(filter_hex)

    def _do_stop_scan(self) -> None:
        try:
            self._dongle.stop_scan()
        finally:
            self._finish_scan()

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
        if not self._ensure_serial("connect_start"):
            return {
                "ok": False,
                "error": "Dongle serial link unavailable",
                "address": address,
                "services": [],
            }
        try:
            self._dongle.stop_scan()
        except Exception:
            pass
        self.scanning = False
        self.connected_addr = address
        self._gatt_lines = []
        self._gatt_ready.clear()
        self._connect_ready.clear()
        self._connect_browse_pending = True
        connect_err = None
        try:
            resp = self._dongle.at_gapconnect(address)
        except Exception as exc:
            connect_err = str(exc)
            resp = None
        resp_text = collect_text(resp) if resp is not None else ""
        ack = getattr(resp, "Ack", None) or {}
        ack_err = None
        if isinstance(ack, dict) and ack.get("err") not in (0, None, "0"):
            ack_err = ack.get("errMsg") or str(ack.get("err"))
        if connect_err or ack_err:
            self._connect_browse_pending = False
            self.connected = False
            self.connected_addr = None
            self.emit("connection", connected=False, address=address, raw=resp_text or connect_err or "")
            self.emit("station", station=self.snapshot())
            return {"ok": False, "error": ack_err or connect_err, "address": address, "services": []}
        conn_ok = self._wait_for_connection(timeout=20.0)
        if not conn_ok:
            self._connect_browse_pending = False
            self.connected = False
            self.connected_addr = None
            self.emit("connection", connected=False, address=address, raw="Connection timed out")
            self.emit("station", station=self.snapshot())
            return {"ok": False, "error": "Connection timed out", "address": address, "services": []}
        self._populate_gatt_after_connect()
        self.connected = True
        pub = public_services(self.services)
        self._connect_browse_pending = False
        # #region agent log
        _agent_dbg(
            "S4",
            "worker.py:_do_connect",
            "connect complete",
            {
                "service_count": len(pub),
                "char_count": sum(len(s.get("characteristics") or []) for s in pub),
                "gatt_lines": len(self._gatt_lines),
                "serial_healthy": self._serial_healthy(),
            },
        )
        # #endregion
        self.emit("connection", connected=True, address=address, raw=resp_text)
        self.emit("gatt", services=pub)
        self.emit("station", station=self.snapshot())
        return {
            "ok": True,
            "address": address,
            "services": pub,
            "gatt_discovery": get_gatt_discovery_mode(),
        }

    def _do_disconnect(self) -> dict[str, Any]:
        err = None
        resp = None
        try:
            self._dongle.stop_scan()
        except Exception as exc:
            log.debug("stop_scan on disconnect: %s", exc)
        self.scanning = False
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
        addr = self.connected_addr
        self.connected = False
        self.connected_addr = None
        self.services = []
        self._gatt_lines = []
        self._gatt_ready.clear()
        raw = collect_text(resp) if resp is not None else (err or "")
        self.emit("connection", connected=False, address=addr, raw=raw)
        self.emit("gatt", services=[])
        self.emit("station", station=self.snapshot())
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
        if not self._maybe_recover_serial("read"):
            return {"ok": False, "error": "Dongle serial link unavailable"}
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

    def _cancel_scan_timer(self) -> None:
        if self._scan_timer_task:
            self._scan_timer_task.cancel()
            self._scan_timer_task = None

    async def _scan_watchdog(self, duration: int) -> None:
        try:
            await asyncio.sleep(max(duration, 1) + 1.0)
            if self.scanning:
                await self._call("stop_scan")
        except asyncio.CancelledError:
            pass
        finally:
            if self._scan_timer_task is asyncio.current_task():
                self._scan_timer_task = None

    async def start_scan(self, duration: int = 0, filter_hex: str = "") -> None:
        if duration <= 0:
            duration = SCAN_DEFAULT_SEC
        self._cancel_scan_timer()
        await self._call("start_scan", duration, filter_hex)
        self._scan_timer_task = asyncio.create_task(self._scan_watchdog(duration))

    async def stop_scan(self) -> None:
        self._cancel_scan_timer()
        await self._call("stop_scan")

    async def scantarget(self, address: str, duration: int = 0) -> None:
        await self._call("scantarget", address, duration)

    async def connect(self, address: str) -> dict[str, Any]:
        return await self._call("connect", address)

    async def disconnect(self) -> dict[str, Any]:
        return await self._call("disconnect")

    async def refresh_gatt(self) -> dict[str, Any]:
        return await self._call("refresh_gatt")

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
