"""kca 命令行入口：kca <ingest|place|route|build|report|doctor>。

doctor（M0）/ ingest（M1）/ place（M2）/ route 电源区（M3-3）为实做；
其余子命令为里程碑占位，到位一个实装一个。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import tools


def _cmd_doctor(args: argparse.Namespace) -> int:
    """工具链体检：kca 四件套 + 全链附加（node / d2s MCP server / 机器配置文件）。"""
    print(f"kca {__version__} 工具链体检")
    print("-" * 60)
    all_ok = True
    for st in tools.all_tools():
        mark = "✓" if st.ok else "✗"
        loc = st.path if st.path else "-"
        print(f"{mark} {st.name:<16} {st.detail}")
        print(f"  {'':<16} {loc}")
        if not st.ok:
            all_ok = False
            print(f"  降级提示: {st.hint}")
    print("-" * 60)
    print("全链附加（d2s 原理图→板链路所需；kca 自身不强依赖）：")
    for st in tools.full_chain_tools():
        mark = "✓" if st.ok else "✗"
        loc = st.path if st.path else "-"
        print(f"{mark} {st.name:<16} {st.detail}")
        print(f"  {'':<16} {loc}")
        if not st.ok:
            print(f"  降级提示: {st.hint}")
    print("-" * 60)
    print("机器配置文件（env > 此文件 > 标准探测）：")
    for path, note in tools.config_report():
        print(f"  {path}: {note}")
    print("-" * 60)
    print("结论: " + ("全部就绪" if all_ok else "有缺项——纯文本功能可用，外部引擎按上列降级"))
    return 0  # 缺项不算命令失败：按设计降级，CI 里不红


def _cmd_new(args: argparse.Namespace) -> int:
    """脚手架新板项目 projects/<name>：doc · schematic{regions,build} · layout{regions,build}。"""
    from .scaffold import default_projects_dir, scaffold_project

    root = Path(args.root).expanduser().resolve() if args.root else default_projects_dir()
    try:
        proj = scaffold_project(args.name, root)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    except FileExistsError as e:
        print(f"✗ 项目已存在: {e}")
        return 2
    print(f"✓ 新项目 → {proj}")
    print("  结构: doc/ 设计规格 · schematic/{regions,build} · layout/{regions,build} · <name>.yaml")
    print(f"  下一步: 填 {proj / f'{args.name}.yaml'} 的 schematic + 板框，再 `kca place`")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    """sch 层级解析 → IR：终端报告 + 可选 JSON（M2 输入）+ 可选 PCB 交叉核对。"""
    from .ingest import build_ir
    from .ingest.report import render_report, to_json_dict
    from .kicad.schematic import Design

    root = Path(args.sch).expanduser().resolve()
    design = Design(root)
    ir = build_ir(design)

    pcb_lines: list[str] | None = None
    if args.pcb:
        pcb_lines = crosscheck_lines(ir, Path(args.pcb).expanduser().resolve())

    print(render_report(ir, pcb_lines))

    if args.json:
        out = Path(args.json).expanduser().resolve()
        out.write_text(json.dumps(to_json_dict(ir), ensure_ascii=False, indent=2))
        print(f"\nIR JSON → {out}")

    if args.strict and any(w.startswith("⚠") for w in ir.warnings):
        print("\n--strict：存在缺陷级警告，退出码 2")
        return 2
    return 0


def crosscheck_lines(ir, pcb_path: Path) -> list[str]:
    from .ingest.crosscheck import crosscheck
    from .kicad.pcb import Board

    if not pcb_path.exists():
        return [f"⚠ PCB 文件不存在: {pcb_path}"]
    return crosscheck(ir, Board(pcb_path))


def _load_recipes():
    """recipes 包在仓库根（wheel 只打包 src/，安装态不含）——入口兜底注入 sys.path。"""
    root = Path(__file__).resolve().parents[2]
    if root not in sys.path:
        sys.path.insert(0, str(root))
    from recipes import load_engine

    return load_engine()


def _design_for(spec):
    """Design(spec.schematic) + spec.part_overrides 内存换装（sch 文件不动）。

    place/route/merge 三入口统一走这里——换装变体板与原理图逐网一致。
    """
    from .kicad.partswap import apply_part_overrides
    from .kicad.schematic import Design

    design = Design(spec.schematic)
    if spec.part_overrides:
        for line in apply_part_overrides(design, spec.part_overrides):
            print(f"  换装 {line}")
    return design


def _cmd_place(args: argparse.Namespace) -> int:
    """板规格 YAML → IR + 几何 + recipe → 整板 floorplan（M2-2b）+ 可选 SVG 预览。"""
    from .ingest import build_ir
    from .kicad.fplib import FootprintLibs
    from .kicad.schematic import Design
    from .place.floorplan import run_floorplan
    from .place.geometry import load_geometry
    from .place.preview import render_svg
    from .place.shrink import shrink_floorplan
    from .spec import load_spec

    spec = load_spec(args.spec)
    if spec.schematic is None:
        print("规格缺 project_info.schematic（place 的 IR 入口），退出码 2")
        return 2
    ir = build_ir(_design_for(spec))
    libs = FootprintLibs(spec.schematic.parent)
    geoms, problems = load_geometry(ir, libs)
    if problems:
        for p in problems:
            print(f"✗ {p}")
        return 2

    if args.shrink:  # M3-3b：spec.size 是起点，质量门把关下二分逼近最小可用
        spec, rounds, shrunk = shrink_floorplan(
            spec, ir, geoms, _load_recipes(), step=args.shrink_step)
        for r in rounds:
            print(f"  {r}")
        if shrunk is None:
            print("✗ 起点 spec.size 本身不过质量门，未收缩（见上）")
            return 2
        print(f"收缩 {rounds[0].size} → {spec.size}"
              f"（{len(rounds)} 轮候选，建议回填 board.outline.size 定案）")
        result = shrunk
    else:
        result = run_floorplan(spec, ir, geoms, _load_recipes())

    n, total = result.coverage
    print(f"coverage {n}/{total}")
    if result.fusion_pushes:
        print(f"融合推挤 {result.fusion_pushes} 步（soft 让出外模块 core⊕band）")
    for sheet, sp in result.sheets.items():
        recipes = "+".join(en.recipe for en in sp.entries if en.recipe) or "力导向兜底"
        print(
            f"  {sheet:<10} {recipes:<26} "
            f"origin=({sp.frame.origin[0]:.1f},{sp.frame.origin[1]:.1f}) "
            f"rot={sp.frame.rot:.0f}° 器件 {len(sp.placements)}"
        )
    print("诊断:")
    if result.diagnostics:
        for d in result.diagnostics:
            print(f"  {'⚠' if '重叠' in d else '·'} {d}")
    else:
        print("  无")

    if args.preview:
        out = Path(args.preview).expanduser().resolve()
        out.write_text(render_svg(result, geoms), encoding="utf-8")
        print(f"\n预览 SVG → {out}")

    if args.out:  # placed-unrouted 导出（模板 = spec.template_pcb）
        if spec.template_pcb is None or not spec.template_pcb.is_file():
            print("规格缺 board.template_pcb（导出底版），退出码 2")
            return 2
        from .kicad.pcbwrite import FootprintSwap, write_placements

        swaps: dict[str, FootprintSwap] = {}
        for ref, ov in spec.part_overrides.items():
            if not ov.footprint or ref not in ir.devices:
                continue  # 纯 pinmap 覆盖不动模板块
            mod = libs.path_of(ov.footprint)
            if mod is None or not mod.is_file():
                print(f"✗ part_overrides.{ref} 封装库找不到 {ov.footprint}，退出码 2")
                return 2
            pad_nets = {str(pin.pin): net.name for net in ir.nets
                        for pin in net.pins if pin.ref == ref}
            swaps[ref] = FootprintSwap(
                lib_id=ov.footprint,
                module_text=mod.read_text(encoding="utf-8"),
                value=ir.devices[ref].value,
                pad_nets=pad_nets)
        out = Path(args.out).expanduser().resolve()
        n, stripped = write_placements(
            spec.template_pcb, out, result.placements, size=spec.size,
            footprint_swaps=swaps or None)
        print(
            f"placed-unrouted 板 → {out}\n"
            f"  写回 {n} 器件放置态，剔除模板布线块 {stripped}（segment/via/zone）"
        )
        for ref in swaps:
            print(f"  换块 {ref} → {swaps[ref].lib_id}（{len(swaps[ref].pad_nets)} 盘配网）")
    return 0


def _verify_base(board_path: Path, spec, stage: str) -> bool:
    """底板-规格一致性门卫（2026-09-11 事故修复）。

    事故：merge 漏传 --board，argparse 默认值 build/placed_unrouted.kicad_pcb
    （6 层 BGA 项目板）被静默用作 LQFP/4 层底板——80×120 的铜被合进
    58×57.5 的边框，370 对假"未连接"（zone 铺在旧框上对不上铜）。
    此后 route/merge 进 base 板必过三关：板框尺寸、铜层数、器件数，
    任一不符即拒。产物（合并板）出盘后同函数复验。防的是"漏传参数 +
    指向陈旧文件的默认值"这类无声错误，与布线算法无关。"""
    import re as _re

    from .route import RouteBoard

    b = RouteBoard(board_path)
    x1, y1, x2, y2 = b.outline
    w, h = x2 - x1, y2 - y1
    problems: list[str] = []
    if (abs(w - spec.size[0]) > 0.5 or abs(h - spec.size[1]) > 0.5):
        problems.append(
            f"板框 {w:.1f}×{h:.1f}mm ≠ 规格 {spec.size[0]:.1f}"
            f"×{spec.size[1]:.1f}mm")
    cu = [m for m in _re.findall(
        r'\(\d+ "([^"]+)" (?:signal|power|jumper|mixed)\)',
        board_path.read_text(encoding="utf-8")) if m.endswith(".Cu")]
    if cu and len(cu) != spec.layer_count:
        problems.append(
            f"铜层 {len(cu)} ≠ 规格 layer_count {spec.layer_count}")
    n_fp = len({p.ref for p in b.pads})
    if n_fp == 0:
        problems.append("0 器件（像是空模板而非 placed 板）")
    if problems:
        print(f"✗ {stage} 底板与规格不符: {board_path}")
        for p_ in problems:
            print(f"  · {p_}")
        print("  多半是 --board 漏传/传错（CLI 默认值指向旧项目板）；"
              "显式传 --board，或先 kca place --out 生成")
        return False
    print(f"· 底板核验 ✓ {board_path.name} 板框 {w:.1f}×{h:.1f}mm"
          f" {len(cu)} 层铜 {n_fp} 器件")
    return True


def _cmd_route(args: argparse.Namespace) -> int:
    """领地布线（锚盘模型，2026-09-11）：区板并行，区内布到锚盘为止。

    每**领地**一块独立 .kicad_pcb 落盘供人工逐区检查；合并（互联段 +
    删盘对接）是检查之后的另一步。流程：

    1. 领地 + 锚盘规划（territories/plan_anchors，警告如实打印）；
    2. 电源落地铜全局落一次（pwr.kicad_pcb：盘中孔/落地孔 + stub，
       zone 仍推迟终相）——各领地区板共此底，平行出发互不排队；
    3. 每领地：write_territory_board（剥外模块跨区网 + 开真实锚盘）
       → route_l2 布本区网（intra + 跨区网本地片段）→ 区板落盘。
    """
    from .ingest import build_ir
    from .kicad.schematic import Design
    from .route import (
        FreerouteError,
        Region,
        RouteBoard,
        UnroutedNets,
        plan_anchors,
        plan_power_drops,
        plan_regions,
        route_l2,
        territories,
        territory_nets,
        widths_from_spec,
        write_region_board,
        write_territory_board,
    )
    from .spec import load_spec

    spec = load_spec(args.spec)
    if spec.schematic is None:
        print("规格缺 project_info.schematic（route 的 IR 入口），退出码 2")
        return 2
    board_path = Path(args.board).expanduser().resolve()
    if not board_path.is_file():
        print(f"✗ base 板不存在: {board_path}（先 kca place --out 生成）")
        return 2
    if not _verify_base(board_path, spec, "route"):
        return 2

    ir = build_ir(_design_for(spec))
    board = RouteBoard(board_path)
    terrs = territories(ir, board)
    anchors, warns = plan_anchors(ir, board, terrs)
    for w in warns:
        print(f"⚠ {w}")
    widths = widths_from_spec(spec)
    out_dir = (Path(args.out).expanduser().resolve() if args.out
               else board_path.parent / "route")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 电源落地铜全局一次：落地孔是 TH 孔穿全部信号层，领地区板看不见
    # 就会撞孔，所以各领地共此底（平行出发，不再是链式排队）
    planes = next(
        (r for r in plan_regions(ir, spec) if r.router == "l1_planes"), None)
    base = board_path
    if planes is not None:
        try:
            segs, vias, _ = plan_power_drops(
                RouteBoard(board_path), planes, spec, widths)
            base = write_region_board(
                board_path, out_dir / "pwr.kicad_pcb", planes, segs, vias)
            print(f"✓ pwr        落地孔 {len(vias)} + stub {len(segs)}"
                  f" → {base.name}（各领地共此底）")
        except (ValueError, FreerouteError) as e:
            print(f"✗ 电源落地铜失败: {e}")
            return 2

    done = 0
    manual: list[str] = []  # 显式遗留：降级剔除的死网（手工兜底清单）
    for mod in sorted(terrs):
        own = [a for a in anchors if a.module == mod]
        nets = territory_nets(ir, mod, own)
        if not nets:
            print(f"· {mod:<10} 0 网 → 空领地，跳过")
            continue
        if not (tools.java_status().ok and tools.freerouting_status().ok):
            print(f"· {mod:<10} {len(nets):>2} 网 → 外部引擎缺项"
                  f"（java/freerouting.jar），跳过")
            continue
        frags = {a.net for a in own}  # 跨区网本地片段数（锚≠网，一网可多锚）
        try:
            terr = write_territory_board(
                base, out_dir / f"{mod}.base.kicad_pcb",
                ir=ir, module=mod, anchors=own)
            live = Region(mod, nets, "l2_freerouting")
            # 显式降级：死网剔除后重跑（≤3 轮），遗留进手工清单——
            # 绝不静默断线；区板平行，一块卡死不传染其他领地
            for _ in range(3):
                try:
                    segs, vias, reports = route_l2(
                        RouteBoard(terr), live, spec,
                        widths=widths, workdir=out_dir)
                    break
                except UnroutedNets as e:
                    dead = list(e.unrouted)
                    manual += dead
                    live = Region(live.name,
                                  tuple(n for n in live.nets
                                        if n not in e.unrouted),
                                  live.router)
                    print(f"⚠ {mod} 剔除 {dead} 重跑（进手工清单）")
            else:
                print(f"✗ 领地 {mod} 降级 3 轮仍失败，跳过（不传染其他领地）")
                continue
            out = write_region_board(
                terr, out_dir / f"{mod}.kicad_pcb", live, segs, vias)
        except (ValueError, FreerouteError) as e:
            print(f"✗ 领地 {mod} 布线失败: {e}")
            return 2
        done += 1
        print(f"✓ {mod:<10} {len(live.nets):>2} 网（intra {len(nets) - len(frags)}"
              f" + 片段 {len(frags)}）→ {out.name}")
        for r in reports:
            print(f"    {r.net:<12} 盘{r.pads:>3} 边{r.edges:>3}"
                  f" 线段{r.segments:>4} 孔{r.vias:>3} 长{r.length:>8.2f}mm")
    print(f"\n{done} 领地区板落盘于 {out_dir}/（人工逐区检查后方可合并，不提供跳过）")
    if manual:
        print(f"⚠ 手工待布 {len(manual)} 网（用户授权兜底路线）: {manual}")
        return 1
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    """领地合并（锚盘模型，2026-09-11）：叠加 → 互联段 → 删盘对接 → 背景段。

    前置 = ``kca route`` 已落领地区板且人工逐区检查完。四步：

    1. **叠加**：placed 底 + pwr 落地铜 + 各领地区板铜（平行差分，缺
       板即拒——不提供跳过）；异网短路先拦一道；
    2. **互联段**：全锚盘开盘，跨区网（system）全网表布线——锚盘唯一
       pin、全量既有铜转障碍（protect 无视实证的根因修复）；死网剔除
       重跑 ≤4 轮后**抢救段**单独补布（大 DSN 里被先到网挤死的短网独
       跑实证可通），仍遗留进手工清单改返回码 1（锚盘保留供人工续布，
       绝不静默断线）；
    3. **删盘对接**：锚盘封装删除，盘心 0.35mm 内本网端点焊回盘心；
    4. **背景段**：GND 缝合 + 平面终相铺铜（--no-fusion 跳过）+ DRC。
    """
    from .ingest import build_ir
    from .kicad.schematic import Design
    from .route import (
        FreerouteError, Region, RouteBoard, UnroutedNets, append_copper,
        drop_anchors, draw_territories, open_anchors, overlay_territories,
        plan_anchors, plan_regions, route_l2, route_planes_final, run_drc,
        stitch_gnd, territory_nets, territories, widths_from_spec,
    )
    from .spec import load_spec

    spec = load_spec(args.spec)
    if spec.schematic is None:
        print("规格缺 project_info.schematic（merge 的 IR 入口），退出码 2")
        return 2
    board_path = Path(args.board).expanduser().resolve()
    if not board_path.is_file():
        print(f"✗ base 板不存在: {board_path}（先 kca place --out 生成）")
        return 2
    if not _verify_base(board_path, spec, "merge"):
        return 2
    route_dir = (Path(args.route_dir).expanduser().resolve() if args.route_dir
                 else board_path.parent / "route")
    out = (Path(args.out).expanduser().resolve() if args.out
           else board_path.parent / "merged.kicad_pcb")

    ir = build_ir(_design_for(spec))
    board = RouteBoard(board_path)
    terrs = territories(ir, board)
    anchors, warns = plan_anchors(ir, board, terrs)
    for w in warns:
        print(f"⚠ {w}")
    widths = widths_from_spec(spec)

    # 1. 叠加（领地清单与 route 的产板条件一一对应：有网才要板）
    mods = [mod for mod in sorted(terrs)
            if territory_nets(ir, mod,
                              [a for a in anchors if a.module == mod])]
    try:
        ov = overlay_territories(
            board_path, route_dir / "inter.base.kicad_pcb", route_dir, mods)
    except ValueError as e:
        print(f"✗ 合并拒绝: {e}")
        return 2
    for name, (ns, nv) in sorted(ov.per_source.items()):
        print(f"✓ {name:<10} +{ns:>3} 线 +{nv:>2} 孔")
    print(f"叠加 +{len(ov.segments)} 线 +{len(ov.vias)} 孔"
          f" → {ov.out.name}")

    # 2. 互联段（跨区网全网表，锚盘互联模式：锚盘=唯一 pin、全量既有铜
    #    =障碍——protect 无视实证下 406 短路的根因修复，2026-09-11）
    if not (tools.java_status().ok and tools.freerouting_status().ok):
        print("✗ java/freerouting.jar 缺项，互联段无法布线")
        return 2
    inter = open_anchors(ov.out, route_dir / "inter.kicad_pcb", anchors)
    anchor_refs = frozenset(
        f"ANC{i}" for i in range(1, len(anchors) + 1))  # open_anchors 位号
    live = Region(
        "inter", tuple(sorted(n.name for n in ir.nets if n.kind == "system")),
        "l2_freerouting")
    manual: list[str] = []
    isegs, ivias = [], []
    if live.nets:
        try:
            for _ in range(4):
                try:
                    isegs, ivias, _reports = route_l2(
                        RouteBoard(inter), live, spec,
                        widths=widths, workdir=route_dir,
                        pin_refs=anchor_refs)
                    break
                except UnroutedNets as e:
                    manual += list(e.unrouted)
                    live = Region(live.name,
                                  tuple(n for n in live.nets
                                        if n not in e.unrouted),
                                  live.router)
                    print(f"⚠ inter 剔除 {list(e.unrouted)} 重跑（记账，"
                          "抢救段再补）")
            else:
                print("✗ 互联段降级 4 轮仍失败")
                return 2
        except (ValueError, FreerouteError) as e:
            print(f"✗ 互联段布线失败: {e}")
            return 2

    # 抢救段：降级掉的死网在已布互联铜之上单独补布。大 DSN 里被先到
    # 网挤死的短网（ETH LED/RBIAS，每轮恰死 1 个）单独跑实证可通
    # （2026-09-11 diag3：3 网独跑 0 unrouted）——通了就从手工清单
    # 销账，全 0 飞线收场；组失败再逐网兜底。
    saved: list[str] = []
    if manual:
        sb = RouteBoard(inter)
        sb.segments.extend(isegs)
        sb.vias.extend(ivias)
        for dead in [tuple(manual)] + [(n,) for n in manual]:
            todo = tuple(n for n in dead if n not in saved)
            if not todo:
                continue
            try:
                s_segs, s_vias, _sr = route_l2(
                    sb, Region("inter_salvage", todo, "l2_freerouting"),
                    spec, widths=widths, workdir=route_dir,
                    pin_refs=anchor_refs)
            except (UnroutedNets, FreerouteError, ValueError) as e:
                print(f"· 抢救组 {list(todo)} 未成: {e}")
                continue
            sb.segments.extend(s_segs)
            sb.vias.extend(s_vias)
            isegs += s_segs
            ivias += s_vias
            saved += list(todo)
        if saved:
            print(f"✓ 抢救段补布 {saved}")
        manual = [n for n in manual if n not in saved]
    append_copper(inter, out, segments=isegs, vias=ivias)
    if manual:
        print(f"⚠ 手工待布 {len(manual)} 网"
              f"（锚盘保留在 {out.name} 供人工续布）: {manual}")
        return 1
    print(f"✓ 互联段 {len(live.nets) + len(saved)} 网"
          f" +{len(isegs)} 线 +{len(ivias)} 孔")

    # 3. 删盘对接（锚盘使命完成：两端铜在盘心焊接连通）
    n_drop, welds = drop_anchors(out, out)
    print(f"删锚盘 {n_drop} + 对接焊 {len(welds)} 段")
    if not _verify_base(out, spec, "merge 产物"):
        return 2  # 板框/层数/器件数与规格不符：底板错了，产物一并拒

    if args.draw_regions:
        draw_territories(out, terrs=terrs, anchors=anchors)
        print(f"· 领地框 + 锚盘标记 {len(terrs)} 领地画入 Eco1.User")

    # 4. 背景段 + DRC
    if args.no_fusion:
        print("· --no-fusion：跳过 GND 缝合 + 平面终相（机械合并，诊断用）")
    else:
        # v8 两相·终相：满铜板上铺平面（merge 后全部穿越铜可见）。
        # 链首 plan_power_drops 只落孔，zone 在这里才产——领地融合带着异网
        # TH 铜的净空切断边（KiCad fill 掏洞语义）算连通，残余碎片走外层
        # 逃逸桥。正是 v7 链首铺铜被穿越孔戳成 67 孤岛的补救架构。
        planes_region = next(
            (r for r in plan_regions(ir, spec)
             if r.router == "l1_planes" and r.nets), None)
        if planes_region is not None:
            fsegs, fvias, fzones, freports = route_planes_final(
                RouteBoard(out), planes_region, spec, widths)
            if fzones or fsegs or fvias:
                append_copper(out, segments=fsegs, vias=fvias, zones=fzones)
            n_def = sum(r.deferred for r in freports)
            print(f"平面铺铜 {len(fzones)} 区 + 逃逸桥 {len(fsegs)} 段"
                  f" +{len(fvias)} 孔" + (f"（⚠ 悬 {n_def} 待 DRC 复核）"
                                          if n_def else ""))
            for r in freports:
                mark = "⚠" if r.deferred else "✓"
                print(f"  {mark} {r.net:<12} 盘{r.pads:>3} 边{r.edges:>3}"
                      f" 孔{r.vias:>3} 桥{r.length:>7.2f}mm"
                      + (f" 悬{r.deferred}" if r.deferred else ""))

        # GND 缝合在平面**之后**：stitch_gnd 读 GND 内层 zone 多边形，
        # zone 由上一步 route_planes_final 才落——先缝后铺恒 0 孔
        # （2026-09-11 实证"缝合 0 孔避让 0 位"）。穿越 In2 电源由
        # KiCad fill 掏反焊盘，不构成短路。
        if args.no_stitch:
            print("· --no-stitch：跳过 GND 缝合")
        else:
            res = stitch_gnd(RouteBoard(out), spec)
            if res.vias:
                append_copper(out, vias=res.vias)
            print(f"GND 缝合 {res.placed} 孔（避让 {res.skipped} 位）")

    violations = run_drc(RouteBoard(out), spec)
    if violations:
        print(f"\n✗ DRC {len(violations)} 违规（合并板已落盘供定位）:")
        for v in violations[:50]:
            print(f"  {v}")
        if len(violations) > 50:
            print(f"  …（另 {len(violations) - 50} 条略）")
        return 2
    print("✓ DRC 干净（短路/间隙/宽度/断裂）")
    return 0


def _cmd_anchors(args: argparse.Namespace) -> int:
    """领地 + 锚盘规划（锚盘布线模型 step 1）：只读规划 + Eco1.User 诊断板。

    领地 = 模块焊盘 bbox + 边距；跨区网（system）每参与领地开一锚盘，
    区内布到盘为止、互联段盘到盘。本命令不产铜——几何是否成立（锚盘
    落位、领地互叠）先在这一步肉眼验收，区板/互联/背景在后续增量实装。
    """
    from .ingest import build_ir
    from .route import (
        RouteBoard, draw_territories, plan_anchors, territories,
    )
    from .spec import load_spec

    spec = load_spec(args.spec)
    if spec.schematic is None:
        print("规格缺 project_info.schematic（anchors 的 IR 入口），退出码 2")
        return 2
    board_path = Path(args.board).expanduser().resolve()
    if not board_path.is_file():
        print(f"✗ base 板不存在: {board_path}（先 kca place --out 生成）")
        return 2
    if not _verify_base(board_path, spec, "anchors"):
        return 2

    ir = build_ir(_design_for(spec))
    board = RouteBoard(board_path)
    terrs = territories(ir, board)
    anchors, warns = plan_anchors(ir, board, terrs)

    intra: dict[str, int] = {}
    for n in ir.nets:
        if n.kind == "intra" and n.modules:
            intra[n.modules[0]] = intra.get(n.modules[0], 0) + 1

    print(f"· 领地 {len(terrs)} 个（模块焊盘 bbox + {_f_margin()}mm 边距）:")
    for name, (x1, y1, x2, y2) in sorted(terrs.items()):
        print(f"  {name:<10} ({x1:6.1f},{y1:6.1f})-({x2:6.1f},{y2:6.1f})"
              f" {x2 - x1:5.1f}×{y2 - y1:<5.1f} 区内网 {intra.get(name, 0)}")
    by_net: dict[str, list] = {}
    for a in anchors:
        by_net.setdefault(a.net, []).append(a)
    print(f"· 跨区网 {len(by_net)} 个 → 锚盘 {len(anchors)} 个（每网每领地一盘）:")
    for net in sorted(by_net):
        pts = " ".join(f"{a.module}({_f_xy(a.x)},{_f_xy(a.y)})"
                       for a in sorted(by_net[net], key=lambda a: a.module))
        print(f"  {net:<14} {pts}")
    if warns:
        print(f"⚠ {len(warns)} 条:")
        for w in warns[:20]:
            print(f"  {w}")
        if len(warns) > 20:
            print(f"  …（另 {len(warns) - 20} 条略）")
    out = (Path(args.out).expanduser().resolve() if args.out
           else board_path.parent / "anchors_viz.kicad_pcb")
    draw_territories(board_path, out, terrs=terrs, anchors=anchors)
    print(f"· 诊断板 → {out}（Eco1.User 领地框 + 锚盘标记，不进铜）")
    return 0


def _f_margin() -> str:
    from .route.anchors import MARGIN
    return f"{MARGIN:g}"


def _f_xy(v: float) -> str:
    return f"{v:g}"


def _stub(name: str, milestone: str) -> "type":
    def cmd(_: argparse.Namespace) -> int:
        print(f"`kca {name}` 属于 {milestone}，尚未实装。")
        return 1

    cmd.__name__ = f"_cmd_{name}"
    return cmd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kca", description="KiCad Access 工具集")
    p.add_argument("--version", action="version", version=f"kca {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "doctor", help="检查 kicad-cli / kicad-python / java / freerouting 工具链"
    ).set_defaults(func=_cmd_doctor)
    nw = sub.add_parser("new", help="脚手架新板项目 projects/<name>（doc/schematic/layout）")
    nw.add_argument("name", help="项目/目录名（字母开头，字母数字 _ -）")
    nw.add_argument(
        "--root", help="projects 根目录（默认仓库根 projects/）",
    )
    nw.set_defaults(func=_cmd_new)
    ing = sub.add_parser("ingest", help="sch 层级解析 → IR（M1）")
    ing.add_argument("sch", help="根 .kicad_sch 路径")
    ing.add_argument("--pcb", help="可选 .kicad_pcb，做器件交叉核对（M1-3）")
    ing.add_argument("--json", help="IR JSON 输出路径（M2 布局输入）")
    ing.add_argument(
        "--strict", action="store_true",
        help="存在缺陷级警告（⚠ net 短接等）时退出码 2（CI 门禁）",
    )
    ing.set_defaults(func=_cmd_ingest)
    pl = sub.add_parser("place", help="板规格 YAML → 整板 floorplan（M2）")
    pl.add_argument("spec", help="板规格 YAML 路径（projects/*.yaml）")
    pl.add_argument("--preview", help="SVG 预览输出路径")
    pl.add_argument("--out", help="placed-unrouted .kicad_pcb 导出路径（模板 = board.template_pcb）")
    pl.add_argument(
        "--shrink", action="store_true",
        help="板尺寸收缩（M3-3b）：spec.size 为起点，W/H 各自二分逼近最小可用板",
    )
    pl.add_argument(
        "--shrink-step", type=float, default=0.5, metavar="MM",
        help="收缩候选边长吸附网格，mm（默认 0.5；粗网格跑得快）",
    )
    pl.set_defaults(func=_cmd_place)
    rt = sub.add_parser(
        "route", help="区域布线：per-region 板（M3，逐区人工检查）"
    )
    rt.add_argument("spec", help="板规格 YAML 路径（projects/*.yaml）")
    rt.add_argument(
        "--board", default="build/placed_unrouted.kicad_pcb",
        help="base 板（placed-unrouted）路径",
    )
    rt.add_argument(
        "--out", help="区板输出目录（默认 <board 所在目录>/route/）",
    )
    rt.set_defaults(func=_cmd_route)
    mg = sub.add_parser(
        "merge", help="区板合并 + GND 缝合 + DRC 闭环（M3-6）"
    )
    mg.add_argument("spec", help="板规格 YAML 路径（projects/*.yaml）")
    mg.add_argument(
        "--board", default="build/placed_unrouted.kicad_pcb",
        help="route 的 base 板（placed-unrouted）路径",
    )
    mg.add_argument(
        "--route-dir", help="区板目录（默认 <board 所在目录>/route/）",
    )
    mg.add_argument(
        "--out", help="合并板输出路径（默认 <board 所在目录>/merged.kicad_pcb）",
    )
    mg.add_argument(
        "--no-stitch", action="store_true", help="跳过 GND 缝合孔",
    )
    mg.add_argument(
        "--no-fusion", action="store_true",
        help="机械合并即止：跳过 GND 缝合 + 平面终相（区域间融合分析用）",
    )
    mg.add_argument(
        "--draw-regions", action="store_true",
        help="Eco1.User 画各区布铜足迹框 + 区名（诊断叠层）",
    )
    mg.set_defaults(func=_cmd_merge)
    an = sub.add_parser(
        "anchors", help="领地+锚盘规划（锚盘布线模型，只读诊断板）"
    )
    an.add_argument("spec", help="板规格 YAML 路径（projects/*.yaml）")
    an.add_argument(
        "--board", default="build/placed_unrouted.kicad_pcb",
        help="base 板（placed-unrouted）路径",
    )
    an.add_argument(
        "--out", help="诊断板输出路径（默认 <board 所在目录>/anchors_viz.kicad_pcb）",
    )
    an.set_defaults(func=_cmd_anchors)
    sub.add_parser(
        "build", help="生成 .kicad_pcb / Gerber（M3/M4）"
    ).set_defaults(func=_stub("build", "M3/M4"))
    sub.add_parser(
        "report", help="DRC / 线长 / 质量报告（M4）"
    ).set_defaults(func=_stub("report", "M4"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
