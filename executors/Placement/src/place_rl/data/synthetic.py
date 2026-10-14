"""合成板生成器——训练数据主力（DesignDocument §3.1）。

采样：板尺寸、元件（尺寸/引脚数/类型从真实封装分布抽取）、网表（MCU+外设+电源
拓扑模板 + 少量随机小 net）。课程阶梯由 ``n_min``/``n_max`` 提供（元件数从易到难）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..env.problem import Component, Net, PlacementProblem

# (category, w, h, pins, weight) —— 尺寸对齐常见封装（mm），中心原点
_POOL = [
    ("passive", 1.6, 0.8, 2, 30),      # 0603 R/C
    ("passive", 2.0, 1.25, 2, 25),     # 0805
    ("passive", 3.2, 1.6, 2, 10),      # 1206
    ("passive", 3.0, 2.5, 2, 5),       # 大电容
    ("ic", 5.0, 6.0, 8, 15),           # SOIC-8
    ("ic", 5.0, 4.0, 14, 8),           # TSSOP-14
    ("ic", 10.0, 10.0, 32, 6),         # QFP-32
    ("ic", 14.0, 14.0, 100, 3),        # QFP-100（MCU）
    ("connector", 10.0, 5.0, 8, 4),
    ("connector", 14.0, 7.0, 16, 2),
]
_PASSIVE = [(w, h, p, wt) for c, w, h, p, wt in _POOL if c == "passive"]
_PERIPH = [(5.0, 6.0, 8), (5.0, 4.0, 14), (10.0, 10.0, 32)]
_CONN = [(10.0, 5.0, 8), (14.0, 7.0, 16)]
_MCU = (14.0, 14.0, 100)
# 时钟网权重（与 data.real.CLK_WEIGHT 对齐）：晶体↔MCU 的关键时序网。
_CLK_WEIGHT = 8.0


@dataclass
class GenConfig:
    seed: int = 0
    n_min: int = 20
    n_max: int = 120
    fill: float = 0.5            # 板面积 / 元件总面积
    aspect: tuple[float, float] = (0.7, 1.5)
    fix_connectors: bool = False
    grid: float = 1.27


def generate(cfg: GenConfig) -> PlacementProblem:
    rng = random.Random(cfg.seed)
    n = max(8, rng.randint(cfg.n_min, cfg.n_max))

    comps: list[Component] = []
    counter = {"ic": 0, "passive": 0, "connector": 0}
    prefix = {"ic": "U", "passive": "R", "connector": "J"}

    def add(cat: str, w: float, h: float, pins: int) -> str:
        counter[cat] += 1
        ref = f"{prefix[cat]}{counter[cat]}"
        comps.append(Component(ref, w, h, pins, cat))
        return ref

    mcu = add("ic", *_MCU)

    n_periph = max(1, min(n // 8, 8))
    periph = [add("ic", *rng.choice(_PERIPH)) for _ in range(n_periph)]

    n_conn = rng.randint(1, 3)
    conn = [add("connector", *rng.choice(_CONN)) for _ in range(n_conn)]

    n_passive = n - len(comps)
    if n_passive > 0:
        for _ in range(n_passive):
            w, h, pins, _ = rng.choices(_PASSIVE, weights=[x[3] for x in _PASSIVE])[0]
            add("passive", w, h, pins)

    # 晶体：2 脚无源，值含频率。其两条时钟网将赋高权，教策略把它贴在 MCU 旁
    # （否则它和普通去耦电容一样按度数排序、被晚放置推离 MCU）。
    crystal = add("passive", 4.0, 2.5, 2)
    comps[-1].value = "8MHz"

    passive = [c.ref for c in comps if c.category == "passive"]

    nets: list[Net] = []
    # 全局电源网：主控 + 外设 + 大部分无源
    gnd = [mcu] + periph + [r for r in passive if rng.random() < 0.7]
    if len(gnd) >= 2:
        nets.append(Net(gnd, name="GND"))
    vcc = [mcu] + periph + [r for r in passive if rng.random() < 0.7]
    if len(vcc) >= 2:
        nets.append(Net(vcc, name="VCC"))
    # 外设信号 + 本地去耦
    for p in periph:
        for _ in range(rng.randint(1, 3)):
            nets.append(Net([mcu, p]))
        if passive:
            nets.append(Net([p] + rng.sample(passive, min(rng.randint(1, 3), len(passive)))))
    # 晶体时钟网（XIN/XOUT）：晶体 + MCU + 各 1 颗负载电容，赋高权。
    other_passives = [r for r in passive if r != crystal]
    if len(other_passives) >= 2:
        ca, cb = rng.sample(other_passives, 2)
    else:
        ca = cb = crystal
    nets.append(Net([crystal, mcu, ca], name="XIN", weight=_CLK_WEIGHT))
    nets.append(Net([crystal, mcu, cb], name="XOUT", weight=_CLK_WEIGHT))
    # 随机小 net（上拉/滤波）
    all_refs = [c.ref for c in comps]
    for _ in range(rng.randint(n // 4, n // 2)):
        nets.append(Net(rng.sample(all_refs, min(rng.randint(2, 4), len(all_refs)))))

    # 板尺寸：总面积 / fill，长宽比采样
    total_area = sum(c.w * c.h for c in comps)
    board_area = total_area / cfg.fill
    asp = rng.uniform(*cfg.aspect)
    W = max(10.0, math.sqrt(board_area * asp))
    H = max(10.0, math.sqrt(board_area / asp))
    W = round(W / cfg.grid) * cfg.grid
    H = round(H / cfg.grid) * cfg.grid

    fixed: dict[str, tuple[float, float, float]] = {}
    if cfg.fix_connectors and conn:
        for i, r in enumerate(conn):
            # 沿底边均布，贴边锁定
            cobj = next(x for x in comps if x.ref == r)
            x = (i + 1) * W / (len(conn) + 1)
            y = cobj.h / 2
            fixed[r] = (round(x / cfg.grid) * cfg.grid,
                        round(y / cfg.grid) * cfg.grid, 0.0)

    return PlacementProblem(components=comps, nets=nets, board=(W, H), fixed=fixed)
