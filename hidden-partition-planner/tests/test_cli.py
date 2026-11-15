"""终端两个操作与真实进程退出码：plan / scan。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from ._helpers import MANIFEST_PATH, TempDirTestCase, query_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HPQ = os.path.join(ROOT, "bin", "hpq.py")


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", HPQ, *args],
                          capture_output=True, text=True, cwd=ROOT)


class TestPlanCli(TempDirTestCase):
    def test_plan_success(self) -> None:
        out = self.path("plan.json")
        proc = run_cli("plan", "--manifest", MANIFEST_PATH,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["summary"]["files_total"], 12)
        with open(out, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["summary"], plan["summary"])
        self.assertIn("elapsed_seconds", plan)
        self.assertIn("peak_rss_bytes", plan)

    def test_plan_catalog_error_exit2_no_output(self) -> None:
        bad = self.write_json("bad.json", {
            "schema": {"fields": [
                {"field_id": 1, "name": "a", "type": "int64",
                 "nullable": True}]},
            "specs": [{"spec_id": "s", "partition_fields": [
                {"partition_id": 1, "name": "a", "source_id": 1,
                 "transform": {"kind": "truncate", "width": 10}}]}],
            "files": [{"file_id": "f", "path": "d/f.jsonl", "spec_id": "s",
                       "row_count": 1, "partitions": {"1": -7}}]})
        out = self.path("plan.json")
        proc = run_cli("plan", "--manifest", bad,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", out)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(out))
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["error"], "catalog")
        self.assertTrue(payload["issues"])

    def test_plan_query_error_exit2(self) -> None:
        bad_query = self.write_json("badq.json",
                                    {"op": "between", "field_id": 1})
        proc = run_cli("plan", "--manifest", MANIFEST_PATH,
                       "--query", bad_query, "--output", self.path("p.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("query", proc.stderr)

    def test_plan_existing_output_exit3(self) -> None:
        out = self.write_text("plan.json", "{}\n")
        proc = run_cli("plan", "--manifest", MANIFEST_PATH,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", out)
        self.assertEqual(proc.returncode, 3)
        with open(out, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{}\n")


class TestScanCli(TempDirTestCase):
    def test_scan_success(self) -> None:
        out, rep = self.path("o.jsonl"), self.path("r.json")
        proc = run_cli("scan", "--manifest", MANIFEST_PATH,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", out, "--report", rep,
                       "--projection", "2,1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["rows_delivered"], 5)
        with open(out, "r", encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        self.assertEqual(list(first.keys()), ["region", "account"])
        self.assertEqual([p["name"] for p in report["projection"]],
                         ["region", "account"])

    def test_scan_data_error_exit2_no_partial(self) -> None:
        # 选一个 pass 文件并截断最后一行 → 行数不符。
        import shutil
        clone = os.path.join(self.dir, "clone")
        shutil.copytree(os.path.dirname(MANIFEST_PATH), clone)
        victim = os.path.join(clone, "data", "f-same-source.jsonl")
        with open(victim, "rb") as fh:
            blob = fh.read()
        with open(victim, "wb") as fh:
            fh.write(blob[:blob.rfind(b"\n", 0, len(blob) - 1)])  # 去掉最后一行
        out, rep = self.path("o.jsonl"), self.path("r.json")
        proc = run_cli("scan", "--manifest",
                       os.path.join(clone, "manifest.json"),
                       "--query", query_path("q1_not_account_lt_neg3"),
                       "--output", out, "--report", rep)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(rep))
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["error"], "data")

    def test_scan_existing_output_exit3(self) -> None:
        out = self.write_text("o.jsonl", "")
        proc = run_cli("scan", "--manifest", MANIFEST_PATH,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", out, "--report", self.path("r.json"))
        self.assertEqual(proc.returncode, 3)

    def test_scan_bad_projection_exit2(self) -> None:
        proc = run_cli("scan", "--manifest", MANIFEST_PATH,
                       "--query", query_path("q7_account_eq_neg1"),
                       "--output", self.path("o.jsonl"),
                       "--report", self.path("r.json"),
                       "--projection", "1,1")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
