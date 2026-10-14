"""PPO 微调——DesignDocument §3.4 阶段二 / M4。

在 BC 预热策略上做掩码动作 + 复合奖励的近端策略优化：位置离散到网格，
用 Pos Mask 保证无重叠；奖励 = −ΔHPWL − λo·新增重叠 − λv·越界（拥塞项 M6
接 FreeRouting 反馈后补）。值头共享 GNN 表示，GAE + clipped surrogate。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.distributions import Categorical

from ..data.synthetic import GenConfig, generate
from ..env.problem import aabb, hpwl, overlap_area
from .bc import _anchor, net_indices, node_features
from .mask import GridPosMask
from .policy import PlacementPolicy


def _device() -> torch.device:
    return torch.device("cpu")


@dataclass
class PPOConfig:
    n_problems: int = 40
    n_min: int = 20
    n_max: int = 60
    grid_mm: float = 1.0          # 位置离散网格
    xy_std: float = 0.08          # 位置高斯（归一化）标准差
    n_iters: int = 200            # PPO 外层迭代
    rollouts_per_iter: int = 16   # 每轮 rollout 板数
    ppo_epochs: int = 4           # 每轮内 epoch
    lr: float = 1e-4
    gamma: float = 0.99
    lam: float = 0.95
    clip: float = 0.2
    ent_coef: float = 0.01
    vf_coef: float = 0.5
    w_overlap: float = 1.0
    w_bounds: float = 1.0
    hidden: int = 128
    gnn_layers: int = 3
    seed: int = 0
    bc_path: str = "policy.pt"
    out: str = "policy_rl.pt"


def _grid_logits(mean, nx, ny, grid_mm, W, H, std, dev):
    gy = torch.arange(ny, device=dev)
    gx = torch.arange(nx, device=dev)
    gy, gx = torch.meshgrid(gy, gx, indexing="ij")
    cx = ((gx.float() + 0.5) * grid_mm) / W
    cy = ((gy.float() + 0.5) * grid_mm) / H
    d2 = (cx - mean[0]) ** 2 + (cy - mean[1]) ** 2
    return (-d2 / (2 * std * std)).reshape(-1)


def _cell_to_xy(cell, nx, grid_mm):
    col = cell % nx
    row = cell // nx
    return (col + 0.5) * grid_mm, (row + 0.5) * grid_mm


def _step_reward(problem, pos, ref, prev_hpwl, cfg):
    """放置 ref 后的增量奖励：−ΔHPWL − λo·新增重叠 − λv·越界。"""
    c = problem.by_ref[ref]
    px, py, rot = pos[ref]
    new_hpwl = hpwl(problem, {r: (p[0], p[1]) for r, p in pos.items()})
    dhpwl = new_hpwl - prev_hpwl
    box = aabb(c, px, py, rot)
    ov = sum(overlap_area(box, aabb(problem.by_ref[r], *pos[r]))
             for r in pos if r != ref)
    x0, y0, x1, y1 = box
    W, H = problem.board
    bnd = max(0.0, -x0) + max(0.0, -y0) + max(0.0, x1 - W) + max(0.0, y1 - H)
    reward = -dhpwl - cfg.w_overlap * ov - cfg.w_bounds * bnd
    return reward, new_hpwl


def _rollout(model, problem, cfg, dev) -> list[dict]:
    model.eval()
    nets = net_indices(problem)
    W, H = problem.board
    pos = {r: (x, y, rot) for r, (x, y, rot) in problem.fixed.items()}
    mask = GridPosMask(W, H, cfg.grid_mm)
    for r, (x, y, rot) in problem.fixed.items():
        x0, y0, x1, y1 = aabb(problem.by_ref[r], x, y, rot)
        mask.mark(x0, y0, x1, y1)
    movable = [c.ref for c in problem.components if c.ref not in problem.fixed]
    trans: list[dict] = []
    prev_hpwl = hpwl(problem, {r: (p[0], p[1]) for r, p in pos.items()})

    with torch.no_grad():
        for step in range(len(movable)):
            x = node_features(problem, pos).to(dev)
            h = model.encode(x, nets)
            placed = torch.tensor([r in pos for r in problem.refs],
                                  dtype=torch.bool, device=dev)
            sel_logits = model.select_logits(h).masked_fill(placed, float("-inf"))
            sel_cat = Categorical(logits=sel_logits)
            sel = int(sel_cat.sample())
            logp_sel = sel_cat.log_prob(torch.tensor(sel, device=dev)).item()
            ref = problem.refs[sel]
            c = problem.by_ref[ref]
            anchor = _anchor(problem, pos, ref).to(dev)
            xy_off, rot_logits, _ = model.place_dist(h, sel, anchor)
            rot_cat = Categorical(logits=rot_logits)
            rot = int(rot_cat.sample())
            logp_rot = rot_cat.log_prob(torch.tensor(rot, device=dev)).item()
            # 朝向先定：90/270 会宽高对调，掩码须用旋转后的外框尺寸
            w_eff, h_eff = (c.w, c.h) if rot % 2 == 0 else (c.h, c.w)
            legal = mask.legal_mask(w_eff, h_eff)
            if not legal.any():
                legal[:] = True  # 兜底：无合法格则退回全板（板将满时）
            mean = anchor + xy_off
            logits = _grid_logits(mean, mask.nx, mask.ny, cfg.grid_mm, W, H,
                                  cfg.xy_std, dev)
            logits = logits.masked_fill(~legal.reshape(-1).to(dev), float("-inf"))
            pos_cat = Categorical(logits=logits)
            cell = int(pos_cat.sample())
            logp_pos = pos_cat.log_prob(torch.tensor(cell, device=dev)).item()

            px, py = _cell_to_xy(cell, mask.nx, cfg.grid_mm)
            rot_deg = rot * 90.0
            pos[ref] = (px, py, rot_deg)
            x0, y0, x1, y1 = aabb(c, px, py, rot_deg)
            mask.mark(x0, y0, x1, y1)

            reward, prev_hpwl = _step_reward(problem, pos, ref, prev_hpwl, cfg)
            value = model.value_head(h).item()
            trans.append(dict(
                x=x.detach().cpu(), nets=nets, sel=sel, w=c.w, h=c.h,
                anchor=anchor.detach().cpu(), legal=legal.reshape(-1).cpu(),
                nx=mask.nx, ny=mask.ny, grid_mm=cfg.grid_mm, W=W, H=H,
                cell=cell, rot=rot,
                logp_sel=logp_sel, logp_pos=logp_pos, logp_rot=logp_rot,
                reward=reward, value=value, done=(step == len(movable) - 1),
            ))
    return trans


def _gae(trans: list[dict], cfg) -> None:
    last = 0.0
    for t in reversed(range(len(trans))):
        next_val = 0.0 if trans[t]["done"] else trans[t + 1]["value"]
        delta = trans[t]["reward"] + cfg.gamma * next_val - trans[t]["value"]
        last = delta + cfg.gamma * cfg.lam * last
        trans[t]["adv"] = last
        trans[t]["ret"] = last + trans[t]["value"]


def _ppo_update(model, opt, trans: list[dict], cfg, dev) -> dict:
    model.train()
    stats = {"policy": 0.0, "value": 0.0, "ent": 0.0, "n": 0}
    for _ in range(cfg.ppo_epochs):
        for t in trans:
            x = t["x"].to(dev)
            h = model.encode(x, t["nets"])
            placed = (x[:, 7] == 1.0).to(dev)
            sel_logits = model.select_logits(h).masked_fill(placed, float("-inf"))
            sel_cat = Categorical(logits=sel_logits)
            logp_sel = sel_cat.log_prob(torch.tensor(t["sel"], device=dev))
            ent_sel = sel_cat.entropy()

            xy_off, rot_logits, _ = model.place_dist(h, t["sel"], t["anchor"].to(dev))
            mean = t["anchor"].to(dev) + xy_off
            logits = _grid_logits(mean, t["nx"], t["ny"], t["grid_mm"], t["W"],
                                  t["H"], cfg.xy_std, dev)
            logits = logits.masked_fill(~t["legal"].to(dev), float("-inf"))
            pos_cat = Categorical(logits=logits)
            logp_pos = pos_cat.log_prob(torch.tensor(t["cell"], device=dev))
            ent_pos = pos_cat.entropy()

            rot_cat = Categorical(logits=rot_logits)
            logp_rot = rot_cat.log_prob(torch.tensor(t["rot"], device=dev))
            ent_rot = rot_cat.entropy()

            old_logp = t["logp_sel"] + t["logp_pos"] + t["logp_rot"]
            new_logp = logp_sel + logp_pos + logp_rot
            ratio = torch.exp(new_logp - old_logp)
            adv = torch.tensor(t["adv"], device=dev)
            surr1 = ratio * adv
            surr2 = torch.clamp(ratio, 1 - cfg.clip, 1 + cfg.clip) * adv
            L_policy = -torch.min(surr1, surr2)

            v = model.value_head(h)
            L_value = F.mse_loss(v, torch.tensor(t["ret"], device=dev))
            ent = ent_sel + ent_pos + ent_rot
            loss = L_policy + cfg.vf_coef * L_value - cfg.ent_coef * ent

            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 0.5)
            opt.step()

            stats["policy"] += L_policy.item()
            stats["value"] += L_value.item()
            stats["ent"] += ent.item()
            stats["n"] += 1
    return {k: (v / stats["n"] if k != "n" else v) for k, v in stats.items()}


def decode_placement(
    model, problem, cfg, dev
) -> tuple[dict[str, tuple[float, float, float]], float, float, float]:
    """贪心解码（M4 掩码版），返回 ``(pos, hpwl, overlap, bounds)``。

    ``pos`` 含固定件与可动件：固定件保原始位姿，可动件取 argmax 合法格 +
    argmax 朝向。真实板放置（M5 place）直接消费这个 ``pos``。
    """
    model.eval()
    nets = net_indices(problem)
    W, H = problem.board
    pos = {r: (x, y, rot) for r, (x, y, rot) in problem.fixed.items()}
    mask = GridPosMask(W, H, cfg.grid_mm)
    for r, (x, y, rot) in problem.fixed.items():
        x0, y0, x1, y1 = aabb(problem.by_ref[r], x, y, rot)
        mask.mark(x0, y0, x1, y1)
    movable = [c.ref for c in problem.components if c.ref not in problem.fixed]
    with torch.no_grad():
        for _ in movable:
            x = node_features(problem, pos).to(dev)
            h = model.encode(x, nets)
            placed = torch.tensor([r in pos for r in problem.refs],
                                  dtype=torch.bool, device=dev)
            sel_logits = model.select_logits(h).masked_fill(placed, float("-inf"))
            sel = int(sel_logits.argmax())
            ref = problem.refs[sel]
            c = problem.by_ref[ref]
            anchor = _anchor(problem, pos, ref).to(dev)
            xy_off, rot_logits, _ = model.place_dist(h, sel, anchor)
            mean = anchor + xy_off
            base = _grid_logits(mean, mask.nx, mask.ny, cfg.grid_mm, W, H,
                                cfg.xy_std, dev)
            # 在四个朝向里挑「合法格内得分最高」的 (rot, cell)：只要板仍有空位，
            # 就绝不触发全板兜底，保证解码零重叠 + 不越界。
            best = None
            for rot in range(4):
                w_eff, h_eff = (c.w, c.h) if rot % 2 == 0 else (c.h, c.w)
                legal = mask.legal_mask(w_eff, h_eff)
                if legal.any():
                    logits = base.masked_fill(~legal.reshape(-1).to(dev),
                                              float("-inf"))
                    score, cell = logits.max(0)
                    if best is None or score.item() > best[0]:
                        best = (score.item(), int(cell), rot)
            if best is None:
                # 四个朝向均无合法格：板已满，退到得分最高的朝向 + 任一格
                rot = int(rot_logits.argmax())
                w_eff, h_eff = (c.w, c.h) if rot % 2 == 0 else (c.h, c.w)
                legal = mask.legal_mask(w_eff, h_eff)
                legal[:] = True
                logits = base.masked_fill(~legal.reshape(-1).to(dev),
                                          float("-inf"))
                cell = int(logits.argmax())
            else:
                _, cell, rot = best
            px, py = _cell_to_xy(cell, mask.nx, cfg.grid_mm)
            rot_deg = rot * 90.0
            pos[ref] = (px, py, rot_deg)
            x0, y0, x1, y1 = aabb(c, px, py, rot)
            mask.mark(x0, y0, x1, y1)
    hp = hpwl(problem, {r: (p[0], p[1]) for r, p in pos.items()})
    boxes = {r: aabb(problem.by_ref[r], *pos[r]) for r in problem.refs}
    ov = sum(overlap_area(boxes[a], boxes[b])
             for i, a in enumerate(problem.refs) for b in problem.refs[i + 1:])
    bnd = sum(max(0.0, -b[0]) + max(0.0, -b[1]) + max(0.0, b[2] - W) + max(0.0, b[3] - H)
             for b in boxes.values())
    return pos, hp, ov, bnd


def _decode(model, problem, cfg, dev) -> tuple[float, float, float]:
    """贪心解码的指标版（M4 评估用），薄包 decode_placement。"""
    _pos, hp, ov, bnd = decode_placement(model, problem, cfg, dev)
    return hp, ov, bnd


def train_rl(cfg: PPOConfig, progress=None) -> dict:
    dev = _device()
    torch.manual_seed(cfg.seed)
    model = PlacementPolicy(hidden=cfg.hidden, gnn_layers=cfg.gnn_layers).to(dev)
    if cfg.bc_path and Path(cfg.bc_path).exists():
        ck = torch.load(cfg.bc_path, map_location=dev, weights_only=False)
        model.load_state_dict(ck["state"], strict=False)  # 值头是新增的
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr)

    hist = {"eval_hpwl": [], "eval_overlap": [], "eval_bounds": [],
            "mean_reward": [], "policy_loss": [], "value_loss": []}
    for it in range(cfg.n_iters):
        all_trans: list[dict] = []
        for k in range(cfg.rollouts_per_iter):
            p = generate(GenConfig(seed=cfg.seed * 10000 + it * 100 + k,
                                   n_min=cfg.n_min, n_max=cfg.n_max))
            trans = _rollout(model, p, cfg, dev)
            _gae(trans, cfg)
            all_trans += trans
        st = _ppo_update(model, opt, all_trans, cfg, dev)
        hist["mean_reward"].append(sum(t["reward"] for t in all_trans) / len(all_trans))
        hist["policy_loss"].append(st["policy"])
        hist["value_loss"].append(st["value"])

        if it % 10 == 9 or it == cfg.n_iters - 1:
            held = generate(GenConfig(seed=cfg.seed + 99991, n_min=cfg.n_min,
                                      n_max=cfg.n_max))
            hp, ov, bnd = _decode(model, held, cfg, dev)
            hist["eval_hpwl"].append(hp)
            hist["eval_overlap"].append(ov)
            hist["eval_bounds"].append(bnd)
            if progress:
                progress(it, hist)

    torch.save({"cfg": cfg, "state": model.state_dict()}, Path(cfg.out))
    return hist
