# -*- coding: utf-8 -*-
"""回归测试：发布响应已经到达时，verify 不能把成功判成失败（假阴性）。

背景：旧实现只在固定 sleep 里轮询 recorder，响应事件根本不会派发，publish_response
恒为 None；即使响应已经写进 recorder.records，verify 也只读 None 并抛错。本次日志
中 30 家店铺实际全部写入成功（visitRecordId 唯一），却被判 0 成功 30 失败。

本测试不启动浏览器，用假的 page/response 验证修复后的判定逻辑。

运行：
    venv\\Scripts\\python.exe tests\\test_verify_false_negative.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'py'))

from shop_followup import ShopFollowupCase  # noqa: E402


class FakeRequest:
    method = 'POST'


class FakeResponse:
    request = FakeRequest()
    url = 'https://qudao-api.youzan.com/visit/save'
    status = 200

    def text(self):
        return '{"code":0,"data":{"visitRecordId":56566253},' \
               '"msg":"操作成功"}'


class FakePage:
    def __init__(self, text='跟进记录 基本信息 店铺名称'):
        self.text = text
        self.pumped = 0

    def wait_for_timeout(self, _ms):
        self.pumped += 1

    def inner_text(self, _selector):
        return self.text


class FakeBot:
    def __init__(self):
        self.page = FakePage()


def main():
    checks = []

    case = ShopFollowupCase(FakeBot(), lambda _m: None)
    record = case._record_from_response(FakeResponse())
    checks.append(('解析响应 status', record.get('status') == 200))
    checks.append(('解析响应 code', record.get('resp_code') == '0'))
    checks.append(('_response_ok', case._response_ok(record) is True))
    checks.append((
        '_is_publish_response',
        ShopFollowupCase._is_publish_response(FakeResponse()) is True,
    ))
    checks.append(('业务提示', '操作成功' in case._response_msg(record)))

    # 模拟旧实现留下的现场：publish_response=None，但事件记录里已有成功响应
    case2 = ShopFollowupCase(FakeBot(), lambda _m: None)
    case2.recorder.records.append(record)
    case2._publish_before = 0
    outcome = case2.verify()
    checks.append(('迟到响应可判定成功', 'api=True' in outcome))
    checks.append(('publish_response 已补齐', case2.publish_response is record))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print('%s %s' % ('PASS' if ok else 'FAIL', name))
    print('RESULT =', 'PASS' if not failed else 'FAIL(%s)' % ','.join(failed))
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
