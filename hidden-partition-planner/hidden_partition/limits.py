"""明示上限：单行字节、表达式深度/节点数、manifest/query 字节数。

目录与表达式允许整体放入内存，但所有上限都有明确默认值并可在 CLI/API 调整；
数据文件逐行处理，只有单行大小上限，没有总行数上限。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    # 单个数据文件一行的最大字节数（含换行符）。
    max_line_bytes: int = 1024 * 1024
    # 查询表达式树的最大嵌套深度。
    max_expr_depth: int = 64
    # 查询表达式树的最大节点数。
    max_expr_nodes: int = 10000
    # manifest 文件最大字节数。
    max_manifest_bytes: int = 256 * 1024 * 1024
    # 查询文件最大字节数。
    max_query_bytes: int = 16 * 1024 * 1024

    def check_positive(self) -> None:
        for name in ("max_line_bytes", "max_expr_depth", "max_expr_nodes",
                     "max_manifest_bytes", "max_query_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"上限 {name} 必须是正整数，实际：{value!r}")


DEFAULT_LIMITS = Limits()
