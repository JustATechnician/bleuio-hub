from __future__ import annotations

from typing import Any


def builtin_macros() -> list[dict[str, Any]]:
    return [
        {
            "id": "probe-permissions",
            "name": "Probe permissions",
            "description": "Try read and write on every characteristic. Log success/failure vs advertised property flags.",
            "builtin": True,
            "params": {"test_hex": "00"},
            "steps": [
                {"op": "probe_permissions", "hex": "{{test_hex}}"},
            ],
        },
        {
            "id": "read-all",
            "name": "Read all characteristics",
            "description": "Read every readable characteristic and log handle, UUID, name, hex, and ASCII.",
            "builtin": True,
            "params": {},
            "steps": [{"op": "read_all"}],
        },
        {
            "id": "write-all-test",
            "name": "Write test value to all",
            "description": "Write a test payload to every writable characteristic. Optionally restore the previous value.",
            "builtin": True,
            "params": {"test_hex": "00", "restore": False},
            "steps": [
                {"op": "write_all", "hex": "{{test_hex}}", "restore": "{{restore}}"},
            ],
        },
        {
            "id": "verify-advertising",
            "name": "Verify advertising",
            "description": "Scan, then check decoded advertisement fields (name, UUIDs, manufacturer data).",
            "builtin": True,
            "params": {
                "address": "",
                "scan_sec": 6,
                "name": "",
                "uuids": "",
                "contains": "",
            },
            "steps": [
                {"op": "scan", "duration": 6},
                {"op": "verify_adv", "address": "{{address}}", "name": "{{name}}", "contains": "{{contains}}"},
            ],
        },
        {
            "id": "advertised-vs-gatt",
            "name": "Advertised vs GATT",
            "description": "After connect, compare advertised service UUIDs to the discovered GATT tree.",
            "builtin": True,
            "params": {"address": ""},
            "steps": [
                {"op": "connect", "address": "{{address}}"},
                {"op": "advertised_vs_gatt"},
            ],
        },
    ]
