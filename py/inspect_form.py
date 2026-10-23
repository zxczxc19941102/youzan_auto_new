# -*- coding: utf-8 -*-
"""临时：进店铺详情页，打开跟进表单，把结构写入 form_report.txt（UTF-8，避免控制台乱码）。调查完删除。"""
import os, sys, subprocess, json, time
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_VENV = os.path.join(_ROOT, 'venv')
_VENV_PY = os.path.join(_VENV, 'Scripts', 'python.exe')
if os.path.isdir(_VENV) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(_VENV)):
    raise SystemExit(subprocess.call([_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]))

import config
from playwright.sync_api import sync_playwright

config.ensure_dirs()
DETAIL_URL = 'https://qudao.youzan.com/#/shop/detail/2tUkp1ko7b6G19m5AZFF9fwjh'
REPORT = os.path.join(config.TXT_DIR, 'form_report.txt')

def dump(page):
    return page.evaluate(r"""() => {
      const out = {};
      // 候选入口（填写/写/新建/跟进/信息表 等短文本元素）
      out.entries = [...document.querySelectorAll('*')].filter(e => {
        const t = (e.textContent||'').trim();
        return t.length <= 8 && /填写|写|新建|跟进|信息表|联系人|发布|沟通/.test(t) && e.children.length <= 2;
      }).map(e => { const r = e.getBoundingClientRect(); return {tag:e.tagName, cls:(e.className||'').slice(0,50), text:(e.textContent||'').trim().slice(0,12), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)}; }).filter(o => o.w>0 && o.h>0);
      // 表单控件（查找跟进日期/沟通情况所在面板，无论可见性）
      const panels = [...document.querySelectorAll('div,section,form,aside,dl')].filter(e => /跟进日期|沟通情况|客户需求备注|跟进方式/.test(e.textContent));
      out.panelCount = panels.length;
      if (panels.length) {
        const panel = panels[panels.length-1];
        out.panelCls = panel.className;
        out.panelVisible = panel.getBoundingClientRect().width > 0;
        out.panelText = panel.innerText.slice(0, 800);
        out.ctrls = [...panel.querySelectorAll('input,select,textarea,button,[class*=checkbox],[class*=select],[class*=radio]')].map(e => {
          const r = e.getBoundingClientRect();
          return {tag:e.tagName, type:e.getAttribute('type'), cls:(e.className||'').slice(0,40), ph:e.getAttribute('placeholder'), val:(e.value||'').slice(0,20), text:(e.textContent||'').trim().slice(0,16), vis:r.width>0&&r.height>0};
        });
        out.panelHTML = panel.outerHTML.slice(0, 6000);
      }
      return out;
    }""")

pw = sync_playwright().start()
browser = pw.chromium.launch(channel='msedge', headless=True)
context = browser.new_context(viewport={'width': 1360, 'height': 950})
context.add_cookies(json.load(open(config.COOKIE_FILE, encoding='utf-8')))
page = context.new_page()
page.goto(DETAIL_URL, wait_until='domcontentloaded')
time.sleep(5)

lines = ['URL=' + page.url, 'TITLE=' + page.title(), '']

# 先尝试多种方式打开“填写信息”
opened = False
for attempt in [
    lambda: page.get_by_text('填写信息', exact=False).click(timeout=3000),
    lambda: page.locator('text=填写信息').first.click(timeout=3000),
    lambda: page.locator('[class*=write],[class*=fill],[class*=add]').filter(has_text='填写').first.click(timeout=3000),
]:
    try:
        attempt()
        opened = True
        lines.append('OPENED via method')
        break
    except Exception as e:
        lines.append('open fail: ' + repr(e)[:80])

# 也点击“跟进记录”标签
try:
    page.get_by_text('跟进记录', exact=False).first.click(timeout=2000)
    lines.append('clicked 跟进记录 tab')
except Exception as e:
    lines.append('跟进记录 tab: ' + repr(e)[:60])

time.sleep(2)
info = dump(page)
lines.append('== DUMP ==')
lines.append(json.dumps(info, ensure_ascii=False, indent=1))
open(REPORT, 'w', encoding='utf-8').write('\n'.join(lines))

# 截图
page.screenshot(path=os.path.join(config.PNG_DIR, 'form_state.png'))
browser.close()
pw.stop()
print('WROTE', REPORT, 'opened=', opened, flush=True)
