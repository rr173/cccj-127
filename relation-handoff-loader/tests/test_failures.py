"""真实失败与修正测试。

每个失败场景都有一套自洽的完整文件集（demo/data/<场景>/ 下含全部四表），
仅在目标场景的表上制造问题。每次失败前后用独立连接逐表读取完整数据，核对
原字节意义的字段值、id、关系与事实完全不变；修正同一输入后可以成功。
合法别名交换与非空循环在端到端测试里已与这些拒绝场景一起验证。
"""
from __future__ import annotations

import json

import psycopg
import pytest

from handoff.errors import HandoffError
from handoff.loader import load_relation_handoff
from tests.conftest import DEMO, loader_dsn
from tests.helpers import build_contract, demo_files, snapshot_all, write_jsonl

DATA = DEMO / "data"


def _scenario_files(subdir: str) -> dict[str, list[str]]:
    """从场景目录拼出完整四表文件集（member 支持单文件或 member-1/2 拆分）。"""
    b = DATA / subdir
    files = {
        "organization": [str(b / "organization.jsonl")],
        "assignment": [str(b / "assignment.jsonl")],
        "weird_named": [str(b / "weird.jsonl")],
    }
    if (b / "member.jsonl").exists():
        files["member"] = [str(b / "member.jsonl")]
    else:
        files["member"] = [str(b / "member-1.jsonl"), str(b / "member-2.jsonl")]
    return files


def _load(schema, files):
    contract = build_contract(schema)
    with psycopg.connect(loader_dsn()) as conn:
        return load_relation_handoff(conn, contract, files)


def _assert_unchanged(before, after):
    # 独立连接逐表读取，完整关系（含字节意义的值、id、引用、事实）必须不变。
    assert after == before


def test_missing_final_parent_only_in_old_deleted_set(fresh_schema):
    """缺失的最终父键：父行只在旧库、本次已删除，不能拿旧行当父记录。"""
    before = snapshot_all(fresh_schema)
    files = _scenario_files("fail_missing_parent")  # assignment 引用本次删除的 M3
    with pytest.raises(HandoffError) as ei:
        _load(fresh_schema, files)
    assert "最终集合中找不到目标自然键" in str(ei.value)
    assert ei.value.logical_table == "assignment"
    assert ei.value.field == "assignee"
    _assert_unchanged(before, snapshot_all(fresh_schema))

    # 修正：换回标准交接（引用最终集合里真实存在的成员），即可成功。
    report = _load(fresh_schema, demo_files("handoff"))
    assert report.committed is True


def test_extra_business_unique_conflict_rejected_then_fixed(fresh_schema):
    before = snapshot_all(fresh_schema)
    files = _scenario_files("fail_alias_conflict")  # M1、M2 别名都变成 alias-two
    with pytest.raises(HandoffError) as ei:
        _load(fresh_schema, files)
    assert "唯一约束" in str(ei.value)
    _assert_unchanged(before, snapshot_all(fresh_schema))

    # 修正：M2 改回 alias-one，即合法交换而非撞车。
    fix_dir = DATA / "fix_alias_conflict"
    bad = DATA / "fail_alias_conflict" / "member.jsonl"
    rows = [json.loads(l) for l in bad.read_text("utf-8").splitlines()]
    for r in rows:
        if r["key"] == ["T1", "M2"]:
            r["values"]["alias"] = "alias-one"
    write_jsonl(fix_dir / "member.jsonl", rows)
    fixed = _scenario_files("fail_alias_conflict")
    fixed["member"] = [str(fix_dir / "member.jsonl")]
    report = _load(fresh_schema, fixed)
    assert report.committed is True


def test_trailing_bad_line_rolls_back_everything(fresh_schema):
    before = snapshot_all(fresh_schema)
    files = _scenario_files("fail_bad_line")  # 末尾一行 JSON 结构损坏
    with pytest.raises(HandoffError):
        _load(fresh_schema, files)
    _assert_unchanged(before, snapshot_all(fresh_schema))

    # 修正：删掉末尾坏行后成功。
    fix_dir = DATA / "fix_bad_line"
    bad = DATA / "fail_bad_line" / "member.jsonl"
    rows = [json.loads(l) for l in bad.read_text("utf-8").splitlines()[:-1]]
    write_jsonl(fix_dir / "member.jsonl", rows)
    fixed = _scenario_files("fail_bad_line")
    fixed["member"] = [str(fix_dir / "member.jsonl")]
    report = _load(fresh_schema, fixed)
    assert report.committed is True


def test_delete_fact_referenced_member_rejected(fresh_schema):
    before = snapshot_all(fresh_schema)
    files = _scenario_files("fail_fact_reference")  # 删除被 fact_usage 引用的 M4
    with pytest.raises(HandoffError) as ei:
        _load(fresh_schema, files)
    assert "外部表" in str(ei.value) and "fact_usage" in str(ei.value)
    _assert_unchanged(before, snapshot_all(fresh_schema))

    # 修正：把被事实引用的 M4 加回最终集合。
    fix_dir = DATA / "fix_fact_reference"
    bad = DATA / "fail_fact_reference" / "member.jsonl"
    rows = [json.loads(l) for l in bad.read_text("utf-8").splitlines()]
    rows.append({
        "key": ["T1", "M4"],
        "values": {"display_name": "成员四（保留原样）", "alias": "alias-four",
                   "score": 40, "vip": True},
        "refs": {"manager": ["T1", "M1"], "org": ["ORG", "C"]},
    })
    write_jsonl(fix_dir / "member.jsonl", rows)
    fixed = _scenario_files("fail_fact_reference")
    fixed["member"] = [str(fix_dir / "member.jsonl")]
    report = _load(fresh_schema, fixed)
    assert report.committed is True
    # 事实仍连到保留身份。
    after = snapshot_all(fresh_schema)
    assert {r["member_id"] for r in after["fact_usage"]} <= {
        r["id"] for r in after["member"]
    }


def test_catalog_mismatch_rejects_without_touching_data(fresh_schema):
    # 在库里加一个契约未声明的额外列，制造目录不匹配。
    with psycopg.connect(loader_dsn().replace("handoff_loader", "handoff")) as conn:
        conn.autocommit = True
        conn.execute(
            psycopg.sql.SQL("ALTER TABLE {s}.member ADD COLUMN surprise text").format(
                s=psycopg.sql.Identifier(fresh_schema))
        )
    before = snapshot_all(fresh_schema)
    with pytest.raises(HandoffError):
        _load(fresh_schema, demo_files("handoff"))
    _assert_unchanged(before, snapshot_all(fresh_schema))


def test_legitimate_alias_swap_and_non_null_cycle_succeed_alongside_rejections(
    fresh_schema
):
    """与拒绝场景对照：合法别名交换 + 非空循环必须成功，证明不是一律拒绝复杂情况。"""
    report = _load(fresh_schema, demo_files("handoff"))
    after = snapshot_all(fresh_schema)
    by_key = {(r["tenant_id"], r["member_code"]): r for r in after["member"]}
    # 别名互换成功落库。
    assert by_key[("T1", "M1")]["alias"] == "alias-two"
    assert by_key[("T1", "M2")]["alias"] == "alias-one"
    # 非空负责人环。
    a = by_key[("T1", "M-NEW-A")]["id"]
    b = by_key[("T1", "M-NEW-B")]["id"]
    assert by_key[("T1", "M-NEW-A")]["manager_id"] == b
    assert by_key[("T1", "M-NEW-B")]["manager_id"] == a
    assert report.tables["member"].inserted == 3
