"""补充边界：无分区 spec、剩余条件化简、重复键拒绝、投影边界。"""
from __future__ import annotations

import json
import unittest

from hidden_partition import (
    Limits,
    build_plan,
    load_manifest,
    load_query,
    parse_manifest,
    parse_query,
    run_plan,
    run_scan,
)

from ._helpers import TempDirTestCase


def make_manifest_obj(partition_fields, partitions, *, nullable=False,
                      row_count=1):
    return {
        "schema": {"fields": [
            {"field_id": 1, "name": "account", "type": "int64",
             "nullable": nullable}]},
        "specs": [{"spec_id": "s", "partition_fields": partition_fields}],
        "files": [{"file_id": "f", "path": "data/f.jsonl", "spec_id": "s",
                   "row_count": row_count, "partitions": partitions}],
    }


class TestUnpartitionedSpec(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        obj = make_manifest_obj([], {})
        self.manifest = parse_manifest(obj)

    def test_no_partition_fields_means_no_constraint(self) -> None:
        query = parse_query({"op": "is_null", "field_id": 1},
                            self.manifest.fields_by_id)
        plan = build_plan(self.manifest, query)
        # 不可空列 + 无分区：is_null 恒 FALSE → drop。
        self.assertEqual(plan["files"][0]["decision"], "drop")
        query = parse_query({"op": "is_not_null", "field_id": 1},
                            self.manifest.fields_by_id)
        plan = build_plan(self.manifest, query)
        self.assertEqual(plan["files"][0]["decision"], "pass")

    def test_unconstrained_comparison_filters(self) -> None:
        query = parse_query({"op": "eq", "field_id": 1, "value": 1},
                            self.manifest.fields_by_id)
        plan = build_plan(self.manifest, query)
        self.assertEqual(plan["files"][0]["decision"], "filter")


class TestResidualSimplification(TempDirTestCase):
    def test_always_true_conjunct_dropped(self) -> None:
        obj = make_manifest_obj(
            [{"partition_id": 1, "name": "账户区间", "source_id": 1,
              "transform": {"kind": "truncate", "width": 10}}],
            {"1": -10})
        manifest = parse_manifest(obj)
        query = parse_query({"op": "and", "children": [
            {"op": "ge", "field_id": 1, "value": -20},
            {"op": "lt", "field_id": 1, "value": -3}]},
            manifest.fields_by_id)
        plan = build_plan(manifest, query)
        entry = plan["files"][0]
        self.assertEqual(entry["decision"], "filter")
        # ge -20 在 [-10,-1] 上恒 TRUE，被吸收；剩余条件只剩 lt -3。
        self.assertEqual(entry["residual"],
                         {"op": "lt", "field_id": 1, "value": -3})

    def test_residual_keeps_original_when_nothing_simplifies(self) -> None:
        obj = make_manifest_obj(
            [{"partition_id": 1, "name": "账户区间", "source_id": 1,
              "transform": {"kind": "truncate", "width": 10}}],
            {"1": -10})
        manifest = parse_manifest(obj)
        query = parse_query({"op": "not", "child":
                             {"op": "lt", "field_id": 1, "value": -3}},
                            manifest.fields_by_id)
        plan = build_plan(manifest, query)
        self.assertEqual(plan["files"][0]["residual"],
                         {"op": "not",
                          "child": {"op": "lt", "field_id": 1, "value": -3}})


class TestStrictJsonLoading(TempDirTestCase):
    def test_duplicate_keys_in_manifest_rejected(self) -> None:
        path = self.path("dup.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema": {"fields": []}, "schema": {"fields": []},'
                     ' "specs": [], "files": []}')
        with self.assertRaises(ValueError):
            load_manifest(path)

    def test_non_standard_constant_rejected(self) -> None:
        path = self.path("nan.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"op": "eq", "field_id": 1, "value": NaN}')
        manifest = parse_manifest(make_manifest_obj([], {}))
        with self.assertRaises(ValueError):
            load_query(path, manifest.fields_by_id)

    def test_manifest_size_limit(self) -> None:
        path = self.write_json("m.json", make_manifest_obj([], {}))
        with self.assertRaises(Exception) as ctx:
            load_manifest(path, Limits(max_manifest_bytes=16))
        self.assertIn("上限", str(ctx.exception))


class TestProjectionEdges(TempDirTestCase):
    def _manifest_path(self) -> str:
        import os
        os.makedirs(os.path.join(self.dir, "data"))
        with open(os.path.join(self.dir, "data", "f.jsonl"), "w",
              encoding="utf-8") as fh:
            fh.write('{"1": 0}\n')
        return self.write_json("m.json", make_manifest_obj([], {}))

    def _query_path(self) -> str:
        return self.write_json("q.json",
                               {"op": "eq", "field_id": 1, "value": 0})

    def test_duplicate_projection_rejected(self) -> None:
        from hidden_partition import resolve_projection
        manifest = parse_manifest(make_manifest_obj([], {}))
        with self.assertRaises(ValueError):
            resolve_projection([1, 1], manifest)
        with self.assertRaises(ValueError):
            resolve_projection([], manifest)
        with self.assertRaises(ValueError):
            resolve_projection([99], manifest)

    def test_scan_with_explicit_projection(self) -> None:
        report = run_scan(self._manifest_path(), self._query_path(),
                          self.path("o.jsonl"), self.path("r.json"),
                          projection=[1])
        self.assertEqual(report["summary"]["rows_delivered"], 1)
        self.assertEqual(report["projection"],
                         [{"field_id": 1, "name": "account",
                           "type": "int64"}])


if __name__ == "__main__":
    unittest.main()
