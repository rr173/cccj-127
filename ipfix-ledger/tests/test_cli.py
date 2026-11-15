"""终端三个操作与真实进程退出码：import / list / trace。

同时验证失败时目标库不可被正常查询入口打开、成功后可跨进程读取。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from ._helpers import TempDirTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    # -I 会忽略 PYTHONPATH；用 bin/ipfixctl.py 在脚本内显式注入工程根
    return subprocess.run(
        [sys.executable, "-I",
         os.path.join(ROOT, "bin", "ipfixctl.py"), *args],
        capture_output=True, text=True, cwd=cwd or ROOT)


class CliTestCase(TempDirTestCase):
    def test_import_list_trace_round_trip_in_new_processes(self) -> None:
        db = self.path("cli.db")
        proc = run_cli("import", "--input",
                       os.path.join(FIXTURES, "generation.jsonl"),
                       "--db", db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["records"], 15)

        proc = run_cli("list", "--db", db, "--exporter", "exporter-1",
                       "--transport", "transport-A", "--domain", "7",
                       "--capture-line", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        self.assertEqual([r["record_ordinal"] for r in records], [1, 2, 3])
        self.assertEqual(
            [f["value"] for f in records[0]["fields"]],
            ["192.0.2.1", "18446744073709551615"])

        proc = run_cli("trace", "--db", db, "--record", "2", "--field", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        trace = json.loads(proc.stdout)
        self.assertEqual(trace["value_range"], [80, 84])
        self.assertEqual(trace["template_field_range"], [64, 68])
        self.assertEqual(trace["sliced_hex"], "0000000a")
        self.assertEqual(trace["template_generation"], 2)
        self.assertIsNone(trace["var_prefix_range"])

    def test_list_by_template_id(self) -> None:
        db = self.path("cli.db")
        run_cli("import", "--input",
                os.path.join(FIXTURES, "variable.jsonl"), "--db", db)
        proc = run_cli("list", "--db", db, "--template", "301")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["template_generation"] for r in rows}, {1})

    def test_import_failure_exit_code_and_missing_db(self) -> None:
        db = self.path("bad.db")
        proc = run_cli("import", "--input",
                       os.path.join(FIXTURES, "rejections",
                                    "sequence_gap.bad.jsonl"),
                       "--db", db)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(db))
        err = json.loads(proc.stderr)
        self.assertFalse(err["ok"])
        self.assertEqual(err["failures"][0]["capture_line"], 3)
        self.assertEqual(err["failures"][0]["phase"], "sequence")
        # 正常查询入口不能打开不存在的库
        proc = run_cli("list", "--db", db)
        self.assertNotEqual(proc.returncode, 0)

    def test_existing_target_exit_code_three(self) -> None:
        db = self.path("existing.db")
        with open(db, "wb") as fh:
            fh.write(b"x")
        proc = run_cli("import", "--input",
                       os.path.join(FIXTURES, "generation.jsonl"),
                       "--db", db)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(open(db, "rb").read(), b"x")

    def test_trace_missing_field_is_clean_error(self) -> None:
        db = self.path("ok.db")
        run_cli("import", "--input",
                os.path.join(FIXTURES, "generation.jsonl"), "--db", db)
        proc = run_cli("trace", "--db", db, "--record", "1", "--field", "9")
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"], "LookupError")


if __name__ == "__main__":
    unittest.main()
