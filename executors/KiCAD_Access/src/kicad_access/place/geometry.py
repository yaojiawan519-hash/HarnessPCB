"""IR 器件 → 足迹几何装载 + 放置态外框/重叠（M2-1）。

place 包的地基：floorplan 非重叠、recipe pad 几何、融合推挤、
SVG 预览、写板全部消费这一层。上游 = DesignIR（M1），下游只认
(Placement, FootprintGeom) 二元组。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ingest.ir import DesignIR
from ..kicad.footprint import Box, FootprintGeom
from ..kicad.fplib import FootprintLibs

DEFAULT_MARGIN = 0.0  # 重叠判定附加间隙（mm）；courtyard 已含厂标余量


@dataclass
class Placement:
    """一个器件的放置态。坐标 = footprint 原点在板上的绝对位置。"""

    ref: str
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0  # 度；y-down 系屏幕逆时针（约定见 footprint.py）
    side: str = "F"  # "F" | "B"——翻面到 B 时 x 镜像（写板时落实）
    mobility: str = "soft"  # "hard" 锁定 | "soft" 融合可推挤


def load_geometry(
    ir: DesignIR, libs: FootprintLibs
) -> tuple[dict[str, FootprintGeom], list[str]]:
    """IR 全器件 → {ref: 几何}；footprint 解析失败的收集为警告行。"""
    geoms: dict[str, FootprintGeom] = {}
    problems: list[str] = []
    for ref, dev in ir.devices.items():
        if not dev.footprint:
            problems.append(f"[{dev.module}] {ref} 无 Footprint 字段")
            continue
        g = libs.load(dev.footprint)
        if g is None:
            problems.append(f"[{dev.module}] {ref} 封装不可解析: {dev.footprint}")
        else:
            geoms[ref] = g
    return geoms, problems


def outline_of(geom: FootprintGeom, plc: Placement) -> Box:
    """放置态外框 AABB（courtyard 优先，缺则 pads 兜底外扩）。"""
    return geom.bbox_at(plc.x, plc.y, plc.rot)


def overlap_area(a: Box, b: Box) -> float:
    """两 AABB 重叠面积（mm²）；相离/相切 = 0。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def find_overlaps(
    placements: dict[str, Placement],
    geoms: dict[str, FootprintGeom],
    margin: float = DEFAULT_MARGIN,
) -> list[tuple[str, str, float]]:
    """全部外框两两重叠检查 → [(refA, refB, 面积)]，按面积降序。

    只对有几何的器件判定；面积 0（相切）不算重叠。
    """
    boxes: dict[str, Box] = {}
    for ref, plc in placements.items():
        g = geoms.get(ref)
        if g is None:
            continue
        x1, y1, x2, y2 = outline_of(g, plc)
        boxes[ref] = (x1 - margin, y1 - margin, x2 + margin, y2 + margin)
    refs = sorted(boxes)
    out: list[tuple[str, str, float]] = []
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            area = overlap_area(boxes[a], boxes[b])
            if area > 0:
                out.append((a, b, area))
    out.sort(key=lambda t: -t[2])
    return out
