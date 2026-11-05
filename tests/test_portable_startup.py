# -*- coding: utf-8 -*-
"""Offline regressions for copying the EXE without project/local configuration."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from startup_support import RUNTIME_DEFAULTS, load_runtime_config, report_startup_error


class PortableStartupTests(unittest.TestCase):
    def test_frozen_copy_uses_bundled_config_without_external_file(self) -> None:
        """An EXE-only copy must find config inside its PyInstaller bundle."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'app'
            bundle = root / 'bundle'
            app.mkdir()
            bundle.mkdir()
            config = {**RUNTIME_DEFAULTS, 'venv_dir': 'bundled-venv'}
            (bundle / 'project_config.json').write_text(json.dumps(config), encoding='utf-8')
            values, source = load_runtime_config(app, frozen=True, bundle_dir=bundle)
            self.assertEqual(values['venv_dir'], 'bundled-venv')
            self.assertEqual(source, str(bundle / 'project_config.json'))
            self.assertFalse((app / 'project_config.json').exists())

    def test_frozen_missing_configs_have_embedded_defaults(self) -> None:
        """Packaged execution does not require a source checkout or .venv."""
        with tempfile.TemporaryDirectory() as temp:
            values, source = load_runtime_config(Path(temp), frozen=True)
            self.assertEqual(values, RUNTIME_DEFAULTS)
            self.assertEqual(source, 'embedded defaults')

    def test_source_missing_config_is_an_explicit_error(self) -> None:
        """Keep source-mode setup errors actionable instead of ignoring them."""
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, 'Cannot load runtime config'):
                load_runtime_config(Path(temp))

    def test_external_override_and_malformed_external_config(self) -> None:
        """Explicit invalid overrides must not silently fall back to defaults."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'project_config.json'
            path.write_text('{"venv_dir": "custom"}', encoding='utf-8-sig')
            values, source = load_runtime_config(root, frozen=True)
            self.assertEqual(values['venv_dir'], 'custom')
            self.assertEqual(source, str(path))
            for invalid in ('{broken', '[]'):
                with self.subTest(invalid=invalid):
                    path.write_text(invalid, encoding='utf-8')
                    with self.assertRaises(RuntimeError):
                        load_runtime_config(root, frozen=True)

    def test_startup_error_written_even_without_console(self) -> None:
        """Record an actionable traceback without copying environment values."""
        with tempfile.TemporaryDirectory() as temp:
            with patch('startup_support.ctypes.windll.user32.MessageBoxW', return_value=0):
                path = report_startup_error(Path(temp), RuntimeError, RuntimeError('test startup failure'), None)
            self.assertIsNotNone(path)
            self.assertIn('RuntimeError: test startup failure', path.read_text(encoding='utf-8'))
            self.assertEqual(path.parent.name.lower(), 'log')


if __name__ == '__main__':
    unittest.main()
