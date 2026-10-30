#!/usr/bin/env python3
"""块交付板上的「空线头」体检 + 清理（2026-10-30）

**什么算空线头**：走线有一头没接上 / 过孔只有一侧有铜。判据**不自证**：
拿 `kicad-cli pcb drc` 的 `track_dangling` + `via_dangling` 当外部裁判——
那正是用户在 KiCad 里看到的那些小圈，不是我们自己的几何猜测。

**三类，只有一类能删**：
- **口岸断头**：空头这根铜本来就该出块——`placement.json` 的 `cut_points`（信号出口）或
  `counts.borrowed_pads`（铜停在块外焊盘中心）点过名，或空头贴在板框 ≤ tol。留着，但**要报数**。
- **载流断头**：空头**删了会把两个同网焊盘断开**（2026-10-30 `bp5_analog_mux` 实测：AMUX_EN
  那根竖走线一头悬空、中段却压在 R402 焊盘上，是 U402.15↔R402.2 之间**唯一**的铜，
  删掉整网就断了）。留着，**报数**。
- **毛刺**：其余全部。两头全空的碎段、飘着的过孔、**轨道网扎进铺铜缝合孔就断的短线**
  （块不带铺铜，那截是铺铜的附件）——没有任何理由存在，**必须删**。

⚠ 「只删一头空的整根线」是错的口径：KiCad 的 `track_dangling` 判的是**端点**，一根线
一头接焊盘、一头悬空，照样整根报空——可它中段可能正压着另一个焊盘（本例就是）。
所以删之前先数「这根铜挨着几个同网对象」（`mates`），≥2 个一律不删。

用户 2026-10-30 的原话：「411 设计块中的空线头没有去掉呀，为什么每次都要提醒呀」——
所以这是交付物的一部分，不是可选项；本模块同时是门禁（退出码 1）。

跑法：
    python3 -m sdi.spur ../DesignBlocks                       # 全库体检（只报，有毛刺退出 1）
    python3 -m sdi.spur ../DesignBlocks --fix                 # 删毛刺（删完自动复检，级联也算）
    python3 -m sdi.spur ../DesignBlocks --fix --write-counts  # 顺手把铜/孔/空线头写回记账
    python3 -m sdi.spur DesignBlocks/x/x.kicad_pcb

⚠ 两进程架构（同 `sdi/cardpcb.py`）：`kicad-cli` 出账、KiCad 自带 python 的 `pcbnew`
出几何与做删除；本模块在普通 python 里当调度。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KICAD_PY = [
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3',
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9',
    '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3',
]
KICAD_CLI = [
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
    '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
]

# ── 子进程①：给被判"空"的项出几何（哪一头空、离板框多远）───────────────
# 入参：板、uuid 名单 json、输出 json
GEOM = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys, math
MM = 1e6
b = pcbnew.LoadBoard(sys.argv[1])
want = set(json.load(open(sys.argv[2])))

tracks = [t for t in b.GetTracks() if t.Type() != pcbnew.PCB_VIA_T]
vias   = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
pads   = [p for f in b.GetFootprints() for p in f.Pads()]
edges  = [d for d in b.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]


def uid(x):
    return str(x.m_Uuid.AsString())


def edge_dist(pt):
    """点到板框的最短距离（mm）。"""
    px, py = pt.x / MM, pt.y / MM
    best = 1e9
    for d in edges:
        a, c = d.GetStart(), d.GetEnd()
        ax, ay, cx, cy = a.x / MM, a.y / MM, c.x / MM, c.y / MM
        dx, dy = cx - ax, cy - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def touched(pt, net, layer, me):
    """这个点上有没有**同网**的别的铜（焊盘/走线/过孔）。

    ⚠ 排除自己必须用 **uuid**：`GetTracks()` 每次给 SWIG 新包一层，
    同一个走线两次拿到的是两个 Python 对象，`o is t` 永远 False（10-30 踩过）。
    """
    for p in pads:
        if p.GetNetname() == net and p.IsOnLayer(layer) and p.HitTest(pt):
            return True
    for t in tracks:
        if uid(t) == me:
            continue
        if t.GetNetname() == net and t.GetLayer() == layer and t.HitTest(pt):
            return True
    for v in vias:
        if uid(v) == me:
            continue
        if v.GetNetname() == net and v.HitTest(pt):
            return True
    return False


def on_seg(a, b, q, acc=0.01):
    """把 q 投影到线段 a→b 上，距离 ≤ acc(mm) 就算压着。"""
    dx, dy = b.x - a.x, b.y - a.y
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(q.x - a.x, q.y - a.y) <= acc * MM
    t = max(0.0, min(1.0, ((q.x - a.x) * dx + (q.y - a.y) * dy) / L2))
    return math.hypot(a.x + t * dx - q.x, a.y + t * dy - q.y) <= acc * MM


def seg_seg(a, b, c, d):
    """两条线段的最短距离（mm）；相交记 0。"""
    def cross(o, p, q):
        return ((p.x - o.x) * (q.y - o.y) - (p.y - o.y) * (q.x - o.x)) / (MM * MM)

    def pt_seg(p, q, r):                     # 点 p 到线段 q→r
        px, py = p.x / MM, p.y / MM
        qx, qy, rx, ry = q.x / MM, q.y / MM, r.x / MM, r.y / MM
        dx, dy = rx - qx, ry - qy
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - qx) * dx + (py - qy) * dy) / L2))
        return math.hypot(px - (qx + t * dx), py - (qy + t * dy))

    d1, d2, d3, d4 = cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(pt_seg(a, c, d), pt_seg(b, c, d), pt_seg(c, a, b), pt_seg(d, a, b))


def mates(pt_a, pt_b, net, layer, me, is_via=False, width=0.0):
    """这段铜**挨着几个别的同网对象**（焊盘 / 走线 / 过孔，各算一次）。

    只数"端点落在谁身上"不够：一根线可以**中段压在焊盘上**（KiCad 认这种压接是连通的），
    2026-10-30 `bp5_analog_mux` 的 AMUX_EN 就是这种。焊盘少 → 沿线段每 0.05mm 采样点判
    `pad.HitTest(点)`（矩形含尺寸）。

    ⚠ 走线**不能只看端点互含 + 0.01mm 容差**：2026-10-31 `usb_serial_cp2102n` 里一根 0.4mm 的
    GND 斜线，中心线离 R1.2 支线端头 0.042mm（> 容差）——可两边半径 0.2+0.125 早就把铜压在一起了，
    它是块里唯一的地桥。现在走线按**铜挨铜**判：两段中心线最短距离 ≤ 两线宽一半之和。
    """
    n, lays = 0, set()
    if is_via:
        pts = [pt_a]
    else:
        L = math.hypot(pt_b.x - pt_a.x, pt_b.y - pt_a.y)
        steps = max(1, int(L / MM / 0.05))
        pts = [pcbnew.VECTOR2I(int(pt_a.x + (pt_b.x - pt_a.x) * i / steps),
                               int(pt_a.y + (pt_b.y - pt_a.y) * i / steps))
               for i in range(steps + 1)]
    for p in pads:
        if p.GetNetname() != net or not any(p.HitTest(q) for q in pts):
            continue
        if layer is not None and not p.IsOnLayer(layer):
            continue
        n += 1
        lays |= {i for i in p.GetLayerSet().Seq()}
    for t in tracks:
        if uid(t) == me or t.GetNetname() != net:
            continue
        if layer is not None and t.GetLayer() != layer:
            continue
        ts, te = t.GetStart(), t.GetEnd()
        near = any(on_seg(ts, te, q) for q in (pt_a, pt_b))
        if not near and not is_via:
            near = any(on_seg(pt_a, pt_b, q) for q in (ts, te))
        if not near and not is_via:
            near = seg_seg(pt_a, pt_b, ts, te) <= (t.GetWidth() + width) / (2 * MM) + 0.001
        if near:
            n += 1
            lays.add(t.GetLayer())
    for v in vias:
        if uid(v) == me or v.GetNetname() != net:
            continue
        if any(v.HitTest(q) for q in (pt_a, pt_b)):
            n += 1
    # 过孔的价值是**换层**：两侧各有一层铜才算载流；两侧都在同一层（2026-10-30 实测：
    # 一个 GND 过孔挂着两段 F.Cu 的拐角线、B.Cu 什么都没有）是死孔，删了不亏。
    return len(lays) if is_via else n


out = []
for t in tracks:
    if uid(t) not in want:
        continue
    net, lay = t.GetNetname(), t.GetLayer()
    s, e = t.GetStart(), t.GetEnd()
    free = [pt for pt in (s, e) if not touched(pt, net, lay, uid(t))]
    # 两头都被同网铜压着却仍被判空 = 叠在别人身上的碎段（KiCad 认"没有端点节点"）：
    # 两个端点都当候选，别让它靠"有人压着"逃掉
    cand = free or [s, e]
    out.append({'kind': 'track', 'uuid': uid(t), 'net': net,
                'layer': t.GetLayerName(), 'len': round(t.GetLength() / MM, 4),
                'width': round(t.GetWidth() / MM, 4),
                'mates': mates(s, e, net, lay, uid(t), width=t.GetWidth()),
                'cand': [[round(p.x / MM, 3), round(p.y / MM, 3), round(edge_dist(p), 3)]
                         for p in cand],
                'ends': [[round(p.x / MM, 3), round(p.y / MM, 3)] for p in (s, e)]})

for v in vias:
    if uid(v) not in want:
        continue
    p = v.GetPosition()
    out.append({'kind': 'via', 'uuid': uid(v), 'net': v.GetNetname(), 'layer': 'via',
                'len': 0.0, 'width': round(v.GetWidth(pcbnew.F_Cu) / MM, 4),
                'mates': mates(p, p, v.GetNetname(), None, uid(v), True),
                'cand': [[round(p.x / MM, 3), round(p.y / MM, 3), round(edge_dist(p), 3)]],
                'ends': [[round(p.x / MM, 3), round(p.y / MM, 3)]]})

json.dump(out, open(sys.argv[3], 'w'))
'''

# ── 子进程②：按 uuid 删 ──────────────────────────────────────────────
REMOVE = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
b = pcbnew.LoadBoard(sys.argv[1])
kill = set(json.load(open(sys.argv[2])))
n = 0
for t in list(b.GetTracks()):
    if str(t.m_Uuid.AsString()) in kill:
        b.Remove(t); n += 1
b.Save(sys.argv[1])
print(n)
'''


COUNTS = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
b = pcbnew.LoadBoard(sys.argv[1])
segs = [t for t in b.GetTracks() if t.Type() != pcbnew.PCB_VIA_T]
vias = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
by = {}
for t in segs:
    n = t.GetLayerName()
    by[n] = by.get(n, 0) + 1
print('@@' + json.dumps({'copper_segs': len(segs), 'vias': len(vias),
                         'segs_by_layer': dict(sorted(by.items())),
                         'zones_kept': len(b.Zones()),
                         'members': len(b.GetFootprints())}))
'''


def _find(cands, what: str) -> str:
    for p in cands:
        if os.path.exists(p):
            return p
    w = shutil.which(what)
    if w:
        return w
    raise SystemExit(f'✗ 找不到 {what}（候选：{cands}）。改 sdi/spur.py 顶部的路径表。')


def _kicad_py() -> str:
    return _find(KICAD_PY, 'python3')


def _run_py(script: str, *args: str) -> str:
    r = subprocess.run([_kicad_py(), '-c', script, *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'✗ 子进程失败：{(r.stderr or r.stdout).strip()[-400:]}')
    return r.stdout.strip()


def unconn_items(pcb: Path) -> int:
    """kicad-cli 判的「未连通」条数（`--refill-zones`，与 `sdi/pcbdrc.py` 同口径）。

    ⚠ 这是给 `trim()` 兜底的第二把尺子。焊盘坨数（`pad_groups`）只数**焊盘**分几坨：
    `usb_ftdi_bridge` 的 `/EE_DAT` 在块里**只有一只焊盘**，删掉整根出走铜它照样是 1 坨 →
    试删门判"安全"，实际把口头铜删断了（kicad-cli 的未连通 6 → 7，2026-10-30 实测）。
    单焊盘网（跨块的口岸网就是这一类）只能靠这条数兜。"""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        out = f.name
    try:
        r = subprocess.run([_find(KICAD_CLI, 'kicad-cli'), 'pcb', 'drc', '--severity-all',
                            '--refill-zones', '--format', 'json', '-o', out, str(pcb)],
                           capture_output=True, text=True)
        if r.returncode or not Path(out).is_file():
            raise SystemExit(f'✗ kicad-cli DRC 失败：{(r.stderr or r.stdout).strip()[-400:]}')
        return len(json.loads(Path(out).read_text()).get('unconnected_items') or [])
    finally:
        Path(out).unlink(missing_ok=True)


def drc_dangling(pcb: Path) -> dict:
    """kicad-cli 的眼睛：{uuid: 'track'|'via'}。这是外部裁判（用户看到的就是它）。"""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        out = f.name
    try:
        r = subprocess.run([_find(KICAD_CLI, 'kicad-cli'), 'pcb', 'drc', '--severity-all',
                            '--format', 'json', '-o', out, str(pcb)],
                           capture_output=True, text=True)
        if r.returncode or not Path(out).is_file():
            raise SystemExit(f'✗ kicad-cli DRC 失败：{(r.stderr or r.stdout).strip()[-400:]}')
        d = json.loads(Path(out).read_text())
    finally:
        Path(out).unlink(missing_ok=True)
    got = {}
    for v in d.get('violations') or []:
        if v.get('type') == 'track_dangling':
            for i in v.get('items') or []:
                got[i['uuid']] = 'track'
        elif v.get('type') == 'via_dangling':
            for i in v.get('items') or []:
                got[i['uuid']] = 'via'
    return got


def _allowance(pcb: Path) -> dict:
    """这块板里**允许空头**的网，分三类记（都来自记账文件，不是猜）：

    - `cut`：`placement.json.cut_points[].net`——pcbcarve 在窗口边切铜的地方（信号口岸）；
    - `borrowed`：`counts.borrowed_pads` 的键（`网名->位号.脚`）——铜停在块外焊盘中心；
    - `rails`：轨道网（`block.sdi.yaml` 的 `rails:`）。**轨道网不吃 `cut` 这条**：
      GND/+3V3 本来就靠拼板整板重铺连通，块里一段短线走到铺铜缝合孔就断的，
      那是铺铜的附件不是出口——删（见 `trim`）。

    没有 `placement.json`（手画块、非裁剪块）→ 三个集合全空：一根空头都不许有。
    """
    pj = pcb.parent / f'{pcb.stem}.placement.json'
    out = {'cut': set(), 'borrowed': set(), 'rails': set()}
    if pj.is_file():
        d = json.loads(pj.read_text(encoding='utf-8'))
        out['cut'] = {c.get('net') for c in (d.get('cut_points') or []) if c.get('net')}
        out['borrowed'] = {k.split('->')[0]
                           for k in ((d.get('counts') or {}).get('borrowed_pads') or {})}
    card = pcb.parent / 'block.sdi.yaml'
    if card.is_file():
        try:
            import yaml
            doc = yaml.safe_load(card.read_text(encoding='utf-8')) or {}
            one = (doc.get('defs') or {}).get(pcb.stem) or {}
            out['rails'] = set(one.get('rails') or [])
        except Exception:  # noqa: BLE001 —— 卡坏了不阻塞体检，按"无轨"算
            pass
    return out


def scan(pcb: Path, tol: float = 0.35) -> dict:
    """空线头逐项。`port_cut` 只认**几何**（必须一头空，两头全空永远算毛刺）：

    ① 空的那一头**落在板框上**（离板框 ≤ tol）——块从整板扣出来的铜走到窗口边就停；
    ② 这根网在 `borrowed_pads` 里——铜停在块外焊盘中心（拼板插上就通）。

    ⚠ 2026-10-30 改：**「网名在 `cut_points` 里」不再是通行证**。原来只看网名，一块板
    重新扣小之后残在板中间的死尾巴（离板框 4~11mm）照样被算成「口岸断头」放过——
    `mcu_stm32f411` 实测 5 根（USB_D_N 10mm、IMU_SCL 1.74mm…），用户连着三次点出来
    「还有空线」。离板框远 = 拼板时没人接得上 = 毛刺。

    ⚠ 2026-10-30 晚删掉过一个更激进的模式：`strict=True` 不认 ①②、所有空头一律当毛刺。
    它把**口岸断头也送进试删门**，而试删门的判据（kicad-cli 未连通条数不涨）对悬空铜
    是瞎的——删掉一条悬空端口会让条数只减不增。实测一块板的口岸铜被清掉 117 段 + 18 孔
    还判绿。所以候选**绝不含口岸断头**；`--strict` 下候选 = 毛刺 + 载流断头预筛
    （2026-10-31 起，见 `trim()`：`mates` 预筛会把互相挨着的浮空过孔当成互相作证）。

    轨道网（GND/电源）只认 ① 和 ③：块不带铺铜，轨道网靠拼板整板重铺连通，
    块里一条短线扎进铺铜缝合孔就断的，是铺铜的附件、不是出口 → 算毛刺。

    第四类 **`binds`（载流断头）**：不满足上面三条、但这段铜挨着 ≥2 个同网对象
    （`mates`，含"中段压在焊盘上"）——删了会把两个焊盘之间的唯一通路删断。
    留着并**报数**，不计进毛刺。这条是 2026-10-30 实测踩出来的（见模块头）。
    """
    want = drc_dangling(pcb)
    if not want:
        return {'items': [], 'tracks': 0, 'vias': 0}
    allow = _allowance(pcb)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(sorted(want), f)
        uu = f.name
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        out = f.name
    try:
        _run_py(GEOM, str(pcb), uu, out)
        items = json.loads(Path(out).read_text())
    finally:
        Path(uu).unlink(missing_ok=True)
        Path(out).unlink(missing_ok=True)
    for i in items:
        net = i['net']
        one_end = len(i['cand']) == 1
        near_edge = all(c[2] <= tol for c in i['cand'])
        why = None
        if one_end and i['kind'] == 'track':
            if near_edge:
                why = ('口岸断头（记账的口岸，贴板框）' if net in allow['cut']
                       else '口岸断头（贴板框）' if net not in allow['rails']
                       else '口岸断头（轨道网贴板框）')
            elif net in allow['borrowed']:
                why = '口岸断头（借道块外焊盘）'
        i['binds'] = why is None and i['mates'] >= 2
        i['port_cut'] = why is not None
        i['keep'] = i['port_cut'] or i['binds']
        i['why'] = why or ('载流断头（删了会断网）' if i['binds']
                           else ('铺铜附件' if net in allow['rails'] else '毛刺'))
    return {'items': items, 'tracks': sum(1 for i in items if i['kind'] == 'track'),
            'vias': sum(1 for i in items if i['kind'] == 'via')}


def _remove(pcb: Path, uuids) -> int:
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
        json.dump(sorted(uuids), f)
        tmp = f.name
    try:
        out = _run_py(REMOVE, str(pcb), tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
    # 子进程的 stdout 混着 swig 的 "memory leak" 唠叨，只认第一行的数字
    for line in out.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return 0


def trim(pcb: Path, tol: float = 0.35, verbose: bool = True, strict: bool = False) -> dict:
    """删毛刺，直到没有（删一段可能让相邻段的头变空）。口岸断头一根不动。

    ⚠ 轮数上限**不能小**：kicad-cli 每轮只判**当前**这截，删掉一段后它后面那段才变空头，
    于是一条 10 段长的死链每轮只掉 1 段——上限 10 时会在链尾剩下 1 根（2026-10-30
    `mcu_stm32f411` 实测：连跑十轮之后仍报「毛刺 1」，且每次都是不同的一段）。
    上限只防死循环（同一批 uuid 反复删不掉），200 轮对 300 段级的板绰绰有余。

    **焊盘坨数门**：动手前记下每网的坨数，删完再比——任何网变多就是删错了（把两个焊盘
    之间的唯一通路删了），一律报红、退出码 1。载流断头（`binds`）本就不删，所以正常不会红。

    **未连通门**（2026-10-30 加）：焊盘坨数只数**焊盘**，单焊盘网（= 跨块的口岸网）删光了
    也还是 1 坨，看不见。所以动手前后各取一次 `kicad-cli` 的未连通条数，涨了就把整块板
    **回退**并报红——`usb_ftdi_bridge` 的 `/EE_DAT` 就是这么被删断的（6 → 7 条）。

    `strict=True` 时**每一个毛刺和每一个载流断头预筛项都走试删门**（`_prune_binds`，
    逐项删→比坨数→涨了放回），不再"整批盲删再事后比"：盲删一批再比只能事后报警、
    板子已经改了；而且 SWIG 一旦在试删里抛异常，整轮门禁就静默失效（2026-10-30 实测）。
    候选**只有**这两类——口岸断头是记账点名要留的铜，永不进候选（把口岸铜塞进候选的
    那一版会把整块板的口岸铜清空还判绿，见 `trim()` 里的注释）。载流断头进候选是
    2026-10-31 加的：`mates` 预筛会误保浮空过孔簇，试删门才是真判据。
    """
    rounds, dead = 0, []
    promote = set()                         # strict 试删证明"删了就断网"的 uuid → 改判载流断头
    base = pad_groups(pcb)                  # 门禁必须先量出来；量不出就退出，不盲删
    snap = pcb.with_name(pcb.stem + '.before_trim.kicad_pcb')
    shutil.copyfile(pcb, snap)              # 兜底要能把整块板放回去
    try:
        base_u = unconn_items(pcb)          # 外部裁判的未连通条数（焊盘坨数看不见单焊盘网）
    except SystemExit:
        base_u = None                       # 量不出来就只靠焊盘坨数（打印说明，不假装量过）
    for _ in range(200):
        rep = scan(pcb, tol)
        spurs = [i for i in rep['items'] if not i['keep']]
        binds = [i for i in rep['items'] if i['binds']]
        if strict:
            # 口岸断头（记账点名要留）**永不**进候选。
            # 2026-10-30 实测把口岸断头也塞进候选的后果：kicad-cli 的"未连通条数"把
            # 悬空端口本身算一条，删掉它条数只减不增，"不涨"这门永远判安全——整块板的
            # 口岸铜被一路清空（usb_ftdi_bridge 170 段 + 22 孔），门还全绿。
            #
            # 2026-10-31 改：**载流断头（`binds`）也进候选**。原来 `mates` 预筛说了算，
            # 可它数的是"端头附近 ≥2 个同网对象"，**彼此挨着的浮空过孔会互相作证**——
            # `usb_c_serial_s3` 删掉 GND 铺铜后剩 9 只 1mm 网格缝合孔 + 8 根死尾巴
            # 共 17 项，`mates` 全 ≥2，一直不进候选；整批试删实测 kicad-cli 未连通
            # 20→20、焊盘坨数 53→53，即**电上是死的**，导致交付板带 18 个空头（只有 3 个
            # 是记账口岸）。判据仍是试删门：删了任何网的坨数/未连通条数不涨就删；
            # 真载流项试删会红，`promote` 原样保住它并把结论喂回 `why`。
            cand = spurs + binds
            if not cand:
                break
            safe, kept = _prune_binds(pcb, [i['uuid'] for i in cand])
            dead += [i for i in cand if i['uuid'] in set(safe)]
            promote |= set(kept)            # 试删说"删了会断网"= 载流断头的定义本身
            rounds += 1
            if verbose:
                print(f'    第 {rounds} 轮：试删 {len(cand)} 项 → 安全 {len(safe)}'
                      f' / 会断网 {len(kept)}')
            if not safe:
                break
            continue
        if not spurs:
            if not binds:
                break
            safe, kept = _prune_binds(pcb, [i['uuid'] for i in binds])
            if not safe:
                break                       # 全是真载流，收工
            dead += [i for i in binds if i['uuid'] in set(safe)]
            rounds += 1
            if verbose:
                print(f'    第 {rounds} 轮：试删载流断头 {len(binds)} 项 → 安全 {len(safe)}'
                      f' / 真载流 {len(kept)}')
            continue
        n = _remove(pcb, [i['uuid'] for i in spurs])
        rounds += 1
        dead += spurs
        if verbose:
            print(f'    第 {rounds} 轮：删 {len(spurs)} 项（落盘 {n}）')
        if not n:
            break
    rep = scan(pcb, tol)
    rep['rounds'] = rounds
    rep['deleted'] = dead
    # 试删门已经量过这几截铜：删了焊盘坨数就变多 = 它真在载流。分类要跟着证据走——
    # `mates` 只是**便宜的预筛**（只看端头挨不挨着），看不见"中段压在别的线头端帽上"那种桥；
    # 不把试删结论喂回来，这截铜就永远既删不掉、又永远算毛刺，门永远红。
    promoted = [i for i in rep['items'] if i['uuid'] in promote and not i['keep']]
    for i in promoted:
        i['binds'] = i['keep'] = True
        i['promoted'] = True
        i['why'] = '载流断头（试删证明：删了会断网）'
    if promoted and verbose:
        print(f'    试删证明载流 {len(promoted)} 项：改判载流断头（不再算毛刺）')
    rep['promoted'] = [i['uuid'] for i in promoted]
    after = pad_groups(pcb)
    broke = {n: (base[n], after[n]) for n in after if after.get(n, 0) > base.get(n, 0)}
    rep['regressed'] = broke
    if broke:
        print('    ✗ 焊盘坨数变多（删断了）：'
              + '、'.join(f'{n} {a}→{z} 坨' for n, (a, z) in broke.items()))
    # 第二把尺子：kicad-cli 的未连通条数只许多减，不许多增。增了说明删坏了一根**单焊盘网**
    # （焊盘坨数对它无感），整块板回退到动手前，并把这件事报出来。
    if base_u is not None:
        now_u = unconn_items(pcb)
        rep['unconn'] = (base_u, now_u)
        if verbose:
            print(f'    未连通（kicad-cli）{base_u} → {now_u} 条')
        if now_u > base_u:
            shutil.copyfile(snap, pcb)
            rep = scan(pcb, tol)             # 板已回到动手前：试删证据作废，promote 不带
            rep['rounds'] = rounds
            rep['deleted'] = []
            rep['unconn_regressed'] = (base_u, now_u)
            print(f'    ✗ 未连通 {base_u} → {now_u} 条：删毛刺把网删断了，整块已回退'
                  '（可试 --strict 逐个试删）')
    snap.unlink(missing_ok=True)
    return rep


GROUPS = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys
b = pcbnew.LoadBoard(sys.argv[1])
pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname()]
b.BuildConnectivity()
c = b.GetConnectivity()


def key(p):
    f = p.GetParentFootprint()          # pad.GetParent() 是 BOARD_ITEM_CONTAINER，取不到位号
    return (f.GetReference(), p.GetNumber())


par = {key(p): key(p) for p in pads}


def find(x):
    while par[x] != x:
        par[x] = par[par[x]]
        x = par[x]
    return x


for p in pads:
    for it in c.GetConnectedItems(p):
        if it.Type() == pcbnew.PCB_PAD_T:
            k = key(it)
            if k in par and find(k) != find(key(p)):
                par[find(k)] = find(key(p))

net = {key(p): p.GetNetname() for p in pads}
g = {}
for p in pads:
    g.setdefault(net[key(p)], set()).add(find(key(p)))
print('@@' + json.dumps({k: len(v) for k, v in g.items()}))
'''


def _prune_binds(pcb: Path, uuids) -> tuple[list, list]:
    """「大概率载流」的逐个试删：删完再算焊盘坨数，不涨就真删，涨了放回去。

    `mates` 只是**便宜的预筛**（挨 ≥2 个同网对象才值得试），判据仍然是坨数——
    2026-10-30 `bp5_analog_mux`：3 个候选里 AMUX_S2 删了照样一坨（它挨的是同一坨里的
    另一段线），GND 那段也是；只有 AMUX_EN 那根删了会把 U402.15↔R402.2 拆成两坨。

    ⚠ **每个候选单独走两个子进程**（复制文件 → `REMOVE` → `GROUPS`）：在同一个 pcbnew
    进程里 `b.Remove()` 之后再 `b.Save()`/`b.LoadBoard()` 会拿到裸 SwigPyObject
    （`no attribute 'GetFootprints'` / `'GetReference'`），门禁静默失效。分开跑就不会。
    """
    base = pad_groups(pcb)
    tmp = pcb.with_name(pcb.stem + '.trial.kicad_pcb')
    safe, kept = [], []
    try:
        for u in uuids:
            shutil.copyfile(pcb, tmp)
            if not _remove(tmp, [u]):
                continue                      # 这个 uuid 在板上找不到，跳过
            now = pad_groups(tmp)
            if any(now.get(n, 0) > base.get(n, 0) for n in now):
                kept.append(u)                # 真载流：不落盘，pcb 原样
            else:
                safe.append(u)
                base = now
                os.replace(tmp, pcb)
    finally:
        tmp.unlink(missing_ok=True)
    return safe, kept


def pad_groups(pcb: Path) -> dict:
    """每个网**焊盘被铜连成几坨**（KiCad 自己的连通判，{网: 坨数}）。

    这是删毛刺的硬底线：删掉一截铜**不许让任何网的坨数变多**。
    2026-10-30 `bp5_analog_mux` 的 AMUX_EN 就是被这个抓出来的（1 坨 → 2 坨）；
    kicad-cli 的「未连通」条数抓不住——GND 本来就有 10 坨，再裂一坨只是换了代表项，
    条数纹丝不动。所以必须是逐网比坨数，不是比总数。
    """
    out = _run_py(GROUPS, str(pcb))
    for line in out.splitlines():
        if line.startswith('@@'):
            return json.loads(line[2:])
    raise SystemExit(f'✗ 算不出焊盘分组：{out[-300:]}')


def recount(pcb: Path) -> dict:
    """交付板现况（铜段/孔/分层/铺铜/件数）——pcbnew 数，不靠正则。"""
    out = _run_py(COUNTS, str(pcb))
    for line in out.splitlines():
        if line.startswith('@@'):
            return json.loads(line[2:])
    raise SystemExit(f'✗ 数不出铜：{out[-300:]}')


def write_counts(pcb: Path, rep: dict, note: str | None = None) -> dict | None:
    """把现况写回 `<块>.placement.json` 的 `counts`（含空线头记账）。

    没有 `placement.json`（手画块）→ 跳过，返回 None。
    `counts.dangling` 里 `trimmed` 是**累计**（多次清理会累加，不重算），
    其余字段每次都按现板重数。
    """
    pj = pcb.parent / f'{pcb.stem}.placement.json'
    if not pj.is_file():
        return None
    d = json.loads(pj.read_text(encoding='utf-8'))
    c = d.setdefault('counts', {})
    now = recount(pcb)
    old_tr = ((c.get('dangling') or {}).get('trimmed') or {})
    for k, v in now.items():
        c[k] = v
    cnt = counts(rep)
    c['dangling'] = {
        'total': cnt['dangling_total'],
        'port_cuts': cnt['port_cuts'],
        'port_cut_nets': sorted({i['net'] for i in rep['items'] if i['port_cut']}),
        'binds': cnt['binds'],
        'bind_nets': sorted({i['net'] for i in rep['items'] if i.get('binds')}),
        'spurs': cnt['spurs'],
        'regressed': (rep.get('regressed') or {}),
        'trimmed': {
            'tracks': old_tr.get('tracks', 0) + sum(1 for i in rep.get('deleted', [])
                                                   if i['kind'] == 'track'),
            'vias': old_tr.get('vias', 0) + sum(1 for i in rep.get('deleted', [])
                                               if i['kind'] == 'via'),
            'nets': sorted(set(old_tr.get('nets') or [])
                           | {i['net'] for i in rep.get('deleted', [])}),
            # 日期现取，别写死：写死的常量会让每一次以后的清理都顶着同一天记账。
            'when': datetime.date.today().isoformat(),
        },
    }
    if note:
        surg = c.setdefault('surgery', [])
        if note not in surg:
            surg.append(note)
    pj.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return c


def counts(rep: dict) -> dict:
    items = rep['items']
    cuts = [i for i in items if i['port_cut']]
    binds = [i for i in items if i.get('binds')]
    return {'dangling_total': len(items),
            'port_cuts': len(cuts),
            'port_cut_tracks': sum(1 for i in cuts if i['kind'] == 'track'),
            'port_cut_vias': sum(1 for i in cuts if i['kind'] == 'via'),
            'binds': len(binds),
            'spurs': len(items) - len(cuts) - len(binds)}


def _report(name: str, c: dict) -> str:
    mark = '✓' if not c['spurs'] else '✗'
    return (f'  {mark} {name:26s} 空线头 {c["dangling_total"]:3d} = '
            f'口岸断头 {c["port_cuts"]:3d}（走线 {c["port_cut_tracks"]} / 过孔 {c["port_cut_vias"]}）'
            f' + 载流断头 {c["binds"]:3d} + 毛刺 {c["spurs"]:3d}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='块交付板的空线头体检 / 清理（kicad-cli 当裁判）')
    ap.add_argument('path', help='块库根目录或单个 .kicad_pcb')
    ap.add_argument('--fix', action='store_true', help='删毛刺（口岸断头不动）')
    ap.add_argument('--strict', action='store_true',
                    help='配合 --fix：每个毛刺/载流断头预筛项都走"逐项试删→比焊盘坨数"，慢但最保守')
                    # ⚠ 口岸断头永不进候选——那一版把整块板的口岸铜清空过两次，
                    #   见文件头 2026-10-30 晚那段。
    ap.add_argument('--write-counts', action='store_true',
                    help='把铜/孔/分层/空线头现况写回 <块>.placement.json 的 counts')
    ap.add_argument('--note', help='同时把这条外科记录追加进 counts.surgery')
    ap.add_argument('--tol', type=float, default=0.35, help='口岸判据 mm（默认 0.35）')
    ap.add_argument('--out', help='机读报告落到这（json）')
    a = ap.parse_args(argv)

    root = Path(a.path)
    if root.is_file():
        files = [root]
    elif (root / f'{root.name}.kicad_pcb').is_file():
        files = [root / f'{root.name}.kicad_pcb']   # 直接点单块目录
    else:
        files = sorted(p for d in root.iterdir() if d.is_dir() and not d.name.startswith('_')
                       for p in [d / f'{d.name}.kicad_pcb'] if p.is_file())
    if not files:
        raise SystemExit(f'✗ {a.path} 下没有 <块名>.kicad_pcb')

    bad, n_spur, n_rb, all_rep = 0, 0, 0, {}
    tot = {'dangling_total': 0, 'port_cuts': 0, 'port_cut_tracks': 0,
           'port_cut_vias': 0, 'binds': 0, 'spurs': 0}
    for pcb in files:
        name = pcb.stem
        rep = trim(pcb, a.tol, strict=a.strict) if a.fix else scan(pcb, a.tol)
        c = counts(rep)
        all_rep[name] = {'counts': c, 'items': rep['items']}
        for k in tot:
            tot[k] += c[k]
        print(_report(name, c))
        if a.fix or a.write_counts:
            w = write_counts(pcb, rep, a.note)
            if w:
                print(f'      记账：铜 {w["copper_segs"]} 段 + {w["vias"]} 孔，'
                      f'空线头 {w["dangling"]["total"]}（口岸 {w["dangling"]["port_cuts"]}'
                      f' / 剩毛刺 {w["dangling"]["spurs"]}），'
                      f'累计清理 {w["dangling"]["trimmed"]["tracks"]} 段 +'
                      f' {w["dangling"]["trimmed"]["vias"]} 孔')
        if rep.get('regressed') or rep.get('unconn_regressed') or c['spurs']:
            bad += 1        # 一块只算一次（回退 + 有毛刺是同一块的两种说法）
        n_spur += 1 if c['spurs'] else 0
        n_rb += 1 if (rep.get('regressed') or rep.get('unconn_regressed')) else 0
        if c['spurs']:
            for i in rep['items']:
                if not i['keep']:
                    print(f'      毛刺：{i["kind"]} {i["net"]} {i["layer"]} '
                          f'长 {i["len"]} 空头 {[[c[0], c[1], f"边距 {c[2]}"] for c in i["cand"]]}')
        for i in rep['items']:
            if i.get('binds'):
                print(f'      载流断头（留）：{i["kind"]} {i["net"]} {i["layer"]} '
                      f'长 {i["len"]} 挨 {i["mates"]} 个同网对象')
    print(f'{"✓" if not bad else "✗"} 空线头门：{len(files) - bad} 块干净 / {n_spur} 块有毛刺'
          f' / {n_rb} 块回退；全库 空线头 {tot["dangling_total"]} = 口岸断头 {tot["port_cuts"]}'
          f' + 载流断头 {tot["binds"]} + 毛刺 {tot["spurs"]}')
    if a.out:
        Path(a.out).write_text(json.dumps(all_rep, ensure_ascii=False, indent=1) + '\n')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
