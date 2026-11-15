"""公开行求值器：对一行求表达式的三值结果。

合同（与规划器 analyze 完全一致）：
- 普通比较（eq/ne/lt/le/gt/ge）遇到 null 得 UNKNOWN；
- is_null / is_not_null 总为 TRUE 或 FALSE；
- not UNKNOWN 仍是 UNKNOWN；
- and：任一 FALSE 则 FALSE，全部 TRUE 才 TRUE，其余 UNKNOWN；
- or：任一 TRUE 则 TRUE，全部 FALSE 才 FALSE，其余 UNKNOWN；
- 只有最终 TRUE 的行能交付（UNKNOWN 与 FALSE 一样不交付，
  但把 null 当 FALSE 再做 not 会错误保留空值，这里严格区分）。

字符串按 Python str 比较，即原 Unicode 码点顺序，不归一化。
行以 ``{field_id: value}`` 字典给出，列齐全、值符合 schema（scan 已校验）。
"""
from __future__ import annotations

from typing import Any, Dict

from .analyze import FALSE, TRUE, UNKNOWN
from .query import (
    OP_EQ,
    OP_GE,
    OP_GT,
    OP_IS_NOT_NULL,
    OP_IS_NULL,
    OP_LE,
    OP_LT,
    OP_NE,
    And,
    Leaf,
    Node,
    Not,
    Or,
)

_NEGATE = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}


def _compare(op: str, value: Any, constant: Any) -> str:
    if op == OP_EQ:
        return TRUE if value == constant else FALSE
    if op == OP_NE:
        return TRUE if value != constant else FALSE
    if op == OP_LT:
        return TRUE if value < constant else FALSE
    if op == OP_LE:
        return TRUE if value <= constant else FALSE
    if op == OP_GT:
        return TRUE if value > constant else FALSE
    if op == OP_GE:
        return TRUE if value >= constant else FALSE
    raise ValueError(f"未知比较算子：{op}")  # pragma: no cover


def evaluate(node: Node, row: Dict[int, Any]) -> str:
    """返回 TRUE / FALSE / UNKNOWN。"""
    if isinstance(node, Leaf):
        value = row[node.field_id]
        if node.op == OP_IS_NULL:
            return TRUE if value is None else FALSE
        if node.op == OP_IS_NOT_NULL:
            return FALSE if value is None else TRUE
        if value is None:
            return UNKNOWN
        return _compare(node.op, value, node.value)
    if isinstance(node, Not):
        return _NEGATE[evaluate(node.child, row)]
    if isinstance(node, And):
        saw_unknown = False
        for child in node.children:
            outcome = evaluate(child, row)
            if outcome == FALSE:
                return FALSE
            if outcome == UNKNOWN:
                saw_unknown = True
        return UNKNOWN if saw_unknown else TRUE
    # Or
    saw_unknown = False
    for child in node.children:
        outcome = evaluate(child, row)
        if outcome == TRUE:
            return TRUE
        if outcome == UNKNOWN:
            saw_unknown = True
    return UNKNOWN if saw_unknown else FALSE


def row_matches(node: Node, row: Dict[int, Any]) -> bool:
    """只有最终 TRUE 的行能交付。"""
    return evaluate(node, row) == TRUE
