#!/usr/bin/env python3
"""外科改图：把"这块要改成什么电路"写进语言，由引擎算几何。

为什么需要它（2026-10-27 组合甲）：平移路线的铁律是**块内一个坐标都不改**，
可人画的块里常常带着不属于这块板的东西。最典型的是仿真台架源——sallen-key
低通那块里有两个 SPICE 源：`V1` 直流源给运放的 V+ 供电、`V2` 脉冲源直接戳在
输入网上。原样平移它们，板子上就多出一个不存在的电源和一个不存在的激励源。

所以语言要能说"这里要改"，但改动必须**声明、计数、可复现**：只写"删哪几个件、
哪个脚改接到哪根网"，不写坐标；几何由引擎算，每一步打进报告。这样"块内一模
一样"就从一条无条件铁律变成一条**可核对的等式**：手术后 == 原样 + 平移。

三条纪律：

① 只认 器件位号 / 引脚号 / 网名，**不收坐标**（和 lint-1 同一把尺子）；
② 删件之后**剪掉悬空的线**——人画的线是接到那个件上的，件没了它就是一截天线；
③ 改接一个脚 = 在那个脚的端点上放一个网标（`label`），文字就是网名。
   只加这一个标签，原有图形一个字节不动。

用法（由 `kicadblocks` / `schwrite` 调用，语言里写在 `surgery:` 下）：

    surgery:
      lpf:
        drop: [V1, V2, "#PWR02"]
        supply: {U1.5: VBUS_5V}

`expose:` 是同一套机制的第二种用途（2026-10-27 四路 RS485）：**把块里一个原本空着、
在图上打了"不接"叉的脚，提成一个新端口**。裁出来的 MCU 块只有原页用过的那些脚是端口，
可这块板还要用它另外几路串口——那是"这块板要用这个脚的什么功能"，属于电路，不属于画法，
所以照样写进 `surgery:`，照样只写位号+脚号+网名。几何上就是在脚端点上放一个网标，
外加把那个"不接"叉删掉（留着它 KiCad 会报"既接了东西又标了不接"）。

    surgery:
      mcu:
        expose: {U1.12: USART2_TX, U1.13: USART2_RX}
"""
from __future__ import annotations

import math

EPS = 1e-6
GRID = 1.27                                 # KiCad 连接网格：桩长必须落在格上
OPS = {'drop', 'supply', 'expose', 'why'}   # 语言里允许出现的操作（why 只是给人读的）


def _k(p):
    return (round(float(p[0]), 6), round(float(p[1]), 6))


def label_style(il):
    """照抄这块图**自己的**标签习惯：字号 + 对齐。

    新贴的网标必须和旁边的老标签长得一样，否则一眼就看得出是"外人加的"
    （2026-10-27 用户："添加的标号很丑"——原图标签是 0.6096mm，我们按 1.27 写，
    整整大了一倍）。图上没有标签时退回 KiCad 默认值。
    """
    seen = {}
    for l in il['labels']:
        key = (float(l.get('size') or 1.27), tuple(l.get('justify') or ('left', 'bottom')))
        seen[key] = seen.get(key, 0) + 1
    if not seen:
        return 1.27, ('left', 'bottom')
    return max(seen, key=seen.get)


def pin_side(sym, pin):
    """这个脚长在符号的哪一边：`left` / `right` / `top` / `bottom`。

    按**放置后**的脚坐标判（不读库里的引脚角度），所以旋转、镜像过的符号一样管用：
    取该符号所有脚的包围盒，看这个脚离哪条边最近（按边长归一，避免长条形符号误判）。
    一条边都判不出来（单脚符号）就退成 right。
    """
    pts = {k: (float(v[0]), float(v[1])) for k, v in (sym.get('pins_at') or {}).items()}
    if pin not in pts or len(pts) < 2:
        return 'right'
    xs = [p[0] for p in pts.values()]
    ys = [p[1] for p in pts.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    px, py = pts[pin]
    w, h = maxx - minx, maxy - miny
    if w <= 0:                                   # 竖着的两脚件：只有上下
        return 'top' if (py - miny) <= (maxy - py) else 'bottom'
    if h <= 0:                                   # 横着的两脚件：只有左右
        return 'left' if (px - minx) <= (maxx - px) else 'right'
    score = {'left': (px - minx) / w, 'right': (maxx - px) / w,
             'top': (py - miny) / h, 'bottom': (maxy - py) / h}
    return min(score, key=score.get)


def label_anchor(sym, pin, text, size=1.27, stub=7.62):
    """给一个脚算"网标贴哪儿 + 文字朝哪"。

    规则（人画页上就是这样画的，2026-10-27 两次被指出来，属基本规则）：
    **文字永远跟引脚同向、水平/竖直摆，不许竖着压一排。** 引脚在左右两侧就是横排字，
    在上下两侧就是竖排字；文字一律从**桩的外端**往外读，所以：
    - 右/上：桩老长（默认 7.62mm），文字从桩端继续往外长；
    - 左/下：文字是从桩端**往回**朝脚的方向长的，桩必须够长，否则字会压到芯片本体上。

    返回 `(桩终点, 文字角度)`；桩 = 从脚端点到这个终点的一段短线。
    """
    side = pin_side(sym, pin)
    px, py = _k(sym['pins_at'][pin])
    ang = 90.0 if side in ('top', 'bottom') else 0.0
    if side in ('right', 'top'):
        length = stub
    else:
        # 字宽估：KiCad 笔画字体一个字大约一个字高宽。再留一格空——文字从桩端往回长，
        # 留出这一格，字才不会顶到引脚尖上（贴太紧看起来像和引脚叠在一起）。
        need = size * len(text) + GRID
        length = max(GRID * 2, GRID * math.ceil(need / GRID))
    dx, dy = {'right': (1, 0), 'left': (-1, 0), 'top': (0, -1), 'bottom': (0, 1)}[side]
    return side, (round(px + dx * length, 2), round(py + dy * length, 2)), ang


def _on_seg(p, a, b, eps=EPS):
    """点是否落在线段上（含两端；T 形接头的中间点也算）。"""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    if abs(dx * (p[1] - ay) - dy * (p[0] - ax)) > eps * max(1.0, abs(dx) + abs(dy)):
        return False
    return (min(ax, bx) - eps <= p[0] <= max(ax, bx) + eps
            and min(ay, by) - eps <= p[1] <= max(ay, by) + eps)


def _fixed_points(il):
    """删件之后还"抓着东西"的位置：引脚、标签、空脚标记。

    结点（junction）不算——结点本身不是连接对象，它只是告诉 KiCad"这里交叉要连"；
    一条线光靠一个孤零零的结点吊着，那还是天线。
    """
    pts = set()
    for s in il['symbols']:
        for p in (s.get('pins_at') or {}).values():
            pts.add(_k(p))
    for l in il['labels']:
        pts.add(_k(l['at']))
    for p in il.get('no_connects') or []:
        pts.add(_k(p))
    return pts


def prune_wires(il, verbose=False):
    """剪掉悬空的线：**两端都得抓着东西**（引脚/标签/其它线的端点或线身）才留。

    反复扫到不动点为止——剪掉一根线会让接在它头上的另一根也悬空，
    组合甲实测要连剪四轮（V1/V2 的短头 → 顶部横线 → 运放 V+ 的引上线）。
    """
    wires = il['wires']
    keep = [True] * len(wires)
    fixed = _fixed_points(il)

    def attached(p, i):
        if _k(p) in fixed:
            return True
        for j, w in enumerate(wires):
            if j == i or not keep[j]:
                continue
            pts = w['pts']
            for t in range(len(pts) - 1):
                if _on_seg(p, pts[t], pts[t + 1]):
                    return True
        return False

    changed = True
    while changed:
        changed = False
        for i, w in enumerate(wires):
            if not keep[i]:
                continue
            if not attached(w['pts'][0], i) or not attached(w['pts'][-1], i):
                keep[i] = False
                changed = True
                if verbose:
                    print(f'    [surgery] 剪掉悬空线 {w["pts"][0]} → {w["pts"][-1]}')
    return keep


def apply(il, ops, verbose=False):
    """就地把一个块的 IL 改成"手术后的电路"。返回报告（含文件级操作，供 schwrite 用）。

    `file_ops` 里的下标都指向**手术前**的 IL 列表，`schwrite` 拿它去删 S 表达式节点；
    IL 本身在这里被就地改掉，所以两边必须先把下标取走再过滤。
    """
    ops = ops or {}
    drop = [str(r) for r in (ops.get('drop') or [])]
    supply = {str(k): str(v) for k, v in (ops.get('supply') or {}).items()}
    expose = {str(k): str(v) for k, v in (ops.get('expose') or {}).items()}
    log = {'why': ops.get('why') or '', 'drop': drop, 'supply': supply, 'expose': expose,
           'dropped': [], 'missing': [], 'wires_pruned': 0, 'labels_added': [],
           'exposed': [], 'no_connects_dropped': 0,
           'file_ops': {'drop_symbols': [], 'drop_wires': [], 'drop_junctions': [],
                        'drop_no_connects': [], 'add_labels': [], 'add_wires': []}}
    if not drop and not supply and not expose:
        return log

    # ── ① 删件：按位号找。多单元器件在同一文件里是三个 symbol 节点、同一个位号，
    #       按位号删就是整颗删（和 renumber 的"整颗算一件"对齐）。
    drop_set = set(drop)
    drop_idx = []
    seen = set()
    for i, s in enumerate(il['symbols']):
        if s.get('id') in drop_set:
            drop_idx.append(i)
            seen.add(s['id'])
    log['dropped'] = sorted(seen)
    log['missing'] = sorted(drop_set - seen)
    for r in log['missing']:
        print(f'    [surgery] 警告：块里没有位号 {r}，没删到东西')
    log['file_ops']['drop_symbols'] = drop_idx
    dropped_i = set(drop_idx)
    il['symbols'] = [s for i, s in enumerate(il['symbols']) if i not in dropped_i]

    # ── ② 改接脚 / 提端口：在脚端点上放一个网标。脚找不到就当场报，不许悄悄跳过。
    # `supply` 和 `expose` 几何上是同一件事（都是"在这个脚上贴个网标"），差别只在
    # 语义：supply 是把**已经接着东西**的脚改接到另一根网（元件删掉之后）；
    # expose 是**启用一个原本空着的脚**，那个网标同时就是这块的新端口。
    # 所以代码共用，记账分开——报告里要能看出这块板多用了哪几个脚。
    new_labels = []
    new_wires = []
    nc_hits = []                    # 新网标"落"在哪个脚端点上（要删那个"不接"叉）
    size, justify = label_style(il)
    for ref_pin, net, op in ([(k, v, 'supply') for k, v in supply.items()]
                             + [(k, v, 'expose') for k, v in expose.items()]):
        ref, _, pin = ref_pin.partition('.')
        if not pin:
            print(f'    [surgery] 错误："{ref_pin}" 不是「位号.引脚号」的形式，跳过')
            continue
        sym = next((s for s in il['symbols']
                    if s.get('id') == ref and not s.get('power')), None)
        if sym is None:
            print(f'    [surgery] 错误：块里没有器件 {ref}，跳过 {op} {ref_pin}')
            continue
        p = (sym.get('pins_at') or {}).get(str(pin))
        if p is None:
            print(f'    [surgery] 错误：{ref} 没有引脚 {pin}'
                  f'（有 {sorted(sym.get("pins_at") or {})}），跳过')
            continue
        # 标签**不贴在脚上**，贴在脚外向一小段"桩"的末端：文字跟引脚同向、横排/竖排，
        # 不压本体也不竖着叠成一列。桩是线，KiCad 按导线连通，电气上和贴在脚上等价。
        side, anchor, ang = label_anchor(sym, str(pin), net, size=size)
        if anchor != _k(p):
            new_wires.append({'pts': [[p[0], p[1]], [anchor[0], anchor[1]]],
                              'alias': None})            # alias 由拼装层补
        new_labels.append({'text': net, 'at': [anchor[0], anchor[1], ang], 'kind': 'local',
                           'shape': None, 'size': size, 'justify': list(justify)})
        rec = {'text': net, 'at': [p[0], p[1]], 'label_at': [anchor[0], anchor[1]],
               'side': side, 'pin': ref_pin}
        log['labels_added' if op == 'supply' else 'exposed'].append(rec)
        log['file_ops']['add_labels'].append({'text': net, 'at': anchor,
                                              'angle': ang, 'size': size,
                                              'justify': list(justify), 'pin_at': _k(p)})
        if anchor != _k(p):
            log['file_ops']['add_wires'].append({'pts': [[p[0], p[1]],
                                                         [anchor[0], anchor[1]]]})
        nc_hits.append(_k(p))

    # 先挂上去再剪线：网标本身就是"抓着东西"的位置之一。
    il['labels'] = list(il['labels']) + new_labels
    il['wires'] = list(il['wires']) + new_wires

    # 新网标如果落在原本打了"不接"叉的脚上，那个叉必须删掉——留着它 KiCad 会报
    # "这个脚既接着东西又标了不接"。这是 expose 的常规情形（空脚都是叉掉的）。
    # **按脚端点判，不按标签坐标判**：标签现在贴在桩的末端，早就不在脚上了。
    hit = set(nc_hits)
    ncs = il.get('no_connects') or []
    keep_nc = [_k(p) not in hit for p in ncs]
    log['file_ops']['drop_no_connects'] = [i for i, k in enumerate(keep_nc) if not k]
    log['no_connects_dropped'] = len(log['file_ops']['drop_no_connects'])
    il['no_connects'] = [p for i, p in enumerate(ncs) if keep_nc[i]]

    # ── ③ 剪悬空线 + 顺手清掉没线可挂的结点
    keep = prune_wires(il, verbose=verbose)
    log['file_ops']['drop_wires'] = [i for i, k in enumerate(keep) if not k]
    log['wires_pruned'] = len(log['file_ops']['drop_wires'])
    il['wires'] = [w for i, w in enumerate(il['wires']) if keep[i]]

    alive = [w for w in il['wires']]
    keepj = []
    for j in il.get('junctions') or []:
        ok = any(_on_seg(j, w['pts'][t], w['pts'][t + 1])
                 for w in alive for t in range(len(w['pts']) - 1))
        keepj.append(ok)
    log['file_ops']['drop_junctions'] = [i for i, k in enumerate(keepj) if not k]
    il['junctions'] = [j for i, j in enumerate(il.get('junctions') or []) if keepj[i]]

    # ── 网表重算：改完电路，旧网表就作废了（谁再用谁错）
    try:
        import extract                                          # noqa: PLC0415
        il['nets'] = extract.build_nets(il)
    except Exception:            # 复制回环/独立脚本里没挂 sys.path 时不影响大门
        pass
    return log
