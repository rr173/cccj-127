"""失败类型与定位信息。

约定：
- 目录（manifest）与查询表达式的问题在规划任何文件之前一次性全部报出，
  每个问题都带 JSON 位置（``$.files[2].partitions["1"]`` 这类路径）；
- 数据文件的问题在 scan 执行中报出，带 file_id / 路径 / 行号；
- 目标路径（计划、结果、报告）已存在时拒绝覆盖，退出码 3。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Issue:
    """一条可定位的输入问题。"""

    location: str
    reason: str

    def to_json(self) -> dict:
        return {"location": self.location, "reason": self.reason}


class HpqError(Exception):
    """本模块错误的基类。"""

    def to_json(self) -> dict:  # pragma: no cover - 子类覆盖
        return {"ok": False, "error": type(self).__name__, "detail": str(self)}


class CatalogError(HpqError):
    """manifest 违反输入合同；携带全部问题，规划前报出。"""

    def __init__(self, issues: List[Issue]):
        self.issues = list(issues)
        summary = "；".join(f"{i.location}: {i.reason}" for i in self.issues[:5])
        if len(self.issues) > 5:
            summary += f"；……共 {len(self.issues)} 处"
        super().__init__(f"目录不符合输入合同（{len(self.issues)} 处）：{summary}")

    def to_json(self) -> dict:
        return {"ok": False, "error": "catalog",
                "issues": [i.to_json() for i in self.issues]}


class QueryShapeError(HpqError):
    """查询表达式违反输入合同；携带全部问题与表达式位置。"""

    def __init__(self, issues: List[Issue]):
        self.issues = list(issues)
        summary = "；".join(f"{i.location}: {i.reason}" for i in self.issues[:5])
        if len(self.issues) > 5:
            summary += f"；……共 {len(self.issues)} 处"
        super().__init__(f"查询表达式不符合输入合同（{len(self.issues)} 处）：{summary}")

    def to_json(self) -> dict:
        return {"ok": False, "error": "query",
                "issues": [i.to_json() for i in self.issues]}


class DataError(HpqError):
    """scan 打开的数据文件不符合约定；首错即停。"""

    def __init__(self, reason: str, *, file_id: Optional[str] = None,
                 path: Optional[str] = None, line: Optional[int] = None,
                 detail: Optional[str] = None):
        self.reason = reason
        self.file_id = file_id
        self.path = path
        self.line = line
        self.detail = detail
        parts = [reason]
        if file_id is not None:
            parts.append(f"file_id={file_id}")
        if path is not None:
            parts.append(f"路径={path}")
        if line is not None:
            parts.append(f"行号={line}")
        if detail:
            parts.append(f"明细：{detail}")
        super().__init__("；".join(parts))

    def to_json(self) -> dict:
        return {"ok": False, "error": "data", "reason": self.reason,
                "file_id": self.file_id, "path": self.path,
                "line": self.line, "detail": self.detail}


class TargetExistsError(HpqError):
    """目标路径（计划/结果/报告）已存在；按约定拒绝覆盖。"""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"目标路径已存在，拒绝覆盖：{path}")

    def to_json(self) -> dict:
        return {"ok": False, "error": "target_exists", "path": self.path}


class LimitError(HpqError):
    """超过明示上限（单行字节、表达式深度/节点数、输入文件大小等）。"""

    def __init__(self, reason: str, *, location: Optional[str] = None):
        self.reason = reason
        self.location = location
        where = f"（{location}）" if location else ""
        super().__init__(f"超过上限{where}：{reason}")

    def to_json(self) -> dict:
        return {"ok": False, "error": "limit", "reason": self.reason,
                "location": self.location}
