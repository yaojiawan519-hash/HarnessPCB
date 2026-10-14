"""融合推挤（M2-2b 收官）：floorplan 后、布线前的纯几何消跨模块重叠。

方案 §2.3/§3：模块边界 = core + fringe——core（hard 器件放置框并集）
绝对锁定，fringe 融合带内只放 soft 器件、兼作模块间走线通道。带宽分档
``board.fusion_band``（模块 override → by_class[sheet] → default），
外模块器件被推出 core⊕band，带内空出来就是模块间通道（通道预留）。

冲突分类（先检后推，不可解的诚实上浮 ``FusionError``）：

- soft × {任何器件 ctyd / 外模块 core⊕band} → 推 soft；
- hard × hard ctyd / hard × 外模块 core → 无解（锚点声明挤了，属板级
  规格问题，推挤救不了），收集后一次性报错。

推挤规则（确定性，两级逃离）：每轮全板扫描取重叠面积最大者，让位方先
在 12 个候选位移（单轴整 1.27mm 格步 ×4 + 斜向 ×8）里选**新位冲突面
积严格更小**者（平手取离原位近/步长短——保 recipe 摆位质量）；无改善
（器件深陷大障碍内部，如去耦电容整枚躺进 RJ45 大院）则**栅格环搜最近
零冲突格**整体腾挪。两级皆空（板内真无空位）或步数耗尽 → 上浮。
让位方：hard 不让，双 soft 取 spec 序靠后模块（前序 = 更锚定），
同模块取 ref 序后者。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..kicad.footprint import Box, FootprintGeom
from ..spec import BoardSpec
from .geometry import Placement, find_overlaps, overlap_area

AREA_EPS = 0.01  # mm²——庭院毛刺容差：0.01mm² ≈ 0.1×0.1mm，远低于放置
# 栅格分辨率，非制造语义；不加它则亚毛刺重叠会让局部择优爬山永不停
# （每步"严格减"一丝、永远到不了零，环搜兜底又只在局部全败时启用）。

if TYPE_CHECKING:  # 仅类型引用（floorplan 已 import 本模块，避免运行时环）
    from .floorplan import SheetPlan

LATTICE = 1.27   # 推让步长栅格（50mil，同兜底消解环）
MAX_STEPS = 400  # 推挤环步数上限


class FusionError(Exception):
    """融合推挤不可解（hard×hard / hard 闯外模块 core / 板内无空位）。"""


def band_width(spec: BoardSpec, sheet: str) -> float:
    """模块融合带宽度：模块 override → by_class[sheet] → default。"""
    for m in spec.modules:
        if m.sheet == sheet and m.fusion_band is not None:
            return m.fusion_band
    return spec.fusion_band.by_class.get(sheet, spec.fusion_band.default)


def _inflate(box: Box, d: float) -> Box:
    return (box[0] - d, box[1] - d, box[2] + d, box[3] + d)


def resolve_fusion(
    spec: BoardSpec,
    sheets: dict[str, SheetPlan],
    placements: dict[str, Placement],
    geoms: dict[str, FootprintGeom],
    *,
    repack: bool = True,
) -> int:
    """原地把 soft 器件推出冲突区，返回推挤步数（0 = 本无冲突）。

    ``placements``（板系合并）与 ``sheets[*].placements`` 同步更新；
    hard 器件与各模块 core 不动。

    ``repack``：单点死锁时是否退全局确定性重排兜底。``place``（板尺寸
    固定）必须 ``True``——贪心推挤走死胡同 ≠ 板真无解，重排保证终态
    「合法不重叠」；``shrink``（尺寸二分）要 ``False``——死锁即「该尺寸
    太紧」，保留 ``FusionError`` 作地板信号，否则重排会把板收到只剩
    散点、质量门拦不住。
    """
    order = list(dict.fromkeys(m.sheet for m in spec.modules))
    mod_of = {r: s for s, sp in sheets.items() for r in sp.placements}
    rank = {r: (order.index(mod_of[r]), r) for r in placements}

    def box_of(r: Placement | str) -> Box:
        p = placements[r] if isinstance(r, str) else r
        g = geoms[p.ref]
        return g.bbox_at(p.x, p.y, p.rot)

    hard = {r for r, p in placements.items() if p.mobility != "soft"}
    territory = {  # core⊕band：外模块 soft 的禁区（本模块器件可贴自己 core）
        s: _inflate(sp.core, band_width(spec, s))
        for s, sp in sheets.items()
        if sp.core is not None
    }
    core = {s: sp.core for s, sp in sheets.items() if sp.core is not None}

    # --- 先检不可解项：hard×hard ctyd、hard × 外模块 core（未膨胀）
    fatal: list[str] = []
    boxes = {r: box_of(r) for r in placements}
    for a, b, area in find_overlaps(placements, geoms):
        if a in hard and b in hard:
            fatal.append(f"hard×hard ctyd 重叠 {a}({mod_of[a]})×{b}({mod_of[b]}) {area:.2f}mm²")
    for r in hard:
        for s, c in core.items():
            if mod_of[r] == s:
                continue
            area = overlap_area(boxes[r], c)
            if area > 0:
                fatal.append(f"hard {r}({mod_of[r]}) 闯 core[{s}] {area:.2f}mm²")
    if fatal:
        raise FusionError("融合推挤不可解（锚点声明冲突，须改 spec）: " + "; ".join(sorted(fatal)))

    # --- 推挤环：每轮全扫取最大冲突，让位方局部择优挪一步
    W, H = spec.size
    home = {r: (placements[r].x, placements[r].y) for r in placements}  # 归属位
    pushes = 0
    for _ in range(MAX_STEPS):
        conflicts: list[tuple[float, str, Box, int]] = []  # 面积·被推方·障碍框·类别序
        detail: list[str] = []
        for a, b, area in find_overlaps(placements, geoms):  # 器件×器件 ctyd
            if a in hard and b in hard:
                continue  # 上面已排除；推动中也不会新增
            # 被推方：hard 不让；双 soft 推 rank（spec 模块序+ref 序）靠后者
            if a in hard:
                push, other = b, a
            elif b in hard:
                push, other = a, b
            elif rank[a] > rank[b]:
                push, other = a, b
            else:
                push, other = b, a
            if area < AREA_EPS:
                continue
            conflicts.append((area, push, boxes[other], 0))
            detail.append(f"{push}({mod_of[push]})×{other}({mod_of[other]}) ctyd {area:.2f}mm²")
        for r, p in placements.items():  # soft × 外模块 core⊕band
            if r in hard:
                continue
            for s, t in territory.items():
                if s == mod_of[r]:
                    continue
                area = overlap_area(boxes[r], t)
                if area >= AREA_EPS:
                    conflicts.append((area, r, t, 1))
                    detail.append(f"{r}({mod_of[r]}) 闯 band[{s}] {area:.2f}mm²")
        if not conflicts:
            return pushes
        conflicts.sort(key=lambda t: (-t[0], t[1], t[3]))
        _, ref, ob, _ = conflicts[0]
        if not _escape(
            placements, sheets, geoms, mod_of, boxes, territory, home, ref, ob, W, H
        ):
            # 单点死锁（局部 12 候选 + 最近零冲突格皆空）≠ 板真无解——贪心
            # 逐点推挤会在窄棋盘里走死胡同（先挪的器件把后挪的唯一出路
            # 占死）。repack 退到全局确定性重排，保证「合法不重叠」终态；
            # shrink 关掉它，让死锁如实上浮为「尺寸太紧」。
            if repack and _repack_soft(
                placements, sheets, geoms, mod_of, boxes,
                territory, core, hard, rank, W, H,
            ):
                return pushes
            raise FusionError(
                f"融合推挤无空位：{ref}({mod_of[ref]}) 局部无改善"
                f"{'且全局重排亦失败' if repack else ''}"
            )
        pushes += 1
    raise FusionError(
        f"融合推挤 {MAX_STEPS} 步耗尽仍有 {len(conflicts)} 处冲突: "
        + "; ".join(sorted(detail))
    )


def _escape(
    placements: dict[str, Placement],
    sheets: dict[str, SheetPlan],
    geoms: dict[str, FootprintGeom],
    mod_of: dict[str, str],
    boxes: dict[str, Box],
    territory: dict[str, Box],
    home: dict[str, tuple[float, float]],
    ref: str,
    obstacle: Box,
    W: float,
    H: float,
) -> bool:
    """两级逃离：局部 12 候选择优（严格减冲突）→ 失败则环搜最近零冲突格。

    单轴 4 向只保证脱出当前障碍、不保证不再撞别的（窄走廊乒乓）；
    斜向候选让被夹器件横轴脱身。深陷大障碍内部（任何位移都等面积）
    时局部搜索必败 → 栅格环搜整板最近零冲突格腾挪。两级皆空 → False。
    """
    p = placements[ref]
    b = boxes[ref]
    pen_x = min(b[2], obstacle[2]) - max(b[0], obstacle[0])
    pen_y = min(b[3], obstacle[3]) - max(b[1], obstacle[1])
    ex = math.ceil((pen_x + 0.01) / LATTICE) * LATTICE
    ey = math.ceil((pen_y + 0.01) / LATTICE) * LATTICE
    cands = (
        (ex, 0.0), (-ex, 0.0), (0.0, ey), (0.0, -ey),
        (ex, LATTICE), (ex, -LATTICE), (-ex, LATTICE), (-ex, -LATTICE),
        (LATTICE, ey), (-LATTICE, ey), (LATTICE, -ey), (-LATTICE, -ey),
    )
    others = [bx for o, bx in boxes.items() if o != ref]  # 全板器件框
    foreign = [t for s, t in territory.items() if s != mod_of[ref]]

    def area_at(nx: float, ny: float) -> float | None:
        nb = geoms[ref].bbox_at(nx, ny, p.rot)
        if not (-1e-9 <= nb[0] and nb[2] <= W + 1e-9 and -1e-9 <= nb[1] and nb[3] <= H + 1e-9):
            return None  # 出板
        return (sum(a for a in (overlap_area(nb, ob) for ob in others) if a >= AREA_EPS)
                + sum(a for a in (overlap_area(nb, t) for t in foreign) if a >= AREA_EPS))

    hx, hy = home[ref]
    best: tuple[float, float, float, float, float] | None = None  # 面积·离家·步长·nx·ny
    cur = area_at(p.x, p.y)
    if cur is not None:
        for dx, dy in cands:
            a = area_at(p.x + dx, p.y + dy)
            if a is None or a >= cur:
                continue  # 出板或无改善
            key = (a, (p.x + dx - hx) ** 2 + (p.y + dy - hy) ** 2,
                   abs(dx) + abs(dy), p.x + dx, p.y + dy)
            if best is None or key < best:
                best = key
    if best is not None:
        _, _, _, nx, ny = best
    else:  # 深陷：环搜最近零冲突格（自 p 出发 1.27 栅格，曼哈顿近者胜）
        nx, ny = _nearest_free(geoms, p, others, foreign, W, H)
        if nx is None:
            return False
    q = Placement(ref, nx, ny, p.rot, p.side, p.mobility)
    placements[ref] = q
    sheets[mod_of[ref]].placements[ref] = q
    boxes[ref] = geoms[ref].bbox_at(nx, ny, p.rot)
    return True


def _nearest_free(
    geoms: dict[str, FootprintGeom],
    p: Placement,
    others: list[Box],
    foreign: list[Box],
    W: float,
    H: float,
) -> tuple[float | None, float | None]:
    """全板 1.27 栅格环搜最近零冲突格（器件框 + 外模块 territory 均须避让）。"""
    gx_max = int(W / LATTICE) + 2
    gy_max = int(H / LATTICE) + 2
    best: tuple[float, float, float] | None = None  # 格步曼哈顿·ny·nx
    for gx in range(-gx_max, gx_max + 1):
        for gy in range(-gy_max, gy_max + 1):
            nx, ny = p.x + gx * LATTICE, p.y + gy * LATTICE
            nb = geoms[p.ref].bbox_at(nx, ny, p.rot)
            if not (-1e-9 <= nb[0] and nb[2] <= W + 1e-9 and -1e-9 <= nb[1] and nb[3] <= H + 1e-9):
                continue
            if any(overlap_area(nb, ob) >= AREA_EPS for ob in others):
                continue
            if any(overlap_area(nb, t) >= AREA_EPS for t in foreign):
                continue
            key = (abs(gx) + abs(gy), ny, nx)
            if best is None or key < best:
                best = key
    if best is None:
        return None, None
    return best[2], best[1]


def _repack_soft(
    placements: dict[str, Placement],
    sheets: dict[str, SheetPlan],
    geoms: dict[str, FootprintGeom],
    mod_of: dict[str, str],
    boxes: dict[str, Box],
    territory: dict[str, Box],
    core: dict[str, Box],
    hard: set[str],
    rank: dict[str, tuple[int, str]],
    W: float,
    H: float,
) -> bool:
    """单点死锁的全局确定性重排：soft 重放到板内自由 1.27 栅格（终态）。

    与 fallback._repack_legal 同构——贪心推挤走死胡同 ≠ 板真无解，退到
    确定性重排保证「合法不重叠」：soft 按 spec 模块序 + ref 序，逐个放到
    板内第一个不碰（硬器件框 ∪ 外模块 core ∪ 外模块 core⊕band ∪ 已放
    soft）的栅格。带宽是走线预留、非合法性硬约束——严格带内放不下时去
    带宽重试；两步皆败才 False（板内真无空位，诚实上浮）。返回 True 时
    placements / sheets / boxes 已原地更新为无冲突终态。
    """
    softs = sorted(
        (r for r, p in placements.items() if p.mobility == "soft"),
        key=lambda r: rank[r],
    )
    hard_boxes = [boxes[h] for h in hard]
    gx_max = int(W / LATTICE) + 1
    gy_max = int(H / LATTICE) + 1

    def _try(respect_band: bool) -> dict[str, tuple[float, float]] | None:
        placed: list[Box] = []
        out: dict[str, tuple[float, float]] = {}
        for r in softs:
            g = geoms[r]
            rot = placements[r].rot
            fixed = hard_boxes + [
                c for s, c in core.items() if s != mod_of[r] and c is not None
            ]
            foreign = (
                [t for s, t in territory.items() if s != mod_of[r]]
                if respect_band else []
            )
            spot = None
            for gx in range(-1, gx_max + 1):
                for gy in range(-1, gy_max + 1):
                    nb = g.bbox_at(gx * LATTICE, gy * LATTICE, rot)
                    if not (-1e-9 <= nb[0] and nb[2] <= W + 1e-9
                            and -1e-9 <= nb[1] and nb[3] <= H + 1e-9):
                        continue
                    if any(overlap_area(nb, ob) >= AREA_EPS for ob in fixed):
                        continue
                    if any(overlap_area(nb, ob) >= AREA_EPS for ob in placed):
                        continue
                    if any(overlap_area(nb, t) >= AREA_EPS for t in foreign):
                        continue
                    spot = (gx * LATTICE, gy * LATTICE)
                    break
                if spot is not None:
                    break
            if spot is None:
                return None
            out[r] = spot
            placed.append(g.bbox_at(spot[0], spot[1], rot))
        return out

    new = _try(True)
    if new is None:
        new = _try(False)
    if new is None:
        return False

    for r, (nx, ny) in new.items():
        p = placements[r]
        q = Placement(r, nx, ny, p.rot, p.side, p.mobility)
        placements[r] = q
        sheets[mod_of[r]].placements[r] = q
        boxes[r] = geoms[r].bbox_at(nx, ny, p.rot)
    return True
