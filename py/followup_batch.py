# -*- coding: utf-8 -*-
"""批量填写第一页前 N 个店铺的跟进记录，只输出规定格式的店铺块。"""
import html
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import config
from shop_followup import ShopFollowupCase
from shop_list_reader import SEPARATOR, SHOP_LIST_URL, ShopListReader
from youzan_bot import YouzanBot


class FollowupBatch:
    def __init__(self, bot, raw_log, styled_log, debug_log, dry_run=False):
        self.bot = bot
        self.page = bot.page
        self.raw = raw_log
        self.styled = styled_log
        self.debug = debug_log
        self.dry_run = dry_run
        self.run_id = time.strftime('%Y%m%d_%H%M%S')
        self.reader = ShopListReader(bot, lambda message: None, debug=debug_log)
        self.results = []
        self._print_lock = threading.Lock()
        self.stop_event = threading.Event()

    def load_first_page(self):
        self.reader._attach()
        self.debug('打开店铺列表：%s' % SHOP_LIST_URL)
        self.page.goto(SHOP_LIST_URL, wait_until='domcontentloaded')
        self.page.wait_for_timeout(1500)
        if not self.reader._wait_data(1):
            self.debug('店铺列表第一页未加载出数据')
            return []
        shops = self.reader._merge(
            self.reader._parse_dom_rows(),
            self.reader.api_pages.get(1, []),
        )
        self.debug('第一页有效店铺：%d 个' % len(shops))
        return shops

    def load_done_ids(self):
        """只跳过当天已成功的店铺；昨天的记录不影响今天。"""
        done = set()
        today = time.strftime('%Y%m%d')
        for path in Path(config.TXT_DIR).glob('followup_batch_*.json'):
            if today not in path.stem:
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('dry_run'):
                    continue
                for item in data.get('results', []):
                    if item.get('success') and item.get('shop_id'):
                        done.add(str(item['shop_id']))
            except Exception:
                continue
        return done

    def load_all_shops(self):
        self.reader._attach()
        self.debug('打开店铺列表：%s' % SHOP_LIST_URL)
        self.page.goto(SHOP_LIST_URL, wait_until='domcontentloaded')
        self.page.wait_for_timeout(1500)
        if not self.reader._wait_data(1):
            self.debug('店铺列表未加载出数据')
            return []
        shops = []
        page_no = 1
        while page_no <= 100:
            self.reader._wait_data(page_no)
            page_numbers = self.reader._page_numbers()
            max_page = max(page_numbers) if page_numbers else page_no
            shops.extend(self.reader._merge(
                self.reader._parse_dom_rows(),
                self.reader.api_pages.get(page_no, []),
            ))
            if page_no >= max_page or not self.reader._next_page():
                break
            page_no += 1
        self.debug('全部有效店铺：%d 个，%d 页' % (len(shops), page_no))
        return shops

    def _fill_one(self, shop, index):
        name = shop.get('shop_name') or ''
        shop_id = shop.get('kdt_id') or shop.get('shop_id') or ''
        detail_url, popup = self.reader.open_detail_popup(shop)
        success = False
        error = ''
        page_obj = popup

        if page_obj is None and detail_url:
            try:
                page_obj = self.page.context.new_page()
                page_obj.goto(detail_url, wait_until='domcontentloaded')
            except Exception as exc:
                error = repr(exc)
                page_obj = None

        result = {}
        if page_obj is None:
            error = error or '无法打开详情页'
        else:
            try:
                case = ShopFollowupCase(
                    SimpleNamespace(page=page_obj),
                    self.debug,
                    dry_run=self.dry_run,
                    shop_id=str(shop_id),
                    detail_url=detail_url,
                    run_id='%s_%02d' % (self.run_id, index),
                )
                result = case.run()
                success = bool(result.get('success'))
                error = result.get('error', '')
            except Exception as exc:
                error = repr(exc)
            finally:
                try:
                    page_obj.close()
                except Exception:
                    pass

        steps = result.get('steps', [])
        self._record_result(shop, detail_url, success, error, steps)
        return success

    def _record_result(self, shop, detail_url, success, error, steps):
        with self._print_lock:
            self.raw('正在进入店铺：%s' % (shop.get('shop_name') or '-'))
            self._emit_steps(steps)
            self._emit_block(
                shop.get('shop_name') or '',
                shop.get('kdt_id') or shop.get('shop_id') or '',
                detail_url,
                success,
            )
            self.results.append({
                'shop_name': shop.get('shop_name') or '',
                'shop_id': shop.get('kdt_id') or shop.get('shop_id') or '',
                'detail_url': detail_url,
                'success': success,
                'error': error,
                'steps': steps,
            })

    def _step_lines(self, steps):
        wanted = {
            '打开指定店铺详情',
            '点击拜访商家打开表单',
            '填写跟进表单',
            '确定并发布',
            '验证发布结果',
        }
        name_map = {
            '打开指定店铺详情': '打开详情',
            '点击拜访商家打开表单': '点击跟进',
            '填写跟进表单': '填写表单',
            '确定并发布': '发布',
            '验证发布结果': '验证结果',
        }
        lines = []
        for step in steps:
            name = step.get('name')
            if name not in wanted:
                continue
            status = '成功' if step.get('status') == 'OK' else '失败'
            lines.append('%s：%s' % (name_map.get(name, name), status))
        return lines

    def _emit_steps(self, steps):
        for line in self._step_lines(steps):
            self.raw(line)

    def _emit_block(self, name, shop_id, detail_url, success):
        self.raw(SEPARATOR)
        self.raw('店铺名称：%s' % (name or '-'))
        self.raw('店铺ID：%s' % (shop_id or '-'))
        self.raw('跟进网址：%s' % (detail_url or '-'))
        if success:
            self.styled('跟进记录：填写完成', 'success')
        else:
            self.styled('跟进记录：填写失败', 'fail')
        self.raw(SEPARATOR)

    def _summary(self):
        attempted = len(self.results)
        success = sum(1 for item in self.results if item['success'])
        failed = attempted - success
        self.raw(
            '填写 %d 条记录已完成，填写成功 %d 条，失败 %d 条。'
            % (attempted, success, failed)
        )
        return attempted, success, failed

    def _save_report(self, attempted, success, failed):
        json_path = os.path.join(
            config.TXT_DIR, 'followup_batch_%s.json' % self.run_id
        )
        with open(json_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'attempted': attempted,
                'success': success,
                'failed': failed,
                'dry_run': self.dry_run,
                'results': self.results,
            }, handle, ensure_ascii=False, indent=2)

        html_path = os.path.join(
            config.TXT_DIR, 'followup_batch_%s.html' % self.run_id
        )
        lines = [
            '<html><head><meta charset="utf-8"><style>',
            'body{font-family:Consolas,"Microsoft YaHei",monospace;font-size:15px;}',
            '.ok{color:#D40000;font-weight:bold;}',
            '.fail{color:#008000;}',
            'pre{white-space:pre-wrap;}',
            '</style></head><body><pre>',
        ]
        for item in self.results:
            for step_line in self._step_lines(item.get('steps', [])):
                lines.append(html.escape(step_line))
            lines.append(html.escape(SEPARATOR))
            lines.append('店铺名称：%s' % html.escape(item['shop_name'] or '-'))
            lines.append('店铺ID：%s' % html.escape(str(item['shop_id'] or '-')))
            lines.append('跟进网址：%s' % html.escape(item['detail_url'] or '-'))
            if item['success']:
                lines.append('<span class="ok">跟进记录：填写完成</span>')
            else:
                lines.append('<span class="fail">跟进记录：填写失败</span>')
            lines.append(html.escape(SEPARATOR))
        lines.append(
            '填写 %d 条记录已完成，填写成功 %d 条，失败 %d 条。'
            % (attempted, success, failed)
        )
        lines.append('</pre></body></html>')
        with open(html_path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))

        txt_path = os.path.join(
            config.TXT_DIR, 'followup_batch_%s.txt' % self.run_id
        )
        text_lines = []
        for item in self.results:
            text_lines.extend(self._step_lines(item.get('steps', [])))
            text_lines.append(SEPARATOR)
            text_lines.append('店铺名称：%s' % (item['shop_name'] or '-'))
            text_lines.append('店铺ID：%s' % (item['shop_id'] or '-'))
            text_lines.append('跟进网址：%s' % (item['detail_url'] or '-'))
            text_lines.append(
                '跟进记录：%s' % ('填写完成' if item['success'] else '填写失败')
            )
            text_lines.append(SEPARATOR)
        text_lines.append(
            '填写 %d 条记录已完成，填写成功 %d 条，失败 %d 条。'
            % (attempted, success, failed)
        )
        with open(txt_path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(text_lines) + '\n')

        self.debug('批量结果 TXT -> %s' % txt_path)
        self.debug('批量结果 JSON -> %s' % json_path)
        self.debug('批量结果 HTML -> %s' % html_path)

    def _fill_direct(self, page, shop, index):
        """并发 Worker：直接在详情 URL 上填表发布，不经过列表点击。"""
        detail_url = shop.get('detail_url') or ''
        result = {}
        success = False
        error = ''
        try:
            case = ShopFollowupCase(
                SimpleNamespace(page=page),
                self.debug,
                dry_run=self.dry_run,
                shop_id=str(shop.get('kdt_id') or shop.get('shop_id') or ''),
                detail_url=detail_url,
                run_id='%s_%02d' % (self.run_id, index),
            )
            result = case.run()
            success = bool(result.get('success'))
            error = result.get('error', '')
        except Exception as exc:
            error = repr(exc)
        self._record_result(
            shop, detail_url, success, error, result.get('steps', [])
        )
        return success

    def _worker(self, chunk):
        bot = YouzanBot(lambda message: None)
        try:
            bot._ensure_browser(storage_state=config.AUTH_FILE)
            for index, shop in chunk:
                if self.stop_event.is_set():
                    break
                ok = self._fill_direct(bot.page, shop, index)
                if not ok:
                    self.stop_event.set()
                    break
        except Exception as exc:
            self.debug('并发 Worker 异常：%r' % exc)
            self.stop_event.set()
        finally:
            try:
                bot.close()
            except Exception:
                pass

    def _run_parallel(self, targets, workers):
        workers = max(1, min(workers, len(targets)))
        self.debug('并发填写：%d 家，%d 线程' % (len(targets), workers))
        indexed = list(enumerate(targets, 1))
        chunks = [indexed[i::workers] for i in range(workers)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._worker, chunk) for chunk in chunks]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    self.debug('并发任务异常：%r' % exc)
                    self.stop_event.set()

    def run(self, count=3, skip=0, all_shops=False, workers=1, skip_done=True):
        shops = self.load_all_shops() if all_shops else self.load_first_page()
        self.raw('当前检索到店铺一共%d个' % len(shops))
        if all_shops:
            targets = shops[skip:]
        else:
            targets = shops[skip:skip + count]
        if skip_done:
            done = self.load_done_ids()
            before = len(targets)
            targets = [
                shop for shop in targets
                if str(shop.get('kdt_id') or shop.get('shop_id') or '') not in done
            ]
            skipped = before - len(targets)
            if skipped:
                self.raw('今日已完成%d个，本次待填写%d个' % (skipped, len(targets)))
            self.debug('跳过当天已成功店铺：%d 家，剩余 %d 家' % (
                skipped, len(targets)
            ))
        if not targets:
            self.debug('没有可填写的店铺')
            self.raw('填写 0 条记录已完成，填写成功 0 条，失败 0 条。')
            return {'attempted': 0, 'success': 0, 'failed': 0}

        self.stop_event.clear()
        if workers > 1:
            self._run_parallel(targets, workers)
        else:
            for index, shop in enumerate(targets, 1):
                self.debug('开始填写第 %d/%d 家：%s' % (
                    index, len(targets), shop.get('shop_name')
                ))
                ok = self._fill_one(shop, index)
                if not ok:
                    self.debug('第 %d 家失败，停止后续店铺' % index)
                    break

        attempted, success, failed = self._summary()
        self._save_report(attempted, success, failed)
        return {'attempted': attempted, 'success': success, 'failed': failed}
