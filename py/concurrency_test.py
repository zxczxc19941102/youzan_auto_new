# -*- coding: utf-8 -*-
"""并发填写跟进记录压测：只填表不发布，测试 2/5/10/20 线程。

运行：
    python py/concurrency_test.py
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from shop_followup import ShopFollowupCase
from shop_list_reader import SHOP_LIST_URL, ShopListReader
from youzan_bot import YouzanBot

config.VIEWPORT = {'width': 1920, 'height': 1080}
LOG_LOCK = threading.Lock()
DEBUG_PATH = os.path.join(
    config.TXT_DIR, 'concurrency_test_%s.log' % time.strftime('%Y%m%d_%H%M%S')
)
config.ensure_dirs()


def debug(msg):
    with LOG_LOCK:
        try:
            with open(DEBUG_PATH, 'a', encoding='utf-8') as handle:
                handle.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
        except Exception:
            pass


def read_shops(limit=20):
    bot = YouzanBot(lambda message: None)
    try:
        bot._ensure_browser(storage_state=config.AUTH_FILE)
        reader = ShopListReader(bot, lambda message: None, debug=debug)
        reader._attach()
        bot.page.goto(SHOP_LIST_URL, wait_until='domcontentloaded')
        bot.page.wait_for_timeout(1500)
        if not reader._wait_data(1):
            return []
        shops = reader._merge(
            reader._parse_dom_rows(),
            reader.api_pages.get(1, []),
        )
        if len(shops) < limit:
            reader._next_page()
            reader._wait_data(2)
            shops.extend(reader._merge(
                reader._parse_dom_rows(),
                reader.api_pages.get(2, []),
            ))
        return shops[:limit]
    finally:
        bot.close()


def fill_one(shop, tag):
    bot = YouzanBot(lambda message: None)
    try:
        bot._ensure_browser(storage_state=config.AUTH_FILE)
        case = ShopFollowupCase(
            SimpleNamespace(page=bot.page),
            debug,
            dry_run=True,
            shop_id=str(shop.get('kdt_id') or shop.get('shop_id') or ''),
            detail_url=shop.get('detail_url') or '',
            run_id=tag,
        )
        result = case.run()
        return bool(result.get('success')), result.get('error', '')
    except Exception as exc:
        return False, repr(exc)
    finally:
        bot.close()


def test_threads(shops, threads):
    targets = shops[:threads]
    started = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {}
        for index, shop in enumerate(targets):
            tag = 'conc_%d_%02d_%s' % (
                threads, index + 1, time.strftime('%H%M%S')
            )
            futures[pool.submit(fill_one, shop, tag)] = shop
        for future in as_completed(futures):
            try:
                ok, error = future.result()
            except Exception as exc:
                ok, error = False, repr(exc)
            results.append((ok, error))
    elapsed = time.time() - started
    success = sum(1 for ok, _ in results if ok)
    print('线程 %d：成功 %d/%d，用时 %.1fs' % (
        threads, success, len(results), elapsed
    ))
    debug('线程 %d：成功 %d/%d，用时 %.1fs' % (
        threads, success, len(results), elapsed
    ))
    return success, len(results), elapsed


def main():
    shops = read_shops(20)
    print('可用于压测的店铺：%d 家' % len(shops))
    if len(shops) < 20:
        print('店铺不足 20 家，20 线程测试将使用现有数量')
    for threads in (2, 5, 10, 20):
        if not shops:
            break
        test_threads(shops, threads)
    print('压测日志：%s' % DEBUG_PATH)


if __name__ == '__main__':
    main()
