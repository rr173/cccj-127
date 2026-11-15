"""关系装载工具（relation handoff loader）。

把一次多表 UTF-8 JSONL 交接，按整表交接语义写进已有 PostgreSQL 仓库。
对外主入口见 :mod:`handoff.loader`，命令行入口见 :mod:`handoff.cli`。
"""
from .contract import Contract, RefSpec, TableSpec, load_contract
from .errors import HandoffError
from .loader import LoadReport, TableReport, load_relation_handoff

__all__ = [
    "Contract",
    "TableSpec",
    "RefSpec",
    "load_contract",
    "HandoffError",
    "load_relation_handoff",
    "LoadReport",
    "TableReport",
]

__version__ = "1.0.0"
