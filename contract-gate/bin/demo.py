#!/usr/bin/env python3
"""一条命令完成样例分流及断言：python3 -I bin/demo.py

对 examples/ 下的业务来件逐一跑分流，并把结果与**手工写出的**预期
（合格行原始字节、覆盖集合、错误关键字/位置/字节范围）逐项核对；
字节范围一律切回原始行字节验证，不靠文本搜索字段名。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from contract_gate import Contract
from contract_gate.cli import (EXIT_CONTRACT, EXIT_OK, EXIT_REJECTED,
                               run_triage)

MANIFEST = "examples/manifest.json"
SC = "https://acme.example/schemas"
OUT = "examples/out"

_CHECKS = []


def check(name, cond, detail=""):
    _CHECKS.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -- {detail}"))


def body_of(path, lineno):
    """取输入文件第 lineno 行（1 起始）去掉行尾 CRLF/LF 后的原始字节。"""
    with open(path, "rb") as f:
        for i, raw in enumerate(f, 1):
            if i == lineno:
                if raw.endswith(b"\r\n"):
                    return raw[:-2]
                if raw.endswith(b"\n"):
                    return raw[:-1]
                return raw
    raise AssertionError(f"{path} 没有第 {lineno} 行")


def cut(path, lineno, span):
    s, e = span
    return body_of(path, lineno)[s:e]


def load_rejected(out_dir):
    with open(os.path.join(out_dir, "rejected.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def all_issues(issue_dicts):
    """展平错误树里的全部 issue（含分支内部）。"""
    for it in issue_dicts:
        yield it
        for br in it.get("branches", []):
            yield from all_issues(br["issues"])


def signatures(rec):
    """一条拒收记录顶层 issue 的 (keyword, instance) 序列。"""
    return [(i["keyword"], i["instance"]) for i in rec["error"]["issues"]]


def run(name, root, inputs, expect_code):
    out_dir = os.path.join(OUT, name)
    os.makedirs(out_dir, exist_ok=True)
    code = run_triage(MANIFEST, root, inputs, out_dir)
    check(f"[{name}] 退出码={expect_code}", code == expect_code, f"实际 {code}")
    return out_dir


def main():
    os.chdir(ROOT)
    shutil.rmtree(OUT, ignore_errors=True)

    # ------------------------------------------------ 1. 主契约：组合覆盖
    orders = "examples/data/orders.jsonl"
    out = run("orders", f"{SC}/root.json", [orders], EXIT_REJECTED)
    orders_accepted = (
        b'{"id":"A","kind":"order"}\n'
        b'{"id":"B","kind":"return","reason":"damaged"}\n'
        b'{"id":"A","kind":"order","reason":null}\n'
        b'{"id":"C","kind":"order","discount":0.5,"currency":"CNY"}\n'
        b'{"id":"A","kind":"order","route":"R1","tag":"t1",'
        b'"child":{"name":"n1","children":[{"name":"n2","children":[]}]}}\n'
    )
    check("[orders] 合格行原样保留（5 行）",
          read_bytes(os.path.join(out, "accepted.jsonl")) == orders_accepted)
    rejected = load_rejected(out)
    check("[orders] 拒收 7 行且行号有序",
          [r["line"] for r in rejected] == [2, 3, 6, 7, 9, 10, 12])
    expect_sig = {
        2: [("properties", ""), ("unevaluatedProperties", "/id")],
        3: [("then", "")],
        6: [("else", ""), ("unevaluatedProperties", "/reason")],
        7: [("dependentSchemas", ""), ("unevaluatedProperties", "/discount")],
        9: [("dependentSchemas", ""), ("unevaluatedProperties", "/discount"),
            ("unevaluatedProperties", "/currency")],
        10: [("unevaluatedProperties", "/extra")],
        12: [("properties", ""), ("unevaluatedProperties", "/child")],
    }
    for rec in rejected:
        want = expect_sig[rec["line"]]
        check(f"[orders] 第{rec['line']}行错误树顶层签名 {want}",
              signatures(rec) == want, f"实际 {signatures(rec)}")
        check(f"[orders] 第{rec['line']}行契约位置非空",
              rec["resource"] is not None and rec["pointer"] is not None)
    by_line = {r["line"]: r for r in rejected}
    # $ref 兄弟 enum：id="D" 被兄弟关键字拦下
    iss2 = by_line[2]["error"]["issues"]
    enum_iss = iss2[0]["branches"][0]["issues"][0]
    check("[orders] 第2行 $ref 兄弟 enum 生效",
          enum_iss["keyword"] == "enum" and enum_iss["instance"] == "/id"
          and enum_iss["pointer"] == "/properties/id/enum")
    check("[orders] 第2行 enum 字节范围切回 \"D\"",
          cut(orders, 2, enum_iss["span"]) == b'"D"')
    check("[orders] 第2行 properties 包装范围切回 \"id\":\"D\"",
          cut(orders, 2, iss2[0]["span"]) == b'"id":"D"')
    # then 缺少 reason：定位到所属容器（整行）
    iss3 = by_line[3]["error"]["issues"]
    req_iss = iss3[0]["branches"][0]["issues"][0]
    check("[orders] 第3行 then 缺少 reason，定位到容器",
          req_iss["keyword"] == "required" and req_iss["instance"] == ""
          and cut(orders, 3, req_iss["span"]) == body_of(orders, 3))
    # 剩余字段 extra
    iss10 = by_line[10]["error"]["issues"][0]
    check("[orders] 第10行 unevaluatedProperties 切回 \"extra\":1",
          iss10["keyword"] == "unevaluatedProperties"
          and iss10["instance"] == "/extra"
          and cut(orders, 10, iss10["span"]) == b'"extra":1')
    # 递归子节点：最深的剩余字段错误落在嵌套资源 node.json
    deep = [i for i in all_issues(by_line[12]["error"]["issues"])
            if i["keyword"] == "unevaluatedProperties"
            and i["resource"] == f"{SC}/node.json"
            and i["instance"] == "/child/children/0/bogus"]
    check("[orders] 第12行递归资源 node.json 定位剩余字段",
          len(deep) == 1
          and deep[0]["pointer"] == "/unevaluatedProperties"
          and cut(orders, 12, deep[0]["span"]) == b'"bogus":true')
    summary = json.loads(read_bytes(os.path.join(out, "summary.json")))
    check("[orders] 汇总 12/5/7",
          summary["total"] == 12 and summary["accepted"] == 5
          and summary["rejected"] == 7
          and summary["files"][0]["file"] == orders)

    # ---------------------------------- 2. 关键字顺序打乱：判定必须完全一致
    out2 = run("orders-shuffled", f"{SC}/root.shuffled.json", [orders],
               EXIT_REJECTED)
    check("[shuffle] 合格字节与正序完全一致",
          read_bytes(os.path.join(out2, "accepted.jsonl")) == orders_accepted)
    rejected2 = load_rejected(out2)
    check("[shuffle] 拒收行与错误签名与正序一致",
          [(r["line"], signatures(r)) for r in rejected2]
          == [(r["line"], signatures(r)) for r in rejected])

    # ---------------------------------- 3. anyOf 两分支都成功：覆盖合并
    anyof = "examples/data/anyof.jsonl"
    out = run("anyof", f"{SC}/combo.anyof.json", [anyof], EXIT_OK)
    check("[anyOf] 两分支都成功时 {\"a\":1,\"b\":2} 合格",
          read_bytes(os.path.join(out, "accepted.jsonl")) == b'{"a":1,"b":2}\n')

    # -------------------------- 4. anyOf 第二分支失败：b 不能逃过剩余检查
    out = run("anyof-strb", f"{SC}/combo.anyof.strb.json", [anyof],
              EXIT_REJECTED)
    rejected = load_rejected(out)
    iss = rejected[0]["error"]["issues"]
    check("[anyOf-strb] 仅剩 unevaluatedProperties /b",
          signatures(rejected[0]) == [("unevaluatedProperties", "/b")])
    check("[anyOf-strb] /b 范围切回 \"b\":2",
          cut(anyof, 1, iss[0]["span"]) == b'"b":2')

    # ---------------------------------- 5. oneOf 两支都成功必须拒收
    out = run("oneof", f"{SC}/combo.oneof.json", [anyof], EXIT_REJECTED)
    rejected = load_rejected(out)
    iss = rejected[0]["error"]["issues"][0]
    check("[oneOf] 两支都成功报 oneOf 且无分支错误",
          iss["keyword"] == "oneOf" and "branches" not in iss
          and "2" in iss["message"])

    # ---------------------------------- 6. contains 只认领命中位置
    contains = "examples/data/contains.jsonl"
    out = run("contains", f"{SC}/combo.contains.json", [contains],
              EXIT_REJECTED)
    check("[contains] [1,3,2] 合格",
          read_bytes(os.path.join(out, "accepted.jsonl")) == b"[1,3,2]\n")
    rejected = load_rejected(out)
    check("[contains] 拒收第 1、3、4 行",
          [r["line"] for r in rejected] == [1, 3, 4])
    iss = rejected[0]["error"]["issues"][0]
    check("[contains] 第1行位置1未覆盖且切回 \"x\"",
          iss["keyword"] == "unevaluatedItems" and iss["instance"] == "/1"
          and cut(contains, 1, iss["span"]) == b'"x"')
    iss = rejected[1]["error"]["issues"][0]
    check("[contains] 第3行 contains 未命中带两个元素分支",
          iss["keyword"] == "contains" and len(iss.get("branches", [])) == 2)
    iss = rejected[2]["error"]["issues"][0]
    check("[contains] 第4行空数组 contains 未命中",
          iss["keyword"] == "contains" and "branches" not in iss)

    # -------------------- 7. 失败分支的深层错误不外泄、合格分支认领不串线
    tangle = "examples/data/tangle.jsonl"
    out = run("tangle", f"{SC}/branch.tangle.json", [tangle], EXIT_REJECTED)
    rejected = load_rejected(out)
    check("[tangle] 第1行只有 unevaluatedProperties /y",
          len(rejected) == 1 and rejected[0]["line"] == 1
          and signatures(rejected[0]) == [("unevaluatedProperties", "/y")])
    leaked = [i for i in all_issues(rejected[0]["error"]["issues"])
              if i["instance"].startswith("/x")]
    check("[tangle] 失败分支的 /x/deep 错误不串线", leaked == [])
    check("[tangle] /y 范围切回 \"y\":9",
          cut(tangle, 1, rejected[0]["error"]["issues"][0]["span"]) == b'"y":9')
    check("[tangle] 第2行合格", read_bytes(os.path.join(out, "accepted.jsonl"))
          == b'{"x":{"deep":1}}\n')

    # -------------------- 8. 组合全失败时错误树保留深层分支身份与原因
    tangle2 = "examples/data/tangle2.jsonl"
    out = run("tangle2", f"{SC}/branch.tangle2.json", [tangle2], EXIT_REJECTED)
    rejected = load_rejected(out)
    iss = rejected[0]["error"]["issues"]
    check("[tangle2] 顶层 allOf + unevaluatedProperties",
          signatures(rejected[0]) == [("allOf", ""),
                                      ("unevaluatedProperties", "/x")])
    anyof_iss = iss[0]["branches"][0]["issues"][0]
    check("[tangle2] allOf[0] 内为 anyOf 两失败分支",
          anyof_iss["keyword"] == "anyOf"
          and [b["name"] for b in anyof_iss["branches"]] == ["anyOf[0]",
                                                             "anyOf[1]"])
    flat = list(all_issues([anyof_iss]))
    check("[tangle2] anyOf[0] 深层 const 错误在 /x/deep",
          any(i["keyword"] == "const" and i["instance"] == "/x/deep"
              for i in flat))
    check("[tangle2] anyOf[1] 深层 required 错误在 /x",
          any(i["keyword"] == "required" and i["instance"] == "/x"
              for i in flat))

    # ---------- 9. 多字节/转义键/重复键/大数/坏行后的好行：逐项切回原始字节
    mixed = "examples/data/mixed.jsonl"
    out = run("mixed", f"{SC}/mixed.json", [mixed], EXIT_REJECTED)
    mixed_accepted = "".join([
        '{"id":1,"big":123456789012345678901234567890,"名称":"中文值"}\n',
        '{"id":2,"amount":9007199254740993}\n',
        '{"id":4,"esc\\u0041ped":true}\n',
        '{"id":10,"名称":"恢复"}\n',
    ]).encode("utf-8")
    check("[mixed] 合格行原样保留（大数与转义键字节不动）",
          read_bytes(os.path.join(out, "accepted.jsonl")) == mixed_accepted)
    rejected = load_rejected(out)
    check("[mixed] 拒收 8 行", [r["line"] for r in rejected]
          == [3, 5, 6, 7, 8, 9, 10, 11])
    by_line = {r["line"]: r for r in rejected}
    # 大数精度：9007199254740992 不等于 const 9007199254740993
    const_iss = by_line[3]["error"]["issues"][0]["branches"][0]["issues"][0]
    check("[mixed] 第3行大数 const 精度拒收",
          const_iss["keyword"] == "const" and const_iss["instance"] == "/amount"
          and cut(mixed, 3, const_iss["span"]) == b"9007199254740992")
    # 转义键重复成员：两处键名位置都能切回原文
    dup = by_line[5]
    check("[mixed] 第5行转义键重复成员双位置",
          dup["category"] == "duplicate" and dup["resource"] is None
          and dup["error"]["key"] == "escAped"
          and cut(mixed, 5, dup["error"]["spans"][0]) == b'"escAped"'
          and cut(mixed, 5, dup["error"]["spans"][1]) == b'"esc\\u0041ped"')
    dup = by_line[6]
    check("[mixed] 第6行普通重复成员双位置",
          dup["category"] == "duplicate"
          and cut(mixed, 6, dup["error"]["spans"][0]) == b'"dup"'
          and cut(mixed, 6, dup["error"]["spans"][1]) == b'"dup"')
    # 多字节成员范围
    iss7 = by_line[7]["error"]["issues"][0]
    check("[mixed] 第7行多字节字段切回 \"extra\":\"多余\"",
          iss7["keyword"] == "unevaluatedProperties"
          and iss7["instance"] == "/extra"
          and cut(mixed, 7, iss7["span"]) == '"extra":"多余"'.encode("utf-8"))
    # 同名字段在不同层：/obj/id 的 5 不靠第一次搜索定位
    inner = by_line[8]["error"]["issues"][0]["branches"][0]["issues"]
    type_iss = [i for i in all_issues(inner) if i["keyword"] == "type"]
    check("[mixed] 第8行内层 /obj/id 类型错误切回第二个 id 的值",
          len(type_iss) == 1 and type_iss[0]["instance"] == "/obj/id"
          and cut(mixed, 8, type_iss[0]["span"]) == b"5")
    # 内层剩余字段
    iss9 = [i for i in all_issues(by_line[9]["error"]["issues"])
            if i["keyword"] == "unevaluatedProperties"
            and i["instance"] == "/obj/bad"]
    check("[mixed] 第9行内层剩余字段 /obj/bad 切回 \"bad\":1",
          len(iss9) == 1 and iss9[0]["instance"] == "/obj/bad"
          and cut(mixed, 9, iss9[0]["span"]) == b'"bad":1')
    # 坏 JSON 与空行：契约位置为 null
    check("[mixed] 第10行坏 JSON 契约位置为 null",
          by_line[10]["category"] == "json"
          and by_line[10]["resource"] is None
          and by_line[10]["pointer"] is None
          and by_line[10]["error"]["span"] == [1, 2])
    check("[mixed] 第11行空物理行有错误",
          by_line[11]["category"] == "empty"
          and by_line[11]["resource"] is None)

    # ---------------------------------- 10. 非法 UTF-8 只影响该行
    encoding = "examples/data/encoding.jsonl"
    out = run("encoding", f"{SC}/mixed.json", [encoding], EXIT_REJECTED)
    check("[encoding] 坏行前后好行照收",
          read_bytes(os.path.join(out, "accepted.jsonl"))
          == b'{"id":20}\n{"id":22}\n')
    rejected = load_rejected(out)
    check("[encoding] 第2行 utf8 类别且契约位置为 null",
          len(rejected) == 1 and rejected[0]["line"] == 2
          and rejected[0]["category"] == "utf8"
          and rejected[0]["resource"] is None
          and rejected[0]["error"]["span"] == [16, 17])

    # ---------------------------------- 11. CRLF 行尾只去行尾、补一个 LF
    crlf = "examples/data/crlf.jsonl"
    out = run("crlf", f"{SC}/mixed.json", [crlf], EXIT_OK)
    check("[crlf] 合格输出去掉 CR 只留 LF",
          read_bytes(os.path.join(out, "accepted.jsonl"))
          == b'{"id":30}\n{"id":31}\n')

    # ---------------------------------- 12. API：手工写出的预期覆盖集合
    c_anyof = Contract.from_manifest(MANIFEST, f"{SC}/combo.anyof.json")
    r = c_anyof.validate_line(b'{"a":1,"b":2}')
    check("[api] anyOf 合并覆盖 {a,b}",
          r.ok and r.coverage == {"properties": ["a", "b"], "items": []})
    c_root = Contract.from_manifest(MANIFEST, f"{SC}/root.json")
    r = c_root.validate_line(body_of(orders, 11))
    check("[api] 主契约根覆盖集合",
          r.ok and r.coverage["properties"]
          == ["child", "id", "kind", "route", "tag"])
    c_contains = Contract.from_manifest(MANIFEST, f"{SC}/combo.contains.json")
    r = c_contains.validate_line(b"[1,3,2]")
    check("[api] contains 命中位置覆盖 {0,1,2}",
          r.ok and r.coverage == {"properties": [], "items": [0, 1, 2]})
    c_mixed = Contract.from_manifest(MANIFEST, f"{SC}/mixed.json")
    r = c_mixed.validate_line(body_of(mixed, 4))
    check("[api] 转义键解码后认领 escAped",
          r.ok and r.coverage["properties"] == ["escAped", "id"])
    r = c_mixed.validate_line(body_of(mixed, 2))
    check("[api] 大数 const 精确相等判合格", r.ok)

    # ---------------------------------- 13. 预检失败：退出码 2 且不产正式文件
    bad_out = os.path.join(OUT, "bad")
    os.makedirs(bad_out, exist_ok=True)
    sink = io.StringIO()
    code = run_triage("examples/bad/manifest.json",
                      "https://acme.example/schemas/bad.json",
                      ["examples/data/orders.jsonl"], bad_out, stream=sink)
    check("[preflight] 坏契约退出码=2", code == EXIT_CONTRACT)
    check("[preflight] 预检失败不产出合格/拒收/汇总文件",
          not os.path.exists(os.path.join(bad_out, "accepted.jsonl"))
          and not os.path.exists(os.path.join(bad_out, "rejected.jsonl"))
          and not os.path.exists(os.path.join(bad_out, "summary.json")))
    messages = sink.getvalue()
    check("[preflight] 报告未知关键字与悬空引用",
          "不支持的关键字" in messages and "悬空引用" in messages)

    passed = sum(1 for _, ok in _CHECKS if ok)
    print(f"\n{passed}/{len(_CHECKS)} 项断言通过；样例输出在 {OUT}/")
    return 0 if passed == len(_CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
