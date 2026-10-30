#!/usr/bin/env python3
"""渲染对账：原页和块各让 KiCad 渲一遍，比窗口里的图元集合。

`carve` 的门② 比的是"我搬过去的图元和原文一样"——进出都是**同一套解析器**，所以它
只能证明我没搬错，不能证明两边画出来一样。这条换个裁判：两张 SVG 都是 KiCad 自己画的，
我只数窗口里有没有多一个、少一个。

判据是集合相等，不是"看着像"：窗口里每一个锚点（`<path d="Mx y …">` 的第一个点、
`<text x= y=>` 的落点……）两边一模一样。2026-10-29 之前的窗口掩膜是按像素差找的，
宽度 2mm 的分区标题整条漏过去都没被发现；图元集合比没有这个问题。

用法：
    python3 -m sdi.renderdiff specs/carve_stm32f405.yaml [--out out/carve_stm32f405]
    python3 -m sdi.renderdiff --page a.kicad_sch --block b.kicad_sch --window 0,0,10,10

退出码非 0 = 有窗口对不上。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

# kicad-cli 的常见位置（本机 KiCad 装在外挂盘上）
CLI_CANDIDATES = ('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
                  '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')

RE_EL = re.compile(r'<(path|circle|text|rect|polyline)\b([^>]*)>')
RE_PATH = re.compile(r'd="M(-?[\d.]+)[ ,](-?[\d.]+)')
RE_XY = re.compile(r'x="(-?[\d.]+)" y="(-?[\d.]+)"')


def kicad_cli() -> str | None:
    import shutil
    for p in (shutil.which('kicad-cli'),) + CLI_CANDIDATES:
        if p and Path(p).is_file():
            return p
    return None


def render_svg(sch) -> str:
    """一张原理图 → SVG 文本。kicad-cli 一次导出，临时目录随用随删。"""
    cli = kicad_cli()
    if not cli:
        raise SystemExit('✗ 找不到 kicad-cli（找过 PATH 和 %s）' % '、'.join(CLI_CANDIDATES))
    with tempfile.TemporaryDirectory(prefix='sdirenderdiff-') as td:
        r = subprocess.run([cli, 'sch', 'export', 'svg', '-o', td, str(sch)],
                           capture_output=True, text=True)
        svgs = sorted(Path(td).glob('*.svg'))
        if r.returncode != 0 or not svgs:
            raise SystemExit(f'✗ 渲不出来 {sch}：{(r.stderr or r.stdout).strip()[:300]}')
        return svgs[0].read_text(encoding='utf-8', errors='replace')


def elements(svg: str, windows=None) -> dict:
    """SVG → {(标签, x, y): 个数}。给了窗口就只数落在窗口里的。"""
    def inside(x, y):
        return any(a <= x <= c and b <= y <= d for a, b, c, d in windows)

    out = {}
    for m in RE_EL.finditer(svg):
        at = RE_PATH.search(m.group(2)) or RE_XY.search(m.group(2))
        if not at:
            continue
        x, y = float(at.group(1)), float(at.group(2))
        if windows is not None and not inside(x, y):
            continue
        k = (m.group(1), round(x, 2), round(y, 2))
        out[k] = out.get(k, 0) + 1
    return out


def compare(page_svg: str, block_svg: str, windows) -> dict:
    p = elements(page_svg, windows)
    b = elements(block_svg, windows)
    only_p = {k: v - b.get(k, 0) for k, v in p.items() if v > b.get(k, 0)}
    only_b = {k: v - p.get(k, 0) for k, v in b.items() if v > p.get(k, 0)}
    return {'page': sum(p.values()), 'block': sum(b.values()),
            'only_page': only_p, 'only_block': only_b,
            'ok': not only_p and not only_b}


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description='原页 vs 块：窗口内 SVG 图元集合对账')
    ap.add_argument('spec', nargs='?', help='carve 规范（自动读 page: 与每个块的 windows:）')
    ap.add_argument('--out', default=None, help='carve 产物目录（默认 out/<规范名>）')
    ap.add_argument('--page'), ap.add_argument('--block'), ap.add_argument('--window', action='append')
    a = ap.parse_args(argv)

    jobs = []
    if a.spec:
        import yaml
        spec = yaml.safe_load(Path(a.spec).read_text(encoding='utf-8'))
        out = Path(a.out) if a.out else None
        page = Path(spec['page'])                 # 规范里的 page: 相对**仓库根**写
        if not page.is_file():
            base = Path(__file__).resolve().parent.parent   # DesignBlockTools/
            for cand in (Path.cwd(), base, base.parent):
                if (cand / page).is_file():
                    page = cand / page
                    break
        for blk in spec['blocks']:
            w = [tuple(float(v) for v in box) for box in (blk.get('windows') or [])]
            if not w:
                continue
            if out is not None:
                bd = out / blk['name'] / f'{blk["name"]}.kicad_sch'
            else:
                from . import find_block
                d = find_block(blk['name'])
                bd = (d or Path('.')) / f'{blk["name"]}.kicad_sch'
            jobs.append((blk['name'], page, bd, w))
    elif a.page and a.block and a.window:
        jobs.append(('block', Path(a.page), Path(a.block),
                     [tuple(float(v) for v in s.split(',')) for s in a.window]))
    else:
        ap.error('要么给规范文件，要么 --page/--block/--window 三样都给')

    bad = 0
    cache = {}
    for name, page, block, wins in jobs:
        if not Path(block).is_file():
            print(f'✗ {name}: 没有产物 {block}')
            bad += 1
            continue
        for f in (page, block):
            cache.setdefault(str(f), render_svg(f))
        r = compare(cache[str(page)], cache[str(block)], wins)
        mark = '✓' if r['ok'] else '✗'
        print(f'  {mark} {name:16s} 原页窗口内 {r["page"]} 个图元 / 块 {r["block"]} 个'
              f'　只在原页 {sum(r["only_page"].values())}，只在块 {sum(r["only_block"].values())}')
        if not r['ok']:
            bad += 1
            for k, v in list(r['only_page'].items())[:5]:
                print(f'      只原页 {k} ×{v}')
            for k, v in list(r['only_block'].items())[:5]:
                print(f'      只在块 {k} ×{v}')
    print(f'{"✓ 全对上" if not bad else f"✗ {bad} 个块对不上"}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
