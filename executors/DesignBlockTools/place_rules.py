#!/usr/bin/env python3
"""规则摆件——替代 `extract_geometry.place_tight` 的"原理图位置等比缩"。

**为什么换**：原来的摆件是把块片段里的**符号位置**乘 0.2 再推开重叠，等于把
原理图的示意距离原样搬到板上。原理图里去耦电容习惯画成一列，板上就是一列——
实测 `mcu_min_stm32` 最远电源脚离最近的去耦电容 **15.61mm**（人手 0.5~1.5mm），
六只电容在 MCU 右侧排成一竖列，全库 10 块 7 块不过 `block_cohesion.py` 的门。
根因不是"摆得不好"，是**摆的规则本身不存在**。

**四条规则（用户 2026-10-26 定调"一面对外"）**：

R1 **接口一面对外**：接插件 + 与接插件共享非电源网的器件（ESD/隔离/收发器/保险丝）
排成两列贴在同一个面上——接插件在外列、**嘴朝外**（嘴 = 焊盘质心的反方向，
这是从 Molex USB 座与 MX126 端子实测出来的通用规则，不靠查表）。这一面之外
不许再放别的件，块才是"有嘴的方块"，拼板才能嘴对嘴贴。
⚠ **排针没有嘴**：单排直插排针是插针、焊盘穿板而下，"嘴"方向不存在。对它用嘴规则
只会转出"脚排横在边上"的解——四根脚沿边一字排开，只有一根正对目标，其余斜着绕进
板内（J3 横放实测多跑 9mm）。排针的规则是**脚排平行于所贴的边**，0°/180° 再由
"脚到目标脚的距离和"定。

R2 **去耦贴脚**：每只电源脚配一只电容，落在该脚**外侧**，VDD 盘朝芯片、
GND 盘朝外让出过孔位；同侧多只时沿边**交错** 0.55mm（禁整齐行，整齐行会
挡住 VDD 脚到 GND 过孔之间的扇出通道）。

R3 **时钟件贴芯片**：块内有时钟件（晶体/负载电容）时紧邻芯片的时钟脚。

R4 **其余按连接亲密度就近**：与已摆器件共享信号网的往它们的质心贴，
同时给一点向心力防止摊开。最后统一推开到 0.45mm 净距。

焊盘朝哪个方向不看文档——`apply` 里对 0/90/180/270 四个朝向**逐个试**，
读回真实焊盘坐标打分，取最优。这样旋转约定错了也不会错（KiCad 的
y 轴向下 + 旋转方向是反复踩的坑）。

用法：
    Place/bin/python DesignBlockTools/place_rules.py --board <base.kicad_pcb> \
        --card <块>.table.json --out <placed.kicad_pcb> [--face E] [--dry-run]
`--dry-run` 只算不写板，打印摆件表与去耦距离（改规则时用它快跑）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_KPY = None


def _config_path():
    return os.environ.get("HWS_CONFIG") or os.path.expanduser("~/.harnesspcb.json")


def kicad_py():
    """KiCad 自带 python（pcbnew 通道）的三级解析，与 `kca doctor` 同一口径。

    环境变量 > 机器配置文件 > 标准探测（装了 kicad-access 就借它的推导）。

    **懒加载**：这段原来写在模块顶层，形如
    `json.loads(open("~/.harnesspcb.json"))["HWS_KICAD_PY"]`——陌生机器上没写配置
    文件时**一 import 就 FileNotFoundError**，连 `--help` 都出不来（2026-10-31 换血
    实测）。现在只有真要开板才解析，缺的时候给三种补法而不是抛栈。
    """
    global _KPY
    if _KPY:
        return _KPY
    p = os.environ.get("HWS_KICAD_PY")
    if not p:
        try:
            with open(_config_path(), encoding="utf-8") as f:
                p = json.load(f).get("HWS_KICAD_PY")
        except (OSError, ValueError):
            p = None
    if not p:
        try:                       # 标准探测：kca 会从 kicad-cli 推导 app 内的 python
            sys.path.insert(0, os.path.normpath(
                os.path.join(HERE, "..", "KiCAD_Access", "src")))
            from kicad_access.tools import kicad_python_status  # noqa: PLC0415
            st = kicad_python_status()
            p = str(st.path) if st.ok and st.path else None
        except Exception:          # noqa: BLE001 —— 没装 kca 就只剩下面那条报错，不影响本意
            p = None
    if not p:
        raise SystemExit(
            "找不到 KiCad 自带 python（pcbnew 通道）。三选一：\n"
            "  1) export HWS_KICAD_PY=<KiCad.app>/Contents/Frameworks/Python.framework/"
            "Versions/3.9/bin/python3\n"
            f"  2) 在 {_config_path()} 里写 {{\"HWS_KICAD_PY\": \"<同上>\"}}\n"
            "  3) 装好 kicad-access 后跑 `kca doctor` 看标准安装位探测到了没有"
        )
    _KPY = p
    return p

GAP = 0.45          # 件间最小净距 mm
STAGGER = 0.55      # 同侧去耦电容的交错步长
# 去耦电容**到焊盘外端**的净距。曾经是 0.15（"DRC 只判 courtyard 相交，越近越好"），
# 2026-10-29 mcu_stm32f411 实测把它否了：QFN 的脚和内层平齐、不外伸，电容排在外圈
# 就等于在**每只脚正前方**砌一堵墙——U2-42/43/45 的直出走廊只剩 0.15mm（门槛 0.45），
# 口岸体检判死，FreeRouting 也在那儿留下 5 根未布通。0.60 = 门槛 0.45 再留一档余量；
# 电气上仍是"贴着脚"（0402 从焊盘走到电容约 1mm）。
CAPGAP = 0.60
# 被脚钉死的件（晶体/串阻/时钟链）与 hub 之间**必须让出的逃逸走廊** mm。
# 用户 2026-10-30："同样的面积下 Y1 应该离芯片远一点，因为以后芯片还可能要引出线来。"
# 原来 place_rest 的代价函数里只有 `0.1 × 离 hub 距离` 这么一条弱惩罚，晶体最终只受
# GAP 约束 → 实测贴到 1.04mm（收边后量到 0.69mm），芯片那一面的出脚通道被晶体整块堵死。
CORRIDOR = 2.0
# 落位搜索预算：沿法向 0~6.0mm、横移 0~±3.1mm。
# ⚠ 预算太小会把"没搜到"误判成"堵死"，然后掉进兜底一路推出去——mcu_stm32f411 的
# C14 就是这么被推到 10.73mm 外的（旧值 4.0mm / ±1.6mm）。
LAND_OUT = 61
LAND_LAT = 31
FACE = {'E': (1, 0), 'S': (0, 1), 'W': (-1, 0), 'N': (0, -1)}   # 面 → 朝外法向（y 向下）
PORT_RAY = 6.0      # 口岸脚"往外走"至少要看多远（mm），撞上别人才算被挡
PORTS = set()       # 口岸网名（--ports），空集时本模块的所有新规则不生效

POWER_RE = re.compile(r'(^|[^A-Za-z0-9_])(\+?3V3|3\.3V|VDD[A-Z0-9_]*|VCC[A-Z0-9_]*'
                      r'|\+?5V|VBUS[0-9A-Z_]*|VIN|VOUT|VRAW|VIS[O0]|\+?12V)', re.I)
GND_RE = re.compile(r'^(GND|FG|AGND|PGND|VSS.*|0V)$', re.I)
CONN_HINTS = ('Connector', 'TerminalBlock', 'USB_', 'RJ45')


# ============================================================ pcbnew 读板
DUMP = r'''
import json, pcbnew
b = pcbnew.LoadBoard(%(board)r)
out = {}
for f in b.GetFootprints():
    bb = f.GetBoundingBox(False, False)
    rot = round(f.GetOrientationDegrees() %% 360, 1)
    try:                                  # 占地按 courtyard 量——DRC 判重叠就是用它
        crt = f.GetCourtyard(pcbnew.F_CrtYd)
        if crt.OutlineCount():
            bb = crt.BBox()
    except Exception:
        pass
    w, h = bb.GetWidth()/1e6, bb.GetHeight()/1e6
    # 局部坐标系里的占地矩形（相对 footprint 锚点），plan 里要给 hub 当障碍物用
    fx, fy = f.GetPosition().x/1e6, f.GetPosition().y/1e6
    crt_rect = [bb.GetLeft()/1e6 - fx, bb.GetTop()/1e6 - fy,
                bb.GetRight()/1e6 - fx, bb.GetBottom()/1e6 - fy]
    # ⚠ **一切局部坐标都以 courtyard 中心为原点**，不是封装锚点。
    # 落位（APPLY）是把"实际包络中心"推到计划点上，所以计划空间的原点 = 包络中心；
    # 焊盘/本体若按锚点记，凡锚点不在中心的封装（iso_dcdc 的 THT 模块锚点离中心 8.9mm）
    # 整条去耦链就会跟着错 8.9mm——实测 VIN 电容跑到 10.94mm 外。
    ccx = (crt_rect[0] + crt_rect[2]) / 2
    ccy = (crt_rect[1] + crt_rect[3]) / 2
    if abs(rot-90) < 1 or abs(rot-270) < 1:
        w, h = h, w
    # 本体包络：F.Fab 上的图形（不含文字）。焊盘不一定对称挂在本体周围，
    # 判"朝外方向"得看脚落在本体的哪一面，不能只看坐标符号。
    bx = []
    for it in f.GraphicalItems():
        if 'Fab' not in it.GetLayerName() or it.GetClass() == 'PCB_TEXT':
            continue
        g = it.GetBoundingBox()
        bx.append((g.GetLeft()/1e6, g.GetTop()/1e6, g.GetRight()/1e6, g.GetBottom()/1e6))
    body = ([min(b[0] for b in bx) - ccx, min(b[1] for b in bx) - ccy,
             max(b[2] for b in bx) - ccx, max(b[3] for b in bx) - ccy] if bx else None)
    pads = []
    for p in f.Pads():
        q = p.GetFPRelativePosition()       # 基板 footprint 全是 rot 0，这就是未旋转的本地偏移
        s = p.GetSize()
        pads.append({'num': p.GetNumber(), 'net': p.GetNetname(),
                     'lx': q.x/1e6 - ccx, 'ly': q.y/1e6 - ccy,
                     'w': s.x/1e6, 'h': s.y/1e6})
    out[f.GetReference()] = {'lib': f.GetFPIDAsString(), 'rot': rot,
                             'w': round(w, 3), 'h': round(h, 3),
                             'body': body,
                             'crt': [round(-w / 2, 3), round(-h / 2, 3),
                                     round(w / 2, 3), round(h / 2, 3)],
                             'pads': pads}
print(json.dumps(out))
'''

# ============================================================ pcbnew 写板
APPLY = r'''
import json, math, pcbnew
b = pcbnew.LoadBoard(%(board)r)
plan = json.load(open(%(plan)r))
byname = {f.GetReference(): f for f in b.GetFootprints()}

def mouth(f):
    ps = [(p.GetPosition().x/1e6, p.GetPosition().y/1e6) for p in f.Pads()]
    if not ps:
        return (0.0, 1.0)
    cx = sum(p[0] for p in ps)/len(ps); cy = sum(p[1] for p in ps)/len(ps)
    ox, oy = f.GetPosition().x/1e6, f.GetPosition().y/1e6
    dx, dy = ox-cx, oy-cy                      # 焊盘质心 → 本体，即朝外方向
    n = math.hypot(dx, dy)
    return (0.0, 1.0) if n < 1e-6 else (dx/n, dy/n)

for ref, spec in plan.items():
    f = byname[ref]
    f.SetPosition(pcbnew.VECTOR2I(int(round(spec['x']*1e6)), int(round(spec['y']*1e6))))
    aim = spec.get('aim')
    best, bestrot = None, 0
    # 计划里给的占位尺寸是**转过之后**的（接插件嘴朝外要转 90/270，轴就换了）。
    # 朝向搜索必须尊重它：只在实际包络对得上的旋转里挑，否则会出现"计划按转过的
    # 尺寸让开、实际却用没转的尺寸摆上去"→ 压邻居（can_node 的 J1 压 FL1/D1）。
    def crt(f):
        try:
            c = f.GetCourtyard(pcbnew.F_CrtYd)
            if c.OutlineCount():
                return c.BBox()
        except Exception:
            pass
        return f.GetBoundingBox(False, False)

    want_wh = spec.get('wh')
    cand = [0, 90, 180, 270]
    # 尺寸过滤**不能跳过**：计划给的 wh 就是这块预留的方框（`place_rest` 已经
    # 两档都搜过一遍，最终写回的是选中的那一档）。放行四个朝向会让件转到没预留的
    # 轴上 —— R1/C12 实测转 90° 后各压上 hub/邻居 0.22~0.3mm（courtyards_overlap）。
    if want_wh:
        dims = {}
        for rot in cand:
            f.SetOrientationDegrees(rot)
            bb = crt(f)
            dims[rot] = (bb.GetWidth()/1e6, bb.GetHeight()/1e6)
        fit = [r for r in cand
               if abs(dims[r][0] - want_wh[0]) < 0.15 and abs(dims[r][1] - want_wh[1]) < 0.15]
        # 一个都对不上（封装不对称、表里的 wh 和实物差一截）：**退化也要守住轴**，
        # 取最接近的那一档，绝不放开四个朝向——放开就等于按没预留的尺寸去挤邻居。
        cand = fit or [min(cand, key=lambda r: abs(dims[r][0] - want_wh[0])
                                       + abs(dims[r][1] - want_wh[1]))]

    def pad_cost(f, nets):
        """这个朝向下，本件的信号脚到目标脚的距离和（四向里挑最短的那个）。"""
        tot = 0.0
        for net, tx, ty in nets or []:
            d = 999.0
            for p in f.Pads():
                if p.GetNetname() != net:
                    continue
                q = p.GetPosition()
                d = min(d, math.hypot(q.x/1e6 - tx, q.y/1e6 - ty))
            if d < 998.0:
                tot += d
        return tot

    for rot in cand:
        f.SetOrientationDegrees(rot)
        if not aim:
            best, bestrot = 0.0, rot; break
        if aim['kind'] == 'toward':
            tx, ty = aim['target']
            d = {}
            for p in f.Pads():
                d[p.GetNumber()] = math.hypot(p.GetPosition().x/1e6-tx, p.GetPosition().y/1e6-ty)
            a = d.get(aim.get('vdd', '1'), 99.0)
            c = d.get(aim.get('gnd', '2'), 99.0)
            s = a - c                               # VDD 盘近、GND 盘远
        elif aim['kind'] == 'pads':
            # 只按"脚到脚"的距离定朝向：晶体两只信号脚要贴 OSC 脚，
            # 只看本体中心或 VDD/GND 会把 180° 解当成一样好。
            s = pad_cost(f, aim.get('nets'))
        elif aim['kind'] == 'rot':
            # 口岸脚朝外（`port_face` 用"射线净空"算好了，这里只是执行）。
            # 排在 `cand` 之外的解不给分也不硬塞：`want_wh` 的占位尺寸优先级更高。
            s = 0.0 if rot == aim['rot'] else 1.0
        else:                                       # mouth_out
            mx, my = mouth(f)
            align = mx*aim['dir'][0] + my*aim['dir'][1]
            # 嘴朝外是**硬约束**（权重 10 压过几毫米线长）；同向的两个解
            # （如 1×4 排针的 0°/180°）再按焊盘距离挑，J3 竖起来线就短了。
            s = -10.0 * align + pad_cost(f, aim.get('nets'))
        if best is None or s < best - 1e-9:
            best, bestrot = s, rot
    f.SetOrientationDegrees(bestrot)
    # 落位以**实际包络中心**为准：很多封装的锚点不在几何中心（J1 端子锚在 1 脚，
    # 本体整个偏在一侧），照计划点设位置会让本体压到邻居身上（can_node 实测 J1
    # 压住 FL1/D1 的 courtyard）。转完再量一次、把中心推到计划点上。
    want = pcbnew.VECTOR2I(int(round(spec['x']*1e6)), int(round(spec['y']*1e6)))
    c = crt(f).GetCenter()
    f.SetPosition(f.GetPosition() + (want - c))
b.Save(%(out)r)
print(json.dumps({'placed': len(plan), 'out': %(out)r}))
'''


def _kpy(script):
    r = subprocess.run([kicad_py(), '-c', script], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('pcbnew 失败：' + (r.stderr or r.stdout)[-800:])
    return r.stdout


# ============================================================ 图结构
def netmap(meta):
    """net → [(ref, pad)]"""
    nets = {}
    for ref, m in meta.items():
        for p in m['pads']:
            if p['net']:
                nets.setdefault(p['net'], []).append((ref, p['num']))
    return nets


def is_power(net):
    return bool(POWER_RE.search(net or ''))


def is_gnd(net):
    return bool(GND_RE.match(net or ''))


def affinity(meta, nets):
    """ref → {ref: 权重}。信号网 1.0，电源/地 0.15（共地不算亲密）。"""
    aff = {r: {} for r in meta}
    for net, mem in nets.items():
        w = 0.15 if (is_power(net) or is_gnd(net)) else 1.0
        refs = sorted({r for r, _ in mem})
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                aff[a][b] = aff[a].get(b, 0.0) + w
                aff[b][a] = aff[b].get(a, 0.0) + w
    return aff


def body_center(m):
    b = m.get('body')
    if b:
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    ps = m['pads']
    if not ps:
        return (0.0, 0.0)
    return (sum(p['lx'] for p in ps) / len(ps), sum(p['ly'] for p in ps) / len(ps))


def out_dir(p, m):
    """焊盘朝外的方向（单位向量，board 坐标）。

    LQFP/QFN 的焊盘沿出线方向拉长 → 用焊盘长轴；THT 模块的方焊盘排成一列 →
    用"排的垂直方向"；都不成立时用"离本体中心最远的那条轴"。
    **不能只看 lx/ly 的符号**：iso_dcdc 的模块脚在本体中部偏左，符号判会把电容
    摆到本体正上方（实测被挤到 4.3mm 外）。
    """
    bc = body_center(m)
    pads = m['pads']
    w, h = p.get('w', 0.0), p.get('h', 0.0)
    if w >= 1.3 * h:
        ax = 'x'
    elif h >= 1.3 * w:
        ax = 'y'
    else:
        xs = {round(q['lx'], 2) for q in pads}
        ys = {round(q['ly'], 2) for q in pads}
        if len(xs) < len(ys):
            ax = 'x'                       # 脚排成一竖列 → 出线横向
        elif len(ys) < len(xs):
            ax = 'y'
        else:
            ax = 'x' if abs(p['lx'] - bc[0]) >= abs(p['ly'] - bc[1]) else 'y'
    d = (p['lx'] - bc[0]) if ax == 'x' else (p['ly'] - bc[1])
    s = 1.0 if d > 0 else -1.0
    return (s, 0.0) if ax == 'x' else (0.0, s)


def _is_conn(m):
    """接插件判据 = **位号 J\\* 优先**，库名关键字兜底。

    J* 是 KiCad 全世界的接插件约定（R1 "接口一面对外" 与块级"接插件必须贴板边"
    两条规则都只对它有定义）；库名只是补充。2026-10-29 mcu_stm32f411 实测：
    J3 当时挂的是项目自带库 `mcu411:` 的封装、库名里没有 Connector，只按库名认
    就把它当普通件丢给 `place_rest`，落到离板边 3.39mm 的板中间，贴边门直接红
    —— 所以位号 J* 永远优先，库名只用来兜住没有 J 前缀的接插件。
    """
    return (str(m.get('ref') or '').startswith('J')
            or any(k in (m.get('lib') or '') for k in CONN_HINTS))


def _pin_axis(m):
    """单排直插接插件（脚全在一条线上、本体也骑在这条线上）→ 脚排走向 'x'/'y'。

    **"嘴朝外"这条规则对排针不成立**：排针是插针，没有嘴，焊盘穿过板子往下走。
    拿嘴的方向去转它，只能转出"脚排横在边上"的解——四根脚沿边一字排开，只有一根
    正对目标，其余三根斜着绕进板内（J3 实测多跑 9mm，10-28）。排针的规则是
    **脚排平行于所贴的边**，每根脚才能垂直出线。

    判据只用几何，不查表：所有焊盘在某一轴上坐标相同 → 脚排在另一条轴上。
    侧插排针（Horizontal）本体偏在焊盘线一侧，那就是真"嘴"，返回 None 交给 R1。
    """
    ps = m['pads']
    if len(ps) < 2:
        return None
    xs = {round(p['lx'], 2) for p in ps}
    ys = {round(p['ly'], 2) for p in ps}
    if len(xs) == 1 and len(ys) == len(ps) > 1:
        ax, line, perp = 'y', ps[0]['lx'], 'lx'
    elif len(ys) == 1 and len(xs) == len(ps) > 1:
        ax, line, perp = 'x', ps[0]['ly'], 'ly'
    else:
        return None
    b = m.get('body')
    if b:                                        # 本体中心离焊盘线 >0.8mm = 侧插，有嘴
        c = ((b[0] + b[2]) / 2) if perp == 'lx' else ((b[1] + b[3]) / 2)
        if abs(c - line) > 0.8:
            return None
    return ax


def _is_cap(m, nets):
    ref = m['ref']
    if ref[:1] not in ('C', 'FB', 'L'):
        return False
    two = [p for p in m['pads'] if p['net']]
    if len(two) != 2:
        return False
    return all(is_power(p['net']) or is_gnd(p['net']) for p in two)


# ============================================================ 摆件
def plan(meta, nets, aff, face='E'):
    for r, m in meta.items():
        m['ref'] = r
    fx, fy = FACE[face]
    parts = sorted(meta)
    # 实际占位尺寸 SZ：**转过的件要换轴**。0603 是 3.01×1.51，转 90° 后是 1.51×3.01，
    # 仍按未旋转尺寸让位就会压上去（实测 4 处 courtyard 重叠 + 2 处 +3V3/GND 短路）。
    SZ = {r: (meta[r]['w'], meta[r]['h']) for r in parts}
    # hub 是"干活的那颗"，不能选成接插件——接插件 pad 多但它是边界，不是中心。
    act = [r for r in parts if not _is_conn(meta[r])] or parts
    hub = max(act, key=lambda r: (len(meta[r]['pads']), meta[r]['w'] * meta[r]['h']))
    pin = {hub: (0.0, 0.0)}                     # 先钉中心，接口行算完再整体挪
    aim = {}

    # ---------- R1 接口面：接插件 + 与它共享非电源网的件
    conns = [r for r in parts if _is_conn(meta[r])]
    # 预留方框必须跟着**最终朝向**走：转 90° 宽高对调（3.63×11.25 ↔ 11.25×3.63），
    # 按没转的尺寸让位就会压邻居。所以朝向在这里就定，不留到 APPLY 再挑。
    flat_conn = set()                            # 单排排针：脚排平行于所贴的边
    for r in conns:
        ax = _pin_axis(meta[r])
        if ax is None:                           # 有嘴的（USB 座/接线端子）→ 嘴朝外 = 转 90°
            if fx:
                SZ[r] = (meta[r]['h'], meta[r]['w'])
            continue
        want = 'y' if fx else 'x'                # 边是竖的 → 脚排也竖着
        flat_conn.add(r)
        if ax != want:
            SZ[r] = (meta[r]['h'], meta[r]['w'])
    conn_nets = {p['net'] for c in conns for p in meta[c]['pads']
                 if p['net'] and not is_power(p['net']) and not is_gnd(p['net'])}
    cluster = set(conns)
    for r in parts:
        if r in cluster or r == hub or _is_cap(meta[r], nets):
            continue
        if any(p['net'] in conn_nets for p in meta[r]['pads'] if p['net']):
            cluster.add(r)

    # ---------- R2 去耦：**先按网分、再按侧分**
    # 电容常常比电源脚少（2 只 100n 伺候 3~5 只 VDD 脚），一只盖一只会漏脚。人手的
    # 做法是"每个面至少一只，就近盖住这个面上的相邻几只"。但**必须先认网**：一块板
    # 上常有两条以上电源轨（VIN/VOUT、+5V/+3V3），不分网就会把 VIN 电容摆到 VOUT 脚
    # 外侧（buck_reg 实测：输入/输出电容叠在一起，6 处短路）。
    caps = [r for r in parts if _is_cap(meta[r], nets) and r not in cluster]
    pins = [p for p in meta[hub]['pads']
            if p['net'] and is_power(p['net']) and not is_gnd(p['net'])]

    def _side(p):
        dx, dy = out_dir(p, meta[hub])
        return ('E' if dx > 0 else 'W') if dx else ('S' if dy > 0 else 'N')

    DIR = {'E': (1.0, 0.0), 'W': (-1.0, 0.0), 'S': (0.0, 1.0), 'N': (0.0, -1.0)}

    def _cap_net(c):
        """这只能去耦哪条轨：非 GND 的电源脚。"""
        pw = [q['net'] for q in meta[c]['pads']
              if q['net'] and is_power(q['net']) and not is_gnd(q['net'])]
        return pw[0] if pw else None

    assign = {}                                  # cap → (目标点(local), 覆盖的脚)
    unmatched = []                               # 找不到同网电源脚的电容，交给 R4 按亲密度摆
    for net in sorted({_cap_net(c) for c in caps} - {None}):
        cs = [c for c in caps if _cap_net(c) == net]
        mine = [p for p in pins if p['net'] == net]
        if not mine:                             # 芯片上没有这条轨的脚（比如 LDO 输出侧）
            unmatched += cs
            continue
        groups = {}
        for p in mine:
            groups.setdefault(_side(p), []).append(p)
        sides = sorted(groups, key=lambda s: -len(groups[s]))

        def _take_side(s, k, groups=groups):
            ps = sorted(groups[s], key=lambda p: (p['ly'], p['lx']) if s in 'EW' else (p['lx'], p['ly']))
            out = []
            for i in range(k):
                a = round(i * len(ps) / k)
                b = max(round((i + 1) * len(ps) / k), a + 1)
                seg = ps[a:b] or [ps[-1]]
                tx = sum(p['lx'] for p in seg) / len(seg)
                ty = sum(p['ly'] for p in seg) / len(seg)
                out.append((tx, ty, seg))
            return out

        left = list(cs)
        alloc = {s: 0 for s in sides}
        for s in sides[:len(left)]:              # 先保证每个面各有一只（够分的话）
            alloc[s] = 1
        left_n = len(left) - sum(alloc.values())
        while left_n > 0:                        # 余下的给"脚最多、已分最少"的面
            s = max(sides, key=lambda s: len(groups[s]) / (alloc[s] + 0.5))
            alloc[s] += 1
            left_n -= 1
        # ⚠ **一个面最多摆「这个面上这条轨的脚数」只电容**。再多就没有"正对脚"的空位
        # 可言了，硬塞只能把电容推到面外。10-30 mcu_stm32f411 实测：西面 3 只 3V3 脚
        # （U2-1/3/9）分了 4 只电容，第 4 只 C14 在该面怎么挪都要压芯片或压晶体，四个
        # 方向全判堵死 → 掉进兜底被推到 10.73mm 外、越过晶体贴在板子最左边，离它自己
        # 要伺候的那只脚 6.5mm。那一格本来该留给晶体往西退。
        spare = 0
        for s in sides:
            while alloc[s] > len(groups[s]):
                alloc[s] -= 1
                spare += 1
        for s in sides:
            for tx, ty, seg in _take_side(s, alloc[s]):
                if not left:
                    break
                assign[left.pop(0)] = ((tx, ty), seg, DIR[s])
        # 分不下的那几只**不钉面**：落地时拿"贴到哪只脚最近"自己挑（见落位循环里
        # `tgt is None` 那一支）。候选要**当场存下来**——`sides`/`groups` 是这条轨的
        # 局部变量，出了这个 for-net 循环就是下一条轨的了（多轨板会张冠李戴）。
        for c in left:
            assign[c] = (None,
                         [(x2, y2, sg2, DIR[s2]) for s2 in sides
                          for x2, y2, sg2 in _take_side(s2, len(groups[s2]))],
                         None)
        if spare:
            print('  去耦面容量：%d 只电容超出本面脚数，改按"贴脚最近"自由选面'
                  % spare, flush=True)

    # ---------- R2' 独脚电容：和去耦同一条规矩，但判据不写网名
    # 电容的非 GND 网如果**全板只有两个焊盘**（它自己 + 芯片上那一只脚），这只电容
    # 就是给那只脚专用的（VCAP、复位一类）。它和去耦没有本质区别，凭什么不贴脚？
    # 实测代价（411，2026-10-29）：C10(2u2,1206) 落在 U2 下方 3.1mm，中间隔着 GND/NRST
    # 的铜，FreeRouting 把 MCU_VCAP 这根两脚网整根布不出来——板子很空、线很少，唯一
    # 没通的就是它。用户拍板"走不通就调整位置"，正解是把判断做进规则，不是事后挪件。
    def _cap_sig(c):
        """这只电容专用那条网（非 GND 且只有一条）。"""
        sg = [q['net'] for q in meta[c]['pads'] if q['net'] and not is_gnd(q['net'])]
        return sg[0] if len(sg) == 1 else None

    hug, hug_asg = [], {}
    # 候选池不能用 `caps`：`_is_cap` 要求两只焊盘都挂电源/地网，VCAP 这类两头都不占
    # （C10 = MCU_VCAP + GND）根本进不去。这里按"两只焊盘的 C 件"自己认。
    cands = [r for r in parts
             if r[:1] == 'C' and r != hub and r not in cluster
             and len([q for q in meta[r]['pads'] if q['net']]) == 2]
    for c in cands:
        if c in assign:
            continue                          # 已经按电源轨分过的（真去耦）不重复处理
        net = _cap_sig(c)
        if not net:
            continue
        mates = nets.get(net) or []
        if len(mates) != 2:                   # 三个以上焊盘 → 不是"独脚专用"，走 R4
            continue
        other = [rq for rq in mates if rq[0] != c]
        if len(other) != 1 or other[0][0] != hub:
            continue                          # 对面那只不在 hub 上 → 不归这条管
        p = next((q for q in meta[hub]['pads']
                  if q['net'] == net and str(q['num']) == str(other[0][1])), None)
        if p is None:
            continue
        hug_asg[c] = ((p['lx'], p['ly']), [p], DIR[_side(p)])
        hug.append((c, net, other[0][1]))
    # ⚠ **顺序**：专脚电容（VCAP/复位这类，一只电容只伺候一只脚）要排在通用去耦
    # 电容**前面**落地。它和大电容一样"被脚钉死"，可它是 1206 那种大块头，最后落地
    # 就等于捡别人剩下的空位——10-30 mcu_stm32f411 实测 C10 被 C14/C16 顶到 3.6mm 外。
    assign = {**hug_asg, **assign}
    if hug:
        print('  独脚电容贴脚摆：'
              + '、'.join(f'{c}（{net} ↔ {hub}.{num}）' for c, net, num in hug),
              flush=True)

    # ---------- 排布（面在 x=0，其余往西）
    if cluster:
        out_col = sorted(conns)                       # 外列：接插件，贴面
        # 内列排序**必须带 ref 兜底**：`cluster` 是 set，字符串哈希每个进程都不同，
        # 亲和度打平时顺序随机 → 同样输入两次能摆出两块不同的板（实测 FL1/D1 对调，
        # 一次 DRC 干净、一次报重叠）。
        in_col = sorted(cluster - set(conns),
                        key=lambda r: (-sum(aff[r].get(c, 0.0) for c in conns), r))
        w0 = max((SZ[r][0] for r in out_col), default=0.0)
        w1 = max((SZ[r][0] for r in in_col), default=0.0)

        def stack(refs, x_right):
            """沿 y 排成一列，右边缘落在 x_right 上。"""
            total = sum(SZ[r][1] for r in refs) + GAP * max(0, len(refs) - 1)
            y, out = -total / 2, {}
            for r in refs:
                out[r] = (x_right - SZ[r][0] / 2, y + SZ[r][1] / 2)
                y += SZ[r][1] + GAP
            return out

        for r, xy in stack(out_col, 0.0).items():
            pin[r] = xy
        x1_right = -(w0 + GAP)                        # 内列右边缘
        for r, xy in stack(in_col, x1_right).items():
            pin[r] = xy
        hub_left = (x1_right - w1) if in_col else -(w0)
        pin[hub] = (hub_left - GAP - SZ[hub][0] / 2, 0.0)
        face_edge = 0.0
    else:
        pin[hub] = (0.0, 0.0)
        face_edge = None

    hx, hy = pin[hub]
    hw, hh = meta[hub]['w'], meta[hub]['h']

    # 接插件：嘴朝外
    for r in conns:
        aim[r] = {'kind': 'mouth_out', 'dir': [fx, fy]}

    # 电容：落在它盖住的那几只脚旁边，VDD 盘指回芯片
    # hub 的实体范围用**本体包络**（F.Fab），不用 footprint 包围盒：模块的丝印/焊盘
    # 会把包围盒撑得比本体大 2~3mm，按包围盒判会让电容"永远找不到空位"，退化到硬推。
    # hub 障碍物用 courtyard 矩形（比 F.Fab 本体大一圈，DRC 就是按它判重叠）。
    # 计划坐标是"以 courtyard 中心为原点"的，所以矩形要先减掉中心偏移。
    hb = meta[hub].get('crt')
    if hb:                                        # dump 里 crt 已经是"以包络中心为原点"
        hub_rect = (hx + hb[0], hy + hb[1], hx + hb[2], hy + hb[3])
    else:
        hb = meta[hub].get('body')
        hub_rect = (hx + hb[0], hy + hb[1], hx + hb[2], hy + hb[3]) if hb else None

    def _free(c, cx, cy, hgap=GAP):
        """这个落点有没有撞上已钉住的件（含 hub 本体、接口簇）。

        `hgap` 只放宽/收紧"与 hub 之间"这一项：去耦电容允许贴得更近（人力习惯就是
        0.3~0.5mm），件与件之间仍按 GAP 让位。
        """
        for q, (qx, qy) in pin.items():
            if q == hub and hub_rect:
                x0, y0, x1, y1 = hub_rect
                if (cx + SZ[c][0] / 2 + hgap > x0 and cx - SZ[c][0] / 2 - hgap < x1 and
                        cy + SZ[c][1] / 2 + hgap > y0 and cy - SZ[c][1] / 2 - hgap < y1):
                    return False
                continue
            if abs(cx - qx) < (SZ[c][0] + SZ[q][0]) / 2 + GAP and \
               abs(cy - qy) < (SZ[c][1] + SZ[q][1]) / 2 + GAP:
                return False
        return True

    def _tip(p, ux, uy):
        """焊盘中心沿 (ux,uy) 方向的外伸量。

        必须按**出线方向的投影**算：LQFP 脚是 0.3×1.5 的长条，往外伸的是那 1.5，
        取 `w/2` 会低估 0.6mm，电容白让出一档（实测所有块都因此多出 0.6~1mm）。
        焊盘 w/h 是未旋转尺寸，hub 转了 90° 就得换轴。
        """
        pw, ph = p.get('w', 0.6) / 2, p.get('h', 0.6) / 2
        if int(round(meta[hub].get('rot', 0))) % 180 == 90:
            pw, ph = ph, pw
        return abs(ux) * pw + abs(uy) * ph

    # CAPGAP 是模块级常量（为什么要 0.60 见文件头附近），这里不再覆盖
    STEP = 0.1                   # 微步：按 0.7mm 跳档会白让出半档

    # ---------- R3：贴脚落位（"脚位置苛刻"的件先放）
    #
    # 顺序本身是硬规则：**先放被脚钉死的件，再放能挪的去耦电容**。晶体 Y1 的两只
    # 信号脚只接 U1 的 OSC_IN/OSC_OUT（西侧中部那两只），去耦电容却能挂在任何一只
    # VDD 脚旁边。先放电容就等于把晶体要走的走廊占了——实测晶体被挤到芯片北边
    # 7.2mm 外，布线只能绕半圈、打三个孔（10-28）。
    #
    # 找位也不能按"邻居质心"：芯片是个方块，质心在中间，最近的空位就跑到两头去。
    # 规则是"贴具体脚"：目标 = 这些信号网在已摆件上的**焊盘位置**，和去耦贴 VDD
    # 脚是同一条原则。
    def _rot(x, y, deg):
        """KiCad `SetOrientationDegrees` 在这套坐标里的样子（实测 90°: (x,y)→(y,-x)）。"""
        if deg == 90:
            return (y, -x)
        if deg == 180:
            return (-x, -y)
        if deg == 270:
            return (-y, x)
        return (x, y)

    def net_targets(r, placed):
        """r 的每根信号网 → 已摆件上最近的同网焊盘位置（hub 局部坐标）。

        电源/地不算：它们走平面，按它们找位等于没找（每块板都有 GND）。

        ⚠ 试过"优先贴同网小件（晶体/串阻）、hub 兜底"——**实测更差**，已回退：
        负载电容去贴晶体焊盘，可晶体的 courtyard 是 3.6×4.3 的方块，cap 被它整块
        顶出去 2~3mm，落到晶体的**另一侧**；晶体的两根时钟脚在对角，于是线要绕着
        晶体走一圈（PD1 11.6 → 24.1mm，还多了 2 个孔）。按"离 hub 最近"挑，cap
        永远落在芯片和晶体之间那侧，反而不绕。同理"把时钟簇整体先摆"也照样绕。
        参考值：人手原板 PD0 20.66mm/3 孔、PD1 13.85/0；本规则 12.44/0、11.57/0。
        """
        tgt = {}
        for p in meta[r]['pads']:
            n = p['net']
            if not n or is_power(n) or is_gnd(n):
                continue
            best = None
            for q, (qx, qy) in placed.items():
                if q == r or aff[r].get(q, 0.0) <= 0:
                    continue
                for qp in meta[q]['pads']:
                    if qp['net'] != n:
                        continue
                    px, py = qx + qp['lx'], qy + qp['ly']
                    # 同一根网在已摆件上有多个焊盘时取离 hub 最近的那个
                    # （多连接器/总线上的大网，取"最靠里的脚"布线最短）。
                    d = math.hypot(px - hx, py - hy)
                    if best is None or d < best[0]:
                        best = (d, px, py)
            if best:
                tgt[n] = (best[1], best[2])
        return tgt

    def rot_cost(r, tgt, cx, cy, rot):
        """按这个朝向摆时，信号脚到目标脚的距离和。

        朝向必须算进来：3225 晶体的两只信号脚在**长轴**两端，转 90° 才能对着
        芯片竖排的 OSC 脚；只按未旋转的偏移量找位，会把"横着贴"当成唯一解。
        """
        tot = 0.0
        for n, (tx, ty) in tgt.items():
            d = min(math.hypot(cx + _rot(p['lx'], p['ly'], rot)[0] - tx,
                               cy + _rot(p['lx'], p['ly'], rot)[1] - ty)
                    for p in meta[r]['pads'] if p['net'] == n)
            tot += d
        return tot

    def place_rest(refs, max_mm=60.0, corridor=None):
        placed = dict(pin)
        # 排序必须有 ref 兜底：`parts` 是 dict 的键序，稳定但亲和度打平时
        # 不同板的件序会漂；带上 ref 至少同输入同输出。
        refs = sorted(refs, key=lambda r: (-len(meta[r]['pads']),
                                           -meta[r]['w'] * meta[r]['h'], r))

        def clash(cx, cy, w, h):
            for q, (qx, qy) in placed.items():
                # 与 hub 之间可以要一条**逃逸走廊**（见 CORRIDOR）：被脚钉死的件
                # 贴着芯片摆，芯片那一面的脚就再也引不出线了。
                g = max(GAP, corridor) if (corridor and q == hub) else GAP
                if abs(cx - qx) < (w + SZ[q][0]) / 2 + g and \
                   abs(cy - qy) < (h + SZ[q][1]) / 2 + g:
                    return True
            return False

        for r in refs:
            tgt = net_targets(r, placed)
            if not tgt:                       # 没有可贴的脚：退回亲密度质心
                nb = {q: v for q, v in aff[r].items() if q in placed}
                if nb:
                    tw = sum(nb.values())
                    ax = sum(placed[q][0] * v for q, v in nb.items()) / tw
                    ay = sum(placed[q][1] * v for q, v in nb.items()) / tw
                else:
                    ax, ay = hx, hy
            else:
                ax = sum(t[0] for t in tgt.values()) / len(tgt)
                ay = sum(t[1] for t in tgt.values()) / len(tgt)
            step = 0.5
            n = int(max_mm / step)
            # 两档尺寸各搜一遍（未旋转 / 转 90°）。占位尺寸必须和最终朝向一起定：
            # 先按未旋转尺寸留位，就只能选同轴的两个解（晶体被逼着"横着贴"）。
            cand_wh = [(meta[r]['w'], meta[r]['h'], (0, 180))]
            if tgt and abs(meta[r]['w'] - meta[r]['h']) > 0.05:
                cand_wh.append((meta[r]['h'], meta[r]['w'], (90, 270)))
            best = None
            for w, h, rots in cand_wh:
                for i in range(-n, n + 1):
                    for j in range(-n, n + 1):
                        cx, cy = hx + i * step, hy + j * step
                        if cx + w / 2 > (face_edge if face_edge is not None else 1e9):
                            continue                # 不侵占接口面
                        if clash(cx, cy, w, h):
                            continue
                        if tgt:
                            rc = min(rot_cost(r, tgt, cx, cy, rot) for rot in rots)
                            # 贴着那几只脚放；离 hub 只要不飞到板外就行（弱惩罚）
                            cost = rc + 0.1 * math.hypot(cx - hx, cy - hy)
                        else:
                            cost = (math.hypot(cx - ax, cy - ay)
                                    + 0.35 * math.hypot(cx - hx, cy - hy))
                        if best is None or cost < best[0]:
                            best = (cost, cx, cy, w, h)
                    if best and i * step > math.hypot(best[1] - hx, best[2] - hy) + 6:
                        break
            if best is None:
                pin[r] = (ax, ay)
            else:
                pin[r] = (best[1], best[2])
                SZ[r] = (best[3], best[4])
            placed[r] = pin[r]
            # 朝向交给几何去比四向（APPLY 的 `pads` 档），用的是真实焊盘位置。
            if tgt:
                aim[r] = {'kind': 'pads',
                          'nets': [[nm, t[0], t[1]] for nm, t in tgt.items()]}

    # 哪几件"被脚钉死"：信号网里至少有两根直接落在 hub 上（晶体的两根、总线缓冲
    # 的两根）。一根信号网的最多算"按亲密度就近"，不值得抢去耦电容的位置。
    hub_nets = {q['net'] for q in meta[hub]['pads']}

    def _one_hop(n, skip):
        """这根网是不是**经一枚两脚串件**（0Ω/串阻/磁珠）落到 hub 上的。

        mcu_stm32f411 实测（10-29）：网表是 `Y1-3 --XTAL_IN-- R7 --MCU_OSC_OUT-- U2-6`，
        晶体那根线并不直接落在 hub 上。只看"直接落在 hub"的话晶体不算被脚钉死，
        排到去耦电容后面 6.3mm 外（晶振实测 5.4mm、2 个过孔，时钟门直接红）。
        串阻本身也是这条链的一部分，跟着一起进 crit 才对（它要夹在两者中间）。
        """
        for other in parts:
            if other in skip or other == hub:
                continue
            pads = meta[other]['pads']
            if len(pads) != 2:
                continue
            ns = {p['net'] for p in pads}
            if n in ns and (ns & hub_nets):
                return True
        return False

    def _hub_nets(r):
        own = {p['net'] for p in meta[r]['pads']
               if p['net'] and not is_power(p['net']) and not is_gnd(p['net'])}
        return {n for n in own if n in hub_nets or _one_hop(n, {r})}

    crit = [r for r in parts if r not in pin and len(_hub_nets(r)) >= 2]
    if crit:
        print('  被脚钉死先摆：%s（各件 %s）'
              % (','.join(crit), {r: len(_hub_nets(r)) for r in crit}), flush=True)
    # 试过"一跳扩展"（把跟晶体共用信号网的负载电容并进 crit 一起先摆）——实测更差：
    # 两只 cap 被晶体的 courtyard 顶到晶体另一侧，时钟网绕着晶体走圈（PD1 24.1mm/2 孔）。
    # 保持"只摆被两根以上 hub 信号网钉死的件"。
    place_rest(crit, corridor=CORRIDOR)

    def _land(c, seg, ux, uy, px, py, pen_other=1.2):
        """把电容摆到脚段 `seg` 旁，返回 (cost, cx, cy, (dx, dy))；一个面都落不下返回 None。

        四个面各试一次：本侧不罚、别的面罚 `pen_other`（免得电容乱跑到背面去）。
        面内沿法向 0~6.0mm、横移 0~±3.1mm 微步，取第一个不压的空位。
        """
        lbig = max(meta[c]['w'], meta[c]['h'])       # 长轴（顺着出线方向摆）
        lsml = min(meta[c]['w'], meta[c]['h'])
        perp = (-uy, ux)
        best = None
        for dx, dy in [(ux, uy), (-ux, -uy), perp, (-perp[0], -perp[1])]:
            pen = 0.0 if (dx, dy) == (ux, uy) else pen_other
            # ⚠ 每个候选方向都要**先换轴的尺寸再判空隙**：换面了还拿旧尺寸量，
            # 就会把"竖着的电容"当横的塞进去（rs485_node/iso_dcdc 各报 1 处压本体）。
            SZ[c] = (lbig, lsml) if dx else (lsml, lbig)
            # 间距从**焊盘外端**算：LQFP 的焊盘是往外伸的（实测 0.5mm 以上），
            # 只按焊盘中心 + 电容半长算，电容会直接压在焊盘上。
            tip = max(_tip(p, dx, dy) for p in seg)
            base = tip + (SZ[c][0] / 2 if dx else SZ[c][1] / 2) + CAPGAP
            done = False
            for i in range(0, LAND_OUT):
                for j in range(0, LAND_LAT):
                    lat = 0.0 if j == 0 else 0.1 * (j + 1) * (1 if j % 2 else -1)
                    cx = px + dx * (base + i * STEP) + perp[0] * lat
                    cy = py + dy * (base + i * STEP) + perp[1] * lat
                    if not _free(c, cx, cy, CAPGAP):
                        continue
                    cost = math.hypot(cx - px, cy - py) + pen
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, cx, cy, (dx, dy))
                    done = True
                if done:
                    break                          # 这一面已找到空位，不必推更远
        return best

    def _plant(c, seg, ux, uy, px, py, pen_other=1.2):
        """落位 + 兜底。返回 (cost, cx, cy, (dx, dy))；兜底的 cost 带 50 的罚分，
        免得它在"自由选面"的比较里冒充当好落点。"""
        lbig = max(meta[c]['w'], meta[c]['h'])
        lsml = min(meta[c]['w'], meta[c]['h'])
        best = _land(c, seg, ux, uy, px, py, pen_other)
        if best is not None:
            return best
        # 真堵死（iso_dcdc 这种本体温仓把四个面都占满的模块）：沿本侧一路推出去，
        # 推到**真的不压**为止——不能只按已摆件数猜个距离，那会把电容压在本体上。
        SZ[c] = (lbig, lsml) if ux else (lsml, lbig)
        tip = max(_tip(p, ux, uy) for p in seg)
        ext = tip + (SZ[c][0] / 2 if ux else SZ[c][1] / 2) + CAPGAP
        cx, cy = px + ux * ext, py + uy * ext
        for _ in range(120):
            if _free(c, cx, cy, CAPGAP):
                break
            cx, cy = cx + ux * STEP, cy + uy * STEP
        return (math.hypot(cx - px, cy - py) + 50.0, cx, cy, (ux, uy))

    cap_tgt, cap_seg = {}, {}
    for c, (tgt, seg, d) in assign.items():
        if tgt is None:
            # 面容量之外的电容：**四面一视同仁**，拿实测落点挑"贴到哪只脚最近"。
            # 判据是跑一遍落位搜索，不是按面蒙一个距离。
            cand = []
            for tx2, ty2, seg2, (sx, sy) in seg:
                r = _land(c, seg2, sx, sy, hx + tx2, hy + ty2, pen_other=0.0)
                if r is not None:
                    cand.append((r[0], (tx2, ty2), seg2, (sx, sy)))
            cand.sort(key=lambda t: t[0])
            _, tgt, seg, (ux, uy) = cand[0]
            print('  %s 面容量外，自由选面 → 贴 %s'
                  % (c, 'E' if ux > 0 else 'W' if ux < 0
                     else 'S' if uy > 0 else 'N'), flush=True)
        else:
            ux, uy = d
        px, py = hx + tgt[0], hy + tgt[1]
        cap_tgt[c], cap_seg[c] = (px, py), seg
        _, cx, cy, (dx, dy) = _plant(c, seg, ux, uy, px, py)
        # 长轴跟着最终落点方向走（横移后可能换面）
        if abs(dx) >= abs(dy):
            SZ[c] = (max(meta[c]['w'], meta[c]['h']), min(meta[c]['w'], meta[c]['h']))
        else:
            SZ[c] = (min(meta[c]['w'], meta[c]['h']), max(meta[c]['w'], meta[c]['h']))
        pin[c] = (cx, cy)
        aim[c] = {'kind': 'toward', 'target': [px, py], 'vdd': _vdd_pad(meta[c]),
                  'gnd': _gnd_pad(meta[c])}

    # ---------- 电容回修：贪心"先到先得"会让后来的电容捡剩下的位置
    # 411 实测：西面 3 只 3V3 电容被依次顶到 2~5mm 外，谁先落地谁占最好的位置，
    # 而排在前面的未必是最该贴的。这里按"当前离脚最远"的顺序各给一次重落位机会：
    # 把它自己从 `pin` 里摘掉（别把自己的旧位置当障碍），在它那只脚周围四面重搜，
    # **只有真的变近了才接受**。改完仍超限的就是真没地方，交给 cap_report 报出来。
    def _cap_gap(c):
        gx, gy = cap_tgt[c]
        x, y = pin[c]
        d0 = math.hypot(x - gx, y - gy)
        if d0 < 1e-6:
            return 0.0
        ux, uy = (gx - x) / d0, (gy - y) / d0
        p = cap_seg[c][0]
        pw, ph = p.get('w', 0.6) / 2, p.get('h', 0.6) / 2
        if int(round(meta[hub].get('rot', 0))) % 180 == 90:
            pw, ph = ph, pw
        w, h = SZ[c]
        return d0 - (abs(ux) * w / 2 + abs(uy) * h / 2) - (abs(ux) * pw + abs(uy) * ph)

    caps_l = [c for c in cap_tgt if c in pin]
    for _round in range(4):
        caps_l.sort(key=lambda c: -_cap_gap(c))
        changed = False
        for c in caps_l:
            before = _cap_gap(c)
            if before <= CAPGAP + 0.15:
                continue
            old = pin.pop(c)
            r = None
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                rr = _land(c, cap_seg[c], dx, dy, *cap_tgt[c], pen_other=0.0)
                if rr and (r is None or rr[0] < r[0]):
                    r = rr
            if r is not None:
                SZ[c] = ((max(meta[c]['w'], meta[c]['h']),
                          min(meta[c]['w'], meta[c]['h'])) if r[3][0]
                         else (min(meta[c]['w'], meta[c]['h']),
                               max(meta[c]['w'], meta[c]['h'])))
                pin[c] = (r[1], r[2])
            else:
                pin[c] = old
            if _cap_gap(c) < before - 1e-6:
                changed = True
        if not changed:
            break

    # 其余件（含负载电容）现在放：它们能贴到的脚这时都已在 pin 里。
    place_rest([r for r in parts if r not in pin])

    # 接插件的朝向：
    # - 有嘴的（USB 座、接线端子）：嘴朝外是硬约束；同向的解再按焊盘距离挑。
    # - 单排排针：没有嘴，脚排平行于边这条已经由预留方框（SZ）锁死，剩下的
    #   0°/180° 由"脚到目标脚的距离和"决定——这就是 J3 竖起来线更短的那条规则。
    for r in conns:
        tgt = net_targets(r, dict(pin))
        nets_l = [[n, t[0], t[1]] for n, t in tgt.items()]
        if r in flat_conn:
            aim[r] = {'kind': 'pads', 'nets': nets_l}
        else:
            aim[r] = {'kind': 'mouth_out', 'dir': [fx, fy], 'nets': nets_l}

    # ---------- 推开残余重叠（电容/接插件/hub 不动）
    fixed = set(assign) | set(conns) | {hub}
    for _ in range(300):
        moved = False
        rs = [r for r in pin]
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                ox = (SZ[a][0] + SZ[b][0]) / 2 + GAP - abs(pin[a][0] - pin[b][0])
                oy = (SZ[a][1] + SZ[b][1]) / 2 + GAP - abs(pin[a][1] - pin[b][1])
                if ox <= 0 or oy <= 0:
                    continue
                if a in fixed and b in fixed:
                    continue
                if ox < oy:
                    d = ox / 2
                    s = 1 if pin[a][0] < pin[b][0] else -1
                    if a not in fixed:
                        pin[a] = (pin[a][0] - s * d, pin[a][1])
                    if b not in fixed:
                        pin[b] = (pin[b][0] + s * d, pin[b][1])
                else:
                    d = oy / 2
                    s = 1 if pin[a][1] < pin[b][1] else -1
                    if a not in fixed:
                        pin[a] = (pin[a][0], pin[a][1] - s * d)
                    if b not in fixed:
                        pin[b] = (pin[b][0], pin[b][1] + s * d)
                moved = True
        if not moved:
            break

    # ---------- 归一到 (2,2)
    minx = min(pin[r][0] - SZ[r][0] / 2 for r in pin)
    miny = min(pin[r][1] - SZ[r][1] / 2 for r in pin)
    out = {}
    for r in pin:
        x, y = pin[r][0] - minx + 2.0, pin[r][1] - miny + 2.0
        # aim 的 target 是 hub 局部坐标推出来的全局量，同样要平移
        a = aim.get(r)
        if a and a.get('kind') == 'toward':
            a = dict(a, target=[a['target'][0] - minx + 2.0, a['target'][1] - miny + 2.0])
        elif a and a.get('nets'):
            a = dict(a, nets=[[n, tx - minx + 2.0, ty - miny + 2.0]
                              for n, tx, ty in a['nets']])
        out[r] = {'x': round(x, 3), 'y': round(y, 3), 'aim': a,
                  'wh': [round(SZ[r][0], 3), round(SZ[r][1], 3)]}
    return out, {'hub': hub, 'connectors': sorted(conns), 'cluster': sorted(cluster),
                 'caps': sorted(assign), 'face': face, 'size': SZ}


def _rt(x, y, deg):
    """与 plan 里 `_rot` 同一套（KiCad 90°: (x,y)→(y,-x)）。"""
    if deg == 90:
        return (y, -x)
    if deg == 180:
        return (-x, -y)
    if deg == 270:
        return (-y, x)
    return (x, y)


def port_face(meta, pos, size, ports, skip=()):
    """口岸脚朝外：**只调朝向，不动位置**。

    口岸 = 块的对外接口脚（语言里 `ifaces` 的网）。拼板时邻居块的线要从块边界一路
    接到这只脚，所以**这只脚得在"交通"的外侧**：串阻的另一只脚接着块内网，块内那
    根线要过来必然贴着口岸脚旁边过（实测 usb 块 R3-2 被自家 `Net-(J1-D-)` 的线压在
    0.242mm，正好卡在 DRC 允许的边缘，够不着拼板线要的 0.65mm）。

    判据 = **非口岸脚朝着它自己那根网要去的地方，口岸脚就自然被推到外侧**。只看
    口岸脚自己有多"空"分不出好坏：实测 R3 两个朝向离邻居都是 0.70（左右邻居一样
    近），射线净空也都是 0.70；而"兄弟脚朝不朝着 J1"一眼就分开了（6.20 边 3.9mm
    vs 7.85 边 2.2mm）。全是电源/地的兄弟脚没有目标可朝，退化成"口岸脚离邻居越远
    越好"。
    只转朝向的原因是：两脚被动件的 0°/180° 包络完全相同（courtyard 对称），翻过来
    一个像素都不多占，所以这条规则不会和"件间净距"打架。细间距大件（>6 脚）不动
    —— 排脚在四周，翻转没有意义。
    """
    if not ports:
        return []
    out = []
    for r, m in meta.items():
        if r not in pos or r in skip or len(m['pads']) > 6:
            continue
        pn = [p for p in m['pads'] if p['net'] in ports]
        if not pn:
            continue
        on = [p for p in m['pads'] if p['net'] not in ports]
        # 网 → **别的件**上的同网焊盘坐标（"兄弟脚要去的地方"；不含本件自己）
        tmap = {}
        for q, qm in meta.items():
            if q == r or q not in pos:
                continue
            for p in qm['pads']:
                n = p['net']
                if n and not is_power(n) and not is_gnd(n):
                    tmap.setdefault(n, []).append((pos[q]['x'] + p['lx'],
                                                   pos[q]['y'] + p['ly']))
        w, h = size[r]
        if abs(w - m['w']) < 0.15 and abs(h - m['h']) < 0.15:
            rots = (0, 180)
        elif abs(w - m['h']) < 0.15 and abs(h - m['w']) < 0.15:
            rots = (90, 270)
        else:
            continue                       # 两档都对不上：朝向已被别的理由锁死
        boxes = [(pos[q]['x'] - size[q][0] / 2 - GAP, pos[q]['y'] - size[q][1] / 2 - GAP,
                  pos[q]['x'] + size[q][0] / 2 + GAP, pos[q]['y'] + size[q][1] / 2 + GAP)
                 for q in pos if q != r]
        cx, cy = pos[r]['x'], pos[r]['y']

        def gap_at(p, rot):
            """这只脚（按这个朝向转完）离最近的别的件的矩形多远。"""
            ox, oy = _rt(p['lx'], p['ly'], rot)
            x, y = cx + ox, cy + oy
            return min((math.hypot(max(b[0] - x, 0.0, x - b[2]),
                                   max(b[1] - y, 0.0, y - b[3])) for b in boxes),
                       default=PORT_RAY)

        def score(rot):
            sib = 0.0
            for p in on:
                n = p['net']
                if not n or n not in tmap:   # 电源/地、或只挂在本件上的网：没有目标可朝
                    continue
                ox, oy = _rt(p['lx'], p['ly'], rot)
                x, y = cx + ox, cy + oy
                sib += min(math.hypot(x - tx, y - ty) for tx, ty in tmap[n])
            return (-sib, min(gap_at(p, rot) for p in pn))

        cur = score(rots[0])
        best = max(((score(rot), rot) for rot in rots), key=lambda t: (t[0], -t[1]))
        out.append((r, rots[0], cur[1], best[1], best[0][1]))
        if best[0] > cur:
            pos[r]['aim'] = {'kind': 'rot', 'rot': best[1]}
    return out


def _vdd_pad(m):
    for p in m['pads']:
        if p['net'] and is_power(p['net']) and not is_gnd(p['net']):
            return p['num']
    return m['pads'][0]['num'] if m['pads'] else '1'


def _gnd_pad(m):
    for p in m['pads']:
        if p['net'] and is_gnd(p['net']):
            return p['num']
    return m['pads'][-1]['num'] if m['pads'] else '2'


# ============================================================ 体检
def report(meta, plan_pos, info):
    """摆完就地量一次：每只电源脚到最近的同网去耦电容焊盘。"""
    SZ = info.get('size') or {r: (m['w'], m['h']) for r, m in meta.items()}
    capset = set(info['caps'])
    rows = []
    hub = info['hub']
    hx, hy = plan_pos[hub]['x'], plan_pos[hub]['y']
    for p in meta[hub]['pads']:
        if not (p['net'] and is_power(p['net']) and not is_gnd(p['net'])):
            continue
        px, py = hx + p['lx'], hy + p['ly']
        ds = []
        for c in capset:
            for q in meta[c]['pads']:
                if q['net'] != p['net']:
                    continue
                # 口径与体检门禁一致：**焊盘中心到焊盘中心**。
                # ⚠ 旧版减的是"电容整颗半对角 + 脚半对角"，比真值少 1.4mm 左右，
                # 报"0.29mm"其实板上是 2.6mm——自欺的门禁比没有门禁更糟。
                # 电容转到哪个角度未知，但"电容中心 → 该网焊盘"的距离旋转不变，可以照用。
                cx, cy = plan_pos[c]['x'], plan_pos[c]['y']
                ds.append((math.hypot(px - cx, py - cy) - math.hypot(q['lx'], q['ly']), c))
        if ds:
            d, c = min(ds)
            rows.append((p['num'], p['net'], round(d, 2), c))
    rows.sort(key=lambda r: -r[2])
    return rows


# 去耦电容**自己**外框到它那只脚焊盘边缘的净距上限 mm。
# 贴脚摆的实测值 ≈ CAPGAP = 0.6mm（与块大小无关——所以量外框到焊盘边缘，不量中心距，
# 否则 1206 和 0402 的"正常值"差 1.8mm，一把尺子量不了两种件）。
CAPFAR_MM = 2.2


def cap_report(meta, plan_pos, info):
    """电容侧的量法：每只去耦电容到**它自己被分到的那只脚**的中心距。

    ⚠ 只量"每只脚到最近电容"是量不全的：一只电容可以被同面的兄弟盖住那只脚，
    自己却漂到 10mm 外占地方，脚侧的尺子**永远是绿的**。10-30 mcu_stm32f411 就是
    这样：C14 被塞进已经满员的西面、四个方向搜不到空位后掉进兜底，一路推到
    10.73mm 外、越过晶体贴在板子最左边，离它要伺候的 U2-9 有 6.5mm；而脚侧的尺子
    报"最远 4.00mm（C13）"完全看不见它。
    """
    SZ = info.get('size') or {r: (m['w'], m['h']) for r, m in meta.items()}
    hub = info['hub']
    hx, hy = plan_pos[hub]['x'], plan_pos[hub]['y']
    hpads = meta[hub]['pads']
    rows = []
    for c in sorted(set(info['caps'])):
        ent = plan_pos.get(c)
        if not ent:
            continue
        t = (ent.get('aim') or {}).get('target')
        if not t:
            continue
        # 目标点 → hub 上最近的那只焊盘（aim 里记的是点，不是编号）
        pad = min(hpads, key=lambda q: math.hypot(hx + q['lx'] - t[0],
                                                  hy + q['ly'] - t[1]))
        gx, gy = hx + pad['lx'], hy + pad['ly']
        x, y = ent['x'], ent['y']
        d0 = math.hypot(x - gx, y - gy)
        if d0 < 1e-6:
            rows.append((c, 0.0))
            continue
        ux, uy = (gx - x) / d0, (gy - y) / d0
        w, h = SZ[c]
        half_cap = abs(ux) * w / 2 + abs(uy) * h / 2      # 电容外框在"指向脚"上的半长
        pw, ph = pad.get('w', 0.6) / 2, pad.get('h', 0.6) / 2
        if int(round(meta[hub].get('rot', 0))) % 180 == 90:
            pw, ph = ph, pw
        half_pad = abs(ux) * pw + abs(uy) * ph
        rows.append((c, round(d0 - half_cap - half_pad, 2)))
    rows.sort(key=lambda r: -r[1])
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description='规则摆件（一面对外 + 去耦贴脚）')
    ap.add_argument('--board', required=True)
    ap.add_argument('--card', default='', help='块卡 *.table.json（当前不用：接口簇从网表推）')
    ap.add_argument('--out', default=None)
    ap.add_argument('--face', default='E', choices=sorted(FACE))
    ap.add_argument('--gap', type=float, default=None, help='件间净距 mm（默认 0.45，布线吃紧就放大）')
    ap.add_argument('--ports', default='',
                    help='口岸网名（逗号分隔）：这些网上的脚要朝块外，射线不许被自家件挡')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    if a.gap is not None:
        global GAP
        GAP = a.gap
    global PORTS
    PORTS = {s for s in a.ports.split(',') if s}

    meta = json.loads(_kpy(DUMP % {'board': a.board}))
    nets = netmap(meta)
    aff = affinity(meta, nets)
    pos, info = plan(meta, nets, aff, face=a.face)

    pf_rows = port_face(meta, pos, info['size'], PORTS, skip=set(info['connectors']))
    if PORTS and pf_rows:
        for r, r0, c0, r1, c1 in pf_rows:
            print('  口岸脚 %-4s %d°→%d°  离别的件 %.2f→%.2fmm'
                  % (r, r0, r1, c0, c1), flush=True)
        print('口岸脚朝外：%d 只带口岸脚的件，翻朝向 %d'
              % (len(pf_rows), sum(1 for t in pf_rows if t[3] != t[1])), flush=True)

    print('hub=%s 面=%s 接插件=%s 接口簇=%s 去耦=%s'
          % (info['hub'], info['face'], ','.join(info['connectors']) or '—',
             ','.join(info['cluster']) or '—', ','.join(info['caps']) or '—'), flush=True)
    for r in sorted(pos):
        print('  %-4s @ (%6.2f, %6.2f)  %-6s %s'
              % (r, pos[r]['x'], pos[r]['y'],
                 '%.2f×%.2f' % info['size'][r],
                 (pos[r]['aim'] or {}).get('kind', '')), flush=True)
    rows = report(meta, pos, info)
    if rows:
        print('去耦（电源脚→同网电容焊盘，中心距）: 最远 %s-%s %.2fmm（%s）'
              % (info['hub'], rows[0][0], rows[0][2], rows[0][3]))
    stray = cap_report(meta, pos, info)
    if stray:
        c, d = stray[0]
        far = [r for r in stray if r[1] > CAPFAR_MM]
        print('去耦电容侧（电容外框→它那只脚焊盘边缘，净距）: 最远 %s %.2fmm，'
              '%d/%d 只超 %.1fmm%s'
              % (c, d, len(far), len(stray), CAPFAR_MM,
                 '' if not far else '（%s）' % '、'.join(x[0] for x in far)),
              flush=True)
    if a.dry_run or not a.out:
        return 0

    pf = os.path.join(os.path.dirname(os.path.abspath(a.out)), '_rule_plan.json')
    json.dump(pos, open(pf, 'w'), ensure_ascii=False, indent=1)
    print(_kpy(APPLY % {'board': a.board, 'plan': pf, 'out': a.out}).strip(), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
