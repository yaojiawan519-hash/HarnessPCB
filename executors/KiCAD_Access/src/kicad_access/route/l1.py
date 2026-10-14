"""L1 确定性布线策略（M3-3 电源树先行；差分对/扇出 M3-4 增补）。

电源树：每网 **Prim 最小生成树**——任意两盘连通且总线长最短，比手捏
链式拓扑省铜且抗单点绕断；跨距大的边后布。三条确定性规则：

- 焊盘选边：距离同分按盘 key 字典序（Prim O(n²)，盘数 ≤ 百级足够）；
- 边序：(长度, 起 key, 终 key) 升序逐条 A*——短边先占好走线资源；
- 网序：(线宽降序, 网名) ——宽网（载流大、min_width 规则网）先挑直道。

线宽来源 ``widths_from_spec()``：spec routing critical 的 min_width 按
网覆盖（如 +24V_RAW 0.5），其余落 RouteConfig.width 默认。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .astar import GridRouter, RouteConfig
from .model import Pad, RouteBoard, Segment, Via
from .regions import Region

_EPS = 1e-6


def widths_from_spec(spec) -> dict[str, float]:
    """spec routing critical 的 min_width 规则 → {网: 线宽}。"""
    out: dict[str, float] = {}
    for rule in spec.routing.get("critical", []):
        if rule.get("rule") != "min_width":
            continue  # diff_pair/pair 是 M3-4 的拓扑约束，不改线宽
        nets = rule.get("nets") or ([rule["net"]] if "net" in rule else [])
        for n in nets:
            out[n] = float(rule["width"])
    return out


@dataclass(frozen=True)
class NetReport:
    """单网布线结果摘要（CLI/report 消费）。"""

    net: str
    pads: int
    edges: int
    segments: int
    vias: int
    length: float  # mm，线段总长（过孔不计）
    deferred: int = 0  # 缓布盘数（BGA 落盘，待 M3-4 扇出接管）


def _mst(pads: list[Pad]) -> list[tuple[Pad, Pad]]:
    """Prim 最小生成树：确定性（距离同分 → 盘 key 字典序）。"""
    if len(pads) < 2:
        return []
    nodes = sorted(pads, key=lambda p: p.key)
    in_tree = [nodes[0]]
    rest = nodes[1:]
    edges: list[tuple[Pad, Pad]] = []
    while rest:
        best_i = 0
        best = (math.inf, "", "")  # (dist, 新盘 key, 树内盘 key)
        for j, q in enumerate(rest):
            for p in in_tree:
                cand = (round(math.hypot(p.x - q.x, p.y - q.y), 6), q.key, p.key)
                if cand < best:
                    best = cand
                    best_i = j
                    best_p = p
        edges.append((best_p, rest[best_i]))
        in_tree.append(rest.pop(best_i))
    return edges


def route_power(
    board: RouteBoard, region: Region, widths: dict[str, float],
    config: RouteConfig | None = None,
    layers: tuple[str, ...] = (),
) -> tuple[list[Segment], list[Via], list[NetReport]]:
    """power 区域：逐网 MST → 逐边 A* → 顺序 commit。

    layers = 信号层栈（缺省 F/B；N 层板传 spec.signal_layers）。
    BGA 落盘缓布（bga_refs 几何检测）：0.8mm 间距下盘间铜隙仅 0.4mm，
    常规线宽/间隙物理穿不过——这些盘从锚点集滤除、deferred 如实计数，
    交 M3-4 专用扇出参数接管（不悄悄跳过）；其余盘照常 MST 全连通。

    返回 (segments, vias, reports)；任一边失败即 ValueError（L1 无
    rip-up，失败上抛让调用方换策略/人工介入，不静默留断线）。
    """
    router = GridRouter(board, config or RouteConfig(), widths, layers=layers)
    by_net = board.pads_by_net()
    missing = sorted(n for n in region.nets if n not in by_net)
    if missing:
        raise ValueError(f"区域 {region.name} 的网络在板上无焊盘: {missing}")
    bga = board.bga_refs()
    segs: list[Segment] = []
    vias: list[Via] = []
    reports: list[NetReport] = []
    # 网序：宽网先布（载流大/min_width 网占直道），同宽按名
    nets = sorted(region.nets, key=lambda n: (-router.width(n), n))
    for net in nets:
        all_pads = by_net[net]
        pads = [p for p in all_pads if p.ref not in bga]
        deferred = len(all_pads) - len(pads)
        edges = sorted(
            _mst(pads),
            key=lambda e: (round(math.hypot(e[0].x - e[1].x, e[0].y - e[1].y), 6),
                           e[0].key, e[1].key),
        )
        n_seg = n_via = 0
        length = 0.0
        for a, b in edges:
            s, v = router.route(net, a, b)
            router.commit(s, v)
            segs.extend(s)
            vias.extend(v)
            n_seg += len(s)
            n_via += len(v)
            length += sum(math.hypot(s2.x2 - s2.x1, s2.y2 - s2.y1) for s2 in s)
        reports.append(NetReport(net, len(all_pads), len(edges), n_seg, n_via,
                                 round(length, 3), deferred))
    return segs, vias, reports
