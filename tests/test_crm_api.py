# -*- coding: utf-8 -*-
"""CRM API payload and live-list verification regressions."""
import datetime as _dt
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py'))

from crm_api import CrmApiClient, CrmApiFollowupBatch  # noqa: E402
import config  # noqa: E402

CUST_LABEL = ''.join(chr(n) for n in (23458,25143,38656,27714,47,22791,27880))
DEFAULT_REMARK = ''.join(chr(n) for n in (26377,38656,35201,20250,32852,31995))
TEST_DATE = _dt.date.today().isoformat()


class FakePage:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def evaluate(self, script, args):
        if 'document.cookie' in script:
            return ''
        payload = self.payloads.pop(0)
        return {'status': 200, 'text': json.dumps(payload, ensure_ascii=False)}


class CrmApiTests(unittest.TestCase):
    def test_remark_prefers_customer_demand_field(self):
        client = CrmApiClient(FakePage([]), 'clue')
        client.form = {'formKey': 'visit_BD_qudao01', 'currentSystemDate': '2026-10-30'}
        client.fields = [
            {'name': 'yes_remark', 'label': '备注', 'type': 'TEXT'},
            {'name': 'visit_remark', 'label': '客户需求/备注', 'type': 'TEXT_AREA'},
        ]
        client.form_fields = {item['name']: item for item in client.fields}
        self.assertEqual(client._field('客户需求/备注', '备注')['name'], 'visit_remark')

    def test_clue_payload_uses_visit_remark(self):
        client = CrmApiClient(FakePage([]), 'clue')
        client.form = {'formKey': 'visit_BD_qudao01', 'currentSystemDate': '2026-10-30'}
        client.fields = [
            {'name': 'visit_contact', 'label': '联系人', 'options': []},
            {'name': 'visit_time', 'label': '跟进日期', 'options': []},
            {'name': 'visit_way', 'label': '跟进方式', 'options': [{'id': 15305, 'label': '电话拜访'}]},
            {'name': '跟进目的', 'label': '跟进目的', 'options': [{'id': 20596, 'label': '了解需求'}]},
            {'name': 'communication_situation', 'label': '沟通情况', 'options': [{'id': 17284, 'label': '电话打通，有交流'}]},
            {'name': 'yes_remark', 'label': '备注', 'options': []},
            {'name': 'visit_remark', 'label': '客户需求/备注', 'options': []},
        ]
        client.form_fields = {item['name']: item for item in client.fields}
        client.load_form = lambda: client.form
        client._contact = lambda _item: 'contact-value'
        payload = client.build_payload({'customerAlias': 'alias'})
        self.assertEqual(payload['form'].get('visit_remark'), '有需要会联系')
        self.assertNotIn('yes_remark', payload['form'])

    def test_verify_saved_requires_date_and_remark(self):
        row = {
            'id': 456,
            'createdTime': TEST_DATE + ' 14:00:00',
            'visitTime': TEST_DATE + ' 00:00:00',
            'resourceIds': [123],
            'visitContent': {CUST_LABEL: DEFAULT_REMARK},
        }
        client = CrmApiClient(FakePage([{'code': 0, 'data': {'items': [row], 'total': 1}}] * 8), 'customer')
        result = client.verify_saved({'opportunityId': 123})
        self.assertTrue(result['verified'])
        self.assertTrue(result['date_ok'])
        self.assertTrue(result['remark_ok'])


    def test_verify_saved_polls_eventual_consistency_without_resubmit(self):
        row = {
            'id': 567,
            'createdTime': TEST_DATE + ' 14:00:00',
            'visitTime': TEST_DATE + ' 00:00:00',
            'resourceIds': [123],
            'visitContent': {CUST_LABEL: DEFAULT_REMARK},
        }
        empty = {'code': 0, 'data': {'items': [], 'total': 0}}
        ready = {'code': 0, 'data': {'items': [row], 'total': 1}}
        client = CrmApiClient(FakePage([empty, ready]), 'customer')
        client._sleep = lambda _seconds: None
        result = client.verify_saved({'opportunityId': 123}, record_id=567)
        self.assertTrue(result['verified'])
        self.assertEqual(len(client.capture()), 2)

    def test_verify_saved_rejects_missing_date(self):
        row = {'id': 456, 'resourceIds': [123], 'visitContent': {CUST_LABEL: DEFAULT_REMARK}}
        payload = {'code': 0, 'data': {'items': [row], 'total': 1}}
        client = CrmApiClient(FakePage([payload] * 8), 'customer')
        result = client.verify_saved({'opportunityId': 123})
        self.assertFalse(result['verified'])
        self.assertFalse(result['date_ok'])


    def test_batch_logs_record_progress_and_new_skip_text(self):
        class FakeRuntime:
            stop_event = threading.Event()

            def __init__(self):
                self.progress = []

            def emit_progress(self, message, current, total):
                self.progress.append((message, current, total))

        class FakeApi:
            def verify_saved(self, item, **_kwargs):
                return {'verified': True}

            def save_capture(self):
                return None

            def capture(self):
                return []

        runtime = FakeRuntime()
        output = []
        batch = CrmApiFollowupBatch(
            bot=type('FakeBot', (), {'page': None})(),
            target_kind='customer',
            log=output.append,
            runtime=runtime,
            dry_run=False,
        )
        batch.api = FakeApi()
        original_interval = config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS
        try:
            config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS = 0
            result = batch.run(items=[
                {'id': 23967205, 'lastVisitTime': '2026-10-30 10:00:00'},
                {'id': 29043661, 'lastVisitTime': '2026-10-30 10:00:00'},
                {'id': 23961667, 'lastVisitTime': '2026-10-30 10:00:00'},
            ])
        finally:
            config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS = original_interval

        self.assertEqual(result['success'], 3)
        self.assertEqual([entry[1:] for entry in runtime.progress], [(1, 3), (1, 3), (2, 3), (3, 3)])
        self.assertIn('\u5f00\u59cb\u63a5\u53e3\u586b\u5199\u5ba2\u6237\u8ddf\u8fdb\u8bb0\u5f55\uff0c\u51713\u6761', runtime.progress[0][0])
        self.assertIn('\u8df3\u8fc7 \u6761\u76ee\u5ba2\u6237ID=23967205\uff1a\u4eca\u65e5\u5df2\u586b\u5199\u8ddf\u8fdb', runtime.progress[1][0])
        self.assertNotIn('\u5e42\u7b49\u4fdd\u62a4', '\n'.join(entry[0] for entry in runtime.progress))

if __name__ == '__main__':
    unittest.main()
