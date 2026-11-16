"""与独立标准实现（jsonschema 库，如已安装）对支持子集做真假对照。

本项目的核心求值与来源定位不依赖该库；这里仅做交叉 sanity check。
库不存在时整组测试跳过。注意不纳入 true 与 1 的 const 比较等
各实现定义不同的边角（本题明确规定 true ≠ 1，见 test_evaluate）。
"""
from __future__ import annotations

import importlib.util
import json
import unittest

from .helpers import make_contract

jsonschema = None
if importlib.util.find_spec("jsonschema") is not None:
    import jsonschema  # type: ignore


CASES = [
    # (schema, instance, 说明)
    ({"type": "integer"}, "1.0", "1.0 是数学整数"),
    ({"type": "integer"}, "1.5", "非整数"),
    ({"type": ["string", "number"]}, "1.5", "类型数组"),
    ({"const": {"a": 1, "b": 2}}, '{"b":2,"a":1}', "对象不看成员顺序"),
    ({"const": [1, 2]}, "[2,1]", "数组看顺序"),
    ({"enum": [1, "a", None]}, "null", "enum 命中 null"),
    ({"required": ["a"]}, "{}", "缺必需字段"),
    ({"properties": {"a": {"type": "integer"}}}, '{"a":"x"}', "属性类型"),
    ({"additionalProperties": False, "properties": {"a": {}}}, '{"a":1,"b":2}',
     "additionalProperties 只认本层 properties"),
    ({"allOf": [{"properties": {"a": {}}}], "unevaluatedProperties": False},
     '{"a":1}', "allOf 覆盖传入 unevaluatedProperties"),
    ({"anyOf": [{"properties": {"a": {}}}, {"properties": {"b": {}}}],
      "unevaluatedProperties": False},
     '{"a":1,"b":2}', "anyOf 两成功分支合并覆盖"),
    ({"anyOf": [{"properties": {"a": {}}}, {"properties": {"b": {"type": "string"}}}],
      "unevaluatedProperties": False},
     '{"a":1,"b":2}', "anyOf 失败分支不贡献"),
    ({"oneOf": [{"properties": {"a": {}}}, {"properties": {"b": {}}}],
      "unevaluatedProperties": False},
     '{"a":1,"b":2}', "oneOf 两成功必须拒收"),
    ({"not": {"type": "string"}}, "5", "not 通过"),
    ({"not": {"type": "string"}}, '"x"', "not 拦截"),
    ({"if": {"properties": {"k": {"const": "r"}}}, "then": {"required": ["r"]},
      "else": {"properties": {"r": {"type": "null"}}}},
     '{"k":"x","r":null}', "else 分支"),
    ({"if": {"properties": {"k": {"const": "r"}}}, "then": {"required": ["r"]}},
     '{"k":"r"}', "then 缺字段"),
    ({"dependentSchemas": {"a": {"required": ["b"]}}}, '{"a":1}', "依赖未满足"),
    ({"dependentSchemas": {"a": {"required": ["b"]}}}, '{"a":1,"b":2}', "依赖满足"),
    ({"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
     '["a",1,2]', "前缀加尾部"),
    ({"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
     '["a","b"]', "尾部类型错误"),
    ({"contains": {"type": "integer"}, "minContains": 2}, '[1,"x",2]',
     "minContains 达标"),
    ({"contains": {"type": "integer"}, "minContains": 2}, '[1,"x"]',
     "minContains 不足"),
    ({"contains": {"type": "integer"}, "maxContains": 1}, "[1,2]",
     "maxContains 超限"),
    ({"contains": {"type": "integer"}, "unevaluatedItems": False}, '[1,"x"]',
     "contains 只认领命中位置"),
    ({"prefixItems": [{}], "unevaluatedItems": False}, '["a",1]',
     "unevaluatedItems 拦截"),
    ({"unevaluatedProperties": False, "properties": {"a": {}}}, '{"a":1,"b":2}',
     "剩余字段拦截"),
    ({"$ref": "#/$defs/x", "$defs": {"x": {"type": "integer"}}}, '"s"',
     "$ref 目标判定"),
    ({"allOf": [{"$ref": "#/$defs/x"}, {"type": "integer"}],
      "$defs": {"x": {"type": "number"}}}, "1.5", "$ref 与兄弟"),
]


@unittest.skipIf(jsonschema is None, "jsonschema 库未安装，跳过交叉对照")
class TestCrossCheck(unittest.TestCase):
    def test_verdicts_match_reference(self):
        from jsonschema import Draft202012Validator
        for schema, instance_text, note in CASES:
            with self.subTest(note):
                instance = json.loads(instance_text)
                ref_valid = Draft202012Validator(schema).is_valid(instance)
                ours = make_contract(schema).validate_line(
                    instance_text.encode("utf-8"))
                self.assertEqual(ours.ok, ref_valid,
                                 f"{note}: 本项目={ours.ok} 参考={ref_valid}")


if __name__ == "__main__":
    unittest.main()
