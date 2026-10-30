#!/usr/bin/env python3
"""把拼装计划落成一份**真正的 KiCad 原理图**（`.kicad_sch`，v9 格式）。

`kicadblocks.py` 出的是 SVG（人看）；这个出的是 KiCad 能打开的源文件。
两者共用同一条几何规则：**块内只加 (dx, dy)**，不取整、不转角、不画新线。
区别只在输出物——SVG 是重画的（我们自己的渲染器），`.kicad_sch` 是把原始文件
搬过来改数字，所以打开后看到的就是原块。

合并四份 `.kicad_sch` 要处理的只有四件事：

1. **lib_symbols 去重**：四块引用同名符号（`Device:R`）内容一致，留一份；
   同名但内容不一致就报冲突（那说明两份文件对同一个符号的理解已经不一样了）。
2. **平移**：页面绝对坐标（`(at x y)`、`(xy x y)`、`(start/end x y)`）加 dx/dy。
   **`lib_symbols` 里的坐标是符号局部坐标，绝不能动**——动了符号本身就被拉变形。
3. **改字**：位号（`property "Reference"` 与 `instances` 两处都要改）和标签文字。
   只改文字，不动坐标。
4. **重发 uuid / 重挂 instances**：四份文件各自的根 uuid 不同，合并后必须统一指向
   新根 uuid，否则 KiCad 认不出这些符号属于本图。

用法：
    python3 -m sdi.schwrite specs/kicad_usb_temp_demo.json --out out/x/sch.kicad_sch
"""
from __future__ import annotations

import copy
import json
import re
import uuid as uuidlib
from pathlib import Path

TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')

# 页面绝对坐标用的键：`at`（点/角度）、`xy`（折线顶点）、`start/end`（矩形/文本框）
COORD_KEYS = ('at', 'xy', 'start', 'end')


class Str(str):
    """原文件里带引号的原子。不带引号的（`at`、`yes`、`none`）是关键字，不能加引号。"""


def parse(text):
    toks = TOK.findall(text)
    pos = 0

    def rd():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t == '(':
            out = []
            while toks[pos] != ')':
                out.append(rd())
            pos += 1
            return out
        if t.startswith('"'):
            return Str(t[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
        return t

    return rd()


def _q(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def dump(n):
    if isinstance(n, Str):
        return _q(n)
    if isinstance(n, str):
        return n
    return '(' + ' '.join(dump(c) for c in n) + ')'


# 同名符号比对时要忽略的"格式开关"：KiCad 不同小版本写文件时多/少这几个键，
# 图形一样但字节不一样。这几个键不影响画出来的样子，不该算冲突。
GEO_HEADS = ('rectangle', 'polyline', 'circle', 'arc', 'bezier')


def _skel(symdef):
    """符号定义的**画出来的那部分**：图形图元坐标 + 引脚（号/名/位置/长度）。

    为什么不用整段比：同一个库符号在不同 KiCad 版本里写法不一样（`(hide yes)` 放
    `property` 里还是 `effects` 里、`Datasheet` 是 `~` 还是空），图形其实一模一样。
    拿整段比会把"版本写法差异"误报成"两块对同一个符号理解不同"。
    """
    out = []

    def walk(n):
        if not isinstance(n, list) or not n:
            return
        h = n[0]
        if h in GEO_HEADS:
            out.append(dump(n))
            return
        if h == 'pin':
            num = nm = at = ln = None
            for c in n[1:]:
                if isinstance(c, list) and c:
                    if c[0] == 'number':
                        num = c[1]
                    elif c[0] == 'name':
                        nm = c[1]
                    elif c[0] == 'at':
                        at = tuple(str(x) for x in c[1:4])
                    elif c[0] == 'length':
                        ln = c[1]
            # `~` 是 KiCad 的"无脚名"占位符，和空串是同一件事
            out.append(('pin', str(num), '' if nm in (None, '~') else str(nm), at, ln))
            return
        for c in n:
            walk(c)

    walk(symdef)
    return out


def _num(x):
    """坐标写回：6 位小数就够（1e-6mm = 1nm，和门的容差同一档），去掉尾零。"""
    s = f'{x:.6f}'.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


# ── 1. 平移 ────────────────────────────────────────────────────────────────
def shift(node, dx, dy):
    """就地把这棵子树里所有页面绝对坐标 +（dx, dy）。

    `symbol` 的子树里只有它自己的 `(at)` 和各个 `property` 的 `(at)`，符号图形在
    `lib_symbols` 里（那部分我们整段跳过），所以这里可以放心一路走到底。
    """
    if not isinstance(node, list):
        return
    head = node[0] if node else None
    if head in COORD_KEYS and len(node) >= 3:
        for i in (1, 2):
            try:
                node[i] = _num(float(node[i]) + (dx if i == 1 else dy))
            except (TypeError, ValueError):
                return
        return                      # 坐标后面的角度/样式不动
    for c in node:
        shift(c, dx, dy)


# ── 2. 改字 ────────────────────────────────────────────────────────────────
def _child(node, key):
    for c in node:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def _children(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def set_symbol_ref(sym, ref, project, root_uuid):
    """位号要改两处：`property "Reference"` 是显示的那一份，`instances` 是 KiCad
    用来认"这个符号属于哪张图、叫什么"的那一份。只改一处，打开后位号会回弹。"""
    p = _child(sym, 'property')
    for prop in _children(sym, 'property'):
        if len(prop) >= 3 and prop[1] == 'Reference':
            prop[2] = Str(ref)
    inst = _child(sym, 'instances')
    if inst is not None:
        for proj in _children(inst, 'project'):
            proj[1] = Str(project)
            for path in _children(proj, 'path'):
                path[1] = Str('/' + root_uuid)
                r = _child(path, 'reference')
                if r is not None:
                    r[1] = Str(ref)
    return p


# 拼成一张平铺图之后，块边界的 `hierarchical_label` 就不再是"接到父页的出口"了：
# 它们现在只是**本页上的同网名**。KiCad 对根页上的层级标签会报
# "cannot be connected to non-existent parent sheet"（组合甲实测 12 个），
# 所以翻译时改成普通 `label`（同页同名照样连通）。这是**改标签类型，不动坐标**，
# 属于声明过的改动，计数打在输出里。
# 注意：文件格式里 `shape` 只有 global/hierarchical 标签才有，本地标签要删掉这个键，
# 否则 KiCad 解析时当未知字段。
LABEL_FLATTEN = {'hierarchical_label': 'label'}


def flatten_label(node):
    """层级标签 → 本地标签（就地改，返回是否改了）。"""
    if node[0] not in LABEL_FLATTEN:
        return False
    node[0] = LABEL_FLATTEN[node[0]]
    node[:] = [c for c in node if not (isinstance(c, list) and c and c[0] == 'shape')]
    return True


def label_text(node):
    return node[1] if len(node) > 1 and isinstance(node[1], str) else None


def make_label(text, x, y, angle=90.0, size=1.27, justify=('left', 'bottom')):
    """造一个本地网标节点，结构和 KiCad 自己写出来的 `(label ...)` 一致。

    外科改图用它给一个脚"贴上"网名——这是最小的改法：**不加符号**，只在脚外向
    一小段桩的末端放一个文字（桩是导线，KiCad 按导线连通，电气上和贴在脚上等价）。
    字号/对齐由调用方照抄**这块图自己的标签习惯**传进来，新标签才不会比老标签大一圈。
    """
    return ['label', Str(text),
            ['at', _num(x), _num(y), _num(angle)],
            ['effects', ['font', ['size', _num(size), _num(size)]],
             # `justify` 后面是**关键字原子**，不能带引号（写成 "(justify \"left\" \"bottom\")"
             # 的话 KiCad 直接 "Failed to load schematic"，整个文件打不开）。
             ['justify'] + [str(j) for j in justify]],
            ['uuid', Str(str(uuidlib.uuid4()))]]


def make_wire(a, b):
    """一段两点的导线（外科给网标加的"桩"）。"""
    return ['wire', ['pts', ['xy', _num(a[0]), _num(a[1])],
                     ['xy', _num(b[0]), _num(b[1])]],
            ['stroke', ['width', '0'], ['type', 'default']],
            ['uuid', Str(str(uuidlib.uuid4()))]]


def set_label_text(node, text):
    node[1] = Str(text)


def reuuid(node, counter):
    """全部重发 uuid：四份文件各自的 uuid 空间是独立的，撞了 KiCad 会认错对象。"""
    if not isinstance(node, list):
        return
    if node and node[0] == 'uuid' and len(node) > 1:
        node[1] = Str(str(uuidlib.uuid4()))
        return
    for c in node:
        reuuid(c, counter)


# ── 主流程 ────────────────────────────────────────────────────────────────
def build(plan_path, out_path, verbose=True):
    import sys
    here = Path(__file__).resolve().parent
    root = here.parent
    iltools = root.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(iltools) not in sys.path:
        sys.path.insert(0, str(iltools))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root.parent))

    import extract                                            # noqa: PLC0415
    from sdi import kicadblocks as K                          # noqa: PLC0415
    from sdi import surgery as SG                             # noqa: PLC0415

    plan = K.load_plan(plan_path)          # .sdi.yaml 或 .json，同一条翻译
    gap = float(plan.get('gap', 20))
    margin = float(plan.get('margin', 20))
    GRID = 1.27                            # KiCad 默认连接网格（50 mil）
    paper = plan.get('paper')
    project = Path(out_path).stem

    # 平移量与位号：直接复用 kicadblocks 的算法，两个产物才不会各说各话。
    blocks = []
    frags = {}
    pending = []
    for i, b in enumerate(plan['blocks']):
        p = Path(b['file'])
        if not p.is_absolute():
            p = root / p
        il = extract.extract(str(p))
        # 外科改图先做，而且要动 `il` 本身：改号（renumber）必须看到**手术后**的
        # 器件表，否则 SVG 那边和 .kicad_sch 这边的位号会各排各的。
        # `raw` 是手术前的同一张图，文件级的下标（第几个 symbol 节点）照着它算。
        raw = copy.deepcopy(il)
        fo = {'drop_symbols': [], 'drop_wires': [], 'drop_junctions': [],
              'drop_no_connects': [], 'add_labels': [], 'add_wires': []}
        if b.get('surgery'):
            fo = SG.apply(il, b['surgery'])['file_ops']
        alias = b.get('alias') or f'b{i}'
        pending.append({'alias': alias, 'b': b, 'path': p, 'il': il, 'raw': raw,
                        'fo': fo, 'box0': K.extent(il)})

    # 摆位：同一个函数、同一套规则（复制的块先竖着叠、整版居中）——SVG 腿和
    # `.kicad_sch` 腿必须逐字一致，否则回读门会红。
    pos, paper_auto, _content = K.sheet_layout(
        [(q['box0'][2] - q['box0'][0], q['box0'][3] - q['box0'][1]) for q in pending],
        [str(q['b']['file']) for q in pending], gap=gap, margin=margin, grid=GRID,
        paper=paper)
    if not paper:
        paper = paper_auto

    for q, (tx, ty) in zip(pending, pos):
        alias, p, il, raw, fo = q['alias'], q['path'], q['il'], q['raw'], q['fo']
        x0, y0, x1, y1 = q['box0']
        # 平移量对到 1.27mm 网格上。块内相对位置一个字节不变（只是整块落在原图那套
        # 网格的同名格点上）；不对的话 KiCad ERC 会甩上百条 "off connection grid"。
        dx = GRID * round((tx - x0) / GRID)
        dy = GRID * round((ty - y0) / GRID)
        frags[alias] = il
        blocks.append({'alias': alias, 'path': p, 'dx': dx, 'dy': dy,
                       'rename': q['b'].get('rename') or {}, 'il': il, 'raw': raw,
                       'file_ops': fo})
    # 权威改号：用语言给的那份位号表（和 design.json / 展开层同一套），
    # 改完 il['symbols'][k]['id'] 就是最终位号。
    K.renumber(frags, plan.get('refmap'))

    root_uuid = str(uuidlib.uuid4())
    libsym = {}                    # 名字 → 节点
    items = []
    lib_src = None
    flattened = 0                  # 层级标签压平成本地标签的处数（声明过的改动）
    operated = 0                   # 外科改图落到文件上的处数

    for blk in blocks:
        tree = parse(blk['path'].read_text(encoding='utf-8', errors='replace'))
        # 文件里的符号顺序 = extract 读出来的顺序（都是文档顺序），按下标配位号
        il_syms = blk['il']['symbols']
        # 外科改图：IL 那份已经在上面动过了，这里要的是**文件级**的操作
        # （删第几个 symbol 节点、删第几个 wire 节点、在哪个坐标加一个网标）。
        # 下标指向手术前的文件顺序，所以照着 `raw` 数。
        fo = blk['file_ops']
        drop_sym = set(fo['drop_symbols'])
        drop_wire = set(fo['drop_wires'])
        drop_junc = set(fo['drop_junctions'])
        drop_nc = set(fo.get('drop_no_connects') or [])
        # 文件节点下标 → 最终位号：活下来的节点按顺序对上改号后的器件表
        ref_map = dict(zip((i for i in range(len(blk['raw']['symbols'])) if i not in drop_sym),
                           [s['id'] for s in il_syms]))
        idx = wid = jid = nid = 0
        for node in tree[1:]:
            if not isinstance(node, list) or not node:
                continue
            kind = node[0]
            if kind == 'lib_symbols':
                for symdef in node[1:]:
                    if not isinstance(symdef, list) or len(symdef) < 2:
                        continue
                    name = symdef[1]
                    if name in libsym:
                        if _skel(libsym[name]) != _skel(symdef):
                            print(f'  [warn] 符号 {name} 在 {blk["alias"]} 里内容不一致，保留第一份')
                        continue
                    libsym[name] = copy.deepcopy(symdef)
                continue
            if kind in ('version', 'generator', 'generator_version', 'uuid', 'paper',
                        'title_block', 'sheet_instances', 'embedded_fonts'):
                continue           # 页头类的由合并后的根统一提供
            if kind == 'symbol':
                i, idx = idx, idx + 1
                if i in drop_sym:
                    operated += 1
                    continue       # 外科：这个件不属于这块板，整节点不搬
            elif kind == 'wire':
                i, wid = wid, wid + 1
                if i in drop_wire:
                    operated += 1
                    continue       # 外科：件删了之后这条线就悬空了
            elif kind == 'junction':
                i, jid = jid, jid + 1
                if i in drop_junc:
                    operated += 1
                    continue
            elif kind == 'no_connect':
                i, nid = nid, nid + 1
                if i in drop_nc:
                    operated += 1
                    continue       # 外科：这个脚被提成端口了，"不接"叉必须跟着删
            shift(node, blk['dx'], blk['dy'])
            if kind == 'symbol':
                old = ref_map.get(idx - 1)
                if old:
                    set_symbol_ref(node, old, project, root_uuid)
            elif kind in ('label', 'global_label', 'hierarchical_label'):
                if flatten_label(node):
                    flattened += 1
                t = label_text(node)
                if t in blk['rename']:
                    set_label_text(node, blk['rename'][t])
            reuuid(node, None)
            items.append(node)
        # 外科加的桩/网标：手术算好了坐标，平移量照旧加（跟别的图元走同一把尺子）
        for nw in fo.get('add_wires') or []:
            a, b = nw['pts']
            node = make_wire(a, b)
            shift(node, blk['dx'], blk['dy'])
            items.append(node)
            operated += 1
        for nl in fo['add_labels']:
            # 外科加的网标也要过改名表：图上原有的标签按 `rename` 改成了语言里的网名，
            # 这些新标签要是不改，同一根网在文件里就一个叫 USART2_TX、一个叫 CH2_TX，
            # 两边各算一根（回读门一抓就是"标签 34/34 但内容不同、分区 55/61"）。
            text = blk['rename'].get(nl['text'], nl['text'])
            node = make_label(text, nl['at'][0], nl['at'][1], nl['angle'],
                              nl.get('size') or 1.27, nl.get('justify') or ('left', 'bottom'))
            shift(node, blk['dx'], blk['dy'])
            items.append(node)
            operated += 1
        if lib_src is None:
            lib_src = tree

    # 根：沿用第一个文件的页头（保住我们没解析的字段），换纸、换 uuid、换 lib_symbols
    head = [c for c in lib_src[1:] if not isinstance(c, list) or not c
            or c[0] in ('version', 'generator', 'generator_version', 'embedded_fonts')]
    out = ['kicad_sch'] + copy.deepcopy(head)
    out.append(['uuid', Str(root_uuid)])
    out.append(['paper', Str(paper)])
    out.append(['lib_symbols'] + [libsym[k] for k in libsym])
    out += items
    out.append(['sheet_instances', ['path', Str('/'), ['page', Str('1')]]])

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = dump(out) + '\n'
    out_path.write_text(text, encoding='utf-8')
    if verbose:
        print(f'  块 {len(blocks)} / 图元 {len(items)} / 符号库 {len(libsym)} 条'
              f' / 边界标签压平 {flattened} 处（hierarchical_label → label）'
              + (f' / 外科 {operated} 处' if operated else ''))
        print(f'→ {out_path}（{len(text) / 1024:.0f} KB）')
    return {'path': str(out_path), 'items': len(items), 'lib_symbols': len(libsym),
            'labels_flattened': flattened, 'surgery_edits': operated,
            'root_uuid': root_uuid, '_plan': plan, 'blocks': [{k: b[k] for k in ('alias', 'dx', 'dy')}
                                               for b in blocks]}


# ── 回读门：写出去的 .kicad_sch 再读回来，必须和拼装结果逐图元一致 ──────────
def verify(il_path, sch_path, tol=0.01, verbose=True):
    """拿我们自己的 extractor 读回写出的文件，和 IL 逐图元比。

    tol 用 0.01mm 而不是 1e-6：`extract` 读文件时把引脚坐标**取整到 2 位小数**
    （`round(p, 2)`），而 IL 里的引脚是"先读、再平移"的结果，没经过第二次取整。
    两边差的是这次取整（≤5µm），不是画得不一样。符号原点、导线端点、标签位置
    仍然按 1e-6 比——那些没经过这道取整。
    """
    import sys
    iltools = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(iltools) not in sys.path:
        sys.path.insert(0, str(iltools))
    import extract                                              # noqa: PLC0415

    a = json.loads(Path(il_path).read_text(encoding='utf-8')) if isinstance(il_path, (str, Path)) \
        and str(il_path).endswith('.json') else il_path
    b = extract.extract(str(sch_path))
    out = {'checks': [], 'ok': True}

    def rec(name, x, y, t=1e-6):
        same = len(x) == len(y) and all(
            _eq_t(u, v, t) for u, v in zip(x, y))
        out['checks'].append({'item': name, 'il': len(x), 'read_back': len(y), 'ok': same})
        out['ok'] &= same
        if verbose:
            print(f'    {name:6s} {len(x):4d} / {len(y):4d}  {"✓" if same else "✗"}')

    # **按 (位号, 单元) 建索引**，不能只按位号：多单元器件（LM393）在文件里是
    # 三个 symbol 节点、共用一个位号，只按位号建 dict 会把它们压成一个，门就瞎了
    # ——2026-10-27 组合甲实测：40 个符号节点被压成 38，门照样打勾。
    def idx(il):
        out = {}
        for s in il['symbols']:
            out[(s['id'], s.get('unit', 1))] = s
        return out

    sa, sb = idx(a), idx(b)
    rec('符号', sorted((k, sa[k].get('lib') or '', tuple(round(v, 6) for v in sa[k]['at'][:3]))
                       for k in sa),
        sorted((k, sb[k].get('lib') or '', tuple(round(v, 6) for v in sb[k]['at'][:3]))
               for k in sb))
    pa = sorted((k, n, round(p[0], 3), round(p[1], 3))
                for k in sa for n, p in (sa[k].get('pins_at') or {}).items())
    pb = sorted((k, n, round(p[0], 3), round(p[1], 3))
                for k in sb for n, p in (sb[k].get('pins_at') or {}).items())
    rec('引脚', pa, pb, tol)
    rec('导线', sorted(tuple(tuple(round(c, 6) for c in q) for q in w['pts']) for w in a['wires']),
        sorted(tuple(tuple(round(c, 6) for c in q) for q in w['pts']) for w in b['wires']))
    rec('标签', sorted((l['text'], round(l['at'][0], 6), round(l['at'][1], 6)) for l in a['labels']),
        sorted((l['text'], round(l['at'][0], 6), round(l['at'][1], 6)) for l in b['labels']))
    for name, key in (('节点', 'junctions'), ('空脚', 'no_connects')):
        rec(name, sorted((round(p[0], 6), round(p[1], 6)) for p in a[key]),
            sorted((round(p[0], 6), round(p[1], 6)) for p in b[key]))
    na = sorted(tuple(sorted((p[0], str(p[1])) for p in n['pins'])) for n in extract.build_nets(a))
    nb = sorted(tuple(sorted((p[0], str(p[1])) for p in n['pins'])) for n in extract.build_nets(b))
    rec('连通分区', na, nb)
    return out


def _eq_t(u, v, tol):
    if isinstance(u, (int, float)) and isinstance(v, (int, float)):
        return abs(u - v) <= tol
    if isinstance(u, (tuple, list)) and isinstance(v, (tuple, list)):
        return len(u) == len(v) and all(_eq_t(x, y, tol) for x, y in zip(u, v))
    return u == v


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='把拼装计划写成 KiCad .kicad_sch')
    ap.add_argument('plan')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    build(a.plan, a.out)
