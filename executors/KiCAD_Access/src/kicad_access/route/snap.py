"""平行段节距整备（Q3 路线 B，2026-10-01）：布线后处理 snap-to-pitch。

路线 C（节距吸引井进 A*）三轮实证为机制级性能缺陷——井折扣破坏启发式
紧致性 → 长网泛洪，23–38/109@600s；但井要追的目标本身有效（f0.95 快
测板已布 31 网 cv 0.22，对照全板 0.343）。本模块把同一目标搬到零搜索
代价的确定性后处理：

    布线完成后（引擎不动），把同网同层的共线连续段链成"直 run"，识别
    与邻近 run 净距落在节距带内的平行对，把可移动的 run 连同其同网
    正交连接段整体平移 δ，使与参考邻居的净距精确等于公共节距 p*
    （初始平行对按重叠加权的中位数，或 --pitch 显式给）。

移动合法性（保守子集，缺一不可）：
- 只动水平/垂直 run（斜线不参与——Q5 的地盘）；
- run 线上（span 内）无同网盘心/过孔（端点接盘/孔的 run 不动）；
- 触线的同网段端点必须真落在本 run span 内（延长线上的不算——那点
  接的是别处铜，拉伸它反而扯断）；正交段随 run 平移自然伸缩（伸缩后
  > 0.05mm），斜线段端点跟移（允许微弯）；跨线交叉（交点在 span 内）
  → 整个 run 冻结；
- 平移后仍在板框内；
- 全量清障复验：异网铜对铜 ≥ clearance（_pair_dist，同 drc 口径）、
  异网铜对盘 ≥ clearance（_seg_pad_dist，比 drc 的短路级更保守），
  不过即弃。

不动的东西：段数/孔数/连通拓扑/弯数全不变——效率刻度零回退，纯间距
重排。贪心接受 = 本 run 全部节距对的 Σ w·dev(g)² 严格下降（dev = 到最近
k·p* 的谐波偏差，见 _dev——只按 p* 罚会产生远邻收缩偏置）；≤ passes
轮，p* 全程不变（格锚不漂）。

CLI：
    python -m kicad_access.route.snap --board r.kicad_pcb --out s.kicad_pcb \
        [--clearance-um 230] [--pitch 0.9] [--passes 3] [--snap-band 0.4] \
        [--min-overlap 0.5] [--report snap.json]

输出板只改被移动段的坐标（文本级替换，其余字节不动）；写盘前跑 run_drc
对账，出现原板没有的新违规 → 不写、报告失败（别破坏）。
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .astar import _seg_pad_dist
from .drc import run_drc
from .model import RouteBoard, Segment
from .quality import quality
from .regions import _f, _pair_dist

_EPS = 1e-3          # 共线/共端点判定的坐标容差（栅格坐标，µm 级精确）
_ONLINE = 0.01       # 盘心/孔心"在线上"的容差（远小于最小铜特征）
_MIN_STUB = 0.05     # 伸缩后连接段最小长度（再短视为退化）
_EDGE_M = 0.25       # run 平移后离板框的最小余量
_OBJ_LO, _OBJ_HI = 0.3, 3.0   # 目标函数的 gap 窗（比度量窗宽，防止把对
                              # 推出度量窗来"作弊"降 cv——出去的照样罚）


@dataclass
class _Run:
    """同网同层共线段的极大链 + 其正交接线段。"""

    segs: list[int]              # board.segments 下标（共线链全体）
    touchers: list[int]          # 同网同层正交段（端点在线上，随平移伸缩）
    net: str
    layer: str
    horiz: bool
    coord: float                 # 水平 run 的 y / 垂直 run 的 x
    lo: float                    # 轴向 span（水平 run 的 x 范围）
    hi: float
    width: float
    movable: bool = True
    blocked: str = ""
    moved: bool = False


# ---------------------------------------------------------------- run 提取

def _axis(s: Segment) -> str | None:
    if abs(s.y2 - s.y1) < _EPS:
        return "h"
    if abs(s.x2 - s.x1) < _EPS:
        return "v"
    return None


def _build_runs(board: RouteBoard, sig: set[str]) -> list[_Run]:
    runs: list[_Run] = []
    by_net_layer: dict[tuple[str, str], list[int]] = {}
    for i, s in enumerate(board.segments):
        if s.net in sig and _axis(s):
            by_net_layer.setdefault((s.net, s.layer), []).append(i)

    for (net, layer), idxs in sorted(by_net_layer.items()):
        # 按方向+线坐标分桶，桶内按共端点并查集合并 → 共线连续链
        lines: dict[tuple[str, float], list[int]] = {}
        for i in idxs:
            s = board.segments[i]
            ax = _axis(s)
            c = round((s.y1 + s.y2) / 2 if ax == "h" else (s.x1 + s.x2) / 2, 3)
            lines.setdefault((ax, c), []).append(i)

        for (ax, c), members in sorted(lines.items()):
            parent = list(range(len(members)))

            def find(a: int) -> int:
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a

            pt: dict[tuple[float, float], int] = {}
            for k, i in enumerate(members):
                s = board.segments[i]
                for p in ((round(s.x1, 3), round(s.y1, 3)),
                          (round(s.x2, 3), round(s.y2, 3))):
                    if p in pt:
                        parent[find(k)] = find(pt[p])
                    else:
                        pt[p] = k

            comps: dict[int, list[int]] = {}
            for k in range(len(members)):
                comps.setdefault(find(k), []).append(members[k])

            for comp in comps.values():
                segs = [board.segments[i] for i in comp]
                horiz = ax == "h"
                if horiz:
                    lo = min(min(s.x1, s.x2) for s in segs)
                    hi = max(max(s.x1, s.x2) for s in segs)
                else:
                    lo = min(min(s.y1, s.y2) for s in segs)
                    hi = max(max(s.y1, s.y2) for s in segs)
                runs.append(_Run(
                    segs=sorted(comp), touchers=[], net=net, layer=layer,
                    horiz=horiz, coord=c, lo=lo, hi=hi,
                    width=max(s.width for s in segs)))

    _find_touchers(board, sig, runs)
    return runs


def _find_touchers(board: RouteBoard, sig: set[str], runs: list[_Run]) -> None:
    """给每个 run 找同网同层触线段：正交端点 → toucher（可随动伸缩）；
    斜线触线 / 跨线交叉 / 线上盘心孔心 → 整 run 冻结。"""
    same: dict[tuple[str, str], list[_Run]] = {}
    for r in runs:
        same.setdefault((r.net, r.layer), []).append(r)

    for (net, layer), group in same.items():
        segs_nl = [(i, s) for i, s in enumerate(board.segments)
                   if s.net == net and s.layer == layer]
        for r in group:
            for i, s in segs_nl:
                ax = _axis(s)
                if ax == ("h" if r.horiz else "v"):
                    continue  # 同向平行/共线异链：不相触，无约束
                # 触点必须真落在本 run 的 span 内——端点在延长线上不算
                # （那点接的是别处铜，拉伸它反而扯断）
                if r.horiz:
                    c_, a1, a2 = r.coord, s.y1, s.y2
                else:
                    c_, a1, a2 = r.coord, s.x1, s.x2
                lo_e, hi_e = r.lo - _EPS, r.hi + _EPS
                on = False
                if abs(a1 - c_) <= _EPS:
                    t = s.x1 if r.horiz else s.y1
                    on = lo_e <= t <= hi_e
                if not on and abs(a2 - c_) <= _EPS:
                    t = s.x2 if r.horiz else s.y2
                    on = lo_e <= t <= hi_e
                if on:
                    if i not in r.segs:
                        r.touchers.append(i)
                    continue
                # 跨线：算实际交点，交点在 span 内才冻结
                if (a1 - c_) * (a2 - c_) < 0:
                    f_ = (c_ - a1) / (a2 - a1)
                    t = s.x1 + f_ * (s.x2 - s.x1) if r.horiz \
                        else s.y1 + f_ * (s.y2 - s.y1)
                    if lo_e <= t <= hi_e:
                        r.movable = False
                        r.blocked = "crossing"

    # 线上同网盘心/孔心 → 冻结（平移会把接点扯开）
    pads_by_net = board.pads_by_net()
    for r in runs:
        eps = _ONLINE
        for p in pads_by_net.get(r.net, ()):  # type: ignore[union-attr]
            if r.layer not in p.layers:
                continue
            if r.horiz:
                if abs(p.y - r.coord) <= eps and r.lo - eps <= p.x <= r.hi + eps:
                    r.movable = False
                    r.blocked = "pad-on-line"
            else:
                if abs(p.x - r.coord) <= eps and r.lo - eps <= p.y <= r.hi + eps:
                    r.movable = False
                    r.blocked = "pad-on-line"
        for v in board.vias:
            if v.net != r.net:
                continue
            if r.horiz:
                if abs(v.y - r.coord) <= eps and r.lo - eps <= v.x <= r.hi + eps:
                    r.movable = False
                    r.blocked = "via-on-line"
            else:
                if abs(v.x - r.coord) <= eps and r.lo - eps <= v.y <= r.hi + eps:
                    r.movable = False
                    r.blocked = "via-on-line"


# ------------------------------------------------------------- 对 / 目标 / p*

def _gap_pairs(runs: list[_Run], layer: str, horiz: bool,
               lo: float, hi: float, min_ov: float):
    """当前状态下 (layer, 方向) 桶内的 (gap, 重叠) 对——与 quality 的
    平行对口径一致（中心距 + 轴向重叠），run 级聚合。"""
    bucket = [r for r in runs if r.layer == layer and r.horiz == horiz]
    out = []
    for i in range(len(bucket)):
        for j in range(i + 1, len(bucket)):
            a, b = bucket[i], bucket[j]
            g = abs(a.coord - b.coord)
            if not (lo <= g <= hi):
                continue
            ov = min(a.hi, b.hi) - max(a.lo, b.lo)
            if ov >= min_ov:
                out.append((g, ov))
    return out


def _neighbors(r: _Run, runs: list[_Run], lo: float, hi: float, min_ov: float):
    """r 的节距邻居（当前状态）：(邻居, gap, 重叠)。"""
    out = []
    for n in runs:
        if n is r or n.layer != r.layer or n.horiz != r.horiz:
            continue
        g = abs(n.coord - r.coord)
        if not (lo <= g <= hi):
            continue
        ov = min(r.hi, n.hi) - max(r.lo, n.lo)
        if ov >= min_ov:
            out.append((n, g, ov))
    return out


def _wmedian(pairs: list[tuple[float, float]]) -> float:
    """重叠加权中位数（p* = 间距分布的众群锚点，对离群对稳健）。"""
    if not pairs:
        return 0.0
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for g, w in sorted(pairs):
        acc += w
        if acc * 2 >= tot:
            return g
    return pairs[-1][0]


def _dev(g: float, p: float) -> float:
    """谐波节距偏差：到最近 k·p*（k≥1）的偏差。

    只按 p* 罚会把远邻对（如 2.7/3.0mm 的非相邻 run）也拉向 p*，
    产生整体收缩偏置——实测会接受“局部挤出新差距、远对凑近”的
    假优解。bus 的第 k 邻间距本应是 k·p*，按最近倍数罚才无偏。
    候选 δ 受 snap_band 限幅，跳到 2p* 的谐波作弊无法被提名。
    """
    best = abs(g - p)
    k = 2
    while k * p <= _OBJ_HI:
        d = abs(g - k * p)
        if d < best:
            best = d
        k += 1
    return best


def _cost(r: _Run, runs: list[_Run], p: float, min_ov: float) -> float:
    """本 run 的 Σ w·dev(g)²（dev 见 _dev；目标窗 [_OBJ_LO,_OBJ_HI]，
    出窗的照样罚）。"""
    s = 0.0
    for _, g, w in _neighbors(r, runs, _OBJ_LO, _OBJ_HI, min_ov):
        s += w * _dev(g, p) ** 2
    return s


# ----------------------------------------------------------------- 平移检查

def _bbox(s: Segment) -> tuple[float, float, float, float]:
    return (min(s.x1, s.x2) - s.width, min(s.y1, s.y2) - s.width,
            max(s.x1, s.x2) + s.width, max(s.y1, s.y2) + s.width)


def _moved_pieces(board: RouteBoard, r: _Run, delta: float) -> list[Segment]:
    c2 = r.coord + delta
    pieces = []
    for i in r.segs:
        s = board.segments[i]
        pieces.append(replace(s, y1=c2, y2=c2) if r.horiz else replace(s, x1=c2, x2=c2))
    for i in r.touchers:
        s = board.segments[i]
        if r.horiz:
            on1 = abs(s.y1 - r.coord) <= _EPS
            pieces.append(replace(s, y1=c2) if on1 else replace(s, y2=c2))
        else:
            on1 = abs(s.x1 - r.coord) <= _EPS
            pieces.append(replace(s, x1=c2) if on1 else replace(s, x2=c2))
    return pieces


def _hard_ok(board: RouteBoard, r: _Run, delta: float, clearance: float,
             outline: tuple[float, float, float, float],
             seg_bb: list[tuple[float, float, float, float]]) -> str | None:
    """平移合法性全查；返回 None=通过，否则返回拒绝原因。"""
    c2 = r.coord + delta
    x1, y1, x2, y2 = outline

    # 板框余量（轴向 span 不变，只查新线坐标）
    if r.horiz:
        if not (y1 + _EDGE_M <= c2 <= y2 - _EDGE_M):
            return "outline"
    else:
        if not (x1 + _EDGE_M <= c2 <= x2 - _EDGE_M):
            return "outline"

    # 连接段伸缩后不得退化
    for i in r.touchers:
        s = board.segments[i]
        if r.horiz:
            far = s.y2 if abs(s.y1 - r.coord) <= _EPS else s.y1
            if abs(c2 - far) < _MIN_STUB:
                return "stub"
        else:
            far = s.x2 if abs(s.x1 - r.coord) <= _EPS else s.x1
            if abs(c2 - far) < _MIN_STUB:
                return "stub"

    pieces = _moved_pieces(board, r, delta)
    skip = set(r.segs) | set(r.touchers)
    reach = clearance + 1.0  # bbox 预筛半径（piece 半宽已含在 bbox 里）
    for pc in pieces:
        pb = _bbox(pc)
        for j, s in enumerate(board.segments):
            if j in skip or s.net == r.net:
                continue
            bb = seg_bb[j]
            if (bb[2] < pb[0] - reach or bb[0] > pb[2] + reach
                    or bb[3] < pb[1] - reach or bb[1] > pb[3] + reach):
                continue
            hit = _pair_dist(pc, s)
            if hit and hit[0] < hit[1] + clearance - 1e-6:
                return "clearance"
        for v in board.vias:
            if v.net == r.net:
                continue
            if not (pb[0] - reach <= v.x <= pb[2] + reach
                    and pb[1] - reach <= v.y <= pb[3] + reach):
                continue
            hit = _pair_dist(pc, v)
            if hit and hit[0] < hit[1] + clearance - 1e-6:
                return "clearance-via"
        for p in board.pads:
            if p.net == r.net:
                continue  # 无网盘（NPTH 等）照样是障碍物
            if pc.layer not in p.layers:
                continue
            if (p.x + p.w / 2 < pb[0] - reach or p.x - p.w / 2 > pb[2] + reach
                    or p.y + p.h / 2 < pb[1] - reach or p.y - p.h / 2 > pb[3] + reach):
                continue
            if _seg_pad_dist(pc.x1, pc.y1, pc.x2, pc.y2, p) - pc.width / 2 \
                    < clearance - 1e-6:
                return "pad-clearance"
    return None


# -------------------------------------------------------------------- 主程

def snap_pitch(board: RouteBoard, *, clearance: float = 0.23,
               pitch: float | None = None, band: tuple[float, float] = (0.2, 2.5),
               snap_band: float = 0.4, min_overlap: float = 0.5,
               passes: int = 3) -> dict:
    """在板对象上就地做节距整备；返回统计 dict（板与文件写回解耦）。"""
    board.inherit_nets()
    zone_nets = {z.net for z in board.zones}
    pads_by_net = board.pads_by_net()
    signal = sorted(
        n for n, ps in pads_by_net.items()
        if n not in zone_nets and len(ps) >= 2
        and not n.startswith("unconnected-("))
    sig = set(signal)

    runs = _build_runs(board, sig)
    lo, hi = band
    all_pairs: list[tuple[float, float]] = []
    layers = sorted({r.layer for r in runs})
    for layer in layers:
        for horiz in (True, False):
            all_pairs += _gap_pairs(runs, layer, horiz, lo, hi, min_overlap)
    p = pitch if pitch else _wmedian(all_pairs)

    stats = {
        "pitch": p, "pitch_samples": len(all_pairs), "runs": len(runs),
        "movable": sum(r.movable for r in runs),
        "blocked": {k: sum(1 for r in runs if r.blocked == k)
                    for k in sorted({r.blocked for r in runs if r.blocked})},
        "passes_used": 0, "moves_accepted": 0, "moves_rejected": {},
        "moves": [], "changed_idx": set(),
    }
    if not all_pairs or p <= 0:
        stats["changed_idx"] = []
        return stats

    # 固定处理序：初始总重叠大的先动（与格锚耦合最深的先就位）
    ov_tot: dict[int, float] = {}
    for i, r in enumerate(runs):
        ov_tot[i] = sum(w for _, g, w in _neighbors(r, runs, lo, hi, min_overlap))
    order = sorted(range(len(runs)),
                   key=lambda i: (-ov_tot[i], runs[i].layer, runs[i].coord,
                                  runs[i].lo, runs[i].net))

    seg_bb = [_bbox(s) for s in board.segments]
    for _ in range(passes):
        accepted = 0
        for ri in order:
            r = runs[ri]
            if not r.movable:
                continue
            c0 = _cost(r, runs, p, min_overlap)
            best: tuple[float, float] | None = None
            seen_d: set[float] = set()
            for n, g, _w in _neighbors(r, runs, lo, hi, min_overlap):
                if abs(g - p) > snap_band:
                    continue
                d = round(math.copysign(p - g, r.coord - n.coord), 3)
                if abs(d) < 1e-9 or d in seen_d:
                    continue
                seen_d.add(d)
                reason = _hard_ok(board, r, d, clearance, board.outline, seg_bb)
                if reason:
                    stats["moves_rejected"][reason] = \
                        stats["moves_rejected"].get(reason, 0) + 1
                    continue
                gain = c0 - _cost_at(r, runs, p, min_ov=min_overlap, delta=d)
                if gain > 1e-9 and (best is None or gain > best[0]):
                    best = (gain, d)
            if best is None:
                continue
            gain, d = best
            _apply_move(board, r, d, seg_bb)
            accepted += 1
            stats["moves_accepted"] += 1
            stats["changed_idx"].update(r.segs)
            stats["changed_idx"].update(r.touchers)
            if len(stats["moves"]) < 300:
                stats["moves"].append({
                    "net": r.net, "layer": r.layer,
                    "axis": "h" if r.horiz else "v",
                    "from": round(r.coord - d, 3), "to": round(r.coord, 3),
                    "delta": d, "gain": round(gain, 4)})
            r.moved = True
        stats["passes_used"] += 1
        if accepted == 0:
            break
    stats["changed_idx"] = sorted(stats["changed_idx"])
    return stats


def _cost_at(r: _Run, runs: list[_Run], p: float, *, min_ov: float,
             delta: float) -> float:
    """平移 δ 后 r 的目标值（只动 coord 视图，不改板）。"""
    old = r.coord
    r.coord = old + delta
    try:
        return _cost(r, runs, p, min_ov)
    finally:
        r.coord = old


def _apply_move(board: RouteBoard, r: _Run, delta: float,
                seg_bb: list[tuple[float, float, float, float]]) -> None:
    c2 = r.coord + delta
    for i in r.segs:
        s = board.segments[i]
        s2 = replace(s, y1=c2, y2=c2) if r.horiz else replace(s, x1=c2, x2=c2)
        board.segments[i] = s2
        seg_bb[i] = _bbox(s2)
    for i in r.touchers:
        s = board.segments[i]
        if r.horiz:
            on1 = abs(s.y1 - r.coord) <= _EPS
            s2 = replace(s, y1=c2) if on1 else replace(s, y2=c2)
        else:
            on1 = abs(s.x1 - r.coord) <= _EPS
            s2 = replace(s, x1=c2) if on1 else replace(s, x2=c2)
        board.segments[i] = s2
        seg_bb[i] = _bbox(s2)
    r.coord = c2


# --------------------------------------------------------------- 文件写回

_START_RE = re.compile(r"\(\s*start\s+[-\d.+\-eE]+\s+[-\d.+\-eE]+\s*\)")
_END_RE = re.compile(r"\(\s*end\s+[-\d.+\-eE]+\s+[-\d.+\-eE]+\s*\)")


def _iter_segment_blocks(text: str):
    """按文档序产出顶层 (segment …) 块的 [起, 止) 字符区间。"""
    i, n = 0, len(text)
    while True:
        k = text.find("(segment", i)
        if k < 0:
            return
        if k + 8 < n and not text[k + 8].isspace():
            i = k + 8
            continue
        depth, j = 0, k
        while j < n:
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            return  # 文件截断，放弃
        yield k, j + 1
        i = j + 1


def _retag(block: str, s: Segment) -> str:
    """块内只换 (start x y)/(end x y) 两对坐标，其余字节不动。"""
    block = _START_RE.sub(f"(start {_f(s.x1)} {_f(s.y1)})", block, count=1)
    return _END_RE.sub(f"(end {_f(s.x2)} {_f(s.y2)})", block, count=1)


def write_snapped(src: Path, out: Path, board: RouteBoard,
                  changed_idx: list[int]) -> None:
    """把改动段的坐标按 board.segments 现值写回新文件（src 不动，
    未改动块逐字节保留）。"""
    text = src.read_text()
    blocks = list(_iter_segment_blocks(text))
    if len(blocks) != len(board.segments):
        raise RuntimeError(
            f"segment 块数 {len(blocks)} ≠ 模型段数 {len(board.segments)}，"
            "文件与模型不对应，拒写")
    changed = set(changed_idx)
    parts: list[str] = []
    pos = 0
    for i, (a, b) in enumerate(blocks):
        if i in changed:
            parts.append(text[pos:a])
            parts.append(_retag(text[a:b], board.segments[i]))
            pos = b
    parts.append(text[pos:])
    out.write_text("".join(parts))


# --------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--clearance-um", type=float, default=230.0)
    ap.add_argument("--pitch", type=float, default=None,
                    help="目标节距（缺省 = 初始平行对重叠加权中位数）")
    ap.add_argument("--band-lo", type=float, default=0.2)
    ap.add_argument("--band-hi", type=float, default=2.5)
    ap.add_argument("--snap-band", type=float, default=0.4,
                    help="|gap−p*| 超过此值的不snap（限单次平移量）")
    ap.add_argument("--min-overlap", type=float, default=0.5)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    clearance = args.clearance_um / 1000.0
    board = RouteBoard(args.board)
    board.inherit_nets()
    q0 = quality(board)
    drc0 = run_drc(board, clearance=clearance)

    st = snap_pitch(board, clearance=clearance, pitch=args.pitch,
                    band=(args.band_lo, args.band_hi), snap_band=args.snap_band,
                    min_overlap=args.min_overlap, passes=args.passes)

    q1 = quality(board)
    drc1 = run_drc(board, clearance=clearance)
    base = {str(v) for v in drc0}
    fresh = [str(v) for v in drc1 if str(v) not in base]

    st["quality_before"] = q0
    st["quality_after"] = q1
    st["drc_before"] = len(drc0)
    st["drc_after"] = len(drc1)
    st["drc_new"] = fresh[:20]

    if fresh:
        st["written"] = False
        print(f"[refuse] snap 引入 {len(fresh)} 条新 DRC 违规，不写盘：")
        for v in fresh[:5]:
            print(f"  {v}")
    else:
        write_snapped(args.board, args.out, board, st["changed_idx"])
        st["written"] = True
        print(f"[ok] {args.out}  p*={st['pitch']:.3f} "
              f"runs={st['runs']}(可动 {st['movable']}) "
              f"moves={st['moves_accepted']} pass={st['passes_used']} "
              f"cv {q0['spacing_cv']:.3f}→{q1['spacing_cv']:.3f} "
              f"束% {q0['parallel_pct']:.1f}→{q1['parallel_pct']:.1f} "
              f"段 {q0['segs']}→{q1['segs']} 孔 {q0['vias']}→{q1['vias']}")
    if args.report:
        args.report.write_text(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
