r"""Rudy 式可布线性/拥塞代理 Ĉ——DesignDocument §3.4 的 \hat{C}_t。

思路：把画布粗栅格化；对每条栅格边界线统计有多少 net 的半周长包围盒横跨它
作为「布线需求」；capacity = 可用布线道数（层数 × 单元间距 / 线距，名义值）。
Ĉ = 各边界 max(0, demand − capacity) 之和，归一化到 0..1。

只需方向正确（更拥塞 → 更大），不求精确——M6 接真实布线后如需可换预测网络。
"""
from __future__ import annotations

import math

from ..reuse import DesignIR

Box = tuple[float, float, float, float]


def net_bboxes(ir: DesignIR, pos: dict[str, tuple[float, float]]) -> list[Box]:
    """各 net 的包围盒（复用 hpwl 的同名逻辑，此处独立避免循环 import）。"""
    out: list[Box] = []
    for net in ir.nets:
        xs: list[float] = []
        ys: list[float] = []
        for p in net.pins:
            if p.ref in pos:
                x, y = pos[p.ref]
                xs.append(x)
                ys.append(y)
        if xs:
            out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def rudy(
    ir: DesignIR,
    pos: dict[str, tuple[float, float]],
    canvas: Box,
    g: float = 5.0,
    layers: int = 2,
    wire_pitch: float = 0.3,
) -> float:
    """归一化拥塞值（越大越拥塞）。canvas=(xmin, ymin, xmax, ymax)。"""
    xmin, ymin, xmax, ymax = canvas
    w, h = xmax - xmin, ymax - ymin
    if w <= 0 or h <= 0:
        return 0.0
    nx = max(1, int(math.ceil(w / g)))
    ny = max(1, int(math.ceil(h / g)))
    cap = layers * (g / wire_pitch)  # 每条边界线容量（名义道数）

    demand_h = [0] * (nx - 1)  # 竖直边界：net 的 x 范围横跨该 x 线
    demand_v = [0] * (ny - 1)  # 水平边界：net 的 y 范围横跨该 y 线
    for (bx1, by1, bx2, by2) in net_bboxes(ir, pos):
        for i in range(nx - 1):
            x = xmin + (i + 1) * g
            if bx1 < x < bx2:
                demand_h[i] += 1
        for j in range(ny - 1):
            y = ymin + (j + 1) * g
            if by1 < y < by2:
                demand_v[j] += 1

    overflow = 0.0
    for d in demand_h + demand_v:
        overflow += max(0.0, d - cap)
    total = len(demand_h) + len(demand_v)
    return overflow / total if total else 0.0
