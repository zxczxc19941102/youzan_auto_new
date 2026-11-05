"""Offline regressions for readable login logs and duplicate-skip pacing."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / 'py'))

import config
import main
from crm_api import CrmApiFollowupBatch
from task_runtime import TaskCancelled


class SkipPacingTests(unittest.TestCase):
    """Check read-only pacing separately from the publishing safety interval."""

    def make_batch(self, kind='customer', runtime=None):
        messages = []
        batch = CrmApiFollowupBatch(SimpleNamespace(page=None), kind, messages.append, runtime=runtime)
        batch.api = Mock()
        batch.api.capture.return_value = []
        batch.api.save_capture.return_value = None
        batch.api.build_payload.side_effect = lambda item: {'id': item['id']}
        batch.api._fetch.return_value = {'code': 0, 'data': {'visitRecordId': 42}}
        batch.api.is_success.return_value = True
        batch.api.verify_saved.return_value = {'verified': True}
        return batch, messages

    def test_clue_and_customer_skips_wait_one_second_without_writes(self):
        for kind in ('clue', 'customer'):
            with self.subTest(kind=kind):
                batch, messages = self.make_batch(kind)
                batch._sleep = Mock()
                items = [{'id': str(i), 'lastVisitTime': time.strftime('%Y-%m-%d 12:00:00')} for i in range(4)]
                with patch.object(config, 'CRM_API_SKIP_TODAY_DUPLICATES', True):
                    result = batch.run(items=items)
                self.assertEqual(result['skipped'], 4)
                self.assertEqual([call.args[0] for call in batch._sleep.call_args_list], [1.0] * 3)
                self.assertEqual(sum('\u4eca\u65e5\u5df2\u586b\u5199\u8ddf\u8fdb' in m for m in messages), 4)
                batch.api._fetch.assert_not_called()
                batch.api.build_payload.assert_not_called()

    def test_mixed_records_keep_publish_and_error_throttle(self):
        batch, messages = self.make_batch()
        batch._sleep = Mock()
        batch._already_done = Mock(side_effect=[True, True, False, True, False, True])
        batch.api._fetch.side_effect = [
            {'code': 0, 'data': {'visitRecordId': 42}},
            RuntimeError('SERVER_RAW_ERROR_4'),
        ]
        result = batch.run(items=[{'id': str(i)} for i in range(6)])
        self.assertEqual([c.args[0] for c in batch._sleep.call_args_list], [1.0, 1.0, 3.01, 1.0, 3.01])
        self.assertEqual(result['skipped'], 4)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(batch.api._fetch.call_count, 2)
        self.assertTrue(any('SERVER_RAW_ERROR_4' in message for message in messages))
        self.assertEqual(config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS, 3.01)

    def test_skip_wait_can_be_cancelled_before_next_item(self):
        class CancelEvent:
            def is_set(self):
                return False

            def wait(self, seconds):
                self.seconds = seconds
                return True

        event = CancelEvent()
        runtime = SimpleNamespace(stop_event=event)
        batch, _ = self.make_batch(runtime=runtime)
        batch._already_done = Mock(return_value=True)
        with self.assertRaises(TaskCancelled):
            batch.run(items=[{'id': 'a'}, {'id': 'b'}])
        self.assertEqual(event.seconds, 1.0)
        self.assertEqual(batch._already_done.call_count, 1)
        batch.api._fetch.assert_not_called()

    def test_shop_skips_use_same_one_second_interval(self):
        shops = [{'shop_id': str(i)} for i in range(3)]
        batch = Mock()
        batch.load_done_ids.return_value = {'0', '1', '2'}
        batch.today_visit_ids.return_value = set()
        batch.shop_ids.side_effect = lambda shop: {shop['shop_id']}
        batch.run.return_value = {'success': True, 'failed': 0, 'cancelled': 0}
        reader = Mock(shops=shops)
        reader.run.return_value = {'success': True}
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False
        with patch('shop_list_reader.ShopListReader', return_value=reader), \
             patch('followup_batch.FollowupBatch', return_value=batch), \
             patch.object(main, 'runner', None), \
             patch.object(main, 'stop_flag', stop_event), \
             patch.object(main, 'DaemonThreadPoolExecutor'), \
             patch.object(main, '_make_debug_log', return_value=Mock()), \
             patch.object(main, 'log') as output:
            result = main._auto_followup_after_login(object(), {
                'auto_fill_all': True, 'auto_shop_list': True,
                'followup_shop': True, 'followup_clue': False, 'followup_customer': False,
            })
        self.assertTrue(result['success'])
        self.assertEqual([c.args[0] for c in stop_event.wait.call_args_list], [1.0, 1.0])
        self.assertEqual(batch.run.call_args.kwargs['shops'], [])
        self.assertEqual(sum('\u4eca\u65e5\u5df2\u586b\u5199\u8ddf\u8fdb' in str(c.args[0]) and 'ID=' in str(c.args[0]) for c in output.call_args_list), 3)

    def test_real_elapsed_time_for_four_offline_skips(self):
        batch, _ = self.make_batch()
        batch._already_done = Mock(return_value=True)
        timestamps = []
        batch._log_progress = lambda message, current, total: timestamps.append(time.monotonic()) if message.startswith('\u8df3\u8fc7') else None
        started = time.monotonic()
        result = batch.run(items=[{'id': str(i)} for i in range(4)])
        duration = time.monotonic() - started
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        self.assertEqual(result['skipped'], 4)
        self.assertEqual(len(gaps), 3)
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.99)
            self.assertLess(gap, 2.0)
        print('OFFLINE_SKIP_TIMING seconds=%.3f gaps=%s' % (duration, [round(g, 3) for g in gaps]))


class LoginLogTests(unittest.TestCase):
    def test_no_question_mark_placeholders_in_login_worker(self):
        import inspect
        self.assertNotIn('???', inspect.getsource(main.login_worker))

    def test_success_log_is_readable_and_follows_confirmation(self):
        fake = Mock()
        fake.start_login.return_value = True
        fake.needs_sms = False
        fake.confirm_workbench.return_value = True
        messages = []
        def emit(message):
            if message == '\u6210\u529f\u767b\u5f55\u5de5\u4f5c\u53f0':
                fake.confirm_workbench.assert_called_once()
            messages.append(message)
        with patch.object(main, 'YouzanBot', return_value=fake), \
             patch.object(main, '_make_debug_log', return_value=Mock()), \
             patch.object(main, 'stop_flag', threading.Event()), \
             patch.object(main, 'runner', None), \
             patch.dict(main.fresh_login_requested, {'v': True}), \
             patch.object(main, '_save_login_config'), \
             patch.object(main, '_auto_followup_after_login', return_value={'success': True}), \
             patch.object(main, 'log', side_effect=emit):
            self.assertTrue(main.login_worker('offline-user', 'offline-password', {}))
        self.assertIn('\u6210\u529f\u767b\u5f55\u5de5\u4f5c\u53f0', messages)
        fake.load_cookies_and_go.assert_not_called()
        fake.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
