"""平面网焊盘前置扇出：布线开跑前，把每个「不在平面层上」的平面网 SMD
焊盘用 过孔 + 短引线 落到平面，并在落孔位放一块 keepout 规则区占位。

为什么必须前置（ESP32S3_TestBoard v1 实测）：布线完成后再缝
（`planes.run_planes_pass` 的 stitch）是在一块已被信号走线占满的板上找净空。
SOT-23-6 的中间 GND 脚（U3-2）左侧被 USB_DM/DP 走线、右侧被 +5V 走线、
上下被邻脚夹死，实测走廊 20–23µm —— 0.2mm 孔的过孔也放不下，链在
`un=3` 卡死。前置时障碍物只剩焊盘，空间充裕；keepout 规则区让
FreeRouting 在布信号时绕开这些预留位（DSN 里导出成 `(keepout ...)`，
与 NPTH 禁区同一机制）。

时序（run_pipeline）::

    放置板 → [plan 找位] → [keepout 占位] → signal → power → finish
           → [落铜 + 删 keepout] → planes（铺铜后自动连通）

为什么不提前落铜：`signal`/`power` 的 DSN 会整棵删除平面网（`del_nets`），
网里的 via/wire 跟着消失 → FreeRouting 看不见它们，会把信号线布在上面。
keepout 不隶属任何网，删网删不掉，是整个 FR 阶段唯一可靠的占位手段。
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from . import env

TRACK_W = 200000        # 0.2mm 引线（与板规 min_track_width 一致）
PATH_CLR = 300000       # 引线中心到异网铜 = 半线宽 0.1 + 间距 0.2
KO_MARGIN = 50000       # keepout 半径 = 过孔半径 + 间距 + 余量
KO_NAME = "FANOUT_KEEPOUT"

#: 逃逸走廊保护（2026-10-29）：落孔位的 keepout 圆**不许碰到非平面网焊盘的
#: 出脚走廊**。0.5mm 脚距的 QFN 里这条是硬伤——keepout 半径 = 过孔半径 0.3 +
#: 0.2 间距 + 0.05 余量 = 0.55mm，比相邻两脚的中心距 0.5mm 还大。所以给"隔一个
#: 脚"的平面脚在正下方落孔时，圆边必然越过邻居的逃逸道。
#:
#: mcu_stm32f411 实测（10-29）：U2.23(GND) 的孔落在 (15.957,15.922)，离
#: U2.22(MCU_VCAP) 的逃逸道中心线 0.522mm，而需要 0.55 + 半线宽 0.1 = 0.65mm
#: → 差 0.128mm。后果是 FreeRouting **四个 pass 全部放弃 MCU_VCAP**（自查日志
#: `Net 'MCU_VCAP' (1 unrouted connection): C10-1 -> U2-22`），块板门禁卡在
#: 「未连通 1」不绿。
#:
#: 三组对照实验钉死了归因（都喂同一个 jar、同参数）：
#:   ① 把 C10 挪到板子另一角 → 照样不通（**改 placement 救不了**，堵点在 MCU 脚边）
#:   ② 清空板上所有既有铜（纯干净板）→ 照样不通（不是拥挤）
#:   ③ 只摘掉 (15.957,15.922) 这一个 keepout → **立刻布通**，未布通 36→33
#:      摘掉全部 66 个 → 未布通 36→3，耗时 36s→4.4s
#:
#: 走廊长度取 1.2mm：够 FR 把信号脚送出密集脚场，又不必把落孔位推到板中央。
ESC_LANE = 1200000

# ---- 散热盘打孔（2026-10-29，同日改口径）-------------------------------------
# 大面积同网焊盘（QFN 的 EP、功率管散热焊盘）面积 >= THERMAL_PAD_MM2 就算"散热盘"，
# 孔按 THERMAL_PITCH 排网格、落在焊盘内（via-in-pad），不拉引线。
#
# ⚠ 2026-10-29 用户拍板：**散热不靠 PCB 过孔，靠芯片上贴散热片**。所以这里的
# 孔数只保"电气接地"下限（1 枚），不再按面积加码——原口径 max(4, 面积/4) 在
# 411 的 31.4mm² EP 上要 7 枚。少打孔 = 少一处树脂塞孔工艺，且 EP 被 48 只
# 0.5mm 脚围死、出不去，盘内打孔是唯一通路，能少一枚就少一枚。
# 电气上 EP 仍必须接 GND（STM32 QFN 的 EP 是 VSS），1 枚孔接进 In1 平面即满足。
#
# ⚠ 必须在**这个**模块里打孔，不能在流水线前面（如块级 blockpcb）提前落铜：
# SES 回灌会把平面网的 via/wire 整棵重放，布线前落的孔到第一轮就没了——实测
# mcu_stm32f411 布线前落 9 枚、priority_000 里 0 枚。本模块的落铜本来就排在
# 所有 FR pass 之后（见文件头时序说明），孔才留得住。
THERMAL_PAD_MM2 = 4.0   # mm²，多大算散热盘
THERMAL_FLOOR = 1       # 每只散热盘的下限孔数（电气接地用；散热交给芯片上的散热片）
THERMAL_PITCH = 1.2     # mm，孔网格节距
THERMAL_INSET = 0.15    # mm，孔盘边到焊盘边

#: 落孔策略（同一块板依次试，取"没落孔位的脚最少"那一个）。
#: 为什么要试：0.5mm 脚距的一列平面网脚只有**一条**朝外走廊，谁先规划谁占近处，
#: 后一只的引线就会从它盘边 0.5mm 处擦过（要 0.6mm）→ 死脚。实测 mcu_stm32f411
#: U2-8(GND) 先占了 0.8mm 处，U2-9(3V3) 53 个可行落点全被判死。交错远近能岔开。
STRATS = ("near", "far", "alt")

#: 过孔阶梯（diam_nm, drill_nm）：先 0.6/0.3，挤不下退 0.5/0.3。
#: 钻孔**不许低于板规 min hole**（0.3mm，KiCad 板设置里就有）。曾经第二档是
#: 0.5/0.2，在块里从没挤到过所以没暴露；10-27 拼板时有 2 枚退到该档，DRC 报
#: `drill_out_of_range`——而它不在铜级清单里，门禁看不见。
VIAS = [(600000, 300000), (500000, 300000)]

_GEOM = r'''
import math

pads, tracks = [], []
for fp in b.GetFootprints():
    for pad in fp.Pads():
        pads.append(pad)
for t in b.GetTracks():
    tracks.append(t)
_pad_net = [p.GetNetname() for p in pads]
_trk_net = [t.GetNetname() for t in tracks]

def _hits(x, y, net, m, layer=None):
    """layer=None → 全层障碍（过孔孔壁贯穿叠层，必须全层避让）；
    给 layer → 只看该层铜（F.Cu 引线不该被 B.Cu 走线挡住）。

    ⚠ **无网焊盘（net == ''）是实打实的铜，必须当障碍**。曾经这里写成
    `_pad_net[i] in (net, '')` 直接跳过，等于把无网焊盘当空气——10-27 拼板
    实测 4 条 `shorting_items`（+3.3V/GND 过孔和引线正落在 U1 的无网 NC 焊盘
    上）。块内流水线给 NC 脚挂过临时网名，所以从未暴露；拼板后临时网被剥掉，
    洞就露出来了。

    ⚠ **只有钢网/阻焊层的焊盘不是铜**。QFN 的 EP 常把锡膏开窗做成许多 paste-only
    小焊盘（mcu_stm32f411 的 U2 有 16 块 1.13mm），它们落在 EP 正中间、又带临时
    网名，曾被当成异网铜把整块散热盘打成禁区（7 枚孔一枚也落不下去）。"""
    pt = pcbnew.VECTOR2I(int(x), int(y))
    for i, pad in enumerate(pads):
        if _pad_net[i] == net:          # 只跳过**同网**焊盘（'' 不在此列）
            continue
        if not (pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.B_Cu)):
            continue                    # 钢网/阻焊开窗：不是铜，不挡路
        if layer is not None and not pad.IsOnLayer(layer):
            continue
        if pad.HitTest(pt, m):
            return ('pad', _pad_net[i])
    for i, t in enumerate(tracks):
        if _trk_net[i] in (net, ''):
            continue
        if layer is not None and t.GetLayer() != layer:
            continue
        if t.GetEffectiveShape().Collide(pt, m):
            return ('track', _trk_net[i])
    return None

# 已落孔位（同名网焊盘互相看不见，会叠在同一处 → holes_co_located）
CHOSEN = []

def _hole_ok(x, y, vd):
    """孔间间距：0.25mm 钻孔边距对所有钻孔成立，**同网也不豁免**。
    _hits 按网名跳过同网焊盘，曾让 GND 过孔贴着 U1 的 GND 通孔引脚落孔
    （hole_to_hole 0.05mm）。这里单独按钻孔半径判，网名不参与。"""
    for i, pad in enumerate(pads):
        d = pad.GetDrillSize()
        hr = max(d.x, d.y) / 2
        if hr <= 0:
            continue
        c = pad.GetPosition()
        if math.hypot(x - c.x, y - c.y) < hr + vd // 2 + 260000:
            return False
    for t in tracks:
        if not isinstance(t, pcbnew.PCB_VIA):
            continue
        c = t.GetPosition()
        if math.hypot(x - c.x, y - c.y) < t.GetDrillValue() / 2 + vd // 2 + 260000:
            return False
    for c in CHOSEN:
        if math.hypot(x - c['x'], y - c['y']) < c['vd'] // 2 + vd // 2 + 260000:
            return False
    return True

def _seg_dist(x, y, x0, y0, x1, y1):
    """点到线段距离（nm），给"新过孔/新引线 vs 老引线"用。"""
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(x - x0, y - y0)
    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / L2))
    return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))

def _lead_seg_clear(x, y, r):
    """新过孔盘 vs 已落孔位的引线（铜对铜要 0.2mm 间距）。"""
    for c in CHOSEN:
        pts = [c['px']]        # 焊盘中心（元组，不是 px/py 两个键）
        if c['elbow']:
            pts.append(c['elbow'])
        pts.append((c['x'], c['y']))
        for a, b_ in zip(pts, pts[1:]):
            if _seg_dist(x, y, a[0], a[1], b_[0], b_[1]) < r + TRACK_W / 2 + 200000:
                return False
    return True

LANE = %%LANE%%          # 逃逸走廊长度（nm），见 fanout.ESC_LANE
KO_MARGIN = %%KO_MRG%%   # keepout 余量：keepout 半径 = clr + 它
LANE_REJ = [0]           # 被走廊门禁挡掉的候选数（打印用，规则必须有数字）

def _esc_dir(pad):
    """焊盘出脚方向 = 离开本体中心的主轴方向。

    QFN/QFP 一整排的脚朝同一方向出，取 dx/dy 里大的那个轴就够；落在本体中心
    上的焊盘（EP、单盘封装）返回 None，不设走廊。"""
    fc = pad.GetParentFootprint().GetPosition()
    c = pad.GetPosition()
    dx, dy = c.x - fc.x, c.y - fc.y
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return (1 if dx > 0 else -1, 0)
    return (0, 1 if dy > 0 else -1)

def _lane_ok(x, y, net, ko_r):
    """落孔位的 keepout 圆不许碰到**别人出脚走廊**。

    被保护的焊盘 = 有铜、非本网、非平面网、非临时 NC 网、非通孔焊盘——也就是
    "FR 还得给它拉线、且拉线只能从焊盘正面出去"的那些脚。平面网焊盘彼此之间
    的落孔冲突由 _hole_ok / _lead_ok 管，不在这里重复判。

    判定距离 = ko_r + 半线宽：keepout 圆边缘碰到别人的走线中心线就算挡死。"""
    need = ko_r + TRACK_W / 2
    for i, pad in enumerate(pads):
        n = _pad_net[i]
        if n == net or not n or n in plane_nets or n.startswith('__NC'):
            continue
        if not (pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.B_Cu)):
            continue
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            continue
        d = _esc_dir(pad)
        if d is None:
            continue
        c = pad.GetPosition()
        if abs(c.x - x) > LANE + need or abs(c.y - y) > LANE + need:
            continue                       # 远场直接跳过，省掉逐点距离
        if _seg_dist(x, y, c.x, c.y,
                     c.x + d[0] * LANE, c.y + d[1] * LANE) < need:
            LANE_REJ[0] += 1
            return False
    return True

THERMALS = []      # 大面积焊盘（QFN 的 EP 等）；由 _PLAN_TMPL 填，见那里的注释

def _in_thermal(x, y, r):
    """这个落孔位的孔盘会不会碰到散热盘。

    **散热盘不许当过孔落脚点**（用户 2026-10-30 第二次强调）。物理原因：盘里
    打过孔，回流焊时焊锡顺孔壁往下漏，盘下留空洞，接地和散热一起变差。

    此前这里不判，于是 QFN 四角的平面网脚把孔**停进 EP 里**——同网、无间距要求、
    引线最短，规划器当然觉得最划算。10-30 mcu_stm32f411 实测：散热盘里 5 枚孔，
    其中 4 枚（U2-8/23/35/47）就是这么"偷懒"落进去的，只有 1 枚是 `_thermal`
    有意放的接地缝合孔。散热盘自己的缝合孔不走 `_spot_ok`，所以不在此列。"""
    if not THERMALS:
        return False
    p = pcbnew.VECTOR2I(int(x), int(y))
    for tp in THERMALS:
        if tp.HitTest(p, int(r)):
            return True
    return False

def _spot_ok(x, y, net, clr, layer, vd, ko_r=None):
    return (_hits(x, y, net, clr, layer) is None and _hole_ok(x, y, vd)
            and not _in_thermal(x, y, vd // 2)
            and _lead_seg_clear(x, y, vd / 2)
            and (ko_r is None or _lane_ok(x, y, net, ko_r)))

def _lead_ok(x, y):
    """引线扫过**已落孔位**的过孔盘时要留住间距。

    孔到孔只判钻头边距（`_hole_ok` 的 0.26mm），可引线是**铜**：一条 0.2mm 的
    引线横穿到老过孔盘边 0.1mm 处，DRC 就是 clearance 违规。10-27 MCU 块实测
    +3.3V 引线 y=12.545 横过 GND 过孔 (10.838,12.045)，间距正好 0.1mm——两个
    落孔位中心距 0.86mm 刚好过 `_hole_ok`，引线却贴着另一个过孔走。
    此处按 过孔半径 + 半线宽 + 0.2mm 间距 判，宁可这一枚退回 inpad/stitch。"""
    for c in CHOSEN:
        if math.hypot(x - c['x'], y - c['y']) < c['vd'] / 2 + TRACK_W / 2 + 200000:
            return False
    return True

def path_free(x0, y0, x1, y1, net, pad_layer):
    n = max(2, int(math.hypot(x1 - x0, y1 - y0) / 40000) + 1)
    for i in range(n + 1):
        t = i / n
        px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        if _hits(px, py, net, PATH_CLR, pad_layer) or not _lead_ok(px, py):
            return False
    return True

DIRS = [(math.cos(i * math.pi / 16), math.sin(i * math.pi / 16)) for i in range(32)]
CANDS = [(int(cx * r), int(cy * r)) for r in range(400000, 6100000, 100000)
         for cx, cy in DIRS]
CANDS_NEAR = CANDS                 # 近到远（缺省）
CANDS_FAR = list(reversed(CANDS))  # 远到近（策略 far / alt 用）

def find_straight(c, net, clr, layer, vd):
    """落点按**全层**避让（过孔是通孔），引线只看自己那层。
    10-27 拼板实测：只看焊盘所在层时，F.Cu 上的 +3.3V 过孔正落在 B.Cu 的
    `Net-(J3-Pin_3)` 走线上——一条 `shorting_items`。"""
    for dx, dy in CANDS:
        x, y = c.x + dx, c.y + dy
        if _spot_ok(x, y, net, clr, None, vd, clr + KO_MARGIN) and \
                path_free(c.x, c.y, x, y, net, layer):
            return (x, y)
    return None

def find_elbow(c, net, clr, layer, vd):
    """L 形引线：直线被邻脚夹死时，从焊盘先沿 x（或 y）出，再拐到落点。"""
    for dx, dy in CANDS:
        x, y = c.x + dx, c.y + dy
        # 落点全层避让（通孔）+ 不许压别人出脚走廊
        if not _spot_ok(x, y, net, clr, None, vd, clr + KO_MARGIN):
            continue
        for (ex, ey) in ((x, c.y), (c.x, y)):
            if path_free(c.x, c.y, ex, ey, net, layer) and path_free(ex, ey, x, y, net, layer):
                return (x, y, ex, ey)
    return None

def via_in_pad(pad, net, r):
    """最后手段：过孔盘整个落在 SMD 焊盘内（无引线，孔壁直达平面）。"""
    c = pad.GetPosition(); sz = pad.GetSize()
    if 2 * r > min(sz.x, sz.y):
        return None
    long_x = sz.x >= sz.y
    off = int(max(sz.x, sz.y) / 2 - r - 20000)
    for o in (0, off, -off):
        x = c.x + (o if long_x else 0)
        y = c.y + (0 if long_x else o)
        inside = all(pad.HitTest(pcbnew.VECTOR2I(int(x + ix), int(y + iy)), 1000)
                     for ix, iy in ((r, 0), (-r, 0), (0, r), (0, -r)))
        if inside and _spot_ok(x, y, net, r + 200000, None, 2 * r):
            return (x, y)
    return None

def choose(pad, net):
    c = pad.GetPosition()
    layer = pad.GetLayer()
    for vd, dr in VIAS:
        clr = vd // 2 + 200000
        spot = find_straight(c, net, clr, layer, vd)
        if spot:
            return {'x': spot[0], 'y': spot[1], 'vd': vd, 'dr': dr,
                    'elbow': None, 'how': 'direct'}
        el = find_elbow(c, net, clr, layer, vd)
        if el:
            return {'x': el[0], 'y': el[1], 'vd': vd, 'dr': dr,
                    'elbow': [el[2], el[3]], 'how': 'elbow'}
    vd, dr = VIAS[-1]
    spot = via_in_pad(pad, net, vd // 2)
    if spot:
        return {'x': spot[0], 'y': spot[1], 'vd': vd, 'dr': dr,
                'elbow': None, 'how': 'inpad'}
    return None
'''

_PLAN_TMPL = r'''
import json
import wx
wx.App(False)   # board/net SWIG 调用需 wxApp 守卫
import pcbnew
b = pcbnew.LoadBoard(%%IN%%)
plane_nets = set(%%NETS%%)
PATH_CLR = %%PATH_CLR%%
TRACK_W = %%TRK_W%%
VIAS = %%VIAS%%
# 上一批（布线前早占位）已经订下的落孔位：本批必须让开，否则两批落孔互相
# 打架——10-29 mcu_stm32f103 实测：早占位给 U1-9(+3.3V) 订了 (9.5875,12.54)，
# 后一批给 U1-8(GND) 又订了 (9.5875,12.04)，两孔只隔 0.5mm → 3 条
# shorting_items + 2 条 clearance + hole_to_hole。规划时眼里只有焊盘和走线，
# 看不到"别人已经订下、但铜还没落"的孔位。
RESERVED = %%RESERVED%%
SKIP = %%SKIP%%          # {(ref, num)} 已经规划过的脚，本轮不再规划
TIGHT = %%TIGHT%%        # None=全都规划 / True=只要细间距引脚场 / False=只要其余
STRAT = %%STRAT%%        # near=近到远 / far=远到近 / alt=按脚序交错
CFG = dict(pad_mm2=%%PAD_MM2%%, floor=%%FLOOR%%, pitch=%%PITCH%%, inset=%%INSET%%)
TIGHT_MM = 1000000
''' + _GEOM + r'''
# 散热盘登记：面积 >= pad_mm2 的焊盘一律**不许当落孔位**（判据在 _in_thermal）。
# 规则必须打印数字，否则会悄悄漂成"没有一个散热盘被登记"还照常出图。
THERMALS.extend(_p for _fp in b.GetFootprints() for _p in _fp.Pads()
                if _p.GetSize().x * _p.GetSize().y / 1e12 >= CFG['pad_mm2'])
print('BIGPADS', len(THERMALS))

def _tight(pad):
    """这只脚是不是夹在**细间距引脚场**里（同封装最近异网脚中心距 <= 1.0mm）。

    0.5mm 脚距的 QFP/QFN 一列里，相邻两脚往往都只有"往外那一条走廊"能落孔：
    谁先占了谁就把旁边那只挤死——10-29 mcu_stm32f103 实测，U1-8(GND) 的落孔
    占住东侧走廊、U1-9(+3.3V) 西侧又被先布轮的 NRST 线横切，pad 9 变成任何
    方向都出不去的死脚（DRC unconnected_items，stitch 也救不了）。这种脚必须
    **在布线之前**占位（keepout）；1.27/2.54 接插件、0805 电容周围有地方，早占
    只会白挡布线（411 的 J2 走廊就是这么被挤掉的）。"""
    c = pad.GetPosition()
    for q in pad.GetParentFootprint().Pads():
        if q.GetNetname() == pad.GetNetname():
            continue
        if math.hypot(q.GetPosition().x - c.x, q.GetPosition().y - c.y) <= TIGHT_MM:
            return True
    return False

CHOSEN.extend(RESERVED)

def _thermal(pad, net, ref, num):
    """大面积同网焊盘：网格打孔，不拉引线（孔盘整个落在焊盘里）。

    孔数 = 下限（=1）与"内缩后几何装得下几枚"取小，**不按面积加码**：
    散热走芯片上的散热片（2026-10-29 用户拍板），PCB 这边只保 EP 接地。"""
    sz = pad.GetSize()
    c = pad.GetPosition()
    vd, dr = VIAS[0]
    r = vd // 2
    ins = int(CFG['inset'] * 1e6)
    if min(sz.x, sz.y) < 2 * (r + ins):
        return []
    area = sz.x * sz.y / 1e12
    if area < CFG['pad_mm2']:
        return []
    pitch = int(CFG['pitch'] * 1e6)
    nx = int((sz.x - 2 * (r + ins)) // pitch) + 1
    ny = int((sz.y - 2 * (r + ins)) // pitch) + 1
    cap = nx * ny
    want = max(1, min(CFG['floor'], cap))
    pts = [(c.x - (nx - 1) * pitch // 2 + i * pitch,
            c.y - (ny - 1) * pitch // 2 + j * pitch)
           for i in range(nx) for j in range(ny)]
    pts.sort(key=lambda p: (math.hypot(p[0] - c.x, p[1] - c.y), p[0], p[1]))
    out = []
    for x, y in pts:
        if len(out) >= want:
            break
        if _hits(x, y, net, r + 200000, None) is not None:
            continue                      # 盘里有异网铜（罕见），让开
        if not _hole_ok(x, y, vd):
            continue
        out.append(dict(ref=ref, num=num, net=net, px=c.x, py=c.y,
                        layer=pad.GetLayer(), x=x, y=y, vd=vd, dr=dr,
                        elbow=None, how='thermal'))
        CHOSEN.append({'x': x, 'y': y, 'vd': vd,
                       'px': (c.x, c.y), 'elbow': None})
    print('THERMAL', ref, num, net, round(area, 1), len(out), want, cap)
    return out

spots = []
_idx = 0
for fp in b.GetFootprints():
    ref = fp.GetReference()
    for pad in fp.Pads():
        net = pad.GetNetname()
        if net not in plane_nets:
            continue
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            continue                      # 通孔焊盘本身贯穿叠层，不必扇出
        if pad.GetLayer() not in (pcbnew.F_Cu, pcbnew.B_Cu):
            continue
        if (ref, pad.GetNumber()) in SKIP:
            continue
        if TIGHT is not None and _tight(pad) != TIGHT:
            continue
        # 落孔策略：近到远 / 远到近 / 按脚序交错。交错是为了让 0.5mm 脚距的
        # 一列平面网脚**岔开远近**——都抢最近那档，后一只的引线必然从先落那枚
        # 孔盘边擦过（0.5mm < 0.6mm）→ 死脚（见 fanout.STRATS 注）。
        if STRAT == 'far':
            CANDS = CANDS_FAR
        elif STRAT == 'alt':
            CANDS = CANDS_FAR if (_idx % 2) else CANDS_NEAR
        else:
            CANDS = CANDS_NEAR
        _idx += 1
        th = _thermal(pad, net, ref, pad.GetNumber())
        if th:                            # 散热盘：网格孔就是它的扇出
            spots.extend(th)
            continue
        s = choose(pad, net)
        if s is None:
            print('NOSPOT', ref, pad.GetNumber(), net)
            continue
        c = pad.GetPosition()
        # 后续焊盘避开已落孔位：连引线走向一起记，新过孔/新引线才能让开老引线
        CHOSEN.append({'x': s['x'], 'y': s['y'], 'vd': s['vd'],
                       'px': (c.x, c.y),
                       'elbow': tuple(s['elbow']) if s['elbow'] else None})
        spots.append(dict(ref=ref, num=pad.GetNumber(), net=net,
                          px=c.x, py=c.y, layer=pad.GetLayer(), **s))
print('LANEREJ', LANE_REJ[0])
print('SPOTS', json.dumps(spots))
'''

_KEEPOUT_TMPL = r'''
import math
import wx
wx.App(False)
import pcbnew
b = pcbnew.LoadBoard(%%IN%%)
spots = %%SPOTS%%
RADII = %%RADII%%
LS = pcbnew.LSET()
for L in (pcbnew.F_Cu, pcbnew.B_Cu):
    LS.AddLayer(L)
for i, s in enumerate(spots):
    z = pcbnew.ZONE(b)
    z.SetLayerSet(LS)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(True)
    z.SetDoNotAllowZoneFills(False)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowFootprints(False)
    z.SetZoneName(%%KO_NAME%%)
    ol = z.Outline()
    ol.NewOutline()
    r = RADII[i]
    n = 24
    for k in range(n):
        a = 2 * math.pi * k / n
        ol.Append(pcbnew.VECTOR2I(int(s['x'] + r * math.cos(a)),
                                  int(s['y'] + r * math.sin(a))))
    b.Add(z)
b.Save(%%OUT%%)
print('keepouts', len(spots))
'''

_COPPER_TMPL = r'''
import wx
wx.App(False)
import pcbnew
b = pcbnew.LoadBoard(%%IN%%)
spots = %%SPOTS%%
TRACK_W = %%TRK_W%%

# 先删 keepout：必须「先收敛成列表再删」——边遍历边 Remove 会毒化 SWIG 类型
# 注册表，之后 GetFootprints() 返回裸 SwigPyObject（fp.Pads 不存在）。
zs = [z for z in b.Zones() if z.GetZoneName() == %%KO_NAME%%]
for z in zs:
    b.Remove(z)
removed = len(zs)

# 网名→网号：不调 b.FindNet（本机 SWIG 返回裸 SwigPyObject，GetNetCode 不存在），
# 直接从焊盘取——焊盘就是扇出目标本身，网号必然对。
wanted = set(s['net'] for s in spots)
netcode = {}
for fp in b.GetFootprints():
    for pad in fp.Pads():
        nm = pad.GetNetname()
        if nm in wanted and nm not in netcode:
            netcode[nm] = pad.GetNetCode()

added = []
for s in spots:
    code = netcode.get(s['net'])
    if code is None:
        print('MISSNET', s['ref'], s['net']); continue
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(int(s['x']), int(s['y'])))
    v.SetWidth(s['vd'])
    v.SetDrill(s['dr'])
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNetCode(code)
    b.Add(v)
    if s['how'] not in ('inpad', 'thermal'):   # inpad/thermal 的孔盘与焊盘重叠，不需要引线
        p0 = pcbnew.VECTOR2I(int(s['px']), int(s['py']))
        p1 = v.GetPosition()
        pts = [p0, pcbnew.VECTOR2I(*s['elbow']), p1] if s['elbow'] else [p0, p1]
        for a, z_ in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(a)
            t.SetEnd(z_)
            t.SetWidth(TRACK_W)
            t.SetLayer(s['layer'])
            t.SetNetCode(code)
            b.Add(t)
    added.append((s['ref'], s['num'], s['net'], s['how']))
b.Save(%%OUT%%)
print('copper', len(added), 'keepouts_removed', removed)
'''


def _fill(tmpl: str, **kw) -> str:
    """模板填充用 str.replace 而非 str.format——模板正文里有大量字面花括号
    （dict 字面量/集合推导），format 会把它们当占位符炸掉。"""
    for k, v in kw.items():
        tmpl = tmpl.replace(f"%%{k.rstrip('_').upper()}%%", repr(v))
    return tmpl


def _run(code: str, marker: str, what: str) -> str:
    r = subprocess.run([env.KPY, "-c", code], capture_output=True,
                       text=True, timeout=600)
    out = (r.stdout + r.stderr).strip()
    if r.returncode or marker not in out:
        raise RuntimeError(f"fanout {what} FAILED:\n{out[-1500:]}")
    return out


def plan_fanout(board: Path, stackup, tight: bool | None = None,
                skip: set[tuple[str, str]] | None = None,
                reserved: list[dict] | None = None) -> list[dict]:
    """在**当前**板上为平面网 SMD 焊盘找落孔位。返回 spot 列表。

    `tight`：None=所有平面网焊盘；True=只要细间距引脚场里的（布线前就得占位，
    否则会被邻脚的落孔/先布的线夹死）；False=只要其余的。
    `skip`：已经规划过的 (位号, 脚号)，本轮不再规划。

    落孔策略（`STRATS`）依次试，取"没落孔位的脚最少、引线总长最短"那一版：
    0.5mm 脚距的一列平面网脚只有一条朝外走廊，先规划的占近处、后一只的引线
    就擦着它盘边走（0.5mm < 0.6mm）→ 死脚。换远近交错的顺序能岔开。
    没出现死脚时只跑第一档（常见情况，不多花时间）。
    """
    nets = sorted(stackup.plane_nets)
    best = None
    tried = []
    for strat in STRATS:
        out = _run(_fill(_PLAN_TMPL, in_=str(board), nets=nets, trk_w=TRACK_W,
                         path_clr=PATH_CLR, vias=VIAS, tight=tight,
                         skip=set(skip or ()), reserved=list(reserved or ()),
                         strat=strat, pad_mm2=THERMAL_PAD_MM2,
                         floor=THERMAL_FLOOR, pitch=THERMAL_PITCH,
                         inset=THERMAL_INSET, lane=ESC_LANE,
                         ko_mrg=KO_MARGIN),
                   "SPOTS", "plan")
        spots = json.loads([l for l in out.splitlines()
                            if l.startswith("SPOTS")][-1][len("SPOTS"):].strip())
        nospot = [l.split()[1] + "-" + l.split()[2]
                  for l in out.splitlines() if l.startswith("NOSPOT")]
        # 引线总长当第二判据：同样没死脚时取短的那版（布线通道留得多）
        cost = sum(math.hypot(s['x'] - s['px'], s['y'] - s['py']) for s in spots)
        tried.append((strat, len(nospot)))
        key = (len(nospot), cost)
        if best is None or key < best[0]:
            best = (key, spots, nospot, out)
        if not nospot:
            break
    key, spots, nospot, out = best
    if len(tried) > 1:
        print("  [fanout] 落孔策略 " +
              " / ".join(f"{s}={n} 死脚" for s, n in tried) +
              f" → 用 {tried[-1][0] if not nospot else '最好那版'}")
    # 「这个平面网焊盘在任何方向都找不到落孔位」——本轮布不成的那几只。
    # 布完之后它们就是死脚（stitch 同样没地方落孔），调用方据此决定要不要
    # 换"布线前占位"重来一次（见 loop.run_pipeline 的 fan_early_only）。
    plan_fanout.last_nospot = nospot
    # 逃逸走廊门禁挡掉了多少候选——规则必须打印数字，否则会悄悄漂掉
    rej = [l.split()[1] for l in out.splitlines() if l.startswith("LANEREJ")]
    if rej and int(rej[-1]):
        print(f"  [fanout] 逃逸走廊门禁挡掉 {rej[-1]} 个候选落孔位"
              f"（keepout 会压住信号脚的出脚道；见 fanout.ESC_LANE）")
    for l in out.splitlines():
        if l.startswith("BIGPADS"):
            print(f"  [fanout] 登记散热盘 {l.split()[1]} 个：孔一律不许落进盘里"
                  f"（散热盘自己的接地缝合孔除外，见 _in_thermal）")
    for l in out.splitlines():
        if l.startswith("THERMAL"):
            p = l.split()
            print(f"  [fanout] {p[1]}-{p[2]} ({p[3]}) {p[4]}mm² 散热盘打孔 "
                  f"{p[5]} 枚（要 {p[6]}/装得下 {p[7]}）")
    for s in spots:
        if s["how"] == "inpad":
            print(f"  [fanout] {s['ref']}-{s['num']} ({s['net']}) 只能孔盘内扇出")
    return spots


plan_fanout.last_nospot = []     # 上一轮"哪儿都落不了孔"的脚，见 plan_fanout


def add_keepouts(board: Path, spots: list[dict], dst: Path) -> Path:
    """把每个落孔位围成 keepout 规则区（F.Cu + B.Cu），供 FR 绕开。"""
    radii = [s["vd"] // 2 + 200000 + KO_MARGIN for s in spots]
    _run(_fill(_KEEPOUT_TMPL, in_=str(board), spots=spots, radii=radii,
               ko_name=KO_NAME, out=str(dst)),
         "keepouts", "keepout")
    return dst


def place_fanout(board: Path, spots: list[dict], dst: Path) -> Path:
    """真落铜：删 keepout + 加过孔/引线。"""
    _run(_fill(_COPPER_TMPL, in_=str(board), spots=spots, trk_w=TRACK_W,
               ko_name=KO_NAME, out=str(dst)),
         "copper", "copper")
    return dst
