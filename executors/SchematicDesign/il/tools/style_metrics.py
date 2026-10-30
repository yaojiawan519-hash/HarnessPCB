#!/usr/bin/env python3
"""画法度量：不问"有没有碰到"，问"是不是照人的习惯画的"。

为什么要另立一套尺子（结果十的教训）：压盖门在人工原图上就有 648 处，
**原样复制一张人工页也会被判负 6.9 倍**。所以它只能当"有没有画崩"的体检，
不能当"像不像人"的分数。这里的四样量都是**人工页自身的分布**当基准，
看生成结果离它多远——三样都能直接从 IL 里算，不需要任何新语料。

  ① 接线方式  `wired_frac`：多脚网里，真被导线连通的那部分引脚占比。
     人尽量拉线，线拉不到才挂字；我们反过来。
  ② 标签用量  `labels_per_sym` / `labels_per_net`：每器件、每个多脚网挂几个标签。
  ③ 导线画法  `wires_per_sym` / `wire_len_per_sym` / `turns_per_wire`。
  ④ 位号方位  `ref_bearing`：位号（Reference）落在本体中心的哪一边。

用法:
  python3 tools/style_metrics.py                     # 人工 23 页 vs 生成(bind) 23 页
  python3 tools/style_metrics.py --dir /tmp/il_hgeo  # 换成别的臂
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import argparse, collections, json, statistics as st
import blocks, extract

EPS = 0.02          # 引脚落在导线上的容差（mm）
CELL = 5.0          # 空间网格边长，别让引脚×线段两两比对


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _seg_pt_dist(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:              # 退化线段（曾经把整张图并成一个网）
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5


def _q(p):
    return (round(p[0], 2), round(p[1], 2))


def wired_state(il):
    """每根引脚是否被导线连了出去。返回 (wired_pins, total_multipin_pins, net_wired 列表)。

    连线判据只有导线：标签不算（标签正是我们要量的"没拉线"）。引脚只要落在某根导线上，
    就和那根导线的两端并成一个连通块；同一个网里最大的那块 ≥2 脚才算"拉通了"。
    """
    segs, grid = [], collections.defaultdict(list)
    for w in il['wires']:
        pts = w.get('pts') or []
        for k in range(len(pts) - 1):
            a, b = tuple(pts[k]), tuple(pts[k + 1])
            if a == b:
                continue
            segs.append((a, b, len(segs)))
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for gx in range(int(x0 // CELL), int(x1 // CELL) + 1):
                for gy in range(int(y0 // CELL), int(y1 // CELL) + 1):
                    grid[(gx, gy)].append(len(segs) - 1)
    d = DSU()
    for a, b, i in segs:
        d.union(('s', i), _q(a))
        d.union(('s', i), _q(b))
    # 引脚 → 落在哪几根线上
    pin_seg = {}
    for net in il.get('nets') or []:
        for ref, num in net.get('pins') or []:
            s = next((x for x in il['symbols'] if x['id'] == ref), None)
            if not s:
                continue
            q = (s.get('pins_at') or {}).get(num)
            if not q:
                continue
            p = (q[0], q[1])
            key = (ref, num)
            hit = []
            cand = grid.get((int(p[0] // CELL), int(p[1] // CELL))) or []
            for i in cand:
                a, b, _ = segs[i]
                if (_seg_pt_dist(p, a, b) <= EPS
                        or _seg_pt_dist(p, b, a) <= EPS):
                    hit.append(i)
            pin_seg[key] = hit
            for i in hit:
                d.union(('pin', key), ('s', i))
    wired = total = 0
    per_net = []
    for net in il.get('nets') or []:
        pins = [tuple(x) for x in (net.get('pins') or [])]
        if len(pins) < 2:
            continue
        groups = collections.Counter()
        for p in pins:
            i = pin_seg.get(p) or []
            groups[d.find(('s', i[0])) if i else ('alone', len(groups), p)] += 1
        big = max(groups.values())
        got = big if big >= 2 else 0
        wired += got
        total += len(pins)
        per_net.append((net.get('name'), len(pins), got))
    return wired, total, per_net


def metrics(il):
    syms = [s for s in il['symbols'] if not s.get('power')]
    nsym = max(1, len(syms))
    wl = 0.0
    turns = 0
    for w in il['wires']:
        pts = w.get('pts') or []
        for k in range(len(pts) - 1):
            wl += abs(pts[k + 1][0] - pts[k][0]) + abs(pts[k + 1][1] - pts[k][1])
        turns += max(0, len(pts) - 2)
    wired, total, per_net = wired_state(il)
    multipin = len(per_net)
    # 位号方位：拿本体中心当原点，看位号往哪边放
    import wireup
    cen = wireup.body_center(il)
    bearing = collections.Counter()
    for s in syms:
        ref = (s.get('fields') or {}).get('Reference') or {}
        at, c = ref.get('at'), cen.get(s['id'])
        if not at or not c:
            continue
        dx, dy = at[0] - c[0], at[1] - c[1]
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            continue
        bearing['right' if abs(dx) >= abs(dy) and dx > 0 else
                'left' if abs(dx) >= abs(dy) else
                'down' if dy > 0 else 'up'] += 1
    return {
        'symbols': len(syms), 'labels': len(il['labels']), 'wires': len(il['wires']),
        'labels_per_sym': round(len(il['labels']) / nsym, 2),
        'wires_per_sym': round(len(il['wires']) / nsym, 2),
        'wire_len_per_sym': round(wl / nsym, 2),
        'turns_per_wire': round(turns / max(1, len(il['wires'])), 2),
        'wired_frac': round(wired / total, 3) if total else None,
        'labels_per_net': round(len(il['labels']) / multipin, 2) if multipin else None,
        'multipin_nets': multipin,
        'ref_bearing': dict(bearing),
        'bearing_mode': bearing.most_common(1)[0][0] if bearing else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=_os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), '..', 'preview', 'sweep'))
    ap.add_argument('--suffix', default='blind')
    ap.add_argument('--json', default='')
    a = ap.parse_args()
    rows = []
    for p in blocks.corpus_pages():
        label = blocks.page_label(p)
        base = label.split('/')[-1].replace('.kicad_sch', '')
        hum = metrics(extract.extract(p))
        gen_p = _os.path.join(a.dir, f'{base}.{a.suffix}.json')
        gen = metrics(json.load(open(gen_p, encoding='utf-8'))) \
            if _os.path.exists(gen_p) else None
        rows.append({'page': label, 'human': hum, 'gen': gen})
        if not gen:
            continue
        print(f'{label:44s} 标签/件 {hum["labels_per_sym"]:.2f}→{gen["labels_per_sym"]:.2f}  '
              f'线/件 {hum["wires_per_sym"]:.2f}→{gen["wires_per_sym"]:.2f}  '
              f'拉线率 {hum["wired_frac"]}→{gen["wired_frac"]}  '
              f'位号 {hum["bearing_mode"]}→{gen["bearing_mode"]}')

    def agg(k, side, how='median', pages=None):
        v = [r[side][k] for r in rows if r[side] and r[side][k] is not None
             and (pages is None or r['page'] in pages)]
        if not v:
            return None
        return round(st.median(v), 3) if how == 'median' else round(sum(v) / len(v), 3)

    # 两派分开看：标签派人工本来就是零导线，混在一起会把差距稀释成"没差别"
    wire_pages = {r['page'] for r in rows if r['human']['wires'] > 0}
    for name, sel in (('全 23 页', None), ('实线派 %d 页' % len(wire_pages), wire_pages)):
        print('\n【%s】中位' % name)
        print('%-22s %8s %8s' % ('指标', '人工', a.suffix))
        for k in ('labels_per_sym', 'wires_per_sym', 'wire_len_per_sym', 'turns_per_wire',
                  'labels_per_net', 'wired_frac'):
            print('%-22s %8s %8s' % (k, agg(k, 'human', pages=sel), agg(k, 'gen', pages=sel)))
    # 位号方位是分布量，看主方位占比（不是中位）
    for side in ('human', 'gen'):
        cnt = collections.Counter()
        for r in rows:
            if r[side]:
                cnt.update(r[side]['ref_bearing'])
        tot = sum(cnt.values()) or 1
        print('%-22s %8s' % ('ref_bearing 主方向', f'{cnt.most_common(1)[0][0]} '
                             f'{100 * cnt.most_common(1)[0][1] / tot:.0f}%'))
    if a.json:
        json.dump(rows, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('→', a.json)


if __name__ == '__main__':
    main()
