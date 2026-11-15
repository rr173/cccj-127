"""``handoff-load`` 终端命令。

示例::

    handoff-load --dsn "host=/tmp port=55432 dbname=handoff_demo user=handoff" \\
        --contract demo/contract.json \\
        --files organization=demo/data/handoff/organization.jsonl \\
        --files member=demo/data/handoff/member-1.jsonl \\
        --files member=demo/data/handoff/member-2.jsonl \\
        --files assignment=demo/data/handoff/assignment.jsonl

成功时退出码 0 并把逐表四类计数以 JSON 打到 stdout；任何失败退出码 1，
诊断（含逻辑表/文件/物理行/字段）打到 stderr，数据库改动整体回滚。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import psycopg

from .contract import load_contract
from .errors import HandoffError
from .loader import load_relation_handoff
from .parser import DEFAULT_BATCH_SIZE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handoff-load",
        description="把一次多表整表交接（UTF-8 JSONL）原子写进已有 PostgreSQL 仓库",
    )
    p.add_argument("--dsn", required=True, help="PostgreSQL libpq 连接串（禁止 superuser/复制角色）")
    p.add_argument("--contract", required=True, help="装载契约 JSON 文件路径")
    p.add_argument(
        "--files",
        action="append",
        default=[],
        metavar="LOGICAL_TABLE=PATH",
        help="逻辑表的交接 JSONL 文件；同一逻辑表可重复出现以提供多个文件",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"单批落库行数（默认 {DEFAULT_BATCH_SIZE}），Python 侧任意时刻只保留一批",
    )
    return p


def _parse_file_args(items: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise HandoffError(
                f"--files 参数格式应为 逻辑表名=文件路径，收到 {item!r}", detail=item
            )
        logical, path = item.split("=", 1)
        logical = logical.strip()
        if not logical:
            raise HandoffError(f"--files 逻辑表名为空: {item!r}")
        out.setdefault(logical, []).append(path)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(Path(args.contract))
        files = _parse_file_args(args.files)
        # 每张参与表都必须显式出现（即使交成空表，也要给空文件）。
        for spec in contract.tables:
            files.setdefault(spec.logical_name, [])
        with psycopg.connect(args.dsn) as conn:
            report = load_relation_handoff(
                conn, contract, files, batch_size=args.batch_size
            )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(
            json.dumps(
                {"error": f"数据库连接/执行失败: {exc}", "pgcode": exc.sqlstate},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
