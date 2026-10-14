"""差分对布线（M3-4）：ETH/CAN 对的确定性布通 + 等长把关。

策略（v1）：对内两网各自 MST + A*（先 P 后 N，铜序贯入索引），布不通
即 ValueError；布通后核 |len(P) − len(N)| ≤ tol，超差点名 ValueError
——不确定的绕长比可见的失败更糟，serpentine 绕长补偿留作实测超差后
的增量项。100Ω 阻抗是叠层/线宽工艺参数：布线器保连通 + 间隙 + 等长，
阻抗归 spec 与板厂，不在本层伪造。

FINE 参数按 0.8mm BGA 节距几何论证（U1：ø0.4 圆盘、精确 0.8 栅格、
球心落在 0.05mm 栅格上）：
- step 0.05：逃逸通道中线 = 球心 ± 0.4（半节距）。0.05 整除 0.4 且
  球阵在 0.05 栅格 → 通道中线恰为栅格结点（偏移 0）。注意 0.1 步长
  不行：真板 U1 球心 y ≡ 0.05 (mod 0.1)，水平通道中线全体错位 0.05；
- width/clearance 0.127（5/5mil，JLC 4 层标准工艺）：通道中线到邻球
  铜缘 0.4 − 0.2 − 0.0635 = 0.1365 ≥ 0.127（余 0.0095，靠零偏移兑现）；
- via 0.4/0.2：孔不落通道中线（球心距 0.4 < 0.2+0.2+0.127），只落
  通道交叉口（dogbone 对角 0.8/√2 = 0.5657 ≥ 0.527，余 0.0387）；
- anchor_radius 0.35：球心在栅格上，最坏锚点距邻球铜缘
  0.8 − 0.35 − 0.2 = 0.25 ≥ 线半宽 0.0635 + 裕量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .astar import GridRouter, RouteConfig
from .l1 import NetReport, _mst
from .model import RouteBoard, Segment, Via
from .regions import Region

FINE = RouteConfig(
    step=0.05, clearance=0.127, width=0.127,
    via_size=0.4, via_drill=0.2,
    via_cost=0.2, turn_cost=0.125, anchor_radius=0.35,
)


@dataclass(frozen=True)
class PairRule:
    """一条差分/成对规则落到一个区域的一对网。"""
    net_p: str
    net_n: str
    tol: float   # 等长容差 mm（spec length_match；diff_pair 缺省 0.5）
    rule: str    # "diff_pair" | "pair"（来源记录，报告用）


@dataclass(frozen=True)
class PairStat:
    """一对网的布通结果（delta 为对内长度差）。"""
    net_p: str
    net_n: str
    len_p: float
    len_n: float

    @property
    def delta(self) -> float:
        return abs(self.len_p - self.len_n)


def pairs_from_spec(spec, region: Region) -> list[PairRule]:
    """spec.routing.critical → 本区差分/成对网表（有序 + 全覆盖核验）。

    rule ∈ {diff_pair, pair} 且两网都在 region.nets 才入列；容差取
    length_match（显式 0 也尊重），diff_pair 未声明时默认 0.5mm。要求
    规则网与区域网**恰好**互相覆盖：半对在区外属区域划分 bug，立即
    ValueError 而不是布半对。
    """
    pairs: list[PairRule] = []
    for c in spec.routing.get("critical", []):
        if c.get("rule") not in ("diff_pair", "pair"):
            continue
        nets = tuple(c.get("nets") or ([c["net"]] if "net" in c else ()))
        if len(nets) == 2 and set(nets) <= set(region.nets):
            lm = c.get("length_match")
            tol = float(lm) if lm is not None else 0.5
            pairs.append(PairRule(nets[0], nets[1], tol, c["rule"]))
    pairs.sort(key=lambda p: (p.net_p, p.net_n))
    covered = {n for p in pairs for n in (p.net_p, p.net_n)}
    if covered != set(region.nets):
        raise ValueError(
            f"区域 {region.name} 差分对覆盖不完整: 规则网 {sorted(covered)}"
            f" ≠ 区域网 {sorted(region.nets)}——检查 regions 分组或 spec 规则")
    return pairs


def _lattice_guard(board: RouteBoard, region: Region, cfg: RouteConfig) -> None:
    """区内在册 BGA 球盘必须落在 step 栅格上，否则逃逸通道中线错位。

    通用性守卫：布局若把 BGA 放到任意坐标，通道中线（球心 ± 半节距）
    不再是栅格结点，A* 会以 0.0095mm 级裕量擦伤或直接无解——提前点名
    比布出擦伤铜诚实。
    """
    bga = board.bga_refs()
    for net in region.nets:
        for pad in board.pads_by_net().get(net, ()):
            if pad.ref not in bga:
                continue
            for v in (pad.x, pad.y):
                k = v / cfg.step
                if abs(k - round(k)) > 1e-6:
                    raise ValueError(
                        f"{pad.key}（网 {net}）不在 FINE.step {cfg.step}mm 栅格上"
                        "——逃逸通道中线将错位，先对齐放置或调 step")


def _route_net(
    router: GridRouter, net: str, by_net: dict,
) -> tuple[list[Segment], list[Via], float]:
    """单网 MST 逐边 A* + 边级 commit；失败上抛（部分铜由调用方回滚）。"""
    pads = sorted(by_net.get(net, ()), key=lambda p: p.key)
    if len(pads) < 2:
        raise ValueError(f"差分网 {net} 在板上不足 2 盘")
    segs: list[Segment] = []
    vias: list[Via] = []
    total = 0.0
    for a, b in _mst(pads):
        try:
            ss, vv = router.route(net, a, b)
        except ValueError as e:
            raise ValueError(f"{net} 边 {a.key}↔{b.key} 布不通: {e}") from e
        router.commit(ss, vv)
        segs += ss
        vias += vv
        total += sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in ss)
    return segs, vias, total


def route_diff(
    board: RouteBoard, region: Region, pairs: list[PairRule],
    config: RouteConfig | None = None,
    layers: tuple[str, ...] = (),
) -> tuple[list[Segment], list[Via], list[NetReport], list[PairStat]]:
    """差分对区域：对内双序试布（P 先→失败回滚→N 先）+ 等长核验。

    layers = 信号层栈（缺省 F/B；N 层板传 spec.signal_layers，逃逸可
    借内层信号层绕行）。对内两网争抢同一逃逸资源（BGA 狗骨孔位/共享
    通道，v6 实证 TD_P 先行会封死 TD_N 全部狗骨）——固定 P-先序对这种
    几何是赌运气；改为 P 先试、任一网失败即回滚整对换 N 先重试，
    两序皆死才判不可布。返回 (segments, vias, reports, pair_stats)。
    任何一对布不通、或对内长度差超容差 → ValueError 点名网/对/盘。
    """
    cfg = config or FINE
    board.inherit_nets()
    _lattice_guard(board, region, cfg)
    by_net = board.pads_by_net()
    router = GridRouter(board, cfg, layers=layers)

    segments: list[Segment] = []
    vias: list[Via] = []
    reports: list[NetReport] = []
    stats: list[PairStat] = []
    for pr in pairs:
        placed: tuple[list[Segment], list[Via], dict[str, float]] | None = None
        last_err: ValueError | None = None
        for order in ((pr.net_p, pr.net_n), (pr.net_n, pr.net_p)):
            acc_s: list[Segment] = []
            acc_v: list[Via] = []
            lens: dict[str, float] = {}
            try:
                for net in order:
                    ss, vv, total = _route_net(router, net, by_net)
                    acc_s += ss
                    acc_v += vv
                    lens[net] = total
            except ValueError as e:
                router.rollback(acc_s, acc_v)
                last_err = e
                continue
            placed = (acc_s, acc_v, lens)
            break
        if placed is None:
            raise ValueError(
                f"差分对 {pr.net_p}/{pr.net_n} 布不通（P 先/N 先两序均试）: "
                f"{last_err}")
        acc_s, acc_v, lens = placed
        segments.extend(acc_s)
        vias.extend(acc_v)
        for net in (pr.net_p, pr.net_n):
            n_seg = sum(1 for s in acc_s if s.net == net)
            n_via = sum(1 for v in acc_v if v.net == net)
            reports.append(NetReport(net, len(by_net[net]),
                                     len(by_net[net]) - 1,
                                     n_seg, n_via, round(lens[net], 3), 0))
        stat = PairStat(pr.net_p, pr.net_n, lens[pr.net_p], lens[pr.net_n])
        stats.append(stat)
        if stat.delta > pr.tol:
            raise ValueError(
                f"差分对 {pr.net_p}/{pr.net_n} 等长超差: "
                f"|{stat.len_p:.3f} − {stat.len_n:.3f}| = {stat.delta:.3f}mm"
                f" > 容差 {pr.tol}mm（绕长补偿未实装，先见失败）")
    return segments, vias, reports, stats
