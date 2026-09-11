"""Build expected GATT catalog from a Zephyr linker map + G2BLEM service table."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from app.gatt_names import lookup_name, normalize_uuid

log = logging.getLogger(__name__)

# Static bt_gatt_service_static entries in zephyr.map → logical service names.
_STATIC_SERVICE_KEYS: dict[str, str] = {
    "_2_gap_svc_": "GENERIC_ACCESS",
    "_1_gatt_svc_": "GENERIC_ATTRIBUTE",
    "dctool_service_": "DC_TOOL",
    "deviceinfo_service_": "DEVICE_INFORMATION",
    "nus_def_svc_": "NUS",
    "secure_access_svc_": "SECURE_ACCESS",
    "testmode_service_": "TEST_MODE",
}

# C++ class prefixes on *_uuid symbols → service table service name.
_SERVICE_UUID_DEFAULTS: dict[str, str] = {
    "GENERIC_ACCESS": "1800",
    "GENERIC_ATTRIBUTE": "1801",
    "DEVICE_INFORMATION": "180a",
    "DC_TOOL": "18ea82915b4211e6bdf40800200c9a66",
    "TEST_MODE": "4b5810518bcc11e6bdf40800200c9a66",
    "SECURE_ACCESS": "edc91b416f7811eaab120800200c9a66",
    "NUS": "6e400001b5a3f393e0a9e50e24dcca9e",
}

_SERVICE_DISPLAY: dict[str, str] = {
    "GENERIC_ACCESS": "Generic Access",
    "GENERIC_ATTRIBUTE": "Generic Attribute",
    "DEVICE_INFORMATION": "Device Information",
    "DC_TOOL": "DC Tool",
    "TEST_MODE": "Test Mode",
    "SECURE_ACCESS": "Secure Access",
    "NUS": "Nordic UART",
}

_CLASS_SERVICE: dict[str, str] = {
    "DCToolService": "DC_TOOL",
    "DeviceInfoService": "DEVICE_INFORMATION",
    "TestModeService": "TEST_MODE",
    "SecureAccessService": "SECURE_ACCESS",
    "NUS": "NUS",
}

# Map firmware symbol tails to G2BLEM characteristic names.
_SYMBOL_CHAR_ALIASES: dict[str, str] = {
    "ota_update": "OAD",
    "tool_connect_en": "ENABLE",
    "time_rtc": "TIME",
    "backup_cell": "BACKUP_CELL",
    "led_control_identify": "LED_CONTROL_IDENTIFY",
    "mode_lock_unlock_status": "MODE_LOCK_UNLOCK_STATUS",
    "fr_status": "FR_STATUS",
    "mcm_hw_rev": "MCM_HARDWARE_REVISION",
    "mcm_fw_rev": "MCM_FIRMWARE_REVISION",
    "pti": "PRODUCT_TYPE_INDEX",
    "device_info_hardware_rev": "HARDWARE_REVISION",
    "device_info_firmware_rev": "FIRMWARE_REVISION",
    "device_info_model_num": "MODEL_NUMBER",
    "device_info_sys_id": "SYSTEM_ID",
    "device_info_manuf_name": "MANUFACTURER_NAME",
    "device_info_serial_num": "SERIAL_NUMBER",
    "test_mode_password": "PASSWORD",
    "test_mode_last_command": "LAST_COMMAND",
    "test_mode_vcell_measure": "VCELL_MEASURE",
    "test_mode_vstack_measure": "VSTACK_MEASURE",
    "test_mode_ptm_enable": "PTM_MODE_ENABLE_OVERRIDE",
    "test_mode_pti_mcm": "PRODUCT_TYPE_INDEX",
    "test_mode_manuf_date": "MANUFACTURER_DATE",
    "test_mode_history_log": "HISTORY_LOG",
    "test_mode_frequency_log": "FREQUENCY_LOG",
    "test_mode_tester_signoff": "TESTER_SIGNOFF",
    "test_mode_mac_address": "MAC_ADDRESS_UPDATE",
    "test_mode_service_reset": "SERVICE_RESET",
    "test_mode_reset_rsn": "RESET_REASON",
    "test_mode_led_count": "LED_COUNT",
    "test_mode_button_status": "BUTTON_STATUS",
    "sas_password": "ACCESS_PASSWORD",
    "sas_sold_status": "SOLD_STATUS",
    "sas_rand_gen": "RANDOM_NUMBER",
    "sas_auth_status": "AUTHORIZATION_STATUS",
    "sas_checkout_mode": "CHECKOUT_MODE",
    "sas_access_mode": "ACCESS_MODE",
    "sas_eng_bypass": "ENGINEERING_BYPASS",
    "sas_conn_timeout": "CONNECTION_TIMEOUT",
    "sas_sm_ctrl_rx": "SM_CONTROL_RX",
    "sas_sm_ctrl_tx": "SM_CONTROL_TX",
    "nus_tx": "UART_TX",
    "nus_rx": "UART_RX",
}

_RE_STATIC_SERVICE = re.compile(
    r"\._bt_gatt_service_static\.static\.(\S+)",
    re.I,
)
_RE_UUID_SYMBOL = re.compile(
    r"(?:\s|^)([A-Za-z_][\w:]*(?:::[\w]+)?_uuid)\s*$",
    re.I,
)
_RE_DEMANGLED = re.compile(
    r"^\s*0x[0-9a-f]+\s+(?P<symbol>[A-Za-z_][\w:]*(?:::[\w]+)?_uuid)\s*$",
    re.I,
)


def _symbol_tail(symbol: str) -> str:
    tail = symbol.split("::")[-1]
    tail = re.sub(r"_uuid_?$", "", tail, flags=re.I)
    return tail.lower()


def _symbol_to_characteristic(symbol: str) -> str | None:
    tail = _symbol_tail(symbol)
    if tail.endswith("service") or tail.endswith("service_uuid"):
        return None
    if tail in _SYMBOL_CHAR_ALIASES:
        return _SYMBOL_CHAR_ALIASES[tail]
    if tail.startswith("device_info_"):
        tail = tail[len("device_info_") :]
    if tail.startswith("test_mode_"):
        tail = tail[len("test_mode_") :]
    if tail.startswith("sas_"):
        tail = tail[len("sas_") :]
    if tail.startswith("dc_"):
        tail = tail[len("dc_") :]
    return tail.upper()


def _symbol_service(symbol: str) -> str | None:
    if "::" in symbol:
        cls = symbol.split("::", 1)[0]
        return _CLASS_SERVICE.get(cls)
    tail = _symbol_tail(symbol)
    if tail.startswith("nus_"):
        return "NUS"
    return None


def parse_zephyr_map(path: Path | str) -> dict[str, Any]:
    """Parse zephyr.map for GATT service static entries and *_uuid symbols."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    static_services: list[str] = []
    uuid_symbols: list[dict[str, str]] = []

    in_service_area = False
    for line in text.splitlines():
        if "bt_gatt_service_static_area" in line:
            in_service_area = True
            continue
        if in_service_area:
            if line.strip() and not line.startswith(" ") and "area" in line and "bt_gatt" not in line:
                in_service_area = False
            else:
                m = _RE_STATIC_SERVICE.search(line)
                if m:
                    static_services.append(m.group(1))

        m = _RE_DEMANGLED.match(line)
        if m:
            sym = m.group("symbol")
            if sym.endswith("_uuid") or sym.endswith("_uuid_"):
                svc = _symbol_service(sym)
                char = _symbol_to_characteristic(sym)
                uuid_symbols.append({"symbol": sym, "service": svc or "", "characteristic": char or ""})
            continue

        if "_uuid" in line and ("lib__blem" in line or "NUS.cpp" in line):
            parts = line.split()
            if parts:
                sym = parts[-1]
                if sym.endswith("_uuid") or sym.endswith("_uuid_"):
                    svc = _symbol_service(sym)
                    char = _symbol_to_characteristic(sym)
                    uuid_symbols.append({"symbol": sym, "service": svc or "", "characteristic": char or ""})

    service_names = set()
    for key in static_services:
        name = _STATIC_SERVICE_KEYS.get(key)
        if name:
            service_names.add(name)
    if any(s["service"] for s in uuid_symbols):
        for s in uuid_symbols:
            if s["service"]:
                service_names.add(s["service"])

    return {
        "static_services": static_services,
        "uuid_symbols": uuid_symbols,
        "service_names": sorted(service_names),
    }


def load_blem_service_table(path: Path | str) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    table = data.get("table") or []
    return [row for row in table if isinstance(row, dict)]


def _service_uuid_from_table(table: list[dict[str, Any]], service: str) -> str | None:
    """Resolve primary service UUID for a logical service name."""
    key = service.upper()
    if key in _SERVICE_UUID_DEFAULTS:
        return normalize_uuid(_SERVICE_UUID_DEFAULTS[key])
    rows = [r for r in table if str(r.get("service") or "").upper() == key]
    if not rows:
        return None
    for row in rows:
        u = normalize_uuid(str(row.get("uuid") or ""))
        if len(u) > 4:
            return u
    return normalize_uuid(str(rows[0].get("uuid") or ""))


def build_expected_gatt(
    map_path: Path | str,
    table_path: Path | str,
) -> list[dict[str, Any]]:
    """Return hub-shaped service list expected for this firmware build."""
    parsed = parse_zephyr_map(map_path)
    table = load_blem_service_table(table_path)
    present_services = set(parsed["service_names"])

    # NUS RX/TX are standard Zephyr NUS even if only nus_tx_uuid appears in .data.
    if "NUS" in present_services:
        parsed["uuid_symbols"].append(
            {"symbol": "NUS::nus_rx_uuid", "service": "NUS", "characteristic": "UART_RX"}
        )

    services: dict[str, dict[str, Any]] = {}

    def ensure_service(name: str) -> dict[str, Any]:
        if name not in services:
            suuid = _service_uuid_from_table(table, name) or ""
            services[name] = {
                "handle": "",
                "uuid": suuid,
                "name": _SERVICE_DISPLAY.get(name) or lookup_name(suuid, "service") or name.replace("_", " ").title(),
                "characteristics": [],
                "expected": True,
                "source": "zephyr.map",
            }
        return services[name]

    # Include full YAML groups for each service linked in the map.
    for svc_name in sorted(present_services):
        svc = ensure_service(svc_name)
        existing_uuids = {normalize_uuid(c.get("uuid") or "") for c in svc["characteristics"]}
        for row in table:
            if str(row.get("service") or "").upper() != svc_name.upper():
                continue
            cuuid = normalize_uuid(str(row.get("uuid") or ""))
            if not cuuid or cuuid in existing_uuids:
                continue
            char_name = str(row.get("characteristic") or cuuid)
            svc["characteristics"].append(
                {
                    "decl_handle": "",
                    "handle": "",
                    "uuid": cuuid,
                    "name": char_name.replace("_", " ").title(),
                    "properties": {"flags": {}, "mask": ""},
                    "descriptors": [],
                    "expected": True,
                    "blem_characteristic": char_name,
                    "access": row.get("access") or {},
                    "format": row.get("format"),
                    "data_size": row.get("dataSize"),
                }
            )
            existing_uuids.add(cuuid)

    # Add map-only symbols not covered by YAML (name from symbol).
    for entry in parsed["uuid_symbols"]:
        svc_name = entry.get("service") or ""
        char_key = entry.get("characteristic") or ""
        if not svc_name or not char_key:
            continue
        svc = ensure_service(svc_name)
        if any(str(c.get("blem_characteristic") or "").upper() == char_key.upper() for c in svc["characteristics"]):
            continue
        yaml_row = next(
            (
                r
                for r in table
                if str(r.get("service") or "").upper() == svc_name.upper()
                and str(r.get("characteristic") or "").upper() == char_key.upper()
            ),
            None,
        )
        cuuid = normalize_uuid(str((yaml_row or {}).get("uuid") or ""))
        svc["characteristics"].append(
            {
                "decl_handle": "",
                "handle": "",
                "uuid": cuuid,
                "name": char_key.replace("_", " ").title(),
                "properties": {"flags": {}, "mask": ""},
                "descriptors": [],
                "expected": True,
                "blem_characteristic": char_key,
                "symbol": entry.get("symbol"),
                "access": (yaml_row or {}).get("access") or {},
            }
        )

    order = [
        "GENERIC_ACCESS",
        "GENERIC_ATTRIBUTE",
        "DEVICE_INFORMATION",
        "DC_TOOL",
        "TEST_MODE",
        "SECURE_ACCESS",
        "NUS",
    ]
    ordered = [services[n] for n in order if n in services]
    ordered.extend(services[n] for n in sorted(services) if n not in order)
    return ordered


def merge_discovered_with_expected(
    discovered: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge live GATT browse with zephyr.map catalog; enrich names and mark gaps."""
    if not expected:
        return discovered

    by_svc_uuid: dict[str, dict[str, Any]] = {}
    for svc in discovered:
        key = normalize_uuid(str(svc.get("uuid") or ""))
        by_svc_uuid[key or str(svc.get("name") or "").lower()] = svc

    exp_by_svc: dict[str, dict[str, Any]] = {}
    for svc in expected:
        key = normalize_uuid(str(svc.get("uuid") or "")) or str(svc.get("name") or "").lower()
        exp_by_svc[key] = svc

    merged: list[dict[str, Any]] = []
    seen_svc: set[str] = set()

    for exp_svc in expected:
        ekey = normalize_uuid(str(exp_svc.get("uuid") or "")) or str(exp_svc.get("name") or "").lower()
        live = by_svc_uuid.get(ekey)
        if not live:
            # try match by service name
            for cand in discovered:
                if (cand.get("name") or "").lower() == (exp_svc.get("name") or "").lower():
                    live = cand
                    break
        if live:
            seen_svc.add(normalize_uuid(str(live.get("uuid") or "")) or str(live.get("name") or "").lower())
            merged.append(_merge_service(live, exp_svc))
        else:
            merged.append(dict(exp_svc))

    for svc in discovered:
        key = normalize_uuid(str(svc.get("uuid") or "")) or str(svc.get("name") or "").lower()
        if key not in seen_svc:
            merged.append(svc)

    return merged


def _merge_service(live: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    out = dict(live)
    exp_chars = expected.get("characteristics") or []
    live_chars = list(out.get("characteristics") or [])
    live_by_uuid = {normalize_uuid(str(c.get("uuid") or "")): c for c in live_chars if c.get("uuid")}

    merged_chars: list[dict[str, Any]] = []
    used: set[str] = set()

    for exp in exp_chars:
        eu = normalize_uuid(str(exp.get("uuid") or ""))
        live_c = live_by_uuid.get(eu) if eu else None
        if not live_c and exp.get("blem_characteristic"):
            for c in live_chars:
                if (c.get("name") or "").lower() == str(exp.get("blem_characteristic") or "").replace("_", " ").lower():
                    live_c = c
                    break
        if live_c:
            item = dict(live_c)
            if exp.get("name"):
                item["name"] = exp.get("name")
            if exp.get("blem_characteristic"):
                item["blem_characteristic"] = exp["blem_characteristic"]
            item["expected"] = True
            merged_chars.append(item)
            if live_c.get("uuid"):
                used.add(normalize_uuid(str(live_c.get("uuid"))))
        else:
            merged_chars.append(dict(exp))

    for c in live_chars:
        cu = normalize_uuid(str(c.get("uuid") or ""))
        if cu and cu in used:
            continue
        if c in merged_chars:
            continue
        extra = dict(c)
        extra["expected"] = False
        merged_chars.append(extra)

    out["characteristics"] = merged_chars
    out["expected"] = True
    if expected.get("name") and not out.get("name"):
        out["name"] = expected["name"]
    return out


_catalog_cache: list[dict[str, Any]] | None = None


def get_expected_gatt_catalog(
    map_path: Path | str | None = None,
    table_path: Path | str | None = None,
    *,
    reload: bool = False,
) -> list[dict[str, Any]]:
    global _catalog_cache
    from app.config import BLEM_SERVICE_TABLE_PATH, ZEPHYR_MAP_PATH

    map_p = Path(map_path or ZEPHYR_MAP_PATH)
    table_p = Path(table_path or BLEM_SERVICE_TABLE_PATH)
    if _catalog_cache is not None and not reload:
        return _catalog_cache
    if not map_p.is_file():
        log.warning("zephyr.map not found at %s", map_p)
        _catalog_cache = []
        return _catalog_cache
    if not table_p.is_file():
        log.warning("G2BLEM service table not found at %s", table_p)
        _catalog_cache = []
        return _catalog_cache
    try:
        _catalog_cache = build_expected_gatt(map_p, table_p)
        log.info(
            "Loaded expected GATT from %s: %d services, %d characteristics",
            map_p.name,
            len(_catalog_cache),
            sum(len(s.get("characteristics") or []) for s in _catalog_cache),
        )
    except Exception as exc:
        log.exception("Failed to build expected GATT catalog: %s", exc)
        _catalog_cache = []
    return _catalog_cache
