""".kicad_pcb 读取层：footprint 实例（ref/value/封装/位置/pad-net）。

只读不改。用途：M1-3 器件交叉核对（sch 真值 vs 板上实例）、
M2 布局 anchoring（连接器/安装孔位置从板继承）。
KiCad 10 格式：net 按名声明（无数字 net code）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr as sx


@dataclass
class Footprint:
    name: str  # "lib:name"
    reference: str
    value: str
    x: float
    y: float
    rot: float
    layer: str  # "F.Cu" | "B.Cu"
    pad_nets: dict[str, str] = field(default_factory=dict)  # pad 号 → net 名


class Board:
    """一块 PCB 的解析结果。"""

    def __init__(self, path: Path):
        self.path = path
        doc = sx.loads(path.read_text())
        root = doc[0] if sx.tag(doc[0]) == "kicad_pcb" else doc
        self.footprints: list[Footprint] = []
        self._read(root)

    def _read(self, root: list) -> None:
        for node in sx.children(root, "footprint"):
            props: dict[str, str] = {}
            for prop in sx.children(node, "property"):
                props[str(sx._value(prop[1]))] = str(sx._value(prop[2]))
            at = next(sx.children(node, "at"), None)
            layer = next(sx.children(node, "layer"), None)
            fp = Footprint(
                name=str(sx._value(node[1])),
                reference=props.get("Reference", ""),
                value=props.get("Value", ""),
                x=sx.num(at[1]) if at else 0.0,
                y=sx.num(at[2]) if at else 0.0,
                rot=sx.num(at[3]) if at and len(at) > 3 else 0.0,
                layer=str(sx._value(layer[1])) if layer else "F.Cu",
            )
            for pad in sx.children(node, "pad"):
                net = next(sx.children(pad, "net"), None)
                if net is not None:
                    # (net "名") 或旧式 (net 5 "名")
                    nm = net[-1] if len(net) > 2 else net[1]
                    fp.pad_nets[str(sx._value(pad[1]))] = str(sx._value(nm))
            self.footprints.append(fp)

    def by_ref(self) -> dict[str, Footprint]:
        return {f.reference: f for f in self.footprints}
