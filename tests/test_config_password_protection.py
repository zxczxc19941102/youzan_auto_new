# -*- coding: utf-8 -*-
"""Regression tests for refusing legacy plaintext password values."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

import config  # noqa: E402


class UnprotectPasswordTests(unittest.TestCase):
    def test_legacy_plaintext_is_not_used_as_a_password(self):
        self.assertEqual(config._unprotect_password('legacy-password'), '')

    def test_invalid_dpapi_payload_fails_closed(self):
        self.assertEqual(config._unprotect_password('dpapi:not-base64!'), '')

    def test_valid_dpapi_payload_is_still_supported(self):
        with patch.object(config, '_dpapi_transform', return_value=b'protected-password'):
            self.assertEqual(config._unprotect_password('dpapi:c2VjcmV0'), 'protected-password')


if __name__ == '__main__':
    unittest.main()
