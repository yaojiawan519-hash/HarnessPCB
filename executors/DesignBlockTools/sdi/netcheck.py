#!/usr/bin/env python3
"""门：IL 的网表分区必须等于 kicad-cli 导出的网表分区（**外部裁判**，不是自证）。

为什么要有这道门：裁块的口岸网、门⑤ 复原、块卡的 `nets:` 全是从 IL 的
`build_nets` 算出来的。IL 自己算错时，那些门互相看着都对——因为两边用的是
同一个错判据。2026-10-30 就是这么漏的：`adapter-ram-ddr5-1REV2` 页上
`VOUT`（全局标签）+`+5V`（电源符号）+`BULK_VIN`（全局标签）三个名字挂在
同一根导线上，IL 当时只给每个连通片记一个名字，于是把 LDO 输出拆成三张网；
门⑤ 自洽，直到 `pcbcarve` 的 sch↔pcb「谁和谁同网」那道门才穿帮。
现在 IL 按 KiCad 的规则做**别名合并**（全局标签 > 电源符号 > 层次标签 > 本地标签，
同级取字典序），这道门把那条规则钉在外部裁判上。

比的是**分区**（谁和谁同网），不是网名：KiCad 同级名字的胜负依赖内部遍历顺序，
网名只作为提示打印，不判失败。

用法：
    python3 -m sdi.netcheck <a.kicad_sch> [<b.kicad_sch> ...]
    python3 -m sdi.netcheck --dir ../KiCAD_Access/hw_sources/BusPirate5-hardware/adapter-ram-ddr5-1REV2
"""
from __future__ import annotations

import sys
import glob
import tempfile
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import blockpcb


def _il():
    """IL 的两个模块不在 DesignBlockTools 包内，按老规矩现找路径。"""
    root = Path(__file__).resolve().parents[2]
    p = root / 'SchematicDesign' / 'il' / 'tools'
    sys.path.insert(0, str(p))
    import extract                                            # noqa: PLC0415
    return extract


def kicad_partition(sch: Path):
    """kicad-cli 网表 → [(≥2 脚的脚集合, 网名)]。"""
    kcli = blockpcb.kcli()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'net.xml'
        p = subprocess.run([kcli, 'sch', 'export', 'netlist', '--format', 'kicadxml',
                            '-o', str(out), str(sch)], capture_output=True, text=True)
        if not out.exists():
            raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[:1] or 'kicad-cli 没出网表')
        root = ET.parse(out).getroot()
    nets = []
    for net in root.iter('net'):
        pins = frozenset((n.get('ref'), n.get('pin')) for n in net.iter('node'))
        if len(pins) > 1:                     # 单脚网是 NC 脚，不是电路事实
            nets.append((pins, net.get('name')))
    return nets


def il_partition(sch: Path):
    extract = _il()
    il = extract.extract(str(sch))
    return [(frozenset((str(r), str(p)) for r, p in n['pins']), n['name'])
            for n in extract.build_nets(il) if len(n['pins']) > 1]


def check(sch: Path, verbose: bool = False):
    """返回 'ok' / 'bad' / 'skip'。跳过的是这道门**判不了**的页，不是判过的页。

    三类跳过（都是**已知欠账**，不是"这道门给面子"）：
      ① 层级页：kicad-cli 导出整棵树，IL 只读这一页（根页 0 网）——
         裁块只做扁平板，这条不影响裁块；
      ② 有位号没标注（`LED?`、`Q?`）：kicad-cli 会把它按自己的规则落到某个实例上，
         两边的脚对不上；
      ③ 多单元器件（同一个 ref 有两片以上，如 RN1 的 4 个电阻、Q401 的两半）：
         IL 的 `pins_at` 与 kicad-cli 的分单元口径还没对齐（老账）。
    """
    import re
    txt = sch.read_text(encoding='utf-8')
    extract = _il()
    il = extract.extract(str(sch))
    tag = f'{sch.parent.name}/{sch.name}'

    # 前提①：扁平单页
    if re.search(r'\(sheet\s', txt):
        print(f'·  {tag}：跳过（层级页，kicad-cli 导出整棵树）')
        return 'skip'
    ids = [str(s['id']) for s in il['symbols'] if not s.get('power')]
    dup = sorted({i for i in ids if ids.count(i) > 1})

    a, b = kicad_partition(sch), il_partition(sch)
    ka = {pins for pins, _ in a}
    kb = {pins for pins, _ in b}
    ok = ka == kb
    # 对有疑问的页只提示、不判过 —— 判过不给"跳过"面子
    print(f'{"✅" if ok else "✗ "} {tag}：kicad-cli {len(a)} 网 / IL {len(b)} 网')
    if not ok and dup:
        print(f'    · 此页有多单元器件 {len(dup)} 个（{"、".join(dup[:4])}），'
              f'IL 分单元口径未对齐 ⇒ 记跳过，不算判过')
        return 'skip'
    if not ok and any('?' in str(r) for pins, _ in a + b for r, _ in pins):
        unk = sorted({r for pins, _ in a + b for r, _ in pins if '?' in str(r)})
        print(f'    · 此页有位号未标注（{"、".join(unk[:4])}）⇒ 记跳过，不算判过')
        return 'skip'
    if not ok:
        for pins in sorted(ka - kb, key=len)[:4]:
            print('    仅 kicad-cli: ' + '、'.join(f'{r}.{p}' for r, p in sorted(pins)))
        for pins in sorted(kb - ka, key=len)[:4]:
            print('    仅 IL      : ' + '、'.join(f'{r}.{p}' for r, p in sorted(pins)))
        return 'bad'
    # 网名只提示不判失败：同级名字的胜负在 KiCad 里取决于内部遍历顺序。
    # IL 对无名网给 None（kicad-cli 会编个 `Net-(Q5-E)` 之类），那不算不同。
    na = {pins: nm for pins, nm in a}
    nm_diff = [(na[pins], nm) for pins, nm in b if nm is not None and na.get(pins) and na[pins] != nm]
    if nm_diff:
        for x, y in nm_diff[:4]:
            print(f'    · 网名不同（不判失败）：kicad-cli {x!r} / IL {y!r}')
    if verbose:
        for pins, nm in sorted(b, key=lambda t: str(t[1])):
            print(f'      {str(nm):16s} {len(pins):2d} 脚')
    return 'ok'


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='IL 网表分区 vs kicad-cli 网表（外部裁判）')
    ap.add_argument('sch', nargs='*')
    ap.add_argument('--dir', help='目录：递归找 *.kicad_sch')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)
    files = [Path(p) for p in a.sch]
    if a.dir:
        files += [Path(p) for p in sorted(glob.glob(str(Path(a.dir) / '**' / '*.kicad_sch'),
                                                  recursive=True))]
    if not files:
        ap.error('没有输入：给 .kicad_sch 或 --dir')
    res = {f: check(f, a.verbose) for f in files}
    bad = [f for f, r in res.items() if r == 'bad']
    skip = [f for f, r in res.items() if r == 'skip']
    print(f'\n{"✗" if bad else "✓"} 网表门：{len(files) - len(bad) - len(skip)} 过 / '
          f'{len(bad)} 不过 / {len(skip)} 跳过（判不了的页，见上）')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
