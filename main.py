'''
入口主程序 main.py —— 有赞后台自动登录 GUI。
运行：python main.py                  （图形界面）
      python main.py --smoke          （无 GUI，无头冒烟：登录+自动滑块到短信阶段即退出）
      python main.py --test-followup      （真实跑指定店铺跟进记录测试用例，会发布）
      python main.py --list-shops         （读取「我的店铺」全部店铺并输出日志，只读）
      python main.py --fill-first3        （填写第一页前3家店铺的跟进记录并发布）
      python main.py --fill-first3-dry    （只填写前3家，不发布）
      python main.py --fill-all           (固定单线程填写并发布)
      python main.py --fill-all --skip 3  (跳过前3家，避免重复)

登录流程：1
  - 点「开始登录」：若本地有 CK 或完整 auth_state，先执行免密登录；失败再转常规登录。
  - 常规登录如需短信：自动发送；输入验证码点「提交验证码」。提交失败(一次性)即废弃本次登录并点亮「开始登录」。
  - 再次点「开始登录」：先结束所有正在运行的登录服务(worker+浏览器)，再重启登录。

线程模型：所有 Playwright 操作都在 worker 线程里执行；GUI 线程只发信号(Event)，绝不直接调 Playwright
         （Playwright sync 连接绑定线程，跨线程调用会报 "Playwright connection closed"）。
'''

import os
import sys
import json
import subprocess
_HERE = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
from pathlib import Path
from startup_support import (RUNTIME_DEFAULTS, install_startup_diagnostics, load_runtime_config, report_startup_error)
if getattr(sys, 'frozen', False):
    install_startup_diagnostics(Path(_HERE))
_RUNTIME_CONFIG_FILE = os.path.join(_HERE, 'project_config.json')
_RUNTIME_DEFAULTS = dict(RUNTIME_DEFAULTS)
try:
    _RUNTIME_CONFIG, _RUNTIME_CONFIG_SOURCE = load_runtime_config(Path(_HERE), frozen=bool(getattr(sys, 'frozen', False)), bundle_dir=Path(getattr(sys, '_MEIPASS', _HERE)))
except RuntimeError as _exc:
    if getattr(sys, 'frozen', False):
        report_startup_error(Path(_HERE), type(_exc), _exc, _exc.__traceback__)
    raise SystemExit(str(_exc)) from _exc

def _project_relative_path(value, default):
    value = str(value or default)
    if os.path.isabs(value):
        raise SystemExit('Project runtime paths must be relative: %s' % value)
    return value

_VENV_REL = _project_relative_path(_RUNTIME_CONFIG.get('venv_dir'), '.venv')
_VENV_PY_REL = _project_relative_path(_RUNTIME_CONFIG.get('python_executable_windows') if os.name == 'nt' else _RUNTIME_CONFIG.get('python_executable_posix'), _RUNTIME_DEFAULTS['python_executable_windows'])
_VENV = os.path.abspath(os.path.join(_HERE, _VENV_REL))
_VENV_PY = os.path.abspath(os.path.join(_VENV, _VENV_PY_REL))
if not getattr(sys, 'frozen', False):
    if os.path.isdir(_VENV) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(_VENV)):
        if not os.path.isfile(_VENV_PY):
            raise SystemExit('Project virtual-environment interpreter is missing: %s; run setup.ps1 to create .venv' % _VENV_PY)
        raise SystemExit(subprocess.call([_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))
if not getattr(sys, 'frozen', False):
    if os.path.abspath(sys.prefix) != _VENV and not os.path.isfile(_VENV_PY):
        raise SystemExit('Project .venv interpreter was not found: %s; run .\\setup.ps1 first' % _VENV_PY)
_PY_DIR = os.path.join(_HERE, 'py')
if _PY_DIR not in sys.path:
    sys.path.insert(0, _PY_DIR)
import contextlib
import queue
import time
import threading
import tkinter as tk
from tkinter import (scrolledtext, messagebox, ttk)
import config
from task_runtime import (DaemonThreadPoolExecutor, TaskCancelled, TaskRuntime)
from youzan_bot import YouzanBot
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
root = entry_user = entry_pwd = entry_code = None
btn_start = btn_submit = btn_ck = btn_stop = log_widget = None
auto_shop_list_var = None
auto_fill_all_var = None
followup_clue_var = None
followup_customer_var = None
followup_shop_var = None
worker_count_var = None
bot = None
runner = None
run_lock = threading.Lock()
login_running = {'v': False}
stop_flag = threading.Event()
code_event = threading.Event()
code_value = {'v': ''}
fresh_login_requested = {'v': False}
log_queue = queue.Queue()
ui_queue = queue.Queue()
LOG_FLUSH_MS = 150
_LOG_FILE = {'handle': None, 'lock': threading.Lock(), 'path': ''}

def _ensure_log_file():
    '''懒创建 LOG/run_YYYYmmdd_HHMMSS.txt，返回文件路径。'''

    with _LOG_FILE['lock']:
        if _LOG_FILE['handle'] is None:
            config.ensure_dirs()
            path = os.path.join(config.LOG_DIR, 'run_%s.txt' % time.strftime('%Y%m%d_%H%M%S'))
            _LOG_FILE['handle'] = open(path, 'a', encoding='utf-8')
            _LOG_FILE['path'] = path
        return _LOG_FILE['path']

def _log_sink(line):
    '''实时写盘：任何线程调用都安全，逐行 flush。'''

    _ensure_log_file()
    with _LOG_FILE['lock']:
        handle = _LOG_FILE['handle']
        try:
            handle.write(str(line).rstrip('\n') + '\n')
            handle.flush()
            return
        except Exception:
            pass

def log(msg):
    '''线程安全日志：工作线程只入队，由主线程批量刷新。'''

    rt = runner
    if rt is not None:
        rt.emit(msg)
        return
    line = f'[{time.strftime('%H:%M:%S')!s}] {msg!s}'
    if log_widget is not None:
        log_queue.put((line, 'normal'))
    _log_sink(line)
    try:
        print(line, flush=True)
        return
    except Exception:
        return

def raw_log(msg):
    '''不加时间戳的原始输出，用于严格的店铺块格式。'''

    text = str(msg).rstrip('\n')
    if log_widget is not None:
        log_queue.put((text, 'normal'))
    _log_sink(text)
    try:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()
        return
    except Exception:
        return

def styled_log(text, kind):
    '''状态行着色：成功红粗、失败绿色、错误红字，均走日志队列。'''

    tag = {'success': 'followup_success', 'fail': 'followup_fail', 'error': 'log_error'}.get(kind, 'followup_fail')
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
    color = '\x1b[1;31m' if kind == 'success' else '\x1b[32m'
    try:
        sys.stdout.write(color + text + '\x1b[0m\n')
        sys.stdout.flush()
    except Exception:
        return

def _copy_log_selection():
    '''Copy the selected log text to the system clipboard.'''

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
        return

def _clear_log_widget():
    '''Clear only the visible log widget; the on-disk log remains intact.'''

    if log_widget is None:
        return
    try:
        log_widget.configure(state='normal')
        log_widget.delete('1.0', tk.END)
        log_widget.configure(state='disabled')
        return
    except tk.TclError:
        return

def _show_log_context_menu(event):
    '''Show right-click actions for the visible log area.'''

    if log_widget is None:
        return 'break'
    menu = tk.Menu(log_widget, tearoff=False)
    try:
        has_selection = bool(log_widget.tag_ranges(tk.SEL))
    except tk.TclError:
        has_selection = False
    menu.add_command(label='复制', command=_copy_log_selection, state='normal' if has_selection else 'disabled')
    menu.add_command(label='清空', command=_clear_log_widget)
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return 'break'

def _disable_ime_for_entry(event=None):
    """Disable Windows IME composition for password-style English input."""
    widget = event.widget if event is not None else None
    if widget is None or os.name != 'nt':
        return
    context = None
    try:
        import ctypes
        hwnd = widget.winfo_id()
        imm32 = ctypes.WinDLL('imm32', use_last_error=True)
        imm32.ImmGetContext.restype = ctypes.c_void_p
        imm32.ImmGetContext.argtypes = [ctypes.c_void_p]
        imm32.ImmSetOpenStatus.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        imm32.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        context = imm32.ImmGetContext(hwnd)
        if context:
            imm32.ImmSetOpenStatus(context, False)
    except (AttributeError, OSError, tk.TclError):
        # On systems without IMM support, retain normal entry behavior.
        return
    finally:
        if context:
            try:
                imm32.ImmReleaseContext(hwnd, context)
            except Exception:
                pass


def _append(line):
    '''仅主线程调用：向日志框追加一行。'''

    log_widget.configure(state='normal')
    log_widget.insert(tk.END, line + '\n')
    log_widget.see(tk.END)
    log_widget.configure(state='disabled')

def _append_styled(text, tag):
    '''仅主线程调用：向日志框追加带颜色标签的一行。'''

    log_widget.configure(state='normal')
    log_widget.insert(tk.END, text + '\n', tag)
    log_widget.see(tk.END)
    log_widget.configure(state='disabled')

def _drain_log_queue():
    '''主线程批量刷新日志：每 150ms 取一次队列，攒批插入再滚动到底部。'''

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
        return

def _post_ui(action):
    '''工作线程投递控件操作，绝不直接触碰 tkinter。'''

    ui_queue.put(action)

def _poll_ui_queue():
    '''主线程执行排队中的控件操作。'''

    for _ in range(50):
        try:
            action = ui_queue.get_nowait()
        except queue.Empty:
            return
        try:
            action()
        except Exception:
            continue

@contextlib.contextmanager
def _step(name, inputs='无', quiet_success=False):
    '''没有运行时的场景（如单测）退化为空上下文。'''

    rt = runner
    if rt is None:
        yield {'value': '完成'}
        return
    with rt.step(name, inputs, emit_start=not quiet_success, emit_success=not quiet_success) as outcome:
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
            kernel32.SetConsoleMode(handle, mode.value | 4)
            return
    except Exception:
        pass

def log_error(msg):
    '''错误提示：日志区红字显示，不弹窗。'''

    styled_log(str(msg), 'error')

def _make_debug_log(prefix):
    '''把运行细节写到独立文件，不污染店铺块输出。'''

    config.ensure_dirs()
    path = os.path.join(config.TXT_DIR, f'{prefix!s}_{time.strftime('%Y%m%d_%H%M%S')!s}.log')
    def debug_log(msg):
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(f'[{time.strftime('%H:%M:%S')!s}] {msg!s}\n')
        except Exception:
            return

    return debug_log

def _ck_token_exist():
    return os.path.exists(config.COOKIE_FILE) or os.path.exists(config.AUTH_FILE)

def enable_code_ui():
    '''短信已确认发送后启用验证码控件并将主窗口提到前台。'''

    try:
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        root.after(700, (lambda: root.attributes('-topmost', False)))
    except tk.TclError:
        pass
    entry_code.config(state='normal')
    btn_submit.config(state='normal')
    entry_code.focus_set()

def _read_workers():
    '''Return the only supported follow-up worker count: one.'''

    return 1

def _read_ui_settings():
    '''Read Tk variables on the Tk thread before handing work to workers.'''

    return {'auto_shop_list': bool(auto_shop_list_var and auto_shop_list_var.get()), 'auto_fill_all': bool(auto_fill_all_var and auto_fill_all_var.get()), 'followup_clue': bool(followup_clue_var and followup_clue_var.get()), 'followup_customer': bool(followup_customer_var and followup_customer_var.get()), 'followup_shop': bool(followup_shop_var and followup_shop_var.get()), 'workers': _read_workers()}

credential_ui_sync = {'ok': False, 'expected_user': '', 'expected_pwd': ''}

def _sync_saved_credentials_to_ui():
    '''Populate saved credentials after Tk controls exist and verify by reading them back.'''

    expected_user = str(config.DEFAULT_USER or '')
    expected_pwd = str(config.DEFAULT_PWD or '')
    credential_ui_sync.update({'ok': False, 'expected_user': expected_user, 'expected_pwd': expected_pwd})
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
        log_error(f'Credential UI read-back failed: username_equal={actual_user == expected_user!s} password_equal={actual_pwd == expected_pwd!s}')
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
    '''Save local GUI settings after successful login; never log the password.'''

    if not user or not pwd:
        return
    try:
        config.save_local_config(user, pwd, auto_shop_list=settings.get('auto_shop_list', True), auto_fill_all=settings.get('auto_fill_all', True), followup_clue=settings.get('followup_clue', False), followup_customer=settings.get('followup_customer', False), followup_shop=settings.get('followup_shop', True))
        log('本机配置已保存：账号、密码及界面选项（仅本地文件，不上传）')
        return
    except Exception as exc:
        log('本机配置保存失败：%r' % exc)

def _set_running_ui(running):
    '''统一按钮状态：运行中禁止重复提交，终止按钮仅运行时可用。'''

    state = 'disabled' if running else 'normal'
    btn_start.config(state=state)
    if btn_ck:
        btn_ck.config(state=state)
    if btn_stop:
        btn_stop.config(state='normal' if running else 'disabled')
    btn_submit.config(state='disabled')
    entry_code.config(state='disabled')

def _start_runtime():
    '''按当前下拉值创建本轮运行时，并发上限严格等于所选值。'''

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
    '''【全部终止】：置位停止事件并取消排队任务，不弹二次确认。'''

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
        return
    stop_flag.set()

def _poll_run_state():
    '''主线程轮询收尾：所有 future 结束后汇总，不阻塞界面。'''

    rt = runner
    if rt is not None and rt.idle():
        _finish_run(rt, stopped=stop_flag.is_set())
    try:
        root.after(200, _poll_run_state)
        return
    except tk.TclError:
        return

def _finish_run(rt, stopped=False):
    '''收尾：输出汇总、清状态、恢复按钮，允许立即开始新一轮。'''

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

def _wait_code_input(timeout=300):
    '''最多等待短信验证码 5 分钟；停止时立即返回。'''

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_flag.is_set():
            return ''
        if code_event.wait(0.25):
            return code_value['v']
    return ''

def _run_crm_menu_followup(bot_obj, target_kind, settings):
    '''Run CRM clue/customer follow-up using API by default, with explicit UI fallback.'''

    from crm_followup_batch import CrmFollowupBatch
    from crm_list_reader import CrmListReader, TARGETS
    label = TARGETS[target_kind]['label']
    debug_log = _make_debug_log('%s_list_run' % target_kind)
    mode = str(settings.get('crm_followup_mode') or config.CRM_FOLLOWUP_MODE).strip().lower()
    if mode == 'ui':
        reader = CrmListReader(bot_obj, target_kind, log, debug=debug_log, runtime=runner)
        log('开始%s跟进记录' % label)
        read_result = reader.run()
        items = reader.items
        if not read_result.get('success') or not items:
            log('%s列表读取失败，未获取到项目' % label)
            return {'success': False, 'count': 0, 'failed': 1}
        if stop_flag.is_set():
            return {'success': False, 'count': len(items), 'cancelled': True}
        result = CrmFollowupBatch(bot_obj, target_kind, raw_log, styled_log, debug_log, runtime=runner).run(items)
        count = len(items)
    else:
        from crm_api import CrmApiFollowupBatch
        result = CrmApiFollowupBatch(bot_obj, target_kind, log, debug=debug_log, runtime=runner).run()
        count = result.get('total', result.get('attempted', 0))
        if result.get('total', 0) != result.get('attempted', 0):
            log('%s接口数量不一致：total=%d attempted=%d' % (label, result.get('total', 0), result.get('attempted', 0)))
            result['failed'] = max(1, result.get('failed', 0))
    log('%s跟进处理完成：成功%d条，失败%d条' % (label, result.get('success', 0), result.get('failed', 0)))
    return {'success': result.get('failed', 0) == 0 and result.get('attempted', 0) == count, 'count': count, **result}

def _followup_batch_step_inputs(total):
    '''Return readable step details for the serialized follow-up batch.'''

    return '共 %d 家，单线程顺序处理' % total

def _auto_followup_after_login(bot_obj, settings):
    '''登录成功后读取店铺数量，并按勾选执行跟进记录。'''

    from followup_batch import FollowupBatch
    from shop_list_reader import ShopListReader
    auto_fill_all = bool(settings.get('auto_fill_all', True))
    show_shop_count = bool(settings.get('auto_shop_list', True))
    selected = {'clue': bool(settings.get('followup_clue', False)), 'customer': bool(settings.get('followup_customer', False)), 'shop': bool(settings.get('followup_shop', True))}
    rt = runner
    if not auto_fill_all:
        selected['clue'] = selected['customer'] = selected['shop'] = False
    if not any(selected.values()) and not show_shop_count:
        return {'success': True, 'skipped': True}
    aggregate = {'success': True, 'targets': {}}
    if auto_fill_all and any(selected.values()):
        log(f'跟进内容：\n{chr(96) * 3!s}\n{'\n'.join(('跟进方式：电话拜访', '跟进目的：了解需求', '沟通情况：电话打通，有交流', '客户需求/备注：有需要会联系'))!s}\n{chr(96) * 3!s}')
    for target_kind in ('clue', 'customer'):
        if selected[target_kind]:
            target_result = _run_crm_menu_followup(bot_obj, target_kind, settings)
            aggregate['targets'][target_kind] = target_result
            aggregate['success'] = aggregate['success'] and bool(target_result.get('success'))
    if not selected['shop']:
        if not selected['clue'] and not selected['customer']:
            log('跟进记录菜单未选择任何区域')
        return aggregate
    debug_log = _make_debug_log('shop_list_run')
    reader = ShopListReader(bot_obj, log, debug=debug_log, runtime=rt)
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
    batch = FollowupBatch(bot_obj, raw_log, styled_log, debug_log, runtime=rt)
    batch.reader = reader
    with _step('统计今日已完成记录', '仅在本地判断是否重复填写') as outcome:
        today_ids = batch.load_done_ids() | batch.today_visit_ids(shops)
    already = [shop for shop in shops if batch.shop_ids(shop) & today_ids]
    outcome['value'] = '已完成 %d 家，待处理 %d 家' % (len(already), max(0, len(shops) - len(already)))
    pending_count = max(0, len(shops) - len(already))
    if already:
        log('检测到今天已有跟进记录：%d家，待处理%d家' % (len(already), pending_count))
        skipped_items = [(position, shop) for position, shop in enumerate(shops, 1)
             if batch.shop_ids(shop) & today_ids]
        stop_event = rt.stop_event if rt is not None else stop_flag
        emit_progress = getattr(rt, 'emit_progress', None) if rt is not None else None
        for skipped_index, (position, shop) in enumerate(skipped_items):
            if skipped_index and stop_event.wait(config.FOLLOWUP_SKIP_INTERVAL_SECONDS):
                return {'success': False, 'count': len(shops), 'cancelled': True}
            shop_id = shop.get('kdt_id') or shop.get('shop_id') or '-'
            message = '跳过 条目店铺ID=%s：今日已填写跟进' % shop_id
            if callable(emit_progress):
                emit_progress(message, position, len(shops))
            else:
                log(message)
        shops = [shop for shop in shops if not batch.shop_ids(shop) & today_ids]
        log('今日已填写跟进自动跳过%d家' % len(already))
    if stop_flag.is_set():
        log('已取消，跳过跟进内容填写')
        return {'success': False, 'count': len(shops), 'cancelled': True}
    requested_workers = settings.get('workers', 1)
    workers = 1
    if str(requested_workers) != '1':
        log('跟进记录固定使用单线程，已忽略其他线程设置')
    fill_debug = _make_debug_log('fill_all')
    fill_batch = FollowupBatch(bot_obj, raw_log, styled_log, fill_debug, runtime=rt)
    fill_batch.reader = reader
    executor = DaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix='YouzanBatch')
    if rt is not None:
        rt.attach_executor(executor)
    log('开始填写跟进内容')
    with _step('填写跟进内容', _followup_batch_step_inputs(len(shops))) as outcome:
        result = fill_batch.run(all_shops=True, workers=workers, skip_done=False, shops=shops, announce_count=False, done_ids=set(), executor=executor)
        outcome['value'] = '成功 %d 家，失败 %d 家，终止 %d 家' % (result.get('success', 0), result.get('failed', 0), result.get('cancelled', 0))
    fill_debug('全量跟进结果：%s' % result)
    log('跟进内容处理完成：成功%d家，失败%d家，终止%d家' % (result.get('success', 0), result.get('failed', 0), result.get('cancelled', 0)))
    shop_result = {'success': result.get('failed', 0) == 0 and result.get('cancelled', 0) == 0, 'count': len(shops), **result}
    aggregate['targets']['shop'] = shop_result
    aggregate['success'] = aggregate['success'] and bool(shop_result.get('success'))
    result = dict(aggregate)
    result.update({key: value for key, value in shop_result.items() if key != 'success'})
    return result

def login_worker(user, pwd, settings):
    """Validate credentials against the live login flow before declaring success."""
    global bot
    runtime = runner
    browser_bot = None
    debug_log = _make_debug_log('login')
    try:
        if stop_flag.is_set():
            raise TaskCancelled()
        # Start Login always verifies entered credentials; only the separate one-click action reuses cookies.
        browser_bot = YouzanBot(debug_log)
        browser_bot.bind_runtime(runtime)
        bot = browser_bot
        if not browser_bot.start_login(user, pwd):
            log('\u8d26\u53f7\u5bc6\u7801\u767b\u5f55\u5931\u8d25\uff1a\u767b\u5f55\u9875\u672a\u786e\u8ba4\u51ed\u636e\u6709\u6548')
            return False
        if browser_bot.needs_sms:
            if not browser_bot.send_sms():
                log('\u672a\u80fd\u786e\u8ba4\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5df2\u53d1\u9001\uff0c\u767b\u5f55\u5931\u8d25')
                return False
            log('\u77ed\u4fe1\u9a8c\u8bc1\u7801\u5df2\u53d1\u9001\uff0c\u8bf7\u8f93\u5165\u9a8c\u8bc1\u7801')
            code = _wait_code_input(timeout=300)
            if not code:
                log('\u77ed\u4fe1\u9a8c\u8bc1\u7801\u7b49\u5f85\u8d85\u65f6\u6216\u4efb\u52a1\u5df2\u53d6\u6d88\uff0c\u767b\u5f55\u5931\u8d25')
                return False
            if browser_bot.submit_code(code) is not True:
                log('\u77ed\u4fe1\u9a8c\u8bc1\u7801\u6821\u9a8c\u5931\u8d25\u6216\u767b\u5f55\u72b6\u6001\u672a\u786e\u8ba4')
                return False
        if not browser_bot.confirm_workbench(timeout=12):
            log('\u767b\u5f55\u672a\u6210\u529f\uff1a\u672a\u786e\u8ba4\u8fdb\u5165\u5de5\u4f5c\u53f0')
            return False
        fresh_login_requested['v'] = False
        log('\u6210\u529f\u767b\u5f55\u5de5\u4f5c\u53f0')
        _save_login_config(user, pwd, settings)
        result = _auto_followup_after_login(browser_bot, settings)
        return bool(result.get('success', True))
    except TaskCancelled:
        log('\u767b\u5f55\u4efb\u52a1\u5df2\u7ec8\u6b62')
        return False
    except Exception as exc:
        detail = str(exc).replace(pwd, '***') if pwd else str(exc)
        log('\u767b\u5f55\u6d41\u7a0b\u5931\u8d25\uff1a%s' % detail[:300])
        return False
    finally:
        try:
            if browser_bot is not None:
                browser_bot.close()
        except Exception:
            pass
        bot = None


def _mfree_login_task(user, pwd, settings):
    """Keep the saved-login session alive until follow-up completes."""
    global bot
    rt = runner
    b = None
    login_debug = _make_debug_log('login')
    try:
        if stop_flag.is_set():
            raise TaskCancelled()
        b = YouzanBot(login_debug)
        b.bind_runtime(rt)
        bot = b
        if not (b.load_cookies_and_go() and b.confirm_workbench(timeout=2)):
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
    '''账号脱敏：保留前 3 位与后 4 位，短账号只保留首位。'''

    if len(account) <= 7:
        return account[:1] + '***'
    return account[:3] + '****' + account[-4:]

def on_start():
    '''点开始登录：只读界面账号/密码框，去空格后为空则红字提示并中止。'''

    read_started = time.perf_counter()
    user = (entry_user.get() or '').strip()
    pwd = (entry_pwd.get() or '').strip()
    if not user or not pwd:
        log_error('账号或密码为空')
        return
    if login_running['v']:
        log('任务运行中，请先点【全部终止】')
        return
    fresh_login_requested['v'] = True
    code_event.clear()
    code_value['v'] = ''
    entry_code.delete(0, tk.END)
    rt = _start_runtime()
    read_elapsed = time.perf_counter() - read_started
    rt.emit('▶ 开始:读取账号密码，关键入参:来源=界面输入框，不弹窗不二次输入')
    rt.emit('✔ 完成:读取账号密码，关键结果:账号=%s 密码=***，耗时 %.4f 秒' % (_mask_account(user), read_elapsed))
    settings = _read_ui_settings()
    rt.emit('账号密码登录：强制走实际凭据校验，不使用本地免密登录态')
    rt.submit('账号 %s' % _mask_account(user), login_worker, user, pwd, settings)

def on_submit():
    code = entry_code.get().strip()
    if not code.isascii() or not code.isdigit() or len(code) != 6:
        messagebox.showwarning('提示', '请输入 6 位数字验证码')
        return
    code_value['v'] = code
    code_event.set()
    btn_submit.config(state='disabled')

def on_ck():
    '''一键免密登录：同样走线程池，受并发上限与停止事件约束。'''

    if login_running['v']:
        return
    if not _ck_token_exist():
        log_error('没有本地登录凭据，请先点「开始登录」')
        return
    user = (entry_user.get() or '').strip()
    pwd = (entry_pwd.get() or '').strip()
    settings = _read_ui_settings()
    rt = _start_runtime()
    rt.submit('免密登录', _mfree_login_task, user, pwd, settings)

def on_close():
    '''关闭窗口：只发停止信号，不做阻塞式 join。'''

    rt = runner
    if rt is not None:
        rt.stop()
    else:
        stop_flag.set()
    try:
        root.destroy()
        return
    except tk.TclError:
        return

def run_gui(startup_check=False):
    global root, entry_user, entry_pwd, entry_code, btn_start, btn_submit, btn_ck, btn_stop, auto_shop_list_var, auto_fill_all_var, followup_clue_var, followup_customer_var, followup_shop_var, worker_count_var, log_widget
    root = tk.Tk()
    if startup_check:
        root.withdraw()
    root.report_callback_exception = sys.excepthook
    root.title('有赞后台自动登录（滑块+短信+抓CK）')
    root.option_add('*Font', ('Microsoft YaHei UI', 9))
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    window_width = min(max(580, int(screen_width * 0.78)), max(420, screen_width - 40))
    window_height = min(max(540, int(screen_height * 0.78)), max(420, screen_height - 80))
    pos_x = max(0, (screen_width - window_width) // 2)
    pos_y = max(0, (screen_height - window_height) // 2)
    root.geometry(f'{window_width}x{window_height}+{pos_x}+{pos_y}')
    root.minsize(min(window_width, 580), min(window_height, 520))
    layout = tk.Frame(root, padx=14, pady=12)
    layout.pack(fill='both', expand=True)
    login_group = tk.LabelFrame(layout, text='登录信息', padx=12, pady=10, bd=1, relief='groove')
    login_group.pack(fill='x', pady=(0, 12))
    login_group.grid_columnconfigure(1, weight=1)
    for row, label_text in enumerate(('账号', '密码', '验证码')):
        tk.Label(login_group, text=label_text, width=9, anchor='w').grid(row=row, column=0, sticky='w', padx=(0, 8), pady=4)
    entry_user = tk.Entry(login_group, width=36)
    entry_user.grid(row=0, column=1, sticky='ew', pady=4)
    entry_pwd = tk.Entry(login_group, width=36, show='*')
    entry_pwd.grid(row=1, column=1, sticky='ew', pady=4)
    entry_pwd.bind('<FocusIn>', _disable_ime_for_entry)
    entry_code = tk.Entry(login_group, width=36, state='disabled')
    entry_code.grid(row=2, column=1, sticky='ew', pady=4)
    button_group = tk.Frame(login_group)
    button_group.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 0))
    for column in range(4):
        button_group.grid_columnconfigure(column, weight=1, uniform='login_buttons')
    button_style = {'width': 15, 'height': 1, 'padx': 6, 'pady': 5, 'relief': 'solid', 'bd': 1, 'font': ('Microsoft YaHei UI', 9)}
    btn_start = tk.Button(button_group, text='开始登录', command=on_start, bg='#1769AA', fg='white', activebackground='#12578F', activeforeground='white', default='active', **button_style)
    btn_start.grid(row=0, column=0, sticky='ew', padx=(0, 8))
    btn_submit = tk.Button(button_group, text='提交验证码', command=on_submit, state='disabled', bg='white', fg='#333333', **button_style)
    btn_submit.grid(row=0, column=1, sticky='ew', padx=(0, 8))
    btn_ck = tk.Button(button_group, text='一键免密登录', command=on_ck, bg='white', fg='#333333', **button_style)
    btn_ck.grid(row=0, column=2, sticky='ew', padx=(0, 8))
    btn_stop = tk.Button(button_group, text='全部终止', command=on_stop_all, state='disabled', bg='#FDECEC', fg='#A12626', activebackground='#F8D7DA', activeforeground='#8A1C1C', **button_style)
    btn_stop.grid(row=0, column=3, sticky='ew')
    options_group = tk.LabelFrame(layout, text='登录选项', padx=12, pady=8, bd=1, relief='groove')
    options_group.pack(fill='x', pady=(0, 12))
    auto_shop_list_var = tk.BooleanVar(value=config.DEFAULT_AUTO_SHOP_LIST)
    tk.Checkbutton(options_group, text='登录后读取店铺数量（只读）', variable=auto_shop_list_var, anchor='w').pack(anchor='w', pady=2)
    auto_fill_all_var = tk.BooleanVar(value=config.DEFAULT_AUTO_FILL_ALL)
    tk.Checkbutton(options_group, text='登录后统计店铺并填写全部跟进记录（默认开启）', variable=auto_fill_all_var, anchor='w').pack(anchor='w', pady=2)
    followup_frame = tk.LabelFrame(options_group, text='跟进记录', padx=8, pady=4, bd=1, relief='groove')
    followup_frame.pack(fill='x', padx=(20, 0), pady=(6, 0))
    followup_clue_var = tk.BooleanVar(value=config.DEFAULT_FOLLOWUP_CLUE)
    followup_customer_var = tk.BooleanVar(value=config.DEFAULT_FOLLOWUP_CUSTOMER)
    followup_shop_var = tk.BooleanVar(value=config.DEFAULT_FOLLOWUP_SHOP)
    for text, variable in ('线索', followup_clue_var), ('客户', followup_customer_var), ('店铺', followup_shop_var):
        tk.Checkbutton(followup_frame, text=text, variable=variable, anchor='w').pack(anchor='w', pady=2)
    worker_count_var = tk.StringVar(value='1')
    log_group = tk.LabelFrame(layout, text='运行日志', padx=8, pady=8, bd=1, relief='groove')
    log_group.pack(fill='both', expand=True)
    log_widget = scrolledtext.ScrolledText(log_group, height=12, font=('Consolas', 9), bg='#FFFFFF', fg='#222222', relief='solid', bd=1, wrap='word', state='disabled')
    log_widget.pack(fill='both', expand=True)
    log_widget.bind('<Button-3>', _show_log_context_menu)
    log_widget.bind('<Control-c>', (lambda _event: (_copy_log_selection(), 'break')[1]))
    log_widget.tag_configure('followup_success', foreground='#D40000', font=('Consolas', 9, 'bold'))
    log_widget.tag_configure('followup_fail', foreground='#008000', font=('Consolas', 9))
    log_widget.tag_configure('log_error', foreground='#FF0000', font=('Consolas', 9, 'bold'))
    root.protocol('WM_DELETE_WINDOW', on_close)
    root.after(LOG_FLUSH_MS, _drain_log_queue)
    root.after(200, _poll_run_state)
    root.after_idle(_sync_saved_credentials_to_ui)
    log('日志文件：%s' % _ensure_log_file())
    if config.LOCAL_PASSWORD_MIGRATION_REQUIRED:
        log_error('本地旧版明文密码不会自动使用；请重新输入密码，登录成功后将以 Windows DPAPI 保护保存')
    if startup_check:
        root.after(200, root.quit)
    root.mainloop()
    if startup_check:
        root.destroy()
    try:
        if _LOG_FILE['handle'] is not None:
            _LOG_FILE['handle'].close()
            return
    except Exception:
        pass

def run_smoke():
    '''无头冒烟：走登录+自动滑块，到短信阶段返回，用于验证软件可正常运行。'''

    b = YouzanBot(log)
    try:
        ok = b.start_login(config.DEFAULT_USER, config.DEFAULT_PWD)
        print('SMOKE_RESULT =', ok)
    finally:
        b.close()

def _wait_sms_code(timeout=600, logger=log):
    '''测试用例模式等待短信验证码：优先环境变量，其次 txt/sms_code.txt。'''

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
    '''CLI 通用登录：优先本地 CK+TOKEN，失败再账号密码/短信。失败返回 None。'''

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
            return
        if b.needs_sms:
            if not b.send_sms():
                logger('短信发送未能确认，任务终止')
                b.close()
                return
            code = _wait_sms_code(logger=logger)
            if not code:
                logger('未获取到短信验证码，任务终止')
                b.close()
                return
            if b.submit_code(code) is not True:
                logger('短信验证码未通过，任务终止')
                b.close()
                return
    return b

def run_crm_followup_cli(target_kind, count=2):
    '''Run a bounded CRM follow-up smoke flow using the real submit path.'''

    from crm_followup_batch import CrmFollowupBatch
    from crm_list_reader import CrmListReader, TARGETS
    if target_kind not in TARGETS:
        log('未知跟进目标：%s' % target_kind)
        return False
    debug_log = _make_debug_log('%s_self_test' % target_kind)
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        if str(config.CRM_FOLLOWUP_MODE).lower() == 'ui':
            reader = CrmListReader(b, target_kind, log, debug=debug_log)
            read_result = reader.run()
            items = reader.items[:max(1, int(count or 2))]
            if not read_result.get('success') or not items:
                log('%s列表读取失败' % TARGETS[target_kind]['label'])
                try:
                    b.close()
                except Exception:
                    pass
                try:
                    b.close()
                except Exception:
                    pass
                try:
                    b.close()
                except Exception:
                    pass
                return False
            result = CrmFollowupBatch(b, target_kind, raw_log, styled_log, debug_log).run(items)
        else:
            from crm_api import CrmApiFollowupBatch
            result = CrmApiFollowupBatch(b, target_kind, log, debug=debug_log).run(count=None if count is None else int(count))
        log('%s自测结束：成功%d条，失败%d条' % (TARGETS[target_kind]['label'], result.get('success', 0), result.get('failed', 0)))
    finally:
        try:
            b.close()
        except Exception:
            pass
    return result.get('attempted', 0) > 0 and result.get('failed', 0) == 0

def run_followup_cli():
    '''Run one real shop follow-up record as an explicitly requested smoke flow.'''

    from shop_followup import ShopFollowupCase
    b = _cli_login()
    if b is None:
        return False
    try:
        result = ShopFollowupCase(b, log).run()
        log(f'测试用例结束：success={result.get('success')!s} 接口={result.get('api_count', 0)!s} 右侧链接={result.get('links_right_side', 0)!s} 右侧功能点={result.get('functions_total', 0)!s}')
        log('FOLLOWUP_RESULT = %s' % result.get('success'))
    finally:
        try:
            b.close()
        except Exception:
            pass
    return bool(result.get('success'))

def run_fill_first3_cli():
    '''Fill the first three shops through the real publish path.'''

    from followup_batch import FollowupBatch
    debug_log = _make_debug_log('fill_first3')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = FollowupBatch(b, raw_log, styled_log, debug_log).run(3)
    finally:
        try:
            b.close()
        except Exception:
            pass
    return result.get('attempted', 0) > 0 and result.get('failed', 0) == 0

def run_fill_all_cli(skip=0, workers=1):
    '''Fill all shops serially; workers is retained only for CLI compatibility.'''

    from followup_batch import FollowupBatch
    debug_log = _make_debug_log('fill_all')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = FollowupBatch(b, raw_log, styled_log, debug_log).run(all_shops=True, skip=skip, workers=1)
    finally:
        try:
            b.close()
        except Exception:
            pass
    return result.get('attempted', 0) > 0 and result.get('failed', 0) == 0

def run_list_shops_cli():
    '''读取「我的店铺」列表，只输出店铺分割线块，运行细节写调试文件。'''

    from shop_list_reader import ShopListReader
    debug_log = _make_debug_log('shop_list_run')
    b = _cli_login(logger=debug_log)
    if b is None:
        return False
    try:
        result = ShopListReader(b, raw_log, debug=debug_log).run()
    finally:
        try:
            b.close()
        except Exception:
            pass
    return bool(result.get('success'))

def run_startup_check(check_browser=False):
    '''Exercise packaged dependencies and actual hidden GUI without logging in.'''

    import io
    import platform
    import numpy as np
    from PIL import Image
    from crm_api import CrmApiClient
    from crm_followup_batch import CrmFollowupBatch
    from crm_list_reader import CrmListReader
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'python': platform.python_version(), 'architecture': platform.machine(), 'runtime_config_source': _RUNTIME_CONFIG_SOURCE, 'image_ok': False, 'numpy_ok': False, 'gui_ok': False, 'browser_checked': bool(check_browser), 'browser_ok': None}
    config.ensure_dirs()
    output = Path(config.LOG_DIR) / 'startup_check.json'
    try:
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), 'white').save(buffer, format='PNG')
        buffer.seek(0)
        with Image.open(buffer) as image:
            image.verify()
        report['image_ok'] = True
        report['numpy_ok'] = int(np.arange(4).sum()) == 6
        run_gui(startup_check=True)
        report['gui_ok'] = bool(entry_user is not None and entry_pwd is not None)
        if check_browser:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=config.BROWSER_CHANNEL, headless=True)
            try:
                page = browser.new_page()
                page.set_content('<title>YouzanAuto startup check</title>')
                report['browser_ok'] = page.title() == 'YouzanAuto startup check'
            finally:
                browser.close()
    except Exception as exc:
        report.update(success=False, error_type=type(exc).__name__, error=str(exc))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['success'] = bool(report['image_ok'] and report['numpy_ok'] and report['gui_ok'] and (not check_browser or report['browser_ok']))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report['success']

if __name__ == '__main__':
    if '--startup-check' in sys.argv:
        if run_startup_check('--check-browser' in sys.argv):
            raise SystemExit(0)
        raise SystemExit(1)
    _skip = 0
    if '--skip' in sys.argv:
        try:
            _skip = int(sys.argv[sys.argv.index('--skip') + 1])
        except Exception:
            _skip = 0
    _workers = 1
    if '--workers' in sys.argv:
        log('跟进记录固定使用单线程，忽略 --workers 参数')
    if '--list-shops' in sys.argv:
        if run_list_shops_cli():
            raise SystemExit(0)
        raise SystemExit(1)
    if '--smoke' in sys.argv:
        run_smoke()
    else:
        run_gui()
