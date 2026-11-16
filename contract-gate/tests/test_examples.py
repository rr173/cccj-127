"""业务组合测试：直接用 examples/ 下的契约与来件，预期全部手工写出。"""
from __future__ import annotations

import unittest

from contract_gate import Contract

from .helpers import EXAMPLES, MANIFEST, SC, flat_issues, keywords


def body_of(path, lineno):
    with open(path, "rb") as f:
        for i, raw in enumerate(f, 1):
            if i == lineno:
                return raw.rstrip(b"\r\n")
    raise AssertionError(path)


class TestRootContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Contract.from_manifest(MANIFEST, f"{SC}/root.json")
        cls.orders = f"{EXAMPLES}/data/orders.jsonl"

    def v(self, lineno):
        return self.c.validate_line(body_of(self.orders, lineno))

    def test_verdicts(self):
        expect = {1: True, 2: False, 3: False, 4: True, 5: True, 6: False,
                  7: False, 8: True, 9: False, 10: False, 11: True, 12: False}
        for lineno, ok in expect.items():
            self.assertEqual(self.v(lineno).ok, ok, f"第{lineno}行")

    def test_ref_sibling_enum(self):
        r = self.v(2)
        kinds = [(i.keyword, i.instance) for i in flat_issues(r.issues)]
        self.assertIn(("enum", "/id"), kinds)

    def test_if_then_required(self):
        r = self.v(3)
        kinds = [(i.keyword, i.instance) for i in flat_issues(r.issues)]
        self.assertIn(("then", ""), kinds)
        self.assertIn(("required", ""), kinds)

    def test_else_branch_selected(self):
        r = self.v(6)
        kinds = [(i.keyword, i.instance) for i in flat_issues(r.issues)]
        self.assertIn(("else", ""), kinds)
        self.assertIn(("type", "/reason"), kinds)

    def test_dependent_schemas(self):
        r = self.v(7)
        kinds = [(i.keyword, i.instance) for i in flat_issues(r.issues)]
        self.assertIn(("dependentSchemas", ""), kinds)
        self.assertIn(("required", ""), kinds)
        r = self.v(9)
        kinds = [(i.keyword, i.instance) for i in flat_issues(r.issues)]
        self.assertIn(("const", "/currency"), kinds)

    def test_coverage_sets(self):
        self.assertEqual(self.v(1).coverage["properties"], ["id", "kind"])
        self.assertEqual(self.v(4).coverage["properties"],
                         ["id", "kind", "reason"])
        self.assertEqual(self.v(8).coverage["properties"],
                         ["currency", "discount", "id", "kind"])
        self.assertEqual(self.v(11).coverage["properties"],
                         ["child", "id", "kind", "route", "tag"])

    def test_nested_resource_error_location(self):
        r = self.v(12)
        deep = [i for i in flat_issues(r.issues)
                if i.keyword == "unevaluatedProperties"
                and i.instance == "/child/children/0/bogus"]
        self.assertEqual(len(deep), 1)
        self.assertEqual(deep[0].resource, f"{SC}/node.json")
        self.assertEqual(deep[0].pointer, "/unevaluatedProperties")
        self.assertEqual(deep[0].span, (71, 83))
        self.assertEqual(body_of(self.orders, 12)[71:83], b'"bogus":true')

    def test_shuffled_contract_same_verdicts(self):
        c2 = Contract.from_manifest(MANIFEST, f"{SC}/root.shuffled.json")
        for lineno in range(1, 13):
            body = body_of(self.orders, lineno)
            self.assertEqual(self.c.validate_line(body).ok,
                             c2.validate_line(body).ok, f"第{lineno}行")


class TestCombos(unittest.TestCase):
    def test_anyof_both_ok(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/combo.anyof.json")
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage, {"properties": ["a", "b"], "items": []})

    def test_anyof_failed_branch_no_claim(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/combo.anyof.strb.json")
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/b")])
        self.assertEqual(r.issues[0].span, (7, 12))

    def test_oneof_both_ok_rejected(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/combo.oneof.json")
        r = c.validate_line(b'{"a":1,"b":2}')
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].keyword, "oneOf")

    def test_contains_positions(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/combo.contains.json")
        r = c.validate_line(b'[1,"x",2]')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r), [("unevaluatedItems", "/1")])
        self.assertEqual(r.issues[0].span, (3, 6))
        r = c.validate_line(b"[1,3,2]")
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage["items"], [0, 1, 2])

    def test_tangle_no_cross_wire(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/branch.tangle.json")
        r = c.validate_line(b'{"x":{"deep":2},"y":9}')
        self.assertFalse(r.ok)
        # 失败分支 anyOf[0] 的深层错误不外泄；合格分支认领 x；只有 /y 未覆盖
        self.assertEqual(keywords(r), [("unevaluatedProperties", "/y")])
        leaked = [i for i in flat_issues(r.issues)
                  if i.instance.startswith("/x")]
        self.assertEqual(leaked, [])
        r = c.validate_line(b'{"x":{"deep":1}}')
        self.assertTrue(r.ok)
        self.assertEqual(r.coverage["properties"], ["x"])

    def test_tangle2_deep_evidence(self):
        c = Contract.from_manifest(MANIFEST, f"{SC}/branch.tangle2.json")
        r = c.validate_line(b'{"x":{"deep":2}}')
        self.assertFalse(r.ok)
        self.assertEqual(keywords(r)[0], ("allOf", ""))
        flat = list(flat_issues(r.issues))
        self.assertTrue(any(i.keyword == "const" and i.instance == "/x/deep"
                            for i in flat))
        self.assertTrue(any(i.keyword == "required" and i.instance == "/x"
                            for i in flat))
        self.assertIn(("unevaluatedProperties", "/x"), keywords(r))


class TestMixedFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Contract.from_manifest(MANIFEST, f"{SC}/mixed.json")
        cls.mixed = f"{EXAMPLES}/data/mixed.jsonl"

    def v(self, lineno):
        return self.c.validate_line(body_of(self.mixed, lineno))

    def test_verdicts(self):
        expect = {1: True, 2: True, 3: False, 4: True, 5: False, 6: False,
                  7: False, 8: False, 9: False, 10: False, 11: False, 12: True}
        for lineno, ok in expect.items():
            self.assertEqual(self.v(lineno).ok, ok, f"第{lineno}行")

    def test_categories(self):
        self.assertEqual(self.v(5).category, "duplicate")
        self.assertEqual(self.v(6).category, "duplicate")
        self.assertEqual(self.v(10).category, "json")
        self.assertEqual(self.v(11).category, "empty")
        self.assertEqual(self.v(3).category, "contract")

    def test_big_number_precision(self):
        self.assertTrue(self.v(2).ok)   # 9007199254740993 == const
        r = self.v(3)                    # 9007199254740992 != const
        const = [i for i in flat_issues(r.issues) if i.keyword == "const"]
        self.assertEqual(len(const), 1)
        self.assertEqual(const[0].span, (17, 33))
        self.assertEqual(body_of(self.mixed, 3)[17:33], b"9007199254740992")

    def test_duplicate_spans(self):
        r = self.v(5)
        self.assertEqual(r.parse["key"], "escAped")
        s1, s2 = r.parse["spans"]
        self.assertEqual(body_of(self.mixed, 5)[s1[0]:s1[1]], b'"escAped"')
        self.assertEqual(body_of(self.mixed, 5)[s2[0]:s2[1]],
                         b'"esc\\u0041ped"')

    def test_multibyte_member_span(self):
        r = self.v(7)
        iss = r.issues[0]
        self.assertEqual(iss.keyword, "unevaluatedProperties")
        self.assertEqual(iss.instance, "/extra")
        self.assertEqual(body_of(self.mixed, 7)[iss.span[0]:iss.span[1]],
                         '"extra":"多余"'.encode("utf-8"))

    def test_same_name_different_levels(self):
        r = self.v(8)
        types = [i for i in flat_issues(r.issues) if i.keyword == "type"]
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0].instance, "/obj/id")
        self.assertEqual(body_of(self.mixed, 8)[types[0].span[0]:types[0].span[1]],
                         b"5")

    def test_escaped_key_coverage(self):
        r = self.v(4)
        self.assertEqual(r.coverage["properties"], ["escAped", "id"])


if __name__ == "__main__":
    unittest.main()
