# -*- coding: utf-8 -*-
"""
YouzanBot：封装 playwright 登录全流程（登录 -> 自动滑块 -> 短信 -> 抓CK/复用）。
浏览器在后台 headless 运行，不弹窗口。线程安全：由 GUI 在工作线程里调用本类方法。
"""
import contextlib
import os
import re
import io
import json
import time
import base64
from urllib.parse import urlsplit
from PIL import Image
from playwright.sync_api import sync_playwright

import config
from captcha import loc_gap_offload, human_drag, JS_GET_IMAGES
from task_runtime import TaskCancelled


def _mask_account(account):
    """账号脱敏：保留前 3 位后 4 位，短账号只留首位。"""
    account = str(account or '')
    if len(account) <= 7:
        return account[:1] + '***'
    return account[:3] + '****' + account[-4:]


QUIET_RUNTIME_STEPS = frozenset({
    '初始化会话', '打开登录页', '输入账号密码', '登录态校验',
    '滑块验证码获取', '滑块识别', '滑块拖动', '滑块结果校验',
    '请求短信验证码', '提交验证码', '抓取 CK',
})


class YouzanBot:
    def __init__(self, log, proxy_server=None):
        self.log = log
        self.proxy_server = (config.DEFAULT_PROXY_SERVER if proxy_server is None else str(proxy_server).strip())
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.needs_sms = False
        self.auth_requests = []
        self.auth_responses = []
        self._login_secrets = []
        self.runtime = None
        self.stop_event = None

    # ---------- 运行时协作：停止检查 / 可中断等待 / 统一计时 ----------
    def bind_runtime(self, runtime):
        """绑定任务运行时：共享停止事件与统一日志、计时。"""
        self.runtime = runtime
        self.stop_event = runtime.stop_event if runtime is not None else None

    def progress(self, message):
        if self.runtime is not None:
            self.runtime.emit(message)
        else:
            self.log(message)

    def check_stop(self):
        """停止检查点：一旦置位立刻抛出取消信号，终止后续步骤。"""
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled()

    def sleep(self, seconds):
        """把长 sleep 拆成 Event.wait，停止指令毫秒级响应。"""
        self.check_stop()
        seconds = max(0.0, float(seconds))
        if self.stop_event is None:
            time.sleep(seconds)
            return
        if self.stop_event.wait(seconds):
            raise TaskCancelled()

    @contextlib.contextmanager
    def step(self, name, inputs='无'):
        """步骤计时：绑定 runtime 时输出统一格式，否则退化为本地日志。"""
        if self.runtime is not None:
            with self.runtime.step(
                name, inputs,
                emit_start=name not in QUIET_RUNTIME_STEPS,
                emit_success=name not in QUIET_RUNTIME_STEPS,
            ) as outcome:
                self.check_stop()
                yield outcome
            return
        started = time.perf_counter()
        self.log('▶ 开始:%s，关键入参:%s' % (name, inputs))
        outcome = {'value': '完成'}
        try:
            yield outcome
        except TaskCancelled:
            self.log('%s 已终止，已执行 %.2f 秒' % (
                name, time.perf_counter() - started
            ))
            raise
        except Exception as exc:
            self.log('%s 失败，耗时 %.2f 秒，原因:%r' % (
                name, time.perf_counter() - started, exc
            ))
            raise
        else:
            self.log('✔ 完成:%s，关键结果:%s，耗时 %.2f 秒' % (
                name, outcome['value'], time.perf_counter() - started
            ))

    def retry(self, action, label='操作', attempts=None):
        """有界重试 + 指数退避；停止时立即放弃，不无限重连。"""
        attempts = attempts or config.RETRY_ATTEMPTS
        last_error = None
        for attempt in range(1, attempts + 1):
            self.check_stop()
            try:
                return action()
            except TaskCancelled:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = config.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                self.log('%s 第 %d 次失败，%.2f 秒后重试：%r' % (
                    label, attempt, delay, exc
                ))
                self.sleep(delay)
        raise last_error

    def _record_auth_request(self, request):
        """只记录认证 API 的方法和路径，不记录敏感查询参数或请求体。"""
        try:
            parsed = urlsplit(request.url)
            if (parsed.scheme != 'https' or parsed.hostname not in
                    ('account.youzan.com', 'passport.youzan.com')):
                return
            if not (parsed.path.startswith('/api/') or parsed.path.endswith('.json')):
                return
            self.auth_requests.append({'method': request.method, 'path': parsed.path})
        except Exception:
            pass

    def _log_auth_requests(self, start, label):
        requests = self.auth_requests[start:]
        if not requests:
            self.log('%s：未捕获到认证接口请求' % label)
            return requests
        for item in requests:
            self.log('%s：请求 %s %s' % (label, item['method'], item['path']))
        return requests

    def _log_auth_diagnostics(self, request_start, response_start, label):
        self._log_auth_requests(request_start, label)
        return self._log_auth_responses(response_start, label)

    def _record_auth_response(self, response):
        """只记录认证接口的状态码与业务码，不保存查询参数或响应数据。"""
        try:
            parsed = urlsplit(response.url)
            if (parsed.scheme != 'https' or parsed.hostname not in
                    ('account.youzan.com', 'passport.youzan.com')):
                return
            if not (parsed.path.startswith('/api/') or parsed.path.endswith('.json')):
                return

            entry = {
                'method': response.request.method,
                'path': parsed.path,
                'status': response.status,
            }
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    code = payload.get('code')
                    if isinstance(code, (int, str)) and len(str(code)) <= 24:
                        entry['code'] = str(code)
                    success = payload.get('success')
                    if isinstance(success, bool):
                        entry['success'] = success
                    for key in ('msg', 'message', 'errorMsg', 'error_message', 'error'):
                        message = payload.get(key)
                        if isinstance(message, str) and message.strip():
                            message = re.sub(r'\b(?:\+?86)?1[3-9]\d{9}\b', '[phone]', message)
                            message = re.sub(r'\b\d{6,}\b', '[redacted]', message)
                            for secret in self._login_secrets:
                                if secret:
                                    message = message.replace(secret, '[redacted]')
                            message = re.sub(r'(?i)(password|passwd|token|cookie|authorization)\s*[:=?]\s*[^\s,;]+', r'\1=[redacted]', message)
                            entry['message'] = re.sub(r'\s+', ' ', message).strip()[:160]
                            break
            except Exception:
                pass
            self.auth_responses.append(entry)
            # Every auth API response is logged as it arrives, including HTTP/business status.
            details = '%s %s HTTP=%s' % (entry['method'], entry['path'], entry['status'])
            if 'code' in entry:
                details += ' code=%s' % entry['code']
            if 'success' in entry:
                details += ' success=%s' % entry['success']
            if 'message' in entry:
                details += ' message=%s' % entry['message']
            self.log('\u670d\u52a1\u7aef\u54cd\u5e94\uff1a%s' % details)
        except Exception:
            pass

    def _visible_login_error(self):
        try:
            values = self.page.evaluate("""() => {
                const selectors = ['[role=alert]', '.el-message', '.ant-message-notice-content', '.login-error', '.error-message', '.form-error'];
                const seen = new Set();
                return selectors.flatMap(s => [...document.querySelectorAll(s)])
                    .filter(el => { const r=el.getBoundingClientRect(); const t=(el.innerText||el.textContent||'').trim(); if(!t||!r.width||!r.height||seen.has(t)) return false; seen.add(t); return true; })
                    .map(el => (el.innerText||el.textContent||'').trim()).slice(0,4);
            }""")
            result=[]
            for value in values or []:
                value=re.sub(r'\b(?:\+?86)?1[3-9]\d{9}\b','[phone]',str(value))
                value=re.sub(r'\b\d{6,}\b','[redacted]',value)
                for secret in self._login_secrets:
                    if secret:
                        value=value.replace(secret,'[redacted]')
                value=re.sub(r'(?i)(password|passwd|token|cookie|authorization)\s*[:=?]\s*[^\s,;]+',r'\1=[redacted]',value)
                value=re.sub(r'\s+',' ',value).strip()[:160]
                if value: result.append(value)
            return result
        except Exception:
            return []

    def _log_visible_login_error(self, label):
        messages=self._visible_login_error()
        if messages:
            for message in messages:
                self.log('%s\uff1a\u9875\u9762\u63d0\u793a=%s' % (label, message))
        else:
            self.log('%s\uff1a\u9875\u9762\u672a\u53d1\u73b0\u53ef\u89c1\u9519\u8bef\u63d0\u793a' % label)

    def _log_auth_responses(self, start, label):
        responses = self.auth_responses[start:]
        if not responses:
            self.log('%s\uff1a\u9875\u9762\u672a\u53d1\u73b0\u53ef\u89c1\u9519\u8bef\u63d0\u793a' % label)
        return responses

    def _is_logged_in(self):
        parsed = urlsplit(self.page.url)
        route = (parsed.path.rstrip('/') + '/' + parsed.fragment.lstrip('/'))
        route = route.split('?', 1)[0].lower()
        on_login_route = re.search(r'(?:^|/)(?:auth/)?login(?:/|$)', route) is not None
        if not (
            parsed.scheme == 'https'
            and parsed.hostname == 'qudao.youzan.com'
            and not on_login_route
        ):
            return False
        # 路由虽是后台，但可能只是“账号已在其他PC登录”的遮罩弹窗，不能算成功
        if self._has_session_conflict():
            return False
        return True

    def _has_session_conflict(self):
        """检测「您的账号已在其他PC登录，请重新登录」的可见弹窗。"""
        try:
            return bool(self.page.evaluate(r"""() => {
                const text = document.body ? document.body.innerText : '';
                if (!/已在其他\s*(?:PC|电脑|设备)\s*登录|账号已在其他/.test(text)) return false;
                return [...document.querySelectorAll('button')].some(btn => {
                    const rect = btn.getBoundingClientRect();
                    const style = getComputedStyle(btn);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden' &&
                        /重新登录/.test(btn.textContent || '');
                });
            }"""))
        except Exception:
            return False

    def _click_relogin(self):
        """点击会话冲突弹窗的「重新登录」，让旧会话失效并进入登录页。"""
        try:
            buttons = self.page.locator('button:visible').filter(has_text='重新登录')
            if buttons.count() == 0:
                return False
            buttons.first.click(timeout=5000, no_wait_after=True)
            self.log('已点击「重新登录」，准备结束旧会话并重新登录')
            self.sleep(2)
            return True
        except Exception as exc:
            if self._is_navigation_transition_error(exc):
                self.log('点击「重新登录」时页面正在跳转')
                self.sleep(2)
                return True
            self.log('点击「重新登录」失败：%r' % exc)
            return False

    def _is_sms_challenge(self):
        try:
            return bool(self.page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                const input = document.querySelector('.account-captcha__input input');
                return /\u9a8c\u8bc1\u624b\u673a|\u77ed\u4fe1\u9a8c\u8bc1|\u8f93\u5165\u9a8c\u8bc1/.test(text) && !!input;
            }"""))
        except Exception:
            return False

    def confirm_workbench(self, timeout=12):
        if not self._is_logged_in():
            return False
        return self._wait_workbench_ready(timeout=timeout)

    def _wait_workbench_ready(self, timeout=12):
        """等待工作台导航选中或仪表盘文案出现；遇到登录页/会话冲突立即返回。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_stop()
            try:
                parsed = urlsplit(self.page.url)
                if parsed.hostname == 'account.youzan.com':
                    return False
                if self._has_session_conflict():
                    return False
                ready = self.page.evaluate("""() => {
                    const nav = document.querySelector('[data-key="/workbench"]');
                    const selected = !!(nav && /selected/.test(nav.className || ''));
                    const text = document.body ? document.body.innerText : '';
                    return selected || /数据仪表盘|本月完成金额/.test(text);
                }""")
                if ready:
                    return True
            except TaskCancelled:
                raise
            except Exception:
                pass
            self.sleep(0.25)
        return False

    def open_target(self):
        """登录成功后统一进入工作台，等待页面渲染；返回是否确认到达工作台。"""
        try:
            self.check_stop()
            self.retry(
                lambda: self.page.goto(
                    config.TARGET_URL,
                    wait_until='domcontentloaded',
                    timeout=config.NAV_TIMEOUT_MS,
                ),
                label='打开工作台',
            )
            ready = self._wait_workbench_ready()
            self.sleep(1.0)
            # 工作台可能先渲染、随后才弹出“账号已在其他PC登录”遮罩，这里再确认一次
            if ready and not self._is_logged_in():
                ready = False
            if ready:
                self.log('已进入工作台：%s' % self._safe_page_location())
            else:
                self.log('未确认工作台加载完成，当前路由=%s' % self._safe_page_location())
            return ready
        except TaskCancelled:
            raise
        except Exception as exc:
            self.log('进入工作台失败：%r' % exc)
            return False

    @staticmethod
    def _is_navigation_transition_error(exc):
        error_text = str(exc).lower()
        return any(marker in error_text for marker in (
            'execution context was destroyed',
            'cannot find context',
            'frame was detached',
            'navigation interrupted',
        ))

    def _safe_page_location(self):
        try:
            parsed = urlsplit(self.page.url)
            host = parsed.hostname or ''
            location = '%s://%s%s' % (parsed.scheme, host, parsed.path)
            route = parsed.fragment.split('?', 1)[0]
            if route:
                location += '#' + route
            return location
        except Exception:
            return '<不可用>'

    # ---------- 浏览器 ----------
    def _ensure_browser(self, storage_state=None):
        if self.browser:
            return
        config.ensure_dirs()
        mode = 'headless 后台' if config.HEADLESS else 'headed 调试'
        self.log('启动 Edge（%s）' % mode)
        self.pw = sync_playwright().start()
        launch_options = {'channel': config.BROWSER_CHANNEL, 'headless': config.HEADLESS}
        if self.proxy_server:
            launch_options['proxy'] = {'server': self.proxy_server}
            self.log('\u6d4f\u89c8\u5668\u4f7f\u7528\u672c\u5730\u4ee3\u7406\uff1a%s' % self.proxy_server)
        self.browser = self.pw.chromium.launch(**launch_options)
        context_options = {'viewport': config.VIEWPORT}
        if storage_state:
            context_options['storage_state'] = storage_state
        self.context = self.browser.new_context(**context_options)
        # 显式超时：任何点击/填表/跳转都不会无限期挂住工作线程
        self.context.set_default_timeout(config.ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        self.page = self.context.new_page()
        self.page.on('request', self._record_auth_request)
        self.page.on('response', self._record_auth_response)
        self.log('浏览器就绪：动作超时 %dms，跳转超时 %dms' % (
            config.ACTION_TIMEOUT_MS, config.NAV_TIMEOUT_MS
        ))

    # ---------- 登录 + 自动滑块 ----------
    def start_login(self, user, pwd):
        """完成填表、点登录、自动滑块；旧会话冲突时结束旧会话并重试一次。"""
        self.check_stop()
        with self.step(
            '初始化会话',
            'headless=%s 通道=%s 视口=%sx%s' % (
                config.HEADLESS, config.BROWSER_CHANNEL,
                config.VIEWPORT['width'], config.VIEWPORT['height'],
            ),
        ) as outcome:
            self._ensure_browser()
            outcome['value'] = '浏览器与页面已就绪'

        page = self.page
        self.progress('\u767b\u5f55\u4e2d')
        request_start = len(self.auth_requests)
        response_start = len(self.auth_responses)
        phone = False
        logged_in = False

        for attempt in range(2):
            self.check_stop()
            attempt_request_start = len(self.auth_requests)
            attempt_response_start = len(self.auth_responses)
            with self.step(
                '打开登录页', '第 %d/2 次尝试' % (attempt + 1)
            ) as outcome:
                self.retry(
                    lambda: page.goto(
                        config.LOGIN_URL,
                        wait_until='domcontentloaded',
                        timeout=config.NAV_TIMEOUT_MS,
                    ),
                    label='打开登录页',
                )
                self.sleep(1.5)
                page.evaluate(
                    "() => { const s=document.querySelector('.js-tab-password-login'); if(s) s.click(); }"
                )
                self.sleep(0.5)
                page.get_by_text('密码登录', exact=True).click()
                self.sleep(0.5)
                outcome['value'] = '密码登录页已切换'

            self.progress('\u8d26\u53f7\u5bc6\u7801\u8f93\u5165\u4e2d')
            with self.step(
                '输入账号密码', '账号=%s 密码=***' % _mask_account(user)
            ) as outcome:
                self.log('登录工作台')
                self.log('输入账号密码中')
                page.get_by_role('textbox', name='请输入手机号').fill(user)
                page.get_by_role('textbox', name='输入登录密码').fill(pwd)
                self.log('输入完成')
                al = page.get_by_role('checkbox', name=re.compile('3\\s*天内自动登录'))
                if al.count() != 1:
                    raise RuntimeError('无法唯一定位「3天内自动登录」复选框')
                if not al.is_checked():
                    al.check()
                if not al.is_checked():
                    raise RuntimeError('「3天内自动登录」复选框未能勾选')
                self.log('已勾选「3天内自动登录」')

                ag = page.get_by_role('checkbox', name=re.compile('阅读并同意'))
                if ag.count() and not ag.is_checked():
                    ag.check()
                outcome['value'] = '账号密码已写入，自动登录已勾选'

            self.check_stop()
            self.log('点击登录')
            page.get_by_role('button', name=re.compile('登\\s*录')).click()

            self.log('滑块验证中')
            self.progress('\u9a8c\u8bc1\u7801\u9636\u6bb5')
            slider_passed = self._solve_slider()
            if not slider_passed:
                self.log('\u6ed1\u5757\u672a\u786e\u8ba4\u901a\u8fc7\uff0c\u505c\u6b62\u767b\u5f55')
                self._log_auth_diagnostics(attempt_request_start, attempt_response_start, '\u767b\u5f55\u5c1d\u8bd5\u8bca\u65ad')
                self._log_visible_login_error('\u767b\u5f55\u5c1d\u8bd5\u8bca\u65ad')
                return False
            phone = False
            logged_in = False
            conflict = False
            for _ in range(12):
                self.check_stop()
                try:
                    conflict = self._has_session_conflict()
                    if conflict:
                        break
                    phone = self._is_sms_challenge()
                    logged_in = self._is_logged_in()
                    if phone or logged_in:
                        break
                except TaskCancelled:
                    raise
                except Exception as exc:
                    if not self._is_navigation_transition_error(exc):
                        raise
                self.sleep(0.5)

            if conflict:
                self.log('检测到「账号已在其他PC登录」，结束旧会话并重新登录')
                if not self._click_relogin():
                    self.log('未能点击「重新登录」，直接重新打开登录页')
                if attempt == 0:
                    continue
                break

            if phone or logged_in:
                if phone:
                    self.progress('\u624b\u673a\u9a8c\u8bc1')
                break

            self._log_auth_diagnostics(attempt_request_start, attempt_response_start, '\u767b\u5f55\u5c1d\u8bd5\u8bca\u65ad')
            self._log_visible_login_error('\u767b\u5f55\u5c1d\u8bd5\u8bca\u65ad')
            self.log('\u672a\u8bc6\u522b\u5230\u77ed\u4fe1\u9a8c\u8bc1\u6216\u5df2\u767b\u5f55\u72b6\u6001\uff1b\u4e0d\u81ea\u52a8\u91cd\u590d\u63d0\u4ea4\u8d26\u53f7\u5bc6\u7801')
            break

        with self.step('登录态校验', '手机验证=%s 已登录=%s' % (phone, logged_in)) as outcome:
            if not phone and not logged_in:
                self._log_visible_login_error('\u5bc6\u7801\u767b\u5f55\u8bca\u65ad')
            self.needs_sms = bool(phone) and not logged_in
            ok = bool(phone or logged_in)
            if ok and logged_in:
                self.save_cookies()
                self.open_target()
                # 进入工作台后再次确认，避免落地页又弹出会话冲突遮罩
                logged_in = self._is_logged_in()
                ok = logged_in
                if logged_in:
                    self.save_page_html('密码登录')
            outcome['value'] = (
                '需短信验证码' if self.needs_sms
                else ('已直接登录' if logged_in else '未通过')
            )

        self.log('登录结果：%s' % (
            '需短信验证码' if self.needs_sms
            else ('已直接登录' if logged_in else '未通过')
        ))
        self.log('登录后路由：%s' % self._safe_page_location())
        return ok

    # ---------- 滑块通用 ----------
    def _solve_slider(self):
        """等待可见滑块出现并自动破解（失败自动刷新重试）。返回 True=滑块已通过（容器消失）。"""
        page = self.page
        for attempt in range(4):
            self.check_stop()
            with self.step(
                '滑块验证码获取', '第 %d/4 次尝试' % (attempt + 1)
            ) as outcome:
                self.log('滑块验证中')
                try:
                    page.wait_for_function(
                        "() => [...document.querySelectorAll('.slide-captcha-container')].some(el => el.getBoundingClientRect().width > 0)",
                        timeout=config.ASSERT_TIMEOUT_MS)
                except Exception as exc:
                    if self._has_session_conflict():
                        self.log('滑块未出现：检测到账号已在其他设备登录')
                        outcome['value'] = '滑块未出现（会话冲突）'
                    elif self._is_navigation_transition_error(exc):
                        self.log('等待滑块时页面已跳转')
                        outcome['value'] = '滑块未出现（页面跳转）'
                    else:
                        self.log('等待滑块超时：%r' % exc)
                        outcome['value'] = '滑块未出现（等待超时）'
                    return False
                self.sleep(1.0)
                info = page.evaluate(JS_GET_IMAGES)
                if info.get('err'):
                    raise RuntimeError('读取拼图失败: ' + info['err'])
                bg = base64.b64decode(info['bigB64'])
                sl = base64.b64decode(info['smallB64'])
                outcome['value'] = '背景图 %d 字节，拼图 %d 字节' % (len(bg), len(sl))

            with self.step(
                '滑块识别', '背景图 CSS 宽=%.0f' % info['bigCssW']
            ) as outcome:
                x, y, score = loc_gap_offload(bg, sl)
                bgw = Image.open(io.BytesIO(bg)).size[0]
                scale = info['bigCssW'] / bgw
                dist = x * scale - info['blockLeft']
                self.progress('\u5f00\u59cb\uff1a\u6ed1\u5757\u8bc6\u522b')
                self.progress('\u6ed1\u5757\u8bc6\u522b\uff1aX=%d\uff0cY=%d' % (x, y))
                self.log('坐标X=%d Y=%d，拖动距离=%.1fpx' % (x, y, dist))
                outcome['value'] = '缺口坐标 X=%d Y=%d，得分=%.1f' % (x, y, score)

            with self.step('滑块拖动', '目标位移=%.1fpx' % dist) as outcome:
                human_drag(page, dist, info['handleCx'], info['handleCy'])
                outcome['value'] = '已拖动 %.1fpx' % dist

            self.sleep(3.2)
            with self.step('滑块结果校验', '检查验证码容器是否消失') as outcome:
                still = True
                for _ in range(8):
                    self.check_stop()
                    try:
                        still = page.evaluate(
                            "() => [...document.querySelectorAll('.slide-captcha-container')].some(el => el.getBoundingClientRect().width > 0)"
                        )
                        break
                    except TaskCancelled:
                        raise
                    except Exception as exc:
                        if not self._is_navigation_transition_error(exc):
                            raise
                        self.sleep(0.25)
                outcome['value'] = '容器仍在（未通过）' if still else '容器已消失（通过）'
            if not still:
                self.log('验证完成')
                return True
            self.log('滑块第%d次未通过，刷新重试' % (attempt + 1))
            self.sleep(1.8)
        return False

    # ---------- 短信 ----------
    def send_sms(self):
        """点获取验证码并通过发码前滑块；仅在确认发码接口成功时返回 True。"""
        self.check_stop()
        page = self.page
        request_start = len(self.auth_requests)
        response_start = len(self.auth_responses)
        with self.step('请求短信验证码', '点击「获取验证码」并完成发码前滑块') as outcome:
            try:
                page.locator('.account-captcha__btn.js-get-identify-code').click(
                    timeout=config.ACTION_TIMEOUT_MS
                )
                self.log('已点「获取验证码」，触发发码前滑块校验')
            except Exception as exc:
                self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
                self.log('点击获取验证码失败: %r' % exc)
                outcome['value'] = '点击失败'
                return False
            try:
                ok = self._solve_slider()
            except TaskCancelled:
                raise
            except Exception as exc:
                self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
                self.log('发码滑块异常: %r' % exc)
                outcome['value'] = '发码滑块异常'
                return False
            self.log('发码滑块 %s' % ('通过 OK' if ok else '未通过'))
            if not ok:
                self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
                outcome['value'] = '发码滑块未通过'
                return False

            deadline = time.monotonic() + 10
            sms_responses = []
            while time.monotonic() < deadline:
                self.check_stop()
                sms_responses = [
                    item for item in self.auth_responses[response_start:]
                    if item['path'].endswith('/api/captcha/sms/send.json')
                ]
                if sms_responses:
                    break
                # Pump Playwright response events; plain Python sleep does not dispatch them.
                try:
                    page.wait_for_timeout(250)
                except Exception as exc:
                    if not self._is_navigation_transition_error(exc):
                        raise
                    self.sleep(0.25)

            self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
            if not sms_responses:
                self.log('未捕获到短信发码接口响应，不能确认短信已发送')
                outcome['value'] = '未捕获发码响应'
                return False

            result = sms_responses[-1]
            sent = result['status'] == 200 and (
                result.get('code') == '0' or result.get('success') is True
            )
            if not sent:
                self.log('短信发码接口返回失败，请检查发码限制或页面提示')
                outcome['value'] = '发码接口返回失败'
                return False
            self.log('短信发码接口已接受请求，请查收手机短信')
            outcome['value'] = 'HTTP=%s code=%s' % (
                result['status'], result.get('code', '-')
            )
        return True

    def submit_code(self, code):
        """填写短信验证码并确定；返回 True=成功、False=明确拒绝、None=无法确认。"""
        page = self.page
        code = str(code).strip()
        if not re.fullmatch(r'[0-9]{6}', code):
            self.log('验证码格式不正确：需要 6 位数字')
            return False

        box = page.locator('.account-captcha__input input:visible')
        if box.count() != 1:
            box = page.get_by_role('textbox', name=re.compile('验证码|code', re.I))
        if box.count() != 1:
            self.log('无法唯一定位短信验证码输入框（匹配数=%d）' % box.count())
            return None

        buttons = page.get_by_role('button', name=re.compile('确定|确认'))
        if buttons.count() != 1:
            self.log('无法唯一定位验证码确认按钮（匹配数=%d）' % buttons.count())
            return None

        request_start = len(self.auth_requests)
        response_start = len(self.auth_responses)
        with self.step(
            '提交验证码', '验证码=***，输入框匹配数=%d' % box.count()
        ) as outcome:
            try:
                box.fill(code)
                buttons.click(
                    timeout=config.ACTION_TIMEOUT_MS, no_wait_after=True
                )
                outcome['value'] = '已填写并点击确定'
            except Exception as exc:
                if not self._is_navigation_transition_error(exc):
                    self._log_auth_diagnostics(
                        request_start, response_start, '验证码提交诊断'
                    )
                    self.log('点击验证码确认失败：%r' % exc)
                    outcome['value'] = '点击失败'
                    return None
                self.log('验证码提交时页面正在跳转，继续确认登录状态')
                outcome['value'] = '页面跳转中，转为状态确认'

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.check_stop()
            try:
                if self._is_logged_in():
                    self.log(
                        '短信验证成功 OK 路由=%s' % self._safe_page_location()
                    )
                    self._log_auth_diagnostics(
                        request_start, response_start, '验证码提交诊断'
                    )
                    self.save_cookies()
                    self.open_target()
                    if not self._is_logged_in():
                        self.log('进入工作台后检测到会话冲突/未登录，本次短信登录未生效')
                        self.save_page_html('短信登录_失败')
                        return None
                    self.save_page_html('短信登录')
                    return True

                rejected = page.evaluate("""() => {
                    const selectors = [
                        '.account-captcha__input--error',
                        '.account-captcha__warn--error'
                    ];
                    const markedError = selectors.some(selector =>
                        [...document.querySelectorAll(selector)].some(el =>
                            el.getBoundingClientRect().width > 0 &&
                            getComputedStyle(el).display !== 'none'
                        )
                    );
                    const text = document.body ? document.body.innerText : '';
                    return markedError || /验证码(?:错误|不正确|无效|已过期|已失效)/.test(text);
                }""")
            except TaskCancelled:
                raise
            except Exception as exc:
                if self._is_navigation_transition_error(exc):
                    self.sleep(0.4)
                    continue
                self._log_auth_diagnostics(
                    request_start, response_start, '验证码提交诊断'
                )
                self.log('读取验证码提交结果失败：%r' % exc)
                return None

            if rejected:
                self._log_auth_diagnostics(
                    request_start, response_start, '验证码提交诊断'
                )
                self.log('页面明确提示验证码无效或错误')
                return False
            try:
                page.wait_for_timeout(250)
            except Exception as exc:
                if not self._is_navigation_transition_error(exc):
                    self._log_auth_diagnostics(
                        request_start, response_start, '验证码提交诊断'
                    )
                    self.log('等待验证码提交结果失败：%r' % exc)
                    return None
                self.sleep(0.25)

        self._log_auth_diagnostics(request_start, response_start, '验证码提交诊断')
        self.log(
            '验证码提交后 15 秒仍未确认登录；页面路由=%s'
            % self._safe_page_location()
        )
        return None

    # ---------- CK / 页面源码保存 ----------
    def save_page_html(self, tag):
        """把当前页面完整源码保存为带时间戳的 HTML（html/ 目录），返回路径；失败返回 None。"""
        try:
            html = self.page.content()
            path = config.timestamped_path(config.HTML_DIR, tag, 'html')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html)
            self.log('已保存页面源码 -> %s（%d 字节）' % (path, len(html)))
            return path
        except Exception as exc:
            self.log('保存页面源码失败：%r' % exc)
            return None

    def save_cookies(self):
        """抓取并落盘 CK；只输出字段名与数量，不打印票据值。"""
        self.check_stop()
        with self.step('抓取 CK', '从浏览器上下文读取 Cookie/localStorage') as outcome:
            cookies = self.context.cookies()
            names = sorted({
                str(item.get('name')) for item in cookies if item.get('name')
            })
            with open(config.COOKIE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            self.context.storage_state(path=config.AUTH_FILE)
            self.log('获取到 %d 个 Cookie 字段：%s' % (len(cookies), ','.join(names)))
            self.log(
                '已更新本地登录凭据 -> %s；含 localStorage TOKEN -> %s'
                % (config.COOKIE_FILE, config.AUTH_FILE)
            )
            outcome['value'] = '%d 个 Cookie 字段已保存' % len(cookies)

    def load_cookies_and_go(self):
        """用已保存的 cookies.json 直接访问目标页，免登录；并抓取登录后页面源码存为 HTML 供检查。"""
        if not os.path.exists(config.AUTH_FILE) and not os.path.exists(config.COOKIE_FILE):
            self.log('没有本地 cookies.json/auth_state.json，请先正常登录一次')
            return False

        self.check_stop()
        storage_state = config.AUTH_FILE if os.path.exists(config.AUTH_FILE) else None
        with self.step(
            '初始化会话', '免密来源=%s' % ('auth_state' if storage_state else 'cookies')
        ) as outcome:
            self._ensure_browser(storage_state=storage_state)
            if storage_state is None:
                with open(config.COOKIE_FILE, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
            outcome['value'] = '浏览器已加载本地登录态'

        self.open_target()
        url = self.page.url
        try:
            title = self.page.title()
        except Exception:
            title = ''
        logged_in = self._is_logged_in()
        conflict = (not logged_in) and self._has_session_conflict()
        if conflict:
            self.log('免密会话已被其他设备顶掉，将结束旧会话并转全新登录')
        if logged_in:
            self.save_cookies()
        self.log('免密登录访问 URL=%s' % url)
        self.log('页面标题=%s' % title)
        self.save_page_html('免密登录' if logged_in else '免密登录_失败')
        if logged_in:
            result_text = '正常（已进入工作台）'
        elif conflict:
            result_text = '异常（账号已在其他设备登录，将转全新登录）'
        else:
            result_text = '异常（可能跳回登录页，请检查 Cookie 是否过期）'
        self.log('免密登录 %s' % result_text)
        return logged_in

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
