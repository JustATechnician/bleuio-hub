from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from app.dongle.base import Station
from app.dongle.parser import (
    ascii_preview,
    decode_ad,
    encode_flags,
    encode_mfg,
    encode_name,
    encode_uuid16_list,
    hex_to_bytes,
    parse_props,
    public_services,
    resolve_handle,
)
from app.gatt_names import lookup_name


def _char(
    handle: str,
    uuid: str,
    prop: int,
    value: bytes,
    *,
    deny_read: bool = False,
    deny_write: bool = False,
    extra_write: bool = False,
) -> dict[str, Any]:
    return {
        "decl_handle": f"{int(handle, 16) - 1:04x}" if int(handle, 16) > 0 else handle,
        "handle": handle,
        "uuid": uuid,
        "value_uuid": uuid,
        "name": lookup_name(uuid, "characteristic"),
        "properties": parse_props(prop),
        "descriptors": [],
        "value": value,
        "deny_read": deny_read,
        "deny_write": deny_write,
        "extra_write": extra_write,
    }


def _svc(handle: str, uuid: str, chars: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "handle": handle,
        "uuid": uuid,
        "name": lookup_name(uuid, "service"),
        "characteristics": chars,
    }


NUS = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


def _devices() -> list[dict[str, Any]]:
    hibou_adv = (encode_flags() + encode_name("HibouAir") + encode_mfg("075B", "05040578e03d001527b800d3")).hex()
    nus_adv = (
        encode_flags()
        + encode_name("NordicUART")
        + encode_uuid16_list(["180a"])
    ).hex()
    batt_adv = (encode_flags() + encode_name("BatteryTag") + encode_uuid16_list(["180f", "180a"])).hex()
    hr_adv = (encode_flags() + encode_name("HR-Hidden") + encode_uuid16_list(["180d"])).hex()

    hibou = {
        "addr": "[0]C1:12:32:45:65:54",
        "name": "HibouAir",
        "rssi": -58,
        "adv_hex": hibou_adv,
        "connectable": True,
        "services": [
            _svc(
                "0001",
                "1800",
                [
                    _char("0003", "2a00", 0x0A, b"HibouAir"),
                    _char("0005", "2a01", 0x02, bytes([0x00, 0x00])),
                ],
            ),
            _svc(
                "000a",
                "180a",
                [
                    _char("000c", "2a29", 0x02, b"Smart Sensor Devices"),
                    _char("000e", "2a24", 0x02, b"HibouAir"),
                ],
            ),
            _svc(
                "0010",
                "180f",
                [_char("0012", "2a19", 0x12, bytes([0x54]))],
            ),
        ],
    }
    uart = {
        "addr": "[1]D1:79:29:DB:CB:CC",
        "name": "NordicUART",
        "rssi": -47,
        "adv_hex": nus_adv,
        "connectable": True,
        "services": [
            _svc("0001", "1800", [_char("0003", "2a00", 0x0A, b"NordicUART")]),
            _svc(
                "000e",
                NUS,
                [
                    _char("0010", NUS_RX, 0x0C, b""),
                    _char("0012", NUS_TX, 0x10, b"ready"),
                ],
            ),
        ],
    }
    battery = {
        "addr": "[0]AA:BB:CC:11:22:33",
        "name": "BatteryTag",
        "rssi": -71,
        "adv_hex": batt_adv,
        "connectable": True,
        "services": [
            _svc("0001", "1800", [_char("0003", "2a00", 0x02, b"BatteryTag")]),
            _svc("000a", "180a", [_char("000c", "2a29", 0x02, b"MockCo")]),
            _svc("0010", "180f", [_char("0012", "2a19", 0x12, bytes([87]))]),
        ],
    }
    hidden = {
        "addr": "[1]11:22:33:44:55:66",
        "name": "HR-Hidden",
        "rssi": -64,
        "adv_hex": hr_adv,
        "connectable": True,
        "services": [
            _svc("0001", "1800", [_char("0003", "2a00", 0x0A, b"HR-Hidden")]),
            _svc("000a", "180d", [_char("000c", "2a37", 0x10, bytes([0x00, 0x48]))]),
            # Extra GATT service not advertised — advertised-vs-GATT check.
            _svc(
                "0018",
                "180a",
                [
                    _char("001a", "2a29", 0x02, b"MockCo"),
                    # Claims read-only but write actually succeeds (permission mismatch).
                    _char("001c", "2a25", 0x02, b"SN-001", extra_write=True),
                    # Claims writable but write is rejected.
                    _char("001e", "2a24", 0x0A, b"Model-X", deny_write=True),
                ],
            ),
        ],
    }
    return [hibou, uart, battery, hidden]


class MockStation(Station):
    def __init__(self, station_id: str, port: str, mac: str, index: int):
        super().__init__(station_id, port)
        self.mac = mac
        self.firmware = "mock-1.0.0"
        self.hardware = "DA14683 (mock)"
        self.product = "BleuIO Mock"
        self._index = index
        self._catalog = _devices()
        self._notify_task: asyncio.Task | None = None
        self._scan_task: asyncio.Task | None = None
        self._notifying: set[str] = set()
        self._peer: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.emit("station", station=self.snapshot())

    async def close(self) -> None:
        await self.stop_scan()
        if self._notify_task:
            self._notify_task.cancel()
            self._notify_task = None

    async def start_scan(self, duration: int = 0, filter_hex: str = "") -> None:
        async with self._lock:
            if self.connected:
                raise RuntimeError("Cannot scan while connected")
            await self._cancel_scan()
            self.scanning = True
            self.emit("station", station=self.snapshot())
            self._scan_task = asyncio.create_task(self._scan_loop(duration, filter_hex.lower()))

    async def _cancel_scan(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scan_task = None
        self.scanning = False

    async def stop_scan(self) -> None:
        async with self._lock:
            await self._cancel_scan()
            self.emit("station", station=self.snapshot())

    async def _scan_loop(self, duration: int, filter_hex: str) -> None:
        started = time.time()
        try:
            while self.scanning:
                for dev in self._catalog:
                    if filter_hex and filter_hex not in (dev.get("adv_hex") or ""):
                        continue
                    parsed = {
                        "addr": dev["addr"],
                        "rssi": (dev["rssi"] or -70) + random.randint(-4, 3),
                        "name": dev["name"],
                        "adv_hex": dev["adv_hex"],
                        "fields": decode_ad(dev["adv_hex"]),
                        "uuids": [],
                        "packet": "adv",
                    }
                    parsed["uuids"] = [
                        u
                        for f in parsed["fields"]
                        for u in (f.get("uuids") or ([f["uuid"]] if f.get("uuid") else []))
                    ]
                    merged = self.merge_device(parsed)
                    self.emit("scan", device=merged)
                if duration and (time.time() - started) >= duration:
                    break
                await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            raise
        finally:
            self.scanning = False
            self.emit("scan_complete")
            self.emit("station", station=self.snapshot())

    async def scantarget(self, address: str, duration: int = 0) -> None:
        await self.start_scan(duration=duration or 6)

    async def connect(self, address: str) -> dict[str, Any]:
        async with self._lock:
            await self._cancel_scan()
            peer = next((d for d in self._catalog if d["addr"].upper() == address.upper() or d["addr"][3:].upper() == address.upper().replace("[0]", "").replace("[1]", "")), None)
            if peer is None:
                # Allow connecting to any scanned addr by cloning first catalog device
                peer = next((d for d in self._catalog if d["addr"] == address), None)
            if peer is None:
                raise RuntimeError(f"Device {address} not found (mock)")
            self._peer = peer
            self.connected = True
            self.connected_addr = peer["addr"]
            self.services = peer["services"]
            self.emit("connection", connected=True, address=peer["addr"])
            self.emit("gatt", services=public_services(self.services))
            self.emit("station", station=self.snapshot())
            return {"ok": True, "address": peer["addr"], "services": public_services(self.services)}

    async def disconnect(self) -> dict[str, Any]:
        async with self._lock:
            self.connected = False
            addr = self.connected_addr
            self.connected_addr = None
            self.services = []
            self._peer = None
            self._notifying.clear()
            if self._notify_task:
                self._notify_task.cancel()
                self._notify_task = None
            self.emit("connection", connected=False, address=addr)
            self.emit("gatt", services=[])
            self.emit("station", station=self.snapshot())
            return {"ok": True}

    async def refresh_gatt(self) -> dict[str, Any]:
        async with self._lock:
            if not self.connected:
                return {"ok": False, "error": "Not connected", "services": []}
            pub = public_services(self.services)
            return {"ok": True, "services": pub, "gatt_discovery": "mock"}

    async def reset_idle(self) -> dict[str, Any]:
        await self.stop_scan()
        result = await self.disconnect()
        self.devices = {}
        self.emit("station", station=self.snapshot())
        return result

    def _require_char(self, handle_or_uuid: str) -> dict[str, Any]:
        if not self.connected:
            raise RuntimeError("Not connected")
        char = resolve_handle(self.services, handle_or_uuid)
        if not char:
            raise RuntimeError(f"Characteristic {handle_or_uuid} not found")
        return char

    async def read(self, handle_or_uuid: str) -> dict[str, Any]:
        async with self._lock:
            char = self._require_char(handle_or_uuid)
            props = char["properties"]["flags"]
            if char.get("deny_read") or not props.get("read"):
                result = {
                    "ok": False,
                    "error": "Read not permitted",
                    "handle": char["handle"],
                    "uuid": char["uuid"],
                    "name": char.get("name"),
                    "hex": "",
                    "ascii": "",
                    "expected_readable": bool(props.get("read")),
                }
                self.emit("read", **result)
                return result
            value: bytes = char.get("value") or b""
            result = {
                "ok": True,
                "handle": char["handle"],
                "uuid": char["uuid"],
                "name": char.get("name"),
                "hex": value.hex(),
                "ascii": ascii_preview(value),
                "expected_readable": True,
            }
            self.emit("read", **result)
            return result

    async def write(
        self,
        handle_or_uuid: str,
        data: str,
        *,
        as_hex: bool = True,
        without_response: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            char = self._require_char(handle_or_uuid)
            props = char["properties"]["flags"]
            writable = props.get("write") or props.get("write_without_response") or char.get("extra_write")
            if char.get("deny_write") or not writable:
                result = {
                    "ok": False,
                    "error": "Write not permitted",
                    "handle": char["handle"],
                    "uuid": char["uuid"],
                    "name": char.get("name"),
                    "hex": data if as_hex else data.encode().hex(),
                    "expected_writable": bool(props.get("write") or props.get("write_without_response")),
                }
                self.emit("write", **result)
                return result
            raw = hex_to_bytes(data) if as_hex else data.encode("utf-8")
            char["value"] = raw
            result = {
                "ok": True,
                "handle": char["handle"],
                "uuid": char["uuid"],
                "name": char.get("name"),
                "hex": raw.hex(),
                "ascii": ascii_preview(raw),
                "without_response": without_response,
                "expected_writable": bool(props.get("write") or props.get("write_without_response")),
                "mismatch": bool(char.get("extra_write") and not (props.get("write") or props.get("write_without_response"))),
            }
            self.emit("write", **result)
            if char["uuid"] == NUS_RX:
                # Echo onto TX notify handle if subscribed.
                tx = resolve_handle(self.services, NUS_TX)
                if tx and tx["handle"] in self._notifying:
                    self.emit(
                        "notify",
                        handle=tx["handle"],
                        uuid=tx["uuid"],
                        name=tx.get("name"),
                        hex=raw.hex(),
                        ascii=ascii_preview(raw),
                    )
            return result

    async def set_notify(self, handle_or_uuid: str, enable: bool, indicate: bool = False) -> dict[str, Any]:
        async with self._lock:
            char = self._require_char(handle_or_uuid)
            flags = char["properties"]["flags"]
            allowed = flags.get("indicate") if indicate else flags.get("notify")
            if enable and not allowed:
                return {"ok": False, "error": "Notify/indicate not permitted", "handle": char["handle"]}
            if enable:
                self._notifying.add(char["handle"])
                if self._notify_task is None:
                    self._notify_task = asyncio.create_task(self._notify_loop())
            else:
                self._notifying.discard(char["handle"])
            return {"ok": True, "handle": char["handle"], "enabled": enable, "indicate": indicate}

    async def _notify_loop(self) -> None:
        try:
            while self._notifying and self.connected:
                await asyncio.sleep(2.5)
                for handle in list(self._notifying):
                    char = resolve_handle(self.services, handle)
                    if not char:
                        continue
                    val: bytes = char.get("value") or b"\x00"
                    if char["uuid"] == "2a19":
                        level = (val[0] if val else 80) % 100
                        val = bytes([max(1, level - 1)])
                        char["value"] = val
                    elif char["uuid"] == "2a37":
                        val = bytes([0x00, random.randint(60, 95)])
                        char["value"] = val
                    self.emit(
                        "notify",
                        handle=char["handle"],
                        uuid=char["uuid"],
                        name=char.get("name"),
                        hex=val.hex(),
                        ascii=ascii_preview(val),
                    )
        except asyncio.CancelledError:
            return

    async def enter_passkey(self, passkey: str) -> dict[str, Any]:
        return {"ok": True, "passkey": passkey}


def make_mock_stations(count: int = 4) -> list[MockStation]:
    macs = [
        "00:00:00:00:00:A1",
        "00:00:00:00:00:A2",
        "00:00:00:00:00:A3",
        "00:00:00:00:00:A4",
        "00:00:00:00:00:A5",
        "00:00:00:00:00:A6",
    ]
    stations = []
    for i in range(count):
        stations.append(
            MockStation(
                station_id=f"mock-{i + 1}",
                port=f"MOCK{i + 1}",
                mac=macs[i % len(macs)],
                index=i,
            )
        )
    return stations
