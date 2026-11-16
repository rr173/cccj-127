"""求值语义测试：组合覆盖传播、数组位置、条件分支、精确相等。"""
from __future__ import annotations

import unittest

from .helpers import flat_issues, keywords, make_contract


class TestAnyOfCoverage(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "anyOf": [
                {"required": ["a"], "properties": {"a": {"type": "integer"}}},
                {"required": ["b"], "properties": {"b": {"type": "integer"}}},
            ],
            "unevaluatedProperties": False,
        }

    def test_both_branches_merge(self):
        c = make_contract(self.schema)
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage, {"properties": ["a", "b"], "items": []})

    def test_failed_branch_contributes_nothing(self):
        schema = dict(self.schema)
        schema["anyOf"] = [
            {"required": ["a"], "properties": {"a": {"type": "integer"}}},
            {"required": ["b"], "properties": {"b": {"type": "string"}}},
        ]
        c = make_contract(schema)
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/b")])

    def test_all_branches_fail_keeps_branch_evidence(self):
        c = make_contract(self.schema)
        r = c.validate_line(b'{"c":1}')
        self.assertFalse(r.ok)
        anyof = r.issues[0]
        self.assertEqual(anyof.keyword, "anyOf")
        self.assertEqual([b.name for b in anyof.branches],
                         ["anyOf[0]", "anyOf[1]"])
        self.assertTrue(all(b.issues for b in anyof.branches))


class TestOneOf(unittest.TestCase):
    def setUp(self):
        self.c = make_contract({
            "oneOf": [
                {"required": ["a"], "properties": {"a": {"type": "integer"}}},
                {"required": ["b"], "properties": {"b": {"type": "integer"}}},
            ],
            "unevaluatedProperties": False,
        })

    def test_both_valid_rejected(self):
        r = self.c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "oneOf")
        self.assertEqual(r.issues[0].branches, [])  # 成功分支不进错误树

    def test_exactly_one_valid(self):
        r = self.c.validate_line(b'{"a":1}')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage["properties"], ["a"])

    def test_zero_valid(self):
        r = self.c.validate_line(b'{"c":1}')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "oneOf")
        self.assertEqual(len(r.issues[0].branches), 2)


class TestNotIfThenElse(unittest.TestCase):
    def test_not_contributes_no_coverage(self):
        c = make_contract({"not": {"required": ["a"]},
                           "unevaluatedProperties": False})
        r = c.validate_line(b'{"b":1}')
        self.assertFalse(r.ok)  # not 通过但 b 仍未被覆盖
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/b")])
        r = c.validate_line(b'{"a":1}')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "not")

    def test_if_success_keeps_coverage(self):
        c = make_contract({
            "if": {"properties": {"a": {"type": "integer"}}},
            "then": {"required": ["a"]},
            "unevaluatedProperties": False,
        })
        # if 成功 → if 的 properties 覆盖保留，a 不触发剩余字段检查
        self.assertTrue(c.validate_line(b'{"a":1}').ok)

    def test_if_failure_drops_coverage(self):
        c = make_contract({
            "if": {"properties": {"a": {"const": 1}}},
            "then": {},
            "unevaluatedProperties": False,
        })
        # if 失败（a≠1）→ if 的覆盖不保留 → a 未覆盖
        r = c.validate_line(b'{"a":2}')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/a")])

    def test_only_selected_branch_contributes(self):
        c = make_contract({
            "if": {"required": ["a"]},
            "else": {"properties": {"b": True}},
            "unevaluatedProperties": False,
        })
        self.assertTrue(c.validate_line(b'{"b":1}').ok)          # else 选中
        r = c.validate_line(b'{"a":1,"b":1}')
        self.assertFalse(r.ok)  # if 成立 → else 不执行 → b 未覆盖
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/a"),
                                       ("unevaluatedProperties", "/b")])

    def test_then_else_ignored_without_if(self):
        c = make_contract({"then": {"required": ["x"]},
                           "else": {"required": ["y"]}})
        self.assertTrue(c.validate_line(b"{}").ok)

    def test_if_without_then_adds_no_coverage(self):
        c = make_contract({
            "if": {"required": ["a"]},
            "unevaluatedProperties": False,
        })
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)  # then 缺席不凭空增加覆盖
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/a"),
                                       ("unevaluatedProperties", "/b")])


class TestDependentSchemas(unittest.TestCase):
    def test_triggered_claims(self):
        c = make_contract({
            "properties": {"a": {}},
            "dependentSchemas": {
                "a": {"properties": {"b": {"type": "integer"}}}},
            "unevaluatedProperties": False,
        })
        self.assertTrue(c.validate_line(b'{"a":1,"b":2}').ok)
        r = c.validate_line(b'{"b":2}')  # 未触发 → b 未覆盖
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/b")])

    def test_failed_dependent_contributes_nothing(self):
        c = make_contract({
            "dependentSchemas": {
                "a": {"required": ["c"],
                      "properties": {"b": {"type": "integer"}}}},
            "unevaluatedProperties": False,
        })
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)
        self.assertIn(("dependentSchemas", ""), keywords(r))
        self.assertIn(("unevaluatedProperties", "/b"), keywords(r))


class TestAdditionalVsUnevaluated(unittest.TestCase):
    def test_additional_properties_only_own_level(self):
        # additionalProperties 不能借用 allOf 分支里的 properties
        c = make_contract({
            "allOf": [{"properties": {"a": {"type": "integer"}}}],
            "additionalProperties": False,
        })
        r = c.validate_line(b'{"a":1}')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "additionalProperties")

    def test_unevaluated_sees_allof_coverage(self):
        c = make_contract({
            "allOf": [{"properties": {"a": {"type": "integer"}}}],
            "unevaluatedProperties": False,
        })
        self.assertTrue(c.validate_line(b'{"a":1}').ok)

    def test_unevaluated_boolean_true_claims_rest(self):
        c = make_contract({
            "properties": {"a": {"type": "integer"}},
            "unevaluatedProperties": True,
        })
        r = c.validate_line(b'{"a":1,"b":"x"}')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage["properties"], ["a", "b"])


class TestRefSiblings(unittest.TestCase):
    def test_sibling_keywords_apply(self):
        c = make_contract({
            "properties": {
                "a": {"$ref": "#/$defs/txt", "enum": ["x", "y"]}},
            "$defs": {"txt": {"type": "string"}},
        })
        self.assertTrue(c.validate_line(b'{"a":"x"}').ok)
        r = c.validate_line(b'{"a":"z"}')  # ref 通过、enum 失败
        self.assertFalse(r.ok)
        kinds = [i.keyword for i in flat_issues(r.issues)]
        self.assertIn("enum", kinds)
        r = c.validate_line(b'{"a":1}')  # ref 失败
        self.assertFalse(r.ok)
        kinds = [i.keyword for i in flat_issues(r.issues)]
        self.assertIn("type", kinds)


class TestArrays(unittest.TestCase):
    def test_prefix_items_positions(self):
        c = make_contract({
            "prefixItems": [{"type": "string"}],
            "items": {"type": "integer"},
            "unevaluatedItems": False,
        })
        r = c.validate_line(b'["a",1,2]')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage["items"], [0, 1, 2])
        r = c.validate_line(b'["a",1,"x"]')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("items", ""),
                                       ("unevaluatedItems", "/2")])
        # 前缀超出长度不报错
        self.assertTrue(c.validate_line(b'["a"]').ok)

    def test_items_starts_after_prefix(self):
        c = make_contract({
            "prefixItems": [{"type": "string"}, {"type": "string"}],
            "items": {"type": "integer"},
        })
        self.assertTrue(c.validate_line(b'["a","b",1]').ok)
        r = c.validate_line(b'["a","b","c"]')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "items")

    def test_contains_claims_only_matched(self):
        c = make_contract({
            "contains": {"type": "integer"},
            "unevaluatedItems": False,
        })
        r = c.validate_line(b'[1,"x"]')
        self.assertFalse(r.ok)  # contains 成功但 "x" 未被认领
        self.assertEqual(keywords(r), [("unevaluatedItems", "/1")])
        self.assertTrue(c.validate_line(b"[1,2]").ok)

    def test_contains_min_max(self):
        c = make_contract({"contains": {"type": "integer"}, "minContains": 2})
        self.assertTrue(c.validate_line(b"[1,2]").ok)
        r = c.validate_line(b'[1,"x"]')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "contains")
        c = make_contract({"contains": {"type": "integer"}, "maxContains": 1})
        r = c.validate_line(b"[1,2]")
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "maxContains")

    def test_contains_default_min_is_one(self):
        c = make_contract({"contains": {"type": "integer"}})
        self.assertFalse(c.validate_line(b'["x"]').ok)

    def test_unevaluated_items_subschema(self):
        c = make_contract({
            "prefixItems": [{"type": "string"}],
            "unevaluatedItems": {"type": "integer"},
        })
        self.assertTrue(c.validate_line(b'["a",1,2]').ok)
        r = c.validate_line(b'["a",1,"x"]')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedItems", "/2")])


class TestTypeGating(unittest.TestCase):
    def test_object_keywords_ignore_non_objects(self):
        c = make_contract({"properties": {"a": {"type": "integer"}},
                           "required": ["a"]})
        self.assertTrue(c.validate_line(b'"just a string"').ok)
        self.assertTrue(c.validate_line(b"[1,2]").ok)

    def test_array_keywords_ignore_non_arrays(self):
        c = make_contract({"items": {"type": "integer"}, "minContains": 3,
                           "contains": {"type": "string"}})
        self.assertTrue(c.validate_line(b"{}").ok)

    def test_type_keyword_decides(self):
        c = make_contract({"type": "object"})
        self.assertTrue(c.validate_line(b"{}").ok)
        self.assertFalse(c.validate_line(b"[]").ok)
        self.assertFalse(c.validate_line(b"null").ok)

    def test_empty_shapes(self):
        c = make_contract({"type": ["object", "array"]})
        self.assertTrue(c.validate_line(b"{}").ok)
        self.assertTrue(c.validate_line(b"[]").ok)
        self.assertFalse(c.validate_line(b"null").ok)

    def test_explicit_null_vs_missing(self):
        c = make_contract({"properties": {"a": {"type": "null"}}})
        self.assertTrue(c.validate_line(b'{"a":null}').ok)
        self.assertTrue(c.validate_line(b"{}").ok)  # 缺失不触发 properties
        self.assertFalse(c.validate_line(b'{"a":0}').ok)


class TestConstEnum(unittest.TestCase):
    def test_const_precision(self):
        c = make_contract({"const": 9007199254740993})
        self.assertTrue(c.validate_line(b"9007199254740993").ok)
        self.assertFalse(c.validate_line(b"9007199254740992").ok)

    def test_const_true_is_not_one(self):
        c = make_contract({"const": 1})
        self.assertTrue(c.validate_line(b"1").ok)
        self.assertFalse(c.validate_line(b"true").ok)

    def test_const_decimal_math(self):
        c = make_contract({"const": 1.0})
        self.assertTrue(c.validate_line(b"1").ok)
        self.assertTrue(c.validate_line(b"1.00").ok)

    def test_const_structures(self):
        c = make_contract({"const": {"a": 1, "b": [1, 2]}})
        self.assertTrue(c.validate_line(b'{"b":[1,2],"a":1}').ok)
        self.assertFalse(c.validate_line(b'{"a":1,"b":[2,1]}').ok)

    def test_enum(self):
        c = make_contract({"enum": [1, "1", True, None, [1], {"x": 1}]})
        for good in (b"1", b'"1"', b"true", b"null", b"[1]", b'{"x":1}', b"1.0"):
            self.assertTrue(c.validate_line(good).ok, good)
        for bad in (b"2", b"false", b'"x"', b"[]", b"{}", b'{"x":2}'):
            self.assertFalse(c.validate_line(bad).ok, bad)


class TestErrorTreeIntegrity(unittest.TestCase):
    def test_successful_branches_not_in_error_tree(self):
        c = make_contract({
            "anyOf": [
                {"properties": {"a": {"type": "integer"}}},
                {"properties": {"b": {"type": "string"}}},
            ],
            "unevaluatedProperties": False,
        })
        r = c.validate_line(b'{"a":1,"c":2}')
        self.assertFalse(r.ok)
        # anyOf 成功（分支0）→ 错误树只有 unevaluatedProperties /c
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/c")])

    def test_failed_rule_coverage_does_not_leak(self):
        c = make_contract({
            "allOf": [
                {"properties": {"a": {"const": 1}}},
                {"properties": {"b": {"type": "integer"}}},
            ],
            "unevaluatedProperties": False,
        })
        r = c.validate_line(b'{"a":2,"b":3}')
        self.assertFalse(r.ok)
        # allOf[0] 失败 → a 未覆盖；b 由 allOf[1] 认领
        sig = keywords(r)
        self.assertIn(("unevaluatedProperties", "/a"), sig)
        self.assertNotIn(("unevaluatedProperties", "/b"), sig)

    def test_keyword_order_irrelevant(self):
        base = {"type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["a"],
                "unevaluatedProperties": False}
        shuffled = {"unevaluatedProperties": False,
                    "required": ["a"],
                    "properties": {"a": {"type": "integer"}},
                    "type": "object"}
        self.assertNotEqual(list(base), list(shuffled))  # 书写顺序确实不同
        c1 = make_contract(dict(base))
        c2 = make_contract(dict(shuffled))
        for line in (b'{"a":1}', b'{"a":"x"}', b'{"a":1,"b":2}', b'{}'):
            self.assertEqual(c1.validate_line(line).ok,
                             c2.validate_line(line).ok, line)


if __name__ == "__main__":
    unittest.main()
