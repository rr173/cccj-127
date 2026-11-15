"""独立的朴素小样本关系模型（测试预言）。

刻意用与产品完全不同的朴素实现（纯 Python 字典/集合 + 逐行 json），不 import
handoff 包里任何映射或差分代码，用来独立计算“这次交接之后目标关系应该是什么”：

* 最终自然键集合、保留/新增/删除身份；
* 每条具名引用在最终集合里解析出的目标 id（含非空循环，不做拓扑排序）；
* 逐表 inserted/updated/deleted/unchanged 四类计数；
* 业务唯一约束冲突与缺失最终父键。

它只读 JSONL 与调用方给出的“旧状态快照”，输出预期，再由测试与数据库实际
提交结果逐项比对。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NaiveRow:
    key: tuple[str, ...]
    values: dict[str, Any]
    refs: dict[str, tuple[str, ...] | None]
    file: str
    line: int


@dataclass
class NaiveTable:
    logical: str
    natural_key: tuple[str, ...]
    value_cols: tuple[str, ...]
    ref_names: tuple[str, ...]
    ref_targets: dict[str, str]
    unique_constraints: tuple[tuple[str, ...], ...]
    rows: dict[tuple[str, ...], NaiveRow] = field(default_factory=dict)
    # 旧状态：自然键 -> id
    old_ids: dict[tuple[str, ...], int] = field(default_factory=dict)
    # 旧行的完整业务画像（值 + 已解析引用 id），用于判 unchanged
    old_profile: dict[tuple[str, ...], tuple[Any, ...]] = field(default_factory=dict)


def _load_strict(path: Path) -> list[NaiveRow]:
    out: list[NaiveRow] = []
    with open(path, "rb") as fh:
        for line_no, raw in enumerate(fh, 1):
            obj = json.loads(raw.decode("utf-8"))  # 测试数据本身假定合法
            key = tuple(obj["key"])
            out.append(
                NaiveRow(
                    key=key,
                    values=obj["values"],
                    refs={k: (tuple(v) if v is not None else None)
                          for k, v in obj["refs"].items()},
                    file=str(path),
                    line=line_no,
                )
            )
    return out


class NaiveModel:
    def __init__(self, contract_doc: dict[str, Any]):
        self.tables: dict[str, NaiveTable] = {}
        for t in contract_doc["tables"]:
            logical = t["logical_name"]
            self.tables[logical] = NaiveTable(
                logical=logical,
                natural_key=tuple(t["natural_key"]),
                value_cols=tuple(c["name"] for c in t.get("columns", [])),
                ref_names=tuple(r["name"] for r in t.get("refs", [])),
                ref_targets={r["name"]: r["target"] for r in t.get("refs", [])},
                unique_constraints=tuple(tuple(u) for u in t.get("unique_constraints", [])),
            )

    # ---- 读入交接文件，构建最终集合（重复自然键报错） ----
    def ingest(self, files: dict[str, list[str]]) -> None:
        for logical, paths in files.items():
            tbl = self.tables[logical]
            for p in paths:
                for row in _load_strict(Path(p)):
                    if row.key in tbl.rows:
                        raise AssertionError(
                            f"重复自然键 {row.key} 于 {row.file}:{row.line}"
                        )
                    tbl.rows[row.key] = row

    def seed_old_state(self, logical: str, old_rows: list[dict[str, Any]]) -> None:
        """old_rows: 每行含 id、自然键分量、普通值与已解析的引用 id（来自 DB 快照）。"""
        tbl = self.tables[logical]
        for r in old_rows:
            key = tuple(r["key"])
            tbl.old_ids[key] = r["id"]
            tbl.old_profile[key] = (
                tuple(r["values"][c] for c in tbl.value_cols),
                tuple(r["ref_ids"][n] for n in tbl.ref_names),
            )

    # ---- 身份：最终集合决定保留/新增/删除，不看文件位置或普通字段 ----
    def identity_plan(self, logical: str, new_id_start: int) -> dict[tuple[str, ...], int]:
        tbl = self.tables[logical]
        final_keys = set(tbl.rows)
        plan: dict[tuple[str, ...], int] = {}
        next_id = new_id_start
        for key in sorted(final_keys):
            if key in tbl.old_ids:
                plan[key] = tbl.old_ids[key]  # 保留身份
            else:
                plan[key] = next_id  # 新身份
                next_id += 1
        return plan

    def deleted_keys(self, logical: str) -> set[tuple[str, ...]]:
        tbl = self.tables[logical]
        return set(tbl.old_ids) - set(tbl.rows)

    # ---- 引用解析：只在最终集合找，支持循环；找不到返回 None ----
    def resolve_refs(
        self, id_plans: dict[str, dict[tuple[str, ...], int]]
    ) -> dict[str, dict[tuple[str, ...], dict[str, int | None]]]:
        resolved: dict[str, dict[tuple[str, ...], dict[str, int | None]]] = {}
        for logical, tbl in self.tables.items():
            per_table: dict[tuple[str, ...], dict[str, int | None]] = {}
            for key, row in tbl.rows.items():
                refs: dict[str, int | None] = {}
                for name in tbl.ref_names:
                    target_logical = tbl.ref_targets[name]
                    fk = row.refs[name]
                    if fk is None:
                        refs[name] = None
                    else:
                        refs[name] = id_plans[target_logical].get(tuple(fk))
                per_table[key] = refs
            resolved[logical] = per_table
        return resolved

    def missing_non_null_parents(
        self,
        resolved: dict[str, dict[tuple[str, ...], dict[str, int | None]]],
        nullable: dict[str, dict[str, bool]],
    ) -> list[tuple[str, tuple[str, ...], str]]:
        problems = []
        for logical, tbl in self.tables.items():
            for key, row in tbl.rows.items():
                for name in tbl.ref_names:
                    if not nullable[logical][name] and resolved[logical][key][name] is None:
                        problems.append((logical, key, name))
        return problems

    # ---- 业务唯一约束（普通 UNIQUE 语义：null 不参与冲突） ----
    def unique_violations(self) -> list[tuple[str, tuple[str, ...]]]:
        problems = []
        for logical, tbl in self.tables.items():
            all_uks = (tbl.natural_key, *tbl.unique_constraints)
            for uk in all_uks:
                seen: dict[tuple[Any, ...], tuple[str, ...]] = {}
                for key, row in tbl.rows.items():
                    if uk == tbl.natural_key:
                        val = key
                    else:
                        val = tuple(row.values[c] for c in uk)
                    if any(v is None for v in val):
                        continue  # 普通 UNIQUE：含 null 不冲突
                    if val in seen:
                        problems.append((logical, uk))
                    seen[val] = key
        return problems

    # ---- 四类计数 ----
    def counts(
        self,
        logical: str,
        id_plans: dict[str, dict[tuple[str, ...], int]],
        resolved: dict[str, dict[tuple[str, ...], dict[str, int | None]]],
    ) -> dict[str, int]:
        tbl = self.tables[logical]
        inserted = updated = unchanged = 0
        for key, row in tbl.rows.items():
            if key not in tbl.old_ids:
                inserted += 1
                continue
            new_profile = (
                tuple(row.values[c] for c in tbl.value_cols),
                tuple(resolved[logical][key][n] for n in tbl.ref_names),
            )
            if new_profile == tbl.old_profile.get(key):
                unchanged += 1
            else:
                updated += 1
        deleted = len(self.deleted_keys(logical))
        return {
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
            "unchanged": unchanged,
        }
