# -*- coding: utf-8 -*-
"""有赞渠道后台：指定店铺跟进记录自动化测试用例。

流程：
  店铺列表(#/shop/list/my) -> 指定店铺详情 -> 抓取右侧链接/功能点
  -> 打开跟进记录 -> 按测试数据填表 -> 确定/发布 -> 校验并输出报告

说明：
  - 只操作当前浏览器上下文，不读取/修改本机其他浏览器的登录态。
  - 接口日志会脱敏 token/ticket/mobile/code 等字段，响应正文保存在本地 txt/ 目录。
"""
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import config
from task_runtime import TaskCancelled

SHOP_ID = '2tUkp1ko7b6G19m5AZFF9fwjh'
SHOP_LIST_URL = 'https://qudao.youzan.com/index#/shop/list/my'
SHOP_DETAIL_URL = 'https://qudao.youzan.com/index#/shop/detail/%s' % SHOP_ID

MASK_KEYWORDS = (
    'token', 'ticket', 'csrf', 'cookie', 'session',
    'mobile', 'phone', 'password',
)


def _canonical_detail_url(url):
    """Use the SPA entry document for detail routes, not the root shell."""
    value = str(url or '')
    return value.replace(
        'https://qudao.youzan.com/#/shop/detail/',
        'https://qudao.youzan.com/index#/shop/detail/',
        1,
    )


def _mask_value(key, value):
    if any(word in key.lower() for word in MASK_KEYWORDS):
        return '***'
    return value[:300]


def _sanitize_url(url):
    """只保留路径和脱敏后的查询参数，避免日志泄露票据/手机号。"""
    try:
        parsed = urlsplit(url)
        pairs = [
            (key, _mask_value(key, value))
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        query = urlencode(pairs)
        return '%s://%s%s%s' % (
            parsed.scheme, parsed.netloc, parsed.path,
            ('?' + query) if query else '',
        )
    except Exception:
        return url[:500]


def _sanitize_body(body):
    """请求体/响应体脱敏，保留结构便于核对接口。"""
    if not body:
        return ''
    try:
        payload = json.loads(body)

        def walk(obj):
            if isinstance(obj, dict):
                result = {}
                for key, value in obj.items():
                    if any(word in key.lower() for word in MASK_KEYWORDS):
                        result[key] = '***'
                    elif (
                        key.lower() in ('code', 'smscode', 'verifycode')
                        and isinstance(value, str)
                        and re.fullmatch(r'\d{6}', value)
                    ):
                        result[key] = '***'
                    else:
                        result[key] = walk(value)
                return result
            if isinstance(obj, list):
                return [walk(item) for item in obj]
            return obj

        return json.dumps(walk(payload), ensure_ascii=False)[:1600]
    except Exception:
        return body[:1600]


def _dedupe(items, *keys):
    seen = set()
    result = []
    for item in items:
        marker = tuple(item.get(key) for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


class ApiRecorder:
    """录制页面 XHR/fetch 接口，输出到日志并落盘。"""

    def __init__(self, log):
        self.log = log
        self.records = []
        self._pending = []

    def attach(self, page):
        page.on('request', self._on_request)
        page.on('response', self._on_response)

    def _on_request(self, request):
        try:
            if request.resource_type not in ('xhr', 'fetch') and request.method not in ('POST', 'PUT'):
                return
            parsed = urlsplit(request.url)
            if 'youzan.com' not in (parsed.hostname or ''):
                return
            record = {
                'method': request.method,
                'path': parsed.path,
                'url': _sanitize_url(request.url),
                'post': _sanitize_body(request.post_data or ''),
                'status': None,
                'response': '',
            }
            self.records.append(record)
            self._pending.append(record)
            self.log('[API] -> %s %s' % (record['method'], record['url']))
        except Exception:
            pass

    def _on_response(self, response):
        try:
            request = response.request
            if request.resource_type not in ('xhr', 'fetch') and request.method not in ('POST', 'PUT'):
                return
            parsed = urlsplit(response.url)
            if 'youzan.com' not in (parsed.hostname or ''):
                return
            record = None
            for item in reversed(self._pending):
                if (
                    item['status'] is None
                    and item['method'] == request.method
                    and item['path'] == parsed.path
                ):
                    record = item
                    break
            if record is None:
                record = {
                    'method': request.method,
                    'path': parsed.path,
                    'url': _sanitize_url(response.url),
                    'post': '',
                    'status': None,
                    'response': '',
                }
                self.records.append(record)
            record['status'] = response.status
            try:
                record['response'] = _sanitize_body(response.text()[:3000])
            except Exception:
                record['response'] = ''
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    if isinstance(payload.get('code'), (int, str)):
                        record['resp_code'] = str(payload.get('code'))
                    if isinstance(payload.get('success'), bool):
                        record['resp_success'] = payload.get('success')
            except Exception:
                pass
            if record in self._pending:
                self._pending.remove(record)
            self.log('[API] <- %s %s HTTP=%s' % (
                request.method, parsed.path, response.status
            ))
        except Exception:
            pass

    def summary(self):
        endpoints = {}
        for item in self.records:
            key = '%s %s' % (item['method'], item['path'])
            endpoints[key] = endpoints.get(key, 0) + 1
        return {
            'count': len(self.records),
            'endpoints': endpoints,
            'records': self.records,
        }

    def save(self, run_id):
        path = os.path.join(config.TXT_DIR, 'api_capture_%s.json' % run_id)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.summary(), handle, ensure_ascii=False, indent=2)
        return path


class ShopFollowupCase:
    """指定店铺的跟进记录发布测试用例。"""

    def __init__(
        self, bot, log, dry_run=False, shop_id=None, detail_url=None,
        run_id=None, runtime=None, publish_lock=None, publish_gate=None,
    ):
        self.bot = bot
        self.page = bot.page
        self.log = log
        self.runtime = runtime
        self.publish_lock = publish_lock
        self.publish_gate = publish_gate
        self.stop_event = (
            runtime.stop_event if runtime is not None
            else getattr(bot, 'stop_event', None)
        )
        self.dry_run = dry_run
        self.shop_id = shop_id or SHOP_ID
        self.detail_url = _canonical_detail_url(detail_url or SHOP_DETAIL_URL)
        self.run_id = run_id or time.strftime('%Y%m%d_%H%M%S')
        self.started_at = time.strftime('%Y-%m-%d %H:%M:%S')
        self.recorder = ApiRecorder(log)
        self.steps = []
        self.links = []
        self.functions = []
        self.tabs = []
        self.list_links = []
        self.list_functions = []
        self.list_dump_path = ''
        self.form_values = {}
        self.publish_response = None
        self.publish_clicked_at = None
        self.publish_response_at = None
        self.publish_interval_seconds = None
        self.server_warnings = []
        self._publish_before = 0     # 发布前的接口记录游标，用于事后补取响应
        self.viewport = 1600
        self.detail_dump_path = ''
        self.api_path = ''
        self.result = {
            'success': False,
            'dry_run': dry_run,
            'shop_id': self.shop_id,
            'shop_detail_url': self.detail_url,
        }

    # ---------- 执行框架 ----------
    def _check(self):
        """停止检查点：店铺循环、重试前、长等待前统一调用。"""
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled()

    def _sleep(self, seconds):
        """可中断等待，替代固定 wait_for_timeout，停止指令毫秒级生效。"""
        self._check()
        seconds = max(0.0, float(seconds))
        if self.stop_event is None:
            time.sleep(seconds)
            return
        if self.stop_event.wait(seconds):
            raise TaskCancelled()

    def _step_inputs(self, name):
        if '详情' in name:
            return '店铺ID=%s' % self.shop_id
        if '表单' in name:
            return '试运行=%s 店铺ID=%s' % (self.dry_run, self.shop_id)
        if '发布' in name:
            return '试运行=%s' % self.dry_run
        return '店铺ID=%s' % self.shop_id

    @staticmethod
    def _step_result(detail):
        if isinstance(detail, (str, int, float)):
            return str(detail)[:160]
        return '完成'

    def _step(self, name, func):
        """统一步骤计时：开始、成功、失败、终止都落步骤记录与日志。"""
        self._check()
        started = time.perf_counter()
        try:
            if self.runtime is not None:
                with self.runtime.step(
                    name,
                    self._step_inputs(name),
                    emit_start=False,
                    emit_success=False,
                ) as outcome:
                    detail = func()
                    outcome['value'] = self._step_result(detail)
            else:
                self.log('▶ 开始:%s，关键入参:%s' % (name, self._step_inputs(name)))
                detail = func()
        except TaskCancelled:
            elapsed = time.perf_counter() - started
            self.steps.append({
                'name': name,
                'status': 'CANCELLED',
                'seconds': round(elapsed, 2),
            })
            self.log('%s 已终止，已执行 %.2f 秒' % (name, elapsed))
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.steps.append({
                'name': name,
                'status': 'FAILED',
                'seconds': round(elapsed, 2),
                'error': repr(exc)[:500],
            })
            self.log('[STEP FAIL] %s: %r' % (name, exc))
            try:
                self._capture('fail_' + re.sub(r'\W+', '_', name))
            except TaskCancelled:
                raise
            except Exception:
                pass
            raise
        elapsed = time.perf_counter() - started
        self.steps.append({
            'name': name,
            'status': 'OK',
            'seconds': round(elapsed, 2),
            'detail': detail if isinstance(detail, (str, int, float)) else '',
        })
        if self.runtime is None:
            self.log('✔ 完成:%s，关键结果:%s，耗时 %.2f 秒' % (
                name, self._step_result(detail), elapsed
            ))
        return detail

    def run(self):
        self.recorder.attach(self.page)
        try:
            self._step('打开指定店铺详情', self.open_detail)
            self._step('抓取右侧链接与功能点', self.dump_detail)
            self._step('点击拜访商家打开表单', self.open_followup)
            self._step('填写跟进表单', self.fill_form)
            if self.dry_run:
                self._step('试运行截图', self.dry_screenshot)
            else:
                self._step('确定并发布', self.publish)
                self._step('验证发布结果', self.verify)
            self.result['success'] = True
        except Exception as exc:
            self.result['error'] = repr(exc)[:800]
            self.log('用例失败：%r' % exc)
        finally:
            self._save_report()
        return self.result

    def _capture(self, tag):
        """截图/存档：默认只留失败现场；成功路径跳过，避免整页截图拖慢单店。"""
        if not (config.SAVE_STEP_SCREENSHOTS or str(tag).startswith('fail_')):
            return
        try:
            png = os.path.join(
                config.PNG_DIR, 'followup_%s_%s.png' % (tag, self.run_id)
            )
            self.page.screenshot(path=png, full_page=True)
            self.log('已截图 -> %s' % png)
        except Exception as exc:
            self.log('截图失败：%r' % exc)
        try:
            html = os.path.join(
                config.HTML_DIR, 'followup_%s_%s.html' % (tag, self.run_id)
            )
            with open(html, 'w', encoding='utf-8') as handle:
                handle.write(self.page.content())
            self.log('已保存页面源码 -> %s' % html)
        except Exception as exc:
            self.log('保存页面源码失败：%r' % exc)

    # ---------- 步骤 1：列表 ----------
    def open_list(self):
        self._check()
        self.page.goto(
            SHOP_LIST_URL,
            wait_until='domcontentloaded',
            timeout=config.NAV_TIMEOUT_MS,
        )
        self._sleep(2.5)
        try:
            self.page.wait_for_function(
                "() => /我的店铺|店铺名称|店铺ID/.test(document.body.innerText)",
                timeout=config.ASSERT_TIMEOUT_MS,
            )
        except TaskCancelled:
            raise
        except Exception:
            pass
        self._capture('shop_list')
        self.log('店铺列表 URL=%s' % self.page.url)
        return self.page.url

    def dump_list(self):
        data = self.page.evaluate(r"""() => {
            const vis = el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden' &&
                    rect.bottom > 0 && rect.top < window.innerHeight;
            };
            const info = el => {
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100),
                    cls: String(el.className || '').slice(0, 100),
                    href: el.getAttribute('href') || '',
                    x: Math.round(rect.left),
                    y: Math.round(rect.top),
                };
            };
            const links = [...document.querySelectorAll('a[href]')].filter(vis).map(info);
            const buttons = [...document.querySelectorAll(
                'button,[role="button"],.fun-btn,[class*="operate"],[class*="action"]'
            )].filter(vis).map(info);
            const rows = [...document.querySelectorAll('tr,[class*="table-row"],[class*="list-item"]')]
                .filter(vis)
                .map(el => (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 300))
                .filter(text => text.length > 10)
                .slice(0, 50);
            return {url: location.href, links, buttons, rows};
        }""")
        self.list_links = _dedupe(data.get('links', []), 'tag', 'text', 'href', 'x', 'y')
        self.list_functions = _dedupe(data.get('buttons', []), 'tag', 'text', 'cls', 'x', 'y')
        self.list_dump_path = os.path.join(
            config.TXT_DIR, 'shop_list_dump_%s.json' % self.run_id
        )
        with open(self.list_dump_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'url': data.get('url'),
                'links': self.list_links,
                'functions': self.list_functions,
                'rows': data.get('rows', []),
            }, handle, ensure_ascii=False, indent=2)
        self.log('列表链接 %d 个，功能点 %d 个 -> %s' % (
            len(self.list_links), len(self.list_functions), self.list_dump_path
        ))
        return 'links=%d functions=%d' % (
            len(self.list_links), len(self.list_functions)
        )

    # ---------- 步骤 2：详情 ----------
    def open_detail(self):
        self._check()
        self.log('直接打开店铺详情 URL=%s' % self.detail_url)
        # 详情是 hash 路由 SPA：commit 即返回，改等页面内容出现，避免白等 45 秒
        try:
            self.page.goto(
                self.detail_url, wait_until='commit',
                timeout=config.DETAIL_NAV_TIMEOUT_MS,
            )
        except TaskCancelled:
            raise
        except Exception as exc:
            self.log('打开详情失败，%.2f 秒后重试一次：%r' % (
                config.RETRY_BASE_DELAY, exc
            ))
            self._sleep(config.RETRY_BASE_DELAY)
            self.page.goto(
                self.detail_url, wait_until='commit',
                timeout=config.DETAIL_NAV_TIMEOUT_MS,
            )
        # 固定等 3 秒改为「内容就绪即走」：多数店铺 1 秒内完成渲染
        self._sleep(0.5)
        try:
            self.page.wait_for_function(
                "() => /跟进记录|基本信息|店铺名称/.test(document.body.innerText)",
                timeout=config.ASSERT_TIMEOUT_MS,
            )
        except TaskCancelled:
            raise
        except Exception:
            self._sleep(0.8)
        self._capture('shop_detail')
        self.log('店铺详情 URL=%s' % self.page.url)
        return self.page.url

    # ---------- 步骤 3：右侧链接/功能点 ----------
    def dump_detail(self):
        data = self.page.evaluate(r"""() => {
            const vw = window.innerWidth;
            const vis = el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden' &&
                    rect.bottom > 0 && rect.top < window.innerHeight;
            };
            const info = el => {
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
                    cls: String(el.className || '').slice(0, 120),
                    href: el.getAttribute('href') || '',
                    x: Math.round(rect.left),
                    y: Math.round(rect.top),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                };
            };
            const links = [...document.querySelectorAll('a[href]')]
                .filter(vis).map(info);
            const functions = [...document.querySelectorAll(
                'button,[role="button"],.fun-tab-bar-tab,.fun-nav-item,[class*="btn"],[class*="link"]'
            )].filter(vis).map(info);
            const tabs = [...document.querySelectorAll('.fun-tab-bar-tab')]
                .filter(vis).map(el => (el.innerText || '').trim()).filter(Boolean);
            const labels = [...document.querySelectorAll('.fun-form-item-label')]
                .filter(vis).map(el => (el.innerText || '').trim()).filter(Boolean);
            return {
                url: location.href,
                title: document.title,
                viewport: vw,
                links,
                functions,
                tabs,
                labels,
            };
        }""")

        viewport = data.get('viewport') or 1600
        self.viewport = viewport
        links = _dedupe(data.get('links', []), 'tag', 'text', 'href', 'x', 'y')
        functions = _dedupe(data.get('functions', []), 'tag', 'text', 'cls', 'x', 'y')
        right_links = [item for item in links if item['x'] >= viewport * 0.5]
        right_functions = [item for item in functions if item['x'] >= viewport * 0.5]

        self.links = links
        self.functions = functions
        self.tabs = data.get('tabs', [])
        self.detail_dump_path = os.path.join(
            config.TXT_DIR, 'shop_detail_dump_%s.json' % self.run_id
        )
        with open(self.detail_dump_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'url': data.get('url'),
                'title': data.get('title'),
                'tabs': self.tabs,
                'labels': data.get('labels', []),
                'right_links': right_links,
                'right_functions': right_functions,
                'all_links': links,
                'all_functions': functions,
            }, handle, ensure_ascii=False, indent=2)

        text_path = os.path.join(
            config.TXT_DIR, 'shop_detail_dump_%s.txt' % self.run_id
        )
        lines = [
            'URL: %s' % data.get('url'),
            'TITLE: %s' % data.get('title'),
            '',
            '右侧标签页: %s' % ', '.join(self.tabs),
            '',
            '右侧链接（%d）:' % len(right_links),
        ]
        lines.extend(
            '- %s | %s' % (item['text'] or '-', item['href'])
            for item in right_links
        )
        lines.append('')
        lines.append('右侧功能点（%d）:' % len(right_functions))
        lines.extend(
            '- [%s] %s | %s' % (item['tag'], item['text'] or '-', item['cls'])
            for item in right_functions
        )
        with open(text_path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))

        self.log('右侧链接 %d 个，右侧功能点 %d 个，标签页：%s' % (
            len(right_links), len(right_functions), ', '.join(self.tabs)
        ))
        self.log('详情结构已保存 -> %s' % self.detail_dump_path)
        return 'links=%d functions=%d tabs=%d' % (
            len(links), len(functions), len(self.tabs)
        )

    # ---------- 步骤 4：跟进表单 ----------
    def _has_followup_form(self):
        return self.page.locator('.fun-form-item:visible').filter(
            has_text='\u8ddf\u8fdb\u65b9\u5f0f'
        ).count() > 0

    def _wait_for_followup_panel(self):
        """Wait for the SPA to mount the follow-up toggle or form."""
        script = r"""() => {
            const visible = el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            };
            const text = el => (el.innerText || el.textContent || '')
                .replace(/\s+/g, ' ').trim();
            const formReady = [...document.querySelectorAll('.fun-form-item')]
                .some(el => visible(el) && text(el).includes('\u8ddf\u8fdb\u65b9\u5f0f'));
            const toggleReady = [...document.querySelectorAll(
                '[class*="SidePanel_toggle"], [data-testid*="visit" i]'
            )].some(el => visible(el) && text(el).includes('\u62dc\u8bbf\u5546\u5bb6'));
            return formReady || toggleReady;
        }"""
        try:
            self.page.wait_for_function(
                script, timeout=config.FOLLOWUP_FORM_TIMEOUT_MS
            )
            return True
        except TaskCancelled:
            raise
        except Exception:
            return False

    def _click_followup_toggle(self):
        """Click the dynamic CSS-module right-side follow-up toggle."""
        script = r"""() => {
            const visible = el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden' &&
                    style.opacity !== '0';
            };
            const text = el => (el.innerText || el.textContent || '')
                .replace(/\s+/g, ' ').trim();
            const candidates = [...document.querySelectorAll(
                '[class*="SidePanel_toggle"], [data-testid*="visit" i]'
            )];
            const el = candidates.find(node =>
                visible(node) && text(node) === '\u62dc\u8bbf\u5546\u5bb6'
            ) || candidates.find(node =>
                visible(node) && text(node).includes('\u62dc\u8bbf\u5546\u5bb6')
            );
            if (!el) return null;
            el.scrollIntoView({block: 'center', inline: 'nearest'});
            el.click();
            return {
                className: String(el.className || ''),
                text: text(el),
            };
        }"""
        return self.page.evaluate(script)

    def _followup_panel_diagnostics(self):
        script = r"""() => [...document.querySelectorAll(
            '[class*="SidePanel"], [class*="fun-form"], button, [role="button"]'
        )].map(el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return {
                text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                className: String(el.className || '').slice(0, 160),
                visible: rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden',
            };
        }).filter(item => item.text).slice(-20)"""
        try:
            return self.page.evaluate(script)
        except Exception:
            return []

    def open_followup(self):
        if self._has_followup_form():
            self.log('\u8ddf\u8fdb\u8bb0\u5f55\u8868\u5355\u5df2\u5c55\u5f00\uff0c\u65e0\u9700\u91cd\u590d\u70b9\u51fb')
        else:
            ready = self._wait_for_followup_panel()
            if ready and not self._has_followup_form():
                toggle = self._click_followup_toggle()
                if toggle:
                    self.log('\u5df2\u70b9\u51fb\u300c\u62dc\u8bbf\u5546\u5bb6\u300d\uff0c\u5c55\u5f00\u53f3\u4fa7\u8868\u5355\uff08%s\uff09' % (
                        toggle.get('className') or '\u52a8\u6001\u5f00\u5173',
                    ))
                else:
                    self.log('\u68c0\u6d4b\u5230\u8ddf\u8fdb\u9762\u677f\u52a0\u8f7d\uff0c\u4f46\u672a\u627e\u5230\u53ef\u70b9\u51fb\u7684\u300c\u62dc\u8bbf\u5546\u5bb6\u300d\u5f00\u5173')
            else:
                self.log('\u7b49\u5f85\u300c\u62dc\u8bbf\u5546\u5bb6\u300d\u9762\u677f\u8d85\u65f6\uff08%dms\uff09' % (
                    config.FOLLOWUP_FORM_TIMEOUT_MS,
                ))

        if not self._has_followup_form():
            try:
                self.page.wait_for_function(
                    r"""() => {
                        const visible = el => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 &&
                                style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        return [...document.querySelectorAll('.fun-form-item')]
                            .some(el => visible(el) &&
                                (el.innerText || el.textContent || '').includes('\u8ddf\u8fdb\u65b9\u5f0f'));
                    }""",
                    timeout=config.FOLLOWUP_FORM_TIMEOUT_MS,
                )
            except TaskCancelled:
                raise
            except Exception:
                pass

        if not self._has_followup_form():
            tab = self.page.locator('.fun-tab-bar-tab').filter(has_text='\u8ddf\u8fdb\u8bb0\u5f55')
            if tab.count():
                tab.first.click(force=True)
                self._sleep(0.8)

        if not self._has_followup_form():
            details = self._followup_panel_diagnostics()
            self.log('\u8ddf\u8fdb\u8868\u5355\u4ecd\u672a\u51fa\u73b0\uff0c\u9875\u9762\u63a7\u4ef6\u8bca\u65ad\uff1a%s' % json.dumps(
                details, ensure_ascii=False, separators=(',', ':')
            )[:2000])
            raise RuntimeError(
                '\u672a\u627e\u5230\u8ddf\u8fdb\u8bb0\u5f55\u8868\u5355\uff08\u7b49\u5f85%dms\u540e\u4ecd\u65e0 .fun-form-item/\u8ddf\u8fdb\u65b9\u5f0f\uff09' %
                config.FOLLOWUP_FORM_TIMEOUT_MS
            )
        self._capture('followup_form')
        return 'form ready'

    def _form_item(self, label_text):
        items = self.page.locator('.fun-form-item')
        for index in range(items.count()):
            item = items.nth(index)
            label = item.locator('.fun-form-item-label')
            try:
                if not label.count():
                    continue
                text = label.first.inner_text().strip()
            except Exception:
                continue
            text = re.sub(r'^[\*\s]+|[\s:：]+$', '', text)
            if text == label_text:
                return item
        raise RuntimeError('未找到表单字段：%s' % label_text)

    def _pick_radio(self, label, option_text):
        item = self._form_item(label)
        target = item.locator('label').filter(has_text=option_text).first
        if not target.count():
            target = item.get_by_text(option_text, exact=True).first
        if not target.count():
            raise RuntimeError('%s 未找到选项：%s' % (label, option_text))
        # 右侧面板在自定义滚动容器内，物理点击会报 outside viewport；用 JS 触发点击
        try:
            target.evaluate("el => el.click()")
        except Exception:
            target.click(force=True)
        self._sleep(0.15)
        value = item.evaluate("""item => {
            const checked = item.querySelector('input[type="radio"]:checked');
            if (!checked) return '';
            const label = checked.closest('label');
            return label ? label.innerText.trim() : '';
        }""")
        if option_text not in value:
            raise RuntimeError('%s 当前选中值不符：%s' % (label, value))
        self.form_values[label] = value
        self.log('表单 %s = %s' % (label, value))

    def _confirm_dropdown(self):
        """下拉/日期面板若出现“确定”，先点它；表单底部通常只有取消/发布。"""
        for selector in (
            '.fun-select-popup-content', '.fun-select-options',
            '.fun-select-dropdown', '.fun-picker', '.fun-calendar', '.fun-dropdown'
        ):
            buttons = self.page.locator(selector).get_by_role(
                'button', name='确定', exact=True
            )
            if buttons.count() and buttons.first.is_visible():
                buttons.first.click()
                self._sleep(0.15)
                self.log('已点击下拉/日期面板的「确定」')
                return True
        return False

    def _pick_select(self, label, option_text=None):
        item = self._form_item(label)
        select = item.locator('.fun-select').first
        if not select.count():
            raise RuntimeError('%s 未找到下拉框' % label)
        try:
            select.evaluate("el => el.click()")
        except Exception:
            select.click(force=True)
        self._sleep(0.3)

        option = None
        selectors = (
            '.fun-select-options .fun-select-option-item:visible',
            '.fun-select-option-item:visible',
            '.fun-select-option:visible',
        )
        for _ in range(10):
            for selector in selectors:
                if option_text:
                    option = self.page.locator(selector).filter(
                        has_text=option_text
                    ).first
                else:
                    option = self.page.locator(selector).first
                if option.count():
                    break
            if option and option.count():
                break
            if option_text and not option.count():
                option = self.page.get_by_text(option_text, exact=True).last
                if option.count():
                    break
            self._sleep(0.12)
        if option is None or not option.count():
            raise RuntimeError('%s 未找到下拉选项：%s' % (label, option_text or '第一个'))
        try:
            option.evaluate("el => el.click()")
        except Exception:
            option.click(force=True)
        self._sleep(0.15)
        self._confirm_dropdown()

        value = item.evaluate("""item => {
            const el = item.querySelector('.fun-select-single-value');
            return el ? el.innerText.trim() : '';
        }""")
        if option_text and option_text not in value:
            raise RuntimeError('%s 当前选中值不符：%s' % (label, value))
        if not value:
            raise RuntimeError('%s 下拉未选中任何值' % label)
        self.form_values[label] = value
        self.log('表单 %s = %s' % (label, value))

    def _fill_textarea(self, label, text):
        item = self._form_item(label)
        textarea = item.locator('textarea').first
        if not textarea.count():
            raise RuntimeError('%s 未找到文本域' % label)
        # 用原生 setter 写值并派发 input/change，兼容 React 受控组件
        textarea.evaluate("""(el, text) => {
            el.scrollIntoView({block: 'center'});
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            ).set;
            setter.call(el, text);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", text)
        self._sleep(0.1)
        value = textarea.input_value().strip()
        if value != text:
            raise RuntimeError('%s 写入失败：%s' % (label, value))
        self.form_values[label] = value
        self.log('表单 %s = %s' % (label, value))

    def fill_form(self):
        # 联系人/跟进日期使用页面默认值（联系人已自动带出，日期默认当天）
        contact = self._form_item('联系人')
        contact_value = contact.locator('.fun-select-single-value')
        if contact_value.count():
            contact_text = contact_value.first.inner_text().strip()
        else:
            contact_text = contact.inner_text().strip().replace('\n', ' ')
        self.form_values['联系人'] = contact_text[:120]
        self.log('表单 联系人 = %s' % self.form_values['联系人'])

        date_input = self._form_item('跟进日期').locator('input').first
        date_value = ''
        for _ in range(20):
            date_value = date_input.input_value().strip()
            if date_value:
                break
            self._sleep(0.15)
        if not date_value:
            raise RuntimeError('跟进日期为空，页面可能未加载完成')
        self.form_values['跟进日期'] = date_value
        self.log('表单 跟进日期 = %s' % date_value)

        self._pick_radio('跟进方式', '电话拜访')
        self._pick_radio('跟进目的', '了解需求')
        self._pick_select('沟通情况')  # 选第一个选项
        self._fill_textarea('\u5ba2\u6237\u9700\u6c42/\u5907\u6ce8', '\u6709\u9700\u8981\u4f1a\u8054\u7cfb')

        if not self._confirm_dropdown():
            self.log('未发现独立「确定」按钮（下拉选项已即时生效）')

        self._capture('followup_filled')
        return json.dumps(self.form_values, ensure_ascii=False)

    def dry_screenshot(self):
        self._capture('dry_run')
        self.log('试运行模式：已填写表单但未点击发布')
        return 'dry-run'

    # ---------- 步骤 5：发布 ----------
    def _visible_button(self, name):
        buttons = self.page.get_by_role('button', name=name, exact=True)
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if button.is_visible():
                    return button
            except Exception:
                continue
        return None

    def _response_ok(self, record):
        """发布是否成功：兼容 code/success/msg/visitRecordId 多种成功标识。"""
        if not record or record.get('status') != 200:
            return False
        if record.get('resp_success') is True:
            return True
        if str(record.get('resp_code')) in ('0', '200', '20000'):
            return True
        body = record.get('response') or ''
        try:
            payload = json.loads(body)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get('success') is True:
            return True
        if str(payload.get('code')) in ('0', '200', '20000'):
            return True
        data = payload.get('data')
        if isinstance(data, dict) and data.get('visitRecordId'):
            return True
        return '操作成功' in str(payload.get('msg') or '')

    def _log_publish_detail(self, record):
        """保留发布接口返回的原始业务错误，不改写服务端说明。"""
        body = (record or {}).get('response') or ''
        try:
            payload = json.loads(body)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get('data')
        if isinstance(data, dict) and data.get('visitRecordId'):
            self.log('拜访记录已生成：visitRecordId=%s' % data['visitRecordId'])

        def record_error(code, message):
            if code in (None, '') and message in (None, ''):
                return
            code_text = str(code) if code not in (None, '') else ''
            message_text = str(message) if message not in (None, '') else ''
            if code_text in ('0', '200', '20000') and not message_text:
                return
            item = {'code': code_text, 'message': message_text}
            if item in self.server_warnings:
                return
            self.server_warnings.append(item)
            # Keep the values from the response verbatim; this prefix only
            # identifies the two raw fields and is not a replacement message.
            self.log('原始错误码=%s 原始错误说明=%s' % (
                code_text, message_text,
            ))

        response_status = (record or {}).get('status')
        response_code = payload.get('code')
        response_message = payload.get('msg') or payload.get('message')
        if response_status != 200:
            record_error(
                response_code if response_code not in (None, '')
                else (record or {}).get('resp_code') or response_status,
                response_message,
            )
        elif str(response_code) not in ('0', '200', '20000') and \
                response_code not in (None, ''):
            record_error(response_code, response_message)

        warnings = data.get('visitReturnCodes') if isinstance(data, dict) else None
        for item in warnings or []:
            if not isinstance(item, dict):
                continue
            code = item.get('code')
            message = item.get('message')
            if str(code) not in ('0', '200', '20000') or (
                    message not in (None, '', '\u6210\u529f\u6dfb\u52a0\u62dc\u8bbf')):
                record_error(code, message)

    def publish(self):
        if self.publish_lock is None:
            return self._publish_unlocked()
        self.log('waiting for same-account publish lock')
        with self.publish_lock:
            self.log('acquired same-account publish lock')
            return self._publish_unlocked()

    def _publish_unlocked(self):
        button = self._visible_button('发布')
        if button is None:
            fallback = self.page.locator('button').filter(has_text='发布')
            for index in range(fallback.count()):
                try:
                    if fallback.nth(index).is_visible():
                        button = fallback.nth(index)
                        break
                except Exception:
                    continue
        if button is None:
            raise RuntimeError('未找到可见的「发布」按钮')

        self._publish_before = len(self.recorder.records)
        started = time.perf_counter()
        if self.publish_gate is not None:
            self.publish_interval_seconds = self.publish_gate.wait_before_click(
                self.shop_id
            )
        self.publish_clicked_at = time.time()
        millis = int((self.publish_clicked_at % 1) * 1000)
        self.log('\u53d1\u5e03\u70b9\u51fb\uff1a\u5e97\u94faID=%s \u65f6\u95f4\u6233=%s.%03d \u4e24\u6b21\u63d0\u4ea4\u95f4\u9694=%s\u79d2' % (
            self.shop_id,
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.publish_clicked_at)),
            millis,
            '-' if self.publish_interval_seconds is None
            else '%.3f' % self.publish_interval_seconds,
        ))
        # @decision DEC-1 用 expect_response 事件驱动等待，替代固定 sleep 轮询。
        # 原因：Playwright 同步 API 的事件循环只在 Playwright 调用期间运行（greenlet 调度），
        # 纯 Event.wait 等待期间 page.on('response') 不会触发，响应永远采集不到。
        # 依据：tests/test_publish_wait.py 与 playwright/_impl/_sync_base.py:wait_for_future。
        try:
            with self.page.expect_response(
                self._is_publish_response,
                timeout=config.PUBLISH_TIMEOUT_MS,
            ) as info:
                try:
                    button.evaluate("el => el.click()")
                except Exception:
                    button.click(force=True)
                # 发布后若再弹“确定”确认框，自动点掉；没有则直接等待接口
                self._sleep(0.4)
                confirm = self._visible_button('确定')
                if confirm is not None:
                    try:
                        confirm.evaluate("el => el.click()")
                    except Exception:
                        confirm.click(force=True)
                    self.log('已点击发布确认「确定」')
                    self._sleep(0.3)
            self.publish_response = self._record_from_response(info.value)
            self.publish_response_at = time.time()
            self.log('发布接口响应耗时 %.2f 秒' % (time.perf_counter() - started))
        except PlaywrightTimeoutError:
            # 超时后事件已由 expect_response 驱动过一轮，先看看是否只是晚到
            self.publish_response = self._latest_publish_record(self._publish_before)
            if self.publish_response is None:
                self.publish_response = self._wait_for_publish_record(
                    config.PUBLISH_RESPONSE_GRACE_MS
                )
            if self.publish_response is None:
                self.log('等待发布接口响应超时（%.1f 秒），期间未收到 /visit/save 响应' % (
                    config.PUBLISH_TIMEOUT_MS / 1000.0
                ))
            else:
                self.log('发布接口响应晚于预期，已从事件记录中补获')
        self._capture('after_publish')
        if self.publish_response:
            self.log('发布接口：%s %s HTTP=%s code=%s' % (
                self.publish_response['method'],
                self.publish_response['path'],
                self.publish_response['status'],
                self.publish_response.get('resp_code', '-'),
            ))
            self._log_publish_detail(self.publish_response)
        else:
            self.log('未捕获到发布接口响应（页面可能未提交或网络超时）')
        return 'publish clicked'

    def _wait_for_publish_record(self, timeout_ms):
        """Pump Playwright events for an already-submitted publish request."""
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            self._check()
            record = self._latest_publish_record(self._publish_before)
            if record is not None:
                return record
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                self.page.wait_for_timeout(min(100, remaining))
            except TaskCancelled:
                raise
            except Exception:
                self._sleep(min(0.1, remaining / 1000.0))
        return self._latest_publish_record(self._publish_before)

    @staticmethod
    def _is_publish_response(response):
        """发布接口判定：POST/PUT 且路径命中 visit/save（兼容同类保存接口）。"""
        try:
            if response.request.method not in ('POST', 'PUT'):
                return False
            path = urlsplit(response.url).path.lower()
            return '/visit/save' in path or ('visit' in path and 'save' in path)
        except Exception:
            return False

    def _record_from_response(self, response):
        """把 expect_response 拿到的响应转成与 ApiRecorder 相同结构的记录。"""
        record = {
            'method': response.request.method,
            'path': '',
            'url': '',
            'post': '',
            'status': None,
            'response': '',
        }
        try:
            url = response.url
            record['url'] = _sanitize_url(url)
            record['path'] = urlsplit(url).path
            record['status'] = response.status
        except Exception:
            return record
        try:
            record['response'] = _sanitize_body(response.text()[:3000])
        except Exception:
            record['response'] = ''
        try:
            payload = json.loads(record['response'])
            if isinstance(payload, dict):
                if isinstance(payload.get('code'), (int, str)):
                    record['resp_code'] = str(payload.get('code'))
                if isinstance(payload.get('success'), bool):
                    record['resp_success'] = payload.get('success')
        except Exception:
            pass
        return record

    def _latest_publish_record(self, start=0):
        """从事件记录中补取最近的发布响应（用于迟到的响应）。"""
        for item in reversed(self.recorder.records[start:]):
            if item.get('method') not in ('POST', 'PUT') or not item.get('status'):
                continue
            path = (item.get('path') or '').lower()
            if '/visit/save' in path or ('visit' in path and 'save' in path):
                return item
        return None

    @staticmethod
    def _response_msg(record):
        """提取接口返回的业务提示，不截断原始服务端说明。"""
        try:
            payload = json.loads((record or {}).get('response') or '')
            message = payload.get('msg') or payload.get('message') or ''
            return str(message)
        except Exception:
            return ''

    def verify(self):
        """页面提示或发布接口任一确认成功；最多轮询 8 秒，避免 toast 闪退误判。"""
        # 发布等待超时时，先做一次最小 Playwright 调用把积压事件派发出来，
        # 再从事件记录里补取真实响应，避免把已经写入成功的记录误判为失败。
        if self.publish_response is None:
            self.publish_response = self._wait_for_publish_record(
                config.PUBLISH_RESPONSE_GRACE_MS
            )
        api_ok = self._response_ok(self.publish_response)
        text_ok = False
        deadline = time.monotonic() + 8
        while True:
            self._check()
            if not api_ok:
                # inner_text 会驱动事件循环；下一轮再补取，可捕获迟到的响应
                self.publish_response = (
                    self.publish_response
                    or self._latest_publish_record(self._publish_before)
                )
                api_ok = self._response_ok(self.publish_response)
            try:
                body_text = self.page.inner_text('body')[:3000]
            except TaskCancelled:
                raise
            except Exception:
                body_text = ''
            text_ok = any(word in body_text for word in (
                '发布成功', '保存成功', '提交成功', '跟进记录发布成功'
            ))
            if text_ok or api_ok or time.monotonic() >= deadline:
                break
            self._sleep(0.25)
        if not (text_ok or api_ok):
            if self.publish_response:
                # When the server answered, preserve its raw code/message as
                # the failure payload. Do not replace it with a local guess.
                raw_code = self.publish_response.get('resp_code')
                raw_message = self._response_msg(self.publish_response)
                raise RuntimeError(json.dumps({
                    'code': raw_code,
                    'message': raw_message,
                }, ensure_ascii=False))
            raise RuntimeError('\u672a\u6355\u83b7\u5230\u53d1\u5e03\u63a5\u53e3\u54cd\u5e94\uff0c\u9875\u9762\u65e0\u6210\u529f\u63d0\u793a')
        self.result['publish_success'] = True
        self.log('发布成功：%s' % ('接口返回成功' if api_ok else '页面提示'))
        return 'text=%s api=%s' % (text_ok, api_ok)

    # ---------- 报告 ----------
    def _save_report(self):
        try:
            config.ensure_dirs()
            self.api_path = self.recorder.save(self.run_id)
            api_summary = self.recorder.summary()
            right_links = [
                item for item in self.links
                if item['x'] >= (self.viewport * 0.5)
            ]
            self.result.update({
                'started_at': self.started_at,
                'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'steps': self.steps,
                'tabs': self.tabs,
                'links_total': len(self.links),
                'links_right_side': len(right_links),
                'functions_total': len(self.functions),
                'form_values': self.form_values,
                'publish_response': self.publish_response,
                'publish_clicked_at': self.publish_clicked_at,
                'publish_response_at': self.publish_response_at,
                'publish_interval_seconds': self.publish_interval_seconds,
                'server_warnings': self.server_warnings,
                'api_count': api_summary['count'],
                'api_endpoints': api_summary['endpoints'],
                'detail_dump': self.detail_dump_path,
                'api_capture': self.api_path,
            })
            json_path = os.path.join(
                config.TXT_DIR, 'followup_test_result_%s.json' % self.run_id
            )
            with open(json_path, 'w', encoding='utf-8') as handle:
                json.dump(self.result, handle, ensure_ascii=False, indent=2)

            text_path = os.path.join(
                config.TXT_DIR, 'followup_test_result_%s.txt' % self.run_id
            )
            lines = [
                '有赞渠道后台 - 指定店铺跟进记录测试用例',
                '运行时间: %s ~ %s' % (
                    self.result.get('started_at'), self.result.get('finished_at')
                ),
                '店铺ID: %s' % self.shop_id,
                '店铺详情: %s' % self.detail_url,
                '试运行: %s' % self.dry_run,
                '结果: %s' % ('SUCCESS' if self.result.get('success') else 'FAILED'),
                '',
                '步骤:',
            ]
            for step in self.steps:
                line = '- [%s] %s (%.2fs)' % (
                    step['status'], step['name'], step['seconds']
                )
                if step.get('error'):
                    line += ' | ' + step['error']
                lines.append(line)
            lines.extend([
                '',
                '右侧标签页: %s' % ', '.join(self.tabs),
                '链接总数: %d，右侧链接: %d' % (
                    self.result.get('links_total', 0),
                    self.result.get('links_right_side', 0),
                ),
                '功能点总数: %d' % self.result.get('functions_total', 0),
                '接口总数: %d' % self.result.get('api_count', 0),
                '接口清单:',
            ])
            for endpoint, count in sorted(
                (self.result.get('api_endpoints') or {}).items()
            ):
                lines.append('- %s x%d' % (endpoint, count))
            lines.extend([
                '',
                '表单值:',
                json.dumps(self.form_values, ensure_ascii=False, indent=2),
                '',
                '详情结构: %s' % self.detail_dump_path,
                '接口明细: %s' % self.api_path,
                '结果 JSON: %s' % json_path,
            ])
            if self.result.get('error'):
                lines.extend(['', '错误: %s' % self.result['error']])
            with open(text_path, 'w', encoding='utf-8') as handle:
                handle.write('\n'.join(lines))

            self.log('测试结果 -> %s' % text_path)
            self.log('接口明细 -> %s' % self.api_path)
        except Exception as exc:
            self.log('保存测试报告失败：%r' % exc)
