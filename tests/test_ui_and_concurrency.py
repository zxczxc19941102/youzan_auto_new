# -*- coding: utf-8 -*-
"""Headless checks for credential UI sync, log actions, and concurrency."""
import sys
import tkinter as tk
from tkinter import scrolledtext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
sys.path.insert(0, str(ROOT))

import followup_batch as followup_batch_module  # noqa: E402
import shop_list_reader as shop_list_reader_module  # noqa: E402
import main  # noqa: E402
from followup_batch import FollowupBatch  # noqa: E402


class FakeEntry:
    def __init__(self):
        self.value = ''

    def delete(self, *_args):
        self.value = ''

    def insert(self, _index, value):
        self.value = str(value)

    def get(self):
        return self.value


def test_saved_credentials_are_written_and_read_back_from_controls():
    user = FakeEntry()
    password = FakeEntry()
    with patch.object(main, 'entry_user', user), patch.object(main, 'entry_pwd', password), \
         patch.object(main.config, 'DEFAULT_USER', 'user-from-config'), \
         patch.object(main.config, 'DEFAULT_PWD', 'password-from-config'), \
         patch.object(main, 'log'), patch.object(main, 'log_error'):
        assert main._sync_saved_credentials_to_ui() is True
    assert user.get() == 'user-from-config'
    assert password.get() == 'password-from-config'
    assert main.credential_ui_sync['ok'] is True




class FakeRepeatReader:
    def __init__(self, *_args, **_kwargs):
        self.shops = [{'shop_id': '1'}]

    def run(self):
        return {'success': True}


class FakeRepeatBatch:
    last_run = None

    def __init__(self, *_args, **_kwargs):
        self.reader = None

    def load_done_ids(self):
        return {'1'}

    def today_visit_ids(self, _shops):
        return set()

    def shop_ids(self, shop):
        return {str(shop['shop_id'])}

    def run(self, **kwargs):
        type(self).last_run = kwargs
        return {'success': True, 'failed': 0, 'cancelled': 0}


class FakeExecutor:
    def __init__(self, **_kwargs):
        pass


def test_repeat_followup_asks_and_allows_explicit_continue():
    FakeRepeatBatch.last_run = None
    old_runner = main.runner
    try:
        main.runner = None
        with patch.object(shop_list_reader_module, 'ShopListReader', FakeRepeatReader), \
             patch.object(followup_batch_module, 'FollowupBatch', FakeRepeatBatch), \
             patch.object(main, 'DaemonThreadPoolExecutor', FakeExecutor), \
             patch.object(main, '_make_debug_log', lambda _name: (lambda _msg: None)), \
             patch.object(main, '_confirm_repeat_followup', return_value=True):
            result = main._auto_followup_after_login(
                object(), {'auto_shop_list': True, 'auto_fill_all': True, 'workers': 1}
            )
        assert result['success'] is True
        assert FakeRepeatBatch.last_run['skip_done'] is False
        assert FakeRepeatBatch.last_run['done_ids'] == set()
    finally:
        main.runner = old_runner


def test_crm_failure_is_not_masked_by_successful_shop_phase():
    FakeRepeatBatch.last_run = None
    old_runner = main.runner
    try:
        main.runner = None
        with patch.object(main, '_run_crm_menu_followup', return_value={
            'success': False, 'attempted': 1, 'failed': 1,
        }), \
             patch.object(shop_list_reader_module, 'ShopListReader', FakeRepeatReader), \
             patch.object(followup_batch_module, 'FollowupBatch', FakeRepeatBatch), \
             patch.object(main, 'DaemonThreadPoolExecutor', FakeExecutor), \
             patch.object(main, '_make_debug_log', lambda _name: (lambda _msg: None)), \
             patch.object(main, '_confirm_repeat_followup', return_value=True), \
             patch.object(main, 'log') as log_mock:
            result = main._auto_followup_after_login(
                object(), {
                    'auto_shop_list': True,
                    'auto_fill_all': True,
                    'followup_customer': True,
                    'followup_shop': True,
                    'workers': 1,
                }
            )
        content_logs = [
            call.args[0] for call in log_mock.call_args_list
            if str(call.args[0]).startswith('\u8ddf\u8fdb\u5185\u5bb9\uff1a')
        ]
        assert len(content_logs) == 1
        assert '\u5ba2\u6237\u9700\u6c42/\u5907\u6ce8\uff1a\u6709\u9700\u8981\u4f1a\u8054\u7cfb' in content_logs[0]
        assert result['success'] is False
        assert result['targets']['customer']['success'] is False
        assert result['targets']['shop']['success'] is True
    finally:
        main.runner = old_runner


def test_repeat_followup_refusal_stops_without_running_batch():
    FakeRepeatBatch.last_run = None
    old_runner = main.runner
    try:
        main.runner = None
        with patch.object(shop_list_reader_module, 'ShopListReader', FakeRepeatReader), \
             patch.object(followup_batch_module, 'FollowupBatch', FakeRepeatBatch), \
             patch.object(main, '_confirm_repeat_followup', return_value=False):
            result = main._auto_followup_after_login(
                object(), {'auto_shop_list': True, 'auto_fill_all': True, 'workers': 1}
            )
        assert result['success'] is False
        assert result['refused'] is True
        assert FakeRepeatBatch.last_run is None
    finally:
        main.runner = old_runner

def test_log_copy_and_clear_actions():
    root = tk.Tk()
    root.withdraw()
    old_root, old_widget = main.root, main.log_widget
    try:
        main.root = root
        main.log_widget = scrolledtext.ScrolledText(root)
        main.log_widget.pack()
        root.update()
        main.log_widget.insert('1.0', 'first line\nsecond line')
        main.log_widget.tag_add(tk.SEL, '1.0', '1.5')
        main._copy_log_selection()
        root.update()
        assert root.clipboard_get() == 'first'
        main._clear_log_widget()
        assert main.log_widget.get('1.0', 'end-1c') == ''
    finally:
        if main.log_widget is not None:
            main.log_widget.destroy()
        main.root, main.log_widget = old_root, old_widget
        root.destroy()

def test_ui_always_uses_one_worker():
    old_worker_var = main.worker_count_var

    class FakeWorkerVar:
        @staticmethod
        def get():
            return '7'

    try:
        main.worker_count_var = FakeWorkerVar()
        assert main._read_workers() == 1
    finally:
        main.worker_count_var = old_worker_var


if __name__ == '__main__':
    test_saved_credentials_are_written_and_read_back_from_controls()
    test_repeat_followup_asks_and_allows_explicit_continue()
    test_repeat_followup_refusal_stops_without_running_batch()
    test_log_copy_and_clear_actions()
    test_ui_always_uses_one_worker()
    print('PASS UI sync and fixed single-thread mode')
