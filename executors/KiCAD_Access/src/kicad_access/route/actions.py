"""动作协议 Routing 版 + 快照原语（V3.1 尾巴交付，V3.2 依赖）。

PLAN.md「动作协议 Routing 版定案」（2026-10-08）的实现载体：
RoutingSession 单入口 act(request) -> response 包 NegotiatedRouter——
引擎的 Phase 1/2/3 协商循环由显式动作序列取代（LLM/脚本调度），统一
协议四件套字段按 op 填充（request / 执行前 candidates+refusals /
执行后 applied+snapshot+diff+eval_delta）。零改动 engine.py/astar.py：
只 import 其公开与同包私有 API（Q7 起授权的同包调用体例）。

会话模型（定案 §会话模型）：

- 构造 = 板路径 + 引擎 kwargs 子集，默认对齐 census 基线（clr 230µm /
  budget 900s / 层全集 board.copper_layers），**不跑 run()**
- targets 判定复用引擎（≥2 盘、无 zone、未连通）；已布板天然支持：
  已连通铜 = 障碍、未布网 = target
- rip_up 对非 target 已布网内建 **adopt**（索引 remove + base 文本外科
  + 进 targets——进程内 strip_nets 语义，write_out 不复活）
- 快照链 snap_%04d：快照 = paths 句柄 + targets + base 文本 + 板铜表
  （对象级回滚依据）；rollback 截断链、计数器单调不回拨（id 全局唯一，
  审计友好）。hist 不随快照回滚（引擎单调语义，如实记录）
- ③ 走廊保留 / ④ 扩窗已终局关闭（REPORT_step5 §五 / step4 fail-fast）：
  op 留 schema、执行即 refusal 附出处——能力边界铁律
- 审计 per-session JSONL（ROUTE_AUDIT_DIR 开 / ROUTE_AUDIT_OFF=1 关），
  写失败只吞（d2s 体例 {ts,seq,op,request,ok,summary}）

CLI（确定性验证轨道：同板同动作序列同结果）：

    python -m kicad_access.route.actions --board in.kicad_pcb \
        --out out.kicad_pcb [--act '<json>' | --script acts.jsonl] \
        [--report r.json] [--clearance-um 230] [--layers F.Cu,B.Cu] \
        [--budget-s 900] [--width 0.25]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from .astar import _point_obs_dist
from .drc import run_drc
from .engine import NetPath, NegotiatedRouter, _mst_edges
from .freeroute import _pads_connected
from .model import Pad, RouteBoard, Segment, Via
from .regions import _append_blocks, _segment_block, _via_block

_OPS = {"candidates", "blockers", "route", "rip_up",
        "snapshot", "rollback", "eval", "write_out"}

# 已终局关闭的动作（③④）：op 留 schema、执行即 refusal 附出处。
_CLOSED = {
    "expand_window": (
        "④ 扩搜索窗已终局关闭：对症网四档全死逃逸门、0 锚 0 种子 = "
        "净距本身不足，扩窗救不了；铁律禁降净距（REPORT_step4_failfast.md）"),
    "preserve": (
        "③ 走廊保留已关闭：失去量化靶子——剩余硬块网死因 = 共门口容量 1 / "
        "净距不足，非走廊被偷（REPORT_step5.md §五）"),
}

_KNOWN_CONSTRAINTS: dict[str, frozenset[str]] = {
    "candidates": frozenset({"limit"}),
    "blockers": frozenset({"limit"}),
    "route": frozenset({"hist_penalty", "max_vias", "layers"}),
    "rip_up": frozenset({"hist_penalty"}),
    "snapshot": frozenset(),
    "rollback": frozenset(),
    "eval": frozenset(),
    "write_out": frozenset(),
}

_TARGET_KEYS: dict[str, tuple[str, ...]] = {
    "candidates": ("net",),
    "blockers": ("net",),
    "route": ("net",),
    "rip_up": ("net",),
    "rollback": ("snapshot",),
    "snapshot": (),
    "eval": (),
    "write_out": (),
}


def _block_items(text: str):
    """产出 kicad_pcb 直接子条目（一个 tab 缩进的 segment/via 块）的
    (start, end)——strip_nets.py 同款括号平衡游走，跳过字符串字面量。"""
    for m in re.finditer(r"\n\t\((segment|via)\b", text):
        i = m.start() + 1  # 跳过换行，指向 '('
        n = len(text)
        depth = 0
        while i < n:
            c = text[i]
            if c == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == '\\' else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        yield m.start(), i


def _strip_net_blocks(text: str, net: str) -> tuple[str, int]:
    """摘除 base 文本中该网的 segment/via 块（进程内 strip_nets 外科）。

    返回 (新文本, 摘除块数)——块数供调用方与板模型对账（文本/模型不一致
    即拒绝 adopt，防半摘状态）。
    """
    pat = re.compile(r'\(net\s+"([^"]*)"\)')
    out, pos, removed = [], 0, 0
    for s, e in _block_items(text):
        m = pat.search(text[s:e])
        if m and m.group(1) == net:
            removed += 1
            out.append(text[pos:s])
            pos = e
            while pos < len(text) and text[pos] == "\n":
                pos += 1
    out.append(text[pos:])
    return "".join(out), removed


class RoutingSession:
    """板上单网级路由动作会话：快照事务 + adopt + 归因读数。"""

    def __init__(self, board_path: Path | str, *,
                 clearance_um: int = 230, budget_s: float = 900.0,
                 layers: tuple[str, ...] | None = None,
                 **engine_kw):
        self.board_path = Path(board_path)
        self._src_text = self.board_path.read_text(encoding="utf-8")
        self.board = RouteBoard(self.board_path)
        # 层全集缺省 = 板层栈（GridRouter 的 layers=() 缺省会退回
        # ("F.Cu","B.Cu")——N 层板必须显式给全，census 基线口径）
        lay = tuple(layers) if layers else self.board.copper_layers
        self.eng = NegotiatedRouter(self.board, clearance_mm=clearance_um / 1000.0,
                                    layers=lay, budget_s=budget_s, **engine_kw)
        self._default_out = self.board_path.with_name(
            self.board_path.stem + "_act.kicad_pcb")
        self._snaps: list[dict] = []
        self._snap_seq = -1  # 单调计数器：rollback 截断链也不回拨
        self._seq = 0
        self._snap()  # snap-0000 = 构造态
        self._audit_path: Path | None = None
        if os.environ.get("ROUTE_AUDIT_DIR") and \
                os.environ.get("ROUTE_AUDIT_OFF") != "1":
            try:
                d = Path(os.environ["ROUTE_AUDIT_DIR"])
                d.mkdir(parents=True, exist_ok=True)
                self._audit_path = d / (
                    f"route_session_{time.strftime('%Y%m%d_%H%M%S')}"
                    f"_{os.getpid()}.jsonl")
            except OSError:
                self._audit_path = None

    # ------------------------------------------------------------ 单入口

    def act(self, request: dict) -> dict:
        """统一动作入口：request = 四件套第一件
        {"tool":"routing","op":...,"target":{...},"constraints":{...},
         "rationale":"..."}（tool/rationale 仅入审计日志）。"""
        self._seq += 1
        resp = self._dispatch(request if isinstance(request, dict) else {})
        self._audit(request, bool(resp.get("applied")), _summary(resp))
        return resp

    def _dispatch(self, request: dict) -> dict:
        op = request.get("op")
        if not isinstance(op, str) or not op:
            return self._refusal(None, "request 缺少 op 字段")
        if op in _CLOSED:
            return self._refusal(op, _CLOSED[op])
        if op not in _OPS:
            return self._refusal(
                op, f"未知 op（可用：{', '.join(sorted(_OPS))}；"
                    f"已关闭：{', '.join(sorted(_CLOSED))}）")
        cons = request.get("constraints") or {}
        if not isinstance(cons, dict):
            return self._refusal(op, "constraints 须为 JSON 对象")
        unknown = sorted(set(cons) - _KNOWN_CONSTRAINTS[op])
        if unknown:
            return self._refusal(
                op, f"未知 constraints 键 {unknown}（可用："
                    f"{sorted(_KNOWN_CONSTRAINTS[op])}）")
        tgt = request.get("target") or {}
        if not isinstance(tgt, dict):
            return self._refusal(op, "target 须为 JSON 对象")
        missing = [k for k in _TARGET_KEYS[op] if k not in tgt]
        if missing:
            return self._refusal(op, f"target 缺必填键 {missing}")
        if op == "route" and "layers" in cons:
            return self._refusal(
                op, "constraints.layers 需动 astar 层参（v1 未解冻；"
                    "层集在会话构造时一次性给定）——PLAN.md 定案 route 行")
        if op == "candidates":
            return self._op_candidates(tgt["net"], cons)
        if op == "blockers":
            return self._op_blockers(tgt["net"], cons)
        if op == "route":
            return self._op_route(tgt["net"], cons)
        if op == "rip_up":
            return self._op_rip_up(tgt["net"], cons)
        if op == "snapshot":
            return {"op": "snapshot", "applied": True,
                    "snapshot": self._snap()["id"]}
        if op == "rollback":
            return self._op_rollback(tgt["snapshot"])
        if op == "eval":
            return {"op": "eval", "applied": True,
                    "snapshot": self._snaps[-1]["id"], **self._eval_report()}
        return self._op_write_out(tgt)

    @staticmethod
    def _refusal(op, reason: str) -> dict:
        return {"op": op, "applied": False, "reason": reason}

    # ------------------------------------------------- 执行前件（只读）

    def _op_candidates(self, net: str, cons: dict) -> dict:
        """狗骨落点枚举（定案：`_dogbone_seeds` 序列化，确定性 id）。"""
        eng = self.eng
        r = eng.router
        if net not in eng.targets:
            return self._refusal("candidates", f"网 {net} 不是会话 target")
        if not r.cfg.dogbone:
            return self._refusal(
                "candidates", "会话未开 dogbone（构造时 dogbone=True 才有"
                              "落点注入；旗标关时 route() 不会消费种子）")
        limit = _as_int(cons.get("limit", 8))
        if limit is None or limit < 1:
            return self._refusal("candidates", "constraints.limit 须为正整数")
        half_w = r.width(net) / 2 + r.cfg.clearance
        dog_half = (half_w if r.cfg.dogbone_width is None
                    else r.cfg.dogbone_width / 2 + r.cfg.clearance)
        st = r.cfg.step
        cands: list[dict] = []
        refusals: list[dict] = []
        for pad in sorted(eng._pads[net], key=lambda q: q.key):
            seeds = r._dogbone_seeds(pad, net, dog_half)
            if not seeds:
                refusals.append({"reason": f"{pad.key} 无狗骨落点"
                                           f"（净距/可达性不足）"})
                continue
            for k, (nix, niy, lix, stub, has_lat) in enumerate(seeds[:limit]):
                vx, vy = nix * st, niy * st
                cands.append({
                    "id": f"dg-{pad.key}-{k:02d}",
                    "pad": pad.key,
                    "layer": r.layers[lix],
                    "landing": [round(vx, 3), round(vy, 3)],
                    "stub_mm": round(stub, 3),
                    "lateral": bool(has_lat),
                    "side_effects": self._seed_side_effects(vx, vy, net),
                })
        if not cands:
            refusals.append({"reason": f"网 {net} 全盘无落点"})
        return {"op": "candidates", "applied": True, "net": net,
                "candidates": cands, "refusals": refusals}

    def _seed_side_effects(self, vx: float, vy: float, net: str) -> list[str]:
        """落点孔环（半径 = 孔径/2+净距）命中的异网盘 = 挤占逃逸带。"""
        r = self.eng.router
        hit, seen = [], set()
        for lay in r.layers:
            for obs in r.index._at_point(lay, vx, vy):
                if obs.net != net and isinstance(obs, Pad) \
                        and obs.key not in seen \
                        and _point_obs_dist(vx, vy, obs) < r._via_radius:
                    seen.add(obs.key)
                    hit.append(f"{obs.key} 逃逸带被挤占")
        return hit

    def _op_blockers(self, net: str, cons: dict) -> dict:
        """挡道网归因：盘对 MST 直连 bbox 框查询 → 异网铜聚合排序。

        对 target 与非 target 网都可用（非 target 现算 MST）——V3.2
        可布性体检报告的直接输入。
        """
        eng = self.eng
        pads = eng._pads.get(net)
        if not pads or len(pads) < 2:
            return self._refusal("blockers", f"网 {net} 盘数不足"
                                             f"（板无此网或 <2 盘）")
        limit = _as_int(cons.get("limit", 8))
        if limit is None or limit < 1:
            return self._refusal("blockers", "constraints.limit 须为正整数")
        pairs = eng._mst.get(net) or _mst_edges(pads)
        r = eng.router
        agg: dict[str, dict] = {}
        for a, b, _d in pairs:
            x1, y1 = min(a.x, b.x), min(a.y, b.y)
            x2, y2 = max(a.x, b.x), max(a.y, b.y)
            for lay in r.layers:
                for obs in r.index._along_edge(lay, x1, y1, x2, y2):
                    if obs.net is None or obs.net == net:
                        continue
                    e = agg.setdefault(obs.net, {
                        "net": obs.net, "hits": 0, "pads": 0,
                        "segments": 0, "vias": 0, "layers": set(),
                        "bbox": [x1, y1, x2, y2]})
                    e["hits"] += 1
                    e["layers"].add(lay)
                    if isinstance(obs, Pad):
                        e["pads"] += 1
                    elif isinstance(obs, Segment):
                        e["segments"] += 1
                    else:
                        e["vias"] += 1
                    bx = e["bbox"]
                    bx[0], bx[1] = min(bx[0], x1), min(bx[1], y1)
                    bx[2], bx[3] = max(bx[2], x2), max(bx[3], y2)

        def _span(e: dict) -> float:
            b = e["bbox"]
            return (b[2] - b[0]) + (b[3] - b[1])

        items = sorted(agg.values(), key=lambda e: (-e["hits"], -_span(e)))
        blockers = [{"net": e["net"], "hits": e["hits"], "pads": e["pads"],
                     "segments": e["segments"], "vias": e["vias"],
                     "layers": sorted(e["layers"]),
                     "span_mm": [round(v, 2) for v in e["bbox"]]}
                    for e in items[:limit]]
        return {"op": "blockers", "applied": True, "net": net,
                "pairs": len(pairs), "blockers": blockers}

    # ------------------------------------------------------- 执行动作

    def _op_route(self, net: str, cons: dict) -> dict:
        """布一网：隐式拆旧铜 → 单布 → max_vias 后校验 → commit+冲突计数。"""
        eng = self.eng
        if net not in eng.targets:
            return self._refusal("route", f"网 {net} 不是会话 target"
                                          f"（已布网先 rip_up/adopt）")
        if "max_vias" in cons:
            mv = _as_int(cons["max_vias"])
            if mv is None or mv < 0:
                return self._refusal("route", "constraints.max_vias 须为非负整数")
        else:
            mv = None
        pre = self._totals()
        p = eng.paths[net]
        ripped = p.copper()
        if ripped:
            eng._rip(net, penalise=False)  # 隐式拆旧铜（含换布语义）
        p.err = None  # 清残留 err（引擎 _rip 不清；新尝试从零计）
        eng._route_net(net, eng.hist if cons.get("hist_penalty") else None)
        p = eng.paths[net]
        committed = False
        if p.err is None and not p.copper():  # 防御：全或无语义不应走到
            p.err = "无可行路径"
        if p.err is None and mv is not None and len(p.vias) > mv:
            nv = len(p.vias)
            eng._rip(net, penalise=False)
            p.err = f"max_vias 超限自动回滚（{nv} > {mv}）"
        if p.err is None:
            eng.router.commit(p.segs, p.vias)
            p.conflicts = eng._conflict_count(net, p)
            committed = True
        snap = self._snap()
        post = self._totals()
        resp = {"op": "route", "applied": True, "net": net,
                "routed": committed, "snapshot": snap["id"],
                "diff": self._diff(pre, post),
                "eval_delta": self._delta(post)}
        if p.err is not None:
            resp["err"] = p.err
        if ripped:
            resp["implicit_rip"] = True
        return resp

    def _op_rip_up(self, net: str, cons: dict) -> dict:
        """拆一网：target 铜走引擎 _rip；非 target 板铜走 adopt。"""
        eng = self.eng
        if net in eng.targets:
            p = eng.paths[net]
            if not p.copper():
                return self._refusal("rip_up", f"网 {net} 无铜可拆")
            pre = self._totals()
            eng._rip(net, penalise=bool(cons.get("hist_penalty")))
            snap = self._snap()
            post = self._totals()
            return {"op": "rip_up", "applied": True, "net": net,
                    "adopt": False, "snapshot": snap["id"],
                    "diff": self._diff(pre, post),
                    "eval_delta": self._delta(post)}
        # ---- 非 target：adopt（进程内 strip_nets 语义）
        pads = eng._pads.get(net)
        if pads is None:
            return self._refusal("rip_up", f"网 {net} 不在板上")
        if net in {z.net for z in self.board.zones}:
            return self._refusal("rip_up", f"网 {net} 有 zone"
                                           f"（fill 保证连通，不 adopt）")
        if len(pads) < 2:
            return self._refusal("rip_up", f"网 {net} 盘数 {len(pads)} < 2"
                                           f"，adopt 后无法重布")
        segs = [s for s in self.board.segments if s.net == net]
        vias = [v for v in self.board.vias if v.net == net]
        if not segs and not vias:
            return self._refusal("rip_up", f"网 {net} 无铜可拆")
        new_text, n_blk = _strip_net_blocks(self._src_text, net)
        if n_blk != len(segs) + len(vias):
            return self._refusal(
                "rip_up", f"base 文本铜块 {n_blk} ≠ 板模型 "
                          f"{len(segs) + len(vias)}（文本/模型不一致，拒 adopt）")
        pre = self._totals()
        eng.router.index.remove(segs, vias)  # 对象身份摘索引
        self.board.segments = [s for s in self.board.segments if s.net != net]
        self.board.vias = [v for v in self.board.vias if v.net != net]
        self._src_text = new_text  # write_out 不复活
        eng._mst[net] = _mst_edges(pads)
        eng.targets = sorted([*eng.targets, net])
        eng.paths[net] = NetPath()  # 未布态 target
        snap = self._snap()
        post = self._totals()
        return {"op": "rip_up", "applied": True, "net": net, "adopt": True,
                "snapshot": snap["id"], "diff": self._diff(pre, post),
                "eval_delta": self._delta(post),
                "note": f"adopt：摘 base 铜 {len(segs)} seg + "
                        f"{len(vias)} via，注册为 target"}

    # ------------------------------------------------------ 快照事务

    def _snap(self) -> dict:
        """全量快照入链：paths 句柄 + targets + base 文本 + 板铜表。

        引擎 _snapshot 的 (segs, vias, err, conflicts) 是浅拷贝表、
        Segment/Via 对象不可变引用——re-route 只换属性引用不改原表
        （--dogbone-rescue-joint 缓存同款安全前提），快照可回放。
        """
        self._snap_seq += 1
        snap = {"id": f"snap-{self._snap_seq:04d}",
                "paths": self.eng._snapshot(),
                "targets": list(self.eng.targets),
                "src_text": self._src_text,
                "board_segments": list(self.board.segments),
                "board_vias": list(self.board.vias)}
        self._snaps.append(snap)
        return snap

    def _op_rollback(self, snap_id) -> dict:
        if not isinstance(snap_id, str):
            return self._refusal("rollback", "target.snapshot 须为字符串 id")
        idx = next((i for i, s in enumerate(self._snaps)
                    if s["id"] == snap_id), None)
        if idx is None:
            return self._refusal(
                "rollback", f"未知快照 {snap_id}（链内："
                            f"{[s['id'] for s in self._snaps]}）")
        self._restore_full(self._snaps[idx])
        self._snaps = self._snaps[:idx + 1]
        return {"op": "rollback", "applied": True, "snapshot": snap_id,
                "chain": [s["id"] for s in self._snaps]}

    def _restore_full(self, snap: dict) -> None:
        """回放全量快照（引擎 _restore 的超集：targets/板表/base 文本）。

        引擎 _restore 按 self.targets 索引旧快照、adopt 扩 targets 后会
        KeyError，故会话自带对象级回放：
        1) 现有路径铜全退 commit（索引+板表按对象身份退回）
        2) 恢复快照后被 adopt 摘除的 base 铜（不在当前板表、也不在快照
           paths 里的对象——后者由 3) 重新 commit，避免板表双插）
        3) targets/paths 回快照态（多删少补），铜重放 + 逐网 commit
        4) base 文本回快照态（adopt 外科回退）
        """
        eng = self.eng
        for p in eng.paths.values():
            if p.copper():
                eng.router.rollback(p.segs, p.vias)
                p.segs, p.vias = [], []
        path_ids = {id(o) for t in snap["paths"].values()
                    for o in (t[0] + t[1])}
        for lst, key in ((self.board.segments, "board_segments"),
                         (self.board.vias, "board_vias")):
            cur = {id(o) for o in lst}
            back = [o for o in snap[key]
                    if id(o) not in cur and id(o) not in path_ids]
            if back:
                eng.router.index.add(
                    [o for o in back if isinstance(o, Segment)],
                    [o for o in back if isinstance(o, Via)])
                lst.extend(back)
        eng.targets = list(snap["targets"])
        for n in list(eng.paths):
            if n not in snap["paths"]:
                del eng.paths[n]
                eng._mst.pop(n, None)
        for n in eng.targets:
            eng.paths.setdefault(n, NetPath())
            eng._mst.setdefault(n, _mst_edges(eng._pads[n]))
            t = snap["paths"][n]
            p = eng.paths[n]
            p.segs, p.vias = list(t[0]), list(t[1])
            p.err, p.conflicts = t[2], t[3]
            if p.copper():
                eng.router.commit(p.segs, p.vias)
        self._src_text = snap["src_text"]

    # --------------------------------------------------------- 读数

    def _totals(self) -> dict:
        eng = self.eng
        return {"nets_routed": sum(1 for n in eng.targets
                                   if eng.paths[n].copper()),
                "nets_total": len(eng.targets),
                "conflicts": sum(p.conflicts for p in eng.paths.values()),
                "segs": sum(len(p.segs) for p in eng.paths.values()),
                "vias": sum(len(p.vias) for p in eng.paths.values())}

    @staticmethod
    def _diff(pre: dict, post: dict) -> str:
        ds, dv = post["segs"] - pre["segs"], post["vias"] - pre["vias"]
        parts = []
        if ds:
            parts.append(f"{ds:+d} seg")
        if dv:
            parts.append(f"{dv:+d} via")
        return " ".join(parts) if parts else "0"

    @staticmethod
    def _delta(post: dict) -> dict:
        return {"nets_routed": post["nets_routed"],
                "nets_total": post["nets_total"],
                "conflicts": post["conflicts"]}

    def _eval_report(self) -> dict:
        """eval op 读数（report 口镜子集 + self_drc）。"""
        eng = self.eng
        targets = eng.targets
        # 会话 target 从未动过 = 无铜无 err——按未布计（引擎 report 按
        # err 计是对 run() 全量布线后的口径，会话动作面须含未动网）
        unrouted = [n for n in targets
                    if eng.paths[n].err is not None
                    or not eng.paths[n].copper()]
        conf = sum(eng.paths[n].conflicts for n in targets)
        segs = [s for n in targets for s in eng.paths[n].segs]
        vias = [v for n in targets for v in eng.paths[n].vias]
        per_net = {}
        for n in targets:
            p = eng.paths[n]
            per_net[n] = {"err": p.err, "conflicts": p.conflicts,
                          "segs": len(p.segs), "vias": len(p.vias),
                          "connected": _pads_connected(
                              self.board,
                              [s for s in self.board.segments if s.net == n],
                              [v for v in self.board.vias if v.net == n], n)}
        status = ("ok" if not unrouted and conf == 0
                  else "partial" if len(unrouted) < len(targets)
                  else "failed")
        return {"status": status,
                "nets_total": len(targets),
                "nets_routed": len(targets) - len(unrouted),
                "nets_unrouted": unrouted,
                "conflicts": conf,
                "segments": len(segs), "vias": len(vias),
                "length_mm": round(sum(abs(s.x2 - s.x1) + abs(s.y2 - s.y1)
                                       for s in segs), 1),
                "clearance_mm": eng.router.cfg.clearance,
                "layers": list(eng.router.layers),
                "per_net": per_net,
                "self_drc": [str(v) for v in run_drc(
                    self.board, clearance=eng.router.cfg.clearance)]}

    def _op_write_out(self, tgt: dict) -> dict:
        """会话铜落盘：外科后的 base 文本 + paths 各网 segs/vias 块
        （write_board 同款原语；adopt 摘过的 base 铜不复活）。"""
        out = Path(tgt.get("path") or self._default_out)
        blocks = ([_segment_block(s) for n in sorted(self.eng.paths)
                   for s in self.eng.paths[n].segs]
                  + [_via_block(v) for n in sorted(self.eng.paths)
                     for v in self.eng.paths[n].vias])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_append_blocks(self._src_text, blocks),
                       encoding="utf-8")
        return {"op": "write_out", "applied": True, "path": str(out),
                "segments": sum(len(p.segs) for p in self.eng.paths.values()),
                "vias": sum(len(p.vias) for p in self.eng.paths.values())}

    # --------------------------------------------------------- 审计

    def _audit(self, request, ok: bool, summary: str) -> None:
        if self._audit_path is None:
            return
        try:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "seq": self._seq, "op": (request or {}).get("op"),
                   "request": request, "ok": ok, "summary": summary}
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 审计失败只吞（d2s 体例）


def _as_int(val):
    """约束值转 int；不可转回 None（refusal 文案由调用方定）。"""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _summary(resp: dict) -> str:
    if not resp.get("applied"):
        return "refused: " + str(resp.get("reason", ""))[:80]
    parts = [str(resp.get("op"))]
    if "routed" in resp:
        parts.append("routed" if resp["routed"] else "unrouted")
    if "adopt" in resp:
        parts.append("adopt" if resp["adopt"] else "target-rip")
    if "snapshot" in resp:
        parts.append(str(resp["snapshot"]))
    return " ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route.actions",
        description="动作协议 Routing 版（RoutingSession 薄 CLI；"
                    "script 重放 = 确定性验证轨道）")
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="输出板（默认 <board>_act.kicad_pcb；收尾总 write_out）")
    ap.add_argument("--act", default=None,
                    help="单条动作 JSON（与 --script 可并用）")
    ap.add_argument("--script", type=Path, default=None,
                    help="动作脚本 JSONL（每行一条，# 注释行跳过）")
    ap.add_argument("--report", type=Path, default=None,
                    help="收尾 eval 报告 JSON 落盘")
    ap.add_argument("--clearance-um", type=int, default=230)
    ap.add_argument("--layers", default=None,
                    help="逗号分隔信号层（默认板层全集）")
    ap.add_argument("--budget-s", type=float, default=900.0)
    ap.add_argument("--width", type=float, default=None)
    args = ap.parse_args()

    sess = RoutingSession(
        args.board, clearance_um=args.clearance_um, budget_s=args.budget_s,
        layers=tuple(args.layers.split(",")) if args.layers else None,
        width=args.width)
    if args.out is not None:
        sess._default_out = args.out
    reqs = []
    if args.act:
        reqs.append(json.loads(args.act))
    if args.script:
        for line in args.script.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            reqs.append(json.loads(line))
    for req in reqs:
        print(json.dumps(sess.act(req), ensure_ascii=False))
    print(json.dumps(sess.act({"op": "write_out",
                               "target": {"path": str(sess._default_out)}}),
                     ensure_ascii=False))
    if args.report:
        args.report.write_text(
            json.dumps(sess._eval_report(), ensure_ascii=False, indent=1),
            encoding="utf-8")


if __name__ == "__main__":
    main()
