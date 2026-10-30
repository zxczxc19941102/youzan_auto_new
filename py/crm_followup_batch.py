# -*- coding: utf-8 -*-
"""\u7ebf\u7d22/\u5ba2\u6237\u8ddf\u8fdb\u8bb0\u5f55\u6279\u5904\u7406\uff1b\u9ed8\u8ba4\u6cbf\u7528\u73b0\u6709\u8868\u5355\uff0c\u652f\u6301 dry-run \u4e0d\u53d1\u5e03\u3002"""
import re
import threading
import time
from types import SimpleNamespace

from crm_list_reader import TARGETS

import config
from shop_followup import ShopFollowupCase
from task_runtime import TaskCancelled


class CrmFollowupBatch:
    def __init__(self, bot, target_kind, raw_log, styled_log, debug_log, dry_run=False, runtime=None):
        self.bot = bot
        self.target_kind = target_kind
        self.raw = raw_log
        self.styled = styled_log
        self.debug = debug_log
        self.dry_run = bool(dry_run)
        self.runtime = runtime
        self.stop_event = runtime.stop_event if runtime is not None else threading.Event()
        self.total_targets = 0
        self.progress_position = 0
        self.results = []
        self.current_page_no = None

    def _check(self):
        if self.stop_event.is_set():
            raise TaskCancelled()

    def _log(self, message, kind='normal'):
        if kind == 'success':
            self.styled(message, 'success')
        elif kind == 'error':
            self.styled(message, 'error')
        else:
            self.raw(message)

    def _list_rows(self):
        rows = self.bot.page.locator('.fun-table tbody tr')
        if rows.count():
            return rows
        return self.bot.page.locator('[role="row"], [class*="table-row"]')

    def _wait_for_row(self, row_index, expected_text=''):
        """Wait briefly for a requested row after SPA pagination/virtualization."""
        deadline = time.monotonic() + 8.0
        expected = re.sub(r'\s+', ' ', str(expected_text or '')).strip()
        while time.monotonic() < deadline:
            self._check()
            rows = self._list_rows()
            try:
                if rows.count() > int(row_index):
                    row = rows.nth(int(row_index))
                    text = re.sub(r'\s+', ' ', row.inner_text()).strip()
                    if not expected or expected[:40] in text:
                        return row
            except Exception:
                pass
            self.stop_event.wait(0.3)
        return None

    def _ensure_list_page(self, page_no, expected_text=''):
        """Open and verify the requested CRM list page."""
        page_no = max(1, int(page_no or 1))
        if self.current_page_no == page_no:
            return
        self.bot.page.goto(
            TARGETS[self.target_kind]['list_url'],
            wait_until='domcontentloaded', timeout=config.NAV_TIMEOUT_MS,
        )
        self.bot.page.wait_for_timeout(1000)
        pager = self.bot.page.locator('.fun-pagination-pager')
        clicked = False
        for _ in range(8):
            for index in range(pager.count()):
                button = pager.nth(index)
                try:
                    if button.inner_text().strip() == str(page_no) and button.is_visible():
                        button.scroll_into_view_if_needed(timeout=config.ACTION_TIMEOUT_MS)
                        button.click(force=True)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
            self.bot.page.wait_for_timeout(300)
        if not clicked:
            raise RuntimeError('\u672a\u627e\u5230\u5ba2\u6237\u5217\u8868\u7b2c%d\u9875\u6309\u94ae' % page_no)
        self.bot.page.wait_for_timeout(800)
        if self._wait_for_row(0) is None:
            raise RuntimeError('\u5ba2\u6237\u5217\u8868\u7b2c%d\u9875\u52a0\u8f7d\u8d85\u65f6' % page_no)
        self.current_page_no = page_no

    def _open_list_followup(self, item):
        """\u5728\u5217\u8868\u884c\u76f4\u63a5\u70b9\u51fb\u300c\u5199\u8ddf\u8fdb/\u53bb\u8ddf\u8fdb\u300d\u5e76\u7b49\u5f85\u8868\u5355\u51fa\u73b0\u3002"""
        self._ensure_list_page(item.get('page_no', 1), item.get('row_text', ''))
        row_index = item.get('row_index')
        if row_index is None:
            return False
        row = self._wait_for_row(row_index, item.get('row_text', ''))
        if row is None:
            self.debug('\u5217\u8868\u9875%d\u884c\u7d22\u5f15%d\u672a\u52a0\u8f7d' % (item.get('page_no', 1), row_index))
            return False
        try:
            # The last visible row may be outside the viewport or rendered by a
            # virtualized table only after scrolling.  Scroll once and hover;
            # this is a short cooperative wait, not a busy loop.
            row.scroll_into_view_if_needed(timeout=config.ACTION_TIMEOUT_MS)
            row.hover(timeout=config.ACTION_TIMEOUT_MS)
            self.bot.page.wait_for_timeout(250)
        except Exception as exc:
            self.debug('\u5217\u8868\u884c\u6eda\u52a8/\u60ac\u505c\u5931\u8d25\uff1a%r' % exc)
        actions = row.locator('button, [role="button"], a').filter(
            has_text=re.compile(r'\u5199\u8ddf\u8fdb|\u53bb\u8ddf\u8fdb|\u62dc\u8bbf\u5546\u5bb6')
        )
        for index in range(actions.count()):
            action = actions.nth(index)
            try:
                if action.is_visible():
                    action.scroll_into_view_if_needed(timeout=config.ACTION_TIMEOUT_MS)
                    action.click(force=True)
                    self.bot.page.wait_for_timeout(500)
                    return True
            except Exception:
                continue
        try:
            visible_actions = row.locator('button, [role="button"], a').all_inner_texts()
            self.debug('\u5217\u8868\u9875%d\u884c%d\u672a\u627e\u5230\u8ddf\u8fdb\u5165\u53e3\uff0c\u5f53\u524d\u6309\u94ae=%s' % (
                item.get('page_no', 1), row_index, visible_actions[:20]
            ))
        except Exception:
            pass
        return False

    def _close_inline_form(self):
        for name in ('\u5173\u95ed', '\u53d6\u6d88'):
            try:
                buttons = self.bot.page.get_by_role('button', name=name, exact=True)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if button.is_visible():
                        button.click(force=True)
                        self.bot.page.wait_for_timeout(300)
                        return
            except Exception:
                continue

    def _fill_one(self, item, position):
        self._check()
        item_id = str(item.get('item_id') or position)
        label = item.get('target_label') or self.target_kind
        detail_url = item.get('detail_url') or ''
        self.debug('%s\u5f00\u59cb\u5904\u7406\uff1aID=%s URL=%s' % (label, item_id, detail_url))
        page = None
        inline = False
        try:
            if not detail_url:
                inline = self._open_list_followup(item)
                if not inline:
                    raise RuntimeError('\u5217\u8868\u4e2d\u672a\u627e\u5230\u53ef\u70b9\u51fb\u7684\u5199\u8ddf\u8fdb/\u53bb\u8ddf\u8fdb\u6309\u94ae')
                page = self.bot.page
            else:
                page = self.bot.page.context.new_page()
                page.goto(detail_url, wait_until='domcontentloaded', timeout=config.NAV_TIMEOUT_MS)
            case = ShopFollowupCase(
                SimpleNamespace(page=page),
                self.debug,
                dry_run=self.dry_run,
                shop_id=item_id,
                detail_url=detail_url,
                run_id='%s_%s_%02d' % (self.target_kind, time.strftime('%Y%m%d_%H%M%S'), position),
                runtime=self.runtime,
                start_at_form=inline,
                record_label=label,
            )
            result = case.run()
            if result.get('success'):
                self._log('\u6210\u529f \u6761\u76ee%sID=%s%s' % (label, item_id, '\uff08\u4ec5\u586b\u5199\u672a\u53d1\u5e03\uff09' if self.dry_run else ''), 'success')
                self.results.append({'success': True, 'item_id': item_id, 'result': result})
                if inline:
                    self._close_inline_form()
                return True
            error = result.get('error') or '\u8ddf\u8fdb\u8868\u5355\u5904\u7406\u5931\u8d25'
            self._log('\u5931\u8d25 \u6761\u76ee%sID=%s\uff1a%s' % (label, item_id, error), 'error')
            self.results.append({'success': False, 'item_id': item_id, 'error': error, 'result': result})
            if inline:
                self._close_inline_form()
            return False
        except TaskCancelled:
            raise
        except Exception as exc:
            self._log('\u5931\u8d25 \u6761\u76ee%sID=%s\uff1a%r' % (label, item_id, exc), 'error')
            self.results.append({'success': False, 'item_id': item_id, 'error': repr(exc)})
            return False
        finally:
            if page is not None and not inline:
                try:
                    page.close()
                except Exception:
                    pass

    def run(self, items, count=None):
        targets = list(items or [])[:count] if count else list(items or [])
        self.total_targets = len(targets)
        if not targets:
            self._log('%s\u6ca1\u6709\u53ef\u586b\u5199\u7684\u8ddf\u8fdb\u8bb0\u5f55' % self.target_kind)
            return {'attempted': 0, 'success': 0, 'failed': 0, 'cancelled': 0}
        self._log('\u5f00\u59cb\u586b\u5199%s\u8ddf\u8fdb\u8bb0\u5f55\uff0c\u5171%d\u6761%s' % (
            self.target_kind, len(targets), '\uff08\u8bd5\u8fd0\u884c\uff0c\u4e0d\u70b9\u51fb\u53d1\u5e03\uff09' if self.dry_run else ''
        ))
        for position, item in enumerate(targets, 1):
            self._check()
            self.progress_position = position
            self._fill_one(item, position)
            if position < len(targets):
                self.stop_event.wait(0.2 if self.dry_run else config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS)
        success = sum(1 for result in self.results if result.get('success'))
        failed = len(self.results) - success
        return {'attempted': len(targets), 'success': success, 'failed': failed, 'cancelled': 0, 'results': self.results}
