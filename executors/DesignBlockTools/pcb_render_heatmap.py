#!/usr/bin/env python3
"""布局挣扎热力图渲染器（2026-11-17）。

引擎压力测试 report.json 的 struggle_heat + zone_attribution → SVG：
热斑（半径/透明度∝罚值）+ 元件 courtyard 真实轮廓 + 位号 + 板框。
热区一眼定位到件——布局复盘的看图入口。

用法（须 KiCad 内置 Python，读 courtyard 用 pcbnew）:
  KPY pcb_render_heatmap.py <report.json> <board.kicad_pcb> [out.svg]

层色：F.Cu=红，B.Cu=蓝，In2.Cu=绿，In1.Cu=金。罚值越高越实。
"""
from __future__ import annotations

import json
import math
import sys

import pcbnew

NM = 1e6
LAYER_COLOR = {"F.Cu": "#c0392b", "B.Cu": "#2471a3",
               "In2.Cu": "#1e8449", "In1.Cu": "#7d6608"}


def courtyard_rect(fp):
    minx = miny = 1e9
    maxx = maxy = -1e9
    for gi in fp.GraphicalItems():
        if gi.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            g = gi.GetBoundingBox()
            minx = min(minx, g.GetX())
            maxx = max(maxx, g.GetRight())
            miny = min(miny, g.GetY())
            maxy = max(maxy, g.GetBottom())
    if minx > 1e8:
        p = fp.GetPosition()
        return (p.x, p.y, p.x, p.y)
    return (minx, miny, maxx, maxy)


def main():
    rpt, board, out = sys.argv[1], sys.argv[2], \
        (sys.argv[3] if len(sys.argv) > 3 else "热力图.svg")
    d = json.load(open(rpt))
    heat = d["process"]["struggle_heat"]
    zone = {tuple(h[:3]): [n for n, _ in h[3]]
            for h in d["process"].get("zone_attribution", []) if h[3]}

    B = pcbnew.LoadBoard(board)
    fps = []
    for fp in B.GetFootprints():
        x0, y0, x1, y1 = courtyard_rect(fp)
        fps.append((fp.GetReference(), x0 / NM, y0 / NM, x1 / NM, y1 / NM))
    edge = []
    for dr in B.GetDrawings():
        if dr.GetClass() == "PCB_SHAPE" and dr.GetLayer() == pcbnew.Edge_Cuts:
            bb = dr.GetBoundingBox()
            edge += [(bb.GetX() / NM, bb.GetY() / NM),
                     (bb.GetRight() / NM, bb.GetBottom() / NM)]

    xs = [p[0] for p in edge] or [h[0] for h in heat]
    ys = [p[1] for p in edge] or [h[1] for h in heat]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    pad = 3.0
    w, h = maxx - minx + 2 * pad, maxy - miny + 2 * pad
    SCALE = 1400 / w

    def tx(x):
        return (x - minx + pad) * SCALE

    def ty(y):
        return (y - miny + pad) * SCALE

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
         f'{int(w * SCALE)} {int(h * SCALE)}" font-family="Menlo,monospace">',
         f'<rect width="{int(w * SCALE)}" height="{int(h * SCALE)}" fill="#0d1117"/>']

    # 板框
    pts = " ".join(f"{tx(x):.1f},{ty(y):.1f}" for x, y in edge)
    s.append(f'<polyline points="{pts}" fill="none" stroke="#444c56" stroke-width="1.5"/>')

    # 元件 courtyard 轮廓 + 位号
    for ref, x0, y0, x1, y1 in fps:
        s.append(f'<rect x="{tx(x0):.1f}" y="{ty(y0):.1f}" '
                 f'width="{max(0.4,(x1-x0)*SCALE):.1f}" '
                 f'height="{max(0.4,(y1-y0)*SCALE):.1f}" '
                 f'fill="none" stroke="#3a434e" stroke-width="0.5"/>')
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        s.append(f'<text x="{tx(cx):.1f}" y="{ty(cy)+2.5:.1f}" font-size="6" '
                 f'fill="#7d8790" text-anchor="middle">{ref}</text>')

    # 热斑
    for x, y, layer, pen in heat:
        col = LAYER_COLOR.get(layer, "#999")
        r = max(4, 11 * math.sqrt(pen / 0.6)) if pen > 0 else 4
        op = min(0.9, 0.25 + pen / 0.8)
        s.append(f'<circle cx="{tx(x):.1f}" cy="{ty(y):.1f}" r="{r:.1f}" '
                 f'fill="{col}" fill-opacity="{op:.2f}"/>')
        key = (x, y, layer)
        if key in zone and pen >= 0.4:
            lbl = ",".join(n.split("/")[-1][:9] for n in zone[key][:2])
            s.append(f'<text x="{tx(x)+r+2:.1f}" y="{ty(y)-r:.1f}" font-size="6.5" '
                     f'fill="#e6edf3">{lbl}</text>')

    # 图例
    ly = 16
    items = list(LAYER_COLOR.items())
    for i, (lyr, col) in enumerate(items):
        lx = int(w * SCALE) - 40 * len(items) + i * 40
        s.append(f'<circle cx="{lx}" cy="{ly}" r="6" fill="{col}"/>')
        s.append(f'<text x="{lx+10}" y="{ly+3}" font-size="9" fill="#e6edf3">{lyr}</text>')
    s.append(f'<text x="12" y="{ly+3}" font-size="9" fill="#8b949e">'
             f'挣扎热力图 · {len(heat)} 热格 · {len(fps)} 元件</text>')
    s.append("</svg>")

    open(out, "w").write("\n".join(s))
    print(f"已渲染 → {out}（{len(heat)} 热格，{len(fps)} 元件，"
          f"板 {w:.1f}x{h:.1f}mm）")


if __name__ == "__main__":
    main()
