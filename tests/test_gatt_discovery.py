import os
import unittest
from unittest.mock import patch

from app.gatt_discovery import atds_enabled_on_open, auto_browse_on_connect, get_gatt_discovery_mode


class GattDiscoveryModeTests(unittest.TestCase):
    def test_default_zephyr_when_catalog_loaded(self):
        with patch("app.gatt_discovery.catalog_available", return_value=True):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BLEUIO_GATT_DISCOVERY", None)
                self.assertEqual(get_gatt_discovery_mode(), "zephyr")
                self.assertFalse(auto_browse_on_connect())
                self.assertFalse(atds_enabled_on_open())

    def test_dongle_mode(self):
        with patch.dict(os.environ, {"BLEUIO_GATT_DISCOVERY": "dongle"}):
            self.assertEqual(get_gatt_discovery_mode(), "dongle")
            self.assertTrue(auto_browse_on_connect())
            self.assertTrue(atds_enabled_on_open())

    def test_merge_mode(self):
        with patch.dict(os.environ, {"BLEUIO_GATT_DISCOVERY": "merge"}):
            self.assertEqual(get_gatt_discovery_mode(), "merge")
            self.assertFalse(auto_browse_on_connect())

    def test_fallback_dongle_without_catalog(self):
        with patch("app.gatt_discovery.catalog_available", return_value=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BLEUIO_GATT_DISCOVERY", None)
                self.assertEqual(get_gatt_discovery_mode(), "dongle")

if __name__ == "__main__":
    unittest.main()
