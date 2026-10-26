# -*- coding: utf-8 -*-
"""Offline regression checks for login, proxy, and concise follow-up logging."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

import followup_batch  # noqa: E402
from shop_list_reader import ShopListReader  # noqa: E402
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
    def seven_worker_parallel_submission_uses_valid_worker_signature():
        """Regression: seven preallocated chunks must execute, not fail in submit()."""
        from concurrent.futures import ThreadPoolExecutor
        import time

        batch = followup_batch.FollowupBatch.__new__(followup_batch.FollowupBatch)
        batch.executor = ThreadPoolExecutor(max_workers=7)
        batch.debug = lambda _message: None
        seen = []

        def fake_worker(chunk):
            time.sleep(0.01)
            seen.append(tuple(index for index, _shop in chunk))

        batch._worker = fake_worker
        targets = [{'shop_id': str(index)} for index in range(30)]
        try:
            batch._run_parallel(targets, 7)
        finally:
            batch.executor.shutdown(wait=True)

        processed = [index for chunk in seen for index in chunk]
        assert sorted(processed) == list(range(1, 31))
        assert len(seen) == 7
        assert sorted(len(chunk) for chunk in seen) == [4, 4, 4, 4, 4, 5, 5]

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
        assert 'PRIVATE_SHOP_ID' not in text


def main():
    checks = [
        ('Edge proxy configuration', RegressionTests.proxy_is_passed_to_edge),
        ('SMS response event pumping',
         RegressionTests.sms_wait_pumps_playwright_response_events),
        ('strict workbench confirmation',
         RegressionTests.only_confirmed_workbench_is_success),
        ('actual shop-menu navigation',
         RegressionTests.my_shops_navigation_is_a_real_click),
        ('seven-worker parallel submission',
         RegressionTests.seven_worker_parallel_submission_uses_valid_worker_signature),
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

