# -*- coding: utf-8 -*-
"""Regression checks for Youzan SPA detail-route construction."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

from shop_list_reader import SHOP_DETAIL_URL, _canonical_detail_url  # noqa: E402
from shop_followup import ShopFollowupCase  # noqa: E402


class DetailRouteTests(unittest.TestCase):
    def test_detail_url_uses_index_entry_document(self):
        self.assertEqual(
            SHOP_DETAIL_URL % 'ALIAS',
            'https://qudao.youzan.com/index#/shop/detail/ALIAS',
        )

    def test_legacy_root_shell_url_is_canonicalized(self):
        self.assertEqual(
            _canonical_detail_url(
                'https://qudao.youzan.com/#/shop/detail/ALIAS'
            ),
            'https://qudao.youzan.com/index#/shop/detail/ALIAS',
        )

    def test_followup_case_normalizes_supplied_url(self):
        bot = SimpleNamespace(page=SimpleNamespace())
        case = ShopFollowupCase(
            bot,
            lambda _message: None,
            detail_url='https://qudao.youzan.com/#/shop/detail/ALIAS',
        )
        self.assertEqual(
            case.detail_url,
            'https://qudao.youzan.com/index#/shop/detail/ALIAS',
        )


if __name__ == '__main__':
    unittest.main()
