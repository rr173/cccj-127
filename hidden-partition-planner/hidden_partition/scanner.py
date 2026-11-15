"""scan：实际执行 plan 给出的同一计划。

- 只打开 pass / filter 文件，逐行处理；drop 文件即便路径不存在或内容
  损坏也绝不触碰；零行文件（规划为 drop）同样不打开；
- 数据是 UTF-8 JSONL，每行一个对象，以十进制 field_id 字符串为键，
  列齐全、值符合 schema；重复键、非法 UTF-8、坏字段、行数与目录不符
  都是失败（非零退出），不交付半份结果；
- 投影是有序、非重复的逻辑 field_id 列表；输出对象以当前 schema 的
  name 为键、按投影顺序；过滤需要但未投影的列参与判断但不混入输出；
- 保持文件顺序、文件内行序、重复行与 null；不因分区命中凭空生成行；
- 输出与报告都走临时文件 + 原子落位：失败时两者都不会出现，
  已存在的同名文件字节不动。
"""
from __future__ import annotations

import os
import resource
import time
from typing import Any, Dict, Iterator, List, Optional

from .analyze import DECISION_DROP, DECISION_FILTER, DECISION_PASS
from .constraints import INT64_MAX, INT64_MIN, TYPE_INT64
from .contract import Manifest, load_manifest
from .errors import DataError
from .evaluator import row_matches
from .jsutil import dumps_canonical, loads_strict, write_text_atomic
from .limits import DEFAULT_LIMITS, Limits
from .planner import build_plan
from .query import load_query, parse_query


def _peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _check_value(field, value: Any) -> bool:
    if value is None:
        return field.nullable
    if field.type == TYPE_INT64:
        return (isinstance(value, int) and not isinstance(value, bool)
                and INT64_MIN <= value <= INT64_MAX)
    return isinstance(value, str)


def iter_rows(path: str, manifest: Manifest, limits: Limits, *,
              file_id: str) -> Iterator[Dict[int, Any]]:
    """逐行读取并校验一个数据文件；只保留当前行，不做全量收集。"""
    expected_keys = {str(f.field_id) for f in manifest.fields}
    try:
        fh = open(path, "rb")
    except OSError as exc:
        raise DataError("数据文件无法打开", file_id=file_id, path=path,
                        detail=str(exc)) from exc
    with fh:
        for lineno, raw in enumerate(fh, 1):
            if len(raw) > limits.max_line_bytes:
                raise DataError(
                    f"单行超过 {limits.max_line_bytes} 字节上限",
                    file_id=file_id, path=path, line=lineno)
            line = raw[:-1] if raw.endswith(b"\n") else raw
            try:
                text = line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise DataError("非法 UTF-8 行", file_id=file_id, path=path,
                                line=lineno, detail=str(exc)) from exc
            try:
                obj = loads_strict(text)
            except ValueError as exc:
                raise DataError("行不是合法 JSON（或含重复键/非标准常量）",
                                file_id=file_id, path=path, line=lineno,
                                detail=str(exc)) from exc
            if not isinstance(obj, dict):
                raise DataError("行不是 JSON 对象", file_id=file_id,
                                path=path, line=lineno)
            keys = set(obj.keys())
            if keys != expected_keys:
                missing = sorted(expected_keys - keys)
                extra = sorted(keys - expected_keys)
                raise DataError("列不齐全或含多余列", file_id=file_id,
                                path=path, line=lineno,
                                detail=f"缺少={missing} 多余={extra}")
            row: Dict[int, Any] = {}
            for field in manifest.fields:
                value = obj[str(field.field_id)]
                if not _check_value(field, value):
                    raise DataError("字段值不符合 schema", file_id=file_id,
                                    path=path, line=lineno,
                                    detail=f"列 {field.name}"
                                           f"(field_id={field.field_id}) "
                                           f"类型 {field.type} "
                                           f"nullable={field.nullable}，"
                                           f"实际值：{value!r}")
                row[field.field_id] = value
            yield row


def resolve_projection(projection: Optional[List[int]],
                       manifest: Manifest) -> List[int]:
    """投影：有序、非重复的逻辑 field_id 列表；缺省为 schema 声明顺序。"""
    if projection is None:
        return [f.field_id for f in manifest.fields]
    if not projection:
        raise ValueError("投影列表不能为空")
    seen: set = set()
    for fid in projection:
        if not isinstance(fid, int) or isinstance(fid, bool):
            raise ValueError(f"投影 field_id 必须是正整数：{fid!r}")
        if fid not in manifest.fields_by_id:
            raise ValueError(f"投影引用了未知列 field_id：{fid}")
        if fid in seen:
            raise ValueError(f"投影 field_id 重复：{fid}")
        seen.add(fid)
    return list(projection)


def _preflight_target(path: str, what: str) -> None:
    from .errors import TargetExistsError
    if os.path.exists(path):
        raise TargetExistsError(path)
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise ValueError(f"{what}的父目录不存在：{parent}")


def run_scan(manifest_path: str, query_path: str, output_path: str,
             report_path: str, projection: Optional[List[int]] = None,
             limits: Limits = DEFAULT_LIMITS) -> dict:
    """scan 终端操作：规划（纯内存）→ 只打开 pass/filter 文件逐行交付。"""
    started = time.perf_counter()
    limits.check_positive()
    if os.path.abspath(output_path) == os.path.abspath(report_path):
        raise ValueError("输出路径与报告路径不能相同")
    manifest = load_manifest(manifest_path, limits)
    query = load_query(query_path, manifest.fields_by_id, limits)
    proj_ids = resolve_projection(projection, manifest)
    plan = build_plan(manifest, query, manifest_path=manifest_path)
    # 先规划完再检查目标，保证目录级错误先于任何输出报出。
    _preflight_target(output_path, "输出路径")
    _preflight_target(report_path, "报告路径")

    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    proj_names = [manifest.fields_by_id[fid].name for fid in proj_ids]
    projection_json = [
        {"field_id": fid,
         "name": manifest.fields_by_id[fid].name,
         "type": manifest.fields_by_id[fid].type}
        for fid in proj_ids
    ]

    file_reports: List[dict] = []
    rows_read_total = 0
    rows_delivered_total = 0

    def _write_output(fh) -> None:
        nonlocal rows_read_total, rows_delivered_total
        for file_plan, entry in zip(plan["files"], manifest.files):
            decision = file_plan["decision"]
            record = {"file_id": entry.file_id, "decision": decision,
                      "opened": False, "rows_read": 0, "rows_delivered": 0}
            file_reports.append(record)
            if decision == DECISION_DROP:
                continue  # drop 文件绝不触碰
            assert decision in (DECISION_PASS, DECISION_FILTER)
            residual = None
            if decision == DECISION_FILTER:
                residual = parse_query(file_plan["residual"],
                                       manifest.fields_by_id, limits)
            full_path = os.path.join(base_dir, entry.path)
            record["opened"] = True
            count = 0
            for row in iter_rows(full_path, manifest, limits,
                                 file_id=entry.file_id):
                count += 1
                if decision == DECISION_PASS or row_matches(residual, row):
                    out = {name: row[fid]
                           for name, fid in zip(proj_names, proj_ids)}
                    fh.write(dumps_canonical(out) + "\n")
                    record["rows_delivered"] += 1
            record["rows_read"] = count
            if count != entry.row_count:
                raise DataError("实际行数与目录 row_count 不符",
                                file_id=entry.file_id, path=full_path,
                                detail=f"目录声明 {entry.row_count}，"
                                       f"实际 {count}")
            rows_read_total += count
            rows_delivered_total += record["rows_delivered"]

    write_text_atomic(output_path, _write_output)

    report = {
        "ok": True,
        "tool": "hidden-partition-planner",
        "manifest": manifest_path,
        "query": plan["query"],
        "output": output_path,
        "projection": projection_json,
        "files": file_reports,
        "summary": {
            "files_total": len(file_reports),
            "files_opened": sum(1 for r in file_reports if r["opened"]),
            "drop": plan["summary"][DECISION_DROP],
            "pass": plan["summary"][DECISION_PASS],
            "filter": plan["summary"][DECISION_FILTER],
            "rows_read": rows_read_total,
            "rows_delivered": rows_delivered_total,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "peak_rss_bytes": _peak_rss_bytes(),
    }
    write_text_atomic(report_path,
                      lambda fh: fh.write(dumps_canonical(report) + "\n"))
    return report
