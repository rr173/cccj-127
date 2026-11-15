"""测试共用辅助：动态契约、数据库快照、文件集构造。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import psycopg

from handoff.contract import parse_contract
from tests.conftest import DEMO, loader_dsn  # noqa: E402


def build_contract(schema: str, override: dict[str, Any] | None = None):
    """以演示契约为模板，替换 schema 名称，生成针对任意测试 schema 的契约。"""
    doc = json.loads((DEMO / "contract.json").read_text(encoding="utf-8"))
    doc["schema"] = schema
    for t in doc["tables"]:
        t["schema"] = schema
    if override:
        # 允许测试改写契约以制造目录不匹配等场景。
        for logical, changes in override.items():
            for t in doc["tables"]:
                if t["logical_name"] == logical:
                    t.update(changes)
    return parse_contract(doc)


def demo_files(subdir: str = "handoff") -> dict[str, list[str]]:
    b = DEMO / "data" / subdir
    return {
        "organization": [str(b / "organization.jsonl")],
        "member": [str(b / "member-1.jsonl"), str(b / "member-2.jsonl")],
        "assignment": [str(b / "assignment.jsonl")],
        "weird_named": [str(b / "weird.jsonl")],
    }


def snapshot_all(schema: str) -> dict[str, list[dict[str, Any]]]:
    """独立连接读取全部参与表 + 事实表的完整数据，用于失败前后字节级比对。"""
    out: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(loader_dsn()) as conn:
        for name, cols in {
            "organization": ["id", "org_code", "org_tenant", "legal_name", "tier",
                             "active", "contact_member"],
            "member": ["id", "tenant_id", "member_code", "display_name", "alias",
                       "score", "vip", "manager_id", "org_id"],
            "assignment": ["id", "project_key", "role_code", "note", "member_id"],
            "fact_usage": ["fact_id", "event_name", "member_id"],
        }.items():
            q = psycopg.sql.SQL("SELECT {c} FROM {s}.{t} ORDER BY 1").format(
                c=psycopg.sql.SQL(", ").join(psycopg.sql.Identifier(c) for c in cols),
                s=psycopg.sql.Identifier(schema),
                t=psycopg.sql.Identifier(name),
            )
            out[name] = [dict(zip(cols, row)) for row in conn.execute(q).fetchall()]
        # 引号中文表
        q = psycopg.sql.SQL('SELECT id, kc, label FROM {s}."weird 名.表 ""x""" ORDER BY 1').format(
            s=psycopg.sql.Identifier(schema)
        )
        out["weird"] = [
            {"id": r[0], "kc": r[1], "label": r[2]}
            for r in conn.execute(q).fetchall()
        ]
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def clone_files(src: dict[str, list[str]]) -> dict[str, list[str]]:
    return copy.deepcopy(src)
