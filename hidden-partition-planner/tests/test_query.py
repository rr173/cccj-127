"""查询表达式合同：节点形状、叶子常量、未知列、位置报告、深度上限。"""
from __future__ import annotations

import unittest

from hidden_partition import LimitError, QueryShapeError, parse_query
from hidden_partition.constraints import INT64_MAX, INT64_MIN
from hidden_partition.contract import Field

FIELDS = {
    1: Field(1, "account", "int64", True),
    2: Field(2, "region", "string", True),
    3: Field(3, "seq", "int64", False),
}


def expect_error(obj, needle: str) -> QueryShapeError:
    try:
        parse_query(obj, FIELDS)
    except QueryShapeError as exc:
        text = str(exc)
        assert needle in text, f"期望错误包含 {needle!r}，实际：{text}"
        return exc
    raise AssertionError(f"期望 QueryShapeError（{needle!r}），但通过了校验")


class TestLeaves(unittest.TestCase):
    def test_all_leaf_ops_accepted(self) -> None:
        for op in ("eq", "ne", "lt", "le", "gt", "ge"):
            parse_query({"op": op, "field_id": 1, "value": -3}, FIELDS)
            parse_query({"op": op, "field_id": 2, "value": "京𠮷"}, FIELDS)
        for op in ("is_null", "is_not_null"):
            parse_query({"op": op, "field_id": 1}, FIELDS)

    def test_unknown_op(self) -> None:
        expect_error({"op": "between", "field_id": 1}, "未支持的节点")

    def test_unknown_field(self) -> None:
        exc = expect_error({"op": "eq", "field_id": 99, "value": 1}, "未知列")
        self.assertIn("$.field_id", str(exc))

    def test_field_id_must_be_positive_int(self) -> None:
        expect_error({"op": "eq", "field_id": True, "value": 1}, "正整数")
        expect_error({"op": "eq", "field_id": "1", "value": 1}, "正整数")

    def test_comparison_constant_must_match_type(self) -> None:
        expect_error({"op": "eq", "field_id": 1, "value": "京"}, "int64")
        expect_error({"op": "eq", "field_id": 1, "value": True}, "bool")
        expect_error({"op": "eq", "field_id": 1, "value": 1.5}, "int64")
        expect_error({"op": "eq", "field_id": 2, "value": 3}, "string")

    def test_comparison_constant_must_be_non_null(self) -> None:
        expect_error({"op": "eq", "field_id": 1, "value": None}, "非 null")

    def test_int64_boundaries(self) -> None:
        parse_query({"op": "eq", "field_id": 1, "value": INT64_MIN}, FIELDS)
        parse_query({"op": "eq", "field_id": 1, "value": INT64_MAX}, FIELDS)
        expect_error({"op": "eq", "field_id": 1, "value": INT64_MIN - 1},
                     "int64")
        expect_error({"op": "eq", "field_id": 1, "value": INT64_MAX + 1},
                     "int64")

    def test_null_leaf_rejects_value_key(self) -> None:
        expect_error({"op": "is_null", "field_id": 1, "value": 1},
                     "不允许的键")

    def test_comparison_leaf_requires_value(self) -> None:
        expect_error({"op": "lt", "field_id": 1}, "缺少 value")


class TestLogicNodes(unittest.TestCase):
    def test_and_or_need_at_least_two_children(self) -> None:
        leaf = {"op": "eq", "field_id": 1, "value": 1}
        expect_error({"op": "and", "children": [leaf]}, "至少两个子式")
        expect_error({"op": "or", "children": []}, "至少两个子式")
        expect_error({"op": "and", "children": "x"}, "至少两个子式")
        parse_query({"op": "and", "children": [leaf, leaf]}, FIELDS)
        parse_query({"op": "or", "children": [leaf, leaf, leaf]}, FIELDS)

    def test_not_needs_exactly_one_child(self) -> None:
        leaf = {"op": "eq", "field_id": 1, "value": 1}
        expect_error({"op": "not"}, "恰一个")
        expect_error({"op": "not", "child": leaf, "children": [leaf]},
                     "不允许的键")
        parse_query({"op": "not", "child": leaf}, FIELDS)

    def test_nested_positions_reported(self) -> None:
        bad = {"op": "and", "children": [
            {"op": "eq", "field_id": 1, "value": 1},
            {"op": "not", "child": {"op": "eq", "field_id": 2, "value": 5}},
        ]}
        exc = expect_error(bad, "string")
        self.assertIn("$.children[1].child.value", str(exc))

    def test_node_must_be_object(self) -> None:
        expect_error({"op": "and", "children": [1, 2]}, "必须是对象")

    def test_deep_nesting_limit(self) -> None:
        from hidden_partition import Limits
        node = {"op": "eq", "field_id": 1, "value": 1}
        for _ in range(10):
            node = {"op": "not", "child": node}
        limits = Limits(max_expr_depth=8)
        try:
            parse_query(node, FIELDS, limits)
        except LimitError as exc:
            self.assertIn("深度", str(exc))
        else:
            raise AssertionError("期望 LimitError")
        parse_query(node, FIELDS, Limits(max_expr_depth=16))

    def test_node_count_limit(self) -> None:
        from hidden_partition import Limits
        leaf = {"op": "eq", "field_id": 1, "value": 1}
        node = {"op": "or", "children": [dict(leaf) for _ in range(20)]}
        try:
            parse_query(node, FIELDS, Limits(max_expr_nodes=10))
        except LimitError as exc:
            self.assertIn("节点数", str(exc))
        else:
            raise AssertionError("期望 LimitError")


if __name__ == "__main__":
    unittest.main()
