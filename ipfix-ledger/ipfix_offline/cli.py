"""终端入口：导入捕获、查询记录、追字段。

用法：
    python -m ipfix_offline.cli import --input capture.jsonl --db result.db
    python -m ipfix_offline.cli list --db result.db --exporter e1 --transport t1 \
        --domain 7 [--capture-line 3] [--template 256] [--limit 100] [--offset 0]
    python -m ipfix_offline.cli trace --db result.db --record 12 --field 1

成功退出码 0；捕获/参数错误退出码 2，目标库已存在退出码 3。
失败时 stderr 输出 JSON，目标结果库不会出现（或保持原样）。
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import (
    ConfigError,
    IpfixDatabaseError,
    IpfixError,
    IpfixInputError,
    TargetExistsError,
)
from .store import ResultStore
from .storage import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CACHE_KB,
    import_capture,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipfix_offline",
        description="离线 IPFIX 接入：捕获 JSONL -> SQLite 记录库（约定子集）")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="把捕获接入为新结果库（目标存在则拒绝）")
    imp.add_argument("--input", required=True, help="UTF-8 JSONL 捕获（只读）")
    imp.add_argument("--db", required=True, help="新结果库路径")
    imp.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                     help="字段批次上限（行数/字段数不是全局缓存）")
    imp.add_argument("--max-templates-per-scope", type=int, default=1024,
                     help="每个设备/会话/domain 的有效模板上限")
    imp.add_argument("--cache-kb", type=int, default=DEFAULT_CACHE_KB,
                     help="SQLite 页缓存上限（KiB）")

    lst = sub.add_parser("list", help="按作用域/捕获行/模板号读取记录")
    lst.add_argument("--db", required=True)
    lst.add_argument("--exporter")
    lst.add_argument("--transport")
    lst.add_argument("--domain", type=int)
    lst.add_argument("--capture-line", type=int)
    lst.add_argument("--template", type=int)
    lst.add_argument("--limit", type=int)
    lst.add_argument("--offset", type=int, default=0)

    tr = sub.add_parser("trace", help="追一个字段：消息内区间与模板说明位置")
    tr.add_argument("--db", required=True)
    tr.add_argument("--record", type=int, required=True,
                    help="结果库记录序号（从 1 开始，按捕获行/Set/Set内序号）")
    tr.add_argument("--field", type=int, required=True,
                    help="该记录内字段序号（从 0 开始，重复元素按位置保留）")
    return parser


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            report = import_capture(
                args.input, args.db, batch_size=args.batch_size,
                max_templates_per_scope=args.max_templates_per_scope,
                cache_kb=args.cache_kb)
            _emit(report.to_json())
            return 0
        store = ResultStore(args.db)
        if args.command == "list":
            records = store.query_records(
                exporter_id=args.exporter, transport_id=args.transport,
                domain_id=args.domain, capture_line=args.capture_line,
                template_id=args.template, limit=args.limit,
                offset=args.offset)
            _emit(records)
            return 0
        _emit(store.trace_field(args.record, args.field))
        return 0
    except TargetExistsError as exc:
        _emit({"ok": False, "error": "target_exists", "detail": str(exc)})
        return 3
    except IpfixInputError as exc:
        print(json.dumps(exc.to_json(), ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    except (ConfigError, IpfixDatabaseError, LookupError,
            FileNotFoundError, ValueError) as exc:
        _emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
        return 2
    except IpfixError as exc:
        _emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
