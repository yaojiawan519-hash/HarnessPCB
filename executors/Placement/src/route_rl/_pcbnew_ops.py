"""pcbnew 桥接脚本（M6）——由 KiCad 内置 Python（3.9 + pcbnew）单独执行。

KiCad 10 的 kicad-cli 已移除 dsn/ses 子命令，DSN⇄SES 只能走 app 内置
Python 的 pcbnew 模块。place-rl 的 venv 是系统 Python，无法 import pcbnew，
故把这几个操作收敛成这个独立脚本：eval 模块用 subprocess 调内置 Python
跑它，stdout 输出 JSON 供上层解析。

用法（argv[1] = 操作）:
    export_dsn  <in.kicad_pcb> <out.dsn> [clearance_um]
    import_ses  <in.ses> <base.kicad_pcb> <out.kicad_pcb>
    stats       <routed.kicad_pcb>          # 打印 JSON：线长/孔/线宽直方
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict

import pcbnew

try:  # 带 zone 的板：net/zone SWIG 调用无 wxApp 会 stdpbase 断言（同 merge_planes 坑）；
    import wx  # 须持引用——不持住则 GC 销毁 C++ app，后续 Get() 仍断言
    _app = wx.App(False)
except Exception:
    pass


def export_dsn(src: str, dst: str, clearance_um: str = "0") -> int:
    clearance_um = int(clearance_um)
    b = pcbnew.LoadBoard(src)
    pcbnew.ExportSpecctraDSN(b, dst)
    if clearance_um > 0:
        # FreeRouting 读 DSN 的 (rule (clearance N)) 作为布线间距；默认 N=板规 200um，
        # 与 DRC 判据同值 → FreeRouting 贴边挤压（实测 clearance 违规 0.177–0.199mm，
        # 全在 0.200mm 之下）。把布线间距抬到板规之上留裕量，DRC 仍按板规 0.2mm 判，
        # 布线板安全通过。只替换「不带 (type …) 的裸 clearance」——smd_smd 等类型
        # 特例（(clearance 50 (type smd_smd))）保留。
        txt = open(dst).read()
        txt = re.sub(r"\(clearance\s+\d+\)", f"(clearance {clearance_um})", txt)
        with open(dst, "w") as f:
            f.write(txt)
    return 0


def import_ses(ses: str, base: str, dst: str) -> int:
    b = pcbnew.LoadBoard(base)
    pcbnew.ImportSpecctraSES(b, ses)
    b.Save(dst)
    return 0


def stats(pcb: str) -> int:
    b = pcbnew.LoadBoard(pcb)
    tracks = list(b.GetTracks())
    vias = [t for t in tracks if t.GetClass() == "PCB_VIA"]
    traces = [t for t in tracks if t.GetClass() == "PCB_TRACK"]
    total_mm = sum(t.GetLength() for t in traces) / 1e6
    widths = Counter(round(t.GetWidth() / 1e6, 3) for t in traces)
    drills = Counter(round(v.GetDrillValue() / 1e6, 3) for v in vias)
    print(json.dumps({
        "n_traces": len(traces),
        "n_vias": len(vias),
        "wirelength_mm": round(total_mm, 2),
        "track_width_mm": dict(sorted(widths.items())),
        "via_drill_mm": dict(sorted(drills.items())),
    }, ensure_ascii=False))
    return 0


def _pt(v):
    """位置 → 按 10nm 取整的 (x, y)，用于端点/过孔/焊盘邻接匹配。"""
    return (round(v.x, -1), round(v.y, -1))


def _near(a, b, eps=500):
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def layer_hist(pcb: str) -> int:
    """走线/过孔按铜层分布（诊断信号是否漏到内层）。"""
    b = pcbnew.LoadBoard(pcb)
    traces = Counter()
    vias = Counter()
    for t in b.GetTracks():
        if t.GetClass() == "PCB_TRACK":
            traces[t.GetLayerName()] += 1
        elif t.GetClass() == "PCB_VIA":
            vias[t.GetLayerName()] += 1
    print(json.dumps({"traces_by_layer": dict(traces),
                      "vias_by_layer": dict(vias)}, ensure_ascii=False))
    return 0


def cleanup(pcb: str, out: str) -> int:
    """FreeRouting 产物清理：加宽低于板规的窄线 + 剪除悬空/孤立过孔。

    两个 DRC 违规源头都是 FreeRouting 的副产物：
      * ``track_width`` —— FreeRouting 用了比板最小线宽还细的信号线；
      * ``via_dangling`` —— 扇出过孔被弃用后留下的孤立/单侧短线过孔。

    剪除采用「迭代剪死端点」不动点算法：把任何一端是死端点（不接焊盘、不过孔、
    不接同层其它走线）的走线标为残段删除，随之孤立出来的过孔也删除，循环到稳定。
    只删死端残段，绝不会动 pad↔pad / pad↔via↔pad 的有效布线。
    """
    b = pcbnew.LoadBoard(pcb)
    ds = b.GetDesignSettings()
    min_w = getattr(ds, "m_TrackMinWidth", 0) or 0  # nm

    all_tracks = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"]
    all_vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]

    # 1) 加宽窄线到板最小线宽（修 track_width 违规）
    widened = 0
    if min_w > 0:
        for t in all_tracks:
            if t.GetWidth() < min_w:
                t.SetWidth(min_w)
                widened += 1

    # 2) 剪悬空：几何邻接（焊盘用包围盒，过孔/走线端点用 500nm 邻接）
    pad_boxes = [p.GetBoundingBox() for p in b.GetPads()]

    def in_pad(x, y):
        for bx in pad_boxes:
            if bx.GetLeft() <= x <= bx.GetRight() and bx.GetTop() <= y <= bx.GetBottom():
                return True
        return False

    def build_adj(tracks, vias):
        via_pts = {_pt(v.GetPosition()): v for v in vias}
        ep = defaultdict(list)
        for t in tracks:
            ep[(_pt(t.GetStart()), t.GetLayerName())].append(t)
            ep[(_pt(t.GetEnd()), t.GetLayerName())].append(t)
        return via_pts, ep

    tracks, vias = list(all_tracks), list(all_vias)
    removed_track = removed_via = 0
    for _ in range(200):  # 安全上界
        via_pts, ep = build_adj(tracks, vias)
        changed = False

        # (a) 删死端走线：任一端点「不通」（不接焊盘/过孔/同层其它走线）
        keep = []
        for t in tracks:
            L = t.GetLayerName()
            dead = False
            for v in (t.GetStart(), t.GetEnd()):
                p = _pt(v)
                if in_pad(*p):
                    continue
                if any(_near(p, vp) for vp in via_pts):
                    continue
                if any(o is not t for o in ep.get((p, L), ())):
                    continue
                dead = True
                break
            if dead:
                removed_track += 1
                changed = True
            else:
                keep.append(t)
        tracks = keep

        # (b) 删悬空过孔：只保留「跨 ≥2 层」或「落在焊盘上」的过孔
        via_pts, ep = build_adj(tracks, vias)
        keep_v = []
        for v in vias:
            p = _pt(v.GetPosition())
            if in_pad(*p):
                keep_v.append(v)
                continue
            layers = set()
            for t in tracks:
                if _near(p, _pt(t.GetStart())) or _near(p, _pt(t.GetEnd())):
                    layers.add(t.GetLayerName())
            if len(layers) >= 2:
                keep_v.append(v)
            else:
                removed_via += 1
                changed = True
        vias = keep_v

        if not changed:
            break

    # 写回：按 Python 对象身份删除被剪掉的走线/过孔
    keep_ids = {id(t) for t in tracks} | {id(v) for v in vias}
    for t in all_tracks:
        if id(t) not in keep_ids:
            b.Remove(t)
    for v in all_vias:
        if id(v) not in keep_ids:
            b.Remove(v)

    print(json.dumps({
        "widened": widened,
        "removed_track": removed_track,
        "removed_via": removed_via,
        "kept_track": len(tracks),
        "kept_via": len(vias),
    }, ensure_ascii=False))
    b.Save(out)
    return 0


_OPS = {"export_dsn": export_dsn, "import_ses": import_ses, "stats": stats,
        "cleanup": cleanup, "layer_hist": layer_hist}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _OPS:
        print(f"usage: {sys.argv[0]} {{{'/'.join(_OPS)}}} ...", file=sys.stderr)
        return 2
    return _OPS[argv[1]](*argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
