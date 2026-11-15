#!/usr/bin/env python3
"""生成 maintainance 大捕获（十万条以上 Data Record）用于性能/内存实测。

这个脚本只是压测数据发生器，不是被测解码器的“答案来源”：
  * 模板/字段布局手写在本文件里，hex 由结构体直接拼出；
  * 期望记录总数、首尾字段值同时写入 manifest，供核对。
它生成两类行：每 500 行一条纯模板（不推进序列号），其余消息携带固定布局的
Data Set；device/transport/domain 按行交错，使模板状态需要多作用域并行。
"""
from __future__ import annotations

import argparse
import json
import os

EXPORTERS = ["exporter-a", "exporter-b"]
TRANSPORTS = ["transport-01", "transport-02"]
DOMAINS = [11, 12]
TEMPLATE_ID = 400
FIELDS = [(8, 4), (12, 4), (2, 4), (7, 2), (11, 2), (1, 8), (152, 8), (153, 8)]
RECORD_LEN = sum(length for _e, length in FIELDS)  # 44


def message(export_time: int, seq: int, domain: int, sets: bytes) -> bytes:
    length = 16 + len(sets)
    return (b"\x00\x0a" + length.to_bytes(2, "big")
            + export_time.to_bytes(4, "big") + seq.to_bytes(4, "big")
            + domain.to_bytes(4, "big") + sets)


def template_set() -> bytes:
    body = TEMPLATE_ID.to_bytes(2, "big") + len(FIELDS).to_bytes(2, "big")
    for element_id, length in FIELDS:
        body += element_id.to_bytes(2, "big") + length.to_bytes(2, "big")
    return (2).to_bytes(2, "big") + (4 + len(body)).to_bytes(2, "big") + body


def data_set(records: list[bytes]) -> bytes:
    payload = b"".join(records)
    return (TEMPLATE_ID.to_bytes(2, "big")
            + (4 + len(payload)).to_bytes(2, "big") + payload)


def record(i: int) -> bytes:
    src = bytes([10, 0, (i >> 8) & 0xFF, i & 0xFF])
    dst = bytes([10, 1, (i >> 8) & 0xFF, i & 0xFF])
    return (src + dst + (i % 65536).to_bytes(4, "big")
            + (1024 + (i % 1000)).to_bytes(2, "big")
            + (2000 + (i % 1000)).to_bytes(2, "big")
            + (i * 1000).to_bytes(8, "big")
            + (1700000000000 + i).to_bytes(8, "big")
            + (1700000001000 + i).to_bytes(8, "big"))


def generate(out_path: str, target_records: int, per_message: int,
             manifest_path: str) -> dict:
    seq = {key: 10_000 for key in
           ((e, t, d) for e in EXPORTERS for t in TRANSPORTS for d in DOMAINS)}
    active = {key: False for key in seq}
    total = 0
    line_no = 0
    first = last = None
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        while total < target_records:
            for exporter in EXPORTERS:
                for transport in TRANSPORTS:
                    for domain in DOMAINS:
                        if total >= target_records:
                            break
                        key = (exporter, transport, domain)
                        line_no += 1
                        if not active[key] or line_no % 500 == 1:
                            # 纯模板消息：只刷新定义，不产生记录，序列号不变
                            payload = message(1700000000, seq[key], domain,
                                              template_set())
                            active[key] = True
                        else:
                            count = min(per_message,
                                        target_records - total)
                            records = []
                            for _ in range(count):
                                records.append(record(total))
                                total += 1
                            payload = message(1700000000, seq[key], domain,
                                              data_set(records))
                            seq[key] = (seq[key] + count) & 0xFFFFFFFF
                            if first is None:
                                first = (line_no, total - count)
                            last = (line_no, total - 1)
                        fh.write(json.dumps({
                            "exporter_id": exporter,
                            "transport_id": transport,
                            "message_hex": payload.hex(),
                        }, separators=(",", ":")) + "\n")
    manifest = {
        "capture": os.path.abspath(out_path),
        "bytes": os.path.getsize(out_path),
        "lines": line_no,
        "records": total,
        "records_per_message": per_message,
        "template_id": TEMPLATE_ID,
        "record_length": RECORD_LEN,
        "fields": [{"element_id": e, "declared_length": l}
                   for e, l in FIELDS],
        "first_record": {"index": 0,
                         "sourceIPv4Address": "10.0.0.0",
                         "destinationIPv4Address": "10.1.0.0",
                         "packetDeltaCount": "0",
                         "octetDeltaCount": "0"},
        "last_record": {"index": total - 1,
                        "sourceIPv4Address":
                            f"10.0.{(total - 1) >> 8 & 0xFF}.{(total - 1) & 0xFF}",
                        "destinationIPv4Address":
                            f"10.1.{(total - 1) >> 8 & 0xFF}.{(total - 1) & 0xFF}",
                        "packetDeltaCount": str((total - 1) % 65536),
                        "octetDeltaCount": str((total - 1) * 1000)},
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--records", type=int, default=120_000)
    parser.add_argument("--per-message", type=int, default=6)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    info = generate(args.out, args.records, args.per_message, args.manifest)
    print(json.dumps({k: info[k] for k in ("bytes", "lines", "records")},
                     ensure_ascii=False))
