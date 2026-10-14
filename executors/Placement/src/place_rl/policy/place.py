"""真实板放置流水线——DesignDocument M5 落地（decode → legalize → 写回 KiCad）。

把一条完整链路串起来：

    .kicad_sch + .kicad_pcb → DesignIR + 板框 bbox → PlacementProblem（本地系）
    → 策略贪心解码（掩码，零重叠）→ 力导向 + 消重叠合法化
    → 坐标本地系→绝对系 → write_placements 写回 placed-unrouted 板。

固定件（连接器 / 安装孔 / 任何不在 IR 里的 footprint）锁定在原位，策略只放
剩余可动件。位姿以板框左下为原点（本地系），写回时用 ``size=(W,H)`` 把板框
重写成 (0,0)-(W,H) 矩形，放置坐标即为本地系、无需再平移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import torch

from .. import reuse
from ..data.real import board_only_components, build_board_nets, build_net_pin_loc, problem_from_ir
from ..env.problem import (PlacementProblem, PowerCluster, aabb, channel_members,
                           origin_of, power_cluster_passive_refs, power_ic_refs)
from .legalize import LegalizeConfig, refine, metrics
from .intent import edge_poses, load_intent, power_cluster_region
from .rl import PPOConfig, decode_placement
from .policy import PlacementPolicy

__all__ = ["PlaceConfig", "load_model", "place_real_board", "default_fixed"]


@dataclass
class PlaceConfig:
    grid_mm: float = 1.27          # 解码位置网格
    xy_std: float = 0.08
    hidden: int = 128
    gnn_layers: int = 3
    legalize_iters: int = 200
    lam_net: float = 0.1
    seed: int = 0
    led_edge: bool = False         # 原则②总开关（默认关 = 与现行引擎逐字节一致）
    iface_edge: bool = False       # 原则①接口贴边总开关（默认关；仅未锁定连接器生效）
    lam_edge_ic: float = 0.0       # 原则①支撑 IC 贴边系数（默认 0=关）
    lam_ic_spread: float = 0.0     # 原则⑥主芯片均匀系数（默认 0=关）
    lam_decoup_dedicated: float = 0.0  # 专属轨去耦电容（轨上唯一 IC）单独系数
                                   # （默认 0=沿用 LegalizeConfig.lam_decoup）
    lam_decoup: float | None = None    # 共享轨去耦电容系数覆盖（None=沿用
                                   # LegalizeConfig 默认 0.15，API 行为不变）
    fix_connectors: bool = True    # J* 连接器锁定
    fix_holes: bool = True         # 安装孔锁定（H*/M*）
    extra_fixed: list[str] = field(default_factory=list)
    intent_file: str | None = None  # 意图 spec JSON（边沿/朝外/凸出；默认 None=关）


def load_model(path: str | Path, hidden: int = 128, gnn_layers: int = 3) -> PlacementPolicy:
    """从 BC/RL checkpoint 载入策略（含 value 头，RL 输出兼容 BC）。"""
    dev = torch.device("cpu")
    model = PlacementPolicy(hidden=hidden, gnn_layers=gnn_layers).to(dev)
    ck = torch.load(Path(path), map_location=dev, weights_only=False)
    model.load_state_dict(ck["state"], strict=False)  # 值头/新增层容忍
    model.eval()
    return model


def default_fixed(
    ir: reuse.DesignIR,
    board: reuse.Board,
    cfg: PlaceConfig,
) -> set[str]:
    """默认锁定件：连接器、安装孔、以及板上存在但 IR 里没有的 footprint。"""
    ir_refs = {d.ref for d in ir.devices.values()}
    by_ref = board.by_ref()
    fixed: set[str] = set()
    for ref in by_ref:
        if ref in cfg.extra_fixed:
            fixed.add(ref)
            continue
        if cfg.fix_holes and (ref.startswith("H") or ref.startswith("M")):
            fixed.add(ref)
            continue
        if cfg.fix_connectors and ref.startswith("J"):
            fixed.add(ref)
            continue
        if ref not in ir_refs:
            # 板上独有（安装孔、测试点、丝印占位等）：锁定原位，不参与放置
            fixed.add(ref)
    return fixed


def place_real_board(
    sch: str | Path,
    pcb: str | Path,
    out: str | Path,
    model_path: str | Path,
    cfg: PlaceConfig | None = None,
    fixed: set[str] | None = None,
) -> dict:
    """读真实板 → 策略放置 → 合法化 → 写回 placed-unrouted 板。

    返回诊断 dict（器件数 / 可动件 / HPWL 前后 / 重叠 / 越界 / 写回数）。
    ``fixed`` 显式给定时覆盖默认锁定集；坐标以板框左下为原点。
    """
    cfg = cfg or PlaceConfig()
    sch, pcb, out = Path(sch), Path(pcb), Path(out)

    ir = reuse.load_design_ir(sch)
    board = reuse.Board(pcb)
    by_ref = board.by_ref()

    xmin, ymin, xmax, ymax = reuse.board_outline_bbox(pcb)
    W, H = xmax - xmin, ymax - ymin

    fixed = fixed if fixed is not None else default_fixed(ir, board, cfg)
    # 板上每器件的本地系锁定位姿（固定件）：footprint 原点 + 朝向
    fixed_local = {
        r: (f.x - xmin, f.y - ymin, f.rot) for r, f in by_ref.items() if r in fixed
    }

    # 板上封装为准覆盖原理图 Footprint 字段（迁移导入/板级换封装时二者不一致，
    # 用原理图字段会拿到偏小的几何，误导重叠判定）。by_ref 的 Footprint.name 即
    # "lib:name" 封装 id。
    fp_override = {ref: f.name for ref, f in by_ref.items() if ":" in f.name}

    libs = reuse.FootprintLibs(pcb.parent)
    ir_refs = {d.ref for d in ir.devices.values()}
    # 板上独有、IR 没有的 footprint（安装孔 MH* / 板级器件 SW*、U6-U9 等）作为
    # 固定障碍加入碰撞模型，否则可动件会被放到它们原位所在处、产生真实 DRC 短路。
    extra_comps, extra_fixed = board_only_components(by_ref, ir_refs, libs, xmin, ymin)

    # 意图 spec（--intent-file）：接口件「哪条边 / 开口朝外 / 凸出 1mm」= 显式
    # 设计输入。位姿（原点系，与 fixed_local 同约定）并入锁定集；board-only
    # 意图件（如按键）同时从 extra_fixed 挪走，避免原位回写覆盖意图位姿。
    # 其余器件不受影响，仍走 sch 驱动独立放置。
    intent_report: list[dict] = []
    spec = load_intent(cfg.intent_file) if cfg.intent_file else None
    if spec is not None:
        obstacles = []
        for c in extra_comps:  # 固定障碍盒（本地系 AABB）：安装孔等参与沿边避让
            if c.ref in spec.items:
                continue  # 意图件自身不是障碍（否则撞自己模板鬼影，推开永不收敛）
            fx, fy, frot = extra_fixed[c.ref]
            hw, hh = (c.w / 2, c.h / 2) if frot % 180 == 0 else (c.h / 2, c.w / 2)
            obstacles.append((c.ref, (fx - hw, fy - hh, fx + hw, fy + hh)))
        iposes, intent_report = edge_poses(
            spec, by_ref, libs, W, H, obstacles, origin=(xmin, ymin))
        fixed |= set(iposes)
        fixed_local.update(iposes)
        for r in iposes:
            extra_fixed.pop(r, None)

    # 引脚坐标（net → ref → 本地 pin 位）：时钟网据此锚到具体引脚而非器件体心。
    net_pin_loc = build_net_pin_loc(by_ref, libs)
    # 板级权威网表（net → refs）：原理图注解与板已漂移时，以 pad_nets 为准。
    board_nets = build_board_nets(by_ref)

    problem = problem_from_ir(ir, libs, board=(W, H),
                              fixed=fixed_local, footprints=fp_override,
                              extra_components=extra_comps, extra_fixed=extra_fixed,
                              net_pin_loc=net_pin_loc, board_nets=board_nets)
    problem.led_edge = cfg.led_edge  # 原则②：锚定框架层的 LED 贴边开关
    problem.iface_edge = cfg.iface_edge  # 原则①：锚定框架层的接口贴边开关
    # 电源簇（意图「电源元器件放置在电源输入端子的下方」）：锚端子位姿（意图件
    # 时 fixed_local 已是意图位姿）→ 簇带矩形；成员 = 电源变换 IC（强锚，锚点
    # 从端子中心升级为带投影）+ 非 C 纯电源被动件（共轨）；去耦电容不进簇、由
    # power_decoup_anchor 的 pc 分支改投簇带投影（归属判据不变）。带建不出来
    # （锚不在板上/几何缺失/钳回板内后退化）→ 如实上报，簇不建立。
    if spec is not None and spec.power_cluster is not None:
        pspec = spec.power_cluster
        af = by_ref.get(pspec.anchor)
        if af is None:
            intent_report.append({"ref": pspec.anchor,
                                  "error": "电源簇锚端子板上无此位号"})
        else:
            anchor_pose = fixed_local.get(pspec.anchor,
                                          (af.x - xmin, af.y - ymin, af.rot))
            region = power_cluster_region(pspec, spec, by_ref, libs, W, H,
                                          anchor_pose)
            if region is None:
                intent_report.append({"ref": pspec.anchor,
                                      "error": "电源簇带为空（锚几何缺失或钳回板内后退化）"})
            else:
                ic_refs = frozenset(power_ic_refs(problem))
                extra_refs = frozenset(
                    power_cluster_passive_refs(problem) - set(channel_members(problem)))
                problem.power_cluster = PowerCluster(
                    region=region, ic_refs=ic_refs, extra_refs=extra_refs,
                    anchor=pspec.anchor)
                intent_report.append({
                    "power_cluster": {
                        "anchor": pspec.anchor,
                        "region": [round(v, 2) for v in region],
                        "n_ic": len(ic_refs), "ic_refs": sorted(ic_refs),
                        "n_extra": len(extra_refs),
                        "extra_refs": sorted(extra_refs),
                    },
                })
    movable = [c.ref for c in problem.components if c.ref not in problem.fixed]

    model = load_model(model_path, hidden=cfg.hidden, gnn_layers=cfg.gnn_layers)
    pcfg = PPOConfig(grid_mm=cfg.grid_mm, xy_std=cfg.xy_std)
    pos, hp_before, ov_before, bd_before = decode_placement(
        model, problem, pcfg, torch.device("cpu"))

    # 合法化（固定件不动）。整个 LegalizeConfig 显式构造透传：新系数
    # （lam_edge_ic / lam_ic_spread / lam_decoup_dedicated，默认 0=关）随 cfg
    # 进来，行为与旧窄口（只传 iters/lam_net/seed、其余走模块默认）等价——
    # 各字段默认值相同。
    lc = LegalizeConfig(
        iters=cfg.legalize_iters, lam_net=cfg.lam_net, seed=cfg.seed,
        lam_edge_ic=cfg.lam_edge_ic, lam_ic_spread=cfg.lam_ic_spread,
        lam_decoup_dedicated=cfg.lam_decoup_dedicated)
    if cfg.lam_decoup is not None:  # None=不动，沿用 LegalizeConfig 默认（0.15）
        lc.lam_decoup = cfg.lam_decoup
    pos = refine(problem, pos, lc)

    m = metrics(problem, pos)

    # 中心化位姿（本地系）→ footprint 原点（本地系），构造 Placement 写回。
    # ``write_placements(size=(W,H))`` 会把板框重写成 (0,0)-(W,H)，故坐标保持
    # 本地系（0..W / 0..H），无需再平移。
    placements: dict[str, reuse.Placement] = {}
    for r, f in by_ref.items():
        if r in pos:
            c = problem.by_ref[r]
            ox, oy = origin_of(c, pos[r][0], pos[r][1], pos[r][2])
            side = "B" if f.layer == "B.Cu" else "F"
            placements[r] = reuse.Placement(r, ox, oy, pos[r][2], side,
                                            "hard" if r in fixed else "soft")
        else:
            # 无几何 / 未放置：锁定原位（本地系）不动
            placements[r] = reuse.Placement(r, f.x - xmin, f.y - ymin, f.rot,
                                            "B" if f.layer == "B.Cu" else "F", "hard")

    written, stripped = reuse.write_placements(pcb, out, placements, size=(W, H))

    return {
        "board": (W, H),
        "n_total": len(by_ref),
        "n_movable": len(movable),
        "n_fixed": len(fixed_local),
        "hpwl_decode": hp_before,
        "overlap_decode": ov_before,
        "bounds_decode": bd_before,
        "hpwl_final": m["hpwl"],
        "overlap_final": m["overlap"],
        "bounds_final": m["bounds"],
        "intent": intent_report,
        "written": written,
        "stripped": stripped,
        "out": str(out),
    }
