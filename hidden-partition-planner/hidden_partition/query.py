"""查询表达式：JSON 表达式树，不接任意 SQL。

语法（全部严格，多余键即拒绝）：

- ``{"op": "and", "children": [e1, e2, ...]}``   至少两个子式
- ``{"op": "or",  "children": [e1, e2, ...]}``   至少两个子式
- ``{"op": "not", "child": e}``                  恰一个子式
- ``{"op": "eq"|"ne"|"lt"|"le"|"gt"|"ge", "field_id": F, "value": V}``
  比较叶，V 为同类型非 null 常量
- ``{"op": "is_null"|"is_not_null", "field_id": F}``

int64 与 string 都可比较；字符串按原 Unicode 码点顺序，不归一化。
未知列、坏类型、空逻辑列表、未支持节点都会被拒绝并给出表达式位置
（``$.children[1].child`` 这类路径）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

from .constraints import INT64_MAX, INT64_MIN, TYPE_INT64, TYPE_STRING
from .errors import Issue, LimitError, QueryShapeError
from .jsutil import load_json_file
from .limits import DEFAULT_LIMITS, Limits
from .transforms import is_int

OP_AND = "and"
OP_OR = "or"
OP_NOT = "not"
OP_EQ = "eq"
OP_NE = "ne"
OP_LT = "lt"
OP_LE = "le"
OP_GT = "gt"
OP_GE = "ge"
OP_IS_NULL = "is_null"
OP_IS_NOT_NULL = "is_not_null"

COMPARISON_OPS = (OP_EQ, OP_NE, OP_LT, OP_LE, OP_GT, OP_GE)
NULL_OPS = (OP_IS_NULL, OP_IS_NOT_NULL)
LOGIC_OPS = (OP_AND, OP_OR, OP_NOT)
LEAF_OPS = COMPARISON_OPS + NULL_OPS
ALL_OPS = LOGIC_OPS + LEAF_OPS

# 深度上限的硬顶：保证解析/求值的递归实现始终远在 Python 递归限制之内。
MAX_DEPTH_CEILING = 256


@dataclass(frozen=True)
class Leaf:
    op: str
    field_id: int
    value: Any = None  # 仅比较叶有值


@dataclass(frozen=True)
class Not:
    child: "Node"


@dataclass(frozen=True)
class And:
    children: Tuple["Node", ...]


@dataclass(frozen=True)
class Or:
    children: Tuple["Node", ...]


Node = Union[Leaf, Not, And, Or]


def _parse_node(obj: Any, loc: str, fields_by_id: dict, depth: int,
                limits: Limits, issues: list, counter: list) -> Optional[Node]:
    counter[0] += 1
    if counter[0] > limits.max_expr_nodes:
        raise LimitError(
            f"表达式节点数超过 {limits.max_expr_nodes} 上限", location=loc)
    if depth > limits.max_expr_depth:
        raise LimitError(
            f"表达式嵌套深度超过 {limits.max_expr_depth} 上限", location=loc)
    if not isinstance(obj, dict):
        issues.append(Issue(loc, f"表达式节点必须是对象，实际：{obj!r}"))
        return None
    op = obj.get("op")
    if op not in ALL_OPS:
        issues.append(Issue(f"{loc}.op",
                            f"未支持的节点 op：{op!r}（支持 {list(ALL_OPS)}）"))
        return None
    if op in LOGIC_OPS:
        allowed = {"op", "children"} if op in (OP_AND, OP_OR) else {"op", "child"}
    elif op in COMPARISON_OPS:
        allowed = {"op", "field_id", "value"}
    else:
        allowed = {"op", "field_id"}
    for key in obj:
        if key not in allowed:
            issues.append(Issue(f"{loc}.{key}", "不允许的键"))

    if op in (OP_AND, OP_OR):
        children_obj = obj.get("children")
        if not isinstance(children_obj, list) or len(children_obj) < 2:
            issues.append(Issue(f"{loc}.children",
                                f"{op} 需要至少两个子式的列表，"
                                f"实际：{children_obj!r}"))
            return None
        children = []
        for index, child_obj in enumerate(children_obj):
            child = _parse_node(child_obj, f"{loc}.children[{index}]",
                                fields_by_id, depth + 1, limits, issues,
                                counter)
            if child is not None:
                children.append(child)
        if len(children) != len(children_obj):
            return None
        return And(tuple(children)) if op == OP_AND else Or(tuple(children))

    if op == OP_NOT:
        if "child" not in obj:
            issues.append(Issue(f"{loc}.child", "not 需要恰一个 child 子式"))
            return None
        child = _parse_node(obj["child"], f"{loc}.child", fields_by_id,
                            depth + 1, limits, issues, counter)
        return Not(child) if child is not None else None

    # 叶子
    field_id = obj.get("field_id")
    field = None
    if not is_int(field_id) or field_id < 1:
        issues.append(Issue(f"{loc}.field_id",
                            f"field_id 必须是正整数，实际：{field_id!r}"))
    elif field_id not in fields_by_id:
        issues.append(Issue(f"{loc}.field_id",
                            f"未知列 field_id：{field_id}"))
    else:
        field = fields_by_id[field_id]
    if op in NULL_OPS:
        if field is None:
            return None
        return Leaf(op, field_id)
    # 比较叶：常量必须同类型且非 null
    if "value" not in obj:
        issues.append(Issue(loc, f"比较叶 {op} 缺少 value 常量"))
        return None
    value = obj["value"]
    if field is None:
        return None
    if value is None:
        issues.append(Issue(f"{loc}.value",
                            "比较叶常量必须非 null（查 null 用 is_null）"))
        return None
    if field.type == TYPE_INT64:
        if not is_int(value) or not (INT64_MIN <= value <= INT64_MAX):
            issues.append(Issue(f"{loc}.value",
                                f"列 {field.name} 是 int64，常量必须是"
                                f" int64 整数（bool 不算），实际：{value!r}"))
            return None
    elif field.type == TYPE_STRING:
        if not isinstance(value, str):
            issues.append(Issue(f"{loc}.value",
                                f"列 {field.name} 是 string，常量必须是字符串，"
                                f"实际：{value!r}"))
            return None
    return Leaf(op, field_id, value)


def parse_query(obj: Any, fields_by_id: dict,
                limits: Limits = DEFAULT_LIMITS) -> Node:
    """校验并组装查询表达式树；问题一次性收集后抛出 QueryShapeError。"""
    limits.check_positive()
    if limits.max_expr_depth > MAX_DEPTH_CEILING:
        raise LimitError(
            f"max_expr_depth 硬上限是 {MAX_DEPTH_CEILING}（保证递归求值安全）")
    issues: list = []
    node = _parse_node(obj, "$", fields_by_id, 1, limits, issues, [0])
    if issues:
        raise QueryShapeError(issues)
    assert node is not None
    return node


def load_query(path: str, fields_by_id: dict,
               limits: Limits = DEFAULT_LIMITS) -> Node:
    """从磁盘读取并校验查询表达式（只读）。"""
    limits.check_positive()
    obj = load_json_file(path, limits.max_query_bytes, what="查询表达式")
    return parse_query(obj, fields_by_id, limits)


def to_json(node: Node) -> dict:
    """把表达式树序列化回合同 JSON（用于计划里的剩余条件）。"""
    if isinstance(node, Leaf):
        if node.op in COMPARISON_OPS:
            return {"op": node.op, "field_id": node.field_id,
                    "value": node.value}
        return {"op": node.op, "field_id": node.field_id}
    if isinstance(node, Not):
        return {"op": OP_NOT, "child": to_json(node.child)}
    if isinstance(node, And):
        return {"op": OP_AND,
                "children": [to_json(c) for c in node.children]}
    return {"op": OP_OR, "children": [to_json(c) for c in node.children]}
