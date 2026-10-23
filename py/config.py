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

# 页面地址（登录成功后统一落到工作台）
LOGIN_URL = 'https://account.youzan.com/login?redirectUrl=https://qudao.youzan.com#/workbench'
TARGET_URL = 'https://qudao.youzan.com/#/workbench'

# 落盘文件（CK / 完整登录态 / 短信码 / 页面源码）
COOKIE_FILE = os.path.join(JSON_DIR, 'cookies.json')
AUTH_FILE = os.path.join(JSON_DIR, 'auth_state.json')
SMS_CODE_FILE = os.path.join(TXT_DIR, 'sms_code.txt')

# 默认账号（可在界面里改）
DEFAULT_USER = '13250866726'
DEFAULT_PWD = '123456'

# 浏览器
HEADLESS = True                 # True=后台无浏览器窗口；False=弹窗调试用
BROWSER_CHANNEL = 'msedge'      # 复用系统 Edge，免下载 chromium
VIEWPORT = {'width': 1280, 'height': 800}


def ensure_dirs():
    """确保所有归档目录存在；写入文件前调用，避免目录被手工删除后报错。"""
    for folder in (JSON_DIR, HTML_DIR, TXT_DIR, PNG_DIR):
        os.makedirs(folder, exist_ok=True)


def timestamped_path(folder, prefix, ext):
    """生成带毫秒时间戳的文件路径，避免同一秒内多次保存互相覆盖。"""
    ensure_dirs()
    stamp = time.strftime('%Y%m%d_%H%M%S') + '_%03d' % (int(time.time() * 1000) % 1000)
    name = '%s_%s.%s' % (prefix, stamp, ext)
    return os.path.join(folder, name)
