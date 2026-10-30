#!/usr/bin/env python3
"""拼装层：语言（`.sdi.yaml`）→ 一整张 schematic（IL + SVG + 指标 + 门禁结论）。

这条链把"电路是什么"和"怎么画"彻底分开：

    语言（块 + 网，零坐标）
      → 块卡取图形（库里的 .kicad_sch，或 defs 就地生成）       ← 怎么画由块卡决定
      → 摆位（floorplan：按连通性最小化同网脚距）                ← 怎么画由几何规则决定
      → 跨块网的标签改成语言里的正名（块内网名只在块里有效）
      → wireup（电源符号 / 块间导线 / 标签外移成短线）
      → 出 SVG + 指标 + **门3 分区门**（画出来的网必须和语言说的一模一样）

门3 是硬门：分区不一致就直接失败（历史上导线共用顶点、同名标签把两根网并了，都是这么抓的）。
画法指标（拉线率等）只报告、不阻断——它是"像不像人画的"，不是"对不对"。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import floorplan
from . import spec as S
from .expand import expand
from .library import Library, SpecLibrary
from .spec import load as load_spec

ILTOOLS = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
ROOT = Path(__file__).resolve().parent.parent
BLOCKS = 'PCBStudio.kicad_blocks'


def _il():
    """schematic 侧工具（extract / wireup / render_full / style_metrics / geom）。"""
    import sys
    d = str(ILTOOLS)
    if d not in sys.path:
        sys.path.insert(0, d)
    import extract, geom, render_full, style_metrics, wireup     # noqa: PLC0415
    return extract, geom, render_full, style_metrics, wireup


def empty_il(paper='A3'):
    return {'schema': 'sdi.v2', 'page': {'paper': paper}, 'symbols': [], 'wires': [],
            'junctions': [], 'labels': [], 'no_connects': [], 'texts': [], 'libsym': {},
            'nets': [], 'meta': {}, 'constraints': {}}


# 就地定义的块要用到的符号图形（Device:R / power:GND …）从哪儿借：
# 先扫库里的块卡（它们自己带 lib_symbols），再扫几块人工板的页兜底。
DONOR_PAGES = ('STM32F103_USB/STM32F103_USB_v1.kicad_sch',
               'ESP32S3_TestBoard/ESP32S3_TestBoard_v3.kicad_sch',
               'USB2RS485/USB2RS485_v7.kicad_sch')
_POOL = None


def symbol_pool(root=ROOT):
    """{库名: 符号图形} —— 就地定义模块造图形时用，扫一遍缓存住。"""
    global _POOL
    if _POOL is not None:
        return _POOL
    extract, *_ = _il()
    from sexpr import parse                                  # noqa: PLC0415
    pool = {}
    pages = sorted((Path(root) / BLOCKS).glob('*/*.kicad_sch'))
    pages += [Path(root).parent / 'KiCAD_Projects' / p for p in DONOR_PAGES]
    for p in pages:
        try:
            root_ = parse(Path(p).read_text(encoding='utf-8', errors='replace'))
        except Exception:
            continue
        for name, g in extract._libsym_from_root(root_).items():
            pool.setdefault(name, g)
    _POOL = pool
    return pool


PAPERS = (('A4', 297, 210), ('A3', 420, 297), ('A2', 594, 420))
MARGIN = 15.0        # 图纸留白：框线内 5mm，再留出位号和电源符号的地方


def page_fit(il):
    """把整张图挪进图纸左上角，并选**装得下的最小纸**。

    摆位算的是"谁离谁近"，画出来的坐标是随便落到哪儿的；不归一化的话，
    内容会飘在 A3 的右上角一小块里，四周全是空白（第一版渲染就是这样）。
    """
    xs, ys = [], []
    for w in il['wires']:
        xs += [p[0] for p in w['pts']]
        ys += [p[1] for p in w['pts']]
    for s in il['symbols']:
        if s.get('at'):
            xs.append(s['at'][0])
            ys.append(s['at'][1])
        for q in (s.get('pins_at') or {}).values():
            xs.append(q[0])
            ys.append(q[1])
    for l in il['labels']:
        xs.append(l['at'][0])
        ys.append(l['at'][1])
    if not xs:
        return 'A4', (0.0, 0.0)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    paper = next((p for p, pw, ph in PAPERS
                  if w + 2 * MARGIN <= pw and h + 2 * MARGIN <= ph), 'A2')
    floorplan.transform(il, 0, (0.0, 0.0), MARGIN - x0, MARGIN - y0)
    il['page'] = {'paper': paper}
    return paper, (w, h)


# ── 跨块的网：块内标签换成语言的正名 ────────────────────────────────────────
def _roots(il, extract, geom):
    """复刻 build_nets 的连接判定，给每个脚/标签一个连通块号。"""
    node_pts = set()
    for s in il['symbols']:
        for p in extract.symbol_pins(il, s).values():
            node_pts.add(geom.key(p))
    for l in il['labels']:
        node_pts.add(geom.key((l['at'][0], l['at'][1])))
    for p in il['junctions']:
        if p:
            node_pts.add(geom.key(p))
    for p in il['no_connects']:
        if p:
            node_pts.add(geom.key(p))
    dsu = extract.connectivity(il['wires'], node_pts)
    out = {}
    for s in il['symbols']:
        for n, p in extract.symbol_pins(il, s).items():
            out[(s['id'], str(n))] = dsu.find(geom.key(p))
    for i, l in enumerate(il['labels']):
        out[('@lab', i)] = dsu.find(geom.key((l['at'][0], l['at'][1])))
    return out


def rename_cross_block(frag, alias, rmap, pin_net, extract, geom):
    """把一块里每个标签换成它所在那根网在语言里的名字。

    块卡是从单页抠出来的，标签写的是块内网名（`VIN`、`TXD`…）。整页拼起来后，
    两个块各自的 `TXD` 会**因为同名而并成一根网**——这是拼装层最隐蔽的错。
    所以每块落位前先按"这组脚在语言里属于哪根网"改名。

    返回 (改动列表, 冲突列表)。冲突 = 同一个连通块里的脚分属语言的两根网，说明块卡
    自己的几何把两根网画在一起了（块卡坏了，不是拼装的问题）。
    """
    r = _roots(frag, extract, geom)
    by_root = {}
    for (a, b), root in r.items():
        if a == '@lab':
            continue
        nm = pin_net.get((rmap.get(a, a), b))
        if nm:
            by_root.setdefault(root, set()).add(nm)
    changed, clash = [], []
    for i, l in enumerate(frag['labels']):
        names = by_root.get(r[('@lab', i)]) or set()
        if len(names) > 1:
            clash.append((l['text'], sorted(names)))
            continue
        if len(names) == 1:
            new = next(iter(names))
            if new != l['text']:
                changed.append((l['text'], new))
                l['text'] = new
    # 电源符号也是"网名"，同样得按语言改。漏掉它，块卡里的 `+5V` 会和语言里的
    # `VBUS5V` 各算一根网——画出来两条网都通电，语言里却是一根（门3 直接抓这个）。
    for s in frag['symbols']:
        if not s.get('power'):
            continue
        cur = extract._power_name(s)
        if not cur:                       # PWR_FLAG 之类不命名网的符号
            continue
        names = by_root.get(r.get((s['id'], '1'))) or set()
        if len(names) != 1:
            continue
        new = next(iter(names))
        if new != cur:
            f = s.setdefault('fields', {}).setdefault('Value', {})
            changed.append((cur, new))
            f['text'] = new
    return changed, clash


# ── 主流程 ────────────────────────────────────────────────────────────────
def assemble(spec_path, out_dir, root=ROOT, paper='A3', place=True, verbose=True):
    extract, geom, render_full, style_metrics, wireup = _il()
    from . import deffrag

    sp = load_spec(str(spec_path))
    # `library:`（外部人画 .kicad_sch 登记的第三种块来源）必须一起传进去，
    # 否则 SVG 这条腿看不见它们，会掉到块卡目录里去找、当场 FileNotFoundError。
    # `schwrite` 那条腿一直是对的——两条腿要看见同一个库。
    lib = SpecLibrary(Library(root), sp.get('defs') or {}, sp.get('library') or {},
                      surgery=sp.get('surgery') or {}, modules=sp.get('modules') or {})
    nl = expand(sp, lib)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pin_net = {(k.split('.')[1], k.split('.')[2]): nm
               for nm, pins in nl.nets.items() for k in pins}

    frags = {}
    for alias, bname in sp['modules'].items():
        rmap = nl.refmap[alias]
        sch = Path(root) / BLOCKS / f'{bname}.kicad_block' / f'{bname}.kicad_sch'
        if not sch.is_file() and bname in (sp.get('library') or {}):
            # `library:` 登记的**外部人画页**：SVG 这条腿原来只认块卡目录，
            # 于是同一条语言在 `sdi blocks` 出得来、在 `sdi schematic` 出不来。
            from .library import _resolve                          # noqa: PLC0415
            sch = _resolve(Path(root), S.lib_path(sp['library'][bname]))
        if sch.is_file():
            il = extract.extract(str(sch))
        elif bname in (sp.get('defs') or {}):
            d = sp['defs'][bname]
            need = {p['lib'] for p in d['parts']} | {'power:GND'}
            pool = symbol_pool(root)
            libsym = {k: json.loads(json.dumps(pool[k])) for k in need if k in pool}
            miss = need - set(libsym)
            if miss:
                raise SystemExit(f'{bname}: 借不到符号图形 {sorted(miss)}')
            il = deffrag.build(bname, d, pin_net, libsym, geom, rmap=rmap)
            il['libsym'] = libsym
            extract.refresh_pins(il)
            if verbose:
                print(f'  {alias:6s} {bname:16s} 就地定义 → 生成片段（{len(il["symbols"])} 器件）')
        else:
            raise SystemExit(f'{alias}: {bname} 既没有 .kicad_sch，也不在 defs 里')
        # 外科改图这条腿也必须做——以前这里完全没做，只是碰巧所有用了 `surgery:` 的板
        # 都走 KiCad 那条腿（`sdi blocks`）。不做的话 SVG 画的是**手术前**的电路，
        # 和语言说的不是一回事，而且门3 会拿它去比语言、把责任推给拼装层。
        # 顺序和 `kicadblocks` 一致：先手术、后改号（`drop` 记的是块内位号）。
        ops = (sp.get('surgery') or {}).get(alias) or {}
        if ops:
            from . import surgery as SG                              # noqa: PLC0415
            slog = SG.apply(il, ops)
            if verbose and (slog['dropped'] or slog['labels_added']):
                print(f'  [surgery] {alias}: 删 {slog["dropped"]}'
                      f' / 提端口 {[x["pin"] + "→" + x["text"] for x in slog["exposed"]]}')
        # 位号当场换成板级位号再摆位：块卡里叫 U1 的件，扩出来可能叫 U3。
        # 以前留到合并时才换，于是 `floorplan.prep` 拿块内位号去对语言的网表，
        # 对不上就把**整根网当成单块内部网丢掉**——摆位完全看不见它，拉线率当然上不去。
        for s in il['symbols']:
            if not s.get('power'):
                s['id'] = rmap.get(s['id'], s['id'])
                ref = (s.get('fields') or {}).get('Reference')
                if ref:
                    ref['text'] = s['id']
        frags[alias] = il
        if verbose:
            print(f'  {alias:6s} {bname:16s} 器件 {len(il["symbols"]):3d}'
                  f' / 线 {len(il["wires"]):3d} / 标签 {len(il["labels"]):3d}')

    # ── 摆位 ──
    alias_of = {s['id']: a for a, il in frags.items() for s in il['symbols']}
    if place:
        src = [{'name': nm, 'pins': [(k.split('.')[1], k.split('.')[2]) for k in pins]}
               for nm, pins in nl.nets.items()]
        geo, netlist = floorplan.prep(frags, src, alias_of)
        state, (cpl, area) = floorplan.solve(geo, netlist)
        if verbose:
            print(f'  摆位：代价 {cpl:.0f} / 外框 {area:.0f}mm²')
        for alias, (tx, ty, rot) in state.items():
            floorplan.transform(frags[alias], rot, geo[alias]['center'], tx, ty)
            if verbose:
                print(f'         {alias:6s} rot {rot:3d} at ({tx:7.1f},{ty:7.1f})')

    # ── 合并 ──
    merged = empty_il(paper)
    for alias, il in frags.items():
        # 位号在摆位前就换成了板级位号，所以这里 rmap 用恒等
        ch, cl = rename_cross_block(il, alias, {}, pin_net, extract, geom)
        if verbose and cl:
            print(f'  [warn] {alias}: 块卡自己把语言的两根网画在一起了 {cl}')
        for s in il['symbols']:
            s['alias'] = alias
        for l in il['labels']:
            l['alias'] = alias
        for k in ('symbols', 'wires', 'junctions', 'labels', 'no_connects'):
            merged[k] += il[k]
        merged['texts'] += il.get('texts') or []
        merged['libsym'].update(il['libsym'])

    # 电源符号的位号 #PWR01 每块都从 1 起编，整页拼起来必然重号（KiCad 里是标注错误）。
    # 电源符号不参与网表，改号不影响任何东西，只是在合并后统一重编一遍。
    seen, n = set(), 0
    for s in merged['symbols']:
        if s.get('power'):
            n += 1
            s['id'] = f'#PWR{n:02d}'
            f = (s.get('fields') or {}).get('Reference')
            if f:
                f['text'] = s['id']
        elif s['id'] in seen:
            raise SystemExit(f'位号重号：{s["id"]}（块卡之间撞名了，先修块卡）')
        else:
            seen.add(s['id'])

    # ── 接线 ──
    wireup.wire_layout(merged)
    # 摆位是连续的，画出来落在哪块坐标只有几何知道；这里把整张图挪进图纸并选最小的纸。
    paper, size = page_fit(merged)
    extract.refresh(merged)
    metrics = style_metrics.metrics(merged)
    wired, total, per_net = style_metrics.wired_state(merged)

    # ── 门3：画出来的网必须和语言说的一模一样 ──
    # `nl.nets` 里的端点已经是**板级位号**（expand 阶段换过号），所以这里直接取
    # `别名.位号.脚` 的第 2、3 段去和画出来的图比。
    # 比的是**分组本身**，不比网名：块卡板里语言知道名字、图上却常常没写字，名字对不齐
    # 是常态；真正要守的是"哪几个脚是一伙的"。
    lang = {frozenset((k.split('.')[1], k.split('.')[2]) for k in pins)
            for nm, pins in nl.nets.items() if len(pins) >= 2}
    drawn_all = []
    for n in extract.build_nets(merged):
        if len(n['pins']) < 2:
            continue
        drawn_all.append((n.get('name'),
                          frozenset((r, str(p)) for r, p in n['pins'])))
    drawn = {g for _, g in drawn_all}
    # 语言没描述、图上却**跨块**连起来的匿名网 = 该有的名字丢了（真错）。
    anon_bad = sorted(sorted(g) for nm, g in drawn_all
                      if not nm and len({alias_of.get(r) for r, _ in g}) > 1)
    gate_ok = lang == drawn and not anon_bad

    json.dump(merged, (out_dir / 'sch.il.json').open('w', encoding='utf-8'),
              ensure_ascii=False)
    render_full.render(merged, str(out_dir / 'sch.svg'))
    report = {
        'spec': str(spec_path), 'root': str(root),
        'modules': len(sp['modules']), 'nets': len(nl.nets),
        'symbols': metrics['symbols'], 'wires': metrics['wires'], 'labels': metrics['labels'],
        'metrics': metrics, 'wired_pins': wired, 'multipin_pins': total,
        'per_net': [{'net': n, 'pins': p, 'wired': g} for n, p, g in per_net],
        'gate3_partition': {'ok': gate_ok, 'lang': len(lang), 'drawn': len(drawn),
                            'missing': sorted([sorted(x) for x in lang - drawn])[:20],
                            'extra': sorted([sorted(x) for x in drawn - lang])[:20],
                            'anon_cross_block': anon_bad[:20]},
    }
    (out_dir / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if verbose:
        print(f'  指标 {json.dumps(metrics, ensure_ascii=False)}')
        print(f'  拉线 {wired}/{total} = {wired / max(1, total):.3f}')
        print(('✓' if gate_ok else '✗') + f' 门3 分区：语言 {len(lang)} 组 / '
              f'画出来 {len(drawn)} 组 / 一致 {len(lang & drawn)}')
        for g in report['gate3_partition']['missing'][:5]:
            print('    缺:', g[:8])
        print(f'→ {out_dir / "sch.svg"}')
    return report
