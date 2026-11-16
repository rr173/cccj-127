"""contract-gate：离线 JSONL 契约分流。

公开 API：
    Contract.from_manifest(manifest_path, root_uri) -> Contract
    contract.validate_line(body: bytes) -> LineResult

异常：
    ContractError  契约无法执行（预检问题清单在 .problems）
    RefCycleError  求值期引用环（保险；静态预检已拒绝同位置环）
"""
from __future__ import annotations

from .api import Contract, LineResult
from .errors import ContractError, RefCycleError
from . import jdoc, pointers

__all__ = ["Contract", "LineResult", "ContractError", "RefCycleError",
           "jdoc", "pointers"]
__version__ = "0.1.0"
