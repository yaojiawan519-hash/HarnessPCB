"""ingest 中间表示（IR）：模块 / 器件 / net 三张表。

IR 是后续所有阶段（floorplan/recipe/布线）的唯一输入，字段从简、
可 JSON 序列化，跨里程碑演进只加字段不改语义。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class PinRef:
    ref: str
    pin: str


@dataclass
class DeviceIR:
    ref: str
    value: str
    footprint: str  # "lib:name"，sch Footprint 字段
    lib_id: str
    module: str  # 模块名 = Sheetname 小写
    in_bom: bool
    dnp: bool
    at: tuple[float, float]  # sch 摆放位置（仅参考，PCB 布局另算）
    pins: dict[str, tuple[float, float]] = field(default_factory=dict)  # pin号 → sch 绝对坐标

    @property
    def footprint_lib(self) -> str:
        return self.footprint.split(":")[0] if ":" in self.footprint else ""


@dataclass
class NetIR:
    name: str
    pins: list[PinRef]
    modules: list[str]  # 去重排序
    kind: str  # "power" | "system" | "intra"

    def to_json(self) -> dict:
        d = asdict(self)
        d["modules"] = self.modules
        return d


@dataclass
class ModuleIR:
    name: str
    sheet_file: str
    devices: list[str]  # ref 列表（排序）


@dataclass
class DesignIR:
    root_sch: str
    modules: dict[str, ModuleIR]
    devices: dict[str, DeviceIR]  # ref → 器件
    nets: list[NetIR]
    warnings: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for n in self.nets:
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        return {
            "modules": len(self.modules),
            "devices": len(self.devices),
            "nets": len(self.nets),
            "nets_by_kind": dict(sorted(kinds.items())),
            "warnings": len(self.warnings),
        }
