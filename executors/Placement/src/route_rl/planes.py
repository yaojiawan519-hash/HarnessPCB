"""平面生成器（merge 法终步，PassSpec "planes" 的执行体）：

1. 删平面层上的电源走线（过孔全保留——通孔孔壁贯穿叠层，平面自动接上）；
   断言层纪律：平面层上不允许出现非平面网走线（信号只准 signal 层）。
2. 每个平面层倒整板实心 zone（bbox 内缩 EDGE_INSET，FULL 连接）。
3. refill 持久化后引擎 DRC 终验。

移植自 eval_out/stm32_plc/merge_planes.py（该板已验证：117 条内层走线删除、
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

    drc = env.kicad_drc(out, refill=True)  # refill 持久化后再判（否则平面指标失真）
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
