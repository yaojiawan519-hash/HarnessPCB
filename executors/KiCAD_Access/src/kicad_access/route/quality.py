"""走线质量打分器（Q0，2026-09-29）。

设计意图：完成度（未布通/铜违规）只是门槛，"走线傻不傻"需要可算的
风格度量。本模块对任意 .kicad_pcb 输出质量向量，供三条线复用：
Q0 对判（人工板 vs FreeRouting）、Q1 引擎代价调参、Q3 RL 奖励。

度量口径（全部只算"信号网"= ≥2 盘、非 zone 网、非 `unconnected-(` 占位
网——zone 网走铺铜，轨道不计，两侧同口径）：

- 门槛：unconn（_pads_connected 判不连通的网数）——非 0 时质量分无意义
- detour：len_ratio = 轨道总长 / 盘 HPWL——绕行系数，**对放置归一**
  （人工板与我们的板放置不同，绝对线长不可比，此比值可比）
- vias：过孔数、via_per_net——FreeRouting 出了名的孔多
- bends：同网同层 degree-2 拼点处方向改变数（T 型交叉/接盘不算弯）
- non45：偏离 0/45/90° 超过 1° 的段数与其长度占比
- 密度：bends/10mm、segs/10mm——碎不碎

秩序刻度（2026-09-30 补）：interf_u 对判的视觉验证证实 FR 输出"难看"
而效率刻度全赢——现行六格只量"省"不量"齐"。三格新刻度全部纯几何、
按长度加权，量的就是眼睛看到的东西：

- parallel_pct：平行成束占比（高好）。同层两段方向差 <10°、垂直间距
  0.2–2.5mm、轴向投影有重叠 → 重叠长度计入（每段只记一次最大重叠，
  防长线被 K 根短线重复累计），占信号总长 %。人工板总线成束 → 高；
  FR 单根散逃 → 低。
- spacing_cv：平行走线对的间距变异系数（低好）。上述平行对按重叠
  长度加权的 std/mean——间距忽紧忽松 → 高，均匀 → 低。
- dir_hv_pct：层内方向纪律（高好）。段长按方向四桶（0/45/90/135°，
  ±10° 容差，其余算"乱向"），取较大正交配对（0+90 或 45+135）占
  层总长 %，各层按层长加权平均——量"这层守不守一套主方向约定"。

已知限制：弧轨道（arc）以 chord() 弦参与连通性、以真弧长计入
len_mm/len_ratio；弧不计 bends/non45/秩序刻度（弧光滑无折弯、天然
平行判定退化，漏计而非误计）。

CLI：
    python -m kicad_access.route.quality --board in.kicad_pcb [--label 名]
        # 单板 JSON 一行；批量对判由外部循环聚合
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .freeroute import _pads_connected
from .model import RouteBoard

_ANG_EPS = math.radians(1.0)     # 共线/45° 判定容差

_PAR_ANG = math.radians(10.0)    # 平行判定方向容差
_PAR_D_LO, _PAR_D_HI = 0.2, 2.5  # 平行列距窗口（mm）：近于下限=并线，远于上限=碰巧同向
_DIR_BUCKETS = (0.0, 45.0, 90.0, 135.0)
_DIR_EPS = math.radians(10.0)    # 方向桶容差


def _order_metrics(segs) -> dict:
    """秩序三刻度：parallel_pct / spacing_cv / dir_hv_pct（见模块 docstring）。"""
    if not segs:
        return {"parallel_pct": None, "spacing_cv": None, "dir_hv_pct": None}
    total = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
    if not total:
        return {"parallel_pct": None, "spacing_cv": None, "dir_hv_pct": None}

    # ---- parallel_pct / spacing_cv：同层两两平行对的重叠长度与列距
    by_layer: dict[str, list] = {}
    for s in segs:
        by_layer.setdefault(s.layer, []).append(s)

    best_par = {id(s): 0.0 for s in segs}     # 每段只记一次最大重叠
    gaps, gap_w = [], []                       # 平行列距样本（重叠长度加权）
    for layer_segs in by_layer.values():
        pre = []                               # (dx,dy,len,ux,uy,px,py)
        for s in layer_segs:
            dx, dy = s.x2 - s.x1, s.y2 - s.y1
            ln = math.hypot(dx, dy)
            if ln < 1e-9:
                continue
            pre.append((s, dx, dy, ln, dx / ln, dy / ln, s.x1, s.y1))
        for i, (a, adx, ady, aln, aux, auy, apx, apy) in enumerate(pre):
            for (b, bdx, bdy, bln, bux, buy, bpx, bpy) in pre[i + 1:]:
                # 方向差（无向）：cos 夹角绝对值接近 1 即平行
                cos_a = abs(aux * bux + auy * buy)
                if cos_a < math.cos(_PAR_ANG):
                    continue
                # 列距：b 两端到 a 所在直线的距离，取 max（准平行不低估）
                d1 = abs((bpx - apx) * auy - (bpy - apy) * aux)
                d2 = abs((bpx + bdx - apx) * auy - (bpy + bdy - apy) * aux)
                d = max(d1, d2)
                if not (_PAR_D_LO <= d <= _PAR_D_HI):
                    continue
                # 轴向投影重叠：b 两端在 a 轴上的投影与 [0, aln] 相交
                t1 = (bpx - apx) * aux + (bpy - apy) * auy
                t2 = t1 + bln * (bux * aux + buy * auy)
                ov = min(max(t1, t2), aln) - max(min(t1, t2), 0.0)
                if ov <= 0:
                    continue
                best_par[id(a)] = max(best_par[id(a)], ov)
                best_par[id(b)] = max(best_par[id(b)], ov)
                gaps.append(d)
                gap_w.append(ov)

    par_len = sum(min(best_par[id(s)],
                      math.hypot(s.x2 - s.x1, s.y2 - s.y1)) for s in segs)
    if gaps:
        wsum = sum(gap_w)
        mean = sum(g * w for g, w in zip(gaps, gap_w)) / wsum
        var = sum(w * (g - mean) ** 2 for g, w in zip(gaps, gap_w)) / wsum
        cv = math.sqrt(var) / mean if mean > 1e-9 else None
    else:
        cv = None

    # ---- dir_hv_pct：层内较大正交配对（0+90 或 45+135）占层长比，层长加权
    num = den = 0.0
    for layer_segs in by_layer.values():
        buckets = [0.0] * 4                     # 0/45/90/135
        other = 0.0
        for s in layer_segs:
            dx, dy = s.x2 - s.x1, s.y2 - s.y1
            ln = math.hypot(dx, dy)
            if ln < 1e-9:
                continue
            ang = math.degrees(math.atan2(dy, dx)) % 180.0
            for k, base in enumerate(_DIR_BUCKETS):
                dev = abs(ang - base)
                dev = min(dev, 180.0 - dev)     # 桶边界环绕
                if math.radians(dev) <= _DIR_EPS:
                    buckets[k] += ln
                    break
            else:
                other += ln                     # 乱向：只进分母，压低纪律
        lay_len = sum(buckets) + other
        if lay_len > 0:
            num += max(buckets[0] + buckets[2], buckets[1] + buckets[3])
            den += lay_len

    return {
        "parallel_pct": round(100 * par_len / total, 1),
        "spacing_cv": round(cv, 3) if cv is not None else None,
        "dir_hv_pct": round(100 * num / den, 1) if den else None,
    }


def _dev_from_45(dx: float, dy: float) -> float:
    """方向偏离最近 45° 倍数的角度（rad）。"""
    ang = math.atan2(dy, dx) % (math.pi / 4)
    return min(ang, math.pi / 4 - ang)


def quality(board: RouteBoard) -> dict:
    zone_nets = {z.net for z in board.zones}
    pads_by_net = board.pads_by_net()
    signal = sorted(
        n for n, ps in pads_by_net.items()
        # `unconnected-(…)` 是 KiCad 给"故意不接"引脚的占位网（vme-wren
        # IC30 两个：0 段 0 孔）——本就无铜可布，不计入信号网，否则
        # 永远卡 unconn 门槛。
        if n not in zone_nets and len(ps) >= 2
        and not n.startswith("unconnected-("))
    sig = set(signal)

    segs = [s for s in board.segments if s.net in sig]
    arcs = [a for a in board.arcs if a.net in sig]
    vias = [v for v in board.vias if v.net in sig]

    # ---- 门槛：连通性（板上既有铜一并参与，同引擎口径；弧经弦并入）
    unconn = [n for n in signal
              if not _pads_connected(
                  board,
                  [s for s in board.segments if s.net == n],
                  [v for v in board.vias if v.net == n], n)]

    # ---- 长度 / HPWL（弧按真弧长计）
    len_mm = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
    len_mm += sum(a.length for a in arcs)
    hpwl = 0.0
    for n in signal:
        xs = [p.x for p in pads_by_net[n]]
        ys = [p.y for p in pads_by_net[n]]
        hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))

    # ---- bends：同网同层 degree-2 拼点方向改变（T 接/盘/孔不算）
    ends: dict[tuple, list] = {}
    for s in segs:
        for (x, y) in ((s.x1, s.y1), (s.x2, s.y2)):
            ends.setdefault(
                (s.net, s.layer, round(x, 3), round(y, 3)), []).append(s)
    bends = 0
    for (_net, _layer, jx, jy), junction in ends.items():
        if len(junction) != 2:
            continue
        a, b = junction

        def away(s):                     # 离开拼点的方向（拼点可能是任一端）
            if (round(s.x1, 3), round(s.y1, 3)) == (jx, jy):
                return (s.x2 - jx, s.y2 - jy)
            return (s.x1 - jx, s.y1 - jy)
        d1, d2 = away(a), away(b)
        cross = abs(d1[0] * d2[1] - d1[1] * d2[0])
        dot = d1[0] * d2[0] + d1[1] * d2[1]
        if math.atan2(cross, dot) > _ANG_EPS:      # 非共线 = 弯
            bends += 1

    # ---- non45：偏离 0/45/90° 超容差的段
    n45 = n45_len = 0.0
    for s in segs:
        if _dev_from_45(s.x2 - s.x1, s.y2 - s.y1) > _ANG_EPS:
            n45 += 1
            n45_len += math.hypot(s.x2 - s.x1, s.y2 - s.y1)

    n_nets = len(signal)
    out = _order_metrics(segs)
    out.update({
        "nets": n_nets,
        "unconn": len(unconn),
        "unconn_nets": unconn[:8],
        "len_mm": round(len_mm, 1),
        "hpwl_mm": round(hpwl, 1),
        "len_ratio": round(len_mm / hpwl, 3) if hpwl else None,
        "vias": len(vias),
        "via_per_net": round(len(vias) / n_nets, 2) if n_nets else None,
        "segs": len(segs),
        "arcs": len(arcs),
        "bends": bends,
        "bends_per_10mm": round(bends / (len_mm / 10), 2) if len_mm else None,
        "non45": n45,
        "non45_len_pct": round(100 * n45_len / len_mm, 1) if len_mm else None,
    })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route.quality",
        description="走线质量打分器（Q0：人工 vs 自动对判 / Q1 调参 / Q3 奖励）")
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--label", default=None, help="报表里的板名（缺省取文件名）")
    a = ap.parse_args()
    board = RouteBoard(a.board)
    board.inherit_nets()
    out = quality(board)
    out["label"] = a.label or a.board.stem
    out["board"] = str(a.board)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
