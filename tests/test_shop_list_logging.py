"""Check quiet shop navigation without removing navigation or hiding failures."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

import config
from shop_list_reader import SHOP_LIST_URL, ShopListReader


class ShopListLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = Mock()
        self.log = Mock()
        self.debug = Mock()
        self.reader = ShopListReader(SimpleNamespace(page=self.page), self.log, debug=self.debug)

    def test_fallback_navigates_without_user_visible_notice(self) -> None:
        """A missing menu still opens the route, but emits no redundant UI log."""
        with patch.object(self.reader, '_click_my_shops', return_value=False):
            self.reader._open_my_shops()
        self.page.goto.assert_called_once_with(
            SHOP_LIST_URL, wait_until='domcontentloaded', timeout=config.NAV_TIMEOUT_MS,
        )
        self.log.assert_not_called()
        self.debug.assert_called_once()

    def test_menu_success_does_not_navigate_again(self) -> None:
        """Preserve the successful menu path without an extra route navigation."""
        with patch.object(self.reader, '_click_my_shops', return_value=True):
            self.reader._open_my_shops()
        self.page.goto.assert_not_called()
        self.log.assert_not_called()

    def test_navigation_failure_is_not_silenced(self) -> None:
        """Removing a notice must not suppress the actual page navigation error."""
        self.page.goto.side_effect = TimeoutError('navigation timed out')
        with patch.object(self.reader, '_click_my_shops', return_value=False):
            with self.assertRaisesRegex(TimeoutError, 'navigation timed out'):
                self.reader._open_my_shops()
        self.log.assert_not_called()


if __name__ == '__main__':
    unittest.main()
