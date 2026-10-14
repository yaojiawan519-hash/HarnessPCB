"""parts 表 → MCP 放件。提炼自 gen_schematic.mjs 第 2 步。"""
from __future__ import annotations


def place_all(client, sch: str, parts: list[dict], log=lambda *a: None) -> int:
    """按设计表放置全部器件；封装名不合法时降级重试（原脚本同策略）。
    放件失败时异常带上 ref/lib 上下文——MCP 原始报错只有服务器视角，
    对不上设计表哪条 part。"""
    n = 0
    for p in parts:
        args = {
            "schematicPath": sch,
            "symbol": p["lib"],
            "reference": p["ref"],
            "value": p.get("value", ""),
            "position": {"x": p["x"], "y": p["y"]},
        }
        if p.get("footprint"):
            args["footprint"] = p["footprint"]
        try:
            client.tool("add_schematic_component", args)
            n += 1
        except Exception:
            args.pop("footprint", None)
            log(f"  重试 {p['ref']} 不带封装")
            try:
                client.tool("add_schematic_component", args)
                n += 1
            except Exception as e:
                raise RuntimeError(f"放置 {p['ref']} ({p['lib']}) 失败: {e}") from e
    return n
