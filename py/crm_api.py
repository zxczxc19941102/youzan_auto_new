# -*- coding: utf-8 -*-
"""Youzan CRM clue/customer follow-up API layer."""
import datetime as _dt
import json
import time
from urllib.parse import quote

import config
from task_runtime import TaskCancelled

API_ORIGIN = 'https://qudao-api.youzan.com'
TARGET_API = {
    'clue': {
        'label': '线索', 'list_path': '/resource/my/list',
        'list_body': lambda page, size: {
            'moduleType': 1, 'viewId': '0', 'sortBy': 'bound_time', 'sortType': 'desc',
            'page': page, 'pageSize': size,
        },
        'form_target': 'customer', 'contact_path': '/visit/form/contacts',
        'alias_key': 'customerAlias', 'form_key_default': 'visit_BD_qudao01',
    },
    'customer': {
        'label': '客户', 'list_path': '/kaOpportunity/myOpportunities',
        'list_body': lambda page, size: {
            'moduleType': 10, 'viewId': '0', 'bizLineId': 0, 'opportunityType': 'ALL',
            'bindRole': 0, 'signState': 'ALL', 'customerType': '', 'industryCode': 0,
            'labels': [], 'sortBy': 'effect_time', 'sortType': 'desc',
            'page': page, 'pageSize': size,
        },
        'form_target': 'entity', 'contact_path': '/visit/form/entityContacts',
        'alias_key': 'bizCustomerIdAlias', 'form_key_default': 'entity_visit_wsc_qudao_01',
    },
}


def _today():
    return _dt.date.today().isoformat()


class CrmApiError(RuntimeError):
    def __init__(self, message, *, method='', url='', status=None, payload=None):
        super().__init__(message)
        self.method, self.url, self.status, self.payload = method, url, status, payload


class CrmApiClient:
    def __init__(self, page, target_kind, log=None, stop_event=None):
        if target_kind not in TARGET_API:
            raise ValueError('unknown CRM target: %s' % target_kind)
        self.page, self.target_kind = page, target_kind
        self.meta = TARGET_API[target_kind]
        self.log = log or (lambda message: None)
        self.stop_event = stop_event
        self.form = None
        self.form_fields = {}
        self.fields = []
        self._capture = []

    def _check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled()

    def _sleep(self, seconds):
        self._check()
        if self.stop_event is None:
            time.sleep(seconds)
        elif self.stop_event.wait(seconds):
            raise TaskCancelled()

    def _fetch(self, method, path, body=None, query=None):
        self._check()
        url = API_ORIGIN + path
        if query:
            url += '?' + '&'.join('%s=%s' % (quote(str(k)), quote(str(v))) for k, v in query.items())
        script = r'''async ({url, method, body, timeoutMs}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            const init = {method, credentials: 'include', headers: {}, signal: controller.signal};
            if (body !== null) {
                init.headers['content-type'] = 'application/json;charset=UTF-8';
                init.body = JSON.stringify(body);
            }
            try {
                const response = await fetch(url, init);
                return {status: response.status, text: await response.text()};
            } finally {
                clearTimeout(timer);
            }
        }'''
        last = None
        attempts = max(1, int(config.CRM_API_RETRY_ATTEMPTS))
        for attempt in range(attempts):
            try:
                result = self.page.evaluate(script, {'url': url, 'method': method, 'body': body, 'timeoutMs': int(config.CRM_API_REQUEST_TIMEOUT_MS)})
                status, raw = int(result.get('status') or 0), result.get('text') or ''
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {'raw': raw}
                self._capture.append({'method': method, 'path': path, 'url': url, 'status': status, 'request': body, 'response': payload})
                if status != 200:
                    raise CrmApiError('HTTP=%s response=%s' % (status, raw[:1000]), method=method, url=url, status=status, payload=payload)
                code = payload.get('code') if isinstance(payload, dict) else None
                if str(code) in ('43002', '500'):
                    raise CrmApiError('code=%s msg=%s response=%s' % (code, payload.get('msg') or payload.get('message') or '', raw[:1000]), method=method, url=url, status=status, payload=payload)
                return payload
            except TaskCancelled:
                raise
            except CrmApiError as exc:
                last = exc
            except Exception as exc:
                last = CrmApiError(repr(exc), method=method, url=url)
            if attempt + 1 < attempts:
                self._sleep(0.5 * (attempt + 1))
        raise last or CrmApiError('unknown API failure', method=method, url=url)

    def list_all(self):
        page, size, items = 1, max(10, int(config.CRM_API_PAGE_SIZE)), []
        total = None
        while True:
            payload = self._fetch('POST', self.meta['list_path'], self.meta['list_body'](page, size))
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                raise CrmApiError('list response missing items: %s' % payload, method='POST', url=API_ORIGIN + self.meta['list_path'], payload=payload)
            batch, total = data['items'], int(data.get('total') or 0)
            items.extend(batch)
            self.log('%s接口第%d页：返回%d条，total=%d' % (self.meta['label'], page, len(batch), total))
            if len(items) >= total or not batch or len(batch) < size:
                break
            page += 1
        return (items[:total] if total else items), total

    def load_form(self):
        if self.form is not None:
            return self.form
        payload = self._fetch('GET', '/visit/getVisitForm', query={'visitTarget': self.meta['form_target']})
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get('fields'), list):
            raise CrmApiError('form response missing fields: %s' % payload, method='GET', url=API_ORIGIN + '/visit/getVisitForm', payload=payload)
        self.form = data
        # Labels are not unique (multiple conditional “跟进目的” fields). Keep the ordered field list;
        # collapsing by label silently selected visit_target4 and caused code=10002.
        self.fields = [f for f in data['fields'] if isinstance(f, dict)]
        self.form_fields = {str(f.get('name') or f.get('label')): f for f in self.fields}
        return data

    def _field(self, *labels):
        self.load_form()
        # Exact labels must win over a shorter fallback label. The forms
        # contain both ``备注`` and ``客户需求/备注``; substring matching alone
        # selected ``yes_remark`` and put the value in the wrong field.
        wanted = [str(label) for label in labels]
        for label in wanted:
            for field in self.fields:
                if str(field.get('label') or '') != label:
                    continue
                if field.get('options') or field.get('name') in ('visit_contact', 'visit_time') or label in ('备注', '客户需求/备注'):
                    return field
        # Fallback for deployments whose labels contain extra wording.
        for field in self.fields:
            field_label = str(field.get('label') or '')
            if any(label in field_label for label in wanted):
                if field.get('options') or field.get('name') in ('visit_contact', 'visit_time'):
                    return field
        return {}

    @staticmethod
    def _option_value(field, label=None, first=False):
        options = field.get('options') or []
        if label:
            for option in options:
                if str(option.get('label') or '').strip() == label:
                    return option.get('id', option.get('value'))
        return (options[0].get('id', options[0].get('value')) if first and options else None)

    def _contact(self, item):
        alias = str(item.get(self.meta['alias_key']) or item.get('customerAlias') or item.get('bizCustomerIdAlias') or '')
        if not alias:
            raise CrmApiError('missing contact alias')
        key = 'customerAlias' if self.target_kind == 'clue' else 'bizCustomerIdAlias'
        payload = self._fetch('GET', self.meta['contact_path'], query={key: alias})
        data = payload.get('data') if isinstance(payload, dict) else None
        options = data.get('options') if isinstance(data, dict) else None
        value = data.get('defaultValue') if isinstance(data, dict) else None
        if not value and options:
            value = options[0].get('value') or options[0].get('id')
        if not value:
            raise CrmApiError('no contact option for alias=%s' % alias, method='GET', url=API_ORIGIN + self.meta['contact_path'], payload=payload)
        return value

    def build_payload(self, item):
        form_meta = self.load_form()
        contact, date_field = self._field('联系人'), self._field('跟进日期')
        way, purpose = self._field('跟进方式'), self._field('跟进目的')
        communication, remark = self._field('沟通情况'), self._field('客户需求/备注', '备注')
        form = {}
        if contact.get('name'): form[contact['name']] = self._contact(item)
        if date_field.get('name'): form[date_field['name']] = str(form_meta.get('currentSystemDate') or _today())[:10]
        if way.get('name'): form[way['name']] = self._option_value(way, '电话拜访')
        if purpose.get('name'): form[purpose['name']] = self._option_value(purpose, '了解需求', first=True)
        if communication.get('name'): form[communication['name']] = self._option_value(communication, first=True)
        if remark.get('name'): form[remark['name']] = '有需要会联系'
        body = {'formKey': form_meta.get('formKey') or self.meta['form_key_default'], 'form': form}
        # The customer list calls this field customerIdAlias, while /visit/save
        # expects the same value under customerAlias plus bizCustomerIdAlias.
        aliases = {
            'customerAlias': item.get('customerAlias') or item.get('customerIdAlias'),
            'bizCustomerIdAlias': item.get('bizCustomerIdAlias'),
        }
        for key, value in aliases.items():
            if value:
                body[key] = value
        return body

    def is_success(self, payload):
        if not isinstance(payload, dict): return False
        return str(payload.get('code')) in ('0', '200', '20000') or bool((payload.get('data') or {}).get('visitRecordId'))

    @staticmethod
    def _last_remark(item):
        value = item.get('lastFollowBrief')
        if value:
            return str(value).strip()
        raw = item.get('lastFollow')
        if isinstance(raw, list):
            records = raw
        else:
            try:
                records = json.loads(raw or '[]')
            except (TypeError, ValueError):
                records = []
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict) and record.get('content') is not None:
                    return str(record.get('content')).strip()
        return ''

    def _current_user_id(self):
        try:
            raw = self.page.evaluate("() => document.cookie") or ''
            for part in raw.split(';'):
                key, _, value = part.strip().partition('=')
                if key == 'user_id' and value.strip():
                    return int(value.strip())
        except Exception:
            pass
        return None

    def _verify_saved_once(self, item, expected_date, expected_remark, record_id=None):
        """Perform one authoritative read-after-write check."""
        payload = self._fetch('POST', '/visit/search', {
            'page': 1,
            'pageSize': 100,
            'sortBy': 'createdTime',
            'sortType': 'desc',
            'userId': self._current_user_id(),
            'userTypeCode': None,
        })
        data = payload.get('data') if isinstance(payload, dict) else None
        rows = data.get('items') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise CrmApiError('visit search response missing items: %s' % payload, payload=payload)
        expected_ids = {
            str(item.get('id') or ''),
            str(item.get('opportunityId') or ''),
            str(item.get('customerId') or ''),
        } - {''}
        expected_aliases = {
            str(item.get('customerAlias') or ''),
            str(item.get('customerIdAlias') or ''),
            str(item.get('bizCustomerIdAlias') or ''),
        } - {''}
        matched = None
        for row in rows:
            contact = row.get('visitContactInfo') if isinstance(row, dict) else {}
            contact = contact if isinstance(contact, dict) else {}
            # Prefer the exact ID from /visit/save; aliases can have older rows.
            if record_id is not None and str(row.get('id') or '') == str(record_id):
                matched = row
                break
            row_ids = {str(value) for value in (row.get('resourceIds') or []) if value is not None}
            row_ids.update({str(contact.get('customerId') or '')} - {''})
            row_aliases = {str(contact.get('customerAlias') or '')} - {''}
            if (expected_ids & row_ids) or (expected_aliases & row_aliases):
                matched = row
                break
        if matched is None:
            return {'verified': False, 'found': False, 'date_ok': False, 'remark_ok': False, 'row': None}
        content = matched.get('visitContent') if isinstance(matched, dict) else {}
        content = content if isinstance(content, dict) else {}
        remark_ok = str(content.get('\u5ba2\u6237\u9700\u6c42/\u5907\u6ce8') or content.get('\u5907\u6ce8') or '').strip() == str(expected_remark).strip()
        date_ok = any(
            str(matched.get(key) or '').startswith(expected_date)
            for key in ('createdTime', 'visitTime')
        )
        return {
            'verified': bool(date_ok and remark_ok),
            'found': True,
            'date_ok': bool(date_ok),
            'remark_ok': bool(remark_ok),
            'row': matched,
        }

    def verify_saved(self, item, expected_date=None, expected_remark='\u6709\u9700\u8981\u4f1a\u8054\u7cfb', record_id=None):
        """Poll the visit index briefly after a successful save.

        /visit/save commits before /visit/search becomes consistent. This is a
        bounded read-only poll; it never resubmits the follow-up.
        """
        expected_date = expected_date or _today()
        attempts = max(1, int(config.CRM_API_VERIFY_ATTEMPTS))
        last_result = None
        for attempt in range(attempts):
            last_result = self._verify_saved_once(item, expected_date, expected_remark, record_id)
            if last_result.get('verified'):
                return last_result
            if attempt + 1 < attempts:
                self._sleep(config.CRM_API_VERIFY_POLL_SECONDS)
        return last_result or {'verified': False, 'found': False, 'date_ok': False, 'remark_ok': False, 'row': None}

    def submit(self, item):
        return self._fetch('POST', '/visit/save', self.build_payload(item))

    def capture(self): return list(self._capture)

    def save_capture(self, run_id=None):
        config.ensure_dirs()
        stamp = run_id or time.strftime('%Y%m%d_%H%M%S')
        path = config.TXT_DIR + '/crm_api_capture_%s_%s.json' % (self.target_kind, stamp)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'target_kind': self.target_kind, 'records': self._capture}, handle, ensure_ascii=False, indent=2)
        return path


class CrmApiFollowupBatch:
    def __init__(self, bot, target_kind, log, debug=None, dry_run=False, runtime=None):
        self.bot, self.target_kind, self.log = bot, target_kind, log
        self.debug = debug or (lambda message: None)
        self.dry_run, self.runtime = bool(dry_run), runtime
        self.stop_event = runtime.stop_event if runtime is not None else None
        self.api = CrmApiClient(bot.page, target_kind, self.debug, self.stop_event)
        self.results = []

    def _check(self):
        if self.stop_event is not None and self.stop_event.is_set(): raise TaskCancelled()

    def _sleep(self, seconds):
        self._check()
        if self.stop_event is None: time.sleep(seconds)
        elif self.stop_event.wait(seconds): raise TaskCancelled()

    def _log_progress(self, message, current, total):
        """Emit a CRM batch line with record position instead of worker-pool counts."""
        if self.runtime is not None and hasattr(self.runtime, 'emit_progress'):
            self.runtime.emit_progress(message, current, total)
        else:
            self.log(message)

    def _already_done(self, item):
        if not config.CRM_API_SKIP_TODAY_DUPLICATES:
            return False
        today = _today()
        if any(str(item.get(k) or '').startswith(today) for k in ('lastVisitTime', 'lastFollowTime', 'lastVisitDate')):
            return True
        # The clue list can lag and omit lastVisitTime even after a successful
        # save. Consult the authoritative visit-search list before deciding to
        # submit again, preserving idempotence without blind duplicate writes.
        try:
            return bool(self.api.verify_saved(item).get('verified'))
        except (CrmApiError, TaskCancelled):
            raise
        except Exception:
            return False

    def run(self, items=None, count=None):
        if items is None: items, total = self.api.list_all()
        else: items, total = list(items), len(items)
        targets = items[:count] if count else items
        label = TARGET_API[self.target_kind]['label']
        progress_total = total
        self._log_progress(
            '\u5f00\u59cb\u63a5\u53e3\u586b\u5199%s\u8ddf\u8fdb\u8bb0\u5f55\uff0c\u5171%d\u6761%s' % (
                label, total, '\uff08\u8bd5\u8fd0\u884c\uff0c\u4e0d\u63d0\u4ea4\uff09' if self.dry_run else ''
            ),
            1 if progress_total else 0, progress_total,
        )
        for position, item in enumerate(targets, 1):
            self._check()
            item_id = str(item.get('opportunityId') or item.get('id') or item.get('customerAlias') or item.get('bizCustomerIdAlias') or position)
            try:
                if self._already_done(item):
                    self._log_progress(
                        '\u8df3\u8fc7 \u6761\u76ee%sID=%s\uff1a\u4eca\u65e5\u5df2\u586b\u5199\u8ddf\u8fdb' % (label, item_id),
                        position, progress_total,
                    )
                    self.results.append({'success': True, 'skipped': True, 'item_id': item_id})
                else:
                    request_body = self.api.build_payload(item)
                    if self.dry_run:
                        self._log_progress(
                            '\u6210\u529f \u6761\u76ee%sID=%s\uff08\u63a5\u53e3\u8bd5\u8fd0\u884c\uff0c\u672a\u63d0\u4ea4\uff09' % (label, item_id),
                            position, progress_total,
                        )
                        self.results.append({'success': True, 'dry_run': True, 'item_id': item_id, 'request': request_body})
                    else:
                        response = self.api._fetch('POST', '/visit/save', request_body)
                        if not self.api.is_success(response):
                            raise CrmApiError(
                                '原始响应=%s' % json.dumps(response, ensure_ascii=False),
                                payload=response,
                            )
                        record_id = (response.get('data') or {}).get('visitRecordId')
                        verification = self.api.verify_saved(
                            item, expected_remark='\u6709\u9700\u8981\u4f1a\u8054\u7cfb', record_id=record_id
                        )
                        if not verification.get('verified'):
                            detail = '回查未确认：found=%s date_ok=%s remark_ok=%s' % (
                                verification.get('found'), verification.get('date_ok'), verification.get('remark_ok')
                            )
                            raise CrmApiError(
                                '提交响应code=0，但%s；原始响应=%s' % (
                                    detail, json.dumps(response, ensure_ascii=False)
                                ),
                                payload={'response': response, 'verification': verification},
                            )
                        self._log_progress(
                            '\u6210\u529f \u6761\u76ee%sID=%s visitRecordId=%s\uff0c\u56de\u67e5\u901a\u8fc7'
                            % (label, item_id, record_id or '-'),
                            position, progress_total,
                        )
                        self.results.append({
                            'success': True, 'item_id': item_id, 'visitRecordId': record_id,
                            'response': response, 'verification': verification,
                        })
            except TaskCancelled: raise
            except Exception as exc:
                self._log_progress(
                    '\u5931\u8d25 \u6761\u76ee%sID=%s\uff1a%s' % (label, item_id, str(exc)),
                    position, progress_total,
                )
                self.results.append({'success': False, 'item_id': item_id, 'error': str(exc)})
            if position < len(targets): self._sleep(config.FOLLOWUP_PUBLISH_INTERVAL_SECONDS)
        success = sum(1 for x in self.results if x.get('success'))
        capture_path = self.api.save_capture()
        return {'attempted': len(targets), 'total': total, 'success': success, 'failed': len(self.results)-success, 'skipped': sum(1 for x in self.results if x.get('skipped')), 'results': self.results, 'api_capture': self.api.capture(), 'api_capture_path': capture_path}
