"""内层平面布线（M3-3c，v8 两相架构）：链首落孔 + 链尾铺铜。

叠层（spec ``board.layers`` 声明角色）：F.Cu/B.Cu 信号专用，
gnd_plane/power_plane 是内层平面。两相拆分（v7 教训：链首就铺铜，
之后 ~530 个链上/缝合孔戳穿领地，KiCad fill 给每个异网 TH 铜掏
r+0.254 的洞，0.5mm 细颈被逐个切断成铜岛——引擎瓦图自报连通，
KiCad DRC 判 67 条飞线）：

- 链首 ``plan_power_drops``：只产 ``vias``（SMD 盘落地孔 / BGA 盘中
  孔 M3-4）+ ``segments``（F.Cu 短 stub）。孔此刻落定，链上后续区与
  stitch 看得见、会避让；不产 zone；
- 链尾 ``route_planes_final``：merge + stitch 之后，在满铜板上铺
  ``zones``。锚点 = TH 盘瓦 + 全部同网孔瓦；领地 Voronoi + 协商融合
  （M3-3d，PathFinder 协商走廊）；连通语义 cut 感知——_carve_cuts 把
  被异网 TH 净空切断的瓦邻接边标成 cut，分量判定与走廊扩展均不许
  跨过（纸面连通 ≠ fill 连通）。单网平面 = 整板矩形 + cut 连通复核。
  残余碎片由外层铜桥缝合（_escape_bridges，电源平面分割的岛桥接）
  ——tile 取 0.5mm（0603 邻盘 1.0mm 间距的分辨粒度）。填充语义（热
  焊盘/反焊盘/孤岛移除）归 KiCad fill 引擎，我们只画领地边界。

落孔阶梯：近场 0.1mm 网格候选（盘心 0.8mm 内，距离升序）→ 盘中直落
→ 仍无解即 ValueError（不静默留断连，人工/M3-4 逃逸布线介入）。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import replace

from .astar import GridRouter, ObstacleIndex, RouteConfig
from .l1 import NetReport
from .model import Pad, RouteBoard, Segment, Via, Zone
from .regions import Region, _is_ground

TILE = 0.5           # Voronoi tile 边长（mm；0603 邻盘 1.0mm 间距的分辨粒度）
LANDING_R = 0.8      # 近场落孔搜索半径（mm，自盘心）
LANDING_GRID = 0.1   # 近场候选网格（mm）
VIP_VIA_SIZE = 0.4   # BGA 盘中孔铜径：0.8mm 节距 ø0.4 盘——邻球心 0.8 −
                     # 孔半径 0.2 − 球半径 0.2 = 铜净距 0.4 ≥ clearance 0.254
VIP_VIA_DRILL = 0.2  # 盘中孔钻径（JLC 4 层最小钻 0.2mm）
FINE_CLR = 0.127     # 细节距落孔净距（0.5mm 节距边盘铜隙 0.2mm 的可容口径）


# ---------------------------------------------------------------- 领地

def _tiling(
    net_pads: dict[str, list[Pad]],
    x1: float, y1: float, x2: float, y2: float,
) -> dict[tuple[int, int], str]:
    """tile → 网络归属（Voronoi 最近盘 + 包含校正）。

    初值按 tile 中心最近盘（距离/网名/盘 key 决胜）；校正步把每个盘
    所在 tile 强制归该盘网络——多盘争同 tile 时距 tile 心最近者胜。
    保证近场落孔在常见去耦布局（异网邻盘贴着摆）有本网领地。
    """
    tiles: dict[tuple[int, int], str] = {}
    for ix in range(math.floor(x1 / TILE), math.floor((x2 - 1e-9) / TILE) + 1):
        for iy in range(math.floor(y1 / TILE), math.floor((y2 - 1e-9) / TILE) + 1):
            cx, cy = (ix + 0.5) * TILE, (iy + 0.5) * TILE
            if not (x1 <= cx <= x2 and y1 <= cy <= y2):
                continue  # tile 心出板（板框非 tile 对齐时的边缘半格）
            best = min(
                (round(math.hypot(p.x - cx, p.y - cy), 6), net, p.key)
                for net, ps in net_pads.items() for p in ps
            )
            tiles[(ix, iy)] = best[1]
    claim: dict[tuple[int, int], tuple[float, str, str]] = {}
    for net, ps in net_pads.items():
        for p in ps:
            t = (math.floor(p.x / TILE), math.floor(p.y / TILE))
            d = round(math.hypot(p.x - (t[0] + 0.5) * TILE,
                                 p.y - (t[1] + 0.5) * TILE), 6)
            if t not in claim or (d, net, p.key) < claim[t]:
                claim[t] = (d, net, p.key)
    for t, (_, net, _) in claim.items():
        tiles[t] = net
    return tiles


def _tile_rects(
    tiles: dict[tuple[int, int], str], net: str,
) -> list[tuple[float, float, float, float]]:
    """同网 tile 集 → 矩形分解（横 span 贪心 + 同构行向下延伸）。

    输出量级 ≪ tile 数（紧凑领地几个矩形即覆盖），zone 块可读可审。
    """
    cells = {t for t, n in tiles.items() if n == net}
    rects: list[tuple[float, float, float, float]] = []
    while cells:
        ix, iy = min(cells)  # 字典序最小起点 → 确定性分解
        x0 = ix
        while (x0 - 1, iy) in cells:
            x0 -= 1
        x1 = ix
        while (x1 + 1, iy) in cells:
            x1 += 1
        y1 = iy
        while all((c, y1 + 1) in cells for c in range(x0, x1 + 1)):
            y1 += 1
        for c in range(x0, x1 + 1):
            for r in range(iy, y1 + 1):
                cells.discard((c, r))
        rects.append((x0 * TILE, iy * TILE, (x1 + 1) * TILE, (y1 + 1) * TILE))
    return rects


# ---------------------------------------------------------------- 融合

_FUSE_CAP = 400      # 单网单轮重布的分量合并轮次上限（每轮至少并掉一个）
_NEG_ITERS = 80      # 协商轮数：pres 线性涨把共享瓦挤到个位数钉住
_PRES_STEP = 2.0     # 每轮现值压力增量（恒定值冲突瓦游走不固化，实测配方）
_ZONE_CLR = 0.254    # zone 对异网铜净空（_zone_block 模板声明值）
_MIN_TH = 0.254      # zone min_thickness（模板值）：窄于此的细颈铜带被剪
_BLOCK = _ZONE_CLR + _MIN_TH / 2  # 瓦边通道判据：净空 + 半最小厚度


def _pt_seg_d(px: float, py: float,
              x1: float, y1: float, x2: float, y2: float) -> float:
    """点到线段距离（共享瓦边是 0.5mm 轴对齐段）。"""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(
        0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_rect_d(x1: float, y1: float, x2: float, y2: float,
                rx1: float, ry1: float, rx2: float, ry2: float) -> float:
    """轴对齐段 ↔ 轴对齐矩形距离（逐轴夹取，段轴对齐故 min/max 即可）。"""
    dx = max(rx1 - max(x1, x2), min(x1, x2) - rx2, 0.0)
    dy = max(ry1 - max(y1, y2), min(y1, y2) - ry2, 0.0)
    return math.hypot(dx, dy)


def _carve_cuts(
    board: RouteBoard, nets: tuple[str, ...],
) -> dict[str, set[frozenset]]:
    """异网 TH 铜（盘/孔）净空切断的 tile 邻接边，按受害网分组。

    KiCad fill 给 zone 内每个异网 TH 铜掏 r+0.254 的圆洞，再被
    min_thickness 剪掉窄于 0.254 的细颈——共享瓦边要过得去铜，离洞心
    必须 ≥ r+0.254+0.127。瓦图连通语义据此修正（v7 教训：忽视它则
    0.5mm 细颈被 530 个穿越孔逐个切断，KiCad 判 67 孤岛而引擎自报绿）。
    """
    def _pad_r(p: Pad) -> float:
        # 盘外沿半径：矩形族取半对角线（角最远，保守侧多算 cut 少假绿）
        if p.shape in ("rect", "roundrect"):
            return math.hypot(p.w, p.h) / 2
        return max(p.w, p.h) / 2

    items = [(v.x, v.y, v.size / 2, v.net) for v in board.vias]
    items += [(p.x, p.y, _pad_r(p), p.net)
              for p in board.pads if p.is_th and p.net]
    out: dict[str, set[frozenset]] = {n: set() for n in nets}
    for x, y, r, n_i in items:
        cr = r + _BLOCK
        for tx in range(math.floor((x - cr) / TILE),
                        math.floor((x + cr) / TILE) + 1):
            for ty in range(math.floor((y - cr) / TILE),
                            math.floor((y + cr) / TILE) + 1):
                bx1, by1 = tx * TILE, ty * TILE
                for edge in (((bx1 + TILE, by1), (bx1 + TILE, by1 + TILE)),
                             ((bx1, by1), (bx1, by1 + TILE)),
                             ((bx1, by1), (bx1 + TILE, by1)),
                             ((bx1, by1 + TILE), (bx1 + TILE, by1 + TILE))):
                    if _pt_seg_d(x, y, *edge[0], *edge[1]) < cr:
                        # 边界段两端点所在的两瓦共享此边
                        t = (math.floor(edge[0][0] / TILE),
                             math.floor(edge[0][1] / TILE))
                        nt = (math.floor(edge[1][0] / TILE),
                              math.floor(edge[1][1] / TILE))
                        for n_v in nets:
                            if n_v != n_i:
                                out[n_v].add(frozenset((t, nt)))
    return out


def _territory_cuts(
    terr: dict[tuple[int, int], str], nets: tuple[str, ...],
) -> dict[str, set[frozenset]]:
    """异网领地边界（zone-zone 净空 + min_thickness）切断的同网邻接边。

    两领地共享瓦边界 → KiCad 双方各退净空后中间无铜带宽 ≈ 边界两侧
    0.254，再剪细颈：同网走廊要过铜，瓦边离异网瓦区必须 ≥ 0.381——
    1 瓦宽（0.5mm）夹缝走廊两侧贴异网，纵向边距 0 全 cut（KiCad 里
    就是死铜）；≥2 瓦宽走廊中线 0.5 ≥ 0.381 存活。_widen 的 ≥3 瓦
    增肥与此对齐，窄缝残余交逃逸桥。"""
    out: dict[str, set[frozenset]] = {n: set() for n in nets}
    for (tx, ty), net in terr.items():
        if net not in out:
            continue
        for nt, seg in (
            ((tx + 1, ty), ((tx + 1) * TILE, ty * TILE,
                            (tx + 1) * TILE, (ty + 1) * TILE)),
            ((tx, ty + 1), (tx * TILE, (ty + 1) * TILE,
                            (tx + 1) * TILE, (ty + 1) * TILE)),
        ):
            if terr.get(nt) != net:
                continue  # 只审同网邻接边
            ex1, ey1, ex2, ey2 = seg
            lox, hix = min(tx, nt[0]) - 1, max(tx, nt[0]) + 1
            loy, hiy = min(ty, nt[1]) - 1, max(ty, nt[1]) + 1
            for hx in range(lox, hix + 1):
                for hy in range(loy, hiy + 1):
                    hn = terr.get((hx, hy))
                    if hn is None or hn == net:
                        continue
                    if _seg_rect_d(ex1, ey1, ex2, ey2,
                                   hx * TILE, hy * TILE,
                                   (hx + 1) * TILE, (hy + 1) * TILE) < _BLOCK:
                        out[net].add(frozenset(((tx, ty), nt)))
                        break
                else:
                    continue
                break  # 已 cut，下一条边
    return out


def _components(
    tiles: dict[tuple[int, int], str], net: str,
    cut: set[frozenset] | None = None,
) -> list[set[tuple[int, int]]]:
    """net 瓦集的 4 连通分量（行先序种子，确定性）。

    cut = 被异网铜净空切断的邻接边（frozenset 瓦对）——分量语义对齐
    KiCad fill 的真实分岛，而非瓦纸面连通。
    """
    seen: set[tuple[int, int]] = set()
    comps: list[set[tuple[int, int]]] = []
    for seed in sorted(t for t, n in tiles.items() if n == net):
        if seed in seen:
            continue
        comp, stack = {seed}, [seed]
        seen.add(seed)
        while stack:
            t = stack.pop()
            for nt in ((t[0] + 1, t[1]), (t[0] - 1, t[1]),
                       (t[0], t[1] + 1), (t[0], t[1] - 1)):
                if nt in seen or tiles.get(nt) != net:
                    continue
                if cut is not None and frozenset((t, nt)) in cut:
                    continue
                seen.add(nt)
                comp.add(nt)
                stack.append(nt)
        comps.append(comp)
    return comps


def _route_net(
    occ: dict[tuple[int, int], set],
    hist: dict[tuple[int, int], float],
    pres: float,
    net: str,
    anchors: dict[str, set[tuple[int, int]]],
    hard: set[tuple[int, int]],
    lox: int, loy: int, hix: int, hiy: int,
    cap: int,
    cut: set[frozenset] | None = None,
) -> bool:
    """单网重布：锚点分量循环拼接到单分量（多源 Dijkstra，确定性堆
    键）。瓦代价 = 自网/free 瓦 1.0，异网占用瓦 1.0 + k×pres + hist[t]
    （k = 占用网数）——PathFinder 协商的现值 + 历史双重压力。blocked =
    异网锚点瓦（硬拓扑，锚点零缝交错时不可越）。无路（锚点通路被异网
    锚点瓦封死）返回 False。

    cut = 本网被异网铜净空切断的邻接边：走廊跨 cut 边 = KiCad fill 在
    该处无铜，锚点纸面连通、电气断连——分量判定与 Dijkstra 扩展都
    不许过（cut 感知，v7 分岛教训）。"""
    blocked = hard - anchors[net]
    for _ in range(cap):
        tiles = {t: net for t in anchors[net]}
        for t, s in occ.items():
            if net in s:
                tiles[t] = net
        comps = [c for c in _components(tiles, net, cut) if c & anchors[net]]
        if len(comps) <= 1:
            return True
        src = max(comps, key=lambda c: (len(c & anchors[net]), len(c), min(c)))
        targets = set().union(*(c for c in comps if c != src))
        own = {t for t, s in occ.items() if net in s} | anchors[net]
        dist = {t: 0.0 for t in src}
        par: dict[tuple[int, int], tuple[int, int]] = {}
        pq: list[tuple[float, tuple[int, int]]] = [(0.0, t) for t in sorted(src)]
        hit = None
        while pq:
            d, t = heapq.heappop(pq)
            if d > dist.get(t, math.inf):
                continue
            if t in targets and t not in src:
                hit = t
                break
            cx, cy = t
            for nt in ((cx + 1, cy), (cx - 1, cy),
                       (cx, cy + 1), (cx, cy - 1)):
                if not (lox <= nt[0] <= hix and loy <= nt[1] <= hiy):
                    continue
                if nt in blocked:
                    continue
                if cut is not None and frozenset((t, nt)) in cut:
                    continue  # 净空切断边：fill 无铜，跨过即假连通
                k = len(occ.get(nt, ()))
                step = 1.0 if (nt in own or k == 0) else (
                    1.0 + k * pres + hist.get(nt, 0.0))
                nd = d + step
                if nd < dist.get(nt, math.inf):
                    dist[nt] = nd
                    par[nt] = t
                    heapq.heappush(pq, (nd, nt))
        if hit is None:
            return False
        t = hit
        while True:
            occ.setdefault(t, set()).add(net)
            if t in src:
                break
            t = par[t]
    return False


def _widen(
    terr: dict[tuple[int, int], str],
    occ: dict[tuple[int, int], set],
    anchors: dict[str, set[tuple[int, int]]],
    hard: set[tuple[int, int]],
    lox: int, loy: int, hix: int, hiy: int,
) -> None:
    """骨架增肥：occ 瓦的 8 邻瓦收归本网——当且仅当该瓦在界内、非
    occ/hard、且其 8 邻无任何异网 occ 瓦或异网锚点瓦（无争用，免排序
    分歧；两骨架夹缝瓦保持 Voronoi 归属）。融合走廊宽 1 瓦（0.5mm），
    两侧 zone clearance 各啃 0.254mm 后存活铜太窄；增肥把无争用走廊顶
    到 ≥3 瓦（≥0.99mm），窄缝处仍 1 瓦——电上真值由 KiCad fill 引擎
    复核（可见性优先：先给眼睛看，引擎说话）。"""
    anchor_of: dict[tuple[int, int], set] = {}
    for net, ts in anchors.items():
        for t in ts:
            anchor_of.setdefault(t, set()).add(net)
    for t in sorted(occ):
        net = next(iter(occ[t]))
        cx, cy = t
        for nt in ((cx + dx, cy + dy)
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            if not (lox <= nt[0] <= hix and loy <= nt[1] <= hiy):
                continue
            if nt in occ or nt in hard:
                continue
            ok = True
            for n2 in ((nt[0] + dx, nt[1] + dy)
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                if occ.get(n2) and net not in occ[n2]:
                    ok = False  # 异网骨架贴身：收了即制造 1 瓦窄缝
                    break
                if n2 in anchor_of and net not in anchor_of[n2]:
                    ok = False  # 异网锚点贴身：同上
                    break
            if ok:
                terr[nt] = net


def _fuse(
    terr: dict[tuple[int, int], str],
    anchors: dict[str, set[tuple[int, int]]],
    hard: set[tuple[int, int]],
    x1: float, y1: float, x2: float, y2: float,
    cap: int = _FUSE_CAP,
    cuts: dict[str, set[frozenset]] | None = None,
) -> dict[str, list[set[tuple[int, int]]]]:
    """领地融合（M3-3d）：同网全部锚点瓦 4 连通化，PathFinder 协商式。
    返回 escapes = {net: 有锚分量列表}——判决后领地仍多于一个有锚分
    量的网（Voronoi 团块天然连通者不触发），交 _escape_bridges 走外
    层铜桥缝合。hard = 全网锚点瓦并集 = 真碰内层的铜（TH 盘/过孔穿
    透；SMD 盘瓦不算墙）。cuts = _carve_cuts 的逐网净空切断边——分量
    判定、Dijkstra 扩展、孤岛剪枝、逃逸判决全部 cut 感知（v7 教训：
    纸面连通 ≠ fill 连通，0.5mm 细颈被穿越孔逐个切断而引擎自报绿）。

    全部网同场逐轮 rip-up 重布：每轮按口袋紧度序（最憋屈的网先布）
    布网，其余网占瓦显代价（现值 pres 线性涨 + 冲突瓦历史累加），共
    享瓦越挤越贵、逐轮互相避让——单网串行方案（脊柱冻结/抢道修复/
    换序）在密孔板上结构性死锁（先融的墙必围死后融的），协商无此不
    变量。实测 _NEG_ITERS 轮把共享瓦挤到个位数后钉住：残余冲突瓦的
    墙由异网锚点瓦构成（硬拓扑，纯重排不可解），非算法缺陷。

    收敛或轮尽即判决定格：冲突瓦归字典序最小网（确定性），败者领地
    被切成多个有锚分量——电源平面分割的岛桥接是常规工程实践，外层
    铜桥见 _escape_bridges。epilogue：occ 唯一归属写回 terr（Voronoi
    团块保留原归属）→ _widen 增肥 → 无锚分量剪枝（死铜）→ escapes。

    单网无路（锚点通路被异网锚点瓦/cut 边封死）不 raise：该网保留
    Voronoi 归属、碎片降级进 escapes 走铜桥——密孔板上这是可自动愈
    合的拓扑，毙掉整个 merge 才是事故。"""
    lox, loy = math.floor(x1 / TILE), math.floor(y1 / TILE)
    hix, hiy = (math.floor((x2 - 1e-9) / TILE),
                math.floor((y2 - 1e-9) / TILE))
    cuts = cuts or {}

    def _pocket(net: str) -> int:
        """网内最紧锚点的自由 4 邻数（异网锚点瓦/板外不算自由路）。"""
        worst = 4
        for a in anchors[net]:
            free = sum(
                1 for n in ((a[0] + 1, a[1]), (a[0] - 1, a[1]),
                            (a[0], a[1] + 1), (a[0], a[1] - 1))
                if lox <= n[0] <= hix and loy <= n[1] <= hiy and n not in hard)
            if free < worst:
                worst = free
                if worst == 0:
                    break
        return worst

    # 口袋紧度升序：最憋屈的网（存在只剩 0~1 条自由邻路的锚点，如去耦
    # 电容群/BGA 球阵口袋）先布——细命脉先画；宽敞大网选项多，晚布也
    # 能改道。紧度同分再按锚点数降序（大网走廊需求多）。
    order = sorted(anchors, key=lambda n: (_pocket(n), -len(anchors[n]), n))
    hist: dict[tuple[int, int], float] = {}
    occ: dict[tuple[int, int], set] = {}
    for it in range(_NEG_ITERS):
        pres = 1.0 + it * _PRES_STEP
        for net in order:
            for t in [t for t, s in occ.items() if net in s]:
                s = occ[t]
                s.discard(net)
                if not s:
                    del occ[t]  # rip-up：清本网占瓦，其余网现场保留
            _route_net(occ, hist, pres, net, anchors, hard,
                       lox, loy, hix, hiy, cap, cuts.get(net))
            # 无路不 raise：碎片降级 escapes（见 docstring）
        conflicts = {t: s for t, s in occ.items() if len(s) > 1}
        if not conflicts:
            break  # 零共享：全体单分量，协商收敛
        for t, s in conflicts.items():
            hist[t] = hist.get(t, 0.0) + 1.0 + 0.5 * (len(s) - 1)  # 历史累加
    for t in [t for t, s in occ.items() if len(s) > 1]:
        occ[t] = {min(occ[t])}  # 判决：字典序定格，败者碎片走铜桥
    for t, s in occ.items():
        terr[t] = next(iter(s))  # 走廊瓦唯一归属覆盖 Voronoi
    _widen(terr, occ, anchors, hard, lox, loy, hix, hiy)
    for net in sorted(anchors):  # 孤岛剪枝：无锚分量 = 死铜（cut 感知）
        for comp in _components(terr, net, cuts.get(net)):
            if not comp & anchors[net]:
                for t in comp:
                    del terr[t]
    escapes: dict[str, list[set[tuple[int, int]]]] = {}
    for net in sorted(anchors):
        comps = [c for c in _components(terr, net, cuts.get(net))
                 if c & anchors[net]]
        if len(comps) > 1:
            escapes[net] = comps
    return escapes


def _escape_bridges(
    board: RouteBoard, cfg: RouteConfig, widths: dict[str, float],
    escapes: dict[str, list[set[tuple[int, int]]]],
    live: dict[str, list[Pad]],
    vias: list[Via],
    segments: list[Segment],
    layers: tuple[str, ...] = (),
    pool: tuple[Via, ...] = (),
) -> dict[str, float]:
    """跨层逃逸桥：领地碎片网在 B.Cu 走铜桥缝合——每网取分量间最近
    的既有端点对（本网落地孔/盘中孔或 TH 盘，全部穿透 B.Cu），合成
    Pad 交 GridRouter A* 布线（确定性，同网铜豁免异网阻挡）；桥后分
    量组并组重扫最近对，直至单组。返回 {net: 桥总长}。

    端点 = 既有铜而非新孔：电气路径 SMD 盘 → stub → 孔 → 内层碎片A
    → 孔a → B.Cu 桥 → 孔b → 内层碎片B，ratsnest 全连通（电源平面分
    割的岛桥接语义，真实工程实践）。pool = 板上既有同网孔（终相的
    锚点孔，已在文件里，只作端点不重复写回；vias 仅收新桥铜）。"""
    w_map = dict(widths)
    for net in escapes:
        w_map.setdefault(net, cfg.width)
    router = GridRouter(board, cfg, w_map, layers=layers)
    router.index.add(segments, vias)  # 本区已落铜（stub/孔）入障碍索引
    lens: dict[str, float] = {}
    for net in sorted(escapes):
        groups: list[set[tuple[float, float]]] = []
        for comp in escapes[net]:
            pts = {(v.x, v.y) for v in (*vias, *pool) if v.net == net
                   and (math.floor(v.x / TILE), math.floor(v.y / TILE)) in comp}
            pts |= {(p.x, p.y) for p in live[net] if p.is_th
                    and (math.floor(p.x / TILE), math.floor(p.y / TILE)) in comp}
            groups.append(pts)
        length = 0.0
        while len(groups) > 1:
            # 全对全端点按距离升序（距离/坐标决胜，确定性），逐对逐层
            # 试布：最近对可能被异网铜封死，次近对/另一外层常有活路
            pairs = sorted(
                (round(math.hypot(a[0] - b[0], a[1] - b[1]), 6), a, b, i, j)
                for i in range(len(groups))
                for j in range(i + 1, len(groups))
                for a in sorted(groups[i]) for b in sorted(groups[j]))
            routed = False
            for _, a, b, i, j in pairs:
                # 层选序：B（通常最空）→ F → 内层信号层（N 层板 virgin，
                # 几乎必通；2 层板退化为 B/F 双选）
                outer = ("B.Cu", "F.Cu")
                for layer in outer + tuple(l for l in layers
                                           if l not in outer):
                    pa = Pad(ref=f"{net}-esc", num="1", net=net, x=a[0],
                             y=a[1], w=cfg.via_size, h=cfg.via_size,
                             shape="circle", layers=(layer,))
                    pb = Pad(ref=f"{net}-esc", num="2", net=net, x=b[0],
                             y=b[1], w=cfg.via_size, h=cfg.via_size,
                             shape="circle", layers=(layer,))
                    try:
                        segs, vs = router.route(net, pa, pb)
                    except ValueError:
                        continue  # 此对此层封路：换下一组合
                    router.commit(segs, vs)
                    segments.extend(segs)
                    vias.extend(vs)
                    length += sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1)
                                  for s in segs)
                    groups[i] |= groups[j]
                    del groups[j]
                    routed = True
                    break
                if routed:
                    break
            if not routed:
                raise ValueError(
                    f"网 {net} 逃逸桥无路：全部分量端点对在外层均被异网铜"
                    f"封死（需人工挪盘或手布桥）")
        lens[net] = length
    return lens


# ---------------------------------------------------------------- 落孔

def _landing(
    board: RouteBoard, idx: ObstacleIndex, pad: Pad,
    cfg: RouteConfig, stub_w: float,
) -> tuple[float, float]:
    """SMD 盘的平面落地孔位：近场网格候选 → 盘中直落 → ValueError。

    只查铜净距（F/B 双面 + stub 沿线），不查领地——孔瓦在融合阶段
    （M3-3d）强制归本网，领地跟着孔走；穿越异网领地由 KiCad fill 的
    反焊盘让开，与盘中孔同一语义。"""
    ox1, oy1, ox2, oy2 = board.outline
    vr = cfg.via_size / 2 + cfg.clearance  # 孔铜净距（与 GridRouter 同口径）

    def _ok(vx: float, vy: float) -> bool:
        if not (ox1 + vr <= vx <= ox2 - vr and oy1 + vr <= vy <= oy2 - vr):
            return False  # 孔铜出板
        if not (idx.point_clear(vx, vy, "F.Cu", pad.net, vr)
                and idx.point_clear(vx, vy, "B.Cu", pad.net, vr)):
            return False  # 与既有铜（异网盘/线/孔）净距不足
        return idx.edge_clear(pad.x, pad.y, vx, vy, "F.Cu", pad.net,
                              stub_w / 2 + cfg.clearance)

    # 1. 近场候选：盘心周围 0.1mm 网格、半径 ≤ reach，距离升序。
    #    封装决定参数：盘比 LANDING_R 还长（QFP/SOIC 边盘 1.6×0.3mm）时
    #    固定圆盘盖不到尖端外净空位（孔心要过尖端 vr+一格），按盘半长
    #    外扩；短盘（0.4 球/0.6 阻容/1×1）保持校准过的 LANDING_R 不动
    #    （LQFP 板实踩：U1-29 落不了孔）。
    half = max(pad.w, pad.h) / 2
    reach = half + vr + LANDING_GRID if half >= LANDING_R else LANDING_R
    rng = math.ceil(reach / LANDING_GRID)
    cands = sorted(
        (round(math.hypot(ix * LANDING_GRID - pad.x, iy * LANDING_GRID - pad.y), 6),
         ix, iy)
        for ix in range(round(pad.x / LANDING_GRID) - rng,
                        round(pad.x / LANDING_GRID) + rng + 1)
        for iy in range(round(pad.y / LANDING_GRID) - rng,
                        round(pad.y / LANDING_GRID) + rng + 1)
    )
    for d, ix, iy in cands:
        if d <= reach and _ok(ix * LANDING_GRID, iy * LANDING_GRID):
            return ix * LANDING_GRID, iy * LANDING_GRID
    # 2. 盘中直落（盘心即本网铜内；领地有效 + 邻铜净距足即可）
    if _ok(pad.x, pad.y):
        return pad.x, pad.y
    raise ValueError(
        f"{pad.key}（网 {pad.net}）近场 {reach:.2f}mm 与盘中均无法落平面孔"
        f"——领地被挤/邻铜太密，需人工或 M3-4 逃逸布线介入"
    )


# ---------------------------------------------------------------- 主入口

def plan_power_drops(
    board: RouteBoard, region: Region, spec,
    widths: dict[str, float] | None = None,
    config: RouteConfig | None = None,
) -> tuple[list[Segment], list[Via], list[NetReport]]:
    """planes 链首相（v8 架构）：只落孔，不铺铜。

    产 BGA 盘中孔 + SMD 盘落地孔 + F.Cu stub——孔此刻落定，链上后续
    区与 stitch 看得见它们（避让）；zone 完全推迟到链尾
    route_planes_final：领地在满铜板上才算，异网穿越孔的净空切断边
    （KiCad fill 掏洞）在连通判定里生效（v7 教训：先铺铜后打孔，
    0.5mm 细颈被 530 个孔逐个切断，KiCad 判 67 孤岛而引擎自报绿）。

    地网 → spec.gnd_plane 层角色，非地电源网 → spec.power_plane 层
    （spec 未声明的角色整层跳过）。返回 (segments, vias, reports)。
    """
    cfg = config or RouteConfig()
    widths = widths or {}
    board.inherit_nets()
    by_net = board.pads_by_net()
    bga = board.bga_refs()
    if cfg.via_size < VIP_VIA_SIZE:
        raise ValueError(
            f"config.via_size {cfg.via_size} < 盘中孔 {VIP_VIA_SIZE}"
            "——索引 margin 不足以审盘中孔净距")
    ox1, oy1, ox2, oy2 = board.outline

    grounds = tuple(sorted(n for n in region.nets if _is_ground(n)))
    powers = tuple(sorted(n for n in region.nets if not _is_ground(n)))

    segments: list[Segment] = []
    vias: list[Via] = []
    reports: list[NetReport] = []

    for _layer, nets in ((spec.gnd_plane, grounds), (spec.power_plane, powers)):
        if _layer is None or not nets:
            continue
        missing = sorted(n for n in nets if n not in by_net)
        if missing:
            raise ValueError(f"planes 区域的网络在板上无焊盘: {missing}")
        live = {n: sorted(by_net[n], key=lambda p: p.key) for n in nets}
        idx = ObstacleIndex(board, cfg.via_size / 2 + cfg.clearance)
        for net in sorted(live):
            w = max(widths.get(net, 0.0), cfg.width)
            n_via = 0
            length = 0.0
            for pad in live[net]:
                if pad.is_th:
                    continue  # TH 盘穿透内层，zone 直连
                if pad.ref in bga:
                    # 盘中孔（M3-4）：孔即盘心、无 stub。查询半径 0.454 ≤
                    # 索引 margin（默认配置 0.554）；穿透异网电源领地由
                    # KiCad zone fill 的反焊盘让开。
                    vr = VIP_VIA_SIZE / 2 + cfg.clearance
                    if not (ox1 + vr <= pad.x <= ox2 - vr
                            and oy1 + vr <= pad.y <= oy2 - vr):
                        raise ValueError(f"{pad.key}（网 {net}）盘中孔出板框")
                    if not (idx.point_clear(pad.x, pad.y, "F.Cu", net, vr)
                            and idx.point_clear(pad.x, pad.y, "B.Cu", net, vr)):
                        raise ValueError(
                            f"{pad.key}（网 {net}）盘中孔与邻铜净距不足")
                    via = Via(pad.x, pad.y, VIP_VIA_SIZE, VIP_VIA_DRILL, net)
                    vias.append(via)
                    idx.add([], [via])
                    n_via += 1
                    continue
                try:
                    vx, vy = _landing(board, idx, pad, cfg, w)
                    vsize, vdrill = cfg.via_size, cfg.via_drill
                except ValueError:
                    # 封装决定参数：细节距周边盘（0.5mm LQFP 边盘，铜隙
                    # 0.2mm）在默认孔 0.6/净距 0.25 下走廊里放不下任何
                    # 孔——降级盘中孔规格 0.4/0.2 + 网类净距 0.127 重试
                    # （尖端外 ~1.2mm 恰好落位；索引 margin 0.55 ≥ 细
                    # 口径 0.327，无需重建）。
                    fine = replace(cfg, via_size=VIP_VIA_SIZE,
                                   via_drill=VIP_VIA_DRILL, clearance=FINE_CLR)
                    vx, vy = _landing(board, idx, pad, fine, w)
                    vsize, vdrill = VIP_VIA_SIZE, VIP_VIA_DRILL
                via = Via(vx, vy, vsize, vdrill, net)
                stub = Segment(pad.x, pad.y, vx, vy, w, "F.Cu", net)
                vias.append(via)
                segments.append(stub)
                idx.add([stub], [via])
                n_via += 1
                length += math.hypot(vx - pad.x, vy - pad.y)
            reports.append(NetReport(
                net, len(by_net[net]), 0, n_via, n_via, round(length, 3), 0))
    return segments, vias, reports


def route_planes_final(
    board: RouteBoard, region: Region, spec,
    widths: dict[str, float] | None = None,
    config: RouteConfig | None = None,
) -> tuple[list[Segment], list[Via], list[Zone], list[NetReport]]:
    """planes 链尾相（v8 架构）：满铜板上铺平面 zone。

    前提：链首相 plan_power_drops 的孔已在板上（region 板写入 + merge
    合并），merge 缝合（stitch_gnd）已跑——本相看到的是全部穿越铜。
    锚点 = TH 盘瓦 + 全部同网孔瓦（含链上信号转孔、缝合孔：凡同网铜
    真碰平面层者皆锚）；cut = _carve_cuts 的异网 TH 净空切断边——分
    量判定与融合走廊均不许跨 cut 边（纸面连通 ≠ fill 连通）。残余碎
    片走 _escape_bridges 外层铜桥，桥失败的网记 reports.deferred 交给
    DRC 口径复核，不毙 merge。返回 (segments, vias, zones, reports)。
    """
    cfg = config or RouteConfig()
    widths = widths or {}
    board.inherit_nets()
    by_net = board.pads_by_net()
    ox1, oy1, ox2, oy2 = board.outline

    grounds = tuple(sorted(n for n in region.nets if _is_ground(n)))
    powers = tuple(sorted(n for n in region.nets if not _is_ground(n)))
    layers = tuple(getattr(spec, "signal_layers", ()) or ())

    segments: list[Segment] = []
    vias: list[Via] = []
    zones: list[Zone] = []
    reports: list[NetReport] = []

    for layer, nets in ((spec.gnd_plane, grounds), (spec.power_plane, powers)):
        if layer is None or not nets:
            continue
        missing = sorted(n for n in nets if n not in by_net)
        if missing:
            raise ValueError(f"planes 区域的网络在板上无焊盘: {missing}")
        live = {n: sorted(by_net[n], key=lambda p: p.key) for n in nets}
        cuts = _carve_cuts(board, nets)
        pool = tuple(v for v in board.vias if v.net in live)
        # 锚点 = 真碰本层的同网铜：TH 盘 + 全部同网孔（链首落地孔、链上
        # 信号转孔、缝合孔）。SMD 盘不碰内层，由旁边落地孔瓦代表。
        anchors = {
            net: ({(math.floor(p.x / TILE), math.floor(p.y / TILE))
                   for p in live[net] if p.is_th}
                  | {(math.floor(v.x / TILE), math.floor(v.y / TILE))
                     for v in pool if v.net == net})
            for net in live}
        blens: dict[str, float] = {}
        deferred: dict[str, int] = {}
        poly_n: dict[str, int] = {}
        if len(live) == 1:
            net = next(iter(live))
            zones.append(Zone(net, layer, (
                ((ox1, oy1), (ox2, oy1), (ox2, oy2), (ox1, oy2)),)))
            poly_n[net] = 1
            # 整板单 zone 也要 cut 复核：异网穿越孔把大平面掏成岛，同网
            # 锚点跨岛即飞线（v7 的 GND 缝合孔救了大部分，残余靠桥）
            terr = {}
            for ix in range(math.floor(ox1 / TILE),
                            math.floor((ox2 - 1e-9) / TILE) + 1):
                for iy in range(math.floor(oy1 / TILE),
                                math.floor((oy2 - 1e-9) / TILE) + 1):
                    if (ox1 <= (ix + 0.5) * TILE <= ox2
                            and oy1 <= (iy + 0.5) * TILE <= oy2):
                        terr[(ix, iy)] = net
            comps = [c for c in _components(terr, net, cuts.get(net))
                     if c & anchors[net]]
            if len(comps) > 1:
                try:
                    blens = _escape_bridges(
                        board, cfg, widths, {net: comps}, live, vias,
                        segments, layers=layers, pool=pool)
                except ValueError as e:
                    print(f"⚠ planes 终相 {net}: {e}", flush=True)
                    deferred[net] = len(comps) - 1
        else:
            terr = _tiling(live, ox1, oy1, ox2, oy2)
            hard = set().union(*anchors.values())
            for net in sorted(live):
                for p in live[net]:  # 全部盘瓦强制归属（含 SMD：覆盖美观）
                    terr[(math.floor(p.x / TILE),
                          math.floor(p.y / TILE))] = net
                for t in anchors[net]:
                    terr[t] = net
            # 切断边两源合并：TH 铜掏洞 + 领地边界净空（后者在 Voronoi
            # 初稿上算，融合走廊改归属后的新边界由 _widen ≥3 瓦增肥兜底）
            tcuts = _territory_cuts(terr, nets)
            for net in live:
                cuts[net] |= tcuts[net]
            escapes = _fuse(terr, anchors, hard, ox1, oy1, ox2, oy2,
                            cuts=cuts)
            if escapes:  # 判决碎片：外层铜桥缝合（电源平面岛桥接）
                try:
                    blens = _escape_bridges(
                        board, cfg, widths, escapes, live, vias, segments,
                        layers=layers, pool=pool)
                except ValueError as e:
                    print(f"⚠ planes 终相: {e}", flush=True)
                    deferred = {n: len(cs) - 1 for n, cs in escapes.items()}
            for net in sorted(live):
                polys = tuple(
                    ((rx1, ry1), (rx2, ry1), (rx2, ry2), (rx1, ry2))
                    for rx1, ry1, rx2, ry2 in _tile_rects(terr, net)
                )
                if polys:
                    zones.append(Zone(net, layer, polys))
                    poly_n[net] = len(polys)
        for net in sorted(live):
            reports.append(NetReport(
                net, len(by_net[net]), poly_n.get(net, 0), 0,
                sum(1 for v in pool if v.net == net),
                round(blens.get(net, 0.0), 3), deferred.get(net, 0)))
    return segments, vias, zones, reports


def route_planes(
    board: RouteBoard, region: Region, spec,
    widths: dict[str, float] | None = None,
    config: RouteConfig | None = None,
) -> tuple[list[Segment], list[Via], list[Zone], list[NetReport]]:
    """兼容入口（单板两相连跑，测试/独立调用用；链式流程分两处调）：
    先落孔（孔/stub 注入 board 作终相锚点与障碍），再满铜铺平面。"""
    segments, vias, reports = plan_power_drops(
        board, region, spec, widths, config)
    board.vias.extend(vias)
    board.segments.extend(segments)
    fsegs, fvias, zones, freports = route_planes_final(
        board, region, spec, widths, config)
    return (segments + fsegs, vias + fvias, zones, reports + freports)

