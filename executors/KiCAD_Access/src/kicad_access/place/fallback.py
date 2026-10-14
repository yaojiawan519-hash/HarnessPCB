"""力导向兜底放置：无 recipe 模块的"合法不重叠"底座（方案 §5 兜底栏）。

质量标准就一条——合法不重叠（方案 326 行：兜底只求合法，质量靠
recipe 库逐步覆盖）。输入 ModuleContext，输出与 recipe 同契约的
RecipeResult（A~D），floorplan 侧复用 ``validate_result`` 复核后入库。

布法（模块局部系，y-down，+x = 板缘方向，同 can_transceiver 惯例）：
- 连接器（Connector*/TerminalBlock* 封装）= hard：rot90 竖排在 x=0
  板缘侧（孔列竖直、进线朝 +x），core = 连接器放置框并集；
- 其余 soft：确定性栅格初值（连接器左壁 − 间隙，列主序）+ 力松弛——
  net 弹簧（同 net 器件互拉，hard 连接器作固定吸引子）+ AABB 重叠
  排斥；固定迭代次数、零随机性、遍历全按 ref 序——结果可复现；
- 收尾 1.27mm 栅格化 + 重叠消解环（格点步进推让；hard 框与级联
  keep_outs 当固定障碍）；步数耗尽仍有重叠 → ValueError 诚实上浮；
- 出口：模块 bbox 四边栅格 Top-K（代价 = 最近 pin 曼哈顿），
  同 decoupling_ring 惯例。
"""

from __future__ import annotations

import math
import re

from ..kicad.footprint import Box
from .geometry import Placement, overlap_area
from .recipe import ExitCandidate, ModuleContext, RecipeResult

HARD_FP = ("Connector", "TerminalBlock")  # 连接器封装库前缀 → hard
HARD_GAP = 1.0        # 连接器堆叠间隙（mm）
WALL_GAP = 1.0        # 连接器内壁 ↔ 软器件栅格墙
GRID_GAP = 0.8        # 软器件初始栅格间隙
SEP_GAP = 1.27        # 力松弛分离光晕（软-软 / 软-固定，栅格对齐）
ITERS = 120           # 力松弛迭代数
K_ATT = 0.15          # net 弹簧系数
DAMP = 0.5            # 步长阻尼
LATTICE = 1.27        # 收尾栅格（50mil，同 KiCad 连接栅格）
MAX_FIX = 200         # 重叠消解环步数上限

# 朝向：0=+x, 90=上(−y), 180=−x, 270=下(+y)
_SIDE_ORIENT = {"L": 180, "R": 0, "T": 90, "B": 270}

# 电源 / 总线网络：net 弹簧只跟信号邻接，不跟供电轨——GND/3V3/24V 等把整
# 板器件连成一张完全图，静止长弹簧对完全图无唯一平衡，会把同轨器件塌成
# 一点（do/di 四路光耦叠死即此因）。这些轨的位置信息为零，直接不进弹簧。
_SUPPLY_NAMES = {
    "gnd", "vss", "fg", "agnd", "dgnd", "earth", "chassis", "shield",
    "vcc", "vdd", "vdda", "vddio", "vref", "vbat", "vin", "vbus", "vcore",
}
_VOLT_RE = re.compile(r"^\d+(?:\.\d+)?v\d*$")


def _is_supply_net(name: str) -> bool:
    """纯供电轨 / 地网判定（GND、+24V、+3V3、FG、VDDCR 之外的电源名）。"""
    n = re.sub(r"[^0-9a-z.]", "", name.lower())
    return n in _SUPPLY_NAMES or bool(_VOLT_RE.match(n))


def force_layout(ctx: ModuleContext, keep_outs: tuple = ()) -> RecipeResult:
    """无 recipe 模块的兜底放置（契约 A~D 全过，floorplan 复核后入库）。"""
    hard = sorted(
        r for r, d in ctx.devices.items() if d.footprint.startswith(HARD_FP)
    )
    soft = sorted(set(ctx.devices) - set(hard))
    placements: dict[str, Placement] = {}
    core: Box | None = None

    if hard:
        placements.update(_stack_connectors(ctx, hard))
        hard_boxes = [
            ctx.geoms[r].bbox_at(placements[r].x, placements[r].y, placements[r].rot)
            for r in hard
        ]
        core = (
            min(b[0] for b in hard_boxes), min(b[1] for b in hard_boxes),
            max(b[2] for b in hard_boxes), max(b[3] for b in hard_boxes),
        )

    if soft:
        fixed = [b for b in (core, *keep_outs) if b is not None]
        _place_soft(ctx, soft, placements, fixed)

    box = _bbox(ctx, placements)
    return RecipeResult(
        placements=placements, exits=_exits(ctx, placements, box), core=core
    )


# ---------------------------------------------------------------- hard：连接器


def _stack_connectors(
    ctx: ModuleContext, hard: list[str]
) -> dict[str, Placement]:
    """连接器 rot90 自上(−y)而下竖排、整组居中 y=0；原点 x=0（板缘侧）。"""
    out: dict[str, Placement] = {}
    cur = 0.0
    tops: list[tuple[str, float]] = []
    for r in hard:
        x1, y1, x2, y2 = ctx.geoms[r].bbox_at(0.0, 0.0, 90.0)
        h = y2 - y1
        tops.append((r, cur - y1))  # 框顶落在 cur，原点 y 偏移
        cur += h + HARD_GAP
    shift = -(cur - HARD_GAP) / 2.0  # 整组居中
    for r, dy in tops:
        out[r] = Placement(r, 0.0, dy + shift, 90.0, "F", "hard")
    return out


# ---------------------------------------------------------------- soft：力松弛


def _place_soft(
    ctx: ModuleContext,
    soft: list[str],
    placements: dict[str, Placement],
    fixed: list[Box],
) -> None:
    """栅格初值 → net 弹簧 + 重叠排斥松弛 → 栅格化 → 重叠消解环。"""
    # 初值：连接器左壁（无固定障碍时取 0）再退一墙隙，列主序近方形栅格
    wall = (min(b[0] for b in fixed) - WALL_GAP) if fixed else 0.0
    pitch_x = max(_span(ctx.geoms[r], "x") for r in soft) + GRID_GAP
    pitch_y = max(_span(ctx.geoms[r], "y") for r in soft) + GRID_GAP
    rows = max(1, math.isqrt(len(soft)))
    for i, r in enumerate(soft):
        col, row = divmod(i, rows)
        placements[r] = Placement(
            r,
            wall - (col + 0.5) * pitch_x,
            (row - (rows - 1) / 2.0) * pitch_y,
            0.0, "F", "soft",
        )

    # net 弹簧成员表：net → 子集内已放器件（hard 连接器作固定吸引子）。
    # 供电轨不进弹簧——完全图无唯一平衡，会把同轨器件塌成一点。
    members: dict[str, list[str]] = {}
    for net in (*ctx.intra_nets, *ctx.external_nets):
        if _is_supply_net(net.name):
            continue
        refs = [p.ref for p in net.pins if p.ref in ctx.refs]
        if len(refs) >= 2:
            members.setdefault(net.name, []).extend(
                r for r in refs if r not in members.get(net.name, ())
            )

    for _ in range(ITERS):
        pos = {r: (placements[r].x, placements[r].y) for r in soft}
        move = {r: [0.0, 0.0] for r in soft}
        for refs in members.values():
            targets = {
                r: (placements[r].x, placements[r].y)
                for r in refs if r in placements
            }
            for r in refs:
                if r not in pos:
                    continue  # hard 连接器：只当靶子不当源
                fx = fy = 0.0
                rr = math.hypot(
                    _span(ctx.geoms[r], "x") / 2.0,
                    _span(ctx.geoms[r], "y") / 2.0,
                )
                for o, (ox, oy) in targets.items():
                    if o == r:
                        continue
                    dx = ox - pos[r][0]
                    dy = oy - pos[r][1]
                    dist = math.hypot(dx, dy)
                    if dist < 1e-6:
                        continue
                    ro = math.hypot(
                        _span(ctx.geoms[o], "x") / 2.0,
                        _span(ctx.geoms[o], "y") / 2.0,
                    )
                    rest = rr + ro + SEP_GAP  # 静止长：贴合 + 间隙；远了吸、近了斥
                    scale = (dist - rest) / dist
                    fx += dx * scale
                    fy += dy * scale
                n = max(1, len(targets) - 1)
                move[r][0] += K_ATT * fx / n
                move[r][1] += K_ATT * fy / n
        for i, a in enumerate(soft):  # 分离排斥（soft-soft，带 SEP_GAP 光晕防塌缩）
            ba = ctx.geoms[a].bbox_at(pos[a][0], pos[a][1], 0.0)
            for b in soft[i + 1:]:
                bb = ctx.geoms[b].bbox_at(pos[b][0], pos[b][1], 0.0)
                if (ba[2] + SEP_GAP < bb[0] or bb[2] + SEP_GAP < ba[0]
                        or ba[3] + SEP_GAP < bb[1] or bb[3] + SEP_GAP < ba[1]):
                    continue
                dx = (ba[0] + ba[2]) / 2 - (bb[0] + bb[2]) / 2
                dy = (ba[1] + ba[3]) / 2 - (bb[1] + bb[3]) / 2
                if abs(dx) >= abs(dy):
                    s = 1.0 if dx >= 0 else -1.0
                    move[a][0] += s
                    move[b][0] -= s
                else:
                    s = 1.0 if dy >= 0 else -1.0
                    move[a][1] += s
                    move[b][1] -= s
        for a in soft:  # 软 ↔ 固定障碍（连接器 core / keep_outs）排斥
            ba = ctx.geoms[a].bbox_at(pos[a][0], pos[a][1], 0.0)
            for fb in fixed:
                if (ba[2] + SEP_GAP < fb[0] or fb[2] + SEP_GAP < ba[0]
                        or ba[3] + SEP_GAP < fb[1] or fb[3] + SEP_GAP < ba[1]):
                    continue
                dx = (ba[0] + ba[2]) / 2 - (fb[0] + fb[2]) / 2
                dy = (ba[1] + ba[3]) / 2 - (fb[1] + fb[3]) / 2
                if abs(dx) >= abs(dy):
                    move[a][0] += 1.0 if dx >= 0 else -1.0
                else:
                    move[a][1] += 1.0 if dy >= 0 else -1.0
        for r in soft:  # 阻尼 + 步长限幅 → 提交（松弛期浮点，不量化）
            p = placements[r]
            dx = max(-2.0, min(2.0, DAMP * move[r][0]))
            dy = max(-2.0, min(2.0, DAMP * move[r][1]))
            placements[r] = Placement(r, p.x + dx, p.y + dy, 0.0, "F", "soft")

    for r in soft:  # 收尾一次性 1.27 栅格化（量化太早会吃掉小步弹簧）
        p = placements[r]
        placements[r] = Placement(
            r, round(p.x / LATTICE) * LATTICE, round(p.y / LATTICE) * LATTICE,
            0.0, "F", "soft",
        )

    _resolve_legal(ctx, soft, placements, fixed)


def _resolve_legal(
    ctx: ModuleContext,
    soft: list[str],
    placements: dict[str, Placement],
    fixed: list[Box],
) -> None:
    """尽力格点推让消重叠；耗尽则确定性重排兜底，保证「合法不重叠」契约。

    力松弛的栅格化结果偶尔会留一个被左右邻居夹死的窄槽（如 do 页 D20
    夹在 Q1 与 D14 之间），贪心单对推让在薄轴上往返循环、200 步也解不
    开。此时退到纯确定性栅格（每格按最大器件跨距留 GRID_GAP，恒不重叠）。
    """
    try:
        _fix_overlaps(ctx, soft, placements, fixed)
    except ValueError:
        _repack_legal(ctx, soft, placements, fixed)


def _repack_legal(
    ctx: ModuleContext,
    soft: list[str],
    placements: dict[str, Placement],
    fixed: list[Box],
) -> None:
    """确定性重排：列主序近方形栅格，格距按最大器件跨距留隙，恒合法。

    与初值同构——每格 pitch = 最大跨距 + GRID_GAP，相邻器件边缘间隙
    ≥ GRID_GAP > 0；所有器件落在连接器左壁（无固定障碍时 0）之左，
    不碰 core / keep_outs。丢弃力松弛结果，质量仍交给 recipe 库。
    """
    wall = (min(b[0] for b in fixed) - WALL_GAP) if fixed else 0.0
    pitch_x = max(_span(ctx.geoms[r], "x") for r in soft) + GRID_GAP
    pitch_y = max(_span(ctx.geoms[r], "y") for r in soft) + GRID_GAP
    rows = max(1, math.isqrt(len(soft)))
    for i, r in enumerate(soft):
        col, row = divmod(i, rows)
        placements[r] = Placement(
            r,
            wall - (col + 0.5) * pitch_x,
            (row - (rows - 1) / 2.0) * pitch_y,
            0.0, "F", "soft",
        )


def _fix_overlaps(
    ctx: ModuleContext,
    soft: list[str],
    placements: dict[str, Placement],
    fixed: list[Box],
) -> None:
    """格点步进推让消重叠（soft-soft / soft-固定障碍）；耗尽步数 → 上浮。

    soft 全在 1.27 栅格上，推让步长取栅格整数倍——消解后仍对齐。
    """
    fixed_of: dict[Box, str | None] = {b: None for b in fixed}
    for _ in range(MAX_FIX):
        pairs: list[tuple[str, str | None, float, Box, Box]] = []
        for i, a in enumerate(soft):
            ba = ctx.geoms[a].bbox_at(
                placements[a].x, placements[a].y, placements[a].rot
            )
            for b in soft[i + 1:]:
                bb = ctx.geoms[b].bbox_at(
                    placements[b].x, placements[b].y, placements[b].rot
                )
                area = overlap_area(ba, bb)
                if area > 0:
                    pairs.append((a, b, area, ba, bb))
            for fb in fixed:
                area = overlap_area(ba, fb)
                if area > 0:
                    pairs.append((a, fixed_of[fb], area, ba, fb))
        if not pairs:
            return
        pairs.sort(key=lambda t: -t[2])
        ref, other, _, ba, bb = pairs[0]
        # 推 soft（双方皆 soft 时推 ref 序后者），沿薄穿透轴整格步推离
        if other is not None and soft.index(other) < soft.index(ref):
            ref, other, ba, bb = other, ref, bb, ba
        dx = (ba[0] + ba[2]) / 2 - (bb[0] + bb[2]) / 2
        dy = (ba[1] + ba[3]) / 2 - (bb[1] + bb[3]) / 2
        pen_x = min(ba[2], bb[2]) - max(ba[0], bb[0])
        pen_y = min(ba[3], bb[3]) - max(ba[1], bb[1])
        if pen_x <= pen_y:
            sign = 1.0 if dx >= 0 else -1.0
            step = math.ceil((pen_x + 0.01) / LATTICE) * LATTICE * sign
            p = placements[ref]
            placements[ref] = Placement(ref, p.x + step, p.y, p.rot, p.side, p.mobility)
        else:
            sign = 1.0 if dy >= 0 else -1.0
            step = math.ceil((pen_y + 0.01) / LATTICE) * LATTICE * sign
            p = placements[ref]
            placements[ref] = Placement(ref, p.x, p.y + step, p.rot, p.side, p.mobility)
    bad = [
        (a, o) for a, o, *_ in pairs[:5]
    ]
    raise ValueError(f"兜底重叠消解失败（{MAX_FIX} 步耗尽）: {bad}")


# ---------------------------------------------------------------- 出口


def _bbox(ctx: ModuleContext, placements: dict[str, Placement]) -> Box:
    xs, ys = [], []
    for ref, p in placements.items():
        g = ctx.geoms.get(ref)
        if g is None:
            continue
        x1, y1, x2, y2 = g.bbox_at(p.x, p.y, p.rot)
        xs += [x1, x2]
        ys += [y1, y2]
    return (min(xs), min(ys), max(xs), max(ys))


def _exits(
    ctx: ModuleContext,
    placements: dict[str, Placement],
    box: Box,
) -> dict[str, list[ExitCandidate]]:
    """每条对外 net：bbox 四边 1.27 栅格点 Top-K（代价 = 最近 pin 曼哈顿）。"""
    x1, y1, x2, y2 = box
    pts: list[tuple[float, float, int]] = []
    for i in range(int((y2 - y1) // LATTICE) + 1):
        pts.append((x1, y1 + i * LATTICE, _SIDE_ORIENT["L"]))
        pts.append((x2, y1 + i * LATTICE, _SIDE_ORIENT["R"]))
    for i in range(int((x2 - x1) // LATTICE) + 1):
        pts.append((x1 + i * LATTICE, y1, _SIDE_ORIENT["T"]))
        pts.append((x1 + i * LATTICE, y2, _SIDE_ORIENT["B"]))

    exits: dict[str, list[ExitCandidate]] = {}
    for net in ctx.external_nets:
        pp: list[tuple[float, float]] = []
        for p in net.pins:
            plc = placements.get(p.ref)
            if plc is None:
                continue
            for num, px, py in ctx.geoms[p.ref].pads_at(plc.x, plc.y, plc.rot):
                if num == p.pin:
                    pp.append((px, py))
        cands = [
            ExitCandidate(
                x, y, o, min((abs(px - x) + abs(py - y) for px, py in pp), default=1e9)
            )
            for x, y, o in pts
        ]
        cands.sort(key=lambda c: (round(c.cost, 6), c.y, c.x))
        exits[net.name] = cands[:12]
    return exits


def _span(geom, axis: str) -> float:
    o = geom.outline()
    return (o[2] - o[0]) if axis == "x" else (o[3] - o[1])
