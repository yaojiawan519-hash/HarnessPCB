#!/usr/bin/env python3
"""从 PCB 提取「每根网的折线数组 + 语义属性」，给 AI / 检索用。

用户 2026-10-30：「能否从 PCB 图中提取 schematic net 的折线数组（多个线段组成）这样后面
就可以检索了」→「折线数组可以包含不同层的折线图。我觉得要增加"语义属性"有利于 AI Code」
→「这和下面要做的事情是类似的，有了折线组，那么不在任何折线组中的线段都是断头线」
→「如果提取了折线数组，应该保留在 DesignBlock 目录中」。

每根网输出两样东西：

1. **`polylines`（画法）**：按层切开的折线数组。同层 segment/arc 按端点首尾相接串成有序
   折线，每条带 `layer`、点列 `points`、线宽、是否闭合、两端过孔坐标、所属**折线组** `group`。
   过孔单独列 `vias`（换层点）。不同层是分开的折线，换层信息在 `vias` 里。
2. **`semantic`（语义属性）**：`role`、`terminals`、`geometry`、`style`、`topology`、
   `connectivity`（折线组数 / 组里没焊盘的「落单铜」/ 断头）、`flags`。

### 折线组从哪来（关键）

**折线组 = KiCad 自己的连通簇**（`sdi/conn.py` 导出的 `_geom/conn.json`），不是我自己算的。
教训：手写端点图在 `mcu_stm32f411` 上把 6 组 3V3 主体铜判成「没焊盘的落单铜」——过孔/焊盘
接在铜的腰上、B.Cu 总线经孔接 F.Cu 这类事，自己算准不了，而 DRC 只认 2 条断头。
没有 `conn.json` 时退化成自算图，并在 `connectivity.source` 里写 `approx`。

**断头**有两种，分开算、分开说：
- `unanchored`：整组铜一个焊盘都不挨 → 落单铜（真该删的残渣）；
- `dangling`：组的端头谁也不挨（口岸断头）——这个**只有 `--drc` 才给**，用
  `kicad-cli pcb drc` 的 `track_dangling` 当裁判，不拿几何近似冒充。

产物默认落在块目录：`DesignBlocks/<块>/_geom/nets.json`（跟 `measured.json`、`conn.json` 同处）。

用法：
    python3 -m sdi.netpoly <block.kicad_pcb> [--out PATH] [--nets a,b] [--drc] [--index]
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / 'KiCAD_Access/src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'KiCAD_Access/src'))

from kicad_access.route.model import RouteBoard  # noqa: E402

try:                       # 同一批 key 函数，两边必须一致
    from . import conn as connmod
except ImportError:        # 直接当脚本跑
    sys.path.insert(0, str(HERE.parent))
    from sdi import conn as connmod

Q = 3                      # 折线点输出的小数位
QN = 2                     # 自算连通图时的节点量化（0.01mm）
EPSM = 0.005               # 铜与铜「碰上」的余量（mm）
ANG_TOL = 6.0              # 判定「直行」的角度容差（度）

KICAD_CLI = Path('/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')

ROLE_RULES = [
    # (role, 正则)——顺序即优先级；只认网名，不猜拓扑
    ('ground',  r'^(?:A|D|P|S)?GND\w*$'),
    ('power',   r'^(?:VCC|VDD|VSS|VBUS|VIN|VOUT|VREF|VREG_\w+|AVDD|DVDD|\+?\d+V\d*|'
                r'\d+V\d+(?:_\w+)?|VBAT|V_\w+|3V3\w*|5V\w*|12V\w*|24V\w*|'
                r'VCC_\w+|VDD_\w+|PWR\w*|VPH?\w*)$'),
    ('clock',   r'(?:CLK|OSC|XTAL|MCO|HSE|LSE)'),
    ('reset',   r'(?:^|\W)(?:NRST|RESET|RST)(?:\W|$|_)'),
    ('debug',   r'^(?:SWDIO|SWCLK|SWO|TCK|TMS|TDI|TDO|JTAG\w*|SWD\w*)$'),
    ('usb',     r'(?:^|_)USB|D\+$|D-$|DP$|DM$'),
]
DIFF_RE = re.compile(r'^(?P<base>.+?)[_-]?(?P<pol>[PN]|DP|DM|D\+|D-)$')


def _rx():
    return [(role, re.compile(pat)) for role, pat in ROLE_RULES]


# ----------------------------------------------------------------- 小几何
def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, Q), round(y, Q))


def _nkey(x: float, y: float) -> tuple[float, float]:
    return (round(x, QN), round(y, QN))


def _pad_hit(px: float, py: float, pad, tol: float = EPSM) -> bool:
    if pad.shape == 'circle':
        return math.hypot(px - pad.x, py - pad.y) <= pad.w / 2 + tol
    return abs(px - pad.x) <= pad.w / 2 + tol and abs(py - pad.y) <= pad.h / 2 + tol


def _proj(px, py, x1, y1, x2, y2):
    """点到线段的（参数 t, 最近距离）。t 不截断，用来分辨「在腰上」还是「在延长线上」。"""
    dx, dy = x2 - x1, y2 - y1
    d2 = dx * dx + dy * dy
    if d2 <= 1e-12:
        return 0.0, math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / d2
    tc = min(max(t, 0.0), 1.0)
    return t, math.hypot(px - (x1 + tc * dx), py - (y1 + tc * dy))


def _lerp(a, b, t):
    return (round(a[0] + (b[0] - a[0]) * t, 4), round(a[1] + (b[1] - a[1]) * t, 4))


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


# ----------------------------------------------------------------- 折线
def _split_at_t(edges: list[dict]) -> list[dict]:
    """段端点扎在另一段腰上（T 岔）→ 在扎点把被扎的那段切开。

    切完「共享端点」才等于「电气相连」，折线才会在岔口断开而不是穿过去。
    """
    out = []
    for i, e in enumerate(edges):
        ts = []
        for j, o in enumerate(edges):
            if i == j or o['layer'] != e['layer']:
                continue
            thr = (e['w'] + o['w']) / 2 + EPSM
            for p in (o['a'], o['b']):
                t, d = _proj(p[0], p[1], e['a'][0], e['a'][1], e['b'][0], e['b'][1])
                if EPSM < t < 1 - EPSM and d <= thr:
                    ts.append(t)
        if not ts:
            out.append(dict(e))
            continue
        cuts = [e['a']] + [_lerp(e['a'], e['b'], t) for t in sorted(set(ts))] + [e['b']]
        for p1, p2 in zip(cuts, cuts[1:]):
            out.append({**e, 'a': p1, 'b': p2})
    for i, e in enumerate(out):
        e['i'] = i
    return out


def _chains(edges: list[dict]) -> list[dict]:
    """同层同网的一组线段串成折线：只沿「度 2」的节点走，三岔/端点断开。"""
    if not edges:
        return []
    adj: dict[tuple, list[int]] = collections.defaultdict(list)
    for k, e in enumerate(edges):
        adj[e['a']].append(k)
        adj[e['b']].append(k)
    used = [False] * len(edges)
    chains = []

    def walk(start: tuple, first: int) -> dict:
        pts = [start]
        widths, idxs, n_arc, cur, k = [], [], 0, start, first
        closed = False
        while True:
            used[k] = True
            e = edges[k]
            nxt = e['b'] if e['a'] == cur else e['a']
            widths.append(e['w'])
            idxs.append(e['i'])
            n_arc += 1 if e['arc'] else 0
            if nxt == pts[0]:
                pts.append(nxt)
                closed = True
                break
            pts.append(nxt)
            cand = [j for j in adj[nxt] if not used[j]]
            if len(adj[nxt]) != 2 or not cand:
                break
            cur, k = nxt, cand[0]
        return {'points': pts, 'closed': closed, 'widths': widths,
                'arcs': n_arc, 'idx': idxs}

    for node, ks in adj.items():          # 先从头/岔起笔，折线端点落在真节点上
        if len(ks) == 2:
            continue
        for k in ks:
            if not used[k]:
                chains.append(walk(node, k))
    for k in range(len(edges)):           # 剩下的是纯环
        if not used[k]:
            chains.append(walk(edges[k]['a'], k))
    return chains


def _angles(pts):
    out = []
    for p0, p1, p2 in zip(pts, pts[1:], pts[2:]):
        a1 = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        a2 = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p0[0]))
        out.append(abs((a2 - a1 + 180.0) % 360.0 - 180.0))
    return out


# ----------------------------------------------------------------- 语义
def _role(net: str, pads: int, diff_pairs: set[str]) -> str:
    if net in diff_pairs:
        return 'differential'
    for role, rx in _rx():
        if rx.search(net):
            return role
    if pads >= 6:
        return 'distribution'
    return 'signal'


def _diff_pairs(names: list[str]) -> set[str]:
    base: dict[str, list[str]] = collections.defaultdict(list)
    for n in names:
        m = DIFF_RE.match(n)
        if m:
            base[m.group('base')].append(n)
    out = set()
    for ns in base.values():
        if len({DIFF_RE.match(n).group('pol') for n in ns}) >= 2:
            out.update(ns)
    return out


# ----------------------------------------------------------------- 折线组
def _groups_from_conn(edges, pads, conn_doc):
    """折线组 = KiCad 连通簇。返回 (edge2group, groups, source)。"""
    cmap = conn_doc.get('cluster_of', {})
    cl = conn_doc.get('clusters', {})
    e2g = {}
    miss = 0
    for e in edges:
        cid = cmap.get(e.get('key') or '')
        if cid is None:
            miss += 1
            cid = -1 - e['i']          # 认不出来的自己成一组，并记账
        e2g[e['i']] = cid
    p2g = {}
    for p in pads:
        p2g[f'{p.ref}.{p.num}'] = cmap.get(connmod.k_pad(p.ref, p.num))
    groups = {}
    for e in edges:
        g = groups.setdefault(e2g[e['i']], {'edges': [], 'pads': [], 'key': e.get('key')})
        g['edges'].append(e)
    for k, cid in p2g.items():
        if cid is not None:
            groups.setdefault(cid, {'edges': [], 'pads': [], 'key': None})['pads'].append(k)
    src = 'kicad-connectivity' if not miss else f'kicad-connectivity（{miss} 段没认出来）'
    return e2g, groups, src


def _groups_approx(edges, pads, vias):
    """没有 conn.json 时的退化方案：自算端点图（会误判，结果标 approx）。"""
    E, V, P = len(edges), len(vias), len(pads)
    uf = _UF(E + V + P)
    node_e = collections.defaultdict(list)
    for e in edges:
        for p in (e['a'], e['b']):
            node_e[(e['layer'], _nkey(*p))].append(e['i'])
    for lst in node_e.values():
        for k in lst[1:]:
            uf.union(lst[0], k)
    for e in edges:
        for p in (e['a'], e['b']):
            for vi, v in enumerate(vias):
                _, d = _proj(v.x, v.y, *p, *p)
                if math.hypot(p[0] - v.x, p[1] - v.y) <= v.size / 2 + EPSM:
                    uf.union(e['i'], E + vi)
            for pi, p2 in enumerate(pads):
                if not (p2.is_th or e['layer'] in p2.layers):
                    continue
                if _pad_hit(p[0], p[1], p2):
                    uf.union(e['i'], E + V + pi)
    e2g = {e['i']: uf.find(e['i']) for e in edges}
    groups = {}
    for e in edges:
        groups.setdefault(e2g[e['i']], {'edges': [], 'pads': [], 'key': None})['edges'].append(e)
    for pi, p in enumerate(pads):
        g = groups.setdefault(uf.find(E + V + pi), {'edges': [], 'pads': [], 'key': None})
        g['pads'].append(f'{p.ref}.{p.num}')
    return e2g, groups, 'approx'


# ----------------------------------------------------------------- 单网
def _net_entry(net: str, board: RouteBoard, pads: list, diff_pairs: set[str],
               conn_doc=None, dangling_nets: set[str] | None = None) -> dict:
    raw, arcs = [], []
    for s in board.segments:
        if s.net == net:
            raw.append({'a': (s.x1, s.y1), 'b': (s.x2, s.y2), 'w': s.width,
                        'layer': s.layer, 'arc': False,
                        'key': connmod.k_track(s.x1, s.y1, s.x2, s.y2, s.layer)})
    for a in board.arcs:
        if a.net == net:              # 连通按弦算；长度一会儿按真弧长补
            raw.append({'a': (a.x1, a.y1), 'b': (a.x2, a.y2), 'w': a.width,
                        'layer': a.layer, 'arc': True,
                        'key': connmod.k_track(a.x1, a.y1, a.x2, a.y2, a.layer)})
            arcs.append(a)
    vias = [v for v in board.vias if v.net == net]
    edges = _split_at_t(raw)

    if conn_doc:
        e2g, groups, src = _groups_from_conn(edges, pads, conn_doc)
    else:
        e2g, groups, src = _groups_approx(edges, pads, vias)

    # 组号重排成 0..n-1（按簇 id 排序，稳定）
    order = {g: i for i, g in enumerate(sorted(groups))}
    for e in edges:
        e['g'] = order[e2g[e['i']]]
    for g in groups.values():
        g['edges'].sort(key=lambda e: e['i'])

    # ---- 折线（按层）
    by_layer: dict[str, list[dict]] = collections.defaultdict(list)
    for e in edges:
        by_layer[e['layer']].append(e)
    via_keys = {_nkey(v.x, v.y) for v in vias}

    # 折线组汇总
    gsum = []
    for gid, g in groups.items():
        L = sum(math.hypot(e['b'][0] - e['a'][0], e['b'][1] - e['a'][1]) for e in g['edges'])
        gsum.append({
            'group': order[gid],
            'anchored': bool(g['pads']),
            'pads': sorted(g['pads']),
            'segments': len(g['edges']),
            'layers': sorted({e['layer'] for e in g['edges']}),
            'length_mm': round(L, 3),
        })
    gsum.sort(key=lambda x: -x['length_mm'])
    unanchored = [g for g in gsum if not g['anchored']]

    polylines = []
    n_bends = ortho = diag = other = 0
    length = 0.0
    for layer in sorted(by_layer):
        for ch in _chains(by_layer[layer]):
            pts = [[round(p[0], 4), round(p[1], 4)] for p in ch['points']]
            angs = _angles(ch['points'])
            n_bends += sum(1 for d in angs if d > ANG_TOL)
            for d in angs:
                if d <= ANG_TOL:
                    continue
                if abs(d - 90) <= ANG_TOL:
                    ortho += 1
                elif abs(d - 45) <= ANG_TOL:
                    diag += 1
                else:
                    other += 1
            L = sum(math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                    for p1, p2 in zip(ch['points'], ch['points'][1:]))
            length += L
            ws = sorted({round(w, 3) for w in ch['widths']})
            ends = [] if ch['closed'] else [pts[0], pts[-1]]
            polylines.append({
                'layer': layer,
                'group': next(e['g'] for e in edges if e['i'] == ch['idx'][0]),
                'kind': 'closed' if ch['closed'] else 'open',
                'points': pts,
                'width': ws[0] if len(ws) == 1 else ws,
                'length_mm': round(L, 3),
                'arcs': ch['arcs'],
                'ends': ends,
                'via_ends': [p for p in ends if _nkey(*p) in via_keys],
            })
    length += sum(a.length - math.hypot(a.x2 - a.x1, a.y2 - a.y1) for a in arcs)

    ws = sorted({round(e['w'], 3) for e in edges})
    xs = [e['a'][0] for e in edges] + [e['b'][0] for e in edges] + [p.x for p in pads]
    ys = [e['a'][1] for e in edges] + [e['b'][1] for e in edges] + [p.y for p in pads]
    deg = collections.Counter()
    for e in edges:
        deg[e['a']] += 1
        deg[e['b']] += 1
    branch = sum(1 for d in deg.values() if d >= 3)
    layers = sorted({e['layer'] for e in edges})
    nb = ortho + diag + other
    role = _role(net, len(pads), diff_pairs)

    dangling = bool(dangling_nets and net in dangling_nets)
    flags = []
    if len(layers) > 1:
        flags.append('multi_layer')
    if vias:
        flags.append('has_vias')
    if ws and ws[-1] >= 0.5:
        flags.append('wide')
    if len(pads) >= 6:
        flags.append('fanout')
    if role in ('ground', 'power'):
        flags.append('rail')
    if unanchored:
        flags.append('isolated_copper')
    if dangling:
        flags.append('dangling')

    return {
        'net': net,
        'semantic': {
            'role': role,
            'pads': {'count': len(pads), 'refs': sorted({p.ref for p in pads}),
                     'th': sum(1 for p in pads if p.is_th)},
            'terminals': [{'ref': p.ref, 'pin': p.num, 'at': [round(p.x, 3), round(p.y, 3)],
                           'layers': list(p.layers)} for p in pads],
            'geometry': {
                'length_mm': round(length, 3),
                'segments': len(raw),
                'split_edges': len(edges),
                'arcs': len(arcs),
                'polylines': len(polylines),
                'bends': n_bends,
                'widths': ws,
                'layers': layers,
                'vias': len(vias),
                'bbox': [round(min(xs), 3), round(min(ys), 3),
                         round(max(xs), 3), round(max(ys), 3)] if xs else None,
                'branch_points': branch,
            },
            'style': {
                'angles': {'ortho': ortho, 'diag45': diag, 'other': other},
                'ortho_share': round(ortho / nb, 3) if nb else None,
                'diag45_share': round(diag / nb, 3) if nb else None,
                'layer_changes': len(vias),
                'longest_polyline_mm': max((p['length_mm'] for p in polylines), default=0.0),
            },
            'topology': {
                'shape': ('point-to-point' if len(pads) <= 2 and branch == 0
                          else 'chain' if branch == 0 else 'tree'),
                'branch_points': branch,
            },
            'connectivity': {
                'source': src,
                'groups': len(gsum),
                'anchored_groups': len(gsum) - len(unanchored),
                'unanchored_groups': len(unanchored),
                'unanchored_mm': round(sum(g['length_mm'] for g in unanchored), 3),
                'dangling': dangling if dangling_nets is not None else None,
            },
            'flags': flags,
        },
        'groups': gsum,
        'polylines': polylines,
        'vias': [{'at': [round(v.x, 3), round(v.y, 3)], 'size': v.size, 'drill': v.drill}
                 for v in vias],
    }


# ----------------------------------------------------------------- DRC（裁判）
def drc_dangling(pcb: Path) -> set[str]:
    """kicad-cli DRC 报 track_dangling 的网名——断头的唯一裁判。"""
    if not KICAD_CLI.is_file():
        return set()
    out = Path('/tmp') / f'_netpoly_drc_{pcb.stem}.json'
    subprocess.run([str(KICAD_CLI), 'pcb', 'drc', '--severity-all', '--format', 'json',
                    '-o', str(out), str(pcb)], capture_output=True, check=False)
    try:
        doc = json.loads(out.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    nets = set()
    for v in doc.get('violations', []):
        if v.get('type') != 'track_dangling':
            continue
        for it in v.get('items', []):
            m = re.search(r'\[([^\]]+)\]', it.get('description', ''))
            if m:
                nets.add(m.group(1))
    return nets


# ----------------------------------------------------------------- 板级
def analyse(pcb: Path, nets: list[str] | None = None, conn_doc=None,
            use_drc: bool = False) -> dict:
    board = RouteBoard(pcb)
    names = sorted(board.net_names())
    pads_by_net = board.pads_by_net()
    diff_pairs = _diff_pairs(names)
    dang = drc_dangling(pcb) if use_drc else None
    if nets:
        names = [n for n in names if n in set(nets)]
    entries = [_net_entry(n, board, pads_by_net.get(n, []), diff_pairs, conn_doc, dang)
               for n in names]
    entries.sort(key=lambda e: (-e['semantic']['geometry']['length_mm'], e['net']))
    return {
        'board': pcb.name,
        'layers': list(board.copper_layers),
        'outline': [round(v, 3) for v in board.outline],
        'drc_checked': use_drc,
        'nets': entries,
    }


def load_conn(pcb: Path, explicit: str = '') -> dict | None:
    p = Path(explicit) if explicit else connmod.default_out(pcb)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _index_row(e: dict) -> str:
    s = e['semantic']
    g = s['geometry']
    c = s['connectivity']
    dang = '断头' if c['dangling'] else ''
    iso = f'{c["unanchored_groups"]}组孤立{c["unanchored_mm"]:.1f}mm' if c['unanchored_mm'] else ''
    return (f'{e["net"]:<20} {s["role"]:<12} {s["pads"]["count"]:>2}脚 {g["length_mm"]:6.1f}mm '
            f'{"/".join(x.split(".")[0] for x in g["layers"]):<8} '
            f'{g["vias"]:>2}孔 {g["polylines"]:>2}线 {g["bends"]:>3}弯 '
            f'{c["groups"]:>2}组 {iso:<16} {dang:<4} {",".join(s["flags"])}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='sdi.netpoly',
                                 description='PCB → 每根网的折线数组 + 语义属性')
    ap.add_argument('pcb')
    ap.add_argument('--out', default='', help='写 JSON（默认 <块>/_geom/nets.json）')
    ap.add_argument('--conn', default='', help='连通簇 JSON（默认 <块>/_geom/conn.json）')
    ap.add_argument('--nets', default='', help='只看这几根（逗号分隔）')
    ap.add_argument('--index', action='store_true', help='只打检索表，不写文件')
    ap.add_argument('--drc', action='store_true', help='跑 kicad-cli DRC，标出断头')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)

    pcb = Path(a.pcb)
    if not pcb.is_file():
        sys.exit(f'找不到 {pcb}')
    conn_doc = load_conn(pcb, a.conn)
    nets = [s for s in a.nets.split(',') if s] or None
    doc = analyse(pcb, nets, conn_doc, a.drc)
    src = doc['nets'][0]['semantic']['connectivity']['source'] if doc['nets'] else '-'
    print(f'{doc["board"]}: {len(doc["nets"])} 网 / 层 {doc["layers"]} / '
          f'板框 {doc["outline"]} / 折线组来源 {src}')
    if not a.quiet:
        for e in doc['nets']:
            print('  ' + _index_row(e))
        tot_g = sum(e['semantic']['connectivity']['unanchored_groups'] for e in doc['nets'])
        tot_mm = sum(e['semantic']['connectivity']['unanchored_mm'] for e in doc['nets'])
        tot_d = sum(1 for e in doc['nets'] if e['semantic']['connectivity']['dangling'])
        print(f'  合计：落单铜 {tot_g} 组 / {tot_mm:.2f} mm'
              + (f'；断头网 {tot_d} 根（kicad-cli DRC 裁判）' if a.drc else '；断头未查（加 --drc）'))
    if a.index:
        return 0
    out = Path(a.out) if a.out else (connmod.default_out(pcb).parent / 'nets.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'→ {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
