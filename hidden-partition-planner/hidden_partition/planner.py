"""查询规划：只读 manifest 与查询，推导每个文件的 drop / pass / filter。

规划阶段绝不 stat 或打开任何数据路径，也绝不借用另一个 spec（或“最新”
spec）去解释旧文件：每个文件只用它自己声明的 spec_id 对应的分区规则。
目录级错误（manifest 或查询违反合同）在输出任何计划之前报出。
"""
from __future__ import annotations

import resource
import time
from typing import Any, Dict, List, Optional

from .analyze import (
    DECISION_DROP,
    DECISION_FILTER,
    DECISION_PASS,
    Analysis,
    analyze,
    decide,
    simplify,
)
from .constraints import (
    ColumnConstraint,
    constraint_to_json,
    render_constraint,
    render_interval,
)
from .contract import Manifest, load_manifest
from .evaluator import row_matches  # noqa: F401  （公开行求值器入口之一）
from .jsutil import dumps_canonical, write_text_atomic
from .limits import DEFAULT_LIMITS, Limits
from .query import Node, load_query, to_json
from .transforms import compute_file_constraints, preimage


def _peak_rss_bytes() -> int:
    # Linux 下 ru_maxrss 单位为 KiB。
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _transform_text(transform) -> str:
    return transform.describe()


def _preimage_text(transform, value, field) -> str:
    piece = preimage(transform, value, source_type=field.type,
                     source_nullable=field.nullable)
    return render_constraint(piece)


def _file_constraints_json(manifest: Manifest, entry,
                           constraints: Dict[int, ColumnConstraint]) -> list:
    spec = manifest.spec_of(entry)
    by_source: Dict[int, list] = {}
    for pf in spec.partition_fields:
        by_source.setdefault(pf.source_id, []).append(pf)
    out = []
    for field in manifest.fields:
        constraint = constraints[field.field_id]
        item = {
            "field_id": field.field_id,
            "name": field.name,
            "type": field.type,
            **constraint_to_json(constraint),
        }
        pieces = []
        for pf in by_source.get(field.field_id, []):
            value = entry.partitions[pf.partition_id]
            pieces.append({
                "partition_id": pf.partition_id,
                "name": pf.name,
                "transform": _transform_text(pf.transform),
                "value": value,
                "preimage": _preimage_text(pf.transform, value, field),
            })
        if pieces:
            item["preimages"] = pieces
        out.append(item)
    return out


def _constraint_reasons(manifest: Manifest, entry,
                        constraints: Dict[int, ColumnConstraint]) -> List[str]:
    spec = manifest.spec_of(entry)
    by_source: Dict[int, list] = {}
    for pf in spec.partition_fields:
        by_source.setdefault(pf.source_id, []).append(pf)
    reasons: List[str] = []
    for field in manifest.fields:
        pfs = by_source.get(field.field_id, [])
        head = f"列 {field.name}(field_id={field.field_id})"
        if not pfs:
            reasons.append(f"{head}：spec {spec.spec_id!r} 未对其分区 → "
                           f"无约束（{render_constraint(constraints[field.field_id])}）")
            continue
        parts = []
        for pf in pfs:
            value = entry.partitions[pf.partition_id]
            text = "null" if value is None else dumps_canonical(value)
            parts.append(
                f"分区字段 {pf.name!r}(partition_id={pf.partition_id}) "
                f"{_transform_text(pf.transform)} 值 {text} → 原像 "
                f"{_preimage_text(pf.transform, value, field)}")
        joined = "；".join(parts)
        tail = "交集" if len(pfs) > 1 else "约束"
        reasons.append(f"{head}：{joined}；{tail} = "
                       f"{render_constraint(constraints[field.field_id])}")
    return reasons


def _leaf_reasons(analysis: Analysis, manifest: Manifest,
                  counter: List[int], reasons: List[str]) -> None:
    from .query import Leaf
    node = analysis.node
    if isinstance(node, Leaf):
        counter[0] += 1
        field = manifest.fields_by_id[node.field_id]
        if node.value is not None:
            cond = f"{field.name} {node.op} {dumps_canonical(node.value)}"
        else:
            cond = f"{field.name} {node.op}"
        outcomes = "、".join(sorted(analysis.outcomes))
        reasons.append(f"叶 #{counter[0]}（{cond}）→ 该文件可能真值 {{{outcomes}}}")
        return
    for child in analysis.children:
        _leaf_reasons(child, manifest, counter, reasons)


def _root_reason(decision: str, outcomes) -> str:
    outcomes_text = "、".join(sorted(outcomes))
    if decision == DECISION_PASS:
        return (f"根节点可能真值 {{{outcomes_text}}}：每一条合法源行都让条件"
                f"为 TRUE → pass（直接交付，无需逐行判断）")
    if decision == DECISION_DROP:
        return (f"根节点可能真值 {{{outcomes_text}}}：不存在能让条件为 TRUE"
                f" 的合法源行 → drop（不打开该文件）")
    return (f"根节点可能真值 {{{outcomes_text}}}：既有行可 TRUE 也有行不然"
            f" → filter（需逐行执行剩余条件）")


def plan_file(manifest: Manifest, entry, query: Node) -> dict:
    """为单个文件生成计划条目（纯函数，不触碰磁盘）。"""
    spec = manifest.spec_of(entry)
    base = {
        "file_id": entry.file_id,
        "path": entry.path,
        "spec_id": entry.spec_id,
        "row_count": entry.row_count,
    }
    if entry.row_count == 0:
        return {
            **base,
            "decision": DECISION_DROP,
            "outcomes": [],
            "constraints": _file_constraints_json(
                manifest, entry,
                compute_file_constraints(manifest.fields_by_id,
                                         spec.partition_fields,
                                         entry.partitions)),
            "reasons": ["row_count=0：零行文件结构已验证，直接跳过（不打开）"],
            "residual": None,
        }
    constraints = compute_file_constraints(
        manifest.fields_by_id, spec.partition_fields, entry.partitions)
    analysis = analyze(query, manifest.fields_by_id, constraints)
    decision = decide(analysis.outcomes)
    reasons = _constraint_reasons(manifest, entry, constraints)
    _leaf_reasons(analysis, manifest, [0], reasons)
    reasons.append(_root_reason(decision, analysis.outcomes))
    residual = None
    if decision == DECISION_FILTER:
        residual = to_json(simplify(analysis))
    return {
        **base,
        "decision": decision,
        "outcomes": sorted(analysis.outcomes),
        "constraints": _file_constraints_json(manifest, entry, constraints),
        "reasons": reasons,
        "residual": residual,
    }


def build_plan(manifest: Manifest, query: Node, *,
               manifest_path: Optional[str] = None,
               query_source: Optional[Any] = None) -> dict:
    """按 manifest 文件顺序为每个文件给出结论（纯内存计算）。"""
    files = [plan_file(manifest, entry, query) for entry in manifest.files]
    summary = {
        "files_total": len(files),
        DECISION_DROP: sum(1 for f in files if f["decision"] == DECISION_DROP),
        DECISION_PASS: sum(1 for f in files if f["decision"] == DECISION_PASS),
        DECISION_FILTER: sum(1 for f in files if f["decision"] == DECISION_FILTER),
    }
    return {
        "ok": True,
        "tool": "hidden-partition-planner",
        "manifest": manifest_path,
        "query": query_source if query_source is not None else to_json(query),
        "files": files,
        "summary": summary,
    }


def run_plan(manifest_path: str, query_path: str, output_path: str,
             limits: Limits = DEFAULT_LIMITS) -> dict:
    """plan 终端操作：载入目录与查询 → 规划 → 原子写出计划文件。"""
    started = time.perf_counter()
    limits.check_positive()
    manifest = load_manifest(manifest_path, limits)
    query = load_query(query_path, manifest.fields_by_id, limits)
    plan = build_plan(manifest, query, manifest_path=manifest_path)
    plan["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    plan["peak_rss_bytes"] = _peak_rss_bytes()
    text = dumps_canonical(plan) + "\n"
    write_text_atomic(output_path, lambda fh: fh.write(text))
    return plan
