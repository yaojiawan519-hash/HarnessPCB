#!/usr/bin/env python3
"""PCB 线（wire）= 一根引脚到另一个引脚/岔口的一段铜，含线段与过孔。

用户 2026-10-30：「构建 PCB 线段数组，线（wires）一根引脚到另一个引脚的线，包括了线段
（segments）和过线孔（via）；segment 有宽度、层的属性，via 也有大小尺寸。这样可以检查
空头线、判断最长的线、过孔多的线，有可能方便修改布线。这样能提高效率，不要每次都硬找。」
再一条（同日）：「wires 数组也可以与 schematic 对应」——每条线的两头是 `位号.脚号`，
所以布线结果能逐网对回原理图：**原理图上连着的脚，板上是不是真的走通了**。

这份产物是**布线算法的量尺**，不是「把某块板做通」的工具：绕行比、过孔数、层数、
断头数都是可比的数，换个布线器/换套网序，同一块板重跑一遍就能看出好在哪、差在哪。

## 线怎么切

## 两级：线（wire）与 路径（path）

- **线 wire** = 铜的**物理件**：一个端点到最近的端点/岔口。撕线重布按这个粒度，
  一撕就是这一截，不会连坐。测断头、测最长的一截、看层叠都靠它。
- **路径 path** = 电路意义上的**连接**：引脚 A 顺着铜走到引脚 B，中途经过岔口也照走。
  用户说的「一根引脚到另一个引脚的线」就是它。**最长 / 过孔最多 / 绕行比都按路径量**，
  否则密集板上最长「线」只是两根岔口之间的一截（411 实测：按线 13.5mm，按路径 39.3mm）。
  `waypoints` 记下中途必须还走到的岔口、`elements` 是从 from 到 to 排好的段+孔，
  重布时靠这两样保住拓扑；岔口上的过孔是**共用**的，撕路径不能连根拔。

- **端点**：焊盘 / 过孔 / 断头 / 岔口。线碰到岔口就断，不把分叉压进同一条线——
  否则撕这根线重布时会把别人的铜一起拆掉（2026-10-30 实测：R5.2→R6.2 那条吞了整根
  3V3 主干，重布后整网断成 3 片）。
- **过孔**是线上的换层点，`elements` 里与 `seg` 并列，带 `size`/`drill`；
  `layers` 记这条线跨了哪几层。

## 两个「别再自己算」的教训（2026-10-30）

1. **断头不按坐标死比**：铜常常停在焊盘边缘而不是盘心，两段铜的端头也常差
   0.01~0.05mm（KiCad 靠**铜形重叠**判连通，不靠坐标相等）。第一版按 0.01mm 量化
   认节点，在 411 上凭空多出 13 条「悬空线」，而 kicad-cli DRC 只认 0 条。
   现在按**端帽重叠**合并节点（同层、圆心距 ≤ (w1+w2)/2），与 KiCad 的判据同源。
2. **断头的最终裁判只认 kicad-cli DRC**（`track_dangling`/`via_dangling`）。
   `--drc` 把几何判出来的悬空跟 DRC 对账，两边不一致就当场打出来——不许拿近似冒充事实。

产物默认 `DesignBlocks/<块>/_geom/wires.json`（与 `nets.json`、`conn.json` 同处）。

用法：
    python3 -m sdi wires <pcb> [--sch <原理图>] [--out PATH] [--nets a,b] [--top 10] [--drc]
"""

from __future__ import annotations

import argparse
import collections
import heapq
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / 'KiCAD_Access/src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'KiCAD_Access/src'))

from kicad_access.route.model import RouteBoard  # noqa: E402

try:
    from . import conn as connmod
except ImportError:
    sys.path.insert(0, str(HERE.parent))
    from sdi import conn as connmod

EPSM = 0.005      # 端帽重叠判据的容差（mm）


# ------------------------------------------------------------------ 几何小工具
def _proj(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    d2 = dx * dx + dy * dy
    if d2 <= 1e-12:
        return 0.0, math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / d2
    tc = min(max(t, 0.0), 1.0)
    return t, math.hypot(px - (x1 + tc * dx), py - (y1 + tc * dy))


def _lerp(a, b, t):
    return (round(a[0] + (b[0] - a[0]) * t, 4), round(a[1] + (b[1] - a[1]) * t, 4))


def _pad_hit(px, py, pad, tol=0.02):
    if pad.shape == 'circle':
        return math.hypot(px - pad.x, py - pad.y) <= pad.w / 2 + tol
    return abs(px - pad.x) <= pad.w / 2 + tol and abs(py - pad.y) <= pad.h / 2 + tol


# ------------------------------------------------------------------ 拆线
def _split_at_t(edges, poss, pid):
    """段端点扎在另一段腰上（T 岔）→ 切开。切开后「共享端点」= 电气相连。

    判据与 KiCad 同源：端点到对方线心的距离 ≤ 两者线宽均值（端帽重叠）。
    """
    out = []
    for i, e in enumerate(edges):
        ax, ay = poss[e['ia']]
        bx, by = poss[e['ib']]
        ts = []
        for j, o in enumerate(edges):
            if i == j or o['layer'] != e['layer']:
                continue
            thr = (e['w'] + o['w']) / 2 + EPSM
            for p in (poss[o['ia']], poss[o['ib']]):
                t, d = _proj(p[0], p[1], ax, ay, bx, by)
                if EPSM < t < 1 - EPSM and d <= thr:
                    ts.append(t)
        if not ts:
            out.append(dict(e))
            continue
        cuts = [e['ia']] + [pid(*_lerp((ax, ay), (bx, by), t)) for t in sorted(set(ts))] + [e['ib']]
        clean = [cuts[0]]
        for c in cuts[1:]:
            if c != clean[-1]:
                clean.append(c)
        for p1, p2 in zip(clean, clean[1:]):
            out.append({**e, 'ia': p1, 'ib': p2})
    edges[:] = out


def _pairs_paths(net, adj, live, kpos, pad_at, via_at, poss, via_layers) -> list[dict]:
    """同一根网里，任两颗引脚顺着铜走到一起的那条路（经过岔口也照走）。

    这是用户说的「一根引脚到另一个引脚的线」：线（wire）只到最近的岔口就断，
    是铜的物理件；路径（path）才是一段**连接**。线路好坏（最长、过孔最多、
    绕行比）该按路径量，不然密集板上最长「线」只是两根岔口之间的一截。

    `elements` 按从 `from` 到 `to` 的顺序排好（段 + 过孔），`waypoints` 是中途必须
    还走到的岔口——撕这条路径重布时，靠这两样保住拓扑，不会把别人的铜一起拆了。
    """
    pad_nodes = [n for n in pad_at if n in adj]
    out = []
    for i, na in enumerate(pad_nodes):
        # 从这颗引脚出发算「顺着铜走过去最短」：T 岔切分会把一根长铜切成一串小段，
        # 按跳数 BFS 会挑出「段数最少」而不是「铜最短」的那条（实测 3V3 的 U2.9→U2.24
        # 被算成 71.85mm / 42 段，其实绕了一大圈）。所以按段长做 Dijkstra。
        dist = {na: 0.0}
        prev = {na: None}
        heap = [(0.0, 0, na)]
        tick = 1
        while heap:
            d, _t, u = heapq.heappop(heap)
            if d > dist.get(u, math.inf) + 1e-9:
                continue
            for ei in adj.get(u, ()):
                e = live[ei]
                v = e['kb'] if e['ka'] == u else e['ka']
                w = math.hypot(poss[e['ib']][0] - poss[e['ia']][0],
                               poss[e['ib']][1] - poss[e['ia']][1])
                nd = d + w
                if nd < dist.get(v, math.inf) - 1e-9:
                    dist[v] = nd
                    prev[v] = (u, ei)
                    tick += 1
                    heapq.heappush(heap, (nd, tick, v))
        for nb in pad_nodes[i + 1:]:
            if nb not in dist:
                continue                      # 不连通：两片铜之间根本没有路
            chain, cur = [], nb
            while cur != na:
                u, ei = prev[cur]
                chain.append((ei, u, cur))
                cur = u
            chain.reverse()

            elems, ln, layers = [], 0.0, []
            vias = 0
            for ei, u, nx in chain:
                e = live[ei]
                if e['ka'] == u:              # 顺着 ka→kb 走
                    p, q, ia, ib = poss[e['ia']], poss[e['ib']], e['ia'], e['ib']
                else:
                    p, q, ia, ib = poss[e['ib']], poss[e['ia']], e['ib'], e['ia']
                ln += math.hypot(q[0] - p[0], q[1] - p[1])
                if e['layer'] not in layers:
                    layers.append(e['layer'])
                elems.append({'kind': 'seg', 'layer': e['layer'], 'w': e['w'],
                              'x1': round(p[0], 4), 'y1': round(p[1], 4),
                              'x2': round(q[0], 4), 'y2': round(q[1], 4),
                              'from': list(u), 'to': list(nx),
                              'na': list(u), 'nb': list(nx)})

            wps = []
            # 中途经过的节点 = 重布时必须还走到的点。取每条段的**到达**节点（chain[:-1] 的 nx），
            # 正好是起点与终点之间那串岔口；拿出发节点（u）会多算起点、漏掉倒数第二个岔口
            # （2026-10-30 实测：41 段 42 节点只出 41 个岔口，最后一条腿被迫跨两段）。
            for _ei, _u, nx in chain[:-1]:
                p = kpos[nx]
                vs = via_at.get(nx, [])
                # 这个点上所有铜在哪几层：新线必须落在其中的哪一层，否则没接上
                lays = sorted({live[k]['layer'] for k in adj.get(nx, ())})
                if vs:
                    lays = sorted(set(lays) | set(via_layers))
                # 岔口上的过孔是**共用**的（别的线也靠它换层），撕这条路径不能连根拔
                wps.append({'at': [round(p[0], 3), round(p[1], 3)],
                            'node': list(nx),
                            'via': {'size': vs[0].size, 'drill': vs[0].drill} if vs else None,
                            'layers': lays,
                            'shared': len(adj.get(nx, ())) >= 3,
                            'degree': len(adj.get(nx, ()))})
                vias += len(vs)
                for v in vs:
                    elems.append({'kind': 'via', 'at': [round(p[0], 3), round(p[1], 3)],
                                  'size': v.size, 'drill': v.drill,
                                  'shared': len(adj.get(nx, ())) >= 3})

            pa, pb = kpos[na], kpos[nb]
            d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            out.append({
                'net': net,
                'from': {'kind': 'pad', 'at': [round(pa[0], 3), round(pa[1], 3)],
                         'pins': sorted(f'{p.ref}.{p.num}' for p in pad_at[na])},
                'to': {'kind': 'pad', 'at': [round(pb[0], 3), round(pb[1], 3)],
                       'pins': sorted(f'{p.ref}.{p.num}' for p in pad_at[nb])},
                'length_mm': round(ln, 3),
                'direct_mm': round(d, 3),
                'detour': round(ln / d, 3) if d > 0.01 else None,
                'segments': len(chain),
                'vias': vias,
                'layers': layers,
                'waypoints': wps,
                'elements': elems,
            })
    return out


def net_wires(net: str, board: RouteBoard) -> dict:
    segs = [s for s in board.segments if s.net == net]
    arcs = [a for a in board.arcs if a.net == net]
    vias = [v for v in board.vias if v.net == net]
    pads = board.pads_by_net().get(net, [])

    # 端点位置去重（只按坐标去重，连通交给下面的「铜形重叠」合并）
    poss: list[tuple[float, float]] = []
    posidx: dict[tuple, int] = {}

    def pid(x, y):
        k = (round(x, 4), round(y, 4))
        i = posidx.get(k)
        if i is None:
            i = len(poss)
            posidx[k] = i
            poss.append((x, y))
        return i

    edges = []
    for s in segs:
        edges.append({'ia': pid(s.x1, s.y1), 'ib': pid(s.x2, s.y2),
                      'w': s.width, 'layer': s.layer, 'arc': False})
    for a in arcs:
        edges.append({'ia': pid(a.x1, a.y1), 'ib': pid(a.x2, a.y2),
                      'w': a.width, 'layer': a.layer, 'arc': True})
    _split_at_t(edges, poss, pid)

    # 每个位置在每层上的最大线宽（合并判据要用）
    inc: list[dict[str, float]] = [dict() for _ in poss]
    for e in edges:
        for i in (e['ia'], e['ib']):
            if e['w'] > inc[i].get(e['layer'], 0.0):
                inc[i][e['layer']] = e['w']

    # 节点锚：焊盘 > 过孔 > 纯几何点
    rep = []
    for i, (x, y) in enumerate(poss):
        k = None
        for pi, p in enumerate(pads):
            if _pad_hit(x, y, p):
                k = ('P', pi)
                break
        if k is None:
            for vi, v in enumerate(vias):
                if math.hypot(x - v.x, y - v.y) <= v.size / 2 + EPSM:
                    k = ('V', vi)
                    break
        rep.append(k if k else ('N', i))

    parent = list(range(len(poss)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    PR = {'P': 0, 'V': 1, 'N': 2}

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if PR[rep[ra][0]] > PR[rep[rb][0]]:
            ra, rb = rb, ra
        parent[rb] = ra

    by_layer = collections.defaultdict(list)
    for i, d in enumerate(inc):
        for lay, w in d.items():
            by_layer[lay].append((i, w))
    for _lay, lst in by_layer.items():
        for a in range(len(lst)):
            i, wi = lst[a]
            for b in range(a + 1, len(lst)):
                j, wj = lst[b]
                if find(i) == find(j):
                    continue
                if math.hypot(poss[i][0] - poss[j][0],
                              poss[i][1] - poss[j][1]) <= (wi + wj) / 2 + EPSM:
                    union(i, j)

    key_of = [rep[find(i)] for i in range(len(poss))]
    kpos: dict[tuple, tuple] = {}
    for i, k in enumerate(key_of):
        if k[0] == 'P':
            kpos[k] = (pads[k[1]].x, pads[k[1]].y)
        elif k[0] == 'V':
            kpos[k] = (vias[k[1]].x, vias[k[1]].y)
        else:
            kpos[k] = poss[k[1]]

    adj: dict[tuple, list[int]] = collections.defaultdict(list)
    live = []
    degen = set()                     # 两头落在同一个焊盘里的短桩（整段在盘内）
    nodes_all = set()
    for e in edges:
        ka, kb = key_of[e['ia']], key_of[e['ib']]
        e['ka'], e['kb'] = ka, kb
        nodes_all.add(ka)
        nodes_all.add(kb)
        if ka == kb:
            # 不能丢：这截铜就在焊盘里，它决定了这颗脚「有没有铜」。只不单独成线。
            degen.add(len(live))
            live.append(e)
            continue
        live.append(e)

    for idx, e in enumerate(live):
        if idx in degen:
            continue
        adj[e['ka']].append(idx)
        adj[e['kb']].append(idx)

    pad_at: dict[tuple, list] = collections.defaultdict(list)
    for pi, p in enumerate(pads):
        pad_at[('P', pi)].append(p)
    via_at: dict[tuple, list] = collections.defaultdict(list)
    for vi, v in enumerate(vias):
        via_at[('V', vi)].append(v)

    def kind_of(n):
        if pad_at.get(n):
            return 'pad'
        if len(adj.get(n, ())) == 1:
            return 'open'
        return 'junction'

    def end_desc(n):
        k = kind_of(n)
        p = kpos.get(n, (0.0, 0.0))
        d = {'kind': k, 'at': [round(p[0], 3), round(p[1], 3)]}
        if k == 'pad':
            d['pins'] = [f'{p.ref}.{p.num}' for p in pad_at[n]]
        if via_at.get(n):
            d['via'] = {'size': via_at[n][0].size, 'drill': via_at[n][0].drill}
        return d

    used = [False] * len(live)
    wires = []

    def walk(start, first):
        """从 start 沿 first 走一步：一条线 = 一个端点/岔口到最近的端点/岔口。

        **碰到岔口就断**，不许顺着岔口继续走——否则「一条线」会把主干连同它上面的
        岔口一起吞掉，撕这条线重布时就会把别人的铜一起拆了（2026-10-30 实测：
        R5.2→R6.2 那条吞了整根 3V3 主干，重布后整网断成 3 片）。
        岔口到岔口的那截由下面的兜底循环补成独立短线。
        """
        ei, cur = first, start
        used[ei] = True
        e = live[ei]
        nxt = e['kb'] if e['ka'] == cur else e['ka']
        pa, pb = poss[e['ia']], poss[e['ib']]
        if e['ka'] != cur:                # 反向走：坐标跟着翻
            pa, pb = pb, pa
        length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        nodes = [start, nxt]
        elems = []
        cp = kpos[cur]
        for v in via_at.get(cur, ()):      # 起点上的过孔
            elems.append({'kind': 'via', 'at': [round(cp[0], 3), round(cp[1], 3)],
                          'size': v.size, 'drill': v.drill,
                          'shared': len(adj.get(cur, ())) >= 3})
        elems.append({'kind': 'seg', 'layer': e['layer'], 'w': e['w'],
                      'x1': round(pa[0], 4), 'y1': round(pa[1], 4),
                      'x2': round(pb[0], 4), 'y2': round(pb[1], 4)})
        last = kpos[nodes[-1]]
        for v in via_at.get(nodes[-1], ()):   # 终点上的过孔
            elems.append({'kind': 'via', 'at': [round(last[0], 3), round(last[1], 3)],
                          'size': v.size, 'drill': v.drill,
                          'shared': len(adj.get(nodes[-1], ())) >= 3})
        so = [x for x in elems if x['kind'] == 'seg']
        vo = [x for x in elems if x['kind'] == 'via']
        layers = []
        for x in so:
            if x['layer'] not in layers:
                layers.append(x['layer'])
        w = {
            'net': net,
            'from': end_desc(nodes[0]),
            'to': end_desc(nodes[-1]),
            'length_mm': round(length, 3),
            'segments': len(so),
            'vias': len(vo),
            'layers': layers,
            'widths': sorted({round(x['w'], 3) for x in so}),
            'elements': elems,
        }
        # 脚到脚才有「绕行比」：实际铜长 / 两头直线的距离
        if w['from']['kind'] == 'pad' and w['to']['kind'] == 'pad':
            a_, b_ = w['from']['at'], w['to']['at']
            d = math.hypot(a_[0] - b_[0], a_[1] - b_[1])
            w['direct_mm'] = round(d, 3)
            w['detour'] = round(length / d, 3) if d > 0.01 else None
        return w

    order = [n for n in adj if kind_of(n) != 'junction'] + \
            [n for n in adj if kind_of(n) == 'junction']
    for n in order:
        for ei in adj[n]:
            if not used[ei]:
                wires.append(walk(n, ei))
    for ei in range(len(live)):            # 纯环：没有端点也没有岔口
        if not used[ei] and ei not in degen:
            wires.append(walk(live[ei]['ka'], ei))

    # 连通分量：一根网本该是一整片铜。分几片 = 这片网哪里没接上（布线/铺铜的欠账），
    # 没有焊盘的片 = 落单铜。这两条正是「布通没布通」的量法，不看 DRC 也能算。
    comps = []
    seen: set = set()
    for start in nodes_all:
        if start in seen:
            continue
        stack, nodes = [start], [start]
        seen.add(start)
        while stack:
            u = stack.pop()
            for ei in adj.get(u, ()):
                for v in (live[ei]['ka'], live[ei]['kb']):
                    if v not in seen:
                        seen.add(v)
                        stack.append(v)
                        nodes.append(v)
        eids = {ei for n in nodes for ei in adj.get(n, ())}
        ln = sum(math.hypot(poss[live[ei]['ib']][0] - poss[live[ei]['ia']][0],
                            poss[live[ei]['ib']][1] - poss[live[ei]['ia']][1]) for ei in eids)
        comps.append({
            'pads': sorted(f'{p.ref}.{p.num}' for n in nodes for p in pad_at.get(n, [])),
            'tracks': len(eids),
            'length_mm': round(ln, 3),
            'via': sum(len(via_at.get(n, [])) for n in nodes),
            'layers': sorted({live[ei]['layer'] for ei in eids}),
        })
    comps.sort(key=lambda c: -len(c['pads']))
    with_pads = [c for c in comps if c['pads']]
    in_copper = {p for c in comps for p in c['pads']}

    # ---- 点到点路径（用户要的「一根引脚到另一个引脚的线」）----
    # 线（wire）是铜的**物理件**：一个端点到最近的端点/岔口，一撕就是这一截。
    # 路径（path）是电路意义上的**连接**：引脚 A 顺着树走到引脚 B，中途经过岔口。
    # 两者都要：断头/撕线看 wire，最长/绕行/过孔多/重布看 path。
    paths = _pairs_paths(net, adj, live, kpos, pad_at, via_at, poss,
                         board.copper_layers)

    for w in wires:
        a, b = w['from']['kind'], w['to']['kind']
        if 'open' in (a, b):
            w['shape'] = 'dangling'
        elif a == 'pad' and b == 'pad':
            w['shape'] = 'pin-to-pin'
        elif 'junction' in (a, b):
            w['shape'] = 'branch'
        else:
            w['shape'] = 'other'

    dang = [w for w in wires if w['shape'] == 'dangling']
    p2p = [w for w in wires if w['shape'] == 'pin-to-pin' and w.get('detour')]
    pdet = [p for p in paths if p.get('detour')]
    return {
        'net': net,
        'wires': wires,
        'paths': paths,
        'pads': sorted(f'{p.ref}.{p.num}' for p in pads),
        'components': with_pads,
        'stats': {
            'wires': len(wires),
            'length_mm': round(sum(w['length_mm'] for w in wires), 3),
            'longest_mm': round(max((w['length_mm'] for w in wires), default=0.0), 3),
            'most_vias': max((w['vias'] for w in wires), default=0),
            'vias': len(vias),
            'segments': len(segs) + len(arcs),
            'detour_max': round(max((w['detour'] for w in p2p), default=0.0), 3),
            'dangling': len(dang),
            'dangling_mm': round(sum(w['length_mm'] for w in dang), 3),
            'islands': len(with_pads),           # 1 = 整根网连通；>1 = 分了几片
            'lone_copper': len(comps) - len(with_pads),
            'pads_no_copper': sorted(set(f'{p.ref}.{p.num}' for p in pads) - in_copper),
            # 路径（引脚↔引脚）：这才是「一条连接」，路由好不好按它量
            'paths': len(paths),
            'path_longest_mm': round(max((p['length_mm'] for p in paths), default=0.0), 3),
            'path_most_vias': max((p['vias'] for p in paths), default=0),
            'path_detour_max': round(max((p['detour'] for p in pdet), default=0.0), 3),
            # 同一根网的脚，有几对在板上根本没有铜路可走（分片 + 没铜脚都算）
            'pairs_total': len(pads) * (len(pads) - 1) // 2,
            'pairs_unreached': len(pads) * (len(pads) - 1) // 2 - len(paths),
        },
    }


# ------------------------------------------------------------------ 外部裁判
def drc_dangling(pcb: Path) -> dict:
    """kicad-cli DRC → {网名: 断头条数}。断头这件事只认它。"""
    try:
        from . import blockpcb
    except ImportError:
        from sdi import blockpcb
    import re

    kcli = blockpcb.kcli()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'drc.json'
        subprocess.run([kcli, 'pcb', 'drc', '--severity-all', '--format', 'json',
                        '-o', str(out), str(pcb)], capture_output=True, text=True)
        if not out.is_file():
            return {'_error': 'kicad-cli 没出 DRC 结果'}
        doc = json.loads(out.read_text(encoding='utf-8'))
    hits: dict[str, int] = collections.Counter()
    for v in doc.get('violations', []):
        if v.get('type') not in ('track_dangling', 'via_dangling'):
            continue
        for item in v.get('items', []):
            m = re.search(r'\[([^\]]+)\]', item.get('description', ''))
            if m:
                hits[m.group(1)] += 1
    return dict(hits)


# ------------------------------------------------------------------ 原理图对照
def sch_nets(sch: Path) -> dict[str, list[str]]:
    """kicad-cli 网表 → {网名: ['U2.7', ...]}（单脚网是 NC 脚，不算电路事实）。"""
    try:
        from . import netcheck
    except ImportError:
        from sdi import netcheck
    out: dict[str, list[str]] = {}
    for pins, name in netcheck.kicad_partition(sch):
        out[str(name)] = sorted(f'{r}.{p}' for r, p in pins)
    return out


def _match_sch(board_net: str, pads: list[str], frags: dict[str, list[str]]):
    """板上这根网 ↔ 原理图的哪根网：先按名字（层级网带 `/` 前缀），再按脚集合。"""
    ps = set(pads)
    for cand in (board_net, '/' + board_net):
        if cand in frags:
            return cand, ('name+pin' if set(frags[cand]) == ps else 'name')
    best, score = None, 0
    for name, pins in frags.items():
        s = len(ps & set(pins))
        if s > score:
            best, score = name, s
    if best and score >= max(2, len(ps) // 2):
        return best, 'pins'
    return None, 'none'


# ------------------------------------------------------------------ 主流程
def analyse(pcb: Path, nets: list[str] | None = None, sch: Path | None = None,
            with_drc: bool = False) -> dict:
    board = RouteBoard(pcb)
    names = sorted(board.net_names())
    if nets:
        names = [n for n in names if n in set(nets)]
    out = [net_wires(n, board) for n in names]
    out.sort(key=lambda d: -d['stats']['length_mm'])

    doc = {'board': pcb.name, 'layers': list(board.copper_layers),
           'outline': [round(v, 3) for v in board.outline],
           'nets': out}

    if sch and sch.is_file():
        frags = sch_nets(sch)
        used_sch = set()
        rows = []
        for d in out:
            name, how = _match_sch(d['net'], d['pads'], frags)
            row = {'board_net': d['net'], 'pads': d['pads'],
                   'sch_net': name, 'match': how,
                   'pins_sch': [], 'on_board_not_in_sch': [], 'in_sch_not_on_board': []}
            if name:
                used_sch.add(name)
                sp = set(frags[name])
                row['pins_sch'] = frags[name]
                row['on_board_not_in_sch'] = sorted(set(d['pads']) - sp)
                row['in_sch_not_on_board'] = sorted(sp - set(d['pads']))
            rows.append(row)
            d['sch'] = row
        doc['schematic'] = {
            'file': sch.name,
            'sch_nets': len(frags),
            'matched': sum(1 for r in rows if r['sch_net']),
            'unmatched_board_nets': [r['board_net'] for r in rows if not r['sch_net']],
            'board_nets_absent_in_sch': [r['board_net'] for r in rows
                                         if r['sch_net'] and
                                         not set(r['pads']) <= set(frags[r['sch_net']])],
            'sch_nets_not_on_board': sorted(set(frags) - used_sch),
        }

    if with_drc:
        dd = drc_dangling(pcb)
        doc['drc'] = {'dangling_by_net': dd,
                      'nets': len(dd), 'items': sum(v for v in dd.values())}
        geo = {d['net']: d['stats']['dangling'] for d in out if d['stats']['dangling']}
        doc['drc']['geometry_nets'] = len(geo)
        doc['drc']['geometry_agrees'] = (set(geo) == set(dd))
        doc['drc']['geometry_only'] = sorted(set(geo) - set(dd))
        doc['drc']['drc_only'] = sorted(set(dd) - set(geo))

    allw = [w for d in out for w in d['wires']]
    allp = [p for d in out for p in d['paths']]
    p2p = [w for w in allw if w['shape'] == 'pin-to-pin' and w.get('detour')]
    pdet = [p for p in allp if p.get('detour')]
    doc['board_stats'] = {
        'nets': len(out),
        'wires': len(allw),
        'paths': len(allp),
        'segments': sum(d['stats']['segments'] for d in out),
        'vias': len([v for v in board.vias if v.net in set(names)]),
        'copper_mm': round(sum(w['length_mm'] for w in allw), 1),
        'longest_mm': round(max((w['length_mm'] for w in allw), default=0.0), 2),
        'most_vias': max((w['vias'] for w in allw), default=0),
        'path_longest_mm': round(max((p['length_mm'] for p in allp), default=0.0), 2),
        'path_most_vias': max((p['vias'] for p in allp), default=0),
        'path_detour_max': round(max((p['detour'] for p in pdet), default=0.0), 3),
        'pairs_total': sum(d['stats']['pairs_total'] for d in out),
        'pairs_unreached': sum(d['stats']['pairs_unreached'] for d in out),
        'dangling': sum(d['stats']['dangling'] for d in out),
        'split_net_count': sum(1 for d in out if d['stats']['islands'] > 1),
        'split_nets': {d['net']: d['stats']['islands'] for d in out if d['stats']['islands'] > 1},
        'lone_copper': sum(d['stats']['lone_copper'] for d in out),
        'pads_no_copper': sum(len(d['stats']['pads_no_copper']) for d in out),
        'detour_median': round(sorted(w['detour'] for w in p2p)[len(p2p) // 2], 3) if p2p else None,
        'detour_max': round(max((w['detour'] for w in p2p), default=0.0), 3),
    }
    return doc


def _end(e: dict) -> str:
    if e['kind'] == 'pad':
        return '+'.join(e.get('pins') or ['pad'])
    tag = {'open': '悬空', 'junction': '岔口'}.get(e['kind'], e['kind'])
    return f'{tag}({e["at"][0]:.2f},{e["at"][1]:.2f})'


def _row(d: dict) -> str:
    s = d['stats']
    flag = ''
    if s['islands'] > 1:
        flag += f' 分{s["islands"]}片'
    if s['lone_copper']:
        flag += f' 落单{s["lone_copper"]}'
    if s['pads_no_copper']:
        flag += f' 没铜脚{len(s["pads_no_copper"])}'
    if s['pairs_unreached']:
        flag += f' 断{s["pairs_unreached"]}对'
    return (f'{d["net"]:<20} {s["wires"]:>3}线 {s["paths"]:>3}路 '
            f'{s["length_mm"]:7.1f}mm 最长{s["path_longest_mm"]:6.1f} '
            f'孔{s["vias"]:>2}(单路最多{s["path_most_vias"]}) '
            f'绕行{s["path_detour_max"]:5.2f} 悬空{s["dangling"]}{flag}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='sdi.wires', description='PCB → 每根网的线（段+过孔）')
    ap.add_argument('pcb')
    ap.add_argument('--out', default='', help='写 JSON（默认 <块>/_geom/wires.json）')
    ap.add_argument('--nets', default='')
    ap.add_argument('--sch', default='', help='原理图（默认找同目录同名 .kicad_sch）')
    ap.add_argument('--top', type=int, default=8, help='打几张榜：最长/过孔最多/绕行最大/悬空')
    ap.add_argument('--drc', action='store_true', help='拿 kicad-cli DRC 对账断头')
    ap.add_argument('--index', action='store_true', help='只打表，不写文件')
    a = ap.parse_args(argv)

    pcb = Path(a.pcb)
    if not pcb.is_file():
        sys.exit(f'找不到 {pcb}')
    nets = [s for s in a.nets.split(',') if s] or None
    sch = Path(a.sch) if a.sch else pcb.with_suffix('.kicad_sch')
    if not sch.is_file():
        sch = None
    doc = analyse(pcb, nets, sch, a.drc)
    bs = doc['board_stats']
    print(f'{doc["board"]}: {bs["nets"]} 网 / {bs["wires"]} 线 / {bs["paths"]} 路（引脚↔引脚）/ '
          f'{bs["segments"]} 段 / {bs["vias"]} 孔 / 铜长 {bs["copper_mm"]}mm / 层 {doc["layers"]}')
    if bs['pairs_unreached']:
        print(f'  同一根网上有 {bs["pairs_unreached"]}/{bs["pairs_total"]} 对脚在板上没有铜路')
    if bs['split_nets']:
        print('  分片的网：' + '、'.join(f'{k}×{v}' for k, v in bs['split_nets'].items()) +
              f'（落单铜 {bs["lone_copper"]} 处 / 没铜脚 {bs["pads_no_copper"]} 个）')
    for d in doc['nets']:
        print('  ' + _row(d))

    allw = [(d['net'], w) for d in doc['nets'] for w in d['wires']]
    allp = [p for d in doc['nets'] for p in d['paths']]
    if a.top:
        print(f'\n最长的 {a.top} 条连接（引脚↔引脚，顺着铜走过岔口）：')
        for p in sorted(allp, key=lambda x: -x['length_mm'])[:a.top]:
            print(f'  {p["length_mm"]:7.2f}mm {p["net"]:<16} '
                  f'{"+".join(p["from"]["pins"])} → {"+".join(p["to"]["pins"])}  '
                  f'{p["segments"]}段/{p["vias"]}孔 过岔口{len(p["waypoints"])}')
        hp = [p for p in allp if p['vias']]
        if hp:
            print(f'\n过孔最多的 {a.top} 条连接：')
            for p in sorted(hp, key=lambda x: -x['vias'])[:a.top]:
                print(f'  {p["vias"]:>2}孔 {p["length_mm"]:7.2f}mm {p["net"]:<16} '
                      f'{"+".join(p["from"]["pins"])} → {"+".join(p["to"]["pins"])}  '
                      f'{p["segments"]}段 {"+".join(x.split(".")[0] for x in p["layers"])}')
        pdet = [p for p in allp if p.get('detour')]
        print(f'\n绕得最远的 {a.top} 条连接（绕行比 = 铜长 / 两头直线）：')
        for p in sorted(pdet, key=lambda x: -x['detour'])[:a.top]:
            print(f'  绕行{p["detour"]:5.2f}  {p["length_mm"]:7.2f}mm(直线'
                  f'{p["direct_mm"]:6.2f}) {p["net"]:<16} '
                  f'{"+".join(p["from"]["pins"])} → {"+".join(p["to"]["pins"])}')

        print(f'\n单根最长的一截铜（物理件，撕线按这个粒度）：')
        for net, w in sorted(allw, key=lambda x: -x[1]['length_mm'])[:min(a.top, 5)]:
            print(f'  {w["length_mm"]:7.2f}mm {net:<16} {w["shape"]:<11} '
                  f'{_end(w["from"])} → {_end(w["to"])}  {w["segments"]}段/{w["vias"]}孔')
        dang = [(n, w) for n, w in allw if w['shape'] == 'dangling']
        print(f'\n悬空的线：{len(dang)} 条')
        for n, w in dang[:a.top]:
            print(f'  {n:<20} {w["length_mm"]:6.2f}mm 悬空端在 '
                  f'{_end(w["from"] if w["from"]["kind"] == "open" else w["to"])}')

    if 'schematic' in doc:
        s = doc['schematic']
        print(f'\n原理图对照（{s["file"]}：{s["sch_nets"]} 网）：'
              f'对上 {s["matched"]}/{len(doc["nets"])}')
        for r in doc['nets']:
            r = r.get('sch')
            if not r:
                continue
            if r['in_sch_not_on_board']:
                print(f'  原理图有、板上没脚：{r["board_net"]} ← '
                      f'{"、".join(r["in_sch_not_on_board"])}')
            if r['on_board_not_in_sch']:
                print(f'  板上有、原理图没脚：{r["board_net"]} ← '
                      f'{"、".join(r["on_board_not_in_sch"])}')
        if s['unmatched_board_nets']:
            print(f'  板上没找回原理图的网：{"、".join(s["unmatched_board_nets"])}')
        if s['sch_nets_not_on_board']:
            print(f'  原理图上、板上整根没有：{"、".join(s["sch_nets_not_on_board"])}')

    if 'drc' in doc:
        d = doc['drc']
        verdict = '✓ 一致' if d['geometry_agrees'] else '✗ 不一致'
        print(f'\nDRC 对账：几何判悬空 {d["geometry_nets"]} 根'
              f' / DRC 认断头 {d["nets"]} 根（{d["items"]} 处）… {verdict}')
        for net in d['geometry_only']:
            print(f'  · 几何说悬空、DRC 说通：{net}（合并判据漏了，属这条网的提取 bug）')
        for net in d['drc_only']:
            print(f'  · DRC 说断头、几何没认出：{net}')

    if a.index:
        return 0
    out = Path(a.out) if a.out else (connmod.default_out(pcb).parent / 'wires.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'\n→ {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
