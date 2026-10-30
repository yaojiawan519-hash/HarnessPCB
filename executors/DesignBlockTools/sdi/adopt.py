#!/usr/bin/env python3
"""把"被同网铜压住却还没网名"的焊盘认领回去（2026-10-30）

## 这是什么账

块板里常有一类焊盘：**文件里没有网名，可有铜实实在在落在它上面**。最典型是散热盘
（`usb_ftdi_bridge` 的 U3 = FT232H，QFN-48 的第 49 脚 + 16 块同号无名子焊盘）——原理图上
没有它，块卡自然不写它，人画板靠整板铺铜连；从板上扣块时铺铜没了，只剩几根 GND 走线
停在这块铜上。KiCad 的 DRC 于是报 `shorting_items`：`Track [GND]` 和 `Pad 49 [<no net>]`
"短"在一起——**铜是通的、电路是对的，错的是焊盘没名字**。

2026-10-30 全库 DRC 记账时，`usb_ftdi_bridge` 的 12 处"短路"全是这一条。

## 判据（不猜）

只认**几何 + 单值**：

1. 焊盘**没有网名**；
2. 有 **走线端点或过孔中心落在它的包围盒里，且在同层上**（铜真的停在它身上，不是擦边；
   B.Cu 的线从贴片焊盘底下穿过不算——2026-10-30 `mcu_esp32s3` 的 `BTN2` 就被这么误判过）；
3. 这些铜**只有一个网名**（两个以上说明真短，报冲突不动手）；
4. 那个网名不是 KiCad 现编的 `Net-(…)`（现编名认领了也只是换来一个没用的名字）。

三条全中才写。写网用 `pad.SetNet()`，几何零改动（`NETINFO_ITEM` 不够就现建）。

## 跑法

    python3 -m sdi.adopt ../DesignBlocks              # 只报
    python3 -m sdi.adopt ../DesignBlocks --apply      # 真写
    python3 -m sdi.adopt ../DesignBlocks/usb_ftdi_bridge --apply

⚠ 两进程架构（同 `sdi/cardpcb.py`）：几何在 KiCad 自带 python 里读，写盘复用
`net_align.apply()`。
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdi import cardpcb               # noqa: E402

SCAN = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
b = pcbnew.LoadBoard(sys.argv[1])
if b is None:
    print('[]'); raise SystemExit(0)
ends = []                                  # [(点, 网名, 层)]：走线两头 + 过孔中心（层=None）
for t in b.GetTracks():
    n = str(t.GetNetname())
    if isinstance(t, pcbnew.PCB_VIA):
        p = t.GetPosition(); ends.append((p.x, p.y, n, None))
    else:
        for q in (t.GetStart(), t.GetEnd()):
            ends.append((q.x, q.y, n, t.GetLayer()))
out = []
for f in b.GetFootprints():
    # ⚠ 散热盘常是一堆同号（甚至空号）子焊盘，按 `位号.脚号` 认会互相吃掉，
    #   所以认焊盘一律用「位号 + 在本器件里的序号」
    for i, p in enumerate(f.Pads()):
        if str(p.GetNetname()):
            continue
        bb = p.GetBoundingBox()
        # **层要对上**：B.Cu 的线从 F.Cu 贴片焊盘底下穿过不算压在上面
        # （2026-10-30 mcu_esp32s3 的 BTN2 就是这么被误判成"压着"散热盘的）。
        # 过孔是通孔，任何层都算。
        hit = {n for (x, y, n, lay) in ends
               if n and x >= bb.GetLeft() and x <= bb.GetRight()
               and y >= bb.GetTop() and y <= bb.GetBottom()
               and (lay is None or p.IsOnLayer(lay))}
        out.append({'ref': f.GetReference(), 'idx': i, 'num': p.GetNumber(),
                    'hits': sorted(hit)})
print(json.dumps(out, ensure_ascii=False))
'''

APPLY = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
job = json.load(open(sys.argv[1]))
b = pcbnew.LoadBoard(job['board'])
res = {'assigned': 0, 'errors': []}
fp = {f.GetReference(): f for f in b.GetFootprints()}
for net, keys in job['assigns']:
    ni = b.FindNet(net)
    if ni is None:
        ni = pcbnew.NETINFO_ITEM(b, net)
        b.Add(ni)
        if b.FindNet(net) is None:
            res['errors'].append('建不出网 %s' % net); continue
    for ref, idx in keys:
        f = fp.get(ref)
        if f is None:
            res['errors'].append('板上没有器件 %s' % ref); continue
        pads = list(f.Pads())
        if idx >= len(pads):
            res['errors'].append('%s 焊盘数 %d，没有第 %d 个' % (ref, len(pads), idx)); continue
        p = pads[idx]
        got = str(p.GetNetname())
        if got and got != net:
            res['errors'].append('%s[%d] 已在网 %s' % (ref, idx, got)); continue
        p.SetNet(ni)
        res['assigned'] += 1
pcbnew.SaveBoard(job['out'], b)
print(json.dumps(res, ensure_ascii=False))
'''


def scan(pcb: Path) -> list[dict]:
    r = subprocess.run([cardpcb._kicad_py(), '-c', SCAN, str(pcb)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'✗ 读板失败（{pcb}）：{(r.stderr or r.stdout).strip()[-400:]}')
    line = [l for l in r.stdout.splitlines() if l.startswith('[')]
    if not line:
        raise SystemExit(f'✗ 没拿到几何：{r.stdout[-300:]}')
    return json.loads(line[-1])


def _label(it: dict) -> str:
    return f'{it["ref"]}.{it["num"] or "(%d)" % it["idx"]}'


def plan(pcb: Path) -> dict:
    adopt, clash, bare = collections.defaultdict(list), [], 0
    for it in scan(pcb):
        hits = it['hits']
        if len(hits) == 1:
            n = hits[0]
            if cardpcb._auto(n):
                bare += 1          # 只有 KiCad 现编名：认领了也没用，不算
                continue
            adopt[n].append(it)
        elif len(hits) > 1:
            clash.append('%s 上压着 %s' % (_label(it), hits))
        else:
            bare += 1
    return {'board': str(pcb),
            'assigns': sorted((k, [[i['ref'], i['idx']] for i in v]) for k, v in adopt.items()),
            'labels': {k: [_label(i) for i in v] for k, v in adopt.items()},
            'conflicts': clash, 'unnetted': bare}


def apply(p: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        jp = Path(td) / 'job.json'
        jp.write_text(json.dumps({'board': p['board'], 'assigns': p['assigns'],
                                  'out': p['board']}, ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([cardpcb._kicad_py(), '-c', APPLY, str(jp)],
                           capture_output=True, text=True)
        line = [l for l in r.stdout.splitlines() if l.startswith('{')]
        if r.returncode != 0 or not line:
            raise SystemExit(f'✗ 写板失败：{(r.stderr or r.stdout).strip()[-400:]}')
        return json.loads(line[-1])


def blocks(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if (path / f'{path.name}.kicad_pcb').is_file():          # 直接给了单个块目录
        return [path / f'{path.name}.kicad_pcb']
    return sorted(p for d in path.iterdir() if d.is_dir() and not d.name.startswith('_')
                  for p in [d / f'{d.name}.kicad_pcb'] if p.is_file())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='把被同网铜压住的无网焊盘认领到那张网上')
    ap.add_argument('path')
    ap.add_argument('--apply', action='store_true', help='真写（缺省只报）')
    ap.add_argument('--only', default='', help='只处理这些块（逗号分隔）')
    a = ap.parse_args(argv)
    only = {x.strip() for x in a.only.split(',') if x.strip()}
    files = [p for p in blocks(Path(a.path)) if not only or p.stem in only]
    if not files:
        raise SystemExit('✗ 没有可处理的块')

    n_ok = n_todo = n_bad = 0
    for pcb in files:
        p = plan(pcb)
        if not p['assigns']:
            n_ok += 1
            if p['conflicts']:
                n_bad += 1
                for c in p['conflicts']:
                    print(f'✗ {pcb.stem}：{c}')
            continue
        n_todo += 1
        n = sum(len(v) for _, v in p['assigns'])
        print(f'· {pcb.stem:22s} 认领 {n} 只无网焊盘（其余无网焊盘 {p["unnetted"]} 只没有铜落在上面）')
        for net, _ in p['assigns']:
            keys = p['labels'][net]
            print(f'   {net:10s} ← {", ".join(keys[:8])}' + (' …' if len(keys) > 8 else ''))
        for c in p['conflicts']:
            print(f'   ✗ {c}')
        if p['conflicts']:
            n_bad += 1                     # 有两只以上的网压在同一只焊盘上 = 真短，不许当没事
        if not a.apply:
            continue
        res = apply(p)
        print(f'   ✓ 设网 {res["assigned"]} 只' + (
            '' if not res['errors'] else '；✗ ' + '；'.join(res['errors'][:4])))
        left = plan(pcb)
        if left['assigns']:
            n_bad += 1
            print(f'   ✗ 还有 {sum(len(v) for _, v in left["assigns"])} 只没认领')
        if res['errors']:
            n_bad += 1
    print('—' * 60)
    print(f'{n_ok} 块不用动 / {n_todo} 块有焊盘要认领 / {n_bad} 处冲突'
          + ('' if a.apply else '（只报，没写：加 --apply）'))
    return 0 if n_bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
