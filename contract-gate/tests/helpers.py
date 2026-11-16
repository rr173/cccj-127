"""测试公共辅助：用临时目录把内存中的契约编成 Contract。"""
from __future__ import annotations

import json
import os
import tempfile

from contract_gate import Contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")
MANIFEST = os.path.join(EXAMPLES, "manifest.json")
SC = "https://acme.example/schemas"


def write_contract_files(files: dict, root_name="root.json"):
    """files: {文件名: 契约对象}；返回 (manifest_path, root_uri)。"""
    dirpath = tempfile.mkdtemp(prefix="cg-test-")
    resources = {}
    for name, schema in files.items():
        with open(os.path.join(dirpath, name), "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False)
        uri = f"https://t.example/{name}"
        resources[uri] = name
        if name == root_name:
            root_uri = uri
    manifest_path = os.path.join(dirpath, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"resources": resources}, f)
    return manifest_path, root_uri


def make_contract(schema, root_name="root.json"):
    manifest_path, root_uri = write_contract_files({root_name: schema}, root_name)
    return Contract.from_manifest(manifest_path, root_uri)


def make_contract_multi(files: dict, root_name="root.json"):
    manifest_path, root_uri = write_contract_files(files, root_name)
    return Contract.from_manifest(manifest_path, root_uri)


def keywords(result):
    """顶层 issue 的 (keyword, instance) 序列。"""
    return [(i.keyword, i.instance) for i in result.issues]


def flat_issues(issues):
    for it in issues:
        yield it
        for br in it.branches:
            yield from flat_issues(br.issues)
