import base64
import configparser
import ctypes
import os
import sys
import time
from ctypes import wintypes

BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_CONFIG_FILE = os.path.join(BASE_DIR, "project_config.json")
LOCAL_CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
LOG_DIR = os.path.join(BASE_DIR, "log")
JSON_DIR = HTML_DIR = TXT_DIR = PNG_DIR = LOG_DIR
PY_DIR = os.path.join(BASE_DIR, "py")
COOKIE_FILE = os.path.join(LOG_DIR, "cookies.json")
AUTH_FILE = os.path.join(LOG_DIR, "auth_state.json")
SMS_CODE_FILE = os.path.join(LOG_DIR, "sms_code.txt")

LOGIN_URL = 'https://account.youzan.com/login?redirectUrl=https://qudao.youzan.com#/workbench'
TARGET_URL = 'https://qudao.youzan.com/#/workbench'
LOCAL_CONFIG_LOAD_ERROR = ""
LOCAL_CONFIG_EXISTS = os.path.isfile(LOCAL_CONFIG_FILE)
LOCAL_PASSWORD_MIGRATION_REQUIRED = False

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

def _dpapi_transform(value: bytes, decrypt: bool = False) -> bytes | None:
    """Protect/unprotect secrets with Windows user-scoped DPAPI."""
    if os.name != "nt":
        return None
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    source = ctypes.create_string_buffer(raw)
    blob = _DataBlob(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output = _DataBlob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(output)):
        return None
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)

def _protect_password(password: str) -> str:
    """Encrypt a password with current-Windows-user DPAPI."""
    value = _dpapi_transform(str(password).encode("utf-8"))
    if value is None:
        raise RuntimeError("Windows DPAPI password encryption failed; credentials were not saved")
    return "dpapi:" + base64.b64encode(value).decode("ascii")

def _unprotect_password(value: str) -> str:
    """Decrypt a DPAPI value, rejecting plaintext and invalid ciphertext."""
    if not value or not value.startswith("dpapi:"):
        return ""
    try:
        raw = _dpapi_transform(base64.b64decode(value[6:], validate=True), decrypt=True)
        return raw.decode("utf-8") if raw is not None else ""
    except (ValueError, TypeError, UnicodeError):
        return ""

def _load_local_config() -> configparser.ConfigParser:
    """Load the root INI without creating startup artifacts."""
    global LOCAL_CONFIG_LOAD_ERROR
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if os.path.isfile(LOCAL_CONFIG_FILE):
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8-sig") as stream:
                parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        LOCAL_CONFIG_LOAD_ERROR = str(exc)
    return parser

_LOCAL_CONFIG = _load_local_config()
def _get_bool(key: str, default: bool) -> bool:
    try:
        return _LOCAL_CONFIG.getboolean("menu", key, fallback=default)
    except (ValueError, configparser.Error):
        return default

DEFAULT_USER = os.environ.get("YOUZAN_USER") or _LOCAL_CONFIG.get("credentials", "username", fallback="")
_ciphertext = _LOCAL_CONFIG.get("credentials", "password_protected", fallback="")
DEFAULT_PWD = os.environ.get("YOUZAN_PASSWORD") or _unprotect_password(_ciphertext)
DEFAULT_AUTO_SHOP_LIST = _get_bool("auto_shop_list", True)
DEFAULT_AUTO_FILL_ALL = _get_bool("auto_fill_all", True)
DEFAULT_FOLLOWUP_CLUE = _get_bool("followup_clue", False)
DEFAULT_FOLLOWUP_CUSTOMER = _get_bool("followup_customer", False)
DEFAULT_FOLLOWUP_SHOP = _get_bool("followup_shop", True)
FOLLOWUP_CONCURRENCY_ENABLED = False
FOLLOWUP_MAX_WORKERS = 1
FOLLOWUP_PUBLISH_INTERVAL_SECONDS = 3.01
# Read-only duplicate skips do not use the write throttle.
FOLLOWUP_SKIP_INTERVAL_SECONDS = 1.0
CRM_FOLLOWUP_MODE = _LOCAL_CONFIG.get("execution", "crm_followup_mode", fallback="api").strip().lower()
CRM_API_RETRY_ATTEMPTS = 2
CRM_API_PAGE_SIZE = 100
CRM_API_REQUEST_TIMEOUT_MS = 20000
CRM_API_SKIP_TODAY_DUPLICATES = True
CRM_API_VERIFY_ATTEMPTS = 8
CRM_API_VERIFY_POLL_SECONDS = 0.75
DEFAULT_WORKERS = 1

def save_local_config(username: str, password: str, auto_shop_list: bool = True,
                      auto_fill_all: bool = True, followup_clue: bool = False,
                      followup_customer: bool = False, followup_shop: bool = True,
                      crm_followup_mode: str | None = None) -> None:
    """Persist account and all menu states in config.ini; password is DPAPI protected."""
    parser = configparser.ConfigParser(interpolation=None)
    parser["credentials"] = {"username": str(username or ""),
                             "password_protected": _protect_password(password or "")}
    parser["menu"] = {name: str(bool(value)).lower() for name, value in {
        "auto_shop_list": auto_shop_list, "auto_fill_all": auto_fill_all,
        "followup_clue": followup_clue, "followup_customer": followup_customer,
        "followup_shop": followup_shop,
    }.items()}
    parser["execution"] = {"workers": "1", "crm_followup_mode":
                           str(crm_followup_mode or CRM_FOLLOWUP_MODE).strip().lower()}
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8", newline="") as stream:
        parser.write(stream)
    try:
        os.chmod(LOCAL_CONFIG_FILE, 0o600)
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
# Conservative Playwright polling prevents long DOM waits from saturating CPU.
WAIT_POLLING_MS = 250
CAPTCHA_WAIT_POLLING_MS = 180
FOLLOWUP_FORM_TIMEOUT_MS = 30000  # async follow-up panel/form render timeout

# Login speed tuning. Prefer DOM/network readiness over fixed sleeps.
LOGIN_PAGE_SETTLE_SECONDS = 0.30
CAPTCHA_IMAGE_SETTLE_SECONDS = 0.80
CAPTCHA_IMAGE_POLL_SECONDS = 0.10
CAPTCHA_IMAGE_READY_TIMEOUT_SECONDS = 3.00
CAPTCHA_RESULT_POLL_SECONDS = 0.10
CAPTCHA_RESULT_TIMEOUT_SECONDS = 3.20
LOGIN_STATE_POLL_SECONDS = 0.25
WORKBENCH_SETTLE_SECONDS = 0.20

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
    """Create only the permitted runtime log directory."""
    os.makedirs(LOG_DIR, exist_ok=True)


def timestamped_path(folder, prefix, ext):
    """生成带毫秒时间戳的文件路径，避免同一秒内多次保存互相覆盖。"""
    ensure_dirs()
    stamp = time.strftime('%Y%m%d_%H%M%S') + '_%03d' % (int(time.time() * 1000) % 1000)
    name = '%s_%s.%s' % (prefix, stamp, ext)
    return os.path.join(folder, name)
