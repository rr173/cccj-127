"""组合求值与覆盖传播（本项目自实现，不借助现成验证器）。

核心概念：一个契约对象在同一实例位置上，由各个「成功的」关键字/分支
贡献覆盖（对象属性名集合、数组下标集合）；unevaluatedProperties /
unevaluatedItems 在全部同位置贡献完成之后，检查尚未覆盖的部分。

传播规则：
- properties 认领实际存在且子契约判定成功的同名属性；additionalProperties
  只针对本层 properties 未列出的名字；
- $ref、allOf、dependentSchemas、实际选中的 then/else 成功时向外合并覆盖；
- anyOf 合并全部成功分支，失败分支不贡献；oneOf 恰好一个成功才贡献；
- not 永不贡献；if 成功时保留其覆盖，失败时不保留；
- if 缺席时 then/else 都忽略；if 存在但无对应分支时不凭空增加覆盖；
- 规则整体失败时其覆盖不外泄，但失败证据（分支身份、违反的关键字）保留；
- prefixItems/items/contains 按实际位置认领；contains 只认领命中的位置；
- 求值顺序与 JSON 成员顺序、契约关键字书写顺序无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import jdoc
from .errors import RefCycleError
from .pointers import escape_token

__all__ = ["Coverage", "Issue", "Branch", "Outcome", "Evaluator"]


@dataclass
class Coverage:
    props: set = field(default_factory=set)
    items: set = field(default_factory=set)

    def merge(self, other: "Coverage"):
        self.props |= other.props
        self.items |= other.items


@dataclass
class Branch:
    """组合规则下一个失败分支的证据。成功分支不会出现在错误树里。"""
    name: str
    resource: str
    pointer: str
    issues: list

    def to_dict(self):
        return {"name": self.name, "valid": False,
                "resource": self.resource, "pointer": self.pointer,
                "issues": [i.to_dict() for i in self.issues]}


@dataclass
class Issue:
    """一处判定违反。span 为原行字节左闭右开范围。"""
    keyword: str
    resource: str
    pointer: str
    instance: str
    message: str
    span: tuple = None
    branches: list = field(default_factory=list)

    def to_dict(self):
        d = {"keyword": self.keyword, "resource": self.resource,
             "pointer": self.pointer, "instance": self.instance,
             "span": list(self.span) if self.span is not None else None,
             "message": self.message}
        if self.branches:
            d["branches"] = [b.to_dict() for b in self.branches]
        return d


@dataclass
class Outcome:
    valid: bool
    coverage: Coverage
    errors: list


def _join(iptr: str, token: str) -> str:
    return f"{iptr}/{token}"


class Evaluator:
    def __init__(self, compiled):
        self.compiled = compiled

    def evaluate(self, node, inst, iptr, res, ptr, active) -> Outcome:
        """对实例 inst（JNode）在位置 iptr 求值契约 node（位于 res/ptr）。"""
        if node is True:
            return Outcome(True, Coverage(), [])
        if node is False:
            return Outcome(False, Coverage(), [Issue(
                "false", res, ptr, iptr, "布尔契约 false 不允许任何实例",
                (inst.start, inst.end))])

        schema = node
        errors = []
        cov = Coverage()
        span = (inst.start, inst.end)

        # ---------------- 断言类关键字（不产生覆盖） ----------------
        t = schema.get("type")
        if t is not None:
            names = [t] if isinstance(t, str) else list(t)
            if not any(jdoc.type_matches(inst, nm) for nm in names):
                errors.append(Issue(
                    "type", res, f"{ptr}/type", iptr,
                    f"期望类型 {'/'.join(names)}，实际为 {jdoc.node_type(inst)}",
                    span))
        if "const" in schema and not jdoc.value_equal(inst, schema["const"]):
            errors.append(Issue("const", res, f"{ptr}/const", iptr,
                                "值不等于 const 约定", span))
        if "enum" in schema and not any(
                jdoc.value_equal(inst, v) for v in schema["enum"]):
            errors.append(Issue("enum", res, f"{ptr}/enum", iptr,
                                "值不在 enum 约定中", span))

        # ---------------- $ref（兄弟关键字照常求值） ----------------
        if "$ref" in schema:
            tnode, tres, tptr = self.compiled.ref_targets[id(schema)]
            key = (id(tnode), id(inst))
            if key in active:
                raise RefCycleError(
                    f"$ref 在同一契约位置与同一实例位置绕回自身: {tres}#{tptr}")
            r = self.evaluate(tnode, inst, iptr, tres, tptr, active | {key})
            if r.valid:
                cov.merge(r.coverage)
            else:
                errors.append(Issue(
                    "$ref", res, f"{ptr}/$ref", iptr, "引用目标判定失败", span,
                    [Branch("$ref", tres, tptr, r.errors)]))

        # ---------------- allOf / anyOf / oneOf / not ----------------
        subs = schema.get("allOf")
        if subs:
            for i, sub in enumerate(subs):
                r = self.evaluate(sub, inst, iptr, res, f"{ptr}/allOf/{i}", active)
                if r.valid:
                    cov.merge(r.coverage)
                else:
                    errors.append(Issue(
                        "allOf", res, f"{ptr}/allOf", iptr,
                        f"allOf 第 {i} 分支判定失败", span,
                        [Branch(f"allOf[{i}]", res, f"{ptr}/allOf/{i}", r.errors)]))
        subs = schema.get("anyOf")
        if subs:
            failed = []
            for i, sub in enumerate(subs):
                r = self.evaluate(sub, inst, iptr, res, f"{ptr}/anyOf/{i}", active)
                if r.valid:
                    cov.merge(r.coverage)
                else:
                    failed.append(
                        Branch(f"anyOf[{i}]", res, f"{ptr}/anyOf/{i}", r.errors))
            if len(failed) == len(subs):
                errors.append(Issue("anyOf", res, f"{ptr}/anyOf", iptr,
                                    "anyOf 没有任何分支成功", span, failed))
        subs = schema.get("oneOf")
        if subs:
            results = [self.evaluate(sub, inst, iptr, res, f"{ptr}/oneOf/{i}", active)
                       for i, sub in enumerate(subs)]
            valids = [i for i, r in enumerate(results) if r.valid]
            if len(valids) == 1:
                cov.merge(results[valids[0]].coverage)
            elif not valids:
                errors.append(Issue(
                    "oneOf", res, f"{ptr}/oneOf", iptr,
                    "oneOf 没有任何分支成功", span,
                    [Branch(f"oneOf[{i}]", res, f"{ptr}/oneOf/{i}", r.errors)
                     for i, r in enumerate(results) if not r.valid]))
            else:
                errors.append(Issue(
                    "oneOf", res, f"{ptr}/oneOf", iptr,
                    f"oneOf 有 {len(valids)} 个分支成功"
                    f"（第 {'、'.join(map(str, valids))} 个），要求恰好一个", span))
        if "not" in schema:
            r = self.evaluate(schema["not"], inst, iptr, res, f"{ptr}/not", active)
            if r.valid:
                errors.append(Issue("not", res, f"{ptr}/not", iptr,
                                    "实例不应通过 not 子契约", span))

        # ---------------- if / then / else ----------------
        if "if" in schema:
            rif = self.evaluate(schema["if"], inst, iptr, res, f"{ptr}/if", active)
            if rif.valid:
                cov.merge(rif.coverage)  # 条件成功：按标准保留其覆盖
                if "then" in schema:
                    rt = self.evaluate(schema["then"], inst, iptr, res,
                                       f"{ptr}/then", active)
                    if rt.valid:
                        cov.merge(rt.coverage)
                    else:
                        errors.append(Issue(
                            "then", res, f"{ptr}/then", iptr,
                            "if 成立时 then 分支判定失败", span,
                            [Branch("then", res, f"{ptr}/then", rt.errors)]))
            else:
                # 条件失败：不保留其覆盖，只执行实际选中的 else
                if "else" in schema:
                    re_ = self.evaluate(schema["else"], inst, iptr, res,
                                        f"{ptr}/else", active)
                    if re_.valid:
                        cov.merge(re_.coverage)
                    else:
                        errors.append(Issue(
                            "else", res, f"{ptr}/else", iptr,
                            "if 不成立时 else 分支判定失败", span,
                            [Branch("else", res, f"{ptr}/else", re_.errors)]))
        # if 缺席时 then/else 都忽略；if 存在但无对应分支时不凭空增加覆盖

        # ---------------- 对象关键字（非对象不施加约束） ----------------
        if inst.kind == "obj":
            self._object_keywords(schema, inst, iptr, res, ptr, active, cov, errors)

        # ---------------- 数组关键字（非数组不施加约束） ----------------
        if inst.kind == "arr":
            self._array_keywords(schema, inst, iptr, res, ptr, active, cov, errors)

        # ---------------- 剩余字段/剩余元素（最后检查） ----------------
        if inst.kind == "obj" and "unevaluatedProperties" in schema:
            sub = schema["unevaluatedProperties"]
            for m in inst.value:
                if m.key in cov.props:
                    continue
                r = self.evaluate(sub, m.value, _join(iptr, escape_token(m.key)),
                                  res, f"{ptr}/unevaluatedProperties", active)
                if r.valid:
                    cov.props.add(m.key)
                else:
                    errors.append(Issue(
                        "unevaluatedProperties", res, f"{ptr}/unevaluatedProperties",
                        _join(iptr, escape_token(m.key)),
                        f"字段 {m.key!r} 未被任何成功规则覆盖",
                        (m.key_start, m.value.end),
                        [Branch("unevaluatedProperties", res,
                                f"{ptr}/unevaluatedProperties", r.errors)]))
        if inst.kind == "arr" and "unevaluatedItems" in schema:
            sub = schema["unevaluatedItems"]
            for i, el in enumerate(inst.value):
                if i in cov.items:
                    continue
                r = self.evaluate(sub, el, f"{iptr}/{i}",
                                  res, f"{ptr}/unevaluatedItems", active)
                if r.valid:
                    cov.items.add(i)
                else:
                    errors.append(Issue(
                        "unevaluatedItems", res, f"{ptr}/unevaluatedItems",
                        f"{iptr}/{i}", f"第 {i} 个元素未被任何成功规则覆盖",
                        (el.start, el.end),
                        [Branch("unevaluatedItems", res,
                                f"{ptr}/unevaluatedItems", r.errors)]))

        return Outcome(not errors, cov, errors)

    # ------------------------------------------------------------ 对象
    def _object_keywords(self, schema, inst, iptr, res, ptr, active, cov, errors):
        span = (inst.start, inst.end)
        req = schema.get("required")
        if req:
            for name in req:
                if name not in inst.index:
                    errors.append(Issue(
                        "required", res, f"{ptr}/required", iptr,
                        f"缺少必需字段 {name!r}", span))
        props = schema.get("properties")
        if props:
            for name, sub in props.items():
                m = inst.index.get(name)
                if m is None:
                    continue
                r = self.evaluate(sub, m.value, _join(iptr, escape_token(name)),
                                  res, f"{ptr}/properties/{escape_token(name)}",
                                  active)
                if r.valid:
                    cov.props.add(name)
                else:
                    errors.append(Issue(
                        "properties", res, f"{ptr}/properties", iptr,
                        f"字段 {name!r} 判定失败", (m.key_start, m.value.end),
                        [Branch(f"properties[{name!r}]", res,
                                f"{ptr}/properties/{escape_token(name)}",
                                r.errors)]))
        if "additionalProperties" in schema:
            declared = set(props) if props else set()
            sub = schema["additionalProperties"]
            for m in inst.value:
                if m.key in declared:
                    continue
                r = self.evaluate(sub, m.value, _join(iptr, escape_token(m.key)),
                                  res, f"{ptr}/additionalProperties", active)
                if r.valid:
                    cov.props.add(m.key)
                else:
                    errors.append(Issue(
                        "additionalProperties", res, f"{ptr}/additionalProperties",
                        iptr, f"额外字段 {m.key!r} 判定失败",
                        (m.key_start, m.value.end),
                        [Branch(f"additionalProperties[{m.key!r}]", res,
                                f"{ptr}/additionalProperties", r.errors)]))
        deps = schema.get("dependentSchemas")
        if deps:
            for trig, sub in deps.items():
                if trig not in inst.index:
                    continue
                r = self.evaluate(sub, inst, iptr, res,
                                  f"{ptr}/dependentSchemas/{escape_token(trig)}",
                                  active)
                if r.valid:
                    cov.merge(r.coverage)
                else:
                    errors.append(Issue(
                        "dependentSchemas", res, f"{ptr}/dependentSchemas", iptr,
                        f"字段 {trig!r} 触发的 dependentSchemas 判定失败", span,
                        [Branch(f"dependentSchemas[{trig!r}]", res,
                                f"{ptr}/dependentSchemas/{escape_token(trig)}",
                                r.errors)]))

    # ------------------------------------------------------------ 数组
    def _array_keywords(self, schema, inst, iptr, res, ptr, active, cov, errors):
        items = inst.value
        span = (inst.start, inst.end)
        pre = schema.get("prefixItems")
        npre = 0
        if pre:
            npre = len(pre)
            for i, sub in enumerate(pre):
                if i >= len(items):
                    break
                el = items[i]
                r = self.evaluate(sub, el, f"{iptr}/{i}",
                                  res, f"{ptr}/prefixItems/{i}", active)
                if r.valid:
                    cov.items.add(i)
                else:
                    errors.append(Issue(
                        "prefixItems", res, f"{ptr}/prefixItems", iptr,
                        f"第 {i} 个元素判定失败", (el.start, el.end),
                        [Branch(f"prefixItems[{i}]", res,
                                f"{ptr}/prefixItems/{i}", r.errors)]))
        if "items" in schema:
            sub = schema["items"]
            for i in range(npre, len(items)):
                el = items[i]
                r = self.evaluate(sub, el, f"{iptr}/{i}", res, f"{ptr}/items",
                                  active)
                if r.valid:
                    cov.items.add(i)
                else:
                    errors.append(Issue(
                        "items", res, f"{ptr}/items", iptr,
                        f"第 {i} 个元素判定失败", (el.start, el.end),
                        [Branch(f"items[{i}]", res, f"{ptr}/items", r.errors)]))
        if "contains" in schema:
            sub = schema["contains"]
            matched = []
            failed = []
            for i, el in enumerate(items):
                r = self.evaluate(sub, el, f"{iptr}/{i}", res, f"{ptr}/contains",
                                  active)
                if r.valid:
                    matched.append(i)
                else:
                    failed.append(Branch(f"contains 元素 {i}", res,
                                         f"{ptr}/contains", r.errors))
            minc = schema.get("minContains", 1)
            maxc = schema.get("maxContains")
            if len(matched) < minc:
                errors.append(Issue(
                    "contains", res, f"{ptr}/contains", iptr,
                    f"contains 命中 {len(matched)} 个元素，少于 minContains {minc}",
                    span, failed))
            elif maxc is not None and len(matched) > maxc:
                errors.append(Issue(
                    "maxContains", res, f"{ptr}/maxContains", iptr,
                    f"contains 命中 {len(matched)} 个元素，超过 maxContains {maxc}",
                    span))
            else:
                cov.items.update(matched)
