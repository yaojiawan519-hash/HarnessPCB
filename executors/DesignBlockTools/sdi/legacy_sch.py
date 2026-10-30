#!/usr/bin/env python3
"""KiCad 5 老格式 `.sch` + `-cache.lib` → KiCad 10 `.kicad_sch`。

**为什么要这一步（2026-10-29）**：Phil's Lab 的 STM32F4 参考板是 KiCad 5 legacy
（首行 `EESchema Schematic File Version 4`），kicad-cli 10 已经不认它：

    $ kicad-cli sch upgrade STM32F4_REV2.sch
    Error saving schematic file …: Expecting ''('' in …, line 1, offset 1.
    $ kicad-cli sch export netlist --format kicadxml -o /tmp/f4.net STM32F4_REV2.sch
    （**不报错**，但吐出来的是 938 字节的空壳：`<components/> <nets/>`）

第二种失败最骗人——退出码 0、文件也生成了，只有打开看才知道一个器件都没读进来。
所以裁块之前先转一次，转完由下面的门验，不然后面每一步都在空图上跑。

## 两套坐标系

legacy 全用 **mil**（KiCad 5 的内部单位），modern 用 **mm**：`× 0.0254`。
库符号坐标和页面坐标是同一套，一起换算。Y 轴不用翻：两边都是"库坐标朝上、
页面坐标朝下"，legacy 的 2×2 变换矩阵里那个 `-1` 就是这次翻转。

## 方向

legacy 每个 `$Comp` 末尾带一个 2×2 变换矩阵，modern 用 `(at x y angle)` + `(mirror x|y)`。
本文件不硬编码对照表，而是**用 `geom.transform` 反推**（先 `y→-y` 再按角度转，镜像最后做），
枚举 4 个角度 × 3 种镜像得到 12 个矩阵，建反查表：

    (1,0,0,-1) → 0°    (-1,0,0,1) → 180°    (0,-1,-1,0) → 90°    (0,1,1,0) → 270°

拿不到的矩阵直接报错（说明这张图用了我们没覆盖的写法，不许猜）。

## 五道门（本文件的意义，不是可选步骤）

① **引脚对账**：每个引脚按 legacy 矩阵算一遍（`legacy_pins`），再让 `extract`（走
   modern 的 angle/mirror 那条路）从写出的文件读一遍，两边必须同一个坐标（≤10µm）。
   方向映射写错，这里立刻红一片——比"打开 KiCad 看一眼"可靠，比等 ERC 报
   "40 个脚没接"更早。
② **连通对账**：把 legacy 的几何（脚 + 线 + 标签 + 电源符号）自己连一遍得到分区，
   与 `extract.build_nets` 从写出的文件算的分区逐组相同。这条盖住"坐标都对、
   但线/标签/电源符号写错了位置"。
③ **图形自检**：库符号的线宽/坐标落在常识范围内（详见 `gate_graphics`）。
④ **渲染对账**：原页和转出文件**都让 KiCad 自己渲一遍**，逐个标签比文字落点与对齐
   （详见 `gate_render`）。①②③ 都是拿我自己的理解去验我自己的输出，这条不是——
   落点是渲染器给的。2026-10-29 标签方向整体镜像，就是靠它才量出确定结论。
⑤ **封装回填**（给了 `--pcb` 才有）：见 `fill_footprints`。

用法：
    python3 -m sdi.legacy_sch <in.sch> --lib <cache.lib> -o <out.kicad_sch> [--project 名字]
"""
from __future__ import annotations

import math
import re
import sys
import uuid as uuidlib
from pathlib import Path

from . import schwrite as W

MIL = 0.0254                 # 1 mil = 0.0254 mm
ROUND = 4                    # 写进文件的坐标位数：mil 整数 × 0.0254 恰好 ≤4 位小数

# 引脚电气类型 / 形状 / 方向（legacy 单字母 → modern 关键字）
ETYPE = {'I': 'input', 'O': 'output', 'B': 'bidirectional', 'T': 'tristate',
         'P': 'passive', 'U': 'unspecified', 'W': 'power_in', 'w': 'power_out',
         'C': 'open_collector', 'E': 'open_emitter', 'N': 'no_connect'}
PSHAPE = {'N': 'line', '': 'line', 'I': 'inverted', 'C': 'clock',
          'F': 'inverted_clock', 'X': 'non_logic',
          'L': 'low_input', 'H': 'edge_clock_high'}
PORIENT = {'R': 0, 'U': 90, 'L': 180, 'D': 270}
LBL_SHAPE = {'Input': 'input', 'Output': 'output', 'BiDi': 'bidirectional',
             '3State': 'tri_state', 'UnSpc': 'passive', 'Passive': 'passive'}


class LegacyError(SystemExit):
    """老格式里出现了转不动的东西。刻意硬退出：猜出来的坐标比没有坐标更坏。"""


def _mm(v) -> float:
    return round(float(v) * MIL, ROUND)


def _num(x) -> str:
    return W._num(float(x))


def _uuid() -> list:
    return ['uuid', W.Str(str(uuidlib.uuid4()))]


def _uuid_lib() -> list:
    """库符号里的图元**不带** uuid：KiCad 10 的解析器在这里是严的，多一个
    `(uuid …)` 整个文件就 'Failed to load schematic'（2026-10-29 实测，23 个符号
    逐个单测，去掉就全过）。页面级图元（线/标签/符号实例）相反，必须有。

    ⚠ 返回 `None` 而不是 `[]`：`[]` 会被 `dump` 写成空节点 `()`，一样打不开文件。"""
    return None


# ── 方向：legacy 矩阵 ↔ modern (angle, mirror) ──────────────────────────────
def _matrix(rot, mirror):
    """按 `geom.transform` 的同一套公式，算 (rot, mirror) 对应的 2×2 矩阵。

    只喂基向量，省得再写一遍公式：x 轴 → (1,0)，y 轴 → (0,1)。
    """
    def t(px, py):
        x, y = px, -py
        a = math.radians(rot or 0)
        xr = x * math.cos(a) + y * math.sin(a)
        yr = -x * math.sin(a) + y * math.cos(a)
        if mirror == 'x':
            yr = -yr
        if mirror == 'y':
            xr = -xr
        return (round(xr), round(yr))

    (x1, y1), (x2, y2) = t(1, 0), t(0, 1)
    return (x1, y1, x2, y2)


ROT_OF_MATRIX: dict[tuple[int, int, int, int], tuple[int, str | None]] = {}
for _r in (0, 90, 180, 270):
    for _m in (None, 'x', 'y'):
        ROT_OF_MATRIX.setdefault(_matrix(_r, _m), (_r, _m))


# ── 1. 读 legacy 符号库 ────────────────────────────────────────────────────
def read_lib(path) -> dict:
    """`-cache.lib` → {DEF 名: {...}}。只认本页真用得上的东西（图形 + 引脚 + F0..Fn）。"""
    syms: dict[str, dict] = {}
    cur = None
    in_draw = in_fp = False
    for raw in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('DEF '):
            p = s.split()
            cur = {'name': p[1], 'ref': p[2], 'offset': float(p[4]) * MIL,
                   'draw_num': p[5] == 'Y', 'draw_name': p[6] == 'Y',
                   'units': int(p[7]) if p[7].isdigit() else 1,
                   'power': p[9] == 'P', 'fields': {}, 'items': [], 'aliases': []}
            in_draw = in_fp = False
            continue
        if cur is None:
            continue
        if s == '$FPLIST':
            in_fp = True
            continue
        if s == '$ENDFPLIST':
            in_fp = False
            continue
        if in_fp:
            continue
        if s.startswith('ALIAS '):
            cur['aliases'] += s.split()[1:]
        elif s == 'DRAW':
            in_draw = True
        elif s == 'ENDDRAW':
            in_draw = False
        elif s == 'ENDDEF':
            syms[cur['name']] = cur
            cur = None
        elif not in_draw and re.match(r'^F\d+ ', s):
            cur['fields'][int(s.split()[0][1:])] = _lib_field(s)
        elif in_draw:
            cur['items'].append(_draw_item(s))
    return syms


_FIELD = re.compile(r'^F(\d+)\s+"((?:[^"\\]|\\.)*)"\s+(-?\d+)\s+(-?\d+)\s+(\d+)'
                    r'\s+([HV])\s+([VI])\s+([LCR])\s+(\S+)(?:\s+"((?:[^"\\]|\\.)*)")?')


def _lib_field(s):
    m = _FIELD.match(s)
    if not m:
        raise LegacyError(f'✗ 读不懂的库字段行：{s!r}')
    return {'n': int(m.group(1)), 'text': m.group(2).replace('\\"', '"'),
            'x': float(m.group(3)), 'y': float(m.group(4)), 'size': float(m.group(5)),
            'orient': m.group(6), 'visible': m.group(7) == 'V',
            'hjust': m.group(8), 'name': m.group(10)}


def _draw_item(s):
    """一条 DRAW 里的图元 → dict。

    每种图元的字段数**是死的**，所以这里逐个卡死长度：**数错一个字段就是画错一整张图**，
    而且画出来还挺像个东西（2026-10-29 抓到过一次：折线把 `P n` 后面的 `unit convert
    width` 当成坐标，整库符号的点和线宽全错位，电源符号渲成一坨 2.54mm 粗的深红块）。
    出错就当场炸，不猜。
    """
    p = s.split()
    k = p[0]

    def need(n, fmt):
        if len(p) != n:
            raise LegacyError(f'✗ 图元字段数不对（{k} 应 {n} 个，实际 {len(p)} 个）：{s!r}\n'
                              f'   格式：{fmt}')

    if k == 'S':            # S x1 y1 x2 y2 unit convert width fill
        need(9, 'S x1 y1 x2 y2 unit convert width fill')
        return {'k': 'rect', 'a': [float(v) for v in p[1:5]],
                'unit': int(p[5]), 'w': float(p[7]), 'fill': p[8]}
    if k == 'P':            # P n unit convert width x1 y1 ... fill
        n = int(p[1])       # ⚠ 只有折线把 n 顶在最前面，其余三种都没有 n
        need(5 + 2 * n + 1, f'P n unit convert width{" x y" * n} fill')
        pts = [(float(p[5 + 2 * i]), float(p[6 + 2 * i])) for i in range(n)]
        return {'k': 'poly', 'a': pts, 'unit': int(p[2]), 'w': float(p[4]),
                'fill': p[5 + 2 * n]}
    if k == 'C':            # C x y r unit convert width fill
        need(8, 'C x y r unit convert width fill')
        return {'k': 'circle', 'a': [float(v) for v in p[1:4]],
                'unit': int(p[4]), 'w': float(p[6]), 'fill': p[7]}
    if k == 'A':            # A x y r a1 a2 unit convert width fill sx sy ex ey（角度是 0.1°）
        need(14, 'A x y r a1 a2 unit convert width fill sx sy ex ey')
        return {'k': 'arc', 'a': [float(v) for v in p[1:6]],
                'unit': int(p[6]), 'w': float(p[8]), 'fill': p[9]}
    if k == 'T':            # T angle x y size hidden unit convert text
        return {'k': 'text', 'a': [float(p[1]), float(p[2]), float(p[3])],
                'size': float(p[4]), 'hidden': p[5] == '1', 'unit': int(p[6]),
                'text': ' '.join(p[9:])}
    if k == 'X':            # 引脚: name num x y len orient Snum Snom unit convert etype [shape]
        return {'k': 'pin', 'name': p[1], 'num': p[2],
                'at': (float(p[3]), float(p[4])), 'len': float(p[5]),
                'orient': p[6], 'num_size': float(p[7]), 'name_size': float(p[8]),
                'unit': int(p[9]), 'etype': p[11], 'shape': p[12] if len(p) > 12 else 'N'}
    return {'k': 'skip'}


# ── 2. 读 legacy 页 ────────────────────────────────────────────────────────
def read_page(path) -> dict:
    lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    page = {'descr': {}, 'comps': [], 'wires': [], 'notes': [], 'junctions': [],
            'noconns': [], 'labels': [], 'texts': []}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith('EESchema Schematic File Version'):
            page['version'] = int(s.split()[-1])
        elif s.startswith('$Descr'):
            page['descr']['paper'] = lines[i].split()[1]
        elif s.startswith('Title '):
            page['descr']['title'] = _quoted(lines[i])
        elif s.startswith('Date '):
            page['descr']['date'] = _quoted(lines[i])
        elif s.startswith('Rev '):
            page['descr']['rev'] = _quoted(lines[i])
        elif s.startswith('Comp '):
            page['descr']['company'] = _quoted(lines[i])
        elif re.match(r'^Comment\d ', s):
            page['descr'].setdefault('comments', {})[int(s[7])] = _quoted(lines[i])
        elif s == '$Comp':
            comp, i = _read_comp(lines, i)
            page['comps'].append(comp)
        elif s.startswith('Wire Wire Line'):
            page['wires'].append(_seg(lines[i + 1]))
            i += 1
        elif s.startswith('Wire Notes Line'):
            page['notes'].append(_seg(lines[i + 1]))
            i += 1
        elif s.startswith('Connection ~'):
            p = s.split()
            page['junctions'].append((_mm(p[2]), _mm(p[3])))
        elif s.startswith('NoConn ~'):
            p = s.split()
            page['noconns'].append((_mm(p[2]), _mm(p[3])))
        elif s.startswith('Text '):
            kind, body = s.split()[1], (lines[i + 1] if i + 1 < len(lines) else '')
            p = s.split()
            if kind in ('Label', 'GLabel', 'HLabel'):
                page['labels'].append({'kind': kind, 'text': body,
                                       'x': _mm(p[2]), 'y': _mm(p[3]),
                                       'orient': int(p[4]), 'size': _mm(p[5]),
                                       'shape': p[6] if len(p) > 6 else 'Input'})
            elif kind == 'Notes' or kind == 'Text':
                page['texts'].append({'text': body, 'x': _mm(p[2]), 'y': _mm(p[3]),
                                      'orient': int(p[4]), 'size': _mm(p[5])})
            i += 1
        i += 1
    return page


def _quoted(line) -> str:
    m = re.search(r'"((?:[^"\\]|\\.)*)"', line)
    return m.group(1) if m else ''


def _seg(line) -> tuple:
    p = line.split()
    return (_mm(p[0]), _mm(p[1])), (_mm(p[2]), _mm(p[3]))


def _read_comp(lines, i):
    comp = {'fields': {}, 'transform': (1, 0, 0, -1)}
    i += 1
    while i < len(lines) and lines[i].strip() != '$EndComp':
        s = lines[i].strip()
        if s.startswith('L '):
            lib_id, ref = s.split()[1], s.split()[2]
            comp['lib_id'], comp['ref'] = lib_id, ref
        elif s.startswith('U '):
            p = s.split()
            comp['unit'], comp['convert'] = int(p[1]), int(p[2])
            comp['stamp'] = p[3] if len(p) > 3 else ''
        elif s.startswith('P '):
            p = s.split()
            comp['at'] = (_mm(p[1]), _mm(p[2]))
        elif re.match(r'^F\s*\d+ ', s):     # 页里是 `F 0 "…"`（有空格），库里是 `F0 "…"`
            m = re.match(r'^F\s*(\d+)\s+"((?:[^"\\]|\\.)*)"\s+([HV])\s+(-?\d+)\s+(-?\d+)'
                         r'\s+(\d+)\s+(\d+)\s+([LCR])\s+([A-Z]+)(?:\s+"((?:[^"\\]|\\.)*)")?', s)
            if not m:
                raise LegacyError(f'✗ 读不懂器件字段行：{s!r}')
            comp['fields'][int(m.group(1))] = {
                'text': m.group(2).replace('\\"', '"'),
                'x': _mm(m.group(4)), 'y': _mm(m.group(5)), 'size': _mm(m.group(6)),
                'flags': m.group(7), 'hjust': m.group(8), 'name': m.group(10)}
        elif re.match(r'^-?\d+\s+-?\d+\s+-?\d+\s+-?\d+$', s):
            comp['transform'] = tuple(int(v) for v in s.split())
        i += 1
    if 'at' not in comp:
        raise LegacyError(f'✗ {comp.get("ref")}: $Comp 里没有 P 行（位置）')
    return comp, i


# ── 3. legacy 引脚坐标（门①的左边）───────────────────────────────────────
def legacy_pins(comp, libsym) -> dict:
    """按 legacy 的 2×2 变换算每个引脚落在纸面上的 mm 坐标。

    `P + M·pin`，`M` 里已经含了"库坐标 y 朝上 → 页面 y 朝下"那次翻转
    （单位阵 `(1,0,0,-1)` 就是它）。
    """
    x1, y1, x2, y2 = comp['transform']
    px, py = comp['at']
    out = {}
    for it in libsym['items']:
        if it['k'] != 'pin' or it['unit'] not in (0, comp.get('unit') or 1):
            continue
        lx, ly = it['at']
        out[it['num']] = (round(px + (x1 * lx + y1 * ly) * MIL, ROUND),
                          round(py + (x2 * lx + y2 * ly) * MIL, ROUND))
    return out


# ── 4. 写 modern 树 ────────────────────────────────────────────────────────
def _effects(size_mm, hidden=False, justify=None):
    e = ['effects', ['font', ['size', _num(size_mm), _num(size_mm)]]]
    if justify:
        e.append(['justify'] + list(justify))
    if hidden:
        e.append(['hide', 'yes'])
    return e


def _prop(name, value, x, y, size, hidden=False, angle=0.0):
    out = ['property', W.Str(name), W.Str(value), ['at', _num(x), _num(y), _num(angle)]]
    if hidden:
        out.append(['hide', 'yes'])
    out += [['show_name', 'no'], ['do_not_autoplace', 'no'],
            _effects(size, hidden and False)]      # hide 只写在 property 上，别写两遍
    return out


def _fill_modern(ch):
    return {'N': 'none', 'F': 'outline', 'f': 'background'}.get(ch, 'none')


def _lib_item(it):
    k = it['k']
    stroke = ['stroke', ['width', _num(it.get('w', 0) * MIL)] if k != 'text' else
              ['width', '0'], ['type', 'default']]
    if k == 'rect':
        x1, y1, x2, y2 = (round(v * MIL, ROUND) for v in it['a'])
        return ['rectangle', ['start', _num(x1), _num(y1)], ['end', _num(x2), _num(y2)],
                stroke, ['fill', ['type', _fill_modern(it['fill'])]], _uuid_lib()]
    if k == 'poly':
        pts = [['xy', _num(round(x * MIL, ROUND)), _num(round(y * MIL, ROUND))] for x, y in it['a']]
        return ['polyline', ['pts'] + pts, stroke,
                ['fill', ['type', _fill_modern(it['fill'])]], _uuid_lib()]
    if k == 'circle':
        x, y, r = (round(v * MIL, ROUND) for v in it['a'])
        return ['circle', ['center', _num(x), _num(y)], ['radius', _num(r)], stroke,
                ['fill', ['type', _fill_modern(it['fill'])]], _uuid_lib()]
    if k == 'arc':
        x, y, r, a1, a2 = it['a']
        t1, t2 = math.radians(a1 / 10), math.radians(a2 / 10)
        # legacy 弧按逆时针从 a1 走到 a2；modern 要 start/mid/end 三个点。
        span = (a2 / 10 - a1 / 10) % 360 or 360
        tm = t1 + math.radians(span / 2)

        def pt(t):
            return [_num(round((x + r * math.cos(t)) * MIL, ROUND)),
                    _num(round((y + r * math.sin(t)) * MIL, ROUND))]
        return ['arc', ['start'] + pt(t1), ['mid'] + pt(tm), ['end'] + pt(t2),
                stroke, ['fill', ['type', _fill_modern(it['fill'])]], _uuid_lib()]
    if k == 'text':
        ang, x, y = it['a']
        return ['text', W.Str(it['text']), ['at', _num(round(x * MIL, ROUND)),
                                            _num(round(y * MIL, ROUND)), _num(ang)],
                _effects(round(it['size'] * MIL, ROUND), it['hidden']), _uuid_lib()]
    if k == 'pin':
        lx, ly = it['at']
        return ['pin', ETYPE.get(it['etype'], 'unspecified'),
                PSHAPE.get(it['shape'], 'line'),
                ['at', _num(round(lx * MIL, ROUND)), _num(round(ly * MIL, ROUND)),
                 _num(PORIENT.get(it['orient'], 0))],
                ['length', _num(round(it['len'] * MIL, ROUND))],
                ['name', W.Str(it['name']), _effects(round(it['name_size'] * MIL, ROUND))],
                ['number', W.Str(it['num']), _effects(round(it['num_size'] * MIL, ROUND))]]
    return None


def lib_symbol_tree(lib_id, sym) -> list:
    short = lib_id.split(':')[-1]
    out = ['symbol', W.Str(lib_id)]
    if sym['power']:
        out.append(['power', 'global'])
    # 电源符号一律不画脚号/脚名，只露 Value。老库里 DEF 那两个标志写的是 `Y Y`，但
    # 电源符号的引脚是零长度的隐形脚（`X +3V3 1 0 0 0 U 50 50 1 1 W N`），KiCad 5 画出来
    # 就只有 Value 一个名字；照 `Y Y` 搬进 KiCad 10，那条隐形脚的名字会被当真名字画出来：
    # `+3.3V` 旁边多一行 `+3V3`、`GND` 旁边多一个 `GND`、再各多一个脚号 `1`
    # （2026-10-29 用户报的"GND 有两个，3V3 边上又有 +3.3V"就是这么来的）。
    # KiCad 自己的电源库（`power.kicad_sym`）也是这么写的：pin_numbers/pin_names 全 hide。
    if sym['power'] or not sym['draw_num']:
        out.append(['pin_numbers', ['hide', 'yes']])
    pn = ['pin_names', ['offset', _num(round(sym['offset'], ROUND))]]
    if sym['power'] or not sym['draw_name']:
        pn.append(['hide', 'yes'])
    out.append(pn)
    out += [['exclude_from_sim', 'no'], ['in_bom', 'yes'], ['on_board', 'yes'],
            ['in_pos_files', 'yes'], ['duplicate_pin_numbers_are_jumpers', 'no']]
    for n, nm in ((0, 'Reference'), (1, 'Value'), (2, 'Footprint'), (3, 'Datasheet')):
        f = sym['fields'].get(n)
        val = (sym['ref'] if n == 0 else short) if not f else f['text']
        if n == 1 and (not f or f['text'] == sym['name']):
            val = short          # 缓存库里 F1 存的是 `库名_符号名` 全称，页面上要的是符号名
        out.append(_prop(nm, val, round((f or {}).get('x', 0) * MIL, ROUND),
                         round((f or {}).get('y', 0) * MIL, ROUND),
                         round((f or {}).get('size', 50) * MIL, ROUND),
                         hidden=not (f or {}).get('visible', True)))
    for n in sorted(k for k in sym['fields'] if k >= 4):
        f = sym['fields'][n]
        out.append(_prop(f['name'] or f'Field{n}', f['text'],
                         round(f['x'] * MIL, ROUND), round(f['y'] * MIL, ROUND),
                         round(f['size'] * MIL, ROUND), hidden=not f['visible']))
    # 图形按单元分组：单元 0 是所有单元共用（单单元器件统一塞进 `_1_1`）
    units = {}
    single = sym['units'] <= 1
    for it in sym['items']:
        node = _lib_item(it)
        if node is None:
            continue
        node = [c for c in node if c is not None]     # 去掉 `_uuid_lib()` 占的位
        u = 1 if single else (it.get('unit') or 0)
        units.setdefault(u, []).append(node)
    for u in sorted(units):
        out.append(['symbol', W.Str(f'{short}_{u}_1')] + units[u])
    return out


def _symbol_tree(comp, libsym, project, root_uuid) -> list:
    if comp['transform'] not in ROT_OF_MATRIX:
        raise LegacyError(f'✗ {comp["ref"]}: 变换 {comp["transform"]} 不认识'
                          f'（已知 {sorted(ROT_OF_MATRIX)}）')
    rot, mirror = ROT_OF_MATRIX[comp['transform']]
    x, y = comp['at']
    out = ['symbol', ['lib_id', W.Str(comp['lib_id'])],
           ['at', _num(x), _num(y), _num(rot)]]
    if mirror:
        out.append(['mirror', mirror])
    out += [['unit', str(comp.get('unit') or 1)], ['body_style', '1'],
            ['exclude_from_sim', 'no'], ['in_bom', 'yes'], ['on_board', 'yes'],
            ['in_pos_files', 'yes'], ['dnp', 'no'], _uuid()]
    for n, nm in ((0, 'Reference'), (1, 'Value'), (2, 'Footprint'), (3, 'Datasheet')):
        f = comp['fields'].get(n) or {}
        val = f.get('text', '')
        out.append(_prop(nm, val, f.get('x', x), f.get('y', y), f.get('size', 1.27),
                         hidden=bool(int(f.get('flags', '0')) & 1) if f.get('flags') else False))
    for n in sorted(k for k in comp['fields'] if k >= 4):
        f = comp['fields'][n]
        out.append(_prop(f['name'] or f'Field{n}', f['text'], f['x'], f['y'], f['size'],
                         hidden=bool(int(f['flags']) & 1)))
    for it in libsym['items']:
        if it['k'] == 'pin' and it['unit'] in (0, comp.get('unit') or 1):
            out.append(['pin', W.Str(it['num']), _uuid()])
    out.append(['instances', ['project', W.Str(project),
                              ['path', W.Str('/' + root_uuid),
                               ['reference', W.Str(comp['ref'])],
                               ['unit', str(comp.get('unit') or 1)]]]])
    return out


def _label_angle(orient):
    """legacy 标签的 4 个方向码 → (角度, 对齐)。

    KiCad 5 的码说的是**文字从锚点往哪边长**：0=往左、1=往上、2=往右、3=往下
    （就是 LABEL_SPIN_STYLE 的 LEFT/UP/RIGHT/BOTTOM）。以前按"0=往右、2=往左"
    写反了，整页标签的文本全落到锚点另一边——2026-10-29 拿 kicad-cli 直接渲老文件
    逐字量出来：每个标签的文本中心都镜像差了 ±2.858mm，一条不例外。
    """
    return {0: (180, ('right',)), 1: (90, ('left',)),
            2: (0, ('left',)), 3: (270, ('right',))}.get(orient, (0, ('left',)))


def build_tree(page, libs, project, paper='A4') -> list:
    root_uuid = str(uuidlib.uuid4())
    lib_ids = {c['lib_id'] for c in page['comps']}
    out = ['kicad_sch', ['version', '20260306'], ['generator', W.Str('eeschema')],
           ['generator_version', W.Str('10.0')], ['embedded_fonts', 'no'],
           ['uuid', W.Str(root_uuid)], ['paper', W.Str(paper)]]
    d = page['descr']
    if any(d.get(k) for k in ('title', 'date', 'rev', 'company')):
        tb = ['title_block']
        for k, tag in (('title', 'title'), ('date', 'date'), ('rev', 'rev'),
                       ('company', 'company')):
            if d.get(k):
                tb.append([tag, W.Str(d[k])])
        for n, v in sorted((d.get('comments') or {}).items()):
            tb.append(['comment', str(n), W.Str(v)])
        out.append(tb)
    out.append(['lib_symbols'] + [lib_symbol_tree(i, libs[i])
                                  for i in sorted(lib_ids) if i in libs])
    for a, b in page['notes']:
        out.append(['polyline', ['pts', ['xy', _num(a[0]), _num(a[1])],
                                 ['xy', _num(b[0]), _num(b[1])]],
                    ['stroke', ['width', '0'], ['type', 'default']], _uuid()])
    for x, y in page['junctions']:
        out.append(['junction', ['at', _num(x), _num(y)], ['diameter', '0'],
                    ['color', '0', '0', '0', '0'], _uuid()])
    for x, y in page['noconns']:
        out.append(['no_connect', ['at', _num(x), _num(y)], _uuid()])
    for a, b in page['wires']:
        out.append(['wire', ['pts', ['xy', _num(a[0]), _num(a[1])],
                             ['xy', _num(b[0]), _num(b[1])]],
                    ['stroke', ['width', '0'], ['type', 'default']], _uuid()])
    for l in page['labels']:
        ang, just = _label_angle(l['orient'])
        if l['kind'] == 'GLabel':
            out.append(['global_label', W.Str(l['text']),
                        ['shape', LBL_SHAPE.get(l['shape'], 'input')],
                        ['at', _num(l['x']), _num(l['y']), _num(ang)],
                        _effects(l['size'], justify=just), _uuid()])
        elif l['kind'] == 'HLabel':
            out.append(['hierarchical_label', W.Str(l['text']),
                        ['shape', LBL_SHAPE.get(l['shape'], 'input')],
                        ['at', _num(l['x']), _num(l['y']), _num(ang)],
                        _effects(l['size'], justify=just), _uuid()])
        else:
            out.append(['label', W.Str(l['text']),
                        ['at', _num(l['x']), _num(l['y']), _num(ang)],
                        _effects(l['size'], justify=just), _uuid()])
    for t in page['texts']:
        out.append(['text', W.Str(t['text']),
                    ['at', _num(t['x']), _num(t['y']), _num(t['orient'])],
                    _effects(t['size']), _uuid()])
    for comp in page['comps']:
        out.append(_symbol_tree(comp, libs[comp['lib_id']], project, root_uuid))
    out.append(['sheet_instances', ['path', W.Str('/'), ['page', W.Str('1')]]])
    return out


def attach_libs(page, libs) -> None:
    """给每个器件挂上它用的库符号。缺符号是硬错误——猜一个会静默丢引脚。"""
    missing = sorted({c['lib_id'] for c in page['comps']} - set(libs))
    if missing:
        raise LegacyError('✗ 缓存库里没有这些符号（-cache.lib 不全？）：' + '、'.join(missing))


def match_libs(page, cache) -> dict:
    """页里的 lib_id → 缓存库里的 DEF。缓存名 = `<库名>_<符号名>`，别名也算。"""
    by_alias = {}
    for name, sym in cache.items():
        by_alias[name] = name
        for a in sym['aliases']:
            by_alias.setdefault(a, name)
    out = {}
    for lib_id in {c['lib_id'] for c in page['comps']}:
        libname, _, symname = lib_id.partition(':')
        for key in (f'{libname}_{symname}', symname):
            if key in by_alias:
                out[lib_id] = cache[by_alias[key]]
                break
    return out


# ── 5. 门① 引脚对账 / 门② 连通对账 ────────────────────────────────────────
def _extract_mod():
    itl = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(itl) not in sys.path:
        sys.path.insert(0, str(itl))
    import extract, geom                                       # noqa: PLC0415
    return extract, geom


def legacy_nets(page, libs) -> set:
    """legacy 侧自己连一遍：脚 + 线 + 标签 + 电源符号（Value 当全局标签名）。"""
    _, geom = _extract_mod()
    dsu, nodes = geom.DSU(), []
    pin_of = []
    for c in page['comps']:
        sym = libs[c['lib_id']]
        power = sym['power']
        val = (c['fields'].get(1) or {}).get('text') or c['lib_id'].split(':')[-1]
        for num, q in legacy_pins(c, sym).items():
            dsu.find(geom.key(q))
            nodes.append((geom.key(q), q))
            if power:
                pin_of.append((('#' + val), q, val))
            else:
                pin_of.append(((c['ref'], num), q, None))
    for a, b in page['wires']:
        ka, kb = geom.key(a), geom.key(b)
        dsu.union(ka, kb)
        for k, q in nodes:
            if geom.on_seg(k, a, b):
                dsu.union(k, ka)
    named = {}
    for l in page['labels']:
        q = (l['x'], l['y'])
        k = geom.key(q)
        dsu.find(k)
        for kk, _q in nodes:
            if kk == k or geom.on_seg(kk, q, q):
                dsu.union(kk, k)
        named.setdefault(l['text'], []).append(dsu.find(k))
    for (ref, q, nm) in pin_of:
        if nm:
            named.setdefault(nm, []).append(dsu.find(geom.key(q)))
    for roots in named.values():           # 同名合并：标签/电源符号就是"同一个网"
        for r in roots[1:]:
            dsu.union(roots[0], r)
    groups = {}
    for (ref, q, nm) in pin_of:
        if nm:
            continue
        groups.setdefault(dsu.find(geom.key(q)), set()).add((ref[0], str(ref[1])))
    return {frozenset(g) for g in groups.values() if g}


def gate_graphics(libs) -> dict:
    """图形自检：符号库里的点/线宽换算成毫米后必须落在常识范围内。

    这条是给"字段读错位"兜底的——数值读串了不一定报错，画出来往往还是一坨有点像的
    形状，只是粗得离谱（电源符号渲成 2.54mm 的深红块就是这么来的）。
    """
    MAX_W = 60.0        # mil；本库里最粗的合法图形是安装孔圆环 50mil（`C 0 50 50 0 1 50 N`）
    MAX_A = 3000.0      # mil；库内坐标离原点超 3 英寸，基本一定是字段错位
    bad, n = [], 0
    for name, sym in libs.items():
        for it in sym['items']:
            if it['k'] in ('pin', 'text', 'skip'):
                continue
            n += 1
            w = it.get('w', 0)
            pts = it['a'] if it['k'] == 'poly' else [it['a'][:2]]
            if not 0 <= w <= MAX_W:
                bad.append(f'{name}: 线宽 {w:g}mil 越界')
            elif len(pts) > 1:      # 线宽不该比自己画的形状还长——错位读出来的宽就是这种
                span = max(max(x for x, _ in pts) - min(x for x, _ in pts),
                           max(y for _, y in pts) - min(y for _, y in pts))
                if w > span:
                    bad.append(f'{name}: 线宽 {w:g}mil 比图形本身（{span:g}mil）还粗')
            for x, y in pts:
                if abs(x) > MAX_A or abs(y) > MAX_A:
                    bad.append(f'{name}: 坐标 ({x:g},{y:g})mil 越界')
    return {'graphics': n, 'graphic_bad': bad[:8], 'graphic_ok': not bad}


def gate_visibility(out_path) -> dict:
    """电源符号只许露 Value，不许露脚名/脚号。

    判据不是"我觉得"，是 KiCad 自己的电源库：`power.kicad_sym` 里每个符号都写着
    `(pin_numbers (hide yes))` + `(pin_names (offset 0) (hide yes))`。
    漏掉它的后果很具体，也很容易被当成"画重了"：`+3.3V` 旁边多出一行脚名 `+3V3`、
    `GND` 旁边多出第二个 `GND`、每个电源符号还各多一个脚号 `1`（2026-10-29 用户报的）。
    """
    s = Path(out_path).read_text(encoding='utf-8')
    bad, n = [], 0
    for m in re.finditer(r'\(symbol "(power:[^"]+)"', s):
        n += 1
        head = s[m.end():m.end() + 400]          # 脚名/脚号就在符号定义的开头几项
        if not re.search(r'\(pin_numbers\s*\(hide yes\)', head):
            bad.append(f'{m.group(1)}: 脚号没藏')
        elif not re.search(r'\(pin_names[^()]*(?:\([^()]*\)\s*)*\(hide yes\)', head):
            bad.append(f'{m.group(1)}: 脚名没藏')
    return {'power_syms': n, 'vis_bad': bad[:8], 'vis_ok': not bad}


# ── 门④ 渲染对账 ──────────────────────────────────────────────────────────
# kicad-cli 本机能找到的几个位置（本机的 KiCad 装在外挂盘上）
CLI_CANDIDATES = ('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
                  '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')
_RE_TEXT = re.compile(r'<text x="(-?[\d.]+)" y="(-?[\d.]+)"(.*?)>(.*?)</text>', re.S)


def _kicad_cli():
    """找到 kicad-cli 就用它做渲染对账，找不到就跳过（门返回 skipped，不当失败）。"""
    import shutil
    for p in (shutil.which('kicad-cli'),) + CLI_CANDIDATES:
        if p and Path(p).is_file():
            return p
    return None


def _svg_texts(path) -> dict:
    """SVG → {内容: {(x, y, 锚点)}}。

    KiCad 画每个文字都先吐一个不可见的 `<text x= y= text-anchor=>内容</text>`
    （`opacity="0"`，只用来选字体），紧跟着 `<g class="stroked-text">` 画真笔画。
    所以这里的 x/y/锚点是**渲染器自己**认的位置——正因如此它才当得了判据。
    """
    s = Path(path).read_text(encoding='utf-8', errors='replace')
    out = {}
    for m in _RE_TEXT.finditer(s):
        x, y = round(float(m.group(1)), 3), round(float(m.group(2)), 3)
        a = re.search(r'text-anchor="(\w+)"', m.group(3))
        out.setdefault(m.group(4), set()).add((x, y, a.group(1) if a else '?'))
    return out


def gate_render(sch, page, out_path) -> dict:
    """原页与转出文件各渲一遍，逐个标签比文字落点。

    前几道门都是"用我的理解验我的输出"：角度算错了，门①照样全绿——它只是用同一套
    错误理解把文件再读一遍。这条不一样：位置由 KiCad 渲染器给出，我只比对。

    判据是**包含**而不是相等：kicad-cli 渲老文件时会丢掉一些符号体（`Device:*_Small`、
    `MCU_ST_STM32F4:*` 等，与符号库表无关），那些符号里的引脚名文字原页渲染里没有、
    转出文件里有。所以只要求"原页有的落点，转出文件里也有并且一模一样"。
    少一个 = 标签写歪了；多出来的不判——那是修好的符号体，不是错。
    """
    cli = _kicad_cli()
    if not cli:
        return {'render_ok': True, 'render_skip': True, 'labels_checked': 0}

    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix='sdirender-') as td:
        got = {}
        for tag, f in (('a', sch), ('b', out_path)):
            r = subprocess.run([cli, 'sch', 'export', 'svg', '-o', f'{td}/{tag}', str(f)],
                               capture_output=True, text=True)
            svgs = sorted(Path(f'{td}/{tag}').glob('*.svg')) if r.returncode == 0 else []
            if not svgs:
                return {'render_ok': True, 'render_skip': True, 'labels_checked': 0,
                        'render_err': (r.stderr or r.stdout).strip()[:200]}
            got[tag] = _svg_texts(svgs[0])

    bad, n = [], 0
    for text in sorted({l['text'] for l in page['labels']}):
        want = got['a'].get(text)
        if not want:                 # 原页渲染里就没有这串字，无可比
            continue
        n += 1
        miss = want - got['b'].get(text, set())
        if miss:
            bad.append(f'{text}: 原页 {sorted(miss)[:2]} 在转出文件里找不到')
    return {'render_ok': not bad, 'render_skip': False,
            'labels_checked': n, 'render_bad': bad[:8]}


def gate(page, libs, out_path) -> dict:
    """写出的文件读回来，逐引脚坐标 + 逐组连通对账。红一条就是转错了。"""
    extract, _ = _extract_mod()
    il = extract.extract(str(out_path))
    by_ref = {}
    for s in il['symbols']:
        by_ref.setdefault(s['id'], []).append(s)

    bad, n = [], 0
    for c in page['comps']:
        want = legacy_pins(c, libs[c['lib_id']])
        got_sym = by_ref.get(c['ref'])
        if not got_sym:
            bad.append(f'{c["ref"]}: 文件里没有这个器件')
            continue
        got = {}
        for s in got_sym:
            got.update({k: tuple(v) for k, v in (s.get('pins_at') or {}).items()})
        for num, q in want.items():
            n += 1
            g = got.get(num)
            if g is None:
                bad.append(f'{c["ref"]}.{num}: 文件里没这个脚')
            elif math.hypot(g[0] - q[0], g[1] - q[1]) > 0.01:
                bad.append(f'{c["ref"]}.{num}: 原 {q} → 现 {g}')

    want_nets = legacy_nets(page, libs)
    have_nets = {frozenset((r[0], str(r[1])) for r in rec['pins'])
                 for rec in extract.build_nets(il) if rec['pins']}
    return {'pins': n, 'pin_bad': bad[:12], 'pin_ok': not bad,
            'nets_legacy': len(want_nets), 'nets_modern': len(have_nets),
            'net_ok': want_nets == have_nets,
            'net_extra': [sorted(x)[:4] for x in list(have_nets - want_nets)[:3]],
            'net_miss': [sorted(x)[:4] for x in list(want_nets - have_nets)[:3]],
            **gate_graphics(libs), **gate_visibility(out_path),
            'symbols': len(il['symbols']), 'wires': len(il['wires']),
            'labels': len(il['labels'])}


def fill_footprints(page, pcb_path) -> dict:
    """从 KiCad 5 的 `.kicad_pcb` 把封装名填回页面器件的 F2 字段。

    老工程的封装经常**只存在 PCB 里**，原理图那一栏是空的（本页 93 个符号全空）。
    空封装的块出不成板，所以这里补回来，并且**缺一个就报错**——不猜、不跳过。
    """
    if not Path(pcb_path).is_file():
        raise LegacyError(f'✗ 找不到 PCB：{pcb_path}')
    text = Path(pcb_path).read_text(encoding='utf-8', errors='replace')
    mods = re.findall(r'\(module\s+"?([^\s")]+)"?', text)
    refs = re.findall(r'\(fp_text\s+reference\s+"?([^\s")]+)"?', text)
    if len(mods) != len(refs):
        raise LegacyError(f'✗ PCB 里 module {len(mods)} 个 vs 位号 {len(refs)} 个，对不上')
    fpmap = {r: m for r, m in zip(refs, mods) if not r.startswith(('#', 'REF', 'G'))}
    got, miss = [], []
    for c in page['comps']:
        ref = c['ref']
        if ref.startswith('#'):
            continue
        fp = fpmap.get(ref)
        if not fp:
            miss.append(ref)
            continue
        v = c['fields'].get(1) or {}
        c['fields'][2] = {'text': fp, 'name': 'Footprint',
                          'x': v.get('x', c['at'][0]), 'y': v.get('y', c['at'][1]),
                          'size': v.get('size', 1.27), 'flags': '0001', 'visible': False}
        got.append(ref)
    if miss:
        raise LegacyError(f'✗ {len(miss)} 个器件在 PCB 里没有封装：{sorted(miss)}')
    return {'filled': len(got), 'pcb_mods': len(fpmap)}


# ── 6. 主流程 ──────────────────────────────────────────────────────────────
def _lib_compare(out_path) -> dict:
    """旁路体检：转出的符号逐个对 KiCad 自带的同名符号（`sdi.libdiff`）。

    **不是门**：老库是旧版本，几何本来就会有正当差异。它管的是"该藏/该露"那一类——
    老 DEF 的显示开关搬进 KiCad 10 后画得不一样（2026-10-29 的 `+3V3`/`GND` 脚名就是这么
    冒出来的，当时靠人肉翻官方库才发现）。这里只报数，不参与 `ok`。
    """
    try:
        from .libdiff import report as lib_report
        r = lib_report(out_path, verbose=False)
        r['bad_ids'] = [x['lib_id'] for x in r['rows'] if x['status'] == 'bad']
        return r
    except Exception as e:                      # 体检坏了不该把转换本身拖下水
        print(f'  ! 符号体检跳过：{type(e).__name__}: {e}')
        return {'skip': True, 'n_pairs': 0, 'n_bad': 0, 'n_soft': 0, 'bad_ids': []}


def convert(sch, lib, out, project=None, paper='A4', pcb=None, verbose=True) -> dict:
    page = read_page(sch)
    cache = read_lib(lib)
    libs = match_libs(page, cache)
    attach_libs(page, libs)
    fp = fill_footprints(page, pcb) if pcb else None
    project = project or Path(out).stem
    tree = build_tree(page, libs, project, paper)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    text = W.dump(tree) + '\n'
    Path(out).write_text(text, encoding='utf-8')
    g = gate(page, libs, out)
    g.update(gate_render(sch, page, out))
    g['lib_cmp'] = _lib_compare(out)
    if verbose:
        print(f'  器件 {len(page["comps"])} / 线 {len(page["wires"])} / 标签 '
              f'{len(page["labels"])} / 空脚 {len(page["noconns"])} / 节点 '
              f'{len(page["junctions"])} / 符号库 {len(libs)} 条')
        print(f'  门① 引脚 {g["pins"]:3d} 个  {"✓" if g["pin_ok"] else "✗ " + str(g["pin_bad"])}')
        print(f'  门② 连通 原图 {g["nets_legacy"]:3d} 网 / 转出 {g["nets_modern"]:3d} 网  '
              f'{"✓" if g["net_ok"] else "✗ " + str(g["net_extra"] + g["net_miss"])}')
        print(f'  门③ 图形 {g["graphics"]} 条图元的线/点  '
              f'{"✓" if g["graphic_ok"] else "✗ " + str(g["graphic_bad"])}'
              f'　电源符号 {g["power_syms"]} 个只露 Value  '
              f'{"✓" if g["vis_ok"] else "✗ " + str(g["vis_bad"])}')
        if g['render_skip']:
            print(f'  门④ 渲染 跳过（本机找不到 kicad-cli）')
        else:
            print(f'  门④ 渲染 逐标签比 {g["labels_checked"]} 种文字的落点  '
                  f'{"✓" if g["render_ok"] else "✗ " + str(g["render_bad"])}')
        if fp:
            print(f'  门⑤ 封装 从 PCB 填回 {fp["filled"]} 个（PCB 上 {fp["pcb_mods"]} 个）  ✓')
        if g.get('lib_cmp') and not g['lib_cmp'].get('skip'):
            lc = g['lib_cmp']
            tail = '✓' if not lc['n_bad'] else '✗ ' + str(lc['bad_ids'])
            print(f'  体检 同名符号 {lc["n_pairs"]:2d} 个对 KiCad 自带库  '
                  f'可见性不一致 {lc["n_bad"]}  {tail}'
                  f'（{lc["n_soft"]} 个版本/画法差异，非门）')
        print(f'→ {out}（{len(text) / 1024:.0f} KB）')
    return {'out': str(out), **g, 'footprints': fp,
            'ok': (g['pin_ok'] and g['net_ok'] and g['graphic_ok']
                   and g['vis_ok'] and g['render_ok'])}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='KiCad 5 老格式 .sch → .kicad_sch')
    ap.add_argument('sch')
    ap.add_argument('--lib', required=True, help='-cache.lib')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--project', default='')
    ap.add_argument('--pcb', default='', help='同工程的 .kicad_pcb：把封装名填回 F2')
    a = ap.parse_args(argv)
    r = convert(a.sch, a.lib, a.out, project=a.project or None, pcb=a.pcb or None)
    return 0 if r['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
