#!/usr/bin/env python3
"""十万条级接入实测：生成捕获 -> 接入 -> 逐行核对 -> 记录资源开销。

用法：python3 -I bin/bench.py [--records 120000] [--workdir /tmp/ipfix-bench]

输出 JSON（同时写 <workdir>/bench-report.json），包含：
  峰值 RSS、耗时、捕获字节数、结果库字节数、逐行核对结果。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ipfix_offline import ResultStore, import_capture  # noqa: E402
from bin.make_bench_capture import generate  # noqa: E402


def peak_rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=120_000)
    parser.add_argument("--per-message", type=int, default=6)
    parser.add_argument("--workdir", default="/tmp/ipfix-bench")
    args = parser.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    capture = os.path.join(args.workdir, "bench.jsonl")
    manifest = os.path.join(args.workdir, "bench-manifest.json")
    db = os.path.join(args.workdir, "bench.db")
    for path in (db,):
        if os.path.exists(path):
            os.unlink(path)

    baseline = peak_rss_kb()
    t0 = time.monotonic()
    info = generate(capture, args.records, args.per_message, manifest)
    t_gen = time.monotonic() - t0

    t0 = time.monotonic()
    report = import_capture(capture, db, batch_size=1000,
                            max_templates_per_scope=8)
    t_import = time.monotonic() - t0
    after_import = peak_rss_kb()

    # 逐行核对总数与首尾字段：用只读游标流式遍历，不把记录放进 Python 列表。
    expected = info
    last_index = expected["records"] - 1
    seen = 0
    first_ok = last_ok = False
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT r.record_ordinal, r.capture_line, r.template_id, "
            "r.template_generation, f.element_id, f.value_hex "
            "FROM records r JOIN record_fields f "
            "ON f.record_ordinal=r.record_ordinal "
            "WHERE f.element_id IN (8, 12, 2, 1) "
            "ORDER BY r.record_ordinal, f.field_ordinal")
        current = None
        values = {}
        for row in cur:
            ordinal = row["record_ordinal"]
            if current is None:
                current = ordinal
            if ordinal != current:
                seen += 1
                if current == 1:
                    first_ok = (values.get(8) == bytes.fromhex("0a000000").hex()
                                and values.get(12) == bytes.fromhex("0a010000").hex())
                if current == expected["records"]:
                    last_ok = (values.get(8) == bytes.fromhex(
                        "0a00%02x%02x" % ((last_index >> 8) & 0xFF,
                                          last_index & 0xFF)).hex())
                current = ordinal
                values = {}
            values[row["element_id"]] = row["value_hex"]
        if current is not None:
            seen += 1
            if current == 1:
                first_ok = (values.get(8) == "0a000000"
                            and values.get(12) == "0a010000")
            if current == expected["records"]:
                last_ok = (values.get(8) == bytes.fromhex(
                    "0a00%02x%02x" % ((last_index >> 8) & 0xFF,
                                      last_index & 0xFF)).hex())
        count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        seqs = conn.execute(
            "SELECT count(DISTINCT sequence_number) FROM messages"
        ).fetchone()[0]

    store = ResultStore(db)
    first_record = store.query_records(limit=1)[0]
    last_page = store.query_records(limit=1, offset=expected["records"] - 1)[0]
    trace = store.trace_field(first_record["record_ordinal"], 0)
    trace_ok = trace["sliced_hex"] == trace["value_hex"] and (
        trace["var_prefix_range"] is None)

    result = {
        "ok": counts_ok(count, expected["records"]) and first_ok and last_ok
              and trace_ok and seen == expected["records"],
        "records_expected": expected["records"],
        "records_counted": count,
        "records_streamed": seen,
        "distinct_sequence_numbers": seqs,
        "first_record_ok": first_ok,
        "last_record_ok": last_ok,
        "trace_ok": trace_ok,
        "capture_lines": expected["lines"],
        "capture_bytes": expected["bytes"],
        "db_bytes": os.path.getsize(db),
        "db_bytes_per_record": round(os.path.getsize(db) / expected["records"], 2),
        "generate_seconds": round(t_gen, 3),
        "import_seconds": round(t_import, 3),
        "records_per_second": round(expected["records"] / t_import, 1),
        "peak_rss_kb_baseline": baseline,
        "peak_rss_kb_after_import": after_import,
        "peak_rss_kb_delta": after_import - baseline,
        "peak_rss_mb_after_import": round(after_import / 1024, 1),
        "first_record": {
            "capture_line": first_record["capture_line"],
            "template_generation": first_record["template_generation"],
            "fields": {f["name"]: f["value"] for f in first_record["fields"]},
        },
        "last_record": {
            "capture_line": last_page["capture_line"],
            "template_generation": last_page["template_generation"],
            "fields": {f["name"]: f["value"] for f in last_page["fields"]},
        },
        "trace_sample": {
            "record_ordinal": trace["record_ordinal"],
            "field_ordinal": trace["field_ordinal"],
            "value_range": trace["value_range"],
            "template_field_range": trace["template_field_range"],
            "sliced_hex": trace["sliced_hex"],
        },
        "import_report": report.to_json(),
    }
    with open(os.path.join(args.workdir, "bench-report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def counts_ok(count: int, expected: int) -> bool:
    return count == expected


if __name__ == "__main__":
    raise SystemExit(main())
