"""测试辅助：临时目录、独立行过滤参考、验收目录路径。

独立行过滤参考（ref_eval）直接对**原始查询 JSON** 求三值，
不使用规划器的剩余条件，也不 import 包内的求值器——
scan 的期望结果由它独立算出，避免“用被测实现生成期望”。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_DIR = os.path.join(ROOT, "examples", "catalog")
QUERIES_DIR = os.path.join(ROOT, "examples", "queries")
MANIFEST_PATH = os.path.join(CATALOG_DIR, "manifest.json")


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def query_path(name: str) -> str:
    return os.path.join(QUERIES_DIR, f"{name}.json")


def load_query_json(name: str):
    return load_json(query_path(name))


def ref_eval(query: dict, row: dict) -> str:
    """独立三值求值：row 以 field_id（int）为键，返回 TRUE/FALSE/UNKNOWN。"""
    op = query["op"]
    if op == "and":
        saw_unknown = False
        for child in query["children"]:
            outcome = ref_eval(child, row)
            if outcome == "FALSE":
                return "FALSE"
            if outcome == "UNKNOWN":
                saw_unknown = True
        return "UNKNOWN" if saw_unknown else "TRUE"
    if op == "or":
        saw_unknown = False
        for child in query["children"]:
            outcome = ref_eval(child, row)
            if outcome == "TRUE":
                return "TRUE"
            if outcome == "UNKNOWN":
                saw_unknown = True
        return "UNKNOWN" if saw_unknown else "FALSE"
    if op == "not":
        outcome = ref_eval(query["child"], row)
        return {"TRUE": "FALSE", "FALSE": "TRUE"}.get(outcome, "UNKNOWN")
    value = row[query["field_id"]]
    if op == "is_null":
        return "TRUE" if value is None else "FALSE"
    if op == "is_not_null":
        return "FALSE" if value is None else "TRUE"
    if value is None:
        return "UNKNOWN"
    constant = query["value"]
    if op == "eq":
        return "TRUE" if value == constant else "FALSE"
    if op == "ne":
        return "TRUE" if value != constant else "FALSE"
    if op == "lt":
        return "TRUE" if value < constant else "FALSE"
    if op == "le":
        return "TRUE" if value <= constant else "FALSE"
    if op == "gt":
        return "TRUE" if value > constant else "FALSE"
    if op == "ge":
        return "TRUE" if value >= constant else "FALSE"
    raise AssertionError(f"参考求值器不支持的 op：{op}")


def iter_catalog_rows(manifest_path: str = MANIFEST_PATH):
    """按 manifest 顺序、文件内行序产出 (file_id, {field_id: value})。"""
    manifest = load_json(manifest_path)
    base = os.path.dirname(manifest_path)
    for entry in manifest["files"]:
        path = os.path.join(base, entry["path"])
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                obj = json.loads(line)
                yield entry["file_id"], {int(k): v for k, v in obj.items()}


def expected_delivery(query: dict, projection: list,
                      fields: list) -> list:
    """独立参考：原始查询逐行求值，只有 TRUE 的行进入期望结果。"""
    names = {f["field_id"]: f["name"] for f in fields}
    out = []
    for _file_id, row in iter_catalog_rows():
        if ref_eval(query, row) == "TRUE":
            out.append({names[fid]: row[fid] for fid in projection})
    return out


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write_json(self, name: str, obj) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False))
        return path

    def write_text(self, name: str, text: str) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return path
