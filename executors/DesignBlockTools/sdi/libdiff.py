#!/usr/bin/env python3
"""拿 KiCad 自带的标准符号库，给转出来的每个符号做一次"该藏/该露"的体检。

## 为什么要有这个

老 `.lib`／`-cache.lib` 里每个符号的 `DEF` 行都带着自己的一套显示开关
（`text_offset draw_pinnumber draw_pinname unit_count units_locked option_flag`）。
这些开关是 KiCad 5 的语义，照字面搬进 KiCad 10 未必画出同一个东西——2026-10-29
那个 bug 就是这么来的：电源符号的 `DEF` 写的是 `Y Y`，搬过去以后那条**零长度隐形脚**
的名字被当成真名字画了出来，`+3.3V` 旁边多一行 `+3V3`、`GND` 旁多一个 `GND`，
每个电源符号还各多一个脚号 `1`。当时是靠人肉翻官方库发现的。

`SharedSupport/symbols/*.kicad_sym` 是 KiCad 自己维护的同名符号——它就是"该画成什么样"
的现成参照。所以这里把它当外部的第二意见，逐个同名符号比：

- **可见性**（脚号藏/露、脚名藏/露、脚名偏移）：这一层不一致就是 bug，**红**。
- **引脚集合**（脚号、脚名、电气类型）：老库是旧版本，官方可能改过名字/增删引脚，
  属正当差异，但值得看一眼，**黄**。
- **图元数量 + 最大线宽**：官方库会重画图形，数量不同很正常；这里真正的用途是接住
  "线宽读串了"那一类（曾经把 100mil 读成 2.54mm 的深红块），**黄**。

**这是报告，不是门**：老库本来就是旧版本，几何差异是正当的，拿它当门会天天红。
真正的门是 `legacy_sch.gate_visibility`（转出文件自己内部一致）。这一份是给未来的
老板子用的自动化"第二意见"——同名符号露不该露的东西，这里会先喊出来。

用法：
    python3 -m sdi.libdiff ../KiCAD_Projects/STM32F4_Reference/STM32F4_REV2.kicad_sch
    python3 -m sdi.libdiff <sch> --json out.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import schwrite as W

# KiCad 装在外挂盘上，`which kicad-cli` 找不到，所以按候选路径直接找库目录。
SYM_DIRS = ('/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols',
            '/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols')

GEO_KINDS = ('rectangle', 'polyline', 'circle', 'arc', 'bezier', 'text')


# ── 符号树 → 可比的摘要 ────────────────────────────────────────────────────
def _kids(node, key):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


def _hide(node, key):
    """`(pin_names (offset 0.254) (hide yes))` / `(pin_numbers (hide yes))` → True。

    老写法是裸的 `(hide yes)`，也见过 `hide` 单独一个原子，两种都认。
    """
    for k in _kids(node, key):
        if 'hide' in k[1:]:
            return True
        for h in _kids(k, 'hide'):
            if len(h) > 1 and str(h[1]) == 'yes':
                return True
            if len(h) == 1:
                return True
    return False


def _offset(node):
    for k in _kids(node, 'pin_names'):
        for o in _kids(k, 'offset'):
            return round(float(o[1]), 3)
    return None


def _stroke_w(item):
    for s in _kids(item, 'stroke'):
        for w in _kids(s, 'width'):
            return float(w[1])
    return 0.0


def _pin(p):
    """`(pin passive line (at …) (length …) (name "PA0" …) (number "1" …))` → 一行摘要。

    `~` 是 KiCad 5 的"没有名字"（缓存库里 R/C/L 的脚都叫 `~`），新格式写成空串。
    不折算的话每个无源器件都会报一次假差异。
    """
    etype = str(p[1]) if len(p) > 1 else '?'
    style = str(p[2]) if len(p) > 2 else '?'
    name = num = ''
    for k in _kids(p, 'name'):
        name = '' if str(k[1]) == '~' else str(k[1])
    for k in _kids(p, 'number'):
        num = str(k[1])
    return {'num': num, 'name': name, 'etype': etype, 'style': style}


def summarize(node, name=None):
    """一个 `(symbol …)` 节点 → {可见性, 引脚, 图元} 三段可比的东西。"""
    pins, kinds, widths = [], Counter(), []
    for sub in _kids(node, 'symbol'):          # 图形/引脚都挂在 `<名>_<unit>_<style>` 子里
        for k in GEO_KINDS:
            for it in _kids(sub, k):
                kinds[k] += 1
                widths.append(_stroke_w(it))
        for p in _kids(sub, 'pin'):
            pins.append(_pin(p))
    return {'name': name or str(node[1]),
            'power': bool(_kids(node, 'power')),
            'num_hide': _hide(node, 'pin_numbers'),
            'name_hide': _hide(node, 'pin_names'),
            'name_offset': _offset(node),
            'pins': pins, 'kinds': dict(kinds),
            'n_items': sum(kinds.values()),
            'max_w': round(max(widths, default=0.0), 3)}


# ── 官方库 ────────────────────────────────────────────────────────────────
def symbols_dir():
    for d in SYM_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    return None


_LIB_CACHE: dict[str, dict] = {}


def _norm(name):
    """比对用的名字指纹：官方库偶尔只改大小写/下划线（`Ferrite_Bead_Small` →
    `FerriteBead_Small`），那不叫"没有官方符号"。"""
    return re.sub(r'[^0-9a-z]', '', name.lower())


def official(lib, name, symdir=None):
    """`Device:R_Small` → 官方同名符号的摘要；没有就 None（自建库/已下架的符号）。

    名字对不上时会再按指纹找一次，找到就按新名比，并在结果里标 `renamed`。
    """
    symdir = symdir or symbols_dir()
    if not symdir:
        return None
    f = Path(symdir) / f'{lib}.kicad_sym'
    if not f.is_file():
        return None
    if lib not in _LIB_CACHE:
        top = W.parse(f.read_text(encoding='utf-8', errors='replace'))
        syms = {str(s[1]): s for s in _kids(top, 'symbol')}
        _LIB_CACHE[lib] = {'syms': syms,
                           'norm': {_norm(k): k for k in syms}}
    cache = _LIB_CACHE[lib]
    renamed = None
    if name not in cache['syms']:
        hit = cache['norm'].get(_norm(name))
        if hit is None:
            return None
        renamed, name = name, hit
    node = cache['syms'][name]
    # `(extends "Base")`：派生的符号自己不画图形、不带引脚，全从基类来
    base = _kids(node, 'extends')
    if base:
        b = cache['syms'].get(str(base[0][1]))
        if b is not None:
            node = ['symbol', name] + [x for x in node[1:]
                                       if not (isinstance(x, list) and x and x[0] == 'extends')] + b[1:]
    out = summarize(node, f'{lib}:{name}')
    out['renamed'] = name if renamed else None      # 官方现在叫这个名字
    return out


# ── 比对 ────────────────────────────────────────────────────────────────────
def compare(loc, off):
    """两边摘要 → 差异清单。`hard` 是可见性（该藏没藏），`soft` 是版本/画法差异。"""
    hard, soft = [], []

    def vis(tag, a, b):
        if a != b:
            hard.append(f'{tag}: 本地 {"藏" if a else "露"} / 官方 {"藏" if b else "露"}')

    vis('脚号', loc['num_hide'], off['num_hide'])
    vis('脚名', loc['name_hide'], off['name_hide'])
    if loc['name_offset'] is not None and off['name_offset'] is not None \
            and abs(loc['name_offset'] - off['name_offset']) > 1e-6:
        soft.append(f'脚名偏移 本地 {loc["name_offset"]}mm / 官方 {off["name_offset"]}mm')
    if loc['power'] != off['power']:
        soft.append(f'电源标志 本地 {"是" if loc["power"] else "否"} / 官方 {"是" if off["power"] else "否"}')

    lp = {(p['num'], p['name'], p['etype']) for p in loc['pins']}
    op = {(p['num'], p['name'], p['etype']) for p in off['pins']}
    if loc['power'] and off['power']:
        # 电源符号的脚名在 KiCad 5 里**就是网名**（`+3V3` 那条隐形脚），KiCad 6+ 改成取
        # 符号的 Value。所以老库的脚名跟官方对不上是约定变了，不是画错——比号比类型就够。
        lp = {(n, '', e) for n, _, e in lp}
        op = {(n, '', e) for n, _, e in op}
    if lp != op:
        miss, extra = sorted(op - lp), sorted(lp - op)

        def fmt(xs):
            shown = '、'.join('/'.join(v for v in x if v) for x in xs[:4])
            return shown + (f' 等 {len(xs)} 个' if len(xs) > 4 else '')
        bits = []
        if miss:
            bits.append(f'官方有本地没有：{fmt(miss)}')
        if extra:
            bits.append(f'本地有官方没有：{fmt(extra)}')
        soft.append(f'引脚 {len(lp)}/{len(op)}　' + '；'.join(bits))

    if loc['n_items'] != off['n_items']:
        soft.append(f'图元 {loc["n_items"]}/{off["n_items"]}')
    if loc['max_w'] > 0.3 and loc['max_w'] > off['max_w'] * 2 + 1e-9:
        soft.append(f'最大线宽 {loc["max_w"]}mm / 官方 {off["max_w"]}mm ← 像读串了字段')

    return {'hard': hard, 'soft': soft}


def report(sch_path, symdir=None, verbose=True) -> dict:
    """转好的 `.kicad_sch` → 逐个 lib_symbols 符号的对照结果。"""
    top = W.parse(Path(sch_path).read_text(encoding='utf-8', errors='replace'))
    libsyms = _kids(top, 'lib_symbols')
    rows = []
    for node in (_kids(libsyms[0], 'symbol') if libsyms else []):
        lib_id = str(node[1])
        lib, _, name = lib_id.partition(':')
        loc = summarize(node, lib_id)
        off = official(lib, name, symdir)
        if off is None:
            rows.append({'lib_id': lib_id, 'status': 'none'})
            continue
        d = compare(loc, off)
        rows.append({'lib_id': lib_id, 'status': 'bad' if d['hard'] else
                     ('soft' if d['soft'] else 'ok'), **d,
                     'renamed': off.get('renamed'),
                     'pins': f'{len(loc["pins"])}/{len(off["pins"])}',
                     'items': f'{loc["n_items"]}/{off["n_items"]}'})
    res = {'sch': str(sch_path), 'symdir': str(symdir or symbols_dir() or ''),
           'rows': rows,
           'n_pairs': sum(r['status'] != 'none' for r in rows),
           'n_bad': sum(r['status'] == 'bad' for r in rows),
           'n_soft': sum(r['status'] == 'soft' for r in rows),
           'n_none': sum(r['status'] == 'none' for r in rows)}
    if verbose:
        _print(res)
    return res


def _print(res):
    print(f'符号体检（转出的 {res["sch"]} ↔ KiCad 自带库）')
    if not res['symdir']:
        print('  ! 找不到 KiCad 的 symbols 目录，跳过')
        return
    icon = {'ok': '✓', 'soft': '·', 'bad': '✗', 'none': '—'}
    for r in res['rows']:
        head = f'  {icon[r["status"]]} {r["lib_id"]:<42}'
        if r['status'] == 'none':
            print(f'{head} 官方库无同名符号（自建库或已下架）')
            continue
        bits = [f'脚 {r["pins"]}']
        if r['items'].split('/')[0] == r['items'].split('/')[1]:
            bits.append(f'图元 {r["items"]}')      # 数量一样就不必在差异里再说一遍
        if r.get('renamed'):
            bits.append(f'官方库里叫 {r["renamed"]}（按这个名字比的）')
        if r.get('hard'):
            bits.append('可见性不一致：' + '；'.join(r['hard']))
        bits += r.get('soft', [])
        print(head + '　'.join(bits))
    print(f'  小计：同名 {res["n_pairs"]} 个（可见性不一致 {res["n_bad"]}，'
          f'版本/画法差异 {res["n_soft"]}），无官方对照 {res["n_none"]} 个')
    if res['n_bad']:
        print('  可见性不一致 = 老 DEF 的开关搬进 KiCad 10 后画得不一样，**要查**。')
    else:
        print('  可见性：全部一致（该藏的脚号/脚名没有漏出来）。')


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    sch = argv[0]
    js = None
    if '--json' in argv:
        js = argv[argv.index('--json') + 1]
    res = report(sch)
    if js:
        Path(js).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'  → {js}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
