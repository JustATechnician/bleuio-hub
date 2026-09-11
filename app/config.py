from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MACROS_DIR = ROOT / "macros"
LOGS_DIR = ROOT / "logs"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


HOST = os.getenv("BLEUIO_HOST", "0.0.0.0")
PORT = _env_int("BLEUIO_PORT", 8000)
# BLEUIO_MOCK=1 forces mock stations. BLEUIO_MOCK=0 forces real dongles only.
# Unset: use real dongles if any are found, otherwise fall back to mock.
MOCK_ENV = os.getenv("BLEUIO_MOCK")
IDLE_TIMEOUT_SEC = _env_int("BLEUIO_IDLE_TIMEOUT", 300)
CLAIM_COOKIE = "bleuio_session"
SCAN_DEFAULT_SEC = _env_int("BLEUIO_SCAN_SEC", 30)
MACRO_STEP_TIMEOUT_SEC = _env_int("BLEUIO_MACRO_STEP_TIMEOUT", 30)
# Serial timeouts (seconds). Pi / slow USB hubs need higher write timeout at dongle init.
SERIAL_READ_TIMEOUT = _env_float("BLEUIO_SERIAL_READ_TIMEOUT", 1.0)
SERIAL_WRITE_TIMEOUT = _env_float("BLEUIO_SERIAL_WRITE_TIMEOUT", 1.0)
GATT_BROWSE_TIMEOUT_SEC = _env_float("BLEUIO_GATT_BROWSE_TIMEOUT", 30.0)
GATT_BROWSE_IDLE_SEC = _env_float("BLEUIO_GATT_BROWSE_IDLE", 3.0)

# Smart Sensor Devices BleuIO USB IDs (application firmware).
BLEUIO_VID = 0x2DCF
BLEUIO_PIDS = {0x6002, 0x6001}

# Expected GATT catalog from firmware linker map (optional).
ZEPHYR_MAP_PATH = Path(os.getenv("BLEUIO_ZEPHYR_MAP", str(ROOT / "zephyr.map")))
BLEM_SERVICE_TABLE_PATH = Path(
    os.getenv("BLEUIO_BLEM_SERVICE_TABLE", str(ROOT / "G2BLEM_Service_Table.yaml"))
)
# GATT on connect: zephyr (map catalog), dongle (GETSERVICES browse), merge (catalog + manual refresh).
# Default: zephyr when zephyr.map loads, else dongle. See app/gatt_discovery.py.
