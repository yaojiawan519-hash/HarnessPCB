"""经典精修与合法化——DesignDocument §3.6 / M5。

学习负责全局粗放（BC/RL 贪心解码的落点），本模块用经典方法收拾细节：

1. **力导向松弛**：重叠斥力（沿最小重叠轴推开）+ net 吸引力（可动件向其
   已放邻居质心收拢）+ 越界斥力（拉回板内），小步迭代、逐步 snap 到栅格；
2. **确定性残余重叠消解**：对仍重叠/越界的可动件，围绕当前位置螺旋搜索
   最近的合法栅格点安置，保证结束时 AABB 零重叠 + 不越界（DRC 干净的前提）。

只消费 ``PlacementProblem`` 与 ``pos: {ref: (x,y,rot)}``，与 KiCad 解耦。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..env.problem import (PlacementProblem, aabb, overlap_area, hpwl, pin_abs,
                           anchor_refs, anchor_point, channel_members,
                           power_decoup_refs, power_decoup_anchor,
                           dedicated_decoup_refs, power_ic_refs)

__all__ = ["LegalizeConfig", "refine", "metrics"]


@dataclass
class LegalizeConfig:
    iters: int = 200             # 力导向迭代轮数
    grid: float = 1.27           # 栅格间距（KiCad 50 mil）
    lam_net: float = 0.1         # net 吸引力系数（0.04 太弱，星形枢纽漂移、
                                 # 晶体被推离 MCU；0.1 使关键网邻接稳定且 HPWL 更低）
    lam_pin: float = 0.5         # 时钟器件引脚粘连系数：晶体被强力拉向 MCU 振荡引脚
                                 # （随 MCU 移动而跟随），压过负载电容/密度的推挤
    lam_decoup: float = 0.15     # 电源去耦电容「最近 IC 电源引脚」弱引力：把 plane 网
                                 # （GND/+3V3 数十 refs，质心偏板心/固定连接器侧）拽离
                                 # 所属 IC 的去耦电容（C1–C12 实测漂到离 U1 20–30mm）
                                 # 贴回其电源引脚。远小于时钟 lam_pin（0.5），且不登记
                                 # anchor_refs（不跳块内聚、不升 tier-1），只作叠加的柔和
                                 # 引力，避免 17 颗 MCU 电容过约束 U1、拽断以太网网。
    lam_decoup_dedicated: float = 0.0  # 专属轨去耦电容（全部非 GND 电源轨上的 IC 唯一，
                                 # dedicated_decoup_refs 划定）的单独系数：0=沿用
                                 # lam_decoup（默认，浮点路径逐位不变）；>0 时仅这类
                                 # 电容改用此系数——共享轨（多 IC 轨）电容仍走
                                 # lam_decoup，不受全局提系数的锚点翻转误伤
                                 # （ecat C8/C10 ±8–12mm 实测）。
    lam_block: float = 0.05      # 块内聚系数（分图语义化）：同功能块的「松散被动件」
                                 # 向其块锚点（IC + 固定连接器）收拢，把 5 个功能块分成
                                 # 独立区域（power 聚到 J1/U2/U3、ethercat 聚到 J2/J3、
                                 # can 聚到 J6/J7/U4/U5），压过全局 HPWL 的「全拉最短」
                                 # 把整板挤成一团的倾向。只动 passive，不动大 IC（锚点）。
    lam_density: float = 0.03    # 密度扩散系数（0=关）：把可动件从密集区推向空旷区
    density_bin: float = 8.0     # 密度网格边长 (mm)
    density_max: float = 0.5     # 每轮密度推力上限 (mm)，防止撕裂关键网邻接
    lam_edge_ic: float = 0.0     # 原则①支撑 IC 贴边（0=关，默认关=A/B 隔离）：非主、
                                 # 非锚定可动 IC 向最近板边带弱吸引（只拉不推，已在带
                                 # 内不受力；每轮上限 _EDGE_IC_CAP 防压过网吸引）。EtherCAT
                                 # U4/U6 类支撑 IC 基线距边 ~19mm 的对症项。
    lam_ic_spread: float = 0.0   # 原则⑥主芯片均匀（0=关）：可动 IC 两两 AABB 间隙低于
                                 # _IC_SPREAD_MARGIN 时沿体心连线软斥（只推开不拉近；
                                 # 间隙<0 已由重叠斥力管，不重复施力）。与全局 lam_density
                                 # 的差别：IC 专属、按间隙线性、不管被动件。
    step: float = 0.1            # 每轮位移缩放（0.1 阻尼：1.0 会让无界重叠推挤把 U1 甩到板边）
    seed: int = 0


def _snap(v: float, g: float) -> float:
    return round(v / g) * g


def _boxes(problem: PlacementProblem, pos: dict) -> dict:
    return {r: aabb(problem.by_ref[r], *pos[r]) for r in problem.refs}


def _overlap_push(ba, bb, rng) -> tuple[float, float]:
    """两 AABB 重叠时，``a`` 相对 ``b`` 的推开向量（沿更窄重叠轴，幅值=重叠深度）。

    可动↔可动各推一半（双方反向施加），可动↔固定只推可动方（固定不动），
    故幅值统一取重叠深度、由调用方决定施加给谁。零重叠返回 (0,0)。
    """
    ov = overlap_area(ba, bb)
    if ov <= 0.0:
        return (0.0, 0.0)
    ox = min(ba[2], bb[2]) - max(ba[0], bb[0])
    oy = min(ba[3], bb[3]) - max(ba[1], bb[1])
    ca = ((ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2)
    cb = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    dx, dy = ca[0] - cb[0], ca[1] - cb[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        dx, dy = rng.uniform(-1, 1), rng.uniform(-1, 1)
    if ox <= oy:
        s = 1.0 if dx >= 0 else -1.0
        return (s * ox, 0.0)
    s = 1.0 if dy >= 0 else -1.0
    return (0.0, s * oy)


def _net_centroid(problem: PlacementProblem, pos: dict, ref: str):
    """ref 的已放 net 邻居质心（按 net.weight 加权；无则 None）。

    关键网（时钟，weight>1）连接的邻居，若知道其在该网上的引脚坐标（net_pin_loc），
    则用**引脚绝对坐标**替代**器件体心**作为吸引点——晶体因此贴 MCU 振荡引脚（如
    U1 的 69/70），而不是贴封装任意一侧；普通网仍用体心。加权质心按 (net, ref)
    逐对累加：同一 IC 经两条时钟网连晶体时，贡献两个相邻引脚点（69 与 70），
    天然把晶体拉到这对引脚之间。
    """
    # 「锚定」器件（时钟/点对点被动件 + 电源 IC）直接返回其统一锚点（IC 引脚或电源
    # 端子）：它们的落点由锚点唯一决定，平面网（GND 数十 refs）的体心质心会把偏置
    # 电阻/LED 电阻拖离 IC 引脚（与晶体负载电容被 GND 拖 15–21mm 同源），故不参与
    # 体心质心。
    if ref in anchor_refs(problem):
        return anchor_point(problem, pos, ref)
    pts: list[tuple[float, float, float]] = []  # (x, y, weight)
    is_clock = ref in getattr(problem, "clock_refs", ())
    # 时钟网成员（晶体/负载电容）的电源/地网是 via 到平面、位置无关：若纳入质心，
    # GND（数十 refs）的体心质心会压过时钟网、把负载电容拖离晶体引脚（实测 C13/C14
    # 被 GND 拉到 MCU 对侧 15–21mm）。故其 weight-1 网一律跳过，质心只由关键网引脚给出。
    is_clock_member = any(net.weight > 1.0 and ref in net.refs for net in problem.nets)
    for net in problem.nets:
        if ref not in net.refs:
            continue
        if net.weight <= 1.0 and is_clock_member:
            continue
        for r in net.refs:
            if r == ref or r not in pos:
                continue
            if net.weight > 1.0:
                # 时钟器件（晶体）只锚到 IC 引脚：负载电容的体心/引脚随其自身移动，
                # 若纳入质心会与晶体互为吸引、在引脚旁挤成振荡；普通时钟网成员
                # （负载电容）仍锚到 IC 引脚 + 晶体引脚，使其落在两者之间。
                if is_clock and problem.by_ref[r].category != "ic":
                    continue
                p = pin_abs(problem, r, net.name, pos)
                if p is not None:
                    pts.append((p[0], p[1], net.weight))
                    continue
            pts.append((pos[r][0], pos[r][1], net.weight))
    if not pts:
        return None
    tw = sum(w for _, _, w in pts)
    cx = sum(x * w for x, _, w in pts) / tw
    cy = sum(y * w for _, y, w in pts) / tw
    return (cx, cy)


def _density_spread(problem: PlacementProblem, pos: dict, movable: list[str],
                    disp: dict, cfg: LegalizeConfig) -> None:
    """密度扩散：把可动件从高占用 bin 推向低占用 bin，使整板分布更均匀。

    以 ``density_bin`` 网格统计**可动件**占用面积，压力 = 占用 − 均匀目标占用；
    每个可动件沿其中心 bin 到「最疏相邻 bin」的方向受推力（∝ 压力差、上限
    ``density_max``/轮）。力很小且设上限，不会压过关键网吸引力（时钟网 weight 8），
    晶体/去耦仍贴 IC；只是把纯体心引力挤成的中心团扩散开、填满固定连接器之间的
    空旷内区。最终合法性由越界斥力 + ``_resolve_overlaps`` 兜底。
    """
    W, H = problem.board
    b = cfg.density_bin
    nx = max(1, int(math.ceil(W / b)))
    ny = max(1, int(math.ceil(H / b)))
    occ = [[0.0] * nx for _ in range(ny)]
    for r in movable:
        box = aabb(problem.by_ref[r], *pos[r])
        x0, y0 = max(0.0, box[0]), max(0.0, box[1])
        x1, y1 = min(W, box[2]), min(H, box[3])
        for cy in range(int(y0 // b), int((y1 - 1e-9) // b) + 1):
            for cx in range(int(x0 // b), int((x1 - 1e-9) // b) + 1):
                if 0 <= cx < nx and 0 <= cy < ny:
                    occ[cy][cx] += (min(x1, (cx + 1) * b) - max(x0, cx * b)) * \
                                   (min(y1, (cy + 1) * b) - max(y0, cy * b))
    total = sum(occ[cy][cx] for cy in range(ny) for cx in range(nx))
    target = total / (nx * ny)
    for r in movable:
        cx = int(pos[r][0] // b)
        cy = int(pos[r][1] // b)
        # 力导向中途器件可能暂时越界，clamp 到有效 bin（越界斥力随后拉回）。
        cx = max(0, min(nx - 1, cx))
        cy = max(0, min(ny - 1, cy))
        p_here = occ[cy][cx] - target
        bdx = bdy = 0.0
        p_best = p_here
        for dcx, dcy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ax, ay = cx + dcx, cy + dcy
            if 0 <= ax < nx and 0 <= ay < ny:
                p = occ[ay][ax] - target
                if p < p_best:
                    p_best = p
                    bdx, bdy = float(dcx), float(dcy)
        if bdx == 0.0 and bdy == 0.0:
            continue  # 已是局部最疏，不再推
        push = (p_here - p_best) / (b * b)  # 面积密度差 → 位移尺度
        mag = min(cfg.lam_density * push, cfg.density_max)
        disp[r][0] += mag * bdx
        disp[r][1] += mag * bdy


# 原则①支撑 IC 贴边带参数：IC bbox 边到最近板边的目标距离（mm）与每轮吸引上限
# （mm，再乘 step=0.1 阻尼）。EtherCAT U4/U5 基线距边 ~19mm → 带设 5mm 即在「主控
# 引脚锚邻域」与「板边」之间留出整段梯度，弱力多轮拉过去。
_EDGE_IC_BAND = 5.0
_EDGE_IC_CAP = 10.0
# 原则⑥主芯片均匀：可动 IC 两两 AABB 间隙低于此值（mm）时施软斥——只管「贴脸」
# 这一段，长程分布交给锚定/块内聚/密度扩散。
_IC_SPREAD_MARGIN = 5.0


def refine(
    problem: PlacementProblem,
    pos: dict[str, tuple[float, float, float]],
    cfg: LegalizeConfig,
    fixed: set[str] | None = None,
) -> dict[str, tuple[float, float, float]]:
    """力导向 + 残余重叠消解，返回新的 ``pos``（可动件已合法化）。

    ``fixed`` 之外的可动件会移动；固定件（含锁定件）原地不动。
    """
    rng = random.Random(cfg.seed)
    W, H = problem.board
    g = cfg.grid
    fixed = fixed or set(problem.fixed)
    pos = {r: (p[0], p[1], p[2]) for r, p in pos.items()}
    movable = [r for r in problem.refs if r not in fixed]
    pin_devices = [r for r in movable if r in anchor_refs(problem)]
    decoup_refs = [r for r in power_decoup_refs(problem) if r in movable]
    # 专属轨（唯一 IC）去耦电容名册：lam_decoup_dedicated>0 时这些电容改用
    # 独立系数（默认 0 → 空集，走 lam_decoup 原路径，浮点运算逐位不变）。
    ded_refs: set[str] = (dedicated_decoup_refs(problem)
                          if cfg.lam_decoup_dedicated > 0 else set())
    # 原则①⑥的 IC 名册（循环外一次算好；power_ic_refs 每轮重算会白费）
    ic_movable = [r for r in movable if problem.by_ref[r].category == "ic"]
    _power_refs = power_ic_refs(problem)
    # ① 靶标名册：非主、非电源的可动 IC（= 支撑 IC）。lam_edge_ic=0 时为空集，
    # _resolve_overlaps 的 keep_legal 收到空集 → 行为与旧引擎逐位一致。
    edge_ic_targets: set[str] = set()
    if cfg.lam_edge_ic > 0 and ic_movable:
        main_ic = max(ic_movable, key=lambda r: problem.by_ref[r].pins)
        edge_ic_targets = {r for r in ic_movable
                           if r != main_ic and r not in _power_refs}

    # —— 1. 力导向松弛 ——
    for _ in range(cfg.iters):
        boxes = _boxes(problem, pos)
        disp: dict[str, list[float]] = {r: [0.0, 0.0] for r in movable}
        # 重叠斥力（可动↔可动 + 可动↔固定）：固定件是障碍，也必须推开可动件，
        # 否则可动件会停在固定连接器/安装孔上、最后被 _resolve_overlaps 强行弹远，
        # 连带把跟随它的晶体/负载电容一起拖离 MCU 引脚。
        ml = movable
        obstacles = [r for r in problem.refs if r in fixed]
        for i in range(len(ml)):
            a = ml[i]
            ba = boxes[a]
            for j in range(i + 1, len(ml)):
                px, py = _overlap_push(ba, boxes[ml[j]], rng)
                disp[a][0] += px
                disp[a][1] += py
                disp[ml[j]][0] -= px
                disp[ml[j]][1] -= py
            for b in obstacles:
                px, py = _overlap_push(ba, boxes[b], rng)
                disp[a][0] += px
                disp[a][1] += py
        # net 吸引力
        for r in movable:
            c = _net_centroid(problem, pos, r)
            if c is None:
                continue
            disp[r][0] += cfg.lam_net * (c[0] - pos[r][0])
            disp[r][1] += cfg.lam_net * (c[1] - pos[r][1])
        # 引脚锚定器件粘连：被强力拉向 IC 引脚（随 MCU 移动跟随），
        # 压过负载电容/密度的推挤，保证合法化后仍贴引脚（而非被弹离）。
        if cfg.lam_pin > 0:
            for r in pin_devices:
                a = anchor_point(problem, pos, r)
                if a is None:
                    continue
                disp[r][0] += cfg.lam_pin * (a[0] - pos[r][0])
                disp[r][1] += cfg.lam_pin * (a[1] - pos[r][1])
        # 块内聚（分图语义化）：只拉「松散被动件」向块的锚点（IC + 固定连接器）收拢，
        # 把 power/ethercat/can/debug 的电容/二极管/电感/电阻留在各自功能区，而非被
        # HPWL 混进 MCU 团。两处关键：
        #  ① 只动 passive——大 IC（U1 MCU / U4/U5 隔离器 / U2/U3 电源 IC）是块的锚、
        #     整板布线的支点，绝不拖动（否则 U1 漂移 +2.5mm、U4 左移 9mm，12 条网
        #     布不通）；锚定件（时钟/点对点被动件 + 电源 IC，见 pin_devices）也不动，
        #     其落点由 IC 引脚/电源端子唯一决定（R5–R10 串阻须贴 U1 PHY 引脚做源端
        #     端接）。
        #  ② 质心只从「锚点」（IC + 固定连接器）算——松散被动件不参与算质心，否则
        #     散开的电容把质心拽偏、把锚点方向算错。
        if cfg.lam_block > 0 and getattr(problem, "blocks", None):
            for r in movable:
                if r in pin_devices:
                    continue
                comp = problem.by_ref[r]
                if comp.category != "passive":
                    continue
                b = comp.block
                anchors = [m for m in problem.blocks.get(b, ())
                           if m in pos and m != r
                           and (problem.by_ref[m].category == "ic" or m in fixed)]
                if not anchors:
                    continue
                cx = sum(pos[m][0] for m in anchors) / len(anchors)
                cy = sum(pos[m][1] for m in anchors) / len(anchors)
                disp[r][0] += cfg.lam_block * (cx - pos[r][0])
                disp[r][1] += cfg.lam_block * (cy - pos[r][1])
        # 密度扩散：从密集区推向空旷区，填满固定连接器之间的内区（均匀分布）
        if cfg.lam_density > 0:
            _density_spread(problem, pos, movable, disp, cfg)
        # 原则①支撑 IC 贴边（lam_edge_ic>0，默认 0=关）：非主、非电源锚定的可动 IC
        # 向最近板边带弱吸引。主芯片（pin 数最多的可动 IC）不动——它是整板支点；
        # 电源 IC 不动——归 ④ 电源区锚（电源输入端子侧就是它的"边"）。U4/U5 这类
        # 引脚锚定的支撑 IC（IC↔IC point 网拉向主控 pad）正是①的对象：贴边力与
        # lam_pin 反向博弈，落点是二者平衡而非硬拖到边。只拉不推（bbox 已在带内
        # 不受力），deficit 每轮上限 _EDGE_IC_CAP、再乘 step 阻尼。
        # ⚠️ 网格量化物理：refine 每轮 _snap 到 1.27 格，每轮位移 < 0.635mm 会被
        # 吸回原格 → 有效系数满足 lam×deficit×step ≥ 0.635，停滞距离
        # e ≈ band + 6.35/lam（lam=1 → ~11.4、lam=2 → ~8.2）。
        if edge_ic_targets:
            for r in edge_ic_targets:
                x1, y1, x2, y2 = boxes[r]
                # (距离, x 方向符号, y 方向符号)：最近边是左/下 → 往负方向拉，
                # 右/上 → 往正方向拉（把 bbox 边送向板边、缩小 e）
                e, sx, sy = min(((x1, -1.0, 0.0), (y1, 0.0, -1.0),
                                 (W - x2, 1.0, 0.0), (H - y2, 0.0, 1.0)))
                if e <= _EDGE_IC_BAND:
                    continue  # 已在贴边带内：不再压向板边（只拉不推）
                deficit = min(e - _EDGE_IC_BAND, _EDGE_IC_CAP)
                disp[r][0] += cfg.lam_edge_ic * deficit * sx
                disp[r][1] += cfg.lam_edge_ic * deficit * sy
        # 原则⑥主芯片均匀（lam_ic_spread>0，默认 0=关）：可动 IC 两两 AABB 间隙低于
        # _IC_SPREAD_MARGIN 时沿体心连线软斥（幅值随 margin−gap 线性、各推一半）。
        # 只推开不拉近：gap≥margin 不施力（长程分布归锚定/块内聚/密度扩散），
        # gap<0 是真重叠、重叠斥力已管、不重复计。与 lam_density 的差别：IC 专属、
        # 按间隙触发（近距才出手）、幅值与缺口成正比。
        if cfg.lam_ic_spread > 0:
            for i in range(len(ic_movable)):
                a = ic_movable[i]
                for j in range(i + 1, len(ic_movable)):
                    b = ic_movable[j]
                    ba, bb = boxes[a], boxes[b]
                    gx = max(ba[0] - bb[2], bb[0] - ba[2])
                    gy = max(ba[1] - bb[3], bb[1] - ba[3])
                    gap = max(gx, gy)
                    if not (0.0 <= gap < _IC_SPREAD_MARGIN):
                        continue
                    dx = (bb[0] + bb[2]) - (ba[0] + ba[2])
                    dy = (bb[1] + bb[3]) - (ba[1] + ba[3])
                    d2 = dx * dx + dy * dy
                    if d2 < 1e-12:  # 中心重合退化：随机方向（_overlap_push 同款）
                        dx, dy = rng.random() - 0.5, rng.random() - 0.5
                        d2 = dx * dx + dy * dy or 1e-6
                    inv = 1.0 / math.sqrt(d2)
                    mag = cfg.lam_ic_spread * (_IC_SPREAD_MARGIN - gap) / 2.0
                    ux, uy = dx * inv, dy * inv
                    disp[a][0] -= mag * ux
                    disp[a][1] -= mag * uy
                    disp[b][0] += mag * ux
                    disp[b][1] += mag * uy
        # 电源去耦电容：柔和引力 → 最近 IC 电源引脚。plane 网（GND/+3V3 数十 refs）
        # 的体心质心把去耦电容拽向板心/固定连接器侧（C4 卡在 U6 GND pad 旁、挡住其
        # 地脚布线），此处用 lam_decoup（远小于时钟 lam_pin 0.5）的弱力把电容贴回它
        # 真正去耦的 IC 电源引脚。不登记 anchor_refs（不跳块内聚、不升 tier-1 强占
        # 引脚邻域），只作一股叠加引力，压过 0.04 的 plane 网漂移，又不至 17 颗 MCU
        # 电容过约束 U1、拽断以太网点对点网。
        if cfg.lam_decoup > 0 or cfg.lam_decoup_dedicated > 0:
            for r in decoup_refs:
                a = power_decoup_anchor(problem, pos, r)
                if a is None:
                    continue
                k = (cfg.lam_decoup_dedicated if r in ded_refs
                     else cfg.lam_decoup)
                if k == 0:
                    continue
                disp[r][0] += k * (a[0] - pos[r][0])
                disp[r][1] += k * (a[1] - pos[r][1])
        # 越界斥力
        for r in movable:
            c = problem.by_ref[r]
            w, h = (c.w, c.h) if pos[r][2] % 180 == 0 else (c.h, c.w)
            lo_x, lo_y = w / 2, h / 2
            hi_x, hi_y = W - w / 2, H - h / 2
            if pos[r][0] < lo_x:
                disp[r][0] += lo_x - pos[r][0]
            elif pos[r][0] > hi_x:
                disp[r][0] += hi_x - pos[r][0]
            if pos[r][1] < lo_y:
                disp[r][1] += lo_y - pos[r][1]
            elif pos[r][1] > hi_y:
                disp[r][1] += hi_y - pos[r][1]
        # 应用位移 + snap
        for r in movable:
            x = _snap(pos[r][0] + cfg.step * disp[r][0], g)
            y = _snap(pos[r][1] + cfg.step * disp[r][1], g)
            pos[r] = (x, y, pos[r][2])

    # —— 2. 残余重叠消解：保证 AABB 零重叠 + 不越界 ——
    pos = _resolve_overlaps(problem, pos, movable, g, rng, fixed,
                            keep_legal=edge_ic_targets)
    return pos


def _resolve_overlaps(problem, pos, movable, g, rng, fixed=None,
                      keep_legal: set[str] | None = None) -> dict:
    """确定性扫尾：按尺寸降序逐个安置可动件，保证结束时 AABB 零重叠 + 不越界。

    关键：合法判定只看「已定格的件」（固定件 + 已安置的可动件）。尚未安置的可动件
    稍后也会被移动绕开，其旧位置不构成永久约束——否则一堆还没收拾的重叠件会把大件
    的所有合法位都堵死，导致 ``found=None``、越界残留。

    时钟器件（晶体/振荡器）落点锚到 IC 引脚：IC 尺寸大、必然先于晶体定格，故晶体
    轮到安置时引脚锚点已是终值，直接取「离引脚最近的合法格 + 4 朝向最优」，保证
    合法化后晶体仍贴 MCU 振荡引脚（随 MCU 移动跟随），不被此扫尾弹离。
    """
    W, H = problem.board
    # 安置顺序分四级，序由**锚点依赖**决定：通道前沿件（锚到固定连接器，锚点不依赖
    # 任何可动件）→ 未锚定 IC（最大，先占位定引脚）→ 引脚/电源锚定件（锚到可动 IC
    # 的 pad / 电源端子，需 IC 引脚坐标为终值）→ 其余。锚定件必须**先于**普通小被动件
    # 占位：其锚点（IC 具体 pad / 电源端子）周围空间极小，若按纯尺寸降序，它排在 R/C
    # 后面、轮到它时 pad 邻域已被去耦电容塞满，pin_nearest 的 15mm 窗口内无合法格 →
    # 退回 spiral 把偏置/LED 电阻弹到整板对侧（布线实测 ETH0_RBIAS/DCDC_LP/
    # ETH1_LED1 因此未布通）。IC 尺寸最大本就先占位，故引脚锚定件放 IC 之后、普通件
    # 之前，保证其 pad 邻域此时仍空。通道前沿件更进一步提到 IC 之前：否则主控在力
    # 松弛后可能恰好蹲进端子走廊并合法保持原位，把整条调理链弹离锚点。
    pin_refs = anchor_refs(problem)
    channel = channel_members(problem)

    def _resolve_priority(r: str) -> tuple[int, float, float, float, str]:
        c = problem.by_ref[r]
        area = -(c.w * c.h)
        if r in channel:
            # 通道前沿件（端子调理链）锚到**固定连接器**：锚点不依赖任何可动件，
            # 必须先于 IC 定格——否则 tier-0 主控在力松弛后蹲进通道走廊、合法保持
            # 原位，通道件轮到时近锚格全在 IC 身体里，被 pin_nearest 弹到 ~11mm 外
            # （stm32_plc U1 压 J5 锚点带即此）。锚点几何序（先 y 后 x）扇出成列。
            a = anchor_point(problem, pos, r)
            return (0, area, a[1] if a is not None else 1e9,
                    a[0] if a is not None else 1e9, r)
        if r in pin_refs:
            # 引脚/电源锚定件按**锚点几何序**（先 y 后 x = 主轴）扇出，替代字母序：7 个
            # 电阻按 pad 从上到下依次占各自引脚最近合法格、天然成列，R9 不再「字母序
            # 最后被挤到 8mm」。电源 IC 锚点=电源端子（常数），二者同序、退化为面积序。
            # 排序期读 force-directed pos，IC 位置刚性平移不改变沿边次序；无锚点退到
            # 本 tier 最后。
            a = anchor_point(problem, pos, r)
            return (2, area, a[1] if a is not None else 1e9,
                    a[0] if a is not None else 1e9, r)
        if c.category == "ic":
            # 未锚定 IC 先于引脚锚定件定格（锚到 IC 引脚的件需引脚坐标为终值），
            # 但晚于通道前沿件（其锚点只依赖固定件，先占走廊防 IC 蹲位）。
            return (1, area, 0.0, 0.0, r)
        return (3, area, 0.0, 0.0, r)

    order = sorted(movable, key=_resolve_priority)
    placed: set[str] = set(fixed or set(problem.fixed))

    def legal_for(ref: str, x: float, y: float, rot: float) -> bool:
        c = problem.by_ref[ref]
        box = aabb(c, x, y, rot)
        if box[0] < 0 or box[1] < 0 or box[2] > W or box[3] > H:
            return False
        for r2 in placed:
            if overlap_area(box, aabb(problem.by_ref[r2], *pos[r2])) > 0:
                return False
        return True

    def spiral(ox: float, oy: float, rot: float):
        """离 (ox,oy) 最近的合法格（保持朝向，Chebyshev 环逐圈外扩）；无则 None。"""
        max_r = max(math.ceil(W / g), math.ceil(H / g)) + 1
        for rad in range(0, max_r + 1):
            for dc in range(-rad, rad + 1):
                for dr in range(-rad, rad + 1):
                    if rad > 0 and max(abs(dc), abs(dr)) != rad:
                        continue
                    cand = (_snap(ox + dc * g, g), _snap(oy + dr * g, g))
                    if legal_for(ref, cand[0], cand[1], rot):
                        return (cand[0], cand[1], rot)
        return None

    def pin_nearest(ax: float, ay: float):
        """离引脚锚点 (ax,ay) 最近的合法格（4 朝向都试，按**欧氏距离**取最小）。

        Chebyshev 环序会把「同环但更偏」的合法点（如 dr=−3）先于「正对引脚」的
        （dr=0）返回，导致晶体沿 MCU 边滑离引脚。这里穷举锚点附近窗口内所有格点、
        按欧氏距离取最小，保证晶体落在引脚正外侧而非斜对角。
        """
        max_dc = int(math.ceil(15.0 / g)) + 1
        best = None
        best_d2 = None
        for rot_deg in (0.0, 90.0, 180.0, 270.0):
            for dc in range(-max_dc, max_dc + 1):
                for dr in range(-max_dc, max_dc + 1):
                    x = _snap(ax + dc * g, g)
                    y = _snap(ay + dr * g, g)
                    if not legal_for(ref, x, y, rot_deg):
                        continue
                    d2 = (x - ax) ** 2 + (y - ay) ** 2
                    if best_d2 is None or d2 < best_d2:
                        best_d2 = d2
                        best = (x, y, rot_deg)
        return best

    # g/2 细网格中心：同时覆盖解码的半格偏移 (k+0.5)·g 与 snap 的整数偏移 k·g，
    # 螺旋失败时靠它在全板穷举里找到合法位（只要有空位就保证清零）。
    step = g / 2.0
    cx_all = [k * step for k in range(int(math.floor(W / step)) + 1)]
    cy_all = [k * step for k in range(int(math.floor(H / step)) + 1)]

    for ref in order:
        x, y, rot = pos[ref]
        # 锚定件：落点锚到统一锚点（IC 引脚 / 电源端子，此时 MCU 已定格在 placed），
        # 取离锚点最近的合法格（4 朝向都试、取离锚点最近者），保证晶体贴振荡引脚、
        # 电源 IC 贴电源输入端子。
        # 原则①豁免（keep_legal，默认空集=行为不变）：支撑 IC 在力循环里已按
        # 「贴边力 vs lam_pin 引脚锚」平衡出落点；无条件拉回锚点邻格会把①完全
        # 抹掉（ON≡OFF）。降级序：力落点合法 → 保持；非法 → 力落点邻域 spiral
        # 找最近合法格（贴边带附近被连接器/先置件挡住时就近让位，①的意图就是
        # 离开锚区）；邻域全满 → 退回 pin_nearest 锚点邻格（安全兜底）。
        if ref in pin_refs:
            if ref in (keep_legal or ()):
                if legal_for(ref, x, y, rot):
                    placed.add(ref)
                    continue
                near = spiral(x, y, rot)
                if near is not None:
                    pos[ref] = near
                    placed.add(ref)
                    continue
            a = anchor_point(problem, pos, ref)
            if a is not None:
                # 离锚点**欧氏最近**的合法格（4 朝向都试）：Chebyshev 环序会沿 MCU 边
                # 把晶体滑到斜对角（dr=−3 先于 dr=0），此处穷举附近窗口取最小欧氏距离，
                # 保证晶体落在引脚正外侧（正对 69/70）而非滑离。电源 IC 同理落在电源
                # 端子（J1）正旁。
                cand = pin_nearest(a[0], a[1])
                if cand is not None:
                    pos[ref] = cand
                    placed.add(ref)
                    continue
            # 引脚附近无合法格（极挤）→ 退回通用处理。
        if legal_for(ref, x, y, rot):
            placed.add(ref)
            continue
        found = spiral(x, y, rot)
        if found is None:
            # 全板穷举：4 个朝向 × 细网格，取第一个合法位（原朝向优先，位移近优先）
            for r_rot in range(4):
                rot_deg = r_rot * 90.0
                for cy in cy_all:
                    for cx in cx_all:
                        if legal_for(ref, cx, cy, rot_deg):
                            found = (cx, cy, rot_deg)
                            break
                    if found:
                        break
                if found:
                    break
        if found:
            pos[ref] = (found[0], found[1], found[2])
        placed.add(ref)
    return pos


def metrics(problem: PlacementProblem, pos: dict) -> dict:
    """放置的整体指标：HPWL / 重叠面积 / 越界量。"""
    W, H = problem.board
    hp = hpwl(problem, {r: (p[0], p[1]) for r, p in pos.items()})
    boxes = _boxes(problem, pos)
    ov = sum(overlap_area(boxes[a], boxes[b])
             for i, a in enumerate(problem.refs) for b in problem.refs[i + 1:])
    bd = sum(max(0.0, -b[0]) + max(0.0, -b[1]) + max(0.0, b[2] - W) + max(0.0, b[3] - H)
             for b in boxes.values())
    return {"hpwl": hp, "overlap": ov, "bounds": bd}
