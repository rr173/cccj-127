"""命令行入口：离线 JSONL 契约分流。

    python3 -I bin/cgate.py --manifest examples/manifest.json \
        --root https://acme.example/schemas/root.json \
        --out examples/out examples/data/orders.jsonl

退出码（三种结果）：
    0  分流完成，全部合格
    1  分流完成，存在拒收
    2  契约无法执行或分流未能完成（预检失败、引用环、输入/输出不可读）

预检失败（退出码 2）不产出 accepted.jsonl / rejected.jsonl / summary.json；
来件中的坏行只影响该行本身。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

from .api import Contract
from .errors import ContractError, RefCycleError

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_CONTRACT = 2

ACCEPTED_NAME = "accepted.jsonl"
REJECTED_NAME = "rejected.jsonl"
SUMMARY_NAME = "summary.json"


def build_parser():
    p = argparse.ArgumentParser(
        prog="cgate",
        description="离线 JSONL 契约分流：组合式 JSON Schema 子集求值，"
                    "输出原样合格行、带精确定位的拒收行与组合判定证据。")
    p.add_argument("--manifest", required=True,
                   help="资源清单 JSON：绝对 URI → 本地契约文件")
    p.add_argument("--root", required=True, help="根契约的绝对 URI")
    p.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    p.add_argument("inputs", nargs="+", help="UTF-8 JSONL 来件，可多个")
    return p


def _print_problems(problems, stream):
    for p in problems:
        loc = []
        if p.get("resource"):
            loc.append(f"resource={p['resource']}")
        if p.get("pointer"):
            loc.append(f"pointer={p['pointer']}")
        if p.get("file"):
            loc.append(f"file={p['file']}")
        suffix = f"（{', '.join(loc)}）" if loc else ""
        print(f"契约错误: {p.get('message', '?')}{suffix}", file=stream)


def run_triage(manifest_path, root_uri, inputs, out_dir, stream=sys.stderr) -> int:
    """可编程入口：完成分流并返回退出码。"""
    try:
        contract = Contract.from_manifest(manifest_path, root_uri)
    except ContractError as e:
        _print_problems(e.problems, stream)
        return EXIT_CONTRACT

    os.makedirs(out_dir, exist_ok=True)
    accepted_path = os.path.join(out_dir, ACCEPTED_NAME)
    rejected_path = os.path.join(out_dir, REJECTED_NAME)
    summary_path = os.path.join(out_dir, SUMMARY_NAME)

    totals = {"total": 0, "accepted": 0, "rejected": 0}
    files = []
    try:
        with open(accepted_path, "wb") as fa, open(rejected_path, "wb") as fr:
            for path in inputs:
                counts = {"file": path, "total": 0, "accepted": 0, "rejected": 0}
                with open(path, "rb") as fin:
                    lineno = 0
                    for raw in fin:
                        lineno += 1
                        counts["total"] += 1
                        body = raw
                        if body.endswith(b"\r\n"):
                            body = body[:-2]
                        elif body.endswith(b"\n"):
                            body = body[:-1]
                        result = contract.validate_line(body)
                        if result.ok:
                            # 原样写回合格行字节，仅补一个 LF 分隔
                            fa.write(body + b"\n")
                            counts["accepted"] += 1
                        else:
                            rec = result.rejection_record(path, lineno)
                            fr.write((json.dumps(rec, ensure_ascii=False)
                                      + "\n").encode("utf-8"))
                            counts["rejected"] += 1
                files.append(counts)
                for k in totals:
                    totals[k] += counts[k]
    except RefCycleError as e:
        print(f"契约错误: {e}", file=stream)
        _remove_outputs(accepted_path, rejected_path, summary_path)
        return EXIT_CONTRACT
    except OSError as e:
        print(f"分流未能完成: {e}", file=stream)
        _remove_outputs(accepted_path, rejected_path, summary_path)
        return EXIT_CONTRACT

    summary = {"root": root_uri, "manifest": manifest_path, "files": files,
               "total": totals["total"], "accepted": totals["accepted"],
               "rejected": totals["rejected"]}
    with open(summary_path, "w", encoding="utf-8") as fs:
        json.dump(summary, fs, ensure_ascii=False, indent=2)
        fs.write("\n")
    return EXIT_REJECTED if totals["rejected"] else EXIT_OK


def _remove_outputs(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    code = [EXIT_CONTRACT]

    def work():
        code[0] = run_triage(args.manifest, args.root, args.inputs, args.out)

    # 深层实例需要更大的递归余量；在大栈线程中运行
    try:
        threading.stack_size(256 * 1024 * 1024)
    except (ValueError, OverflowError, RuntimeError):
        pass
    sys.setrecursionlimit(100000)
    t = threading.Thread(target=work)
    t.start()
    t.join()
    return code[0]


if __name__ == "__main__":
    sys.exit(main())
