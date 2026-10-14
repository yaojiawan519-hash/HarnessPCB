"""floorplan 结果的 SVG 预览（M2-2b）：手工拼 SVG，零第三方依赖。

一块颜色一个模块：ctyd 矩形 + 位号；红虚线 = core（契约 C 硬核）；
红点 = 对外网络出口（契约 B）；板框 + 安装孔 + 图例（coverage / 各
sheet 的 recipe·frame）。人在环验收用——放置质量一眼可判。
"""

from __future__ import annotations

from ..kicad.footprint import FootprintGeom
from .floorplan import FloorplanResult

SCALE = 10.0  # px / mm
MARGIN_MM = 6.0
COLORS = ("#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2")
LINE_H = 18  # 图例行高（px）


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(result: FloorplanResult, geoms: dict[str, FootprintGeom]) -> str:
    W, H = result.spec.size
    ox = oy = MARGIN_MM * SCALE  # 板原点像素偏移
    svg_w = (W + 2 * MARGIN_MM) * SCALE
    svg_h = oy + H * SCALE + 16 + (1 + len(result.sheets)) * LINE_H + 8

    def X(x: float) -> float:
        return ox + x * SCALE

    def Y(y: float) -> float:
        return oy + y * SCALE

    e: list[str] = []
    e.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w:.0f}" '
        f'height="{svg_h:.0f}" viewBox="0 0 {svg_w:.0f} {svg_h:.0f}">'
    )
    e.append('<rect width="100%" height="100%" fill="#ffffff"/>')

    # 板框 + 安装孔（参考板无孔、新版 4 角 M3——虚线示意）
    e.append(
        f'<rect x="{ox:.1f}" y="{oy:.1f}" width="{W * SCALE:.1f}" '
        f'height="{H * SCALE:.1f}" fill="#fafafa" stroke="#333" stroke-width="1.5"/>'
    )
    for h in result.spec.mount_holes:
        e.append(
            f'<circle cx="{X(h.pos[0]):.1f}" cy="{Y(h.pos[1]):.1f}" '
            f'r="{h.size / 2 * SCALE:.1f}" fill="none" stroke="#999" stroke-dasharray="3 2"/>'
        )

    # 各模块：ctyd 矩形 + 位号 + core + 出口
    mod_color: dict[str, str] = {}
    for i, (sheet, sp) in enumerate(result.sheets.items()):
        color = COLORS[i % len(COLORS)]
        mod_color[sheet] = color
        for ref, p in sp.placements.items():
            g = geoms.get(ref)
            if g is None:
                continue
            x1, y1, x2, y2 = g.bbox_at(p.x, p.y, p.rot)
            e.append(
                f'<rect x="{X(x1):.1f}" y="{Y(y1):.1f}" width="{(x2 - x1) * SCALE:.1f}" '
                f'height="{(y2 - y1) * SCALE:.1f}" fill="{color}" fill-opacity="0.55" '
                f'stroke="{color}"/>'
            )
            e.append(
                f'<text x="{X((x1 + x2) / 2):.1f}" y="{Y((y1 + y2) / 2) + 3:.1f}" '
                f'font-size="8" text-anchor="middle" fill="#222">{_esc(ref)}</text>'
            )
        if sp.core is not None:
            x1, y1, x2, y2 = sp.core
            e.append(
                f'<rect x="{X(x1):.1f}" y="{Y(y1):.1f}" width="{(x2 - x1) * SCALE:.1f}" '
                f'height="{(y2 - y1) * SCALE:.1f}" fill="none" stroke="#d62728" '
                f'stroke-dasharray="4 3"/>'
            )
        for cands in sp.exits.values():
            for c in cands:
                e.append(f'<circle cx="{X(c.x):.1f}" cy="{Y(c.y):.1f}" r="2" fill="#d62728"/>')

    # 图例：coverage + 每 sheet 摘要
    n, total = result.coverage
    ly = oy + H * SCALE + 16
    e.append(
        f'<text x="{ox:.1f}" y="{ly:.1f}" font-size="12" fill="#111">'
        f"placed {n}/{total} 器件</text>"
    )
    for sheet, sp in result.sheets.items():
        ly += LINE_H
        recipes = "+".join(en.recipe for en in sp.entries if en.recipe) or "力导向兜底"
        e.append(
            f'<rect x="{ox:.1f}" y="{ly - 9:.1f}" width="10" height="10" '
            f'fill="{mod_color[sheet]}"/>'
            f'<text x="{ox + 16:.1f}" y="{ly:.1f}" font-size="11" fill="#333">'
            f"{_esc(sheet)} · {_esc(recipes)} · "
            f"origin({sp.frame.origin[0]:.1f},{sp.frame.origin[1]:.1f}) "
            f"rot {sp.frame.rot:.0f}° · {len(sp.placements)} 器件</text>"
        )
    e.append("</svg>")
    return "\n".join(e)
