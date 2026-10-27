# -*- coding: utf-8 -*-
"""Explicit live publish-interval probe.

WARNING: this command clicks the real publish button. It can create a real
follow-up record for every shop at every tested interval. Run it only when
that side effect is intended.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "py"))

import main as app_main
from followup_batch import FollowupBatch
from followup_pressure import run_publish_interval_probe
from shop_list_reader import ShopListReader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--intervals",
        default="1,1.5,2,2.5,3,3.5,4,4.5,5,5.5,6",
        help="comma-separated click intervals in seconds",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "TXT" / "publish_interval_probe.json"),
    )
    args = parser.parse_args()
    intervals = tuple(float(item.strip()) for item in args.intervals.split(",") if item.strip())
    logger = app_main._make_debug_log("publish_probe")
    bot = app_main._cli_login(logger=logger)
    if bot is None:
        return 1
    try:
        reader = ShopListReader(bot, app_main.raw_log, debug=logger)
        listed = reader.run()
        shops = reader.shops
        if not listed.get("success") or not shops:
            logger("shop list unavailable; probe stopped")
            return 1

        def factory(gate):
            batch = FollowupBatch(
                bot,
                app_main.raw_log,
                app_main.styled_log,
                logger,
                dry_run=False,
                publish_gate=gate,
            )
            batch.reader = reader
            return batch

        results = run_publish_interval_probe(factory, shops, intervals)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "shop_count": len(shops),
            "intervals": list(intervals),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("probe report: %s" % output)
        return 0
    finally:
        try:
            bot.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
