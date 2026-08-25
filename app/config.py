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


HOST = os.getenv("BLEUIO_HOST", "0.0.0.0")
PORT = _env_int("BLEUIO_PORT", 8000)
# BLEUIO_MOCK=1 forces mock stations. BLEUIO_MOCK=0 forces real dongles only.
# Unset: use real dongles if any are found, otherwise fall back to mock.
MOCK_ENV = os.getenv("BLEUIO_MOCK")
IDLE_TIMEOUT_SEC = _env_int("BLEUIO_IDLE_TIMEOUT", 300)
CLAIM_COOKIE = "bleuio_session"
SCAN_DEFAULT_SEC = _env_int("BLEUIO_SCAN_SEC", 8)
MACRO_STEP_TIMEOUT_SEC = _env_int("BLEUIO_MACRO_STEP_TIMEOUT", 30)

# Smart Sensor Devices BleuIO USB IDs (application firmware).
BLEUIO_VID = 0x2DCF
BLEUIO_PIDS = {0x6002, 0x6001}
