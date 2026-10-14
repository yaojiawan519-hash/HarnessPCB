"""段级派活调度器（挂账⑤，2026-10-11 用户拍板）：布线派活单位从
「整网」改成「段」，计分从「网连通」换成「焊盘连接率」。

动机（一块工业接口板的复盘，工作台 eval_out 归档 REPORT
§十二）：引擎 run() Phase 1 按 MST 总长整网排队、网间查预算——队尾
大网（GND/+3V3 平面网）一条边都没布就撞预算墙；相邻盘的便宜段
（C24-1↔C28-1，2.54mm，纯铜面零阻挡）本可先连，却因整网全或无
（_route_edges 任一边 ValueError 整网弃）颗粒归公，基线 45/68 网
= 98/290 盘（34%）。

两阶段（零改动 engine/astar，与 actions.RoutingSession 同款「新文件
包旧引擎」）：

Phase A（全网 MST 边全局池，边长升序逐边布）
  所有 target 网的 MST 边汇成一个全局池，按 (边长, 网, 盘 key) 升序
  ——便宜的相邻段先连（先布铜即 commit 留痕，后布贵段绕行）。
  逐边：预算墙 → 全停；并查集已连（既有铜或先布边）→ 跳过；布通 →
  即刻 commit（router.commit，同网豁免——同网先布铜不成障）+ 井采样
  三连（_pitch_update/_via_pitch_update/_bundle_pitch_update，均
  self-guard，缺省旗标下直通）；失败 → 该边放弃，**网不死**（其他
  边照常——全或无语义的解除正是本调度器存在的理由）。

Phase B（真实铜面孤岛桥接）
  Phase A 后每网按 _order() 复测真实连通（_pad_roots 并查集）；>1
  孤岛 → 跨孤岛最短盘对逐个试布（≤bridge_pairs 候选，按
  (距离, 盘 key) 排序），桥成即重测；最多 bridge_rounds 轮，全败
  即止（同组候选重试无益）。

与引擎的语义差异（有意为之，别当 bug 报）：
  · 无协商轮（Phase 3）——逐边即 commit、铜面只增不减，跨网冲突按
    构造为 0（A* 只走 edge_clear 格、同网豁免）；代价是贪心无回退，
    铜效（孔数/线长）让位于完成度。
  · Q5 软帽回退是边级：单边撞 DiagNodeCapExceeded → 网入 _diag_fb、
    本边四向重试（engine._route_net_once 认可口径）。引擎另有的网级
    累计帽是整网连布的口径，跨网交织调度下不适用。
  · hist/refine/stagnation/max_iter 等协商形参透传后 inert（本类
    不调用 eng.run()）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import signal
import time
from collections import Counter
from pathlib import Path

from .astar import DiagNodeCapExceeded
from .drc import run_drc
from .engine import NegotiatedRouter, write_board
from .freeroute import _EPS, _pads_connected
from .model import (Pad, RouteBoard, Segment, Via, seg_dist,
                    seg_rect_dist, seg_seg_dist)


class _EdgeTimeout(Exception):
    """单条边墙钟切片到点（见 _timed）。"""


_ARMED = {"on": False}      # 武装闸：迟到 SIGALRM 不误伤后续代码


def _sigalrm(signum, frame) -> None:
    if _ARMED["on"]:
        raise _EdgeTimeout()


def _timed(fn, seconds: float):
    """fn 限时执行：setitimer 到点抛 _EdgeTimeout（纯 Python A* 字节码
    间可靠打断）。seconds<=0 直跑；非主线程（signal 不可用）诚实降级
    直跑。迟到信号被 _ARMED 闸吞掉，不会在 disarm 后误伤无关代码。

    为什么需要：astar 单次 route() 无节点上限（正交模式无 pop 帽）、
    engine 预算只在网间检查——被围死的边 A* 须穷尽全部可达状态才判
    死，实测一颗病态边把进程吃到 47.7GB/40min+（2026-10-11 S 臂）。"""
    if seconds <= 0:
        return fn()
    try:
        old = signal.signal(signal.SIGALRM, _sigalrm)
    except ValueError:      # signal only works in main thread
        return fn()
    _ARMED["on"] = True
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        _ARMED["on"] = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _pad_roots(board: RouteBoard, net: str) -> dict[str, tuple[float, float]]:
    """本网每盘 → 真实铜连通分量根（并查集）。

    判据镜像 freeroute._pads_connected（端点精确匹配/盘身搭接/同号盘
    合一/孔桥/孔-盘搭接/T 接/弧弦全套）——**改一处须两处同步**。新铜
    经 router.commit 已入 board.segments/vias，与报告口径同源
    （pre 铜与新铜等效），故不收参数铜。返回 {pad.key: root}；
    len(set(values())) == 1 ⟺ _pads_connected True（单测锁此等价）。"""
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def _key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 3), round(y, 3))

    pre_segs = [s for s in board.segments if s.net == net]
    arc_segs = [a.chord() for a in board.arcs if a.net == net]
    all_segs = pre_segs + arc_segs
    ends = [(_key(s.x1, s.y1), _key(s.x2, s.y2)) for s in all_segs]
    for k1, k2 in ends:
        union(k1, k2)
    pre_vias = [v for v in board.vias if v.net == net]
    via_lays = frozenset({s.layer for s in all_segs} | {"F.Cu", "B.Cu"})
    items: list[tuple[str, str, float, float, frozenset[str], float, float, bool]] = [
        (p.ref, p.num, p.x, p.y, frozenset(p.layers), p.w / 2, p.h / 2, p.shape == "circle")
        for p in board.pads_by_net()[net]
    ] + [("", f"via@{v.x:.3f},{v.y:.3f}", v.x, v.y, via_lays,
          v.size / 2, v.size / 2, True)
         for v in pre_vias]
    same_node: dict[tuple[str, str], tuple[float, float]] = {}
    for ref, num, x, y, *_ in items:
        union(same_node.setdefault((ref, num), _key(x, y)), _key(x, y))
    for ref, num, x, y, layers, hw, hh, is_circle in items:
        k = _key(x, y)
        find(k)
        for s, (k1, _k2) in zip(all_segs, ends):
            hit = (seg_dist(x, y, s) <= s.width / 2 + hw + _EPS if is_circle
                   else seg_rect_dist(s, x, y, hw, hh) <= s.width / 2 + _EPS)
            if s.layer in layers and hit:
                union(k, k1)
    for vx, vy, vlays, vhw, _vhh, _c in [
        (x, y, lays, hw, hh, c) for r, _n, x, y, lays, hw, hh, c in items if r == ""
    ]:
        for r2, _n2, px, py, plays, phw, phh, pcirc in items:
            if r2 == "":
                continue
            if not (vlays & plays):
                continue
            d = (math.hypot(vx - px, vy - py) - min(phw, phh) if pcirc else
                 seg_rect_dist(Segment(vx, vy, vx, vy, 0.0, "F.Cu", net),
                               px, py, phw, phh))
            if d <= vhw + _EPS:
                union(_key(vx, vy), _key(px, py))
    by_layer: dict[str, list[Segment]] = {}
    for s in all_segs:
        by_layer.setdefault(s.layer, []).append(s)
    for lays in by_layer.values():
        for i, s in enumerate(lays):
            for t in lays[i + 1:]:
                if seg_seg_dist(s, t) <= (s.width + t.width) / 2 + _EPS:
                    union(_key(s.x1, s.y1), _key(t.x1, t.y1))
    return {p.key: find(_key(p.x, p.y))
            for p in board.pads_by_net()[net]}


class _UF:
    """pad.key 并查集（Phase A 跳过判据）：比 _pad_roots 便宜，逐边
    增量维护。只会滞后不会超前——真实铜并了的它可能没并（多布一条
    冗余边，Phase B 真实测量自愈），绝不会把没连的说成连了。"""

    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def add(self, k: str) -> None:
        self.p.setdefault(k, k)

    def find(self, k: str) -> str:
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def same(self, a: str, b: str) -> bool:
        return self.find(a) == self.find(b)

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


class SegmentRouter:
    """段级调度（挂账⑤）：包 NegotiatedRouter，零改动引擎本体。"""

    def __init__(self, board: RouteBoard, *, clearance_mm: float,
                 budget_s: float = 600.0, bridge_rounds: int = 2,
                 bridge_pairs: int = 8, edge_slice_s: float = 45.0,
                 **eng_kw):
        """clearance_mm/budget_s/bridge_*/edge_slice_s 显式提签（调度器
        本体消费）；其余 NegotiatedRouter 形参原样透传（构造语义与引擎
        CLI 全同，含工艺参数/井/Q5/dogbone 全家）。协商轮形参（max_iter/
        stagnation_k/hist_penalty/refine*）透传后 inert——本类不调用
        eng.run()，Phase 3 协商不存在。edge_slice_s=单条边墙钟切片
        （0=不限），到点本边放弃、网不死（见 _timed 因果）。"""
        if clearance_mm <= 0:
            raise ValueError(f"clearance_mm 须为正，得 {clearance_mm}")
        self.budget_s = budget_s
        self.bridge_rounds = bridge_rounds
        self.bridge_pairs = bridge_pairs
        self.edge_slice_s = edge_slice_s
        self._timeouts = 0
        self._deadline: float | None = None
        self.eng = NegotiatedRouter(board, clearance_mm=clearance_mm,
                                    budget_s=budget_s, **eng_kw)

    # ---------------------------------------------------------------- 内部
    def _route_edge(self, net: str, a: Pad, b: Pad
                    ) -> tuple[list[Segment], list[Via]] | None:
        """布一条盘对（Q5 软帽回退的边级版）：8 向撞 DiagNodeCapExceeded
        → 网入 _diag_fb、本边四向重试（legacy 桌）。ValueError = 本边
        布不通 → None（边失败，网不死——与引擎整网弃的语义差异点）。
        每次尝试套 _timed 墙钟片（≤edge_slice_s，且不越过 run() 预算墙）
        ——到点 _EdgeTimeout 按本边失败计，不触发四向重试（正交探索域
        ⊇ 8 向，8 向都超时的边四向只会更久）。"""
        eng = self.eng
        sl = self.edge_slice_s
        if self._deadline is not None:
            sl = min(sl, max(self._deadline - time.time(), 1.0))

        def _go():
            return _timed(lambda: eng.router.route(net, a, b), sl)

        if not eng.diag45 or net in eng._diag_fb:
            try:
                return _go()
            except ValueError:
                return None
            except _EdgeTimeout:
                self._timeouts += 1
                return None
        try:
            return _go()
        except DiagNodeCapExceeded:
            eng._diag_fb.add(net)
            eng.router.diag_mode(False)
            try:
                return _go()
            except ValueError:
                return None
            except _EdgeTimeout:
                self._timeouts += 1
                return None
            finally:
                eng.router.diag_mode(True)
        except ValueError:
            return None
        except _EdgeTimeout:
            self._timeouts += 1
            return None

    def _commit(self, net: str, segs: list[Segment],
                vias: list[Via]) -> None:
        """即布即 commit（同网豁免——后布同网边绕开先布铜）+ 井采样
        三连（engine Phase 2 同款调用序；全部 self-guard，未开井直通）。"""
        eng = self.eng
        p = eng.paths[net]
        p.segs.extend(segs)
        p.vias.extend(vias)
        eng.router.commit(segs, vias)
        eng._pitch_update(net)
        eng._via_pitch_update(net)
        eng._bundle_pitch_update(net)

    def _uf_from_copper(self, net: str) -> _UF:
        """初始并查集 = 既有真实铜的孤岛划分（部分连通网的已连盘对
        免布——Phase A skip 判据的初值）。"""
        eng = self.eng
        roots = _pad_roots(eng.board, net)
        uf = _UF()
        by_root: dict[tuple[float, float], list[str]] = {}
        for pad in eng._pads[net]:
            uf.add(pad.key)
            by_root.setdefault(roots[pad.key], []).append(pad.key)
        for keys in by_root.values():
            for k in keys[1:]:
                uf.union(keys[0], k)
        return uf

    # ---------------------------------------------------------------- 主入口
    def run(self) -> dict:
        eng = self.eng
        t0 = time.time()
        eng._t0 = t0          # F2 武装闸零点随 run 重置（engine 同款保险）
        deadline = t0 + self.budget_s
        self._deadline = deadline   # _route_edge 切片上界（不越过预算墙）
        exhausted = False

        # Phase A：全网 MST 边全局池，(边长, 网, 盘 key) 升序——便宜段先连
        pool = sorted(((d, net, a, b) for net in eng.targets
                       for a, b, d in eng._mst[net]),
                      key=lambda e: (e[0], e[1], e[2].key, e[3].key))
        uf_by_net = {net: self._uf_from_copper(net) for net in eng.targets}
        pa: Counter[str] = Counter()
        for _d, net, a, b in pool:
            if time.time() > deadline:
                exhausted = True
                break
            uf = uf_by_net[net]
            if uf.same(a.key, b.key):
                pa["skip"] += 1        # 既有铜/先布边已连——免布
                continue
            r = self._route_edge(net, a, b)
            if r is None:
                pa["fail"] += 1        # 本边放弃，网不死
                continue
            self._commit(net, *r)
            uf.union(a.key, b.key)
            pa["ok"] += 1

        # Phase B：真实铜面孤岛桥接（每网最多 bridge_rounds 轮，
        # 每轮 ≤ bridge_pairs 个最短跨岛盘对候选，桥成重测、全败即止）
        pb: Counter[str] = Counter()
        for net in eng._order():
            pads = eng._pads[net]
            for _rnd in range(self.bridge_rounds):
                if time.time() > deadline:
                    exhausted = True
                    break
                roots = _pad_roots(eng.board, net)
                if len(set(roots.values())) <= 1:
                    break               # 单岛——无需桥
                pairs = sorted(
                    ((math.hypot(q.x - p.x, q.y - p.y), p, q)
                     for p, q in itertools.combinations(pads, 2)
                     if roots[p.key] != roots[q.key]),
                    key=lambda t: (t[0], t[1].key, t[2].key))
                bridged = False
                for _d, a, b in pairs[:self.bridge_pairs]:
                    if time.time() > deadline:
                        exhausted = True
                        break
                    r = self._route_edge(net, a, b)
                    if r is None:
                        pb["fail"] += 1
                        continue
                    self._commit(net, *r)
                    pb["ok"] += 1
                    bridged = True
                    break               # 桥成——重测再决定下一轮
                if exhausted or not bridged:
                    break
            if exhausted:
                break

        # ---- 记账（真实铜面终测——per_net/网级/盘覆盖三口径同源）
        per_net: dict[str, dict] = {}
        unrouted: list[str] = []
        pads_connected = 0
        pads_total = 0
        for n in eng.targets:
            roots = _pad_roots(eng.board, n)
            islands = len(set(roots.values()))
            comp = max(Counter(roots.values()).values())  # 最大真实分量盘数
            pads_total += len(roots)
            pads_connected += comp
            p = eng.paths[n]
            conn = islands <= 1
            p.err = None if conn else f"未连通（{islands} 孤岛）"
            if not conn:
                unrouted.append(n)
            per_net[n] = {
                "err": p.err, "conflicts": 0,
                "segs": len(p.segs), "vias": len(p.vias),
                "connected": conn, "islands": islands,
                "pads": comp, "pads_total": len(roots),
            }
        segs = [s for n in eng.targets for s in eng.paths[n].segs]
        vias = [v for n in eng.targets for v in eng.paths[n].vias]
        status = ("ok" if not unrouted
                  else "partial" if len(unrouted) < len(eng.targets)
                  else "failed")
        return {
            "scheduler": "segment",
            "status": status,
            "clearance_mm": eng.router.cfg.clearance,
            "layers": list(eng.router.layers),
            "dir_prior": eng.dir_prior,
            "via_cost": eng.router.cfg.via_cost,
            "space": eng.space,
            "space_band": eng.router.cfg.space_band,
            "space_cost": eng.router.cfg.space_cost,
            "via_pitch": eng.via_pitch,
            "via_pitch_star": {
                f"{k[0]}|{'h' if k[1] else 'v'}": round(v, 3)
                for k, v in eng.router.via_pitch_star.items()},
            "via_pitch_band": eng.router.cfg.via_pitch_band,
            "via_pitch_samples": {
                f"{k[0]}|{'h' if k[1] else 'v'}": len(v)
                for k, v in eng._via_pitch_gaps.items()},
            "diag45": eng.diag45,
            "diag_cost": eng.diag_cost,
            "h_weight": eng.h_weight,
            "diag_node_cap": eng.diag_node_cap,
            "diag_fallback_nets": sorted(eng._diag_fb),
            "via_size": eng.router.cfg.via_size,
            "via_drill": eng.router.cfg.via_drill,
            "pad_clearance": eng.router.cfg.pad_clearance,
            "relax_delta_mm": round(eng.router.relax_stats["delta_mm"], 4),
            "relax_triggered_pads": eng.router.relax_stats["triggered_pads"],
            "relax_zone_cells": eng.router.relax_stats["zone_cells"],
            "bridge_rounds": self.bridge_rounds,
            "bridge_pairs": self.bridge_pairs,
            "edge_slice_s": self.edge_slice_s,
            "edge_timeouts": self._timeouts,
            "phase_a": dict(pa),
            "phase_b": dict(pb),
            "nets_total": len(eng.targets),
            "nets_routed": len(eng.targets) - len(unrouted),
            "nets_unrouted": unrouted,
            "nets_conflicted": [],
            "stop_reason": "budget" if exhausted else "done",
            "conflicts": 0,          # 逐边即 commit 按构造无跨网冲突
            "iterations": 1,
            "converged": not unrouted,
            "segments": len(segs), "vias": len(vias),
            "length_mm": round(sum(abs(s.x2 - s.x1) + abs(s.y2 - s.y1)
                                   for s in segs), 1),
            "pads_connected": pads_connected,
            "pads_total": pads_total,
            "pad_coverage": (round(pads_connected / pads_total, 4)
                             if pads_total else 1.0),
            "per_net": per_net,
            "self_drc": [str(v) for v in run_drc(
                eng.board, clearance=eng.router.cfg.clearance)],
            "wall_s": round(time.time() - t0, 1),
        }


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route.segment",
        description="段级派活调度器（挂账⑤）：全网 MST 边全局池按边长"
                    "升序逐边布 + 真实铜面孤岛桥接，计分 = 焊盘连接率。"
                    "旗标面与 route.engine 全同（协商轮形参 inert），"
                    "可直接替换命令行")
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--clearance-um", type=int, default=230,
                    help="铜间隙 µm（RL 动作口径；默认 230）")
    ap.add_argument("--layers", default=None,
                    help="逗号分隔信号层栈（默认 F.Cu,B.Cu；N 层板显式给全）")
    ap.add_argument("--width", type=float, default=None,
                    help="线宽 mm（缺省 RouteConfig.width=0.25）")
    ap.add_argument("--via-size", type=float, default=None,
                    help="过孔盘径 mm（缺省 RouteConfig 0.6；人工板实测"
                         " 0.6548）")
    ap.add_argument("--via-drill", type=float, default=None,
                    help="过孔孔径 mm（缺省 RouteConfig 0.3；人工板实测"
                         " 0.35；须 < --via-size）")
    ap.add_argument("--pad-clearance-um", type=float, default=None,
                    help="细间距盘免责净距 µm（语义同 engine；None = 不启用）")
    ap.add_argument("--relax-radius", type=float, default=1.0,
                    help="触发盘免责区半径 mm（配合 --pad-clearance-um，"
                         "默认 1.0）")
    ap.add_argument("--max-iter", type=int, default=15,
                    help="兼容形参（段级调度无协商轮，inert）")
    ap.add_argument("--budget-s", type=float, default=600.0,
                    help="Phase A+B 共享墙钟秒（逐边/逐桥前检查）")
    ap.add_argument("--bridge-rounds", type=int, default=2,
                    help="Phase B 每网最大桥接轮数（桥成重测再进下一轮；"
                         "默认 2）")
    ap.add_argument("--bridge-pairs", type=int, default=8,
                    help="Phase B 每轮尝试的最短跨岛盘对候选上限（默认 8）")
    ap.add_argument("--edge-slice-s", type=float, default=45.0,
                    help="单条边墙钟切片秒：到点本边放弃、网不死"
                         "（0=不限；正交回退无 pop 帽，被围死的边 A* 会"
                         "穷尽可达域，须墙钟兜底。默认 45）")
    ap.add_argument("--dir-prior", action="store_true",
                    help="层方向先验（语义同 engine）")
    ap.add_argument("--par-factor", type=float, default=0.85)
    ap.add_argument("--perp-factor", type=float, default=1.5)
    ap.add_argument("--via-cost", type=float, default=None,
                    help="过孔代价 mm 等效（--dir-prior 时缺省 0.2，"
                         "否则 RouteConfig 缺省 0.4）")
    ap.add_argument("--space", action="store_true",
                    help="间距软排斥（语义同 engine）")
    ap.add_argument("--space-band", type=float, default=0.75)
    ap.add_argument("--space-cost", type=float, default=0.6)
    ap.add_argument("--pitch", action="store_true",
                    help="节距吸引井（语义同 engine；井采样随每边 commit）")
    ap.add_argument("--pitch-band", type=float, default=0.15)
    ap.add_argument("--pitch-factor", type=float, default=0.85)
    ap.add_argument("--pitch-star", type=float, default=1.0)
    ap.add_argument("--pitch-cap", type=float, default=2.5)
    ap.add_argument("--pitch-samples", type=int, default=50)
    ap.add_argument("--via-pitch", action="store_true",
                    help="via 落点节距井（语义同 engine；采样随每边 commit）")
    ap.add_argument("--via-pitch-band", type=float, default=0.1)
    ap.add_argument("--via-pitch-factor", type=float, default=0.85)
    ap.add_argument("--via-pitch-star", type=float, default=0.0)
    ap.add_argument("--via-pitch-cap", type=float, default=2.5)
    ap.add_argument("--via-pitch-samples", type=int, default=20)
    ap.add_argument("--bundle-order", action="store_true",
                    help="兼容形参（束序作用于整网连布序；段级调度 inert）")
    ap.add_argument("--diag45", action="store_true",
                    help="45° 走线族（语义同 engine；软帽回退为边级）")
    ap.add_argument("--turn45-factor", type=float, default=0.3)
    ap.add_argument("--turn135-factor", type=float, default=2.0)
    ap.add_argument("--via-corner-cost", type=float, default=0.3)
    ap.add_argument("--diag-cost", type=float, default=1.0)
    ap.add_argument("--h-weight", type=float, default=1.0)
    ap.add_argument("--diag-node-cap", type=int, default=0,
                    help="Q5 软帽（边级：单边弹出超帽即四向回退整网记账）")
    ap.add_argument("--stagnation", type=int,
                    default=int(os.environ.get("RRL_ENGINE_STAGNATION", "0")),
                    help="兼容形参（无协商轮，inert）")
    ap.add_argument("--refine", action="store_true",
                    help="兼容形参（无精修轮，inert）")
    ap.add_argument("--refine-k", type=int, default=8,
                    help="兼容形参（无精修轮，inert）")
    ap.add_argument("--bundle-lay", action="store_true",
                    help="兼容形参（束武装在引擎 Phase 3，段级调度 inert；"
                         "构造期束规划照常，仅无消费）")
    ap.add_argument("--bundle-band", type=float, default=0.3)
    ap.add_argument("--bundle-factor", type=float, default=0.5)
    ap.add_argument("--bundle-pitch-lo", type=float, default=1.0)
    ap.add_argument("--bundle-arm-cap", type=int, default=150000)
    ap.add_argument("--bundle-arm-reserve-s", type=float, default=120.0)
    ap.add_argument("--dogbone", action="store_true",
                    help="狗骨逃逸落点候选常开注入（语义同 engine Phase 1）")
    ap.add_argument("--dogbone-reach", type=float, default=1.6)
    ap.add_argument("--dogbone-lat", type=float, default=0.8)
    ap.add_argument("--dogbone-cap", type=int, default=64)
    ap.add_argument("--dogbone-rescue", action="store_true",
                    help="兼容形参（补针遍挂在引擎收尾，段级调度 inert——"
                         "Phase B 桥接已是它的段级形态）")
    ap.add_argument("--dogbone-width", type=float, default=None)
    ap.add_argument("--dogbone-rescue-budget-s", type=float, default=0.0)
    ap.add_argument("--dogbone-rescue-joint", action="store_true")
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    if a.dogbone_width is not None and a.dogbone_width <= 0:
        ap.error("--dogbone-width 须为正数（mm）")
    if a.via_size is not None and a.via_size <= 0:
        ap.error("--via-size 须为正数（mm）")
    if a.via_drill is not None and a.via_drill <= 0:
        ap.error("--via-drill 须为正数（mm）")
    if (a.via_size is not None and a.via_drill is not None
            and a.via_drill >= a.via_size):
        ap.error("--via-drill 须小于 --via-size")
    if (a.pad_clearance_um is not None
            and not 0.0 < a.pad_clearance_um / 1000.0
            < a.clearance_um / 1000.0):
        ap.error("--pad-clearance-um 须在 (0, --clearance-um) 开区间内")
    if a.relax_radius <= 0:
        ap.error("--relax-radius 须为正数（mm）")
    if a.bridge_rounds < 0 or a.bridge_pairs < 1:
        ap.error("--bridge-rounds 须 ≥ 0 且 --bridge-pairs 须 ≥ 1")
    if a.edge_slice_s < 0:
        ap.error("--edge-slice-s 须 ≥ 0")

    layers = tuple(a.layers.split(",")) if a.layers else ()
    board = RouteBoard(a.board)
    sr = SegmentRouter(
        board, clearance_mm=a.clearance_um / 1000.0, layers=layers,
        budget_s=a.budget_s, bridge_rounds=a.bridge_rounds,
        bridge_pairs=a.bridge_pairs, edge_slice_s=a.edge_slice_s,
        width=a.width, max_iter=a.max_iter,
        stagnation_k=a.stagnation, dir_prior=a.dir_prior,
        par_factor=a.par_factor, perp_factor=a.perp_factor,
        via_cost=a.via_cost, space=a.space, space_band=a.space_band,
        space_cost=a.space_cost, pitch=a.pitch,
        pitch_band=a.pitch_band, pitch_factor=a.pitch_factor,
        pitch_star=a.pitch_star, pitch_cap=a.pitch_cap,
        pitch_samples=a.pitch_samples, via_pitch=a.via_pitch,
        via_pitch_band=a.via_pitch_band,
        via_pitch_factor=a.via_pitch_factor,
        via_pitch_star=a.via_pitch_star, via_pitch_cap=a.via_pitch_cap,
        via_pitch_samples=a.via_pitch_samples,
        bundle_order=a.bundle_order,
        diag45=a.diag45, turn45_factor=a.turn45_factor,
        turn135_factor=a.turn135_factor,
        via_corner_cost=a.via_corner_cost,
        diag_cost=a.diag_cost, h_weight=a.h_weight,
        diag_node_cap=a.diag_node_cap,
        refine=a.refine, refine_k=a.refine_k,
        bundle_lay=a.bundle_lay, bundle_band=a.bundle_band,
        bundle_factor=a.bundle_factor, bundle_pitch_lo=a.bundle_pitch_lo,
        bundle_arm_cap=a.bundle_arm_cap,
        bundle_arm_reserve_s=a.bundle_arm_reserve_s,
        dogbone=a.dogbone, dogbone_reach=a.dogbone_reach,
        dogbone_lat=a.dogbone_lat, dogbone_cap=a.dogbone_cap,
        dogbone_rescue=a.dogbone_rescue, dogbone_width=a.dogbone_width,
        dogbone_rescue_budget_s=a.dogbone_rescue_budget_s,
        dogbone_rescue_joint=a.dogbone_rescue_joint,
        via_size=a.via_size, via_drill=a.via_drill,
        pad_clearance_mm=(a.pad_clearance_um / 1000.0
                          if a.pad_clearance_um is not None else None),
        relax_radius=a.relax_radius)
    rep = sr.run()
    write_board(a.board, a.out, sr.eng.paths)
    rep["board_out"] = str(a.out)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(
            json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"segment: {rep['status']} nets={rep['nets_routed']}/"
          f"{rep['nets_total']} pads={rep['pads_connected']}/"
          f"{rep['pads_total']}({rep['pad_coverage'] * 100:.1f}%) "
          f"phaseA={rep['phase_a']} phaseB={rep['phase_b']} "
          f"segs={rep['segments']} vias={rep['vias']} "
          f"{rep['wall_s']}s -> {a.out}")


if __name__ == "__main__":
    main()
