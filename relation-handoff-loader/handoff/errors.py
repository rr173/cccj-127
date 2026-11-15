"""带完整定位信息的诊断异常。

诊断始终携带逻辑表名、来源文件、物理行号（文件内 1 起算，与是否为空行无关）
以及具体字段/引用名，便于操作员直接定位到交接文件里的问题位置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class HandoffError(Exception):
    """所有可预期的装载失败统一抛出的异常类型。

    任何抛出该异常的装载都不会提交：调用方观察到的目标库仍保持完整旧关系。
    """

    message: str
    logical_table: str | None = None
    file: str | None = None
    line: int | None = None
    field: str | None = None
    detail: Any = None

    def __str__(self) -> str:
        parts = [self.message]
        loc = self._location()
        if loc:
            parts.append(f"位置: {loc}")
        if self.field:
            parts.append(f"字段/引用: {self.field!r}")
        if self.detail is not None:
            parts.append(f"详情: {self.detail!r}")
        return "；".join(parts)

    def _location(self) -> str:
        bits: list[str] = []
        if self.logical_table is not None:
            bits.append(f"逻辑表 {self.logical_table!r}")
        if self.file is not None:
            bits.append(f"文件 {self.file!r}")
        if self.line is not None:
            bits.append(f"物理行 {self.line}")
        return "，".join(bits)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message}
        if self.logical_table is not None:
            out["logical_table"] = self.logical_table
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        if self.field is not None:
            out["field"] = self.field
        if self.detail is not None:
            out["detail"] = self.detail
        return out
