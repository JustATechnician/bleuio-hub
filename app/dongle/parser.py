from __future__ import annotations

import json
import re
from typing import Any

from app.gatt_names import lookup_name, normalize_uuid

AD_TYPES = {
    0x01: "Flags",
    0x02: "Incomplete List of 16-bit Service UUIDs",
    0x03: "Complete List of 16-bit Service UUIDs",
    0x04: "Incomplete List of 32-bit Service UUIDs",
    0x05: "Complete List of 32-bit Service UUIDs",
    0x06: "Incomplete List of 128-bit Service UUIDs",
    0x07: "Complete List of 128-bit Service UUIDs",
    0x08: "Shortened Local Name",
    0x09: "Complete Local Name",
    0x0A: "Tx Power Level",
    0x16: "Service Data (16-bit UUID)",
    0x19: "Appearance",
    0x20: "Service Data (32-bit UUID)",
    0x21: "Service Data (128-bit UUID)",
    0xFF: "Manufacturer Specific Data",
}

PROP_BITS = [
    (0x01, "B", "broadcast"),
    (0x02, "R", "read"),
    (0x04, "Wnr", "write_without_response"),
    (0x08, "W", "write"),
    (0x10, "N", "notify"),
    (0x20, "I", "indicate"),
    (0x40, "As", "authenticated_signed_writes"),
    (0x80, "E", "extended"),
]


def hex_to_bytes(value: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", value or "")
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned) if cleaned else b""


def bytes_to_hex(data: bytes) -> str:
    return data.hex()


def ascii_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def encode_ad_field(ad_type: int, value: bytes) -> bytes:
    return bytes([len(value) + 1, ad_type]) + value


def encode_name(name: str, complete: bool = True) -> bytes:
    return encode_ad_field(0x09 if complete else 0x08, name.encode("utf-8"))


def encode_flags(flags: int = 0x06) -> bytes:
    return encode_ad_field(0x01, bytes([flags]))


def encode_uuid16_list(uuids: list[str], complete: bool = True) -> bytes:
    payload = b"".join(bytes.fromhex(u.replace("0x", "").zfill(4))[::-1] for u in uuids)
    return encode_ad_field(0x03 if complete else 0x02, payload)


def encode_mfg(company_id: str, data_hex: str) -> bytes:
    cid = bytes.fromhex(company_id.replace("0x", "").zfill(4))[::-1]
    return encode_ad_field(0xFF, cid + hex_to_bytes(data_hex))


def uuid16_from_le(data: bytes) -> list[str]:
    out = []
    for i in range(0, len(data) - 1, 2):
        out.append(f"{data[i + 1]:02x}{data[i]:02x}")
    return out


def uuid128_from_le(data: bytes) -> list[str]:
    out = []
    for i in range(0, len(data) - 15, 16):
        chunk = data[i : i + 16][::-1].hex()
        out.append(f"{chunk[0:8]}-{chunk[8:12]}-{chunk[12:16]}-{chunk[16:20]}-{chunk[20:32]}")
    return out


def decode_ad(hex_str: str) -> list[dict[str, Any]]:
    data = hex_to_bytes(hex_str)
    fields: list[dict[str, Any]] = []
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0:
            break
        if i + 1 + length > len(data) + 0:
            # length includes type byte
            break
        if i + length >= len(data):
            break
        ad_type = data[i + 1]
        value = data[i + 2 : i + 1 + length]
        field: dict[str, Any] = {
            "type": ad_type,
            "type_name": AD_TYPES.get(ad_type, f"Unknown 0x{ad_type:02X}"),
            "hex": value.hex(),
            "ascii": ascii_preview(value),
        }
        if ad_type == 0x01 and value:
            flags = value[0]
            field["flags"] = flags
            field["decoded"] = {
                "le_limited_discoverable": bool(flags & 0x01),
                "le_general_discoverable": bool(flags & 0x02),
                "br_edr_not_supported": bool(flags & 0x04),
                "simultaneous_le_br_controller": bool(flags & 0x08),
                "simultaneous_le_br_host": bool(flags & 0x10),
            }
        elif ad_type in (0x08, 0x09):
            try:
                field["text"] = value.decode("utf-8")
            except UnicodeDecodeError:
                field["text"] = ascii_preview(value)
        elif ad_type in (0x02, 0x03):
            field["uuids"] = uuid16_from_le(value)
            field["names"] = [lookup_name(u, "service") or u for u in field["uuids"]]
        elif ad_type in (0x06, 0x07):
            field["uuids"] = uuid128_from_le(value)
            field["names"] = [lookup_name(u, "service") or u for u in field["uuids"]]
        elif ad_type == 0x0A and value:
            tx = int.from_bytes(value[:1], "little", signed=True)
            field["tx_power_dbm"] = tx
        elif ad_type == 0xFF and len(value) >= 2:
            company = f"{value[1]:02X}{value[0]:02X}"
            field["company_id"] = company
            field["mfg_data"] = value[2:].hex()
        elif ad_type == 0x16 and len(value) >= 2:
            uuid = f"{value[1]:02x}{value[0]:02x}"
            field["uuid"] = uuid
            field["name"] = lookup_name(uuid, "service")
            field["service_data"] = value[2:].hex()
        fields.append(field)
        i += 1 + length
    return fields


def advertised_uuids(fields: list[dict[str, Any]]) -> list[str]:
    uuids: list[str] = []
    for field in fields:
        for u in field.get("uuids") or []:
            uuids.append(normalize_uuid(u))
        if field.get("uuid"):
            uuids.append(normalize_uuid(field["uuid"]))
    seen = set()
    out = []
    for u in uuids:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def advertised_name(fields: list[dict[str, Any]]) -> str | None:
    short = None
    complete = None
    for field in fields:
        if field.get("type") == 0x09:
            complete = field.get("text")
        elif field.get("type") == 0x08:
            short = field.get("text")
    return complete or short


def parse_props(prop: Any) -> dict[str, Any]:
    value = 0
    if isinstance(prop, int):
        value = prop
    elif isinstance(prop, str):
        m = re.search(r"prop\s*=\s*([0-9a-fA-F]+)", prop)
        if m:
            value = int(m.group(1), 16)
        else:
            cleaned = prop.strip().lower().replace("0x", "")
            if re.fullmatch(r"[0-9a-f]+", cleaned):
                value = int(cleaned, 16)
            else:
                letters = prop.upper()
                value = 0
                if "R" in letters:
                    value |= 0x02
                if "W" in letters and "WNR" not in letters.replace(" ", ""):
                    value |= 0x08
                if "N" in letters:
                    value |= 0x10
                if "I" in letters:
                    value |= 0x20
                if "X" in letters:
                    value |= 0x04
    flags = {name: bool(value & bit) for bit, _short, name in PROP_BITS}
    label = ""
    for bit, short, name in PROP_BITS:
        label += short[0] if (value & bit) and short[0] in "BRWNIE" else "-"
        # Build a compact BleuIO-like 8-char mask
    mask = []
    for bit, short, _name in [
        (0x01, "B", "broadcast"),
        (0x02, "R", "read"),
        (0x04, "X", "write_without_response"),
        (0x08, "W", "write"),
        (0x10, "N", "notify"),
        (0x20, "I", "indicate"),
        (0x40, "A", "auth"),
        (0x80, "E", "ext"),
    ]:
        mask.append(short if value & bit else "-")
    return {"value": value, "mask": "".join(mask), "flags": flags}


def can_read(props: dict[str, Any]) -> bool:
    return bool(props.get("flags", {}).get("read"))


def can_write(props: dict[str, Any]) -> bool:
    flags = props.get("flags", {})
    return bool(flags.get("write") or flags.get("write_without_response"))


def parse_scan_payload(raw: Any) -> dict[str, Any] | None:
    """Normalize BleuIO scan callback / verbose JSON into a device dict."""
    if raw is None:
        return None
    obj = raw
    if isinstance(raw, (list, tuple)) and raw:
        obj = raw[0]
    if isinstance(obj, bytes):
        obj = obj.decode("utf-8", errors="replace")
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return _parse_scan_text(text)
    if not isinstance(obj, dict):
        return None

    addr = obj.get("addr") or obj.get("address") or obj.get("mac")
    if not addr:
        return None
    data_hex = obj.get("data") or obj.get("adv") or obj.get("scandata") or ""
    fields = decode_ad(data_hex) if data_hex else []
    rssi = obj.get("rssi")
    name = obj.get("name") or advertised_name(fields)
    pkt_type = obj.get("type")
    kind = "adv"
    if pkt_type in (4, "4", "RESP", "SCAN_RSP"):
        kind = "scan_rsp"
    elif "SF" in obj or "ST" in obj:
        kind = "scan_rsp" if pkt_type in (1, 4) else "adv"
    return {
        "addr": normalize_addr(str(addr)),
        "rssi": rssi,
        "name": name,
        "mfsid": obj.get("mfsid") or obj.get("mfg") or obj.get("company_id"),
        "adv_hex": data_hex.lower() if isinstance(data_hex, str) else "",
        "fields": fields,
        "uuids": advertised_uuids(fields),
        "packet": kind,
        "raw": obj,
    }


_DEVICE_LINE = re.compile(
    r"\[(?P<idx>\d+)\]\s*Device:\s*(?P<addr>\[[01]\][0-9A-Fa-f:]+)\s*RSSI:\s*(?P<rssi>-?\d+)(?:\s*\((?P<name>[^)]+)\))?",
    re.I,
)
_FIND_LINE = re.compile(
    r"\[(?P<addr>[^\]]+)\](?:\s+\((?P<name>[^)]+)\))?(?:\s+\[MFSID:\s*(?P<mfsid>[0-9A-Fa-f]+)\])?\s*Device Data\s*\[(?P<kind>ADV|RESP)\]:\s*(?P<data>[0-9A-Fa-f]+)",
    re.I,
)


def _parse_scan_text(text: str) -> dict[str, Any] | None:
    m = _FIND_LINE.search(text)
    if m:
        data_hex = m.group("data")
        fields = decode_ad(data_hex)
        return {
            "addr": normalize_addr(m.group("addr")),
            "rssi": None,
            "name": m.group("name") or advertised_name(fields),
            "mfsid": m.group("mfsid"),
            "adv_hex": data_hex.lower(),
            "fields": fields,
            "uuids": advertised_uuids(fields),
            "packet": "scan_rsp" if m.group("kind").upper() == "RESP" else "adv",
            "raw": text,
        }
    m = _DEVICE_LINE.search(text)
    if m:
        return {
            "addr": normalize_addr(m.group("addr")),
            "rssi": int(m.group("rssi")),
            "name": m.group("name"),
            "mfsid": None,
            "adv_hex": "",
            "fields": [],
            "uuids": [],
            "packet": "adv",
            "raw": text,
        }
    return None


def normalize_addr(addr: str) -> str:
    addr = addr.strip()
    if re.match(r"^\[[01]\]", addr):
        prefix, mac = addr[:3], addr[3:]
        return prefix + mac.upper()
    if re.match(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$", addr):
        return "[1]" + addr.upper()
    return addr.upper()


_SERV_RE = re.compile(r"^(?P<handle>[0-9a-fA-F]{4})\s+serv\s+(?P<uuid>\S+)", re.I)
_CHAR_RE = re.compile(
    r"^(?P<handle>[0-9a-fA-F]{4})\s+char\s+(?P<uuid>\S+)\s+prop=(?P<prop>[0-9a-fA-F]+)\s*(?:\((?P<mask>[^)]+)\))?",
    re.I,
)
_VAL_RE = re.compile(r"^(?P<handle>[0-9a-fA-F]{4})\s+----\s+(?P<uuid>\S+)", re.I)
_DESC_RE = re.compile(r"^(?P<handle>[0-9a-fA-F]{4})\s+desc\s+(?P<uuid>\S+)", re.I)


def parse_gatt_browse(text: str | list[Any]) -> list[dict[str, Any]]:
    if isinstance(text, list):
        lines = []
        for item in text:
            if isinstance(item, dict):
                lines.append(item.get("resp") or item.get("data") or json.dumps(item))
            else:
                lines.append(str(item))
        text = "\n".join(lines)
    services: list[dict[str, Any]] = []
    current = None
    current_char = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.upper() in {"OK", "SCANNING..."}:
            continue
        if "handle_evt" in line.lower() or line.startswith("AT+"):
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
        m = _SERV_RE.match(line)
        if m:
            uuid = m.group("uuid").replace("0x", "")
            current = {
                "handle": m.group("handle").lower(),
                "uuid": uuid.lower(),
                "name": lookup_name(uuid, "service"),
                "characteristics": [],
            }
            services.append(current)
            current_char = None
            continue
        m = _CHAR_RE.match(line)
        if m and current is not None:
            uuid = m.group("uuid").replace("0x", "")
            props = parse_props(m.group("prop"))
            current_char = {
                "decl_handle": m.group("handle").lower(),
                "handle": None,
                "uuid": uuid.lower(),
                "name": lookup_name(uuid, "characteristic"),
                "properties": props,
                "descriptors": [],
            }
            current["characteristics"].append(current_char)
            continue
        m = _VAL_RE.match(line)
        if m and current_char is not None:
            uuid = m.group("uuid").replace("0x", "")
            current_char["handle"] = m.group("handle").lower()
            current_char["value_uuid"] = uuid.lower()
            if not current_char.get("name"):
                current_char["name"] = lookup_name(uuid, "characteristic")
            continue
        m = _DESC_RE.match(line)
        if m and current_char is not None:
            uuid = m.group("uuid").replace("0x", "")
            current_char["descriptors"].append(
                {
                    "handle": m.group("handle").lower(),
                    "uuid": uuid.lower(),
                    "name": lookup_name(uuid, "descriptor"),
                }
            )
            continue
    for svc in services:
        for char in svc["characteristics"]:
            if not char.get("handle"):
                char["handle"] = char.get("decl_handle")
    return services


def gatt_json_to_line(obj: dict[str, Any]) -> str | None:
    """Convert BleuIO verbose JSON GATT entries to text browse lines."""
    handle = str(obj.get("handle") or obj.get("hdl") or "").strip().lower().replace("0x", "")
    if not re.fullmatch(r"[0-9a-f]{4}", handle):
        return None
    uuid = str(obj.get("uuid") or obj.get("value_uuid") or obj.get("value") or "").strip()
    uuid = uuid.replace("0x", "").lower()
    kind = str(obj.get("type") or obj.get("kind") or "").strip().lower()
    if kind in {"service", "serv", "primary", "secondary", "svc"}:
        return f"{handle} serv {uuid}"
    if kind in {"characteristic", "char"} or obj.get("prop") is not None or obj.get("propFormat"):
        prop = obj.get("prop")
        suffix = ""
        if prop is not None:
            suffix = f" prop={prop}"
            mask = obj.get("propFormat") or obj.get("prop_format")
            if mask:
                suffix += f" ({mask})"
        return f"{handle} char {uuid}{suffix}"
    if kind in {"value", "val", "char_value"} or obj.get("value_uuid"):
        vu = str(obj.get("value_uuid") or uuid).replace("0x", "").lower()
        return f"{handle} ---- {vu}"
    if kind in {"descriptor", "desc"}:
        return f"{handle} desc {uuid}"
    return None


def parse_gatt_response(resp: Any) -> list[dict[str, Any]]:
    """Parse GATT tree from BleuIO GETSERVICES response and/or accumulated browse text."""
    services = parse_gatt_browse(collect_text(resp))
    if services:
        return services
    lines: list[str] = []
    rsp = getattr(resp, "Rsp", None) if resp is not None else None
    if isinstance(resp, dict):
        rsp = resp.get("Rsp", rsp)
    if isinstance(rsp, dict):
        rsp = [rsp]
    if isinstance(rsp, (list, tuple)):
        for item in rsp:
            if isinstance(item, dict):
                line = gatt_json_to_line(item)
                if line:
                    lines.append(line)
            elif isinstance(item, str):
                for part in item.replace("\r", "\n").split("\n"):
                    part = part.strip()
                    if part and "handle_evt" not in part.lower():
                        lines.append(part)
    return parse_gatt_browse("\n".join(lines))


def public_services(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop non-JSON internal fields (bytes values, mock flags) before sending to clients."""
    skip = {"value", "deny_read", "deny_write", "extra_write"}
    keep = {"expected", "blem_characteristic", "symbol", "access", "format", "data_size", "source"}
    out = []
    for svc in services or []:
        item = {k: v for k, v in svc.items() if k != "characteristics"}
        for k in keep:
            if k in svc:
                item[k] = svc[k]
        chars = []
        for char in svc.get("characteristics") or []:
            out_char = {k: v for k, v in char.items() if k not in skip}
            for k in keep:
                if k in char:
                    out_char[k] = char[k]
            chars.append(out_char)
        item["characteristics"] = chars
        out.append(item)
    return out


def flatten_characteristics(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for svc in services:
        for char in svc.get("characteristics") or []:
            item = dict(char)
            item["service_uuid"] = svc.get("uuid")
            item["service_name"] = svc.get("name")
            out.append(item)
    return out


def resolve_handle(services: list[dict[str, Any]], handle_or_uuid: str) -> dict[str, Any] | None:
    target = (handle_or_uuid or "").strip().lower().replace("0x", "")
    chars = flatten_characteristics(services)
    for char in chars:
        if char.get("handle") == target or char.get("decl_handle") == target:
            return char
    for char in chars:
        cu = normalize_uuid(char.get("uuid") or "")
        vu = normalize_uuid(char.get("value_uuid") or "")
        if target in (cu, vu) or cu.endswith(target) or target.endswith(cu):
            return char
    return None


def collect_text(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    parts = []
    for attr in ("Rsp", "rsp", "Cmd", "Ack", "End"):
        val = getattr(resp, attr, None) if not isinstance(resp, dict) else resp.get(attr)
        if val is None:
            continue
        if isinstance(val, list):
            for item in val:
                parts.append(json.dumps(item) if isinstance(item, dict) else str(item))
        elif isinstance(val, dict):
            parts.append(json.dumps(val))
        else:
            parts.append(str(val))
    return "\n".join(parts)


def parse_read_payload(resp: Any) -> dict[str, Any]:
    text = collect_text(resp)
    hex_match = re.search(r"\b([0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})+|[0-9A-Fa-f]{4,})\b", text)
    hex_val = ""
    if hex_match:
        hex_val = re.sub(r"\s+", "", hex_match.group(1)).lower()
    # Prefer JSON verbose fields
    if hasattr(resp, "Rsp"):
        for item in resp.Rsp or []:
            if isinstance(item, dict):
                for key in ("data", "hex", "value", "resp"):
                    if key in item and isinstance(item[key], str) and re.fullmatch(r"[0-9A-Fa-f]+", item[key].replace(" ", "")):
                        hex_val = item[key].replace(" ", "").lower()
                        break
    data = hex_to_bytes(hex_val)
    return {
        "hex": hex_val,
        "ascii": ascii_preview(data),
        "raw": text,
    }


def gatt_uuids(services: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for svc in services:
        found.add(normalize_uuid(svc.get("uuid") or ""))
        for char in svc.get("characteristics") or []:
            found.add(normalize_uuid(char.get("uuid") or ""))
            if char.get("value_uuid"):
                found.add(normalize_uuid(char["value_uuid"]))
    found.discard("")
    return found
