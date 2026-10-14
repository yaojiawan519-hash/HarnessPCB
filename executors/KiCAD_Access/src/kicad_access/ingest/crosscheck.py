"""M1-3 器件交叉核对：sch 真值（IR）vs PCB 板上实例。

核对三件事：
1. ref 集合：sch 有板无 / 板有 sch 无（器件数 N=N）；
2. 封装指派：sch Footprint 字段 vs 板上 footprint lib:name（sch 是真值源，方案 §9 决策 5）；
3. pad-net 抽查（信息级）：板上 pad 引用的 net 名是否都在 IR net 名空间里
   ——板由旧版 sch 同步时会有出入，报差异不判失败。
"""

from __future__ import annotations

from ..kicad.pcb import Board
from .ir import DesignIR


def crosscheck(ir: DesignIR, board: Board) -> list[str]:
    lines: list[str] = []
    pcb = board.by_ref()
    sch_refs = set(ir.devices)
    pcb_refs = set(pcb)

    lines.append(f"sch 器件 {len(sch_refs)} | PCB footprint {len(pcb_refs)}")
    only_sch = sorted(sch_refs - pcb_refs)
    only_pcb = sorted(pcb_refs - sch_refs)
    lines.append(f"sch 有板无: {only_sch or '—'}")
    lines.append(f"板有 sch 无: {only_pcb or '—'}")
    lines.append(f"器件数一致: {'✅' if sch_refs == pcb_refs else '❌'}")

    mismatch = [
        (r, ir.devices[r].footprint, pcb[r].name)
        for r in sorted(sch_refs & pcb_refs)
        if ir.devices[r].footprint and ir.devices[r].footprint != pcb[r].name
    ]
    no_fp = [r for r in sorted(sch_refs & pcb_refs) if not ir.devices[r].footprint]
    lines.append(f"封装指派不一致: {len(mismatch)}")
    for r, s, p in mismatch[:10]:
        lines.append(f"  {r}: sch={s} 板={p}")
    for r, s, p in mismatch[10:]:
        lines.append(f"  {r}: sch={s} 板={p}")
    if no_fp:
        lines.append(f"sch 无 Footprint 字段: {sorted(no_fp)}")

    ir_nets = {n.name for n in ir.nets}
    unknown: dict[str, list[str]] = {}
    for fp in board.footprints:
        for pad, net in fp.pad_nets.items():
            if net not in ir_nets:
                unknown.setdefault(net, []).append(f"{fp.reference}.{pad}")
    lines.append(f"板引用而 IR 无的 net 名: {len(unknown)}（板为旧版同步时属预期）")
    for net in sorted(unknown)[:10]:
        lines.append(f"  {net}: {unknown[net][:6]}")
    return lines
