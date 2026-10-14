"""recipe 引擎：注册 / 参数校验 / mobility / 出口候选契约（M2-2）。

recipe = 参数化布局函数（方案 §5）：输入 ModuleContext（器件子集 + 几何 +
net 关系 + 参数），输出 RecipeResult（模块局部系 placements + 对外 net 的
Top-K 出口候选）。引擎只认契约，不 import 任何具体 recipe——配方库在顶层
``recipes/`` 包，由调用方（CLI/测试）注入。

契约（硬规则，违反即 RecipeError，方案 §5）：
A 覆盖——ctx 器件恰好放置一遍（无缺无多）；
B 出口——每条对外 net（有 pin 在子集外）输出 8~16 个候选
  (坐标, 朝向 0/90/180/270, 有限代价)；不得给未知 net 出口；
C mobility——存在 hard 器件时必须声明 core，且 hard 器件放置态
  courtyard ⊆ core（fringe 融合带内无 hard → 融合推挤永远有余地；
  融合阶段的带内复查在 M2 融合切片落实）；
D 合法——子集内部 courtyard 两两重叠 = 0。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ingest.ir import DesignIR, DeviceIR, NetIR
from ..kicad.footprint import Box, FootprintGeom
from .geometry import Placement, find_overlaps

EPS = 1e-6
ORIENTS = (0, 90, 180, 270)
EXIT_K_MIN, EXIT_K_MAX = 8, 16


# ---------------------------------------------------------------- 数据结构

@dataclass
class ExitCandidate:
    """模块对外 net 的一个出口点（模块局部系）。朝向 = 出线方向。"""

    x: float
    y: float
    orient: int  # 0=+x, 90=上(−y), 180=−x, 270=下(+y)——与 Placement 旋转向量同约定
    cost: float  # 预估绕行长度 + 方向拥堵（recipe 定义，引擎只查有限性）


@dataclass
class RecipeResult:
    placements: dict[str, Placement]  # ref → 模块局部系放置
    exits: dict[str, list[ExitCandidate]] = field(default_factory=dict)
    core: Box | None = None  # 契约 C：hard 器件专属区（局部系）


@dataclass
class ModuleContext:
    """recipe 的输入：一个模块（或其子集）的全部相关信息。

    external/intra 按子集 ref 划分（net 有 pin 的 ref 不在子集内 = 对外），
    子集 = 整模块时与 net.modules 划分等价。
    """

    name: str
    devices: dict[str, DeviceIR]  # 子集器件
    geoms: dict[str, FootprintGeom]  # 全板几何（recipe 可查模块外器件如 BGA 锚点）
    intra_nets: list[NetIR]
    external_nets: list[NetIR]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def refs(self) -> set[str]:
        return set(self.devices)


def build_module_context(
    ir: DesignIR,
    geoms: dict[str, FootprintGeom],
    module: str,
    refs: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> ModuleContext:
    """IR → ModuleContext。refs=None 取整模块；指定则为子集（module_spec override）。"""
    mod = ir.modules.get(module)
    if mod is None:
        raise KeyError(f"IR 无模块 {module!r}（有: {sorted(ir.modules)}）")
    subset = set(refs) if refs is not None else set(mod.devices)
    unknown = subset - set(ir.devices)
    if unknown:
        raise KeyError(f"子集引用了不存在/不在该模块的器件: {sorted(unknown)}")
    devices = {r: ir.devices[r] for r in sorted(subset)}
    intra: list[NetIR] = []
    external: list[NetIR] = []
    for net in ir.nets:
        if not any(p.ref in subset for p in net.pins):
            continue  # 与子集无关
        (external if any(p.ref not in subset for p in net.pins) else intra).append(net)
    return ModuleContext(
        name=module,
        devices=devices,
        geoms=geoms,
        intra_nets=intra,
        external_nets=external,
        params=dict(params or {}),
    )


# ---------------------------------------------------------------- 注册与校验

@dataclass
class RecipeDef:
    name: str
    version: int
    params: dict[str, Any]  # 参数名 → 默认值（也是合法参数名集合）
    run: Callable[[ModuleContext], RecipeResult]


class RecipeError(Exception):
    """契约违反（或 recipe 缺失/参数不合法），violations 逐条可读。"""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class RecipeEngine:
    """注册表 + 契约执行器。具体 recipe 由 recipes/ 包注入。"""

    def __init__(self) -> None:
        self._recipes: dict[str, RecipeDef] = {}

    def register(self, defn: RecipeDef) -> None:
        if defn.name in self._recipes:
            raise ValueError(f"recipe 重名: {defn.name}")
        self._recipes[defn.name] = defn

    def get(self, name: str) -> RecipeDef:
        if name not in self._recipes:
            raise RecipeError([f"未注册的 recipe: {name!r}（有: {sorted(self._recipes)}）"])
        return self._recipes[name]

    def names(self) -> list[str]:
        return sorted(self._recipes)

    def run(self, name: str, ctx: ModuleContext) -> RecipeResult:
        defn = self.get(name)
        violations: list[str] = []
        bad_params = set(ctx.params) - set(defn.params)
        if bad_params:
            violations.append(
                f"参数 {sorted(bad_params)} 不在 {name} 参数表 {sorted(defn.params)}"
            )
        merged = {**defn.params, **{k: v for k, v in ctx.params.items() if k in defn.params}}
        ctx.params = merged
        result = defn.run(ctx)
        violations.extend(validate_result(result, ctx))
        if violations:
            raise RecipeError(violations)
        return result


def validate_result(result: RecipeResult, ctx: ModuleContext) -> list[str]:
    """契约 A~D 逐条检查 → 违规清单（空 = 通过）。"""
    v: list[str] = []

    # A 覆盖
    missing = sorted(ctx.refs - set(result.placements))
    extra = sorted(set(result.placements) - ctx.refs)
    if missing:
        v.append(f"[A] 未放置: {missing}")
    if extra:
        v.append(f"[A] 放置了子集外器件: {extra}")

    # B 出口候选
    all_net_names = {n.name for n in ctx.intra_nets} | {n.name for n in ctx.external_nets}
    unknown_exits = sorted(set(result.exits) - all_net_names)
    if unknown_exits:
        v.append(f"[B] 出口给了未知 net: {unknown_exits}")
    for net in ctx.external_nets:
        cands = result.exits.get(net.name)
        if not cands:
            v.append(f"[B] 对外 net {net.name} 缺出口候选")
            continue
        if not (EXIT_K_MIN <= len(cands) <= EXIT_K_MAX):
            v.append(f"[B] {net.name} 出口数 {len(cands)} 不在 [{EXIT_K_MIN},{EXIT_K_MAX}]")
        for i, c in enumerate(cands):
            if c.orient not in ORIENTS:
                v.append(f"[B] {net.name}#{i} 朝向 {c.orient} 非 0/90/180/270")
            if not math.isfinite(c.cost) or c.cost < 0:
                v.append(f"[B] {net.name}#{i} 代价 {c.cost} 非有限非负")

    # C mobility / core
    hard = sorted(r for r, p in result.placements.items() if p.mobility == "hard")
    if hard and result.core is None:
        v.append(f"[C] 存在 hard 器件 {hard} 但未声明 core")
    if result.core is not None:
        cx1, cy1, cx2, cy2 = result.core
        if cx2 - cx1 <= 0 or cy2 - cy1 <= 0:
            v.append(f"[C] core 非法（零/负尺寸）: {result.core}")
        for ref in hard:
            g = ctx.geoms.get(ref)
            if g is None:
                continue  # 几何缺失已由装载层报告
            plc = result.placements[ref]
            x1, y1, x2, y2 = g.bbox_at(plc.x, plc.y, plc.rot)
            if (x1 < cx1 - EPS or y1 < cy1 - EPS
                    or x2 > cx2 + EPS or y2 > cy2 + EPS):
                v.append(f"[C] hard 器件 {ref} 的 courtyard 超出 core")

    # D 合法性：side + 子集内 courtyard 零重叠
    for ref, p in sorted(result.placements.items()):
        if p.side not in ("F", "B"):
            v.append(f"[D] {ref} side {p.side!r} 非 F/B")
        if not (math.isfinite(p.x) and math.isfinite(p.y) and math.isfinite(p.rot)):
            v.append(f"[D] {ref} 坐标/旋转非有限值")
    overlaps = find_overlaps(result.placements, ctx.geoms)
    for a, b, area in overlaps:
        v.append(f"[D] {a} 与 {b} courtyard 重叠 {area:.2f}mm²")

    return v
