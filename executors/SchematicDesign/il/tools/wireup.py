#!/usr/bin/env python3
"""试验：把"标签连着"的画法改成"符号 + 导线"的画法。

两条 pass：
  A 电源口：GND / 电源网的标签 -> 电源符号 + 短引线
  B 走线：同一网上距离够近、路径干净的引脚对 -> 正交导线（L 型），撤掉该引脚的标签

几何挡路的一律不动，只报告原因。这是编排层要做的决策，这里先用确定性规则打样。
"""
import sys, json, math
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import extract, index, render_full
from sexpr import H, find, one

R = lambda v: round(v, 2)
STUB = 3.81          # 电源符号引线长度
# 信号走线能拉多长。**35mm 是按人工语料定的，不是拍的**：
# 语料 11 张页 1714 条同网引脚边（MST）里 79% ≤ 35mm、82% ≤ 40mm；
# 单段导线长度的 p97 是 36.8mm。早先的 25mm 把人工 1/3 的正常连线判成"太远"，
# 逼着它们改挂标签——拉线率卡在 0.25 上不去，根子就在这。
MAXLEN = float(_os.environ.get('WIREUP_MAXLEN') or 35.0)
MAXGAP = 15.24       # 电源轨相邻引脚最大间距（不许跨页拉长线）
POWER = {'GND', '+3V3', 'VBUS5V'}
RAIL_UP = {'+3V3', 'VBUS5V', '+5V', 'VCC'}
# 收尾钩子：接线画完后、落盘前给调用方一次改 IL 的机会（拼装器用它统一摆文字）
POST = None
# 画法流派：'wire' 实线派（导线 + 电源符号）/ 'label' 标签派（一根线不画，全靠同名标签）。
# 由调用方按目标页人工画法定（assemble.school_of），默认实线派。
SCHOOL = 'wire'
# 折线路由：只试"直 + 两个 L"时，块内一有件挡着就无路可走（STM32F103 在 MAXLEN=200
# 下 22 个失败里有 15 个是 blocked，不是太远）。人是随手拐两下的，所以再补 Z 形候选。
ZROUTE = _os.environ.get('WIREUP_ZROUTE', '1') != '0'
GRID = 1.27


# ---------- 工具 ----------
def _route_paths(a, b):
    """两脚之间试走的折线候选：直 → L×2 → Z×若干（只拐 1~3 下，够人画的样子了）。"""
    if abs(a[0] - b[0]) < 0.02 or abs(a[1] - b[1]) < 0.02:
        yield [a, b]
        return
    yield [a, (b[0], a[1]), b]
    yield [a, (a[0], b[1]), b]
    if not ZROUTE:
        return
    seen = set()
    for f in (0.5, 1 / 3, 2 / 3):
        x = R(round((a[0] + (b[0] - a[0]) * f) / GRID) * GRID)
        if (x, 'v') not in seen:
            seen.add((x, 'v'))
            yield [a, (x, a[1]), (x, b[1]), b]
        y = R(round((a[1] + (b[1] - a[1]) * f) / GRID) * GRID)
        if (y, 'h') not in seen:
            seen.add((y, 'h'))
            yield [a, (a[0], y), (b[0], y), b]


# ---------- 工具 ----------
def balanced_end(text, i):
    """text[i] == '(' -> 返回配对 ')' 之后的位置。"""
    depth = 0
    j = i
    in_str = False
    while j < len(text):
        c = text[j]
        if in_str:
            if c == '\\':
                j += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError('unbalanced')


def splice_libs(dst_path, src_path, names, out_path):
    """把 src 的 lib_symbols 里指定符号原文塞进 dst 的 lib_symbols。"""
    dst = open(dst_path, encoding='utf-8').read()
    src = open(src_path, encoding='utf-8').read()
    i = src.index('(lib_symbols')
    end = balanced_end(src, i)
    blocks = []
    j = i + len('(lib_symbols')
    while j < end:
        k = src.find('(symbol "', j)
        if k == -1 or k > end:
            break
        e = balanced_end(src, k)
        head = src[k:e]
        m = head.split('"')[1]
        if m in names:
            blocks.append(head)
        j = e
    ins = dst.index('(lib_symbols')
    ins = balanced_end(dst, ins) - 1
    merged = dst[:ins] + '\n' + '\n'.join(blocks) + '\n' + dst[ins:]
    open(out_path, 'w', encoding='utf-8').write(merged)


def bbox(sym, prims):
    xs, ys = [], []
    for p in prims:
        h = H(p)
        pts = []
        if h == 'rectangle':
            for t in ('start', 'end'):
                n = one(p, t)
                if n:
                    pts.append((float(n[1]), float(n[2])))
        elif h in ('polyline',):
            pts += [(float(x[1]), float(x[2])) for x in find(one(p, 'pts') or [], 'xy')]
        elif h == 'pin':
            a = one(p, 'at')
            ln = one(p, 'length')
            if a:
                L = float(ln[1]) if ln else 2.54
                x, y = float(a[1]), float(a[2])
                ang = math.radians(float(a[3]) if len(a) > 3 else 0)
                pts += [(x, y), (x + L * math.cos(ang), y + L * math.sin(ang))]
        elif h == 'circle':
            c = one(p, 'center')
            if c:
                pts.append((float(c[1]), float(c[2])))
        for x, y in pts:
            w = render_full.tf(x, y, sym['at'][0], sym['at'][1], sym['at'][2] if len(sym['at']) > 2 else 0, sym.get('mirror'))
            xs.append(w[0])
            ys.append(w[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def body_center(il):
    """器件本体（不含引脚）的中心，**页面坐标**。拼装器和切块器共用同一套口径。

    别拿库里的 `libsym[...]['pins']` 直接减符号原点：库符号 y 轴向上、页面 y 轴向下，
    混着用会把"上面那根脚"算成"下面那根脚"。这里统一走 `bbox(真 symbol, 图形笔)`，
    转角和镜像都由 `render_full.tf` 处理。符号原点常在本体左上角（Antmicro 风格），
    拿原点当"器件在哪"会把方位算歪一整格。
    """
    out = {}
    for s in il['symbols']:
        if s.get('power'):
            continue
        lib = (il.get('libsym') or {}).get(s['lib']) or {}
        prims = [pr for pr in (lib.get('prims') or []) if H(pr) != 'pin']
        bb = bbox(s, prims) or bbox(s, lib.get('prims') or [])
        out[s['id']] = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2) if bb \
            else (s['at'][0], s['at'][1])
    return out


def seg_hits_rect(a, b, rect, pad=0.8):
    x1, y1, x2, y2 = rect[0] + pad, rect[1] + pad, rect[2] - pad, rect[3] - pad
    if x1 >= x2 or y1 >= y2:
        return False
    # 轴对齐线段 vs 矩形
    ax, ay = a
    bx, by = b
    if abs(ax - bx) < 1e-6:  # 竖直
        if not (x1 < ax < x2):
            return False
        return not (max(ay, by) <= y1 or min(ay, by) >= y2)
    if abs(ay - by) < 1e-6:  # 水平
        if not (y1 < ay < y2):
            return False
        return not (max(ax, bx) <= x1 or min(ax, bx) >= x2)
    return False


def pt_on_seg(p, a, b, eps=0.02):
    return index.on_seg(p, a, b) and index.dist(
        p, a) + index.dist(p, b) - index.dist(a, b) < 0.05


def _merges(pth, net, drawn):
    """这条折线会不会把别的网并进来：共用顶点 / 顶点落在别人线段的中间。

    只是**穿过**别人的线不算（KiCad 不画 junction 就不连），所以只查顶点。
    """
    for s0, s1, n2 in drawn:
        if n2 == net:
            continue
        for q in pth:
            if pt_on_seg(q, s0, s1):
                return True
        for q in (s0, s1):
            for k in range(len(pth) - 1):
                if pt_on_seg(q, pth[k], pth[k + 1]):
                    return True
    return False


CH = 0.95          # 单个字符的横向占位（mm，1.5 号字）


def label_box(tip, rot, text):
    """标签文字实际占的矩形（近似）：从 tip 起沿文字方向铺开，高 1.8mm。"""
    n = max(1, len(text)) * CH
    x, y = tip
    r = round(rot or 0) % 360
    if r == 0:
        return (x, y - 0.9, x + n, y + 0.9)
    if r == 180:
        return (x - n, y - 0.9, x, y + 0.9)
    if r == 90:
        return (x - 0.9, y - n, x + 0.9, y)
    return (x - 0.9, y, x + 0.9, y + n)


STROKE = 0.15      # 图形线宽的一半（mm）：细到零面积的折线也得挡得住线


def body_hit(a, b, rects, grow=STROKE):
    """线段是否压到器件**图形**。rects 是逐个图形原语的矩形（不是整体包围盒）。

    整体包围盒会把"引脚长在图形里面"的符号（LED 的发光箭头伸到引脚左边、
    USBLC6 的散点）判成"往哪边都被自己挡住"——引脚其实在盒子里，
    四个方向全废。逐原语判就不会：短线只跟真正画出来的那几笔比。
    每个原语的矩形先长出 grow（折线是零面积矩形，不涨就永远判不到）。
    """
    return any(seg_hits_rect(a, b, r, pad=-grow) for r in rects)


def rects_hit(a, b, pad=0.0):
    return (a[0] < b[2] + pad and b[0] - pad < a[2]
            and a[1] < b[3] + pad and b[1] - pad < a[3])


def field_boxes(il):
    """所有器件的 Reference/Value 文字占位（标签不许压在上面）。"""
    out = {}
    for s in il['symbols']:
        m = s.get('fields') or {}
        user = not s['id'].startswith('#PWR')
        for k in ('Reference', 'Value'):
            f = m.get(k)
            if not f or not f.get('text'):
                continue
            if not user and k == 'Reference':
                continue
            x, y, r = (f.get('at') or [0, 0, 0] + [0])[:3]
            n = len(str(f['text'])) * CH
            w, h = (n / 2, 0.9) if round(r or 0) % 180 == 0 else (0.9, n / 2)
            out[id(f)] = (x - w, y - h, x + w, y + h)
    return list(out.values())


# ---------- 主流程 ----------
def _components(il, pin_pt):
    """按**导线几何**把引脚/标签/电源符号并成连通块，返回 {栅格键: 根}。

    和 `extract.build_nets` 同一套判据（端点相连 + 落在段中间），只是这里要的是
    "某个点属于哪个块"，好给引脚找回它的网名。
    """
    d = index.DSU()
    keys = set()
    for q in pin_pt.values():
        keys.add(index.key(q))
    for l in il['labels']:
        keys.add(index.key((l['at'][0], l['at'][1])))
    for s in il['symbols']:
        if s.get('power'):
            keys.add(index.key((s['at'][0], s['at'][1])))
    pts = {}
    for q in list(pin_pt.values()) + [(l['at'][0], l['at'][1]) for l in il['labels']] + \
            [(s['at'][0], s['at'][1]) for s in il['symbols'] if s.get('power')]:
        pts.setdefault(index.key(q), q)
    for w in il['wires']:
        p = w['pts']
        for i in range(len(p) - 1):
            a, b = tuple(p[i]), tuple(p[i + 1])
            d.union(index.key(a), index.key(b))
            for k in keys:
                if pt_on_seg(pts[k], a, b):
                    d.union(k, index.key(a))
    return {k: d.find(k) for k in keys}


def wire_layout(il, skip_nets=()):
    """实线派：画导线 + 电源符号（原来的画法）。

    il 必须是 extract 出的完整 IL（带 libsym）；本函数只读 IL，不碰原文件。"""
    # 电源符号的图形可能不在本页符号库里（子图只用全局标签），从人工页借一份
    extract.borrow_libsym(il, extra={f'power:{n}' for n in POWER})
    lp = extract.libpins(il)
    geoms = {k: v['prims'] for k, v in il.get('libsym', {}).items()}

    # 引脚世界坐标
    pins = {}          # ref -> {num: (x,y)}
    sym_by_ref = {}
    boxes = {}
    body_rects = {}    # ref -> [每个图形原语各自的矩形]（引脚不算）——判"压到自己图形"用
    for s in il['symbols']:
        r = s['id']
        sym_by_ref[r] = s
        pins[r] = extract.symbol_pins(il, s, cache=lp)
        prims = geoms.get(s['lib']) or []
        bb = bbox(s, prims)
        if bb:
            boxes[r] = bb
        rs_ = []
        for pr in prims:
            if H(pr) == 'pin':
                continue
            pb = bbox(s, [pr])
            if pb:
                rs_.append(pb)
        body_rects[r] = rs_

    label_at = {}
    for l in il['labels']:
        label_at[(R(l['at'][0]), R(l['at'][1]))] = l

    def drop_labels_at(k):
        """撤掉这个坐标上的**所有**标签（叠脚会有好几个挂在同一点），返回是否撤过。"""
        hit = [l for l in il['labels'] if (R(l['at'][0]), R(l['at'][1])) == k]
        for l in hit:
            il['labels'].remove(l)
        label_at.pop(k, None)
        return bool(hit)

    pin_pt = {}        # (ref,pin) -> (x,y)
    for r, pm in pins.items():
        for n, p in pm.items():
            pin_pt[(r, n)] = p

    # 引脚属于哪根网：**看它所在的电气连通块取名字，不能只看脚下有没有标签**。
    # 块卡只要自带导线（人类抽出来的块几乎都带：buck、power_entry 都是），引脚就是
    # 通过那根线才碰到名字标签的。只看脚下的话这些脚全是无名脚——Pass B 不知道它们同网，
    # 只把剩下的少数同名脚连起来，然后判定"整根网已连通"，把唯一的名字标签当冗余删掉，
    # 整根 +5V 就这么散成两半、还没有名字（门3 当场翻红）。
    geo_root = _components(il, pin_pt)
    names = {}
    for l in il['labels']:
        names.setdefault(geo_root.get(index.key((l['at'][0], l['at'][1]))), set()).add(l['text'])
    for s in il['symbols']:
        if s.get('power'):
            v = ((s.get('fields') or {}).get('Value') or {}).get('text')
            if v:
                names.setdefault(geo_root.get(index.key((s['at'][0], s['at'][1]))), set()).add(v)

    net_of = {}        # (ref,pin) -> net
    for (r, n), p in pin_pt.items():
        ns = names.get(geo_root.get(index.key(p))) or set()
        if len(ns) == 1:
            net_of[(r, n)] = next(iter(ns))
        else:
            # 脚下正好压着一个标签就还是听它的（连通块里挂了两个不同网名的，
            # 是块卡自己把两根网画在一起了，留给 draw 报冲突，这里不擅自改名）
            l = label_at.get((R(p[0]), R(p[1])))
            net_of[(r, n)] = l['text'] if l else None

    report = {'pass_a': [], 'pass_b': [], 'blocked': []}
    pwr_n = 0

    # ---------- 电源符号的图形长在引脚的哪一侧 ----------
    # 图形和引脚是连在一起的：符号摆在引脚哪边，图形就朝哪边伸。
    # 摆错侧（比如 VDD 的箭头朝上、却把符号摆到引脚下方）时，导线从引脚到符号
    # 那一小段必然穿过图形本身——不是"擦边"，是整段重叠。所以方向必须让图形背对导线。
    # 方向从库几何里量（power:VDD 箭头在上、power:GND 横杠在下、antmicropower 又各不同），
    # 不按库名硬编码。
    _gcache = {}

    def glyph_geom(lib):
        """(图形相对引脚的方向, 图形相对引脚的矩形)；量不出来返回 (None, None)。"""
        if lib in _gcache:
            return _gcache[lib]
        g = geoms.get(lib) or []
        pin = (lp.get(lib) or {}).get('1')
        out = (None, None)
        if g and pin:
            bb = bbox({'id': '', 'lib': lib, 'at': [0, 0, 0]}, g)
            if bb:
                dx = (bb[0] + bb[2]) / 2 - pin[0]
                dy = (bb[1] + bb[3]) / 2 - pin[1]
                d = (0, 1 if dy > 0 else -1) if abs(dy) >= abs(dx) else (1 if dx > 0 else -1, 0)
                out = (d, (bb[0] - pin[0], bb[1] - pin[1], bb[2] - pin[0], bb[3] - pin[1]))
        _gcache[lib] = out
        return out

    def order_dirs(dirs, lib):
        """图形那一侧优先；图形对面最后。"""
        d, _ = glyph_geom(lib)
        if not d:
            return dirs
        want = {1: (0, 1), -1: (0, -1), 2: (1, 0), -2: (-1, 0)}
        return sorted(dirs, key=lambda k: 0 if want[k] == d
                      else (2 if want[k] == (-d[0], -d[1]) else 1))

    def rot_vec(v, R):
        """把一个方向按符号旋转 R 转过去（和 render_full.tf 同一套约定，y 朝下）。"""
        u, w = v
        if R == 180:
            return (-u, -w)
        if R == 90:
            return (w, -u)
        if R == 270:
            return (-w, u)
        return (u, w)

    def rot_for(lib, d):
        """想让图形朝 d 伸出去，符号该转多少度。转不动返回 None。"""
        g, _ = glyph_geom(lib)
        if not g:
            return 0
        for R in (0, 180, 90, 270):
            if rot_vec(g, R) == d:
                return R
        return None

    def glyph_rect(lib, tip, R=0):
        """把图形矩形转到 R 再摆到 tip 上。"""
        _, r = glyph_geom(lib)
        if not r:
            return None
        pts = [rot_vec((r[0], r[1]), R), rot_vec((r[0], r[3]), R),
               rot_vec((r[2], r[1]), R), rot_vec((r[2], r[3]), R)]
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        return (min(xs) + tip[0], min(ys) + tip[1], max(xs) + tip[0], max(ys) + tip[1])

    pwr_glyphs = []          # 已落电源符号的图形矩形（配线时不许穿）

    def glyph_clear(lib, tip, R=0):
        """图形摆在这里会不会压到器件本体、别的电源符号图形、或已经画好的导线。"""
        gr = glyph_rect(lib, tip, R)
        if not gr:
            return True
        for bb in boxes.values():
            if rects_hit(gr, bb):
                return False
        for og in pwr_glyphs:
            if rects_hit(gr, og):
                return False
        for w in il['wires']:
            for i in range(len(w['pts']) - 1):
                if seg_hits_rect(w['pts'][i], w['pts'][i + 1], gr, pad=0.4):
                    return False
        return True

    def make_power(net, tip, R, pwr_n):
        """按方向摆一个电源符号：符号转到 R，让图形朝导线那头伸出去，文字挂在图形外侧。"""
        pref = f'#PWR{pwr_n:02d}'
        gr = glyph_rect(f'power:{net}', tip, R)
        vert = 'GND' if net == 'GND' else net
        # 不转的时候沿用老位置（STOP 复刻的 27 个电源符号都是这么摆的，别去动它）；
        # 转了才按图形实际落在哪边算文字位置。
        vat = [tip[0], tip[1] + (3.2 if net == 'GND' else -4.2), 0.0]
        if gr and R:
            cx, cy = (gr[0] + gr[2]) / 2, (gr[1] + gr[3]) / 2
            if abs(gr[3] - gr[1]) >= abs(gr[2] - gr[0]):
                # 图形竖着长：文字压在图形下面/上面
                vat = [cx, gr[3] + 1.8, 0.0] if cy >= tip[1] else [cx, gr[1] - 1.8, 0.0]
            else:
                vat = [gr[2] + len(vert) * CH / 2 + 0.5, cy, 0.0] if cx >= tip[0] \
                    else [gr[0] - len(vert) * CH / 2 - 0.5, cy, 0.0]
        return {
            'id': pref, 'lib': f'power:{net}', 'at': [tip[0], tip[1], float(R)],
            'rot': float(R),
            'fields': {'Reference': {'text': pref, 'at': [tip[0], tip[1], 0.0]},
                       'Value': {'text': vert, 'at': vat}},
            'pins': ['1'], 'pins_at': {'1': [tip[0], tip[1]]}, 'power': True,
        }

    def note_power(s):
        """电源符号一落上就登记成障碍物。

        早先只在 Pass A 整个跑完之后统一登记一次，于是同一条 Pass A 里
        **后面画的电源轨看不见前面落的符号**——dcdc 那条竖直轨就从 GND 横杠左边
        0.2mm 处穿了过去（gfx 口径 21 处 wire-sym 里绝大多数是这个）。
        """
        pr = geoms.get(s['lib']) or []
        bb_ = bbox(s, pr)
        if bb_:
            boxes[s['id']] = bb_
        body_rects[s['id']] = [bbox(s, [q]) for q in pr
                               if H(q) != 'pin' and bbox(s, [q])]

    def clear_path(a, b, skip_keys):
        """a->b 之间不能压到别的引脚、不能穿器件身体。"""
        for key2, q in pin_pt.items():
            if key2 in skip_keys:
                continue
            if index.dist(q, a) < 0.05 or index.dist(q, b) < 0.05 or pt_on_seg(q, a, b):
                return False
        for r2, bb in boxes.items():
            if seg_hits_rect(a, b, bb):
                return False
            # 包围盒还要缩掉 0.8 才判"穿"，可电源符号的横杠/箭头只有 2.5mm 见方，
            # 一缩就没了 —— 信号线于是从 GND 的横杠上横着划过去（gfx 口径下成片报 wire-sym）。
            # 逐笔判补上这一块：细笔画按线宽算，够宽就挡得住。
            if body_hit(a, b, body_rects.get(r2, [])):
                return False
        return True

    # ---------- Pass A：电源口（先合并共线引脚成电源轨） ----------
    # 块卡自己已经在这个引脚上放了电源符号的，别再放一个：同一个脚两个箭头，图上就多出
    # 一个不存在的电源口（人工抽的块卡 mcu_min_stm32 的 5 个 +3V3 全在脚上，wireup 又补了 5 个）。
    pw_at = {(R(s['at'][0]), R(s['at'][1])): ((s.get('fields') or {}).get('Value') or {}).get('text')
             for s in il['symbols'] if s.get('power')}

    pp = {}
    for key, net in net_of.items():
        if net in POWER and not key[0].startswith('#PWR'):
            p = pin_pt[key]
            if pw_at.get((R(p[0]), R(p[1]))) == net:      # 已经有同名电源符号压在这个脚上
                continue
            pp.setdefault(net, []).append((key[0], key[1], pin_pt[key]))

    # 叠在同一坐标的引脚（如 U1 的 23/35/47）只留一个代表
    for net, lst in pp.items():
        seen, uniq = set(), []
        for it in lst:
            k = (R(it[2][0]), R(it[2][1]))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(it)
        pp[net] = uniq

    runs = []
    for net, lst in pp.items():
        for axis in ('h', 'v'):
            groups = {}
            for it in lst:
                g = R(it[2][1]) if axis == 'h' else R(it[2][0])
                groups.setdefault(g, []).append(it)
            for g, items in groups.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda t: t[2][0] if axis == 'h' else t[2][1])
                chain = [items[0]]
                for prev, cur in zip(items, items[1:]):
                    # 块卡自己已经把这俩脚连上了（自带导线），别再画一根压上去
                    if geo_root.get(index.key(prev[2])) == geo_root.get(index.key(cur[2])):
                        if len(chain) > 1:
                            runs.append((net, chain))
                        chain = [cur]
                        continue
                    if index.dist(prev[2], cur[2]) <= MAXGAP and clear_path(
                            prev[2], cur[2], {(prev[0], prev[1]), (cur[0], cur[1])}):
                        chain.append(cur)
                    else:
                        if len(chain) > 1:
                            runs.append((net, chain))
                        chain = [cur]
                if len(chain) > 1:
                    runs.append((net, chain))

    handled = set()
    for net, chain in runs:
        # 一条电源轨画成"一根整线"（人工页就是这样：线穿过去，中间每个脚打结点），
        # 而不是一段一段接力 —— 视觉上才是轨，不是串。
        # 但整线是"两点连线"，中间可能压着别的器件（早先就有一条 GND 轨直接穿过二极管）。
        # 整段压着就退回**逐段**：链上相邻两脚本来就是一段段验过能画的（上面建链时验的），
        # 中间夹了个器件就断开那一段，别把整条轨都扔掉——一扔，五个脚各自去挂电源符号，
        # 一根线都不剩，拉线率凭空掉 5 个点（R2S 板 +3V3 就是这么掉的）。
        if clear_path(chain[0][2], chain[-1][2], {(c[0], c[1]) for c in chain}):
            il['wires'].append({'pts': [[R(chain[0][2][0]), R(chain[0][2][1])],
                                        [R(chain[-1][2][0]), R(chain[-1][2][1])]]})
        else:
            for prev, cur in zip(chain, chain[1:]):
                il['wires'].append({'pts': [[R(prev[2][0]), R(prev[2][1])],
                                            [R(cur[2][0]), R(cur[2][1])]]})
            report['blocked'].append({'net': net, 'pin': f'{chain[0][0]}.{chain[0][1]}',
                                      'why': '电源轨整段压器件，改逐段'})
        end = chain[-1]
        p = end[2]
        placed = False
        dirs = [1, -1, 2, -2] if net not in RAIL_UP else [-1, 1, -2, 2]
        for d in order_dirs(dirs, f'power:{net}'):
            vec = {1: (0, STUB), -1: (0, -STUB), 2: (STUB, 0), -2: (-STUB, 0)}[d]
            tip = (p[0] + vec[0], p[1] + vec[1])
            if not clear_path(p, tip, {(end[0], end[1])}):
                continue
            R_ = rot_for(f'power:{net}', (vec[0] / STUB, vec[1] / STUB))
            if R_ is None or not glyph_clear(f'power:{net}', tip, R_):
                continue
            pwr_n += 1
            il['symbols'].append(make_power(net, tip, R_, pwr_n))
            il['wires'].append({'pts': [[R(p[0]), R(p[1])], [R(tip[0]), R(tip[1])]]})
            if glyph_rect(f'power:{net}', tip, R_):
                pwr_glyphs.append(glyph_rect(f'power:{net}', tip, R_))
            note_power(il['symbols'][-1])
            placed = True
            break
        if not placed:
            report['blocked'].append({'net': net, 'pin': f'{end[0]}.{end[1]}',
                                      'why': '电源轨末端无符号落点'})
        for it in chain:
            handled.add((it[0], it[1]))
            report['pass_a'].append({'net': net, 'pin': f'{it[0]}.{it[1]}',
                                     'sym': f'power:{net}', 'rail': len(chain)})
        # 整条轨撤标签 —— 只有真落上了电源符号才撤，否则撤了网就断了
        for it in chain:
            if placed or net not in POWER:
                drop_labels_at((R(it[2][0]), R(it[2][1])))

    used_pts = set()
    for (r, n), net in sorted(net_of.items()):
        if net not in POWER or (r, n) in handled:
            continue
        p = pin_pt[(r, n)]
        k = (R(p[0]), R(p[1]))
        if k in used_pts or pw_at.get(k) == net:
            continue
        # 方向：GND 往下、电源往上；被自己身体挡住就翻面
        dirs = [1, -1, 2, -2] if net not in RAIL_UP else [-1, 1, -2, 2]
        chosen = None
        why = []
        for d in order_dirs(dirs, f'power:{net}'):
            vec = {1: (0, STUB), -1: (0, -STUB), 2: (STUB, 0), -2: (-STUB, 0)}[d]
            tip = (p[0] + vec[0], p[1] + vec[1])
            # 自己本体按**逐个图形原语**判，不用整体包围盒：
            # LED 的发光箭头伸在引脚外侧，包围盒把引脚包在里面，四个方向就全"压本体"了。
            if body_hit(p, tip, body_rects.get(r, [])):
                why.append(f'{d}:短线压本体')
                continue
            R_ = rot_for(f'power:{net}', (vec[0] / STUB, vec[1] / STUB))
            if R_ is None or not glyph_clear(f'power:{net}', tip, R_):
                why.append(f'{d}:图形压东西')
                continue
            bad = None
            for r2, other in boxes.items():
                if r2 == r:
                    continue
                if seg_hits_rect(p, tip, other):
                    bad = f'{d}:短线压{r2}'
                    break
            if bad:
                why.append(bad)
                continue
            # 不能压到别的器件的引脚
            for k2, q in pin_pt.items():
                if k2 == (r, n):
                    continue
                if index.dist(q, tip) < 0.1 or (pt_on_seg(q, p, tip) and index.dist(q, p) > 0.1):
                    bad = f'{d}:短线压{k2[0]}.{k2[1]}'
                    break
            if bad:
                why.append(bad)
                continue
            chosen = (vec, tip, R_)
            break
        if not chosen:
            report['blocked'].append({'net': net, 'pin': f'{r}.{n}',
                                      'why': '电源符号无落点 ' + ' '.join(why)})
            continue
        vec, tip, R_ = chosen
        used_pts.add(k)
        pwr_n += 1
        il['symbols'].append(make_power(net, tip, R_, pwr_n))
        il['wires'].append({'pts': [[R(p[0]), R(p[1])], [R(tip[0]), R(tip[1])]]})
        if glyph_rect(f'power:{net}', tip, R_):
            pwr_glyphs.append(glyph_rect(f'power:{net}', tip, R_))
        note_power(il['symbols'][-1])
        drop_labels_at(k)
        report['pass_a'].append({'net': net, 'pin': f'{r}.{n}', 'sym': f'power:{net}'})

    # 电源符号自带的引脚坐标也要进 pin_pt（避免后续走线压到）
    for s in il['symbols']:
        if s.get('power') and s['id'].startswith('#PWR'):
            pins[s['id']] = {'1': (s['at'][0], s['at'][1])}
            pin_pt[(s['id'], '1')] = (s['at'][0], s['at'][1])
            net_of[(s['id'], '1')] = s['lib'].split(':', 1)[1]
            # 图形也当障碍物——Pass B 走线时 boxes 里还没有这些符号，
            # 早先信号线就从 GND 的横杠/电源的箭头里穿过去了。
            bb_ = bbox(s, geoms.get(s['lib']) or [])
            if bb_:
                boxes[s['id']] = bb_
            body_rects[s['id']] = [bbox(s, [pr]) for pr in (geoms.get(s['lib']) or [])
                                   if H(pr) != 'pin' and bbox(s, [pr])]

    # ---------- Pass B：信号走线 ----------
    nets = {}
    for key, net in net_of.items():
        if net in POWER or not net:
            continue
        nets.setdefault(net, []).append(key)

    # 已经画下的线段（带网名）：新线只要**和别的网的线共用一个顶点**，KiCad 就判这两根
    # 网短接。以前没查这一条，靠"摆得开"侥幸不出事——块一劈细、线一多就现形：
    # STM32F103 上 J1.3/R2.1 的拐点正好落在 VBUS 的竖线上，两网并成一个。
    drawn = []
    for w in il['wires']:
        pts = w.get('pts') or []
        for k in range(len(pts) - 1):
            drawn.append((tuple(pts[k]), tuple(pts[k + 1]), None))
    wired = set()
    for net, members in sorted(nets.items()):
        if net in skip_nets:
            continue
        members = [m for m in members if m in pin_pt]
        edges = []
        # 拒绝原因分开记：这几项决定了"能连就连"到底难在哪 ——
        #   too_long = 摆得太远（排布问题） / blocked = 太远但没路（布线器问题）。
        # 以前只有"连上/没连上"两个数，分不清该改排布还是改布线。
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = pin_pt[members[i]], pin_pt[members[j]]
                # 已经在一个几何连通块里（块卡自带导线连好的），不要再画一根
                if geo_root.get(index.key(a)) == geo_root.get(index.key(b)):
                    continue
                paths = _route_paths(a, b)
                why = 'too_long'
                for pth in paths:
                    L = sum(index.dist(pth[k], pth[k + 1]) for k in range(len(pth) - 1))
                    if L > MAXLEN:
                        continue
                    ok = True
                    for k in range(len(pth) - 1):
                        s0, s1 = pth[k], pth[k + 1]
                        for key2, q in pin_pt.items():
                            if key2 in (members[i], members[j]):
                                continue
                            if index.dist(q, s0) < 0.05 or index.dist(q, s1) < 0.05:
                                ok = False
                                break
                            if pt_on_seg(q, s0, s1):
                                ok = False
                                break
                        if not ok:
                            break
                        for r2, bb in boxes.items():
                            # 同 clear_path：包围盒之外还要逐笔判，
                            # 否则 2.5mm 见方的电源横杠在 pad=0.8 下一缩就消失了。
                            if seg_hits_rect(s0, s1, bb) or body_hit(s0, s1, body_rects.get(r2, [])):
                                ok = False
                                break
                        if not ok:
                            break
                    if ok and _merges(pth, net, drawn):
                        ok = False
                    if ok:
                        edges.append((L, members[i], members[j], pth))
                        break
                    why = 'blocked'
                else:
                    report.setdefault('reject', {}).setdefault(why, 0)
                    report['reject'][why] += 1
        edges.sort()
        # Kruskal
        parent = {m: m for m in members}

        def f(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for L, a, b, pth in edges:
            if f(a) == f(b):
                continue
            parent[f(a)] = f(b)
            # 两个脚画在同一个点（USB 座子的 A1/A12/B1/B12、TVS 的 5/6…）本来就接着，
            # 再画一根零长度的线没意义，还曾经把整张图并成一个网（见 geom.on_seg）。
            if L > 0.02:
                il['wires'].append({'pts': [[R(x), R(y)] for x, y in pth]})
                for k in range(len(pth) - 1):
                    drawn.append((tuple(pth[k]), tuple(pth[k + 1]), net))
            wired.add(a)
            wired.add(b)
            report['pass_b'].append({'net': net, 'a': f'{a[0]}.{a[1]}', 'b': f'{b[0]}.{b[1]}',
                                     'len': round(L, 2)})
        # 同网但连不上的
        roots = {f(m) for m in members}
        if len(roots) > 1:
            for m in members:
                q = pin_pt[m]
                others = [index.dist(q, pin_pt[o]) for o in members if o != m]
                if others and min(others) > MAXLEN:
                    report['blocked'].append({'net': net, 'pin': f'{m[0]}.{m[1]}',
                                              'why': f'最近的同网引脚 {min(others):.1f}mm > {MAXLEN:.0f}mm'})
    # 撤掉已经被导线接上的标签
    # 但只有"整条网都连通"时才全撤；网被拆成几块时必须每块留一个标签当网名，
    # 否则只连了一半的那块会变成无名的孤岛（网表就断了）。
    members = {}
    for key, net in net_of.items():
        if net:
            members.setdefault(net, []).append(key)
    uf = {}

    def uf_find(x):
        uf.setdefault(x, x)
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    for e in report['pass_b']:
        a = tuple(e['a'].rsplit('.', 1))
        b = tuple(e['b'].rsplit('.', 1))
        ra, rb = uf_find(a), uf_find(b)
        if ra != rb:
            uf[ra] = rb

    keep_one = set()
    for net, ms in members.items():
        comps = {}
        for m in ms:
            comps.setdefault(uf_find(m), []).append(m)
        if len(comps) > 1:
            for comp in comps.values():
                keep_one.add(comp[0])

    def _hit(l):
        return [m for m in wired if index.dist(pin_pt[m], (l['at'][0], l['at'][1])) < 0.02]

    keep_i, root_lab = set(), {}
    for i, l in enumerate(il['labels']):
        hit = _hit(l)
        if hit:
            root_lab.setdefault(uf_find(hit[0]), []).append(i)
        # 同一坐标上可能叠着好几个脚（hit 里有好几个），只要**有一个**该留名就留。
        # 早先只看 hit[0]：那是集合里的任意一个，留还是删全看遍历顺序——
        # 每次跑出来的网数都不一样，usb_hub 上 U27 的 5/6 脚就常因此丢标签。
        if hit and not any(m in keep_one for m in hit):
            continue
        keep_i.add(i)
    # 每根网至少留一个名字。导线只解决"通不通"，不解决"叫什么"：全撤干净的话，
    # 语言里叫 `VIN_BUCK` 的网画完就成了 KiCad 的 `Net-(C6-Pad2)`——语言的权威
    # 到下游（PCB 网名、走线约束）就丢了，而人工页也是"拉一根线 + 留一个网名"。
    for idxs in root_lab.values():
        if not any(i in keep_i for i in idxs):
            keep_i.add(idxs[0])
    il['labels'] = [l for i, l in enumerate(il['labels']) if i in keep_i]

    # 标签和同名电源符号已经连在一起 —— 人工页不会再标一次，删掉（否则字压在器件上）
    pwr_pts = {}
    for s_ in il['symbols']:
        if s_.get('power'):
            val = ((s_.get('fields') or {}).get('Value') or {}).get('text')
            if val in POWER:
                pwr_pts.setdefault(val, []).append((s_['at'][0], s_['at'][1]))
    if pwr_pts:
        d_ = index.DSU()
        allsegs = [(w['pts'][i], w['pts'][i + 1]) for w in il['wires']
                   for i in range(len(w['pts']) - 1)]
        nodes_ = list(pin_pt.values()) + [q for v in pwr_pts.values() for q in v]
        for a_, b_ in allsegs:
            d_.union(index.key(a_), index.key(b_))
            for k_ in nodes_:
                if pt_on_seg(k_, a_, b_):
                    d_.union(index.key(k_), index.key(a_))
        keep2 = []
        for l in il['labels']:
            pt_ = (l['at'][0], l['at'][1])
            if l['text'] in pwr_pts and any(
                    d_.find(index.key(pt_)) == d_.find(index.key(q)) for q in pwr_pts[l['text']]):
                continue
            keep2.append(l)
        il['labels'] = keep2

    # 两个电源符号挨太近时，数值文字左右错开，别叠在一起
    pw_ = [s_ for s_ in il['symbols'] if s_.get('power')]
    for i_ in range(len(pw_)):
        for j_ in range(i_ + 1, len(pw_)):
            a_, b_ = pw_[i_], pw_[j_]
            fa_ = (a_.get('fields') or {}).get('Value') or {}
            fb_ = (b_.get('fields') or {}).get('Value') or {}
            if not fa_.get('at') or not fb_.get('at') or fa_.get('text') != fb_.get('text'):
                continue
            pa_, pb_ = fa_['at'], fb_['at']
            if abs(pa_[1] - pb_[1]) < 1.0 and abs(pa_[0] - pb_[0]) < 4.6:
                r_ = pb_[2] if len(pb_) > 2 else 0.0
                fa_['at'] = [round(pa_[0] - 4.2, 2), pa_[1], r_]
                fb_['at'] = [round(pb_[0] + 4.2, 2), pb_[1], r_]

    # ---------- 结点 ----------
    # 人工页的规矩：引脚落在导线中间（电源轨穿过去）才打点；两根线端对端相接不打点。
    segs = []
    for w in il['wires']:
        for i in range(len(w['pts']) - 1):
            segs.append((w['pts'][i], w['pts'][i + 1]))
    for a, b in segs:
        for k in pin_pt.values():
            if pt_on_seg(k, a, b) and index.dist(k, a) > 0.05 and index.dist(k, b) > 0.05:
                if all(index.dist(k, j) > 0.1 for j in il['junctions']):
                    il['junctions'].append([R(k[0]), R(k[1])])
    # 别处的线端点搭在这根线的中间 —— 也算三通
    for w in il['wires']:
        for e in (w['pts'][0], w['pts'][-1]):
            for a, b in segs:
                if pt_on_seg(e, a, b) and index.dist(e, a) > 0.05 and index.dist(e, b) > 0.05:
                    if all(index.dist(e, j) > 0.1 for j in il['junctions']):
                        il['junctions'].append([R(e[0]), R(e[1])])
                    break

    # ---------- Pass C：标签外移成短线（复刻人工画法：标签从不贴引脚） ----------
    moved_n = 0
    # 电源符号是后面才落的，把它们的占位补进 boxes，Pass C 才不会把标签挪到符号身上
    for s_ in il['symbols']:
        if s_.get('power') and s_['id'] not in boxes:
            bb_ = bbox(s_, geoms.get(s_['lib']) or [])
            if bb_:
                boxes[s_['id']] = bb_
            body_rects[s_['id']] = [bbox(s_, [pr]) for pr in (geoms.get(s_['lib']) or [])
                                    if H(pr) != 'pin' and bbox(s_, [pr])]
    fboxes = field_boxes(il)
    used_boxes = []
    # 已经画好的导线：标签短线绝不能落在别的线上（那就是短路）
    wire_segs = [(w['pts'][i], w['pts'][i + 1])
                 for w in il['wires'] for i in range(len(w['pts']) - 1)]

    def lands_on_wire(t):
        for a, b in wire_segs:
            if pt_on_seg(t, a, b) or index.dist(t, a) < 0.05 or index.dist(t, b) < 0.05:
                return True
        return False

    def cuts_wire(a, b):
        """a->b 这段线上不能压到已画导线的端点（压上就是接上了，等于短路）。"""
        for s0, s1 in wire_segs:
            for e in (s0, s1):
                if pt_on_seg(e, a, b) and index.dist(e, a) > 0.05 and index.dist(e, b) > 0.05:
                    return True
        return False
    for l in list(il['labels']):
        p = (l['at'][0], l['at'][1])
        owner = None
        for key, q in pin_pt.items():
            if index.dist(q, p) < 0.05:
                owner = key
                break
        if not owner:
            continue
        bb = boxes.get(owner[0])
        if not bb:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        dx, dy = p[0] - cx, p[1] - cy
        # 首选"背离本体"的方向，其余三个方向兜底：哪个方向文字不打架就走哪个。
        # **方向按"离哪条边最近"定，不能按"本体中心看过去的方位"定**：
        # 48 脚 MCU 右下角那只脚，从中心看是"偏下"，可它其实长在右边，
        # 往下走就是贴着本体边缘纵向划过，必然穿过下面每一只脚——
        # 四个方向全灭，Pass C 只好把标签留在引脚上（拉线率上不去的大头就在这）。
        dirs = [d for _, d in sorted([(abs(p[0] - bb[0]), (-1, 0)),
                                      (abs(p[0] - bb[2]), (1, 0)),
                                      (abs(p[1] - bb[1]), (0, -1)),
                                      (abs(p[1] - bb[3]), (0, 1))])]
        seen_dir = set()
        tip = None
        for d in dirs:
            if d in seen_dir:
                continue
            seen_dir.add(d)
            rot = 0.0 if d[0] == 1 else (180.0 if d[0] == -1 else (90.0 if d[1] == -1 else 270.0))
            # 由近及远：先试人工页常见的 2.54~7.62，再往外放到 25.4。
            # 摆位密的时候近处全被占，不收长距离就只能把标签留在引脚上（压在器件上）。
            for L in (7.62, 5.08, 10.16, 2.54, 12.7, 15.24, 17.78, 20.32, 25.4):
                t = (p[0] + d[0] * L, p[1] + d[1] * L)
                if any(index.dist(q, t) < 0.5 for q in pin_pt.values()):
                    continue
                # 短线不能从自己器件的另一个脚上穿过去 —— 那就是把两个网焊在一起
                if any(pt_on_seg(q, p, t) and index.dist(q, p) > 0.05
                       for q in pin_pt.values()):
                    continue
                if lands_on_wire(t):
                    continue
                if cuts_wire(p, t):
                    continue
                # 别人家的器件用整体包围盒（留 0.4 余量，保守点不吃亏）；
                # 自己器件按逐个图形原语判——它的包围盒可能把引脚包在里头
                # （LED 的箭头、USBLC6 的散点），用盒子判就等于哪儿都不让走。
                if any(seg_hits_rect(p, t, bb2, pad=0.4) or body_hit(p, t, body_rects.get(r2, []))
                       for r2, bb2 in boxes.items() if r2 != owner[0]):
                    continue
                if body_hit(p, t, body_rects.get(owner[0], [])):
                    continue
                lb = label_box(t, rot, l['text'])
                if any(rects_hit(lb, bx, 0.4) for bx in boxes.values()):
                    continue
                if any(rects_hit(lb, fx, 0.5) for fx in fboxes):
                    continue
                if any(rects_hit(lb, o, 0.4) for o in used_boxes):
                    continue
                tip = (t, rot)
                break
            if tip:
                break
        if not tip:
            # 挪不动就原地留着（挪开就断了连接），只把朝向翻到本体外侧，别让字压在本体上
            if l['at'][0] < cx - 0.01:
                l['at'] = [l['at'][0], l['at'][1], 180.0]
            elif l['at'][0] > cx + 0.01:
                l['at'] = [l['at'][0], l['at'][1], 0.0]
            elif l['at'][1] < cy:
                l['at'] = [l['at'][0], l['at'][1], 90.0]
            else:
                l['at'] = [l['at'][0], l['at'][1], 270.0]
            report['blocked'].append({'net': l['text'], 'pin': f'{owner[0]}.{owner[1]}',
                                      'why': '标签无处外移（原地留，朝向翻到外侧）'})
            continue
        t, rot = tip
        used_boxes.append(label_box(t, rot, l['text']))
        l['at'] = [round(t[0], 2), round(t[1], 2), rot]
        # 外移量为 0 就别画这根线：零长度导线会被当成"经过整张图"（见 geom.on_seg）
        if index.dist(t, p) > index.EPS:
            il['wires'].append({'pts': [[round(p[0], 2), round(p[1], 2)],
                                        [round(t[0], 2), round(t[1], 2)]]})
            wire_segs.append(([round(p[0], 2), round(p[1], 2)], [round(t[0], 2), round(t[1], 2)]))
            moved_n += 1

    if callable(POST):
        POST(il)
    report['pass_c'] = moved_n
    return report


def label_layout(il):
    """标签派：**一根导线都不画**，每个有网的脚上直接挂一个同名标签。

    这不是我们的发明，是人工页的定义性画法（11 张本地页：导线条数全 0，标签到最近引脚
    的距离中位数 0.00，即 100% 贴在脚上；电源也不画符号，画成全局标签）。
    拼装器要复刻某个人的画法，就得能在这两种流派里选，而不是一律实线。

    唯一要做对的是**朝向**：标签文字要从引脚朝本体外侧伸出去，否则字压在自己器件上。
    """
    geoms = {k: (v.get('prims') or []) for k, v in (il.get('libsym') or {}).items()}
    boxes = {}
    for s in il['symbols']:
        bb = bbox(s, geoms.get(s['lib']) or [])
        if bb:
            boxes[s['id']] = bb
    pin_pt = {}
    for s in il['symbols']:
        for n, q in (s.get('pins_at') or {}).items():
            pin_pt[(s['id'], n)] = (q[0], q[1])

    report = {'pass_a': [], 'pass_b': [], 'pass_c': 0, 'blocked': [], 'school': 'label'}
    for l in il['labels']:
        p = (l['at'][0], l['at'][1])
        owner = next((k for k, q in pin_pt.items() if index.dist(q, p) < 0.05), None)
        bb = boxes.get(owner[0]) if owner else None
        if not bb:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        dx, dy = p[0] - cx, p[1] - cy
        # 和 Pass C 同一套朝向约定：+x→0，-x→180，-y→90，+y→270
        if abs(dx) >= abs(dy):
            rot = 0.0 if dx >= 0 else 180.0
        else:
            rot = 90.0 if dy < 0 else 270.0
        l['at'] = [round(p[0], 2), round(p[1], 2), rot]
    return report


def run(il, out_il, out_svg, report_path, skip_nets=()):
    """按当前流派画这张图，然后落盘。流派由调用方设 `SCHOOL`（默认实线派）。"""
    report = label_layout(il) if SCHOOL == 'label' else wire_layout(il, skip_nets)
    if callable(POST):
        POST(il)
    # 落盘前把 pins_at / nets 重算一遍 —— 导出的 IL 必须是自洽的
    extract.refresh(il)
    json.dump(il, open(out_il, 'w', encoding='utf-8'), ensure_ascii=False)
    render_full.render(il, out_svg, fit=True)
    json.dump(report, open(report_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps({
        'school': SCHOOL,
        'symbols': len(il['symbols']), 'wires': len(il['wires']),
        'labels': len(il['labels']), 'junctions': len(il['junctions']),
        'power_syms': sum(1 for s in il['symbols'] if s.get('power')),
        'pass_a': len(report['pass_a']), 'pass_b': len(report['pass_b']),
        'pass_c': report.get('pass_c', 0),
        'blocked': len(report['blocked']),
    }, ensure_ascii=False))


if __name__ == '__main__':
    run(extract.extract(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4])
