#!/usr/bin/env python3
"""纯几何原语：坐标变换、线段判定、并查集、符号库几何。

这一层**不做文件 IO、不认识 kicad_sch 也不认识 IL**——
extract（kicad_sch → IL）和 index（直接解析原图，只当验收对照）共用它，
这样两边算出来的坐标必然同一套口径，比对才有意义。
"""
import math
import re

from sexpr import H, one  # noqa: F401

EPS = 0.02
# "端点对端点"的容差：比 EPS 松。IL 坐标是 0.01mm 量化的（`key`），两个在文件里**完全
# 重合**的点，量化后可以差到 0.01；再叠上平移/换算的 0.01 级误差，实测要到 0.03 才稳
# （2026-10-29 F405 页：裁块门③ 在 dy=0.315mm 平移下把 U3.2 与它的 GND 符号拆开）。
# 0.05mm 仍远小于原理图最小格 0.635/1.27mm，不会把两个真连接点并到一起。
ABUT = 0.05


def key(p):
    return (round(p[0], 2), round(p[1], 2))


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def on_seg(pt, a, b):
    ax, ay = a
    bx, by = b
    px, py = pt
    # 零长度线段（a==b）单独判：下面那套共线+点在区间内的算法对它是退化的——
    # cross 恒为 0、dot 恒为 0，于是"整张图都在它上面"，一次就把所有节点并成一个网。
    # 它还会同时骗过 extract 和 index（两家共用这个函数），check_il 于是也发现不了。
    if dist(a, b) <= EPS:
        return dist(pt, a) <= EPS
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 0.05 * max(1, dist(a, b)):
        return False
    # 端点余量必须是**长度**量纲：dot 是长度²，直接拿 EPS 比等于把容差除以线段长——
    # 长线上只允许 EPS/len（10mm 线 = 0.002mm），而 key() 自己就round 到 0.01mm，
    # 于是"脚端点 vs 长导线端点"差 0.005mm 就判不连（实测 F405 页 L1.1 孤网）。
    L = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -EPS * L:
        return False
    if dot > L * L + EPS * L:
        return False
    return True


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def transform(px, py, sx, sy, rot, mirror=None):
    """库坐标（y 朝上）→ 图纸坐标（y 朝下）。"""
    x, y = px, -py
    a = math.radians(rot or 0)
    xr = x * math.cos(a) + y * math.sin(a)
    yr = -x * math.sin(a) + y * math.cos(a)
    if mirror == 'x':
        yr = -yr
    if mirror == 'y':
        xr = -xr
    return (sx + xr, sy + yr)


def lib_pins_units(root):
    """lib name -> {单元号: {引脚号: (x, y)}}，库坐标（y 朝上）。

    **为什么要分单元**：KiCad 把库符号按"单元"拆成子符号 `名字_<单元>_<风格>`，
    单元 0 是所有单元共有的部分。LM393 这种双比较器 + 电源单元的画法里，两个比较器
    的三角形在库里画在**同一处**，于是按并集取脚会把 B 单元的 5/6/7 摆到 A 单元
    3/2/1 的位置上——凭空在每个比较器输出上多接一个脚（2026-10-27 组合甲实测：
    一页 16 个幽灵脚，KiCad 网表一比就露）。放置的符号带 `(unit N)`，按 N 取脚才对。
    """
    libs = one(root, 'lib_symbols')
    out = {}
    if not libs:
        return out
    for s in libs[1:]:
        if not isinstance(s, list) or H(s) != 'symbol':
            continue
        name = s[1]
        base = str(name).split(':')[-1]
        units = {}

        def walk(n, unit=0):
            for c in n[1:]:
                if not isinstance(c, list):
                    continue
                if H(c) == 'pin':
                    a = one(c, 'at')
                    num = one(c, 'number')
                    if a and num:
                        try:
                            units.setdefault(unit, {})[num[1]] = (float(a[1]), float(a[2]))
                        except (ValueError, IndexError):
                            pass
                elif H(c) == 'symbol':
                    # 子符号名形如 `LM393_1_1`（单元_风格）；认不出就继承上一层单元
                    m = re.match(re.escape(base) + r'_(\d+)_(\d+)$', str(c[1]))
                    walk(c, int(m.group(1)) if m else unit)

        walk(s)
        out[name] = units
    return out


def lib_hidden_power(root):
    """lib name -> {单元号: {引脚号: 脚名}}，只收**隐藏的 `power_in` 脚**。

    KiCad 的"不可见电源脚"规矩（`lib_pins` 看不到、几何提取抓不到的那一类）：
    符号里电气类型是 `power_in` 且画成隐藏的脚，**不画线也算接在"以脚名为名的全局网"**上。
    Bus Pirate 5 的 `TFT_20_QT200H1201` 就是这么做的——12 脚的 FPC 只有脚 1 画在图上，
    5/6/12 三只 GND 是隐藏电源脚；kicad-cli 网表把 1/5/6/12 全并进 GND，按几何算则
    5/6/12 浮空（2026-10-30 裁 `bp5_lcd_display` 时被"sch↔pcb 同一电路"门抓住）。
    """
    libs = one(root, 'lib_symbols')
    out = {}
    if not libs:
        return out
    for s in libs[1:]:
        if not isinstance(s, list) or H(s) != 'symbol':
            continue
        name = s[1]
        base = str(name).split(':')[-1]
        units = {}

        def walk(n, unit=0):
            for c in n[1:]:
                if not isinstance(c, list):
                    continue
                if H(c) == 'pin':
                    typ = str(c[1]) if len(c) > 1 else ''
                    # 隐藏写法两种：老文件一个裸 `hide` 原子，新文件 `(hide yes)`
                    hidden = any(isinstance(x, str) and x == 'hide' for x in c[1:]) \
                        or any(isinstance(x, list) and H(x) == 'hide'
                               and (len(x) < 2 or str(x[1]) in ('yes', 'true')) for x in c[1:])
                    nm, num = one(c, 'name'), one(c, 'number')
                    if typ == 'power_in' and hidden and nm and num:
                        units.setdefault(unit, {})[num[1]] = nm[1]
                elif H(c) == 'symbol':
                    m = re.match(re.escape(base) + r'_(\d+)_(\d+)$', str(c[1]))
                    walk(c, int(m.group(1)) if m else unit)

        walk(s)
        out[name] = units
    return out


def lib_pins(root):
    """lib name -> {pin_number: (x, y)}，库坐标（y 朝上），**所有单元并集**。

    只适合"这个库符号一共有哪些脚"这类问题（画图、符号信息）；**算某个放置实例的
    引脚位置必须用 `pins_for_unit`**，否则多单元器件会造出幽灵脚。
    """
    out = {}
    for name, units in lib_pins_units(root).items():
        pins = {}
        for u in sorted(units):
            pins.update(units[u])
        out[name] = pins
    return out


def pins_for_unit(units, unit):
    """按单元取脚：共有单元（0）+ 本单元。

    `units` 里没有这个单元号（老文件、借来的图形、单单元符号）时退回并集——
    宁可多算也不能少算，少算会把真的连接丢掉。
    """
    if not units:
        return {}
    uni = {}
    for u, m in units.items():
        try:
            uni[int(u)] = m
        except (TypeError, ValueError):
            uni[u] = m
    common = uni.get(0) or {}
    spec = uni.get(int(unit or 1)) or {}
    if not spec:
        merged = {}
        for u in sorted(uni, key=str):
            merged.update(uni[u])
        return merged
    merged = dict(common)
    merged.update(spec)
    return merged


def collect_prims(node, out):
    for c in node[1:]:
        if not isinstance(c, list):
            continue
        h = H(c)
        if h in ('rectangle', 'polyline', 'circle', 'arc', 'pin', 'text'):
            out.append(c)
        elif h == 'symbol':
            collect_prims(c, out)
    return out


def lib_geometry(root):
    """lib name -> 图形原语列表（矩形/折线/圆/弧/引脚），仍是 S 表达式结构。"""
    libs = one(root, 'lib_symbols')
    out = {}
    if not libs:
        return out
    for s in libs[1:]:
        if isinstance(s, list) and H(s) == 'symbol':
            out[s[1]] = collect_prims(s, [])
    return out


def pins_at(libpins, at, mirror=None):
    """库内引脚 → 图纸绝对坐标。"""
    a = list(at) + [0, 0, 0]
    sx, sy, rot = a[0], a[1], a[2]
    return {n: transform(px, py, sx, sy, rot, mirror) for n, (px, py) in libpins.items()}
