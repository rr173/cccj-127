"""演示脚本：正向转换 / 反向候选 / 覆盖率 / 封存 / 冲突发布的真实输出。

运行：.venv/bin/python demo.py
"""
from __future__ import annotations

import json
from datetime import date

from service import MappingError, MappingService


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = MappingService()

    # 1. 正向转换：区间交界（CRM 2025-07-01 换码，半开区间）
    show("正向：2025-06-30 CRM:A（旧码，合并到 ACTIVE）",
         svc.forward_convert("CRM", "CUSTOMER_STATUS", "A",
                             date(2025, 6, 30), "v2025"))
    show("正向：2025-07-01 CRM:ACT（交界当天新码生效）",
         svc.forward_convert("CRM", "CUSTOMER_STATUS", "ACT",
                             date(2025, 7, 1), "v2025"))

    # 2. 无对应项：v2024 下 POS:2 尚无目标码，带业务原因与批准人
    show("正向：2024 年 POS:2 暂时无对应项",
         svc.forward_convert("POS", "CUSTOMER_STATUS", "2",
                             date(2024, 5, 1), "v2024"))

    # 3. 反向候选：ACTIVE 在 m2024 下有三个来源（含合并组 g1）
    show("反向：ACTIVE 的多候选来源（m2024）",
         svc.reverse_lookup("ACTIVE", date(2024, 5, 1), "v2024"))

    # 4. 覆盖率核验：m2024 含两个显式无对应项
    show("覆盖率：m2024", svc.coverage("m2024"))
    show("覆盖率：m2026（全部映射）", svc.coverage("m2026"))

    # 5. 冲突发布：一次性返回全部违规（演示可重复运行：先清理上次残留草稿）
    with svc.connect() as conn:
        conn.execute(
            "DELETE FROM mapping_row WHERE mapping_version_id IN "
            "(SELECT id FROM mapping_version WHERE version_label='demo_bad_draft')")
        conn.execute("DELETE FROM mapping_version WHERE version_label='demo_bad_draft'")
        vid = svc.create_draft(conn, "v2026", "demo_bad_draft",
                               date(2026, 1, 1))
        bad_rows = [
            {"system_code": "CRM", "code_domain": "CUSTOMER_STATUS",
             "source_code": "ACT", "target_code": "GHOST",
             "mapping_group_id": "g10", "group_type": "ONE_TO_ONE",
             "effective_from": date(2026, 1, 1), "effective_to": None,
             "business_reason": "目标码不存在", "approver": "演示"},
            {"system_code": "CRM", "code_domain": "CUSTOMER_STATUS",
             "source_code": "ACT", "target_code": "ACTIVE",
             "mapping_group_id": "g10", "group_type": "ONE_TO_ONE",
             "effective_from": date(2026, 2, 1), "effective_to": date(2026, 3, 1),
             "business_reason": "与上一条重叠", "approver": "演示"},
        ]
        for r in bad_rows:
            svc.add_mapping_row(conn, vid, r)
    try:
        svc.publish(vid)
    except MappingError as e:
        show(f"冲突发布被拦截（草稿 id={vid}，共 {len(e.violations)} 类问题）",
             e.violations)

    # 6. 封存：m2025 被报表引用后不可改写
    with svc.connect() as conn:
        sealed = conn.execute(
            """
            INSERT INTO report_snapshot(report_code, report_period, mapping_version_id)
            SELECT 'R1_CUSTOMER_MONTHLY', %s, id
              FROM mapping_version WHERE version_label='m2025' AND status='published'
            ON CONFLICT (report_code, report_period) DO UPDATE
                SET report_code = EXCLUDED.report_code
            RETURNING id, report_code, report_period, mapping_version_id
            """,
            (date(2025, 3, 31),),
        ).fetchone()
    show("封存报表 R1 到映射版本 m2025", dict(sealed))


if __name__ == "__main__":
    main()
