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

import config

SHOP_ID = '2tUkp1ko7b6G19m5AZFF9fwjh'
SHOP_LIST_URL = 'https://qudao.youzan.com/#/shop/list/my'
SHOP_DETAIL_URL = 'https://qudao.youzan.com/#/shop/detail/%s' % SHOP_ID

MASK_KEYWORDS = (
    'token', 'ticket', 'csrf', 'cookie', 'session',
    'mobile', 'phone', 'password',
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

    def __init__(self, bot, log, dry_run=False, shop_id=None, detail_url=None, run_id=None):
        self.bot = bot
        self.page = bot.page
        self.log = log
        self.dry_run = dry_run
        self.shop_id = shop_id or SHOP_ID
        self.detail_url = detail_url or SHOP_DETAIL_URL
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
    def _step(self, name, func):
        self.log('== %s ==' % name)
        started = time.monotonic()
        try:
            detail = func()
        except Exception as exc:
            self.steps.append({
                'name': name,
                'status': 'FAILED',
                'seconds': round(time.monotonic() - started, 2),
                'error': repr(exc)[:500],
            })
            self.log('[STEP FAIL] %s: %r' % (name, exc))
            self._capture('fail_' + re.sub(r'\W+', '_', name))
            raise
        self.steps.append({
            'name': name,
            'status': 'OK',
            'seconds': round(time.monotonic() - started, 2),
            'detail': detail if isinstance(detail, (str, int, float)) else '',
        })
        self.log('[STEP OK] %s (%.1fs)' % (name, time.monotonic() - started))
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
        self.page.goto(SHOP_LIST_URL, wait_until='domcontentloaded')
        self.page.wait_for_timeout(2500)
        try:
            self.page.wait_for_function(
                "() => /我的店铺|店铺名称|店铺ID/.test(document.body.innerText)",
                timeout=15000,
            )
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
        self.log('直接打开店铺详情 URL=%s' % self.detail_url)
        try:
            self.page.goto(
                self.detail_url, wait_until='domcontentloaded', timeout=60000
            )
        except Exception as exc:
            self.log('打开详情超时，重试一次：%r' % exc)
            self.page.goto(
                self.detail_url, wait_until='domcontentloaded', timeout=60000
            )
        self.page.wait_for_timeout(3000)
        try:
            self.page.wait_for_function(
                "() => /跟进记录|基本信息|店铺名称/.test(document.body.innerText)",
                timeout=15000,
            )
        except Exception:
            pass
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
            has_text='跟进方式'
        ).count() > 0

    def open_followup(self):
        # 右侧“拜访商家”面板默认可能是收起状态（SidePanel_hide），先展开
        toggle = self.page.locator('[class*="SidePanel_toggle"]').filter(
            has_text='拜访商家'
        ).first
        for _ in range(20):
            if toggle.count():
                break
            self.page.wait_for_timeout(500)
        if not toggle.count():
            toggle = self.page.locator('[class*="SidePanel_toggle"]').first
        if toggle.count():
            classes = toggle.get_attribute('class') or ''
            self.log('拜访商家面板开关状态：%s' % classes)
            if 'hide' in classes.lower():
                toggle.evaluate("el => el.click()")
                self.log('已点击「拜访商家」，展开右侧表单')
                self.page.wait_for_timeout(1200)
            else:
                self.log('「拜访商家」面板已展开，无需重复点击')
        else:
            self.log('未找到「拜访商家」开关，尝试使用当前表单')

        for _ in range(40):
            item = self.page.locator('.fun-form-item:visible').filter(
                has_text='跟进方式'
            ).first
            if item.count():
                box = item.bounding_box()
                if box:
                    self.log('表单位置 x=%d y=%d' % (box['x'], box['y']))
                    break
            self.page.wait_for_timeout(500)

        if not self._has_followup_form():
            tab = self.page.locator('.fun-tab-bar-tab').filter(has_text='跟进记录')
            if tab.count():
                tab.first.evaluate("el => el.click()")
                self.page.wait_for_timeout(1500)
        if not self._has_followup_form():
            raise RuntimeError('未找到跟进记录表单（.fun-form-item）')
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
        self.page.wait_for_timeout(300)
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
                self.page.wait_for_timeout(250)
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
        self.page.wait_for_timeout(500)

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
            self.page.wait_for_timeout(200)
        if option is None or not option.count():
            raise RuntimeError('%s 未找到下拉选项：%s' % (label, option_text or '第一个'))
        try:
            option.evaluate("el => el.click()")
        except Exception:
            option.click(force=True)
        self.page.wait_for_timeout(300)
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
        self.page.wait_for_timeout(200)
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
            self.page.wait_for_timeout(250)
        if not date_value:
            raise RuntimeError('跟进日期为空，页面可能未加载完成')
        self.form_values['跟进日期'] = date_value
        self.log('表单 跟进日期 = %s' % date_value)

        self._pick_radio('跟进方式', '电话拜访')
        self._pick_radio('跟进目的', '了解需求')
        self._pick_select('沟通情况')  # 选第一个选项
        self._fill_textarea('客户需求/备注', '有需要联系我')

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
        if not record or record.get('status') != 200:
            return False
        if record.get('resp_success') is True:
            return True
        if str(record.get('resp_code')) in ('0', '200', '20000'):
            return True
        body = record.get('response') or ''
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                if payload.get('success') is True:
                    return True
                if str(payload.get('code')) in ('0', '200', '20000'):
                    return True
        except Exception:
            pass
        return False

    def publish(self):
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

        before = len(self.recorder.records)
        try:
            button.evaluate("el => el.click()")
        except Exception:
            button.click(force=True)
        # 发布后若再弹“确定”确认框，自动点掉；没有则直接等待接口
        self.page.wait_for_timeout(800)
        confirm = self._visible_button('确定')
        if confirm is not None:
            try:
                confirm.evaluate("el => el.click()")
            except Exception:
                confirm.click(force=True)
            self.log('已点击发布确认「确定」')
            self.page.wait_for_timeout(500)
        self.page.wait_for_timeout(4000)
        new_records = self.recorder.records[before:]
        posts = [
            item for item in new_records
            if item['method'] in ('POST', 'PUT') and item.get('status')
        ]
        if not posts:
            posts = [
                item for item in self.recorder.records
                if item['method'] in ('POST', 'PUT') and item.get('status')
                and any(word in item['path'].lower() for word in (
                    'follow', 'record', 'visit', 'save', 'add', 'publish'
                ))
            ]
        self.publish_response = posts[-1] if posts else None
        self._capture('after_publish')
        if self.publish_response:
            self.log('发布接口：%s %s HTTP=%s' % (
                self.publish_response['method'],
                self.publish_response['path'],
                self.publish_response['status'],
            ))
        else:
            self.log('未捕获到发布接口响应')
        return 'publish clicked'

    def verify(self):
        body_text = self.page.inner_text('body')[:3000]
        text_ok = any(word in body_text for word in (
            '发布成功', '保存成功', '提交成功', '跟进记录发布成功'
        ))
        api_ok = self._response_ok(self.publish_response)
        if not (text_ok or api_ok):
            raise RuntimeError('未检测到发布成功信号（页面/接口均未确认）')
        self.result['publish_success'] = True
        self.log('发布成功：%s' % ('页面提示' if text_ok else '接口返回成功'))
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
