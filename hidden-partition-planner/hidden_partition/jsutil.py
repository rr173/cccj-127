"""严格 JSON 读写工具。

读取侧：UTF-8 严格解码、拒绝重复键、拒绝 NaN/Infinity 等非标准常量、
限制输入字节数。写出侧：临时文件 + 原子链接，目标已存在绝不覆盖，
失败时临时文件清理掉，不会留下半份结果。
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from .errors import LimitError, TargetExistsError


def _reject_constant(name: str) -> Any:
    raise ValueError(f"非标准 JSON 常量：{name}")


def _no_duplicate_object(pairs: list) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"重复 JSON 键：{key!r}")
        obj[key] = value
    return obj


def loads_strict(text: str) -> Any:
    """按本模块合同解析 JSON：拒绝重复键与非标准常量。"""
    return json.loads(text, object_pairs_hook=_no_duplicate_object,
                      parse_constant=_reject_constant)


def load_bytes(path: str, max_bytes: int, *, what: str) -> bytes:
    """以只读方式载入整个文件，限制字节数，严格校验 UTF-8 之前先限大小。"""
    with open(path, "rb") as fh:
        blob = fh.read(max_bytes + 1)
    if len(blob) > max_bytes:
        raise LimitError(f"{what}超过 {max_bytes} 字节上限", location=path)
    return blob


def load_json_file(path: str, max_bytes: int, *, what: str) -> Any:
    """读取并严格解析一个 JSON 文件（manifest / query）。"""
    blob = load_bytes(path, max_bytes, what=what)
    try:
        text = blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{what}不是合法 UTF-8：{path}（{exc}）") from exc
    try:
        return loads_strict(text)
    except ValueError as exc:
        raise ValueError(f"{what}不是合法 JSON：{path}（{exc}）") from exc


def dumps_canonical(obj: Any) -> str:
    """模块内统一输出格式：保留非 ASCII 字符与键序，紧凑分隔。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _link_or_rename(tmp_path: str, final_path: str) -> None:
    """把临时文件原子地落到最终路径；已存在则报 TargetExistsError。"""
    try:
        os.link(tmp_path, final_path)
    except FileExistsError as exc:
        raise TargetExistsError(final_path) from exc
    except OSError:
        # 极少数文件系统不支持硬链接：退回先检查后改名（进程外竞争由调用方自担）。
        if os.path.exists(final_path):
            raise TargetExistsError(final_path)
        os.replace(tmp_path, final_path)
        return
    os.unlink(tmp_path)


def write_text_atomic(path: str, write_fn: Callable[[Any], None],
                      *, encoding: str = "utf-8") -> None:
    """在目标同目录写临时文件，成功后原子落位；失败清理临时文件。

    ``write_fn`` 拿到以 ``encoding`` 打开的文本句柄，负责写全部内容。
    目标路径已存在时抛出 :class:`TargetExistsError`，原文件字节不动。
    """
    if os.path.exists(path):
        raise TargetExistsError(path)
    directory = os.path.dirname(os.path.abspath(path))
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    try:
        with open(tmp_path, "x", encoding=encoding, newline="\n") as fh:
            write_fn(fh)
            fh.flush()
            os.fsync(fh.fileno())
        _link_or_rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
