from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional

from app.dongle.parser import public_services

EventCallback = Callable[[dict[str, Any]], Optional[Awaitable[None]]]


class Station(ABC):
    """One BleuIO dongle (or a mock stand-in). Commands are serialized per station."""

    def __init__(self, station_id: str, port: str):
        self.id = station_id
        self.port = port
        self.mac = ""
        self.firmware = ""
        self.hardware = ""
        self.product = "BleuIO"
        self.scanning = False
        self.connected = False
        self.connected_addr: str | None = None
        self.services: list[dict[str, Any]] = []
        self.devices: dict[str, dict[str, Any]] = {}
        self.macro_run_id: str | None = None
        self.last_error: str | None = None
        self._subs: list[EventCallback] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        self._subs.append(callback)

        def _unsub() -> None:
            try:
                self._subs.remove(callback)
            except ValueError:
                pass

        return _unsub

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, "station_id": self.id, "ts": time.time(), **payload}
        loop = self._loop
        for cb in list(self._subs):
            try:
                if asyncio.iscoroutinefunction(cb):
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(cb(event), loop)
                else:
                    if loop and loop.is_running():
                        loop.call_soon_threadsafe(cb, event)
                    else:
                        cb(event)
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "port": self.port,
            "mac": self.mac,
            "firmware": self.firmware,
            "hardware": self.hardware,
            "product": self.product,
            "scanning": self.scanning,
            "connected": self.connected,
            "connected_addr": self.connected_addr,
            "services": public_services(self.services),
            "device_count": len(self.devices),
            "macro_run_id": self.macro_run_id,
            "last_error": self.last_error,
        }

    def merge_device(self, parsed: dict[str, Any]) -> dict[str, Any]:
        addr = parsed["addr"]
        existing = self.devices.get(addr) or {
            "addr": addr,
            "name": None,
            "rssi": None,
            "mfsid": None,
            "adv_hex": "",
            "scan_rsp_hex": "",
            "fields": [],
            "scan_rsp_fields": [],
            "uuids": [],
            "last_seen": time.time(),
        }
        if parsed.get("rssi") is not None:
            existing["rssi"] = parsed["rssi"]
        if parsed.get("name"):
            existing["name"] = parsed["name"]
        if parsed.get("mfsid"):
            existing["mfsid"] = parsed["mfsid"]
        packet = parsed.get("packet") or "adv"
        if packet == "scan_rsp":
            if parsed.get("adv_hex"):
                existing["scan_rsp_hex"] = parsed["adv_hex"]
                existing["scan_rsp_fields"] = parsed.get("fields") or []
        else:
            if parsed.get("adv_hex"):
                existing["adv_hex"] = parsed["adv_hex"]
                existing["fields"] = parsed.get("fields") or []
        uuids = list(existing.get("uuids") or [])
        for u in parsed.get("uuids") or []:
            if u not in uuids:
                uuids.append(u)
        existing["uuids"] = uuids
        existing["last_seen"] = time.time()
        existing["raw"] = parsed.get("raw")
        self.devices[addr] = existing
        return existing

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    @abstractmethod
    async def start_scan(self, duration: int = 0, filter_hex: str = "") -> None:
        ...

    @abstractmethod
    async def stop_scan(self) -> None:
        ...

    @abstractmethod
    async def scantarget(self, address: str, duration: int = 0) -> None:
        ...

    @abstractmethod
    async def connect(self, address: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def disconnect(self) -> dict[str, Any]:
        ...

    @abstractmethod
    async def refresh_gatt(self) -> dict[str, Any]:
        """Run dongle GATT browse and merge handles into the current service tree."""
        ...

    @abstractmethod
    async def reset_idle(self) -> dict[str, Any]:
        """Drop BLE links and scan, then mark this station idle (serial stays open)."""
        ...

    @abstractmethod
    async def read(self, handle_or_uuid: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def write(
        self,
        handle_or_uuid: str,
        data: str,
        *,
        as_hex: bool = True,
        without_response: bool = False,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    async def set_notify(self, handle_or_uuid: str, enable: bool, indicate: bool = False) -> dict[str, Any]:
        ...

    @abstractmethod
    async def enter_passkey(self, passkey: str) -> dict[str, Any]:
        ...
