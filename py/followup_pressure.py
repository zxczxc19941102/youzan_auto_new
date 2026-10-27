# -*- coding: utf-8 -*-
"""Analyze follow-up publish intervals and provide an explicit click gate.

The click gate never retries and never changes server response fields. It only
waits before a caller explicitly clicks the publish button.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

_TIMESTAMP_RE = re.compile(
    r"^\[(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})"
    r"(?:\.(?P<ms>\d{1,6}))?\]"
)
_POST_MARKER = "[API] -> POST "
_VISIT_SAVE_MARKER = "/visit/save"
_CLICK_MARKER = "\u53d1\u5e03\u70b9\u51fb\uff1a"
_CLICK_TS_RE = re.compile(r"\u65f6\u95f4\u6233=(?P<date>[^ ]+)\.(?P<ms>\d{3})")
_ERROR4_MARKER = "\u539f\u59cb\u9519\u8bef\u7801=4"
_ERROR_RE = re.compile(
    re.escape("\u539f\u59cb\u9519\u8bef\u7801=")
    + r"(?P<code>[^ ]*) "
    + re.escape("\u539f\u59cb\u9519\u8bef\u8bf4\u660e=")
    + r"(?P<message>.*)$"
)
_CODE_RE = re.compile(r"\bcode=(?P<code>[^ ]+)")
_SHOP_ID_RE = re.compile(r"ID=(?P<id>[0-9]+)")


def _timestamp_seconds(line: str) -> float | None:
    match = _TIMESTAMP_RE.match(line)
    if match is None:
        return None
    fraction = (match.group("ms") or "0")
    fraction = (fraction + "000000")[:6]
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(fraction) / 1_000_000
    )


class PublishIntervalGate:
    """Enforce a minimum interval between explicit publish clicks."""

    def __init__(
        self,
        interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_click = None

    def wait_before_click(self, _shop_id: str | None = None) -> float | None:
        with self._lock:
            now = self._clock()
            if self._last_click is None:
                self._last_click = now
                return None
            wait = max(0.0, self.interval_seconds - (now - self._last_click))
            if wait:
                self._sleep(wait)
            now = self._clock()
            delta = now - self._last_click
            self._last_click = now
            return delta


def _event_error(lines: list[str], start: int, end: int) -> dict:
    for line in lines[start:end]:
        if _ERROR4_MARKER not in line:
            continue
        match = _ERROR_RE.search(line)
        if match:
            return {
                "code": match.group("code"),
                "message": match.group("message"),
            }
        return {"code": "4", "message": ""}
    return {}


def parse_fill_log(path: str | Path) -> list[dict]:
    """Parse ordered publish submissions from one fill_all log.

    New logs use the millisecond timestamp emitted immediately before the
    publish click. Older logs fall back to the enclosing API POST timestamp.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    click_events = []
    for index, line in enumerate(lines):
        if _CLICK_MARKER not in line:
            continue
        match = _CLICK_TS_RE.search(line)
        if match is None:
            continue
        date_text = match.group("date")
        try:
            stamp = time.strptime(date_text, "%Y-%m-%d %H:%M:%S")
            timestamp = time.mktime(stamp) + int(match.group("ms")) / 1000.0
        except ValueError:
            continue
        click_events.append((index, timestamp))

    if click_events:
        event_ranges = click_events
    else:
        event_ranges = []
        for index, line in enumerate(lines):
            timestamp = _timestamp_seconds(line)
            if timestamp is not None and _POST_MARKER in line and _VISIT_SAVE_MARKER in line:
                event_ranges.append((index, timestamp))

    events = []
    for position, (start, timestamp) in enumerate(event_ranges):
        end = event_ranges[position + 1][0] if position + 1 < len(event_ranges) else len(lines)
        section = lines[start:end]
        error = _event_error(lines, start, end)
        shop_id = None
        for line in section:
            match = _SHOP_ID_RE.search(line)
            if match:
                shop_id = match.group("id")
        response_code = None
        for line in section:
            if "publish response" in line.lower() or "publish interface" in line.lower():
                match = _CODE_RE.search(line)
                if match:
                    response_code = match.group("code")
        events.append({
            "source": str(path),
            "index": position + 1,
            "timestamp_seconds": timestamp,
            "shop_id": shop_id,
            "response_code": response_code,
            "error_code": error.get("code"),
            "error_message": error.get("message"),
            "error4": bool(error),
            "timestamp_source": "click_millisecond" if click_events else "api_second",
        })
    for previous, current in zip(events, events[1:]):
        current["submit_interval_seconds"] = round(
            current["timestamp_seconds"] - previous["timestamp_seconds"], 6
        )
    if events:
        events[0]["submit_interval_seconds"] = None
    return events


def analyze_submission_intervals(paths: Iterable[str | Path]) -> dict:
    events = []
    per_file = []
    for raw_path in paths:
        current = parse_fill_log(raw_path)
        if not current:
            continue
        events.extend(current)
        per_file.append({
            "source": str(raw_path),
            "submissions": len(current),
            "error4": sum(1 for item in current if item["error4"]),
        })

    buckets = defaultdict(lambda: {"total": 0, "error4": 0})
    for event in events:
        interval = event.get("submit_interval_seconds")
        if interval is None:
            continue
        bucket = str(interval).rstrip("0").rstrip(".") or "0"
        buckets[bucket]["total"] += 1
        buckets[bucket]["error4"] += int(event["error4"])

    error_intervals = [
        item["submit_interval_seconds"] for item in events
        if item["error4"] and item.get("submit_interval_seconds") is not None
    ]
    no_error_intervals = [
        item["submit_interval_seconds"] for item in events
        if not item["error4"] and item.get("submit_interval_seconds") is not None
    ]
    by_source_safe = []
    last_error_interval = max(error_intervals) if error_intervals else None
    for source in sorted({item["source"] for item in events}):
        source_events = [item for item in events if item["source"] == source]
        clean = [
            item["submit_interval_seconds"] for item in source_events[1:]
            if not item["error4"]
            and item.get("submit_interval_seconds") is not None
            and (last_error_interval is None or item["submit_interval_seconds"] > last_error_interval)
        ]
        if clean:
            by_source_safe.append(min(clean))
    safe_recommendation = max(by_source_safe) if by_source_safe else (
        last_error_interval + 1 if last_error_interval is not None else None
    )

    return {
        "events": events,
        "files": per_file,
        "buckets": dict(sorted(buckets.items(), key=lambda item: float(item[0]))),
        "sample_count": len(events),
        "error4_count": sum(1 for item in events if item["error4"]),
        "last_error4_interval_seconds": max(error_intervals) if error_intervals else None,
        "first_no_error_interval_seconds": min(no_error_intervals) if no_error_intervals else None,
        "conservative_safe_recommendation_seconds": safe_recommendation,
        "limitations": [
            "Historical timestamps may have only one-second resolution.",
            "Global ordering in a multi-thread log is not a same-thread interval.",
            "Code 4 is inside visitReturnCodes; visitRecordId may still confirm the record was created.",
        ],
    }


def run_publish_interval_probe(
    batch_factory: Callable[[PublishIntervalGate], object],
    shops: list[dict],
    intervals: Iterable[float] = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0),
) -> list[dict]:
    """Run explicit sequential publish probes at increasing intervals.

    ``batch_factory`` must create a FollowupBatch-like object with the supplied
    gate. Each interval is a new batch and therefore may publish the same shop
    again. This helper is intentionally opt-in and does not retry a failed
    shop; it stops after the first interval with no failed item and no raw
    server warning.
    """
    results = []
    for raw_interval in intervals:
        interval = float(raw_interval)
        gate = PublishIntervalGate(interval)
        batch = batch_factory(gate)
        result = batch.run(
            all_shops=True,
            workers=1,
            skip_done=False,
            shops=shops,
            announce_count=False,
        )
        item = {
            "interval_seconds": interval,
            "result": result,
        }
        results.append(item)
        if result.get("failed", 0) == 0 and result.get("server_warnings", 0) == 0:
            break
    return results


def render_report(report: dict) -> str:
    lines = [
        "Follow-up submission interval analysis",
        "sample submissions: %s" % report["sample_count"],
        "error code 4 count: %s" % report["error4_count"],
        "largest preceding interval with error 4: %s seconds" % report["last_error4_interval_seconds"],
        "smallest preceding interval without error 4: %s seconds" % report["first_no_error_interval_seconds"],
        "conservative safe recommendation: %s seconds" % report["conservative_safe_recommendation_seconds"],
        "",
        "interval buckets:",
    ]
    for interval, value in report["buckets"].items():
        rate = value["error4"] / value["total"] if value["total"] else 0
        lines.append(
            "- %s seconds: samples=%d, error4=%d, rate=%.1f%%"
            % (interval, value["total"], value["error4"], rate * 100)
        )
    lines.extend(["", "limitations:"])
    lines.extend("- " + item for item in report["limitations"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fill-log-dir", default="TXT")
    parser.add_argument("--output", default="TXT/followup_pressure_report.json")
    args = parser.parse_args()
    paths = sorted(Path(args.fill_log_dir).glob("fill_all_*.log"))
    report = analyze_submission_intervals(paths)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_report(report), end="")
    print("JSON report: %s" % output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
