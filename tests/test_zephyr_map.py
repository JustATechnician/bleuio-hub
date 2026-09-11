import unittest
from pathlib import Path

from app.dongle.zephyr_map import build_expected_gatt, parse_zephyr_map

ROOT = Path(__file__).resolve().parent.parent


class ZephyrMapTests(unittest.TestCase):
    def test_parse_zephyr_map_finds_blem_services(self):
        parsed = parse_zephyr_map(ROOT / "zephyr.map")
        self.assertIn("DC_TOOL", parsed["service_names"])
        self.assertIn("DEVICE_INFORMATION", parsed["service_names"])
        self.assertIn("SECURE_ACCESS", parsed["service_names"])
        self.assertIn("NUS", parsed["service_names"])
        self.assertIn("TEST_MODE", parsed["service_names"])
        self.assertGreaterEqual(len(parsed["uuid_symbols"]), 25)

    def test_build_expected_gatt_matches_firmware_table(self):
        catalog = build_expected_gatt(ROOT / "zephyr.map", ROOT / "G2BLEM_Service_Table.yaml")
        self.assertGreaterEqual(len(catalog), 6)
        chars = sum(len(s.get("characteristics") or []) for s in catalog)
        self.assertGreaterEqual(chars, 40)
        svc_names = {s.get("name") for s in catalog}
        self.assertIn("DC Tool", svc_names)
        self.assertIn("Nordic UART", svc_names)


if __name__ == "__main__":
    unittest.main()
