"""CLI 测试：三种退出码、输出文件、逐行流式、坏行隔离。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from contract_gate.cli import (EXIT_CONTRACT, EXIT_OK, EXIT_REJECTED,
                               run_triage)

from .helpers import EXAMPLES, MANIFEST, SC, make_contract, write_contract_files


class TestExitCodes(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="cg-cli-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.out, ignore_errors=True)

    def _write(self, name, data: bytes):
        path = os.path.join(self.out, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_exit0_all_accepted(self):
        data = self._write("in.jsonl", b'{"id":30}\n{"id":31}\n')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [data], self.out)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":30}\n{"id":31}\n')
        with open(os.path.join(self.out, "rejected.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b"")
        with open(os.path.join(self.out, "summary.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual((s["total"], s["accepted"], s["rejected"]), (2, 2, 0))

    def test_exit1_with_rejections(self):
        data = self._write("in.jsonl", b'{"id":1}\n{"id":"x"}\n{"id":2}\n')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [data], self.out)
        self.assertEqual(code, EXIT_REJECTED)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":1}\n{"id":2}\n')
        with open(os.path.join(self.out, "rejected.jsonl"), encoding="utf-8") as f:
            rejected = [json.loads(line) for line in f]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["line"], 2)
        self.assertEqual(rejected[0]["category"], "contract")

    def test_exit2_contract_error_no_outputs(self):
        bad_manifest = os.path.join(EXAMPLES, "bad", "manifest.json")
        code = run_triage(bad_manifest, "https://acme.example/schemas/bad.json",
                          [self._write("in.jsonl", b"{}\n")], self.out)
        self.assertEqual(code, EXIT_CONTRACT)
        for name in ("accepted.jsonl", "rejected.jsonl", "summary.json"):
            self.assertFalse(os.path.exists(os.path.join(self.out, name)), name)

    def test_exit2_missing_input(self):
        code = run_triage(MANIFEST, f"{SC}/mixed.json",
                          [os.path.join(self.out, "nope.jsonl")], self.out)
        self.assertEqual(code, EXIT_CONTRACT)

    def test_bad_line_only_affects_itself(self):
        data = self._write("in.jsonl",
                           b'{"id":1}\nnot json\n{"id":2}\n\n{"id":3}\n')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [data], self.out)
        self.assertEqual(code, EXIT_REJECTED)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":1}\n{"id":2}\n{"id":3}\n')
        with open(os.path.join(self.out, "rejected.jsonl"), encoding="utf-8") as f:
            rejected = [json.loads(line) for line in f]
        self.assertEqual([r["line"] for r in rejected], [2, 4])
        self.assertEqual([r["category"] for r in rejected], ["json", "empty"])
        for r in rejected:
            self.assertIsNone(r["resource"])
            self.assertIsNone(r["pointer"])

    def test_multiple_files_order_and_summary(self):
        a = self._write("a.jsonl", b'{"id":1}\n{"id":"x"}\n')
        b = self._write("b.jsonl", b'{"id":2}\n')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [a, b], self.out)
        self.assertEqual(code, EXIT_REJECTED)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":1}\n{"id":2}\n')
        with open(os.path.join(self.out, "summary.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual([f_["file"] for f_ in s["files"]], [a, b])
        self.assertEqual([(f_["total"], f_["accepted"], f_["rejected"])
                          for f_ in s["files"]], [(2, 1, 1), (1, 1, 0)])
        self.assertEqual((s["total"], s["accepted"], s["rejected"]), (3, 2, 1))

    def test_accepted_bytes_not_reserialized(self):
        # 键顺序、空白、数字写法都按原字节保留
        line = b'{ "b" : 1e2 , "a" : 9007199254740993 }'
        data = self._write("in.jsonl", line + b"\n")
        manifest, root = write_contract_files({"root.json": True})
        code = run_triage(manifest, root, [data], self.out)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), line + b"\n")

    def test_crlf_stripped(self):
        data = self._write("in.jsonl", b'{"id":1}\r\n{"id":2}\r\n')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [data], self.out)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":1}\n{"id":2}\n')

    def test_last_line_without_newline(self):
        data = self._write("in.jsonl", b'{"id":1}')
        code = run_triage(MANIFEST, f"{SC}/mixed.json", [data], self.out)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(self.out, "accepted.jsonl"), "rb") as f:
            self.assertEqual(f.read(), b'{"id":1}\n')


if __name__ == "__main__":
    unittest.main()
