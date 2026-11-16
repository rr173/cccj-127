"""契约编译（预检）：资源清单、嵌套 $id、$ref 解析、形状校验、同位置环拒绝。

离线资源组织：

- 资源清单把绝对 URI（可为 https 等离线标识）绑定到本地文件，文件路径
  只负责取字节，不构成资源身份；
- 契约里的嵌套 $id 创建新的解析基址与资源根；
- $ref 按所在位置的基址解析，支持相对 URI、空片段、JSON Pointer 片段；
- 同一绝对资源 URI 被两处不同定义占用、悬空引用、坏 Pointer、不支持的
  片段、关键字形状不合法、同位置引用环，全部在处理来件之前拒绝。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urldefrag, urljoin, urlsplit

from .errors import ContractError
from .pointers import PointerError, escape_token, fragment_tokens

__all__ = ["Resource", "Compiled", "compile_contract", "TYPE_NAMES", "KNOWN_KEYWORDS"]

TYPE_NAMES = ("null", "boolean", "object", "array", "string", "number", "integer")

ANNOTATION_KEYWORDS = ("title", "description", "$comment")

KNOWN_KEYWORDS = frozenset({
    "$id", "$schema", "$defs", "$ref",
    "type", "const", "enum",
    "required", "properties", "additionalProperties", "dependentSchemas",
    "unevaluatedProperties",
    "prefixItems", "items", "contains", "minContains", "maxContains",
    "unevaluatedItems",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "title", "description", "$comment",
})

# 值为单个子契约的关键字
_SCHEMA_VALUE = ("additionalProperties", "unevaluatedProperties", "items",
                 "contains", "unevaluatedItems", "not", "if", "then", "else")
# 值为子契约数组的关键字
_SCHEMA_ARRAY = ("prefixItems", "allOf", "anyOf", "oneOf")
# 值为「名字 → 子契约」的关键字
_SCHEMA_MAP = ("$defs", "properties", "dependentSchemas")


@dataclass
class Resource:
    uri: str
    root: object
    origin: str  # 来源文件（诊断用）


@dataclass
class Compiled:
    resources: dict       # 绝对 URI -> Resource
    ref_targets: dict     # id(含 $ref 的契约对象) -> (目标节点, 资源 URI, 契约 Pointer)


class _DupKey(Exception):
    pass


def _strict_object(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise _DupKey(k)
        obj[k] = v
    return obj


def _navigate(root, tokens):
    node = root
    for tok in tokens:
        if isinstance(node, dict):
            if tok not in node:
                raise KeyError(tok)
            node = node[tok]
        elif isinstance(node, list):
            if tok == "0":
                idx = 0
            elif tok and tok[0] in "123456789" and tok.isascii() and tok.isdigit():
                idx = int(tok)
            else:
                raise KeyError(tok)
            if idx >= len(node):
                raise KeyError(tok)
            node = node[idx]
        else:
            raise KeyError(tok)
    return node


def _cyclic_sccs(edges):
    """迭代 Tarjan，返回所有成环的强连通分量（大小>1 或带自环）。"""
    index = {}
    lowlink = {}
    onstack = set()
    stack = []
    result = []
    counter = 0
    for start in edges:
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            v, ci = work[-1]
            if ci == 0:
                index[v] = lowlink[v] = counter
                counter += 1
                stack.append(v)
                onstack.add(v)
            descend = False
            children = edges.get(v, ())
            while ci < len(children):
                w = children[ci]
                ci += 1
                if w not in index:
                    work[-1] = (v, ci)
                    work.append((w, 0))
                    descend = True
                    break
                if w in onstack:
                    lowlink[v] = min(lowlink[v], index[w])
            if descend:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])
            if lowlink[v] == index[v]:
                scc = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1 or scc[0] in edges.get(scc[0], ()):
                    result.append(scc)
    return result


class _Builder:
    def __init__(self):
        self.problems = []
        self.resources = {}
        self.locations = {}   # id(node) -> (resource_uri, pointer)，仅预检期使用
        self.nodes = []
        self.refs = []        # (含 $ref 的契约对象, 该位置基址)
        self.ref_targets = {}

    def problem(self, message, resource=None, pointer=None, file=None):
        d = {"message": message}
        if resource is not None:
            d["resource"] = resource
        if pointer is not None:
            d["pointer"] = pointer
        if file is not None:
            d["file"] = file
        self.problems.append(d)

    def register(self, uri, node, origin):
        old = self.resources.get(uri)
        if old is not None and old.root is not node:
            self.problem(f"同一绝对资源 URI 被两处不同定义占用: {uri}", resource=uri)
            return
        self.resources[uri] = Resource(uri, node, origin)

    # ------------------------------------------------------------ 结构遍历
    def walk(self, node, resource_uri, base_uri, pointer, origin):
        if isinstance(node, dict):
            sid = node.get("$id")
            if isinstance(sid, str):
                if urlsplit(sid).fragment != "":
                    self.problem("$id 不允许携带片段", resource_uri, pointer)
                else:
                    new_uri = urldefrag(urljoin(base_uri, sid))[0]
                    resource_uri = new_uri
                    base_uri = new_uri
                    pointer = ""
                    self.register(new_uri, node, origin)
        self.nodes.append(node)
        self.locations[id(node)] = (resource_uri, pointer)
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            self.problem("契约节点必须是布尔值或对象", resource_uri, pointer)
            return
        self.check_keywords(node, resource_uri, pointer)
        if isinstance(node.get("$ref"), str):
            self.refs.append((node, base_uri))
        for kw in _SCHEMA_VALUE:
            sub = node.get(kw)
            if isinstance(sub, (bool, dict)):
                self.walk(sub, resource_uri, base_uri,
                          f"{pointer}/{escape_token(kw)}", origin)
        for kw in _SCHEMA_ARRAY:
            arr = node.get(kw)
            if isinstance(arr, list):
                for i, sub in enumerate(arr):
                    if isinstance(sub, (bool, dict)):
                        self.walk(sub, resource_uri, base_uri,
                                  f"{pointer}/{escape_token(kw)}/{i}", origin)
        for kw in _SCHEMA_MAP:
            m = node.get(kw)
            if isinstance(m, dict):
                for name, sub in m.items():
                    if isinstance(sub, (bool, dict)):
                        self.walk(sub, resource_uri, base_uri,
                                  f"{pointer}/{escape_token(kw)}/{escape_token(name)}",
                                  origin)

    # ------------------------------------------------------------ 形状校验
    def check_keywords(self, node, res, ptr):
        for kw in node:
            if kw not in KNOWN_KEYWORDS:
                self.problem(f"不支持的关键字 {kw!r}（超出本题契约子集，明确拒绝）",
                             res, f"{ptr}/{escape_token(kw)}")

        def bad(msg, kw):
            self.problem(msg, res, f"{ptr}/{escape_token(kw)}")

        if "$id" in node and not isinstance(node["$id"], str):
            bad("$id 必须是字符串", "$id")
        if "$schema" in node and not isinstance(node["$schema"], str):
            bad("$schema 必须是字符串", "$schema")
        for kw in ANNOTATION_KEYWORDS:
            if kw in node and not isinstance(node[kw], str):
                bad(f"{kw} 必须是字符串（注解关键字，不参与判定）", kw)
        if "$ref" in node and not isinstance(node["$ref"], str):
            bad("$ref 必须是字符串", "$ref")
        if "$defs" in node:
            v = node["$defs"]
            if not isinstance(v, dict):
                bad("$defs 必须是对象", "$defs")
            else:
                for name, sub in v.items():
                    if not isinstance(sub, (bool, dict)):
                        bad(f"$defs 的成员 {name!r} 必须是契约（布尔或对象）", "$defs")
        if "type" in node:
            v = node["type"]
            if isinstance(v, str):
                if v not in TYPE_NAMES:
                    bad(f"未知类型名 {v!r}", "type")
            elif isinstance(v, list):
                if not v:
                    bad("type 数组不能为空", "type")
                else:
                    seen = set()
                    for t in v:
                        if not isinstance(t, str) or t not in TYPE_NAMES:
                            bad(f"type 数组元素必须是合法类型名，得到 {t!r}", "type")
                            break
                        if t in seen:
                            bad(f"type 数组元素重复 {t!r}", "type")
                            break
                        seen.add(t)
            else:
                bad("type 必须是类型名或非空类型名数组", "type")
        if "enum" in node:
            v = node["enum"]
            if not isinstance(v, list) or not v:
                bad("enum 必须是非空数组", "enum")
        if "required" in node:
            v = node["required"]
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                bad("required 必须是字符串数组", "required")
            elif len(set(v)) != len(v):
                bad("required 存在重复字段名", "required")
        for kw in ("properties", "dependentSchemas"):
            if kw in node:
                v = node[kw]
                if not isinstance(v, dict):
                    bad(f"{kw} 必须是对象", kw)
                else:
                    for name, sub in v.items():
                        if not isinstance(sub, (bool, dict)):
                            bad(f"{kw} 的成员 {name!r} 必须是契约（布尔或对象）", kw)
        for kw in _SCHEMA_VALUE:
            if kw in node and not isinstance(node[kw], (bool, dict)):
                bad(f"{kw} 必须是契约（布尔或对象）", kw)
        if "prefixItems" in node:
            v = node["prefixItems"]
            if not isinstance(v, list) or not v:
                bad("prefixItems 必须是非空契约数组", "prefixItems")
            elif any(not isinstance(s, (bool, dict)) for s in v):
                bad("prefixItems 元素必须是契约（布尔或对象）", "prefixItems")
        for kw in ("allOf", "anyOf", "oneOf"):
            if kw in node:
                v = node[kw]
                if not isinstance(v, list) or not v:
                    bad(f"{kw} 必须是非空契约数组", kw)
                elif any(not isinstance(s, (bool, dict)) for s in v):
                    bad(f"{kw} 元素必须是契约（布尔或对象）", kw)
        for kw in ("minContains", "maxContains"):
            if kw not in node:
                continue
            v = node[kw]
            integral = (isinstance(v, int) and not isinstance(v, bool)) or (
                isinstance(v, Decimal) and v == v.to_integral_value())
            if not integral:
                bad(f"{kw} 必须是非负整数", kw)
            elif v < 0:
                bad(f"{kw} 必须是非负整数", kw)
            elif "contains" not in node:
                bad(f"{kw} 只能与 contains 一起出现", kw)

    # ------------------------------------------------------------ 引用解析
    def resolve_refs(self):
        for node, base in self.refs:
            res0, ptr0 = self.locations[id(node)]
            where = f"{ptr0}/$ref"
            ref = node["$ref"]
            doc, frag = urldefrag(urljoin(base, ref))
            resource = self.resources.get(doc)
            if resource is None:
                self.problem(f"悬空引用: {ref!r} 解析为未注册资源 {doc!r}", res0, where)
                continue
            if frag == "":
                self.ref_targets[id(node)] = (resource.root, resource.uri, "")
                continue
            try:
                tokens = fragment_tokens(frag)
            except PointerError as e:
                self.problem(f"不支持的片段: {e}", res0, where)
                continue
            try:
                target = _navigate(resource.root, tokens)
            except KeyError:
                self.problem(f"坏 Pointer: 片段 {frag!r} 指向不存在的契约位置",
                             res0, where)
                continue
            if not isinstance(target, (bool, dict)):
                self.problem("坏 Pointer: 片段目标不是契约节点", res0, where)
                continue
            canonical = self.locations.get(id(target))
            if canonical is not None and isinstance(target, dict):
                self.ref_targets[id(node)] = (target, canonical[0], canonical[1])
            else:
                # 布尔目标（单例）用本次导航路径定位
                target_ptr = ("/" + "/".join(escape_token(t) for t in tokens)
                              if tokens else "")
                self.ref_targets[id(node)] = (target, resource.uri, target_ptr)

    # ------------------------------------------------------------ 同位置环
    def check_cycles(self):
        edges = {}
        for node in self.nodes:
            if not isinstance(node, dict):
                continue
            out = []
            hit = self.ref_targets.get(id(node))
            if hit is not None:
                out.append(id(hit[0]))
            for kw in ("allOf", "anyOf", "oneOf"):
                v = node.get(kw)
                if isinstance(v, list):
                    out.extend(id(s) for s in v if isinstance(s, (bool, dict)))
            for kw in ("not", "if", "then", "else"):
                v = node.get(kw)
                if isinstance(v, (bool, dict)):
                    out.append(id(v))
            v = node.get("dependentSchemas")
            if isinstance(v, dict):
                out.extend(id(s) for s in v.values() if isinstance(s, (bool, dict)))
            if out:
                edges[id(node)] = out
        for scc in _cyclic_sccs(edges):
            res, ptr = self.locations[scc[0]]
            self.problem(
                f"引用与组合在同一实例位置构成环（未经对象属性或数组元素下降），"
                f"涉及 {len(scc)} 个契约节点，明确拒绝", res, ptr)


def compile_contract(manifest_path) -> Compiled:
    """读取资源清单并完成全部预检；任何问题都抛 ContractError。"""
    b = _Builder()
    manifest_path = os.fspath(manifest_path)
    try:
        with open(manifest_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ContractError([{"message": f"无法读取资源清单: {e}",
                              "file": manifest_path}]) from e
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except _DupKey as e:
        raise ContractError([{"message": f"资源清单存在重复成员: {e.args[0]!r}",
                              "file": manifest_path}]) from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ContractError([{"message": f"资源清单不是合法 UTF-8 JSON: {e}",
                              "file": manifest_path}]) from e
    if (not isinstance(manifest, dict)
            or not isinstance(manifest.get("resources"), dict)
            or not manifest["resources"]):
        raise ContractError([{"message": "资源清单必须是含非空 resources 对象的 JSON",
                              "file": manifest_path}])

    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    entries = []
    for uri, rel in manifest["resources"].items():
        if not isinstance(uri, str) or not isinstance(rel, str):
            b.problem("resources 的键与值都必须是字符串", file=manifest_path)
            continue
        if not urlsplit(uri).scheme:
            b.problem(f"资源 URI 不是绝对 URI: {uri!r}", file=manifest_path)
            continue
        if urlsplit(uri).fragment != "":
            b.problem(f"资源 URI 不允许携带片段: {uri!r}", file=manifest_path)
            continue
        entries.append((urldefrag(uri)[0], rel))
    if b.problems:
        raise ContractError(b.problems)

    for uri, rel in entries:
        path = rel if os.path.isabs(rel) else os.path.join(base_dir, rel)
        try:
            with open(path, "rb") as f:
                craw = f.read()
        except OSError as e:
            b.problem(f"无法读取契约文件: {e}", file=path)
            continue
        try:
            doc = json.loads(craw.decode("utf-8"),
                             parse_float=Decimal, parse_int=int)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            b.problem(f"契约文件不是合法 UTF-8 JSON: {e}", file=path)
            continue
        b.walk(doc, uri, uri, "", path)
        existing = b.resources.get(uri)
        if existing is None:
            b.resources[uri] = Resource(uri, doc, path)
        elif existing.root is not doc:
            b.problem(f"同一绝对资源 URI 被两处不同定义占用: {uri}", resource=uri)

    b.resolve_refs()
    b.check_cycles()
    if b.problems:
        raise ContractError(b.problems)
    return Compiled(resources=b.resources, ref_targets=b.ref_targets)
