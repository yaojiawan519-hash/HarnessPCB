"""平面生成器（merge 法终步，PassSpec "planes" 的执行体）：

1. 删平面层上的电源走线（过孔全保留——通孔孔壁贯穿叠层，平面自动接上）；
   断言层纪律：平面层上不允许出现非平面网走线（信号只准 signal 层）。
2. 每个平面层倒整板实心 zone（bbox 内缩 EDGE_INSET，FULL 连接）。
3. refill 持久化后引擎 DRC 终验。

移植自工作台 merge_planes.py（一块 4 层板已验证：117 条内层走线删除、
GND 7193 / +3V3 8476 顶点、铜级 0）。pcbnew 经 KPY 子进程执行；
无 wxApp 时 zone/net SWIG 调用会触发断言，须先建 wx.App。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import env, metrics

EDGE_INSET = 475000       # 平面边界离板边 0.475mm
MIN_THICKNESS = 254000    # 0.254mm
ZONE_CLEARANCE = 200000   # 0.2mm（与板规一致）

_STRIP_TMPL = """
import pcbnew
b = pcbnew.LoadBoard({in_!r})
planes = {planes!r}
plane_nets = {{n for _, n in planes}}
inner = {{getattr(pcbnew, nm.replace('.', '_')) for nm, _ in planes}}
removed, bad = 0, []
for t in list(b.GetTracks()):
    layer = t.GetLayer()
    if layer in inner and not isinstance(t, pcbnew.PCB_VIA):
        net = t.GetNetname()
        if net in plane_nets:
            b.Remove(t)  # 单遍历同 wrapper 即删；id() 双遍历会 wrapper 复用误删+毒化 SWIG
            removed += 1
            continue
        bad.append((net, str(t.GetPosition())))
if bad:
    raise SystemExit('LAYER-DISCIPLINE VIOLATIONS: ' + repr(bad[:8]))
print('removed_inner_power_tracks', removed)
b.Save({stripped!r})
print('stripped_saved')
"""

_ZONE_TMPL = """
import wx
app = wx.App(False)  # 无 wxApp 时 zone/net SWIG 调用会触发 stdpbase 断言污染返回值
import pcbnew
b = pcbnew.LoadBoard({stripped!r})
planes = {planes!r}
ins, minth, clr = {inset!r}, {minth!r}, {zclr!r}
def add_zone(layer_name, net_name):
    z = pcbnew.ZONE(b)
    z.SetLayer(getattr(pcbnew, layer_name.replace('.', '_')))
    z.SetNetCode(b.FindNet(net_name).GetNetCode())
    z.SetMinThickness(minth)
    z.SetLocalClearance(clr)
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    bb = b.GetBoardEdgesBoundingBox()
    ol = z.Outline()
    ol.NewOutline()
    for x, y in [(bb.GetX() + ins, bb.GetY() + ins),
                 (bb.GetRight() - ins, bb.GetY() + ins),
                 (bb.GetRight() - ins, bb.GetBottom() - ins),
                 (bb.GetX() + ins, bb.GetBottom() - ins)]:
        ol.Append(pcbnew.VECTOR2I(x, y))
    b.Add(z)

for nm, net in planes:
    add_zone(nm, net)
b.Save({out!r})
print('zones_added', len(planes))
"""


def _kpy_run(code: str, ok_marker: str, what: str) -> None:
    r = subprocess.run([env.KPY, "-c", code], capture_output=True, text=True,
                       timeout=300)
    out = (r.stdout + r.stderr).strip()
    if r.returncode or ok_marker not in out:
        raise RuntimeError(f"planes {what} FAILED:\n{out[-1200:]}")


# --------------------------------------------------------------------------
# 平面网「缝合过孔」（stitch）：FreeRouting 的 power pass 偶尔漏连 1–3 个
# 平面网焊盘；平面铺好后这些 SMD 焊盘只要有一个落面过孔就能连上，不必回布线器。
# DRC 报 unconnected → 找到其中的平面网 SMD 焊盘 → 在焊盘外侧找一处净空落
# 0.3/0.6 过孔 + 0.2mm 短引线，重复至无平面网焊盘失连（上限 3 轮）。
# 两条硬规矩（10-29 mcu_stm32f103 实测补的）：①已经接到同网铜的焊盘不缝
# （否则重复钻孔 holes_co_located）②落点不许与任何既有过孔重合。
# --------------------------------------------------------------------------
_STITCH_TMPL = r'''
import math
import wx
wx.App(False)  # zone/net SWIG 调用需 wxApp 守卫
import pcbnew
b = pcbnew.LoadBoard({in_!r})
plane_nets = set({nets!r})
targets = {targets!r}          # [(ref, padnum, net)]
PATH = 300000                  # 引线中心到异网铜（half track 0.1 + 间距 0.2）
TRK_W = 200000
# 主选 0.6/0.3，挤不下退 0.5/0.3。**钻孔不许低于板规 0.3mm**——这一档原是
# 0.5/0.2，拼板时会产出 `drill_out_of_range`，而它不在铜级清单里，门禁看不见
# （fanout.py 的 VIAS 已先修过同一处，这里是同一个洞）。
VIAS = [(600000, 300000), (500000, 300000)]

# 障碍用真实几何判定，不用 bbox——45° 长走线的 bbox 是一大片矩形，
# 会把旁边合法空位全判死（第一版 stitch 对三个焊盘全 NOSPOT 的根因）。
pads, tracks = [], []
for fp in b.GetFootprints():
    for pad in fp.Pads():
        pads.append(pad)
for t in b.GetTracks():
    tracks.append(t)
_pad_net = [p.GetNetname() for p in pads]
_trk_net = [t.GetNetname() for t in tracks]
vias = [t for t in tracks if t.GetClass() == 'PCB_VIA']


def _wired(pad, net):
    """这只焊盘**已经接到同网铜**了吗？接到的不用缝，缝了就是多一个重复孔。

    10-29 mcu_stm32f103 实测：+3.3V 的两个失连脚同时出现在 DRC 的
    unconnected_items 里，stitch 不去分辨谁是真失连，给**已经有扇出过孔的
    U1-1** 又落了一枚 0.6/0.3 过孔 + 一条同形引线，位置一模一样
    (10.318, 8.387) → `holes_co_located`（两个钻孔重合），而真正没出路的
    U1-9 仍然悬空。"""
    c = pad.GetPosition()
    for v in vias:
        if v.GetNetname() != net:
            continue
        vp = v.GetPosition()
        if pad.HitTest(vp, v.GetWidth() // 2 + 50000):
            return True
    for t in tracks:
        if t.GetNetname() != net or t.GetClass() == 'PCB_VIA':
            continue
        if pad.HitTest(t.GetStart(), 50000) or pad.HitTest(t.GetEnd(), 50000):
            return True
    return False


def _via_free(x, y, vd):
    """落点不许和**任何**既有过孔重合（同网也不行：同网重合孔 = 重复钻孔）。

    `_hits` 按设计放过同网铜（引线要贴到同网焊盘上），所以这里必须单独判。"""
    for v in vias:
        vp = v.GetPosition()
        if math.hypot(x - vp.x, y - vp.y) < (vd + v.GetWidth()) / 2 + 100000:
            return False
    return True

def _hits(x, y, net, m, layer=None):
    """layer=None → 全层障碍（过孔孔壁贯穿所有层，必须全层避让）；
    给 layer → 只看该层铜（F.Cu 引线不该被 B.Cu/内层走线挡住）。"""
    pt = pcbnew.VECTOR2I(int(x), int(y))
    for i, pad in enumerate(pads):
        # 只放过**同网**焊盘。空网名焊盘（未连接的脚、机械脚）照样是铜，必须避让：
        # 早先写成 `in (net, '')`，把无网焊盘当透明，缝合过孔落到 MCU 的 NC 脚上
        # （10-27 块级几何：clearance 50µm + hole_clearance 200µm 两条铜级）。
        if _pad_net[i] == net:
            continue
        if layer is not None and not pad.IsOnLayer(layer):
            continue
        if pad.HitTest(pt, m):
            return ('pad', _pad_net[i])
    for i, t in enumerate(tracks):
        if _trk_net[i] == net:      # 同证：无网走线也是铜
            continue
        if layer is not None and t.GetLayer() != layer:
            continue
        if t.GetEffectiveShape().Collide(pt, m):
            return ('track', _trk_net[i])
    return None

def path_free(x0, y0, x1, y1, net, pad_layer):
    """引线整条不得碰异网铜（只查端点会漏掉中途横穿 → tracks_crossing）。"""
    n = max(2, int(math.hypot(x1 - x0, y1 - y0) / 40000) + 1)
    for i in range(n + 1):
        t = i / n
        if _hits(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, net, PATH, pad_layer):
            return False
    return True

# 候选：32 方向 × 0.4–6.0mm（0.1 步长），按半径升序（贴得越近越好）。
DIRS = [(math.cos(i * math.pi / 16), math.sin(i * math.pi / 16)) for i in range(32)]
CANDS = [(int(cx * r), int(cy * r)) for r in range(400000, 6100000, 100000)
         for cx, cy in DIRS]

def find_straight(c, net, clr, layer):
    for dx, dy in CANDS:
        x, y = c.x + dx, c.y + dy
        if not _via_free(x, y, VIAS[0][0]):
            continue
        if _hits(x, y, net, clr) is None and path_free(c.x, c.y, x, y, net, layer):
            return (x, y)
    return None

def find_elbow(c, net, clr, layer):
    """L 形引线：直线被邻脚夹死时，从焊盘先沿 x（或 y）出，再拐到落点。"""
    for dx, dy in CANDS:
        x, y = c.x + dx, c.y + dy
        if _hits(x, y, net, clr) is not None or not _via_free(x, y, VIAS[0][0]):
            continue
        for (ex, ey) in ((x, c.y), (c.x, y)):
            if path_free(c.x, c.y, ex, ey, net, layer) and path_free(ex, ey, x, y, net, layer):
                return (x, y, ex, ey)
    return None

def via_in_pad(pad, net, r):
    """最后手段：过孔盘整个落在 SMD 焊盘内（ESD 地脚这类被邻脚夹死的位置，
    母盘 1.325×0.6 能容 0.5/0.2；无引线，孔壁直达平面）。"""
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
        if inside and _hits(x, y, net, r + 200000) is None and _via_free(x, y, 2 * r):
            return (x, y)
    return None

added = []
for ref, num, net in targets:
    fp = b.FindFootprintByReference(ref)
    if fp is None:
        print('MISS', ref); continue
    pad = next((p for p in fp.Pads() if p.GetNumber() == num), None)
    if pad is None or pad.GetNetname() != net:
        print('MISSPAD', ref, num); continue
    if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
        continue               # 通孔焊盘本身已贯穿叠层，不必缝
    if _wired(pad, net):
        print('WIRED', ref, num, net); continue   # 已接铜：再落孔就是重复钻孔
    c = pad.GetPosition()
    chosen = None
    for vd, dr in VIAS:
        clr = vd // 2 + 200000
        spot = find_straight(c, net, clr, pad.GetLayer())
        if spot:
            chosen = (spot, vd, dr, None, 'direct'); break
        el = find_elbow(c, net, clr, pad.GetLayer())
        if el:
            chosen = ((el[0], el[1]), vd, dr, (el[2], el[3]), 'elbow'); break
    if chosen is None:
        vd, dr = VIAS[-1]
        spot = via_in_pad(pad, net, vd // 2)
        if spot is None:
            print('NOSPOT', ref, num, net); continue
        chosen = (spot, vd, dr, None, 'inpad')
    spot, vd, dr, elbow, how = chosen
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(*spot))
    v.SetWidth(vd)
    v.SetDrill(dr)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNetCode(pad.GetNetCode())
    b.Add(v)
    if how != 'inpad':         # inpad 的孔盘与焊盘重叠，不需要引线
        pts = [c, pcbnew.VECTOR2I(*elbow), v.GetPosition()] if elbow else [c, v.GetPosition()]
        for a, z in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(a)
            t.SetEnd(z)
            t.SetWidth(TRK_W)
            t.SetLayer(pad.GetLayer())
            t.SetNetCode(pad.GetNetCode())
            b.Add(t)
    added.append((ref, num, net, how, round((spot[0] - c.x) / 1e6, 2),
                  round((spot[1] - c.y) / 1e6, 2)))
b.Save({out!r})
print('stitched', len(added), added)
'''


def _unconnected_plane_pads(drc: dict, plane_nets: set[str]) -> list[tuple]:
    """从 DRC 的 unconnected_items 里挑出「平面网的 SMD 焊盘」目标。"""
    import re as _re
    out: list[tuple] = []
    for u in drc.get("unconnected_items") or []:
        for it in u.get("items") or []:
            m = _re.match(r"Pad (\S+) \[([^\]]+)\] of (\S+) on ", it.get("description", ""))
            if m and m.group(2) in plane_nets:
                key = (m.group(3), m.group(1), m.group(2))
                if key not in out:
                    out.append(key)
    return out

def run_planes_pass(board: Path, out_dir: Path, stackup, run_id: str) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stripped = out_dir / "_stripped.kicad_pcb"
    out = out_dir / "planes_out.kicad_pcb"
    planes = [(l.name, l.net) for l in stackup.plane_layers]

    _kpy_run(_STRIP_TMPL.format(in_=str(board), planes=planes,
                                stripped=str(stripped)),
             "stripped_saved", "strip")
    _kpy_run(_ZONE_TMPL.format(stripped=str(stripped), planes=planes,
                               inset=EDGE_INSET, minth=MIN_THICKNESS,
                               zclr=ZONE_CLEARANCE, out=str(out)),
             "zones_added", "zone")

    # 缝合：平面网 SMD 焊盘若失连，落一枚过孔接通当前平面（最多 3 轮）。
    # refill 后再判（否则平面指标失真）。
    drc = None
    for _ in range(3):
        drc = env.kicad_drc(out, refill=True)
        targets = _unconnected_plane_pads(drc, stackup.plane_nets)
        if not targets:
            break
        print(f"  [planes] stitch {len(targets)} pad(s): {targets}")
        r = subprocess.run(
            [env.KPY, "-c", _STITCH_TMPL.format(
                in_=str(out), nets=[n for _, n in planes], targets=targets,
                clr=ZONE_CLEARANCE + 300000, out=str(out))],
            capture_output=True, text=True, timeout=300)
        tail = (r.stdout + r.stderr)
        if r.returncode or "stitched" not in tail:
            raise RuntimeError(f"planes stitch FAILED:\n{tail[-1200:]}")
        if "stitched 0 " in tail:
            print("  [planes] stitch 无可落孔位，停止重试")
            break

    drc = env.kicad_drc(out, refill=True)
    outcome = metrics.summarize_drc(drc, stackup)
    try:
        outcome["layer_hist"] = env.pcbnew_json("layer_hist", str(out))
    except Exception:
        outcome["layer_hist"] = None
    print(f"  [planes] un={outcome['un']} copper={outcome['copper']}")
    return {
        "run_id": run_id, "board_id": stackup.board_id, "pass": "planes",
        "iter": 0, "action": {"edge_inset_um": EDGE_INSET // 1000},
        "board_in": str(board), "board_out": str(out), "ses_ok": None,
        "outcome": outcome, "score": [outcome["un"], outcome["copper"]],
    }
