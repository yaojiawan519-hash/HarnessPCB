"""未用引脚自动 no-connect。泛化自 finalize_schematic.mjs 的 KEEP 集逻辑：
原脚本硬编码 U1 的 KEEP 名单；这里 = conns 已连引脚 ∪ keep_connected（设计表），
其余未用引脚自动 NC，ERC 因此不再报 "Pin not connected"。"""
from __future__ import annotations

from . import nets as N


def apply(client, sch: str, design: dict, used: dict[str, set], log=lambda *a: None) -> int:
    """used: nets.connect_all/place_pwr_flags 返回的 {ref: set(norm 引脚名)}。
    设计表 no_connect[].pin_prefix 声明的引脚（如 U1 的 OSC32）同样落入
    "未连且不在 keep" → 自动 NC，无需单独处理。"""
    keep = {N.norm(k) for k in design.get("keep_connected", [])}
    nc_list = []
    for p in design["parts"]:
        ref = p["ref"]
        pl = N.pins(client, sch, ref)
        if not pl:
            continue
        for pin in pl:
            n = N.norm(pin.get("pin_name", ""))
            if n in used.get(ref, set()):
                continue  # 已连
            if n in keep:
                continue  # 显式保留（虽然没连，也别 NC——交给工程师）
            nc_list.append({"componentRef": ref, "pinName": pin.get("pin_name")})
    if nc_list:
        client.tool("batch_add_no_connects", {"schematicPath": sch, "pins": nc_list})
        log(f"  no-connect {len(nc_list)} 引脚")
    return len(nc_list)
