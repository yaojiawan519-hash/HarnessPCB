"""模拟退火（SA）教师——DesignDocument §3.1 / M2。

在自有板上生成参考放置：目标 = HPWL + 重叠惩罚 + 越界惩罚，Metropolis 退火，
位置落在 1.27mm 栅格上、朝向取 0/90/180/270，故输出可直接作行为克隆（M3）目标。

增量评估：每次候选 move 只重算受影响 net 的 HPWL 与受影响器件的重叠，
复杂度 O(受影响度数 + N)，支持 200 器件 / 万级迭代。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..env.problem import PlacementProblem, aabb, overlap_area
from .. import reuse

ROTATIONS = (0.0, 90.0, 180.0, 270.0)


@dataclass
class SAConfig:
    iters: int = 20000
    T0: float = 100.0
    T1: float = 0.05
    grid: float = 1.27
    lam_overlap: float = 20.0   # 每 mm² 重叠惩罚
    lam_bounds: float = 50.0    # 每 mm 越界惩罚
    seed: int = 0
    move_p: float = 0.7         # 移动一器件
    swap_p: float = 0.2         # 交换两器件
    # 剩余概率 = 旋转一器件


@dataclass
class SAResult:
    solution: dict[str, "reuse.Placement"]
    hpwl: float
    overlap: float
    bounds: float
    energy: float
    accepted: int
    iters: int = field(default=0)


def solve(problem: PlacementProblem, cfg: SAConfig) -> SAResult:
    rng = random.Random(cfg.seed)
    W, H = problem.board
    g = cfg.grid
    refs = problem.refs
    fixed = dict(problem.fixed)
    movable = [r for r in refs if r not in fixed]

    # ref → 所属 net 下标
    ref2nets: dict[str, list[int]] = {r: [] for r in refs}
    for i, net in enumerate(problem.nets):
        for r in net.refs:
            if r in ref2nets:
                ref2nets[r].append(i)

    def snap(v: float) -> float:
        return round(v / g) * g

    def random_xy() -> tuple[float, float]:
        return (snap(rng.uniform(0.0, W)), snap(rng.uniform(0.0, H)))

    def net_hpwl(net_idx: int, pos: dict) -> float:
        net = problem.nets[net_idx]
        xs = [pos[r][0] for r in net.refs]
        ys = [pos[r][1] for r in net.refs]
        return net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))

    def out(box) -> float:
        return max(0.0, -box[0]) + max(0.0, -box[1]) + max(0.0, box[2] - W) + max(0.0, box[3] - H)

    def full(pos: dict) -> tuple[float, float, float, float]:
        boxes = {r: aabb(problem.by_ref[r], *pos[r]) for r in refs}
        hp = sum(net_hpwl(i, pos) for i in range(len(problem.nets)))
        ov = 0.0
        for a in range(len(refs)):
            for b in range(a + 1, len(refs)):
                ov += overlap_area(boxes[refs[a]], boxes[refs[b]])
        bd = sum(out(boxes[r]) for r in refs)
        return hp, ov, bd

    # 初始化：随机 on-grid 位置，朝向 0
    pos: dict[str, tuple[float, float, float]] = {}
    for r in refs:
        if r in fixed:
            pos[r] = (fixed[r][0], fixed[r][1], fixed[r][2])
        else:
            pos[r] = (*random_xy(), 0.0)
    boxes = {r: aabb(problem.by_ref[r], *pos[r]) for r in refs}

    hp, ov, bd = full(pos)
    energy = hp + cfg.lam_overlap * ov + cfg.lam_bounds * bd
    best_energy = energy
    best_pos = dict(pos)
    accepted = 0

    for it in range(cfg.iters):
        T = cfg.T0 * (cfg.T1 / cfg.T0) ** (it / max(1, cfg.iters))
        u = rng.random()

        # 选 move，产生 affected 集合与候选新位姿
        if u < cfg.move_p:
            r = rng.choice(movable)
            new = {r: (*random_xy(), rng.choice(ROTATIONS))}
        elif u < cfg.move_p + cfg.swap_p:
            r1, r2 = rng.sample(movable, 2)
            new = {r1: pos[r2], r2: pos[r1]}
        else:
            r = rng.choice(movable)
            new = {r: (pos[r][0], pos[r][1], rng.choice(ROTATIONS))}

        affected = list(new)
        # 候选完整位姿
        trial = dict(pos)
        trial.update(new)

        dhp = 0.0
        nets_touched: set[int] = set()
        for r in affected:
            nets_touched.update(ref2nets[r])
        for i in nets_touched:
            dhp += net_hpwl(i, trial) - net_hpwl(i, pos)

        dov = 0.0
        for r in affected:
            nb = aabb(problem.by_ref[r], *new[r])
            ob = boxes[r]
            for s in refs:
                if s in affected:
                    continue
                dov += overlap_area(nb, boxes[s]) - overlap_area(ob, boxes[s])
        for ai in range(len(affected)):
            for bi in range(ai + 1, len(affected)):
                r, s = affected[ai], affected[bi]
                dov += overlap_area(aabb(problem.by_ref[r], *new[r]), aabb(problem.by_ref[s], *new[s])) \
                     - overlap_area(boxes[r], boxes[s])

        dbd = 0.0
        for r in affected:
            dbd += out(aabb(problem.by_ref[r], *new[r])) - out(boxes[r])

        dE = dhp + cfg.lam_overlap * dov + cfg.lam_bounds * dbd
        if dE <= 0.0 or rng.random() < math.exp(-dE / T):
            for r in affected:
                pos[r] = new[r]
                boxes[r] = aabb(problem.by_ref[r], *new[r])
            energy += dE
            accepted += 1
            if energy < best_energy:
                best_energy = energy
                best_pos = dict(pos)

    hp_b, ov_b, bd_b = full(best_pos)
    solution = {
        r: reuse.Placement(r, *best_pos[r], "F", "soft") for r in refs
    }
    return SAResult(
        solution=solution,
        hpwl=hp_b,
        overlap=ov_b,
        bounds=bd_b,
        energy=hp_b + cfg.lam_overlap * ov_b + cfg.lam_bounds * bd_b,
        accepted=accepted,
        iters=cfg.iters,
    )
