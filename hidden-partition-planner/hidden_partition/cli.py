"""终端入口：plan 与 scan 两个操作。

用法：
    python -m hidden_partition.cli plan --manifest M.json --query Q.json \
        --output plan.json
    python -m hidden_partition.cli scan --manifest M.json --query Q.json \
        --output rows.jsonl --report report.json [--projection 2,1]

退出码：0 成功；2 目录/查询/数据/上限/参数错误；3 目标路径已存在拒绝覆盖。
失败时 stderr 输出 JSON 定位，目标文件不会出现（或保持原样）。
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import (
    CatalogError,
    DataError,
    HpqError,
    LimitError,
    QueryShapeError,
    TargetExistsError,
)
from .limits import DEFAULT_LIMITS, Limits
from .planner import run_plan
from .scanner import run_scan


def _add_limit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-line-bytes", type=int,
                        default=DEFAULT_LIMITS.max_line_bytes,
                        help="单个数据行的最大字节数（含换行符）")
    parser.add_argument("--max-expr-depth", type=int,
                        default=DEFAULT_LIMITS.max_expr_depth,
                        help="查询表达式最大嵌套深度（硬上限 256）")
    parser.add_argument("--max-expr-nodes", type=int,
                        default=DEFAULT_LIMITS.max_expr_nodes,
                        help="查询表达式最大节点数")
    parser.add_argument("--max-manifest-bytes", type=int,
                        default=DEFAULT_LIMITS.max_manifest_bytes,
                        help="manifest 文件最大字节数")
    parser.add_argument("--max-query-bytes", type=int,
                        default=DEFAULT_LIMITS.max_query_bytes,
                        help="查询文件最大字节数")


def _limits_from(args: argparse.Namespace) -> Limits:
    limits = Limits(
        max_line_bytes=args.max_line_bytes,
        max_expr_depth=args.max_expr_depth,
        max_expr_nodes=args.max_expr_nodes,
        max_manifest_bytes=args.max_manifest_bytes,
        max_query_bytes=args.max_query_bytes,
    )
    limits.check_positive()
    return limits


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hidden_partition",
        description="离线隐藏分区查询规划器：业务列条件 → 每文件 drop/pass/filter")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="只读 manifest 与查询，输出每文件计划")
    plan.add_argument("--manifest", required=True, help="manifest JSON（只读）")
    plan.add_argument("--query", required=True, help="查询表达式 JSON（只读）")
    plan.add_argument("--output", required=True,
                      help="计划输出路径（已存在则拒绝覆盖）")
    _add_limit_args(plan)

    scan = sub.add_parser("scan", help="执行同一计划，只打开 pass/filter 文件")
    scan.add_argument("--manifest", required=True)
    scan.add_argument("--query", required=True)
    scan.add_argument("--output", required=True,
                      help="结果 JSONL 输出路径（已存在则拒绝覆盖）")
    scan.add_argument("--report", required=True,
                      help="扫描报告输出路径（已存在则拒绝覆盖）")
    scan.add_argument("--projection", default=None,
                      help="有序非重复 field_id 列表，如 2,1；缺省为 schema 顺序")
    _add_limit_args(scan)
    return parser


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_projection(text):
    if text is None:
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    projection = []
    for part in parts:
        try:
            projection.append(int(part))
        except ValueError:
            raise ValueError(f"投影 field_id 不是整数：{part!r}")
    return projection


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limits = _limits_from(args)
        if args.command == "plan":
            plan = run_plan(args.manifest, args.query, args.output, limits)
            _emit(plan)
            return 0
        report = run_scan(args.manifest, args.query, args.output, args.report,
                          projection=_parse_projection(args.projection),
                          limits=limits)
        _emit(report)
        return 0
    except TargetExistsError as exc:
        _emit(exc.to_json())
        return 3
    except (CatalogError, QueryShapeError, DataError, LimitError) as exc:
        print(json.dumps(exc.to_json(), ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, OSError) as exc:
        _emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
        return 2
    except HpqError as exc:  # pragma: no cover - 兜底
        _emit(exc.to_json())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
