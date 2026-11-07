# -*- coding: utf-8 -*-
"""Portable bootstrap helpers; only standard-library dependencies are allowed here."""
import ctypes
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

RUNTIME_DEFAULTS = {
    'venv_dir': '.venv',
    'python_executable_windows': 'Scripts/python.exe',
    'python_executable_posix': 'bin/python',
}


def load_runtime_config(
    app_dir: Path, *, frozen: bool = False, bundle_dir: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Prefer explicit local settings, then bundled defaults for packaged apps.

    Source runs still require their project configuration. A malformed external
    config is an error rather than a reason to silently discard user settings.
    """
    external = app_dir / 'project_config.json'
    selected = external
    if not external.exists() and frozen:
        selected = (bundle_dir or app_dir) / 'project_config.json'
        if not selected.exists():
            return dict(RUNTIME_DEFAULTS), 'embedded defaults'
    try:
        with selected.open(encoding='utf-8-sig') as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError('expected a JSON object')
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError('Cannot load runtime config %s: %s' % (selected, exc)) from exc
    return {**RUNTIME_DEFAULTS, **values}, str(selected)


def report_startup_error(app_dir: Path, exc_type: type, exc: BaseException, tb: Any) -> Path | None:
    """Persist startup exceptions and show a native dialog, even if Tk cannot load."""
    error_path = None
    detail = ''.join(traceback.format_exception(exc_type, exc, tb))
    # Do not capture locals/environment: they can contain passwords or cookies.
    bases = [app_dir]
    fallback = os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP')
    if fallback:
        bases.append(Path(fallback) / 'YouzanAuto')
    for base in bases:
        try:
            folder = base / 'log'
            folder.mkdir(parents=True, exist_ok=True)
            error_path = folder / 'startup_error.log'
            with error_path.open('a', encoding='utf-8') as handle:
                handle.write('\n[%s] Python %s\n%s' % (
                    dt.datetime.now().isoformat(timespec='seconds'), sys.version, detail,
                ))
            break
        except OSError:
            error_path = None
    if os.name == 'nt':
        message = '程序启动失败，请发送启动日志用于排查。\n错误类型：%s\n日志：%s' % (
            exc_type.__name__, error_path or '无法写入，请检查文件夹权限',
        )
        try:
            ctypes.windll.user32.MessageBoxW(None, message, 'YouzanAuto 启动失败', 0x10)
        except (AttributeError, OSError):
            pass
    return error_path


def install_startup_diagnostics(app_dir: Path) -> None:
    """Handle otherwise invisible uncaught exceptions in a windowed EXE."""
    def hook(exc_type: type, exc: BaseException, tb: Any) -> None:
        report_startup_error(app_dir, exc_type, exc, tb)
    sys.excepthook = hook
