"""拆除走线：demo/预布线板 → 干净基板（删 PCB_TRACK + PCB_VIA，留 footprint/zone/图形）。

由 KiCad 内置 Python（KPY）单独执行——同 _pcbnew_ops.py 的运行方式，但属
route_rl 私有（不动 place_rl 共享文件，保 l0_equiv_check 的字节等价纪律）。
删除按 _pcbnew_ops 实证过的惯用法：GetTracks() 过滤 GetClass() 后 b.Remove()。

用法:
    KPY unroute_pcb.py <in.kicad_pcb> <out.kicad_pcb> [stats]

  - out ≠ in（demo 原件永不覆写）；第 3 参给 "stats" 时只读不写（盘点用）。
  - zone 保留并**重填**：原填充对应 demo 老走线，拆线后即陈旧——DRC 判
    track↔zone 会假爆（探针实测 pic@245 clearance 154→refill 后 3、
    interf 332→0）。stackup 推断按 zone 判 pass 计划（见 R0 记录）。
  - stdout 一行 JSON：拆前/拆后 track·via 计数 + 板规模（nets/footprints/
    pads/zones/层列表），供扫描集选板与结果核对。
"""
from __future__ import annotations

import json
import re
import sys

import pcbnew

try:  # 带 zone 的板：net/zone SWIG 调用无 wxApp 会 stdpbase 断言（同 _pcbnew_ops 头部）
    import wx  # 须持引用——不持住则 GC 销毁 C++ app，后续 Get() 仍断言
    _app = wx.App(False)
except Exception:
    pass


def _survey(b) -> dict:
    tracks = list(b.GetTracks())
    vias = [t for t in tracks if t.GetClass() == "PCB_VIA"]
    traces = [t for t in tracks if t.GetClass() == "PCB_TRACK"]
    zones = list(b.Zones())                  # KiCad 10：BOARD.Zones()，无 GetZones
    fps = list(b.GetFootprints())
    pads = sum(len(f.Pads()) for f in fps)
    nets = b.GetNetInfo()
    layers = sorted({b.GetLayerName(t.GetLayer()) for t in traces}
                    | {b.GetLayerName(z.GetLayer()) for z in zones})
    return {
        "n_track": len(traces), "n_via": len(vias),
        "n_zone": len(zones), "n_footprint": len(fps), "n_pad": pads,
        "n_net": len(list(nets.NetsByNetcode().keys())) - 1,  # 净掉 netcode 0
        "trace_layers": layers,
        "zone_nets": sorted({z.GetNetname() for z in zones}),
    }


def _normalize_copper_names(b) -> dict[str, str]:
    """legacy 层名（top_layer / top_copper 等）→ 规范名（F.Cu/B.Cu/InN.Cu）。

    dsn_edit/stackup/planes 全按层名匹配，DSN 导出也写显示名——demo 老板存
    什么名 DSN 就是什么名。此处只改显示名（layer id 不动），下游与门板一致。
    """
    ren = {}
    for lid in b.GetEnabledLayers().CuStack():
        if lid == pcbnew.F_Cu:
            canon = "F.Cu"
        elif lid == pcbnew.B_Cu:
            canon = "B.Cu"
        else:
            canon = f"In{lid - pcbnew.In1_Cu + 1}.Cu"
        old = b.GetLayerName(lid)
        if old != canon:
            b.SetLayerName(lid, canon)
            ren[old] = canon
    return ren


def _refill_zones(b) -> int:
    """重填全部 zone（拆线后、保存前）。

    demo 原件的 zone 填充对应它自己的老走线——unroute 删走线不删填充，
    留下的陈旧多边形会让"基板状态判定"（run_pass 首行 DRC，refill=False）
    拿旧几何当真相。重填一次让基板自带与新走线无关的干净填充。
    """
    filler = pcbnew.ZONE_FILLER(b)
    filler.Fill(list(b.Zones()))
    return len(list(b.Zones()))


def _normalize_zone_clearance(b, clearance_nm: int = 200_000) -> dict:
    """zone local clearance → 板规 0.2mm。

    legacy demo 的 GND zone 判距 0.5/0.508mm，而阶梯最高档 260µm < 0.5mm——
    DRC 的 track↔zone 判距 FreeRouting 全程无法满足，整条阶梯 copper 平响应
    （实测探针：pic@245 → clearance 159、interf@245 → clearance 348，全是 vs
    zone）。归一到与板规一致的 0.2mm（两板 setup 均 (clearance 0.2)），
    DSN 抬 245 的裕量对 zone 同样生效。热焊盘缝隙（thermal_gap）不动——
    它只塑形连接辐条，starved_thermal 是板固有画像。
    """
    ren = {}
    for z in b.Zones():
        old = z.GetLocalClearance()
        if old != clearance_nm:
            z.SetLocalClearance(clearance_nm)
            ren[z.GetNetname()] = [old / 1e6, clearance_nm / 1e6]
    return ren


def main() -> int:
    src, dst = sys.argv[1], sys.argv[2]
    stats_only = len(sys.argv) > 3 and sys.argv[3] == "stats"
    if not stats_only and src == dst:
        print(json.dumps({"error": "in == out：demo 原件禁止覆写"}))
        return 2
    b = pcbnew.LoadBoard(src)
    if b is None:
        print(json.dumps({"error": f"LoadBoard None: {src}"}))
        return 2
    renamed = _normalize_copper_names(b)
    zone_clr = _normalize_zone_clearance(b)
    before = _survey(b)
    if not stats_only:
        for t in list(b.GetTracks()):
            b.Remove(t)
        n_filled = _refill_zones(b)
        b.Save(dst)
        # 同进程二次 LoadBoard 会拿到裸 SwigPyObject——改为对保存文件文本计数
        # （KiCad 10 保存为多行 (segment\n (start…)：正则按 token 后空白匹配，
        # 单行/多行都吃；`\(segment \(start` 在多行格式下恒 0 是假阴性）
        txt = open(dst, encoding="utf-8").read()
        after = {"n_track_txt": len(re.findall(r"\(segment\s", txt)),
                 "n_via_txt": len(re.findall(r"\(via\s", txt))}
    else:
        after = None
    print(json.dumps({"src": src, "dst": None if stats_only else dst,
                      "renamed_layers": renamed,
                      "zone_clearance_mm": zone_clr,
                      "zones_refilled": None if stats_only else n_filled,
                      "before": before, "after": after}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
