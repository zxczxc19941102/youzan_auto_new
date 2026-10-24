# -*- coding: utf-8 -*-
"""Regression tests for local shop-list report generation."""
import ast
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

from shop_list_reader import ShopListReader  # noqa: E402
import shop_followup  # noqa: E402
import config  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        reader = ShopListReader.__new__(ShopListReader)
        reader.run_id = 'test'
        reader.total_text = '? 2 ?'
        reader.shops = [
            {'shop_name': 'PRIVATE_SHOP_A', 'shop_id': 'PRIVATE_ID_A', 'reminds': [], 'tags': [], 'actions': []},
            {'shop_name': 'PRIVATE_SHOP_B', 'shop_id': 'PRIVATE_ID_B', 'reminds': [], 'tags': [], 'actions': []},
        ]
        reader.debug = lambda _message: None
        with patch.object(config, 'TXT_DIR', temp_dir):
            json_path = reader._save()
        data = json.loads(Path(json_path).read_text(encoding='utf-8'))
        report = (Path(temp_dir) / 'shop_list_test.txt').read_text(encoding='utf-8')
        assert data['count'] == 2
        assert len(data['shops']) == 2
        assert '\u603b\u6570: 2' in report
        assert 'PRIVATE_SHOP' not in report
        assert 'PRIVATE_ID' not in report
    source = Path(shop_followup.__file__).read_text(encoding='utf-8')
    module = ast.parse(source)
    case_class = next(node for node in module.body
                      if isinstance(node, ast.ClassDef)
                      and node.name == 'ShopFollowupCase')
    fill_form = next(node for node in case_class.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == 'fill_form')
    calls = [node for node in ast.walk(fill_form)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    radio_values = [ast.literal_eval(node.args[1]) for node in calls
                    if node.func.attr == '_pick_radio']
    selected_first = any(node.func.attr == '_pick_select'
                         and len(node.args) == 1 for node in calls)
    note = next(ast.literal_eval(node.args[1]) for node in calls
                if node.func.attr == '_fill_textarea')
    assert radio_values == ['\u7535\u8bdd\u62dc\u8bbf', '\u4e86\u89e3\u9700\u6c42']
    assert selected_first
    assert note == '\u6709\u9700\u8981\u4f1a\u8054\u7cfb'
    print('PASS shop-list save and requested follow-up form values')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
