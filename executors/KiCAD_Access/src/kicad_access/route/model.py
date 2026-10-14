"""布线态板模型（M3-1）：pads/segments/vias 只读解析 + 坐标变换烘焙。

KiCad 板文件里 pad 坐标是**封装本地系**（M2-1 用真板 J5 校准的同一约定）：
绝对 = 封装原点 + rot(本地)，x' = x·cosθ + y·sinθ、y' = −x·sinθ + y·cosθ。
``pad`` 的 ``(at x y angle)`` 角度 token 则是**板绝对系**（KiCad 方言：
位置本地、角度绝对；60 板 56 个带 token 盘逐一满足 token == (封装角 +
库自转角) % 360，J1/J2/J3 的手改孤儿 token 全落在方形盘上、几何无感），
token 缺省即 0。故 w/h 交换**只看 token**（45°<token%180<135°）；
旋转封装由 pcbwrite 同步 token += Δrot，模型侧不得再叠加封装角
（叠加 = 双重计算，曾致 placed 板 U2 盘形态错判）。

网络方言：本项目板（含 60 参考板）net 按名声明（``(net "NAME")``，无数字
编码）；标准 KiCad 的 ``(net 5 "NAME")`` 亦兼容（取末元素为名）。

距离场（pad_dist/seg_dist/via_dist）为 A* 布线器与 DRC 共用的间隙几何
基元：roundrect 用精确 SDF（平边段 = |offset|−w/2、圆角段 = 角圆心距−r）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from ..kicad import sexpr as sx

BOTH_LAYERS = ("F.Cu", "B.Cu")


def _angle(at: list | None) -> float:
    """(at x y [angle [unlocked]]) 的角度元素；缺省/非数值（unlocked）= 0。"""
    if at is None or len(at) < 4 or not isinstance(at[3], (int, float)):
        return 0.0
    return float(at[3])


@dataclass(frozen=True)
class Pad:
    """绝对坐标系（mm，KiCad y-down）的焊盘，w/h 已烘焙旋转。"""

    ref: str
    num: str
    net: str | None  # None = 无网络（未用脚/NPTH，纯障碍物）
    x: float
    y: float
    w: float
    h: float
    shape: str  # rect | roundrect | circle | oval | trapezoid | custom
    layers: tuple[str, ...]  # 铜层参与面：("F.Cu",) | ("F.Cu", "B.Cu")
    radius: float = 0.0  # roundrect 圆角半径（0 = 直角）

    @property
    def is_th(self) -> bool:
        return len(self.layers) > 1

    @property
    def key(self) -> str:
        return f"{self.ref}-{self.num}"


@dataclass(frozen=True)
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str


@dataclass(frozen=True)
class Arc:
    """圆弧轨道（人工板常见；KiCad (arc (start)(mid)(end)) 三点定弧）。

    连通性按 chord() 弦近似——弧端点即连接点，与段等效；弧腹与盘/轨
    相交的极端形态漏计（弦垂距远小于盘半径，可忽略）。length 为真弧长
    （外接圆），风格度量里弧光滑无折弯、不参与 non45。
    """

    x1: float
    y1: float
    xm: float
    ym: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str

    def chord(self) -> Segment:
        return Segment(self.x1, self.y1, self.x2, self.y2,
                       self.width, self.layer, self.net)

    @property
    def length(self) -> float:
        """真弧长：三点外接圆 + 经过 mid 的扫角。"""
        x1, y1, xm, ym, x2, y2 = self.x1, self.y1, self.xm, self.ym, self.x2, self.y2
        # 外接圆心用鞋板(shoelace)标准式——首版分子/分母约定错位，算出的
        # "圆心"是真外心过原点的反射像（原点圆心的单测暴露不了），近共线
        # 三点会把 0.27mm 半径读成 370mm、弧长爆到 2300mm
        d = 2.0 * (x1 * (ym - y2) + xm * (y2 - y1) + x2 * (y1 - ym))
        if abs(d) < 1e-12:           # 共线退化 → 折线长
            return math.hypot(xm - x1, ym - y1) + math.hypot(x2 - xm, y2 - ym)
        s1, sm, s2 = x1 * x1 + y1 * y1, xm * xm + ym * ym, x2 * x2 + y2 * y2
        cx = (s1 * (ym - y2) + sm * (y2 - y1) + s2 * (y1 - ym)) / d
        cy = (s1 * (x2 - xm) + sm * (x1 - x2) + s2 * (xm - x1)) / d
        r = math.hypot(x1 - cx, y1 - cy)

        def _norm(t: float) -> float:    # 归一到 (0, 2π]
            return t % (2.0 * math.pi) or 2.0 * math.pi

        a1 = math.atan2(y1 - cy, x1 - cx)
        am = math.atan2(ym - cy, xm - cx)
        a2 = math.atan2(y2 - cy, x2 - cx)
        pos = _norm(a2 - a1)             # 正向扫角候选
        # mid 在正向弧 (0,pos) 内 → 正向；否则取负向（pos−2π，绝对值≤2π）
        sweep = pos if 0 < _norm(am - a1) < pos else pos - 2.0 * math.pi
        return r * abs(sweep)


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    size: float
    drill: float
    net: str


@dataclass(frozen=True)
class Zone:
    """平面铺铜区（M3-3c）：一层上归属同一网络的多边形集合。

    多边形为顶点环（KiCad y-down，mm）；填充由 KiCad fill 引擎完成——
    我们只产出领地多边形（Voronoi 分区/整板矩形），热焊盘、反焊盘、
    孤岛移除都是 fill 侧语义。
    """

    net: str
    layer: str
    polys: tuple[tuple[tuple[float, float], ...], ...]


class RouteBoard:
    """布线器视角的板：pads（含网络）+ 既有铜（segments/vias）+ 板框。

    用法：``board = RouteBoard(Path("... .kicad_pcb"))``；背面封装（B.Cu
    footprint）当前真板不存在，遇到即 ValueError（镜像语义待真需要时定）。
    """

    def __init__(self, src: Path | str):
        text = src.read_text(encoding="utf-8") if isinstance(src, Path) else src
        root = sx.loads(text)
        if sx.tag(root) != "kicad_pcb":
            raise ValueError(f"不是 .kicad_pcb 文件（顶层 {sx.tag(root)!r}）")
        self.pads: list[Pad] = []
        self.segments: list[Segment] = []
        self.arcs: list[Arc] = []
        self.vias: list[Via] = []
        self.zones: list[Zone] = []
        self.refs: list[str] = []
        self.fp_lib: dict[str, str] = {}  # 位号 → 封装 lib_id（分策证据）
        self.copper_layers = self._read_layer_stack(root)
        self._read_pads(root)
        self._read_copper(root)
        self._read_zones(root)
        self.outline = self._read_outline(root)

    # ---------------------------------------------------------------- 读取

    @staticmethod
    def _read_layer_stack(root: list) -> tuple[str, ...]:
        """板头部 (layers (0 "F.Cu" signal) (1 "In1.Cu" power) …) 的全部
        铜层名，按层号排序。4 层板 = ("F.Cu","In1.Cu","In2.Cu","B.Cu")；
        解析不出（方言板/残缺）退回 F/B——TH 盘层集用真实栈，否则内层
        轨落到 TH 盘身内也层不匹配（kit-dev-coldfire 4 层板 /URTS1：
        In2.Cu 轨端距 P302.3 盘心 0.016mm，恒判断裂）。"""
        block = next(sx.children(root, "layers"), None)
        if block is None:
            return BOTH_LAYERS
        cus = []
        for ent in block[1:]:
            if not isinstance(ent, list) or len(ent) < 2:
                continue
            name = str(sx._value(ent[1]))
            if name.endswith(".Cu"):
                idx = ent[0] if isinstance(ent[0], (int, float)) else 999
                cus.append((idx, name))
        return tuple(n for _i, n in sorted(cus)) or BOTH_LAYERS

    def _read_pads(self, root: list) -> None:
        for fp in sx.children(root, "footprint"):
            props = {
                str(sx._value(p[1])): str(sx._value(p[2]))
                for p in sx.children(fp, "property")
            }
            ref = props.get("Reference")
            if ref is None:  # legacy 方言板：fp_text reference 兜底
                for t in sx.children(fp, "fp_text"):
                    if str(sx._value(t[1])) == "reference":
                        ref = str(sx._value(t[2]))
                        break
            ref = ref or "?"
            if len(fp) > 1 and not isinstance(fp[1], list):
                self.fp_lib[ref] = str(sx._value(fp[1]))
            self.refs.append(ref)
            at = next(sx.children(fp, "at"), None)
            fx, fy = sx.num(at[1]) if at else 0.0, sx.num(at[2]) if at else 0.0
            frot = _angle(at)
            flayer = next(sx.children(fp, "layer"), None)
            fside = str(sx._value(flayer[1])) if flayer else "F.Cu"
            # 背面封装：pad 局部坐标在文件里已烘焙翻转（2026-09-29 实证：
            # stickhub R7/U2/J9 六盘，同 top 公式 vs pcbnew 解析全零误差），
            # 位置计算与正面同式；差异仅 smd 铜层面（B.Cu）。
            cos_a, sin_a = math.cos(math.radians(frot)), math.sin(math.radians(frot))
            for pad in sx.children(fp, "pad"):
                p = self._read_pad(pad, ref, fx, fy, cos_a, sin_a, fside)
                if p is not None:
                    self.pads.append(p)

    def _read_pad(
        self, pad: list, ref: str, fx: float, fy: float,
        cos_a: float, sin_a: float, fside: str = "F.Cu",
    ) -> "Pad | None":
        num = str(sx._value(pad[1]))
        ptype = str(sx._value(pad[2]))  # smd | connect | thru_hole | np_thru_hole
        shape = str(sx._value(pad[3]))
        pa = next(sx.children(pad, "at"), None)
        px, py = sx.num(pa[1]), sx.num(pa[2])
        prot = _angle(pa)
        sz = next(sx.children(pad, "size"), None)
        w, h = sx.num(sz[1]), sx.num(sz[2])
        net = self._net_name(pad)
        # 角度 token 是板绝对角（见模块 docstring）→ 只看 token 烘焙旋转
        # AABB：w' = w·|cosθ|+h·|sinθ|、h' = w·|sinθ|+h·|cosθ|。0°/90° 走
        # 恒等/精确交换（与旧“竖放交换”逐位一致）；任意斜放角（45° 等）
        # 旧代码不换也不外接，y 向占位被低估——BusPirate5 U103 QFP-48
        # 斜放 45°：0.875×0.2 真外接 0.762×0.762，差 0.56mm，track 压真
        # 铜面短路（KiCad 实测间隙 0.0018mm，2026-10-10）。
        q = prot % 180.0
        if abs(q - 90.0) <= 1e-9:
            w, h = h, w
        elif q > 1e-9:
            c, s = abs(math.cos(math.radians(q))), abs(math.sin(math.radians(q)))
            w, h = w * c + h * s, w * s + h * c
        rr = next(sx.children(pad, "roundrect_rratio"), None)
        radius = sx.num(rr[1]) * min(w, h) if (rr and shape == "roundrect") else 0.0
        if ptype in ("smd", "connect"):
            # connect 盘（金手指/边缘连接器）同 smd 只在声明铜层：interf_u
            # BUS1 的 F.Cu/B.Cu 手指 31 对同位异网矩形，误当 TH 全层会让
            # 对面手指在本层幻影占位，24 个 ISA 网逃逸起点全被封死
            # （2026-09-30 实锤：修前三档 0/28 手指接线、26 未布通里 24 个
            # /PC-*）。声明层挑铜层：EP 锡膏分割开窗（仅 F.Paste）无铜不是
            # 盘，记成 F.Cu 障碍会挡住 EP 落孔（eLQFP 库 9 个 pad "" 实踩）
            lay = next(sx.children(pad, "layers"), None)
            cu = [str(sx._value(v)) for v in lay[1:]] if lay else []
            if not any(l.endswith(".Cu") for l in cu):
                return None
            # 铜层以盘自身声明为准（KiCad 语义），不随封装面硬指：sonde
            # xilinx J1 edge-mount DSUB 封装声明 F.Cu、盘却声明 B.Cu（作
            # 者把盘推到底面），按封装面读成 F.Cu 会把 B.Cu 轨落盘判成
            # 跨层假断裂（/VCC_SENSE-ERROR*：KiCad DRC 判 0 未连）。
            # "*.Cu" 通配=全铜层；声明层不在板栈（方言残缺）退回封装面。
            if "*.Cu" in cu:
                layers = self.copper_layers
            else:
                layers = (tuple(l for l in cu if l in self.copper_layers)
                          or (("B.Cu",) if fside == "B.Cu" else ("F.Cu",)))
        else:
            # TH 盘通全部铜层（含内层）：用板层叠而非恒 F/B（见
            # _read_layer_stack docstring 的 kit-dev /URTS1 案例）
            layers = self.copper_layers
        return Pad(
            ref=ref,
            num=num,
            net=net,
            x=fx + px * cos_a + py * sin_a,
            y=fy - px * sin_a + py * cos_a,
            w=w,
            h=h,
            shape=shape,
            layers=layers,
            radius=radius,
        )

    @staticmethod
    def _net_name(pad: list) -> str | None:
        net = next(sx.children(pad, "net"), None)
        if net is None:
            return None
        # (net "名") 本项目方言 / (net 5 "名") 标准 KiCad —— 取末元素
        return str(sx._value(net[-1]))

    def _read_copper(self, root: list) -> None:
        # 标准 KiCad 板：顶层铜 (net N) 只写数字，名字在文件头部
        # (net N "name") 声明表（demo/外部板形态；本项目方言名字直书无表）
        self._net_ids = {
            str(sx._value(n[1])): str(sx._value(n[2]))
            for n in sx.children(root, "net") if len(n) > 2
        }
        for seg in sx.children(root, "segment"):
            start, end = next(sx.children(seg, "start")), next(sx.children(seg, "end"))
            width = next(sx.children(seg, "width"), None)
            layer = next(sx.children(seg, "layer"), None)
            net = self._top_net(seg)
            self.segments.append(Segment(
                x1=sx.num(start[1]), y1=sx.num(start[2]),
                x2=sx.num(end[1]), y2=sx.num(end[2]),
                width=sx.num(width[1]) if width else 0.0,
                layer=str(sx._value(layer[1])) if layer else "F.Cu",
                net=net,
            ))
        # (arc (start x y) (mid x y) (end x y) (width ..) (layer ..) (net ..))
        # —— 人工板常用（stickhub 全板 180 条，+3.3V 一网 20 条）；mid 缺失
        # 或三点共线的畸形弧跳过（退化形态无连接信息增量）
        for arc in sx.children(root, "arc"):
            start = next(sx.children(arc, "start"), None)
            mid = next(sx.children(arc, "mid"), None)
            end = next(sx.children(arc, "end"), None)
            if not (start and mid and end):
                continue
            width = next(sx.children(arc, "width"), None)
            layer = next(sx.children(arc, "layer"), None)
            self.arcs.append(Arc(
                x1=sx.num(start[1]), y1=sx.num(start[2]),
                xm=sx.num(mid[1]), ym=sx.num(mid[2]),
                x2=sx.num(end[1]), y2=sx.num(end[2]),
                width=sx.num(width[1]) if width else 0.0,
                layer=str(sx._value(layer[1])) if layer else "F.Cu",
                net=self._top_net(arc),
            ))
        for via in sx.children(root, "via"):
            at = next(sx.children(via, "at"), None)
            size = next(sx.children(via, "size"), None)
            drill = next(sx.children(via, "drill"), None)
            self.vias.append(Via(
                x=sx.num(at[1]), y=sx.num(at[2]),
                size=sx.num(size[1]) if size else 0.6,
                drill=sx.num(drill[1]) if drill else 0.3,
                net=self._top_net(via),
            ))

    def _read_zones(self, root: list) -> None:
        """(zone (net ..) (layer ..) (polygon (pts (xy ..) ..)) ..) → Zone。

        一个 zone 块可含多个 polygon 轮廓（KiCad 多轮廓 zone）， polys
        原序保留。zone 是 M3-3c 平面铜，非 F/B 障碍——只作数据。
        """
        for zone in sx.children(root, "zone"):
            if next(sx.children(zone, "net"), None) is None:
                continue          # auto-placement-area / 禁布区：无网络，非铺铜
            layer = next(sx.children(zone, "layer"), None)
            polys: list[tuple[tuple[float, float], ...]] = []
            for poly in sx.children(zone, "polygon"):
                pts = next(sx.children(poly, "pts"), None)
                if pts is None:
                    continue
                ring = tuple(
                    (sx.num(xy[1]), sx.num(xy[2])) for xy in sx.children(pts, "xy")
                )
                if ring:
                    polys.append(ring)
            if polys:
                self.zones.append(Zone(
                    net=self._top_net(zone),
                    layer=str(sx._value(layer[1])) if layer else "",
                    polys=tuple(polys),
                ))

    def _top_net(self, node: list) -> str:
        net = next(sx.children(node, "net"), None)
        if net is None:
            raise ValueError(f"{sx.tag(node)} 块缺 net —— 布线态铜必须带网络")
        name = str(sx._value(net[-1]))
        return self._net_ids.get(name, name)  # 纯数字引用 → 声明表换名

    @staticmethod
    def _read_outline(root: list) -> tuple[float, float, float, float]:
        """Edge.Cuts 图形包围盒（MVP 板框=矩形；一般多边形留 M4）。"""
        xs: list[float] = []
        ys: list[float] = []
        for kind in ("gr_line", "gr_arc"):
            for g in sx.children(root, kind):
                lay = next(sx.children(g, "layer"), None)
                if lay is None or str(sx._value(lay[1])) != "Edge.Cuts":
                    continue
                for pt in ("start", "end"):
                    p = next(sx.children(g, pt), None)
                    if p is not None:
                        xs.append(sx.num(p[1]))
                        ys.append(sx.num(p[2]))
                mid = next(sx.children(g, "mid"), None)
                if mid is not None:
                    xs.append(sx.num(mid[1]))
                    ys.append(sx.num(mid[2]))
        if not xs:
            raise ValueError("Edge.Cuts 无图形，板框未知")
        return (min(xs), min(ys), max(xs), max(ys))

    # ---------------------------------------------------------------- 查询

    def inherit_nets(self) -> int:
        """无网络焊盘完全落在同封装某有网络焊盘铜内 → 继承其网络。

        eLQFP/BGA 的 EP 阵列盘（同一裸铜切成多盘、只有一盘带网络）实际
        电气同网；不继承会让布线器把 EP 铜当纯障碍物。返回继承数。
        """
        n = 0
        by_ref: dict[str, list[Pad]] = {}
        for p in self.pads:
            by_ref.setdefault(p.ref, []).append(p)
        new_pads = list(self.pads)
        for i, p in enumerate(self.pads):
            if p.net is not None:
                continue
            for q in by_ref[p.ref]:
                if q.net is None or q is p:
                    continue
                if (abs(p.x - q.x) < (q.w - p.w) / 2
                        and abs(p.y - q.y) < (q.h - p.h) / 2):
                    new_pads[i] = Pad(**{**p.__dict__, "net": q.net})
                    n += 1
                    break
        if n:
            self.pads = new_pads
        return n

    def pads_by_net(self) -> dict[str, list[Pad]]:
        out: dict[str, list[Pad]] = {}
        for p in self.pads:
            if p.net is not None:
                out.setdefault(p.net, []).append(p)
        return out

    def net_names(self) -> set[str]:
        """板上出现过的全部网络（pads ∪ 既有铜）。"""
        names = {p.net for p in self.pads if p.net is not None}
        names |= {s.net for s in self.segments}
        names |= {v.net for v in self.vias}
        return names

    def bga_refs(self, min_pads: int = 20, max_pitch: float = 1.0,
                 min_fill: float = 0.6) -> set[str]:
        """面阵列（BGA）检测：封装形态决定扇出算法与参数的入口判据。

        三判据缺一不可：盘数 ≥ min_pads、最近邻节距中位数 ≤ max_pitch、
        面阵列形态——盘占满 x/y 聚类网格 ≥ min_fill（lib 名含 bga 直接
        采信，名字即封装声明）。只认形态不认位号：真板 U1（BGA-121，
        11×11 网格填充率 1.0）命中；eLQFP100L（101 盘 @0.5mm）前两条全
        中但引脚只沿四边走，26×26 网格填充率 ~0.15——周边封装不是面
        阵列，0.3mm 边盘打盘中孔必挤爆净距（LQFP 板实跑踩过）。BGA 落
        盘需要专用扇出参数（0.8mm 间距下盘间铜隙仅 0.4mm），布线器把它
        们从普通锚点集里分出来交 M3-4 扇出策略接管；周边封装走普通
        锚点集。
        """
        by_ref: dict[str, list[Pad]] = {}
        for p in self.pads:
            by_ref.setdefault(p.ref, []).append(p)
        out: set[str] = set()
        for ref, ps in by_ref.items():
            # 同名焊盘按 num 折叠成电气盘、取面积最大者为代表：QFN/周边封装
            # 的 EP 热过孔子盘与本体叠在同一 num 下，不去重会把盘数/网格
            # 填充率撑爆（U4 VQFN-24 实测 35 盘 grid_fill 1.4 → 误判 BGA）。
            uniq: dict[str, Pad] = {}
            for p in ps:
                cur = uniq.get(p.num)
                if cur is None or p.w * p.h > cur.w * cur.h:
                    uniq[p.num] = p
            u = list(uniq.values())
            if len(u) < min_pads:
                continue
            nn = [
                min(
                    math.hypot(a.x - b.x, a.y - b.y)
                    for j, b in enumerate(u) if j != i
                )
                for i, a in enumerate(u)
            ]
            pitch = median(nn)
            if pitch > max_pitch:
                continue
            named = re.search(r"bga", self.fp_lib.get(ref, ""), re.I)
            if not named and self._grid_fill(u, pitch) < min_fill:
                continue
            out.add(ref)
        return out

    @staticmethod
    def _grid_fill(ps: "list[Pad]", pitch: float) -> float:
        """x/y 各按节距/2 单链聚类 → 盘对网格的占有率。

        BGA 面阵列 ≈ 1.0（盘占满网格）；QFP/SOIC 周边封装引脚只沿边，
        内部空 → 占有率 < 0.2（eLQFP100 实测 ~0.15）。
        """
        def clusters(vals: list[float]) -> int:
            n, last = 0, -1e18
            for v in sorted(vals):
                if v - last > pitch / 2:
                    n += 1
                last = v
            return n

        return len(ps) / (clusters([p.x for p in ps])
                          * clusters([p.y for p in ps]))


# ---------------------------------------------------------------- 距离场


def pad_dist(px: float, py: float, pad: Pad) -> float:
    """点到焊盘铜边缘的最短距离（负 = 在铜内）。roundrect 用精确 SDF。"""
    if pad.shape == "circle":
        return math.hypot(px - pad.x, py - pad.y) - pad.w / 2
    if pad.radius > 1e-9:
        dx = max(abs(px - pad.x) - (pad.w - 2 * pad.radius) / 2, 0.0)
        dy = max(abs(py - pad.y) - (pad.h - 2 * pad.radius) / 2, 0.0)
        return math.hypot(dx, dy) - pad.radius  # 铜内为负（dx=dy=0 → −r）
    dx = max(abs(px - pad.x) - pad.w / 2, 0.0)
    dy = max(abs(py - pad.y) - pad.h / 2, 0.0)
    return math.hypot(dx, dy)


def seg_dist(px: float, py: float, s: Segment) -> float:
    """点到线段中心线的最短距离（铜缘距离由调用方减 width/2）。"""
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    length = dx * dx + dy * dy
    if length == 0.0:
        return math.hypot(px - s.x1, py - s.y1)
    t = ((px - s.x1) * dx + (py - s.y1) * dy) / length
    t = max(0.0, min(1.0, t))
    return math.hypot(px - s.x1 - t * dx, py - s.y1 - t * dy)


def seg_seg_dist(a: Segment, b: Segment) -> float:
    """两线段中心线最短距离（相交/共线重叠 = 0；异网短路判定用）。

    相交或共线重叠 → 0；否则最短距必为某端点到对方内段（min-of-4 端点
    投影即精确值）。共线包含（短线整体在长线内）端点距不为 0，需显式
    共线在线上判定兜住。
    """
    def _cross(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
        return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)

    def _on(px: float, py: float, s: Segment) -> bool:  # 共线前提下点在线段范围内
        return (min(s.x1, s.x2) - 1e-12 <= px <= max(s.x1, s.x2) + 1e-12
                and min(s.y1, s.y2) - 1e-12 <= py <= max(s.y1, s.y2) + 1e-12)

    p = [(a.x1, a.y1), (a.x2, a.y2)]
    q = [(b.x1, b.y1), (b.x2, b.y2)]
    d1, d2 = _cross(*q[0], *q[1], *p[0]), _cross(*q[0], *q[1], *p[1])
    d3, d4 = _cross(*p[0], *p[1], *q[0]), _cross(*p[0], *p[1], *q[1])
    if d1 * d2 < 0.0 and d3 * d4 < 0.0:
        return 0.0  # 规范相交（严格跨越；端点触及走下面共线分支）
    eps = 1e-9
    if abs(d1) < eps and _on(*p[0], b):
        return 0.0  # p 端点共线且落在 b 段上（含共线重叠/包含）
    if abs(d2) < eps and _on(*p[1], b):
        return 0.0
    if abs(d3) < eps and _on(*q[0], a):
        return 0.0
    if abs(d4) < eps and _on(*q[1], a):
        return 0.0
    return min(
        seg_dist(p[0][0], p[0][1], b), seg_dist(p[1][0], p[1][1], b),
        seg_dist(q[0][0], q[0][1], a), seg_dist(q[1][0], q[1][1], a),
    )


def seg_rect_dist(s: Segment, cx: float, cy: float, hw: float, hh: float) -> float:
    """线段中心线到轴对齐矩形（心/半宽/半高）的最短距；端点在内 = 0。

    细长盘（鸥翼 1.75×0.4）的轨迹端点常落在矩形盘身内但离盘心超过
    min(w,h)/2——圆判据在这些盘上系统性误判断裂（video U11.140 实测：
    |dx|=0.5<0.875 且 |dy|=0.01<0.2 在身内，距心 0.496 > 0.301 阈值）。
    Pad 的 w/h 已烘焙旋转（轴对齐），矩形 containment 是精确判据。
    """
    def pt_in(px: float, py: float) -> bool:
        return abs(px - cx) <= hw and abs(py - cy) <= hh

    if pt_in(s.x1, s.y1) or pt_in(s.x2, s.y2):
        return 0.0
    edges = (
        Segment(cx - hw, cy - hh, cx + hw, cy - hh, 0.0, "", ""),
        Segment(cx + hw, cy - hh, cx + hw, cy + hh, 0.0, "", ""),
        Segment(cx + hw, cy + hh, cx - hw, cy + hh, 0.0, "", ""),
        Segment(cx - hw, cy + hh, cx - hw, cy - hh, 0.0, "", ""),
    )
    return min(seg_seg_dist(s, e) for e in edges)


def via_dist(px: float, py: float, v: Via) -> float:
    return math.hypot(px - v.x, py - v.y) - v.size / 2
