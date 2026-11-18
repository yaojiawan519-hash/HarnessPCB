#!/usr/bin/env python3
"""L0 静态布局规则检查器——不布线，只读 pad/courtyard 坐标，毫秒级。

用法（须 KiCad 内置 Python）:
  KPY eval_rules.py <board.kicad_pcb> [--r1-max 0.35] [--r2-mm 10] \
      [--r3-ov 0.25] [--r4-clear 0.5] [--r4-gap 1.2]

四条规则（量法 + 阈值 + 门禁，rules-must-be-gates 口径）:
  R1 芯片方位: 每脚「脚→对端最近 pad」向量 vs「芯片中心→脚」向量夹角>110°
     计背朝一次；背朝比超阈值 = 方向放反（HPM5E00 U4/PHY 各 180° 的教训）
  R2 晶振就近: 晶振本体到芯片 XI/XO 脚距离（超阈值 = 时钟件离服务对象太远）
  R3 差分同排: 差分对成员网的 pad 包络互相重叠超比 = 两对差分绞在一起
     （PHY↔变压器交叉接的教训：换位同排后重叠归零）
  R4 逃逸走廊: 芯片每边 3mm 带内被正面件覆盖的比例 + 最小间隙（西走廊
     被 DCDC 簇堵死的教训；背面藏件不挡 F.Cu 逃逸，不计）

退出码: 0=全绿, 1=有红（供 stress_cycle/CI 当门禁）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import pcbnew

NM = 1e6


# ---------------------------------------------------------------- 基础设施
def courtyard(fp):
    minx = miny = 1e9
    maxx = maxy = -1e9
    for gi in fp.GraphicalItems():
        if gi.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            g = gi.GetBoundingBox()
            minx = min(minx, g.GetX())
            maxx = max(maxx, g.GetRight())
            miny = min(miny, g.GetY())
            maxy = max(maxy, g.GetBottom())
    if minx > 1e8:
        p = fp.GetPosition()
        minx, maxx = p.x - int(0.5 * NM), p.x + int(0.5 * NM)
        miny, maxy = p.y - int(0.5 * NM), p.y + int(0.5 * NM)
    return (minx, miny, maxx, maxy)


def build_maps(board):
    """net → [ (ref, padnum, x, y) ]；ref → footprint。"""
    nets: dict[str, list] = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            n = p.GetNetname()
            if n:
                nets.setdefault(n, []).append(
                    (fp.GetReference(), p.GetNumber(),
                     p.GetPosition().x, p.GetPosition().y))
    return nets


def is_ic(fp):
    """IC 判定：≥12 pad 且 ref 不是连接器/测试点（J*/P*/CN*/TP*）。"""
    ref = fp.GetReference()
    if re.match(r"^(J|P|CN|TP)\d", ref):
        return False
    return len(list(fp.Pads())) >= 12


_SKIP_NET = re.compile(r"^(\+|GND$|Earth$|unconnected-)", re.I)
_POWERISH = re.compile(r"(VDD|VCC|VBUS|3V3|3\.3V|5V|VREF|VANA|DCDC|/SOC$|ECAT_3)", re.I)


def routable_net(net: str, pads) -> bool:
    if _SKIP_NET.match(net):
        return False
    if len(pads) > 6:            # 电源轨/大总线：方向语义弱，噪声大
        return False
    if _POWERISH.search(net):
        return False
    return True


# ---------------------------------------------------------------- R1 方位
def r1_orientation(board, nets, max_ratio):
    findings = []
    for fp in board.GetFootprints():
        if not is_ic(fp):
            continue
        c = fp.GetPosition()
        cx, cy = c.x, c.y
        total = back = 0
        worst = []
        for p in fp.Pads():
            n = p.GetNetname()
            if not n or not routable_net(n, nets.get(n, [])):
                continue
            px, py = p.GetPosition().x, p.GetPosition().y
            dests = [(x, y, r, pn) for (r, pn, x, y) in nets[n]
                     if r != fp.GetReference()]
            if not dests:
                continue
            dx, dy, dr, dpn = min(dests, key=lambda d: (d[0]-px)**2 + (d[1]-py)**2)
            vpx, vpy = px - cx, py - cy
            wdx, wdy = dx - px, dy - py
            vn = (vpx*vpx + vpy*vpy) ** .5 or 1
            wn = (wdx*wdx + wdy*wdy) ** .5 or 1
            cos = (vpx*wdx + vpy*wdy) / (vn * wn)
            total += 1
            if cos < -0.34:                        # >110°
                back += 1
                if len(worst) < 4:
                    worst.append(f"{n.split('/')[-1]}({p.GetNumber()}→{dr}.{dpn})")
        if total < 8:
            continue
        ratio = back / total
        mark = "FAIL" if ratio > max_ratio else "ok"
        findings.append((mark, f"{fp.GetReference()}: 背朝 {back}/{total}"
                         f" ({ratio:.0%}) {' '.join(worst)}"))
    return findings


# ---------------------------------------------------------------- R2 晶振
_XTAL_NET = re.compile(r"(XTAL|XTLI|XTLO|[-_/](XI|XO)$)", re.I)


def r2_crystal(board, nets, max_mm):
    findings = []
    xnets = {n: ps for n, ps in nets.items() if _XTAL_NET.search(n)}
    for n, ps in xnets.items():
        chip_pads = [(x, y, r) for (r, pn, x, y) in ps if is_ic_ref(board, r)]
        passive = [(x, y, r, pn) for (r, pn, x, y) in ps if not is_ic_ref(board, r)]
        if not chip_pads or len(passive) < 2:
            continue
        # 晶振本体 = 同挂两条晶振网的 2-pad 件
        for fp in board.GetFootprints():
            pads_n = {p.GetNetname() for p in fp.Pads()}
            hits = pads_n & set(xnets)
            if len(hits) == 2 and len(list(fp.Pads())) == 2:
                q = fp.GetPosition()
                for (x, y, r) in chip_pads:
                    d = ((x-q.x)**2 + (y-q.y)**2) ** .5 / NM
                    if d > max_mm:
                        findings.append(("FAIL", f"{fp.GetReference()}→{r}"
                                         f"({n.split('/')[-1]}) {d:.1f}mm"))
                    else:
                        findings.append(("ok", f"{fp.GetReference()}→{r}"
                                         f" {d:.1f}mm"))
                break
    return dedupe(findings)


def is_ic_ref(board, ref):
    fp = board.FindFootprintByReference(ref)
    return fp is not None and is_ic(fp)


def dedupe(findings):
    seen, out = set(), []
    for m, t in findings:
        if t not in seen:
            seen.add(t)
            out.append((m, t))
    return out


# ---------------------------------------------------------------- R3 差分同排
_PAIR_SUF = [("_P", "_N"), ("+", "-"), ("_p", "_n")]


def pair_key(net):
    # key 用全路径去后缀：P0/P1 各有 TD_P/TD_N，basename 会撞车丢对
    for a, b in _PAIR_SUF:
        for s in (a, b):
            if net.endswith(s) and len(net) > len(s):
                return net[: -len(s)]
    return None


def r3_diff(board, nets, max_overlap):
    groups: dict[str, list[str]] = {}
    for n, ps in nets.items():
        k = pair_key(n)
        if k and len(ps) >= 2:
            groups.setdefault(k, []).append(n)
    pairs = []
    for k, ns in groups.items():
        if len(ns) != 2:
            continue
        xs = [x for n in ns for (_, _, x, y) in nets[n]]
        ys = [y for n in ns for (_, _, x, y) in nets[n]]
        refs = frozenset(r for n in ns for (r, _, _, _) in nets[n])
        pairs.append((k, min(xs), min(ys), max(xs), max(ys), refs))
    findings = []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            (ka, ax0, ay0, ax1, ay1, ra), (kb, bx0, by0, bx1, by1, rb) = \
                pairs[i], pairs[j]
            if ra == rb:
                continue      # 同信道收发对（PHY↔变压器 TX/RX）：包络天然重叠
            ox = max(0, min(ax1, bx1) - max(ax0, bx0))
            oy = max(0, min(ay1, by1) - max(ay0, by0))
            inter = ox * oy
            amin = min((ax1-ax0)*(ay1-ay0), (bx1-bx0)*(by1-by0)) or 1
            r = inter / amin
            if r > max_overlap:
                def tag(k, refs):
                    rs = "↔".join(sorted(refs)[:2])
                    return f"{k.split('/')[-1]}({rs})"
                findings.append(("FAIL", f"{tag(ka, ra)} × {tag(kb, rb)}"
                                 f" 包络重叠 {r:.0%}"))
    return findings


# ---------------------------------------------------------------- R4 走廊
def r4_corridor(board, min_clear, min_gap):
    findings = []
    ics = [fp for fp in board.GetFootprints() if is_ic(fp) and not fp.IsFlipped()]
    fronts = [(fp.GetReference(), courtyard(fp)) for fp in board.GetFootprints()
              if not fp.IsFlipped()]
    for fp in ics:
        x0, y0, x1, y1 = courtyard(fp)
        ref = fp.GetReference()
        # 本芯片的随行件（共网的被动件）不算走廊障碍——它们是引脚的目的地
        my_nets = {p.GetNetname() for p in fp.Pads()}
        for side, axis, edge in [
            ("西", "x", x0),
            ("东", "x", x1),
            ("北", "y", y0),
            ("南", "y", y1),
        ]:
            band = int(3.0 * NM)
            covered = []          # [(沿边起点, 终点, 间隙)]
            for oref, (ox0, oy0, ox1, oy1) in fronts:
                if oref == ref:
                    continue
                ofp = board.FindFootprintByReference(oref)
                if ofp and {p.GetNetname() for p in ofp.Pads()} & my_nets \
                        and len(list(ofp.Pads())) <= 4:
                    continue          # 随行被动件豁免
                if axis == "x":
                    if oy1 < y0 or oy0 > y1:
                        continue
                    lo, hi = max(oy0, y0), min(oy1, y1)
                    if edge == x0:            # 西边：障碍在西侧 3mm 内
                        if ox1 <= x0 and ox1 > x0 - band:
                            covered.append((lo, hi, x0 - ox1))
                    else:                     # 东边
                        if ox0 >= x1 and ox0 < x1 + band:
                            covered.append((lo, hi, ox0 - x1))
                else:
                    if ox1 < x0 or ox0 > x1:
                        continue
                    lo, hi = max(ox0, x0), min(ox1, x1)
                    if edge == y0:            # 北边
                        if oy1 <= y0 and oy1 > y0 - band:
                            covered.append((lo, hi, y0 - oy1))
                    else:                     # 南边
                        if oy0 >= y1 and oy0 < y1 + band:
                            covered.append((lo, hi, oy0 - y1))
            if not covered:
                continue
            edge_len = (y1 - y0) if axis == "x" else (x1 - x0)
            cov = sorted((lo, hi) for lo, hi, _ in covered)
            merged, cl = [], 0.0
            for lo, hi in cov:
                if merged and lo <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
                else:
                    merged.append([lo, hi])
            tot = sum(hi - lo for lo, hi in merged)
            clear = 1 - tot / edge_len
            gap = min(g for _, _, g in covered) / NM
            bad = clear < min_clear and gap < min_gap
            findings.append(("FAIL" if bad else "ok",
                             f"{ref} {side}侧: 净空 {clear:.0%}, 最近障碍 {gap:.1f}mm"))
    return findings


# ---------------------------------------------------------------- 主流程
def _skel_pads(skel_path):
    """读骨架 JSON（兼容扁平数组 / {dogbone, channel}），返回有逃逸的 pad 集合。"""
    d = json.load(open(skel_path))
    if isinstance(d, list):
        return {s["pad"] for s in d}
    return {s["pad"] for s in d.get("dogbone", [])} \
        | {c["pad"] for c in d.get("channel", [])}


def r5_skeleton(board, skel_path):
    """骨架完整性（查漏）：每个 U4 信号球必须有 dogbone 孔或通道 F.Cu
    逃逸（channel）。内部信号球两者都无 = 死球。
    PA16/18/20 成片红的教训：骨架只提取了 dogbone，漏了通道逃逸。"""
    if not skel_path:
        return [("skip", "未给 --skel，跳过 R5")]
    skel_pads = _skel_pads(skel_path)
    findings = []
    for fp in board.GetFootprints():
        if fp.GetReference() != "U4":
            continue
        missing = []
        for p in fp.Pads():
            n = p.GetNetname()
            if not n or "GND" in n or _POWERISH.search(n):
                continue
            pn = p.GetNumber()
            if pn in skel_pads:
                continue
            is_edge = (pn.endswith("1") or pn.endswith("14")
                       or pn.startswith("A") or pn.startswith("N")
                       or pn.startswith("P"))
            if not is_edge:
                missing.append(f"{pn}({n.split('/')[-1]})")
        if missing:
            findings.append(("FAIL", f"U4 缺逃逸 {len(missing)} 球: "
                             + " ".join(sorted(missing)[:14])))
        else:
            findings.append(("ok", "U4 信号球全部有 dogbone 或为边缘球"))
    return findings


def r6_escape(board, skel_path, min_clear):
    """逃逸遮挡（查挡）：边缘球向外 F.Cu 逃逸方向 3mm 内有无正面件挡道。
    dogbone 球由骨架保证（R5 已查）；随行被动件（共网 ≤4 pad）豁免。"""
    if not skel_path:
        return [("skip", "未给 --skel，跳过 R6")]
    skel_pads = _skel_pads(skel_path)
    findings = []
    for fp in board.GetFootprints():
        if fp.GetReference() != "U4":
            continue
        my_nets = {p.GetNetname() for p in fp.Pads()}
        fronts = [(r, courtyard(f), {p.GetNetname() for p in f.Pads()})
                  for f in board.GetFootprints()
                  if (r := f.GetReference()) != "U4" and not f.IsFlipped()
                  and len(list(f.Pads())) >= 2]
        blocked = []
        for p in fp.Pads():
            n = p.GetNetname()
            if not n or "GND" in n or _POWERISH.search(n):
                continue
            pn = p.GetNumber()
            if pn in skel_pads:
                continue
            direc = None
            if pn.endswith("1"):
                direc = (1, 0)
            elif pn.endswith("14"):
                direc = (-1, 0)
            elif pn.startswith("A"):
                direc = (0, -1)
            elif pn.startswith("N") or pn.startswith("P"):
                direc = (0, 1)
            if not direc:
                continue
            px, py = p.GetPosition().x, p.GetPosition().y
            hw = int(0.4 * NM)   # 逃逸走廊半宽（球间距 0.8mm）
            rng = int(min_clear * NM)
            if direc[0] > 0:
                corr = (px, py - hw, px + rng, py + hw)
            elif direc[0] < 0:
                corr = (px - rng, py - hw, px, py + hw)
            elif direc[1] < 0:
                corr = (px - hw, py - rng, px + hw, py)
            else:
                corr = (px - hw, py, px + hw, py + rng)
            for ref, (x0, y0, x1, y1), nets in fronts:
                if nets & my_nets and len(nets) <= 4:
                    continue          # 随行被动件豁免
                if x0 < corr[2] and x1 > corr[0] and y0 < corr[3] and y1 > corr[1]:
                    blocked.append(f"{pn}←{ref}")
        if blocked:
            findings.append(("FAIL", f"U4 边缘球逃逸被挡 {len(set(blocked))}: "
                             + " ".join(sorted(set(blocked))[:10])))
        else:
            findings.append(("ok", "U4 边缘球 F.Cu 逃逸畅通"))
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--r1-max", type=float, default=0.35)
    ap.add_argument("--r2-mm", type=float, default=10.0)
    ap.add_argument("--r3-ov", type=float, default=0.25)
    ap.add_argument("--r4-clear", type=float, default=0.5)
    ap.add_argument("--r4-gap", type=float, default=1.2)
    ap.add_argument("--skel", default="", help="骨架 JSON（查 R5/R6，缺省跳过）")
    ap.add_argument("--r6-clear", type=float, default=3.0)
    a = ap.parse_args()

    board = pcbnew.LoadBoard(a.board)
    nets = build_maps(board)
    rules = [
        ("R1 芯片方位(背朝比≤%.0f%%)" % (a.r1_max * 100),
         r1_orientation(board, nets, a.r1_max)),
        ("R2 晶振就近(≤%.0fmm)" % a.r2_mm, r2_crystal(board, nets, a.r2_mm)),
        ("R3 差分同排(包络重叠≤%.0f%%)" % (a.r3_ov * 100),
         r3_diff(board, nets, a.r3_ov)),
        ("R4 逃逸走廊(净空≥%.0f%%或间隙≥%.1fmm)" % (a.r4_clear * 100, a.r4_gap),
         r4_corridor(board, a.r4_clear, a.r4_gap)),
        ("R5 骨架完整性", r5_skeleton(board, a.skel)),
        ("R6 逃逸遮挡(≤%.0fmm)" % a.r6_clear, r6_escape(board, a.skel, a.r6_clear)),
    ]
    n_fail = 0
    print(f"== L0 静态布局规则 · {a.board.split('/')[-1]} ==")
    for name, findings in rules:
        fails = sum(1 for m, _ in findings if m == "FAIL")
        n_fail += fails
        print(f"\n[{name}]  {'全绿' if not findings else f'{fails} 红 / {len(findings)} 项'}")
        for m, t in findings:
            print(f"  {m:4s} {t}")
    print(f"\n== 门禁: {'全绿 ✅' if n_fail == 0 else f'{n_fail} 红 ❌'} ==")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
