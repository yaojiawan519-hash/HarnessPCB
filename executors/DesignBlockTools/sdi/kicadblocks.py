#!/usr/bin/env python3
"""直接拿 KiCad design block（`.kicad_sch`）拼一张图，**块内只做平移**。

这个工具和 `draw.py` 是一条链上的两个极端：

    draw.py        ：块卡 + 语言 + 摆位 + 接线引擎（块内图形可能被后面的步骤动过）
    kicadblocks.py ：只挪 XY，其余一个字节都不改（摆位固定、不转、不画新线）

为什么要有这个极端：用户要看的判据是"局部的 schematic 应该和 design block 一模一样"。
那就得有个工具把这句话做成**可验证的门**——`identity_gate()` 逐图元比对
（符号/引脚/导线/标签/节点/字段），只有"平移后的坐标 = 原坐标 + (dx, dy)"才放行。

连接靠**网名**（电源符号 + 标签），不画跨块导线：一旦画线，线是新的图元，
"一模一样"就不再是逐图元的等式了。所以这里只做：

    取块（原样） → 按行排开（只加 dx/dy） → 端口标签按映射改名（只改文字，不动坐标）
    → 合并 → 渲染 → 门：逐图元 Δ=0 → 报告

用法：
    python3 -m sdi.kicadblocks plan.json --out out/kicad_blocks_demo
plan.json:
    {"blocks": [{"file": "a.kicad_sch", "rename": {"VIN": "VIN_5V"}}, ...],
     "gap": 20, "paper": "A3", "margin": 20}
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from . import surgery                 # 声明过的外科改图（删件/改接脚），见 surgery.py

ILTOOLS = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'


def _il():
    import importlib
    d = str(ILTOOLS)
    if d not in sys.path:
        sys.path.insert(0, d)
    mods = {n: importlib.import_module(n) for n in ('extract', 'geom', 'render_full', 'style_metrics')}
    return mods['extract'], mods['render_full'], mods['style_metrics']


# ── 平移：唯一的几何操作，且不做任何取整 ────────────────────────────────────
def translate(il, dx, dy):
    """整块 +（dx, dy）。**不 round**——源文件坐标是 1.27 栅格上的精确数，
    取整会把"一模一样"偷偷变成"差不多一样"。"""
    def m(p):
        return [p[0] + dx, p[1] + dy] + list(p[2:])

    for s in il['symbols']:
        s['at'] = m(s['at'])
        for f in (s.get('fields') or {}).values():
            if f.get('at'):
                f['at'] = m(f['at'])
        s['pins_at'] = {k: m(v) for k, v in (s.get('pins_at') or {}).items()}
    for w in il['wires']:
        w['pts'] = [m(p) for p in w['pts']]
    il['junctions'] = [m(p) for p in il['junctions']]
    for l in il['labels']:
        l['at'] = m(l['at'])
    il['no_connects'] = [m(p) for p in il['no_connects']]
    for t in il.get('texts') or []:
        if t.get('at'):
            t['at'] = m(t['at'])
    return il


def extent(il):
    """图形范围：引脚 + 导线 + 标签 + 节点（符号本体有 libsym 尺寸，这里不管）。"""
    xs, ys = [], []
    for s in il['symbols']:
        # 符号自己的原点也要算：有的符号原点落在引脚包围盒外面（图形不对称），
        # 只按引脚算 box，合并后按 box 取回就会漏掉它——门会报"图元个数不一致"。
        xs.append(s['at'][0]); ys.append(s['at'][1])
        for q in (s.get('pins_at') or {}).values():
            xs.append(q[0]); ys.append(q[1])
        for f in (s.get('fields') or {}).values():
            if f.get('at'):
                xs.append(f['at'][0]); ys.append(f['at'][1])
    for w in il['wires']:
        for p in w['pts']:
            xs.append(p[0]); ys.append(p[1])
    for l in il['labels']:
        xs.append(l['at'][0]); ys.append(l['at'][1])
    for j in il['junctions']:
        xs.append(j[0]); ys.append(j[1])
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


# ── 位号：跨块撞名才改号，改的只是文字 ──────────────────────────────────────
def renumber(frags, refmap=None):
    """同一页上位号必须唯一；同前缀的按出现顺序往后排。返回改动列表。

    `refmap`（2026-10-27 四路 RS485 加的）：**语言给的那份编号**（`expand` 算的
    `nl.refmap`，展开层、design.json 用的就是它）。给了就按它改，不再自己猜。
    以前这里自己猜——"旧号没人用就留着"——于是同一页里 ldo 的 U4 原样保留、
    MCU 的 U1 也保留，中间空出 U2；而展开层是"按模块声明序从头数"，两边差一位，
    整个网表在位号上对不上（网的分区明明一模一样，就是名字全错开）。
    两个产物只能有一个编号权威，那个权威是语言。

    **多单元器件必须整颗算一件**：LM393 的三个单元在文件里是三个 `symbol` 节点、
    共用一个位号。按节点编号会把一颗芯片拆成 U2/U3/U4 三颗——KiCad 会报
    "input power pins in units […] are not placed"，BOM 和 PCB 也会多出两颗；
    引脚模型里同一位号下不同单元的脚号本来就不冲突（1,2,3 / 5,6,7 / 4,8）。
    所以这里按"每个 (块, 旧位号) 分配一个新位号"，块内同旧位号的单元一起改。
    """
    used, changed = {}, []
    for alias, il in frags.items():
        seen = {}                    # 本块 旧位号 -> 新位号（多单元共用同一个）
        given = (refmap or {}).get(alias) or {}
        for s in il['symbols']:
            if s.get('power'):
                continue
            ref = seen.get(s['id'])
            if ref is None and s['id'] in given:
                ref = given[s['id']]
                seen[s['id']] = ref
                if ref != s['id']:
                    changed.append((alias, s['id'], ref))
            elif ref is None:
                old = s['id']
                prefix = ''.join(ch for ch in old if ch.isalpha())
                num = ''.join(ch for ch in old if ch.isdigit())
                key = (prefix, num)
                if key in used:
                    n = max([int(k[1]) for k in used if k[0] == prefix] or [0]) + 1
                    ref = f'{prefix}{n}'
                    used[(prefix, str(n))] = alias
                else:
                    ref = old
                    used[key] = alias
                seen[old] = ref
                if ref != old:
                    changed.append((alias, old, ref))
            s['id'] = ref
            f = (s.get('fields') or {}).get('Reference')
            if f:
                f['text'] = ref
    n = 0
    for il in frags.values():
        for s in il['symbols']:
            if s.get('power'):
                n += 1
                s['id'] = f'#PWR{n:02d}'
                f = (s.get('fields') or {}).get('Reference')
                if f:
                    f['text'] = s['id']
    return changed


def rename_labels(frag, mapping):
    """端口标签改名：只改 text，坐标不动。返回 (旧, 新) 列表。"""
    done = []
    for l in frag['labels']:
        new = mapping.get(l['text'])
        if new and new != l['text']:
            done.append((l['text'], new))
            l['text'] = new
    return done


# ── 门：逐图元比"平移后 = 原样 + (dx,dy)" ───────────────────────────────────
def _in_box(pt, box, eps=1e-6):
    return box[0] - eps <= pt[0] <= box[2] + eps and box[1] - eps <= pt[1] <= box[3] + eps


def _geom(il, box=None):
    """把图形抽成可比较的元组表（位号/文字都带上，坐标保留全精度）。

    `box` 给定时只取落在这个矩形里的图元——整页拼好后按"这块自己的地盘"取回来，
    这样导线/标签/节点不必带 alias 也能分清是谁的（块之间留了柜距，不会串）。
    """
    def keep(pts):
        return box is None or any(_in_box(p, box) for p in pts)

    out = []
    for s in il['symbols']:
        if not keep([s['at']]):
            continue
        tag = s.get('id')
        out.append(('sym', tag, tuple(s['at']), (s.get('lib') or '')))
        for k, v in sorted((s.get('pins_at') or {}).items()):
            out.append(('pin', tag, k, tuple(v)))
        for k, f in sorted((s.get('fields') or {}).items()):
            if f.get('at'):
                out.append(('field', tag, k, tuple(f['at']), f.get('text') or ''))
    for w in il['wires']:
        if not keep([tuple(p[:2]) for p in w['pts']]):
            continue
        out.append(('wire', tuple(tuple(p) for p in w['pts'])))
    for l in il['labels']:
        if not keep([l['at']]):
            continue
        out.append(('label', tuple(l['at']), l['text']))
    for j in il['junctions']:
        if not keep([j]):
            continue
        out.append(('junction', tuple(j)))
    return out


def _eq(a, b, tol=1e-6):
    """逐分量比较：数字带容差（1e-6mm = 1nm，远低于 KiCad 分辨率），其余严格相等。"""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_eq(x, y, tol) for x, y in zip(a, b))
    return a == b


def identity_gate(before, after, offset):
    """before/after 都是 _geom() 表；判据：after == before + offset。

    平移本身是精确的（只加 dx/dy），但 `extract.refresh()` 会按 libsym 重算引脚，
    浮点尾数会差 1e-14 —— 那不是"画得不一样"，所以坐标按 1e-6mm 容差判。
    位号/标签文字是**声明过的改动**，不在这道门里：门只保图形。
    """
    dx, dy = offset

    def shifted(t):
        k = t[0]
        if k == 'sym':
            return ('sym', t[1], (t[2][0] + dx, t[2][1] + dy) + tuple(t[2][2:]), t[3])
        if k == 'pin':
            return ('pin', t[1], t[2], (t[3][0] + dx, t[3][1] + dy) + tuple(t[3][2:]))
        if k == 'field':
            return ('field', t[1], t[2], (t[3][0] + dx, t[3][1] + dy) + tuple(t[3][2:]), t[4])
        if k == 'wire':
            return ('wire', tuple((p[0] + dx, p[1] + dy) + tuple(p[2:]) for p in t[1]))
        if k == 'label':
            return ('label', (t[1][0] + dx, t[1][1] + dy) + tuple(t[1][2:]), t[2])
        if k == 'junction':
            return ('junction', (t[1][0] + dx, t[1][1] + dy) + tuple(t[1][2:]))
        return t

    bad, n = [], 0
    if len(before) != len(after):
        return {'primitives': len(before), 'mismatch': abs(len(before) - len(after)),
                'count_before': len(before), 'count_after': len(after),
                'examples': [{'why': '图元个数不一致'}]}
    for a, b in zip(before, after):
        n += 1
        if not _eq(shifted(a), b):
            bad.append({'kind': a[0], 'before': shifted(a), 'after': b})
    return {'primitives': n, 'mismatch': len(bad), 'examples': bad[:5]}


# ── 主流程 ────────────────────────────────────────────────────────────────
PAPERS = (('A4', 297, 210), ('A3', 420, 297), ('A2', 594, 420),
          ('A1', 841, 594), ('A0', 1189, 841))


def pick_paper(w, h):
    """装得下的最小纸。纸和间距不写进语言——那是几何，引擎算。"""
    return next((n for n, pw, ph in PAPERS if w <= pw and h <= ph), 'A0')


def paper_size(name):
    return next(((pw, ph) for n, pw, ph in PAPERS if n == name), PAPERS[-1][1:])


def sheet_layout(sizes, keys, gap=20.0, margin=20.0, grid=1.27, paper=None):
    """块级摆位：**复制出来的块先竖着叠，剩下的横着铺**，整版居中。

    2026-10-27 用户定的基本规则（属"画法"不属"电路"），所以由引擎算，语言不写）：
    ① 同一个块被复制几份时，**先考虑垂直复制**——叠成一列，读图的人一眼看出"这是同一块"；
    ② 不同块之间再水平摆开；
    ③ 整页尽量铺满、别把七块挤在一条横线上下半张纸空着——按"装得下的最小纸"反推要分成几行。

    `sizes` = [(宽, 高)]，`keys` = 每个块的身份（源文件路径；同 key 就是同一块的复制）。
    返回 `(positions, paper, bbox)`：positions 是每块**左上角**的落点（已居中），
    bbox = (宽, 高) 内容尺寸。
    """
    n = len(sizes)
    if not n:
        return [], 'A4', (0.0, 0.0)

    # ── ① 复制 → 垂直：同 key 的块叠成一列（保持声明顺序）───────────────
    groups: dict = {}
    order: list = []
    for i, k in enumerate(keys):
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(i)
    stacks = []                              # (列宽, 列高, [块下标])
    for k in order:
        idxs = groups[k]
        stacks.append((max(sizes[i][0] for i in idxs),
                       sum(sizes[i][1] for i in idxs) + gap * (len(idxs) - 1), idxs))

    def pack(nrows):
        """把列分成 nrows 行：按"每行宽度 ≈ 总宽 / 行数"连续切。"""
        total = sum(s[0] for s in stacks) + gap * (len(stacks) - 1)
        target = total / nrows
        rows, cur, cur_w = [], [], 0.0
        for s in stacks:
            if cur and len(rows) < nrows - 1 and cur_w + s[0] > target:
                rows.append(cur)
                cur, cur_w = [], 0.0
            cur.append(s)
            cur_w += s[0] + gap
        if cur:
            rows.append(cur)
        if len(rows) != nrows:
            return None
        w = max(sum(s[0] for s in r) + gap * (len(r) - 1) for r in rows)
        h = sum(max(s[1] for s in r) for r in rows) + gap * (len(rows) - 1)
        return rows, w, h

    # ── ② 行数按"装得下的最小纸"反推：纸面积最小者胜，同面积取长宽比最像横版的 ──
    area = {n_: pw * ph for n_, pw, ph in PAPERS}
    best = None
    for nrows in range(1, len(stacks) + 1):
        got = pack(nrows)
        if not got:
            continue
        rows, w, h = got
        name = pick_paper(w + 2 * margin, h + 2 * margin)
        key = (area[name], abs((w / h if h else 99) - 1414 / 1000))
        if best is None or key < best[0]:
            best = (key, rows, w, h, name)
    if best is None:                         # 只有一列、pack(1) 也不成立（不该发生）
        best = ((0, 0), [stacks], max(s[0] for s in stacks),
                sum(s[1] for s in stacks) + gap * (len(stacks) - 1), 'A0')
    _, rows, w, h, name = best

    # ── ③ 落点：行内列顶对齐、行间留 gap；整版在纸上居中 ─────────────────
    if paper:
        name = paper
    pw, ph = paper_size(name)
    ox = max(margin, (pw - w) / 2.0)
    oy = max(margin, (ph - h) / 2.0)
    pos: list = [None] * n
    y = oy
    for row in rows:
        band = max(s[1] for s in row)
        x = ox
        for (sw, sh, idxs) in row:
            yy = y + (band - sh) / 2.0        # 列在行带里竖直居中，高度不齐也不会顶在上沿
            for i in idxs:
                pos[i] = (grid * round(x / grid), grid * round(yy / grid))
                yy += sizes[i][1] + gap
            x += sw + gap
        y += band + gap
    return pos, name, (w, h)


def load_plan(path):
    """计划可以有两种写法：`.sdi.yaml`（语言，人写）或 `.json`（拼板清单，机器写）。

    语言那条路由 `langblocks` 翻译：`nets:` 说明谁和谁同网，这里只算出"哪个标签改名"。
    """
    p = Path(path)
    if p.suffix in ('.yaml', '.yml'):
        from .langblocks import load                                  # noqa: PLC0415
        return load(p)[0]
    return json.loads(p.read_text(encoding='utf-8'))


def assemble(plan_path, out_dir, verbose=True):
    extract, render_full, style_metrics = _il()
    plan = load_plan(plan_path)
    gap = float(plan.get('gap', 20))
    margin = float(plan.get('margin', 20))
    GRID = 1.27                              # KiCad 默认连接网格（50 mil）
    paper = plan.get('paper')                # 没写就按内容自动选最小纸

    frags, cursors, reports = {}, {}, []
    prepared = []
    for i, b in enumerate(plan['blocks']):
        p = Path(b['file'])
        il = extract.extract(str(p))
        # 外科改图先做：删掉不属于这块板的件、把某个脚改接到某根网。
        # **顺序很重要**——门1 的"原样"取的是手术**之后**的样子，
        # 否则门会拿手术前的图去比手术后的图，把声明过的改动算成不一致。
        surgery_log = {}
        if b.get('surgery'):
            surgery_log = surgery.apply(il, b['surgery'])
            if verbose and (surgery_log['dropped'] or surgery_log['labels_added']):
                print(f'  外科 {b.get("alias")}: 删 {surgery_log["dropped"]}'
                      f' / 剪悬空线 {surgery_log["wires_pruned"]} 根'
                      f' / 改接 {[x["pin"] + "→" + x["text"] for x in surgery_log["labels_added"]]}')
        alias = b.get('alias') or f'b{i}'
        src = copy.deepcopy(il)                       # 门1用的"原样"（手术后、平移前）
        x0, y0, x1, y1 = extent(il)
        prepared.append({'i': i, 'b': b, 'p': p, 'il': il, 'src': src, 'alias': alias,
                         'box0': (x0, y0, x1, y1), 'renamed': None,
                         'surgery': surgery_log})

    # 摆位（摆位是"怎么画"，引擎算）：复制的块先竖着叠，其余的横着铺，整版居中。
    # `dx/dy` 对到 1.27mm 网格——`schwrite` 那边调用的是同一个函数，两个产物必须一模一样。
    pos, paper_auto, content = sheet_layout(
        [(q['box0'][2] - q['box0'][0], q['box0'][3] - q['box0'][1]) for q in prepared],
        [str(q['b']['file']) for q in prepared], gap=gap, margin=margin, grid=GRID,
        paper=paper)
    if not paper:
        paper = paper_auto
        if verbose:
            print(f'  选纸 {paper}（内容 {content[0]:.0f}×{content[1]:.0f}mm）')

    for q, (tx, ty) in zip(prepared, pos):
        il, src, alias = q['il'], q['src'], q['alias']
        x0, y0, x1, y1 = q['box0']
        dx = GRID * round((tx - x0) / GRID)           # 只算平移量
        dy = GRID * round((ty - y0) / GRID)
        translate(il, dx, dy)
        placed = copy.deepcopy(il)                    # 门1：平移完、还没改名改号
        renamed = rename_labels(il, q['b'].get('rename') or {})
        for s in il['symbols']:
            s['alias'] = alias
        for w in il['wires']:
            w['alias'] = alias
        for l in il['labels']:
            l['alias'] = alias
        frags[alias] = il
        cursors[alias] = {'dx': dx, 'dy': dy, 'src': src, 'placed': placed,
                          'box': (x0 + dx, y0 + dy, x1 + dx, y1 + dy)}
        reports.append({'alias': alias, 'file': str(q['p']), 'dx': dx, 'dy': dy,
                        'w': round(x1 - x0, 2), 'h': round(y1 - y0, 2),
                        'symbols': len([s for s in il['symbols'] if not s.get('power')]),
                        'wires': len(il['wires']), 'labels': len(il['labels']),
                        'renamed': renamed, 'surgery': q['surgery']})
        if verbose:
            print(f'  {alias:10s} {q["p"].name[:44]:46s} 器件 {reports[-1]["symbols"]:2d}'
                  f' / 线 {reports[-1]["wires"]:2d} / 标签 {reports[-1]["labels"]:2d}'
                  f'  平移 ({dx:7.2f},{dy:7.2f})')

    changed_refs = renumber(frags, plan.get('refmap'))
    expected = {a: copy.deepcopy(il) for a, il in frags.items()}   # 门2：改完名/号的样子

    merged ={'schema': 'sdi.v2', 'page': {'paper': paper}, 'symbols': [], 'wires': [],
              'junctions': [], 'labels': [], 'no_connects': [], 'texts': [], 'libsym': {},
              'nets': [], 'meta': {}, 'constraints': {}}
    for alias, il in frags.items():
        for k in ('symbols', 'wires', 'junctions', 'labels', 'no_connects'):
            merged[k] += il[k]
        merged['texts'] += il.get('texts') or []
        merged['libsym'].update(il['libsym'])

    # 不调 extract.refresh()：它会按 libsym 重算引脚并**取整到 2 位小数**，
    # 而这份语料的坐标是 3 位（61.656 → 61.66）。取整就是改图，门会当场抓住。
    # 片段的 pins_at 在提取时就已算好、平移时原样搬过，直接拿去建网即可。
    merged['nets'] = extract.build_nets(merged)
    gates = {}
    for alias, c in cursors.items():
        g1 = identity_gate(_geom(c['src']), _geom(c['placed']), (c['dx'], c['dy']))
        g2 = identity_gate(_geom(expected[alias]), _geom(merged, c['box']), (0.0, 0.0))
        gates[alias] = {'translate': g1, 'post_merge': g2,
                        'mismatch': g1['mismatch'] + g2['mismatch'],
                        'primitives': g1['primitives']}

    nets = [{'net': n['name'], 'pins': n['pins']} for n in extract.build_nets(merged)]
    metrics = style_metrics.metrics(merged)
    wired, total, _ = style_metrics.wired_state(merged)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    render_full.render(merged, str(out_dir / 'sch.svg'))
    json.dump(merged, (out_dir / 'sch.il.json').open('w', encoding='utf-8'), ensure_ascii=False)
    report = {'plan': str(plan_path), 'blocks': reports, 'renumbered': changed_refs,
              'identity': gates, 'nets': nets, 'metrics': metrics,
              'wired': wired, 'pins': total}
    (out_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                         encoding='utf-8')
    if verbose:
        ok = all(g['mismatch'] == 0 for g in gates.values())
        print(f'  位号改动 {len(changed_refs)} 处')
        print(f'  门·逐图元一致：{sum(g["primitives"] for g in gates.values())} 个图元，'
              f'不一致 {sum(g["mismatch"] for g in gates.values())} 个  ' + ('✓' if ok else '✗'))
        print(f'  网 {len(nets)} 根 / 拉线 {wired}/{total} = {wired / max(1, total):.3f}')
        print(f'→ {out_dir / "sch.svg"}')
    return report


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='KiCad design block 原样平移拼装')
    ap.add_argument('plan')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    assemble(a.plan, a.out)
