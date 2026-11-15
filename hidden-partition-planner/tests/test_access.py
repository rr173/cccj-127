"""文件访问监测证明：plan 零数据访问，scan 不访问 drop 路径。

用 CPython 审计钩子记录进程内全部 open 事件；同时用 mock 包裹
os.stat / os.path.exists / os.listdir / os.scandir，证明规划阶段
连元数据都没有碰过数据路径。监测期间在 drop 路径上故意放置损坏
或不存在的数据文件：窄查询照样成功，改成必须读取它的条件则真实失败。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

from hidden_partition import DataError, run_plan, run_scan

from ._helpers import (
    CATALOG_DIR,
    MANIFEST_PATH,
    TempDirTestCase,
    query_path,
)

DATA_DIR = os.path.join(CATALOG_DIR, "data")

_OPEN_EVENTS: list = []


def _audit_hook(event: str, args) -> None:
    if event == "open":
        _OPEN_EVENTS.append(args[0])


sys.addaudithook(_audit_hook)


def opened_paths() -> list:
    return list(_OPEN_EVENTS)


def clear_opened() -> None:
    del _OPEN_EVENTS[:]


class StatGuard:
    """包裹常见元数据调用：一旦碰到数据目录就抛错。"""

    def __init__(self, forbidden_prefix: str):
        self.forbidden = forbidden_prefix
        self._patches = []

    def _check(self, path) -> None:
        if isinstance(path, int):
            return
        target = os.fspath(path)
        if isinstance(target, str) and target.startswith(self.forbidden):
            raise AssertionError(f"规划阶段触碰了数据路径：{target}")

    def __enter__(self):
        for name in ("stat", "listdir", "scandir"):
            original = getattr(os, name)

            def make_wrapped(orig):
                def wrapped(path, *args, **kwargs):
                    self._check(path)
                    return orig(path, *args, **kwargs)
                return wrapped

            self._patches.append(mock.patch(f"os.{name}",
                                            make_wrapped(original)))
        original_exists = os.path.exists

        def exists_guard(path):
            self._check(path)
            return original_exists(path)

        self._patches.append(mock.patch("os.path.exists", exists_guard))
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self._patches:
            patcher.stop()
        return False


class TestPlanZeroDataAccess(TempDirTestCase):
    def test_plan_opens_only_manifest_and_query(self) -> None:
        out = self.path("plan.json")
        clear_opened()
        with StatGuard(DATA_DIR):
            run_plan(MANIFEST_PATH, query_path("q7_account_eq_neg1"), out)
        opened = opened_paths()
        touched_data = [p for p in opened
                        if isinstance(p, str) and p.startswith(DATA_DIR)]
        self.assertEqual(touched_data, [],
                         f"plan 不得打开任何数据路径，实际：{touched_data}")
        self.assertIn(MANIFEST_PATH, opened)
        self.assertIn(query_path("q7_account_eq_neg1"), opened)

    def test_plan_with_missing_data_files_still_works(self) -> None:
        # 把验收目录复制一份并删掉全部数据文件：plan 照常成功。
        import shutil
        clone = os.path.join(self.dir, "clone")
        shutil.copytree(CATALOG_DIR, clone)
        for name in os.listdir(os.path.join(clone, "data")):
            os.unlink(os.path.join(clone, "data", name))
        manifest = os.path.join(clone, "manifest.json")
        out = self.path("plan.json")
        with StatGuard(os.path.join(clone, "data")):
            plan = run_plan(manifest, query_path("q7_account_eq_neg1"), out)
        self.assertEqual(plan["summary"]["files_total"], 12)


class TestScanSkipsDropPaths(TempDirTestCase):
    def test_scan_opens_only_pass_and_filter_files(self) -> None:
        out = self.path("out.jsonl")
        rep = self.path("report.json")
        clear_opened()
        run_scan(MANIFEST_PATH, query_path("q7_account_eq_neg1"), out, rep)
        opened_data = {p for p in opened_paths()
                       if isinstance(p, str) and p.startswith(DATA_DIR)}
        expected = {os.path.join(DATA_DIR, f"{fid}.jsonl") for fid in
                    ("f-old-wide", "f-new-narrow", "f-same-source", "f-void")}
        self.assertEqual(opened_data, expected,
                         "scan 只能打开 pass/filter 文件，drop 一律不碰")

    def test_corrupt_and_missing_drop_paths_not_touched(self) -> None:
        # 造一个目录：f1 可读，f2 损坏，f3 不存在；窄查询只读 f1。
        os.makedirs(os.path.join(self.dir, "data"))
        manifest = {
            "schema": {"fields": [
                {"field_id": 1, "name": "account", "type": "int64",
                 "nullable": True},
                {"field_id": 2, "name": "region", "type": "string",
                 "nullable": True}]},
            "specs": [{"spec_id": "s", "partition_fields": [
                {"partition_id": 1, "name": "账户区间", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 10}},
                {"partition_id": 2, "name": "地区", "source_id": 2,
                 "transform": {"kind": "identity"}}]}],
            "files": [
                {"file_id": "f1", "path": "data/f1.jsonl", "spec_id": "s",
                 "row_count": 1, "partitions": {"1": 0, "2": "京"}},
                {"file_id": "f2", "path": "data/f2.jsonl", "spec_id": "s",
                 "row_count": 1, "partitions": {"1": -10, "2": "沪"}},
                {"file_id": "f3", "path": "data/f3.jsonl", "spec_id": "s",
                 "row_count": 1, "partitions": {"1": -20, "2": "津"}},
            ],
        }
        with open(self.path("data/f1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"1": 3, "2": "京"}\n')
        with open(self.path("data/f2.jsonl"), "wb") as fh:
            fh.write(b"\xff\xfe bad bytes\n")  # 损坏：非法 UTF-8
        # f3 根本不存在。
        manifest_path = self.write_json("manifest.json", manifest)
        narrow = self.write_json("narrow.json",
                                 {"op": "eq", "field_id": 1, "value": 3})
        out, rep = self.path("o.jsonl"), self.path("r.json")
        clear_opened()
        report = run_scan(manifest_path, narrow, out, rep)
        opened = opened_paths()
        self.assertIn(self.path("data/f1.jsonl"), opened)
        self.assertNotIn(self.path("data/f2.jsonl"), opened)
        self.assertNotIn(self.path("data/f3.jsonl"), opened)
        with open(out, "r", encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline()),
                             {"account": 3, "region": "京"})
        self.assertEqual(report["summary"]["files_opened"], 1)

        # 改成必须读取它们的条件（account < 10 覆盖全部区间）→ 真实失败。
        wide = self.write_json("wide.json",
                               {"op": "lt", "field_id": 1, "value": 10})
        with self.assertRaises(DataError):
            run_scan(manifest_path, wide, self.path("o2.jsonl"),
                     self.path("r2.json"))
        self.assertFalse(os.path.exists(self.path("o2.jsonl")))


if __name__ == "__main__":
    unittest.main()
