"""conns 表 → 网络连接 + PWR_FLAG。

策略（从原 gen→fix 链的试错中固化）：
- 连接统一走 add_schematic_net_label(componentRef, pinNumber=真引脚号)，
  不用 connect_to_net 的引脚名匹配——ST/连接器库引脚名带 ~、别名（D-/DM）时
  名字匹配会静默失败（原链靠 sch_fix 系列返工）。
- 引脚名先 norm() 归一（去 ~{}\\/\\s、大写），支持 pin_aliases 回退、
  multi 多实例（VDD×3/VSS×2/OSC 对）按引脚号逐一贴标签。
- pin:null 在设计表中即 no-connect 声明（如 J1 的 ID），由 auto_nc 处理。
"""
from __future__ import annotations

import json
import re


def norm(s: str) -> str:
    return re.sub(r"[~{}\\/\s]", "", str(s)).upper()


# ---------- 引脚发现 ----------
def pins(client, sch: str, ref: str) -> list[dict]:
    """get_schematic_pin_locations → [{pin_number, pin_name, x, y, ...}]。
    兼容 JSON 与文本表格两种返回（原脚本同款双路径）。"""
    t = client.tool(
        "get_schematic_pin_locations",
        {"schematicPath": sch, "reference": ref},
        check=False,
    )
    try:
        j = json.loads(t)
        arr = j.get("pins") or j.get("pin_locations") or (j if isinstance(j, list) else [])
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            return arr
    except (ValueError, TypeError):
        pass
    # 文本格式：finalize_schematic.mjs 验证过的正则 "Pin 12 (PA9):"
    # ⚠ 引脚号不全是数字——J1 的 Shield 脚号是 "SH"（Pin SH (Shield)），
    # \d+ 会静默漏掉它 → 既连不上也不被 auto_nc 兜底，ERC 报 Pin not connected。
    out = []
    for m in re.finditer(r"Pin\s+([A-Za-z0-9]+)\s*\((.+?)\)", t):
        out.append({"pin_number": m.group(1), "pin_name": m.group(2)})
    return out


def match_pins(pin_list: list[dict], key: str, aliases: tuple = (), multi: bool = False,
               exclude_substring: str | None = None) -> list[dict]:
    """归一名精确匹配 → 包含匹配 → 别名 → 引脚号。multi 时返回全部同名引脚。"""
    k = norm(key)

    def candidate(p: dict) -> bool:
        if exclude_substring and exclude_substring.upper() in norm(p.get("pin_name", "")):
            return False
        return True

    cands = [p for p in pin_list if candidate(p)]
    exact = [p for p in cands if norm(p.get("pin_name", "")) == k]
    if multi:
        if exact:
            return exact
        return [p for p in cands if norm(p.get("pin_name", "")).find(k) >= 0] or \
               [p for p in cands if norm(p.get("pin_number", "")) == k]
    if exact:
        return exact[:1]
    inc = [p for p in cands if k in norm(p.get("pin_name", ""))]
    if inc:
        return inc[:1]
    for a in aliases:
        ka = norm(a)
        m = [p for p in cands if norm(p.get("pin_name", "")) == ka] or \
            [p for p in cands if ka in norm(p.get("pin_name", ""))]
        if m:
            return m[:1]
    bynum = [p for p in cands if norm(p.get("pin_number", "")) == k]
    return bynum[:1]


# ---------- 主入口 ----------
def connect_all(client, sch: str, conns: list[dict], log=lambda *a: None) -> tuple[dict, dict]:
    """贴全部网络标签。返回 (used, resolved)：
    used     {ref: set(norm 引脚名)} —— 已连引脚，供 auto_nc；
    resolved {(ref, pin_number): net} —— 贴过的每枚标签的意图（写后回读对账的期望侧，
             MCP tool_soft 返回 ok ≠ 落地，以 export_netlist 读回为准）。"""
    pin_cache: dict[str, list[dict]] = {}
    used: dict[str, set[str]] = {}
    resolved: dict[tuple[str, str], str] = {}
    fails = []

    def get_pins(ref):
        if ref not in pin_cache:
            pin_cache[ref] = pins(client, sch, ref)
        return pin_cache[ref]

    for c in conns:
        net = c.get("net")
        if not net:
            continue  # null = no-connect 声明
        ref = c["ref"]
        pl = get_pins(ref)
        if not pl:
            fails.append(f"{ref}: 引脚查询为空")
            continue
        m = match_pins(pl, c["pin"], tuple(c.get("pin_aliases", ())),
                       multi=bool(c.get("multi")), exclude_substring=c.get("exclude_substring"))
        if not m:
            fails.append(f"{ref}.{c['pin']} 未找到引脚")
            continue
        for p in m:
            pno = str(p.get("pin_number") or p.get("number"))
            key = (ref, pno)
            if key in resolved and resolved[key] != net:
                raise RuntimeError(
                    f"设计表冲突: {ref}.{pno} 被两条 conn 指到不同网 "
                    f"({resolved[key]} / {net})——先修设计表"
                )
            client.tool_soft(
                "add_schematic_net_label",
                {
                    "schematicPath": sch,
                    "componentRef": ref,
                    "pinNumber": pno,
                    "netName": net,
                },
            )
            resolved[key] = net
            used.setdefault(ref, set()).add(norm(p.get("pin_name", "")))
    if fails:
        # 硬门禁：连接失败必须终止——否则 auto_nc 会把没连上的引脚 NC 掉，
        # 用 ERC 静默掩盖设计表错误（如引脚名对不上符号库）。
        raise RuntimeError(
            f"连接失败 {len(fails)} 条（设计表 pin 与符号库引脚对不上）:\n  "
            + "\n  ".join(fails)
        )
    return used, resolved


def place_pwr_flags(client, sch: str, pwr_flags: list[dict],
                    log=lambda *a: None) -> tuple[dict, dict]:
    """放置 PWR_FLAG 符号并贴到所属网。返回 (used, resolved)，与 connect_all 同构，
    resolved 供回读对账核对辅助件落网（PWR_FLAG 的脚必须在声明的网上）。
    经验（finalize + d2s 实测）：PWR_FLAG 本身是 power_out 脚——
    网上已有 power_out 驱动（J1.GND/J1.VBUS/LDO VO）时再加会报
    "電源輸出 和 電源輸出 的接腳已連接"。默认留空，ERC 报
    "power input not driven" 时才给对应网补。"""
    used: dict[str, set[str]] = {}
    resolved: dict[tuple[str, str], str] = {}
    for f in pwr_flags:
        ref = f.get("ref", f"F{len(used) + 1}")
        client.tool(
            "add_schematic_component",
            {
                "schematicPath": sch,
                "symbol": "power:PWR_FLAG",
                "reference": ref,
                "value": "PWR_FLAG",
                "position": {"x": f["x"], "y": f["y"]},
            },
        )
        pl = pins(client, sch, ref)
        if pl:
            pno = str(pl[0].get("pin_number") or pl[0].get("number"))
            client.tool_soft(
                "add_schematic_net_label",
                {
                    "schematicPath": sch,
                    "componentRef": ref,
                    "pinNumber": pno,
                    "netName": f["net"],
                },
            )
            resolved[(ref, pno)] = f["net"]
            used[ref] = {norm(pl[0].get("pin_name", ""))}
    return used, resolved
