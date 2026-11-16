"""错误类型。

- ContractError：契约无法执行（清单、资源、形状、引用、环等预检问题）。
  携带结构化问题清单 problems，每个问题是 dict，可能含
  message / resource（契约资源 URI）/ pointer（契约内 JSON Pointer）/ file（本地文件）。
- RefCycleError：求值期捕获的引用环。静态预检已拒绝全部同位置环，
  此类只是保险，绝不以递归溢出收场。
"""
from __future__ import annotations


class ContractError(Exception):
    """契约无法执行。problems 为结构化问题清单。"""

    def __init__(self, problems):
        self.problems = [dict(p) for p in problems]
        summary = "; ".join(p.get("message", "?") for p in self.problems[:5])
        if len(self.problems) > 5:
            summary += f"；……共 {len(self.problems)} 处问题"
        super().__init__(summary or "契约无法执行")


class RefCycleError(Exception):
    """同一契约位置与同一实例位置未经下降绕回自身的引用环。"""
