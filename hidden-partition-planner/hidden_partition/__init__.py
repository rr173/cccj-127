"""离线隐藏分区查询规划器。

数仓历史文件按各自时代的分区规则落盘（账户区间收窄、地区全名改前缀），
本模块不改动任何历史文件，只依据每个文件自己声明的 spec 推导：
哪些文件不用读（drop）、哪些可直接交付（pass）、哪些要逐行过滤（filter）。

公开 API：
- :func:`load_manifest` / :func:`parse_manifest`  目录合同校验
- :func:`load_query` / :func:`parse_query`        查询表达式校验
- :func:`build_plan` / :func:`run_plan`           规划（plan）
- :func:`run_scan`                                执行（scan）
- :func:`evaluate` / :func:`row_matches`          公开行求值器
"""
from __future__ import annotations

from .analyze import (
    DECISION_DROP,
    DECISION_FILTER,
    DECISION_PASS,
    FALSE,
    TRUE,
    UNKNOWN,
)
from .contract import (
    Field,
    FileEntry,
    Manifest,
    PartitionField,
    Spec,
    load_manifest,
    parse_manifest,
)
from .errors import (
    CatalogError,
    DataError,
    HpqError,
    Issue,
    LimitError,
    QueryShapeError,
    TargetExistsError,
)
from .evaluator import evaluate, row_matches
from .limits import DEFAULT_LIMITS, Limits
from .planner import build_plan, plan_file, run_plan
from .query import (
    And,
    Leaf,
    Node,
    Not,
    Or,
    load_query,
    parse_query,
    to_json,
)
from .scanner import iter_rows, resolve_projection, run_scan

__version__ = "1.0.0"

__all__ = [
    "And",
    "build_plan",
    "CatalogError",
    "DataError",
    "DECISION_DROP",
    "DECISION_FILTER",
    "DECISION_PASS",
    "DEFAULT_LIMITS",
    "evaluate",
    "FALSE",
    "Field",
    "FileEntry",
    "HpqError",
    "Issue",
    "iter_rows",
    "Leaf",
    "LimitError",
    "Limits",
    "load_manifest",
    "load_query",
    "Manifest",
    "Node",
    "Not",
    "Or",
    "parse_manifest",
    "parse_query",
    "PartitionField",
    "plan_file",
    "QueryShapeError",
    "resolve_projection",
    "row_matches",
    "run_plan",
    "run_scan",
    "Spec",
    "TargetExistsError",
    "to_json",
    "TRUE",
    "UNKNOWN",
]
