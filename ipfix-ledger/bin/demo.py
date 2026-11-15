#!/usr/bin/env python3
"""端到端演示：模板换代、变长/企业字段、拒绝与改正、追字段。

运行：python3 -I bin/demo.py
全部输入取自 tests/fixtures，输出为真实接入结果（不伪造）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ipfix_offline import (  # noqa: E402
    IpfixInputError,
    ResultStore,
    import_capture,
)
from ipfix_offline.inspect import inspect_capture  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="ipfix-demo-")
    gen_db = os.path.join(workdir, "generation.db")
    var_db = os.path.join(workdir, "variable.db")

    hr("1. 模板 256 换代接入：同消息 1 条 + 2 条，下一消息序列号 +3")
    report = import_capture(os.path.join(FIXTURES, "generation.jsonl"), gen_db)
    show_json({k: getattr(report, k) for k in (
        "capture_lines", "messages", "records", "template_definitions",
        "template_revocations", "template_scopes", "distinct_templates")})
    store = ResultStore(gen_db)
    for rec in store.query_records(capture_line=1):
        print(f"\n记录 #{rec['record_ordinal']}  行 {rec['capture_line']} "
              f"Set {rec['set_ordinal']} 记录 {rec['record_in_set']} "
              f"模板 {rec['template_id']} 代次 {rec['template_generation']} "
              f"序列号 {rec['sequence_number']}")
        for f in rec["fields"]:
            print(f"  字段{f['field_ordinal']} element={f['element_id']} "
                  f"{f['name']} 区间={f['value_range']} "
                  f"模板说明={f['template_field_range']} "
                  f"hex={f['value_hex']} 值={f['value']} ({f['value_type']})")
    print("\n下一消息（第 2 行）序列号：",
          store.get_message(2)["sequence_number"], "（1000 + 3）")

    hr("2. 追字段：旧记录在模板撤销/替换后仍能追到原始字节")
    trace = store.trace_field(1, 1)
    show_json({k: trace[k] for k in (
        "record_ordinal", "template_generation", "template_event_id",
        "value_range", "var_prefix_range", "template_field_range",
        "sliced_hex")})
    print("切片 == 保存字节：", trace["sliced_hex"] == trace["value_hex"])

    hr("3. 变长/零长/254/255/企业号/重复元素/IPv6/精简计数")
    import_capture(os.path.join(FIXTURES, "variable.jsonl"), var_db)
    vstore = ResultStore(var_db)
    for ordinal in (1, 2, 3):
        rec = vstore.get_record(ordinal)
        print(f"\n记录 #{ordinal}（行 {rec['capture_line']}）")
        for f in rec["fields"]:
            print(f"  {f['field_ordinal']:>2} el={f['element_id']:<5}"
                  f" ent={f['enterprise']:<5} decl={f['declared_length']:<5}"
                  f" act={f['actual_length']:<3} range={f['value_range']}"
                  f" var={f['var_prefix_range']} "
                  f"tplspec={f['template_field_range']} "
                  f"hex={f['value_hex'][:20]}"
                  f"{'...' if len(f['value_hex']) > 20 else ''}")

    hr("4. 拒绝演练：序列缺口 / 最后一行坏消息 / 数据在模板前")
    for name in ("sequence_gap", "last_line_bad", "data_before_template"):
        bad = os.path.join(FIXTURES, "rejections", f"{name}.bad.jsonl")
        db = os.path.join(workdir, f"{name}.db")
        try:
            import_capture(bad, db)
        except IpfixInputError as exc:
            print(f"\n{name}: 失败 -> {exc}")
            print("  目标库存在？", os.path.exists(db))
        good = os.path.join(FIXTURES, "rejections", f"{name}.good.jsonl")
        report = import_capture(good, os.path.join(workdir, f"{name}-good.db"))
        print(f"  改正后接入：{report.records} 条记录")

    hr("5. 只解码侦察（不落盘）")
    for msg in inspect_capture(os.path.join(FIXTURES, "generation.jsonl")):
        if msg["capture_line"] == 10:
            show_json({"capture_line": msg["capture_line"],
                       "sequence_number": msg["sequence_number"],
                       "definitions": [
                           {"template_id": d["template_id"],
                            "generation": d["generation"]}
                           for d in msg["definitions"]],
                       "records": [
                           {"template_generation": r["template_generation"],
                            "fields": [(f["element_id"], f["value"])
                                       for f in r["fields"]]}
                           for r in msg["records"]]})
            break
    print("\n演示结果库目录：", workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
