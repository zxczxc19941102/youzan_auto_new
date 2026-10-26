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
import json
import subprocess

# ---- 自动切换到项目 venv 的 python（保证 playwright/pillow/numpy 可用）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_CONFIG_FILE = os.path.join(_HERE, 'project_config.json')
_RUNTIME_DEFAULTS = {
    'venv_dir': '.venv',
    'python_executable_windows': os.path.join('Scripts', 'python.exe'),
    'python_executable_posix': os.path.join('bin', 'python'),
}
try:
    with open(_RUNTIME_CONFIG_FILE, 'r', encoding='utf-8') as _handle:
        _RUNTIME_CONFIG = dict(_RUNTIME_DEFAULTS)
        _RUNTIME_CONFIG.update(json.load(_handle))
except FileNotFoundError as _exc:
    raise SystemExit(
        'Project runtime config is missing: %s; restore project_config.json and retry' % _RUNTIME_CONFIG_FILE
    )
except (OSError, ValueError, TypeError) as _exc:
    raise SystemExit(
        'Project runtime config project_config.json could not be read: %s' % _exc
    )

def _project_relative_path(value, default):
    value = str(value or default)
    if os.path.isabs(value):
        raise SystemExit('Project runtime paths must be relative: %s' % value)
    return value


_VENV_REL = _project_relative_path(_RUNTIME_CONFIG.get('venv_dir'), '.venv')
_VENV_PY_REL = _project_relative_path(
    _RUNTIME_CONFIG.get('python_executable_windows')
    if os.name == 'nt'
    else _RUNTIME_CONFIG.get('python_executable_posix'),
    _RUNTIME_DEFAULTS['python_executable_windows'],
)
_VENV = os.path.abspath(os.path.join(_HERE, _VENV_REL))
_VENV_PY = os.path.abspath(os.path.join(_VENV, _VENV_PY_REL))
if os.path.isdir(_VENV) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(_VENV)):
    if not os.path.isfile(_VENV_PY):
        raise SystemExit(
            'Project virtual-environment interpreter is missing: %s; run setup.ps1 to create .venv' % _VENV_PY
        )
    raise SystemExit(subprocess.call([_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))
if os.path.abspath(sys.prefix) != _VENV and not os.path.isfile(_VENV_PY):
    raise SystemExit(
        'Project .venv interpreter was not found: %s; run .\\setup.ps1 first' % _VENV_PY
    )

# ---- 源码模块归档在 py/，加入 import 路径（入口 main.py 固定留在项目根目录）----
_PY_DIR = os.path.join(_HERE, 'py')
if _PY_DIR not in sys.path:
    sys.path.insert(0, _PY_DIR)

import contextlib
import queue
import time
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

import config
from task_runtime import DaemonThreadPoolExecutor, TaskCancelled, TaskRuntime
from youzan_bot import YouzanBot

# 控制台编码：Windows 默认 GBK 打印 emoji/特殊字符会报错，统一转 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# ---- GUI 控件（模块级，供回调使用） ----
root = entry_user = entry_pwd = entry_code = None
btn_start = btn_submit = btn_ck = btn_stop = log_widget = None
auto_shop_list_var = None  # GUI 勾选：登录成功后读取店铺列表（只读）
auto_fill_all_var = None   # GUI 勾选：登录后全量填写跟进记录
fill_all_dry_var = None    # GUI 勾选：全量跟进试运行（不发布）
worker_count_var = None    # GUI 下拉：并发线程数
proxy_server_var = None    # 本地代理，仅保存在本地配置

# ---- 运行状态（全部由主线程读写，工作线程只投递队列） ----
bot = None
runner = None                       # 当前这一轮的 TaskRuntime
run_lock = threading.Lock()
login_running = {'v': False}
stop_flag = threading.Event()       # 全局停止事件，与 runtime 共用同一个 Event
code_event = threading.Event()      # GUI 提交验证码信号
code_value = {'v': ''}
fresh_login_requested = {'v': False}  # 旧 CK 失效后，后续登录跳过免密复用

log_queue = queue.Queue()           # 工作线程 -> 主线程：日志行
ui_queue = queue.Queue()            # 工作线程 -> 主线程：控件操作与询问请求
LOG_FLUSH_MS = 150                  # 日志批量刷新间隔（避免逐条重绘）

# LOG/ 下按启动时间落一个 TXT，逐行 flush，崩溃也不丢日志
_LOG_FILE = {'handle': None, 'lock': threading.Lock(), 'path': ''}


def _ensure_log_file():
    """懒创建 LOG/run_YYYYmmdd_HHMMSS.txt，返回文件路径。"""
    with _LOG_FILE['lock']:
        if _LOG_FILE['handle'] is None:
            config.ensure_dirs()
            path = os.path.join(
                config.LOG_DIR, 'run_%s.txt' % time.strftime('%Y%m%d_%H%M%S')
            )
            _LOG_FILE['handle'] = open(path, 'a', encoding='utf-8')
            _LOG_FILE['path'] = path
        return _LOG_FILE['path']


def _log_sink(line):
    """实时写盘：任何线程调用都安全，逐行 flush。"""
    _ensure_log_file()
    with _LOG_FILE['lock']:
        handle = _LOG_FILE['handle']
        try:
            handle.write(str(line).rstrip('\n') + '\n')
            handle.flush()
        except Exception:
            pass


def log(msg):
    """线程安全日志：工作线程只入队，由主线程批量刷新。"""
    rt = runner
    if rt is not None:
        rt.emit(msg)
        return
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), msg)
    if log_widget is not None:
        log_queue.put((line, 'normal'))
    _log_sink(line)
    try:
        print(line, flush=True)
    except Exception:
        pass


def raw_log(msg):
    """不加时间戳的原始输出，用于严格的店铺块格式。"""
    text = str(msg).rstrip('\n')
    if log_widget is not None:
        log_queue.put((text, 'normal'))
    _log_sink(text)
    try:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()
    except Exception:
        pass


def styled_log(text, kind):
    """状态行着色：成功红粗、失败绿色、错误红字，均走日志队列。"""
    tag = {
        'success': 'followup_success',
        'fail': 'followup_fail',
        'error': 'log_error',
    }.get(kind, 'followup_fail')
    text = str(text)
    if log_widget is not None:
        log_queue.put((text, tag))
    _log_sink(text)
    try:
        is_tty = bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        is_tty = False
    if not is_tty:
        try:
            sys.stdout.write(text + '\n')
            sys.stdout.flush()
        except Exception:
            pass
        return
    _enable_ansi_once()
    color = '\033[1;31m' if kind == 'success' else '\033[32m'
    try:
        sys.stdout.write(color + text + '\033[0m\n')
        sys.stdout.flush()
    except Exception:
        pass


def _copy_log_selection():
    """Copy the selected log text to the system clipboard."""
    if log_widget is None or root is None:
        return
    try:
        selected = log_widget.get('sel.first', 'sel.last')
    except tk.TclError:
        return
    if not selected:
        return
    try:
        root.clipboard_clear()
        root.clipboard_append(selected)
        root.update_idletasks()
    except tk.TclError:
        pass


def _clear_log_widget():
    """Clear only the visible log widget; the on-disk log remains intact."""
    if log_widget is None:
        return
    try:
        log_widget.delete('1.0', tk.END)
    except tk.TclError:
        pass


def _show_log_context_menu(event):
    """Show right-click actions for the visible log area."""
    if log_widget is None:
        return 'break'
    menu = tk.Menu(log_widget, tearoff=False)
    try:
        has_selection = bool(log_widget.tag_ranges(tk.SEL))
    except tk.TclError:
        has_selection = False
    menu.add_command(
        label='\u590d\u5236', command=_copy_log_selection,
        state='normal' if has_selection else 'disabled',
    )
    menu.add_command(label='\u6e05\u7a7a', command=_clear_log_widget)
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return 'break'


def _append(line):
    """仅主线程调用：向日志框追加一行。"""
    log_widget.insert(tk.END, line + '\n')
    log_widget.see(tk.END)


def _append_styled(text, tag):
    """仅主线程调用：向日志框追加带颜色标签的一行。"""
    log_widget.insert(tk.END, text + '\n', tag)
    log_widget.see(tk.END)


def _drain_log_queue():
    """主线程批量刷新日志：每 150ms 取一次队列，攒批插入再滚动到底部。"""
    if log_widget is None:
        return
    lines = []
    for _ in range(500):
        try:
            lines.append(log_queue.get_nowait())
        except queue.Empty:
            break
    if lines:
        try:
            for text, tag in lines:
                if tag == 'normal':
                    _append(text)
                else:
                    _append_styled(text, tag)
        except tk.TclError:
            return
    _poll_ui_queue()
    try:
        root.after(LOG_FLUSH_MS, _drain_log_queue)
    except tk.TclError:
        pass


def _post_ui(action):
    """工作线程投递控件操作，绝不直接触碰 tkinter。"""
    ui_queue.put(action)


def _poll_ui_queue():
    """主线程执行排队中的控件操作。"""
    for _ in range(50):
        try:
            action = ui_queue.get_nowait()
        except queue.Empty:
            return
        try:
            action()
        except Exception:
            pass


@contextlib.contextmanager
def _step(name, inputs='无', quiet_success=False):
    """没有运行时的场景（如单测）退化为空上下文。"""
    rt = runner
    if rt is None:
        yield {'value': '完成'}
        return
    with rt.step(
        name,
        inputs,
        emit_start=not quiet_success,
        emit_success=not quiet_success,
    ) as outcome:
        yield outcome


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


def log_error(msg):
    """错误提示：日志区红字显示，不弹窗。"""
    styled_log(str(msg), 'error')




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
    """短信已确认发送后启用验证码控件并将主窗口提到前台。"""
    try:
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        root.after(700, lambda: root.attributes('-topmost', False))
    except tk.TclError:
        pass
    entry_code.config(state='normal')
    btn_submit.config(state='normal')
    entry_code.focus_set()




def _read_workers():
    """Read and clamp the worker count from the GUI."""
    try:
        value = int(worker_count_var.get()) if worker_count_var else config.DEFAULT_WORKERS
    except Exception:
        value = config.DEFAULT_WORKERS
    return max(1, min(10, value))


def _read_ui_settings():
    """Read Tk variables on the Tk thread before handing work to workers."""
    return {
        'auto_shop_list': bool(auto_shop_list_var and auto_shop_list_var.get()),
        'auto_fill_all': bool(auto_fill_all_var and auto_fill_all_var.get()),
        'fill_all_dry': bool(fill_all_dry_var and fill_all_dry_var.get()),
        'workers': _read_workers(),
        'proxy_server': (
            proxy_server_var.get().strip()
            if proxy_server_var else config.DEFAULT_PROXY_SERVER
        ),
    }


credential_ui_sync = {'ok': False, 'expected_user': '', 'expected_pwd': ''}


def _sync_saved_credentials_to_ui():
    """Populate saved credentials after Tk controls exist and verify by reading them back."""
    expected_user = str(config.DEFAULT_USER or '')
    expected_pwd = str(config.DEFAULT_PWD or '')
    credential_ui_sync.update({
        'ok': False,
        'expected_user': expected_user,
        'expected_pwd': expected_pwd,
    })
    if entry_user is None or entry_pwd is None:
        log_error('Credential UI sync failed: input controls are not initialized')
        return False
    try:
        entry_user.delete(0, tk.END)
        entry_user.insert(0, expected_user)
        entry_pwd.delete(0, tk.END)
        entry_pwd.insert(0, expected_pwd)
        actual_user = str(entry_user.get() or '')
        actual_pwd = str(entry_pwd.get() or '')
    except Exception as exc:
        log_error('Credential UI sync failed while writing controls: %s' % exc)
        return False
    if actual_user != expected_user or actual_pwd != expected_pwd:
        log_error(
            'Credential UI read-back failed: username_equal=%s password_equal=%s'
            % (actual_user == expected_user, actual_pwd == expected_pwd)
        )
        return False
    credential_ui_sync['ok'] = True
    if expected_user or expected_pwd:
        log('Saved credentials populated into UI; read-back passed: account=%s password=loaded' % _mask_account(expected_user))
    else:
        log('No saved credentials found; waiting for manual input')
    if getattr(config, 'LOCAL_CONFIG_LOAD_ERROR', ''):
        log_error('Local config read error: %s' % config.LOCAL_CONFIG_LOAD_ERROR)
    return True


def _save_login_config(user, pwd, settings):
    """Save local GUI settings after successful login; never log the password."""
    if not user or not pwd:
        return
    try:
        config.save_local_config(
            user, pwd,
            auto_shop_list=settings.get('auto_shop_list', True),
            auto_fill_all=settings.get('auto_fill_all', True),
            fill_all_dry=settings.get('fill_all_dry', False),
            workers=settings.get('workers', config.DEFAULT_WORKERS),
            proxy_server=settings.get('proxy_server', config.DEFAULT_PROXY_SERVER),
        )
        log('\u672c\u673a\u914d\u7f6e\u5df2\u4fdd\u5b58\uff1a\u8d26\u53f7\u3001\u5bc6\u7801\u53ca\u754c\u9762\u9009\u9879\uff08\u4ec5\u672c\u5730\u6587\u4ef6\uff0c\u4e0d\u4e0a\u4f20\uff09')
    except Exception as exc:
        log('\u672c\u673a\u914d\u7f6e\u4fdd\u5b58\u5931\u8d25\uff1a%r' % exc)


def _set_running_ui(running):
    """统一按钮状态：运行中禁止重复提交，终止按钮仅运行时可用。"""
    state = 'disabled' if running else 'normal'
    btn_start.config(state=state)
    if btn_ck:
        btn_ck.config(state=state)
    if btn_stop:
        btn_stop.config(state='normal' if running else 'disabled')
    # 验证码输入始终锁住，只有收到发码成功后才由 enable_code_ui 打开
    btn_submit.config(state='disabled')
    entry_code.config(state='disabled')


def _start_runtime():
    """按当前下拉值创建本轮运行时，并发上限严格等于所选值。"""
    global runner
    workers = _read_workers()
    stop_flag.clear()
    rt = TaskRuntime(workers, stop_flag, log_queue, sink=_log_sink)
    with run_lock:
        runner = rt
    login_running['v'] = True
    _set_running_ui(True)
    rt.emit('任务启动：并发上限 %d' % workers, source='主控')
    return rt


def on_stop_all():
    """【全部终止】：置位停止事件并取消排队任务，不弹二次确认。"""
    rt = runner
    if rt is None and not login_running['v']:
        return
    log('收到【全部终止】指令，正在停止全部任务…')
    if btn_stop:
        btn_stop.config(state='disabled')
    btn_start.config(state='disabled')
    if btn_ck:
        btn_ck.config(state='disabled')
    btn_submit.config(state='disabled')
    if rt is not None:
        rt.stop()
    else:
        stop_flag.set()


def _poll_run_state():
    """主线程轮询收尾：所有 future 结束后汇总，不阻塞界面。"""
    rt = runner
    if rt is not None and rt.idle():
        _finish_run(rt, stopped=stop_flag.is_set())
    try:
        root.after(200, _poll_run_state)
    except tk.TclError:
        pass


def _finish_run(rt, stopped=False):
    """收尾：输出汇总、清状态、恢复按钮，允许立即开始新一轮。"""
    global runner, bot
    with run_lock:
        if runner is not rt:
            return
        runner = None
    rt.finish(stopped=stopped, quiet_success=not stopped)
    bot = None
    login_running['v'] = False
    code_event.clear()
    code_value['v'] = ''
    _set_running_ui(False)


def _confirm_repeat_followup(done_count):
    """工作线程发起确认：弹窗投递主线程，Event 等待结果，可被停止打断。"""
    if root is None:
        return False

    answer = {'yes': False}
    decided = threading.Event()

    def ask():
        try:
            answer['yes'] = messagebox.askyesno(
                '重复填写确认',
                '检测到今天已经填写过 %d 家店铺的跟进记录，是否还需要再填写一次？'
                % done_count,
                parent=root,
            )
        except Exception:
            answer['yes'] = False
        finally:
            decided.set()

    _post_ui(ask)
    while not decided.wait(0.1):
        if stop_flag.is_set():
            return False
    return answer['yes']


def _wait_code_input(timeout=300):
    """最多等待短信验证码 5 分钟；停止时立即返回。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_flag.is_set():
            return ''
        if code_event.wait(0.25):
            return code_value['v']
    return ''


def _auto_followup_after_login(bot_obj, settings):
    """登录成功后读取店铺数量，并按勾选执行跟进记录。"""
    from followup_batch import FollowupBatch
    from shop_list_reader import ShopListReader

    auto_fill_all = bool(settings.get('auto_fill_all', True))
    show_shop_count = bool(settings.get('auto_shop_list', True))
    rt = runner
    if not auto_fill_all and not show_shop_count:
        return {'success': True, 'skipped': True}

    debug_log = _make_debug_log('shop_list_run')
    reader = ShopListReader(
        bot_obj, log, debug=debug_log, runtime=rt
    )
    log('开始获取店铺数')
    read_result = reader.run()
    shops = reader.shops
    if not read_result.get('success') or not shops:
        log('获取店铺数失败，未读取到店铺列表')
        return {'success': False, 'count': len(shops)}
    log('获取店铺数%d个' % len(shops))

    if stop_flag.is_set():
        log('已取消，跳过跟进记录自动化')
        return {'success': False, 'count': len(shops), 'cancelled': True}
    if not auto_fill_all:
        log('已关闭自动填写跟进内容，仅完成店铺数量读取')
        return {'success': True, 'count': len(shops), 'skipped': True}

    batch = FollowupBatch(
        bot_obj, raw_log, styled_log, debug_log, runtime=rt
    )
    batch.reader = reader
    with _step('统计今日已完成记录', '仅在本地判断是否重复填写') as outcome:
        today_ids = batch.load_done_ids() | batch.today_visit_ids(shops)
        already = [shop for shop in shops if batch.shop_ids(shop) & today_ids]
        outcome['value'] = '已完成 %d 家，待处理 %d 家' % (
            len(already), max(0, len(shops) - len(already)),
        )

    repeat_followup = False
    if already:
        pending_count = max(0, len(shops) - len(already))
        log('\u68c0\u6d4b\u5230\u4eca\u5929\u5df2\u6709\u8ddf\u8fdb\u8bb0\u5f55\uff1a%d\u5bb6\uff0c\u5f85\u5904\u7406%d\u5bb6' % (
            len(already), pending_count,
        ))
        if not _confirm_repeat_followup(len(already)):
            log('\u7528\u6237\u62d2\u7edd\u91cd\u590d\u586b\u5199\uff0c\u672c\u6b21\u8ddf\u8fdb\u4efb\u52a1\u5df2\u53d6\u6d88')
            return {
                'success': False, 'count': len(shops), 'refused': True,
                'already': len(already), 'pending': pending_count,
            }
        repeat_followup = True
        log('\u7528\u6237\u9009\u62e9\u7ee7\u7eed\uff0c\u672c\u6b21\u5141\u8bb8\u91cd\u590d\u586b\u5199\u5df2\u6709\u8bb0\u5f55\u7684\u5e97\u94fa')
    if stop_flag.is_set():
        log('已取消，跳过跟进内容填写')
        return {'success': False, 'count': len(shops), 'cancelled': True}

    dry_run = bool(settings.get('fill_all_dry', False))
    workers = max(1, min(10, int(settings.get('workers', config.DEFAULT_WORKERS))))
    fill_debug = _make_debug_log('fill_all')
    skip_done = not repeat_followup
    batch_done_ids = set() if repeat_followup else today_ids
    fill_debug('全量跟进：workers=%d dry_run=%s skip_done=%s' % (
        workers, dry_run, skip_done
    ))
    fill_batch = FollowupBatch(
        bot_obj, raw_log, styled_log, fill_debug,
        dry_run=dry_run, runtime=rt,
    )
    fill_batch.reader = reader
    executor = DaemonThreadPoolExecutor(
        max_workers=workers, thread_name_prefix='YouzanBatch'
    )
    if rt is not None:
        rt.attach_executor(executor)
    log('开始填写跟进内容')
    with _step(
        '填写跟进内容', '共 %d 家，并发 %d，试运行=%s' % (
            len(shops), workers, dry_run
        ), quiet_success=True,
    ) as outcome:
        result = fill_batch.run(
            all_shops=True,
            workers=workers,
            skip_done=skip_done,
            shops=shops,
            announce_count=False,
            done_ids=batch_done_ids,
            executor=executor,
        )
        outcome['value'] = '成功 %d 家，失败 %d 家，终止 %d 家' % (
            result.get('success', 0),
            result.get('failed', 0),
            result.get('cancelled', 0),
        )
    fill_debug('全量跟进结果：%s' % result)
    log('跟进内容处理完成：成功%d家，失败%d家，终止%d家' % (
        result.get('success', 0), result.get('failed', 0),
        result.get('cancelled', 0),
    ))
    return {
        'success': result.get('failed', 0) == 0 and result.get('cancelled', 0) == 0,
        'count': len(shops),
        **result,
    }


def login_worker(user, pwd, settings):
    """一次完整登录：验证工作台后才宣布成功，再执行选中的后续步骤。"""
    global bot
    rt = runner
    b = None
    login_debug = _make_debug_log('login')
    try:
        if not fresh_login_requested['v'] and _ck_token_exist():
            b = YouzanBot(
                login_debug, proxy_server=settings.get('proxy_server')
            )
            b.bind_runtime(rt)
            bot = b
            mfree_ok = False
            try:
                mfree_ok = b.load_cookies_and_go()
            except TaskCancelled:
                raise
            except Exception as exc:
                login_debug('免密登录异常：%s' % str(exc)[:240])
            if mfree_ok and b.confirm_workbench(timeout=2):
                fresh_login_requested['v'] = False
                log('成功登录工作台')
                _save_login_config(user, pwd, settings)
                followup = _auto_followup_after_login(b, settings)
                return bool(followup.get('success', True))
            log('本地登录态不可用，转账号密码登录')
            try:
                b.close()
            except Exception:
                pass
            b = None
            bot = None

        if stop_flag.is_set():
            raise TaskCancelled()

        b = YouzanBot(
            login_debug, proxy_server=settings.get('proxy_server')
        )
        b.bind_runtime(rt)
        bot = b
        ok = b.start_login(user, pwd)
        if not ok:
            log('登录失败：账号密码、验证码或登录页面状态未通过校验')
            return False

        if b.needs_sms:
            if not b.send_sms():
                log('未能确认短信验证码已发送，本次登录失败')
                return False
            log('短信验证码已发送，请输入验证码（有效期5分钟）')
            _post_ui(enable_code_ui)
            deadline = time.monotonic() + 300
            with _step('等待短信验证码', '有效期5分钟，超时自动终止全部任务') as outcome:
                code = _wait_code_input(timeout=max(0, deadline - time.monotonic()))
                outcome['value'] = '已收到验证码' if code else '未收到验证码'
            if not code:
                if stop_flag.is_set():
                    log('短信验证等待已取消')
                else:
                    log('短信验证码等待超过5分钟，强制终止全部线程，请重新开始登录')
                    if rt is not None:
                        rt.stop()
                    else:
                        stop_flag.set()
                return False
            submit_result = b.submit_code(code)
            if submit_result is not True:
                log('短信验证码未通过，未确认进入工作台；请重新开始登录')
                return False

        fresh_login_requested['v'] = False
        if not b.confirm_workbench(timeout=8):
            log('登录失败：未确认成功进入工作台，不执行后续步骤')
            return False

        log('成功登录工作台')
        _save_login_config(user, pwd, settings)
        followup = _auto_followup_after_login(b, settings)
        return bool(followup.get('success', True))
    except TaskCancelled:
        log('登录任务已终止')
        return False
    except Exception as exc:
        safe_error = str(exc)
        if pwd:
            safe_error = safe_error.replace(pwd, '***')
        log('登录流程失败：%s' % safe_error[:300])
        return False
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass
        bot = None


def _mfree_login_task(user, pwd, settings):
    """一键免密登录任务：验证工作台后再执行后续流程。"""
    global bot
    rt = runner
    b = None
    login_debug = _make_debug_log('login')
    try:
        b = YouzanBot(
            login_debug, proxy_server=settings.get('proxy_server')
        )
        b.bind_runtime(rt)
        bot = b
        ok = b.load_cookies_and_go() and b.confirm_workbench(timeout=2)
        if not ok:
            fresh_login_requested['v'] = True
            log('免密登录未能确认进入工作台，请点「开始登录」')
            return False

        fresh_login_requested['v'] = False
        log('成功登录工作台')
        _save_login_config(user, pwd, settings)
        followup = _auto_followup_after_login(b, settings)
        return bool(followup.get('success', True))
    except TaskCancelled:
        log('免密登录任务已终止')
        return False
    except Exception as exc:
        fresh_login_requested['v'] = True
        safe_error = str(exc).replace(pwd, '***') if pwd else str(exc)
        log('免密登录失败：%s' % safe_error[:300])
        return False
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass
        bot = None



def _mask_account(account):
    """账号脱敏：保留前 3 位与后 4 位，短账号只保留首位。"""
    if len(account) <= 7:
        return account[:1] + '***'
    return account[:3] + '****' + account[-4:]


def on_start():
    """点开始登录：只读界面账号/密码框，去空格后为空则红字提示并中止。"""
    read_started = time.perf_counter()
    user = (entry_user.get() or '').strip()
    pwd = (entry_pwd.get() or '').strip()
    if not user or not pwd:
        log_error('账号或密码为空')
        return
    if login_running['v']:
        log('任务运行中，请先点【全部终止】')
        return
    fresh_login_requested['v'] = False
    code_event.clear()
    code_value['v'] = ''
    entry_code.delete(0, tk.END)
    rt = _start_runtime()
    read_elapsed = time.perf_counter() - read_started
    rt.emit('▶ 开始:读取账号密码，关键入参:来源=界面输入框，不弹窗不二次输入')
    rt.emit('✔ 完成:读取账号密码，关键结果:账号=%s 密码=***，耗时 %.4f 秒' % (
        _mask_account(user), read_elapsed
    ))
    settings = _read_ui_settings()
    rt.submit('\u8d26\u53f7 %s' % _mask_account(user), login_worker, user, pwd, settings)


def on_submit():
    code = entry_code.get().strip()
    if not code.isascii() or not code.isdigit() or len(code) != 6:
        messagebox.showwarning('提示', '请输入 6 位数字验证码')
        return
    code_value['v'] = code
    code_event.set()
    btn_submit.config(state='disabled')  # 一次性


def on_ck():
    """一键免密登录：同样走线程池，受并发上限与停止事件约束。"""
    if login_running['v']:
        return
    if not _ck_token_exist():
        log_error('没有本地登录凭据，请先点「开始登录」')
        return
    user = (entry_user.get() or '').strip()
    pwd = (entry_pwd.get() or '').strip()
    settings = _read_ui_settings()
    rt = _start_runtime()
    rt.submit('\u514d\u5bc6\u767b\u5f55', _mfree_login_task, user, pwd, settings)


def on_close():
    """关闭窗口：只发停止信号，不做阻塞式 join。"""
    rt = runner
    if rt is not None:
        rt.stop()
    else:
        stop_flag.set()
    try:
        root.destroy()
    except tk.TclError:
        pass


def run_gui():
    global root, entry_user, entry_pwd, entry_code, log_widget
    global btn_start, btn_submit, btn_ck, btn_stop
    global auto_shop_list_var, auto_fill_all_var, fill_all_dry_var, worker_count_var
    global proxy_server_var
    root = tk.Tk()
    root.title('有赞后台自动登录（滑块+短信+抓CK）')
    root.geometry('760x700')

    frm = tk.Frame(root)
    frm.pack(fill='x', padx=10, pady=8)

    tk.Label(frm, text='账号').grid(row=0, column=0, sticky='e')
    entry_user = tk.Entry(frm, width=24)
    entry_user.grid(row=0, column=1, padx=4)

    tk.Label(frm, text='密码').grid(row=1, column=0, sticky='e')
    entry_pwd = tk.Entry(frm, width=24, show='*')
    entry_pwd.grid(row=1, column=1, padx=4)

    tk.Label(frm, text='验证码').grid(row=2, column=0, sticky='e')
    entry_code = tk.Entry(frm, width=24, state='disabled')
    entry_code.grid(row=2, column=1, padx=4)

    btn_start = tk.Button(frm, text='开始登录', command=on_start, width=12)
    btn_start.grid(row=0, column=2, padx=6)
    btn_stop = tk.Button(
        frm,
        text='全部终止',
        command=on_stop_all,
        width=12,
        state='disabled',
        bg='#D40000',
        fg='white',
        activebackground='#B00000',
        activeforeground='white',
        disabledforeground='#F5C2C2',
    )
    btn_stop.grid(row=1, column=2, padx=6)
    btn_submit = tk.Button(frm, text='提交验证码', command=on_submit, width=12, state='disabled')
    btn_submit.grid(row=2, column=2, padx=6)
    btn_ck = tk.Button(frm, text='一键免密登录', command=on_ck, width=12)
    btn_ck.grid(row=3, column=2, padx=6, pady=4)

    auto_shop_list_var = tk.BooleanVar(value=config.DEFAULT_AUTO_SHOP_LIST)
    tk.Checkbutton(
        frm,
        text='登录后读取店铺数量（只读）',
        variable=auto_shop_list_var,
    ).grid(row=3, column=0, columnspan=2, sticky='w', pady=4)

    auto_fill_all_var = tk.BooleanVar(value=config.DEFAULT_AUTO_FILL_ALL)
    tk.Checkbutton(
        frm,
        text='登录后统计店铺并填写全部跟进记录（默认开启）',
        variable=auto_fill_all_var,
    ).grid(row=4, column=0, columnspan=2, sticky='w', pady=4)

    fill_all_dry_var = tk.BooleanVar(value=config.DEFAULT_FILL_ALL_DRY)
    tk.Checkbutton(
        frm, text='全量跟进试运行（只填表不发布）',
        variable=fill_all_dry_var,
    ).grid(row=5, column=0, sticky='w', pady=4)

    tk.Label(frm, text='并发线程').grid(row=5, column=1, sticky='e', padx=4)
    worker_count_var = tk.StringVar(value=str(config.DEFAULT_WORKERS))
    ttk.Combobox(
        frm,
        textvariable=worker_count_var,
        values=tuple(str(index) for index in range(1, 11)),
        width=5,
        state='readonly',
    ).grid(row=5, column=2, sticky='w', padx=6)

    tk.Label(frm, text='本地代理').grid(row=6, column=0, sticky='e', padx=4, pady=4)
    proxy_server_var = tk.StringVar(value=config.DEFAULT_PROXY_SERVER)
    tk.Entry(frm, textvariable=proxy_server_var, width=42).grid(
        row=6, column=1, columnspan=2, sticky='w', padx=4, pady=4
    )

    tk.Label(root, text='运行日志（登录、验证码、工作台和跟进进度）').pack(anchor='w', padx=10)
    log_widget = scrolledtext.ScrolledText(root, height=26, font=('Consolas', 10))
    log_widget.pack(fill='both', expand=True, padx=10, pady=6)
    log_widget.bind('<Button-3>', _show_log_context_menu)
    log_widget.bind('<Control-c>', lambda _event: (_copy_log_selection(), 'break')[1])
    log_widget.tag_configure(
        'followup_success', foreground='#D40000', font=('Consolas', 10, 'bold')
    )
    log_widget.tag_configure(
        'followup_fail', foreground='#008000', font=('Consolas', 10)
    )
    log_widget.tag_configure(
        'log_error', foreground='#FF0000', font=('Consolas', 10, 'bold')
    )

    root.protocol('WM_DELETE_WINDOW', on_close)
    # 主线程定时批量刷新日志 + 轮询任务收尾，工作线程只入队
    root.after(LOG_FLUSH_MS, _drain_log_queue)
    root.after(200, _poll_run_state)
    root.after_idle(_sync_saved_credentials_to_ui)
    log('日志文件：%s' % _ensure_log_file())
    if config.LOCAL_PASSWORD_MIGRATION_REQUIRED:
        log_error('\u672c\u5730\u65e7\u7248\u660e\u6587\u5bc6\u7801\u4e0d\u4f1a\u81ea\u52a8\u4f7f\u7528\uff1b\u8bf7\u91cd\u65b0\u8f93\u5165\u5bc6\u7801\uff0c\u767b\u5f55\u6210\u529f\u540e\u5c06\u4ee5 Windows DPAPI \u4fdd\u62a4\u4fdd\u5b58')
    root.mainloop()
    try:
        if _LOG_FILE['handle'] is not None:
            _LOG_FILE['handle'].close()
    except Exception:
        pass


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
