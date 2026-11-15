"""端到端演示：离线把各夹具展开成区间事实，并展示输入故障被拒绝。

运行：python3 -I bin/demo.py
所有输出来自真实展开；期望值的手工推导见 tests/ 下各测试的注释。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from calendar_facts import CalendarFactService, CalendarInputError  # noqa: E402
from tests._helpers import fixture, error_fixture  # noqa: E402

TZ = "America/New_York"


def show(title, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    svc = CalendarFactService(TZ)
    tmp = tempfile.mkdtemp(prefix="cf-demo-")
    facts_path = os.path.join(tmp, "facts.jsonl")
    report_path = os.path.join(tmp, "report.json")

    # 1) DST：春季缺口跳过且 COUNT 补足、回拨取较早 UTC、月末/倒数规则
    report = svc.expand_file(
        fixture("dst.ics"),
        "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z",
        facts_path, report_path)
    show("DST 展开（事实计数 + 被排除项）", {
        "fact_count": report.fact_count,
        "excluded": report.to_json()["excluded"],
        "fact_file": facts_path,
    })
    print("前 3 行事实：")
    with open(facts_path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 3:
                break
            print("  " + json.dumps(json.loads(line), ensure_ascii=False))

    # 2) 例外归属：窗外挪入 / 窗内挪出 / 取消 / 稳定身份
    report = svc.expand_file(
        fixture("overrides.ics"),
        "2024-03-11T00:00:00Z", "2024-03-25T00:00:00Z",
        facts_path, report_path)
    show("例外归属（审计 + 取消/排除）", {
        "fact_count": report.fact_count,
        "overrides": report.overrides,
        "excluded": report.to_json()["excluded"],
    })

    # 3) 全天事件跨春秋切换：23/25 小时、例外改长度
    report = svc.expand_file(
        fixture("allday.ics"),
        "2024-11-01T00:00:00Z", "2024-11-10T00:00:00Z",
        facts_path, report_path)
    show("全天事件秋季窗口（注意 25 小时与 48 小时）", {
        "fact_count": report.fact_count,
        "excluded": report.to_json()["excluded"],
    })

    # 4) 输入故障：整份拒绝，不产出结果
    try:
        svc.expand_file(
            error_fixture("exdate_conflict.ics"),
            "2024-03-01T00:00:00Z", "2024-04-01T00:00:00Z",
            facts_path, report_path)
    except CalendarInputError as exc:
        show("输入故障被拒绝（EXDATE 与例外冲突）", {"problems": exc.problems})

    print("\n演示完成。")


if __name__ == "__main__":
    main()
