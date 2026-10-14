"""place-rl 对 kicad-access 数据层的适配层。

设计原则（见 PlacementRL_实施规划.md §7）：
- place-rl 只依赖这里暴露的名字，不直接 import kicad_access 内部实现；
- kca 内部漂移（重命名/重构）时，只需改这一个文件，必要时 vendoring 4 个纯数据文件。

覆盖范围：ingest（IR）、footprint 几何、fplib、放置几何、pcb 读/写。
"""
from __future__ import annotations

from pathlib import Path

# —— ingest：sch 层级 → IR ——
from kicad_access.ingest import build_ir, DesignIR, DeviceIR, ModuleIR, NetIR, PinRef
from kicad_access.kicad.schematic import Design

# —— footprint 几何 ——
from kicad_access.kicad.footprint import Box, FootprintGeom
from kicad_access.kicad.fplib import FootprintLibs

# —— 放置几何 ——
from kicad_access.place.geometry import (
    Placement,
    load_geometry,
    outline_of,
    overlap_area,
    find_overlaps,
)

# —— pcb 读 / 写 ——
from kicad_access.kicad.pcb import Board, Footprint
from kicad_access.kicad.pcbwrite import write_placements
from kicad_access.kicad import sexpr as _sx

__all__ = [
    "build_ir", "Design", "DesignIR", "DeviceIR", "ModuleIR", "NetIR", "PinRef",
    "Box", "FootprintGeom", "FootprintLibs",
    "Placement", "load_geometry", "outline_of", "overlap_area", "find_overlaps",
    "Board", "Footprint", "write_placements",
]


def load_design_ir(sch: str | Path) -> DesignIR:
    """便捷入口：读 .kicad_sch（层级，两层够用）→ DesignIR。

    kca 的 ``Design.all_symbols`` 只遍历子图（modules）——扁平单页设计
    （器件全在 root 图）会解析出 0 器件。这里在适配层兜底：无子图时把
    root 图注册为模块 "root"，不改动 kca 源码。
    """
    design = Design(Path(sch))
    if not design.modules and design.root.symbols:
        design.modules["root"] = design.root
    return build_ir(design)


def board_outline_bbox(pcb: str | Path) -> tuple[float, float, float, float]:
    """读 Edge.Cuts 板框图形的最小外接矩形 (xmin, ymin, xmax, ymax) mm。

    遍历 .kicad_pcb 顶层与 footprint 块内的 Edge.Cuts 图形（gr_line/gr_arc/
    gr_rect/gr_circle/gr_poly），取其所有端点/起止坐标的包围盒。真实板写回时
    用它把「板框本地系」对齐到 (0,0)，放置坐标即本地系。无板框则退化到
    已放 footprint 包围盒。
    """
    pcb = Path(pcb)
    root = _sx.loads(pcb.read_text())
    pts: list[tuple[float, float]] = []

    def _xy(node: list) -> tuple[float, float] | None:
        """取 (tag x y [更多]) 节点的 x,y 数值对。"""
        if len(node) < 3:
            return None
        x, y = _sx.num(node[1], None), _sx.num(node[2], None)
        if x is None or y is None:
            return None
        return float(x), float(y)

    def _walk(nodes: list) -> None:
        for node in nodes:
            if not isinstance(node, list):
                continue
            t = _sx.tag(node)
            if t == "footprint":
                _walk(node[1:])
                continue
            if t in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly"):
                layer = _sx.attr(node, "layer")
                if layer != "Edge.Cuts":
                    continue
                for key in ("start", "end", "center", "mid"):
                    for kid in _sx.children(node, key):
                        p = _xy(kid)
                        if p:
                            pts.append(p)
                for pts_node in _sx.children(node, "pts"):
                    for xy in _sx.children(pts_node, "xy"):
                        p = _xy(xy)
                        if p:
                            pts.append(p)
            else:
                _walk(node[1:])

    _walk(root)
    if not pts:
        raise ValueError(f"{pcb} 无 Edge.Cuts 板框图形")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
