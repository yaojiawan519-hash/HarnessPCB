""".kicad_mod 足迹几何解析（M2-1）。

只读不改；给布局引擎（floorplan 非重叠 / recipe pad 几何 / 融合推挤）
和写板层（raw 整块嵌入）提供几何。

变换约定（2026-09-08 从 EtherCAT_CAN_60 板上 J5/PinHeader_1x03 θ=90 校准）：
- 板文件 footprint 内 pad 坐标 = 库本地系原样（不随封装旋转预变换）；
- 绝对位置 = 原点 + rot(本地, θ)，rot 为 y-down 系屏幕逆时针：
  x' = x·cosθ + y·sinθ, y' = −x·sinθ + y·cosθ；
- pad 自身无旋转时，文件里 pad angle = 封装角（KiCad 7+ 存绝对角）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import sexpr as sx

Box = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax)


@dataclass
class PadGeom:
    number: str
    x: float  # footprint 本地系
    y: float
    w: float
    h: float
    angle: float  # 文件中的 pad 角（无自身旋转时 = 封装角）
    kind: str  # smd | thru_hole | np_thru_hole
    layers: list[str] = field(default_factory=list)
    drill: float | None = None  # THT 孔径


@dataclass
class FootprintGeom:
    name: str  # "lib:name"
    pads: list[PadGeom] = field(default_factory=list)
    boxes: dict[str, Box] = field(default_factory=dict)  # 层名 → 本地 AABB
    raw: str = ""  # .kicad_mod 原文（写板时整块嵌入，不重序列化）

    @property
    def courtyard(self) -> Box | None:
        return self.boxes.get("F.CrtYd")

    @property
    def fab(self) -> Box | None:
        return self.boxes.get("F.Fab")

    @property
    def silk(self) -> Box | None:
        return self.boxes.get("F.SilkS")

    def outline(self) -> Box:
        """占位外框：courtyard 优先，无则 pad 外缘外扩 0.25mm（自定义库常见缺 CrtYd）。"""
        if self.courtyard:
            return self.courtyard
        if not self.pads:
            return (0.0, 0.0, 0.0, 0.0)
        m = 0.25
        return (
            min(p.x - p.w / 2 for p in self.pads) - m,
            min(p.y - p.h / 2 for p in self.pads) - m,
            max(p.x + p.w / 2 for p in self.pads) + m,
            max(p.y + p.h / 2 for p in self.pads) + m,
        )

    def bbox_at(self, x: float, y: float, rot: float = 0.0) -> Box:
        """本地外框旋转 rot 后的轴对称包围盒（绝对坐标）。"""
        return _rot_box(self.outline(), x, y, rot)

    def pads_at(
        self, x: float, y: float, rot: float = 0.0
    ) -> list[tuple[str, float, float]]:
        """放置态各 pad 绝对坐标 [(number, x, y, ...)]。"""
        out: list[tuple[str, float, float]] = []
        c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        for p in self.pads:
            ax = x + p.x * c + p.y * s
            ay = y - p.x * s + p.y * c
            out.append((p.number, ax, ay))
        return out


def _rot_box(b: Box, x: float, y: float, rot: float) -> Box:
    """本地框四角旋转后取 AABB，平移到 (x, y)。"""
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    corners = [
        (b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])
    ]
    pts = [(x + px * c + py * s, y - px * s + py * c) for px, py in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _merge(box: Box | None, other: Box) -> Box:
    if box is None:
        return other
    return (
        min(box[0], other[0]), min(box[1], other[1]),
        max(box[2], other[2]), max(box[3], other[3]),
    )


def _pts_bounds(node: list) -> list[tuple[float, float]]:
    """(pts (xy x y) ...) 子节点的全部点。"""
    out = []
    for pts in sx.children(node, "pts"):
        for xy in sx.children(pts, "xy"):
            out.append((sx.num(xy[1]), sx.num(xy[2])))
    return out


def parse_footprint(text: str, name: str = "") -> FootprintGeom:
    """解析 .kicad_mod 文本 → FootprintGeom（只取布局需要的几何子集）。"""
    doc = sx.loads(text)
    root = doc[0] if sx.tag(doc[0]) == "footprint" else doc
    fp = FootprintGeom(name=name or str(sx._value(root[1])), raw=text)

    boxes: dict[str, Box] = {}

    def acc(layer: str, pts: list[tuple[float, float]]) -> None:
        if not pts:
            return
        b = (min(p[0] for p in pts), min(p[1] for p in pts),
             max(p[0] for p in pts), max(p[1] for p in pts))
        boxes[layer] = _merge(boxes.get(layer), b)

    for tag in ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly", "fp_curve"):
        for node in sx.children(root, tag):
            layer = next(sx.children(node, "layer"), None)
            if layer is None:
                continue
            ln = str(sx._value(layer[1]))
            pts: list[tuple[float, float]] = []
            for key in ("start", "end", "mid", "center"):
                for nd in sx.children(node, key):
                    pts.append((sx.num(nd[1]), sx.num(nd[2])))
            pts.extend(_pts_bounds(node))  # fp_poly / fp_curve
            acc(ln, pts)

    for pad in sx.children(root, "pad"):
        at = next(sx.children(pad, "at"), None)
        size = next(sx.children(pad, "size"), None)
        layers = next(sx.children(pad, "layers"), None)
        drill = next(sx.children(pad, "drill"), None)
        drill_v = None
        if drill is not None:
            # (drill 1) 或 (drill oval 1 2)——取首个数值
            for v in drill[1:]:
                if isinstance(v, (int, float)):
                    drill_v = float(v)
                    break
        fp.pads.append(PadGeom(
            number=str(sx._value(pad[1])),
            x=sx.num(at[1]) if at else 0.0,
            y=sx.num(at[2]) if at else 0.0,
            w=sx.num(size[1]) if size else 0.0,
            h=sx.num(size[2]) if size and len(size) > 2 else 0.0,
            angle=sx.num(at[3]) if at and len(at) > 3 else 0.0,
            kind=str(sx._value(pad[2])),
            layers=[str(sx._value(v)) for v in layers[1:]] if layers else [],
            drill=drill_v,
        ))

    fp.boxes = boxes
    return fp
