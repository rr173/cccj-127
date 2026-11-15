#!/usr/bin/env python3
"""规模基准：可调文件数与行数，记录计划耗时、打开文件数、读取行数、峰值 RSS。

生成 N 个文件 × 每文件 M 行的目录（新旧两个 truncate spec 交替），
用一个只命中单个桶的等值查询跑 plan 与 scan（子进程，真实退出码），
把两个进程自报的耗时与峰值 RSS 汇总为一份 JSON 打到 stdout。

用法：
    python3 -I bin/bench.py --dir /tmp/hpq-bench --files 200 --rows-per-file 1000
    python3 -I bin/bench.py --dir /tmp/hpq-bench2 --files 200 --rows-per-file 100000

两次运行对比 peak_rss_bytes：行数放大 100 倍而峰值 RSS 基本不动，
即逐行交付（不全量收集）的直接证据。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HPQ = os.path.join(ROOT, "bin", "hpq.py")

SPECS = [
    {"spec_id": "old-trunc10", "partition_fields": [
        {"partition_id": 1, "name": "账户区间", "source_id": 1,
         "transform": {"kind": "truncate", "width": 10}},
        {"partition_id": 2, "name": "地区", "source_id": 2,
         "transform": {"kind": "prefix", "length": 2}}]},
    {"spec_id": "new-trunc4", "partition_fields": [
        {"partition_id": 1, "name": "账户区间", "source_id": 1,
         "transform": {"kind": "truncate", "width": 4}},
        {"partition_id": 2, "name": "地区", "source_id": 2,
         "transform": {"kind": "prefix", "length": 2}}]},
]

REGIONS = ["北京", "上海", "京𠮷", "广州", "深圳"]


def generate(dir_path: str, files: int, rows_per_file: int) -> str:
    """生成目录；返回命中文件的 file_id（账户等值查询的目标）。"""
    os.makedirs(os.path.join(dir_path, "data"))
    manifest = {
        "schema": {"fields": [
            {"field_id": 1, "name": "account", "type": "int64",
             "nullable": True},
            {"field_id": 2, "name": "region", "type": "string",
             "nullable": True}]},
        "specs": SPECS,
        "files": [],
    }
    for index in range(files):
        spec = SPECS[index % 2]
        width = spec["partition_fields"][0]["transform"]["width"]
        base = index * 20  # 桶起点是 10 与 4 的公倍数，且跨文件互不重叠
        region = REGIONS[index % len(REGIONS)]  # 文件内地区恒定，与分区一致
        file_id = f"f{index:05d}"
        rel = f"data/{file_id}.jsonl"
        with open(os.path.join(dir_path, rel), "w", encoding="utf-8",
                  newline="\n") as fh:
            for j in range(rows_per_file):
                account = base + (j % width)
                fh.write(json.dumps({"1": account, "2": region},
                                    ensure_ascii=False) + "\n")
        manifest["files"].append({
            "file_id": file_id, "path": rel, "spec_id": spec["spec_id"],
            "row_count": rows_per_file,
            "partitions": {"1": base, "2": region}})
    with open(os.path.join(dir_path, "manifest.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest


def run_hpq(*args: str) -> dict:
    proc = subprocess.run([sys.executable, "-I", HPQ, *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"hpq 失败（{proc.returncode}）：{proc.stderr}")
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="基准目录（必须不存在）")
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--rows-per-file", type=int, default=1000)
    args = parser.parse_args()
    if args.files < 2 or args.rows_per_file < 1:
        raise SystemExit("--files 与 --rows-per-file 必须为正（files≥2）")
    if os.path.exists(args.dir):
        raise SystemExit(f"基准目录已存在，拒绝覆盖：{args.dir}")

    manifest = generate(args.dir, args.files, args.rows_per_file)
    manifest_path = os.path.join(args.dir, "manifest.json")

    # 等值查询：只命中正中间那个文件的桶。
    hit_index = args.files // 2
    target = hit_index * 20 + 1
    query_path = os.path.join(args.dir, "query.json")
    with open(query_path, "w", encoding="utf-8") as fh:
        json.dump({"op": "eq", "field_id": 1, "value": target}, fh)

    plan = run_hpq("plan", "--manifest", manifest_path,
                   "--query", query_path,
                   "--output", os.path.join(args.dir, "plan.json"))
    report = run_hpq("scan", "--manifest", manifest_path,
                     "--query", query_path,
                     "--output", os.path.join(args.dir, "rows.jsonl"),
                     "--report", os.path.join(args.dir, "report.json"))

    # 抽查：交付行的账户必须都等于目标值。
    delivered_ok = True
    with open(os.path.join(args.dir, "rows.jsonl"), "r",
              encoding="utf-8") as fh:
        for line in fh:
            if json.loads(line)["account"] != target:
                delivered_ok = False
                break

    metrics = {
        "files": args.files,
        "rows_per_file": args.rows_per_file,
        "total_rows": args.files * args.rows_per_file,
        "query": {"op": "eq", "field_id": 1, "value": target},
        "plan": {
            "elapsed_seconds": plan["elapsed_seconds"],
            "peak_rss_bytes": plan["peak_rss_bytes"],
            "summary": plan["summary"],
        },
        "scan": {
            "elapsed_seconds": report["elapsed_seconds"],
            "peak_rss_bytes": report["peak_rss_bytes"],
            "files_opened": report["summary"]["files_opened"],
            "rows_read": report["summary"]["rows_read"],
            "rows_delivered": report["summary"]["rows_delivered"],
        },
        "delivered_rows_all_match_query": delivered_ok,
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
