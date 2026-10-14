"""真实板 → PlacementProblem 转换（留出集 / SA 教师输入）。

把 DesignIR（reuse.load_design_ir）转成 env 的统一表示：器件外框尺寸取 footprint
外框（courtyard 优先），网表抽 ref 级，画布由调用方给定（本模块不解析 Edge.Cuts）。
"""
from __future__ import annotations

import re

from ..env.problem import Component, Net, PlacementProblem, center_of
from .. import reuse

_HOLE_RE = re.compile(r"MountingHole[_:](\d+(?:\.\d+)?)mm")

# 时钟器件识别：值含频率（8MHz/32.768kHz…），或 footprint/lib_id 名带晶体/振荡器。
_CLK_VALUE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:M|G)?Hz", re.IGNORECASE)
_CLK_NAME_RE = re.compile(r"Crystal|XTAL|Oscillator|Quartz", re.IGNORECASE)

# 时钟网权重：晶体↔MCU 网是关键时序路径，HPWL/奖励/顺序都按此放大，
# 使晶体被强烈拉向 MCU 引脚侧（否则淹没在去耦网里、被晚放置推远）。
CLK_WEIGHT = 8.0

# 电源网名识别：镜像 kicad_access.ingest.netbuild._is_power_name（未导出）。
# board-only 网（如 +5V_ISO1/GND1，板级隔离 DC-DC U6/U7 的输出轨）只存在于板 netlist、
# 不进 ir.nets，故其 kind 需在此处补标——否则 net_class 把它们归 "other"、去耦电容
# （C31/C32/C35/C36）失锚、固定 DC-DC 的地脚被块内聚拽空。与 kca 判据保持一致。
_POWER_HINTS = ("GND", "VCC", "VBUS", "+3V3", "+5V", "+12V", "+24V", "VIN", "VDD", "VSS")


def _is_power_name(name: str) -> bool:
    """网名是否为电源网（GND/VCC/VDD/+/VIN…），镜像 kca 的判据。"""
    n = name.upper()
    return any(h in n for h in _POWER_HINTS) or n.startswith("+")


def _category(ref: str) -> str:
    if ref.startswith("J"):
        return "connector"
    if ref.startswith("U"):
        return "ic"
    return "passive"


def _is_clock_device(dev: "reuse.DeviceIR") -> bool:
    """判断器件是否为晶体/振荡器（值含频率，或封装/符号名带时钟字样）。"""
    if _CLK_VALUE_RE.search(dev.value or ""):
        return True
    return bool(_CLK_NAME_RE.search((dev.footprint or "") + " " + (dev.lib_id or "")))


def _hole_size(name: str | None) -> float | None:
    """从 MountingHole_*.mm 名字解析孔径，返回「孔径 + 2mm 禁区余量」的障碍边长。

    安装孔 footprint 库（MountingHole）在 fp-lib-table 里缺失、``libs.load`` 返回
    None，但它们是板框内的固定障碍：可动件若压到孔上会产生 hole_clearance / 短路。
    用孔径 + 余量合成方形禁区，宁可稍保守也不让可动件贴近孔。
    """
    m = _HOLE_RE.search(name or "")
    if not m:
        return None
    # 孔径 + 0.5mm 禁区余量：足矣挡住「可动件压孔」造成的 hole_clearance/短路，
    # 又不超过 ~3.0mm，避免与紧邻的固定件（如 RJ45 庭院角）产生假重叠。
    return float(m.group(1)) + 0.5


def board_only_components(
    by_ref: dict[str, "reuse.Footprint"],
    ir_refs: set[str],
    libs: "reuse.FootprintLibs",
    xmin: float,
    ymin: float,
) -> tuple[list[Component], dict[str, tuple[float, float, float]]]:
    """板上独有、原理图 IR 里没有的 footprint（安装孔 / 板级器件）→ 固定障碍件。

    返回 ``(components, fixed_center)``：几何可解析的按 courtyard/pad 外框；安装孔
    （MountingHole_*.mm）库缺失时按 ``_hole_size`` 合成方形禁区。这些件不参与放置，
    只作为碰撞障碍写回原位，防止可动件压上去产生真实 DRC 短路/庭院重叠。
    """
    comps: list[Component] = []
    fixed: dict[str, tuple[float, float, float]] = {}
    for ref, f in sorted(by_ref.items()):
        if ref in ir_refs:
            continue
        name = f.name
        w: float | None = None
        h: float | None = None
        cx = cy = 0.0
        g = libs.load(name) if name else None
        if g is not None:
            box = g.outline()
            w, h = box[2] - box[0], box[3] - box[1]
            if w > 0 and h > 0:
                cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            else:
                w = h = None
        if w is None or h is None:
            d = _hole_size(name)
            if d is None:
                # 几何未知且非安装孔：跳过。写回仍锁定原位，只是不进碰撞模型。
                continue
            w = h = d
            cx = cy = 0.0
        c = Component(ref, w, h, 0, _category(ref), cx, cy, fp_name=name)
        ox, oy = f.x - xmin, f.y - ymin
        cx_c, cy_c = center_of(c, ox, oy, f.rot)
        fixed[ref] = (cx_c, cy_c, f.rot)
        comps.append(c)
    return comps, fixed


def build_board_nets(by_ref: dict[str, "reuse.Footprint"]) -> dict[str, set[str]]:
    """板级权威网表：net 名 → 该网上所有 ref（由 footprint 的 pad_nets 导出）。

    物理板是放置的权威真值：原理图注解可能已漂移（晶体负载电容 C17/C18 ↔ C13/C14、
    MCU 引脚 E10/E11 ↔ 69/70），但 pad_nets 是 netlist 导入到板上的 pad↔net 权威映射。
    用它替代原理图 ir.nets 的成员关系，锚定/网长/顺序都以物理板为准。
    """
    out: dict[str, set[str]] = {}
    for ref, f in by_ref.items():
        for _pnum, net in (getattr(f, "pad_nets", None) or {}).items():
            out.setdefault(net, set()).add(ref)
    return out


def build_net_pin_loc(
    by_ref: dict[str, "reuse.Footprint"],
    libs: "reuse.FootprintLibs",
) -> dict[str, dict[str, tuple[float, float]]]:
    """从板 footprint 的 pad_nets（pad 号 → net 名）+ 封装 pad 几何，构建引脚坐标。

    返回 ``net_name → {ref → (footprint 本地 x, 本地 y)}``。原理图 symbol 的引脚号
    与 footprint pad 号可能不一致（如 U1 原理图叫 E10/E11、封装 pad 却是 69/70），
    故以**板级 pad_nets** 为准：它由 netlist 导入、是 pad↔net 的权威映射。
    """
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for ref, f in by_ref.items():
        pad_nets = getattr(f, "pad_nets", None)
        if not pad_nets:
            continue
        g = libs.load(f.name) if f.name else None
        if g is None:
            continue
        pad_xy = {p.number: (p.x, p.y) for p in g.pads if p.number}
        for pnum, net in pad_nets.items():
            if pnum in pad_xy:
                out.setdefault(net, {})[ref] = pad_xy[pnum]
    return out


def problem_from_ir(
    ir: reuse.DesignIR,
    libs: reuse.FootprintLibs,
    board: tuple[float, float],
    fixed: dict[str, tuple[float, float, float]] | None = None,
    skip_nets_kind: tuple[str, ...] = (),
    footprints: dict[str, str] | None = None,
    extra_components: list[Component] | None = None,
    extra_fixed: dict[str, tuple[float, float, float]] | None = None,
    net_pin_loc: dict[str, dict[str, tuple[float, float]]] | None = None,
    board_nets: dict[str, set[str]] | None = None,
) -> PlacementProblem:
    """IR → PlacementProblem。

    ``board`` 为画布 (W, H) mm；``fixed`` 可锁定部分器件（如连接器）不参与放置，
    值 = footprint **原点** 位置 + 朝向 ``(ox, oy, rot)``（KiCad 板绝对系/本地系），
    本函数用中心偏移把它转成中心化位姿。``skip_nets_kind`` 可剔除某些 net 类。

    ``footprints`` 可选覆盖 ref → footprint lib_id：当原理图 Footprint 字段与板上
    实际封装不一致时（迁移导入 / 板级换封装），以**板上封装**为准解析几何——否则
    几何偏小会误导重叠判定，产生真实 DRC 短路/庭院重叠。
    """
    fp_override = footprints or {}

    components: list[Component] = []
    for ref, dev in ir.devices.items():
        fp = fp_override.get(ref, dev.footprint)
        if not fp:
            continue
        g = libs.load(fp)
        if g is None:
            continue
        box = g.outline()
        w, h = box[2] - box[0], box[3] - box[1]
        if w <= 0 or h <= 0:
            continue
        # 外框中心在 footprint 本地系的偏移（footprint 原点不必在体心）
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2
        components.append(Component(ref, w, h, len(dev.pins), _category(ref), cx, cy,
                                    dev.value, block=dev.module, fp_name=fp))

    # 板上独有、IR 没有的固定障碍件（安装孔/板级器件）：加入碰撞模型，锁定位姿。
    if extra_components:
        components.extend(extra_components)

    refset = {c.ref for c in components}
    # 分图语义化：板上独有件（board-only，如 U8/U9 收发器）在 IR 里没有 module，
    # 用「块专属网」投票推断——某网上所有 IR 成员同属一个块，则该网判定该块。
    # GND/+5V 等横跨多块的电源网不算（多块 → 弃权），故 U8/U9 的 CAN1_* 网全指向
    # can 块、正确归 can；而 U6 的 +5V_ISO1/GND1 也连 ethercat 的 C31/C32（弃权）、
    # SW1/SW2 的 RSTN/BOOT0 也连 mcu 的 U1（弃权）→ 空块，交给网表/电源岛自然落点。
    if board_nets and extra_components:
        module_of = {d.ref: d.module for d in ir.devices.values() if d.module}
        ref_nets: dict[str, list[str]] = {}
        for name, refs in board_nets.items():
            for r in refs:
                ref_nets.setdefault(r, []).append(name)
        for c in extra_components:
            if c.block:
                continue
            votes: dict[str, int] = {}
            for name in ref_nets.get(c.ref, ()):
                mods = {module_of[r] for r in board_nets[name]
                        if r in module_of and r != c.ref}
                if len(mods) == 1:
                    b = next(iter(mods))
                    votes[b] = votes.get(b, 0) + 1
            if votes:
                c.block = max(votes, key=votes.get)
    # 块名 → 该块 ref 列表（空块不入表：合成板/无归属件不参与块内聚）。
    blocks: dict[str, list[str]] = {}
    for c in components:
        if c.block:
            blocks.setdefault(c.block, []).append(c.ref)

    # 时钟网：晶体/振荡器（clock_refs）与 IC（ic_refs，通常是 MCU）之间的网，赋高权。
    clock_refs = {ref for ref, dev in ir.devices.items() if _is_clock_device(dev)}
    ic_refs = {c.ref for c in components if c.category == "ic"}

    nets: list[Net] = []
    for net in ir.nets:
        if net.kind in skip_nets_kind:
            continue
        # 板级权威成员关系：pad_nets 是物理真值，原理图注解可能已漂移。同名的网
        # 用板上 refs 替代原理图 refs（C13/C14 才是晶体负载电容、C17/C18 已改挂
        # +24V_IN/BOOT5V），使锚定/网长/顺序与真实板一致。
        if board_nets and net.name in board_nets:
            refs = [r for r in sorted(board_nets[net.name]) if r in refset]
        else:
            refs = []
            for p in net.pins:
                if p.ref in refset and p.ref not in refs:
                    refs.append(p.ref)
        if len(refs) < 2:
            continue
        rs = set(refs)
        # 电源网永不赋时钟权：4 脚晶振（GND 屏蔽脚，watchy Y2 2520-4Pin）会把 GND
        # 网拖进 clock_refs 集合 → GND 被 net_class 判 "clock" → 全板 IC 误判为
        # 信号 IC、电源簇成员清零（2026-10-12 watchy 泛化验证实测）。时钟权只给
        # 信号网（晶体↔IC 的 XTAL 网 kind 本就不是 power）。
        weight = (CLK_WEIGHT if (clock_refs & rs) and (ic_refs & rs)
                  and net.kind != "power" else 1.0)
        nets.append(Net(refs, name=net.name, weight=weight, kind=net.kind))

    # 板上独有、原理图 IR 里没有的网（board-only，如 U4↔U8 的 CAN1_RXD_ISO）：原理图
    # 网表不含板级器件（U8/U9 收发器是 board_only_components 加入的），但 pad_nets 是
    # 物理真值。若只遍历 ir.nets，这些网被丢弃 → 隔离器 U4/U5 的 ic_pin_anchor 只看到
    # MCU 侧引脚、被拉贴 MCU 左缘、压住 JTAG_TDI/BOOT1/VDD_SOC 造成布不通。board-only
    # 网按同规则补入（kind 空，2-pin 网仍按 net_class 归 "point"、照常锚定）。
    ir_net_names = {net.name for net in ir.nets}
    if board_nets:
        for name in sorted(board_nets):
            if name in ir_net_names:
                continue
            refs = [r for r in sorted(board_nets[name]) if r in refset]
            if len(refs) < 2:
                continue
            rs = set(refs)
            # 同上：board-only 电源轨（GND/…）含晶振屏蔽脚时同样不赋时钟权。
            weight = (CLK_WEIGHT if (clock_refs & rs) and (ic_refs & rs)
                      and not _is_power_name(name) else 1.0)
            # board-only 电源轨（+5V_ISO1/GND1/…）补标 kind="power"，否则 net_class
            # 归 "other"、去耦电容失锚。2-pin 信号网（CAN1_*_ISO）仍留空 → "point"。
            kind = "power" if _is_power_name(name) else ""
            nets.append(Net(refs, name=name, weight=weight, kind=kind))

    by_ref = {c.ref: c for c in components}
    fixed_center: dict[str, tuple[float, float, float]] = {}
    for r, t in (fixed or {}).items():
        if r not in refset:
            continue
        ox, oy, rot = (t[0], t[1], t[2]) if len(t) >= 3 else (t[0], t[1], 0.0)
        cx, cy = center_of(by_ref[r], ox, oy, rot)
        fixed_center[r] = (cx, cy, rot)
    # extra_fixed 已是中心化位姿（中心 x, 中心 y, rot），直接并入。
    for r, t in (extra_fixed or {}).items():
        if r in refset:
            fixed_center[r] = (t[0], t[1], t[2])
    return PlacementProblem(components=components, nets=nets, board=board,
                            fixed=fixed_center, net_pin_loc=net_pin_loc or {},
                            clock_refs=clock_refs, blocks=blocks)
