# -*- coding: utf-8 -*-
"""
入口主程序 main.py —— 有赞后台自动登录 GUI。
运行：python main.py                  （图形界面）
      python main.py --smoke          （无 GUI，无头冒烟：登录+自动滑块到短信阶段即退出）
      python main.py --test-followup      （真实跑指定店铺跟进记录测试用例，会发布）
      python main.py --test-followup-dry  （只填表并截图，不点击发布）
      python main.py --list-shops         （读取「我的店铺」全部店铺并输出日志，只读）
      python main.py --fill-first3        （填写第一页前3家店铺的跟进记录并发布）
      python main.py --fill-first3-dry    （只填写前3家，不发布）
      python main.py --fill-all           （全量店铺跟进；失败即停止）
      python main.py --fill-all --skip 3  （跳过前3家，避免重复）
      python main.py --fill-all --workers 5（5线程并发填写）
      python main.py --fill-all-dry --workers 10（10线程试填写，不发布）

登录流程：1
  - 点「开始登录」：若本地有 CK 或完整 auth_state，先执行免密登录；失败再转常规登录。
  - 常规登录如需短信：自动发送；输入验证码点「提交验证码」。提交失败(一次性)即废弃本次登录并点亮「开始登录」。
  - 再次点「开始登录」：先结束所有正在运行的登录服务(worker+浏览器)，再重启登录。

线程模型：所有 Playwright 操作都在 worker 线程里执行；GUI 线程只发信号(Event)，绝不直接调 Playwright
         （Playwright sync 连接绑定线程，跨线程调用会报 "Playwright connection closed"）。
"""
import os
import sys
import subprocess

# ---- 自动切换到项目 venv 的 python（保证 playwright/pillow/numpy 可用）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV = os.path.join(_HERE, 'venv')
_VENV_PY = os.path.join(_VENV, 'Scripts', 'python.exe')
if os.path.isdir(_VENV) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(_VENV)):
    raise SystemExit(subprocess.call([_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))

# ---- 源码模块归档在 py/，加入 import 路径（入口 main.py 固定留在项目根目录）----
_PY_DIR = os.path.join(_HERE, 'py')
if _PY_DIR not in sys.path:
    sys.path.insert(0, _PY_DIR)

import time
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

import config
from youzan_bot import YouzanBot

# 控制台编码：Windows 默认 GBK 打印 emoji/特殊字符会报错，统一转 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ---- GUI 控件（模块级，供回调使用） ----
root = entry_user = entry_pwd = entry_code = None
btn_start = btn_submit = btn_ck = log_widget = None
auto_followup_var = None   # GUI 勾选：登录成功后自动填写并发布跟进记录
auto_shop_list_var = None  # GUI 勾选：登录成功后读取店铺列表（只读）
auto_fill_all_var = None   # GUI 勾选：登录后全量填写跟进记录
fill_all_dry_var = None    # GUI 勾选：全量跟进试运行（不发布）
worker_count_var = None    # GUI 下拉：并发线程数

# ---- 登录运行状态 ----
bot = None
worker_thread = None
login_running = {'v': False}
stop_flag = threading.Event()       # 置位=要求当前登录停止
code_event = threading.Event()      # GUI 提交验证码信号
code_value = {'v': ''}
fresh_login_requested = {'v': False}  # 旧 CK 失效后，后续登录跳过免密复用


def log(msg):
    ts = time.strftime('%H:%M:%S')
    line = '[%s] %s' % (ts, msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if log_widget:
        log_widget.after(0, lambda: _append(line))


def _append(line):
    log_widget.insert(tk.END, line + '\n')
    log_widget.see(tk.END)


def raw_log(msg):
    """不加时间戳的原始输出，用于严格的店铺块格式。"""
    try:
        sys.stdout.write(str(msg).rstrip('\n') + '\n')
        sys.stdout.flush()
    except Exception:
        pass
    if log_widget:
        log_widget.after(0, lambda: _append(msg))


def _append_styled(text, tag):
    log_widget.insert(tk.END, text + '\n', tag)
    log_widget.see(tk.END)


_ANSI_ENABLED = {'v': False}


def _enable_ansi_once():
    if _ANSI_ENABLED['v']:
        return
    _ANSI_ENABLED['v'] = True
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def styled_log(text, kind):
    """跟进结果状态行：成功加粗红色，失败绿色。"""
    if log_widget:
        tag = 'followup_success' if kind == 'success' else 'followup_fail'
        log_widget.after(0, lambda: _append_styled(text, tag))
        return
    try:
        is_tty = bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        is_tty = False
    if not is_tty:
        try:
            sys.stdout.write(str(text) + '\n')
            sys.stdout.flush()
        except Exception:
            pass
        return
    _enable_ansi_once()
    color = '\033[1;31m' if kind == 'success' else '\033[32m'
    try:
        sys.stdout.write(color + str(text) + '\033[0m\n')
        sys.stdout.flush()
    except Exception:
        pass


def _make_debug_log(prefix):
    """把运行细节写到独立文件，不污染店铺块输出。"""
    config.ensure_dirs()
    path = os.path.join(
        config.TXT_DIR, '%s_%s.log' % (prefix, time.strftime('%Y%m%d_%H%M%S'))
    )

    def debug_log(msg):
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
        except Exception:
            pass

    return debug_log


def _ck_token_exist():
    return os.path.exists(config.COOKIE_FILE) or os.path.exists(config.AUTH_FILE)


def enable_code_ui():
    entry_code.config(state='normal')
    btn_submit.config(state='normal')
    entry_code.focus_set()


def _reset_start_btn():
    btn_start.config(state='normal')
    if btn_ck:
        btn_ck.config(state='normal')


def _stop_all_services():
    """结束当前 worker/浏览器会话，确保下一次登录从干净状态开始。"""
    global bot, worker_thread
    thread = worker_thread
    if not login_running['v'] and (thread is None or not thread.is_alive()):
        bot = None
        return True
    stop_flag.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=8)
    if thread is not None and thread.is_alive():
        log('当前登录服务仍未退出，请稍后再试')
        return False
    bot = None
    return True


def _confirm_repeat_followup(done_count):
    """在 GUI 主线程询问是否重复填写当天已有记录。"""
    if root is None:
        return False

    answer = {'yes': False}
    decided = threading.Event()

    def ask():
        try:
            answer['yes'] = messagebox.askyesno(
                '重复填写确认',
                '检测到今天已经填写过 %d 条跟进记录，是否还需要再填写一次？'
                % done_count,
                parent=root,
            )
        except Exception:
            answer['yes'] = False
        finally:
            decided.set()

    try:
        root.after(0, ask)
    except Exception:
        return False

    while not decided.wait(0.2):
        if stop_flag.is_set():
            return False
    return answer['yes']


def _auto_followup_after_login(bot_obj):
    """登录成功后先统计店铺，再按 GUI 勾选执行跟进记录和/或列表读取。"""
    from followup_batch import FollowupBatch
    from shop_list_reader import ShopListReader

    auto_fill_all = bool(auto_fill_all_var and auto_fill_all_var.get())
    auto_followup = bool(auto_followup_var and auto_followup_var.get())
    show_shop_list = bool(auto_shop_list_var and auto_shop_list_var.get())
    debug_log = _make_debug_log('shop_list_run')
    batch = FollowupBatch(bot_obj, raw_log, styled_log, debug_log)
    reader = batch.reader

    try:
        debug_log('开始读取店铺列表（只读）')
        # 先静默收集完整列表，确保总数出现在店铺明细之前。
        reader = ShopListReader(bot_obj, lambda message: None, debug=debug_log)
        list_result = reader.run()
        shops = reader.shops
        batch.reader = reader
        debug_log('店铺列表读取结果：success=%s count=%s' % (
            list_result.get('success'), list_result.get('count', 0)
        ))
        raw_log('当前检索到店铺一共%d个' % len(shops))
        if show_shop_list:
            for index, shop in enumerate(shops):
                block = reader._shop_block_lines(shop)
                if index:
                    block = block[1:]
                raw_log('\n'.join(block))
    except Exception as exc:
        debug_log('登录后店铺统计异常：%r' % exc)
        log('登录后店铺统计失败：%r' % exc)
        shops = []

    if stop_flag.is_set():
        log('已取消，跳过跟进记录自动化')
        return
    if not auto_fill_all and not auto_followup:
        return

    today_ids = batch.load_done_ids() | batch.today_visit_ids(shops)
    if auto_fill_all:
        already_done_shops = [
            shop for shop in shops if batch.shop_ids(shop) & today_ids
        ]
        already_done_count = len(already_done_shops)
        if already_done_count:
            log('检测到今天已有跟进记录的店铺：%d 家' % already_done_count)
            if not _confirm_repeat_followup(already_done_count):
                log('已选择不重复填写，跳过跟进记录')
                return
        if stop_flag.is_set():
            log('已取消，跳过全量跟进')
            return

        fill_debug = _make_debug_log('fill_all')
        dry_run = bool(fill_all_dry_var and fill_all_dry_var.get())
        try:
            workers = int(worker_count_var.get()) if worker_count_var else 1
        except Exception:
            workers = 1
        fill_debug('全量跟进：workers=%d dry_run=%s' % (workers, dry_run))
        try:
            fill_batch = FollowupBatch(
                bot_obj, raw_log, styled_log, fill_debug, dry_run=dry_run
            )
            fill_batch.reader = reader
            result = fill_batch.run(
                all_shops=True,
                workers=workers,
                skip_done=not bool(already_done_count),
                shops=shops,
                announce_count=False,
                done_ids=today_ids,
            )
            fill_debug('全量跟进结果：%s' % result)
        except Exception as exc:
            fill_debug('全量跟进异常：%r' % exc)
        return

    if auto_followup:
        from shop_followup import SHOP_ID, ShopFollowupCase

        target_done = SHOP_ID in today_ids
        if target_done:
            log('检测到该店铺今天已经填写过跟进记录')
            if not _confirm_repeat_followup(1):
                log('已选择不重复填写，跳过跟进记录')
                return
        if stop_flag.is_set():
            log('已取消，跳过跟进记录自动化')
            return
        log('开始填写跟进记录')
        try:
            case = ShopFollowupCase(bot_obj, log, dry_run=False)
            result = case.run()
            if not result.get('success'):
                log('跟进记录失败：%s' % result.get('error', '未知错误'))
        except Exception as exc:
            log('跟进记录异常：%r' % exc)


def login_worker(user, pwd):
    """一次完整登录尝试：免密(可选) -> 常规 -> 短信(一次性)。全部 Playwright 操作在本线程执行。"""
    global bot
    b = None
    login_debug = _make_debug_log('login')
    try:
        # 1) 若最近一次一键免密失败，持续跳过旧凭据，直到常规登录成功
        if fresh_login_requested['v']:
            pass
        elif _ck_token_exist():
            b = YouzanBot(login_debug)
            bot = b
            if stop_flag.is_set():
                return
            mfree_ok = False
            try:
                mfree_ok = b.load_cookies_and_go()
            except Exception as e:
                log('免密登录异常: %r' % e)
            if stop_flag.is_set():
                return
            if mfree_ok:
                fresh_login_requested['v'] = False
                log('登录工作台成功')
                _auto_followup_after_login(b)
                return
            fresh_login_requested['v'] = True
            log('免密登录失败，转账号密码登录')
            try:
                b.close()
            except Exception:
                pass
            b = None
            bot = None

        if stop_flag.is_set():
            return

        # 2) 常规登录（账号密码 + 自动滑块）
        b = YouzanBot(login_debug)
        bot = b
        ok = b.start_login(user, pwd)
        if stop_flag.is_set():
            return
        if not ok:
            log('常规登录未通过，请重新点「开始登录」')
            return

        # 3) 如需短信：自动发送 + 一次性提交
        if b.needs_sms:
            if not b.send_sms():
                log('未能确认短信已发送，本次登录结束；请检查页面/认证接口诊断后重新开始')
                return
            log('短信验证码已发送，请输入验证码')
            if root:
                root.after(0, enable_code_ui)
            code = None
            for _ in range(1200):  # 最多等 20 分钟，期间响应停止/取消
                if stop_flag.wait(0.5):
                    log('登录已取消')
                    return
                if code_event.wait(0.5):
                    code = code_value['v']
                    break
            if stop_flag.is_set() or not code:
                log('未提交验证码，本次登录结束')
                return
            submit_result = b.submit_code(code)
            if submit_result is not True:
                if submit_result is False:
                    log('验证码被页面明确拒绝，本次登录结束（一次性方案）。请重新点「开始登录」')
                else:
                    log('验证码提交结果未能确认，本次登录结束（一次性方案）。请检查诊断信息后重新开始')
                return
            fresh_login_requested['v'] = False
            log('登录工作台成功')
        else:
            fresh_login_requested['v'] = False
            log('登录工作台成功')
        _auto_followup_after_login(b)
    except Exception as e:
        log('登录异常: %r' % e)
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass
        bot = None
        stop_flag.clear()
        code_event.clear()
        code_value['v'] = ''
        login_running['v'] = False
        if root:
            root.after(0, _reset_start_btn)


def on_start():
    user = entry_user.get().strip()
    pwd = entry_pwd.get().strip()
    if not user or not pwd:
        messagebox.showwarning('提示', '请先填写账号和密码')
        return
    global worker_thread
    # 每次点「开始登录」都先结束全部历史登录会话，再重新走登录流程
    if not _stop_all_services():
        return
    fresh_login_requested['v'] = False  # 每次都优先检测免密登录
    log('正在登录工作台...')
    stop_flag.clear()
    code_event.clear()
    code_value['v'] = ''
    btn_start.config(state='disabled')
    entry_code.config(state='disabled')
    btn_submit.config(state='disabled')
    entry_code.delete(0, tk.END)
    login_running['v'] = True
    worker_thread = threading.Thread(target=login_worker, args=(user, pwd), daemon=True)
    worker_thread.start()


def on_submit():
    code = entry_code.get().strip()
    if not code.isascii() or not code.isdigit() or len(code) != 6:
        messagebox.showwarning('提示', '请输入 6 位数字验证码')
        return
    code_value['v'] = code
    code_event.set()
    btn_submit.config(state='disabled')  # 一次性


def on_ck():
    global bot, worker_thread
    if login_running['v']:
        return
    btn_start.config(state='disabled')
    btn_ck.config(state='disabled')
    login_running['v'] = True

    def task():
        global bot
        b = None
        login_debug = _make_debug_log('login')
        try:
            b = YouzanBot(login_debug)
            bot = b
            ok = b.load_cookies_and_go()
            if ok:
                fresh_login_requested['v'] = False
                log('登录工作台成功')
                _auto_followup_after_login(b)
            else:
                fresh_login_requested['v'] = True
                log('免密登录失败，请点「开始登录」')
        except Exception as e:
            fresh_login_requested['v'] = True
            log('免密登录异常，已标记旧凭据失效：%r' % e)
        finally:
            try:
                if b is not None:
                    b.close()
            except Exception:
                pass
            bot = None
            stop_flag.clear()
            login_running['v'] = False
            if root:
                root.after(0, _reset_start_btn)

    worker_thread = threading.Thread(target=task, daemon=True)
    worker_thread.start()


def on_close():
    _stop_all_services()
    root.destroy()


def run_gui():
    global root, entry_user, entry_pwd, entry_code, btn_start, btn_submit, btn_ck, log_widget
    global auto_followup_var, auto_shop_list_var, auto_fill_all_var, fill_all_dry_var, worker_count_var
    root = tk.Tk()
    root.title('有赞后台自动登录（滑块+短信+抓CK）')
    root.geometry('720x650')

    frm = tk.Frame(root)
    frm.pack(fill='x', padx=10, pady=8)

    tk.Label(frm, text='账号').grid(row=0, column=0, sticky='e')
    entry_user = tk.Entry(frm, width=24)
    entry_user.grid(row=0, column=1, padx=4)
    entry_user.insert(0, config.DEFAULT_USER)

    tk.Label(frm, text='密码').grid(row=1, column=0, sticky='e')
    entry_pwd = tk.Entry(frm, width=24, show='*')
    entry_pwd.grid(row=1, column=1, padx=4)
    entry_pwd.insert(0, config.DEFAULT_PWD)

    tk.Label(frm, text='验证码').grid(row=2, column=0, sticky='e')
    entry_code = tk.Entry(frm, width=24, state='disabled')
    entry_code.grid(row=2, column=1, padx=4)

    btn_start = tk.Button(frm, text='开始登录', command=on_start, width=12)
    btn_start.grid(row=0, column=2, padx=6)
    btn_submit = tk.Button(frm, text='提交验证码', command=on_submit, width=12, state='disabled')
    btn_submit.grid(row=2, column=2, padx=6)
    btn_ck = tk.Button(frm, text='一键免密登录', command=on_ck, width=12)
    btn_ck.grid(row=3, column=2, padx=6, pady=4)

    auto_followup_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        frm,
        text='登录后填写指定店铺跟进记录（单条任务）',
        variable=auto_followup_var,
    ).grid(row=3, column=0, columnspan=2, sticky='w', pady=4)

    auto_shop_list_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        frm,
        text='登录后读取店铺列表并输出日志（只读，不修改数据）',
        variable=auto_shop_list_var,
    ).grid(row=4, column=0, columnspan=2, sticky='w', pady=4)

    auto_fill_all_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        frm,
        text='登录后统计店铺并填写全部跟进记录（默认开启）',
        variable=auto_fill_all_var,
    ).grid(row=5, column=0, columnspan=2, sticky='w', pady=4)

    fill_all_dry_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        frm, text='全量跟进试运行（只填表不发布）',
        variable=fill_all_dry_var,
    ).grid(row=6, column=0, sticky='w', pady=4)

    tk.Label(frm, text='并发线程').grid(row=6, column=1, sticky='e', padx=4)
    worker_count_var = tk.StringVar(value='5')
    ttk.Combobox(
        frm,
        textvariable=worker_count_var,
        values=('1', '2', '5', '8', '10', '15', '20'),
        width=5,
        state='readonly',
    ).grid(row=6, column=2, sticky='w', padx=6)

    tk.Label(root, text='运行日志（缺口计算 / 拖动距离 / 登录结果等关键信息全程输出）').pack(anchor='w', padx=10)
    log_widget = scrolledtext.ScrolledText(root, height=26, font=('Consolas', 10))
    log_widget.pack(fill='both', expand=True, padx=10, pady=6)
    log_widget.tag_configure(
        'followup_success', foreground='#D40000', font=('Consolas', 10, 'bold')
    )
    log_widget.tag_configure(
        'followup_fail', foreground='#008000', font=('Consolas', 10)
    )

    root.protocol('WM_DELETE_WINDOW', on_close)
    root.mainloop()


def run_smoke():
    """无头冒烟：走登录+自动滑块，到短信阶段返回，用于验证软件可正常运行。"""
    b = YouzanBot(log)
    try:
        ok = b.start_login(config.DEFAULT_USER, config.DEFAULT_PWD)
        print('SMOKE_RESULT =', ok)
    finally:
        b.close()


def _wait_sms_code(timeout=600, logger=log):
    """测试用例模式等待短信验证码：优先环境变量，其次 txt/sms_code.txt。"""
    code = os.environ.get('YOUZAN_SMS_CODE', '').strip()
    if code:
        return code
    logger('等待短信验证码：设置 YOUZAN_SMS_CODE 或写入 %s' % config.SMS_CODE_FILE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_flag.is_set():
            return ''
        try:
            if os.path.exists(config.SMS_CODE_FILE):
                with open(config.SMS_CODE_FILE, encoding='utf-8') as handle:
                    code = handle.read().strip()
                if code:
                    return code
        except Exception:
            pass
        time.sleep(1)
    return ''


def _cli_login(logger=log):
    """CLI 通用登录：优先本地 CK+TOKEN，失败再账号密码/短信。失败返回 None。"""
    config.ensure_dirs()
    config.VIEWPORT = {'width': 1920, 'height': 1080}
    try:
        if os.path.exists(config.SMS_CODE_FILE):
            os.remove(config.SMS_CODE_FILE)
    except OSError:
        pass

    b = YouzanBot(logger)
    logged_in = False
    if _ck_token_exist():
        logger('检测到本地登录态，先检测免密登录')
        try:
            logged_in = b.load_cookies_and_go()
        except Exception as exc:
            logger('免密登录异常：%r' % exc)
        if not logged_in:
            logger('免密不可用，转账号密码登录')
            try:
                b.close()
            except Exception:
                pass
            b = YouzanBot(logger)

    if not logged_in:
        ok = b.start_login(config.DEFAULT_USER, config.DEFAULT_PWD)
        if not ok:
            logger('登录未通过，任务终止')
            b.close()
            return None
        if b.needs_sms:
            if not b.send_sms():
                logger('短信发送未能确认，任务终止')
                b.close()
                return None
            code = _wait_sms_code(logger=logger)
            if not code:
                logger('未获取到短信验证码，任务终止')
                b.close()
                return None
            if b.submit_code(code) is not True:
                logger('短信验证码未通过，任务终止')
                b.close()
                return None
    return b


def run_followup_cli(dry_run=False):
    """运行指定店铺跟进记录测试用例。"""
    from shop_followup import ShopFollowupCase

    b = _cli_login()
    if b is None:
        return False
    try:
        case = ShopFollowupCase(b, log, dry_run=dry_run)
        result = case.run()
        log('测试用例结束：success=%s 接口=%s 右侧链接=%s 右侧功能点=%s' % (
            result.get('success'),
            result.get('api_count', 0),
            result.get('links_right_side', 0),
            result.get('functions_total', 0),
        ))
        log('FOLLOWUP_RESULT = %s' % result.get('success'))
        return bool(result.get('success'))
    finally:
        try:
            b.close()
        except Exception:
            pass


def run_fill_first3_cli(dry_run=False):
    """填写第一页前 3 家店铺的跟进记录，只输出块和汇总。"""
    from followup_batch import FollowupBatch

    debug_log = _make_debug_log('fill_first3')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = FollowupBatch(
            b, raw_log, styled_log, debug_log, dry_run=dry_run
        ).run(3)
        return result.get('attempted', 0) > 0 and result.get('failed', 0) == 0
    finally:
        try:
            b.close()
        except Exception:
            pass


def run_fill_all_cli(skip=0, dry_run=False, workers=1):
    """全量填写跟进记录；任一失败立即停止。"""
    from followup_batch import FollowupBatch

    debug_log = _make_debug_log('fill_all')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = FollowupBatch(
            b, raw_log, styled_log, debug_log, dry_run=dry_run
        ).run(all_shops=True, skip=skip, workers=workers)
        return result.get('attempted', 0) > 0 and result.get('failed', 0) == 0
    finally:
        try:
            b.close()
        except Exception:
            pass


def run_list_shops_cli():
    """读取「我的店铺」列表，只输出店铺分割线块，运行细节写调试文件。"""
    from shop_list_reader import ShopListReader

    debug_log = _make_debug_log('shop_list_run')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = ShopListReader(b, raw_log, debug=debug_log).run()
        return bool(result.get('success'))
    finally:
        try:
            b.close()
        except Exception:
            pass


if __name__ == '__main__':
    _skip = 0
    if '--skip' in sys.argv:
        try:
            _skip = int(sys.argv[sys.argv.index('--skip') + 1])
        except Exception:
            _skip = 0
    _workers = 1
    if '--workers' in sys.argv:
        try:
            _workers = max(1, int(sys.argv[sys.argv.index('--workers') + 1]))
        except Exception:
            _workers = 1
    if '--fill-all-dry' in sys.argv:
        raise SystemExit(0 if run_fill_all_cli(
            skip=_skip, dry_run=True, workers=_workers
        ) else 1)
    if '--fill-all' in sys.argv:
        raise SystemExit(0 if run_fill_all_cli(
            skip=_skip, dry_run=False, workers=_workers
        ) else 1)
    if '--fill-first3-dry' in sys.argv:
        raise SystemExit(0 if run_fill_first3_cli(dry_run=True) else 1)
    if '--fill-first3' in sys.argv:
        raise SystemExit(0 if run_fill_first3_cli(dry_run=False) else 1)
    if '--test-followup-dry' in sys.argv:
        raise SystemExit(0 if run_followup_cli(dry_run=True) else 1)
    if '--test-followup' in sys.argv:
        raise SystemExit(0 if run_followup_cli(dry_run=False) else 1)
    if '--list-shops' in sys.argv:
        raise SystemExit(0 if run_list_shops_cli() else 1)
    if '--smoke' in sys.argv:
        run_smoke()
    else:
        run_gui()
