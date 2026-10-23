# -*- coding: utf-8 -*-
"""回归测试：Playwright 同步 API 下，等待方式决定事件能否被采集。

背景（本次 BUG 根因）：
  sync_api 的 asyncio 事件循环跑在同一线程的 greenlet 里（playwright/_impl/_sync_base.py 的
  wait_for_future + sync_api/_context_manager.py 的 dispatcher_fiber），只有调用 Playwright API
  时才会切过去跑循环。纯 Python 等待（time.sleep / threading.Event.wait）期间不派发任何事件，
  page.on('response') / page.on('console') 因此收不到。

三段断言：
  1) 红：浏览器 300ms 后 console.log，随后只用 Event.wait 等 1.2 秒 -> 捕获数必须为 0。
  2) 证明：紧接着一次 page.wait_for_timeout(50) -> 积压事件立刻派发，捕获数 >= 1。
  3) 绿：expect_response 包裹点击，路由本地兑现 POST /visit/save -> 立即拿到 HTTP 200 / code=0。

运行：
    venv\\Scripts\\python.exe tests\\test_publish_wait.py
退出码 0 表示 红/证明/绿 全部符合预期。全程不访问外网。
"""
import json
import threading
import time

from playwright.sync_api import sync_playwright

CONSOLE_PAGE = """<!doctype html><meta charset="utf-8">
<button id="b">发布</button>
<script>
document.getElementById('b').onclick = () => setTimeout(() => console.log('saved'), 300);
</script>"""

FETCH_PAGE = """<!doctype html><meta charset="utf-8">
<button id="b">发布</button>
<script>
document.getElementById('b').onclick = () =>
  fetch('http://local.test/visit/save', {method: 'POST', body: '{}'});
</script>"""

RESPONSE = json.dumps({"code": 0, "data": {"visitRecordId": 1}, "msg": "操作成功"})


def main():
    messages = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.on("console", lambda msg: messages.append(msg.text))
        page.set_content(CONSOLE_PAGE)

        # 1) 红：纯 Python 等待，事件不派发
        page.click("#b")
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            threading.Event().wait(0.1)
        red_count = len(messages)

        # 2) 证明：一次 Playwright 调用即可把积压事件全部派发
        page.wait_for_timeout(50)
        pumped_count = len(messages)

        # 3) 绿：事件驱动等待（路由本地兑现，不产生真实网络请求）
        page2 = browser.new_page()
        page2.route(
            "**/visit/save",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                body=RESPONSE,
            ),
        )
        page2.set_content(FETCH_PAGE)
        started = time.perf_counter()
        with page2.expect_response(
            lambda r: "/visit/save" in r.url, timeout=15000
        ) as info:
            page2.click("#b")
        response = info.value
        elapsed = time.perf_counter() - started
        green_status = response.status
        green_code = json.loads(response.text()).get("code")

        browser.close()

    print("红：Event.wait 1.2 秒后捕获数 = %d（期望 0）" % red_count)
    print("证明：一次 Playwright 调用后捕获数 = %d（期望 >=1）" % pumped_count)
    print("绿：expect_response HTTP=%s code=%s 耗时=%.2fs（期望 200/0）" % (
        green_status, green_code, elapsed
    ))
    ok = (
        red_count == 0
        and pumped_count >= 1
        and green_status == 200
        and green_code == 0
        and elapsed < 5
    )
    print("RESULT =", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
