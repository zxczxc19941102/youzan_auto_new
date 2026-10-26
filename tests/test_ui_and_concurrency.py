# -*- coding: utf-8 -*-
"""Headless checks for credential UI sync, log actions, and concurrency."""
import sys
import threading
import time
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
                object(), {'auto_shop_list': True, 'auto_fill_all': True, 'workers': 2}
            )
        assert result['success'] is True
        assert FakeRepeatBatch.last_run['skip_done'] is False
        assert FakeRepeatBatch.last_run['done_ids'] == set()
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
                object(), {'auto_shop_list': True, 'auto_fill_all': True, 'workers': 2}
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

def test_partition_is_contiguous_complete_and_unique():
    targets = [{'shop_id': str(i)} for i in range(10)]
    chunks = FollowupBatch._partition_targets(targets, 3)
    assert [len(chunk) for chunk in chunks] == [4, 3, 3]
    flattened = [item['shop_id'] for chunk in chunks for _, item in chunk]
    assert flattened == [str(i) for i in range(10)]


def test_thread_pool_io_tasks_overlap():
    started = []
    lock = threading.Lock()

    def work(_item):
        with lock:
            started.append(time.perf_counter())
        time.sleep(0.10)

    from concurrent.futures import ThreadPoolExecutor
    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(work, range(10)))
    elapsed = time.perf_counter() - started_at
    assert elapsed < 0.45, elapsed
    assert len(started) == 10


if __name__ == '__main__':
    test_saved_credentials_are_written_and_read_back_from_controls()
    test_repeat_followup_asks_and_allows_explicit_continue()
    test_repeat_followup_refusal_stops_without_running_batch()
    test_log_copy_and_clear_actions()
    test_partition_is_contiguous_complete_and_unique()
    test_thread_pool_io_tasks_overlap()
    print('PASS UI sync and concurrency overlap')
