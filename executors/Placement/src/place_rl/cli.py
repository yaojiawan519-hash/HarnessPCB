"""plr CLI：place-rl 元器件放置工具入口。

M0/M1 实装 ``inspect``（读板 → IR 统计 + HPWL / 重叠 / 拥塞）；
M2 实装 ``gen``（合成板）+ ``teach``（SA 教师，合成 / 真实板）。
后续 bc/train/legalize/eval 按里程碑 M3–M6 补。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import reuse
from .metrics.congestion import rudy
from .metrics.hpwl import hpwl


def _fmt(n: float) -> str:
    return f"{n:,.2f}"


def cmd_inspect(args) -> int:
    sch = Path(args.sch).expanduser().resolve()
    ir = reuse.load_design_ir(sch)
    print(f"root_sch : {ir.root_sch}")
    print(f"stats    : {ir.stats()}")
    for w in ir.warnings[:8]:
        print(f"  ⚠ {w}")
    if not args.pcb:
        return 0

    pcb = Path(args.pcb).expanduser().resolve()
    board = reuse.Board(pcb)
    by_ref = board.by_ref()
    pos = {f.reference: (f.x, f.y) for f in board.footprints if f.reference}

    print(f"\n板上实例 : {len(board.footprints)}（位号 {len(pos)}）")
    if pos:
        print(f"HPWL（中心近似，{len(pos)} 个已放件）: {_fmt(hpwl(ir, pos))} mm")

        xs = [p[0] for p in pos.values()]
        ys = [p[1] for p in pos.values()]
        canvas = (min(xs), min(ys), max(xs), max(ys))
        print(f"Rudy 拥塞 Ĉ（归一化）: {rudy(ir, pos, canvas):.4f}")

    # 重叠：IR 器件 × 库几何 × 板上位姿
    libs = reuse.FootprintLibs(pcb.parent)
    geoms, problems = reuse.load_geometry(ir, libs)
    plc = {
        r: reuse.Placement(r, f.x, f.y, f.rot, "F" if f.layer != "B.Cu" else "B")
        for r, f in by_ref.items()
        if r in geoms
    }
    overlaps = reuse.find_overlaps(plc, geoms)
    print(f"\nfootprint 解析失败 : {len(problems)}")
    for p in problems[:8]:
        print(f"  ✗ {p}")
    print(f"重叠对             : {len(overlaps)}")
    for a, b, area in overlaps[:10]:
        print(f"  {a} × {b} = {_fmt(area)} mm²")
    return 0


def cmd_gen(args) -> int:
    from .data.synthetic import GenConfig, generate

    p = generate(GenConfig(seed=args.seed, n_min=args.n, n_max=args.n,
                           fill=args.fill, fix_connectors=args.fix_connectors))
    print(f"components : {p.n}")
    print(f"nets       : {len(p.nets)}")
    print(f"board      : {p.board[0]:.1f} × {p.board[1]:.1f} mm")
    print(f"fixed      : {len(p.fixed)}")
    cats = {}
    for c in p.components:
        cats[c.category] = cats.get(c.category, 0) + 1
    print(f"categories : {cats}")
    return 0


def _problem_from_args(args) -> "tuple[object, object]":
    from .data.synthetic import GenConfig, generate
    from .env.problem import PlacementProblem

    if args.syn:
        return (generate(GenConfig(seed=args.seed, n_min=args.n, n_max=args.n,
                                   fill=args.fill, fix_connectors=args.fix_connectors)),
                None)
    # 真实板：IR → PlacementProblem，画布取当前放置包围盒 + 边距
    from .data.real import problem_from_ir

    sch = Path(args.sch).expanduser().resolve()
    pcb = Path(args.pcb).expanduser().resolve()
    ir = reuse.load_design_ir(sch)
    board = reuse.Board(pcb)
    pos_all = {f.reference: (f.x, f.y) for f in board.footprints if f.reference}
    if not pos_all:
        raise SystemExit("板无已放件，无法推导画布")
    xs = [p[0] for p in pos_all.values()]
    ys = [p[1] for p in pos_all.values()]
    margin = args.margin
    W = max(xs) - min(xs) + 2 * margin
    H = max(ys) - min(ys) + 2 * margin
    libs = reuse.FootprintLibs(pcb.parent)
    problem = problem_from_ir(ir, libs, board=(W, H))
    return problem, pos_all


def cmd_teach(args) -> int:
    from .teacher.sa import SAConfig, solve

    problem, _orig = _problem_from_args(args)
    cfg = SAConfig(seed=args.seed, iters=args.iters)
    print(f"problem    : {problem.n} 器件 / {len(problem.nets)} net / "
          f"板 {problem.board[0]:.0f}×{problem.board[1]:.0f} mm")
    res = solve(problem, cfg)
    print(f"SA 教师    : {res.iters} 迭代 / 接受 {res.accepted}")
    print(f"HPWL       : {_fmt(res.hpwl)} mm")
    print(f"重叠       : {_fmt(res.overlap)} mm²")
    print(f"越界       : {_fmt(res.bounds)} mm")
    return 0


def cmd_bc(args) -> int:
    from .policy.bc import BCConfig, train

    cfg = BCConfig(
        n_problems=args.n_problems, n_min=args.n_min, n_max=args.n_max,
        iters=args.iters, epochs=args.epochs, lr=args.lr,
        hidden=args.hidden, gnn_layers=args.layers,
        seed=args.seed, out=args.out, device=args.device,
    )

    def progress(epoch, hist):
        hp = hist["eval_hpwl"][-1]
        ov = hist["eval_overlap"][-1]
        print(f"epoch {epoch + 1:>3}/{cfg.epochs}  "
              f"sel={hist['select'][-1]:.3f} xy={hist['xy'][-1]:.5f} "
              f"rot={hist['rot'][-1]:.3f}  解码 HPWL={hp:,.1f} 重叠={ov:.3f}")

    hist = train(cfg, progress=progress)
    sa_hp, sa_ov = hist["sa_hpwl"], hist["sa_overlap"]
    hp, ov = hist["eval_hpwl"][-1], hist["eval_overlap"][-1]
    print(f"\ncheckpoint : {cfg.out}")
    print(f"末轮损失   : sel={hist['select'][-1]:.3f} xy={hist['xy'][-1]:.5f} "
          f"rot={hist['rot'][-1]:.3f}")
    print(f"留出板基线 (SA {cfg.eval_iters} iter) : HPWL={sa_hp:,.1f} mm  "
          f"重叠={sa_ov:.3f} mm²")
    print(f"策略解码   : HPWL={hp:,.1f} mm（SA 的 {hp / max(sa_hp, 1e-9):.2f}×）  "
          f"重叠={ov:.3f} mm²")
    return 0


def cmd_rl(args) -> int:
    from .policy.rl import PPOConfig, train_rl

    cfg = PPOConfig(
        n_min=args.n_min, n_max=args.n_max, grid_mm=args.grid_mm,
        xy_std=args.xy_std, n_iters=args.iters,
        rollouts_per_iter=args.rollouts, ppo_epochs=args.ppo_epochs,
        lr=args.lr, ent_coef=args.ent_coef, vf_coef=args.vf_coef,
        w_overlap=args.w_overlap, w_bounds=args.w_bounds,
        hidden=args.hidden, gnn_layers=args.layers,
        seed=args.seed, bc_path=args.bc_path, out=args.out,
    )

    def progress(it, hist):
        hp = hist["eval_hpwl"][-1]
        ov = hist["eval_overlap"][-1]
        bd = hist["eval_bounds"][-1]
        mr = hist["mean_reward"][-1]
        print(f"iter {it + 1:>4}/{cfg.n_iters}  reward={mr:+.3f}  "
              f"解码 HPWL={hp:,.1f} 重叠={ov:.3f} 越界={bd:.3f}")

    hist = train_rl(cfg, progress=progress)
    print(f"\ncheckpoint : {cfg.out}")
    print(f"首/末 mean_reward : {hist['mean_reward'][0]:+.3f} → "
          f"{hist['mean_reward'][-1]:+.3f}")
    print(f"首/末 解码 HPWL   : {hist['eval_hpwl'][0]:,.1f} → "
          f"{hist['eval_hpwl'][-1]:,.1f} mm")
    print(f"末 重叠={hist['eval_overlap'][-1]:.3f} mm²  "
          f"越界={hist['eval_bounds'][-1]:.3f} mm")
    return 0


def cmd_place(args) -> int:
    from .policy.place import PlaceConfig, place_real_board

    cfg = PlaceConfig(
        grid_mm=args.grid_mm, xy_std=args.xy_std, hidden=args.hidden,
        gnn_layers=args.layers, legalize_iters=args.legalize_iters,
        lam_net=args.lam_net, seed=args.seed,
        led_edge=args.led_edge, lam_edge_ic=args.lam_edge_ic,
        lam_ic_spread=args.lam_ic_spread, iface_edge=args.iface_edge,
        lam_decoup_dedicated=args.lam_decoup_dedicated,
        lam_decoup=args.lam_decoup,
        fix_connectors=not args.no_fix_connectors,
        fix_holes=not args.no_fix_holes,
        extra_fixed=list(args.extra_fixed or []),
        intent_file=args.intent_file,
    )
    d = place_real_board(args.sch, args.pcb, args.out, args.model, cfg)
    print(f"板尺寸       : {d['board'][0]:.1f} × {d['board'][1]:.1f} mm")
    print(f"器件         : 总 {d['n_total']} / 可动 {d['n_movable']} / 锁定 {d['n_fixed']}")
    print(f"解码 HPWL    : {d['hpwl_decode']:,.1f} mm（重叠 {d['overlap_decode']:.3f}，"
          f"越界 {d['bounds_decode']:.3f}）")
    print(f"合法化 HPWL  : {d['hpwl_final']:,.1f} mm（重叠 {d['overlap_final']:.3f}，"
          f"越界 {d['bounds_final']:.3f}）")
    for it in d.get("intent") or []:
        if "error" in it:
            print(f"意图 ✗       : {it['ref']} — {it['error']}")
            continue
        if "power_cluster" in it:
            pcc = it["power_cluster"]
            print(f"意图 电源簇  : 锚 {pcc['anchor']} 带 {pcc['region']} "
                  f"（IC {pcc['n_ic']} 个 {pcc['ic_refs']}，"
                  f"电源件 {pcc['n_extra']} 个 {pcc['extra_refs']}）")
            continue
        extra = f"  ⚠ {it['warn']}" if it.get("warn") else ""
        print(f"意图 {it['ref']:<4} {it['edge']:<6} along {it['along_spec']:.2f}→"
              f"{it['along_final']:.2f}  rot {it['rot']:.0f}°（{it['rot_signal']}）"
              f"  凸出 {it['protrude_eff']:.2f}mm"
              f"（钳位 {it['clamp']}）{extra}")
    print(f"写回         : {d['written']} 器件 → {d['out']}（剔除布线块 {d['stripped']}）")
    return 0


def cmd_eval(args) -> int:
    from .eval import EvalConfig, eval_real_board

    cfg = EvalConfig(
        model=args.model, hidden=args.hidden, layers=args.layers,
        grid_mm=args.grid_mm, xy_std=args.xy_std,
        legalize_iters=args.legalize_iters, lam_net=args.lam_net,
        seed=args.seed, max_passes=args.max_passes,
        attempts=args.attempts, timeout_s=args.timeout,
        keep_artifacts=args.keep_artifacts,
        led_edge=args.led_edge, lam_edge_ic=args.lam_edge_ic,
        lam_ic_spread=args.lam_ic_spread, iface_edge=args.iface_edge,
        lam_decoup_dedicated=args.lam_decoup_dedicated,
        lam_decoup=args.lam_decoup,
    )
    d = eval_real_board(args.sch, args.pcb, args.out, cfg)

    print(f"板尺寸        : {d['board'][0]:.1f} × {d['board'][1]:.1f} mm")
    print(f"器件          : 总 {d['n_total']} / 可动 {d['n_movable']} / "
          f"锁定 {d['n_fixed']}")
    print(f"放置 HPWL     : {d['hpwl_placed']:,.1f} mm（重叠 {d['overlap_placed']:.3f}，"
          f"越界 {d['bounds_placed']:.3f}）")
    print(f"── 后布线 ──")
    cl = d.get("cleanup") or {}
    if cl:
        print(f"清理          : 加宽 {cl.get('widened', 0)} 窄线 + 剪 "
              f"{cl.get('removed_via', 0)} 悬空过孔 / {cl.get('removed_track', 0)} 残段")
    print(f"线长          : {d['wirelength_mm']:,.1f} mm（{d['n_traces']} 段）")
    print(f"过孔          : {d['n_vias']}")
    print(f"布通率        : {d['completion']*100:.1f}%"
          f"（未布通 {d['unconnected_items']}）")
    print(f"DRC 违规      : 总 {d['drone_total']} "
          f"= 铜级 {d['drone_copper']} + 丝印 {d['drone_silk']} "
          f"+ 库级 {d['drone_lib']}")
    if d["drone_breakdown"]:
        bd = ", ".join(f"{k}={v}" for k, v in sorted(d["drone_breakdown"].items()))
        print(f"  细分        : {bd}")
    print(f"布线板        : {d['routed']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="plr", description="PCB 元器件自动放置（RL）")
    sub = p.add_subparsers(dest="cmd", required=True)

    ins = sub.add_parser("inspect", help="读 .kicad_sch/.kicad_pcb → IR 统计 + 指标")
    ins.add_argument("sch")
    ins.add_argument("--pcb", default=None)
    ins.set_defaults(func=cmd_inspect)

    gen = sub.add_parser("gen", help="生成合成放置问题")
    gen.add_argument("--n", type=int, default=60)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--fill", type=float, default=0.5)
    gen.add_argument("--fix-connectors", action="store_true")
    gen.set_defaults(func=cmd_gen)

    teach = sub.add_parser("teach", help="SA 教师：合成板或真实板生成参考放置")
    teach.add_argument("--syn", action="store_true", help="合成板（否则真实板）")
    teach.add_argument("--n", type=int, default=60)
    teach.add_argument("--seed", type=int, default=0)
    teach.add_argument("--fill", type=float, default=0.5)
    teach.add_argument("--fix-connectors", action="store_true")
    teach.add_argument("--iters", type=int, default=20000)
    teach.add_argument("--margin", type=float, default=5.0)
    teach.add_argument("sch", nargs="?", default=None)
    teach.add_argument("--pcb", default=None)
    teach.set_defaults(func=cmd_teach)

    bc = sub.add_parser("bc", help="行为克隆：SA 解 → GNN 策略（M3）")
    bc.add_argument("--n-problems", type=int, default=40)
    bc.add_argument("--n-min", type=int, default=20)
    bc.add_argument("--n-max", type=int, default=60)
    bc.add_argument("--iters", type=int, default=10000)
    bc.add_argument("--epochs", type=int, default=30)
    bc.add_argument("--lr", type=float, default=3e-4)
    bc.add_argument("--hidden", type=int, default=128)
    bc.add_argument("--layers", type=int, default=3)
    bc.add_argument("--seed", type=int, default=0)
    bc.add_argument("--out", default="policy.pt")
    bc.add_argument("--device", default="auto", choices=["auto", "cpu", "mps"])
    bc.set_defaults(func=cmd_bc)

    rl = sub.add_parser("rl", help="PPO 微调：掩码动作 + 复合奖励（M4）")
    rl.add_argument("--n-min", type=int, default=20)
    rl.add_argument("--n-max", type=int, default=60)
    rl.add_argument("--grid-mm", type=float, default=1.0)
    rl.add_argument("--xy-std", type=float, default=0.08)
    rl.add_argument("--iters", type=int, default=200)
    rl.add_argument("--rollouts", type=int, default=16)
    rl.add_argument("--ppo-epochs", type=int, default=4)
    rl.add_argument("--lr", type=float, default=1e-4)
    rl.add_argument("--ent-coef", type=float, default=0.01)
    rl.add_argument("--vf-coef", type=float, default=0.5)
    rl.add_argument("--w-overlap", type=float, default=1.0)
    rl.add_argument("--w-bounds", type=float, default=1.0)
    rl.add_argument("--hidden", type=int, default=128)
    rl.add_argument("--layers", type=int, default=3)
    rl.add_argument("--seed", type=int, default=0)
    rl.add_argument("--bc-path", default="policy.pt")
    rl.add_argument("--out", default="policy_rl.pt")
    rl.set_defaults(func=cmd_rl)

    plc = sub.add_parser("place", help="真实板：解码 + 合法化 + 写回 KiCad（M5）")
    plc.add_argument("sch", help=".kicad_sch")
    plc.add_argument("pcb", help=".kicad_pcb 模板板")
    plc.add_argument("--out", default=None, help="输出 placed-unrouted .kicad_pcb")
    plc.add_argument("--model", default="policy_rl.pt", help="策略 checkpoint")
    plc.add_argument("--grid-mm", type=float, default=1.27)
    plc.add_argument("--xy-std", type=float, default=0.08)
    plc.add_argument("--hidden", type=int, default=128)
    plc.add_argument("--layers", type=int, default=3)
    plc.add_argument("--legalize-iters", type=int, default=200)
    plc.add_argument("--lam-net", type=float, default=0.1)
    plc.add_argument("--seed", type=int, default=0)
    plc.add_argument("--led-edge", dest="led_edge", action="store_true", default=True,
                     help="原则②：LED+同网串联电阻锚贴边带（默认开，批 A 净赢；--no-led-edge 关）")
    plc.add_argument("--no-led-edge", dest="led_edge", action="store_false",
                     help="关闭原则② LED 贴边")
    plc.add_argument("--iface-edge", dest="iface_edge", action="store_true", default=True,
                     help="原则①：未锁定连接器锚贴边带（默认开；锁定臂成员表恒空自然 no-op；--no-iface-edge 关）")
    plc.add_argument("--no-iface-edge", dest="iface_edge", action="store_false",
                     help="关闭原则① 接口贴边")
    plc.add_argument("--lam-edge-ic", type=float, default=0.0,
                     help="原则①：支撑 IC 贴边系数（v1 已废，默认 0=关）")
    plc.add_argument("--lam-ic-spread", type=float, default=5.0,
                     help="原则⑥：主芯片均匀软斥系数（默认 5=开，批 A 双板正收益；0=关）")
    plc.add_argument("--lam-decoup-dedicated", type=float, default=0.0,
                     help="专属轨去耦电容（轨上唯一 IC）单独系数（默认 0=沿用 lam_decoup 现行为）")
    plc.add_argument("--lam-decoup", type=float, default=None,
                     help="共享轨去耦电容系数覆盖（默认 None=沿用 LegalizeConfig 0.15）")
    plc.add_argument("--no-fix-connectors", action="store_true")
    plc.add_argument("--no-fix-holes", action="store_true")
    plc.add_argument("--intent-file", default=None,
                     help="意图 spec JSON（接口件边沿/开口朝外/凸出，默认 None=关）")
    plc.add_argument("--extra-fixed", action="append", default=None,
                     help="额外锁定位号（可重复）")
    plc.set_defaults(func=cmd_place)

    ev = sub.add_parser("eval", help="M6 闭环布线评估：放置→布线→后布线指标")
    ev.add_argument("sch", help=".kicad_sch")
    ev.add_argument("pcb", help=".kicad_pcb 模板板")
    ev.add_argument("--out", default=None, help="输出目录（placed/routed 板）")
    ev.add_argument("--model", default="policy_rl.pt", help="策略 checkpoint")
    ev.add_argument("--grid-mm", type=float, default=1.27)
    ev.add_argument("--xy-std", type=float, default=0.08)
    ev.add_argument("--hidden", type=int, default=128)
    ev.add_argument("--layers", type=int, default=3)
    ev.add_argument("--legalize-iters", type=int, default=200)
    ev.add_argument("--lam-net", type=float, default=0.1)
    ev.add_argument("--seed", type=int, default=0)
    ev.add_argument("--led-edge", dest="led_edge", action="store_true", default=True,
                    help="原则②：LED+同网串联电阻锚贴边带（默认开；--no-led-edge 关）")
    ev.add_argument("--no-led-edge", dest="led_edge", action="store_false",
                    help="关闭原则② LED 贴边")
    ev.add_argument("--iface-edge", dest="iface_edge", action="store_true", default=True,
                    help="原则①：未锁定连接器锚贴边带（默认开；--no-iface-edge 关）")
    ev.add_argument("--no-iface-edge", dest="iface_edge", action="store_false",
                    help="关闭原则① 接口贴边")
    ev.add_argument("--lam-edge-ic", type=float, default=0.0,
                    help="原则①：支撑 IC 贴边系数（v1 已废，默认 0=关）")
    ev.add_argument("--lam-ic-spread", type=float, default=5.0,
                    help="原则⑥：主芯片均匀软斥系数（默认 5=开；0=关）")
    ev.add_argument("--lam-decoup-dedicated", type=float, default=0.0,
                    help="专属轨去耦电容（轨上唯一 IC）单独系数（默认 0=沿用 lam_decoup）")
    ev.add_argument("--lam-decoup", type=float, default=None,
                    help="共享轨去耦电容系数覆盖（默认 None=沿用 LegalizeConfig 0.15）")
    ev.add_argument("--max-passes", type=int, default=40, help="FreeRouting --max-passes")
    ev.add_argument("--attempts", type=int, default=1, help="best-of-N 布线次数")
    ev.add_argument("--timeout", type=float, default=600.0, help="单次布线超时 s")
    ev.add_argument("--keep-artifacts", action="store_true",
                    help="保留 route.dsn / route.ses 中间产物")
    ev.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
