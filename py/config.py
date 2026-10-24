# -*- coding: utf-8 -*-
"""全局配置：URL、文件路径、默认账号、浏览器参数。集中修改，避免散落在各模块。"""
import base64
import ctypes
import json
import os
import tempfile
import time
from ctypes import wintypes

# 目录（config.py 在 py/ 下，项目根目录是它的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(BASE_DIR, 'py')
JSON_DIR = os.path.join(BASE_DIR, 'json')
HTML_DIR = os.path.join(BASE_DIR, 'html')
TXT_DIR = os.path.join(BASE_DIR, 'txt')
PNG_DIR = os.path.join(BASE_DIR, 'png')
LOG_DIR = os.path.join(BASE_DIR, 'LOG')   # 运行日志实时落盘目录（TXT）

# 页面地址（登录成功后统一落到工作台）
LOGIN_URL = 'https://account.youzan.com/login?redirectUrl=https://qudao.youzan.com#/workbench'
TARGET_URL = 'https://qudao.youzan.com/#/workbench'

# 落盘文件（CK / 完整登录态 / 短信码 / 页面源码）
COOKIE_FILE = os.path.join(JSON_DIR, 'cookies.json')
AUTH_FILE = os.path.join(JSON_DIR, 'auth_state.json')
SMS_CODE_FILE = os.path.join(TXT_DIR, 'sms_code.txt')

# 默认账号（可在界面里改）；凭据从环境变量读取，避免提交到仓库。
# Local GUI settings. The file is ignored by Git and stays on this computer.
LOCAL_CONFIG_FILE = os.path.join(JSON_DIR, 'local_config.json')


def _load_local_config():
    try:
        with open(LOCAL_CONFIG_FILE, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ('cbData', wintypes.DWORD),
        ('pbData', ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_transform(value, decrypt=False):
    """Protect/unprotect a secret with Windows user-scoped DPAPI."""
    if os.name != 'nt':
        return None
    raw = value if isinstance(value, bytes) else str(value).encode('utf-8')
    input_buffer = ctypes.create_string_buffer(raw)
    input_blob = _DataBlob(
        len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = _DataBlob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0,
            ctypes.byref(output_blob)
        )
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob), None, None, None, None, 0,
            ctypes.byref(output_blob)
        )
    if not ok:
        return None
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _protect_password(password):
    protected = _dpapi_transform(password, decrypt=False)
    if protected is None:
        raise RuntimeError('Windows DPAPI password protection failed')
    return 'dpapi:' + base64.b64encode(protected).decode('ascii')


def _unprotect_password(value):
    if not value:
        return ''
    if not str(value).startswith('dpapi:'):
        # Never treat legacy plaintext configuration as a usable credential.
        # The GUI asks the user to re-enter it; the normal save path uses DPAPI.
        return ''
    try:
        protected = base64.b64decode(str(value)[6:])
        raw = _dpapi_transform(protected, decrypt=True)
        return raw.decode('utf-8') if raw is not None else ''
    except (ValueError, TypeError, UnicodeError):
        return ''


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'y')
    if value is None:
        return default
    return bool(value)


_LOCAL_CONFIG = _load_local_config()

# Environment variables take precedence over the saved local settings.
DEFAULT_USER = os.environ.get('YOUZAN_USER') or str(_LOCAL_CONFIG.get('username', ''))
_LOCAL_PASSWORD_VALUE = _LOCAL_CONFIG.get(
    'password_protected', _LOCAL_CONFIG.get('password', '')
)
_PASSWORD_FROM_ENV = os.environ.get('YOUZAN_PASSWORD')
LOCAL_PASSWORD_MIGRATION_REQUIRED = bool(
    not _PASSWORD_FROM_ENV
    and _LOCAL_PASSWORD_VALUE
    and not str(_LOCAL_PASSWORD_VALUE).startswith('dpapi:')
)
DEFAULT_PWD = _PASSWORD_FROM_ENV or _unprotect_password(_LOCAL_PASSWORD_VALUE)
DEFAULT_AUTO_SHOP_LIST = _as_bool(_LOCAL_CONFIG.get('auto_shop_list'), True)
DEFAULT_AUTO_FILL_ALL = _as_bool(_LOCAL_CONFIG.get('auto_fill_all'), True)
DEFAULT_FILL_ALL_DRY = _as_bool(_LOCAL_CONFIG.get('fill_all_dry'), False)
DEFAULT_PROXY_SERVER = (
    os.environ.get('YOUZAN_PROXY_SERVER')
    or str(_LOCAL_CONFIG.get('proxy_server') or 'http://127.0.0.1:10808').strip()
)
try:
    DEFAULT_WORKERS = max(1, min(10, int(_LOCAL_CONFIG.get('workers', 3))))
except (TypeError, ValueError):
    DEFAULT_WORKERS = 3


def save_local_config(username, password, auto_shop_list=True,
                      auto_fill_all=True, fill_all_dry=False, workers=3,
                      proxy_server=None):
    """Atomically save ignored local GUI settings."""
    ensure_dirs()
    payload = {
        'username': str(username or ''),
        'password_protected': _protect_password(password or ''),
        'auto_shop_list': bool(auto_shop_list),
        'auto_fill_all': bool(auto_fill_all),
        'fill_all_dry': bool(fill_all_dry),
        'workers': max(1, min(10, int(workers))),
        'proxy_server': str(proxy_server or DEFAULT_PROXY_SERVER).strip(),
    }
    fd, temp_path = tempfile.mkstemp(
        prefix='.local_config_', suffix='.tmp', dir=JSON_DIR
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, LOCAL_CONFIG_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


HEADLESS = True                 # True=后台无浏览器窗口；False=弹窗调试用
BROWSER_CHANNEL = 'msedge'      # 复用系统 Edge，免下载 chromium
VIEWPORT = {'width': 1280, 'height': 800}

# 网络超时（毫秒）：Playwright 所有动作/导航都不能无限等待
ACTION_TIMEOUT_MS = 20000       # 单次点击/填表/等待元素
NAV_TIMEOUT_MS = 45000          # 页面跳转
DETAIL_NAV_TIMEOUT_MS = 30000   # 店铺详情（hash 路由，commit 即返回更快）
ASSERT_TIMEOUT_MS = 15000       # 断言类等待（wait_for_function / load_state）

# 重试与限速：失败指数退避，请求之间随机停顿，避免高频空转压垮对端与本机
RETRY_ATTEMPTS = 2              # 单个网络动作最大尝试次数
RETRY_BASE_DELAY = 0.6          # 首次退避秒数，之后按 2 倍递增
RATE_LIMIT_MIN = 1.0            # 店铺/请求之间的最小随机停顿（规避“操作太快”）
RATE_LIMIT_MAX = 2.2            # 最大随机停顿

# 发布接口等待（毫秒）：用 expect_response 事件驱动等待 /visit/save 响应。
# 不能用固定 sleep 轮询——同步 API 的事件循环只在 Playwright 调用期间运行。
PUBLISH_TIMEOUT_MS = 20000
PUBLISH_RESPONSE_GRACE_MS = 8000

# 店铺列表：接口已返回 detail_url 时，不再逐店点「详情」开弹窗解析（约省 2.4 秒/店）
SHOP_DETAIL_RESOLVE_BY_CLICK = False

# 截图/存档：默认只保留失败现场；成功路径跳过可显著缩短单店耗时
SAVE_STEP_SCREENSHOTS = False


def ensure_dirs():
    """确保所有归档目录存在；写入文件前调用，避免目录被手工删除后报错。"""
    for folder in (JSON_DIR, HTML_DIR, TXT_DIR, PNG_DIR, LOG_DIR):
        os.makedirs(folder, exist_ok=True)


def timestamped_path(folder, prefix, ext):
    """生成带毫秒时间戳的文件路径，避免同一秒内多次保存互相覆盖。"""
    ensure_dirs()
    stamp = time.strftime('%Y%m%d_%H%M%S') + '_%03d' % (int(time.time() * 1000) % 1000)
    name = '%s_%s.%s' % (prefix, stamp, ext)
    return os.path.join(folder, name)
