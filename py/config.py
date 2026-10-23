# -*- coding: utf-8 -*-
"""全局配置：URL、文件路径、默认账号、浏览器参数。集中修改，避免散落在各模块。"""
import os
import time

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
DEFAULT_USER = os.environ.get('YOUZAN_USER', '')
DEFAULT_PWD = os.environ.get('YOUZAN_PASSWORD', '')

# 浏览器
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
