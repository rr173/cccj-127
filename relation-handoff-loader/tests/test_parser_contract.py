"""JSONL 严格解析与契约自洽性测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff.contract import parse_contract
from handoff.errors import HandoffError
from handoff.parser import BIGINT_MAX, BIGINT_MIN, iter_batches, iter_jsonl


def base_contract_doc() -> dict:
    return {
        "schema": "s",
        "tables": [
            {
                "logical_name": "t",
                "schema": "s",
                "table": "t",
                "natural_key": ["k1", "k2"],
                "columns": [
                    {"name": "txt", "type": "text", "nullable": True},
                    {"name": "num", "type": "bigint", "nullable": False},
                    {"name": "flag", "type": "boolean", "nullable": True},
                ],
                "refs": [
                    {"name": "parent", "column": "parent_id", "target": "t",
                     "nullable": True, "deferrable": True, "initially_deferred": True}
                ],
                "unique_constraints": [["txt"]],
                "id_sequence": "t_id_seq",
            }
        ],
    }


def write(tmp: Path, name: str, content: bytes) -> Path:
    p = tmp / name
    p.write_bytes(content)
    return p


def row(key, txt="x", num=1, flag=None, parent=None):
    return {"key": key, "values": {"txt": txt, "num": num, "flag": flag},
            "refs": {"parent": parent}}


def test_natural_key_text_preserved_007_neq_7(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    p = write(tmp_path, "a.jsonl",
              (json.dumps(row(["007", "z"], num=5)) + "\n").encode())
    rows = list(iter_jsonl(spec, contract, p))
    assert rows[0].key == ("007", "z")  # 原样文本，不做数值化


def test_reject_duplicate_json_member(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    bad = b'{"key": ["a","b"], "key": ["c","d"], "values": {"txt": null, "num": 1, "flag": null}, "refs": {"parent": null}}\n'
    p = write(tmp_path, "a.jsonl", bad)
    with pytest.raises(HandoffError) as ei:
        list(iter_jsonl(spec, contract, p))
    assert "重复成员" in str(ei.value)


def test_reject_blank_line(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    content = (json.dumps(row(["a", "b"])) + "\n   \n").encode()
    p = write(tmp_path, "a.jsonl", content)
    it = iter_jsonl(spec, contract, p)
    next(it)
    with pytest.raises(HandoffError) as ei:
        next(it)
    assert "空行" in str(ei.value) and ei.value.line == 2


def test_reject_invalid_utf8(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    p = write(tmp_path, "a.jsonl", b'{"key": ["a",\xff], "values": {"txt": null, "num": 1, "flag": null}, "refs": {"parent": null}}\n')
    with pytest.raises(HandoffError) as ei:
        list(iter_jsonl(spec, contract, p))
    assert "UTF-8" in str(ei.value)


def test_reject_bad_structure(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    # 缺少 refs、夹带额外字段
    bad = b'{"key": ["a","b"], "values": {"txt": null, "num": 1, "flag": null}, "extra": 1}\n'
    p = write(tmp_path, "a.jsonl", bad)
    with pytest.raises(HandoffError):
        list(iter_jsonl(spec, contract, p))


def test_reject_missing_and_extra_value_columns(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    bad = {"key": ["a", "b"], "values": {"num": 1, "flag": None},
           "refs": {"parent": None}}  # 缺 txt
    p = write(tmp_path, "a.jsonl", (json.dumps(bad) + "\n").encode())
    with pytest.raises(HandoffError) as ei:
        list(iter_jsonl(spec, contract, p))
    assert "values" in str(ei.value)


def test_bool_cannot_stand_for_bigint(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    p = write(tmp_path, "a.jsonl",
              (json.dumps(row(["a", "b"], num=True)) + "\n").encode())
    with pytest.raises(HandoffError) as ei:
        list(iter_jsonl(spec, contract, p))
    assert "bigint" in str(ei.value)


def test_float_rejected_for_bigint(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    p = write(tmp_path, "a.jsonl",
              (json.dumps(row(["a", "b"], num=1.5)) + "\n").encode())
    with pytest.raises(HandoffError):
        list(iter_jsonl(spec, contract, p))


def test_bigint_boundaries_accepted_and_out_of_range_rejected(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    ok = write(tmp_path, "ok.jsonl",
               (json.dumps(row(["a", "b"], num=BIGINT_MAX)) + "\n"
                + json.dumps(row(["c", "d"], num=BIGINT_MIN)) + "\n").encode())
    rows = list(iter_jsonl(spec, contract, ok))
    assert [r.values["num"] for r in rows] == [BIGINT_MAX, BIGINT_MIN]
    bad = write(tmp_path, "bad.jsonl",
                (json.dumps(row(["e", "f"], num=BIGINT_MAX + 1)) + "\n").encode())
    with pytest.raises(HandoffError):
        list(iter_jsonl(spec, contract, bad))


def test_non_null_ref_cannot_be_null(tmp_path):
    doc = base_contract_doc()
    doc["tables"][0]["refs"][0]["nullable"] = False
    contract = parse_contract(doc)
    spec = contract.table("t")
    p = write(tmp_path, "a.jsonl",
              (json.dumps(row(["a", "b"], parent=None)) + "\n").encode())
    with pytest.raises(HandoffError) as ei:
        list(iter_jsonl(spec, contract, p))
    assert "不允许 null" in str(ei.value)


def test_ref_arity_and_non_null_components(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    bad = row(["a", "b"], parent=["only-one"])  # 目标自然键是 2 分量
    p = write(tmp_path, "a.jsonl", (json.dumps(bad) + "\n").encode())
    with pytest.raises(HandoffError):
        list(iter_jsonl(spec, contract, p))
    bad2 = row(["a", "b"], parent=["x", ""])  # 空串分量
    p2 = write(tmp_path, "b.jsonl", (json.dumps(bad2) + "\n").encode())
    with pytest.raises(HandoffError):
        list(iter_jsonl(spec, contract, p2))


def test_batches_are_bounded(tmp_path):
    contract = parse_contract(base_contract_doc())
    spec = contract.table("t")
    lines = "".join(json.dumps(row([f"k{i}", "b"])) + "\n" for i in range(5))
    p = write(tmp_path, "a.jsonl", lines.encode())
    batches = list(iter_batches(spec, contract, [p], batch_size=2))
    assert [len(b) for b in batches] == [2, 2, 1]


def test_contract_rejects_ref_to_non_participating_table():
    doc = base_contract_doc()
    doc["tables"][0]["refs"][0]["target"] = "ghost"
    with pytest.raises(HandoffError):
        parse_contract(doc)


def test_contract_rejects_duplicate_logical_name():
    doc = base_contract_doc()
    t2 = json.loads(json.dumps(doc["tables"][0]))
    doc["tables"].append(t2)
    with pytest.raises(HandoffError):
        parse_contract(doc)


def test_contract_rejects_unsupported_type():
    doc = base_contract_doc()
    doc["tables"][0]["columns"].append({"name": "d", "type": "numeric"})
    with pytest.raises(HandoffError):
        parse_contract(doc)
