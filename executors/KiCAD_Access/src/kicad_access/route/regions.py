"""布线区域规划 + per-region 板生成/合并（M3-2）——人工审查关卡载体。

拍板（2026-09-08）：布线分区域进行，每区域一个独立 ``.kicad_pcb`` 落盘
（如 ``build/route/power.kicad_pcb``），用户逐个人工检查后才能合并全板
——per-region 板就是合并前的审查关卡，不提供跳过。

**区域规划** ``plan_regions()``：全部非地网络划分成互不相交区域（每网
恰好归属一区，规划器内自校验）；声明内层平面（M3-3c ``board.layers``）
时地网也归区：
0. ``planes``  地网（gnd_plane）+ 非地电源网（power_plane）→ 内层
   铺铜 + SMD 落孔（route/planes.py），最先产出、后续信号区板以它
   为 base 链——审查信号区时平面铜可见；
1. ``power``   全部非地电源网（跨模块，L1 电源树先布；平面架构下
   已并入 planes，此区为空不产板）；
2. 差分/对区域  spec routing critical 里的 diff_pair/pair 规则按网名首段
   （``ETH0_TD_P``→``ETH0``）并组成区（L1 差分；一条规则内即一对）；
3. 模块区域    intra 网（单模块内部）→ 该模块区域——模块/floorplan 块
   粒度，与拍板口径一致；
4. ``system``  其余跨模块信号网（L2 freerouting 兜底）。
以上为**认领顺序**（显式规则优先于隐式归类：CAN1_H 本是 can 模块 intra
网，diff_pair 规则先赢）；**布线顺序**再按受限度重排——planes 铺底 →
power → 全部 l2 区（模块 intra + system）→ 差分区殿后（l2 锚点密、
逃逸通道窄，链式先布先得下后布者会把它困死；差分对有整板绕行自由度）。
地网（GND/VSS 子串）在无平面声明时不进任何区域：铺铜 + 缝合过孔在
M3-6 统一处理；有 gnd_plane 时归 planes 区（见上）。
BGA 扇出（M3-4）不是区域——是 base 板上的确定性预铜，各区板共享、
各区审查时可见。

**写回** ``write_region_board()``：base 板（placed-unrouted，后续可含
扇出预铜）定点追加本区铜——只追加不重排（同 pcbwrite 手术风格，diff
可读）；铜网名必须 ∈ 区域网集合且板上真实存在，越界即 ValueError
（布线器网泄漏在此拦下）。

**合并** ``merge_regions()``：重读每区板**文件**（用户审查时可能手改，
以盘上文件为准，不信内存状态）。区板生成是链式的（route 逐区以前区
板为底），合并按同一链逐区多重集相减，diff 按铜网归属三向分流（网表
互不相交，每网恰一区）：本区网 = added；**前区网 = 前区板生成后又被
手改留下的陈旧烘焙副本，丢弃**（前区板才是该网的权威，改坐标被采纳
而非重复落铜）；后续区网/无主网 = 越界拒收。四道关卡：
- 规划出的每个区域必须有板文件（不提供跳过）；未知板文件（不在规划
  内）也报错——两向完备；
- 区板不得比前区板少本区网铜（删 base 预铜 = 对 base 的认知失配，
  要改改 base）；前区网铜缺失 = 前区板被手改，正常不拦；
- added 铜网名必须 ∈ 区域网集合（手改/布线器跑偏都能抓住）；
- 冲突检测：跨区域重复铜（同一条线布两遍）与异网短路（铜交叠，含
  vs base 既有铜）→ ValueError 列清单。间隙类违规归 drc 模块（M3-6），
  合并只拦"短路级"。
"""

from __future__ import annotations

import math
import uuid as _uuid_mod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import RouteBoard, Segment, Via, Zone, seg_dist, seg_seg_dist

_GROUND_HINTS = ("GND", "VSS")  # 地网子串（上粗网名）；不进区域，M3-6 铺铜
_PAIR_RULES = ("diff_pair", "pair")  # critical 里成对布线的规则种类
_EPS = 1e-6  # mm；铜交叠判定浮点容差（≪ 最小线宽）


@dataclass(frozen=True)
class Region:
    """一组网 + 归属布线器标签；name = 区板文件名（build/route/<name>.kicad_pcb）。"""

    name: str
    nets: tuple[str, ...]
    router: str  # "l1_power" | "l1_diff" | "l2_freerouting"（M3-3/4/5 按此挑区）


def _is_ground(name: str) -> bool:
    n = name.upper()
    return any(h in n for h in _GROUND_HINTS)


def plan_regions(ir, spec) -> list[Region]:
    """IR + 板规格 → 有序区域表（布线顺序：planes → power → 全部 l2
    → 差分对殿后；认领顺序见模块 docstring，两者解耦）。

    每个非地网络恰好归属一区（有 gnd_plane 时含地网，内部断言失配即
    ValueError）；空区不产 Region（平面架构下 power 策略区并入 planes
    后为空，"power" 名让给 power 模块的 intra 区）。
    """
    regions: list[Region] = []
    taken: set[str] = set()

    def _claim(names: Iterable[str]) -> tuple[str, ...]:
        claimed = tuple(sorted(set(names) - taken))
        taken.update(claimed)
        return claimed

    def _add(name: str, nets: tuple[str, ...], router: str) -> None:
        if nets:  # 空区不产板（CLI 无需特判）
            regions.append(Region(name, nets, router))

    # 0. 内层平面（M3-3c）：gnd_plane 吃地网、power_plane 吃非地电源网
    if spec.gnd_plane or spec.power_plane:
        want: list[str] = []
        if spec.gnd_plane:
            want += [n.name for n in ir.nets if _is_ground(n.name)]
        if spec.power_plane:
            want += [n.name for n in ir.nets
                     if n.kind == "power" and not _is_ground(n.name)]
        _add("planes", _claim(want), "l1_planes")

    # 1. 电源（非地）
    _add("power", _claim(
        n.name for n in ir.nets
        if n.kind == "power" and not _is_ground(n.name)), "l1_power")

    # 2. 差分/对区域：规则按首段前缀并组（ETH0_TD_* 与 ETH0_RD_* 同区）。
    #    认领先于模块区：CAN1_H 本是 can 模块 intra 网，spec 规则声明的
    #    差分区优先（显式规则 > 隐式归类）
    pair_by_prefix: dict[str, list[str]] = {}
    for rule in spec.routing.get("critical", []):
        if rule.get("rule") not in _PAIR_RULES:
            continue  # min_width 等约束不改归属（+24V_RAW 本就是电源网）
        nets = rule.get("nets") or ([rule["net"]] if "net" in rule else [])
        prefix = nets[0].split("_")[0]
        pair_by_prefix.setdefault(prefix, []).extend(nets)
    for prefix in sorted(pair_by_prefix):
        _add(prefix.lower(), _claim(pair_by_prefix[prefix]), "l1_diff")

    # 3. 模块区域：intra 网 → 所属模块（无 intra 网的模块自然无区）。
    #    与策略区重名（真板实况：power 模块 vs power 电源树区）加 "_mod" 后缀
    intra: dict[str, list[str]] = {}
    for n in ir.nets:
        if n.kind == "intra" and n.name not in taken:
            intra.setdefault(n.modules[0], []).append(n.name)
    taken_names = {r.name for r in regions}
    for module in sorted(intra):
        name = module if module not in taken_names else f"{module}_mod"
        _add(name, _claim(intra[module]), "l2_freerouting")

    # 4. 其余跨模块信号
    _add("system", _claim(
        n.name for n in ir.nets if n.kind == "system"), "l2_freerouting")

    # 布线顺序 ≠ 认领顺序：受限度优先重排，l2 与差分的先后由 spec
    # routing.region_order 声明（缺省 l2_first）。缺省序的依据：l2 区
    # （模块 intra + system）锚点密（BGA/晶振角）、逃逸通道窄，链式先
    # 布先得下会被后布区抢占——system 殿后于差分时饿死（4 层 BGA 真板
    # 实证 2026-09-10：eth 差分竖线占领 BGA 内部通道，24 网死 8；挪到
    # 差分前布 23/24）。diff_first 给反面形态：周边封装（LQFP）+ 两面
    # 信号层的 4 层板，差分无内层高速通道可借、长距对（U1↔RJ45 斜穿
    # 全板）是硬约束，先挑走廊，松散单端网后布让路（LQFP 4 层实证
    # 2026-09-11：l2 先布满两面，eth0 TD_P 无处下脚）。planes 恒铺底。
    groups = {
        "l1_planes": [r for r in regions if r.router == "l1_planes"],
        "l1_power": [r for r in regions if r.router == "l1_power"],
        "l2": [r for r in regions if r.router == "l2_freerouting"],
        "diff": [r for r in regions if r.router == "l1_diff"],
    }
    if spec.routing.get("region_order") == "diff_first":
        regions = groups["l1_planes"] + groups["l1_power"] + groups["diff"] + groups["l2"]
    else:
        regions = groups["l1_planes"] + groups["l1_power"] + groups["l2"] + groups["diff"]

    # 自校验：非地网全覆盖、恰一区（gnd_plane 声明时地网也须全归区）
    expect = {n.name for n in ir.nets
              if spec.gnd_plane or not _is_ground(n.name)}
    if taken != expect:
        missing = sorted(expect - taken)
        extra = sorted(taken - expect)
        raise ValueError(f"区域划分失配：漏 {missing} / 多 {extra}")
    return regions


# ---------------------------------------------------------------- 写回


def _f(x: float) -> str:
    """定点 4 位小数再去尾零（69.4 → "69.4"，5.0 → "5"）——与模板审美一致。"""
    if x == 0.0:
        x = 0.0  # 归一 −0.0
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _segment_block(s: Segment) -> str:
    return (
        f'\t(segment\n'
        f'\t\t(start {_f(s.x1)} {_f(s.y1)})\n'
        f'\t\t(end {_f(s.x2)} {_f(s.y2)})\n'
        f'\t\t(width {_f(s.width)})\n'
        f'\t\t(layer "{s.layer}")\n'
        f'\t\t(net "{s.net}")\n'
        f'\t)'
    )


def _via_block(v: Via) -> str:
    return (
        f'\t(via\n'
        f'\t\t(at {_f(v.x)} {_f(v.y)})\n'
        f'\t\t(size {_f(v.size)})\n'
        f'\t\t(drill {_f(v.drill)})\n'
        f'\t\t(layers "F.Cu" "B.Cu")\n'
        f'\t\t(net "{v.net}")\n'
        f'\t)'
    )


def _merge_rect_polys(
    polys: tuple[tuple[tuple[float, float], ...], ...],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """轴对齐矩形瓦片 → 每连通分量一凸包轮廓。

    KiCad 多轮廓 zone 语义 = 每 ``polygon`` 一块**独立**铜，且填充器
    对细齿轮廓会碎成小片。Voronoi 领地逐瓦落盘（0.5 方瓦或合并条）的
    阶梯轮廓两罪并罚：ø0.6 落地孔在单瓦上无法成环锚定 + 阶梯齿在
    min_thickness/清距雕刻下碎片化 → 孔悬空成片（LQFP 80×120 In2
    电源层 66 孔悬空根因，probe_a/probe_b/probe_d 三探针实证：阶梯
    轮廓 8 孔悬 8，同 zone 头换 bbox 整块 8 孔全焊合）。

    Voronoi 胞本就是凸集，阶梯只是栅格化——每连通分量取凸包即还原
    凸胞本形；胞间少量重叠（凹角/栅格齿）由 KiCad zone 清距自然裁
    开。坐标压缩不假设全局格距（1×0.5 条/0.5 方瓦/混合矩形集通用）；
    识别不了的（非轴对齐矩形）多边形原样透传，行为不回退。
    """
    rects: list[tuple[float, float, float, float]] = []
    passthrough: list[tuple[tuple[float, float], ...]] = []
    for poly in polys:
        xs = sorted({round(p[0], 6) for p in poly})
        ys = sorted({round(p[1], 6) for p in poly})
        if (len(xs) == 2 and len(ys) == 2
                and len({(round(p[0], 6), round(p[1], 6)) for p in poly}) == 4):
            rects.append((xs[0], ys[0], xs[1], ys[1]))
        else:
            passthrough.append(poly)
    if not rects:
        return polys
    xsv = sorted({x for r in rects for x in (r[0], r[2])})
    ysv = sorted({y for r in rects for y in (r[1], r[3])})
    xi = {x: i for i, x in enumerate(xsv)}
    yj = {y: j for j, y in enumerate(ysv)}
    occ: set[tuple[int, int]] = set()
    for x1, y1, x2, y2 in rects:
        for i in range(xi[x1], xi[x2]):
            for j in range(yj[y1], yj[y2]):
                occ.add((i, j))
    comps: list[set[tuple[int, int]]] = []  # 连通分量（4 邻接）
    rest = set(occ)
    while rest:
        seed = rest.pop()
        comp, stack = {seed}, [seed]
        while stack:
            i, j = stack.pop()
            for nb in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if nb in rest:
                    rest.discard(nb)
                    comp.add(nb)
                    stack.append(nb)
        comps.append(comp)

    def hull(points: set[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
        """Andrew 单调链凸包（共线点压掉）。"""
        seq = sorted(points)
        if len(seq) < 3:
            return tuple(seq)

        def half(itr):
            out: list[tuple[float, float]] = []
            for p in itr:
                while len(out) >= 2 and (
                    (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0]) <= 0):
                    out.pop()
                out.append(p)
            return out

        lo, up = half(seq), half(reversed(seq))
        return tuple(lo[:-1] + up[:-1]) if len(lo) + len(up) > 4 else tuple(seq)

    out = [hull({(xsv[i], ysv[j]) for i, j in comp
                 for i, j in ((i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1))})
           for comp in comps]
    out = [h for h in out if len(h) >= 3]
    return tuple(out + passthrough) if out else polys


def _zone_block(z: Zone) -> str:
    """zone 块（M3-3c 平面铺铜）——方言逐字段对齐参考板模板：

    ``connect_pads thru_hole_only`` 只连 TH 盘/过孔（SMD 盘靠落地孔 +
    F.Cu stub 补偿，见 route/planes.py）；uuid 省略（同 segment/via），
    多轮廓 = 多个 (polygon (pts ..)) 子块（模板方言：一 polygon 一 pts）。

    ``island_removal_mode 1``（永不删岛）——**有意偏离模板**：模板手工区
    是整块大平面（0=总删孤岛无害），机器 Voronoi 领地是几百块 0.5mm 瓦
    片，孔离盘 ≥ 半毫米就会分进"只有贯孔没有焊盘"的瓦片；KiCad 孤岛
    判定只认焊盘不认贯孔，mode 0 会把这类瓦片整块蒸发 → 孔悬空成片
    （LQFP 80×120 电源层 66 未连接事故根因之一，逐孔点检实证）。

    瓦片轮廓先经 ``_merge_rect_polys`` 并成整片领地外轮廓再落盘——
    逐瓦片独立轮廓会让 ø0.6 落地孔无法在 0.5mm 单瓦内成环锚定
    （同一事故的另一半根因，probe_a/probe_b 对照实证）。
    """
    polys = "".join(
        "\t\t(polygon\n\t\t\t(pts\n"
        + "".join(f'\t\t\t\t(xy {_f(x)} {_f(y)})\n' for x, y in poly)
        + "\t\t\t)\n\t\t)\n"
        for poly in _merge_rect_polys(z.polys)
    )
    return (
        f'\t(zone\n'
        f'\t\t(net "{z.net}")\n'
        f'\t\t(layer "{z.layer}")\n'
        f'\t\t(hatch edge 0.5)\n'
        f'\t\t(connect_pads thru_hole_only (clearance 0.254))\n'
        f'\t\t(min_thickness 0.254)\n'
        f'\t\t(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.5)'
        f' (island_removal_mode 1))\n'
        f'{polys}'
        f'\t)'
    )


def _append_blocks(text: str, blocks: list[str]) -> str:
    """根闭括号前定点追加（base 其余字节保真，同 pcbwrite 手术风格）。"""
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        raise ValueError("base 板文件不以 ')' 结尾——不是 .kicad_pcb？")
    head = stripped[:-1].rstrip()  # 去根闭括号及其前置空白
    if not blocks:
        return head + "\n)\n"
    return head + "\n" + "\n".join(blocks) + "\n)\n"


def write_region_board(
    base: Path, out: Path, region: Region,
    segments: Iterable[Segment], vias: Iterable[Via],
    zones: Iterable[Zone] = (),
) -> Path:
    """base 板 + 本区铜（segments/vias/zones）→ 区板落盘。

    越界网（∉ 区域网 / 板上不存在）即 ValueError。zones 为 M3-3c 平面
    铺铜（planes 区产出），其余区不用。
    """
    segs, vs, zs = list(segments), list(vias), list(zones)
    allowed = set(region.nets)
    have = RouteBoard(base).net_names()
    for c in (*segs, *vs):
        if c.net not in allowed:
            raise ValueError(
                f"区域 {region.name} 收到越界网络 {c.net!r} 的铜"
                f"（区域网 {len(allowed)} 个，布线器网泄漏）"
            )
        if c.net not in have:
            raise ValueError(f"网络 {c.net!r} 不在 base 板上（无焊盘无铜）")
    for z in zs:
        if z.net not in allowed:
            raise ValueError(
                f"区域 {region.name} 收到越界网络 {z.net!r} 的铺铜"
                f"（layer {z.layer}，{len(z.polys)} 轮廓）"
            )
    blocks = ([_segment_block(s) for s in segs] + [_via_block(v) for v in vs]
              + [_zone_block(z) for z in zs])
    out.write_text(
        _append_blocks(Path(base).read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    return out


def append_copper(
    board_path: Path, out: Path | None = None, *,
    segments: Iterable[Segment] = (), vias: Iterable[Via] = (),
    zones: Iterable[Zone] = (),
) -> Path:
    """既有板文件追加铜（M3-6）：合并后 GND 缝合孔等的公共落盘口。

    与 write_region_board 不同：不做区域网校验（全板视角——如 GND
    缝合孔，GND 常不在任何信号区）。out 缺省原地写回。
    """
    board_path = Path(board_path)
    out = Path(out) if out is not None else board_path
    blocks = ([_segment_block(s) for s in segments] + [_via_block(v) for v in vias]
              + [_zone_block(z) for z in zones])
    out.write_text(
        _append_blocks(board_path.read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    return out


def _copper_bbox(
    items: list[Segment | Via],
) -> tuple[float, float, float, float] | None:
    """一组铜件（segment/via）的轴对齐包围盒；空集 → None。"""
    xs: list[float] = []
    ys: list[float] = []
    for c in items:
        if isinstance(c, Segment):
            xs += (c.x1, c.x2)
            ys += (c.y1, c.y2)
        else:
            xs.append(c.x)
            ys.append(c.y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def draw_region_boxes(
    board_path: Path, out: Path | None = None, *,
    boxes: dict[str, tuple[float, float, float, float]],
) -> Path:
    """Eco1.User 画各区布铜足迹外框 + 区名（诊断叠层，不进铜不进制造）。

    region 是网分组不是板面授权划分，足迹框 = 该区全部 segment/via 的
    包围盒，即"这区的铜实际住哪"的诚实几何。配 ``--no-fusion`` 用：
    无缝合无领地铺铜的机械合并板上叠画，区与区的铜如何穿插/越界一眼
    可见（gr_line 方言逐字段对齐模板板，stroke 块 + Eco1.User）。
    """
    blocks: list[str] = []

    def _uuid() -> str:
        return str(_uuid_mod.uuid4())
    for name, (x1, y1, x2, y2) in sorted(boxes.items()):
        for a, b, c, d in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                           (x2, y2, x1, y2), (x1, y2, x1, y1)):
            blocks.append(
                f'\t(gr_line\n'
                f'\t\t(start {_f(a)} {_f(b)})\n'
                f'\t\t(end {_f(c)} {_f(d)})\n'
                f'\t\t(stroke\n'
                f'\t\t\t(width 0.15)\n'
                f'\t\t\t(type default)\n'
                f'\t\t)\n'
                f'\t\t(layer "Eco1.User")\n'
                f'\t)'
            )
        blocks.append(
            f'\t(gr_text "{name}"\n'
            f'\t\t(at {_f(x1)} {_f(y1 - 1.5)} 0)\n'
            f'\t\t(layer "Eco1.User")\n'
            f'\t\t(uuid "{_uuid()}")\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size 1.5 1.5) (thickness 0.15))\n'
            f'\t\t)\n'
            f'\t)'
        )
    board_path = Path(board_path)
    out = Path(out) if out is not None else board_path
    out.write_text(
        _append_blocks(board_path.read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------- 合并


@dataclass(frozen=True)
class MergeResult:
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]
    per_region: dict[str, tuple[int, int]]  # name → (nseg, nvia) added 计数
    # name → 布铜足迹包围盒（--draw-regions 诊断叠层的几何来源）
    boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)


def _pair_dist(c: Segment | Via, b: Segment | Via) -> tuple[float, float] | None:
    """两铜件（中心距, 半宽和)；不同铜层的双 segment 不相遇 → None。"""
    if isinstance(c, Segment) and isinstance(b, Segment):
        if c.layer != b.layer:
            return None  # 不同铜层的线不相遇（via 才跨层）
        return seg_seg_dist(c, b), (c.width + b.width) / 2
    if isinstance(c, Via) and isinstance(b, Via):
        return math.hypot(c.x - b.x, c.y - b.y), (c.size + b.size) / 2
    if isinstance(c, Via):  # via ✕ segment（via 跨层，总相遇）
        return seg_dist(c.x, c.y, b), (c.size + b.width) / 2
    return seg_dist(b.x, b.y, c), (b.size + c.width) / 2


def _shorts(items: list[tuple[str, Segment | Via]], base_b: RouteBoard) -> list[str]:
    """异网铜交叠检测：added×added（标注区域对）+ added×base。

    短路级 = 中心距 < 半宽和（铜真正交叠）；间隙不足不在此拦（drc 的活）。
    """
    bad: list[str] = []

    def _desc(c: Segment | Via) -> str:
        if isinstance(c, Segment):
            return (f"segment {c.net} ({c.x1:.3f},{c.y1:.3f})-({c.x2:.3f},{c.y2:.3f})"
                    f"@{c.layer}")
        return f"via {c.net} ({c.x:.3f},{c.y:.3f})"

    n = len(items)
    for i in range(n):
        r1, c1 = items[i]
        for j in range(i + 1, n):
            r2, c2 = items[j]
            if c1.net == c2.net:
                continue
            hit = _pair_dist(c1, c2)
            if hit and hit[0] < hit[1] - _EPS:
                bad.append(f"短路: [{r1}] {_desc(c1)} ✕ [{r2}] {_desc(c2)}"
                           f"（距 {hit[0]:.3f} < {hit[1]:.3f}）")
    # added vs base 既有铜（扇出预铜等；L1 布线时 base 是障碍，撞上即错）
    base_items = [("base", c) for c in (*base_b.segments, *base_b.vias)]
    for r, c in items:
        for br, b in base_items:
            if c.net == b.net:
                continue
            hit = _pair_dist(c, b)
            if hit and hit[0] < hit[1] - _EPS:
                bad.append(f"短路: [{r}] {_desc(c)} ✕ {br} 既有铜 {_desc(b)}"
                           f"（距 {hit[0]:.3f} < {hit[1]:.3f}）")
    return bad


def merge_regions(
    base: Path, out: Path, regions: list[Region], boards: dict[str, Path],
) -> MergeResult:
    """审查后的区板 → 全板。重读文件（含手改），按链逐区多重集差分。

    区板生成是链式的（route 逐区以前区板为底：本区布线器可见全部已落
    铜，少撞车），合并按同一链逐区做多重集相减，diff 按铜网归属三向
    分流（每网恰一区）：本区网 = added；前区网 = 前区板生成后被手改留
    下的陈旧烘焙副本，丢弃（前区板才是该网权威——改坐标被采纳）；后
    续区网/无主网 = 越界拒收。少铜只拦本区网（审查删 base 预铜）与后
    续网（生成链断链）；前区网缺失 = 前区被手改，不拦。zones 以 planes
    板盘上文件为权威，相对 merge base 相减后重发一次进合并板（链上后
    续区板里的同名 zone 不重复计）。
    """
    base = Path(base)
    names = [r.name for r in regions]
    missing = [n for n in names if n not in boards or not Path(boards[n]).is_file()]
    if missing:
        raise ValueError(f"区域板缺失（不提供跳过）: {', '.join(missing)}")
    unknown = sorted(set(boards) - set(names))
    if unknown:
        raise ValueError(f"未知区域板（不在规划内）: {', '.join(unknown)}")

    planes = next((r for r in regions if r.router == "l1_planes"), None)
    planes_b = RouteBoard(Path(boards[planes.name])) if planes else None

    base_b = RouteBoard(base)
    owner = {n: i for i, r in enumerate(regions) for n in r.nets}
    after = len(regions)  # 无主网位次：按"后续区"从严（越界/少铜都拒）

    added_segs: dict[str, list[Segment]] = {}
    added_vias: dict[str, list[Via]] = {}
    prev_b = base_b  # 链式 diff：与 route 的区板生成链同构（placed→planes→…）
    for i, r in enumerate(regions):
        ref_segs, ref_vias = Counter(prev_b.segments), Counter(prev_b.vias)
        b = RouteBoard(Path(boards[r.name]))
        segs, vs = Counter(b.segments), Counter(b.vias)
        for c in [*(ref_segs - segs).elements(), *(ref_vias - vs).elements()]:
            if owner.get(c.net, after) >= i:
                raise ValueError(
                    f"区域 {r.name} 板比前区板少铜（{c.net} 网：审查删了预铜"
                    f"要改改 base，或生成链断链）"
                )
        # 多重集差展开：Counter 相减丢弃 ≤0，再按剩余计数还原每条铜
        new_items: list[Segment | Via] = (
            [s for s in b.segments for _ in range((segs - ref_segs).get(s, 0))]
            + [v for v in b.vias for _ in range((vs - ref_vias).get(v, 0))]
        )
        dsegs: list[Segment] = []
        dvias: list[Via] = []
        for c in new_items:
            j = owner.get(c.net, after)
            if j == i:
                (dsegs if isinstance(c, Segment) else dvias).append(c)
            elif j > i:
                raise ValueError(
                    f"区域 {r.name} 板含越界网络 {c.net!r} 的铜"
                    f"（手改或布线器跑偏；区域网 {len(r.nets)} 个）"
                )
            # j < i：前区板生成后被手改留下的陈旧烘焙副本 → 丢弃不落盘
        added_segs[r.name] = dsegs
        added_vias[r.name] = dvias
        prev_b = b

    # 跨区域重复铜（同一条线布两遍）
    dup_seg = Counter(s for ss in added_segs.values() for s in ss)
    dup_via = Counter(v for vs_ in added_vias.values() for v in vs_)
    dups = [f"重复 segment {s.net} ({s.x1:.3f},{s.y1:.3f})-({s.x2:.3f},{s.y2:.3f})"
            for s, k in dup_seg.items() if k > 1] + \
           [f"重复 via {v.net} ({v.x:.3f},{v.y:.3f})" for v, k in dup_via.items() if k > 1]
    if dups:
        raise ValueError("跨区域重复铜：\n  " + "\n  ".join(sorted(dups)))

    items = [(name, c) for name in names for c in (*added_segs[name], *added_vias[name])]
    bad = _shorts(items, base_b)
    if bad:
        raise ValueError("异网短路（合并拒绝）：\n  " + "\n  ".join(bad))

    all_segs = tuple(s for n in names for s in added_segs[n])
    all_vias = tuple(v for n in names for v in added_vias[n])
    # 平面铺铜权威 = planes 板文件，相对 merge base 做多重集差
    # （base 自带 zone——如直接拿模板合并——不重复重发，同 segment 口径）
    zones = (list((Counter(planes_b.zones) - Counter(base_b.zones)).elements())
             if planes_b else [])
    blocks = ([_segment_block(s) for s in all_segs] + [_via_block(v) for v in all_vias]
              + [_zone_block(z) for z in zones])
    Path(out).write_text(
        _append_blocks(base.read_text(encoding="utf-8"), blocks), encoding="utf-8",
    )
    return MergeResult(
        segments=all_segs,
        vias=all_vias,
        per_region={n: (len(added_segs[n]), len(added_vias[n])) for n in names},
        boxes={n: b for n in names
               if (b := _copper_bbox([*added_segs[n], *added_vias[n]])) is not None},
    )
