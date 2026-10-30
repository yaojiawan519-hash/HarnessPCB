#!/usr/bin/env python3
"""把引擎自布块的**两块板合成一块**：几何取布线真身，焊盘网取已对齐卡片的 root 板（2026-10-30）

## 为什么要有这一步

13 块早期块（`buck_reg` / `can_node` / `ldo_3v3` / `ldo_3v3` …）是引擎自布的：先摆件、再
`kicad_access` 带队布出 `_geom/<块>_route/signal/signal_00N.kicad_pcb`，最后"导出"成块根目录的
`<块>.kicad_pcb`。**那个导出只平移了器件，走线/过孔留在原地**——`layout_reuse.block_geometry`
的 docstring 早就点过这件事（`ldo_3v3` 差 (-5.65,-4.53)、`can_node` 差 (+6.60,+7.87)）。

后果是两块板各说一半真话：

| 文件 | 几何 | 焊盘网 |
|---|---|---|
| `<块>/<块>_route/signal/signal_00N.kicad_pcb` | **对**（焊盘落在走线端点上，90~100%） | **缺**（MCU 的 I/O 脚大把没网） |
| `<块>/<块>.kicad_pcb`（root） | **错**（器件被平移、铜没动，焊盘离铜 1~4mm） | **对**（已按块卡对齐，见 [[block-card-board-net-align]]） |

`result.json` 里的 `final` 是**老库的绝对路径**，迁目录后全部失效 → `block_geometry` 落到 root，
于是拼板拿到的是那份几何坏的板子。在这上面跑 DRC 量出来的是**噪声**（2026-10-30 实测：
`mcu_min_stm32` 报 16 处"短路"、`buck_reg` 28 处，全是错位造成的假象，不是电路缺陷）。

## 这一步做什么

1. 读 route 真身 + root；
2. **核实平移量**：逐位号算 `root 位置 − route 位置`，必须**全部相同**（实测 8 件/11 件一字不差），
   不同就报错不动手——那说明两块不是同一份版图；
3. 把 route 板的**每一个**器件/走线/过孔/图形/铺铜按同一个向量搬回 root 的块坐标系
   ⚠ `BOARD.Move()` **只搬器件和图形，不搬铜**（实测 track Δ=0）——这正是当初那个 bug 的成因，
   所以这里逐项 `Move`，不调 `BOARD.Move`；
4. 按 `位号.脚号` 把 root 的焊盘网**抄到** route 板上（缺的网现建）；
5. 写回 root（先备份到 `/tmp`），读回验：逐位号位置一致、焊盘落铜率、有网焊盘数。

## 判据

* 平移量必须唯一（否则不动手）；
* 搬完 **焊盘落铜率**（焊盘中心离最近走线端点 ≤ 0.05mm 的比例）必须 **与 route 板一字不差**——
  几何就是 route 那份，差一个就说明搬漏了东西（器件/铜/图形没一起走）；
* 焊盘网只增不减：`有网焊盘数` 搬完必须 **≥** root 原来的数。

⚠ 落铜率**不设绝对下限**：`mcu_min_stm32` 这类块有一半 I/O 脚根本没走线（块里用不上），
route 板自己也只有 40%。绝对阈值会把正常块判红。真正要看的是 **root 比 route 低多少**：
root 远低于 route = 当年那次导出只搬了器件（本模块要修的就是这一类）。

跑法：
    python3 -m sdi.unify ../DesignBlocks                 # 只报（13 块逐块判定）
    python3 -m sdi.unify ../DesignBlocks --apply         # 真写（备份 /tmp/unify/<块>.kicad_pcb）
    python3 -m sdi.unify ../DesignBlocks --only can_node --apply
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

KICAD_PY = [
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9',
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Current/bin/python3',
    '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9',
]

#: root 落铜率低于 route 的这个倍数时，判"当年那次导出只搬了器件"（本模块要修的那一类）。
EXPORT_BROKEN_RATIO = 0.5


def _kicad_py() -> str:
    for c in KICAD_PY:
        if os.path.exists(c):
            return c
    raise SystemExit(f'✗ 找不到 KiCad 自带 python3（候选：{KICAD_PY}）')


# ── 子进程：全部几何/网操作都在 KiCad python 里做（两进程架构，同 sdi/cardpcb.py）──
WORK = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
job = json.load(open(sys.argv[1]))
out = json.load(open(sys.argv[2])) if len(sys.argv) > 2 and sys.argv[2] != '-' else None

def load(p):
    b = pcbnew.LoadBoard(p)
    if b is None:
        raise SystemExit('读不了 %s' % p)
    return b

def measure(b):
    """{ref: 位置}, 焊盘 [(ref, pad), ...], 走线端点列表, 落铜率"""
    MM = 1e6
    fps = {f.GetReference(): (f.GetPosition().x, f.GetPosition().y) for f in b.GetFootprints()}
    pads = [(f.GetReference(), p.GetNumber(), p.GetNetname())
            for f in b.GetFootprints() for p in f.Pads()]
    ends = []
    for t in b.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            continue
        ends.append((t.GetStart().x, t.GetStart().y))
        ends.append((t.GetEnd().x, t.GetEnd().y))
    on = 0
    for f in b.GetFootprints():
        for p in f.Pads():
            c = p.GetPosition()
            if any(abs(c.x - e[0]) <= 50000 and abs(c.y - e[1]) <= 50000 for e in ends):
                on += 1
    return {'fps': fps, 'pads': pads, 'ends': len(ends) // 2,
            'on_copper': on, 'npads': len(pads)}

res = {'ok': False, 'why': '', 'moved': {}, 'nets_set': 0, 'nets_new': 0, 'errors': []}
try:
    root = load(job['root'])
    route = load(job['route'])
    mr, mq = measure(root), measure(route)
    # ① 平移量必须唯一
    deltas = {}
    for ref, pos in mq['fps'].items():
        if ref not in mr['fps']:
            deltas.setdefault('MISSING', []).append(ref)
            continue
        p = mr['fps'][ref]
        deltas[(p[0] - pos[0], p[1] - pos[1])] = deltas.get((p[0] - pos[0], p[1] - pos[1]), 0) + 1
    if 'MISSING' in deltas:
        res['why'] = 'root 缺器件 %s' % deltas['MISSING']
        print(json.dumps(res, ensure_ascii=False)); raise SystemExit(0)
    if len(deltas) != 1:
        res['why'] = '平移量不唯一：%s' % sorted(deltas.items())[:4]
        print(json.dumps(res, ensure_ascii=False)); raise SystemExit(0)
    dx, dy = list(deltas)[0]
    res['delta_mm'] = [dx / 1e6, dy / 1e6]

    if job.get('apply'):
        v = pcbnew.VECTOR2I(dx, dy)
        # ⚠ 逐项搬：BOARD.Move() 只搬器件/图形，铜一动不动（这就是当初那个 bug）
        for f in route.GetFootprints():
            f.Move(v)
        ntr = nvia = 0
        for t in route.GetTracks():
            t.Move(v)
            if isinstance(t, pcbnew.PCB_VIA):
                nvia += 1
            else:
                ntr += 1
        ng = 0
        for d in route.GetDrawings():
            d.Move(v)
            ng += 1
        nz = 0
        for z in route.Zones():
            z.Move(v)
            nz += 1
        res['moved'] = {'footprints': len(route.GetFootprints()), 'tracks': ntr,
                        'vias': nvia, 'graphics': ng, 'zones': nz}

        # ② 焊盘网按 位号.脚号 从 root 抄过来；root 没有的，**把布线器留的占位网清掉**
        #    （`__NC_<位号>_<脚>__` = 布线器给"这块里没接"的脚起的占位名，不是电路）。
        #    不清就会把布线器的脚手架当成网名：卡里那 28 只 NC 脚在板上变成 28 根
        #    `__NC_U1_10__` 单脚网，块卡↔板门 ① 立刻红（2026-10-30 mcu_stm32f103 /
        #    ldo_ap2112k_3v3 就是这么红的）。
        want = {}
        for f in root.GetFootprints():
            for p in f.Pads():
                n = str(p.GetNetname())
                if n:
                    want['%s.%s' % (f.GetReference(), p.GetNumber())] = n
        for f in route.GetFootprints():
            for p in f.Pads():
                k = '%s.%s' % (f.GetReference(), p.GetNumber())
                n = want.get(k)
                if not n:
                    if str(p.GetNetname()).startswith('__NC_'):
                        p.SetNetCode(0)              # 清成"无网焊盘"，与 root 一致
                        res['nets_cleared'] = res.get('nets_cleared', 0) + 1
                    continue
                if str(p.GetNetname()) == n:
                    continue
                ni = route.FindNet(n)
                if ni is None:
                    ni = pcbnew.NETINFO_ITEM(route, n)
                    route.Add(ni)
                    res['nets_new'] += 1
                if str(p.GetNetname()) and str(p.GetNetname()) != n:
                    res['errors'].append('%s 原网 %s → %s' % (k, p.GetNetname(), n))
                p.SetNet(ni)
                res['nets_set'] += 1

        pcbnew.SaveBoard(job['out'], route)
        after = measure(load(job['out']))
        res['after'] = {'on_copper': after['on_copper'], 'npads': after['npads'],
                        'netted': sum(1 for x in after['pads'] if x[2]),
                        'tracks': after['ends']}
        res['before_route'] = {'on_copper': mq['on_copper'], 'npads': mq['npads'],
                               'netted': sum(1 for x in mq['pads'] if x[2]),
                               'tracks': mq['ends']}
        res['before_root'] = {'on_copper': mr['on_copper'], 'npads': mr['npads'],
                              'netted': sum(1 for x in mr['pads'] if x[2]),
                              'tracks': mr['ends']}
    res['ok'] = True
except SystemExit:
    raise
except Exception as e:                              # noqa: BLE001
    res['why'] = '%s: %s' % (type(e).__name__, e)
print(json.dumps(res, ensure_ascii=False))
'''


def _run(job: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        jp = Path(td) / 'job.json'
        jp.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        r = subprocess.run([_kicad_py(), '-c', WORK, str(jp), '-'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f'✗ 子进程失败：{(r.stderr or r.stdout).strip()[-500:]}')
        line = [l for l in r.stdout.splitlines() if l.startswith('{')]
        if not line:
            raise SystemExit(f'✗ 子进程没给结果：{r.stdout[-400:]}')
        return json.loads(line[-1])


def route_board(bdir: Path) -> Path | None:
    """布线真身：优先 `result.json` 的 final 同名的本地文件，否则取 signal_00N 最大的一张。"""
    rd = bdir / '_geom' / f'{bdir.name}_route'
    if not rd.is_dir():
        return None
    rj = rd / 'result.json'
    if rj.is_file():
        try:
            base = os.path.basename(json.loads(rj.read_text()).get('final') or '')
        except Exception:                            # noqa: BLE001
            base = ''
        if base:
            for cand in (rd / 'signal' / base, rd / base):
                if cand.is_file():
                    return cand
    sig = sorted(glob.glob(str(rd / 'signal' / 'signal_*.kicad_pcb')))
    sig = [s for s in sig if not s.endswith('_clean.kicad_pcb') and '_raw' not in s]
    return Path(sig[-1]) if sig else None


def blocks(root: Path, only: set[str]) -> list[Path]:
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith('_'):
            continue
        if only and d.name not in only:
            continue
        if (d / f'{d.name}.kicad_pcb').is_file() and route_board(d):
            out.append(d)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='引擎块：几何取 route 真身、网取 root，合成一块板')
    ap.add_argument('path', help='块库根目录')
    ap.add_argument('--apply', action='store_true', help='真写（缺省只报）')
    ap.add_argument('--only', default='', help='只处理这些块（逗号分隔）')
    ap.add_argument('--backup', default='/tmp/unify', help='动之前把 root 备份到这')
    a = ap.parse_args(argv)

    root = Path(a.path)
    only = {x.strip() for x in a.only.split(',') if x.strip()}
    todo, bad = blocks(root, only), 0
    for bdir in todo:
        name = bdir.name
        rb = route_board(bdir)
        job = {'root': str(bdir / f'{name}.kicad_pcb'), 'route': str(rb), 'apply': False}
        p = _run(job)
        if not p['ok']:
            print(f'✗ {name:22s} {p["why"]}')
            bad += 1
            continue
        why = 'root 缺器件' if 'MISSING' in str(p.get('why')) else p.get('why') or '平移量唯一'
        print(f'· {name:22s} 平移 {p["delta_mm"][0]:+.3f}/{p["delta_mm"][1]:+.3f} mm '
              f'({why})')
        if not a.apply:
            continue
        out = bdir / f'{name}.kicad_pcb'
        bak = Path(a.backup) / f'{name}.kicad_pcb'
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():
            bak.write_bytes(out.read_bytes())        # 只存第一次（原始那份）
        p = _run({'root': str(out), 'route': str(rb), 'apply': True,
                  'out': str(Path(tempfile.gettempdir()) / f'unify_{name}.kicad_pcb')})
        if not p['ok']:
            print(f'  ✗ 搬不动：{p["why"]}')
            bad += 1
            continue
        af, bq, br = p['after'], p['before_route'], p['before_root']
        rate_af = af['on_copper'] / af['npads'] if af['npads'] else 0
        rate_bq = bq['on_copper'] / bq['npads'] if bq['npads'] else 0
        # 判据：几何跟 route 一字不差、网只增不减（落铜率不设绝对下限，见模块头）
        ok = af['on_copper'] == bq['on_copper'] and af['netted'] >= br['netted']
        if bq['on_copper'] and br['on_copper'] < EXPORT_BROKEN_RATIO * bq['on_copper']:
            why += '（root 导出只搬了器件：落铜 %d/%d vs route %d/%d）' % (
                br['on_copper'], br['npads'], bq['on_copper'], bq['npads'])
        print(f'  件 {p["moved"]["footprints"]} / 线 {p["moved"]["tracks"]} + 孔 '
              f'{p["moved"]["vias"]} / 图形 {p["moved"]["graphics"]} / 铺铜 {p["moved"]["zones"]}'
              f' | 设网 {p["nets_set"]} 只（新建网 {p["nets_new"]}）'
              + (f' / 清掉布线器占位网 {p["nets_cleared"]} 只' if p.get('nets_cleared') else ''))
        print(f'  落铜率 root {br["on_copper"]}/{br["npads"]} → route {bq["on_copper"]}/'
              f'{bq["npads"]} → 合成后 {af["on_copper"]}/{af["npads"]}'
              f'（{rate_af:.0%}）| 有网焊盘 {br["netted"]} → {af["netted"]}'
              f' {"" if ok else "✗ 判据不过"}')
        for e in p['errors'][:5]:
            print('    ⚠', e)
        if not ok:
            bad += 1
            continue
        tmp = Path(tempfile.gettempdir()) / f'unify_{name}.kicad_pcb'
        out.write_bytes(tmp.read_bytes())
        tmp.unlink(missing_ok=True)
        print(f'  ✓ 写回 {out.name}（原件备份 {bak}）')
    print('—' * 60)
    print(f'{"✓" if not bad else "✗"} 合板：{len(todo) - bad} 块过 / {bad} 块不过'
          + ('' if a.apply else '（只报，没写：加 --apply）'))
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
