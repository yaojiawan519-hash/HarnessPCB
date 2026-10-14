"""GND 缝合孔（M3-6-1）：内层 GND 平面 → 全板缝合过孔阵列。

四层叠层下 GND 在 In1 整面铺铜，外层铜仅经"落地孔"局部接地——缝合
孔把平面锚点铺满全板（降低平面阻抗、给后续外层铜就近留地）。

规则（确定性，spec ``routing.stitch`` 可覆盖 pitch/via_size/via_drill/
clearance/margin）：
- 网格：outline 内缩 margin（默认 via_size/2 + clearance），pitch
  网格行先序扫描；
- 只落 GND 内层 zone 多边形内（even-odd 点包含，含镂空环）；
- 与一切既有铜/盘保持 clearance——孔盘全穿透，F/B 线、既有孔、全部
  盘（含 GND 盘：缝合孔贴盘无增益还占通道）都要避开；内层 In2 电源
  领地不查：PTH 孔穿 In2 时由 KiCad fill 引擎自动 carve 反焊盘，不
  构成短路。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .diff import FINE
from .model import RouteBoard, Via, pad_dist, seg_dist


@dataclass(frozen=True)
class StitchResult:
    """缝合产物：孔列表 + 落点/避让计数（CLI 报告消费）。"""

    vias: tuple[Via, ...]
    placed: int
    skipped: int


def _in_polys(polys: tuple[tuple[tuple[float, float], ...], ...],
              x: float, y: float) -> bool:
    """even-odd 点包含（多环：外环 + 镂空环一并翻转，KiCad y-down 无关）。"""
    inside = False
    for poly in polys:
        for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
    return inside


def _clear(board: RouteBoard, x: float, y: float, radius: float,
           clearance: float) -> bool:
    """候选点与全部盘/孔/F-B 线净距充足（盘 = pad_dist 精确距离场：圆
    包络在盘角方向漏检——方盘对角伸到 r√2，内切圆只到 r，曾有缝合孔
    沿对角落进盘角 37µm）。"""
    for p in board.pads:
        if pad_dist(x, y, p) - radius < clearance:
            return False
    for v in board.vias:
        if math.hypot(v.x - x, v.y - y) < radius + v.size / 2 + clearance:
            return False
    for s in board.segments:
        if seg_dist(x, y, s) < radius + s.width / 2 + clearance:
            return False
    return True


def stitch_gnd(
    board: RouteBoard, spec: Any = None, *,
    pitch: float | None = None,
    via_size: float | None = None,
    via_drill: float | None = None,
    clearance: float | None = None,
    margin: float | None = None,
) -> StitchResult:
    """GND zone 板 → 缝合孔阵列；无 GND 内层 zone 即空结果（非四层板）。"""
    zones = [z for z in board.zones if z.net == "GND"
             and z.layer.startswith("In")]
    if not zones:
        return StitchResult((), 0, 0)

    cfg: dict = {}
    if spec is not None:
        cfg = (spec.routing or {}).get("stitch") or {}
    pitch = float(pitch or cfg.get("pitch", 2.5))
    via_size = float(via_size or cfg.get("via_size", FINE.via_size))
    via_drill = float(via_drill or cfg.get("via_drill", FINE.via_drill))
    clearance = float(clearance if clearance is not None
                      else cfg.get("clearance", FINE.clearance))
    margin = float(margin or cfg.get("margin", via_size / 2 + clearance))

    polys: tuple[tuple[tuple[float, float], ...], ...] = tuple(
        p for z in zones for p in z.polys)
    x1, y1, x2, y2 = board.outline
    lo_x, lo_y = x1 + margin, y1 + margin
    hi_x, hi_y = x2 - margin, y2 - margin

    vias: list[Via] = []
    placed = skipped = 0
    nx = max(0, int((hi_x - lo_x) / pitch) + 1)
    ny = max(0, int((hi_y - lo_y) / pitch) + 1)
    for iy in range(ny):
        y = round(lo_y + iy * pitch, 3)
        for ix in range(nx):
            x = round(lo_x + ix * pitch, 3)
            if x > hi_x or y > hi_y:
                continue
            if not _in_polys(polys, x, y) or not _clear(
                    board, x, y, via_size / 2, clearance):
                skipped += 1
                continue
            vias.append(Via(x, y, via_size, via_drill, "GND"))
            placed += 1
    return StitchResult(tuple(vias), placed, skipped)
