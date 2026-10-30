#!/usr/bin/env python3
"""只读探针：把第三方原理图/板的**结构**一次打全，供裁块时定边界（不写任何文件）。

为什么单独做一个：裁每块新板前都要问同样几个问题——
谁是功能件、页上写了哪些标题、作者有没有画分区框、网怎么连、板上件/铜/铺铜长什么样。
以前每块板现写一段一次性脚本，答案只留在那次对话里；这里统一成一条命令，
数字全部现算（**能算的不写**）。

用法：
    python3 -m sdi.pageprobe <xxx.kicad_sch>                 # 只读原理图
    python3 -m sdi.pageprobe <xxx.kicad_sch> --pcb <x.kicad_pcb>   # 连板一起看
    python3 -m sdi.pageprobe <xxx.kicad_sch> --island          # 按电路岛聚类（无分区框时用）

判读要点：
  · `图形` 那节列的是**页面级**图元（不含 `lib_symbols` 里的符号体）。作者用矩形/折线
    画分区框时，它们会以 4 顶点 polyline 或 rectangle 出现——那就是 `windows:` 的真值。
  · `网` 那节只列 ≥2 脚的网；单脚网单列一条数（NC 脚会被 kicad-cli 写成单脚网，不是电路事实）。
  · `--island` 的簇按"共享网 + 距离"贪心合并，只是**参考**：块边界是作者的设计决定，
    没有分区框时才用标题圈岛（见 carve 的 `decoration:`）。
"""
from __future__ import annotations

import sys
import collections
from pathlib import Path

from . import schwrite


def _get(node, key):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def _all(node, key):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _pts_of(node):
    """图元的顶点：polyline/`pts`、rectangle 的 start-end、text 的 at。"""
    pts = []
    p = _get(node, 'pts')
    if p:
        for xy in _all(p, 'xy'):
            pts.append((_num(xy[1]), _num(xy[2])))
    for key in ('start', 'end', 'at'):
        n = _get(node, key)
        if n:
            pts.append((_num(n[1]), _num(n[2])))
    return pts


def read_page(path: Path) -> dict:
    """**页面级图元**（`lib_symbols` 里的符号体不算）：分区框就是这里的矩形/折线。"""
    tree = schwrite.parse(path.read_text(encoding='utf-8'))
    geo, n_lib = [], 0
    libsym = _get(tree, 'lib_symbols')
    n_lib = len(libsym[1:]) if libsym else 0
    for node in tree[1:]:
        if not isinstance(node, list) or not node:
            continue
        if node[0] in ('rectangle', 'polyline', 'circle', 'arc', 'bezier'):
            p = _pts_of(node)
            if p:
                geo.append({'kind': node[0], 'pts': p})
    return {'geo': geo, 'n_lib': n_lib}


def nets_of(path: Path):
    sys.path.insert(0, str((Path(__file__).resolve().parents[2] / 'SchematicDesign' / 'il' / 'tools')))
    import extract                                            # noqa: PLC0415
    d = extract.extract(str(path))
    multi, single = {}, 0
    for k, n in enumerate(d['nets']):
        pins = n.get('pins') or []
        if len(pins) < 2:
            single += 1
            continue
        # 无名网各自一张（别都塞进同一个 None 键，那样会把几张不同的网叠成一张）
        name = n['name'] or f'<匿名{k}>'
        multi[name] = sorted(f'{r}.{p}' for r, p in pins)
    return multi, single, d


def islands(nets: dict, syms: list, max_mm: float = 12.0) -> list:
    """按"共享网 + 中心距"贪心合并成簇。只是参考值，不是块的判据。"""
    xy = {s['id']: (s['at'][0], s['at'][1]) for s in syms if s.get('at')}
    parent = {s['id']: s['id'] for s in syms}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for name, pins in nets.items():
        refs = [p.split('.')[0] for p in pins]
        for r in refs[1:]:
            if refs[0] in parent and r in parent:
                union(refs[0], r)
    groups = collections.defaultdict(list)
    for s in syms:
        groups[find(s['id'])].append(s['id'])
    return [sorted(v) for v in groups.values()]


def print_pcb(pcb: Path) -> None:
    import pcbnew                                            # noqa: PLC0415
    b = pcbnew.LoadBoard(str(pcb))
    fps = list(b.GetFootprints())
    print(f'\n## 板 {pcb.name}：{len(fps)} 封装')
    for f in sorted(fps, key=lambda f: str(f.GetReference())):
        p = f.GetPosition()
        print(f"   {str(f.GetReference()):6s} {str(f.GetFPID().GetLibItemName()):34s} "
              f"{pcbnew.ToMM(p.x):8.2f},{pcbnew.ToMM(p.y):8.2f} rot={f.GetOrientationDegrees():6.1f} "
              f"{'B' if f.IsFlipped() else 'F'}")
    segs = [t for t in b.GetTracks() if t.GetClass() == 'PCB_TRACK']
    vias = [t for t in b.GetTracks() if t.GetClass() == 'PCB_VIA']
    print(f' 走线 {len(segs)} 段（{collections.Counter(t.GetLayerName() for t in segs)}）'
          f' + 过孔 {len(vias)}')
    zones = list(b.Zones())
    print(f' 铺铜 {len(zones)} 张：')
    for z in zones:
        bb = z.GetBoundingBox()
        print(f"   {z.GetNetname():12s} {z.GetLayerName():8s} "
              f"{pcbnew.ToMM(bb.GetWidth()):6.2f}×{pcbnew.ToMM(bb.GetHeight()):6.2f}mm")
    pads = collections.Counter()
    for f in fps:
        for p in f.Pads():
            if p.GetNetname():
                pads[p.GetNetname()] += 1
    print(f' 网 {len(pads)} 张（按焊盘数）：' + '、'.join(f'{n}×{k}' for n, k in pads.most_common(12)))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='第三方原理图/板只读探针')
    ap.add_argument('sch')
    ap.add_argument('--pcb')
    ap.add_argument('--island', action='store_true', help='按共享网聚类（参考值）')
    ap.add_argument('--all-text', action='store_true')
    a = ap.parse_args(argv)
    sch = Path(a.sch)
    page = read_page(sch)
    nets, n_single, il = nets_of(sch)
    syms = [s for s in il['symbols'] if not s.get('power')]
    n_pwr = len(il['symbols']) - len(syms)

    print(f'## 页 {sch.name}  纸 {il["page"]["paper"]}  功能件 {len(syms)}（另有电源符号 {n_pwr}）'
          f'  页面图元 {len(page["geo"])}  文本 {len(il["texts"])}  标签 {len(il["labels"])}  '
          f'导线 {len(il["wires"])}  lib_symbols {page["n_lib"]}')
    print(f'## 网 {len(nets)} 张（≥2 脚），单脚网 {n_single} 张（多为 NC 脚，不是电路事实）')

    print('\n## 器件')
    for s in sorted(syms, key=lambda s: s['id']):
        x, y = s['at'][0], s['at'][1]
        print(f"   {s['id']:6s} {str(s.get('value')):18s} {s['lib']:34s} "
              f"{x:8.2f},{y:8.2f} rot={float(s.get('rot') or 0):5.1f} u{s.get('unit') or 1}"
              + (f" {s['mirror']}" if s.get('mirror') else '')
              + (f"  ({len(s.get('pins') or [])} 脚)" if len(s.get('pins') or []) > 4 else ''))

    geo = page['geo']
    if geo:
        print('\n## 页面图元（不含 lib_symbols；分区框就是 4 顶点的折线/矩形）')
        for g in geo:
            bb = _bbox(g['pts'])
            print(f"   {g['kind']:10s} {len(g['pts'])} 顶点  "
                  f"[{bb[0]:7.2f}, {bb[1]:7.2f}]-[{bb[2]:7.2f}, {bb[3]:7.2f}]  "
                  f"({bb[2] - bb[0]:.2f}×{bb[3] - bb[1]:.2f})")
    else:
        print('\n## 页面图元：**0 个**（作者没画分区框 → 按标题圈的功能岛裁，标题进 decoration:）')

    print('\n## 文本')
    for t in il['texts']:
        x, y = t['at'][0], t['at'][1]
        body = str(t['text']).replace('\n', ' / ')
        print(f'   @{x:7.2f},{y:7.2f}  {body}')

    print('\n## 标签')
    for t in il['labels']:
        x, y = t['at'][0], t['at'][1]
        print(f"   {t['kind']:8s} @{x:7.2f},{y:7.2f}  {t['text']}")

    print('\n## 网（≥2 脚）')
    for n, pins in sorted(nets.items(), key=lambda kv: str(kv[0])):
        print(f'   {str(n):16s} {len(pins):2d} 脚  ' + '、'.join(pins))

    if a.island:
        print('\n## 电路岛（按共享网聚类，**参考**）')
        for i, g in enumerate(sorted(islands(nets, syms), key=lambda g: -len(g)), 1):
            print(f'   {i:2d}. {len(g):2d} 件：' + '、'.join(g))

    if a.pcb:
        print_pcb(Path(a.pcb))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
