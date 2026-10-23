# -*- coding: utf-8 -*-
"""临时：进店铺详情页，定位顶部「填写信息/跟进记录」标签并打开跟进表单，抓结构写文件。调查完删。"""
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
REPORT = os.path.join(config.TXT_DIR, 'detail_report.txt')
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
    log('登录未通过，终止')
    bot.close()
    sys.exit(1)
if bot.needs_sms:
    log('需短信，发验证码')
    if not bot.send_sms():
        log('短信发送未能确认，终止')
        bot.close()
        sys.exit(1)
    log('>>> 短信发码请求已接受，请把6位验证码写入 sms_code.txt')
    code = None
    for i in range(150):
        time.sleep(2)
        c = open(config.SMS_CODE_FILE, encoding='utf-8').read().strip() if os.path.exists(config.SMS_CODE_FILE) else ''
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

# dump所有短文本可见元素（找填写信息/跟进记录/新建/发布 等），带位置
els = bot.page.evaluate(r"""() => {
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent||'').trim();
    if (t.length === 0 || t.length > 6 || e.children.length > 2) continue;
    if (!/填写|写|新建|跟进|信息表|权益|发布|联系人|沟通|备注|保存|确定|取消/.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({tag:e.tagName, cls:(e.className||'').slice(0,40), text:t.slice(0,10), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)});
  }
  return out;
}""")
log('== 候选元素 ==')
log(json.dumps(els, ensure_ascii=False, indent=1))

# 优先点内容区的「填写信息」（x>200，非左侧导航）
def click_by_text_area(txt, min_x=200):
    hit = [e for e in els if txt in e['text'] and e['x'] >= min_x]
    if hit:
        t = hit[0]
        bot.page.mouse.click(t['x'] + t['w']//2, t['y'] + t['h']//2)
        log('点击 %s @(%d,%d)' % (txt, t['x'], t['y']))
        return True
    return False

opened = False
for txt in ['填写信息', '写跟进', '新建', '填写']:
    if click_by_text_area(txt):
        opened = True
        time.sleep(2)
        break
if not opened:
    log('未找到填写信息入口，尝试顶部跟进记录标签')
    click_by_text_area('跟进记录', min_x=200)
    time.sleep(2)

# 抓表单
form = bot.page.evaluate(r"""() => {
  const panels = [...document.querySelectorAll('div,section,form,aside,dl')].filter(e => /跟进日期|沟通情况|客户需求|跟进方式|联系人/.test(e.textContent) && e.querySelector('input,select,textarea'));
  if (!panels.length) return {err:'no form panel with controls', matched: [...document.querySelectorAll('div')].filter(e=>/跟进日期|沟通情况/.test(e.textContent)).length};
  const panel = panels[panels.length-1];
  const ctrls = [...panel.querySelectorAll('input,select,textarea,button,[class*=checkbox],[class*=select],[class*=radio]')].map(e => {
    const r = e.getBoundingClientRect();
    return {tag:e.tagName, type:e.getAttribute('type'), cls:(e.className||'').slice(0,45), ph:e.getAttribute('placeholder'), val:(e.value||'').slice(0,24), text:(e.textContent||'').trim().slice(0,16), vis:r.width>0&&r.height>0};
  });
  return {panelCls:panel.className, panelVisible:panel.getBoundingClientRect().width>0, panelText:panel.innerText.slice(0,1000), ctrls, html:panel.outerHTML.slice(0,10000)};
}""")
log('== 表单 ==')
log(json.dumps(form, ensure_ascii=False, indent=1))
bot.page.screenshot(path=os.path.join(config.PNG_DIR, 'detail_state.png'))
log('截图 detail_state.png')

bot.close()
log('DONE')
