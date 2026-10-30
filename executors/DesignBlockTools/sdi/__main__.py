#!/usr/bin/env python3
"""sdi 命令行：`python3 -m sdi <check|build|verify|doctor|schematic> <板名>.sdi.yaml`

  check     语法 + 三条 lint + 展开 + 门1 回读（不出文件）
  build     再写出 <板名>.design.json（pcbstudio.design.v1，能直接喂 d2s）
  verify    build + 门2 分区对比（--against 一份旧拼装的 design.json）
  doctor    门0 库自检（块卡自己的账对不对）
  schematic 出整张原理图：块卡取图形 → 摆位 → 接线 → SVG + 指标 + 门3 分区门
  blocks    原样平移路线：块内一个坐标都不改，出 sch.svg + sch.kicad_sch + 逐图元门

另有几条不进这张表的入口（参数不是板名，各自独立）：
    python3 -m sdi.carve <裁块规范>.yaml --out <目录>     人画页 → 块（五道门）
    python3 -m sdi.block_sch <块卡规范>.yaml              块 → **只含原理图**的块卡（两道门）
    python3 -m sdi.blockpcb                               块 → 连同板级几何的完整块（重，要布线）
    python3 -m sdi.legacy_sch <老.sch> --lib <cache.lib> -o <新.kicad_sch>
                                                          KiCad 5 → 10 转换器（五道门 + 符号体检）
    python3 -m sdi.renderdiff <裁块规范>.yaml              原页 vs 块 逐图元比（"完全截取"的定义）
    python3 -m sdi.libdiff <新.kicad_sch>                  转出的符号 vs KiCad 自带库（体检，非门）
    python3 -m sdi.checkall ../DesignBlocks                块库四道门一次跑完（卡↔板 / 卡回读 / 短路 / 空线头）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gates, spec as S
from .expand import ExpandError, expand
from .library import Library, SpecLibrary

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path):
    sp = S.load(str(path))
    base = Library(ROOT)
    lib = SpecLibrary(base, sp.get('defs') or {}, sp.get('library') or {},
                      surgery=sp.get('surgery') or {}, modules=sp.get('modules') or {})
    return sp, lib


def cmd_check(path: Path, quiet: bool = False):
    sp, lib = _load(path)
    probs = S.lint(sp, lib)
    errs = [p for p in probs if p.level == 'error']
    warns = [p for p in probs if p.level == 'warn']
    for p in errs + warns:
        print(' ', p)
    if errs:
        print(f'✗ {path.name}: {len(errs)} 个错误 / {len(warns)} 个警告')
        raise SystemExit(1)
    try:
        nl = expand(sp, lib)
    except ExpandError as e:
        for m in e.errors:
            print(f'  [error] expand: {m}')
        print(f'✗ {path.name}: 展开失败')
        raise SystemExit(1)
    rbs = gates.readback(sp, nl, lib)
    for m in rbs:
        print(f'  [error] gate1: {m}')
    if rbs:
        print(f'✗ {path.name}: 门1 回读 {len(rbs)} 处不一致')
        raise SystemExit(1)
    if not quiet:
        n_pins = sum(len(v) for v in nl.nets.values())
        print(f'✓ {path.name}: 模块 {len(sp["modules"])} / 网 {len(nl.nets)} / 引脚 {n_pins}'
              f' / 丢弃单端 {len(nl.dropped)} / 警告 {len(warns)}')
        planes = (sp.get('board') or {}).get('planes') or {}
        if planes:
            print('  平面网：' + '、'.join(f'{n}→{ly}' for n, ly in planes.items()))
        lic = S.license_rollup(sp, sp['modules'], lib)
        if lic['internal']:
            print(f'  许可：整板 {lic["tier"]}（internal 块：{"、".join(lic["internal"])}）')
        else:
            print(f'  许可：整板 {lic["tier"]}')
        for w in nl.warnings:
            print(f'  [warn] {w}')
        if nl.aliases:
            print('  别名：' + '; '.join(f'{k} ← {sorted(v)}' for k, v in sorted(nl.aliases.items())))
        if sp.get('defs'):
            print(f'  就地定义模块 {sorted(sp["defs"])}（几何待造，见 sdi build --defs）')
    return sp, nl, lib


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='sdi', description='模块化设计语言（YAML）工具')
    ap.add_argument('cmd', choices=['check', 'build', 'verify', 'doctor', 'schematic', 'blocks',
                                    'card', 'netpoly', 'conn', 'wires'])
    ap.add_argument('file', nargs='?', default='')
    ap.add_argument('--init', action='store_true',
                    help='card：从现有两份 json 生成 block.sdi.yaml（一次性迁移）')
    ap.add_argument('--fix-internal', action='store_true',
                    help='card：把漏登记的无名内部网补进 internal_nets（2026-10-30 之前的老卡）')
    ap.add_argument('--out', default='')
    ap.add_argument('--root', default='', help='模块库根目录（默认 DesignBlockTools）')
    ap.add_argument('--no-place', action='store_true', help='不摆位，只按声明序排（对照用）')
    ap.add_argument('--against', default='', help='旧拼装 design.json，做门2 分区对比')
    ap.add_argument('--public', action='store_true',
                    help='按公开发行包的标准出（含 internal 块就拒）')
    a, extra = ap.parse_known_args(argv)

    if a.cmd in ('netpoly', 'conn', 'wires'):
        # 这几个吃 .kicad_pcb 而不是 spec，参数面也不同，直接把剩下的交给它们
        from . import conn, netpoly, wires
        mod = {'conn': conn, 'netpoly': netpoly, 'wires': wires}[a.cmd]
        return mod.main(([a.file] if a.file else []) + extra)

    if a.cmd == 'card':
        from . import card
        if not a.file:
            print('✗ card 需要一个块目录（或装着 *.kicad_block 的根目录）')
            return 1
        if a.fix_internal:
            return card.fix_auto_internal(Path(a.file))
        return card.init(Path(a.file)) if a.init else card.check(Path(a.file))

    if a.cmd == 'doctor':
        from .library import Library
        probs = gates.doctor(Library(ROOT))
        for m in probs:
            print(' ', m)
        errs = [m for m in probs if m.startswith('[error]')]
        print(('✗' if errs else '✓') + f' 库自检：{len(errs)} 个错误 / {len(probs) - len(errs)} 个警告')
        return 1 if errs else 0

    path = Path(a.file)
    sp, nl, lib = cmd_check(path)

    if a.cmd == 'check':
        if a.public:
            lic = S.license_rollup(sp, sp['modules'], lib)
            if lic['tier'] != 'public':
                print(f'✗ --public：整板许可为 {lic["tier"]}，含 internal 块 '
                      f'{"、".join(lic["internal"])}')
                return 1
        return 0

    if a.cmd == 'blocks':
        from . import kicadblocks, schwrite
        out = Path(a.out) if a.out else Path('build') / 'blocks' / path.stem.split('.')[0]
        rep = kicadblocks.assemble(path, out)
        sch = schwrite.build(path, out / 'sch.kicad_sch')
        chk = schwrite.verify(out / 'sch.il.json', out / 'sch.kicad_sch')
        ok = all(g['mismatch'] == 0 for g in rep['identity'].values())
        print(f'{"✓" if ok else "✗"} 逐图元门 {sum(g["primitives"] for g in rep["identity"].values())}'
              f' 图元 / 不一致 {sum(g["mismatch"] for g in rep["identity"].values())}；'
              f'KiCad 原理图 {sch["path"]}')
        print(('✓' if chk['ok'] else '✗') + ' 回读门：写出的 .kicad_sch 读回来与拼装结果一致'
              if chk['ok'] else '✗ 回读门：写出的 .kicad_sch 与拼装结果不一致')
        return 0 if ok and chk['ok'] else 1

    if a.cmd == 'schematic':
        from .draw import assemble
        out = Path(a.out) if a.out else Path('build') / 'sch' / path.stem.split('.')[0]
        rep = assemble(path, out, root=Path(a.root) if a.root else ROOT,
                       place=not a.no_place)
        return 0 if rep['gate3_partition']['ok'] else 1

    # 许可层级跟着 design.json 走：出公开发行包的那步只看这一栏就有东西可拒，
    # 不用去翻块卡的散文（`provenance.license`）或报告脚注。
    lic = S.license_rollup(sp, sp['modules'], lib)
    if a.public and lic['tier'] != 'public':
        print(f'✗ --public：整板许可为 {lic["tier"]}，含 internal 块 '
              f'{"、".join(lic["internal"])}；不许进公开发行包')
        return 1
    design = nl.to_design(sp)
    design['license'] = lic
    out = Path(a.out) if a.out else path.with_suffix('.design.json')
    out.write_text(json.dumps(design, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'→ {out}')

    if a.cmd == 'verify':
        if not a.against:
            print('✗ verify 需要 --against <design.json>')
            return 1
        r = gates.compare_partition(nl, a.against)
        print(f'  旧板 {r["ref_nets"]} 网 / {r["ref_pins"]} pin；'
              f'本语言 {r["our_nets"]} 网 / {r["our_pins"]} pin；同网同 pin 组 {r["common"]}')
        if r['only_ref']:
            print(f'  旧板有、我们没有 {len(r["only_ref"])} 组：')
            for g in r['only_ref'][:10]:
                print('    ', g)
        if r['only_ours']:
            print(f'  我们有、旧板没有 {len(r["only_ours"])} 组：')
            for g in r['only_ours'][:10]:
                print('    ', g)
        if r['only_ref'] or r['only_ours']:
            print('✗ 门2 分区不一致')
            return 1
        print('✓ 门2 分区一致（逐 pin 同一套网）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
