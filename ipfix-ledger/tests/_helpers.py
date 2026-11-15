"""测试辅助：JSONL 落盘、抓错误。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from ipfix_offline import IpfixInputError


def write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def line(exporter: str, transport: str, message: bytes) -> dict:
    return {"exporter_id": exporter, "transport_id": transport,
            "message_hex": message.hex()}


def raw_lines(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for item in lines:
            fh.write(item + "\n")


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def fail_with(self, capture: str, **kwargs) -> IpfixInputError:
        """接入预期失败的捕获，断言目标库不存在，返回错误。"""
        from ipfix_offline import import_capture
        db = self.path("out.db")
        with self.assertRaises(IpfixInputError) as ctx:
            import_capture(capture, db, **kwargs)
        self.assertFalse(os.path.exists(db), "失败后不得留下目标结果库")
        return ctx.exception
