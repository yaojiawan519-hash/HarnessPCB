#!/usr/bin/env python3
"""共线段合并 + 重复段去重（布线引擎的收尾工序）。

为什么要有这一步：`sdi.reroute` 撕线只撕「路径与铜的交集」，焊盘附近留下的短桩会被
重新布一遍，产出**完全重复**的两段（2026-10-30 `mcu_stm32f411` SWDIO 实测：
(37.4,20.4)→(37.9,20.34) 正反各一条）；A* 的 45° 走出阶梯，也会把一个直段拆成好几截。
用户的口径是「减少 wire 的段数」——段数多不等于铜多，但读数难看、也挡后续布线。

做什么：同一 (网, 层, 宽) 上
  ① 端点完全相同的两条 → 删一条（真重复）；
  ② 两条共享一个端点、且三点共线、且该点没有被第三条铜或过孔占着 → 合并成一条
     （跨岔口不合并：那会把岔口结构抹掉，虽然电学上仍连通）。

跑法：（必须用 KiCad 的 python）
    $KP sdi/collapse.py <board.kicad_pcb> [--net NET] [--dry]
"""

from __future__ import annotations

import argparse
import collections
import math
import sys

import pcbnew

MM = 1e6
EPS = 1e-4          # mm，坐标取整精度
COLL = 1e-3         # 共线判据：叉积 / 长度


def g(t):
    s, e = t.GetStart(), t.GetEnd()
    return (round(s.x / MM, 4), round(s.y / MM, 4)), (round(e.x / MM, 4), round(e.y / MM, 4))


def cross(a, b, c):
    """b 相对 a→c 的偏离（mm），用来判共线。"""
    ax, ay = a
    cx, cy = c
    bx, by = b
    L = math.hypot(cx - ax, cy - ay)
    if L < 1e-9:
        return float('inf')
    return abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / L


def between(a, m, b):
    """m 是否落在 a-b 之间（含端点）。"""
    return (min(a[0], b[0]) - EPS <= m[0] <= max(a[0], b[0]) + EPS
            and min(a[1], b[1]) - EPS <= m[1] <= max(a[1], b[1]) + EPS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='共线段合并 + 重复段去重')
    ap.add_argument('pcb')
    ap.add_argument('--net', default='', help='只处理这根网（默认全板）')
    ap.add_argument('--dry', action='store_true', help='只报数，不落盘')
    a = ap.parse_args(argv)

    b = pcbnew.LoadBoard(a.pcb)
    tracks = [t for t in b.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
    vias = [t for t in b.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    if a.net:
        tracks = [t for t in tracks if t.GetNetname() == a.net]

    # ① 去重
    seen, dead, live = {}, [], []
    for t in tracks:
        p, q = g(t)
        k = (t.GetNetname(), t.GetLayer(), round(t.GetWidth() / MM, 4),
             (p, q) if p <= q else (q, p))
        if k in seen:
            dead.append(t)
        else:
            seen[k] = t
            live.append(t)
    dup = len(dead)

    # ② 合并：迭代到不动
    segs = [{'t': t, 'a': g(t)[0], 'b': g(t)[1], 'n': t.GetNetname(),
             'lay': t.GetLayer(), 'w': round(t.GetWidth() / MM, 4)} for t in live]
    merged = 0
    while True:
        # 端点占用表：某点被几条铜/几孔占着（合并只在独占点上做）
        use = collections.Counter()
        for s in segs:
            use[s['a']] += 1
            use[s['b']] += 1
        for v in vias:
            use[(round(v.GetPosition().x / MM, 4), round(v.GetPosition().y / MM, 4))] += 1
        hit = None
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                s, t = segs[i], segs[j]
                if (s['n'], s['lay'], s['w']) != (t['n'], t['lay'], t['w']):
                    continue
                shared = [p for p in (s['a'], s['b']) if p in (t['a'], t['b'])]
                if len(shared) != 1:
                    continue
                m = shared[0]
                if use[m] != 2:
                    continue                      # 岔口/过孔占着的点不合并
                o = s['b'] if m == s['a'] else s['a']
                o2 = t['b'] if m == t['a'] else t['a']
                if not between(o, m, o2):
                    continue
                if cross(m, o, o2) > COLL:
                    continue
                hit = (i, j, o, o2)
                break
            if hit:
                break
        if not hit:
            break
        i, j, o, o2 = hit
        s, t = segs[i], segs[j]
        segs = [x for k, x in enumerate(segs) if k not in (i, j)]
        segs.append({'t': s['t'], 't2': t['t'], 'a': o, 'b': o2, 'n': s['n'],
                     'lay': s['lay'], 'w': s['w']})
        merged += 1

    new = [s for s in segs if 't2' in s]
    gone = {id(s['t']) for s in new} | {id(s['t2']) for s in new}
    gone |= {id(t) for t in dead}
    before = len(tracks)
    after = before - len(gone) + len(new)
    print(f'{a.pcb}: {a.net or "全板"} 铜段 {before} → {after}'
          f'（重复 {dup} + 合并 {merged * 2} → 新 {len(new)}）')
    if a.dry:
        return 0

    for s in new:
        nt = pcbnew.PCB_TRACK(b)
        nt.SetStart(pcbnew.VECTOR2I(int(round(s['a'][0] * MM)), int(round(s['a'][1] * MM))))
        nt.SetEnd(pcbnew.VECTOR2I(int(round(s['b'][0] * MM)), int(round(s['b'][1] * MM))))
        nt.SetLayer(s['lay'])
        nt.SetWidth(int(round(s['w'] * MM)))
        nt.SetNet(s['t'].GetNet())
        b.Add(nt)
    for t in list(tracks):
        if id(t) in gone:
            b.Remove(t)
    b.Save(a.pcb)
    print(f'  ✓ 落盘')
    return 0


if __name__ == '__main__':
    sys.exit(main())
