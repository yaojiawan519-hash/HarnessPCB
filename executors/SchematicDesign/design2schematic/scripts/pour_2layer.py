#!/usr/bin/env python3.9
"""双层板 GND 铺铜（KiCad 自带 Python / pcbnew swig，无第三方依赖）。

收编 2026-09-22 会话在两块板（60×60 用户板 / 100×100 语料板
pic_programmer）上验证过的参数：inset 0.475mm / 间隙 0.2mm / 最小颈 0.254mm /
THERMAL 十字花（gap 0.2 / spoke 0.5）+ 指定器件实连（starved_thermal 修复）。

zone 建立后不在此处 fill——铺铜的 refill 与 DRC 终判由 d2s chain 统一用
kicad-cli drc --refill-zones --save-board 在隔离副本上完成（防止覆写板文件）。

用法（KPY = KiCad.app Frameworks 下的 python3.9）:
  KPY pour_2layer.py --board placed.kicad_pcb --out poured.kicad_pcb \
      [--gnd GND] [--full-pads J1,J2] [--inset-mm 0.475]
"""
from __future__ import annotations

import argparse
import sys

try:  # KiCad 的 wx 依赖：无显示环境也需 App 守卫
    import wx
    wx.App(False)
except Exception:
    pass

import pcbnew  # noqa: E402  (必须在 wx 之后)

NM = 1_000_000  # mm → nm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gnd", default="GND")
    ap.add_argument("--inset-mm", type=float, default=0.475)
    ap.add_argument("--clearance-mm", type=float, default=0.2)
    ap.add_argument("--min-width-mm", type=float, default=0.254)
    ap.add_argument("--thermal-gap-mm", type=float, default=0.2)
    ap.add_argument("--spoke-mm", type=float, default=0.5)
    ap.add_argument("--full-pads", default="", help="逗号分隔 ref：焊盘改实连（starved_thermal 修复）")
    args = ap.parse_args()

    pcb = pcbnew.LoadBoard(args.board)
    net = pcb.FindNet(args.gnd)
    if net is None or net.GetNetCode() == 0:
        print(f"✗ 找不到网络 {args.gnd}")
        return 1

    bbox = pcb.ComputeBoundingBox()
    x0, y0 = bbox.GetPosition().x, bbox.GetPosition().y
    w, h = bbox.GetWidth(), bbox.GetHeight()
    ins = int(args.inset_mm * NM)
    pts = [
        (x0 + ins, y0 + ins),
        (x0 + w - ins, y0 + ins),
        (x0 + w - ins, y0 + h - ins),
        (x0 + ins, y0 + h - ins),
    ]

    made = 0
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = pcbnew.ZONE(pcb)
        z.SetLayer(layer)
        z.SetNetCode(net.GetNetCode())
        z.SetMinThickness(int(args.min_width_mm * NM))
        z.SetLocalClearance(int(args.clearance_mm * NM))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        z.SetThermalReliefGap(int(args.thermal_gap_mm * NM))
        z.SetThermalReliefSpokeWidth(int(args.spoke_mm * NM))
        z.SetIsFilled(False)
        outline = z.Outline()
        outline.NewOutline()
        for (x, y) in pts:
            outline.Append(pcbnew.VECTOR2I(x, y))
        pcb.Add(z)
        made += 1

    fixed = 0
    for ref in [r.strip() for r in args.full_pads.split(",") if r.strip()]:
        fp = pcb.FindFootprintByReference(ref)
        if fp is None:
            print(f"⚠ full-pads 引用不存在: {ref}")
            continue
        for pad in fp.Pads():
            pad.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)
            fixed += 1

    pcb.Save(args.out)
    print(f"✓ {made} zones (F.Cu+B.Cu) net={args.gnd} inset={args.inset_mm}mm "
          f"full-pads 焊盘 {fixed} 个 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
