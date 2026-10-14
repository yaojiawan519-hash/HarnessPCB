"""s-表达式薄封装（M1 定案：直用 sexpdata 库，不用 kicad-skip）。

kicad-skip 冒烟结论（2026-09-08）：读 v10 能活但停在 2024、未知键告警 60 处、
写路径是正则字符串手术（re.sub 插换行）——文件损坏温床，不采用。
sexpdata 是零依赖纯解析库，typed 提取自己写，写出（M2/M3）自己构造树。

KiCad sch 的 s-expr 里：符号（symbol）是标识符、带引号的是字符串、
裸数字是 int/float、true/false 是 Symbol。统一在本层做归一。
"""

from __future__ import annotations

from typing import Any, Iterator

import sexpdata


def loads(text: str) -> list:
    """解析文本 → 顶层表达式列表（.kicad_sch 只有一个顶层 (kicad_sch ...)）。"""
    return sexpdata.loads(text)


def tag(node: Any) -> str | None:
    """节点首元素为符号 → 标签名（如 'wire'）；否则 None（原子）。"""
    if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol):
        return str(node[0])
    return None


def attr(node: list, name: str) -> Any | None:
    """取子节点 (name value...) 的第二元素：attr(wire, 'uuid') → str。"""
    for kid in node[1:]:
        if isinstance(kid, list) and tag(kid) == name:
            return _value(kid[1]) if len(kid) > 1 else True
    return None


def children(node: list, name: str) -> Iterator[list]:
    """遍历直接子节点中 tag == name 的节点。"""
    for kid in node[1:]:
        if isinstance(kid, list) and tag(kid) == name:
            yield kid


def _value(raw: Any) -> Any:
    if isinstance(raw, sexpdata.Symbol):
        return str(raw)
    return raw


def num(raw: Any, default: float = 0.0) -> float:
    """容错取数：KiCad 坐标/角度有时是 int 有时是 float。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
