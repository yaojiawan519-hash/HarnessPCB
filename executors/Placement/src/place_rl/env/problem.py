"""放置问题的统一表示 + 几何基元。

合成板（data/synthetic）与真实板（data/real 从 IR 转换）都产出 PlacementProblem；
下游 SA 教师 / RL env 只消费它，与 KiCad / kca 解耦。

几何约定：元件抽象为「外框 AABB + 中心原点」矩形（DesignDocument「抽象为矩形」），
朝向只取 0/90/180/270，故旋转后 AABB 无需三角函数——90/270 时宽高对调即可（aabb()）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

Box = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)


@dataclass
class Component:
    """一个可放置器件。w/h = 外框尺寸（mm），pins = 引脚数（GNN 节点特征用）。

    ``cx/cy`` 是外框中心在 footprint 本地系的偏移（相对 footprint 原点）。
    KiCad footprint 原点往往不在体心（如 RJ45 原点在角上），中心化的几何模型
    需要这个偏移把「footprint 原点」与「外框中心」互转（见 center_of/origin_of）。
    合成板元件体心即原点，故恒为 0。
    """

    ref: str
    w: float
    h: float
    pins: int = 0
    category: str = "passive"  # passive | ic | connector | power
    cx: float = 0.0            # 外框中心 x 偏移（footprint 本地系）
    cy: float = 0.0            # 外框中心 y 偏移（footprint 本地系）
    value: str = ""            # 器件值（如 8MHz / 25MHz / 100nF），时钟检测用
    block: str = ""            # 功能块名（原理图子图 Sheetname 小写）；空 = 无归属/合成板。
                               # 分图语义化的块内聚依据（见 PlacementProblem.blocks）。
    fp_name: str = ""          # 封装 lib_id（如 "LED_SMD:LED_0603_1608Metric"）；空 = 合成板。
                               # 器件类识别用（LED 贴边锚靠它——value 常是 "RUN" 这类
                               # 不含类名的字符串，LED 标记只在封装名里）。


@dataclass
class Net:
    """一条网：成员 ref + 名称 + 线长权重 + 网分类 kind。

    ``weight > 1`` 表示该网更关键（如时钟/去耦网），HPWL 与 SA/RL 目标按权加权，
    使策略优先缩短关键网。合成板默认 weight=1；真实板由时钟检测赋权。

    ``kind`` 复用 kca ingest 的网分类（"power"|"system"|"intra"，见 data/real），
    统一锚定框架据此区分「多成员电源网」（去耦分布，下一步锚最近 IC 电源引脚）
    与「点对点/时钟网」。合成板默认空、无意义。
    """

    refs: list[str]
    name: str = ""
    weight: float = 1.0
    kind: str = ""           # "power"|"system"|"intra"，来自 kicad_access.ingest 的
                             # _is_power_name（GND/VCC/VDD/+/VIN…）。锚定框架的电源网依据。


@dataclass
class PlacementProblem:
    """一次放置任务：器件 + 网表（超图）+ 画布 + 可选锁定件。

    ``fixed`` 值为 3 元组 ``(中心x, 中心y, rot)``——中心化本地系（与 pos 同约定），
    朝向保留真实板锁定件的原始角度（而非强迫 0°）。
    """

    components: list[Component]
    nets: list[Net]                    # 每条 net = Net(refs, name, weight)
    board: tuple[float, float]         # (W, H) mm，画布 (0,0)-(W,H)
    fixed: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # ref → (中心x, 中心y, rot)
    # 引脚坐标：net_name → {ref → (footprint 本地 x, 本地 y)}。时钟网（weight>1）据此
    # 锚定到邻居 IC 的实际引脚而非体心——晶体须贴 MCU 振荡引脚（如 69/70），不是贴
    # 封装任意一侧。合成板为空；真实板由 place_real_board 从板 footprint 的 pad_nets 构建。
    net_pin_loc: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    # 时钟器件（晶体/振荡器，如 Y1）ref 集：解码时 pin 锚定、合法化时保持不动——
    # 它的落点由 MCU 引脚唯一决定，力导向松弛会把它与负载电容（同为时钟网成员、
    # 体心彼此吸引）挤成一团振荡、最终把晶体弹离引脚，故此处显式锁定。
    clock_refs: set[str] = field(default_factory=set)
    # 功能块分解：块名 → 该块 ref 列表（分图语义化的数据基础）。来自 DeviceIR.module
    # （原理图子图 Sheetname 小写），板上独有件由 data/real 用「块专属网」投票推断；
    # 合成板无块（空 dict），块内聚自动 no-op。空块名不入表。
    blocks: dict[str, list[str]] = field(default_factory=dict)
    # 原则②（LED 就近靠边）总开关：False 时 led_edge_refs 恒空、锚定框架与现行
    # 引擎逐字节一致（默认关 = A/B 变量隔离前提）；True 时 LED + 同网串联限流
    # 电阻登记进统一锚定集、锚到「驱动 IC 引脚 → 最近板边」投影的贴边带。
    led_edge: bool = False
    # 原则①（接口贴边）总开关：未锁定连接器锚到「功能锚 → 最近板边」投影的
    # 贴边带（带宽按体量自适应）。False（默认）或 J* 全被锁定时成员表恒空、
    # 锚定框架零变化——兼容合同不动；独立放置（--no-fix-connectors）时才生效。
    iface_edge: bool = False

    def __post_init__(self) -> None:
        self.by_ref: dict[str, Component] = {c.ref: c for c in self.components}
        self.refs: list[str] = [c.ref for c in self.components]

    @property
    def n(self) -> int:
        return len(self.components)


def aabb(comp: Component, x: float, y: float, rot: float) -> Box:
    """中心 (x,y)、朝向 rot 的外框 AABB（rot ∈ {0,90,180,270}）。"""
    w, h = (comp.w, comp.h) if rot % 180 == 0 else (comp.h, comp.w)
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _rot_off(dx: float, dy: float, rot: float) -> tuple[float, float]:
    """footprint 本地偏移 (dx,dy) 旋转 rot 后的绝对偏移（y-down 屏幕逆时针，
    约定与 kicad_access.kicad.footprint 一致）。"""
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    return dx * c + dy * s, -dx * s + dy * c


def center_of(comp: Component, ox: float, oy: float, rot: float) -> tuple[float, float]:
    """footprint 原点 (ox,oy) + 朝向 rot → 外框中心绝对坐标。"""
    dx, dy = _rot_off(comp.cx, comp.cy, rot)
    return ox + dx, oy + dy


def origin_of(comp: Component, cx: float, cy: float, rot: float) -> tuple[float, float]:
    """外框中心 (cx,cy) + 朝向 rot → footprint 原点绝对坐标（center_of 的逆）。"""
    dx, dy = _rot_off(comp.cx, comp.cy, rot)
    return cx - dx, cy - dy


def pin_abs(problem: PlacementProblem, ref: str, net_name: str,
            pos: dict[str, tuple[float, float, float]]) -> tuple[float, float] | None:
    """ref 在 net_name 上的引脚绝对坐标（中心化本地系，与 pos 同约定）；无则 None。

    时钟网（weight>1）应锚定到邻居 IC 的实际引脚而非体心——晶体须贴 MCU 振荡引脚
    （如 U1 的 69/70），不是贴封装任意一侧。footprint 本地系 → 体心相对偏移
    ``_rot_off(pin - (cx,cy), rot)``，再叠加体心坐标；与 center_of/origin_of 同约定。
    合成板无 net_pin_loc，恒返回 None。
    """
    loc = (problem.net_pin_loc or {}).get(net_name, {}).get(ref)
    if loc is None or ref not in pos:
        return None
    c = problem.by_ref.get(ref)
    if c is None:
        return None
    x, y, rot = pos[ref]
    dx, dy = _rot_off(loc[0] - c.cx, loc[1] - c.cy, rot)
    return (x + dx, y + dy)


def net_class(net: Net) -> str:
    """统一网分类：clock | point | power | other。锚定框架的单一规则入口。

    判定顺序敏感——**point 先于 power**：2-pin 专用去耦网（如 VDD_PHY0CAP）kind 也标
    power，但当前它按「点对点 2-pin」锚到 IC pad；若先判 power 会把它重分类、静默取消
    锚定（行为退化）。故 point 覆盖所有 2-pin 网，"power" 只覆盖多成员电源网（正是下一步
    要新增「锚最近 IC 电源引脚」的去耦分布类）。
    """
    if net.weight > 1.0:
        return "clock"      # 晶体↔IC 时钟网（problem_from_ir 已赋 CLK_WEIGHT）
    if len(net.refs) == 2:
        return "point"      # 点对点网（含 2-pin 专用去耦网 IC↔cap）
    if net.kind == "power":
        return "power"      # 多成员电源网（plane/去耦分布）
    return "other"          # 多成员信号总线


def pin_anchor_refs(problem: PlacementProblem) -> set[str]:
    """应「锚定 IC 引脚」的器件：时钟器件 + 点对点被动件 + 时钟网负载电容/电阻。

    统一规则（见 net_class）：
    - clock 网（weight>1）上的被动件（负载电容/阻尼电阻）→ 贴振荡引脚；
    - point 网（2-pin、另一头 IC，如 R9↔U1 pad17、L1↔U1 pad30）的**单侧**被动件
      → 贴 IC 具体 pad，否则体心锚点 + 密度扩散散到整板对侧、2-pin 网布不通；
    - point 网（2-pin、两头都是 IC）→ 锚 pin 少者（外设 IC 贴主控 IC pad，如
      U4 隔离器→U1 MCU 的 CAN2_RXD/TXD/STBY），否则两 IC 各漂各的、信号链布太长；
    - **串联桥被动件不锚定**：一个被动件同时落在 ≥2 条 point 网（如 R5↔U1 LED pad +
      R5↔J2 连接器的 ETH0_LED0/_A），锚 IC 会把固定连接器侧拉长、并与兄弟电阻挤成
      IC 左缘一列造成布线拥堵（4 条 LED 网布不通）。放任其随 HPWL 漂向两侧中点
      （golden 放置即如此，0 未布通）。==1 才是「单侧信号」被动件（RBIAS R→IC + GND）。
    - power（多成员电源网）/other → 不锚定（via 到平面 / 总线）。
    「placement 要锚定 Pin」从时钟器件推广到普通点对点网、再到时钟网负载电容，
    再到 IC↔IC 点对点信号链——统一的「小件贴大件引脚」规则。
    """
    refs = set(getattr(problem, "clock_refs", ()))
    # 每器件落在几条 point 网：>=2 是「串联桥」（连接器↔限流电阻↔IC / IC↔限流电阻↔LED），
    # 不锚；==1 是单侧信号被动件，锚 IC。计数只取 point 网（时钟网负载电容走 clock 分支）。
    point_count: dict[str, int] = {}
    for net in problem.nets:
        if net_class(net) != "point":
            continue
        for r in net.refs:
            point_count[r] = point_count.get(r, 0) + 1

    for net in problem.nets:
        cls = net_class(net)
        if cls == "clock":
            for r in net.refs:
                if problem.by_ref[r].category == "passive":
                    refs.add(r)
        elif cls == "point":
            a, b = net.refs
            ca, cb = problem.by_ref[a].category, problem.by_ref[b].category
            if ca == "ic" and cb == "ic":
                # IC↔IC 点对点信号链：锚 pin 少者（外设 → 主控）。pin 数 0 的是板级
                # 独有固定件（board_only_components 未计数），跳过避免误锚。
                pa, pb = problem.by_ref[a].pins, problem.by_ref[b].pins
                if 0 < pa < pb:
                    refs.add(a)
                elif 0 < pb < pa:
                    refs.add(b)
            elif ca == "passive" and cb == "ic":
                if point_count.get(a, 0) == 1:
                    refs.add(a)
            elif cb == "passive" and ca == "ic":
                if point_count.get(b, 0) == 1:
                    refs.add(b)
    return refs


def ic_pin_anchor(problem: PlacementProblem, pos: dict[str, tuple[float, float, float]],
                  ref: str) -> tuple[float, float] | None:
    """ref 的 IC 引脚锚点（绝对坐标均值）；无 IC 引脚邻居则 None。

    统一规则（见 net_class）：
    - clock 网上的 IC 引脚（晶体→U1 的 69/70）；
    - point 网（2-pin）上唯一 IC 邻居的引脚（R9→U1 pad17）。
    power（多成员电源网）/other 里即便有 IC 也不锚——via 到平面、位置无关。
    锚点挂在引脚上而非体心/冻结坐标，MCU 自身在合法化里移动时用**当前**位姿重算、随动跟随。
    """
    pts: list[tuple[float, float]] = []
    for net in problem.nets:
        if ref not in net.refs:
            continue
        if net_class(net) not in ("clock", "point"):
            continue  # 平面网（power/other）跳过，只对点对点/时钟锚引脚
        for r in net.refs:
            if r == ref or r not in pos:
                continue
            if problem.by_ref[r].category != "ic":
                continue
            p = pin_abs(problem, r, net.name, pos)
            if p is not None:
                pts.append(p)
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


# —— 电源区（power island）规则：电源变换 IC 锚到电源输入端子 ——
# 信号 IC（MCU/收发器）之间有点对点信号链（U1↔U4/U5 的 CAN2_RXD/TXD），电源 IC
# （buck/LDO）只经电源网供电、不与任何 IC 有信号链。据此把「电源区」（U2 buck / U3
# LDO 及其输出电容）整体拉到电源输入端子（J1='24V_IN'）旁，而非挤在 MCU 边。
_POWER_IN_TOKENS = ("VBUS", "VIN", "VCC", "VDD", "VSS", "VEE", "BAT", "BATT",
                    "PWR", "POWER", "SUPPLY")


def _is_power_input(value: str) -> bool:
    """连接器值是否为「电源输入端子」（如 24V_IN / VBUS / VIN）。

    两个信号：显式电源标记（VBUS/VIN/VCC/VDD/BAT/PWR/POWER/SUPPLY），或
    「带电压数字 + IN」的电压输入（24V_IN → ["24V","IN"]）。信号连接器
    （JTAG/UART0/CAN_CAN1/MDIO 这类协议信号网）都不含这些，判定为 False。
    """
    v = (value or "").upper()
    toks = [t for t in re.split(r"[^A-Z0-9]+", v) if t]
    if any(t in _POWER_IN_TOKENS for t in toks):
        return True
    return "IN" in toks and any(ch.isdigit() for ch in v)


def power_connector_refs(problem: PlacementProblem) -> list[str]:
    """固定连接器中「电源输入端子」的 ref（如 J1='24V_IN'）：电源区的锚点。"""
    return [r for r, c in problem.by_ref.items()
            if r in problem.fixed and c.category == "connector" and _is_power_input(c.value)]


def power_connector_pos(problem: PlacementProblem) -> tuple[float, float] | None:
    """电源输入端子中心（多个取均值）；无则 None。"""
    rs = power_connector_refs(problem)
    if not rs:
        return None
    xs = [problem.fixed[r][0] for r in rs]
    ys = [problem.fixed[r][1] for r in rs]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def power_ic_refs(problem: PlacementProblem) -> set[str]:
    """电源变换 IC（buck/LDO）：可动 IC 且无 clock/point 网连到其它 IC。

    区分「电源区」与「信号区」的唯一依据：信号 IC 之间有点对点信号链（含 IC↔IC
    point 网），电源 IC 没有。buck→LDO 的中间轨（如 BOOT5V）是多成员电源网
    （kind=="power"），net_class 归 "power" 而非 "point"，故 U2/U3 不被误判为
    信号 IC。无需板级硬编码。
    """
    movable_ics = {r for r, c in problem.by_ref.items()
                   if c.category == "ic" and r not in problem.fixed}
    signal_ics: set[str] = set()
    for net in problem.nets:
        if net_class(net) not in ("clock", "point"):
            continue
        ics = [r for r in net.refs if problem.by_ref[r].category == "ic"]
        if len(ics) >= 2:
            signal_ics.update(ics)
    return movable_ics - signal_ics


def _is_gnd_name(name: str) -> bool:
    """网名是否为地（GND/GND1/VSS/…）：去耦电容的返回端，不作为「电源轨」锚定依据。

    电源网分「电源轨」（+5V/+5V_ISO1/VDD_*，电容由此找 IC 电源引脚）与「地」（GND/GND1，
    返回端）。锚定只按电源轨找最近的 IC——若把 GND 也算进去，GND 遍布全板、最近的
    IC 可能是任意一颗，去耦电容会被拽离它真正供电的 IC（GND 上最近的 IC 往往是
    板对侧某颗），反而把它拽空。
    """
    n = name.upper()
    return "GND" in n or n.startswith("VSS")


def power_decoup_refs(problem: PlacementProblem) -> set[str]:
    """电源去耦电容：可动被动件、只挂 power 网、且某根非 GND 电源轨上有 IC。

    统一锚定框架的「电源去耦分布」类（plan 里 deferred 的第 3 类）。多成员电源轨
    （+5V/GND、+5V_ISO1/GND1…）上的去耦电容不参与块内聚——否则被块质心拽离它真正
    去耦的固定 DC-DC（如 U6，block=''、无归属），把 U6 的地脚（GND pad 2）孤立在
    块质心之外 → 布不通。改为锚到「该电源轨上最近的 IC 电源引脚」。

    与 pin_anchor_refs 无交集：后者覆盖 clock/point 网（weight>1 或 len==2），本类只
    覆盖多成员 power 网（len>2 且 kind=="power"），net_class 互斥、不重复计数。
    """
    power_nets: dict[str, list[Net]] = {}
    for net in problem.nets:
        if net_class(net) != "power":
            continue
        for r in net.refs:
            power_nets.setdefault(r, []).append(net)

    out: set[str] = set()
    for r, nets in power_nets.items():
        c = problem.by_ref.get(r)
        if c is None or c.category != "passive" or r in problem.fixed:
            continue
        if not c.ref.startswith("C"):
            continue  # 只锚电容：电源路径件（D 二极管 / L 电感 / R 反馈电阻）须留在块内
                      # 或电源端子旁，不该被「最近 IC 电源引脚」拽离电源区（如 D1/L2/R2）。
        for net in nets:
            if _is_gnd_name(net.name):
                continue  # GND 是返回端，不作为电源轨找 IC
            if any(m != r and problem.by_ref[m].category == "ic" for m in net.refs):
                out.add(r)
                break
    return out


def power_decoup_anchor(problem: PlacementProblem,
                        pos: dict[str, tuple[float, float, float]],
                        ref: str) -> tuple[float, float] | None:
    """去耦电容 ref 的锚点：它所在非 GND 电源轨上「最近 IC」的电源引脚（无则 None）。

    固定 IC（如 U6/U7 隔离 DC-DC）与可动 IC 同权、纯按欧氏距离取最近——电源区电容
    （C19–C22）近 U2/U3（可动、同区）就锚 U2/U3，不被固定 IC 拽跑；CAN 区电容
    （C33/C34）近 U6/U7（固定、同区）就锚 U6/U7。锚点用 IC 引脚（pin_abs）而非体心，
    随可动 IC 位姿重算、随动跟随；用电容当前位姿求最近，迭代中电容被拽向某 IC 后
    最近者稳定不再翻转。

    电源簇模式（problem.power_cluster 非空）：最近者是**电源变换 IC** 时，锚点改投
    「端子下方簇带」的矩形投影——电容归属判据不变（仍是最近电源轨 IC），只是电源
    IC 的随行电容目的地从 IC 引脚换成簇带；最近者是信号 IC 的电容不受影响（留在
    自己的 IC 边）。电容已在带内 → 投影 None → 回退 IC 引脚锚（不悬空）。
    """
    x0, y0 = pos[ref][0], pos[ref][1]
    best: tuple[float, float] | None = None
    best_ic: str | None = None
    best_d2: float | None = None
    for net in problem.nets:
        if net_class(net) != "power" or ref not in net.refs:
            continue
        if _is_gnd_name(net.name):
            continue
        for m in net.refs:
            if m == ref or m not in pos:
                continue
            if problem.by_ref[m].category != "ic":
                continue
            p = pin_abs(problem, m, net.name, pos)
            if p is None:
                p = (pos[m][0], pos[m][1])
            d2 = (p[0] - x0) ** 2 + (p[1] - y0) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best = p
                best_ic = m
    pc = getattr(problem, "power_cluster", None)
    if pc is not None and best_ic in pc.ic_refs:
        return power_cluster_anchor(pc, (x0, y0)) or best
    return best


def dedicated_decoup_refs(problem: PlacementProblem) -> set[str]:
    """专属轨去耦电容：power_decoup_refs 中，全部非 GND 电源轨上的 IC 唯一
    （并集恰为一颗 IC）的电容——归属零歧义，电容就是这颗 IC 的。

    ecat 实测（r3_engine 探针）：默认 lam_decoup=0.15 下 U1 专属轨电容
    （VDD_SOC/VDD_PMCCAP/VDD_OTPCAP）停在离 U1 20–28mm 的东侧平衡点；全局抬
    系数到 0.5/1.0 能拉回 12–17mm，但共享轨（+3V3 类多 IC 轨）电容 C8/C10/C33
    锚点翻转被拽远 8–12mm。本查询函数划出可安全施强引力的子集，供
    ``lam_decoup_dedicated`` 单独施力；共享轨电容仍走 lam_decoup 弱引力。
    纯查询函数，不改动任何既有判定。
    """
    out: set[str] = set()
    for r in power_decoup_refs(problem):
        owners: set[str] | None = None
        dedicated = True
        for net in problem.nets:
            if net_class(net) != "power" or r not in net.refs:
                continue
            if _is_gnd_name(net.name):
                continue
            ics = {m for m in net.refs if m != r
                   and problem.by_ref[m].category == "ic"}
            if not ics:
                continue  # 无 IC 的轨不构成归属证据
            owners = ics if owners is None else owners | ics
            if len(owners) > 1:
                dedicated = False
                break
        if dedicated and owners:
            out.add(r)
    return out


# —— 电源簇（power cluster）：电源元器件聚集到电源输入端子的「下方」带 ——
# 意图语义（放置意图说明「电源元器件放置在电源输入端子的下方」）的通用机制：
# 锚 = 电源输入端子（意图件，边沿已知），带 = 端子外框沿**内法向**挤出 depth 的
# 矩形（top 边端子 → 板内 +y，即屏幕「下方」；bottom/left/right 边端子同理取
# 板内侧——「下方」的几何本质是「朝板内的一侧」，换边自动成立）。成员三层：
#   1. 变换 IC（power_ic_refs）：强锚通道（lam_pin + tier-2 贴锚格），原锚点
#      「端子中心」升级为「带投影」；
#   2. 非 C 电源件（防反二极管/电感/磁珠等，见 PowerCluster.extra_refs）：
#      纯电源网且与电源 IC 共非地轨——同样走强锚通道；混信号件（反馈/分压电阻
#      挂 point 网）自动排除，仍走 IC 引脚锚随行自己的 IC；
#   3. 去耦电容：不进 members，走 power_decoup_anchor 的弱锚通道（归属=最近
#      电源轨 IC，见上）——信号 IC 边的去耦电容不被拖离（+3V3 轨遍布全板，
#      全收会把 MCU 去耦集体拽过板）。
# 带内成员锚点返回 None（到站即停，同 iface_edge 哲学：带内只受重叠推挤/网力，
# 不再外拉）。problem.power_cluster 为 None（默认）时全部分支 no-op。


@dataclass
class PowerCluster:
    """电源簇配置（place.py 从意图 spec 算好注入，problem 层只做几何/成员查询）。"""
    region: tuple[float, float, float, float]  # 带矩形（本地系 x0, y0, x1, y1）
    ic_refs: frozenset[str]   # 电源变换 IC（电容归属判据 + 强锚成员）
    extra_refs: frozenset[str]  # 非 C 电源件（纯电源网共轨；强锚成员）
    anchor: str = ""           # 锚端子 ref（报告用）

    @property
    def members(self) -> set[str]:
        return set(self.ic_refs) | set(self.extra_refs)


def power_cluster_anchor(pc: PowerCluster,
                         p: tuple[float, float]) -> tuple[float, float] | None:
    """点 → 簇带矩形最近点；点在带内返回 None（到站即停）。"""
    x, y = p
    rx0, ry0, rx1, ry1 = pc.region
    qx = min(max(x, rx0), rx1)
    qy = min(max(y, ry0), ry1)
    if abs(qx - x) < 1e-9 and abs(qy - y) < 1e-9:
        return None
    return (qx, qy)


def power_cluster_passive_refs(problem: PlacementProblem) -> set[str]:
    """电源簇的非 C 强锚成员：可动被动件、非 C 前缀、只挂 power 网、且在某根
    非 GND 电源轨上与电源变换 IC 共轨（防反二极管/电感/磁珠类电源路径件）。

    纯度判据（所有挂网都是 power 类）自动排除混信号件——反馈/分压电阻挂 point
    网（FB 抽头），留在 IC 引脚锚随行自己的 IC；C* 去耦电容走
    power_decoup_anchor 弱锚通道不进簇（+3V3 轨遍布全板，全收会把 MCU 去耦
    集体拽过板）。通道前沿件的排除由调用方做（channel 优先级本就高于簇）。
    """
    nets_of: dict[str, list[Net]] = {}
    for net in problem.nets:
        for r in net.refs:
            nets_of.setdefault(r, []).append(net)
    ic_refs = power_ic_refs(problem)
    ic_rails: set[str] = set()  # 电源变换 IC 挂的非 GND 电源轨
    for net in problem.nets:
        if net_class(net) != "power" or _is_gnd_name(net.name):
            continue
        if any(m in ic_refs for m in net.refs):
            ic_rails.add(net.name)
    out: set[str] = set()
    for r, nets in nets_of.items():
        if r in problem.fixed:
            continue
        c = problem.by_ref.get(r)
        if c is None or c.category != "passive" or r.startswith("C"):
            continue
        if not nets or any(net_class(n) != "power" for n in nets):
            continue  # 混信号件不进簇
        if any(n.name in ic_rails for n in nets):
            out.add(r)
    return out


# —— 通道前沿（channel front-end）锚定：端子的指示/调理链贴端子内侧 ——
# 固定连接器（接线端子/插座）的「通道前沿」= 信号进板后的无源调理链：指示 LED 及其
# 限流电阻、端接/偏置/ESD、低边驱动管+续流。这些件的归属由端子决定（人眼对位、
# 检修、通道隔离都在端子侧），不该由 HPWL 中点决定。两类信号**并集**识别（只收可动
# 被动件）：
#   1. 拓扑：从端子经「细信号网」BFS ≤4 跳——非电源类且成员数 ≤7（FG 场地/总线等
#      公共轨成员多，天然排除）；不穿越 IC/连接器/锁定件（隔离光耦即 BFS 边界，
#      MCU 侧滤波件不会被拽过隔离带）；
#   2. 命名：网名以 `<端子value>_` 为前缀（DO_1_OUT / DO_1_LED_A…）——当指示链挂在
#      MCU 侧、被隔离光耦挡住时，拓扑够不着，设计者的通道命名是唯一线索。
# 再两道闸门（成员资格按顺序收紧）：
#   3. **核心规模 ≥3 才是「链」**：BFS 核（拓扑可达的被动件）至少 3 块才算调理前沿
#      ——1 块是孤立件：串联端接（J—R—IC，R5-R8 这类只挂一个 {J,R} 网）、
#      上拉（J—R—电源轨，R11）、CAN 端接（J—R—IC×2，R12/R13）；2 块是头旁 RC 对
#      （R14+C37 同网即止）。这些归属在两侧中点，锚到端子会拉长另一侧（同
#      pin_anchor_refs 的串联桥教训）。规模判据对观察者对称：差分总线的调理网络
#      （RS485 的 D4/R4/R5/R6）从连接器 J3 与轨上跳线 JP1 双双达标 → 走歧义判定
#      弃锚漂浮，不会出现「真连接器被深度闸掉、跳线认领」的不对称。命名匹配成员
#      （DO 指示链在 MCU 侧、被隔离光耦挡住）只搭在达标的 BFS 核上：核不达标则
#      整个端子弃锚。
#   4. **薄桥剔除**：入口网恰为 {J,P} 且其余细网全是 IC 的成员单独弃锚（核达标时仍
#      可能存在的局部桥）。≥2 端子可达的歧义件不锚，维持漂浮。
_CHANNEL_FANOUT_MAX = 7        # 网成员数超过视为公共轨/总线，不作通道边
_CHANNEL_BFS_HOPS = 4          # 从端子出发最多穿过的网数
_CHANNEL_MIN_CORE = 3          # BFS 核被动件少于此数不构成调理链（孤立件/RC 对漂浮）
_CHANNEL_ANCHOR_OFFSET = 2.2   # 锚点越过端子外框向板内推进的距离（mm）
_CHANNEL_EDGE_FLUSH = 3.0      # 外框距板边小于此值（mm）视为贴边安装（内向判定）


def _channel_edge_ok(net: Net) -> bool:
    """通道 BFS/命名的边判定：细信号网（非多成员电源类、非公共轨）。"""
    return net.kind != "power" and len(net.refs) <= _CHANNEL_FANOUT_MAX


def channel_members(problem: PlacementProblem) -> dict[str, str]:
    """通道前沿成员 → 其归属固定连接器（member ref → connector ref）。

    anchor_refs/anchor_point 的「通道前沿」类登记数据；结果缓存在 problem 上
    （anchor_point 在合法化内层循环逐件调用，不能每次重算图）。
    """
    cached = getattr(problem, "_channel_members_cache", None)
    if cached is not None:
        return cached
    problem._channel_members_cache = cached = _compute_channel_members(problem)
    return cached


def _compute_channel_members(problem: PlacementProblem) -> dict[str, str]:
    by_ref = problem.by_ref
    fixed_conn = [r for r in problem.fixed
                  if r in by_ref and by_ref[r].category == "connector"]
    net_by_name = {n.name: n for n in problem.nets}
    nets_of: dict[str, list[Net]] = {}
    for net in problem.nets:
        for r in net.refs:
            nets_of.setdefault(r, []).append(net)

    owner: dict[str, str] = {}
    contested: set[str] = set()
    for j in fixed_conn:
        def _collect(net: Net, found: set[str]) -> None:
            for m in net.refs:
                if m == j or m in problem.fixed:
                    continue
                if by_ref[m].category == "passive":
                    found.add(m)

        found: set[str] = set()
        # 1) 拓扑 BFS：从端子的细信号网出发，只沿被动件扩展
        seen: set[str] = set()
        queue: list[tuple[str, int]] = []
        for net in nets_of.get(j, []):
            if _channel_edge_ok(net) and net.name not in seen:
                seen.add(net.name)
                queue.append((net.name, 0))
        idx = 0
        while idx < len(queue):
            name, h = queue[idx]
            idx += 1
            net = net_by_name[name]
            _collect(net, found)
            if h >= _CHANNEL_BFS_HOPS:
                continue
            for m in net.refs:
                if m == j or m in problem.fixed:
                    continue
                if by_ref[m].category != "passive":
                    continue  # 不穿越 IC / 连接器 / 锁定件（隔离边界）
                for n2 in nets_of.get(m, []):
                    if _channel_edge_ok(n2) and n2.name not in seen:
                        seen.add(n2.name)
                        queue.append((n2.name, h + 1))
        # 「链」资格：BFS 核至少 3 块被动件；孤立件/RC 对归属中点，整体弃锚
        # （评估板所有连接器邻域均止步于此层，canonical 放置零扰动）
        if len(found) < _CHANNEL_MIN_CORE:
            continue
        # 2) 命名：网名以 <端子value>_ 为前缀
        v = (by_ref[j].value or "").strip().upper()
        if len(v) >= 2:
            pref = v + "_"
            for net in problem.nets:
                if net.name.upper().startswith(pref) and _channel_edge_ok(net):
                    _collect(net, found)

        # 3) 薄桥剔除：与端子同网恰为 {J,P}、其余细网全是 IC → 归属中点，不锚
        for p in list(found):
            shared = [n for n in nets_of.get(p, []) if j in n.refs]
            if not shared:
                continue  # 命名匹配来的成员（不与端子同网），不受此限
            if not all(set(n.refs) == {j, p} for n in shared):
                continue  # 入口网不薄（如 DO_1_OUT 三成员）→ 非桥
            others = [n for n in nets_of.get(p, [])
                      if j not in n.refs and _channel_edge_ok(n)]
            if others and all(
                    all(m == p or by_ref[m].category == "ic" for m in n.refs)
                    for n in others):
                found.discard(p)

        for p in found:
            if p in owner and owner[p] != j:
                contested.add(p)  # ≥2 端子可达 → 歧义，弃锚
            else:
                owner[p] = j
    for p in contested:
        owner.pop(p, None)
    return owner


def channel_anchor_point(problem: PlacementProblem,
                         pos: dict[str, tuple[float, float, float]],
                         ref: str) -> tuple[float, float] | None:
    """通道前沿成员的锚点：端子种子焊盘沿「板内方向」投影出外框 + 偏移。

    「放在端子下方/旁边」的几何语义：取端子与任一通道成员的公共细网上的端子引脚
    （通道的进入点）。板内方向优先按**贴边安装**判定——端子外框哪条边距对应板边
    < _CHANNEL_EDGE_FLUSH，板内即背离该边（顶边端子 → 下方）；角位端子取最贴的边
    （板心主轴在角附近 dx/dy 接近，会误选水平分支）。都不贴边（板中部连接器）退回
    「板中心 − 外框中心」主轴。落点只作吸引子，合法化的 pin_nearest 会吸附到最近
    合法网格槽（外框重叠约束在 legal_for 里），故偏移是先验而非硬约束。
    """
    owner = channel_members(problem)
    j = owner.get(ref)
    if j is None:
        return None
    c = problem.by_ref[j]
    x, y, rot = problem.fixed[j]
    box = aabb(c, x, y, rot)
    W, H = problem.board

    members = {r for r, jj in owner.items() if jj == j}
    seeds = [n for n in problem.nets
             if j in n.refs and _channel_edge_ok(n) and (members & set(n.refs))]
    if not seeds:  # 退化：仅命名匹配且无公共网 → 任取端子细信号网
        seeds = [n for n in problem.nets if j in n.refs and _channel_edge_ok(n)]
    pin = None
    for net in seeds:
        pin = pin_abs(problem, j, net.name, pos)
        if pin is not None:
            break
    ax = pin[0] if pin else (box[0] + box[2]) / 2
    ay = pin[1] if pin else (box[1] + box[3]) / 2
    off = _CHANNEL_ANCHOR_OFFSET
    d_top, d_bottom = box[1], H - box[3]   # 外框到四条板边的距离
    d_left, d_right = box[0], W - box[2]
    best = min(d_top, d_bottom, d_left, d_right)
    if best <= _CHANNEL_EDGE_FLUSH:  # 贴边安装 → 板内方向 = 背离最贴的板边
        if best == d_top:
            return (ax, box[3] + off)
        if best == d_bottom:
            return (ax, box[1] - off)
        if best == d_left:
            return (box[2] + off, ay)
        return (box[0] - off, ay)
    dx = W / 2 - (box[0] + box[2]) / 2     # 板中部：退回板心主轴
    dy = H / 2 - (box[1] + box[3]) / 2
    if abs(dy) >= abs(dx):
        return (ax, box[3] + off) if dy > 0 else (ax, box[1] - off)
    return (box[2] + off, ay) if dx > 0 else (box[0] - off, ay)


# —— LED 贴边带（原则②：LED 就近靠边）——
# LED 是对外可见性器件：用户要求贴板边放（就近 + 靠边），而非默认被动件那样贴
# 驱动 IC 引脚（= 板中央，某评估板 D4 距边 20.9mm 的案例根因）。识别靠封装名
# （value 常是 "RUN" 这类不含类名的串，LED 标记只在 fp_name 里）；同网 2-pin
# 串联限流电阻随行（r1 交互搬位实测：只搬 LED 不搬 R，串联网拉长反布不通）。
_LED_EDGE_BAND = 3.5  # 贴边带中心距板边（mm）；抄通道 _CHANNEL_EDGE_FLUSH=3.0 先例


def _is_led_component(c: Component) -> bool:
    s = ((c.value or "") + " " + (c.fp_name or "")).upper()
    return c.ref.startswith("D") and "LED" in s


def led_edge_refs(problem: PlacementProblem) -> dict[str, str]:
    """原则②成员表：member ref → LED ref（LED 自映射 + 同网串联限流电阻）。

    ``problem.led_edge`` 为 False 时恒空 dict（默认关 = 锚定框架零变化）。
    通道前沿件若同时命中（端子口指示灯），anchor_point 里通道分支优先、
    归属端子，不受影响。结果缓存（与 _channel_members_cache 同款）。
    """
    if not getattr(problem, "led_edge", False):
        return {}
    cached = getattr(problem, "_led_edge_cache", None)
    if cached is not None:
        return cached
    members: dict[str, str] = {}
    led_refs = {c.ref for c in problem.components
                if _is_led_component(c) and c.ref not in problem.fixed}
    for led in sorted(led_refs):
        members[led] = led
        for net in problem.nets:
            if led not in net.refs or len(net.refs) != 2:
                continue
            other = next(r for r in net.refs if r != led)
            oc = problem.by_ref[other]
            if (oc.category == "passive" and other.startswith("R")
                    and other not in problem.fixed):
                members[other] = led  # 串联限流电阻随行
    problem._led_edge_cache = members
    return members


def led_edge_anchor(problem: PlacementProblem,
                    pos: dict[str, tuple[float, float, float]],
                    ref: str) -> tuple[float, float] | None:
    """LED 贴边带锚点：驱动 IC 引脚位置 → 沿最近板边法向压进贴边带。

    驱动引脚优先取串联电阻的点对点网（R17↔U1），LED 直连 IC 时取 LED 自己的；
    都没有（纯被动驱动）退到共享网上最近 IC 的体心。锚点 = 引脚点在「最近板边」
    法向上的投影、坐标压到 _LED_EDGE_BAND（就近分量化：贴边但留在驱动那一侧，
    不横穿整板）；驱动点已在带内则原样返回（LED 贴驱动，不再外拉）。
    """
    W, H = problem.board
    partners = led_edge_refs(problem)
    led = partners.get(ref, ref)
    # 驱动 LED 家族 = LED 自身 + 同网串联电阻。LED 常无直连 IC 网（D4–R17–U1 两跳），
    # 故逐一试家族成员的 ic_pin_anchor（串联电阻 U1 侧的点对点网命中真引脚），
    # 取第一个命中的。
    family = {led} | {m for m, l in partners.items() if l == led}
    p = None
    for cand in sorted(family):
        p = ic_pin_anchor(problem, pos, cand)
        if p is not None:
            break
    if p is None:
        # 退路：与家族共享网的最近 IC 体心（无点对点驱动网的纯被动链）
        linked: set[str] = set()
        for net in problem.nets:
            if family & set(net.refs):
                linked |= {r for r in net.refs
                           if r in pos and problem.by_ref[r].category == "ic"}
        if not linked:
            return None
        lx, ly = pos.get(ref, pos.get(led, (W / 2, H / 2, 0.0)))[:2]
        p = min(((pos[r][0], pos[r][1]) for r in linked),
                key=lambda q: (q[0] - lx) ** 2 + (q[1] - ly) ** 2)
    ax, ay = p
    d_left, d_right = ax, W - ax
    d_top, d_bottom = ay, H - ay
    best = min(d_left, d_right, d_top, d_bottom)
    if best <= _LED_EDGE_BAND:
        return (ax, ay)  # 驱动已在贴边带内：LED 贴驱动即可
    if best == d_left:
        return (_LED_EDGE_BAND, ay)
    if best == d_right:
        return (W - _LED_EDGE_BAND, ay)
    if best == d_top:
        return (ax, _LED_EDGE_BAND)
    return (ax, H - _LED_EDGE_BAND)


# —— 接口贴边带（原则①：接口贴边）——
# 接口件（连接器）是「对外」器件：IO 从板缘进出，应贴板边放。批A 曾把①实装成
# 「支撑 IC 贴边」（--lam-edge-ic）——对象错了：支撑 IC 的正确位置由功能网决定，
# 盲拉切断功能耦合（REPORT §三）。第一版①实装成「功能锚（IC 体心）的带投影」也
# 错了锚的对象：IC 体心锚把连接器往板心 IC 簇拉（锚点本身就是被 IC 占着的位置），
# 且锚随 IC 移动漂移——独立放置实测 J2/J3 大块头（19×22.3）长途穿越拥挤中区被
# 卡死在半路（12–18mm），贴边 6/8 差的两条全是它。终版锚 = 连接器**自身当前位置**
# 的最近板边带投影（见 iface_edge_anchor），不追功能伙伴。
# 只面向未锁定连接器：默认档（连接器锁定）成员表恒空，开了也是 no-op；独立放置
# （--no-fix-connectors）解锁后才有目标人群。带宽按体量自适应（见 anchor 注释）。
_IFACE_EDGE_MARGIN = 1.5  # 接口体近侧面距板边（mm）：贴边但留丝印/装配余量


def iface_edge_refs(problem: PlacementProblem) -> dict[str, str]:
    """原则①成员表：连接器 ref → 自身（仅未锁定的连接器）。

    判据用语义分类 ``category == "connector"``（与 channel_members 同款、
    任意板通用的引擎级约定），不绑 J* 前缀——分类扩充时自动受益。
    ``problem.iface_edge`` 为 False 时恒空 dict（默认关 = 锚定框架零变化）。
    结果缓存（与 _led_edge_cache 同款）。
    """
    if not getattr(problem, "iface_edge", False):
        return {}
    cached = getattr(problem, "_iface_edge_cache", None)
    if cached is not None:
        return cached
    members = {c.ref: c.ref for c in problem.components
               if c.category == "connector" and c.ref not in problem.fixed}
    problem._iface_edge_cache = members
    return members


def iface_edge_anchor(problem: PlacementProblem,
                      pos: dict[str, tuple[float, float, float]],
                      ref: str) -> tuple[float, float] | None:
    """接口贴边锚点：连接器**自身当前位置** → 最近板边的贴边带投影。

    投影保持切向坐标、只压法向（如最近边是下边 → (cx, band)）：拉力短程、稳定、
    收敛——不追功能 IC（IC 体心锚随 IC 移动漂移，且把连接器拉向板心 IC 簇：
    锚点本身就是被 IC 占着的位置，大块头长途穿越拥挤中区会被卡死在半路）。
    沿边位置（切向）由解码初值 + 重叠推挤自然演化，不强加功能偏好。

    已在带内 → 返回 None（到站即停：不再受锚力/网力，只受重叠推挤；被推出带
    锚会重新出现、拉回带内）。不要求 IC 线索——①是几何原则（接口属于板边），
    纯转接（J↔J）同样贴边。带宽按体量自适应：min(w,h)/2 + margin——锚到带线
    时**近侧面**恰距边 margin；长边垂直朝边、体框仍出界的，由越界斥力/合法化
    bounds 检查兜底推回（锚只拉不推、不管朝向）。
    """
    W, H = problem.board
    p = pos.get(ref)
    if p is None:
        return None
    cx, cy = p[0], p[1]
    comp = problem.by_ref[ref]
    band = min(comp.w, comp.h) / 2 + _IFACE_EDGE_MARGIN
    d_left, d_right = cx, W - cx
    d_top, d_bottom = cy, H - cy
    best = min(d_left, d_right, d_top, d_bottom)
    if best <= band:
        return None  # 已在贴边带内：不外拉、不再锚（交回重叠推挤/普通合法化）
    if best == d_left:
        return (band, cy)
    if best == d_right:
        return (W - band, cy)
    if best == d_top:
        return (cx, band)
    return (cx, H - band)


def anchor_refs(problem: PlacementProblem) -> set[str]:
    """统一锚定集：引脚锚定件（时钟/点对点/IC↔IC）+ 电源 IC（锚到电源端子）
    + 通道前沿件（端子指示/调理链，锚端子内侧投影点）+ LED 贴边家族（原则②）
    + 接口贴边件（原则①，未锁定连接器，锚自身当前位置的贴边带投影）。

    legalize/bc 的单一规则入口——要加新的一类「功能锚定」只需在此并集里登记，
    不再写新的 if 分支。

    注：电源去耦电容的「最近 IC 电源引脚」锚定（power_decoup_refs）已实现但**暂不登记**
    ——一次把 33 颗去耦电容全锚到 IC 电源引脚（含 17 颗 MCU 电容，lam_pin=0.5 且跳过
    块内聚）会过度约束 MCU 区，把 U1 从固定以太网连接器 J2/J3 旁拽到板右侧（x≈55），
    使 ETH0_LED0_A/ETH1_TD_N/ETH1_RD_P 三条点对点网布不通（0→3 未连接）。这些电容在
    「mcu/can」等功能块内靠块内聚已贴紧所属 IC；真正需要单独处理的是 board-only
    固定 DC-DC（U6/U7）的无归属电容，需更外科的规则（见下一步）。

    电源簇模式：problem.power_cluster 非空时并入簇成员（变换 IC + 非 C 电源件），
    其锚点（anchor_point 电源簇分支）= 簇带矩形投影；去耦电容仍不登记（弱锚通道
    随 power_decoup_anchor 切换目的地，见其 docstring）。None 时零变化。
    """
    out = (pin_anchor_refs(problem) | power_ic_refs(problem)
           | set(channel_members(problem)) | set(led_edge_refs(problem))
           | set(iface_edge_refs(problem)))
    pc = getattr(problem, "power_cluster", None)
    if pc is not None:
        out |= pc.members
    return out


def anchor_point(problem: PlacementProblem, pos: dict[str, tuple[float, float, float]],
                 ref: str) -> tuple[float, float] | None:
    """ref 的统一锚点（分支顺序 = 优先级）：通道前沿 → LED 贴边 → 接口贴边 → 电源簇 → 电源 IC → IC 引脚。

    通道前沿件（端子指示/调理链）优先于引脚锚定——同一器件两类都命中时（如 DO 通道
    的栅极电阻既是端子前沿、又有点对点网贴驱动 IC），归属端子。LED 贴边（原则②，
    led_edge 开启时）次之——端子口指示灯归通道、板内指示灯（RUN 类）归贴边带；
    压过电源 IC/引脚分支，R17 这类串联电阻由此脱离 IC 引脚锚。电源 IC 无 clock/point
    网连到 IC，ic_pin_anchor 恒返回 None，走电源端子分支；其余走引脚分支。固定件
    （电源端子/固定 DC-DC）坐标是冻结的，锚点不随力导向漂移。

    电源簇模式（problem.power_cluster 非空）：簇成员（变换 IC + 非 C 电源件）锚
    「端子下方簇带」的矩形投影，带内返回 None（到站即停）；无簇模式退回电源端子
    中心（旧行为，逐字节一致）。
    """
    p = channel_anchor_point(problem, pos, ref)
    if p is not None:
        return p
    led = led_edge_refs(problem)
    if ref in led:
        return led_edge_anchor(problem, pos, led[ref])
    iface = iface_edge_refs(problem)
    if ref in iface:
        return iface_edge_anchor(problem, pos, iface[ref])
    pc = getattr(problem, "power_cluster", None)
    if pc is not None and ref in pc.members:
        return power_cluster_anchor(pc, (pos[ref][0], pos[ref][1]))
    if ref in power_ic_refs(problem):
        return power_connector_pos(problem)
    return ic_pin_anchor(problem, pos, ref)


def overlap_area(a: Box, b: Box) -> float:
    """两 AABB 交叠面积（mm²）。"""
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if (dx > 0 and dy > 0) else 0.0


def hpwl(problem: PlacementProblem, pos: dict[str, tuple[float, float]]) -> float:
    """加权中心近似 HPWL（mm）。未放置的 ref 跳过；每条 net 按其 weight 加权。"""
    total = 0.0
    for net in problem.nets:
        xs = [pos[r][0] for r in net.refs if r in pos]
        ys = [pos[r][1] for r in net.refs if r in pos]
        if xs:
            total += net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return total
