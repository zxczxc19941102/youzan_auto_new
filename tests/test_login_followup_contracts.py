# -*- coding: utf-8 -*-
"""Offline regression checks for login, proxy, and concise follow-up logging."""
import sys
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / 'py'))

import main as app_main  # noqa: E402
import config  # noqa: E402
import followup_batch  # noqa: E402
from shop_list_reader import ShopListReader  # noqa: E402
from shop_followup import ShopFollowupCase  # noqa: E402
from youzan_bot import QUIET_RUNTIME_STEPS, YouzanBot  # noqa: E402


class FakeRequest:
    method = 'POST'


class FakeResponse:
    request = FakeRequest()
    url = 'https://account.youzan.com/api/captcha/sms/send.json'
    status = 200

    @staticmethod
    def json():
        return {'code': 0, 'success': True}


class FakeSmsPage:
    def __init__(self, bot):
        self.bot = bot
        self.wait_count = 0
        self.clicked = False
        self.response_pending = True

    def locator(self, _selector):
        return self

    def click(self, **_kwargs):
        self.clicked = True

    def wait_for_timeout(self, _milliseconds):
        self.wait_count += 1
        if self.response_pending:
            self.response_pending = False
            self.bot._record_auth_response(FakeResponse())


class FakeChromium:
    def __init__(self):
        self.launch_options = None

    def launch(self, **options):
        self.launch_options = options
        return FakeBrowser()


class FakeBrowser:
    def new_context(self, **_options):
        return FakeContext()

    def close(self):
        pass


class FakeContext:
    def set_default_timeout(self, _timeout):
        pass

    def set_default_navigation_timeout(self, _timeout):
        pass

    def new_page(self):
        return SimpleNamespace(on=lambda *_args: None)


class FakeMenuItem:
    def __init__(self, page):
        self.page = page

    def is_visible(self):
        return True

    def click(self, **_kwargs):
        self.page.url = 'https://qudao.youzan.com/index#/shop/list/my'


class FakeMenuLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        return 1 if self.selector == 'a[href*="/shop/list/my"]' else 0

    def nth(self, _index):
        return FakeMenuItem(self.page)


class FakeMenuPage:
    url = 'https://qudao.youzan.com/#/workbench'

    def locator(self, selector):
        return FakeMenuLocator(self, selector)

    def wait_for_timeout(self, _milliseconds):
        pass


class FakeFollowupCase:
    def __init__(self, *_args, **_kwargs):
        self.form_values = {
            '\u8ddf\u8fdb\u65b9\u5f0f': '\u5fae\u4fe1',
            '\u8ddf\u8fdb\u76ee\u7684': '\u4e86\u89e3\u8fd1\u51b5',
            '\u6c9f\u901a\u60c5\u51b5': '\u5df2\u5b8c\u6210\u6c9f\u901a',
            '\u5ba2\u6237\u9700\u6c42/\u5907\u6ce8': '\u540e\u7eed\u56de\u8bbf',
        }

    def run(self):
        return {'success': True, 'error': '', 'steps': []}


class RegressionTests:
    @staticmethod
    def proxy_is_passed_to_edge():
        chromium = FakeChromium()
        fake_pw = SimpleNamespace(chromium=chromium, stop=lambda: None)
        fake_sync = SimpleNamespace(start=lambda: fake_pw)
        with patch('youzan_bot.sync_playwright', return_value=fake_sync):
            bot = YouzanBot(lambda _message: None,
                            proxy_server='http://127.0.0.1:10808')
            bot._ensure_browser()
        assert chromium.launch_options['proxy']['server'] == \
            'http://127.0.0.1:10808'
        assert chromium.launch_options['headless'] is True

    @staticmethod
    def sms_wait_pumps_playwright_response_events():
        messages = []
        bot = YouzanBot(messages.append)
        page = FakeSmsPage(bot)
        bot.page = page
        bot._solve_slider = lambda: True
        assert bot.send_sms() is True
        assert page.clicked and page.wait_count >= 1
        assert any('code=0' in message for message in messages)

    @staticmethod
    def only_confirmed_workbench_is_success():
        bot = YouzanBot(lambda _message: None)
        bot._is_logged_in = lambda: False
        assert bot.confirm_workbench(timeout=0) is False
        bot._is_logged_in = lambda: True
        bot._wait_workbench_ready = lambda timeout=0: True
        assert bot.confirm_workbench(timeout=0) is True
        assert {'\u521d\u59cb\u5316\u4f1a\u8bdd', '\u8f93\u5165\u8d26\u53f7\u5bc6\u7801',
                '\u6ed1\u5757\u9a8c\u8bc1\u7801\u83b7\u53d6'} <= QUIET_RUNTIME_STEPS

    @staticmethod
    def my_shops_navigation_is_a_real_click():
        messages = []
        page = FakeMenuPage()
        reader = ShopListReader(SimpleNamespace(page=page), messages.append)
        assert reader._click_my_shops() is True
        assert page.url.endswith('#/shop/list/my')
        assert messages == ['\u70b9\u51fb\uff1a\u5e97\u94fa--\u6211\u7684\u5e97\u94fa']

    @staticmethod
    def start_login_forces_real_credential_validation():
        class FakeBot:
            def __init__(self, *_args, **_kwargs):
                self.start_login_called = False
                self.load_cookie_called = False

            def bind_runtime(self, _runtime):
                pass

            def load_cookies_and_go(self):
                self.load_cookie_called = True
                raise AssertionError('password login must not use local cookies')

            def start_login(self, _user, _password):
                self.start_login_called = True
                return False

            def close(self):
                pass

        fake_bot = FakeBot()
        old_flag = app_main.fresh_login_requested['v']
        try:
            app_main.fresh_login_requested['v'] = True
            with patch.object(app_main, 'YouzanBot', return_value=fake_bot), \
                 patch.object(app_main, '_ck_token_exist', return_value=True), \
                 patch.object(app_main, '_make_debug_log', return_value=lambda _message: None), \
                 patch.object(app_main, 'log'), \
                 patch.object(app_main, '_auto_followup_after_login'):
                result = app_main.login_worker('user', 'wrong-password', {'proxy_server': ''})
            assert result is False
            assert fake_bot.start_login_called is True
            assert fake_bot.load_cookie_called is False
        finally:
            app_main.fresh_login_requested['v'] = old_flag

    @staticmethod
    def followup_server_warning_is_recorded_without_hiding_success():
        messages = []
        case = ShopFollowupCase.__new__(ShopFollowupCase)
        case.log = messages.append
        case.server_warnings = []
        case._log_publish_detail({
            'response': json.dumps({
                'code': 0,
                'data': {
                    'visitRecordId': 123,
                    'visitReturnCodes': [
                        {'code': 4, 'message': '\u64cd\u4f5c\u592a\u5feb\uff0c\u53ef\u80fd\u4f1a\u51fa\u9519\u5662'},
                        {'code': 0, 'message': '\u6210\u529f\u6dfb\u52a0\u62dc\u8bbf'},
                    ],
                },
            }, ensure_ascii=False),
        })
        assert case.server_warnings == [{
            'code': '4', 'message': '\u64cd\u4f5c\u592a\u5feb\uff0c\u53ef\u80fd\u4f1a\u51fa\u9519\u5662',
        }]
        assert any('原始错误码=4' in message for message in messages)

    @staticmethod
    def root_server_error_is_preserved_verbatim():
        messages = []
        case = ShopFollowupCase.__new__(ShopFollowupCase)
        case.log = messages.append
        case.server_warnings = []
        case._log_publish_detail({
            'status': 429,
            'resp_code': '429001',
            'response': json.dumps({
                'code': '429001',
                'message': 'rate limit from server',
            }),
        })
        assert case.server_warnings == [{
            'code': '429001', 'message': 'rate limit from server',
        }]
        assert len(messages) == 1
        assert '429001' in messages[0]
        assert 'rate limit from server' in messages[0]

    @staticmethod
    def single_thread_keeps_sequential_batch_execution():
        calls = []
        debug_messages = []
        batch = followup_batch.FollowupBatch.__new__(followup_batch.FollowupBatch)
        batch.executor = None
        batch.runtime = None
        batch.abort_event = threading.Event()
        batch.stop_event = threading.Event()
        batch.results = []
        batch.debug = debug_messages.append
        batch.raw = lambda _message: None
        batch._check = lambda: None
        batch._pace = lambda: None
        batch._note = lambda _message: None
        batch._fill_one = lambda shop, position: calls.append((shop['shop_id'], position)) or True
        batch._summary = lambda: (2, 2, 0, 0)
        batch._save_report = lambda *_args: None
        result = batch.run(
            all_shops=True,
            workers=1,
            skip_done=False,
            shops=[{'shop_id': 'a'}, {'shop_id': 'b'}],
            announce_count=False,
        )
        assert result == {'attempted': 2, 'success': 2, 'failed': 0, 'cancelled': 0, 'server_warnings': 0}
        assert calls == [('a', 1), ('b', 2)]

    @staticmethod
    def partition_and_single_thread_enforcement():
        targets = [{'shop_id': str(index)} for index in range(30)]
        expected_sizes = {2: [15, 15], 3: [10, 10, 10], 4: [8, 8, 7, 7]}
        for workers, sizes in expected_sizes.items():
            chunks = followup_batch.FollowupBatch._partition_targets(
                targets, workers
            )
            assert [len(chunk) for chunk in chunks] == sizes
            flattened = [shop['shop_id'] for chunk in chunks for _, shop in chunk]
            assert flattened == [shop['shop_id'] for shop in targets]

        calls = []
        batch = followup_batch.FollowupBatch.__new__(followup_batch.FollowupBatch)
        batch.executor = None
        batch.runtime = None
        batch.abort_event = threading.Event()
        batch.stop_event = threading.Event()
        batch.results = []
        batch.raw = lambda _message: None
        batch.debug = lambda _message: None
        batch._note = lambda _message: None
        batch._check = lambda: None
        batch._pace = lambda: None
        batch._fill_one = lambda shop, position: calls.append((shop['shop_id'], position)) or True
        batch._summary = lambda: (30, 30, 0, 0)
        batch._save_report = lambda *_args: None

        with patch.object(config, 'FOLLOWUP_CONCURRENCY_ENABLED', True), \
             patch.object(config, 'FOLLOWUP_MAX_WORKERS', 4):
            result = batch.run(
                all_shops=True,
                workers=7,
                skip_done=False,
                shops=targets,
                announce_count=False,
            )
        assert result['attempted'] == 30
        assert calls == [(str(index), index + 1) for index in range(30)]

    @staticmethod
    def followup_logs_use_batch_progress_without_start_noise():
        messages = []

        class FakeRuntime:
            def emit_progress(self, message, current, total, **_kwargs):
                messages.append((message, current, total))

        batch = followup_batch.FollowupBatch.__new__(followup_batch.FollowupBatch)
        batch.runtime = FakeRuntime()
        batch.raw = messages.append
        batch.total_targets = 30
        batch.progress_position = 1
        batch._note('成功 条目店铺ID=40785558')
        batch.progress_position = 2
        batch._note('成功 条目店铺ID=44588156')

        assert messages == [
            ('成功 条目店铺ID=40785558', 1, 30),
            ('成功 条目店铺ID=44588156', 2, 30),
        ]

    @staticmethod
    def default_batch_uses_fixed_publish_interval():
        batch = followup_batch.FollowupBatch(
            SimpleNamespace(page=object()),
            lambda _message: None,
            lambda *_args: None,
            lambda _message: None,
            dry_run=False,
        )
        assert batch.publish_gate is not None
        assert batch.publish_gate.interval_seconds == 3.01
        dry_batch = followup_batch.FollowupBatch(
            SimpleNamespace(page=object()),
            lambda _message: None,
            lambda *_args: None,
            lambda _message: None,
            dry_run=True,
        )
        assert dry_batch.publish_gate is None

    @staticmethod
    def followup_toggle_waits_for_async_panel_and_clicks_dynamic_class():
        class FakeFormLocator:
            def __init__(self, page):
                self.page = page

            def filter(self, **_kwargs):
                return self

            def count(self):
                return 1 if self.page.clicked else 0

        class FakePage:
            def __init__(self):
                self.clicked = False
                self.wait_timeout = None

            def locator(self, _selector):
                return FakeFormLocator(self)

            def wait_for_function(self, _script, timeout=None):
                self.wait_timeout = timeout

            def evaluate(self, script):
                if 'SidePanel_toggle' in script:
                    self.clicked = True
                    return {
                        'className': 'SidePanel_toggle_dynamic SidePanel_hide_dynamic',
                        'text': '\u62dc\u8bbf\u5546\u5bb6',
                    }
                return []

        page = FakePage()
        messages = []
        case = ShopFollowupCase.__new__(ShopFollowupCase)
        case.page = page
        case.log = messages.append
        case._capture = lambda _tag: None
        case._sleep = lambda _seconds: None
        case.stop_event = None

        assert case.open_followup() == 'form ready'
        assert page.clicked is True
        assert page.wait_timeout == 30000
        assert any('\u5c55\u5f00\u53f3\u4fa7\u8868\u5355' in message for message in messages)

    @staticmethod
    def parallel_followup_logs_content_once_without_shop_details():
        messages = []
        batch = followup_batch.FollowupBatch.__new__(followup_batch.FollowupBatch)
        batch.bot = SimpleNamespace(proxy_server='http://127.0.0.1:10808')
        batch.runtime = SimpleNamespace(emit=messages.append)
        batch.debug = lambda _message: None
        batch.dry_run = False
        batch.publish_lock = threading.Lock()
        batch.run_id = 'test'
        batch.total_targets = 1
        batch._print_lock = threading.Lock()
        batch._content_logged = False
        batch.results = []
        batch.raw = lambda _message: None
        batch.styled = lambda *_args: None
        batch._check = lambda: None
        with patch.object(followup_batch, 'ShopFollowupCase', FakeFollowupCase):
            result = batch._fill_direct(
                object(),
                {'shop_name': 'PRIVATE_SHOP_NAME', 'shop_id': 'PRIVATE_SHOP_ID',
                 'detail_url': 'https://example.invalid/detail'},
                1,
            )
        text = '\n'.join(messages)
        assert result is True
        assert text.count('```') == 2
        assert text.count('\u8ddf\u8fdb\u5185\u5bb9\uff1a') == 1
        assert '\u5fae\u4fe1' in text and '\u540e\u7eed\u56de\u8bbf' in text
        assert 'PRIVATE_SHOP_NAME' not in text
        assert 'PRIVATE_SHOP_ID' in text


def main():
    checks = [
        ('Edge proxy configuration', RegressionTests.proxy_is_passed_to_edge),
        ('SMS response event pumping',
         RegressionTests.sms_wait_pumps_playwright_response_events),
        ('strict workbench confirmation',
         RegressionTests.only_confirmed_workbench_is_success),
        ('actual shop-menu navigation',
         RegressionTests.my_shops_navigation_is_a_real_click),
        ('real credential validation',
         RegressionTests.start_login_forces_real_credential_validation),
        ('server warning recording',
         RegressionTests.followup_server_warning_is_recorded_without_hiding_success),
        ('root server error preserved',
         RegressionTests.root_server_error_is_preserved_verbatim),
        ('single-thread sequential batch',
         RegressionTests.single_thread_keeps_sequential_batch_execution),
        ('partition and fixed single-thread batch',
         RegressionTests.partition_and_single_thread_enforcement),
        ('follow-up log progress without start noise',
         RegressionTests.followup_logs_use_batch_progress_without_start_noise),
        ('fixed publish interval',
         RegressionTests.default_batch_uses_fixed_publish_interval),
        ('async follow-up panel toggle',
         RegressionTests.followup_toggle_waits_for_async_panel_and_clicks_dynamic_class),
        ('parallel follow-up log content',
         RegressionTests.parallel_followup_logs_content_once_without_shop_details),
    ]
    failed = []
    for name, check in checks:
        try:
            check()
        except Exception as exc:
            failed.append(name)
            print('FAIL %s: %r' % (name, exc))
        else:
            print('PASS %s' % name)
    print('RESULT =', 'FAIL' if failed else 'PASS')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
