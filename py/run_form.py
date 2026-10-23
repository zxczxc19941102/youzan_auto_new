# -*- coding: utf-8 -*-
"""最终执行：登录->进店铺详情->自动填跟进表单->点发布->抓提交接口->验证。结果写 form_result.txt。"""
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
RESULT = os.path.join(config.TXT_DIR, 'form_result.txt')
lines = []

def log(m):
    lines.append(m)
    with open(RESULT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(m, flush=True)

captured = []
def on_req(r):
    try:
        u = r.url
        if any(k in u for k in ('follow', 'visit', 'record', 'publish', 'save', 'customer', 'shop', 'api')):
            captured.append({'t': 'REQ', 'm': r.method, 'url': u, 'post': (r.post_data or '')[:400]})
    except Exception:
        pass

def on_res(r):
    try:
        u = r.url
        if any(k in u for k in ('follow', 'visit', 'record', 'publish', 'save', 'add')):
            b = ''
            try:
                b = r.text()[:300]
            except Exception:
                b = '<nobody>'
            captured.append({'t': 'RES', 'status': r.status, 'url': u, 'body': b})
    except Exception:
        pass

try:
    os.remove(config.SMS_CODE_FILE)  # 清掉过期验证码，避免误用旧码
except Exception:
    pass
config.VIEWPORT = {'width': 1400, 'height': 1800}  # 加高视口以容纳整张表单
bot = YouzanBot(log)
log('== 1.登录 ==')
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
    log('>>> 短信发码请求已接受，请把最新一条短信的6位验证码写入 sms_code.txt')
    logged = False
    for attempt in range(3):
        code = None
        for i in range(450):
            time.sleep(2)
            p = config.SMS_CODE_FILE
            c = open(p, encoding='utf-8').read().strip() if os.path.exists(p) else ''
            if c:
                code = c
                break
        if not code:
            log('等待验证码超时')
            break

        log('提交短信验证码（第%d次）' % (attempt + 1))
        submit_result = bot.submit_code(code)
        if submit_result is True:
            logged = True
            break
        if submit_result is None:
            log('提交结果未能确认，终止以避免重复提交')
            break

        try:
            os.remove(config.SMS_CODE_FILE)
        except OSError:
            pass
        log('页面明确拒绝验证码；已清空旧码，请写入最新短信验证码')

    if not logged:
        log('登录未成功，终止')
        bot.close()
        sys.exit(1)
else:
    log('直接登录成功')

bot.page.on('request', on_req)
bot.page.on('response', on_res)

log('== 2.进店铺详情 ==')
bot.page.goto(DETAIL_URL, wait_until='domcontentloaded')
time.sleep(5)
log('URL=' + bot.page.url)

log('== 3.填表 ==')
page = bot.page

def js_click_text(txt):
    return page.evaluate("""(txt) => {
      let scopes = [...document.querySelectorAll('.fun-form-item')];
      scopes.push(document.body);
      for (const scope of scopes) {
        const cands = [...scope.querySelectorAll('label, .fun-checkbox-wrapper, .fun-radio-wrapper, .fun-select-option, span, option')]
          .filter(e => (e.textContent||'').trim().includes(txt));
        for (const e of cands) {
          try { e.scrollIntoView({block:'center'}); e.click(); return (e.textContent||'').trim().slice(0,16); } catch(err){}
        }
      }
      return null;
    }""", txt)

# 跟进方式：线下拜访
log('跟进方式 -> %s' % js_click_text('线下拜访'))
# 跟进目的/事由：拜访约见
log('跟进目的 -> %s' % js_click_text('拜访约见'))

# 沟通情况：fun-select 下拉
try:
    item = page.locator('.fun-form-item').filter(has_text='沟通情况').first
    item.locator('.fun-select').first.evaluate("e => { e.scrollIntoView({block:'center'}); e.click(); }")
    time.sleep(1)
    log('沟通情况 -> %s' % js_click_text('电话打通，有交流'))
except Exception as e:
    log('沟通情况 失败: %r' % e)

# 客户需求/备注
try:
    item = page.locator('.fun-form-item').filter(has_text='客户需求').first
    item.locator('textarea').first.evaluate("""e => {
      e.scrollIntoView({block:'center'});
      const set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      set.call(e, '有需要会联系');
      e.dispatchEvent(new Event('input', {bubbles:true}));
      e.dispatchEvent(new Event('change', {bubbles:true}));
    }""")
    log('备注 -> 有需要会联系 OK')
except Exception as e:
    log('备注 失败: %r' % e)

time.sleep(1)
page.screenshot(path=os.path.join(config.PNG_DIR, 'form_filled.png'))
log('已截图 form_filled.png（填表后）')

log('== 4.点击发布 ==')
try:
    r = page.evaluate("""() => {
      const b = [...document.querySelectorAll('button')].find(x => x.className.includes('primary') && /发布|确定/.test(x.textContent));
      if (b) { b.scrollIntoView({block:'center'}); b.click(); return b.textContent.trim(); }
      return null;
    }""")
    log('已点发布: %s' % r)
except Exception as e:
    log('点发布 失败: %r' % e)

time.sleep(3)
log('== 5.结果 ==')
state = page.evaluate(r"""() => {
  const body = document.body.innerText;
  const okMsg = /发布成功|保存成功|提交成功|成功/.test(body);
  return {url: location.href, okMsg, body: body.slice(0, 300)};
}""")
log('页面状态: ' + json.dumps(state, ensure_ascii=False))
log('抓到的接口:')
log(json.dumps(captured, ensure_ascii=False, indent=1))
page.screenshot(path=os.path.join(config.PNG_DIR, 'form_after.png'))
log('已截图 form_after.png')
log('== 完成 ==')

bot.close()
