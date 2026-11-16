"""契约预检测试：形状不合法、悬空引用、坏 Pointer、资源冲突、同位置环。"""
from __future__ import annotations

import unittest

from contract_gate import Contract, ContractError

from .helpers import make_contract, make_contract_multi


def expect_error(testcase, schema, needle, files=None):
    files = files or {"root.json": schema}
    with testcase.assertRaises(ContractError) as cm:
        make_contract_multi(files)
    messages = " | ".join(p["message"] for p in cm.exception.problems)
    testcase.assertIn(needle, messages)
    return cm.exception


class TestKeywordShapes(unittest.TestCase):
    def test_unknown_keyword_rejected(self):
        expect_error(self, {"minimum": 5}, "不支持的关键字")
        expect_error(self, {"pattern": "^a"}, "不支持的关键字")
        expect_error(self, {"format": "email"}, "不支持的关键字")
        expect_error(self, {"definitions": {}}, "不支持的关键字")

    def test_annotation_keywords_ok(self):
        c = make_contract({"title": "t", "description": "d", "$comment": "c",
                           "type": "null"})
        self.assertTrue(c.validate_line(b"null").ok)

    def test_annotation_shape(self):
        expect_error(self, {"title": 5}, "title 必须是字符串")

    def test_type_shape(self):
        expect_error(self, {"type": "foo"}, "未知类型名")
        expect_error(self, {"type": []}, "type 数组不能为空")
        expect_error(self, {"type": ["string", "string"]}, "重复")
        expect_error(self, {"type": 5}, "type 必须是类型名")
        expect_error(self, {"type": ["string", 1]}, "合法类型名")

    def test_enum_shape(self):
        expect_error(self, {"enum": []}, "enum 必须是非空数组")
        expect_error(self, {"enum": "x"}, "enum 必须是非空数组")

    def test_required_shape(self):
        expect_error(self, {"required": "a"}, "required 必须是字符串数组")
        expect_error(self, {"required": [1]}, "required 必须是字符串数组")
        expect_error(self, {"required": ["a", "a"]}, "重复")
        # 空 required 在 2020-12 合法
        c = make_contract({"required": []})
        self.assertTrue(c.validate_line(b"{}").ok)

    def test_prefixitems_shape(self):
        expect_error(self, {"prefixItems": []}, "prefixItems 必须是非空契约数组")
        expect_error(self, {"prefixItems": [5]}, "元素必须是契约")

    def test_combinator_shape(self):
        expect_error(self, {"allOf": []}, "allOf 必须是非空契约数组")
        expect_error(self, {"anyOf": [5]}, "anyOf 元素必须是契约")
        expect_error(self, {"oneOf": "x"}, "oneOf 必须是非空契约数组")
        # 布尔契约是合法分支
        c = make_contract({"allOf": [True, {"type": "null"}]})
        self.assertTrue(c.validate_line(b"null").ok)

    def test_min_max_contains(self):
        expect_error(self, {"minContains": 1}, "只能与 contains 一起出现")
        expect_error(self, {"maxContains": 0}, "只能与 contains 一起出现")
        expect_error(self, {"contains": {}, "minContains": -1}, "非负整数")
        expect_error(self, {"contains": {}, "minContains": 1.5}, "非负整数")
        expect_error(self, {"contains": {}, "minContains": True}, "非负整数")
        c = make_contract({"contains": {"type": "integer"}, "minContains": 0})
        self.assertTrue(c.validate_line(b'["x"]').ok)

    def test_schema_valued_shape(self):
        expect_error(self, {"items": 5}, "items 必须是契约")
        expect_error(self, {"not": "x"}, "not 必须是契约")
        expect_error(self, {"properties": {"a": 5}}, "必须是契约")
        expect_error(self, {"unevaluatedProperties": 0}, "必须是契约")

    def test_id_shape(self):
        expect_error(self, {"$id": 5}, "$id 必须是字符串")
        expect_error(self, {"$id": "https://x/s.json#frag"}, "不允许携带片段")

    def test_node_must_be_bool_or_object(self):
        files = {"root.json": 5}
        expect_error(self, None, "契约节点必须是布尔值或对象", files=files)
        files = {"root.json": {"$defs": {"x": 5}}}
        expect_error(self, None, "必须是契约", files=files)


class TestReferences(unittest.TestCase):
    def test_dangling_ref(self):
        expect_error(self, {"$ref": "missing.json"}, "悬空引用")

    def test_bad_pointer(self):
        expect_error(self, {"$ref": "#/$defs/nope", "$defs": {"a": {}}},
                     "坏 Pointer")

    def test_pointer_to_non_schema(self):
        expect_error(self, {"$ref": "#/$defs/a/type", "$defs": {"a": {"type": "null"}}},
                     "目标不是契约节点")

    def test_unsupported_fragment(self):
        expect_error(self, {"$ref": "#anchor"}, "不支持的片段")

    def test_bad_percent_fragment(self):
        expect_error(self, {"$ref": "#/%zz"}, "不支持的片段")

    def test_resource_conflict(self):
        files = {
            "root.json": {"$id": "https://t.example/root.json",
                          "$defs": {"n": {"$id": "dup.json", "type": "null"}}},
            "other.json": {"$id": "https://t.example/other.json",
                           "$defs": {"n": {"$id": "dup.json", "type": "string"}}},
        }
        expect_error(self, None, "两处不同定义占用", files=files)

    def test_same_node_reregistered_ok(self):
        # 根 $id 与清单 URI 相同：同一节点重复注册不算冲突
        files = {"root.json": {"$id": "https://t.example/root.json",
                               "type": "null"}}
        c = make_contract_multi(files)
        self.assertTrue(c.validate_line(b"null").ok)

    def test_relative_ref_after_nested_id(self):
        files = {"root.json": {
            "$id": "https://t.example/root.json",
            "properties": {"n": {"$ref": "sub/inner.json"}},
            "$defs": {
                "s": {"$id": "sub/inner.json", "type": "integer"}}}}
        c = make_contract_multi(files)
        self.assertTrue(c.validate_line(b'{"n": 5}').ok)
        self.assertFalse(c.validate_line(b'{"n": "x"}').ok)

    def test_ref_to_boolean_schema(self):
        files = {"root.json": {"properties": {"a": {"$ref": "#/$defs/nothing"}},
                               "$defs": {"nothing": False}}}
        c = make_contract_multi(files)
        self.assertFalse(c.validate_line(b'{"a": 1}').ok)
        self.assertTrue(c.validate_line(b"{}").ok)


class TestCycles(unittest.TestCase):
    def test_direct_self_cycle(self):
        expect_error(self, {"allOf": [{"$ref": "#"}]}, "构成环")

    def test_indirect_cycle(self):
        files = {"root.json": {
            "$id": "https://t.example/root.json",
            "anyOf": [{"$ref": "#/$defs/a"}],
            "$defs": {"a": {"oneOf": [{"$ref": "#"}]}}}}
        expect_error(self, None, "构成环", files=files)

    def test_dependent_schemas_cycle(self):
        expect_error(self, {"dependentSchemas": {"x": {"$ref": "#"}}}, "构成环")

    def test_if_then_cycle(self):
        expect_error(self, {"if": {}, "then": {"$ref": "#"}}, "构成环")

    def test_descending_recursion_allowed(self):
        # 沿对象属性/数组元素下降的递归是合法用法
        c = make_contract({
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "children": {"type": "array", "items": {"$ref": "#"}}},
            "unevaluatedProperties": False})
        ok = c.validate_line(b'{"name":"a","children":[{"name":"b","children":[]}]}')
        self.assertTrue(ok.ok)
        bad = c.validate_line(b'{"name":"a","children":[{"name":"b","extra":1}]}')
        self.assertFalse(bad.ok)


class TestManifest(unittest.TestCase):
    def test_root_not_registered(self):
        import os
        import json
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "m.json"), "w") as f:
            json.dump({"resources": {"https://t.example/a.json": "a.json"}}, f)
        with open(os.path.join(d, "a.json"), "w") as f:
            json.dump({}, f)
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(os.path.join(d, "m.json"),
                                   "https://t.example/other.json")
        self.assertIn("未在资源清单/契约中注册", str(cm.exception))

    def test_relative_manifest_paths(self):
        # 清单相对路径基于清单所在目录解析（见 make_contract 全部用例）
        c = make_contract({"type": "integer"})
        self.assertTrue(c.validate_line(b"1").ok)


class TestManifestErrors(unittest.TestCase):
    def _manifest_dir(self, manifest_text, files=None):
        import os
        import tempfile
        d = tempfile.mkdtemp(prefix="cg-mani-")
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(manifest_text)
        for name, text in (files or {}).items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
        return os.path.join(d, "manifest.json")

    def test_duplicate_manifest_key(self):
        path = self._manifest_dir(
            '{"resources": {"https://t.example/a.json": "a.json",'
            ' "https://t.example/a.json": "a.json"}}',
            {"a.json": "{}"})
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("重复成员", str(cm.exception))

    def test_manifest_shape(self):
        for bad in ('{}', '{"resources": {}}', '[1]', '"x"'):
            path = self._manifest_dir(bad)
            with self.assertRaises(ContractError, msg=bad):
                Contract.from_manifest(path, "https://t.example/a.json")

    def test_manifest_not_json(self):
        path = self._manifest_dir('{oops')
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("不是合法 UTF-8 JSON", str(cm.exception))

    def test_manifest_uri_must_be_absolute(self):
        path = self._manifest_dir('{"resources": {"a.json": "a.json"}}',
                                  {"a.json": "{}"})
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("绝对 URI", str(cm.exception))

    def test_manifest_uri_no_fragment(self):
        path = self._manifest_dir(
            '{"resources": {"https://t.example/a.json#x": "a.json"}}',
            {"a.json": "{}"})
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("不允许携带片段", str(cm.exception))

    def test_contract_file_missing(self):
        path = self._manifest_dir(
            '{"resources": {"https://t.example/a.json": "nope.json"}}')
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("无法读取契约文件", str(cm.exception))

    def test_contract_file_bad_json(self):
        path = self._manifest_dir(
            '{"resources": {"https://t.example/a.json": "a.json"}}',
            {"a.json": "{bad"})
        with self.assertRaises(ContractError) as cm:
            Contract.from_manifest(path, "https://t.example/a.json")
        self.assertIn("不是合法 UTF-8 JSON", str(cm.exception))

    def test_root_uri_fragment(self):
        path = self._manifest_dir(
            '{"resources": {"https://t.example/a.json": "a.json"}}',
            {"a.json": "{}"})
        # 空片段归一化，可用
        c = Contract.from_manifest(path, "https://t.example/a.json#")
        self.assertEqual(c.root_uri, "https://t.example/a.json")
        # 非空片段拒绝
        with self.assertRaises(ContractError):
            Contract.from_manifest(path, "https://t.example/a.json#x")


if __name__ == "__main__":
    unittest.main()
