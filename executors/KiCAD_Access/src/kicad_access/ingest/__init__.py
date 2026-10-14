"""ingest：sch 层级解析 → 模块/器件/net 三表 IR（方案 §7 M1）。"""

from .ir import DesignIR, DeviceIR, ModuleIR, NetIR, PinRef
from .netbuild import build_ir

__all__ = ["DesignIR", "DeviceIR", "ModuleIR", "NetIR", "PinRef", "build_ir"]
