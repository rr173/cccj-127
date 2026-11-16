"""JSON 解析器测试：字节范围、多字节、转义键、重复成员、精确数、深嵌套。"""
from __future__ import annotations

import unittest
from decimal import Decimal

from contract_gate.jdoc import (DuplicateMember, EmptyLine, LineError,
                                Utf8LineError, node_type, parse_line,
                                type_matches, value_equal)


class TestSpans(unittest.TestCase):
    def test_scalar_spans(self):
        node = parse_line(b'  {"a": 12, "b": [true, null]}  ')
        self.assertEqual((node.start, node.end), (2, 30))
        m_a = node.index["a"]
        self.assertEqual((m_a.key_start, m_a.key_end), (3, 6))
        self.assertEqual((m_a.value.start, m_a.value.end), (8, 10))
        self.assertEqual(m_a.value.value, Decimal(12))
        m_b = node.index["b"]
        arr = m_b.value
        self.assertEqual((arr.start, arr.end), (17, 29))
        self.assertEqual((arr.value[0].start, arr.value[0].end), (18, 22))
        self.assertEqual((arr.value[1].start, arr.value[1].end), (24, 28))

    def test_multibyte_spans(self):
        line = '{"k":"中文"}'.encode("utf-8")
        node = parse_line(line)
        m = node.index["k"]
        # "{"k":" = 5 字节；中=3 文=3；值 "中文" 共 8 字节
        self.assertEqual((m.value.start, m.value.end), (5, 13))
        self.assertEqual(line[5:13], '"中文"'.encode("utf-8"))

    def test_escaped_key_decoded_and_spanned(self):
        line = b'{"esc\\u0041ped":1}'
        node = parse_line(line)
        self.assertIn("escAped", node.index)
        m = node.index["escAped"]
        self.assertEqual((m.key_start, m.key_end), (1, 15))
        self.assertEqual(line[1:15], b'"esc\\u0041ped"')

    def test_unicode_escape_pairs(self):
        node = parse_line(b'"\\uD83D\\uDE00"')
        self.assertEqual(node.value, "\U0001F600")
        node = parse_line(b'"\\u0041\\u00e4"')
        self.assertEqual(node.value, "Aä")

    def test_deep_nesting_no_recursion_error(self):
        depth = 20000
        line = b"[" * depth + b"1" + b"]" * depth
        node = parse_line(line)
        cur = node
        for _ in range(depth):
            self.assertEqual(cur.kind, "arr")
            cur = cur.value[0]
        self.assertEqual(cur.value, Decimal(1))


class TestParseErrors(unittest.TestCase):
    def test_empty_line(self):
        with self.assertRaises(EmptyLine) as cm:
            parse_line(b"")
        self.assertEqual(cm.exception.category, "empty")
        self.assertEqual((cm.exception.start, cm.exception.end), (0, 0))

    def test_whitespace_only(self):
        with self.assertRaises(LineError) as cm:
            parse_line(b"   ")
        self.assertEqual(cm.exception.category, "json")

    def test_invalid_utf8(self):
        with self.assertRaises(Utf8LineError) as cm:
            parse_line(b'{"a":"\xff"}')
        self.assertEqual(cm.exception.category, "utf8")
        self.assertEqual((cm.exception.start, cm.exception.end), (6, 7))

    def test_bad_json_position(self):
        with self.assertRaises(LineError) as cm:
            parse_line(b'{"a":1,}')
        self.assertEqual(cm.exception.category, "json")
        self.assertEqual((cm.exception.start, cm.exception.end), (7, 8))

    def test_trailing_data(self):
        with self.assertRaises(LineError) as cm:
            parse_line(b'{} []')
        self.assertEqual((cm.exception.start, cm.exception.end), (3, 4))

    def test_duplicate_member_both_positions(self):
        with self.assertRaises(DuplicateMember) as cm:
            parse_line(b'{"dup":1,"dup":2}')
        e = cm.exception
        self.assertEqual(e.category, "duplicate")
        self.assertEqual(e.extra["key"], "dup")
        self.assertEqual(e.extra["spans"], [[1, 6], [9, 14]])

    def test_duplicate_after_escape_decoding(self):
        with self.assertRaises(DuplicateMember) as cm:
            parse_line(b'{"escAped":true,"esc\\u0041ped":false}')
        self.assertEqual(cm.exception.extra["spans"], [[1, 10], [16, 30]])

    def test_bad_number(self):
        for bad in (b"01", b"-", b"1.", b".5", b"1e", b"+1", b"0x1"):
            with self.assertRaises(LineError, msg=bad):
                parse_line(bad)

    def test_bad_string(self):
        for bad in (b'"\\x"', b'"\\u12"', b'"\\uzzzz"', b'"a\x01b"', b'"\\uD800"',
                    b'"\\uDC00"', b'"\\uD800\\u0041"'):
            with self.assertRaises(LineError, msg=bad):
                parse_line(bad)


class TestNumbers(unittest.TestCase):
    def test_big_integer_exact(self):
        node = parse_line(b"9007199254740993")
        self.assertEqual(node.value, Decimal(9007199254740993))
        self.assertEqual(node_type(node), "integer")

    def test_beyond_float64(self):
        node = parse_line(b"123456789012345678901234567890")
        self.assertEqual(node.value, Decimal("123456789012345678901234567890"))

    def test_exponent_integral(self):
        self.assertEqual(node_type(parse_line(b"1e2")), "integer")
        self.assertEqual(node_type(parse_line(b"1.5")), "number")
        self.assertEqual(node_type(parse_line(b"-0.0")), "integer")

    def test_type_matches(self):
        num = parse_line(b"1.5")
        self.assertTrue(type_matches(num, "number"))
        self.assertFalse(type_matches(num, "integer"))
        self.assertFalse(type_matches(num, "string"))
        self.assertTrue(type_matches(parse_line(b"true"), "boolean"))
        self.assertFalse(type_matches(parse_line(b"true"), "number"))


class TestEquality(unittest.TestCase):
    def test_decimal_math_equality(self):
        self.assertTrue(value_equal(parse_line(b"1"), 1))
        self.assertTrue(value_equal(parse_line(b"1.0"), 1))
        self.assertTrue(value_equal(parse_line(b"1"), Decimal("1.00")))
        self.assertTrue(value_equal(parse_line(b"100"), Decimal("1e2")))

    def test_bool_is_not_number(self):
        self.assertFalse(value_equal(parse_line(b"true"), 1))
        self.assertFalse(value_equal(parse_line(b"1"), True))
        self.assertTrue(value_equal(parse_line(b"true"), True))

    def test_object_order_insensitive(self):
        self.assertTrue(value_equal(parse_line(b'{"a":1,"b":2}'),
                                    {"b": 2, "a": 1}))
        self.assertFalse(value_equal(parse_line(b'{"a":1,"b":2}'),
                                     {"a": 1, "b": 3}))

    def test_array_order_sensitive(self):
        self.assertTrue(value_equal(parse_line(b"[1,2]"), [1, 2]))
        self.assertFalse(value_equal(parse_line(b"[1,2]"), [2, 1]))

    def test_nested(self):
        self.assertTrue(value_equal(
            parse_line(b'{"x":[{"y":null}]}'), {"x": [{"y": None}]}))


if __name__ == "__main__":
    unittest.main()
