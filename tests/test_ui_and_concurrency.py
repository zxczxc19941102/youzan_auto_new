# -*- coding: utf-8 -*-
"""?????????? UI ????????????"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
sys.path.insert(0, str(ROOT))

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
        time.sleep(0.08)

    from concurrent.futures import ThreadPoolExecutor
    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(work, range(5)))
    elapsed = time.perf_counter() - started_at
    assert elapsed < 0.30, elapsed
    assert len(started) == 5


if __name__ == '__main__':
    test_saved_credentials_are_written_and_read_back_from_controls()
    test_partition_is_contiguous_complete_and_unique()
    test_thread_pool_io_tasks_overlap()
    print('PASS UI sync and concurrency overlap')
