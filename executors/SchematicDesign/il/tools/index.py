#!/usr/bin/env python3
"""**只当验收对照**：直接从 .kicad_sch 算连通关系。

正常流程一个都不该用它——主链路是 extract（kicad_sch → IL）之后全走 IL。
留在这里是为了"两条独立实现互相对账"：check_il.py 拿它的结果和 IL 里的 nets 比，
分组一致才说明 IL 真的把网表带全了。几何口径和 extract 共用 geom，避免各算一套。
"""
import sys, json, math
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import geom
from sexpr import parse, H, find, one, at

EPS = geom.EPS
key = geom.key
dist = geom.dist
on_seg = geom.on_seg
DSU = geom.DSU
transform = geom.transform
lib_pins = geom.lib_pins
lib_pins_units = geom.lib_pins_units
pins_for_unit = geom.pins_for_unit

# 第三方板的电源符号在自己的库里（antmicropower:+1V8），靠 'power:' 前缀认不出来。
# 改用 KiCad 写进库定义的 `(power)` 标记——这是文件里的事实，不是某一方的实现约定。
NO_NAME_POWER = {'PWR_FLAG'}      # 只声明"有电"，不命名网


def power_libs(root):
    out = set()
    for node in root[1:]:
        if not isinstance(node, list) or H(node) != 'lib_symbols':
            continue
        for sym in find(node, 'symbol'):
            if len(sym) > 1 and any(isinstance(c, list) and H(c) == 'power' for c in sym[1:]):
                out.add(sym[1])
    return out


def is_power_lib(lib, pwl):
    return lib.startswith('power:') or lib in pwl


def _abut(dsu, node_pts):
    """端点与端点重合（EPS 内）也并网。

    和 extract 的那条规则**独立实现**（这个文件的职责就是当第二条实现对账，
    共用代码就等于自证），但口径必须一样：人工页里电源符号常常直接压在器件
    脚下、中间没有导线，只认导线会把它们拆成孤网。
    """
    ps = sorted(node_pts)
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            if b[0] - a[0] > geom.ABUT:
                break
            if abs(b[1] - a[1]) <= geom.ABUT and dist(a, b) <= geom.ABUT:
                dsu.union(a, b)


def is_inst(n):
    """器件实例：`lib_id` 不一定排在第一个孩子（本地改过缓存副本的符号前面会多一行
    `lib_name`）。判第一个孩子会静默漏件——见 extract.is_inst 的注释。"""
    return H(n) == 'symbol' and any(isinstance(c, list) and H(c) == 'lib_id' for c in n[1:])


def build(path):
    text = open(path, encoding='utf-8', errors='replace').read()
    root = parse(text)
    pins_lib = lib_pins(root)
    units_lib = lib_pins_units(root)      # 多单元器件必须按放置的 (unit N) 取脚
    pwl = power_libs(root)
    dsu = DSU(); points = {}
    symbols = []; wires = []; labels = []; ncs = []; junctions = []
    segs = []
    for n in root[1:]:
        if not isinstance(n, list): continue
        h = H(n)
        if is_inst(n):
            lib = one(n, 'lib_id')[1]; a = at(n) or [0,0,0]
            ref = None; val = None
            for p in find(n, 'property'):
                if len(p) > 2 and p[1] == 'Reference': ref = p[2]
                if len(p) > 2 and p[1] == 'Value': val = p[2]
            mir = one(n, 'mirror'); mir = mir[1] if mir else None
            u = one(n, 'unit')
            try:
                unit = int(u[1]) if u else 1
            except (TypeError, ValueError, IndexError):
                unit = 1
            # 按单元取脚：并集会把别的单元的脚摆到本单元位置上，凭空多出连接
            # （LM393 这类双单元符号两个三角形在库里画在同一处）
            lp = pins_for_unit(units_lib.get(lib) or {}, unit) if units_lib.get(lib) else \
                (pins_lib.get(lib) or {})
            pins = {}
            for num, (px, py) in lp.items():
                pins[num] = transform(px, py, a[0], a[1], a[2] if len(a) > 2 else 0, mir)
            sym = {'id': ref, 'lib': lib, 'at': a[:3], 'pins': pins, 'val': val}
            symbols.append(sym)
        elif h == 'wire':
            pts = one(n, 'pts')
            xy = [[float(p[1]), float(p[2])] for p in find(pts or [], 'xy')]
            wires.append(xy)
            for i in range(len(xy)-1): segs.append((xy[i], xy[i+1]))
        elif h == 'junction':
            junctions.append(at(n))
        elif h in ('label', 'global_label', 'hierarchical_label'):
            labels.append({'kind': h, 'text': n[1], 'at': at(n)})
        elif h == 'no_connect':
            ncs.append(at(n))

    # nodes
    for w in wires:
        for p in w: dsu.find(key(p))
    for s in symbols:
        for num, p in s['pins'].items(): dsu.find(key(p))
    for l in labels: dsu.find(key(l['at']))
    for p in junctions: dsu.find(key(p))

    def union_pt(p, q):
        if dist(p, q) <= EPS: dsu.union(key(p), key(q)); return True
        return False

    # wire segments: endpoints and any node lying on the segment
    node_pts = set()
    for s in symbols:
        for num, p in s['pins'].items(): node_pts.add(key(p))
    for l in labels: node_pts.add(key(l['at']))
    for p in junctions: node_pts.add(key(p))
    for p in ncs: node_pts.add(key(p))
    for a, b in segs:
        dsu.union(key(a), key(b))
        for k in node_pts:
            if on_seg(k, a, b): dsu.union(k, key(a))
    _abut(dsu, node_pts)

    # labels/power symbols name nets
    net_name = {}
    for l in labels:
        r = dsu.find(key(l['at']))
        net_name.setdefault(r, l['text'])
    power_nets = set()
    for s in symbols:
        if is_power_lib(s['lib'], pwl) and '1' in s['pins']:
            # KiCad 按 Value 命名电源网；库名只是默认值。第三方板常常复用一个符号、
            # 只改 Value 来造不同轨（antmicropower:+1V8 的 Value 写成 +1V15）——
            # 按库名命名会把两条轨并成一条。
            name = s.get('val') or s['lib'].split(':', 1)[1]
            if s['lib'].split(':', 1)[-1] in NO_NAME_POWER:
                continue
            r = dsu.find(key(s['pins']['1']))
            net_name.setdefault(r, name); power_nets.add(r)

    # symbol <-> nets
    sym_nets = {}
    for i, s in enumerate(symbols):
        ns = set()
        for num, p in s['pins'].items(): ns.add(dsu.find(key(p)))
        sym_nets[i] = ns

    # clusters: ignore power nets and globally-labelled nets
    global_roots = set(power_nets)
    for l in labels:
        if l['kind'] in ('global', 'hier'): global_roots.add(dsu.find(key(l['at'])))
    # also ignore nets used by >6 symbols (likely buses/global)
    root_users = {}
    for i, ns in sym_nets.items():
        for r in ns: root_users.setdefault(r, set()).add(i)
    for r, users in root_users.items():
        if len(users) > 6: global_roots.add(r)

    parent = list(range(len(symbols)))
    def f(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for r, users in root_users.items():
        if r in global_roots: continue
        users = list(users)
        for u in users[1:]: parent[f(u)] = f(users[0])

    clusters = {}
    for i, s in enumerate(symbols):
        if is_power_lib(s['lib'], pwl): continue
        clusters.setdefault(f(i), []).append(i)

    def netlabel(r):
        return net_name.get(r) or f'N${abs(hash(r)) % 10000}'

    out = []
    for root_id, idxs in clusters.items():
        pts = [symbols[i]['at'][:2] for i in idxs]
        ox = sum(p[0] for p in pts)/len(pts); oy = sum(p[1] for p in pts)/len(pts)
        members = []
        for i in idxs:
            s = symbols[i]
            members.append({'id': s['id'], 'lib': s['lib'],
                            'rel': [round(s['at'][0]-ox, 2), round(s['at'][1]-oy, 2)],
                            'rot': s['at'][2] if len(s['at']) > 2 else 0})
        nets = set()
        for i in idxs: nets |= {netlabel(r) for r in sym_nets[i] if r not in global_roots}
        out.append({'size': len(idxs), 'members': members, 'nets': sorted(nets)})
    out.sort(key=lambda c: -c['size'])
    return {'symbols': len(symbols), 'wires': len(wires), 'labels': len(labels),
            'segments': len(segs), 'clusters': out[:12],
            'pin_match': pin_match_rate(symbols, segs, labels, ncs, junctions)}

def pin_match_rate(symbols, segs, labels, ncs, junctions):
    pts = []
    for a, b in segs: pts += [a, b]
    pts += [l['at'][:2] for l in labels] + [p[:2] for p in ncs if p] + [p[:2] for p in junctions if p]
    total = hit = 0
    for s in symbols:
        for num, p in s['pins'].items():
            total += 1
            if any(dist(p, q) <= EPS for q in pts): hit += 1
    return round(hit/total, 3) if total else 0

if __name__ == '__main__':
    r = build(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=1))

def net_records(path):
    """命名网络 -> 成员引脚/走线/包围盒（检索单元）。"""
    text = open(path, encoding='utf-8', errors='replace').read()
    root = parse(text); pins_lib = lib_pins(root); pwl = power_libs(root)
    units_lib = lib_pins_units(root)
    dsu = DSU(); wires = []; labels = []; syms = []; ncs = []; juncs = []
    for n in root[1:]:
        if not isinstance(n, list): continue
        h = H(n)
        if is_inst(n):
            lib = one(n, 'lib_id')[1]; a = at(n) or [0,0,0]; ref = None; val = None
            for p in find(n, 'property'):
                if len(p) > 2 and p[1] == 'Reference': ref = p[2]
                if len(p) > 2 and p[1] == 'Value': val = p[2]
            mir = one(n, 'mirror'); mir = mir[1] if mir else None
            u = one(n, 'unit')
            try:
                unit = int(u[1]) if u else 1
            except (TypeError, ValueError, IndexError):
                unit = 1
            lp = pins_for_unit(units_lib.get(lib) or {}, unit) if units_lib.get(lib) else \
                (pins_lib.get(lib) or {})
            pins = {num: transform(px, py, a[0], a[1], a[2] if len(a) > 2 else 0, mir)
                    for num, (px, py) in lp.items()}
            syms.append({'id': ref, 'lib': lib, 'at': a[:3], 'pins': pins, 'val': val})
        elif h == 'wire':
            xy = [[float(p[1]), float(p[2])] for p in find(one(n,'pts') or [], 'xy')]
            wires.append(xy)
        elif h == 'junction': juncs.append(at(n))
        elif h in ('label','global_label','hierarchical_label'):
            labels.append({'kind': h, 'text': n[1], 'at': at(n)})
        elif h == 'no_connect': ncs.append(at(n))
    segs = [(w[i], w[i+1]) for w in wires for i in range(len(w)-1)]
    node_pts = set()
    for s in syms:
        for num,p in s['pins'].items(): node_pts.add(key(p))
    for l in labels: node_pts.add(key(l['at']))
    for p in juncs: node_pts.add(key(p))
    for p in ncs: node_pts.add(key(p))
    for w in wires:
        for p in w: dsu.find(key(p))
    for a,b in segs:
        dsu.union(key(a), key(b))
        for k in node_pts:
            if on_seg(k,a,b): dsu.union(k, key(a))
    _abut(dsu, node_pts)
    names = {}
    for l in labels: names.setdefault(dsu.find(key(l['at'])), l['text'])
    power = set()
    for s in syms:
        if is_power_lib(s['lib'], pwl) and '1' in s['pins']:
            if s['lib'].split(':', 1)[-1] in NO_NAME_POWER:
                continue
            nm = s.get('val') or s['lib'].split(':', 1)[1]
            r = dsu.find(key(s['pins']['1'])); names.setdefault(r, nm); power.add(r)
    recs = {}
    for s in syms:
        if is_power_lib(s['lib'], pwl): continue
        for num,p in s['pins'].items():
            r = dsu.find(key(p)); recs.setdefault(r, {'name': names.get(r), 'power': r in power, 'pins': [], 'wires': []})
            recs[r]['pins'].append({'ref': s['id'], 'pin': num, 'at': [round(p[0],2), round(p[1],2)]})
    for w in wires:
        r = dsu.find(key(w[0]))
        if r in recs: recs[r]['wires'].append([[round(x,2), round(y,2)] for x,y in w])
    out = []
    for r,rec in recs.items():
        pts = [q['at'] for q in rec['pins']] + [p for w in rec['wires'] for p in w]
        if not pts: continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        rec.update(span=[round(max(xs)-min(xs),1), round(max(ys)-min(ys),1)],
                   n_pins=len(rec['pins']), n_wires=len(rec['wires']), root=r)
        out.append(rec)
    out.sort(key=lambda x: (x['power'], -x['n_pins']))
    return out

if __name__ == '__main__' and len(sys.argv) > 2 and sys.argv[2] == 'nets':
    recs = net_records(sys.argv[1])
    for r in recs[:14]:
        print(f"{str(r['name']):14s} pins={r['n_pins']:2d} wires={r['n_wires']:2d} span={r['span']} "
              f"members=" + ', '.join(f"{p['ref']}.{p['pin']}" for p in r['pins'][:6]))

def named_nets(path):
    recs = net_records(path)
    merged = {}
    for r in recs:
        k = r['name'] or ('~' + str(r['root']))
        m = merged.setdefault(k, {'name': r['name'], 'power': r['power'], 'pins': [], 'wires': [], 'islands': 0})
        m['pins'] += r['pins']; m['wires'] += r['wires']; m['islands'] += 1
    out = []
    for m in merged.values():
        pts = [q['at'] for q in m['pins']] + [p for w in m['wires'] for p in w]
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        m['span'] = [round(max(xs)-min(xs),1), round(max(ys)-min(ys),1)]
        m['n_pins'] = len(m['pins']); m['n_wires'] = len(m['wires'])
        out.append(m)
    out.sort(key=lambda x: (x['power'], -x['n_pins']))
    return out
