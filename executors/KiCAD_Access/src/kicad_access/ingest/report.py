"""IR 序列化与人读报告（M1-5）。

JSON 是 M2（布局）的机器输入；终端报告给人看：
模块表 / 三类 net / 缺陷级警告（⚠ 全列，孤立 pin 聚合）。
"""

from __future__ import annotations

from dataclasses import asdict

from .crosscheck import crosscheck
from .ir import DesignIR


def to_json_dict(ir: DesignIR) -> dict:
    return asdict(ir)


def render_report(ir: DesignIR, pcb_lines: list[str] | None = None) -> str:
    st = ir.stats()
    lines: list[str] = []
    lines.append(f"kca ingest — {ir.root_sch}")
    lines.append("=" * 64)
    lines.append(
        f"模块 {st['modules']} · 器件 {st['devices']} · net {st['nets']}"
        f"（power {st['nets_by_kind'].get('power', 0)}"
        f" · system {st['nets_by_kind'].get('system', 0)}"
        f" · intra {st['nets_by_kind'].get('intra', 0)}）"
    )

    lines.append("")
    lines.append("模块表:")
    for m in sorted(ir.modules.values(), key=lambda m: -len(m.devices)):
        lines.append(f"  {m.name:<10} {m.sheet_file:<18} {len(m.devices)} 器件")

    lines.append("")
    lines.append("电源 net:")
    for n in ir.nets:
        if n.kind == "power":
            lines.append(f"  {n.name:<14} {len(n.pins):>3} pin  {'+'.join(n.modules)}")

    lines.append("")
    lines.append("系统 net（跨模块信号）:")
    for n in ir.nets:
        if n.kind == "system":
            lines.append(f"  {n.name:<18} {' ↔ '.join(n.modules):<24} {len(n.pins)} pin")

    if pcb_lines:
        lines.append("")
        lines.append("PCB 交叉核对（--pcb）:")
        lines.extend(f"  {ln}" for ln in pcb_lines)

    defects = [w for w in ir.warnings if w.startswith("⚠")]
    isolated = [w for w in ir.warnings if "未接线" in w]
    lines.append("")
    lines.append(f"缺陷级警告（{len(defects)}）:")
    lines.extend(f"  {w}" for w in defects) if defects else lines.append("  无")
    lines.append(f"未接线 pin（{len(isolated)}，多为悬空/NC，聚合显示）:")
    if isolated:
        by_mod: dict[str, int] = {}
        for w in isolated:
            mod = w.split("]")[0].lstrip("[")
            by_mod[mod] = by_mod.get(mod, 0) + 1
        lines.append("  " + " · ".join(f"{m} {c}" for m, c in sorted(by_mod.items())))
    else:
        lines.append("  无")
    return "\n".join(lines)
