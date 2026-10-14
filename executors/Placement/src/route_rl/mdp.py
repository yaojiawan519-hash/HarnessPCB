"""R1 数据入口：triples.jsonl → 定长 MDP 数据集（d3rlpy.MDPDataset 兼容布局）。

episode 划分：每个 (run_id, pass) 组一个 episode；base 行（iter=-1）只提供
状态基线、不产生转移。转移 (s, a, r, s', terminal)：

- s  = 定长观测向量 OBS_SPEC（pass one-hot / iter / 上轮动作槽 / 基线
       sig·pwr_plane·copper·un / viol 词表向量 / heatmap 16×16）——
       全部取自动作前可得信息（round 的 state_before = pass 基线板判定）
- a  = multi-discrete 4 槽 [clearance, via_ladder, layer_act, edge_inset]；
       各槽 0 = 未设（缺省），值域 = 网格档位 + 1。另存 actions_flat
       （笛卡尔平铺索引）供单离散算法直接吃
- r  = -(w₁·sig + w₂·copper + w₃·un)；SES 失败 = 词表上限惩罚
       （w 由 WEIGHTS 预设给出，lex 模式按数据集分量的观测上限嵌入字典序）；
       q4 模式 = 质量分对比 FR 基线（九格刻度进奖励，设计见
       KiCAD_Access/eval_out/q4_reward/PLAN.md）
- s' = 下轮同构向量（当前 best-of-N 循环里仅 iter/last_action 随轮演化；
       链式喂板（R1 后续）时 state_before 逐轮不同，本函数自动跟随）
- terminal = 该轮触发早停（完成度+铜级双 0）——真终态不 bootstrap；
  timeouts = 阶梯跑尽 —— 截断，bootstrap

输出 .npz + .meta.json（权重/网格/词表/溯源逐行映射全留档，可再生校验）。
d3rlpy 未装也能转（延迟导入 to_mdp_dataset）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# 定长词表 / 网格（动作空间与观测维度的唯一事实源；变更 = 数据集重生）
# --------------------------------------------------------------------------
PASS_NAMES = ("signal", "power", "finish", "planes")

#: viol 词表 = 6 份 gate triples 观测键的全集 + 预留 + other 兜底。
#: 观测向量定长的前提：新 DRC 键一律落 "other"，不改维度。
VIOL_KEYS = (
    "clearance", "copper_edge_clearance", "courtyards_overlap",
    "drill_out_of_range", "hole_clearance", "hole_to_hole", "holes_co_located",
    "isolated_copper", "lib_footprint_issues", "lib_footprint_mismatch",
    "shorting_items", "silk_edge_clearance", "silk_over_copper",
    "silk_overlap", "solder_mask_bridge", "starved_thermal", "track_dangling",
    "track_width", "unconnected", "via_dangling", "via_diameter",
    "zones_intersect", "other",
)

CLEARANCE_GRID = tuple(range(200, 265, 5))    # 200–260µm ×5µm，13 档
VIA_LADDER_MAX = 8                             # via_ladder 槽上限（0=未设）
LAYER_ACT_MAX = 15                             # 层激活子集位上限（0=未设）
EDGE_INSET_GRID = tuple(range(300, 625, 25))   # 300–600µm ×25µm，13 档

ACTION_SLOTS = ("clearance", "via_ladder", "layer_act", "edge_inset")
SLOT_CARD = (len(CLEARANCE_GRID) + 1, VIA_LADDER_MAX + 1,
             LAYER_ACT_MAX + 1, len(EDGE_INSET_GRID) + 1)

#: 观测布局（与 obs_vector 的拼装顺序一一对应）
OBS_SPEC = {
    "pass_onehot": len(PASS_NAMES),            # 4
    "iter": 1,
    "last_action": len(ACTION_SLOTS),          # 4
    "scalars": 4,                              # sig pwr_plane copper un（log1p）
    "viol": len(VIOL_KEYS),                    # log1p 计数
    "heatmap": 256,                            # 16×16（log1p）
}
OBS_DIM = sum(OBS_SPEC.values())               # 292


def _bucket(value, grid: tuple[int, ...]) -> int:
    """值→最近档位索引（越界钳到端点）。调用方负责"键存在才编非 0 槽"。"""
    return min(range(len(grid)), key=lambda i: abs(grid[i] - value))


def encode_action(action: dict | None) -> tuple[int, int, int, int]:
    """动作 dict → multi-discrete 槽值。槽 0 恒为"未设"；网格值从 1 起。"""
    a = action or {}
    out = []
    if "clearance_um" in a:
        out.append(_bucket(a["clearance_um"], CLEARANCE_GRID) + 1)
    else:
        out.append(0)
    out.append(min(int(a.get("via_idx", 0)), VIA_LADDER_MAX))
    out.append(min(int(a.get("layer_act", 0)), LAYER_ACT_MAX))
    if "edge_inset_um" in a:
        out.append(_bucket(a["edge_inset_um"], EDGE_INSET_GRID) + 1)
    else:
        out.append(0)
    return tuple(out)


def flatten_action(slots: tuple[int, ...]) -> int:
    """multi-discrete → 单离散笛卡尔索引（供 discrete CQL 等直接用）。"""
    idx = 0
    for v, card in zip(slots, SLOT_CARD):
        idx = idx * card + v
    return idx


def unflatten_action(idx: int) -> tuple[int, ...]:
    """笛卡尔索引 → multi-discrete 槽值（flatten_action 的逆；训练后回读用）。"""
    slots = []
    for card in reversed(SLOT_CARD):
        slots.append(idx % card)
        idx //= card
    return tuple(reversed(slots))


# --------------------------------------------------------------------------
# 标量奖励
# --------------------------------------------------------------------------
def reward_weights(mode: str, caps: dict[str, float] | None = None) -> dict:
    """奖励权重预设。caps = 数据集各分量观测上限（build 时统计）。

    - flat：w=(1,1,1)，公式原样，量纲不换算 —— 供扫参敏感性对照
      （实测会把 copper 的原始量级 438 放在完成度 sig 之上，慎作训练目标）
    - lex：嵌入字典序 (sig ≻ copper ≻ un)：w₁=w₂·(cap_copper+1)，
      w₂=cap_un+1，w₃=1 —— 观测上限内与 score tuple 排序严格一致
      （代价是量级悬殊 ~2e5:223:1，对 CQL 的 Q 回归不友好，默认不用）
    - norm：w_k = 优先级_k / cap_k —— 各分量贡献 ∈ [0, 优先级]，有界；
      优先级默认 (1,1,1)，调比例即可表达偏好（CQL 训练的推荐起点）
    """
    if mode == "flat":
        return {"mode": "flat", "w_sig": 1.0, "w_copper": 1.0, "w_un": 1.0}
    if mode == "lex":
        cap_un = (caps or {}).get("un", 300.0)
        cap_copper = (caps or {}).get("copper", 1000.0)
        w2 = cap_un + 1
        return {"mode": "lex", "w_sig": float(w2 * (cap_copper + 1)),
                "w_copper": float(w2), "w_un": 1.0}
    if mode == "norm":
        caps = caps or {}
        prio = {"sig": 1.0, "copper": 1.0, "un": 1.0}
        return {"mode": "norm",
                "w_sig": prio["sig"] / max(caps.get("sig", 6.0), 1.0),
                "w_copper": prio["copper"] / max(caps.get("copper", 438.0), 1.0),
                "w_un": prio["un"] / max(caps.get("un", 112.0), 1.0)}
    raise ValueError(f"未知权重模式: {mode}")


def reward(outcome: dict | None, w: dict) -> float:
    """r = -(w₁·sig + w₂·copper + w₃·un)。SES 失败/无结果 = 词表上限惩罚。"""
    if not outcome:
        # 失败惩罚与 lex 上限同构：各分量按 caps 取满（caps 编在 w 里）
        return -(w["w_sig"] * w.get("cap_sig", 300.0)
                 + w["w_copper"] * w.get("cap_copper", 1000.0)
                 + w["w_un"] * w.get("cap_un", 300.0))
    return -(w["w_sig"] * outcome.get("sig", 0)
             + w["w_copper"] * outcome.get("copper", 0)
             + w["w_un"] * outcome.get("un", 0))


# --------------------------------------------------------------------------
# Q4 质量分奖励（对比 FR 基线；设计 = KiCAD_Access/eval_out/q4_reward/PLAN.md）
# --------------------------------------------------------------------------
#: 刻度 → (权重, 方向)。秩序三刻度权重 1.0（Q0 裁决：人工近全胜、FR 系统性缺），
#: 效率压 sanity 档 0.25（Q0：效率 FR 全胜，防走样）。segs 不进（与 bends/len
#: 共线）；arcs 不进（FR 恒 0 纯送分）。
Q4_SCALES = {
    "parallel_pct":   (1.0, +1),
    "spacing_cv":     (1.0, -1),
    "dir_hv_pct":     (1.0, +1),
    "len_ratio":      (0.25, -1),
    "via_per_net":    (0.25, -1),
    "bends_per_10mm": (0.25, -1),
    "non45_len_pct":  (0.25, -1),
}
Q4_FAIL_REWARD = -2.0        # 硬门不及格：低于任何及格分（质量分 ∈ [-1, 1]）


def q4_weights(baselines: Path) -> dict:
    """q4 权重模式：权重表 + 整张 fr_baselines.json（q4_fr_baselines.py 生成）。"""
    p = Path(baselines)
    doc = json.loads(p.read_text())
    return {"mode": "q4", "scales": Q4_SCALES, "baselines": doc,
            "baselines_path": str(p),
            "baselines_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "baseline_quality_sha256": doc.get("quality_sha256")}


def _q4_anchor(w: dict, board_id, clearance_um) -> tuple[str, dict | None]:
    """锚查找四级兜底（PLAN §3c）：同档 → champion → dq_reference → none。

    同档再按 gate_pass 细分 cat：same_c（干净锚）/ same_c_dq（该档 FR 没布完，
    全 DQ 板如 mixer 全走这档）——PLAN §4 的"DQ 锚净胜单列"靠这个区分。
    """
    b = (w.get("baselines") or {}).get("boards", {}).get(board_id or "")
    if b:
        if clearance_um is not None:
            e = b.get("fr_by_clearance", {}).get(str(int(clearance_um)))
            if e is not None:
                return ("same_c" if e.get("gate_pass") else "same_c_dq"), e["quality"]
        if b.get("fr_champion"):
            return "champion", b["fr_champion"]
        if b.get("dq_reference"):
            return "dq", b["dq_reference"]
    return "none", None


def _q4_step_reward(rec: dict, w: dict, base_copper: int) -> tuple[float, str]:
    """一轮的 q4 奖励（PLAN §3d）。

    硬门先行：unconn>0 / 引入铜>0 / outcome 或 quality 缺失 → −2.0。
    无锚板（kit-dev / tinytapeout）只算硬门：及格 0（中性），不造跨板常数锚。
    有锚按逐刻度相对分 s_i = dir·(x−b)/(x+b)，r = Σw·s/Σw ∈ [−1,1]，
    0 = 恰好追平锚。刻度缺失跳过+权重重归一；双 0 = 追平（s=0）。
    """
    oc = rec.get("outcome") if rec.get("ses_ok") else None
    q = rec.get("quality")
    if not oc or not q:
        return Q4_FAIL_REWARD, "fail_missing"
    # un 门用 quality.unconn（信号网口径，与锚表 gate_pass 同源）而非 DRC 全口径 un：
    # DRC 的 un 在 refill 后含 zone 网 NPTH 机械盘等板固有飞线（pic U2 pin10/13），
    # 物理不可连、quality 不数，用 DRC 口径会误杀（2026-10-02 冒烟查实）。
    if (q.get("unconn") or 0) > 0 or (oc.get("copper") or 0) - base_copper > 0:
        return Q4_FAIL_REWARD, "fail_gate"
    clr = (rec.get("action") or {}).get("clearance_um")
    cat, anchor = _q4_anchor(w, rec.get("board_id"), clr)
    if anchor is None:
        return 0.0, cat
    num = den = 0.0
    for m, (wt, direction) in w["scales"].items():
        x, b = q.get(m), anchor.get(m)
        if x is None or b is None:            # 刻度/锚值缺失：跳过+重归一
            continue
        den += wt
        if x + b > 0:                         # x=b=0 → 追平，s=0
            num += wt * direction * (x - b) / (x + b)
    return (num / den if den else 0.0), cat


# --------------------------------------------------------------------------
# 观测
# --------------------------------------------------------------------------
def obs_vector(base: dict | None, pass_name: str, it: int,
               last_action: dict | None) -> np.ndarray:
    """定长观测（动作前信息）。base = round 的 state_before（pass 基线判定；
    链式循环下为当前基板判定，字段同构）。计数类一律 log1p 压动态。"""
    v = np.zeros(OBS_DIM, dtype=np.float32)
    o = 0
    code = PASS_NAMES.index(pass_name) if pass_name in PASS_NAMES else -1
    for i in range(len(PASS_NAMES)):
        v[o + i] = 1.0 if i == code else 0.0
    o += OBS_SPEC["pass_onehot"]
    v[o] = min(it, 99)                     # clip：防御异常长阶梯
    o += 1
    v[o:o + 4] = encode_action(last_action)
    o += 4
    if base:
        for j, k in enumerate(("sig", "pwr_plane", "copper", "un")):
            v[o + j] = math.log1p(base.get(k) or 0)
    o += 4
    if base:
        viol = base.get("viol") or {}
        known = dict.fromkeys(VIOL_KEYS[:-1], 0)
        for k, n in viol.items():
            if k in known:
                known[k] = n
            else:
                known["other"] = known.get("other", 0) + n
        for j, k in enumerate(VIOL_KEYS):
            v[o + j] = math.log1p(known.get(k, 0))
    o += len(VIOL_KEYS)
    if base and base.get("heatmap"):
        hm = base["heatmap"]
        n = min(len(hm), OBS_SPEC["heatmap"])
        v[o:o + n] = np.log1p(np.asarray(hm[:n], dtype=np.float32))
    return v


def obs_from_record(rec: dict, last_action: dict | None) -> np.ndarray:
    """从 round 记录取动作前观测（iter/基线来自记录本身）。"""
    return obs_vector(rec.get("state_before"), rec.get("pass", ""),
                      rec.get("iter", 0), last_action)


# --------------------------------------------------------------------------
# triples → episodes
# --------------------------------------------------------------------------
@dataclass
class Step:
    obs: np.ndarray
    act: tuple[int, ...]
    rew: float
    next_obs: np.ndarray
    terminal: bool
    timeout: bool
    src: dict          # 溯源：file/line/run_id/pass/iter/board_id


def _is_early_stop(rec: dict) -> bool:
    """双 0 早停判定与 loop.run_pass 同款（score 前两键=完成度+铜级）。"""
    sc = rec.get("score")
    return bool(sc) and sc[0] == 0 and sc[1] == 0 and rec.get("ses_ok")


def build_steps(records: list[tuple[Path, int, dict]],
                w: dict) -> tuple[list[Step], dict]:
    """单 (run_id, pass) 组的 round 序列 → 转移列表。

    records = [(文件, 行号, 记录)]，iter 升序；base 行（iter=-1）与
    planes 单发记录之外均产生转移。s' 取下一轮记录的基线/iter、本轮动作
    作 last_action；末轮的 s' 用 (同基线, iter+1, 本轮动作) 合成。
    """
    q4 = w.get("mode") == "q4"
    # q4：无 outcome 的轮（外生杀停残骸）是噪声标签，不进数据集（§6j）；
    # v1 保持原语义——ses_fail 轮以惩罚分计入 steps（C2 既定行为）
    rounds = [t for t in records if t[2].get("iter", -1) >= 0
              and t[2].get("action") is not None
              and (not q4 or t[2].get("outcome") is not None)]
    # q4：pass 基线行（iter=-1）的 copper = 引入铜口径的减数（q0 同语义）
    base_row = next((t[2] for t in records if t[2].get("iter") == -1), None)
    base_copper = ((base_row or {}).get("outcome") or {}).get("copper") or 0
    steps: list[Step] = []
    for i, (f, ln, rec) in enumerate(rounds):
        last_a = dict(rounds[i - 1][2]["action"]) if i else None
        nxt = rounds[i + 1][2] if i + 1 < len(rounds) else None
        if nxt is not None:
            n_obs = obs_from_record(nxt, rec["action"])
        else:  # 末轮：合成下轮（基线不变、iter+1、last=本轮）
            n_obs = obs_vector(rec.get("state_before"), rec.get("pass", ""),
                               rec.get("iter", 0) + 1, rec["action"])
        terminal = _is_early_stop(rec)
        timeout = (nxt is None) and not terminal      # 阶梯跑尽=截断
        cat = None
        if q4:
            rew, cat = _q4_step_reward(rec, w, base_copper)
        else:
            rew = reward(rec.get("outcome") if rec.get("ses_ok") else None, w)
        src = {"file": str(f), "line": ln, "run_id": rec.get("run_id"),
               "pass": rec.get("pass"), "iter": rec.get("iter"),
               "board_id": rec.get("board_id"),
               "clearance_um": (rec.get("action") or {}).get("clearance_um")}
        if cat is not None:
            src["anchor_cat"] = cat
        steps.append(Step(
            obs=obs_from_record(rec, last_a),
            act=encode_action(rec["action"]),
            rew=rew, next_obs=n_obs, terminal=terminal, timeout=timeout,
            src=src,
        ))
    stats = {"n_rounds": len(rounds), "n_steps": len(steps),
             "n_early_stop": sum(s.terminal for s in steps),
             "n_ses_fail": sum(1 for _, _, r in rounds
                               if r.get("ses_ok") is False)}
    if q4:
        stats["anchor_cats"] = dict(Counter(s.src["anchor_cat"] for s in steps))
    return steps, stats


def load_triples(paths: list[Path]) -> list[tuple[Path, int, dict]]:
    out = []
    for p in paths:
        for ln, line in enumerate(Path(p).read_text().splitlines(), 1):
            if line.strip():
                out.append((Path(p), ln, json.loads(line)))
    return out


def build_dataset(paths: list[Path], weights_mode: str = "flat",
                  baselines: Path | None = None) -> dict:
    """多份 triples → {npz 数组, meta}。caps 先扫一遍全量结果再定 lex 权重。"""
    recs = load_triples(paths)
    rounds = [r for r in recs if r[2].get("iter", -1) >= 0
              and r[2].get("action") is not None
              and r[2].get("ses_ok") and r[2].get("outcome")]
    caps = {k: max((r["outcome"].get(k) or 0) for _, _, r in rounds)
            for k in ("sig", "copper", "un")} if rounds else {}
    if weights_mode == "q4":
        if baselines is None:
            raise ValueError("q4 模式需要 --baselines fr_baselines.json")
        w = q4_weights(baselines)
    else:
        w = reward_weights(weights_mode, caps)
        w = {**w, **{f"cap_{k}": v for k, v in caps.items()}}

    # (run_id, pass) 分组（保持文件顺序）
    groups: dict[tuple, list] = {}
    for t in recs:
        r = t[2]
        if r.get("run_id") and r.get("pass"):
            groups.setdefault((r["run_id"], r["pass"]), []).append(t)

    steps: list[Step] = []
    episodes = []
    for key, g in groups.items():
        g.sort(key=lambda t: t[2].get("iter", -1))
        es, st = build_steps(g, w)
        if not es:
            continue
        bid = next((t[2].get("board_id") for t in g if t[2].get("board_id")), None)
        episodes.append({"run_id": key[0], "pass": key[1],
                         "board_id": bid, **st,
                         "step_range": [len(steps), len(steps) + len(es)]})
        steps.extend(es)

    if not steps:
        raise ValueError("无可用转移（只有 base 行？）")
    meta = {
        "weights": {k: v for k, v in w.items() if k != "baselines"},
        "grids": {
            "clearance_um": CLEARANCE_GRID, "via_ladder_max": VIA_LADDER_MAX,
            "layer_act_max": LAYER_ACT_MAX,
            "edge_inset_um": EDGE_INSET_GRID},
        "slot_cardinalities": list(SLOT_CARD),
        "obs_spec": OBS_SPEC, "obs_dim": OBS_DIM,
        "viol_vocab": list(VIOL_KEYS),
        "caps": caps, "episodes": episodes,
        "sources": [s.src for s in steps],
    }
    if weights_mode == "q4":
        by_board: Counter = Counter()
        total: Counter = Counter()
        for e in episodes:
            bid = e.get("board_id") or "?"
            for cat, n in e.get("anchor_cats", {}).items():
                by_board[f"{bid}:{cat}"] += n
                total[cat] += n
        meta["anchor_cats"] = dict(total)
        meta["anchor_cats_by_board"] = dict(by_board)
    return {
        "observations": np.stack([s.obs for s in steps]),
        "actions": np.asarray([s.act for s in steps], dtype=np.int32),
        "actions_flat": np.asarray([flatten_action(s.act) for s in steps],
                                   dtype=np.int32),
        "rewards": np.asarray([s.rew for s in steps], dtype=np.float32),
        "terminals": np.asarray([s.terminal for s in steps], dtype=bool),
        "timeouts": np.asarray([s.timeout for s in steps], dtype=bool),
        "meta": meta,
    }


def save_dataset(ds: dict, out: Path) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = ds.pop("meta")
    np.savez(out.with_suffix(".npz"), **ds)
    out.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    ds["meta"] = meta          # 幂等：build 结果可重复 save


def to_mdp_dataset(npz_path: Path):
    """npz → d3rlpy MDPDataset（延迟导入；未装则提示 uv sync --group rl 扩展）。"""
    try:
        import d3rlpy
    except ImportError as e:  # pragma: no cover
        raise ImportError("需要 d3rlpy：uv add d3rlpy 或 pip install d3rlpy") from e
    z = np.load(npz_path)
    return d3rlpy.dataset.MDPDataset(
        observations=z["observations"], actions=z["actions"],
        rewards=z["rewards"], terminals=z["terminals"],
        timeouts=z["timeouts"])


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route_rl.mdp",
        description="triples.jsonl → 定长 MDP 数据集（npz + meta.json）")
    ap.add_argument("out", type=Path, help="输出前缀（生成 <out>.npz/.meta.json）")
    ap.add_argument("triples", nargs="+", type=Path, help="triples.jsonl 路径")
    ap.add_argument("--weights", default="flat",
                    choices=("flat", "lex", "norm", "q4"),
                    help="奖励权重模式（默认 flat；训练建议 norm；"
                         "q4=质量分对比 FR 基线，须配 --baselines）")
    ap.add_argument("--baselines", type=Path, default=None,
                    help="q4 模式的 fr_baselines.json（q4_fr_baselines.py 生成）")
    a = ap.parse_args()
    ds = build_dataset(a.triples, a.weights, a.baselines)
    n_ep = len(ds["meta"]["episodes"])
    save_dataset(ds, a.out)
    m = ds["meta"]
    print(f"steps={len(ds['rewards'])} episodes={n_ep} obs_dim={OBS_DIM} "
          f"weights={a.weights} caps={m['caps']}")
    if "anchor_cats" in m:
        print(f"  anchor_cats={m['anchor_cats']}")
    for e in m["episodes"]:
        print(f"  ep {e['run_id']} {e['pass']:6s} rounds={e['n_rounds']} "
              f"early_stop={e['n_early_stop']} ses_fail={e['n_ses_fail']}")


if __name__ == "__main__":
    main()
