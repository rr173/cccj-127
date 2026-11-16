"""单条记录判定 API。

    from contract_gate import Contract

    contract = Contract.from_manifest("examples/manifest.json",
                                      "https://acme.example/schemas/root.json")
    result = contract.validate_line(b'{"id":"A","kind":"order"}')
    result.ok        # 是否合格
    result.category  # ok / contract / json / utf8 / duplicate / empty
    result.issues    # contract 类别的错误树（Issue 列表）
    result.coverage  # 合格时根对象/数组的覆盖集合 {"properties": [...], "items": [...]}
    result.parse     # 解析类错误的类别与字节位置
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urldefrag, urlsplit

from . import jdoc
from .contract import compile_contract
from .errors import ContractError
from .evaluate import Evaluator

__all__ = ["Contract", "LineResult"]


@dataclass
class LineResult:
    ok: bool
    category: str                 # ok | contract | json | utf8 | duplicate | empty
    issues: list = None           # category == "contract" 时的错误树
    coverage: dict = None         # ok 时根对象/数组的覆盖集合
    parse: dict = None            # 解析类错误的定位信息

    def rejection_record(self, fname: str, lineno: int) -> dict:
        """生成 rejected.jsonl 中的一行（不含业务载荷本身）。"""
        rec = {"file": fname, "line": lineno, "category": self.category}
        if self.category == "contract":
            first = self.issues[0]
            rec["resource"] = first.resource
            rec["pointer"] = first.pointer
            rec["error"] = {"issues": [i.to_dict() for i in self.issues]}
        else:
            # 尚未进入契约求值：契约位置明确记为 null，不伪造契约来源
            rec["resource"] = None
            rec["pointer"] = None
            rec["error"] = self.parse
        return rec


class Contract:
    """已编译契约 + 选定根资源。构造即完成全部预检。"""

    def __init__(self, compiled, root_uri):
        self.compiled = compiled
        self.root_uri = root_uri
        self.root_node = compiled.resources[root_uri].root
        self._evaluator = Evaluator(compiled)

    @classmethod
    def from_manifest(cls, manifest_path, root_uri) -> "Contract":
        if not urlsplit(root_uri).scheme:
            raise ContractError([{"message": f"根契约 URI 不是绝对 URI: {root_uri!r}"}])
        if urlsplit(root_uri).fragment != "":
            raise ContractError([{"message": f"根契约 URI 不允许携带片段: {root_uri!r}"}])
        root_uri = urldefrag(root_uri)[0]
        compiled = compile_contract(manifest_path)
        if root_uri not in compiled.resources:
            raise ContractError([{
                "message": f"根契约 URI 未在资源清单/契约中注册: {root_uri!r}",
                "resource": root_uri}])
        return cls(compiled, root_uri)

    def validate_line(self, body) -> LineResult:
        """判定单条记录（一行 UTF-8 字节，不含行尾换行）。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            node = jdoc.parse_line(body)
        except jdoc.LineError as e:
            return LineResult(False, e.category, parse=e.to_dict())
        outcome = self._evaluator.evaluate(
            self.root_node, node, "", self.root_uri, "", frozenset())
        if outcome.valid:
            return LineResult(True, "ok", coverage={
                "properties": sorted(outcome.coverage.props),
                "items": sorted(outcome.coverage.items)})
        return LineResult(False, "contract", issues=outcome.errors)
