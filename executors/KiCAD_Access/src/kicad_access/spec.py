"""板规格 YAML（方案 §6）：每块板一个文件，描述"这块板与别块的差异"。

这是通用工具的板级入口——引擎/recipe 库跨板复用，板子本身不写代码，
只写 ``projects/<board>.yaml``：

    project_info / board(板框·层叠·融合带·模板板) / modules(锚点·recipe
    指派·参数·子集 override) / routing(关键网络规则，M3 消费)。

本模块只做解析 + 结构校验（违规收集后一次性抛 SpecError）；与 IR 的
绑定（sheet 是否存在、refs 是否落在 sheet 内）在 floorplan 阶段做——
spec 单独可测、可 diff、可版本化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EDGES = ("LEFT", "RIGHT", "TOP", "BOTTOM")
ANCHOR_KINDS = ("coordinate", "edge_align", "relative")
OUTER_COPPER = ("F.Cu", "B.Cu")  # 外层 = 信号专用（M3-3c 平面只许内层）
_INNER_CU_RE = re.compile(r"^In\d+\.Cu$")


class SpecError(Exception):
    """板规格违规（逐条可读）。"""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass
class MountHole:
    size: float
    pos: tuple[float, float]


@dataclass
class FusionBand:
    default: float
    by_class: dict[str, float] = field(default_factory=dict)


@dataclass
class PlacementSpec:
    anchor: str  # coordinate | edge_align | relative
    pos: tuple[float, float] | None = None
    edge: str | None = None  # edge_align: LEFT/RIGHT/TOP/BOTTOM
    locked: bool = False
    relative_to: str | None = None  # relative: "board_center" 或另一 sheet 名


@dataclass
class ModuleSpec:
    sheet: str
    placement: PlacementSpec
    recipe: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    refs: list[str] | None = None  # 子集 override（None=整 sheet）
    fusion_band: float | None = None  # 覆盖全局 default


@dataclass(frozen=True)
class PartOverride:
    """器件换装（part_overrides.<ref>）：同 sch 换封装变体的内存覆盖。

    footprint 覆盖封装字符串；pinmap_from/to 按 pin 名对齐把现引脚号
    （如球号）重编号为目标号（如 eLQFP 数字号）——sch 文件不动。
    """

    footprint: str | None = None
    pinmap_from: Path | None = None
    pinmap_to: Path | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    drop_unmapped: bool = True


@dataclass
class BoardSpec:
    name: str
    eda_target: str
    outline_shape: str
    size: tuple[float, float]
    layer_count: int
    gnd_plane: str | None  # 内层平面角色（M3-3c）：层名，如 "In1.Cu"；None = 无
    power_plane: str | None
    signal_layers: tuple[str, ...] = ()  # 派生：F + 非平面内层 + B（栈序，M3 布线层表）
    mount_holes: list[MountHole] = field(default_factory=list)
    template_pcb: Path | None = None  # 参考/模板板（相对 spec 文件解析；placed-unrouted 导出底版）
    schematic: Path | None = None  # project_info.schematic（相对 spec 文件解析；place 的 IR 入口）
    fusion_band: FusionBand = field(default_factory=lambda: FusionBand(1.5))
    modules: list[ModuleSpec] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)  # 原样透传（M3 布线消费）
    part_overrides: dict[str, PartOverride] = field(default_factory=dict)
    source: Path | None = None


# ---------------------------------------------------------------- 校验工具


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _vec2(v: Any) -> tuple[float, float] | None:
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v):
        return (float(v[0]), float(v[1]))
    return None


def _plane_layer(layers: dict[str, Any], role: str, v: list[str],
                 layer_count: int) -> str | None:
    """board.layers.<role> → 规范层名（None=未声明）；违规收集、坏值归 None。"""
    val = layers.get(role)
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        v.append(f"board.layers.{role} 层名为空")
    elif s in OUTER_COPPER:
        v.append(f"board.layers.{role}={s!r} 是外层——平面须用内层")
    elif not _INNER_CU_RE.match(s):
        v.append(f"board.layers.{role}={s!r} 须为内层铜名（如 In1.Cu）")
    elif int(s[2:-3]) > layer_count - 2:
        v.append(
            f"board.layers.{role}={s!r} 超出 layer_count={layer_count}（内层至多 "
            f"In{layer_count - 2}.Cu）")
    else:
        return s
    return None


def _signal_layers(layer_count: int, *planes: str | None) -> tuple[str, ...]:
    """信号层表（栈序）：F + 非平面内层 + B——布线器/DSN 的层宇宙。

    2 层 (F,B)；4 层平面 In1/In2 → 仍 (F,B)；6 层平面 In1/In4 →
    (F.Cu, In2.Cu, In3.Cu, B.Cu)——内层信号层由此解锁。
    """
    taken = {p for p in planes if p}
    return (
        ("F.Cu",)
        + tuple(f"In{i}.Cu" for i in range(1, layer_count - 1)
                if f"In{i}.Cu" not in taken)
        + ("B.Cu",)
    )


def load_spec(path: str | Path) -> BoardSpec:
    """YAML → BoardSpec；全部违规收集后一次抛出。"""
    src = Path(path)
    if not src.is_file():
        raise SpecError([f"规格文件不存在: {src}"])
    try:
        raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SpecError([f"YAML 解析失败: {e}"]) from e
    if not isinstance(raw, dict):
        raise SpecError(["规格根节点必须是 mapping"])

    v: list[str] = []

    # --- project_info
    info = raw.get("project_info")
    if not isinstance(info, dict):
        v.append("缺 project_info {name, eda_target}")
        info = {}
    name = str(info.get("name") or "")
    eda_target = str(info.get("eda_target") or "")
    if not name:
        v.append("project_info.name 为空")
    if not eda_target:
        v.append("project_info.eda_target 为空")

    # --- board
    b = raw.get("board")
    if not isinstance(b, dict):
        v.append("缺 board 块")
        b = {}
    outline = b.get("outline") or {}
    shape = str(outline.get("shape") or "")
    size = _vec2(outline.get("size"))
    if shape != "rectangle":
        v.append(f"board.outline.shape {shape!r} 不支持（当前仅 rectangle）")
    if size is None:
        v.append("board.outline.size 须为 [宽, 高] 两数值")
        size = (0.0, 0.0)
    elif size[0] <= 0 or size[1] <= 0:
        v.append(f"board.outline.size {size} 须为正")
    layer_count = b.get("layer_count")
    if not isinstance(layer_count, int) or isinstance(layer_count, bool) or layer_count < 2:
        v.append(f"board.layer_count {layer_count!r} 须为 ≥2 的整数")
        layer_count = 4

    # board.layers（M3-3c 可选层角色）：gnd_plane/power_plane → 内层平面
    layers = b.get("layers") or {}
    if layers and not isinstance(layers, dict):
        v.append("board.layers 须为 mapping {gnd_plane, power_plane}")
        layers = {}
    gnd_plane = _plane_layer(layers, "gnd_plane", v, layer_count)
    power_plane = _plane_layer(layers, "power_plane", v, layer_count)
    if gnd_plane and power_plane and gnd_plane == power_plane:
        v.append(f"board.layers.gnd_plane 与 power_plane 不能同为 {gnd_plane!r}")
    if (gnd_plane or power_plane) and layer_count < 4:
        v.append(f"board.layer_count={layer_count} 不足以声明内层平面（须 ≥4）")

    holes: list[MountHole] = []
    for i, h in enumerate(b.get("mount_holes") or []):
        pos = _vec2(h.get("pos"))
        sz = h.get("size")
        if pos is None or not _num(sz) or sz <= 0:
            v.append(f"mount_holes[{i}] 须为 {{size>0, pos:[x,y]}}")
        else:
            holes.append(MountHole(float(sz), pos))

    fb = b.get("fusion_band") or {}
    default_band = fb.get("default")
    if not _num(default_band) or default_band <= 0:
        v.append("board.fusion_band.default 须为 >0 数值")
        default_band = 1.5
    by_class: dict[str, float] = {}
    for k, val in (fb.get("by_class") or {}).items():
        if not _num(val) or val <= 0:
            v.append(f"fusion_band.by_class.{k}={val!r} 须为 >0 数值")
        else:
            by_class[str(k)] = float(val)

    tp = b.get("template_pcb")
    template_pcb = (src.parent / str(tp)).resolve() if tp else None
    sch = info.get("schematic")
    schematic = (src.parent / str(sch)).resolve() if sch else None

    # --- modules
    mods_raw = raw.get("modules")
    if not isinstance(mods_raw, list) or not mods_raw:
        v.append("modules 须为非空列表")
        mods_raw = []
    modules: list[ModuleSpec] = []
    for i, mm in enumerate(mods_raw):
        if not isinstance(mm, dict):
            v.append(f"modules[{i}] 须为 mapping")
            continue
        sheet = str(mm.get("sheet") or "")
        if not sheet:
            v.append(f"modules[{i}].sheet 为空")
        pl = mm.get("placement") or {}
        kind = str(pl.get("anchor") or "")
        if kind not in ANCHOR_KINDS:
            v.append(f"modules[{i}]({sheet}) placement.anchor {kind!r} 不在 {ANCHOR_KINDS}")
            kind = "coordinate"
        pos = _vec2(pl.get("pos"))
        edge = pl.get("edge")
        rel_to = pl.get("relative_to")
        if kind == "coordinate" and pos is None:
            v.append(f"modules[{i}]({sheet}) coordinate 锚点缺 pos [x,y]")
        if kind == "edge_align":
            if edge not in EDGES:
                v.append(f"modules[{i}]({sheet}) edge_align 的 edge {edge!r} 不在 {EDGES}")
            if pos is not None:
                v.append(f"modules[{i}]({sheet}) edge_align 不接受 pos（位置由 floorplan 定）")
        if kind == "relative" and not rel_to:
            v.append(f"modules[{i}]({sheet}) relative 锚点缺 relative_to")
        refs = mm.get("refs")
        if refs is not None and (
            not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs)
        ):
            v.append(f"modules[{i}]({sheet}) refs 须为非空字符串列表")
            refs = None
        params = mm.get("params") or {}
        if not isinstance(params, dict):
            v.append(f"modules[{i}]({sheet}) params 须为 mapping")
            params = {}
        band = mm.get("fusion_band")
        if band is not None and (not _num(band) or band <= 0):
            v.append(f"modules[{i}]({sheet}) fusion_band={band!r} 须为 >0 数值")
            band = None
        modules.append(
            ModuleSpec(
                sheet=sheet,
                placement=PlacementSpec(
                    anchor=kind,
                    pos=pos,
                    edge=str(edge) if edge in EDGES else None,
                    locked=bool(pl.get("locked", False)),
                    relative_to=str(rel_to) if rel_to else None,
                ),
                recipe=str(mm["recipe"]) if mm.get("recipe") else None,
                params=dict(params),
                refs=list(refs) if refs else None,
                fusion_band=float(band) if band else None,
            )
        )

    v.extend(_check_module_topology(modules))

    # --- routing（结构轻校验，语义 M3 才消费）
    routing = raw.get("routing") or {}
    if not isinstance(routing, dict):
        v.append("routing 须为 mapping")
        routing = {}

    # --- part_overrides（器件换装：同 sch 的封装变体，内存覆盖不写回）
    po_raw = raw.get("part_overrides") or {}
    if po_raw and not isinstance(po_raw, dict):
        v.append("part_overrides 须为 mapping {ref: {footprint|pinmap_from+pinmap_to, …}}")
        po_raw = {}
    part_overrides: dict[str, PartOverride] = {}
    for ref, cfg in po_raw.items():
        if not isinstance(cfg, dict):
            v.append(f"part_overrides.{ref} 须为 mapping")
            continue
        fp = cfg.get("footprint")
        pmf, pmt = cfg.get("pinmap_from"), cfg.get("pinmap_to")
        if not fp and not (pmf and pmt):
            v.append(f"part_overrides.{ref} 至少要 footprint 或 pinmap_from+pinmap_to")
        if (pmf is None) != (pmt is None):
            v.append(f"part_overrides.{ref} pinmap_from/pinmap_to 须成对出现")
        aliases_raw = cfg.get("aliases") or {}
        if not isinstance(aliases_raw, dict):
            v.append(f"part_overrides.{ref}.aliases 须为 mapping {{现名: 目标名}}")
            aliases_raw = {}
        part_overrides[str(ref)] = PartOverride(
            footprint=str(fp) if fp else None,
            pinmap_from=(src.parent / str(pmf)).resolve() if pmf else None,
            pinmap_to=(src.parent / str(pmt)).resolve() if pmt else None,
            aliases={str(k): str(x) for k, x in aliases_raw.items()},
            drop_unmapped=bool(cfg.get("drop_unmapped", True)),
        )

    if v:
        raise SpecError(v)
    return BoardSpec(
        name=name,
        eda_target=eda_target,
        outline_shape=shape or "rectangle",
        size=size,
        layer_count=int(layer_count),
        gnd_plane=gnd_plane,
        power_plane=power_plane,
        signal_layers=_signal_layers(layer_count, gnd_plane, power_plane),
        mount_holes=holes,
        template_pcb=template_pcb,
        schematic=schematic,
        fusion_band=FusionBand(float(default_band), by_class),
        modules=modules,
        routing=dict(routing),
        part_overrides=part_overrides,
        source=src,
    )


def _check_module_topology(modules: list[ModuleSpec]) -> list[str]:
    """跨条目规则：sheet 重名/子集相容/relative_to 引用与环。"""
    v: list[str] = []
    by_sheet: dict[str, list[ModuleSpec]] = {}
    for m in modules:
        by_sheet.setdefault(m.sheet, []).append(m)

    for sheet, group in by_sheet.items():
        if len(group) == 1:
            continue
        # 同 sheet 多条目 = 拆子集分别挂 recipe：至多一条不带 refs（吃余量）
        no_refs = [m for m in group if m.refs is None]
        if len(no_refs) > 1:
            v.append(f"sheet {sheet} 有 {len(no_refs)} 条无 refs 条目（至多 1 条吃余量）")
        # 子集共享同一模块坐标系：placement 声明必须逐字段一致
        first = group[0].placement
        for m in group[1:]:
            if m.placement != first:
                v.append(
                    f"sheet {sheet} 多条目 placement 声明不一致（子集共享模块坐标系，须相同）"
                )
                break
        seen: set[str] = set()
        for m in group:
            if m.refs is None:
                continue
            dup_in = sorted({r for r in m.refs if m.refs.count(r) > 1})
            if dup_in:
                v.append(f"sheet {sheet} 条目 refs 内部重复器件 {dup_in}")
            dup = sorted(set(m.refs) & seen)
            if dup:
                v.append(f"sheet {sheet} 子集重复器件 {dup}")
            seen |= set(m.refs)

    sheets = set(by_sheet)
    for m in modules:
        rel = m.placement.relative_to
        if m.placement.anchor == "relative" and rel and rel != "board_center":
            if rel not in sheets:
                v.append(f"{m.sheet} relative_to {rel!r} 不是本板的 sheet")
            elif rel == m.sheet:
                v.append(f"{m.sheet} relative_to 指向自己")

    # relative 依赖环（board_center 为根，不参与环）
    graph = {
        m.sheet: [m.placement.relative_to]
        for m in modules
        if m.placement.anchor == "relative"
        and m.placement.relative_to
        and m.placement.relative_to != "board_center"
    }
    for start in graph:
        node, path = start, []
        while node in graph:
            if node in path:
                v.append(f"relative_to 成环: {' → '.join(path + [node])}")
                break
            path.append(node)
            node = graph[node][0]
    return v
