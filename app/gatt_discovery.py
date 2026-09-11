"""How the hub populates the GATT tree after BLE connect."""

from __future__ import annotations

import os

from app.config import ZEPHYR_MAP_PATH

_VALID = frozenset({"zephyr", "dongle", "merge"})


def catalog_available() -> bool:
    from app.dongle.zephyr_map import get_expected_gatt_catalog

    if not ZEPHYR_MAP_PATH.is_file():
        return False
    return bool(get_expected_gatt_catalog())


def get_gatt_discovery_mode() -> str:
    """zephyr: map catalog only on connect; dongle: GETSERVICES browse; merge: catalog then manual refresh."""
    raw = (os.getenv("BLEUIO_GATT_DISCOVERY") or "").strip().lower()
    if raw in _VALID:
        return raw
    return "zephyr" if catalog_available() else "dongle"


def auto_browse_on_connect() -> bool:
    return get_gatt_discovery_mode() == "dongle"


def atds_enabled_on_open() -> bool:
    """ATDS streams GATT over serial on connect — disable unless dongle-only discovery."""
    return get_gatt_discovery_mode() == "dongle"
