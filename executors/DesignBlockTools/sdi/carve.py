#!/usr/bin/env python3
"""从一张人画原理图里裁出可复用的 design block——**不改布局、不改连线，只删不改**。

为什么不能按矩形裁（2026-10-27 实测 STM32F103_USB_v1）：橡皮筋框住 13 个器件，
会切断 **16 根导线**、把 3 个电源符号留在框外——框里剩下的是一堆断头线。

人画图的时候其实已经替我们切好了：**他画标签和电源符号的地方就是缝**。
同一页里 `UART1_TX` 出现两次、`+3.3V` 裂成 8 个几何片、`GND` 裂成 13 片，
每个子电路旁边都有自己那个电源符号。所以切法分两层：

    **电气层**（判"哪些件是一块电路"）：
    按**几何连通**把整页拆成一片片导线簇（不按网名合并，否则跨页网全糊一起）
    → 只取"成员引脚沾到"的簇（簇是原子，整簇搬走）
    → 簇的边界天然是 引脚 / 标签 / 电源符号
    → 跨出块外的簇才需要处理：要么把对面那件也收进来，要么起个名加个标签

    **画法层**（判"原图上这一片长什么样"，2026-10-29 用户拍板"一定是完全截取，
    才能保证吸取原图的设计"）：簇只管电路，管不了分区标题、批注、分隔线、字位。
    这些靠规范里写的 `windows:`——作者自己画的分区线围出来的格子——**整窗照搬**，
    一个字节不改。窗口里躺着不属于本块的器件就当场报错，逼人拍板（写进 parts
    或把窗口收窄），既不悄悄收进来，也不悄悄留个洞。
    `origin: page` 再进一步：纸、图框、标题栏、坐标全部照原页，块 = 原页那一块。

门槛（本文件的全部意义，五条都过才算裁成功）：

  ① **覆盖**：每个非电源器件恰好属于一个块（不重不漏）
  ② **往返**：块平移回原坐标后，每个图元与原始图逐字节相同（Δ=0）；
     装饰节点同样逐字节比（只放过 uuid），装饰是"照搬"不是"重画"
  ③ **网表**：块内的网必须等于原页的网 **∩ 本块引脚**（无混簇时的必然结果，
     这条一破就说明裁到了别人家的东西）
  ④ **封口**：块外一根裸线都不许剩——每个跨出去的簇都必须有名字
  ⑤ **复原**：所有块按原坐标拼回一页，网表与原页逐 pin 相同

用法：
    python3 -m sdi.carve specs/carve_stm32f103usb.yaml --out out/carve_stm32f103usb
"""
from __future__ import annotations

import copy
import json
import re
import sys
import uuid as uuidlib
from pathlib import Path

from . import schwrite as W

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ILTOOLS = ROOT.parent / 'SchematicDesign' / 'il' / 'tools'


def _extract():
    if str(ILTOOLS) not in sys.path:
        sys.path.insert(0, str(ILTOOLS))
    import extract, geom                                         # noqa: PLC0415
    return extract, geom


# ── 几何分片：只按导线连通，不按网名合并 ────────────────────────────────────
def fragmentize(il):
    """把整页拆成"导线簇"。返回 (frag_of_point, 每簇的引脚表)。

    刻意**不**按网名合并：`+3.3V` 在图上本来就有 8 个互不相连的片，
    合并了就看不出"人在这里放了一个电源符号，那就是缝"。
    """
    extract, geom = _extract()
    nodes = set()
    for s in il['symbols']:
        for q in extract.symbol_pins(il, s).values():
            nodes.add(geom.key(q))
    for l in il['labels']:
        nodes.add(geom.key((l['at'][0], l['at'][1])))
    for q in il['junctions']:
        nodes.add(geom.key(q))
    # 连接判据只有一份：extract.connectivity（导线 + 端点重合，与 KiCad 同）
    dsu = extract.connectivity(il['wires'], nodes)

    def frag(pt):
        return dsu.find(geom.key((pt[0], pt[1])))

    pins = {}
    for s in il['symbols']:
        for n, q in (extract.symbol_pins(il, s) or {}).items():
            pins.setdefault(frag(q), []).append((s['id'], n))
    return frag, pins


def carriers(il, frag):
    """每簇上的"名字载体"：标签文字 + 电源符号名（PWR_FLAG 不算名字）。"""
    extract, _ = _extract()
    out = {}
    for l in il['labels']:
        if l.get('text'):
            out.setdefault(frag(l['at']), []).append(l['text'])
    for s in il['symbols']:
        if not s.get('power'):
            continue
        nm = ((s.get('fields') or {}).get('Value') or {}).get('text')
        if nm and nm not in ('PWR_FLAG',):
            q = (extract.symbol_pins(il, s) or {}).get('1') or (s['at'][0], s['at'][1])
            out.setdefault(frag(q), []).append(nm)
    return out


# ── 裁一块 ────────────────────────────────────────────────────────────────
def sym_box(s):
    """器件在图上的占地（脚 + 位号/数值字位），用来判"在不在窗口里"。"""
    xs = [s['at'][0]] + [p[0] for p in (s.get('pins_at') or {}).values()]
    ys = [s['at'][1]] + [p[1] for p in (s.get('pins_at') or {}).values()]
    return min(xs), min(ys), max(xs), max(ys)


def hit(box, w):
    """两个矩形有没有叠（边界算叠）。"""
    return not (box[2] < w[0] or box[0] > w[2] or box[3] < w[1] or box[1] > w[3])


def in_pt(q, w):
    return w[0] <= q[0] <= w[2] and w[1] <= q[1] <= w[3]


def plan_block(il, name, members, expose=None, decoration=None, windows=None,
               verbose=True):
    """算出这块要搬走哪些图元。

    `expose`     = {块内引脚: 网名}，给裸的跨簇起名。
    `decoration` = 额外点名要带的**页面文字**（原话照抄）。窗口已经能按几何带走
                   文字，这个只留给"字落在窗口外、但确实是本块的批注"。
    `windows`    = 原图**分区框**（作者自己画的分隔线围出来的格子），
                   `[[x0, y0, x1, y1], …]`，页坐标 mm。窗口是"完全截取"的唯一
                   可算判据：框里躺着的图元整窗照搬，一个字节不改，不挑不补。
                   框里有别人家的器件就当场报错——要么加进 `parts`（完全截取），
                   要么把窗口缩小；绝不悄悄收进来，也不悄悄留个洞。
    """
    extract, geom = _extract()
    expose = expose or {}
    decoration = list(decoration or [])
    windows = [tuple(float(v) for v in w) for w in (windows or [])]
    frag, pins = fragmentize(il)
    car = carriers(il, frag)
    # ⚠ 多单元器件（U2 = A/B/C/D 四个 unit、U8 = 两个 unit）在 IL 里是**多条 symbol 记录、
    # 同一个位号**。按 `{id: symbol}` 建表会把前面几个单元静默吞掉，只剩最后一条：
    # 症状 = 被吞掉的单元引脚没有簇、没有线，块文件里成了单脚网（UPduino 的 U2.38/31/41），
    # 门③ 报"块里多出三个单脚网"。所以这里按位号收成 list。
    symof = {}
    for s in il['symbols']:
        symof.setdefault(s['id'], []).append(s)
    is_power = {s['id']: bool(s.get('power')) for s in il['symbols']}
    member_pins = {r for r in members if r in symof}
    for r in members:
        if r not in symof:
            raise SystemExit(f'✗ {name}: 页上没有位号 {r}')
    # 成员引脚沾到的簇 = 整块。簇是原子：绝不切半个簇。
    frags = set()
    for r in members:
        for s in symof[r]:
            for n, q in (extract.symbol_pins(il, s) or {}).items():
                frags.add(frag(q))

    def pin_xy(r, n):
        """位号 + 脚名 → 页坐标；多单元器件要找到**有这个脚的那个单元**。"""
        for s in symof[r]:
            q = (s.get('pins_at') or {}).get(n)
            if q:
                return tuple(q)
        return None

    # 跨出去的簇：簇里有非成员的器件脚。
    # 电源符号（GND/+3V3/PWR_FLAG）不算"别人家的器件"——它们跟着簇自动搬走。
    crossing = {}
    for f in frags:
        out = [(r, n) for r, n in pins.get(f, [])
               if r not in member_pins and not is_power.get(r)]
        if out:
            crossing[f] = out

    # 裸的跨簇（既没有标签也没有电源符号收口）必须由语言起名，否则块不封口
    bare = {f: v for f, v in crossing.items() if not car.get(f)}
    missing = []
    for f, outs in bare.items():
        inside = [r for r, n in pins.get(f, []) if r in member_pins]
        got = next((expose[f'{r}.{n}'] for r, n in pins.get(f, [])
                    if f'{r}.{n}' in expose), None)
        if not got:
            missing.append({'inside': sorted(set(inside)), 'outside': sorted(set(outs)),
                            'hint': f'expose: {{"{outs[0][0]}.{outs[0][1]}": "<网名>"}}'})
    if missing:
        raise SystemExit(f'✗ {name}: {len(missing)} 个裸跨簇没起名，块不封口：'
                         + json.dumps(missing, ensure_ascii=False))

    # 选图元：符号按成员；线/结点/标签/空脚按"落在这块选中的簇上"
    # 电源符号要按**它的引脚**取簇，不能按符号原点（图形不对称时原点在脚外）。
    def sym_frag(s):
        if s.get('power'):
            q = (extract.symbol_pins(il, s) or {}).get('1') or (s['at'][0], s['at'][1])
            return frag(q)
        return None

    keep_sym = [i for i, s in enumerate(il['symbols'])
                if s['id'] in member_pins or (s.get('power') and sym_frag(s) in frags)]
    keep_wire = [i for i, w in enumerate(il['wires']) if frag(w['pts'][0]) in frags]
    keep_junc = [i for i, j in enumerate(il['junctions']) if frag(j) in frags]
    keep_lab = [i for i, l in enumerate(il['labels']) if frag(l['at']) in frags]
    keep_nc = [i for i, q in enumerate(il['no_connects']) if frag(q) in frags]

    # 分区框：框里的图元一律照搬（簇判据管不到的电源符号/散线也一并带走）
    intruders = []
    if windows:
        for s in il['symbols']:
            if s['id'] in member_pins or s.get('power'):
                continue
            if any(hit(sym_box(s), w) for w in windows):
                intruders.append(s['id'])
        if intruders:
            raise SystemExit(
                f'✗ {name}: 分区框里有不属于本块的器件 {sorted(intruders)}'
                f'——要么把它写进 parts（完全截取），要么把窗口收窄')
        keep_sym = sorted(set(keep_sym) | {
            i for i, s in enumerate(il['symbols'])
            if s.get('power') and any(hit(sym_box(s), w) for w in windows)})
        keep_wire = sorted(set(keep_wire) | {
            i for i, w in enumerate(il['wires'])
            if any(hit((min(p[0] for p in w['pts']), min(p[1] for p in w['pts']),
                        max(p[0] for p in w['pts']), max(p[1] for p in w['pts'])), win)
                   for win in windows)})
        keep_junc = sorted(set(keep_junc) | {
            i for i, j in enumerate(il['junctions']) if any(in_pt(j, w) for w in windows)})
        keep_lab = sorted(set(keep_lab) | {
            i for i, l in enumerate(il['labels']) if any(in_pt(l['at'], w) for w in windows)})
        keep_nc = sorted(set(keep_nc) | {
            i for i, q in enumerate(il['no_connects']) if any(in_pt(q, w) for w in windows)})

    # 起名标签放在"外面那只脚"原来的位置上：线一个字节都不动，只是末端多了个字
    add = []
    for f in bare:
        outs = bare[f]
        got = next(expose[f'{r}.{n}'] for r, n in pins.get(f, [])
                   if f'{r}.{n}' in expose)
        r, n = outs[0]
        q = pin_xy(r, n)
        if q is None:
            raise SystemExit(f'✗ {name}: 起名标签找不到 {r}.{n} 的坐标')
        add.append({'text': got, 'at': (q[0], q[1]), 'angle': 0.0,
                    'why': f'{r}.{n} 跨出本块'})

    inside_pins = sorted({(r, n) for r in members for s in symof[r]
                          for n in (s.get('pins_at') or {})})
    rep = {'block': name, 'members': sorted(members), 'fragments': len(frags),
           'symbols': len(keep_sym), 'wires': len(keep_wire),
           'junctions': len(keep_junc), 'labels': len(keep_lab),
           'no_connects': len(keep_nc), 'labels_added': add,
           'decoration': list(decoration),
           'crossing': [{'inside': sorted({r for r, n in pins.get(f, []) if r in member_pins}),
                         'outside': sorted(set(v)),
                         'sealed_by': sorted(set(car.get(f) or []))}
                        for f, v in crossing.items()]}
    plan = {'name': name, 'keep_sym': keep_sym, 'keep_wire': keep_wire,
            'keep_junc': keep_junc, 'keep_lab': keep_lab, 'keep_nc': keep_nc,
            'add': add, 'pins': inside_pins, 'frag': frag, 'decoration': list(decoration),
            'windows': windows}
    if verbose:
        print(f'  {name:16s} 器件 {len(members):2d} / 簇 {len(frags):2d} / 线 {len(keep_wire):2d}'
              f' / 外挂标签 {len(add)}  跨簇 {len(crossing)}（裸 {len(bare)}）')
    return plan, rep


# ── 写文件：整棵 S 表达式只挑节点、只加标签，坐标一律不改──────────────
MARGIN = 20.0           # 块文件左上角留白（mm），和 KiCad 页面网格对齐
GRID = 1.27             # KiCad 默认连接网格（50 mil）


def page_grid(il):
    """整页的坐标最大公约数——人画图落在哪套网格上。"""
    import math
    g = 0
    vals = []
    for s in il['symbols']:
        vals += list(s['at'][:2])
        for p in (s.get('pins_at') or {}).values():
            vals += [p[0], p[1]]
    for w in il['wires']:
        for p in w['pts']:
            vals += [p[0], p[1]]
    for v in vals:
        q = int(round(abs(v) * 1000))
        if q:
            g = math.gcd(g, q)
    return g / 1000.0 if g else None


def snap_offset(x0, g):
    """把平移量对到网格上：**相对位置一个字节不动**，只是整块落在原图那套网格的
    同名格点上。不做这一步，ERC 会甩一堆 "off connection grid"（实测 79 条）。"""
    raw = MARGIN - x0
    if not g or abs(GRID % g) > 1e-6:      # 原图网格不比 1.27 细，对不齐就不硬对
        return raw
    return GRID * round(raw / GRID)


def block_extent(il0, plan):
    """这块的图形范围（原坐标）。加进来的起名标签也算——否则标签会跑出页面。"""
    xs, ys = [], []
    for i in plan['keep_sym']:
        s = il0['symbols'][i]
        xs.append(s['at'][0]); ys.append(s['at'][1])
        for q in (s.get('pins_at') or {}).values():
            xs.append(q[0]); ys.append(q[1])
        for f in (s.get('fields') or {}).values():
            if f.get('at'):
                xs.append(f['at'][0]); ys.append(f['at'][1])
    for i in plan['keep_wire']:
        for p in il0['wires'][i]['pts']:
            xs.append(p[0]); ys.append(p[1])
    for i in plan['keep_lab']:
        xs.append(il0['labels'][i]['at'][0]); ys.append(il0['labels'][i]['at'][1])
    for i in plan['keep_junc']:
        xs.append(il0['junctions'][i][0]); ys.append(il0['junctions'][i][1])
    for i in plan['keep_nc']:
        xs.append(il0['no_connects'][i][0]); ys.append(il0['no_connects'][i][1])
    for a in plan['add']:
        xs.append(a['at'][0]); ys.append(a['at'][1])
    return min(xs), min(ys), max(xs), max(ys)


DECO_KINDS = ('text', 'text_box', 'polyline', 'rectangle', 'arc', 'circle', 'image')


# ⚠ 引号可省：源文件里是 `(uuid "…")`，`W.dump` 输出的是 `(uuid …)`。
#   早先写死带引号 → "应带" 那一侧的 uuid 抹不掉、"写回" 那一侧抹得掉，
#   装饰往返门**永远不可能通过**（2026-10-30 实测，5 个节点内容全同、只差 uuid 引号）。
UUID_RE = re.compile(r'\(uuid "?[0-9a-fA-F-]{36}"?\)')


def _no_uuid(text):
    """比对时把 uuid 抹掉：块里每个节点都换了新 uuid，那不是"改了图"。"""
    return UUID_RE.sub('(uuid <新>)', text if isinstance(text, str) else W.dump(text))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 笔画字体一个字的宽度 ≈ 0.88 × 字高（原页实测：'Connectors and USB' 18 字、字高 1.27mm、
# 量得总宽 19.9mm）。宁可按宽一点算：窗口判据上多收一条不会错，漏收就会缺一块图。
CHAR_W = 0.88


def _font_box(node):
    """文字节点**真正画出来的**那块地方。

    只认锚点会漏东西：原页的分区标题 'Connectors and USB' 锚点在 x=146.685（窗口外），
    字却从 136.7 横跨到 156.6——左半截明明就画在 MCU 这一格里。用户拍板"完全截取"，
    那就得按字实际占多大算，不能按锚点算。
    """
    at = next((c for c in node[1:] if isinstance(c, list) and c and c[0] == 'at'
               and len(c) >= 3), None)
    if at is None:
        return None
    ax, ay = _num(at[1]), _num(at[2])
    if ax is None or ay is None:
        return None

    if node[0] == 'text_box':                    # text_box 自带 (size 高 宽)，就是框的大小
        size = next((c for c in node[1:] if isinstance(c, list) and c and c[0] == 'size'
                     and len(c) >= 3), None)
        if size:
            h, w = _num(size[1]) or 0, _num(size[2]) or 0
            return ax - w / 2, ay - h / 2, ax + w / 2, ay + h / 2

    text = str(node[1]) if len(node) > 1 else ''
    size, just = 1.27, set()
    for c in node[1:]:
        if not (isinstance(c, list) and c and c[0] == 'effects'):
            continue
        for d in c[1:]:
            if not (isinstance(d, list) and d):
                continue
            if d[0] == 'font':
                for e in d[1:]:
                    if isinstance(e, list) and e and e[0] == 'size' and len(e) >= 2:
                        size = _num(e[1]) or size
            elif d[0] == 'justify':
                just = {str(x) for x in d[1:]}
    w, h = CHAR_W * size * max(len(text), 1), size
    x0, x1 = ((ax, ax + w) if 'left' in just else
              (ax - w, ax) if 'right' in just else (ax - w / 2, ax + w / 2))
    y0, y1 = ((ay, ay + h) if 'top' in just else
              (ay - h, ay) if 'bottom' in just else (ay - h / 2, ay + h / 2))
    return x0, y0, x1, y1


def node_box(node):
    """装饰节点的占地：at / start / end / center / xy 全收，圆再加上半径。

    `W.parse` 把原子都读成字符串（写回要逐字一致），这里转成数来量。
    """
    if node[0] in ('text', 'text_box'):
        return _font_box(node)

    xs, ys = [], []
    for c in node[1:]:
        if not isinstance(c, list) or not c:
            continue
        if c[0] in ('at', 'start', 'end', 'center') and len(c) >= 3:
            a, b = _num(c[1]), _num(c[2])
            if a is not None and b is not None:
                xs.append(a); ys.append(b)
        elif c[0] == 'pts':
            for q in c[1:]:
                if isinstance(q, list) and q and q[0] == 'xy' and len(q) >= 3:
                    a, b = _num(q[1]), _num(q[2])
                    if a is not None and b is not None:
                        xs.append(a); ys.append(b)
        elif c[0] == 'radius' and len(c) >= 2 and xs:
            r = _num(c[1])
            if r:
                xs += [xs[0] - r, xs[0] + r]; ys += [ys[0] - r, ys[0] + r]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def plan_deco(page, windows, texts=None):
    """窗口里的页面装饰（分区标题、批注、分隔线、框、图）原样带走。

    装饰没有电气身份，判归属只有一条可算的路：**落在作者自己画的分区框里**。
    返回 (下标, 每个节点的原文, 没搬的清单)；下标与 `write_block` 里的计数器同序。
    """
    tree = W.parse(Path(page).read_text(encoding='utf-8', errors='replace'))
    texts = list(texts or [])
    windows = [tuple(float(v) for v in w) for w in (windows or [])]
    keep, dumps, dropped = [], [], []
    i = 0
    for node in tree[1:]:
        if not isinstance(node, list) or not node or node[0] not in DECO_KINDS:
            continue
        idx = i
        i += 1
        k = node[0]
        box = node_box(node)
        named = k == 'text' and len(node) > 1 and node[1] in texts
        if named or (windows and box and any(hit(box, w) for w in windows)):
            keep.append(idx)
            dumps.append(W.dump(node))
        else:
            dropped.append(f'text:{node[1]}' if k == 'text' and len(node) > 1 else k)
    return keep, dumps, dropped


def write_block(page, out_path, plan, name):
    tree = W.parse(Path(page).read_text(encoding='utf-8', errors='replace'))
    dx, dy = plan['dx'], plan['dy']
    origin = plan.get('origin') or 'corner'
    ks, kw, kj, kl, kn = (set(plan['keep_sym']), set(plan['keep_wire']),
                          set(plan['keep_junc']), set(plan['keep_lab']), set(plan['keep_nc']))
    kdeco = set(plan.get('keep_deco') or [])
    isym = iwire = ijunc = ilab = inc = ideco = 0
    items, libs, used = [], {}, set()
    head = [c for c in tree[1:] if isinstance(c, list) and c
            and c[0] in ('version', 'generator', 'generator_version', 'embedded_fonts')]
    libsrc = None
    for node in tree[1:]:
        if not isinstance(node, list) or not node:
            continue
        k = node[0]
        if k == 'lib_symbols':
            libsrc = node
            continue
        if k in ('version', 'generator', 'generator_version', 'uuid', 'paper',
                 'title_block', 'sheet_instances', 'embedded_fonts'):
            continue
        take = True
        note = None                                     # 没搬走的**装饰/判不了**的节点
        if k == 'symbol':
            take = isym in ks
            isym += 1
        elif k == 'wire':
            take = iwire in kw
            iwire += 1
        elif k == 'junction':
            take = ijunc in kj
            ijunc += 1
        elif k in ('label', 'global_label', 'hierarchical_label'):
            take = ilab in kl
            ilab += 1
        elif k == 'no_connect':
            take = inc in kn
            inc += 1
        elif k in DECO_KINDS:
            # 装饰（分区标题/批注/分隔线/框/图）按**分区框**几何带走（见 plan_deco）
            take = ideco in kdeco
            ideco += 1
            note = f'text:{node[1]}' if k == 'text' and len(node) > 1 else k
        elif k in ('bus', 'bus_entry', 'sheet'):
            # 总线/子页不在 extract 的判据里，搬走等于搬进一块判不了连接的东西。
            # 不悄悄丢：记账，由主流程报出来，让人决定这块还能不能裁。
            take = False
            note = k
        if not take:
            # 装饰的"没搬"由 plan_deco 记账（这里再记一遍就重复了）；总线/子页只会落到这里
            if note and k in ('bus', 'bus_entry', 'sheet'):
                plan.setdefault('dropped', []).append(note)
            continue
        items.append(node)
        if k == 'symbol':
            for c in node:
                # ⚠ `lib_name` 必须一起收：Altium 导出的多 part 符号在页里是**一个器件一条
                # lib_symbols**（`b4654…`、`b4654…_1` … `b4654…_5`），实例靠 `(lib_name
                # "b4654…_5")` 指自己那条。只按 `lib_id` 收，那 5 颗电容的**符号本体在块里
                # 整条消失**——线还在、脚名还在，图上看就是"电容没画出来"。门①~⑤ 全绿也
                # 看不出来（它们比的是 IL 和文本），只有 sdi.renderdiff 拿 KiCad 自己渲的图
                # 一比才现形（2026-10-29，mcu_stm32f411 少 627 个图元、imu_mpu6050 少 77）。
                if isinstance(c, list) and c and c[0] in ('lib_id', 'lib_name'):
                    used.add(c[1])

    if libsrc is not None:
        for d in libsrc[1:]:
            if isinstance(d, list) and len(d) > 1 and d[1] in used:
                libs[d[1]] = d

    root_uuid = str(uuidlib.uuid4())
    for node in items:
        if node and node[0] == 'symbol':
            W.set_symbol_ref(node, _ref(node), name, root_uuid)
        W.shift(node, dx, dy)
        W.reuuid(node, None)
    for a in plan['add']:
        n = W.make_label(a['text'], a['at'][0] + dx, a['at'][1] + dy, a['angle'])
        items.append(n)

    out = ['kicad_sch'] + copy.deepcopy(head)
    out.append(['uuid', W.Str(root_uuid)])
    if origin == 'page':
        # 完全截取：纸、图框、标题栏、坐标全部照原页——块就是原页那一块，不做任何
        # 再加工（不挪、不缩、不换纸）。要看"吸取了多少原图的设计"，开起来一目了然。
        for node in tree[1:]:
            if isinstance(node, list) and node and node[0] in ('paper', 'title_block'):
                out.append(copy.deepcopy(node))
    else:
        # 另一种口径：纸按块自己的大小裁（内容 + 四边留白），坐标平移到左上角。
        w = round(plan['extent'][2] - plan['extent'][0] + 2 * MARGIN, 2)
        h = round(plan['extent'][3] - plan['extent'][1] + 2 * MARGIN, 2)
        out.append(['paper', W.Str('User'), f'{w:g}', f'{h:g}'])
    out.append(['lib_symbols'] + [libs[k] for k in libs])
    out += items
    out.append(['sheet_instances', ['path', W.Str('/'), ['page', W.Str('1')]]])
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(W.dump(out) + '\n', encoding='utf-8')
    return out_path


def _ref(sym):
    for prop in sym:
        if isinstance(prop, list) and prop and prop[0] == 'property' \
                and len(prop) >= 3 and prop[1] == 'Reference':
            return prop[2]
    return '?'


# ── ⑤ 复原门：把四块按原坐标拼回一页，看能不能变回原来那张图 ───────────────
def recompose(page, out_path, plans):
    """块 → 一页。每块平移回它原来的位置，合在一起。

    这是"裁没裁坏"最硬的一道：块各自 ERC 必然报"电源没人驱动 / 标签只接一脚"
    ——那本来就是块边界；拼回去之后这些必须**全部消失**，并且网表与原页逐 pin 相同。
    """
    tree = W.parse(Path(page).read_text(encoding='utf-8', errors='replace'))
    head = [c for c in tree[1:] if isinstance(c, list) and c
            and c[0] in ('version', 'generator', 'generator_version', 'embedded_fonts')]
    libs = {}
    for node in tree[1:]:
        if isinstance(node, list) and node and node[0] == 'lib_symbols':
            for d in node[1:]:
                if isinstance(d, list) and len(d) > 1:
                    libs[d[1]] = d

    root_uuid = str(uuidlib.uuid4())
    items = []
    for plan in plans:
        sub = W.parse(Path(plan['file']).read_text(encoding='utf-8', errors='replace'))
        for node in sub[1:]:
            if not isinstance(node, list) or not node:
                continue
            if node[0] in ('version', 'generator', 'generator_version', 'uuid', 'paper',
                           'title_block', 'sheet_instances', 'embedded_fonts'):
                continue
            if node[0] == 'lib_symbols':
                for d in node[1:]:
                    if isinstance(d, list) and len(d) > 1 and d[1] not in libs:
                        libs[d[1]] = d
                continue
            W.shift(node, -plan['dx'], -plan['dy'])
            if node[0] == 'symbol':
                W.set_symbol_ref(node, _ref(node), 'recompose', root_uuid)
            W.reuuid(node, None)
            items.append(node)

    out = ['kicad_sch'] + copy.deepcopy(head)
    out.append(['uuid', W.Str(root_uuid)])
    out.append(['paper', W.Str('A3')])
    out.append(['lib_symbols'] + [libs[k] for k in libs])
    out += items
    out.append(['sheet_instances', ['path', W.Str('/'), ['page', W.Str('1')]]])
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(W.dump(out) + '\n', encoding='utf-8')
    return out_path


# ── 主流程 ────────────────────────────────────────────────────────────────
def page_texts(page) -> set:
    """整页的**文字**（原话）——用来核对规范里点的装饰句，写错一个字就报错。"""
    tree = W.parse(Path(page).read_text(encoding='utf-8', errors='replace'))
    return {n[1] for n in tree[1:]
            if isinstance(n, list) and n and n[0] == 'text' and len(n) > 1
            and isinstance(n[1], str)}


def carve(spec_path, out_dir, verbose=True):
    import yaml
    spec = yaml.safe_load(Path(spec_path).read_text(encoding='utf-8'))
    page = Path(spec['page'])
    if not page.is_absolute():
        for base in (Path.cwd(), ROOT, ROOT.parent):
            if (base / page).is_file():
                page = base / page
                break
    extract, geom = _extract()
    il0 = extract.extract(str(page))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 块直接落在 `<out_dir>/<块名>/`（`sdi.BLOCKS` = 仓库根 `DesignBlocks/`）；
    # 过程账（复原页 + report）收进 `_carve/<规范名>/`，不跟块混在一起。
    proc = out_dir / '_carve' / Path(spec_path).stem
    proc.mkdir(parents=True, exist_ok=True)
    reps, plans = [], []
    # 装饰句点错了名字就当场报错：静默不搬等于"标题悄悄消失"，比搬错还难发现
    known = page_texts(page)
    bad = sorted({t for b in spec['blocks'] for t in (b.get('decoration') or [])} - known)
    if bad:
        raise SystemExit(f'✗ 规范里的装饰句页上找不到 {bad}（页上的文字：{sorted(known)}）')
    origin = str(spec.get('origin') or 'corner')
    if origin not in ('page', 'corner'):
        raise SystemExit(f'✗ origin 只能是 page / corner，规范里写的是 {origin!r}')
    for b in spec['blocks']:
        plan, rep = plan_block(il0, b['name'], list(b['parts']),
                               b.get('expose') or {}, b.get('decoration') or [],
                               b.get('windows') or [], verbose=verbose)
        plan['origin'] = origin
        kdeco, dumps, drop = plan_deco(page, plan['windows'], b.get('decoration'))
        plan['keep_deco'], plan['deco_dumps'], plan['dropped'] = kdeco, dumps, drop
        rep['deco'] = len(kdeco)
        rep['dropped'] = drop
        plans.append(plan)
        reps.append(rep)

    # ① 覆盖：每个非电源器件恰好属于一块
    seen = {}
    for b in spec['blocks']:
        for r in b['parts']:
            seen.setdefault(r, []).append(b['name'])
    allparts = {s['id'] for s in il0['symbols'] if not s.get('power')}
    dup = {r: v for r, v in seen.items() if len(v) > 1}
    miss = sorted(allparts - set(seen))
    cover_ok = not dup and not miss
    if verbose:
        print(f'  门① 覆盖：页上 {len(allparts)} 个器件；重复 {dup or "无"}；漏掉 {miss or "无"}'
              f'  {"✓" if cover_ok else "✗"}')

    # ②③ 写文件 → 把文件读回来 → 搬回原坐标，逐图元 / 逐网与原始图比。
    # 只比"我打算留下的那些"是自证；这里的对照物是**原始页本身**。
    orig_nets = [set(map(tuple, n['pins'])) for n in extract.build_nets(il0) if n['pins']]
    grid = page_grid(il0)
    rt, net_ok = [], True
    for b, plan, rep in zip(spec['blocks'], plans, reps):
        x0, y0, x1, y1 = block_extent(il0, plan)
        dx, dy = (0.0, 0.0) if plan['origin'] == 'page' else \
            (snap_offset(x0, grid), snap_offset(y0, grid))
        plan['dx'], plan['dy'] = dx, dy
        plan['extent'] = (x0, y0, x1, y1)          # 纸按这块的大小裁（见 write_block）
        rep['grid'] = grid
        rep['origin'] = [round(x0 + dx, 3), round(y0 + dy, 3)]
        # `pseudo: true` 的块（例如只为过门①覆盖的 rest_of_page）不是可复用块：
        # 产物落 `_carve/`，不进 `DesignBlocks/`。
        bdst = (proc / 'blocks' if b.get('pseudo') else out_dir) / rep['block']
        p = write_block(page, bdst / f'{rep["block"]}.kicad_sch', plan, rep['block'])
        rep['file'] = str(p)
        plan['file'] = str(p)
        rep['size_mm'] = [round(x1 - x0, 3), round(y1 - y0, 3)]
        rep['offset'] = [round(dx, 3), round(dy, 3)]
        rep['dropped'] = plan.get('dropped', [])
        if verbose:
            print(f'   装饰 {rep["block"]}: 照搬 {rep["deco"]} 处'
                  + (f' / 不搬 {len(rep["dropped"])} 处（{", ".join(rep["dropped"])}）'
                     if rep['dropped'] else ''))

        # 装饰也要过门：写出来的文件里那些节点必须与原文一字不差（原样搬 = 不是重画）
        got = [_no_uuid(n) for n in W.parse(Path(p).read_text(encoding='utf-8'))[1:]
               if isinstance(n, list) and n and n[0] in DECO_KINDS]
        want = [_no_uuid(n) for n in (plan.get('deco_dumps') or [])]
        rep['deco_gate'] = {'ok': got == want, 'got': len(got), 'want': len(want)}
        if got != want:
            raise SystemExit(f'✗ 装饰往返不一致 {rep["block"]}: '
                             f'写回 {len(got)} 处 / 应带 {len(want)} 处')

        back = extract.extract(str(p))                       # 真读回来
        _shift_il(back, -dx, -dy)
        d = _diff_prim(back, il0, plan)
        rep['roundtrip'] = d
        rt.append(d)

        bp = set(map(tuple, plan['pins']))
        have = {frozenset(map(tuple, n['pins'])) for n in extract.build_nets(back) if n['pins']}
        want = {frozenset(o & bp) for o in orig_nets} - {frozenset()}
        ok = have == want
        net_ok &= ok
        rep['net_gate'] = {'ok': ok, 'have': len(have), 'want': len(want),
                           'extra': [sorted(x) for x in list(have - want)[:3]],
                           'miss': [sorted(x) for x in list(want - have)[:3]]}

    # ④ 封口已经由 plan_block 硬拦（裸跨簇没起名直接退出）
    for rep in reps:
        rep['sealed'] = True

    # ⑤ 复原：四块按原坐标拼回一页
    rp = recompose(page, proc / 'recompose.kicad_sch', plans)
    back = extract.extract(str(rp))
    have = {frozenset(map(tuple, n['pins'])) for n in extract.build_nets(back) if n['pins']}
    want = {frozenset(o) for o in orig_nets}
    rec_ok = have == want
    rec = {'file': str(rp), 'ok': rec_ok, 'nets': len(have), 'want': len(want),
           'extra': [sorted(x) for x in list(have - want)[:3]],
           'miss': [sorted(x) for x in list(want - have)[:3]]}

    ok = cover_ok and all(d['mismatch'] == 0 for d in rt) and net_ok and rec_ok
    (proc / 'carve.report.json').write_text(
        json.dumps({'page': str(page), 'ok': ok, 'recompose': rec, 'blocks': reps},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if verbose:
        for d, rep in zip(rt, reps):
            print(f'  门② 往返 {rep["block"]:16s} {d["primitives"]:4d} 图元 / 不一致 '
                  f'{d["mismatch"]}  {"✓" if d["mismatch"] == 0 else "✗ " + repr(d["extra"] + d["missing"])}')
        for rep in reps:
            g = rep['net_gate']
            print(f'  门③ 网表 {rep["block"]:16s} {g["have"]:3d} 网  '
                  f'{"✓" if g["ok"] else "✗ " + json.dumps([g["extra"], g["miss"]], ensure_ascii=False)}')
        print(f'  门⑤ 复原 {len(plans)} 块拼回一页 {rec["nets"]:3d} 网 / 原页 {rec["want"]:3d} 网  '
              f'{"✓" if rec_ok else "✗ " + json.dumps([rec["extra"], rec["miss"]], ensure_ascii=False)}')
        print(('✓' if ok else '✗') + f' 裁剪 {len(plans)} 块 → {out_dir}')
    return {'ok': ok, 'blocks': reps}


def _unused_subset(il0, plan):
    """按 plan 从原 IL 里挑出这块的图元（坐标保持原样，不平移）。"""
    return {'schema': il0.get('schema'), 'libsym': il0.get('libsym') or {},
            'symbols': [il0['symbols'][i] for i in plan['keep_sym']],
            'wires': [il0['wires'][i] for i in plan['keep_wire']],
            'junctions': [il0['junctions'][i] for i in plan['keep_junc']],
            'labels': [il0['labels'][i] for i in plan['keep_lab']]
                      + [{'text': a['text'], 'at': [a['at'][0], a['at'][1], a['angle']],
                          'kind': 'local', 'shape': None} for a in plan['add']],
            'no_connects': [il0['no_connects'][i] for i in plan['keep_nc']],
            'nets': [], 'texts': []}


def _shift_il(il, dx, dy):
    """把读回来的块整体平移（用于"搬回原坐标"比对）。"""
    for s in il['symbols']:
        s['at'] = [s['at'][0] + dx, s['at'][1] + dy] + list(s['at'][2:])
        # `pins_at` 是 extract 缓存的绝对脚坐标，`symbol_pins` 直接用它——
        # 漏了它就等于"符号挪了、脚没挪"，网表门会报一堆假的错。
        if s.get('pins_at'):
            s['pins_at'] = {n: [p[0] + dx, p[1] + dy] for n, p in s['pins_at'].items()}
        for f in (s.get('fields') or {}).values():
            if f.get('at'):
                f['at'] = [f['at'][0] + dx, f['at'][1] + dy] + list(f['at'][2:])
    for w in il['wires']:
        w['pts'] = [[p[0] + dx, p[1] + dy] for p in w['pts']]
    for l in il['labels']:
        l['at'] = [l['at'][0] + dx, l['at'][1] + dy] + list(l['at'][2:])
    il['junctions'] = [[j[0] + dx, j[1] + dy] for j in il['junctions']]
    il['no_connects'] = [[q[0] + dx, q[1] + dy] for q in il['no_connects']]


TOL = 4          # 比对精度：4 位小数 = 0.1µm。平移写回再读回来只有浮点噪声，远小于此。


def _diff_prim(back, il0, plan):
    """块（搬回原坐标）与原始页逐图元比。新增的起名标签单独记，不算改动。"""
    def prim(x):
        out = []
        for s in x['symbols']:
            out.append(('sym', s['id'], round(s['at'][0], TOL), round(s['at'][1], TOL)))
        for w in x['wires']:
            out.append(('wire', tuple((round(p[0], TOL), round(p[1], TOL)) for p in w['pts'])))
        for l in x['labels']:
            out.append(('label', l['text'], round(l['at'][0], TOL), round(l['at'][1], TOL)))
        for j in x['junctions']:
            out.append(('junction', round(j[0], TOL), round(j[1], TOL)))
        for q in x['no_connects']:
            out.append(('nc', round(q[0], TOL), round(q[1], TOL)))
        return sorted(map(repr, out))

    base = {'symbols': [il0['symbols'][i] for i in plan['keep_sym']],
            'wires': [il0['wires'][i] for i in plan['keep_wire']],
            'junctions': [il0['junctions'][i] for i in plan['keep_junc']],
            'labels': [il0['labels'][i] for i in plan['keep_lab']],
            'no_connects': [il0['no_connects'][i] for i in plan['keep_nc']]}
    want = set(prim(base))
    for a in plan['add']:
        want.add(repr(('label', a['text'], round(a['at'][0], TOL), round(a['at'][1], TOL))))
    have = prim(back)
    bad = [h for h in have if h not in want]
    lost = [w for w in want if w not in set(have)]
    return {'primitives': len(want), 'mismatch': len(bad) + len(lost),
            'extra': bad[:3], 'missing': lost[:3], 'labels_added': len(plan['add'])}


if __name__ == '__main__':
    import argparse
    from . import BLOCKS
    ap = argparse.ArgumentParser(description='从人画原理图裁 design block')
    ap.add_argument('spec')
    ap.add_argument('--out', default=str(BLOCKS),
                    help=f'块写到哪（默认 {BLOCKS}；过程账落在 <out>/_carve/<规范名>/）')
    a = ap.parse_args()
    r = carve(a.spec, a.out)
    sys.exit(0 if r['ok'] else 1)
