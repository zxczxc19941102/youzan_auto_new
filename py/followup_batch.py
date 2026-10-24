# -*- coding: utf-8 -*-
"""批量填写第一页前 N 个店铺的跟进记录，只输出规定格式的店铺块。"""
import html
import json
import os
import random
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from pathlib import Path
from types import SimpleNamespace

import config
from shop_followup import ShopFollowupCase
from shop_list_reader import SEPARATOR, SHOP_LIST_URL, ShopListReader
from task_runtime import TaskCancelled
from youzan_bot import YouzanBot


class FollowupBatch:
    def __init__(
        self, bot, raw_log, styled_log, debug_log, dry_run=False,
        runtime=None, executor=None, publish_lock=None,
    ):
        self.bot = bot
        self.page = bot.page
        self.raw = raw_log
        self.styled = styled_log
        self.debug = debug_log
        self.dry_run = dry_run
        self.runtime = runtime
        # 并发上限由 GUI 下拉框决定；有 runtime 时复用它的受控线程池
        self.executor = executor or (runtime.executor if runtime else None)
        self.stop_event = (
            runtime.stop_event if runtime is not None else threading.Event()
        )
        self.abort_event = threading.Event()   # 批次内失败快速停止，不混同全局取消
        # Same-account writes are serialized by the server; keep reads and
        # form filling concurrent, but protect the publish critical section.
        self.publish_lock = publish_lock or threading.Lock()
        self.run_id = time.strftime('%Y%m%d_%H%M%S')
        self.reader = ShopListReader(
            bot, lambda message: None, debug=debug_log, runtime=runtime
        )
        self.results = []
        self.total_targets = 0
        self._print_lock = threading.Lock()
        self._content_logged = False

    def _halted(self):
        return self.abort_event.is_set() or self.stop_event.is_set()

    def _check(self):
        if self.stop_event.is_set():
            raise TaskCancelled()
        if self.abort_event.is_set():
            raise RuntimeError('批次已因失败停止')

    def _pace(self):
        """请求之间随机限速，避免高频空转压垮本机和对端。"""
        self._check()
        delay = random.uniform(config.RATE_LIMIT_MIN, config.RATE_LIMIT_MAX)
        if self.stop_event.wait(delay):
            raise TaskCancelled()

    def _note(self, message):
        """进度明细：有运行时走统一日志，否则用原始输出。"""
        if self.runtime is not None:
            self.runtime.emit(message)
        else:
            self.raw(message)

    def load_first_page(self):
        self._check()
        self.reader._attach()
        self.debug('打开店铺列表：%s' % SHOP_LIST_URL)
        self.page.goto(
            SHOP_LIST_URL,
            wait_until='domcontentloaded',
            timeout=config.NAV_TIMEOUT_MS,
        )
        self.reader._sleep(1.5)
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
        """读取当天已成功发布的店铺 ID；试运行和其他日期不计入。"""
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
        for path in Path(config.TXT_DIR).glob('followup_test_result_*.json'):
            if today not in path.stem:
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('dry_run') or not data.get('success'):
                    continue
                shop_id = data.get('shop_id')
                if shop_id:
                    done.add(str(shop_id))
            except Exception:
                continue
        return done

    @staticmethod
    def shop_ids(shop):
        return {
            str(shop.get(key)).strip()
            for key in ('kdt_id', 'shop_id')
            if shop.get(key) is not None and str(shop.get(key)).strip()
        }

    def today_visit_ids(self, shops):
        """从店铺列表最近拜访日期中识别今天已有记录的店铺。"""
        today = time.strftime('%Y%m%d')
        done = set()
        for shop in shops:
            last_visit = str(shop.get('last_visit') or '')
            visit_date = ''.join(re.findall(r'\d+', last_visit))[:8]
            if visit_date == today:
                done.update(self.shop_ids(shop))
        return done

    def load_all_shops(self):
        self._check()
        self.reader._attach()
        self.debug('打开店铺列表：%s' % SHOP_LIST_URL)
        self.page.goto(
            SHOP_LIST_URL,
            wait_until='domcontentloaded',
            timeout=config.NAV_TIMEOUT_MS,
        )
        self.reader._sleep(1.5)
        if not self.reader._wait_data(1):
            self.debug('店铺列表未加载出数据')
            return []
        shops = []
        page_no = 1
        while page_no <= 100:
            self._check()
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
        self._check()
        name = shop.get('shop_name') or ''
        shop_id = shop.get('kdt_id') or shop.get('shop_id') or ''
        self.debug('开始填写：%s（ID=%s）' % (name or '-', shop_id or '-'))
        detail_url, popup = self.reader.open_detail_popup(shop)
        success = False
        error = ''
        page_obj = popup

        if page_obj is None and detail_url:
            try:
                page_obj = self.page.context.new_page()
                page_obj.goto(
                    detail_url,
                    wait_until='domcontentloaded',
                    timeout=config.NAV_TIMEOUT_MS,
                )
            except TaskCancelled:
                raise
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
                    runtime=self.runtime,
                    publish_lock=self.publish_lock,
                )
                result = case.run()
                self._emit_followup_content(case.form_values)
                success = bool(result.get('success'))
                error = result.get('error', '')
            except TaskCancelled:
                self._record_result(
                    shop, detail_url, False, '已终止',
                    result.get('steps', []), cancelled=True,
                )
                raise
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

    def _record_result(self, shop, detail_url, success, error, steps,
                       cancelled=False):
        with self._print_lock:
            if not success:
                self._emit_steps(steps)
            self.results.append({
                'shop_name': shop.get('shop_name') or '',
                'shop_id': shop.get('kdt_id') or shop.get('shop_id') or '',
                'detail_url': detail_url,
                'success': success,
                'cancelled': bool(cancelled),
                'error': error,
                'steps': steps,
            })

    def _emit_followup_content(self, values):
        """首次表单填完后，在主日志用代码块展示实际跟进内容。"""
        labels = ('跟进方式', '跟进目的', '沟通情况', '客户需求/备注')
        if not all(values.get(label) for label in labels):
            return
        with self._print_lock:
            if self._content_logged:
                return
            self._content_logged = True
            lines = ['%s：%s' % (label, values[label]) for label in labels]
            fence = chr(96) * 3
            self._note('跟进内容：\n%s\n%s\n%s' % (
                fence, '\n'.join(lines), fence
            ))

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

    def _summary(self):
        attempted = len(self.results)
        success = sum(1 for item in self.results if item['success'])
        cancelled = sum(1 for item in self.results if item.get('cancelled'))
        failed = attempted - success - cancelled
        self.debug(
            '填写 %d 条记录已完成，填写成功 %d 条，失败 %d 条，终止 %d 条。'
            % (attempted, success, failed, cancelled)
        )
        return attempted, success, failed, cancelled

    def _save_report(self, attempted, success, failed, cancelled=0):
        json_path = os.path.join(
            config.TXT_DIR, 'followup_batch_%s.json' % self.run_id
        )
        with open(json_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'attempted': attempted,
                'success': success,
                'failed': failed,
                'cancelled': cancelled,
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
        self._check()
        if self.runtime is not None:
            self.debug('店铺进度 %d/%d：%s（ID=%s）' % (
                index, self.total_targets,
                shop.get('shop_name') or '-',
                shop.get('kdt_id') or shop.get('shop_id') or '-',
            ))
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
                runtime=self.runtime,
                publish_lock=self.publish_lock,
            )
            result = case.run()
            self._emit_followup_content(case.form_values)
            success = bool(result.get('success'))
            error = result.get('error', '')
        except TaskCancelled:
            self._record_result(
                shop, detail_url, False, '已终止',
                result.get('steps', []), cancelled=True,
            )
            raise
        except Exception as exc:
            error = repr(exc)
        self._record_result(
            shop, detail_url, success, error, result.get('steps', [])
        )
        return success

    def _worker(self, chunk):
        """池线程：独占一个浏览器，顺序处理分配到的店铺，停止时立刻退出。"""
        runtime = self.runtime
        started = time.perf_counter()
        label = '跟进批次[%d家]' % len(chunk)
        status = '成功'
        if runtime is not None:
            runtime.begin_task(label, emit=False)
        bot = None
        try:
            self._pace()   # 错峰启动浏览器，削掉并发启动的 CPU 尖峰
            bot = YouzanBot(
                self.debug,
                proxy_server=getattr(self.bot, 'proxy_server', None),
            )
            bot.bind_runtime(runtime)
            bot.stop_event = self.stop_event
            bot._ensure_browser(storage_state=config.AUTH_FILE)
            for index, shop in chunk:
                self._check()
                # @decision DEC-2 每个店铺用独立页面，不复用 bot.page。
                # 原因：ShopFollowupCase.run() 每次都会 page.on('response') 注册监听器，
                # Playwright 无法单独移除；复用同一页面时监听器随店铺数线性叠加，
                # 每个响应被重复解析 N 次（本次日志出现 1,1,2,2…15,15 共 240 条重复）。
                page = bot.context.new_page()
                try:
                    ok = self._fill_direct(page, shop, index)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                if not ok:
                    # 单店失败不再中断整批，继续处理后面店铺，最后按数量汇总
                    status = '失败'
                    self.debug('第 %d 家未成功，继续下一家' % index)
                self._pace()
        except TaskCancelled:
            status = '已终止'
            self.debug('并发 Worker 已终止')
            if runtime is not None:
                runtime.emit(
                    '已被终止，中断于步骤:逐店铺填写跟进记录', 'warning'
                )
        except Exception as exc:
            status = '失败'
            self.debug('并发 Worker 异常：%r' % exc)
            self.abort_event.set()
        finally:
            try:
                if bot is not None:
                    bot.close()
            except Exception:
                pass
            if runtime is not None:
                runtime.end_task(label, status, started, emit=True)

    def _run_parallel(self, targets, workers):
        """把店铺切片提交到受控线程池，池容量严格等于下拉框选定的并发数。"""
        workers = max(1, min(workers, len(targets)))
        self.debug('并发填写：%d 家，%d 线程' % (len(targets), workers))
        indexed = list(enumerate(targets, 1))
        chunks = [indexed[offset::workers] for offset in range(workers)]
        own_pool = None
        pool = self.executor
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            own_pool = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix='YouzanBatch'
            )
            pool = own_pool
        tasks: list[Future] = [pool.submit(self._worker, chunk) for chunk in chunks]
        try:
            # 阻塞等待而非轮询；工作线程靠停止事件在检查点自行退出
            wait_futures(tasks)
        finally:
            if own_pool is not None:
                own_pool.shutdown(wait=False, cancel_futures=True)
        for task in tasks:
            if task.cancelled():
                continue
            try:
                task.result()
            except TaskCancelled:
                continue
            except Exception as exc:
                self.debug('并发任务异常：%r' % exc)
                self.abort_event.set()

    def run(
        self,
        count=3,
        skip=0,
        all_shops=False,
        workers=1,
        skip_done=True,
        shops=None,
        announce_count=True,
        done_ids=None,
        executor=None,
    ):
        if executor is not None:
            self.executor = executor
        self.abort_event.clear()
        if shops is None:
            shops = self.load_all_shops() if all_shops else self.load_first_page()
        if announce_count:
            self.raw('开始处理%d家店铺' % len(shops))
        if all_shops:
            targets = shops[skip:]
        else:
            targets = shops[skip:skip + count]
        if skip_done:
            done = set(done_ids or ())
            done.update(self.load_done_ids())
            before = len(targets)
            targets = [
                shop for shop in targets
                if not (self.shop_ids(shop) & done)
            ]
            skipped = before - len(targets)
            if skipped:
                self.raw('今日已完成%d个，本次待填写%d个' % (skipped, len(targets)))
            self.debug('跳过当天已成功店铺：%d 家，剩余 %d 家' % (
                skipped, len(targets)
            ))
        if not targets:
            self.debug('没有可填写的店铺')
            self.raw('填写 0 条记录已完成，填写成功 0 条，失败 0 条，终止 0 条。')
            return {
                'attempted': 0, 'success': 0, 'failed': 0, 'cancelled': 0,
            }

        self.total_targets = len(targets)
        if workers > 1 and len(targets) > 1:
            self._run_parallel(targets, workers)
        else:
            for position, shop in enumerate(targets, 1):
                self._check()
                self._note('开始填写第 %d/%d 家店铺' % (
                    position, len(targets)
                ))
                ok = self._fill_one(shop, position)
                if not ok:
                    self.debug('第 %d 家未成功，继续下一家' % position)
                self._pace()

        attempted, success, failed, cancelled = self._summary()
        self._save_report(attempted, success, failed, cancelled)
        return {
            'attempted': attempted,
            'success': success,
            'failed': failed,
            'cancelled': cancelled,
        }
