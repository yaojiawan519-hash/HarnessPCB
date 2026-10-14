"""电源轨道视觉增强（可选，默认关闭）。

泛化自 rebuild_power_rails.mjs 的拓扑：同一器件同一电源网的 N 个引脚
不再各挂一个标签（gen 版做法，ERC 等效但视觉凌乱），改为
每引脚短桩 wire → 一条横轨 → 轨中央单个 net 标签——人类工程师画法。

坐标全部运行时从 pins() 取得（原脚本坐标冻结自历史迭代，不可复用）。
默认 off：轨道几何依赖引脚 angle 字段，因库而异；ERC 门禁不依赖本模块。
"""
from __future__ import annotations

from . import nets as N

STUB = 2.54  # mm，引脚端点向轨道的短桩长


def draw_rails(client, sch: str, conns: list[dict], log=lambda *a: None) -> int:
    """对 conns 中 multi=true 的电源类连接画轨道。返回画轨数（失败跳过）。"""
    drawn = 0
    for c in conns:
        if not (c.get("multi") and c.get("net")):
            continue
        ref = c["ref"]
        pl = N.pins(client, sch, ref)
        matched = N.match_pins(pl, c["pin"], multi=True,
                               exclude_substring=c.get("exclude_substring"))
        pts = []
        for p in matched:
            x, y = p.get("x"), p.get("y")
            if x is None or y is None:
                log(f"  轨道跳过 {ref}.{c['pin']}：无坐标")
                pts = []
                break
            pts.append((float(x), float(y)))
        if len(pts) < 2:
            continue
        # 轨道取所有引脚最外 y（引脚排布在同一侧）：短桩朝外 2.54
        ys = [y for _, y in pts]
        rail_y = max(ys) + STUB if max(ys) - min(ys) < 0.01 else None
        if rail_y is None:
            continue  # 引脚不在一条线上，保守跳过
        xs = sorted(x for x, _ in pts)
        ok = True
        for x, y in pts:
            r = client.tool_soft("add_schematic_wire",
                                 {"schematicPath": sch,
                                  "waypoints": [[x, y], [x, rail_y]], "snapToPins": True})
            ok = ok and bool(r)
        r = client.tool_soft("add_schematic_wire",
                             {"schematicPath": sch,
                              "waypoints": [[xs[0], rail_y], [xs[-1], rail_y]], "snapToPins": False})
        ok = ok and bool(r)
        mid = xs[0] + (xs[-1] - xs[0]) / 2
        r = client.tool_soft("add_schematic_net_label",
                             {"schematicPath": sch, "netName": c["net"],
                              "position": [round(mid, 2), rail_y], "orientation": 0})
        ok = ok and bool(r)
        if ok:
            drawn += 1
            log(f"  轨道 {ref} {c['net']} ×{len(pts)} 脚 @y={rail_y:.2f}")
    return drawn
