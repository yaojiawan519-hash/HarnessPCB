""".kicad_sch 读取层：层级展开 → 器件实例 / 线 / 标签 / 库 pin 几何。

只读不改。器件真值源 = sch（方案 §9 决策 5）。
坐标注意：sch 文件坐标 Y 轴向下，符号库 pin 坐标 Y 轴向上——
pin 绝对坐标换算见 pin_positions()。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr as sx

# 电源符号库前缀：实例即全局 net（net 名 = 其 Value）
POWER_LIB_PREFIX = "power:"


@dataclass(frozen=True)
class LibPin:
    """库符号 pin：符号局部坐标（Y 向上），angle = pin 线延伸方向。"""

    number: str
    name: str
    etype: str
    x: float
    y: float
    angle: float
    length: float


@dataclass
class SchSymbol:
    """放置的符号实例（含电源符号）。"""

    lib_id: str
    x: float
    y: float
    rot: float  # 度，视觉逆时针
    mirror: str  # "" | "x" | "y"
    reference: str  # 电源符号为 "#PWR..."，真实器件为 "R12" 等
    value: str
    footprint: str
    in_bom: bool
    dnp: bool
    uuid: str
    pin_numbers: list[str] = field(default_factory=list)
    sheet: str = ""  # 所属模块名（sheet 装配时回填）
    sheet_file: str = ""

    @property
    def is_power(self) -> bool:
        return self.lib_id.startswith(POWER_LIB_PREFIX) or self.reference.startswith("#PWR")


@dataclass(frozen=True)
class Wire:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class Label:
    """local label（页内）/ global label（跨页）/ 电源符号（全局）。"""

    name: str
    x: float
    y: float
    global_: bool


class Sheet:
    """一张 sch 的解析结果（根图或子图）。"""

    def __init__(self, path: Path):
        self.path = path
        doc = sx.loads(path.read_text())
        root = doc[0] if sx.tag(doc[0]) == "kicad_sch" else doc
        self.uuid: str = sx.attr(root, "uuid") or ""
        self.symbols: list[SchSymbol] = []
        self.wires: list[Wire] = []
        self.labels: list[Label] = []
        self.junctions: list[tuple[float, float]] = []
        self.lib_pins: dict[str, list[LibPin]] = {}  # lib_id → pins（跨 unit 聚合）
        self.subsheets: list[tuple[str, str]] = []  # (Sheetname, Sheetfile)
        self._read(root)

    # ---- 读取 -----------------------------------------------------------

    def _read(self, root: list) -> None:
        lib = next(sx.children(root, "lib_symbols"), None)
        if lib:
            for sym in sx.children(lib, "symbol"):
                self._read_lib_symbol(sym)
        for node in sx.children(root, "symbol"):
            self._read_symbol(node)
        for node in sx.children(root, "wire"):
            pts = next(sx.children(node, "pts"), [])
            xys = [kid for kid in pts if sx.tag(kid) == "xy"]
            for a, b in zip(xys, xys[1:]):
                self.wires.append(Wire(sx.num(a[1]), sx.num(a[2]), sx.num(b[1]), sx.num(b[2])))
        for tag_name, is_global in (("label", False), ("global_label", True)):
            for node in sx.children(root, tag_name):
                at = next(sx.children(node, "at"), None)
                self.labels.append(
                    Label(
                        name=str(sx._value(node[1])),
                        x=sx.num(at[1]) if at else 0.0,
                        y=sx.num(at[2]) if at else 0.0,
                        global_=is_global,
                    )
                )
        for node in sx.children(root, "junction"):
            at = next(sx.children(node, "at"), None)
            if at:
                self.junctions.append((sx.num(at[1]), sx.num(at[2])))
        for node in sx.children(root, "sheet"):
            name = file = ""
            for prop in sx.children(node, "property"):
                key = str(sx._value(prop[1]))
                if key == "Sheetname":
                    name = str(sx._value(prop[2]))
                elif key == "Sheetfile":
                    file = str(sx._value(prop[2]))
            if file:
                self.subsheets.append((name, file))

    def _read_lib_symbol(self, sym: list) -> None:
        """lib_symbols 条目：顶层 (symbol "NAME" ...) 含 pin（或各 unit 子块含 pin）。"""
        name = str(sx._value(sym[1]))
        pins: list[LibPin] = []
        units = [sym] + [kid for kid in sym[1:] if sx.tag(kid) == "symbol"]
        for unit in units:
            for pin in sx.children(unit, "pin"):
                at = next(sx.children(pin, "at"), None)
                if at is None:
                    continue
                pins.append(
                    LibPin(
                        number=str(sx._value(sx.attr(pin, "number"))),
                        name=str(sx._value(sx.attr(pin, "name"))),
                        etype=str(sx._value(sx.attr(pin, "type")) or ""),
                        x=sx.num(at[1]),
                        y=sx.num(at[2]),
                        angle=sx.num(at[3]) if len(at) > 3 else 0.0,
                        length=sx.num(sx.attr(pin, "length") or 0.0),
                    )
                )
        if pins:
            self.lib_pins[name] = pins

    def _read_symbol(self, node: list) -> None:
        at = next(sx.children(node, "at"), None)
        props: dict[str, str] = {}
        for prop in sx.children(node, "property"):
            props[str(sx._value(prop[1]))] = str(sx._value(prop[2]))
        mirror = ""
        for kid in node[1:]:
            if sx.tag(kid) == "mirror":
                mirror = str(sx._value(kid[1])) if len(kid) > 1 else "x"
        self.symbols.append(
            SchSymbol(
                lib_id=str(sx._value(sx.attr(node, "lib_id"))),
                x=sx.num(at[1]) if at else 0.0,
                y=sx.num(at[2]) if at else 0.0,
                rot=sx.num(at[3]) if at and len(at) > 3 else 0.0,
                mirror=mirror,
                reference=props.get("Reference", ""),
                value=props.get("Value", ""),
                footprint=props.get("Footprint", ""),
                in_bom=str(sx.attr(node, "in_bom") or "yes") != "no",
                dnp=str(sx.attr(node, "dnp") or "no") == "yes",
                uuid=str(sx.attr(node, "uuid") or ""),
                pin_numbers=[
                    str(sx._value(p[1])) for p in sx.children(node, "pin")
                ],
            )
        )

    # ---- pin 几何 ---------------------------------------------------------

    def pin_positions(self, sym: SchSymbol) -> dict[str, tuple[float, float]]:
        """实例 pin → sch 页面绝对坐标（mm，Y 向下）。

        库 pin 坐标 Y 向上，先翻 Y，再按视觉逆时针旋转（文件系即顺时针），
        镜像先于旋转施加。公式以「标签全落在节点上」（浮动标签=0）校准。
        """
        out: dict[str, tuple[float, float]] = {}
        import math

        rad = math.radians(-sym.rot)  # 文件坐标系（Y 下）里的旋转角
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        for pin in self.lib_pins.get(sym.lib_id, []):
            lx, ly = pin.x, pin.y
            if sym.mirror == "x":
                lx = -lx
            elif sym.mirror == "y":
                ly = -ly
            fx, fy = lx, -ly  # Y 上 → Y 下
            out[pin.number] = (
                round(sym.x + fx * cos_r - fy * sin_r, 4),
                round(sym.y + fx * sin_r + fy * cos_r, 4),
            )
        return out


class Design:
    """层级设计：根图 + 全部子图，递归展开（本工程两层，够用）。"""

    def __init__(self, root_sch: Path):
        self.root_sch = root_sch
        self.root = Sheet(root_sch)
        self.sheets: dict[str, Sheet] = {"root": self.root}
        # 模块名（规范化小写）→ Sheet；Sheetname = 模块名（方案 §4）
        self.modules: dict[str, Sheet] = {}
        for name, file in self.root.subsheets:
            sheet = Sheet(root_sch.parent / file)
            self.modules[name.lower()] = sheet
            self.sheets[name.lower()] = sheet

    def all_symbols(self) -> list[SchSymbol]:
        """全部实例（真实器件 + 电源符号），sheet 字段已回填模块名。"""
        out: list[SchSymbol] = []
        for mod, sheet in self.modules.items():
            for sym in sheet.symbols:
                sym.sheet = mod
                sym.sheet_file = sheet.path.name
                out.append(sym)
        return out
