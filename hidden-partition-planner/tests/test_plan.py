"""验收目录上的手写计划期望：每个查询 × 每个文件的 drop/pass/filter。

期望决策是手工从分区原像推出的（见 README 第 6 节的推导表），
与实现相互核对；剩余条件必须能重新通过查询合同校验，且在该文件
所有数据行上与原查询等价（用独立参考核对）。
"""
from __future__ import annotations

import os
import unittest

from hidden_partition import (
    CatalogError,
    build_plan,
    load_manifest,
    load_query,
    parse_query,
    run_plan,
)
from hidden_partition.errors import TargetExistsError

from ._helpers import (
    MANIFEST_PATH,
    TempDirTestCase,
    load_query_json,
    query_path,
    ref_eval,
    iter_catalog_rows,
)

# 手写期望：query 名 -> {file_id: decision}
EXPECTED_DECISIONS = {
    "q1_not_account_lt_neg3": {
        "f-old-wide": "filter", "f-new-narrow": "filter",
        "f-same-source": "pass", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "pass", "f-empty-str": "pass",
        "f-neg-bucket": "drop", "f-pos-mixed": "pass", "f-old-neg": "drop",
        "f-old-2": "pass",
    },
    "q2_empty_and": {fid: "drop" for fid in [
        "f-old-wide", "f-new-narrow", "f-same-source", "f-all-null",
        "f-void", "f-empty", "f-short-prefix", "f-empty-str",
        "f-neg-bucket", "f-pos-mixed", "f-old-neg", "f-old-2"]},
    "q3_not_empty_and": {
        "f-old-wide": "pass", "f-new-narrow": "pass",
        "f-same-source": "pass", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "pass", "f-empty-str": "pass",
        "f-neg-bucket": "pass", "f-pos-mixed": "pass", "f-old-neg": "pass",
        "f-old-2": "pass",
    },
    "q4_region_eq": {
        "f-old-wide": "drop", "f-new-narrow": "filter",
        "f-same-source": "filter", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "drop", "f-pos-mixed": "filter",
        "f-old-neg": "drop", "f-old-2": "pass",
    },
    "q5_region_adjacent": {
        "f-old-wide": "pass", "f-new-narrow": "pass",
        "f-same-source": "filter", "f-all-null": "drop", "f-void": "pass",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "drop", "f-pos-mixed": "filter",
        "f-old-neg": "drop", "f-old-2": "pass",
    },
    "q6_region_lt": {
        "f-old-wide": "drop", "f-new-narrow": "drop",
        "f-same-source": "filter", "f-all-null": "drop", "f-void": "drop",
        "f-empty": "drop", "f-short-prefix": "pass", "f-empty-str": "pass",
        "f-neg-bucket": "drop", "f-pos-mixed": "filter",
        "f-old-neg": "drop", "f-old-2": "drop",
    },
    "q7_account_eq_neg1": {
        "f-old-wide": "filter", "f-new-narrow": "filter",
        "f-same-source": "pass", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "drop", "f-pos-mixed": "drop", "f-old-neg": "drop",
        "f-old-2": "drop",
    },
    "q8_mixed": {
        "f-old-wide": "drop", "f-new-narrow": "filter",
        "f-same-source": "drop", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "drop", "f-pos-mixed": "filter",
        "f-old-neg": "drop", "f-old-2": "filter",
    },
    "q9_is_null_account": {
        "f-old-wide": "drop", "f-new-narrow": "drop",
        "f-same-source": "drop", "f-all-null": "pass", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "drop", "f-pos-mixed": "drop", "f-old-neg": "drop",
        "f-old-2": "drop",
    },
    "q10_region_empty": {
        "f-old-wide": "drop", "f-new-narrow": "drop",
        "f-same-source": "filter", "f-all-null": "drop", "f-void": "drop",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "pass",
        "f-neg-bucket": "drop", "f-pos-mixed": "drop", "f-old-neg": "drop",
        "f-old-2": "drop",
    },
    "q11_extreme_range": {
        "f-old-wide": "drop", "f-new-narrow": "drop",
        "f-same-source": "drop", "f-all-null": "drop", "f-void": "filter",
        "f-empty": "drop", "f-short-prefix": "drop", "f-empty-str": "drop",
        "f-neg-bucket": "pass", "f-pos-mixed": "drop", "f-old-neg": "drop",
        "f-old-2": "drop",
    },
}


class TestHandwrittenDecisions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(MANIFEST_PATH)

    def test_decisions_match_handwritten_table(self) -> None:
        for query_name, expected in EXPECTED_DECISIONS.items():
            with self.subTest(query=query_name):
                query = load_query(query_path(query_name),
                                   self.manifest.fields_by_id)
                plan = build_plan(self.manifest, query)
                actual = {f["file_id"]: f["decision"]
                          for f in plan["files"]}
                self.assertEqual(actual, expected)
                self.assertEqual(
                    [f["file_id"] for f in plan["files"]],
                    [f.file_id for f in self.manifest.files],
                    "计划必须按 manifest 文件顺序给出")

    def test_residuals_are_valid_and_equivalent(self) -> None:
        """filter 的剩余条件：合法、可执行、在该文件行上与原式等价。"""
        rows_by_file = {}
        for file_id, row in iter_catalog_rows():
            rows_by_file.setdefault(file_id, []).append(row)
        for query_name in EXPECTED_DECISIONS:
            with self.subTest(query=query_name):
                query_json = load_query_json(query_name)
                query = load_query(query_path(query_name),
                                   self.manifest.fields_by_id)
                plan = build_plan(self.manifest, query)
                for file_plan in plan["files"]:
                    if file_plan["decision"] != "filter":
                        self.assertIsNone(file_plan["residual"])
                        continue
                    residual = parse_query(file_plan["residual"],
                                           self.manifest.fields_by_id)
                    from hidden_partition import evaluate
                    for row in rows_by_file.get(file_plan["file_id"], []):
                        original = ref_eval(query_json, row) == "TRUE"
                        via_residual = evaluate(residual, row) == "TRUE"
                        self.assertEqual(
                            original, via_residual,
                            f"{file_plan['file_id']} 行 {row} 上剩余条件"
                            f"与原式不等价")

    def test_reasons_are_verifiable(self) -> None:
        query = load_query(query_path("q1_not_account_lt_neg3"),
                           self.manifest.fields_by_id)
        plan = build_plan(self.manifest, query)
        by_id = {f["file_id"]: f for f in plan["files"]}
        same_source = by_id["f-same-source"]
        text = "\n".join(same_source["reasons"])
        self.assertIn("truncate(10)", text)
        self.assertIn("identity", text)
        self.assertIn("[-1, -1]", text)  # 同源交集收成一个值
        self.assertIn("pass", text)
        # 约束摘要可机读核对
        account = next(c for c in same_source["constraints"]
                       if c["field_id"] == 1)
        self.assertEqual(account["range"], "[-1, -1]")
        self.assertFalse(account["null_possible"])


class TestPlanCommand(TempDirTestCase):
    def test_plan_writes_file_and_refuses_overwrite(self) -> None:
        out = self.path("plan.json")
        plan = run_plan(MANIFEST_PATH, query_path("q7_account_eq_neg1"), out)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(plan["summary"]["filter"], 3)
        with self.assertRaises(TargetExistsError):
            run_plan(MANIFEST_PATH, query_path("q7_account_eq_neg1"), out)

    def test_catalog_error_reported_before_any_output(self) -> None:
        bad_manifest = self.write_json("bad.json", {
            "schema": {"fields": [
                {"field_id": 1, "name": "account", "type": "int64",
                 "nullable": True}]},
            "specs": [{"spec_id": "s", "partition_fields": [
                {"partition_id": 1, "name": "a", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 10}}]}],
            "files": [{"file_id": "f", "path": "data/f.jsonl",
                       "spec_id": "s", "row_count": 1,
                       "partitions": {"1": -7}}],  # 不是 10 的倍数
        })
        out = self.path("plan.json")
        with self.assertRaises(CatalogError):
            run_plan(bad_manifest, query_path("q7_account_eq_neg1"), out)
        self.assertFalse(os.path.exists(out), "目录级错误时不得产生计划文件")

    def test_empty_catalog(self) -> None:
        empty_manifest = self.write_json("empty.json", {
            "schema": {"fields": [
                {"field_id": 1, "name": "account", "type": "int64",
                 "nullable": True}]},
            "specs": [],
            "files": [],
        })
        out = self.path("plan.json")
        plan = run_plan(empty_manifest, query_path("q7_account_eq_neg1"), out)
        self.assertEqual(plan["files"], [])
        self.assertEqual(plan["summary"]["files_total"], 0)

    def test_zero_row_file_dropped_without_residual(self) -> None:
        query = load_query(query_path("q1_not_account_lt_neg3"),
                           load_manifest(MANIFEST_PATH).fields_by_id)
        plan = build_plan(load_manifest(MANIFEST_PATH), query)
        empty = next(f for f in plan["files"] if f["file_id"] == "f-empty")
        self.assertEqual(empty["decision"], "drop")
        self.assertIsNone(empty["residual"])
        self.assertIn("row_count=0", empty["reasons"][0])


if __name__ == "__main__":
    unittest.main()
