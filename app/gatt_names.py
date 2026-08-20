from __future__ import annotations

# Bluetooth SIG 16-bit assigned numbers commonly seen in testing.
# Source: https://www.bluetooth.com/specifications/assigned-numbers/

SERVICES = {
    "1800": "Generic Access",
    "1801": "Generic Attribute",
    "1802": "Immediate Alert",
    "1803": "Link Loss",
    "1804": "Tx Power",
    "1805": "Current Time",
    "180a": "Device Information",
    "180d": "Heart Rate",
    "180f": "Battery",
    "1812": "HID",
    "181a": "Environmental Sensing",
    "181c": "User Data",
    "181d": "Weight Scale",
    "1822": "Pulse Oximeter",
    "1826": "Fitness Machine",
    "183a": "Insulin Delivery",
    "183b": "Binary Sensor",
    "1844": "Glucose",
    "1855": "Gaming Audio",
    "fe59": "Nordic DFU",
}

CHARACTERISTICS = {
    "2a00": "Device Name",
    "2a01": "Appearance",
    "2a04": "Peripheral Preferred Connection Parameters",
    "2a05": "Service Changed",
    "2a19": "Battery Level",
    "2a23": "System ID",
    "2a24": "Model Number String",
    "2a25": "Serial Number String",
    "2a26": "Firmware Revision String",
    "2a27": "Hardware Revision String",
    "2a29": "Manufacturer Name String",
    "2a37": "Heart Rate Measurement",
    "2a38": "Body Sensor Location",
    "2a6d": "Pressure",
    "2a6e": "Temperature",
    "2a6f": "Humidity",
    "2aa6": "Central Address Resolution",
    "2ac4": "Object Changed",
}

DESCRIPTORS = {
    "2901": "Characteristic User Description",
    "2902": "Client Characteristic Configuration",
    "2903": "Server Characteristic Configuration",
    "2904": "Characteristic Presentation Format",
}

# Well-known 128-bit UUIDs
UUID128 = {
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART Service",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART RX",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART TX",
}


def _short(uuid: str) -> str:
    u = uuid.lower().replace("0x", "").replace("-", "")
    if len(u) == 32 and u.startswith("0000") and u.endswith("00001000800000805f9b34fb"):
        return u[4:8]
    if len(u) == 4:
        return u
    return uuid.lower()


def lookup_name(uuid: str, kind: str = "any") -> str | None:
    if not uuid:
        return None
    full = uuid.lower().replace("0x", "")
    if full in UUID128:
        return UUID128[full]
    short = _short(uuid)
    if kind in ("any", "service") and short in SERVICES:
        return SERVICES[short]
    if kind in ("any", "characteristic") and short in CHARACTERISTICS:
        return CHARACTERISTICS[short]
    if kind in ("any", "descriptor") and short in DESCRIPTORS:
        return DESCRIPTORS[short]
    if short in SERVICES:
        return SERVICES[short]
    if short in CHARACTERISTICS:
        return CHARACTERISTICS[short]
    if short in DESCRIPTORS:
        return DESCRIPTORS[short]
    return None


def normalize_uuid(uuid: str) -> str:
    u = (uuid or "").strip().lower().replace("0x", "")
    if len(u) == 4:
        return u
    return u
