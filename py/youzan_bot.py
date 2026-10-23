# -*- coding: utf-8 -*-
"""
YouzanBot：封装 playwright 登录全流程（登录 -> 自动滑块 -> 短信 -> 抓CK/复用）。
浏览器在后台 headless 运行，不弹窗口。线程安全：由 GUI 在工作线程里调用本类方法。
"""
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
from captcha import loc_gap, human_drag, JS_GET_IMAGES


class YouzanBot:
    def __init__(self, log):
        self.log = log
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.needs_sms = False
        self.auth_requests = []
        self.auth_responses = []

    def _record_auth_request(self, request):
        """只记录认证 API 的方法和路径，不记录敏感查询参数或请求体。"""
        try:
            parsed = urlsplit(request.url)
            if parsed.hostname not in ('account.youzan.com', 'passport.youzan.com'):
                return
            if not parsed.path.startswith('/api/'):
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
            if parsed.hostname not in ('account.youzan.com', 'passport.youzan.com'):
                return
            if not parsed.path.startswith('/api/'):
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
                    for key in ('msg', 'message', 'errorMsg', 'error_message'):
                        message = payload.get(key)
                        if isinstance(message, str) and message.strip():
                            message = re.sub(r'\b(?:\+?86)?1[3-9]\d{9}\b', '[phone]', message)
                            message = re.sub(r'\b\d{6,}\b', '[redacted]', message)
                            entry['message'] = message[:160]
                            break
            except Exception:
                pass
            self.auth_responses.append(entry)
        except Exception:
            pass

    def _log_auth_responses(self, start, label):
        responses = self.auth_responses[start:]
        if not responses:
            self.log('%s：未捕获到认证接口响应' % label)
            return responses
        for item in responses:
            details = '%s %s HTTP=%s' % (
                item.get('method', 'GET'), item['path'], item['status']
            )
            if 'code' in item:
                details += ' code=%s' % item['code']
            if 'success' in item:
                details += ' success=%s' % item['success']
            if 'message' in item:
                details += ' message=%s' % item['message']
            self.log('%s：%s' % (label, details))
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
            time.sleep(2)
            return True
        except Exception as exc:
            if self._is_navigation_transition_error(exc):
                self.log('点击「重新登录」时页面正在跳转')
                time.sleep(2)
                return True
            self.log('点击「重新登录」失败：%r' % exc)
            return False

    def _wait_workbench_ready(self, timeout=12):
        """等待工作台导航选中或仪表盘文案出现；遇到登录页/会话冲突立即返回。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
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
            except Exception:
                pass
            try:
                self.page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
        return False

    def open_target(self):
        """登录成功后统一进入工作台，等待页面渲染；返回是否确认到达工作台。"""
        try:
            self.page.goto(config.TARGET_URL, wait_until='domcontentloaded')
            ready = self._wait_workbench_ready()
            time.sleep(1.0)
            # 工作台可能先渲染、随后才弹出“账号已在其他PC登录”遮罩，这里再确认一次
            if ready and not self._is_logged_in():
                ready = False
            if ready:
                self.log('已进入工作台：%s' % self._safe_page_location())
            else:
                self.log('未确认工作台加载完成，当前路由=%s' % self._safe_page_location())
            return ready
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
        self.browser = self.pw.chromium.launch(channel=config.BROWSER_CHANNEL, headless=config.HEADLESS)
        context_options = {'viewport': config.VIEWPORT}
        if storage_state:
            context_options['storage_state'] = storage_state
        self.context = self.browser.new_context(**context_options)
        self.page = self.context.new_page()
        self.page.on('request', self._record_auth_request)
        self.page.on('response', self._record_auth_response)

    # ---------- 登录 + 自动滑块 ----------
    def start_login(self, user, pwd):
        """完成填表、点登录、自动滑块；旧会话冲突时结束旧会话并重试一次。"""
        self._ensure_browser()
        page = self.page
        request_start = len(self.auth_requests)
        response_start = len(self.auth_responses)
        phone = False
        logged_in = False
        slider_ok = False

        for attempt in range(2):
            self.log('打开登录页' if attempt == 0 else '旧会话已结束，重新打开登录页')
            page.goto(config.LOGIN_URL, wait_until='domcontentloaded')
            time.sleep(1.5)
            page.evaluate("() => { const s=document.querySelector('.js-tab-password-login'); if(s) s.click(); }")
            time.sleep(0.5)
            page.get_by_text('密码登录', exact=True).click()
            time.sleep(0.5)

            self.log('填写账号密码')
            page.get_by_role('textbox', name='请输入手机号').fill(user)
            page.get_by_role('textbox', name='输入登录密码').fill(pwd)
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

            self.log('点击登录')
            page.get_by_role('button', name=re.compile('登\\s*录')).click()

            self.log('等待滑块出现')
            slider_ok = self._solve_slider()
            phone = False
            logged_in = False
            conflict = False
            for _ in range(12):
                try:
                    conflict = self._has_session_conflict()
                    if conflict:
                        break
                    phone = page.evaluate(
                        "() => document.body.innerText.includes('验证手机号')"
                    )
                    logged_in = self._is_logged_in()
                    if phone or logged_in:
                        break
                except Exception as exc:
                    if not self._is_navigation_transition_error(exc):
                        raise
                time.sleep(0.5)

            if conflict:
                self.log('检测到「账号已在其他PC登录」，结束旧会话并重新登录')
                if not self._click_relogin():
                    self.log('未能点击「重新登录」，直接重新打开登录页')
                if attempt == 0:
                    continue
                break

            if phone or logged_in:
                break

        if not phone and not logged_in:
            self._log_auth_diagnostics(request_start, response_start, '密码登录诊断')
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
        self.log('登录结果：%s' % ('需短信验证码' if self.needs_sms else ('已直接登录' if logged_in else '未通过')))
        self.log('登录后路由：%s' % self._safe_page_location())
        return ok

    # ---------- 滑块通用 ----------
    def _solve_slider(self):
        """等待可见滑块出现并自动破解（失败自动刷新重试）。返回 True=滑块已通过（容器消失）。"""
        page = self.page
        for attempt in range(4):
            try:
                page.wait_for_function(
                    "() => [...document.querySelectorAll('.slide-captcha-container')].some(el => el.getBoundingClientRect().width > 0)",
                    timeout=15000)
            except Exception as exc:
                if self._has_session_conflict():
                    self.log('滑块未出现：检测到账号已在其他设备登录')
                elif self._is_navigation_transition_error(exc):
                    self.log('等待滑块时页面已跳转')
                else:
                    self.log('等待滑块超时：%r' % exc)
                return False
            time.sleep(1.0)
            info = page.evaluate(JS_GET_IMAGES)
            if info.get('err'):
                raise RuntimeError('读取拼图失败: ' + info['err'])
            bg = base64.b64decode(info['bigB64'])
            sl = base64.b64decode(info['smallB64'])
            x, y, score = loc_gap(bg, sl)
            bgw = Image.open(io.BytesIO(bg)).size[0]
            scale = info['bigCssW'] / bgw
            dist = x * scale - info['blockLeft']
            self.log('拼图#%d x=%d y=%d score=%.1f 拖动=%.1fpx' % (attempt + 1, x, y, score, dist))
            human_drag(page, dist, info['handleCx'], info['handleCy'])
            time.sleep(3.2)
            still = True
            for _ in range(8):
                try:
                    still = page.evaluate(
                        "() => [...document.querySelectorAll('.slide-captcha-container')].some(el => el.getBoundingClientRect().width > 0)"
                    )
                    break
                except Exception as exc:
                    if not self._is_navigation_transition_error(exc):
                        raise
                    time.sleep(0.25)
            if not still:
                self.log('滑块通过 OK')
                return True
            self.log('滑块第%d次未通过，刷新重试' % (attempt + 1))
            time.sleep(1.8)
        return False

    # ---------- 短信 ----------
    def send_sms(self):
        """点获取验证码并通过发码前滑块；仅在确认发码接口成功时返回 True。"""
        page = self.page
        request_start = len(self.auth_requests)
        response_start = len(self.auth_responses)
        try:
            page.locator('.account-captcha__btn.js-get-identify-code').click(timeout=5000)
            self.log('已点「获取验证码」，触发发码前滑块校验')
        except Exception as exc:
            self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
            self.log('点击获取验证码失败: %r' % exc)
            return False
        try:
            ok = self._solve_slider()
            self.log('发码滑块 %s' % ('通过 OK' if ok else '未通过'))
            if not ok:
                self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
                return False
        except Exception as exc:
            self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
            self.log('发码滑块异常: %r' % exc)
            return False

        deadline = time.monotonic() + 10
        sms_responses = []
        while time.monotonic() < deadline:
            sms_responses = [
                item for item in self.auth_responses[response_start:]
                if item['path'].endswith('/api/captcha/sms/send.json')
            ]
            if sms_responses:
                break
            try:
                page.wait_for_timeout(250)
            except Exception as exc:
                if not self._is_navigation_transition_error(exc):
                    self._log_auth_diagnostics(
                        request_start, response_start, '短信发送诊断'
                    )
                    self.log('等待短信发码响应失败: %r' % exc)
                    return False
                time.sleep(0.25)

        self._log_auth_diagnostics(request_start, response_start, '短信发送诊断')
        if not sms_responses:
            self.log('未捕获到短信发码接口响应，不能确认短信已发送')
            return False

        result = sms_responses[-1]
        sent = result['status'] == 200 and (
            result.get('code') == '0' or result.get('success') is True
        )
        if sent:
            self.log('短信发码接口已接受请求，请查收手机短信')
            return True
        self.log('短信发码接口返回失败，请检查发码限制或页面提示')
        return False

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
        try:
            box.fill(code)
            buttons.click(timeout=5000, no_wait_after=True)
        except Exception as exc:
            if not self._is_navigation_transition_error(exc):
                self._log_auth_diagnostics(
                    request_start, response_start, '验证码提交诊断'
                )
                self.log('点击验证码确认失败：%r' % exc)
                return None
            self.log('验证码提交时页面正在跳转，继续确认登录状态')

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
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
            except Exception as exc:
                if self._is_navigation_transition_error(exc):
                    time.sleep(0.4)
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
                time.sleep(0.25)

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
        cookies = self.context.cookies()
        with open(config.COOKIE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        self.context.storage_state(path=config.AUTH_FILE)
        self.log(
            '已更新本地登录凭据：%d 条 Cookie/CK -> %s；含 localStorage TOKEN -> %s'
            % (len(cookies), config.COOKIE_FILE, config.AUTH_FILE)
        )

    def load_cookies_and_go(self):
        """用已保存的 cookies.json 直接访问目标页，免登录；并抓取登录后页面源码存为 HTML 供检查。"""
        if not os.path.exists(config.AUTH_FILE) and not os.path.exists(config.COOKIE_FILE):
            self.log('没有本地 cookies.json/auth_state.json，请先正常登录一次')
            return False

        storage_state = config.AUTH_FILE if os.path.exists(config.AUTH_FILE) else None
        self._ensure_browser(storage_state=storage_state)
        if storage_state is None:
            with open(config.COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            self.context.add_cookies(cookies)
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
