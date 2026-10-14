"""floorplan：板规格锚点 × recipe 结果 → 整板坐标（M2-2b）。

消费 ``BoardSpec``（锚点声明）+ IR + 几何 + recipe 引擎，把各模块的
局部系装配进板系：

- 先在**局部系**逐条目跑 recipe（与板位置无关），再解锚点得 ModuleFrame
  （原点 + 90° 整数倍旋转），最后整体变换到板系；
- ``edge_align``：模块局部 +x = "连接器朝外"方向，映射到板边外法向
  （RIGHT=0°/TOP=90°/LEFT=180°/BOTTOM=270°，与器件旋转向量同约定）；
  同边多模块按 spec 声明序沿边排布（edge_gap 间隔、整组居中、贴边内缩
  edge_inset）；
- ``relative``：board_center = 板心；<sheet> = 继承目标模块 frame；
  同 sheet 多条目共享一个 frame（spec 已保证 placement 声明一致）；
- 无 recipe 的条目用力导向兜底（``fallback.force_layout``）：只求合法
  不重叠（方案兜底栏），结果过同一套契约校验（A~D）后入库；
- anchor 级联：同 sheet 条目按声明序跑，前序条目的局部系放置结果以
  级联参数注入后序条目——``anchor_pos``/``anchor_rot`` = recipe 声明的
  anchor 器件在本帧的位置（如 mcu_core 放 U1 → decoupling_ring 环心），
  ``keep_outs`` = 前序器件 ctyd 框清单（环槽位避让晶振/电感簇）；只注入
  目标 recipe 参数表声明的名字（引擎拒收未知参数，白名单过滤）；无
  recipe 条目同样收 keep_outs（兜底把前序放置当固定障碍）；
- 融合推挤（``fusion.resolve_fusion``）：板系变换后、诊断前的纯几何步——
  soft 器件被推出外模块 core⊕band（band = 融合带宽度，兼作模块间走线
  通道预留），hard 器件与 core 绝对锁定；hard×hard / hard 闯外模块
  core 无解时 FusionError 上浮（锚点声明冲突，须改 spec）；
- 诊断：全板 ctyd 重叠 + core 与外模块器件重叠（融合推挤后应为空，
  非空即融合有漏网——测试断言兜底）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..ingest.ir import DesignIR
from ..kicad.footprint import Box, FootprintGeom
from ..spec import BoardSpec, ModuleSpec
from .fallback import force_layout
from .fusion import AREA_EPS, resolve_fusion
from .geometry import Placement, find_overlaps, overlap_area
from .recipe import (
    ExitCandidate,
    RecipeEngine,
    build_module_context,
    validate_result,
)

EDGE_ROT = {"RIGHT": 0.0, "TOP": 90.0, "LEFT": 180.0, "BOTTOM": 270.0}


class FloorplanError(Exception):
    """规格 ↔ IR 绑定失败 / 锚点不可解。"""


def _rot_pt(x: float, y: float, deg: float) -> tuple[float, float]:
    """y-down 系旋转（与 footprint.bbox_at 同约定）。"""
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _rot_box(box: Box, deg: float) -> Box:
    x1, y1, x2, y2 = box
    pts = (
        _rot_pt(x1, y1, deg), _rot_pt(x1, y2, deg),
        _rot_pt(x2, y1, deg), _rot_pt(x2, y2, deg),
    )
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _union_box(a: Box | None, b: Box | None) -> Box | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


# ---------------------------------------------------------------- 数据结构


@dataclass
class ModuleFrame:
    """模块局部系 → 板系：原点平移 + 90° 整数倍旋转。"""

    origin: tuple[float, float]
    rot: float  # 0/90/180/270


@dataclass
class EntryPlan:
    refs: list[str]
    recipe: str | None
    status: str  # placed | fallback | empty


@dataclass
class SheetPlan:
    sheet: str
    frame: ModuleFrame
    entries: list[EntryPlan]
    placements: dict[str, Placement] = field(default_factory=dict)  # 板系
    exits: dict[str, list[ExitCandidate]] = field(default_factory=dict)  # 板系
    core: Box | None = None  # 板系


@dataclass
class FloorplanResult:
    spec: BoardSpec
    sheets: dict[str, SheetPlan]
    placements: dict[str, Placement]  # 板系合并
    total_devices: int
    diagnostics: list[str]
    fusion_pushes: int = 0  # 融合推挤步数（0 = 本无跨模块冲突）

    @property
    def coverage(self) -> tuple[int, int]:
        return len(self.placements), self.total_devices


# ---------------------------------------------------------------- 主流程


def _cascade(
    engine: RecipeEngine,
    spec_params: dict,
    recipe: str,
    prev: dict[str, Placement],
    geoms: dict[str, FootprintGeom],
) -> dict:
    """同 sheet 已放条目 → 本条目级联参数（按 recipe 参数表白名单过滤）。

    - ``anchor_pos``/``anchor_rot``：anchor 器件（spec.params 或 recipe
      默认声明）在前序放置中的本帧局部系位置/旋转；
    - ``keep_outs``：前序器件放置态 ctyd 框列表（本帧局部系）。
    recipe 未声明的参数名不注入（引擎会拒收）。
    """
    defn = engine.get(recipe)
    extra: dict = {}
    anchor = spec_params.get("anchor", defn.params.get("anchor"))
    if anchor in prev:
        p = prev[anchor]
        if "anchor_pos" in defn.params:
            extra["anchor_pos"] = (p.x, p.y)
        if "anchor_rot" in defn.params:
            extra["anchor_rot"] = p.rot
    if prev and "keep_outs" in defn.params:
        extra["keep_outs"] = [
            geoms[r].bbox_at(p.x, p.y, p.rot)
            for r, p in sorted(prev.items()) if r in geoms
        ]
    return extra


def run_floorplan(
    spec: BoardSpec,
    ir: DesignIR,
    geoms: dict[str, FootprintGeom],
    engine: RecipeEngine,
    *,
    edge_gap: float = 2.0,
    edge_inset: float = 0.5,
    allow_repack: bool = True,
) -> FloorplanResult:
    # --- 1) spec ↔ IR 绑定 + 条目 refs 展开（无 refs 条目吃余量）
    by_sheet: dict[str, list[ModuleSpec]] = {}
    for m in spec.modules:
        by_sheet.setdefault(m.sheet, []).append(m)
    unknown = sorted(set(by_sheet) - set(ir.modules))
    if unknown:
        raise FloorplanError(f"spec sheet 不在 IR: {unknown}（有: {sorted(ir.modules)}）")

    entry_refs: dict[str, list[tuple[ModuleSpec, list[str]]]] = {}
    for sheet, group in by_sheet.items():
        devs = set(ir.modules[sheet].devices)
        covered: set[str] = set()
        entries: list[tuple[ModuleSpec, list[str]]] = []
        rest: ModuleSpec | None = None
        for m in group:
            if m.refs is None:
                rest = m
                continue
            bad = sorted(set(m.refs) - devs)
            if bad:
                raise FloorplanError(f"sheet {sheet} refs 不属于该模块: {bad}")
            covered |= set(m.refs)
            entries.append((m, list(m.refs)))
        leftover = sorted(devs - covered)
        if rest is not None:
            entries.append((rest, leftover))  # 余量条目（至多一条，spec 已保证）
        elif leftover:
            raise FloorplanError(f"sheet {sheet} 器件未被任何条目覆盖: {leftover}")
        entry_refs[sheet] = entries

    # --- 2) 局部系逐条目跑 recipe（与板位置无关）
    local: dict[str, dict] = {}
    for sheet, entries in entry_refs.items():
        plcs: dict[str, Placement] = {}
        exits: dict[str, list[ExitCandidate]] = {}
        core: Box | None = None
        plans: list[EntryPlan] = []
        for m, refs in entries:
            if not refs:  # 余量空集：条目无事可做
                plans.append(EntryPlan([], m.recipe, "empty"))
                continue
            if m.recipe:
                ctx = build_module_context(
                    ir, geoms, sheet, refs=refs,
                    params={
                        **m.params,
                        **_cascade(engine, m.params, m.recipe, plcs, geoms),
                    },
                )
                res = engine.run(m.recipe, ctx)
                status = "placed"
            else:  # 无 recipe → 力导向兜底：合法不重叠，同一套契约复核
                ctx = build_module_context(ir, geoms, sheet, refs=refs)
                res = force_layout(ctx, keep_outs=tuple(
                    geoms[r].bbox_at(p.x, p.y, p.rot)
                    for r, p in sorted(plcs.items()) if r in geoms
                ))
                bad = validate_result(res, ctx)
                if bad:
                    raise FloorplanError(f"sheet {sheet} 兜底结果违约: {bad}")
                status = "fallback"
            clash = sorted(set(res.placements) & set(plcs))
            if clash:  # spec 已保证 refs 不相交——防御性上浮
                raise FloorplanError(f"sheet {sheet} 条目间器件冲突: {clash}")
            plcs.update(res.placements)
            exits.update(res.exits)
            core = _union_box(core, res.core)
            plans.append(EntryPlan(refs, m.recipe, status))
        local[sheet] = {"placements": plcs, "exits": exits, "core": core, "entries": plans}

    def _bbox(plcs: dict[str, Placement]) -> Box | None:
        box: Box | None = None
        for ref, pl in plcs.items():
            g = geoms.get(ref)
            if g is not None:
                box = _union_box(box, g.bbox_at(pl.x, pl.y, pl.rot))
        return box

    local_box = {s: _bbox(d["placements"]) for s, d in local.items()}

    # --- 3) 锚点 → ModuleFrame
    W, H = spec.size
    frames: dict[str, ModuleFrame] = {}
    for sheet, group in by_sheet.items():
        pl = group[0].placement
        if pl.anchor == "coordinate":
            frames[sheet] = ModuleFrame(pl.pos or (0.0, 0.0), 0.0)

    edge_groups: dict[str, list[str]] = {}
    for sheet, group in by_sheet.items():
        pl = group[0].placement
        if pl.anchor == "edge_align" and pl.edge:
            edge_groups.setdefault(pl.edge, []).append(sheet)
    for edge, sheets_e in edge_groups.items():
        theta = EDGE_ROT[edge]
        rot_boxes = [
            (s, _rot_box(local_box[s], theta) if local_box[s] else (0.0, 0.0, 0.0, 0.0))
            for s in sheets_e
        ]
        horizontal = edge in ("TOP", "BOTTOM")
        lengths = [
            (rb[2] - rb[0]) if horizontal else (rb[3] - rb[1]) for _, rb in rot_boxes
        ]
        edge_len = W if horizontal else H
        total = sum(lengths) + edge_gap * (len(rot_boxes) - 1)
        cursor = max(edge_inset, (edge_len - total) / 2.0)
        for (sheet, rb), ln in zip(rot_boxes, lengths):
            if edge == "TOP":
                ox, oy = cursor - rb[0], edge_inset - rb[1]
            elif edge == "BOTTOM":
                ox, oy = cursor - rb[0], H - edge_inset - rb[3]
            elif edge == "RIGHT":
                ox, oy = W - edge_inset - rb[2], cursor - rb[1]
            else:  # LEFT
                ox, oy = edge_inset - rb[0], cursor - rb[1]
            frames[sheet] = ModuleFrame((ox, oy), theta)
            cursor += ln + edge_gap

    rel = {
        sheet: group[0].placement.relative_to
        for sheet, group in by_sheet.items()
        if group[0].placement.anchor == "relative"
    }
    while rel:
        progressed = False
        for sheet, target in list(rel.items()):
            if target == "board_center":
                frames[sheet] = ModuleFrame((W / 2.0, H / 2.0), 0.0)
            elif target in frames:
                frames[sheet] = ModuleFrame(frames[target].origin, frames[target].rot)
            else:
                continue
            del rel[sheet]
            progressed = True
        if not progressed:
            raise FloorplanError(f"relative_to 不可解: {sorted(rel)}")

    # --- 4) 板系变换（placements/exits/core）
    sheets: dict[str, SheetPlan] = {}
    merged: dict[str, Placement] = {}
    for sheet, d in local.items():
        fm = frames[sheet]
        ox, oy = fm.origin

        def _tx_exit(c: ExitCandidate, _fm=fm, _ox=ox, _oy=oy) -> ExitCandidate:
            bx, by = _rot_pt(c.x, c.y, _fm.rot)
            return ExitCandidate(
                _ox + bx, _oy + by, int((c.orient + _fm.rot) % 360), c.cost
            )

        plcs = {}
        for ref, p in d["placements"].items():
            bx, by = _rot_pt(p.x, p.y, fm.rot)
            plcs[ref] = Placement(
                ref, ox + bx, oy + by, (p.rot + fm.rot) % 360.0, p.side, p.mobility
            )
        exits = {name: [_tx_exit(c) for c in cands] for name, cands in d["exits"].items()}
        core = None
        if d["core"] is not None:
            rb = _rot_box(d["core"], fm.rot)
            core = (ox + rb[0], oy + rb[1], ox + rb[2], oy + rb[3])
        sheets[sheet] = SheetPlan(sheet, fm, d["entries"], plcs, exits, core)
        merged.update(plcs)

    # --- 4.5) 融合推挤：soft 让出外模块 core⊕band（纯几何，hard/core 锁定）
    pushes = resolve_fusion(spec, sheets, merged, geoms, repack=allow_repack)

    # --- 5) 诊断：全板重叠 + core 非重叠（融合推挤后应为空；
    #     与 fusion.AREA_EPS 同阈——毛刺级重叠是推挤收敛容差，非质量事故）
    diags: list[str] = []
    for a, b, area in find_overlaps(merged, geoms):
        if area >= AREA_EPS:
            diags.append(f"ctyd 重叠 {a}×{b} {area:.2f}mm²")
    mod_of = {ref: s for s, sp in sheets.items() for ref in sp.placements}
    for sheet, sp in sheets.items():
        if sp.core is None:
            continue
        for ref, p in merged.items():
            if mod_of[ref] == sheet:
                continue
            g = geoms.get(ref)
            if g is None:
                continue
            area = overlap_area(sp.core, g.bbox_at(p.x, p.y, p.rot))
            if area >= AREA_EPS:
                diags.append(f"core[{sheet}] 与 {ref} 重叠 {area:.2f}mm²")

    return FloorplanResult(
        spec=spec,
        sheets=sheets,
        placements=merged,
        total_devices=len(ir.devices),
        diagnostics=diags,
        fusion_pushes=pushes,
    )
