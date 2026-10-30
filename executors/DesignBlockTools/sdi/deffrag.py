#!/usr/bin/env python3
"""就地定义的模块（语言里的 `defs:`）→ 一块 schematic 片段。

语言只写"这块电路是什么"：有哪些件、各脚的网。**一个坐标都不写**。几何由这里按规则算：

1. 内部两脚网把器件串成**链**（端口/电源轨不算链上的边——它们是这块对外的面）；
2. 一条链横着排一行，链间竖着叠；
3. 每个器件从 {0,90,180,270} 里挑一个朝向，让"接前一件的脚"朝左、"接后一件的脚"朝右；
4. 每个引脚挂一个网名标签——拉线和电源符号交给 wireup，和库块走同一条路
   （这样就地定义的块和库块最后是同一套画法，不会出现"生成的块看起来像另一拨人画的"）。

这是 `sdi promote`（定义 → 正式块卡）的几何那一半：先把 sch 造出来，验收过了才入库。
"""
import math


def _pins(geom_mod, libsym, lib, at, rot):
    lp = (libsym.get(lib) or {}).get('pins') or {}
    return geom_mod.pins_at({k: tuple(v) for k, v in lp.items()}, [at[0], at[1], rot])


def _bbox(libsym, lib):
    """器件本体的粗包围盒（取引脚坐标范围，够排版用）。"""
    pins = (libsym.get(lib) or {}).get('pins') or {}
    if not pins:
        return (-3.81, -3.81, 3.81, 3.81)
    xs = [p[0] for p in pins.values()]
    ys = [p[1] for p in pins.values()]
    return (min(xs), min(ys), max(xs), max(ys))


def build(dname, d, pin_net, libsym, geom_mod, origin=(0.0, 0.0), rmap=None):
    """返回一个 fragment IL（symbols + labels，无线）。

    pin_net: (板级 ref, pin) -> 全局网名；rmap: 块内 ref -> 板级 ref
    （一定要传 rmap：`defs` 里的 R1/D1 和别的块的 R1/D1 撞名，不映射就会挂到别人网的标签上）。
    """
    rmap = rmap or {}
    parts = {p['ref']: p for p in d['parts']}
    conns = d['conns']
    ports = {p['name'] for p in d.get('ports') or []} | set(d.get('rails') or [])
    # 网 → 引脚列表
    net_pins = {}
    for c in conns:
        net_pins.setdefault(c['net'], []).append((c['ref'], str(c['pin'])))
    # 内部两脚网 = 链上的边
    edges = {r: [] for r in parts}
    for net, ps in net_pins.items():
        refs = [r for r, _ in ps]
        if net in ports:
            continue          # 端口/电源轨是这块的对外面，不是链上的边
        if len(ps) == 2 and len(set(refs)) == 2:
            a, b = ps
            edges[a[0]].append((b[0], a[1], b[1]))
            edges[b[0]].append((a[0], b[1], a[1]))
    # 连通分量 → 链（按声明序出发做深度优先）
    seen, chains = set(), []
    for r in parts:
        if r in seen:
            continue
        chain, stack = [], [r]
        while stack:
            x = stack.pop(0)
            if x in seen:
                continue
            seen.add(x)
            chain.append(x)
            for nb, _, _ in edges[x]:
                if nb not in seen:
                    stack.append(nb)
        chains.append(chain)

    symbols, labels = [], []
    GAP_IN, GAP_ROW = 12.0, 16.0
    y = origin[1]
    for chain in chains:
        # 链内相邻两件的"对接脚"：找它们之间的那条两脚网
        face = {}          # (ref) -> (向左的脚, 向右的脚)
        for i, ref in enumerate(chain):
            left = right = None
            for j in (i - 1, i + 1):
                if 0 <= j < len(chain):
                    nb = chain[j]
                    for r2, pa, pb in edges[ref]:
                        if r2 == nb:
                            if j < i:
                                left = pa
                            else:
                                right = pb
            if left is None or right is None:
                own = [p for ps in net_pins.values() for r, p in ps if r == ref]
                left = left or own[0]
                right = right or next((p for p in own if p != left), own[0])
            face[ref] = (left, right)
        x = origin[0]
        row_h = 0.0
        for k, ref in enumerate(chain):
            if k:
                x += GAP_IN
            p = parts[ref]
            lib, left, right = p['lib'], *face[ref]
            best = None
            for rot in (0, 90, 180, 270):
                at = [x + 10.0, y + 10.0, float(rot)]
                pp = _pins(geom_mod, libsym, lib, at, rot)
                if left not in pp or right not in pp:
                    continue
                dx = pp[right][0] - pp[left][0]
                score = (dx > 0, -abs(dx))
                if best is None or score > best[0]:
                    best = (score, rot, pp)
            if best is None:
                continue
            _, rot, pp = best
            bb = _bbox(libsym, lib)
            symbols.append({'id': ref, 'lib': lib, 'at': [round(x + 10.0, 2), round(y + 10.0, 2), float(rot)],
                            'rot': float(rot), 'mirror': None,
                            'fields': {'Reference': {'text': ref, 'at': [round(x + 10.0, 2), round(y + 4.0, 2), 0.0]},
                                       'Value': {'text': p.get('value') or '', 'at': [round(x + 10.0, 2), round(y + 16.0, 2), 0.0]}},
                            'pins': sorted(pp), 'pins_at': {n: [round(v[0], 2), round(v[1], 2)] for n, v in pp.items()},
                            'power': False})
            for n, pt in pp.items():
                nm = pin_net.get((rmap.get(ref, ref), n))
                if nm:
                    labels.append({'kind': 'global' if nm in ('GND',) else 'local',
                                   'text': nm, 'at': [round(pt[0], 2), round(pt[1], 2), 0.0]})
            row_h = max(row_h, bb[3] - bb[1])
        y += row_h + GAP_ROW

    return {'schema': 'sdi.v2', 'page': {'paper': 'A4'}, 'symbols': symbols, 'wires': [],
            'junctions': [], 'labels': labels, 'no_connects': [], 'texts': [], 'libsym': {},
            'nets': [], 'meta': {}, 'constraints': {}}
