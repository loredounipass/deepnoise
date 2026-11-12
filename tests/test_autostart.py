import unittest
import sys
import winreg
from unittest.mock import patch, MagicMock

from src.autostart import (
    get_executable_command,
    is_autostart_enabled,
    set_autostart,
    sync_autostart_with_config,
    APP_NAME,
    REG_KEY,
)


class TestAutostart(unittest.TestCase):
    def test_get_executable_command(self):
        cmd = get_executable_command()
        self.assertIsInstance(cmd, str)
        self.assertTrue(len(cmd) > 0)
        self.assertTrue(cmd.startswith('"'))

    def test_registry_autostart_lifecycle(self):
        if sys.platform != "win32":
            self.skipTest("Windows-only test")

        # Save previous state if any
        was_enabled = is_autostart_enabled()

        try:
            # 1. Enable autostart
            success = set_autostart(True)
            self.assertTrue(success)
            self.assertTrue(is_autostart_enabled())

            # Verify actual registry entry
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ) as key:
                val, typ = winreg.QueryValueEx(key, APP_NAME)
                self.assertEqual(typ, winreg.REG_SZ)
                self.assertTrue(len(val) > 0)

            # 2. Disable autostart
            success = set_autostart(False)
            self.assertTrue(success)
            self.assertFalse(is_autostart_enabled())

            # 3. Disabling again idempotently should succeed
            success = set_autostart(False)
            self.assertTrue(success)
            self.assertFalse(is_autostart_enabled())

        finally:
            # Restore previous state
            set_autostart(was_enabled)

    def test_sync_autostart_with_config(self):
        if sys.platform != "win32":
            self.skipTest("Windows-only test")

        was_enabled = is_autostart_enabled()
        try:
            # Test sync with start_with_windows = False
            sync_autostart_with_config({"start_with_windows": False})
            self.assertFalse(is_autostart_enabled())

            # Test sync with start_with_windows = True
            sync_autostart_with_config({"start_with_windows": True})
            self.assertTrue(is_autostart_enabled())

        finally:
            set_autostart(was_enabled)


if __name__ == "__main__":
    unittest.main()

