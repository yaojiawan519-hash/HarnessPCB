#!/usr/bin/env python3
"""块级摆位（schematic floorplan）：只认"同网的脚离得近"，不认任何人工坐标。

为什么语言层不带坐标、非要到这里才决定位置：**摆位是"怎么画"，不是"是什么"**。
但摆位又不是随便排——引擎只会给**够近**的两脚之间画导线（`wireup.MAXLEN`），
8 个块横平竖直一摆，同网的脚动辄 80~100mm，拉线率就永远上不去。
所以摆位必须由连通性算出来：这是把"电路是什么"翻译成"怎么画"的第一道几何。

做法：带硬约束（块不许重叠）的模拟退火，状态 = 每个块的 (tx, ty, rot)。
- 转角只取 0/90/180/270（符号 rot 的合成律见 `transform`）；
- 代价 = Σ(每个网里引脚最小生成树的曼哈顿长，**超 25mm 的段三倍重罚**)
        + 重叠重罚 + 外框面积小罚；
- 电源网（GND/+3V3/…）权重压到 0.2：它们靠电源符号画，不许把整张图拧成一团。
"""
from __future__ import annotations

import math
import os
import random

GRID = 1.27          # KiCad 原理图栅格；块心与平移都落在这上面，转完角的脚还在格上
MAXLEN = float(os.environ.get('WIREUP_MAXLEN') or 35.0)   # 与 wireup 同一道门槛（同一个 env）
POWERISH = {'GND', '+3V3', '+5V', 'VRAW+', 'VBUS5V', '+5V_USB', 'VCC', 'VRAW-'}


def _rot(p, a, c=(0.0, 0.0)):
    """存盘坐标（y 向下）里绕 c 转 a 度。

    与 `geom.pins_at` 的转角合成律配套：**页面转 +a ⇔ 符号 rot 减 a**
    （`transform` 里就是这么写的；写成加 a，转出来的脚会左右翻）。
    """
    if a % 360 == 0:
        return (p[0], p[1])
    dx, dy = p[0] - c[0], p[1] - c[1]
    r = math.radians(a)
    co, si = math.cos(r), math.sin(r)
    return (c[0] + dx * co - dy * si, c[1] + dx * si + dy * co)


def _upright(r):
    """文字朝向只留 0/90，别让位号和网名倒着写。"""
    r %= 360
    return r - 180 if r >= 180 else r


def transform(il, rot, center, tx, ty):
    """把一个块刚性搬到 (tx, ty)：页面转 rot 度、绕 center。"""
    cx, cy = center

    def m(p):
        q = _rot((p[0], p[1]), rot, (cx, cy))
        return [round(q[0] + tx, 2), round(q[1] + ty, 2)]

    for s in il['symbols']:
        at = m(s['at'])
        s['at'] = at + [(s['at'][2] - rot) % 360]
        if 'rot' in s:
            s['rot'] = s['at'][2]
        for f in (s.get('fields') or {}).values():
            if f.get('at'):
                r0 = f['at'][2] if len(f['at']) > 2 else 0.0
                f['at'] = m(f['at']) + [_upright(r0 - rot)]
        s['pins_at'] = {k: m(v) for k, v in (s.get('pins_at') or {}).items()}
    for w in il['wires']:
        w['pts'] = [m(p) for p in w['pts']]
    il['junctions'] = [m(p) for p in il['junctions']]
    for l in il['labels']:
        r0 = l['at'][2] if len(l['at']) > 2 else 0.0
        l['at'] = m(l['at']) + [_upright(r0 - rot)]
    il['no_connects'] = [m(p) for p in il['no_connects']]
    for t in il.get('texts') or []:
        if t.get('at'):
            t['at'] = m(t['at'])
    return il


def prep(frags, nets, alias_of):
    """每个块折成 {rot: (引脚相对坐标, 相对包围盒, 导线段)}。块心取引脚包围盒中心并落到栅格上。

    导线段也要带上：块卡自带的那几根线是刚性的（平移块就得原样搬走），
    摆位时如果让它们扫过别人的引脚，**画出来就已经并网了**——这跟块重叠是同一级别的硬错。
    """
    geo = {}
    for a, il in frags.items():
        pts = [tuple(q) for s in il['symbols'] for q in (s.get('pins_at') or {}).values()]
        cx = round((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2 / GRID) * GRID
        cy = round((min(p[1] for p in pts) + max(p[1] for p in pts)) / 2 / GRID) * GRID
        per = {}
        for rot in (0, 90, 180, 270):
            rel = {}
            for s in il['symbols']:
                for n, q in (s.get('pins_at') or {}).items():
                    rel[(s['id'], str(n))] = _rot((q[0], q[1]), rot, (cx, cy))
            xs = [v[0] for v in rel.values()]
            ys = [v[1] for v in rel.values()]
            segs = []
            for w in il['wires']:
                q = w['pts']
                for i in range(len(q) - 1):
                    p0 = _rot((q[i][0], q[i][1]), rot, (cx, cy))
                    p1 = _rot((q[i + 1][0], q[i + 1][1]), rot, (cx, cy))
                    segs.append((p0[0], p0[1], p1[0], p1[1]))
            labs = [_rot((l['at'][0], l['at'][1]), rot, (cx, cy))
                    for l in il['labels'] if l.get('at')]
            per[rot] = (rel, (min(xs), min(ys), max(xs), max(ys)), segs, labs)
        geo[a] = {'center': (cx, cy), 'by_rot': per}
    netlist = []
    for n in nets:
        ps = [(alias_of[r], r, str(p)) for r, p in n['pins'] if r in alias_of]
        if len({x[0] for x in ps}) < 2:          # 块内网跟摆位无关，纯属常数项
            continue
        w = 0.2 if (not n['name'] or n['name'] in POWERISH) else 1.0
        netlist.append((w, [(a, (r, p)) for a, r, p in ps]))
    return geo, netlist


PEN = 5.0        # 超长段每 mm 的额外代价
BONUS = 300.0    # 超长段每段的固定代价（"这根网根本拉不出线"必须比"总长难看"更疼）
# 外框形状：人画出来的是一张接近纸面的长方形，不是一条细长带。
# 只按面积罚是罚不住的——8 个块排成 330×90 的横带，面积和 200×160 差不多，
# 但前者在 A3 上只占三成高度，下面一大片空白。所以单独罚"偏离 1.414:1"。
ASPECT = float(os.environ.get('SDI_ASPECT') or 1.414)
ASPECT_W = float(os.environ.get('SDI_ASPECT_W') or 0.05)
AREA_W = float(os.environ.get('SDI_AREA_W') or 0.02)
ROT_W = float(os.environ.get('SDI_ROT_W') or 40.0)   # 每块转过 90° 的小罚


def _mst(pts, pen=None, bonus=None):
    """网内引脚生成树的（代价, 超长边数）。

    只算总长不够：把两个脚摆到 40mm 和 30mm 的总长差不多，但它俩之间**一根线都拉不出来**
    （`wireup.MAXLEN`）。所以超过 MAXLEN 的边既按长度加罚、又加一笔固定罚。
    """
    if len(pts) < 2:
        return 0.0, 0
    pen = PEN if pen is None else pen
    bonus = BONUS if bonus is None else bonus
    rest = list(pts[1:])
    tree = [pts[0]]
    tot = 0.0
    nlong = 0
    while rest:
        best, bd, braw = None, 1e18, 0.0
        for p in rest:
            for q in tree:
                d = abs(p[0] - q[0]) + abs(p[1] - q[1])
                sc = d + (pen * (d - MAXLEN) + bonus if d > MAXLEN else 0.0)
                if sc < bd:
                    bd, best, braw = sc, p, d
        tot += bd
        nlong += braw > MAXLEN
        rest.remove(best)
        tree.append(best)
    return tot, nlong


def _mst_len(pts):
    return _mst(pts)[0]


def _score(state, geo, netlist):
    """(超长边数, 代价) —— 摆位好不好，第一看有几条边根本拉不出线，第二才看总长。"""
    pos = {}
    for a, (tx, ty, rot) in state.items():
        for k, v in geo[a]['by_rot'][rot][0].items():
            pos[k] = (tx + v[0], ty + v[1])
    nlong = 0
    tot = 0.0
    for w, ps in netlist:
        c, n = _mst([pos[k] for _, k in ps if k in pos])
        tot += w * c
        nlong += n if w >= 1.0 else 0
    return (nlong, round(tot, 3))


def _boxes(state, geo):
    out = {}
    for a, (tx, ty, rot) in state.items():
        b = geo[a]['by_rot'][rot][1]
        out[a] = (tx + b[0], ty + b[1], tx + b[2], ty + b[3])
    return out


def _overlap(state, geo, a, gap):
    bx = _boxes(state, geo)
    A = bx[a]
    for b, B in bx.items():
        if b == a:
            continue
        dx = min(A[2], B[2]) - max(A[0], B[0])
        dy = min(A[3], B[3]) - max(A[1], B[1])
        if dx > -gap and dy > -gap:
            return True
    return False


def _on_seg(p, a, b, eps=0.02):
    """点是否落在（含端点的）线段上——与 `geom.on_seg` 同一套判据。"""
    (px, py), (ax, ay), (bx, by) = p, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > eps * max(1.0, abs(bx - ax) + abs(by - ay)):
        return False
    return (min(ax, bx) - eps <= px <= max(ax, bx) + eps and
            min(ay, by) - eps <= py <= max(ay, by) + eps)


def _abs_of(state, geo, a):
    """块 a 在 state 里的绝对几何：引脚点、标签点、导线段。"""
    tx, ty, rot = state[a]
    rel, _, segs, labs0 = geo[a]['by_rot'][rot]
    pins = [(x + tx, y + ty) for x, y in rel.values()]
    labs = [(x + tx, y + ty) for x, y in labs0]
    segs = [((x1 + tx, y1 + ty), (x2 + tx, y2 + ty)) for x1, y1, x2, y2 in segs]
    return pins, labs, segs


def crossing(state, geo, only=None):
    """不同块的可连接几何互相顶上了吗（导线压到别人的脚/标签，或两个标签/脚同点）。

    任何一处都会把语言里的两根网并成一根，所以和"块重叠"同级，是硬约束。
    """
    out = []
    for a in ([only] if only else list(geo)):
        ap, al, asg = _abs_of(state, geo, a)
        for b in geo:
            if b == a:
                continue
            bp, bl, bsg = _abs_of(state, geo, b)
            for seg in asg:
                for q, kind in [(q, 'pin') for q in bp] + [(q, 'label') for q in bl]:
                    if _on_seg(q, *seg):
                        out.append((a, b, f'{a} 的导线压住 {b} 的{kind} {q}'))
            for seg in bsg:
                for q in ap:
                    if _on_seg(q, *seg):
                        out.append((b, a, f'{b} 的导线压住 {a} 的pin {q}'))
            for q in ap:
                for kind, r in (('pin', bp), ('label', bl)):
                    if any(abs(q[0] - z[0]) < 0.02 and abs(q[1] - z[1]) < 0.02 for z in r):
                        out.append((a, b, f'{a} 的脚 {q} 和 {b} 的{kind}同点'))
    return out


def cost(state, geo, netlist):
    pos = {}
    for a, (tx, ty, rot) in state.items():
        for k, v in geo[a]['by_rot'][rot][0].items():
            pos[k] = (tx + v[0], ty + v[1])
    tot = 0.0
    for w, ps in netlist:
        tot += w * _mst_len([pos[k] for _, k in ps if k in pos])
    boxes = list(_boxes(state, geo).values())
    ix0 = min(b[0] for b in boxes); ix1 = max(b[2] for b in boxes)
    iy0 = min(b[1] for b in boxes); iy1 = max(b[3] for b in boxes)
    w, h = ix1 - ix0, iy1 - iy0
    area = w * h
    # 走形量按 **面积加权**：`r/A + A/r - 2` 在正好 A:1 时为 0、只跟比例有关，
    # 乘面积后与面积项同量纲。**不能只写 |w − A·h|**——那个式子靠"把 h 撑大"就能满足，
    # 退火会把块摊开去凑比例，外框反而从 26000 涨到 44000（试过，面积一路变大）。
    r = w / max(h, 1e-6)
    dev = r / ASPECT + ASPECT / r - 2.0
    # 转角偏好：人很少把整块（尤其接插件、稳压器）转 90° 画——转过去位号和值就竖着了。
    # 但转过来能换来更短的连线时人是会转的，所以只记一笔小罚，不禁止。
    turned = sum(1 for _, _, rot in state.values() if rot % 360)
    return tot + AREA_W * area + ASPECT_W * area * dev + ROT_W * turned, area


def solve(geo, netlist, seed=7, iters=40000, gap=6.0, restarts=8):
    """模拟退火跑几轮，取**超长边最少**的那一版。

    单轮退火在这类刚性大块上落差极大（同一套参数换种子，代价能差 40%），
    与其调参数不如多起几轮——摆位没有"唯一正解"，多试几次挑好的就是最便宜的办法。
    """
    best = None
    for k in range(restarts):
        st = _anneal(geo, netlist, seed + 1013 * k, iters, gap)
        out = {a: (round(tx / GRID) * GRID, round(ty / GRID) * GRID, rot)
               for a, (tx, ty, rot) in st.items()}
        try:
            _separate(out, geo, gap)
            _repair(out, geo, gap)
        except RuntimeError:
            continue
        sc = _score(out, geo, netlist)
        if best is None or sc < best[0]:
            best = (sc, out)
    if best is None:
        raise RuntimeError('所有摆位尝试都不合法（块太多，页面塞不下）')
    return best[1], cost(best[1], geo, netlist)


def _anneal(geo, netlist, seed, iters, gap):
    rnd = random.Random(seed)
    aliases = list(geo)
    # 每个块"该往谁身边靠"：(自己的脚, 对方块, 对方的脚)。
    # 纯随机平移在一个塞满页面的布局里 82% 都被重叠挡回来，退火等于原地打转；
    # 按同网配对直接往对方身边走一步，收敛快得多。
    pull = {a: [] for a in geo}
    for _, ps in netlist:
        for ai, ki in ps:
            for bi, ki2 in ps:
                if ai != bi:
                    pull[ai].append((ki, bi, ki2))
    state, x = {}, 0.0
    for a in aliases:
        b = geo[a]['by_rot'][0][1]
        state[a] = (x - b[0], 0.0, 0)
        x += (b[2] - b[0]) + 20
    cur = cost(state, geo, netlist)[0]
    best, best_state = cur, dict(state)
    T0, T1 = 20.0, 0.15
    for it in range(iters):
        T = T0 * (T1 / T0) ** (it / iters)
        st = dict(state)
        r = rnd.random()
        a = rnd.choice(aliases)
        b = None
        tx, ty, rot = st[a]
        if r < 0.45 and pull.get(a):
            # 朝"同网的另一只脚"走一步（最多 15mm）。步长同样落栅格：
            # 搜的时候和交出去的摆法是**同一个离散格**，否则收尾取整会把
            # "刚好留 6mm 缝"的摆法推成重叠，`_separate` 一推就把代价打回原形。
            ka, bi, kb = rnd.choice(pull[a])
            pa = geo[a]['by_rot'][rot][0].get(ka)
            q = geo[bi]['by_rot'][st[bi][2]][0].get(kb)
            if pa is None or q is None:
                continue
            dx = (st[bi][0] + q[0]) - (tx + pa[0])
            dy = (st[bi][1] + q[1]) - (ty + pa[1])
            n = max(abs(dx), abs(dy), 1e-9)
            s = min(n, 15.0) / n
            st[a] = (tx + round(dx * s / GRID) * GRID,
                     ty + round(dy * s / GRID) * GRID, rot)
        elif r < 0.65:
            st[a] = (tx + round(rnd.gauss(0, 10) / GRID) * GRID,
                     ty + round(rnd.gauss(0, 10) / GRID) * GRID, rot)
        elif r < 0.85:
            st[a] = (tx, ty, (rot + 90 * rnd.choice((1, 3))) % 360)
        else:
            b = rnd.choice(aliases)
            if b == a:
                continue
            st[a], st[b] = st[b], st[a]
        # 换位动的是两个块，两个都得查（以前只查 a，换位换出来的重叠能一直活到收尾）
        if _overlap(st, geo, a, gap) or (b and _overlap(st, geo, b, gap)):
            continue
        c = cost(st, geo, netlist)[0]
        if c < cur or rnd.random() < math.exp(-(c - cur) / max(T, 1e-6)):
            state, cur = st, c
            # `crossing` 只在"要记成最优解"时算一次：它比代价贵得多，
            # 而并网与否只取决于**最后交出去的那个摆法**，中间走点坏路没关系。
            if (c < best and not crossing(st, geo, only=a)
                    and not (b and crossing(st, geo, only=b))):
                best, best_state = c, dict(st)
    return best_state


def _separate(state, geo, gap):
    """落栅格后若还挤在一起，沿 x 推开（栅格取整最多挪半格，缝留了 gap 一般不撞）。

    不写 `assert`：块卡尺寸一变（比如就地定义的块换了封装），断言就会在拼装时炸掉，
    而"推开 1 格"永远有解。推完仍然是合法摆位，只是代价略差一点。
    """
    for _ in range(400):
        boxes = _boxes(state, geo)
        order = list(boxes)
        moved = False
        for i, a in enumerate(order):
            for b in order[i + 1:]:
                A, B = boxes[a], boxes[b]
                dx = min(A[2], B[2]) - max(A[0], B[0])
                dy = min(A[3], B[3]) - max(A[1], B[1])
                if dx > -gap and dy > -gap:
                    # 只推"后面的"块，两个块才不会互相推着来回弹
                    need = math.ceil((dx + gap) / GRID) * GRID
                    tx, ty, rot = state[b]
                    state[b] = (tx + need, ty, rot)
                    moved = True
                    break
            if moved:
                break
        if not moved:
            return state
    raise RuntimeError('块摆位推不开（块太多或 gap 太大）')


def _repair(state, geo, gap, reach=10):
    """落格/推开之后若还有"导线压别人的脚"，把涉事块往四个方向挪几格。

    退火里只在"记最优解"时查了这个约束，块推开的 `_separate` 又动了坐标，
    所以最后再兜一道：要么挪干净，要么报错——**绝不带着并网出厂**。
    """
    for _ in range(60):
        bad = crossing(state, geo)
        if not bad:
            return state
        moved = False
        for name in sorted({x for a, b, _ in bad for x in (a, b)}):
            tx, ty, rot = state[name]
            for step in range(1, reach + 1):
                for dx, dy in ((step * GRID, 0), (-step * GRID, 0),
                               (0, step * GRID), (0, -step * GRID)):
                    old = state[name]
                    state[name] = (tx + dx, ty + dy, rot)
                    if _overlap(state, geo, name, gap) or crossing(state, geo, only=name):
                        state[name] = old
                        continue
                    moved = True
                    break
                if moved:
                    break
            if moved:
                break
        if not moved:
            raise RuntimeError('摆位挪不开：' + '；'.join(d for _, _, d in bad[:3]))
    raise RuntimeError('摆位挪不开：' + '；'.join(d for _, _, d in crossing(state, geo)[:3]))
