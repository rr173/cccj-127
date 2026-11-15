#!/usr/bin/env python3
"""生成验收目录：examples/catalog（manifest + 数据文件）与 examples/queries。

目录混放三代 spec：
- old-2023     账户 truncate(10)（宽区间）+ 地区 identity（全名）
- narrow-2024  账户 truncate(4)（收窄）   + 地区 prefix(2)
- prefix-2025  账户 identity              + 地区 prefix(1)
- dual-2025    账户 truncate(10) 与 identity 双分区字段（同源交集）+ 地区 void
- void-2025    账户 void                  + 地区 prefix(2)

分区说明名称故意相同或改名（"账户区间" 在 old/narrow/dual 中重复出现、
"地区" 一会儿是 identity 一会儿是 prefix），验证关联只靠 field_id/
partition_id，绝不靠说明名称。

数据覆盖：-11、-10、-1、0、3、4、int64 两端、null、空串、中文与非 BMP
字符（𠮷，U+20BB7）。生成是幂等的，重复运行字节一致。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

CATALOG_DIR = os.path.join(ROOT, "examples", "catalog")
QUERIES_DIR = os.path.join(ROOT, "examples", "queries")

SCHEMA = {
    "fields": [
        {"field_id": 1, "name": "account", "type": "int64", "nullable": True},
        {"field_id": 2, "name": "region", "type": "string", "nullable": True},
    ]
}

SPECS = [
    {"spec_id": "old-2023", "partition_fields": [
        {"partition_id": 1, "name": "账户区间", "source_id": 1,
         "transform": {"kind": "truncate", "width": 10}},
        {"partition_id": 2, "name": "地区", "source_id": 2,
         "transform": {"kind": "identity"}},
    ]},
    {"spec_id": "narrow-2024", "partition_fields": [
        {"partition_id": 1, "name": "账户区间", "source_id": 1,
         "transform": {"kind": "truncate", "width": 4}},
        {"partition_id": 2, "name": "地区", "source_id": 2,
         "transform": {"kind": "prefix", "length": 2}},
    ]},
    {"spec_id": "prefix-2025", "partition_fields": [
        {"partition_id": 1, "name": "账户", "source_id": 1,
         "transform": {"kind": "identity"}},
        {"partition_id": 2, "name": "地区前缀", "source_id": 2,
         "transform": {"kind": "prefix", "length": 1}},
    ]},
    {"spec_id": "dual-2025", "partition_fields": [
        {"partition_id": 1, "name": "账户区间", "source_id": 1,
         "transform": {"kind": "truncate", "width": 10}},
        {"partition_id": 2, "name": "账户快照", "source_id": 1,
         "transform": {"kind": "identity"}},
        {"partition_id": 3, "name": "地区", "source_id": 2,
         "transform": {"kind": "void"}},
    ]},
    {"spec_id": "void-2025", "partition_fields": [
        {"partition_id": 1, "name": "账户", "source_id": 1,
         "transform": {"kind": "void"}},
        {"partition_id": 2, "name": "地区", "source_id": 2,
         "transform": {"kind": "prefix", "length": 2}},
    ]},
]

# (file_id, spec_id, partitions, rows)；rows 为 (account, region) 列表。
FILES = [
    ("f-old-wide", "old-2023", {"1": -10, "2": "京𠮷照"},
     [(-10, "京𠮷照"), (-5, "京𠮷照"), (-1, "京𠮷照")]),
    ("f-new-narrow", "narrow-2024", {"1": -4, "2": "京𠮷"},
     [(-4, "京𠮷"), (-3, "京𠮷照"), (-1, "京𠮷")]),
    ("f-same-source", "dual-2025", {"1": -10, "2": -1, "3": None},
     [(-1, "沪"), (-1, None), (-1, "京")]),
    ("f-all-null", "prefix-2025", {"1": None, "2": None},
     [(None, None), (None, None)]),
    ("f-void", "void-2025", {"1": None, "2": "京𠮷"},
     [(INT64_MIN, "京𠮷"), (0, "京𠮷照"), (None, "京𠮷"),
      (INT64_MAX, "京𠮷照")]),
    ("f-empty", "old-2023", {"1": 0, "2": "沪"}, []),
    ("f-short-prefix", "narrow-2024", {"1": 0, "2": "京"},
     [(0, "京"), (1, "京"), (3, "京")]),
    ("f-empty-str", "narrow-2024", {"1": 4, "2": ""},
     [(4, ""), (5, "")]),
    ("f-neg-bucket", "old-2023", {"1": -9223372036854775810, "2": "沪"},
     [(INT64_MIN, "沪"), (-9223372036854775801, "沪")]),
    ("f-pos-mixed", "prefix-2025", {"1": 3, "2": "京"},
     [(3, "京"), (3, "京𠮷"), (3, "京𠮸")]),
    ("f-old-neg", "old-2023", {"1": -20, "2": "沪"},
     [(-20, "沪"), (-11, "沪")]),
    ("f-old-2", "old-2023", {"1": 0, "2": "京𠮷"},
     [(0, "京𠮷"), (4, "京𠮷")]),
]

QUERIES = {
    "q1_not_account_lt_neg3":
        {"op": "not", "child": {"op": "lt", "field_id": 1, "value": -3}},
    "q2_empty_and":
        {"op": "and", "children": [
            {"op": "ge", "field_id": 1, "value": -3},
            {"op": "lt", "field_id": 1, "value": -3}]},
    "q3_not_empty_and":
        {"op": "not", "child": {"op": "and", "children": [
            {"op": "ge", "field_id": 1, "value": -3},
            {"op": "lt", "field_id": 1, "value": -3}]}},
    "q4_region_eq":
        {"op": "eq", "field_id": 2, "value": "京𠮷"},
    "q5_region_adjacent":
        {"op": "and", "children": [
            {"op": "ge", "field_id": 2, "value": "京𠮷"},
            {"op": "lt", "field_id": 2, "value": "京𠮸"}]},
    "q6_region_lt":
        {"op": "lt", "field_id": 2, "value": "京𠮷"},
    "q7_account_eq_neg1":
        {"op": "eq", "field_id": 1, "value": -1},
    "q8_mixed":
        {"op": "and", "children": [
            {"op": "or", "children": [
                {"op": "lt", "field_id": 1, "value": -3},
                {"op": "gt", "field_id": 1, "value": 0}]},
            {"op": "eq", "field_id": 2, "value": "京𠮷"}]},
    "q9_is_null_account":
        {"op": "is_null", "field_id": 1},
    "q10_region_empty":
        {"op": "eq", "field_id": 2, "value": ""},
    "q11_extreme_range":
        {"op": "and", "children": [
            {"op": "ge", "field_id": 1, "value": INT64_MIN},
            {"op": "lt", "field_id": 1, "value": -9223372036854775800}]},
}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    os.makedirs(os.path.join(CATALOG_DIR, "data"), exist_ok=True)
    os.makedirs(QUERIES_DIR, exist_ok=True)

    files_json = []
    for file_id, spec_id, partitions, rows in FILES:
        rel = f"data/{file_id}.jsonl"
        files_json.append({
            "file_id": file_id,
            "path": rel,
            "spec_id": spec_id,
            "row_count": len(rows),
            "partitions": partitions,
        })
        lines = []
        for account, region in rows:
            lines.append(json.dumps({"1": account, "2": region},
                                    ensure_ascii=False))
        with open(os.path.join(CATALOG_DIR, rel), "w",
                  encoding="utf-8", newline="\n") as fh:
            for line in lines:
                fh.write(line + "\n")

    manifest = {"schema": SCHEMA, "specs": SPECS, "files": files_json}
    with open(os.path.join(CATALOG_DIR, "manifest.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(_dump(manifest))

    for name, query in QUERIES.items():
        with open(os.path.join(QUERIES_DIR, f"{name}.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(_dump(query))

    print(f"已生成 {len(files_json)} 个数据文件、{len(QUERIES)} 个查询："
          f"{CATALOG_DIR} 与 {QUERIES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
