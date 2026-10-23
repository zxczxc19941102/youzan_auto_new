# -*- coding: utf-8 -*-
"""临时：登录进详情页，抓取跟进表单完整结构（每个字段的控件/选项）写 form_full.txt。调查完删。"""
import os, sys, subprocess, json, time
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_VENV = os.path.join(_ROOT, 'venv')
_VENV_PY = os.path.join(_VENV, 'Scripts', 'python.exe')
if os.path.isdir(_VENV) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(_VENV)):
    raise SystemExit(subprocess.call([_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))

import config
from youzan_bot import YouzanBot

config.ensure_dirs()
DETAIL_URL = 'https://qudao.youzan.com/#/shop/detail/2tUkp1ko7b6G19m5AZFF9fwjh'
REPORT = os.path.join(config.TXT_DIR, 'form_full.txt')
lines = []

def log(m):
    lines.append(m)
    with open(REPORT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(m, flush=True)

bot = YouzanBot(log)
log('== 登录 ==')
ok = bot.start_login(config.DEFAULT_USER, config.DEFAULT_PWD)
if not ok:
    log('登录未通过')
    bot.close()
    sys.exit(1)
if bot.needs_sms:
    if not bot.send_sms():
        log('短信发送未能确认，终止')
        bot.close()
        sys.exit(1)
    log('>>> 短信发码请求已接受，请把6位验证码写入 sms_code.txt')
    code = None
    for i in range(150):
        time.sleep(2)
        p = config.SMS_CODE_FILE
        c = open(p, encoding='utf-8').read().strip() if os.path.exists(p) else ''
        if c:
            code = c
            break
    if not code:
        log('超时')
        bot.close()
        sys.exit(1)
    result = bot.submit_code(code)
    if result is not True:
        log('短信验证码登录未能确认成功，终止')
        bot.close()
        sys.exit(1)
else:
    log('直接登录成功')

bot.page.goto(DETAIL_URL, wait_until='domcontentloaded')
time.sleep(5)
log('URL=' + bot.page.url)

# 抓取完整表单：遍历 fun-form-item，输出 label + 控件HTML + 选项
form = bot.page.evaluate(r"""() => {
  const items = [...document.querySelectorAll('.fun-form-item')];
  const rows = items.map(it => {
    const label = (it.querySelector('.fun-form-item-label')||{}).textContent || '';
    const ctrl = it.querySelector('.fun-form-item-control');
    const ctrlHTML = ctrl ? ctrl.outerHTML.slice(0, 1500) : '';
    // 提取选项文本
    const opts = [...it.querySelectorAll('label,option,.fun-select-option,[class*=option]')].map(o => (o.textContent||'').trim()).filter(Boolean);
    const inputs = [...it.querySelectorAll('input,textarea,select')].map(i => ({type:i.getAttribute('type'), val:i.value, ph:i.getAttribute('placeholder')}));
    return {label: label.trim(), opts, inputs, ctrlHTML};
  });
  // 发布按钮
  const btns = [...document.querySelectorAll('button')].filter(b => /发布|确定|保存/.test(b.textContent)).map(b => ({text:b.textContent.trim(), cls:b.className}));
  return {itemCount: items.length, rows, btns};
}""")
log(json.dumps(form, ensure_ascii=False, indent=1))
bot.close()
log('DONE')
