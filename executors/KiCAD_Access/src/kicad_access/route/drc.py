"""自有 DRC 子集（M3-6-2）：短路 / 间隙 / 宽度 / 断裂——合并板闭环。

不追 KiCad DRC 全集，只查**我们产铜能犯的错**四类：

- 短路：异网铜件中心距 < 半宽/半径和（真交叠）；
- 间隙：异网铜件距离 < 半宽和 + clearance（铜对铜用精确几何）；
  **铜对盘只查短路级**：astar 同款精确盘距离场（盘 w/h 已烘焙封装
  旋转；roundrect = 收缩盒⊕圆盘），细长盘旁的合法贴近不再误报；
  间隙级涉及盘环工艺余量，宁缺毋滥；
- 宽度：线宽 < max(spec min_width, FINE.width)；
- 断裂：非 zone 网的全盘经本网铜归一连通分量（借 freeroute 的并查
  集核验，同一实现两处用——布线时逐网把关、合并后全板复验）。zone
  网连通由 KiCad fill 语义保证（fill 时热焊盘），线段图不可验 → 跳过。

pairwise O(n²) 对当前板规模（<1k 铜件）秒级；结果按 (kind, detail)
排序保确定性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .astar import _seg_pad_dist
from .diff import FINE
from .freeroute import _pads_connected
from .l1 import widths_from_spec
from .model import RouteBoard, Segment, Via, pad_dist
from .regions import _pair_dist

_EPS = 1e-6  # mm；铜交叠判定浮点容差（≪ 最小线宽）

ERROR_KINDS = ("短路", "间隙", "宽度", "断裂")


@dataclass(frozen=True)
class DrcViolation:
    """一条违规：kind ∈ ERROR_KINDS + 人读 detail（CLI/report 消费）。"""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def _desc(c: Segment | Via) -> str:
    if isinstance(c, Segment):
        return (f"{c.net} 线({c.x1:.3f},{c.y1:.3f})-({c.x2:.3f},{c.y2:.3f})"
                f" {c.width}@{c.layer}")
    return f"{c.net} 孔({c.x:.3f},{c.y:.3f}) ø{c.size}"


def run_drc(
    board: RouteBoard, spec: Any = None, *,
    clearance: float | None = None,
    widths: dict[str, float] | None = None,
) -> list[DrcViolation]:
    """全板 DRC → 违规列表（空 = 干净）。铜对铜精确几何，铜对盘只查交叠。"""
    clearance = FINE.clearance if clearance is None else clearance
    if widths is None:
        widths = widths_from_spec(spec) if spec is not None else {}

    out: list[DrcViolation] = []

    # 宽度（spec min_width 收紧的网按 spec，其余工艺底）。工艺底可经
    # spec ``routing.width_floor`` 放宽：FR 在 0.5mm 间距 LQFP 逃逸通道
    # 内会颈缩（952 DSN 单位 = 0.0952mm，2026-09-11 实证；通道净宽
    # 0.2mm，加宽必擦邻盘）；JLC 4 层工艺底 0.09mm，0.0952 合法。
    floor = FINE.width
    if spec is not None:
        try:
            floor = float(spec.routing.get("width_floor") or FINE.width)
        except (AttributeError, TypeError, ValueError):
            pass
    for s in board.segments:
        need = max(widths.get(s.net, 0.0), floor)
        if s.width < need - _EPS:
            out.append(DrcViolation("宽度", f"{_desc(s)} < {need}mm"))

    # 铜 ✕ 铜：短路 + 间隙（同网不查——并线/堆叠是布线自由）
    items: list[Segment | Via] = [*board.segments, *board.vias]
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.net == b.net:
                continue
            hit = _pair_dist(a, b)
            if not hit:
                continue
            dist, need = hit
            if dist < need - _EPS:
                out.append(DrcViolation(
                    "短路", f"{_desc(a)} ✕ {_desc(b)}（距 {dist:.3f} < {need:.3f}）"))
            elif dist < need + clearance - _EPS:
                out.append(DrcViolation(
                    "间隙", f"{_desc(a)} ↔ {_desc(b)}（距 {dist:.3f} < "
                    f"{need + clearance:.3f}）"))

    # 铜 ✕ 异网盘：短路级精确几何（via = pad_dist 点场 − 半径；线 =
    # astar 的线段-盘距离场 − 半线宽；负值 = 铜真交叠）
    for c in items:
        if isinstance(c, Via):
            for p in board.pads:
                if p.net is None or p.net == c.net:
                    continue
                if pad_dist(c.x, c.y, p) - c.size / 2 < -_EPS:
                    out.append(DrcViolation(
                        "短路", f"{_desc(c)} ✕ 盘 {p.key} {p.w:g}×{p.h:g}"))
        else:
            for p in board.pads:
                if p.net is None or p.net == c.net:
                    continue
                if not p.is_th and c.layer not in p.layers:
                    continue
                if _seg_pad_dist(c.x1, c.y1, c.x2, c.y2, p) - c.width / 2 < -_EPS:
                    out.append(DrcViolation(
                        "短路", f"{_desc(c)} ✕ 盘 {p.key} {p.w:g}×{p.h:g}"))

    # 断裂：非 zone 网逐网连通（zone 网 = fill 语义，跳过）
    zone_nets = {z.net for z in board.zones}
    by_net = board.pads_by_net()
    for net in sorted(by_net):
        if len(by_net[net]) < 2 or net in zone_nets:
            continue
        segs = [s for s in board.segments if s.net == net]
        vias = [v for v in board.vias if v.net == net]
        if segs or vias:  # 全无铜的网是缺布（route 逐区报过），断裂只查有铜的
            if not _pads_connected(board, segs, vias, net):
                out.append(DrcViolation(
                    "断裂", f"{net} {len(by_net[net])} 盘未归一连通分量"))

    return sorted(out, key=lambda v: (v.kind, v.detail))
