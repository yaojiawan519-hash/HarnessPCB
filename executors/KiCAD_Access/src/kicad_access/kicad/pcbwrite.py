"""placed-unrouted 板导出（M2 验收）：模板板定点文本手术。

括号配平扫描（字符串感知，``\\"`` 转义）定位 ``(kicad_pcb ...)`` 根的
直接子块，按块分治：

- ``footprint``：按位号改写直属 ``(at x y [rot])`` 与 ``(layer …)``；
  pad 坐标是封装本地系（M2-1 用真板 J5 校准过），随 ``at`` 自动变换——
  但 pad 的 ``(at x y angle)`` 角度 token 是**板绝对系**（KiCad 方言：
  位置本地、角度绝对，60 板逐盘对账钉死），旋转封装必须同步 token
  += Δrot，否则 KiCad 渲染盘不随封转、板无效；其余字节原样；
- ``segment``/``arc``/``via``/``zone``/``group``：整块剔除——模板是
  参考板，旧铜箔/网络铜皮/分组引用与新放置无关，留 = 假 unrouted；
  footprint 块内嵌的 ``zone`` 同剥（顶层扫描够不到的模板作者铜皮）；
- ``Edge.Cuts`` 板框图形：平移到 floorplan 本地系（原点 = 板框左下，
  与 spec 的 size/mount_holes/锚点同系）——放置坐标是本地系，板框
  留在模板绝对系会让焊盘整板落在板框外；
- 其余（setup/layers/nets/其他层图形…）原样保留。

不做全树重序列化（sexpdata 写出会重排格式、diff 不可读）——定点
替换、其余字节保真。位号在模板缺失 → ValueError（写回必须完备）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # 鸭子类型即可，仅注解引用（避免 kicad→place 层级依赖）
    from ..place.geometry import Placement

STRIP_TAGS = frozenset({"segment", "arc", "via", "zone", "group"})  # 布线/铜箔/分组引用
_IDENT = re.compile(r"[A-Za-z0-9_\-!?*]*")


@dataclass(frozen=True)
class FootprintSwap:
    """封装换块（part_overrides 落盘）：模板块 → 库模块实例化。

    module_text = .kicad_mod 原文（调用方经 FootprintLibs.path_of 读入）；
    pad_nets = 盘号 → 网名（IR 净表；不在表的盘不配网 = 真悬空）。
    """

    lib_id: str
    module_text: str
    value: str
    pad_nets: dict[str, str]


def _legacy_text_to_property(text: str, ref: str, value: str) -> str:
    """fp_text reference/value/user → KiCad10 property 节点。

    库模块多 legacy 方言（fp_text/tstamp）——KiCad 打开时会自转换，但
    本仓读板器只认 property（route/model.py），位号读成 "?" 会让按封装
    分策的链路（BGA 扇出/盘中孔）全失明。reference → "Reference"（位号
    重定），value → "Value"，user 原文透传成 "User"；tstamp 标签改
    uuid（板方言带引号），at 尾巴 unlocked 去掉。property 方言零改动。
    """
    edits: list[tuple[int, int, str]] = []
    for tag, s, e in _iter_blocks(text, base_depth=1):
        if tag != "fp_text":
            continue
        blk = text[s:e]
        m = re.match(
            r'\(\s*fp_text\s+(reference|value|user)\s+"((?:[^"\\]|\\.)*)"', blk)
        if not m:
            continue
        kind = {"reference": "Reference", "value": "Value"}.get(m.group(1), "User")
        val = {"Reference": ref, "Value": value}.get(kind, m.group(2))
        body = blk[m.end():blk.rfind(")")].replace(" unlocked", "")
        body = re.sub(r'\(\s*tstamp\s+"?([0-9a-fA-F-]+)"?\s*\)', r'(uuid "\1")', body)
        edits.append((s, e, f'(property "{kind}" "{val}"{body})'))
    for s, e, rep in sorted(edits, key=lambda t: t[0], reverse=True):
        text = text[:s] + rep + text[e:]
    return text


def _swap_block(swap: FootprintSwap, p: "Placement") -> str:
    """库模块 → 板内封装块：lib_id 限定、插 (at)、转 property、盘配网。

    盘的绝对角 token 同 _twist_pad 语义：新块本地角 0，绝对角 = 放置角
    p.rot。
    """
    if p.side != "F":
        raise ValueError(f"封装换装暂只支持 F.Cu 面（{p.ref} 在 {p.side}.Cu）")
    text = re.sub(r'^\(\s*footprint\s+"[^"]*"', f'(footprint "{swap.lib_id}"',
                  swap.module_text, count=1)
    angle = f" {p.rot:g}" if p.rot else ""
    text = re.sub(r'(\(\s*layer\s+"F\.Cu"\s*\))',
                  rf'\1\n\t\t(at {p.x:.4f} {p.y:.4f}{angle})', text, count=1)
    text = _legacy_text_to_property(text, p.ref, swap.value)
    if "REF**" in text:
        raise ValueError(f"{swap.lib_id} 模块无 Reference 文本锚，换块失败")

    pad_edits: list[tuple[int, int, str]] = []
    for tag, s, e in _iter_blocks(text, base_depth=1):
        if tag != "pad":
            continue
        block = text[s:e]
        num = re.search(r'"([^"]+)"', block)
        net = swap.pad_nets.get(num.group(1)) if num else None
        if p.rot:  # 盘绝对角 = 放置角（本地角 0）
            block = re.sub(r"\(at (-?[\d.]+) (-?[\d.]+)\)",
                           rf"(at \1 \2 {p.rot:g})", block, count=1)
        if net:
            block = block[: block.rfind(")")].rstrip() + f' (net "{net}"))'
        pad_edits.append((s, e, block))
    for s, e, rep in sorted(pad_edits, key=lambda t: t[0], reverse=True):
        text = text[:s] + rep + text[e:]
    return text


def _iter_blocks(text: str, base_depth: int = 1) -> Iterator[tuple[str, int, int]]:
    """产出 ``base_depth`` 层子块 ``(tag, start, end)``（含括号，按闭括号序）。

    根表达式自身的括号不计：对整文件用 base_depth=1 得 (kicad_pcb …)
    的直接子块；对单个块文本（以 ``(tag …`` 开头）用 base_depth=1 得
    该块的直接子块。字符串字面量感知（``\\"`` 转义不落井）。
    """
    i, n = 0, len(text)
    depth = 0
    in_str = False
    stack: list[tuple[int, str]] = []
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            m = _IDENT.match(text, i + 1)
            stack.append((i, m.group(0)))
            depth += 1
        elif c == ")":
            depth -= 1
            start, tag = stack.pop()
            if depth == base_depth:
                yield tag, start, i + 1
        i += 1


def _reference(block: str) -> str | None:
    """footprint 块直属 property 节点里挖 Reference（第二字符串）。"""
    for tag, s, e in _iter_blocks(block, base_depth=1):
        if tag != "property":
            continue
        strs = re.findall(r'"((?:[^"\\]|\\.)*)"', block[s:e])
        if len(strs) >= 2 and strs[0] == "Reference":
            return strs[1]
    return None


def _fmt_at(p: Placement) -> str:
    """KiCad 惯例：0° 省略角度分量。"""
    angle = f" {p.rot:g}" if p.rot else ""
    return f"(at {p.x:.4f} {p.y:.4f}{angle})"


_AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")


def _rot_of(at_block: str) -> float:
    """封装/盘 at 块的角度分量（缺省 0）——Δrot 计算用。"""
    m = _AT_RE.fullmatch(at_block.strip())
    return float(m.group(3)) if m and m.group(3) else 0.0


def _twist_pad(block: str, delta: float) -> str:
    """pad 块直属 (at x y [a]) 的绝对角 += delta（360 取模，0 省略）。

    只动角度 token，x/y 字节保真；非常规 at（正则不匹配）宁缺勿错不动。
    """
    for ctag, cs, ce in _iter_blocks(block, base_depth=1):
        if ctag != "at":
            continue
        m = _AT_RE.fullmatch(block[cs:ce].strip())
        if m is None:
            continue
        na = (float(m.group(3) or 0.0) + delta) % 360
        angle = f" {na:g}" if na else ""
        return block[:cs] + f"(at {m.group(1)} {m.group(2)}{angle})" + block[ce:]
    return block


_EDGE_SHAPES = frozenset({"gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_bbox"})
# gr_* 块内的坐标 token：名 + 两数值（gr_poly 的 (xy x y) 同形处理）
_COORD = re.compile(r'\((start|mid|end|center|xy) (-?[\d.]+) (-?[\d.]+)\)')


def _is_edge(block: str) -> bool:
    """gr_* 块直属 (layer "Edge.Cuts") 判定（板框图形；层名带不带引号都认）。"""
    return re.search(r'\(layer "?Edge\.Cuts"?\)', block) is not None


def _edge_bbox_min(blocks: list[str]) -> tuple[float, float] | None:
    """Edge.Cuts 图形全部坐标的最小 (x, y)——模板板框原点。无图形 → None。"""
    xs: list[float] = []
    ys: list[float] = []
    for b in blocks:
        for m in _COORD.finditer(b):
            xs.append(float(m.group(2)))
            ys.append(float(m.group(3)))
    return (min(xs), min(ys)) if xs else None


def _shift_block(block: str, dx: float, dy: float) -> str:
    def _sub(m: re.Match) -> str:
        return f"({m.group(1)} {float(m.group(2)) + dx:.4f} {float(m.group(3)) + dy:.4f})"

    return _COORD.sub(_sub, block)


def write_placements(
    template: Path,
    out: Path,
    placements: dict[str, Placement],
    size: tuple[float, float] | None = None,
    footprint_swaps: dict[str, FootprintSwap] | None = None,
) -> tuple[int, int]:
    """模板板 + floorplan 放置态 → placed-unrouted 板；返回（写回数，剔除块数）。

    ``placements`` 只需带 ``x/y/rot/side``（floorplan 本地系，原点=板框
    左下）。位号不在模板 → ValueError；模板独有器件（如安装孔 footprint）
    原样保留。

    ``size``（spec 的 outline.size）给出时板框重写为 (0,0)-(W,H) 矩形——
    板尺寸由规格说了算，不再继承模板（收缩流的基础）；缺省仍字节保真
    平移模板板框。
    """
    text = template.read_text(encoding="utf-8")
    edge_blocks: list[str] = []  # 先收齐：板框原点要全部图形参与
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    seen: set[str] = set()
    stripped = 0
    for tag, s, e in _iter_blocks(text, base_depth=1):
        if tag in STRIP_TAGS:
            ls = s
            while ls > 0 and text[ls - 1] in " \t":  # 连前导水平空白一起删
                ls -= 1
            edits.append((ls, e, ""))
            stripped += 1
        elif tag == "footprint":
            block = text[s:e]
            ref = _reference(block)
            if ref is None or ref not in placements:
                continue  # 无位号 / 模板独有（安装孔等）：原样
            seen.add(ref)
            p = placements[ref]
            if footprint_swaps and ref in footprint_swaps:
                # part_overrides 落盘：整块换成库模块实例（几何/盘网都来自
                # IR——模板块是新封装无关物，改坐标没有意义）
                edits.append((s, e, _swap_block(footprint_swaps[ref], p)))
                continue
            old_rot = 0.0
            pad_spans: list[tuple[int, int]] = []
            for ctag, cs, ce in _iter_blocks(block, base_depth=1):
                if ctag == "at":
                    edits.append((s + cs, s + ce, _fmt_at(p)))
                    old_rot = _rot_of(text[s + cs:s + ce])
                elif ctag == "layer":
                    edits.append((s + cs, s + ce, f'(layer "{p.side}.Cu")'))
                elif ctag == "pad":
                    pad_spans.append((s + cs, s + ce))
                elif ctag == "zone":
                    # 模板作者嵌在封装实例块里的铜皮（60 板 R1 内嵌 60×60
                    # 手工区随块混入、顶层 STRIP_TAGS 扫不到，LQFP 80×120
                    # 电源层双区交叠事故根因）。placed-unrouted 底板零 zone
                    # ——铺铜权威 = merge 终相 route_planes_final。
                    ls = s + cs
                    while ls > 0 and text[ls - 1] in " \t":
                        ls -= 1
                    edits.append((ls, s + ce, ""))
                    stripped += 1
            delta = (p.rot - old_rot) % 360
            if delta:  # 封装转过 → pad 绝对角 token 同步（见模块 docstring）
                for ps, pe in pad_spans:
                    edits.append((ps, pe, _twist_pad(text[ps:pe], delta)))
        elif tag in _EDGE_SHAPES:
            block = text[s:e]
            if _is_edge(block):
                edge_blocks.append(block)
                edits.append((s, e, None))  # 占位，等原点算好后回填
    missing = sorted(set(placements) - seen)
    if missing:
        raise ValueError(f"模板板缺 {len(missing)} 个器件，无法写回: {missing}")
    # Edge.Cuts：原点要全部板框图形参与（两遍扫描），故先占位后回填
    if size is not None:
        # 规格尺寸说了算：模板板框块整体替换为 (0,0)-(W,H) 矩形四边
        w, h = size
        rect = "\n".join(
            f'\t(gr_line (start {x1:g} {y1:g}) (end {x2:g} {y2:g})'
            f' (layer "Edge.Cuts") (width 0.05))'
            for x1, y1, x2, y2 in [
                (0, 0, w, 0), (w, 0, w, h), (w, h, 0, h), (0, h, 0, 0),
            ]
        )
        # 首块位置写矩形，其余板框块删除（模板多边形/圆角收拢为矩形）
        first = True
        filled = []
        for s, e, r in edits:
            if r is None:
                filled.append((s, e, rect if first else ""))
                first = False
            else:
                filled.append((s, e, r))
        edits = filled
    else:
        origin = _edge_bbox_min(edge_blocks)
        if origin is not None and (abs(origin[0]) > 1e-9 or abs(origin[1]) > 1e-9):
            dx, dy = -origin[0], -origin[1]
            shifts = iter([_shift_block(b, dx, dy) for b in edge_blocks])
        else:
            shifts = iter(edge_blocks)  # 无偏移/无图形：原块回填，字节保真
        edits = [(s, e, next(shifts) if r is None else r) for s, e, r in edits]
    for st, en, rep in sorted(edits, key=lambda t: t[0], reverse=True):
        text = text[:st] + rep + text[en:]
    out.write_text(text, encoding="utf-8")
    return len(seen), stripped
