#!/usr/bin/env python3
"""sdi.v0 + 符号库 -> SVG（真正的原理图重绘）"""
import sys, json, math, html
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import geom
from sexpr import H, find, one

COL = {'sym': '#840000', 'fill': '#fffcc2', 'pin': '#840000', 'wire': '#008400',
       'label': '#000000', 'global': '#a00000', 'hier': '#800080',
       'ref': '#008484', 'val': '#008484', 'power': '#840000', 'frame': '#c00000'}

def esc(t): return html.escape(str(t), quote=False)
def tf(x, y, sx, sy, rot, mirror=None):
    lx, ly = x, -y
    a = math.radians(rot or 0)
    xr = lx*math.cos(a) + ly*math.sin(a)
    yr = -lx*math.sin(a) + ly*math.cos(a)
    if mirror == 'x': yr = -yr
    if mirror == 'y': xr = -xr
    return (sx + xr, sy + yr)

lib_geometry = geom.lib_geometry          # 从 S 表达式库取图形（只给对照路径用）
collect_prims = geom.collect_prims

def arc_points(start, mid, end, n=24):
    ax, ay = start; bx, by = mid; cx, cy = end
    d = 2*(ax*(by-cy)+bx*(cy-ay)+cx*(ay-by))
    if abs(d) < 1e-9: return [start, end]
    ux = ((ax*ax+ay*ay)*(by-cy)+(bx*bx+by*by)*(cy-ay)+(cx*cx+cy*cy)*(ay-by))/d
    uy = ((ax*ax+ay*ay)*(cx-bx)+(bx*bx+by*by)*(ax-cx)+(cx*cx+cy*cy)*(bx-ax))/d
    r = math.hypot(ax-ux, ay-uy)
    a0 = math.atan2(ay-uy, ax-ux); a1 = math.atan2(by-uy, bx-ux); a2 = math.atan2(cy-uy, cx-ux)
    def norm(t): return t % (2*math.pi)
    # choose direction that passes through mid
    pts = []
    for i in range(n+1):
        t = a0 + (a2-a0)*i/n
        pts.append((ux+r*math.cos(t), uy+r*math.sin(t)))
    if not any(abs(math.atan2(p[1]-uy, p[0]-ux)-a1) < 0.2 for p in pts):
        pts = []
        d0 = (a2-a0) % (2*math.pi) - 2*math.pi
        for i in range(n+1):
            t = a0 + d0*i/n
            pts.append((ux+r*math.cos(t), uy+r*math.sin(t)))
    return pts

def draw_symbol(sym, prims, S):
    sx, sy, rot = (sym['at'] + [0,0,0])[:3]
    out = []
    def P(x, y): return tf(float(x), float(y), sx, sy, rot, sym.get('mirror'))
    for p in prims:
        h = H(p)
        if h == 'rectangle':
            st = one(p, 'start'); en = one(p, 'end')
            x1, y1 = P(st[1], st[2]); x2, y2 = P(en[1], en[2])
            fill = one(p, 'fill'); ft = (fill[1][1] if fill and isinstance(fill[1], list) else 'none')
            fc = COL['fill'] if ft in ('background', 'outline') else 'none'
            out.append(f'<rect x="{min(x1,x2):.2f}" y="{min(y1,y2):.2f}" width="{abs(x2-x1):.2f}" '
                       f'height="{abs(y2-y1):.2f}" fill="{fc}" stroke="{COL["sym"]}" stroke-width="0.2"/>')
        elif h == 'polyline':
            pts = [P(x[1], x[2]) for x in find(one(p, 'pts') or [], 'xy')]
            if pts:
                d = ' '.join(f'{x:.2f},{y:.2f}' for x, y in pts)
                out.append(f'<polyline points="{d}" fill="none" stroke="{COL["sym"]}" stroke-width="0.2"/>')
        elif h == 'circle':
            c = one(p, 'center'); r = one(p, 'radius')
            cx, cy = P(c[1], c[2])
            out.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{float(r[1]):.2f}" fill="none" '
                       f'stroke="{COL["sym"]}" stroke-width="0.2"/>')
        elif h == 'arc':
            st, mi, en = one(p, 'start'), one(p, 'mid'), one(p, 'end')
            if st and mi and en:
                pts = [P(x, y) for x, y in arc_points((float(st[1]), float(st[2])),
                                                      (float(mi[1]), float(mi[2])),
                                                      (float(en[1]), float(en[2])))]
                d = ' '.join(f'{x:.2f},{y:.2f}' for x, y in pts)
                out.append(f'<polyline points="{d}" fill="none" stroke="{COL["sym"]}" stroke-width="0.2"/>')
        elif h == 'pin':
            a = one(p, 'at'); ln = one(p, 'length'); num = one(p, 'number')
            if not a: continue
            x, y = float(a[1]), float(a[2]); ang = float(a[3]) if len(a) > 3 else 0
            L = float(ln[1]) if ln else 2.54
            ex, ey = x + L*math.cos(math.radians(ang)), y + L*math.sin(math.radians(ang))
            p1 = P(x, y); p2 = P(ex, ey)
            out.append(f'<line x1="{p1[0]:.2f}" y1="{p1[1]:.2f}" x2="{p2[0]:.2f}" y2="{p2[1]:.2f}" '
                       f'stroke="{COL["pin"]}" stroke-width="0.2"/>')
            if num and num[1] and len(num) > 1:
                mx, my = (p1[0]+p2[0])/2, (p1[1]+p2[1])/2
                out.append(f'<text x="{mx:.2f}" y="{my-0.4:.2f}" font-size="1.0" fill="#555" '
                           f'text-anchor="middle">{esc(num[1])}</text>')
    return out

def render(il, out, fit=False):
    # 图形全部来自 IL 自带的 libsym —— 渲染不再回头读 .kicad_sch
    geoms = {k: (v.get('prims') or []) for k, v in (il.get('libsym') or {}).items()}
    paper = (il.get('page') or {}).get('paper') or 'A4'
    W, Hh = {'A4': (297, 210), 'A3': (420, 297), 'A2': (594, 420),
              'A1': (841, 594), 'A0': (1189, 841)}.get(paper, (297, 210))
    body = [f'<rect x="0" y="0" width="{W}" height="{Hh}" fill="#fffdf5"/>']
    if not fit:
        body.append(f'<rect x="5" y="5" width="{W-10}" height="{Hh-10}" fill="none" stroke="{COL["frame"]}" stroke-width="0.35"/>')
    for w in il['wires']:
        d = ' '.join(f'{x:.2f},{y:.2f}' for x, y in w['pts'])
        body.append(f'<polyline points="{d}" fill="none" stroke="{COL["wire"]}" stroke-width="0.28"/>')
    for s in il['symbols']:
        prims = geoms.get(s['lib']) or []
        body += draw_symbol(s, prims, 1)
        for name, f in (s.get('fields') or {}).items():
            if name not in ('Reference', 'Value'): continue
            if name == 'Reference' and s.get('power'): continue
            if not f.get('at') or not f.get('text'): continue
            x, y, r = (f['at'] + [0])[:3]
            col = COL['ref'] if name in ('Reference', 'Value') else '#444'
            fs = 1.6 if name == 'Reference' else 1.4
            tr = f' transform="rotate({-r} {x:.2f} {y:.2f})"' if r else ''
            body.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{fs}" fill="{col}" '
                        f'text-anchor="middle"{tr}>{esc(f["text"])}</text>')
    for j in il['junctions']:
        if j: body.append(f'<circle cx="{j[0]:.2f}" cy="{j[1]:.2f}" r="0.45" fill="{COL["wire"]}"/>')
    for l in il['labels']:
        x, y, r = (l['at'] + [0])[:3]
        col = {'local': COL['label'], 'global': COL['global'], 'hier': COL['hier']}.get(l['kind'], '#000')
        # KiCad 的 4 个朝向：0 向右、180 向左（文字都正立），90 向上、270 向下（文字竖排）
        r = round(r or 0) % 360
        if r == 180:
            anchor, tr = 'end', ''
        elif r == 90:
            anchor, tr = 'start', f' transform="rotate(-90 {x:.2f} {y:.2f})"'
        elif r == 270:
            anchor, tr = 'end', f' transform="rotate(-90 {x:.2f} {y:.2f})"'
        else:
            anchor, tr = 'start', ''
        body.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="1.5" fill="{col}" '
                    f'text-anchor="{anchor}" dominant-baseline="middle"{tr}>{esc(l["text"])}</text>')
    for p in il['no_connects']:
        if p:
            x, y = p[0], p[1]
            body.append(f'<path d="M{x-0.7:.2f} {y-0.7:.2f}L{x+0.7:.2f} {y+0.7:.2f}'
                        f'M{x-0.7:.2f} {y+0.7:.2f}L{x+0.7:.2f} {y-0.7:.2f}" stroke="#666" stroke-width="0.15"/>')
    body.append(f'<text x="{W-10:.1f}" y="{Hh-10:.1f}" font-size="2.2" fill="#666" text-anchor="end">sdi.v0 render</text>')
    vb = (0, 0, W, Hh)
    if fit:
        xs = [s['at'][0] for s in il['symbols'] if s.get('at')]
        ys = [s['at'][1] for s in il['symbols'] if s.get('at')]
        for w in il['wires']:
            xs += [p[0] for p in w['pts']]; ys += [p[1] for p in w['pts']]
        xs += [l['at'][0] for l in il['labels'] if l.get('at')]
        ys += [l['at'][1] for l in il['labels'] if l.get('at')]
        if xs:
            m = 15; vb = (min(xs)-m, min(ys)-m, max(xs)-min(xs)+2*m, max(ys)-min(ys)+2*m)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{vb[2]:.0f}mm" height="{vb[3]:.0f}mm" '
           f'viewBox="{vb[0]:.1f} {vb[1]:.1f} {vb[2]:.1f} {vb[3]:.1f}" font-family="Menlo,monospace">'
           + '\n'.join(body) + '</svg>')
    open(out, 'w', encoding='utf-8').write(svg)

if __name__ == '__main__':
    il = json.load(open(sys.argv[1], encoding='utf-8'))
    render(il, sys.argv[2], fit=(len(sys.argv) > 3 and sys.argv[3] == '--fit'))
