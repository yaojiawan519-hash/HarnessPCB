"""行为克隆（BC）——DesignDocument §3.4 阶段一 / M3。

用 SA 教师（teacher.sa）在合成板上批量生成参考放置，把「最终放置」按一个
规范顺序（网表度数降序、同位号升序）展开成逐元件轨迹，作为监督信号训练
两级策略（元件选择 + 连续落点 + 朝向）。验收：策略贪心解码产出无重叠、
线长接近 SA 的放置。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import torch
import torch.nn.functional as F

from ..data.synthetic import GenConfig, generate
from ..env.problem import PlacementProblem, aabb, overlap_area, hpwl
from ..teacher.sa import SAConfig, solve
from .policy import PlacementPolicy, ROT_BINS

_CATS = {"passive": 0, "ic": 1, "connector": 2}


def _device() -> torch.device:
    # M3 的图很小（≤ 数百节点），MPS 逐算子启动开销 + 每步标量同步远慢于 CPU；
    # 大板（M4+ 的 RL）再切 mps。
    return torch.device("cpu")


def node_features(
    problem: PlacementProblem, pos: dict[str, tuple[float, float, float]]
) -> torch.Tensor:
    """部分放置 → 节点特征 [N, 12]（尺寸/引脚/度数/类型/是否已放/位姿）。"""
    W, H = problem.board
    max_pins = max((c.pins for c in problem.components), default=1) or 1
    deg = {c.ref: 0.0 for c in problem.components}
    for net in problem.nets:
        for r in net.refs:
            if r in deg:
                deg[r] += net.weight
    max_deg = max(deg.values(), default=1.0) or 1.0
    rows = []
    for c in problem.components:
        placed = c.ref in pos
        x, y, rot = pos.get(c.ref, (0.0, 0.0, 0.0))
        rad = math.radians(rot)
        rows.append([
            c.w / W, c.h / H,
            math.log1p(c.pins) / math.log1p(max_pins),
            deg[c.ref] / max_deg,
            *[1.0 if _CATS.get(c.category, 0) == i else 0.0 for i in range(3)],
            1.0 if placed else 0.0,
            x / W, y / H,
            math.cos(rad), math.sin(rad),
        ])
    return torch.tensor(rows, dtype=torch.float32)


def net_indices(problem: PlacementProblem) -> list[list[int]]:
    idx = {c.ref: i for i, c in enumerate(problem.components)}
    return [[idx[r] for r in net.refs if r in idx] for net in problem.nets]


def _anchor(
    problem: PlacementProblem, pos: dict[str, tuple[float, float, float]], ref: str
) -> torch.Tensor:
    """已放置邻居质心（归一化）；无已放邻居则板中心。"""
    neighbors: set[str] = set()
    for net in problem.nets:
        if ref in net.refs:
            neighbors.update(net.refs)
    neighbors.discard(ref)
    W, H = problem.board
    pts = [pos[r] for r in neighbors if r in pos]
    if pts:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return torch.tensor([cx / W, cy / H], dtype=torch.float32)
    return torch.tensor([0.5, 0.5], dtype=torch.float32)


def canonical_order(problem: PlacementProblem) -> list[str]:
    """放置顺序：加权网表度数降序、同位号升序（SA 最终放置的重放次序）。

    度数按 net.weight 加权：关键网（如时钟，weight>1）连接的器件度数更高、
    更早放置，使其先占位、随后邻质心吸引同网邻居聚拢。
    """
    deg = {c.ref: 0.0 for c in problem.components}
    for net in problem.nets:
        for r in net.refs:
            if r in deg:
                deg[r] += net.weight
    return [c.ref for c in sorted(problem.components, key=lambda c: (-deg[c.ref], c.ref))]


def sa_to_steps(problem: PlacementProblem, solution: dict) -> list[dict]:
    """SA 最终解 → 逐元件 (state, action) 监督样本。

    固定件始终在局内；动作只覆盖可动件。每步给出「部分放置 pos」与
    该步要放的 ref 及其 SA 落点/朝向。
    """
    sol_xy = {r: (p.x, p.y, p.rot) for r, p in solution.items()}
    pos: dict[str, tuple[float, float, float]] = {
        r: (x, y, rot) for r, (x, y, rot) in problem.fixed.items()
    }
    steps = []
    for ref in canonical_order(problem):
        if ref in problem.fixed:
            continue
        x, y, rot = sol_xy[ref]
        steps.append({"pos": dict(pos), "ref": ref, "xy": (x, y), "rot": rot})
        pos[ref] = (x, y, rot)
    return steps


@dataclass
class BCConfig:
    n_problems: int = 40
    n_min: int = 20
    n_max: int = 60
    iters: int = 10000          # 每块板 SA 迭代（数据生成用，略低于评估）
    eval_iters: int = 20000     # 留出板 SA 基线迭代（强参考）
    epochs: int = 30
    lr: float = 3e-4
    hidden: int = 128
    gnn_layers: int = 3
    w_xy: float = 20.0          # 落点 MSE 相对选择/朝向 CE 的权重
    seed: int = 0
    out: str = "policy.pt"
    device: str = "auto"


def _collect(cfg: BCConfig) -> tuple[list[tuple], list[PlacementProblem]]:
    """生成合成板 → SA 解 → 轨迹，返回 (样本列表, 问题列表)。"""
    problems: list[PlacementProblem] = []
    samples: list[tuple] = []
    for k in range(cfg.n_problems):
        p = generate(GenConfig(seed=cfg.seed * 1000 + k,
                               n_min=cfg.n_min, n_max=cfg.n_max))
        res = solve(p, SAConfig(seed=cfg.seed * 1000 + k, iters=cfg.iters))
        idx = {c.ref: i for i, c in enumerate(p.components)}
        nets = net_indices(p)
        W, H = p.board
        for st in sa_to_steps(p, res.solution):
            x = node_features(p, st["pos"])
            sel = idx[st["ref"]]
            anchor = _anchor(p, st["pos"], st["ref"])
            xy_t = torch.tensor([st["xy"][0] / W, st["xy"][1] / H],
                                dtype=torch.float32) - anchor
            rot_t = int(round(st["rot"] / 90.0)) % ROT_BINS
            placed_mask = torch.zeros(len(p.components), dtype=torch.bool)
            for r in st["pos"]:
                placed_mask[idx[r]] = True
            samples.append((x, nets, sel, xy_t, rot_t, placed_mask, anchor))
        problems.append(p)
    return samples, problems


def _baseline(cfg: BCConfig) -> tuple[PlacementProblem, "SAResult"]:
    """留出板 + 完整 SA 基线（训练未见、种子错开）。"""
    p = generate(GenConfig(seed=cfg.seed + 99991, n_min=cfg.n_min, n_max=cfg.n_max))
    res = solve(p, SAConfig(seed=cfg.seed + 99991, iters=cfg.eval_iters))
    return p, res


def train(cfg: BCConfig, progress=None) -> dict:
    """收集 SA 轨迹并训练策略，返回训练指标。``progress`` 可选回调。"""
    rng = random.Random(cfg.seed)
    torch.manual_seed(cfg.seed)
    dev = _device() if cfg.device == "auto" else torch.device(cfg.device)
    samples, _problems = _collect(cfg)
    rng.shuffle(samples)

    held, base = _baseline(cfg)

    model = PlacementPolicy(hidden=cfg.hidden, gnn_layers=cfg.gnn_layers).to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr)

    hist = {"select": [], "xy": [], "rot": [], "eval_hpwl": [], "eval_overlap": [],
            "sa_hpwl": base.hpwl, "sa_overlap": base.overlap}
    for epoch in range(cfg.epochs):
        model.train()
        sl = xl = rl = 0.0
        n = 0
        for x, nets, sel, xy_t, rot_t, placed, anchor in samples:
            x = x.to(dev); xy_t = xy_t.to(dev)
            rot_t = torch.tensor(rot_t, dtype=torch.long, device=dev)
            h = model.encode(x, nets)
            logits = model.select_logits(h)
            logits = logits.masked_fill(placed.to(dev), float("-inf"))
            xy_m, rot_logits, _ = model.place_dist(h, sel, anchor.to(dev))
            ls = F.cross_entropy(logits, torch.tensor(sel, device=dev))
            lx = F.mse_loss(xy_m, xy_t)
            lr = F.cross_entropy(rot_logits, rot_t)
            loss = ls + cfg.w_xy * lx + lr
            opt.zero_grad()
            loss.backward()
            opt.step()
            sl += ls.item(); xl += lx.item(); rl += lr.item(); n += 1
        hist["select"].append(sl / n)
        hist["xy"].append(xl / n)
        hist["rot"].append(rl / n)

        # 每隔几轮在留出板上贪心解码评估
        if epoch % 5 == 4 or epoch == cfg.epochs - 1:
            m, ov = _eval_decode(model, held, dev)
            hist["eval_hpwl"].append(m)
            hist["eval_overlap"].append(ov)
            if progress:
                progress(epoch, hist)

    out = Path(cfg.out)
    torch.save({"cfg": cfg, "state": model.state_dict()}, out)
    return hist


def _eval_decode(model, problem: PlacementProblem, dev) -> tuple[float, float]:
    """贪心解码：逐件选 argmax、落点取均值、朝向取 argmax，回代 HPWL/重叠。"""
    model.eval()
    idx = {c.ref: i for i, c in enumerate(problem.components)}
    nets = net_indices(problem)
    W, H = problem.board
    pos = {r: (x, y, rot) for r, (x, y, rot) in problem.fixed.items()}
    movable = [c.ref for c in problem.components if c.ref not in problem.fixed]
    with torch.no_grad():
        for _ in movable:
            x = node_features(problem, pos).to(dev)
            h = model.encode(x, nets)
            logits = model.select_logits(h).clone()
            for r, i in idx.items():
                if r in pos:
                    logits[i] = float("-inf")
            sel = int(logits.argmax())
            ref = problem.components[sel].ref
            anchor = _anchor(problem, pos, ref).to(dev)
            xy_off, rot_logits, _ = model.place_dist(h, sel, anchor)
            px = (float(xy_off[0]) + float(anchor[0])) * W
            py = (float(xy_off[1]) + float(anchor[1])) * H
            rot = int(rot_logits.argmax()) * 90.0
            pos[ref] = (px, py, rot)
    hp = hpwl(problem, pos)
    boxes = {r: aabb(problem.by_ref[r], *pos[r]) for r in problem.refs}
    ov = sum(
        overlap_area(boxes[a], boxes[b])
        for i, a in enumerate(problem.refs)
        for b in problem.refs[i + 1:]
    )
    return hp, ov
