# -*- coding: utf-8 -*-
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from followup_pressure import (
    PublishIntervalGate, _ERROR4_MARKER, analyze_submission_intervals, parse_fill_log,
    run_publish_interval_probe,
)


def test_parse_and_analyze_intervals(tmp_path=None):
    holder = Path(tmp_path) if tmp_path is not None else Path(tempfile.mkdtemp())
    path = holder / "fill_all_test.log"
    path.write_text(
        "\n".join([
            "[10:00:00] [API] -> POST https://example/visit/save",
            "[10:00:01] [API] -> POST https://example/visit/save",
            f"[10:00:01] {_ERROR4_MARKER} original",
            "[10:00:04] [API] -> POST https://example/visit/save",
            "[10:00:05] publish interface POST /visit/save HTTP=200 code=0",
        ]) + "\n",
        encoding="utf-8",
    )
    events = parse_fill_log(path)
    assert len(events) == 3
    assert events[1]["submit_interval_seconds"] == 1.0
    assert events[1]["error4"] is True
    assert events[2]["submit_interval_seconds"] == 3.0
    report = analyze_submission_intervals([path])
    assert report["error4_count"] == 1
    assert report["last_error4_interval_seconds"] == 1.0
    assert report["conservative_safe_recommendation_seconds"] == 3.0


def test_publish_interval_probe_stops_without_retry():
    seen = []

    class FakeBatch:
        def __init__(self, gate):
            seen.append(gate.interval_seconds)

        def run(self, **_kwargs):
            if len(seen) == 1:
                return {"failed": 0, "server_warnings": 1}
            return {"failed": 0, "server_warnings": 0}

    results = run_publish_interval_probe(
        lambda gate: FakeBatch(gate), [{"shop_id": "1"}], intervals=(1.0, 1.5, 2.0)
    )
    assert seen == [1.0, 1.5]
    assert [item["interval_seconds"] for item in results] == [1.0, 1.5]


def test_publish_interval_gate_enforces_interval():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    gate = PublishIntervalGate(1.5, sleep=sleep, clock=clock)
    assert gate.wait_before_click("a") is None
    now[0] += 0.2
    assert round(gate.wait_before_click("b"), 3) == 1.5
    assert sleeps == [1.3]


if __name__ == "__main__":
    test_parse_and_analyze_intervals()
    test_publish_interval_probe_stops_without_retry()
    test_publish_interval_gate_enforces_interval()
    print("PASS follow-up pressure analysis")
