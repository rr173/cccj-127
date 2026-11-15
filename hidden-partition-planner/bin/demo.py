#!/usr/bin/env python3
"""端到端演示：在验收目录上跑 plan 与 scan，打印真实执行结果。

用法：python3 -I bin/demo.py [输出目录]
输出目录缺省为 /tmp/hpq-demo.<pid>；已存在的文件不会被覆盖。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hidden_partition import run_plan, run_scan  # noqa: E402

CATALOG = os.path.join(ROOT, "examples", "catalog", "manifest.json")
QUERIES = os.path.join(ROOT, "examples", "queries")

SHOWCASE = [
    ("q1_not_account_lt_neg3", [1, 2]),
    ("q5_region_adjacent", [1, 2]),
    ("q7_account_eq_neg1", [2, 1]),
]


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else f"/tmp/hpq-demo.{os.getpid()}"
    os.makedirs(out_dir, exist_ok=True)
    for name, projection in SHOWCASE:
        query_path = os.path.join(QUERIES, f"{name}.json")
        plan_path = os.path.join(out_dir, f"{name}.plan.json")
        rows_path = os.path.join(out_dir, f"{name}.rows.jsonl")
        report_path = os.path.join(out_dir, f"{name}.report.json")
        plan = run_plan(CATALOG, query_path, plan_path)
        print(f"\n=== {name} ===")
        for entry in plan["files"]:
            print(f"  {entry['file_id']:15s} {entry['decision']:7s}"
                  f"  {entry['reasons'][-1]}")
        report = run_scan(CATALOG, query_path, rows_path, report_path,
                          projection=projection)
        summary = report["summary"]
        print(f"  → 打开文件 {summary['files_opened']} 个，"
              f"读取 {summary['rows_read']} 行，"
              f"交付 {summary['rows_delivered']} 行；"
              f"峰值 RSS {report['peak_rss_bytes']} 字节")
        with open(rows_path, "r", encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        for row in rows[:8]:
            print(f"    {json.dumps(row, ensure_ascii=False)}")
        if len(rows) > 8:
            print(f"    ……共 {len(rows)} 行")
    print(f"\n计划、结果与报告已写入 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
