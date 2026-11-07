import configparser
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import config


class LocalConfigSecurityTests(unittest.TestCase):
    """Verify local INI persistence and user-bound password protection."""

    def test_dpapi_round_trip_does_not_store_plaintext(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI is required by the application")
        password = "synthetic-test-password-!@#"
        ciphertext = config._protect_password(password)
        self.assertTrue(ciphertext.startswith("dpapi:"))
        self.assertNotIn(password, ciphertext)
        self.assertEqual(password, config._unprotect_password(ciphertext))
        self.assertEqual("", config._unprotect_password(password))

    def test_ini_contains_all_menu_booleans_and_encrypted_password(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI is required by the application")
        original_path = config.LOCAL_CONFIG_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            config.LOCAL_CONFIG_FILE = os.path.join(temp_dir, "config.ini")
            try:
                config.save_local_config(
                    "synthetic-user", "synthetic-password",
                    auto_shop_list=True, auto_fill_all=False,
                    followup_clue=True, followup_customer=False, followup_shop=True,
                )
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(config.LOCAL_CONFIG_FILE, encoding="utf-8")
                self.assertEqual("synthetic-user", parser["credentials"]["username"])
                cipher = parser["credentials"]["password_protected"]
                self.assertTrue(cipher.startswith("dpapi:"))
                self.assertNotIn("synthetic-password", cipher)
                self.assertEqual("true", parser["menu"]["auto_shop_list"])
                self.assertEqual("false", parser["menu"]["auto_fill_all"])
                self.assertEqual("true", parser["menu"]["followup_clue"])
                self.assertEqual("false", parser["menu"]["followup_customer"])
                self.assertEqual("true", parser["menu"]["followup_shop"])
            finally:
                config.LOCAL_CONFIG_FILE = original_path

    def test_runtime_output_paths_are_root_config_and_log_only(self):
        self.assertEqual(Path(config.LOCAL_CONFIG_FILE).name, "config.ini")
        self.assertEqual(Path(config.LOG_DIR).name.lower(), "log")
        for path in (config.JSON_DIR, config.HTML_DIR, config.TXT_DIR, config.PNG_DIR):
            self.assertEqual(Path(path).resolve(), Path(config.LOG_DIR).resolve())


if __name__ == "__main__":
    unittest.main()
