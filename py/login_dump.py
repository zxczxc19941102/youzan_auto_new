# -*- coding: utf-8 -*-
"""
后台流程 Phase1：登录(滑块自动+短信) -> 进店铺详情 -> 打开「填写信息」跟进表单 -> 抓取表单结构。
短信验证码通过 sms_code.txt 传入（写入6位码即可）。结果写 login_dump_result.txt / form_dump2.*
"""
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
CODE_FILE = config.SMS_CODE_FILE
RESULT = os.path.join(config.TXT_DIR, 'login_dump_result.txt')
DETAIL_URL = 'https://qudao.youzan.com/#/shop/detail/2tUkp1ko7b6G19m5AZFF9fwjh'
lines = []

def log(m):
    lines.append(m)
    try:
        with open(RESULT, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception:
        pass
    print(m, flush=True)

# 清空旧验证码
try:
    if os.path.exists(CODE_FILE):
        os.remove(CODE_FILE)
except Exception:
    pass

bot = YouzanBot(log)
try:
    log('== 1.登录（自动滑块）==')
    ok = bot.start_login(config.DEFAULT_USER, config.DEFAULT_PWD)
    if not ok:
        log('登录滑块未通过，终止')
        bot.close()
        sys.exit(1)

    if bot.needs_sms:
        log('== 2.需短信，发送验证码 ==')
        if not bot.send_sms():
            log('短信发送未能确认，终止')
            bot.close()
            sys.exit(1)
        log('>>> 短信发码请求已接受，请把6位验证码写入 sms_code.txt')
        code = None
        for i in range(150):
            time.sleep(2)
            if os.path.exists(CODE_FILE):
                c = open(CODE_FILE, encoding='utf-8').read().strip()
                if c:
                    code = c
                    break
            if i % 15 == 0:
                log('等待验证码...%ds' % (i * 2))
        if not code:
            log('超时未收到验证码，终止')
            bot.close()
            sys.exit(1)
        log('== 3.提交短信验证码 ==')
        submit_result = bot.submit_code(code)
        if submit_result is not True:
            log('验证码被拒绝或提交结果未能确认，终止')
            bot.close()
            sys.exit(1)
    else:
        log('== 2.已直接登录，无需短信 ==')
    log('登录成功')

    log('== 4.进入店铺详情页 ==')
    bot.page.goto(DETAIL_URL, wait_until='domcontentloaded')
    time.sleep(5)
    log('URL=' + bot.page.url)

    log('== 5.打开填写信息表单 ==')
    opened = False
    for fn in [
        lambda: bot.page.get_by_text('填写信息', exact=False).click(timeout=4000),
        lambda: bot.page.locator('text=填写信息').first.click(timeout=4000),
    ]:
        try:
            fn()
            opened = True
            log('已打开填写信息表单')
            break
        except Exception as e:
            log('开表单: ' + repr(e)[:60])
    if not opened:
        try:
            bot.page.get_by_text('跟进记录', exact=True).first.click(timeout=3000)
            log('已点跟进记录tab')
        except Exception as e:
            log('跟进记录tab: ' + repr(e)[:50])
    time.sleep(2)

    log('== 6.抓取表单结构 ==')
    info = bot.page.evaluate(r"""() => {
      const out = {};
      const panels = [...document.querySelectorAll('div,section,form,aside,dl')].filter(e => /跟进日期|沟通情况|客户需求备注|跟进方式/.test(e.textContent));
      out.panelCount = panels.length;
      if (panels.length) {
        const panel = panels[panels.length - 1];
        out.panelCls = panel.className;
        out.panelVisible = panel.getBoundingClientRect().width > 0;
        out.panelText = panel.innerText.slice(0, 1200);
        out.ctrls = [...panel.querySelectorAll('input,select,textarea,button,[class*=checkbox],[class*=select],[class*=radio],[class*=option]')].map(e => {
          const r = e.getBoundingClientRect();
          return {tag:e.tagName, type:e.getAttribute('type'), cls:(e.className||'').slice(0,50), ph:e.getAttribute('placeholder'), val:(e.value||'').slice(0,30), text:(e.textContent||'').trim().slice(0,20), vis:r.width>0&&r.height>0};
        });
        out.panelHTML = panel.outerHTML.slice(0, 12000);
      }
      return out;
    }""")
    with open(os.path.join(config.TXT_DIR, 'form_dump2.txt'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(info, ensure_ascii=False, indent=1))
    log('表单结构已存 form_dump2.txt，panelCount=%s' % info.get('panelCount'))
    bot.page.screenshot(path=os.path.join(config.PNG_DIR, 'form_state2.png'))
    log('截图 form_state2.png')
    log('== 完成 ==')

finally:
    try:
        bot.close()
    except Exception:
        pass
