# -*- coding: utf-8 -*-
"""\u8bfb\u53d6 CRM \u7684\u7ebf\u7d22/\u5ba2\u6237\u5217\u8868\uff0c\u8f93\u51fa\u53ef\u7528\u4e8e\u8ddf\u8fdb\u8bb0\u5f55\u7684\u76ee\u6807\u6761\u76ee\u3002"""
import json
import os
import re
import time
from urllib.parse import urlsplit

import config
from task_runtime import TaskCancelled

TARGETS = {
    "clue": {
        "label": "\u7ebf\u7d22",
        "list_url": "https://qudao.youzan.com/index#/customer/list/my",
        "route": "/customer/",
        "id_keys": ("id", "customerId", "clueId", "leadId"),
    },
    "customer": {
        "label": "\u5ba2\u6237",
        "list_url": "https://qudao.youzan.com/index#/entity/list/my",
        "route": "/entity/",
        "id_keys": ("id", "customerId", "entityId"),
    },
}


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


class CrmListReader:
    """\u901a\u7528 CRM \u5217\u8868\u8bfb\u53d6\u5668\uff1b\u4ec5\u8bfb\u53d6\u5217\u8868\u548c\u8be6\u60c5\u94fe\u63a5\uff0c\u4e0d\u63d0\u4ea4\u4efb\u4f55\u8868\u5355\u3002"""

    def __init__(self, bot, target_kind, log, debug=None, runtime=None):
        if target_kind not in TARGETS:
            raise ValueError("unknown CRM target: %s" % target_kind)
        self.bot = bot
        self.page = bot.page
        self.target_kind = target_kind
        self.meta = TARGETS[target_kind]
        self.log = log
        self.debug = debug or (lambda message: None)
        self.runtime = runtime
        self.stop_event = runtime.stop_event if runtime is not None else None
        self.run_id = time.strftime("%Y%m%d_%H%M%S")
        self.items = []
        self.api_items = []
        self.page_count = 0

    def _check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled()

    def _sleep(self, seconds):
        self._check()
        if self.stop_event is None:
            time.sleep(seconds)
        elif self.stop_event.wait(max(0.0, float(seconds))):
            raise TaskCancelled()

    def _is_visible(self, locator):
        try:
            return locator.is_visible()
        except Exception:
            return False

    def _attach(self):
        self.page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            if response.status != 200:
                return
            url = response.url.lower()
            if self.meta["route"].strip("/") not in url:
                return
            payload = response.json()
            found = []
            def walk(value):
                if isinstance(value, list):
                    if value and all(isinstance(item, dict) for item in value):
                        found.extend(value)
                    for item in value:
                        walk(item)
                elif isinstance(value, dict):
                    for item in value.values():
                        walk(item)
            walk(payload)
            if found:
                self.api_items.extend(found)
                self.debug("[%s-API] %s \u8fd4\u56de\u5019\u9009 %d \u6761" % (self.target_kind, urlsplit(response.url).path, len(found)))
        except Exception:
            pass

    def _wait_ready(self):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self._check()
            try:
                rows = self.page.locator(".fun-table tbody tr")
                if rows.count() > 0:
                    for index in range(rows.count()):
                        text = _clean(rows.nth(index).inner_text())
                        if text and text not in ("\u6682\u65e0\u6570\u636e", "\u52a0\u8f7d\u4e2d"):
                            return True
                body = self.page.locator("body").inner_text()
                if "\u6682\u65e0\u6570\u636e" in body or "\u65e0\u6570\u636e" in body:
                    # Continue polling briefly because the SPA may first mount an empty row.
                    if time.monotonic() + 1 >= deadline:
                        return True
            except TaskCancelled:
                raise
            except Exception:
                pass
            self._sleep(0.5)
        return False

    def _extract_rows(self):
        return self.page.evaluate(r"""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            const getId = el => {
                for (const key of ['data-id','data-key','data-customer-id','data-entity-id','data-clue-id']) {
                    if (el.getAttribute(key)) return el.getAttribute(key);
                }
                return '';
            };
            const rows = [...document.querySelectorAll('.fun-table tbody tr, [role="row"], [class*="table-row"]')];
            return rows.filter(visible).map((row, index) => {
                const links = [...row.querySelectorAll('a[href]')].filter(visible).map(a => ({
                    text: text(a), href: a.href || a.getAttribute('href') || ''
                }));
                const buttons = [...row.querySelectorAll('button,[role="button"],a')].filter(visible).map(el => ({
                    text: text(el), href: el.href || el.getAttribute('href') || ''
                })).filter(x => x.text);
                return {
                    index, id: getId(row), text: text(row), links, buttons
                };
            }).filter(row => row.text);
        }""")

    def _detail_from_row(self, row):
        candidates = list(row.get("links") or []) + list(row.get("buttons") or [])
        for item in candidates:
            href = str(item.get("href") or "")
            if self.meta["route"] in href:
                return href.replace("https://qudao.youzan.com/#/", "https://qudao.youzan.com/index#/")
        return ""

    def _resolve_detail_by_click(self, row_index):
        """\u5f53\u5217\u8868\u672a\u66b4\u9732 href \u65f6\uff0c\u70b9\u51fb\u5f53\u524d\u884c\u7684\u8be6\u60c5/\u67e5\u770b\u5165\u53e3\u89e3\u6790\u8be6\u60c5\u8def\u7531\u3002"""
        try:
            rows = self.page.locator('.fun-table tbody tr, [role="row"], [class*="table-row"]')
            row = rows.nth(row_index)
            candidates = row.locator('button, [role="button"], a').filter(
                has_text=re.compile(r'\u8be6\u60c5|\u67e5\u770b|\u8be6\u7ec6|\u7f16\u8f91')
            )
            for index in range(candidates.count()):
                button = candidates.nth(index)
                if not self._is_visible(button):
                    continue
                before = self.page.url
                button.click(force=True)
                self._sleep(0.8)
                current = self.page.url
                if self.meta['route'] in current and current != before:
                    detail_url = current.replace(
                        'https://qudao.youzan.com/#/',
                        'https://qudao.youzan.com/index#/',
                        1,
                    )
                    try:
                        self.page.go_back(wait_until='domcontentloaded', timeout=config.NAV_TIMEOUT_MS)
                    except Exception:
                        self.page.goto(self.meta['list_url'], wait_until='domcontentloaded', timeout=config.NAV_TIMEOUT_MS)
                    self._sleep(0.8)
                    return detail_url
        except TaskCancelled:
            raise
        except Exception as exc:
            self.debug('\u8be6\u60c5\u5165\u53e3\u89e3\u6790\u5931\u8d25\uff1a%r' % exc)
        return ''

    def _item_from_row(self, row, index, page_no=1, ordinal=None):
        detail_url = self._detail_from_row(row)
        if not detail_url:
            detail_url = self._resolve_detail_by_click(index)
        text = _clean(row.get("text"))
        item_id = _clean(row.get("id"))
        if not item_id:
            match = re.search(r"(?:ID|id|\u7f16\u53f7|\u5ba2\u6237\u7f16\u53f7|\u7ebf\u7d22\u7f16\u53f7)\s*[:\uff1a]?\s*([A-Za-z0-9_-]{4,})", text)
            if match:
                item_id = match.group(1)
        if not item_id and detail_url:
            match = re.search(r"/(?:customer|entity)/(?:detail/)?([^/?#]+)", detail_url)
            if match:
                item_id = match.group(1)
        return {
            "target_kind": self.target_kind,
            "target_label": self.meta["label"],
            "item_id": item_id or str((ordinal if ordinal is not None else index) + 1),
            "name": text[:160],
            "detail_url": detail_url,
            "row_index": index,
            "page_no": page_no,
            "row_text": text,
        }

    def _next_page(self):
        try:
            operations = self.page.locator('.fun-pagination-operation')
            if not operations.count():
                return False
            button = operations.last
            cls = button.get_attribute('class') or ''
            if 'disabled' in cls.lower() or not self._is_visible(button):
                return False
            before = self.page.locator('.fun-table tbody tr').first.inner_text()
            button.click(force=True)
            self._sleep(1.5)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                self._check()
                try:
                    current = self.page.locator('.fun-table tbody tr').first.inner_text()
                    if current and current != before:
                        return True
                except Exception:
                    pass
                self._sleep(0.4)
        except TaskCancelled:
            raise
        except Exception as exc:
            self.debug("\u4e0b\u4e00\u9875\u5931\u8d25\uff1a%r" % exc)
        return False

    def _save(self):
        config.ensure_dirs()
        path = os.path.join(config.TXT_DIR, "%s_list_%s.json" % (self.target_kind, self.run_id))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"target_kind": self.target_kind, "url": self.meta["list_url"], "count": len(self.items), "items": self.items}, handle, ensure_ascii=False, indent=2)
        self.debug("%s\u5217\u8868 JSON -> %s" % (self.meta["label"], path))
        return path

    def run(self):
        self._check()
        self._attach()
        self.page.goto(self.meta["list_url"], wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
        self._sleep(1.2)
        if not self._wait_ready():
            self._save()
            return {"success": False, "count": 0, "items": []}
        page_no = 1
        while page_no <= 100:
            self._check()
            rows = self._extract_rows()
            if not rows:
                break
            for row_index, row in enumerate(rows):
                if row.get("text") in ("\u6682\u65e0\u6570\u636e", "\u52a0\u8f7d\u4e2d"):
                    continue
                # row_index must be local to the current page.  The previous
                # global index made every item on page 2 impossible to locate
                # because page 2 only contains its own visible rows.
                item = self._item_from_row(row, row_index, page_no, ordinal=len(self.items))
                if item["detail_url"] or item["row_text"]:
                    self.items.append(item)
            self.page_count = page_no
            if not self._next_page():
                break
            page_no += 1
        self._save()
        self.log("\u83b7\u53d6%s\u6570%d\u4e2a" % (self.meta["label"], len(self.items)))
        return {"success": bool(self.items), "count": len(self.items), "items": self.items, "pages": self.page_count}
