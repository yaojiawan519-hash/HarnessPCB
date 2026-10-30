#!/usr/bin/env python3
"""KiCad 自己的连通性导出 → `<块>/_geom/conn.json`。

为什么要有它：折线组（哪几段铜电气相连）这套判断**不要自己算**。我手写的端点图在 411 上
把 6 组 3V3 主体铜判成了「没焊盘的落单铜」，而 kicad-cli DRC 只认 2 条断头——差的那 6 组是
「过孔/焊盘接在铜的腰上」「B.Cu 总线经孔接 F.Cu」这类只要算错一点就全歪的事。
KiCad 的 `CONNECTIVITY_DATA` 就是 DRC 用的那套引擎，直接问它。

⚠ 这个 `dump` **必须用 KiCad 自带的 python 3.9 跑**（pcbnew 只装在那里）；其余函数是纯 python，
`netpoly` 在普通 python3 下直接 import 它们拼 key。

产物形状：
    {"pcb": "...", "clusters": {"<cid>": {"pads": [...], "tracks": n}}, "cluster_of": {key: cid}}

key 的拼法（两边必须一致，`netpoly` 用同一批函数）：
    走线/弧  T|<层>|<x1>,<y1>|<x2>,<y2>      过孔  V|<x>,<y>
    焊盘     P|<位号>|<脚号>

跑法（KiCad python）：
  /Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\
Versions/3.9/bin/python3.9 -m sdi.conn <block.kicad_pcb> [-o conn.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# ----------------------------------------------------------------- key（纯 python）
def _r(v: float) -> str:
    return f'{v:.3f}'


def k_track(x1, y1, x2, y2, layer: str) -> str:
    return f'T|{layer}|{_r(x1)},{_r(y1)}|{_r(x2)},{_r(y2)}'


def k_via(x, y) -> str:
    return f'V|{_r(x)},{_r(y)}'


def k_pad(ref, num) -> str:
    return f'P|{ref}|{num}'


def default_out(pcb: Path) -> Path:
    """块目录约定：`DesignBlocks/<块>/_geom/conn.json`（跟 measured.json 同处）。"""
    d = pcb.parent
    if d.name == pcb.stem or (d / 'block.sdi.yaml').is_file():
        return d / '_geom' / 'conn.json'
    return pcb.with_suffix('.conn.json')


# ----------------------------------------------------------------- dump（KiCad python）
def dump(pcb_path, out_path) -> dict:
    import pcbnew  # 只有 KiCad 的 python 有

    board = pcbnew.LoadBoard(str(pcb_path))
    board.BuildConnectivity()
    conn = board.GetConnectivity()

    def keyof(it):
        if isinstance(it, pcbnew.PAD):
            # ⚠ PAD.GetParent() 在 KiCad 10 里给的是 BOARD_ITEM_CONTAINER，不是封装
            return k_pad(it.GetParentFootprint().GetReference(), it.GetNumber())
        if isinstance(it, pcbnew.PCB_VIA):
            p = it.GetPosition()
            return k_via(p.x / 1e6, p.y / 1e6)
        if isinstance(it, pcbnew.PCB_TRACK):
            s, e = it.GetStart(), it.GetEnd()
            lay = board.GetLayerName(it.GetLayer())
            return k_track(s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6, lay)
        return None

    parent: dict[str, str] = {}

    def find(a: str) -> str:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    items = [t for t in board.GetTracks()]
    for fp in board.GetFootprints():
        items.extend(fp.Pads())
    nb = 0
    for it in items:
        k = keyof(it)
        if k is None:
            continue
        find(k)
        for other in conn.GetConnectedItems(it):
            ok = keyof(other)
            if ok:
                union(k, ok)
        nb += 1

    # 归并成簇号
    cid_of: dict[str, int] = {}
    for k in list(parent):
        r = find(k)
        if r not in cid_of:
            cid_of[r] = len(cid_of)
    cluster_of = {k: cid_of[find(k)] for k in parent}
    clusters: dict[int, dict] = {}
    for k, cid in cluster_of.items():
        c = clusters.setdefault(cid, {'pads': [], 'tracks': 0})
        if k.startswith('P|'):
            c['pads'].append(k[2:])
        elif k.startswith('T|'):
            c['tracks'] += 1
    doc = {
        'pcb': Path(pcb_path).name,
        'items': nb,
        'clusters': {str(cid): {'pads': sorted(v['pads']), 'tracks': v['tracks']}
                     for cid, v in clusters.items()},
        'cluster_of': cluster_of,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return doc


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='sdi.conn', description='导出 KiCad 连通性簇')
    ap.add_argument('pcb')
    ap.add_argument('-o', '--out', default='')
    a = ap.parse_args(argv)
    pcb = Path(a.pcb)
    out = Path(a.out) if a.out else default_out(pcb)
    try:
        doc = dump(pcb, out)
    except ImportError:
        sys.exit('✗ 这个命令要用 KiCad 自带的 python 跑：\n'
                 '  /Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/'
                 'Python.framework/Versions/3.9/bin/python3.9 -m sdi.conn <pcb>')
    n_orphan = sum(1 for c in doc['clusters'].values() if not c['pads'])
    print(f'{doc["pcb"]}: {doc["items"]} 个铜元素 → {len(doc["clusters"])} 簇，'
          f'其中没焊盘的 {n_orphan} 簇')
    print(f'→ {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
