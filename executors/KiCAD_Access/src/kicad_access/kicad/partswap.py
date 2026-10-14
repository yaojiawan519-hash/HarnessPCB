"""spec.part_overrides：同一原理图的封装变体（BGA→eLQFP 等）内存换装。

器件真值源 = sch（方案 §9 决策 5）——换装**不写回任何文件**，只改内存
Design：封装字符串 + 库 pin 编号（按 pin 名对齐重编号）。连线语义不动：
pin 坐标原样保留，netbuild 按位置吸附、编号随行，网表逐网连通性与原板
一致，只有该器件的 pad 编号变成新封装的盘号。

spec 形态：

    part_overrides:
      U1:
        footprint: HPM_footprints:eLQFP100L_14x14_P0.5mm_EP5x5mm
        pinmap_from: ../../KiCAD_Projects/hpm5e_pins_bga.json   # 现号(球号)+名
        pinmap_to: ../../KiCAD_Projects/hpm5e_pins.json         # 目标号(数字)+名
        aliases: {VSS: EP, VSSA: VSSA/VREFL, VREFL: VSSA/VREFL}
        drop_unmapped: true    # 名字对不上 = 目标封装无此脚，丢弃（记日志）

多球并一脚（16×VSS→EP）按同名分组顺序配对去重——同名多球↔同名多脚
1:1 全配对（VDD_SOC 5↔5），源盈余并脚丢弃（同轨同网电气等价），日志
逐条可查。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ..spec import PartOverride
from .schematic import Design, LibPin


def _load_num2name(p: Path) -> dict[str, str]:
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(e["num"]): str(e["name"]) for e in data}


def _load_name2nums(p: Path) -> dict[str, list[str]]:
    """目标表 name → [num,…]（保序，重名脚全收：VDD_SOC×2 等）。"""
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for e in data:
        out.setdefault(str(e["name"]), []).append(str(e["num"]))
    return out


def apply_part_overrides(design: Design,
                         overrides: dict[str, PartOverride]) -> list[str]:
    """按 spec 覆盖表改内存 Design；返回日志行（调用方打印）。违规抛 ValueError。"""
    log: list[str] = []
    for ref, ov in overrides.items():
        syms = [s for s in design.all_symbols() if s.reference == ref
                and not s.is_power]
        if not syms:
            raise ValueError(f"part_overrides.{ref}: sch 中无此器件")
        if ov.footprint:
            for sym in syms:
                sym.footprint = ov.footprint
            log.append(f"{ref} 封装 {syms[0].value} → {ov.footprint}")
        if not (ov.pinmap_from and ov.pinmap_to):
            continue

        num2name = _load_num2name(ov.pinmap_from)
        name2nums = _load_name2nums(ov.pinmap_to)
        # 库 pin 表挂在符号所在 sheet（多 unit 实例共享 lib_id）
        sheet = design.sheets.get(syms[0].sheet) or design.root
        old = sheet.lib_pins.get(syms[0].lib_id)
        if not old:
            raise ValueError(
                f"part_overrides.{ref}: 库符号 {syms[0].lib_id} 无 pin 表")

        # 同名分组顺序配对：同名多球 ↔ 同名多脚 1:1（VDD_SOC 5球↔5脚 全保，
        # 每脚都有网）；源盈余 = 并脚丢弃（同轨同网等价）；目标盈余 = 无源，弃。
        cur: dict[str, int] = {}
        remap: dict[str, str] = {}   # 旧号 → 新号（实例 pin 过滤表同步用）
        newp: list[LibPin] = []
        dropped: list[str] = []
        for pin in old:
            name = num2name.get(pin.number)
            if name is None:
                raise ValueError(
                    f"part_overrides.{ref}: pin {pin.number} 不在 pinmap_from")
            key = ov.aliases.get(name, name)
            nums = name2nums.get(key, ())
            i = cur.get(key, 0)
            if i >= len(nums):
                if nums:   # 目标有此名但脚已用尽：多球并一脚，同网等价
                    dropped.append(f"{pin.number}({name})→{key} 并脚")
                elif not ov.drop_unmapped:
                    raise ValueError(
                        f"part_overrides.{ref}: pin {pin.number}({name}) "
                        f"无目标且 drop_unmapped=false")
                else:
                    dropped.append(f"{pin.number}({name})")
                continue
            tgt = nums[i]
            cur[key] = i + 1
            remap[pin.number] = tgt
            newp.append(replace(pin, number=tgt))
        sheet.lib_pins[syms[0].lib_id] = newp
        for sym in syms:       # 多 unit 实例的 pin 过滤表同步重编号
            if sym.pin_numbers:
                sym.pin_numbers = [remap[n] for n in sym.pin_numbers
                                   if n in remap]
        log.append(f"{ref} pin 重编号 {len(newp)}/{len(old)}"
                   f"（丢 {len(dropped)}: {' '.join(dropped) or '无'}）")
    return log
