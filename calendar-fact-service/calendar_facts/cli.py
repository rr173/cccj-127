"""实际展开入口（离线 CLI）。

用法：
    python -m calendar_facts.cli \
        --input fixtures/dst.ics --tz America/New_York \
        --window-start 2024-03-01T00:00:00Z --window-end 2024-04-01T00:00:00Z \
        --out facts.jsonl --report report.json

成功：退出码 0，事实写 JSONL，诊断写 JSON（原子替换旧文件）。
失败：退出码 2，全部故障打印到 stderr，已有结果文件原字节不动。
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import CalendarInputError
from .service import CalendarFactService
from .timeutil import TimeParseError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="calendar_facts",
        description="把 iCalendar 检修计划展开为 UTC 区间事实（JSONL）")
    p.add_argument("--input", required=True, help="本地 .ics 文件（UTF-8）")
    p.add_argument("--tz", required=True,
                   help="设备所属 IANA 时区，如 America/New_York")
    p.add_argument("--window-start", required=True,
                   help="查询窗起点 UTC（含），如 2024-03-01T00:00:00Z")
    p.add_argument("--window-end", required=True,
                   help="查询窗终点 UTC（不含），如 2024-04-01T00:00:00Z")
    p.add_argument("--out", required=True, help="事实 JSONL 输出路径")
    p.add_argument("--report", required=True, help="诊断 JSON 输出路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        svc = CalendarFactService(args.tz)
        report = svc.expand_file(
            args.input, args.window_start, args.window_end,
            args.out, args.report)
    except (CalendarInputError, TimeParseError, FileNotFoundError, OSError) as exc:
        payload = {"ok": False,
                   "problems": exc.problems if isinstance(exc, CalendarInputError)
                   else [str(exc)]}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report.to_json()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
