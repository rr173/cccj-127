"""scan 执行：真实行、投影、失败交付状态。

期望行分两层核对：关键查询用手写期望，全部查询用独立行过滤参考
（tests/_helpers.ref_eval，直接对原始查询 JSON 求值）交叉核对。
"""
from __future__ import annotations

import json
import os
import unittest

from hidden_partition import DataError, run_scan
from hidden_partition.errors import TargetExistsError

from ._helpers import (
    CATALOG_DIR,
    MANIFEST_PATH,
    TempDirTestCase,
    expected_delivery,
    load_json,
    load_query_json,
    query_path,
)

INT64_MAX = 2 ** 63 - 1
INT64_MIN = -(2 ** 63)

ALL_QUERIES = [
    "q1_not_account_lt_neg3", "q2_empty_and", "q3_not_empty_and",
    "q4_region_eq", "q5_region_adjacent", "q6_region_lt",
    "q7_account_eq_neg1", "q8_mixed", "q9_is_null_account",
    "q10_region_empty", "q11_extreme_range",
]


def read_jsonl(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ScanCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._scan_seq = 0

    def scan(self, query_name: str, projection=None, manifest=MANIFEST_PATH):
        self._scan_seq += 1
        out = self.path(f"out{self._scan_seq}.jsonl")
        rep = self.path(f"report{self._scan_seq}.json")
        report = run_scan(manifest, query_path(query_name), out, rep,
                          projection=projection)
        return report, read_jsonl(out)


class TestScanDelivery(ScanCase):
    def test_q7_handwritten_rows(self) -> None:
        report, rows = self.scan("q7_account_eq_neg1")
        self.assertEqual(rows, [
            {"account": -1, "region": "京𠮷照"},
            {"account": -1, "region": "京𠮷"},
            {"account": -1, "region": "沪"},
            {"account": -1, "region": None},
            {"account": -1, "region": "京"},
        ])
        opened = {r["file_id"] for r in report["files"] if r["opened"]}
        self.assertEqual(opened, {"f-old-wide", "f-new-narrow",
                                  "f-same-source", "f-void"})
        self.assertEqual(report["summary"]["rows_read"], 13)
        self.assertEqual(report["summary"]["rows_delivered"], 5)

    def test_q1_handwritten_rows(self) -> None:
        # not(account<-3) ≡ account>=-3 且非 null：filter 文件逐行判断，
        # pass 文件（f-same-source/f-short-prefix/f-empty-str/f-pos-mixed/
        # f-old-2）整文件交付，共 18 行。
        _report, rows = self.scan("q1_not_account_lt_neg3")
        self.assertEqual(rows, [
            {"account": -1, "region": "京𠮷照"},
            {"account": -3, "region": "京𠮷照"},
            {"account": -1, "region": "京𠮷"},
            {"account": -1, "region": "沪"},
            {"account": -1, "region": None},
            {"account": -1, "region": "京"},
            {"account": 0, "region": "京𠮷照"},
            {"account": INT64_MAX, "region": "京𠮷照"},
            {"account": 0, "region": "京"},
            {"account": 1, "region": "京"},
            {"account": 3, "region": "京"},
            {"account": 4, "region": ""},
            {"account": 5, "region": ""},
            {"account": 3, "region": "京"},
            {"account": 3, "region": "京𠮷"},
            {"account": 3, "region": "京𠮸"},
            {"account": 0, "region": "京𠮷"},
            {"account": 4, "region": "京𠮷"},
        ])

    def test_all_queries_match_independent_reference(self) -> None:
        manifest = load_json(MANIFEST_PATH)
        fields = manifest["schema"]["fields"]
        projection = [1, 2]
        for query_name in ALL_QUERIES:
            with self.subTest(query=query_name):
                query_json = load_query_json(query_name)
                _report, rows = self.scan(query_name)
                self.assertEqual(
                    rows, expected_delivery(query_json, projection, fields))

    def test_projection_order_and_unprojected_filter_column(self) -> None:
        # 只投影 region，但过滤仍按 account 进行；account 不得混入输出。
        report, rows = self.scan("q7_account_eq_neg1", projection=[2])
        self.assertEqual(rows, [
            {"region": "京𠮷照"}, {"region": "京𠮷"}, {"region": "沪"},
            {"region": None}, {"region": "京"},
        ])
        self.assertEqual([p["name"] for p in report["projection"]],
                         ["region"])
        # 反序投影 [2,1] 的键序就是投影序。
        _report, rows = self.scan("q7_account_eq_neg1", projection=[2, 1])
        self.assertEqual(list(rows[0].keys()), ["region", "account"])

    def test_empty_result_still_reports_projection(self) -> None:
        report, rows = self.scan("q2_empty_and", projection=[2])
        self.assertEqual(rows, [])
        self.assertEqual(report["projection"],
                         [{"field_id": 2, "name": "region",
                           "type": "string"}])
        self.assertEqual(report["summary"]["files_opened"], 0)
        self.assertEqual(report["summary"]["rows_read"], 0)

    def test_duplicates_nulls_and_order_preserved(self) -> None:
        # f-all-null 有两行相同的 (null,null)；q9 对它 pass，两行都交付。
        _report, rows = self.scan("q9_is_null_account")
        self.assertEqual(rows, [
            {"account": None, "region": None},
            {"account": None, "region": None},
            {"account": None, "region": "京𠮷"},
        ])

    def test_report_lists_real_opened_files_and_counts(self) -> None:
        report, _rows = self.scan("q4_region_eq")
        by_id = {r["file_id"]: r for r in report["files"]}
        self.assertFalse(by_id["f-old-wide"]["opened"])
        self.assertEqual(by_id["f-old-2"]["rows_read"], 2)
        self.assertEqual(by_id["f-old-2"]["rows_delivered"], 2)
        self.assertEqual(by_id["f-new-narrow"]["rows_delivered"], 2)
        self.assertEqual(by_id["f-empty"]["rows_read"], 0)


class TestScanFailures(TempDirTestCase):
    """失败交付状态：非零失败、不交付半份结果、不覆盖旧文件。"""

    def make_catalog(self, rows_by_file: dict, partitions_by_file: dict,
                     spec_id="s1", row_count_delta=None) -> str:
        """在临时目录造一个小目录：单 spec truncate(10)+identity。"""
        manifest = {
            "schema": {"fields": [
                {"field_id": 1, "name": "account", "type": "int64",
                 "nullable": True},
                {"field_id": 2, "name": "region", "type": "string",
                 "nullable": True}]},
            "specs": [{"spec_id": spec_id, "partition_fields": [
                {"partition_id": 1, "name": "账户区间", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 10}},
                {"partition_id": 2, "name": "地区", "source_id": 2,
                 "transform": {"kind": "identity"}}]}],
            "files": [],
        }
        os.makedirs(os.path.join(self.dir, "data"), exist_ok=True)
        for file_id, rows in rows_by_file.items():
            rel = f"data/{file_id}.jsonl"
            with open(self.path(rel), "w", encoding="utf-8",
                      newline="\n") as fh:
                for row in rows:
                    if isinstance(row, str):
                        fh.write(row + "\n")
                    else:
                        fh.write(json.dumps(
                            {"1": row[0], "2": row[1]},
                            ensure_ascii=False) + "\n")
            row_count = len(rows) + (row_count_delta or 0)
            manifest["files"].append({
                "file_id": file_id, "path": rel, "spec_id": spec_id,
                "row_count": row_count,
                "partitions": partitions_by_file[file_id]})
        return self.write_json("manifest.json", manifest)

    def scan_manifest(self, manifest: str, query_name: str):
        out = self.path("out.jsonl")
        rep = self.path("report.json")
        return run_scan(manifest, query_path(query_name), out, rep), out, rep

    def test_missing_selected_file_fails(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京")], "f2": [(-5, "沪")]},
            {"f1": {"1": 0, "2": "京"}, "f2": {"1": -10, "2": "沪"}})
        os.unlink(self.path("data/f2.jsonl"))
        # 窄查询（account=0）只需 f1：f2 被 drop，不存在也成功。
        narrow = self.write_json("narrow.json",
                                 {"op": "eq", "field_id": 1, "value": 0})
        out, rep = self.path("o1.jsonl"), self.path("r1.json")
        report = run_scan(manifest, narrow, out, rep)
        self.assertEqual(read_jsonl(out), [{"account": 0, "region": "京"}])
        # 宽查询（account<5）必须读 f2：真实失败。
        wide = self.write_json("wide.json",
                               {"op": "lt", "field_id": 1, "value": 5})
        with self.assertRaises(DataError):
            run_scan(manifest, wide, self.path("o2.jsonl"),
                     self.path("r2.json"))
        self.assertFalse(os.path.exists(self.path("o2.jsonl")))
        self.assertFalse(os.path.exists(self.path("r2.json")))

    def test_corrupt_drop_file_never_touched(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京")], "f2": ["这不是JSON，是损坏内容"]},
            {"f1": {"1": 0, "2": "京"}, "f2": {"1": -10, "2": "沪"}})
        narrow = self.write_json("narrow.json",
                                 {"op": "eq", "field_id": 1, "value": 0})
        out, rep = self.path("o.jsonl"), self.path("r.json")
        run_scan(manifest, narrow, out, rep)
        self.assertEqual(read_jsonl(out), [{"account": 0, "region": "京"}])

    def test_last_line_bad_fails_without_partial_output(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京"), (1, "京"), "{坏行"]},
            {"f1": {"1": 0, "2": "京"}})
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        out, rep = self.path("o.jsonl"), self.path("r.json")
        with self.assertRaises(DataError) as ctx:
            run_scan(manifest, wide, out, rep)
        self.assertEqual(ctx.exception.line, 3)
        self.assertFalse(os.path.exists(out), "失败不得留下半份输出")
        self.assertFalse(os.path.exists(rep))

    def test_row_count_mismatch_fails(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京"), (1, "京")]},
            {"f1": {"1": 0, "2": "京"}},
            row_count_delta=1)  # 目录说 3 行，实际 2 行
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        with self.assertRaises(DataError) as ctx:
            run_scan(manifest, wide, self.path("o.jsonl"), self.path("r.json"))
        self.assertIn("row_count", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path("o.jsonl")))

    def test_duplicate_json_keys_fail(self) -> None:
        manifest = self.make_catalog(
            {"f1": ['{"1": 0, "1": 1, "2": "京"}']},
            {"f1": {"1": 0, "2": "京"}})
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        with self.assertRaises(DataError):
            run_scan(manifest, wide, self.path("o.jsonl"), self.path("r.json"))

    def test_invalid_utf8_fails(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京")]}, {"f1": {"1": 0, "2": "京"}})
        with open(self.path("data/f1.jsonl"), "ab") as fh:
            fh.write(b'{"1": 1, "2": "\xff\xfe"}\n')
        # 行数也得对齐：把目录行数改成 2。
        doc = load_json(manifest)
        doc["files"][0]["row_count"] = 2
        self.write_json("manifest.json", doc)
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        with self.assertRaises(DataError) as ctx:
            run_scan(manifest, wide, self.path("o.jsonl"), self.path("r.json"))
        self.assertIn("UTF-8", str(ctx.exception))

    def test_bad_field_values_fail(self) -> None:
        bad_rows = [
            ('{"1": true, "2": "京"}', "bool 不算整数"),
            ('{"1": 9223372036854775808, "2": "京"}', "int64 范围"),
            ('{"1": 0, "2": 5}', "string 列收到数字"),
            ('{"1": 0}', "缺列"),
            ('{"1": 0, "2": "京", "3": 1}', "多列"),
            ('{"1": null, "2": "京"}', "ok"),  # null 合法，不报错
        ]
        for line, note in bad_rows:
            with self.subTest(note=note):
                manifest = self.make_catalog(
                    {"f1": [line]}, {"f1": {"1": 0, "2": "京"}})
                wide = self.write_json(
                    "wide.json", {"op": "ge", "field_id": 1, "value": 0})
                if note == "ok":
                    run_scan(manifest, wide, self.path("o.jsonl"),
                             self.path("r.json"))
                else:
                    with self.assertRaises(DataError):
                        run_scan(manifest, wide, self.path("o.jsonl"),
                                 self.path("r.json"))
                for name in ("o.jsonl", "r.json", "manifest.json",
                             "wide.json", "data/f1.jsonl"):
                    p = self.path(name)
                    if os.path.exists(p):
                        os.unlink(p)

    def test_line_too_long_fails(self) -> None:
        from hidden_partition import Limits
        manifest = self.make_catalog(
            {"f1": [(0, "京京京京京京")]}, {"f1": {"1": 0, "2": "京"}})
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        with self.assertRaises(DataError) as ctx:
            run_scan(manifest, wide, self.path("o.jsonl"), self.path("r.json"),
                     limits=Limits(max_line_bytes=16))
        self.assertIn("上限", str(ctx.exception))

    def test_existing_targets_refused(self) -> None:
        manifest = self.make_catalog(
            {"f1": [(0, "京")]}, {"f1": {"1": 0, "2": "京"}})
        wide = self.write_json("wide.json",
                               {"op": "ge", "field_id": 1, "value": 0})
        out = self.write_text("o.jsonl", "旧内容\n")
        rep = self.path("r.json")
        with self.assertRaises(TargetExistsError):
            run_scan(manifest, wide, out, rep)
        with open(out, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "旧内容\n", "旧文件字节不得被动")
        # 报告路径已存在同样拒绝。
        run_scan(manifest, wide, self.path("o2.jsonl"), rep)
        with self.assertRaises(TargetExistsError):
            run_scan(manifest, wide, self.path("o3.jsonl"), rep)


class TestEmptyCatalogAndFile(ScanCase):
    def test_empty_catalog_scan(self) -> None:
        empty_manifest = self.write_json("empty.json", {
            "schema": {"fields": [
                {"field_id": 1, "name": "account", "type": "int64",
                 "nullable": True}]},
            "specs": [], "files": []})
        query = self.write_json("q.json",
                                {"op": "eq", "field_id": 1, "value": 1})
        out, rep = self.path("o.jsonl"), self.path("r.json")
        report = run_scan(empty_manifest, query, out, rep)
        self.assertEqual(report["summary"]["files_opened"], 0)
        self.assertEqual(read_jsonl(out), [])
        self.assertEqual(report["projection"],
                         [{"field_id": 1, "name": "account",
                           "type": "int64"}])

    def test_zero_row_file_not_opened(self) -> None:
        # f-empty 在验收目录中 row_count=0；q1 下它 drop，报告 opened=false。
        report, _ = self.scan("q1_not_account_lt_neg3")
        by_id = {r["file_id"]: r for r in report["files"]}
        self.assertEqual(by_id["f-empty"]["decision"], "drop")
        self.assertFalse(by_id["f-empty"]["opened"])


if __name__ == "__main__":
    unittest.main()
