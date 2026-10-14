"""HPWL（半周长线长）——DesignDocument §3.4 的线长项。

中心近似：每个器件的坐标用其 footprint 原点表示（v1 不展开引脚坐标）。
完整公式见 DesignDocument.md：

    HPWL = Σ_n∈Nets [ (max_p x_p − min_p x_p) + (max_p y_p − min_p y_p) ]

提供：
- ``hpwl(ir, pos)``：整板静态 HPWL（mm）。
- ``HPWLTracker``：顺序放置下的增量 ΔHPWL，O(Δ) 而非 O(N) 重算——RL 每一步
  奖励里都要取 -ΔHPWL，增量是训练吞吐的前提。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..reuse import DesignIR

# 每 net 的 (minx, miny, maxx, maxy, 已连器件数)；未连满的 net 暂不计入 HPWL
NetBox = list[float]  # [minx, miny, maxx, maxy]


def net_bboxes(ir: DesignIR, pos: dict[str, tuple[float, float]]) -> list[NetBox]:
    """各 net 的包围盒（只统计 ref 在 pos 里出现的引脚）。"""
    out: list[NetBox] = []
    for net in ir.nets:
        xs: list[float] = []
        ys: list[float] = []
        for p in net.pins:
            if p.ref in pos:
                x, y = pos[p.ref]
                xs.append(x)
                ys.append(y)
        if xs:
            out.append([min(xs), min(ys), max(xs), max(ys)])
    return out


def hpwl(ir: DesignIR, pos: dict[str, tuple[float, float]]) -> float:
    """整板 HPWL（mm），中心近似。"""
    total = 0.0
    for (x1, y1, x2, y2) in net_bboxes(ir, pos):
        total += (x2 - x1) + (y2 - y1)
    return total


@dataclass
class HPWLTracker:
    """增量 HPWL 计算器：每次 ``place(ref, (x, y))`` 返回该步 ΔHPWL。

    只重算受影响（该器件所在）的 net，其余 net 的贡献缓存不动。顺序放置里
    器件一次落定不再移动，故无需支持撤销；重算用「当前 box ± 新点」即可。
    """

    ir: DesignIR
    pos: dict[str, tuple[float, float]] = field(default_factory=dict)
    _ref2nets: dict[str, list[int]] = field(default_factory=dict, init=False)
    _netidx: dict[str, int] = field(default_factory=dict, init=False)
    _boxes: dict[int, list[float]] = field(default_factory=dict, init=False)
    _total: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        # ref → 所属 net 下标（倒排索引），一次建好
        for i, net in enumerate(self.ir.nets):
            self._netidx[net.name] = i
            for p in net.pins:
                self._ref2nets.setdefault(p.ref, []).append(i)

    @property
    def total(self) -> float:
        return self._total

    def _box_cost(self, box: list[float]) -> float:
        return (box[2] - box[0]) + (box[3] - box[1])

    def place(self, ref: str, xy: tuple[float, float]) -> float:
        """落下 ref 到 xy，返回本步 ΔHPWL（已累加到 ``total``）。"""
        self.pos[ref] = xy
        delta = 0.0
        for i in self._ref2nets.get(ref, []):
            net = self.ir.nets[i]
            # 只有 ref 同 net 的引脚（可能复用位号如多单元，这里取 ref 的第一次）
            old = self._boxes.get(i)
            xs = [xy[0]]
            ys = [xy[1]]
            for p in net.pins:
                if p.ref == ref:
                    continue
                if p.ref in self.pos:
                    x, y = self.pos[p.ref]
                    xs.append(x)
                    ys.append(y)
            if old is None and len(xs) < 2:
                continue  # 单点不成线，不计
            new_box = [min(xs), min(ys), max(xs), max(ys)]
            if old is None:
                # net 首次出现 ≥2 个已连器件：计入整条 HPWL
                delta += self._box_cost(new_box)
            else:
                delta += self._box_cost(new_box) - self._box_cost(old)
            self._boxes[i] = new_box
        self._total += delta
        return delta
