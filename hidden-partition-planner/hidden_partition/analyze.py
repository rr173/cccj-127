"""按文件推导查询的三值可能真值，给出 drop / pass / filter 结论。

三值逻辑（与公开行求值器同一合同）：
- 普通比较遇到 null 得 UNKNOWN；is_null / is_not_null 总为 TRUE 或 FALSE；
- not UNKNOWN 仍是 UNKNOWN；
- and：任一 FALSE 则 FALSE，全部 TRUE 才 TRUE，其余 UNKNOWN；
- or：任一 TRUE 则 TRUE，全部 FALSE 才 FALSE，其余 UNKNOWN；
- 只有最终 TRUE 的行能交付。

每个叶子在“该文件该列的取值约束”上的可能真值是精确计算的：
比较叶把列约束区间与叶子的真值区间求交/求差；null 叶看 null 是否可能。
同一列的比较与空值叶子组成的纯 and 会合并且精确求交（例如
``x >= -3 and x < -3`` 的真值区间交集为空）；跨列、跨 or 的组合按独立
假设保守合并——只会多报可能真值（多保留文件），绝不会漏掉本可 TRUE
的行，也不会把不能证明匹配的文件误判成 pass。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from .constraints import (
    INT64_MAX,
    INT64_MIN,
    TYPE_INT64,
    ColumnConstraint,
)
from .intervals import intersect, subtract
from .query import (
    COMPARISON_OPS,
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

TRUE = "TRUE"
FALSE = "FALSE"
UNKNOWN = "UNKNOWN"

DECISION_DROP = "drop"
DECISION_PASS = "pass"
DECISION_FILTER = "filter"

_NEGATE = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}


@dataclass(frozen=True)
class Analysis:
    """一个表达式节点在某文件约束下的分析结果。"""

    node: Node
    outcomes: FrozenSet[str]
    children: Tuple["Analysis", ...] = ()


def leaf_true_region(leaf: Leaf, column_type: str):
    """叶子在非 null 值上的真值区间列表（半开区间，hi=None 无界）。"""
    if leaf.op == OP_IS_NULL:
        return []
    if leaf.op == OP_IS_NOT_NULL:
        return _universe_region(column_type)
    c = leaf.value
    if column_type == TYPE_INT64:
        lo_all, hi_all = INT64_MIN, INT64_MAX + 1
        if leaf.op == OP_EQ:
            raw = [(c, c + 1)]
        elif leaf.op == OP_NE:
            raw = [(lo_all, c), (c + 1, hi_all)]
        elif leaf.op == OP_LT:
            raw = [(lo_all, c)]
        elif leaf.op == OP_LE:
            raw = [(lo_all, c + 1)]
        elif leaf.op == OP_GT:
            raw = [(c + 1, hi_all)]
        else:  # OP_GE
            raw = [(c, hi_all)]
    else:
        after = c + "\x00"  # 大于 c 的最小字符串
        if leaf.op == OP_EQ:
            raw = [(c, after)]
        elif leaf.op == OP_NE:
            raw = [("", c), (after, None)]
        elif leaf.op == OP_LT:
            raw = [("", c)]
        elif leaf.op == OP_LE:
            raw = [("", after)]
        elif leaf.op == OP_GT:
            raw = [(after, None)]
        else:  # OP_GE
            raw = [(c, None)]
    return [(lo, hi) for lo, hi in raw if hi is None or lo < hi]


def _universe_region(column_type: str):
    if column_type == TYPE_INT64:
        return [(INT64_MIN, INT64_MAX + 1)]
    return [("", None)]


def _leaf_at_null(leaf: Leaf) -> str:
    if leaf.op == OP_IS_NULL:
        return TRUE
    if leaf.op == OP_IS_NOT_NULL:
        return FALSE
    return UNKNOWN  # 普通比较对 null 得 UNKNOWN


def leaf_outcomes(leaf: Leaf, column_type: str,
                  constraint: ColumnConstraint) -> FrozenSet[str]:
    """单个叶子在列约束上的精确可能真值集合。"""
    out = set()
    region = leaf_true_region(leaf, column_type)
    s = constraint.interval_list()
    if leaf.op in COMPARISON_OPS:
        if constraint.null_allowed:
            out.add(UNKNOWN)
        if s:
            if intersect(s, region):
                out.add(TRUE)
            if subtract(s, region):
                out.add(FALSE)
    elif leaf.op == OP_IS_NULL:
        if constraint.null_allowed:
            out.add(TRUE)
        if s:
            out.add(FALSE)
    else:  # is_not_null
        if constraint.null_allowed:
            out.add(FALSE)
        if s:
            out.add(TRUE)
    return frozenset(out)


def _group_outcomes(leaves: List[Leaf], column_type: str,
                    constraint: ColumnConstraint) -> FrozenSet[str]:
    """同一列的叶子纯 and：把各叶真值区间与列约束一起精确求交。"""
    out = set()
    s = constraint.interval_list()
    region = leaf_true_region(leaves[0], column_type)
    for leaf in leaves[1:]:
        region = intersect(region, leaf_true_region(leaf, column_type))
    if s:
        if intersect(s, region):
            out.add(TRUE)
        if subtract(s, region):
            out.add(FALSE)
    if constraint.null_allowed:
        null_values = [_leaf_at_null(leaf) for leaf in leaves]
        if FALSE in null_values:
            out.add(FALSE)
        elif UNKNOWN in null_values:
            out.add(UNKNOWN)
        else:
            out.add(TRUE)
    return frozenset(out)


def _and_combine(parts: List[FrozenSet[str]]) -> FrozenSet[str]:
    out = set()
    if all(TRUE in p for p in parts):
        out.add(TRUE)
    if any(FALSE in p for p in parts):
        out.add(FALSE)
    if all(TRUE in p or UNKNOWN in p for p in parts) and \
            any(UNKNOWN in p for p in parts):
        out.add(UNKNOWN)
    return frozenset(out)


def _or_combine(parts: List[FrozenSet[str]]) -> FrozenSet[str]:
    out = set()
    if any(TRUE in p for p in parts):
        out.add(TRUE)
    if all(FALSE in p for p in parts):
        out.add(FALSE)
    if all(FALSE in p or UNKNOWN in p for p in parts) and \
            any(UNKNOWN in p for p in parts):
        out.add(UNKNOWN)
    return frozenset(out)


def _flatten_and(node: Node) -> List[Node]:
    """and 满足结合律，展开嵌套的 and 便于识别同列纯 and。"""
    out: List[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, And):
            stack.extend(current.children)
        else:
            out.append(current)
    return out


def analyze(node: Node, fields_by_id: Dict,
            constraints: Dict[int, ColumnConstraint]) -> Analysis:
    """计算节点在文件约束下的可能真值集合（保守方向只会多报）。"""
    if isinstance(node, Leaf):
        field = fields_by_id[node.field_id]
        return Analysis(node, leaf_outcomes(
            node, field.type, constraints[node.field_id]))
    if isinstance(node, Not):
        child = analyze(node.child, fields_by_id, constraints)
        return Analysis(node, frozenset(_NEGATE[o] for o in child.outcomes),
                        (child,))
    if isinstance(node, And):
        conjuncts = _flatten_and(node)
        leaf_groups: Dict[int, List[Leaf]] = {}
        others: List[Node] = []
        for conjunct in conjuncts:
            if isinstance(conjunct, Leaf):
                leaf_groups.setdefault(conjunct.field_id, []).append(conjunct)
            else:
                others.append(conjunct)
        parts: List[FrozenSet[str]] = []
        child_analyses: List[Analysis] = []
        for field_id, leaves in leaf_groups.items():
            field = fields_by_id[field_id]
            outcomes = _group_outcomes(leaves, field.type,
                                       constraints[field_id])
            parts.append(outcomes)
            for leaf in leaves:
                # 叶级分析仍记录，供计划理由逐叶核对
                child_analyses.append(Analysis(
                    leaf, leaf_outcomes(leaf, field.type,
                                        constraints[field_id])))
        for other in others:
            sub = analyze(other, fields_by_id, constraints)
            parts.append(sub.outcomes)
            child_analyses.append(sub)
        return Analysis(node, _and_combine(parts), tuple(child_analyses))
    # Or：跨 or 关联保守合并
    child_analyses = tuple(analyze(c, fields_by_id, constraints)
                           for c in node.children)
    return Analysis(node, _or_combine([c.outcomes for c in child_analyses]),
                    child_analyses)


def decide(outcomes: FrozenSet[str]) -> str:
    """根节点可能真值 → 文件结论。"""
    if outcomes == frozenset({TRUE}):
        return DECISION_PASS
    if TRUE not in outcomes:
        return DECISION_DROP
    return DECISION_FILTER


def simplify(analysis: Analysis) -> Node:
    """生成剩余条件：去掉对本文件恒 TRUE 的 and 子式、恒 FALSE 的 or 子式。

    只利用“该文件所有合法行上恒为某值”的事实做等价改写，不追求最短；
    对该文件所有可能源行，剩余条件与原式是否最终 TRUE 完全等价。
    """
    node = analysis.node
    if isinstance(node, Leaf):
        return node
    if isinstance(node, Not):
        return Not(simplify(analysis.children[0]))
    if isinstance(node, And):
        kept = [simplify(c) for c in analysis.children
                if c.outcomes != frozenset({TRUE})]
        if not kept:
            return node  # 恒 TRUE 的 and 只会被父式吸收；兜底保留原式
        if len(kept) == 1:
            return kept[0]
        return And(tuple(kept))
    kept = [simplify(c) for c in analysis.children
            if c.outcomes != frozenset({FALSE})]
    if not kept:
        return node
    if len(kept) == 1:
        return kept[0]
    return Or(tuple(kept))
