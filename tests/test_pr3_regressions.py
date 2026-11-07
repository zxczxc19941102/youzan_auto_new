"""Offline regressions for PR #3 saved-login lifetime and safe API retries."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))
sys.path.insert(0, str(ROOT))

import main
from crm_api import CrmApiClient, CrmApiError
from task_runtime import TaskCancelled


class SavedLoginTests(unittest.TestCase):
    def setUp(self):
        self.original_fresh = main.fresh_login_requested['v']
        self.original_bot = main.bot
        self.browser = Mock()
        self.browser.load_cookies_and_go.return_value = True
        self.browser.confirm_workbench.return_value = True
        self.browser.closed = False
        self.browser.close.side_effect = lambda: setattr(self.browser, 'closed', True)
        patches = {
            'YouzanBot': Mock(return_value=self.browser),
            '_make_debug_log': Mock(return_value=lambda _message: None),
            '_save_login_config': Mock(),
            '_auto_followup_after_login': Mock(return_value={'success': True}),
            'log': Mock(),
            'stop_flag': Mock(is_set=Mock(return_value=False)),
        }
        for name, value in patches.items():
            patcher = patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.restore_state)

    def restore_state(self):
        main.fresh_login_requested['v'] = self.original_fresh
        main.bot = self.original_bot

    def run_login(self):
        return main._mfree_login_task('test-user', 'test-password', {})

    def test_browser_remains_open_until_followup_finishes(self):
        def followup(browser, _settings):
            self.assertIs(browser, self.browser)
            self.assertIs(main.bot, browser)
            self.assertFalse(browser.closed)
            return {'success': True}
        main._auto_followup_after_login.side_effect = followup
        self.assertTrue(self.run_login())
        self.browser.close.assert_called_once()
        self.assertTrue(self.browser.closed)
        self.assertIsNone(main.bot)
        self.assertFalse(main.fresh_login_requested['v'])

    def test_failed_confirmation_never_saves_or_runs_followup(self):
        self.browser.confirm_workbench.return_value = False
        self.assertFalse(self.run_login())
        main._save_login_config.assert_not_called()
        main._auto_followup_after_login.assert_not_called()
        self.browser.close.assert_called_once()
        self.assertTrue(main.fresh_login_requested['v'])
        self.assertIsNone(main.bot)

    def test_failed_cookie_login_does_not_confirm_or_run_followup(self):
        self.browser.load_cookies_and_go.return_value = False
        self.assertFalse(self.run_login())
        self.browser.confirm_workbench.assert_not_called()
        main._save_login_config.assert_not_called()
        main._auto_followup_after_login.assert_not_called()
        self.browser.close.assert_called_once()

    def test_followup_failure_returns_false_and_closes_session(self):
        main._auto_followup_after_login.return_value = {'success': False}
        self.assertFalse(self.run_login())
        self.browser.close.assert_called_once()
        self.assertIsNone(main.bot)

    def test_login_and_followup_exceptions_close_session_and_mask_password(self):
        for target in (self.browser.load_cookies_and_go, main._auto_followup_after_login):
            with self.subTest(target=target):
                target.side_effect = RuntimeError('failed with test-password')
                self.browser.close.reset_mock()
                main.log.reset_mock()
                self.assertFalse(self.run_login())
                self.browser.close.assert_called_once()
                self.assertIsNone(main.bot)
                self.assertNotIn('test-password', str(main.log.call_args_list))
                target.side_effect = None

    def test_cancellation_closes_session_and_returns_false(self):
        self.browser.load_cookies_and_go.side_effect = TaskCancelled()
        self.assertFalse(self.run_login())
        main._auto_followup_after_login.assert_not_called()
        self.browser.close.assert_called_once()

    def test_pre_cancelled_task_does_not_create_browser(self):
        main.stop_flag.is_set.return_value = True
        self.assertFalse(self.run_login())
        main.YouzanBot.assert_not_called()
        main._auto_followup_after_login.assert_not_called()


class ApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock()
        self.client = CrmApiClient(self.page, 'customer')
        self.client._sleep = Mock()
        patcher = patch('crm_api.config.CRM_API_RETRY_ATTEMPTS', 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, status=200, code=0):
        return {'status': status, 'text': json.dumps({'code': code, 'data': {}})}

    def test_ambiguous_save_timeout_is_never_retried(self):
        self.page.evaluate.side_effect = TimeoutError('response lost after save')
        with self.assertRaises(CrmApiError):
            self.client._fetch('POST', '/visit/save', {'form': {}})
        self.page.evaluate.assert_called_once()
        self.client._sleep.assert_not_called()

    def test_save_http_and_business_failures_are_never_retried(self):
        for status, code in ((500, 0), (429, 0), (200, 500), (200, 43002)):
            with self.subTest(status=status, code=code):
                self.page.evaluate.reset_mock()
                self.page.evaluate.return_value = self.response(status, code)
                with self.assertRaises(CrmApiError):
                    self.client._fetch('POST', '/visit/save', {'form': {}})
                self.page.evaluate.assert_called_once()
                self.client._sleep.assert_not_called()

    def test_explicit_read_only_endpoints_can_retry(self):
        for method, path in (
            ('GET', '/visit/getVisitForm'),
            ('POST', '/visit/search'),
            ('POST', '/resource/my/list'),
            ('POST', '/kaOpportunity/myOpportunities'),
        ):
            with self.subTest(method=method, path=path):
                self.page.evaluate.reset_mock()
                self.page.evaluate.side_effect = [TimeoutError('transient'), self.response()]
                self.assertEqual(self.client._fetch(method, path)['code'], 0)
                self.assertEqual(self.page.evaluate.call_count, 2)

    def test_unknown_write_endpoints_are_not_retried(self):
        self.page.evaluate.side_effect = TimeoutError('ambiguous write')
        with self.assertRaises(CrmApiError):
            self.client._fetch('POST', '/unknown-write', {})
        self.page.evaluate.assert_called_once()

    def test_read_retries_remain_bounded(self):
        self.page.evaluate.side_effect = TimeoutError('read unavailable')
        with self.assertRaises(CrmApiError):
            self.client._fetch('POST', '/visit/search', {})
        self.assertEqual(self.page.evaluate.call_count, 3)
        self.assertEqual(self.client._sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
