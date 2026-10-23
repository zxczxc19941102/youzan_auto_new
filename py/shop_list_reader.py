# -*- coding: utf-8 -*-
"""读取有赞「我的店铺」列表，逐条输出到日志并保存结构化文件。

数据来源：
  1. .fun-table tbody tr 的可见列（保底）
  2. /shop/v2/my/list 接口响应（含店铺ID/客户/跟进人等完整字段）
"""
import csv
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import config

SHOP_LIST_URL = 'https://qudao.youzan.com/index#/shop/list/my'
SHOP_DETAIL_URL = 'https://qudao.youzan.com/#/shop/detail/%s'
SEPARATOR = '-' * 46


class ShopListReader:
    def __init__(self, bot, log, debug=None):
        self.bot = bot
        self.page = bot.page
        self.log = log
        self.debug = debug or (lambda message: None)
        self.run_id = time.strftime('%Y%m%d_%H%M%S')
        self.api_pages = {}
        self.shops = []
        self.total_text = ''

    # ---------- 接口响应 ----------
    def _attach(self):
        self.page.on('response', self._on_response)

    def _on_response(self, response):
        try:
            if '/shop/v2/my/list' not in response.url:
                return
            if response.status != 200:
                return
            payload = response.json()
            data = payload.get('data') or {}
            items = data.get('items') if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                return
            query = parse_qs(urlsplit(response.url).query)
            page_no = int((query.get('page') or ['1'])[0])
            self.api_pages[page_no] = items
            self.debug('[SHOP-API] 第 %d 页返回 %d 个店铺' % (page_no, len(items)))
        except Exception:
            pass

    # ---------- 页面读取 ----------
    def _has_real_rows(self):
        names = self.page.locator('.fun-table tbody tr [class*="shop-info"]')
        for index in range(names.count()):
            try:
                if names.nth(index).inner_text().strip():
                    return True
            except Exception:
                continue
        return False

    def _wait_rows(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._has_real_rows():
                    return self.page.locator('.fun-table tbody tr').count()
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        return 0

    def _wait_data(self, page_no, timeout=25):
        """等待列表接口或真实店铺行出现，避免读到骨架行。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.api_pages.get(page_no):
                self._wait_rows(timeout=3)
                return True
            if self._has_real_rows():
                return True
            self.page.wait_for_timeout(500)
        return False

    def _parse_dom_rows(self):
        return self.page.evaluate(r"""() => {
            return [...document.querySelectorAll('.fun-table tbody tr')].map(tr => {
                const tds = [...tr.querySelectorAll('td')];
                const shopCell = tds[1] || null;
                const nameEl = shopCell ? shopCell.querySelector('[class*="shop-info"]') : null;
                const tagTexts = shopCell
                    ? [...shopCell.querySelectorAll('.fun-tag-text')].map(el => (el.innerText || '').trim()).filter(Boolean)
                    : [];
                const actionButtons = tds[9]
                    ? [...tds[9].querySelectorAll('button')].map(el => (el.innerText || '').trim()).filter(Boolean)
                    : [];
                return {
                    shop_name: nameEl ? nameEl.innerText.trim() : '',
                    cell_text: shopCell ? shopCell.innerText.trim().replace(/\s+/g, ' ') : '',
                    tags: tagTexts,
                    recent_login: tds[2] ? tds[2].innerText.trim() : '',
                    customer: tds[3] ? tds[3].innerText.trim() : '',
                    service_expiry: tds[4] ? tds[4].innerText.trim() : '',
                    amount_30d: tds[5] ? tds[5].innerText.trim() : '',
                    region: tds[6] ? tds[6].innerText.trim().replace(/\s+/g, ' ') : '',
                    last_visit: tds[7] ? tds[7].innerText.trim() : '',
                    last_order: tds[8] ? tds[8].innerText.trim() : '',
                    actions: actionButtons,
                };
            });
        }""")

    def _merge(self, dom_rows, api_items):
        api_by_name = {
            item.get('shopName'): item
            for item in api_items
            if isinstance(item, dict) and item.get('shopName')
        }
        shops = []
        for index, dom in enumerate(dom_rows):
            api = api_by_name.get(dom.get('shop_name'))
            if api is None and index < len(api_items):
                api = api_items[index] if isinstance(api_items[index], dict) else {}
            api = api or {}
            if not api and not (dom.get('shop_name') or '').strip():
                continue
            reminds = [
                item.get('typeName')
                for item in (api.get('reminds') or [])
                if isinstance(item, dict) and item.get('typeName')
            ]
            alias = api.get('alias') or ''
            shops.append({
                'shop_name': api.get('shopName') or dom.get('shop_name', ''),
                'shop_id': alias,
                'detail_url': (SHOP_DETAIL_URL % alias) if alias else '',
                'kdt_id': api.get('kdtId') or '',
                'shop_type': api.get('shopTypeName') or '',
                'status': api.get('openStatusName') or '',
                'cert_type': api.get('certTypeName') or '',
                'shop_role': api.get('shopRoleName') or '',
                'customer': api.get('customerName') or dom.get('customer', ''),
                'binder': api.get('shopSalesBinder') or '',
                'recent_login': api.get('lastLoginTime') or dom.get('recent_login', ''),
                'last_visit': api.get('lastVisitTime') or dom.get('last_visit', ''),
                'last_order': api.get('lastOrderTime') or dom.get('last_order', ''),
                'amount_30d': api.get('day30OrderAmount') or dom.get('amount_30d', ''),
                'service_expiry': api.get('feeExpiredTime') or dom.get('service_expiry', ''),
                'region': (api.get('region') or dom.get('region', '')).strip(),
                'category': api.get('categoryName') or '',
                'reminds': reminds,
                'tags': dom.get('tags', []),
                'actions': dom.get('actions', []),
            })
        return shops

    def _shop_block_lines(self, shop):
        return [
            SEPARATOR,
            '店铺名称：%s' % (shop.get('shop_name') or '-'),
            '店铺ID：%s' % (shop.get('kdt_id') or shop.get('shop_id') or '-'),
            '跟进网址：%s' % (shop.get('detail_url') or '-'),
            SEPARATOR,
        ]

    def _find_row(self, shop_name):
        rows = self.page.locator('.fun-table tbody tr')
        for index in range(rows.count()):
            row = rows.nth(index)
            name = row.locator('[class*="shop-info"]')
            try:
                if name.count() and name.first.inner_text().strip() == shop_name:
                    return row
            except Exception:
                continue
        return None

    def _restore_page(self, page_no):
        try:
            active = self.page.locator(
                '.fun-pagination-pager-active'
            ).first.inner_text().strip()
        except Exception:
            active = ''
        if active == str(page_no):
            return True
        pagers = self.page.locator('.fun-pagination-pager')
        for index in range(pagers.count()):
            pager = pagers.nth(index)
            try:
                if pager.inner_text().strip() == str(page_no):
                    pager.evaluate("el => el.click()")
                    self.page.wait_for_timeout(1500)
                    self._wait_rows()
                    return True
            except Exception:
                continue
        return False

    def open_detail_popup(self, shop):
        """点击详情并返回 (url, popup_page)；popup_page 由调用方负责关闭。"""
        fallback = shop.get('detail_url') or ''
        row = self._find_row(shop.get('shop_name', ''))
        if row is None:
            self.debug('未找到店铺行，使用接口拼接详情 URL：%s' % shop.get('shop_name'))
            return fallback, None

        button = row.get_by_role('button', name='详情', exact=True)
        if not button.count():
            button = row.locator('button').filter(has_text='详情')
        if not button.count():
            self.debug('未找到「详情」按钮，使用拼接 URL：%s' % shop.get('shop_name'))
            return fallback, None

        target = button.first.locator('span').first
        if not target.count():
            target = button.first
        try:
            target.scroll_into_view_if_needed()
        except Exception:
            pass

        pages_before = {id(page) for page in self.page.context.pages}
        try:
            with self.page.context.expect_page(timeout=10000) as page_info:
                try:
                    target.click(force=True)
                except Exception:
                    target.evaluate("el => el.click()")
            new_page = page_info.value
            try:
                new_page.wait_for_load_state('domcontentloaded', timeout=10000)
            except Exception:
                pass
            for _ in range(20):
                if '/shop/detail/' in new_page.url:
                    break
                new_page.wait_for_timeout(250)
            if '/shop/detail/' in new_page.url:
                self.debug('详情新标签 URL=%s' % new_page.url)
                return new_page.url, new_page
            try:
                new_page.close()
            except Exception:
                pass
        except Exception as exc:
            self.debug('等待详情新标签失败：%r' % exc)

        for opened in self.page.context.pages:
            if id(opened) not in pages_before:
                if '/shop/detail/' in opened.url:
                    return opened.url, opened
                try:
                    opened.close()
                except Exception:
                    pass
        return fallback, None

    def _fetch_detail_url(self, shop, page_no):
        """点击当前行「详情」的内部 span，捕获新标签 URL 后关闭；失败则用接口拼接。"""
        detail_url, popup = self.open_detail_popup(shop)
        if popup is not None:
            try:
                popup.close()
            except Exception:
                pass
            return detail_url

        if '/shop/detail/' in self.page.url:
            detail_url = self.page.url
            try:
                self.page.go_back(wait_until='domcontentloaded')
            except Exception:
                pass
            self.page.wait_for_timeout(1200)
            self._wait_rows()
            self._restore_page(page_no)

        if not detail_url:
            self.debug('未获取到详情 URL，使用拼接 URL：%s' % shop.get('shop_name'))
            detail_url = (shop.get('detail_url') or '')
        return detail_url

    def _page_numbers(self):
        try:
            texts = self.page.locator('.fun-pagination-pager').all_inner_texts()
        except Exception:
            return []
        numbers = []
        for text in texts:
            text = text.strip()
            if text.isdigit():
                numbers.append(int(text))
        return numbers

    def _next_page(self):
        operations = self.page.locator('.fun-pagination-operation')
        if not operations.count():
            return False
        button = operations.last
        classes = button.get_attribute('class') or ''
        if 'disabled' in classes.lower():
            return False
        try:
            first_row = self.page.locator('.fun-table tbody tr').first.inner_text()
        except Exception:
            first_row = ''
        try:
            button.evaluate("el => el.click()")
        except Exception:
            try:
                button.click(force=True)
            except Exception:
                return False
        self.page.wait_for_timeout(1800)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                current = self.page.locator('.fun-table tbody tr').first.inner_text()
            except Exception:
                current = ''
            if current and current != first_row:
                return True
            self.page.wait_for_timeout(500)
        return False

    # ---------- 保存 ----------
    def _save(self):
        json_path = os.path.join(
            config.TXT_DIR, 'shop_list_%s.json' % self.run_id
        )
        with open(json_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'url': SHOP_LIST_URL,
                'total_text': self.total_text,
                'count': len(self.shops),
                'shops': self.shops,
            }, handle, ensure_ascii=False, indent=2)

        txt_path = os.path.join(
            config.TXT_DIR, 'shop_list_%s.txt' % self.run_id
        )
        with open(txt_path, 'w', encoding='utf-8') as handle:
            handle.write('店铺列表 URL: %s\n' % SHOP_LIST_URL)
            handle.write('总数: %d\n' % len(self.shops))
            for index, shop in enumerate(self.shops):
                block = self._shop_block_lines(shop)
                if index:
                    block = block[1:]
                handle.write('\n'.join(block) + '\n')

        csv_path = os.path.join(
            config.TXT_DIR, 'shop_list_%s.csv' % self.run_id
        )
        fields = [
            'shop_name', 'shop_id', 'kdt_id', 'detail_url', 'shop_type',
            'status', 'cert_type', 'shop_role', 'customer', 'binder',
            'recent_login', 'last_visit', 'last_order', 'amount_30d',
            'service_expiry', 'region', 'category', 'reminds', 'tags', 'actions',
        ]
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for shop in self.shops:
                row = dict(shop)
                row['reminds'] = '/'.join(row.get('reminds') or [])
                row['tags'] = '/'.join(row.get('tags') or [])
                row['actions'] = '/'.join(row.get('actions') or [])
                writer.writerow({key: row.get(key, '') for key in fields})

        self.debug('店铺列表 JSON -> %s' % json_path)
        self.debug('店铺列表 TXT -> %s' % txt_path)
        self.debug('店铺列表 CSV -> %s' % csv_path)
        return json_path

    # ---------- 主流程 ----------
    def run(self):
        self._attach()
        self.debug('打开店铺列表：%s' % SHOP_LIST_URL)
        self.page.goto(SHOP_LIST_URL, wait_until='domcontentloaded')
        self.page.wait_for_timeout(1500)
        if not self._wait_data(1):
            self.debug('店铺列表未加载出真实数据（接口/表格均为空）')
            self._save()
            return {'success': False, 'count': 0}

        try:
            total = self.page.locator('.fun-pagination-total').first.inner_text()
            self.total_text = total.strip()
            self.debug('分页信息：%s' % self.total_text)
        except Exception:
            pass

        page_no = 1
        while page_no <= 100:
            page_numbers = self._page_numbers()
            max_page = max(page_numbers) if page_numbers else page_no
            self.debug('读取第 %d/%d 页...' % (page_no, max_page))
            self._wait_data(page_no)
            dom_rows = self._parse_dom_rows()
            api_items = self.api_pages.get(page_no, [])
            shops = self._merge(dom_rows, api_items)
            if not shops:
                self.debug('第 %d 页没有有效店铺数据' % page_no)
                break
            for shop in shops:
                shop['detail_url'] = self._fetch_detail_url(shop, page_no)
                is_first = not self.shops
                self.shops.append(shop)
                block = self._shop_block_lines(shop)
                if not is_first:
                    block = block[1:]
                self.log('\n'.join(block))

            if page_no >= max_page or not self._next_page():
                break
            page_no += 1

        self._save()
        success = bool(self.shops)
        self.debug('店铺读取完成：共 %d 个店铺，%d 页' % (len(self.shops), page_no))
        return {
            'success': success,
            'count': len(self.shops),
            'pages': page_no,
            'json': os.path.join(config.TXT_DIR, 'shop_list_%s.json' % self.run_id),
        }
