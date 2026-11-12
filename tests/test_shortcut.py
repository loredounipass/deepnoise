import unittest
import sys
import os

from src.shortcut import (
    get_shortcut_path,
    get_target_executable,
    create_or_update_shortcut,
    register_recent_docs,
    ensure_shortcut_and_recent,
)


class TestShortcutIntegration(unittest.TestCase):
    def test_paths(self):
        sc_path = get_shortcut_path()
        self.assertTrue(sc_path.endswith(".lnk"))
        self.assertIn("Start Menu", sc_path)

        target, work_dir, ico = get_target_executable()
        self.assertTrue(len(target) > 0)
        self.assertTrue(len(work_dir) > 0)
        self.assertTrue(ico.endswith(".ico"))

    def test_shortcut_and_recent_registration(self):
        if sys.platform != "win32":
            self.skipTest("Windows-only test")

        sc = create_or_update_shortcut()
        self.assertIsNotNone(sc)
        self.assertTrue(os.path.exists(sc))

        # Register recent docs should execute without raising exceptions
        register_recent_docs(sc)
        ensure_shortcut_and_recent()


if __name__ == "__main__":
    unittest.main()

