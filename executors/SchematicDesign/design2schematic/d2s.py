#!/usr/bin/env python3
"""design2schematic (d2s) — 设计表 → 原理图 → 板骨架 → RL 放置 → RL 布线 → 铺铜 → DRC 0。

三个子命令（产物默认落 PCBStudioV2/KiCAD_Projects/<项目>/，规范命名 + 版本编号）：
  d2s sch   --design t.json            → <项目>_vN.kicad_sch / .net / .pdf（ERC 0 错误门禁）
  d2s board --sch S --design t.json    → <项目>_vN_base.kicad_pcb（板框/安装孔/锚点预放/stackup）
  d2s chain --design t.json            → …_placed.kicad_pcb → …_route/ → …_routed.kicad_pcb（DRC 0 终板）

版本规则：vN 自动递增（项目目录内扫描 max+1）；chain --step-from 重入时自动复用最新版本；
--version N 显式指定。board 子命令的版本从 --sch 文件名推导（sch 文件名即版本载体）。

Python stdlib-only。KiCad 写入经 KiCAD-MCP-Server（mcp_client.py，JSON-RPC/stdio）；
Placement / Routing 以子进程原样调用（内部零改动）。
路径可用环境变量覆盖：D2S_MCP / D2S_NODE / D2S_KICAD_EXT / D2S_PLACEMENT_RL / D2S_PROJECTS。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcp_client import (MCPClient, resolve_kicad_cli, resolve_kicad_ext,  # noqa: E402
                        resolve_kicad_py)
from lib import auto_nc, components, erc, nets, power_rails, verify  # noqa: E402

PLACEMENT_RL = os.environ.get(
    "D2S_PLACEMENT_RL", os.path.normpath(os.path.join(HERE, "..", "..", "Placement"))
)
# KICAD_Projects 根（用户 2026-09-22/23 指令：项目文件按 <项目>/<项目>_v<N> 规范存放。
# 统一用 PCBStudioV2 顶层既有的 KiCAD_Projects/，与 EtherCAT_CAN_Converter 等历史项目同处）
PROJECTS_DIR = os.environ.get(
    "D2S_PROJECTS", os.path.normpath(os.path.join(HERE, "..", "..", "KiCAD_Projects"))
)
# Placement 的 uv venv（CLAUDE.md：uv sync 已装 kicad-access 路径依赖 ../KiCAD_Access）。
# place_rl/route_rl 统一用它跑；无 venv 时回退当前解释器 + PYTHONPATH=src（kicad_access 仍需自备）。
VENV_PY = os.path.join(PLACEMENT_RL, ".venv", "bin", "python")
RL_PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
RL_ENV = None if os.path.exists(VENV_PY) else {**os.environ, "PYTHONPATH": "src"}


def log(*a):
    print(*a, flush=True)


# ================= 版本解析 =================
def project_dir(proj: str) -> str:
    return os.path.join(PROJECTS_DIR, proj)


def existing_versions(pdir: str, proj: str) -> list[int]:
    """扫描目录内 <proj>_v<N>.kicad_sch，返回升序版本号列表。"""
    if not os.path.isdir(pdir):
        return []
    pat = re.compile(re.escape(proj) + r"_v(\d+)\.kicad_sch$")
    return sorted(int(m.group(1)) for fn in os.listdir(pdir) if (m := pat.match(fn)))


def resolve_version(pdir: str, proj: str, pinned: int | None, fresh: bool) -> int:
    """fresh=True 生成新版本（max+1，无历史则 1）；False 复用最新版本（重入语义）。
    pinned 显式指定时直接采用。"""
    if pinned:
        return pinned
    vs = existing_versions(pdir, proj)
    if fresh:
        return (vs[-1] + 1) if vs else 1
    if not vs:
        raise RuntimeError(f"{pdir} 内无 {proj}_vN.kicad_sch 可复用（先跑 sch，或 --version 指定）")
    return vs[-1]


def load_design(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ================= sch =================
def generate_schematic(client, design: dict, out_dir: str, name: str,
                       erc_gate: bool = True) -> dict:
    """设计表 → 原理图。渐进构建 report，任一步失败：报告先落盘（status=failed）
    再挂到异常 e.report 上 re-raise——失败也是可观测状态，现场留给复盘。"""
    os.makedirs(out_dir, exist_ok=True)
    sch = os.path.join(out_dir, f"{name}.kicad_sch")
    report: dict = {"status": "failed", "steps_done": []}

    def _fail(e: Exception) -> Exception:
        report["error"] = f"{type(e).__name__}: {e}"
        try:
            with open(os.path.join(out_dir, f"{name}_sch_report.json"), "w",
                      encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        e.report = report
        return e

    try:
        client.tool_soft("create_schematic", {"name": name, "path": out_dir})
        if not os.path.exists(sch):
            alt = os.path.join(out_dir, name, f"{name}.kicad_sch")
            if os.path.exists(alt):
                sch = alt
            else:
                raise RuntimeError(f"原理图未生成: {sch}")
        report["sch"] = sch

        log(f"[sch] 放件 {len(design['parts'])} …")
        report["parts_placed"] = components.place_all(client, sch, design["parts"], log)
        report["steps_done"].append("place")

        log("[sch] 连网 …")
        used, resolved = nets.connect_all(client, sch, design["conns"], log)
        report["labels"] = len(resolved)
        report["steps_done"].append("connect")

        log("[sch] PWR_FLAG …")
        used_pw, resolved_pw = nets.place_pwr_flags(
            client, sch, design.get("power", {}).get("pwr_flags", []), log)
        for ref, pins_used in used_pw.items():
            used.setdefault(ref, set()).update(pins_used)
        resolved.update(resolved_pw)
        report["steps_done"].append("pwr_flags")

        log("[sch] 自动 no-connect …")
        auto_nc.apply(client, sch, design, used, log)
        report["steps_done"].append("auto_nc")
        if os.environ.get("D2S_PRETTY_RAILS"):
            log("[sch] 电源轨道（可选） …")
            power_rails.draw_rails(client, sch, design["conns"], log)
        client.tool_soft("annotate_schematic", {"schematicPath": sch})
        client.tool_soft("snap_to_grid",
                         {"schematicPath": sch, "gridSize": 1.27,
                          "elements": ["wires", "junctions", "labels"]})

        n_err, n_warn = erc.gate(client, sch, strict=erc_gate, log=log)
        report["erc_errors"], report["erc_warnings"] = n_err, n_warn
        report["steps_done"].append("erc")

        netlist = os.path.join(out_dir, f"{name}.net")
        erc.export_netlist(client, sch, netlist)
        report["netlist"] = netlist
        # 写后回读对账（比 ERC 更强的端到端门禁）：ERC 查规则合规，这里逐 pin 核对
        # "连的就是设计表要的"。MCP tool_soft 贴标签 ok ≠ 落地，以网表读回为准。
        report["verify"] = verify.gate(
            resolved, netlist, {p["ref"] for p in design["parts"]}, log=log)
        report["steps_done"].append("verify")

        client.tool_soft("export_schematic_pdf",
                         {"schematicPath": sch,
                          "outputPath": os.path.join(out_dir, f"{name}.pdf")})
        report["status"] = "applied"
        report.pop("error", None)
        return report
    except Exception as e:
        raise _fail(e) from e


def cmd_sch(args) -> int:
    design = load_design(args.design)
    name = args.name or design["project"]["name"]
    out = args.out or project_dir(name)
    v = resolve_version(out, name, args.version, fresh=True)
    stem = f"{name}_v{v}"
    if os.path.exists(os.path.join(out, f"{stem}.kicad_sch")):
        raise RuntimeError(f"{stem}.kicad_sch 已存在——生成步不覆盖，请换 --version 或移除旧文件")
    client = MCPClient()
    try:
        log(f"[sch] MCP {client.initialize()}")
        client.wait_for_backend()
        log(f"[sch] 版本 {stem} → {out}")
        report = generate_schematic(client, design, out, stem, erc_gate=not args.no_erc_gate)
        report.update({"name": name, "version": v, "parts": len(design["parts"]),
                       "conns": len(design["conns"])})
        with open(os.path.join(out, f"{stem}_sch_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log(f"[sch] ✓ {report['sch']}")
        return 0
    except Exception as e:
        # generate_schematic 已把失败报告落盘并挂 e.report；这里补齐元数据重写一次
        log(f"[sch] ✗ {e}")
        rep = getattr(e, "report", None)
        if rep is not None:
            rep.update({"name": name, "version": v, "parts": len(design["parts"]),
                        "conns": len(design["conns"])})
            try:
                with open(os.path.join(out, f"{stem}_sch_report.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(rep, f, ensure_ascii=False, indent=1)
                log(f"[sch] 失败报告 {stem}_sch_report.json "
                    f"（steps_done={rep.get('steps_done')}）")
            except OSError:
                pass
        return 1
    finally:
        client.close()


# ================= board =================
def _board_footprint_refs(board_path: str) -> list[str]:
    """从板文件文本提取全部 footprint 的 Reference（按文件顺序）。"""
    txt = open(board_path, encoding="utf-8", errors="replace").read()
    refs = []
    for chunk in re.split(r'\(footprint "', txt)[1:]:
        m = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', chunk)
        refs.append(m.group(1) if m else "?")
    return refs


def generate_board(client, sch: str, design: dict, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.basename(sch)[: -len(".kicad_sch")]  # 版本载体 = sch 文件名
    board = os.path.join(out_dir, f"{stem}_base.kicad_pcb")
    b = design["board"]
    W, H = b["width"], b["height"]

    client.tool("create_board_from_schematic",
                {"schematicPath": sch, "boardPath": board, "overwrite": True})

    # 同步丢件硬门禁：Footprint 字段库名/封装名在库中不存在时，sync 会静默跳过该器件
    # （2026-09-22 J1/J2 丢件事故）——计数不符立即终止，绝不带着缺件板进放置。
    refs = _board_footprint_refs(board)
    want = {p["ref"] for p in design["parts"]}
    got = set(refs)
    if got != want:
        missing = sorted(want - got)
        raise RuntimeError(
            f"同步丢件门禁：设计表 {len(want)} 件，板上仅同步 {len(got & want)} 件。"
            + (f"缺失：{', '.join(missing)}。" if missing else "")
            + "通常是 parts[].footprint 的库名/封装名在 KiCad 库中不存在，sync 静默跳过。")

    client.tool("replace_board_outline",
                {"shape": "rectangle",
                 "params": {"width": W, "height": H, "x": 0, "y": 0, "unit": "mm"}})

    # 最小 .kicad_pro（部分工具链需要工程文件在场；与 sch 同 stem 共用一个工程）
    pro = os.path.join(out_dir, f"{stem}.kicad_pro")
    if not os.path.exists(pro):
        with open(pro, "w", encoding="utf-8") as f:
            json.dump({"board": {"design_settings": {}}, "meta": {"filename": pro}}, f)

    o = b.get("hole_edge_offset", 4.0)
    d = b.get("hole_d", 4.0)
    holes = []
    if b.get("mounting_holes"):
        for (x, y) in [(o, o), (W - o, o), (o, H - o), (W - o, H - o)][: b["mounting_holes"]]:
            t = client.tool("add_mounting_hole",
                            {"position": {"x": x, "y": y, "unit": "mm"}, "diameter": d})
            holes.append(t.strip().splitlines()[0][:80] if t else f"({x},{y})")
        log(f"[board] 安装孔 ×{len(holes)} Ø{d}mm @{o}mm 边距")

    if design.get("anchors"):
        moves = {a["ref"]: {"x": a["x"], "y": a["y"],
                            "rotation": a.get("rotation", 0), "unit": "mm"}
                 for a in design["anchors"]}
        client.tool("batch_move_components", {"moves": moves})
        log(f"[board] 锚点预放 ×{len(moves)}: {', '.join(moves)}")

    stackup = {
        "board_id": stem.lower().replace("-", "_"),
        "layers": [{"name": "F.Cu", "role": "signal"}, {"name": "B.Cu", "role": "signal"}],
        "aux_power_nets": [],
        "via_ladder": [[300, 600]],  # PCBRouter.md: 过孔 0.3 孔 / 0.6 外径
    }
    stackup_path = os.path.join(out_dir, f"{stem}_stackup.json")
    with open(stackup_path, "w", encoding="utf-8") as f:
        json.dump(stackup, f, indent=1)
    return {"board": board, "stackup": stackup_path, "holes": len(holes),
            "footprints": len(refs)}


def cmd_board(args) -> int:
    design = load_design(args.design)
    # 版本从 --sch 文件名推导（<项目>_vN.kicad_sch）；产物落 sch 同目录，--out 可改投他处
    out = args.out or os.path.dirname(os.path.abspath(args.sch))
    client = MCPClient()
    try:
        log(f"[board] MCP {client.initialize()}")
        client.wait_for_backend()
        report = generate_board(client, args.sch, design, out)
        log(f"[board] ✓ {report['board']}（footprints={report['footprints']}）")
        return 0
    finally:
        client.close()


# ================= chain =================

# DRC 铜级门禁：电气/铜制造硬项，须 0（与 realboard 验收标准一致：0 未布通 + 0 铜级）。
_DRC_COPPER = {"shorting", "clearance", "solder_mask_bridge", "hole_clearance",
               "hole_to_hole", "track_width", "via_dangling", "unconnected"}
# 容忍项（footprint 原生/注释级，如实报数不挡链）：丝印四类（含压板边）、
# 库副本警告、庭院/文本。
_DRC_TOLERATED = {"silk_overlap", "silk_over_copper", "silkscreen", "silk",
                  "silk_edge_clearance",
                  "lib_footprint_mismatch", "courtyard_overlap", "courtyard",
                  "text_overlap", "text", "reference", "value"}


def _run(cmd, cwd=None, env=None, log_prefix="") -> dict:
    log(f"{log_prefix}$ {' '.join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd))}")
    r = subprocess.run(cmd, cwd=cwd, env=env)
    return {"cmd": " ".join(cmd), "returncode": r.returncode, "cwd": cwd}


def cmd_chain(args) -> int:
    design = load_design(args.design)
    name = args.name or design["project"]["name"]
    out = os.path.abspath(args.out or project_dir(name))
    os.makedirs(out, exist_ok=True)
    # 工具路径三级解析（P0）：D2S_* env > ~/.harnesspcb.json > 标准探测。
    ext = resolve_kicad_ext()
    kpy = resolve_kicad_py()
    kicad_cli = resolve_kicad_cli()
    if not (ext and kpy and kicad_cli):
        log("[chain] KiCad 路径未解析（ext/kpy/cli 任一为空）。"
            "export D2S_KICAD_EXT=… 或跑 kca doctor 体检")
        return 1
    steps: list = []

    # step_from 语义：从该步起真跑，之前各步复用既有产物（不存在即失败）。
    # 从 sch 起跑 = 全新链 → 新版本号；从后续步重入 → 复用目录内最新版本。
    run_sch = args.step_from == "sch"
    run_board = args.step_from in ("sch", "board")
    run_place = args.step_from in ("sch", "board", "place")
    run_route = args.step_from in ("sch", "board", "place", "route")
    run_pour = args.step_from != "drc"

    v = resolve_version(out, name, args.version, fresh=run_sch)
    stem = f"{name}_v{v}"
    log(f"[chain] 版本 {stem} → {out}（step_from={args.step_from}）")
    sch = os.path.join(out, f"{stem}.kicad_sch")
    base = os.path.join(out, f"{stem}_base.kicad_pcb")
    placed = os.path.join(out, f"{stem}_placed.kicad_pcb")
    route_out = os.path.join(out, f"{stem}_route")
    poured = os.path.join(out, f"{stem}_routed.kicad_pcb")
    stackup = os.path.join(out, f"{stem}_stackup.json")
    if run_sch and os.path.exists(sch):
        raise RuntimeError(f"{stem}.kicad_sch 已存在——全链生成步不覆盖，请换 --version 或移除旧文件")

    planned = [s for s, run in (("sch", run_sch), ("board", run_board),
                                 ("place", run_place), ("route", run_route),
                                 ("pour", run_pour), ("drc", True)) if run]

    def merge_step_payload(kw: dict) -> dict:
        """step() 自管的键从步骤负载剥离，防 **kw 撞键 TypeError。"""
        return {k: w for k, w in kw.items()
                if k not in ("step", "ok", "status", "error", "version")}

    def step(sname, ok, status=None, **kw) -> int:
        steps.append({"step": sname, "ok": bool(ok),
                      "status": status or ("applied" if ok else "failed"),
                      "version": v, **kw})
        with open(os.path.join(out, f"{stem}_chain_report.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name, "stem": stem, "planned": planned,
                       "steps": steps}, f, ensure_ascii=False, indent=1)
        if not ok:
            log(f"[chain] ✗ {sname} 失败——链终止（报告见 {stem}_chain_report.json）")
            return 1
        return 0

    cur = {"step": "init"}

    try:
        # 1+2. sch → board（同一 MCP 会话）
        if run_sch or run_board:
            client = MCPClient()
            try:
                log(f"[chain] MCP {client.initialize()}")
                client.wait_for_backend()
                if run_sch:
                    cur["step"] = "sch"
                    r = generate_schematic(client, design, out, stem)
                    if step("sch", r["erc_errors"] == 0, **merge_step_payload(r)):
                        return 1
                elif not os.path.exists(sch):
                    return step("sch", False, error=f"复用失败，不存在 {sch}")
                else:
                    log(f"[chain] sch 复用 {sch}")
                    step("sch", True, status="reused", note=f"复用 {sch}")
                cur["step"] = "board"
                rb = generate_board(client, sch, design, out)
                if step("board", True, **merge_step_payload(rb)):
                    return 1
            finally:
                client.close()
        elif not os.path.exists(base):
            return step("board", False, error=f"复用失败，不存在 {base}")
        else:
            log(f"[chain] board 复用 {base}")
            step("board", True, status="reused", note=f"复用 {base}")

        # 3. Placement（.venv python：place_rl 依赖 kicad_access 包，仅 src 布局的
        # PYTHONPATH 不够——uv venv 里已装路径依赖；曾因 sys.executable 直跑报
        # ModuleNotFoundError: place_rl / kicad_access）
        if run_place:
            cur["step"] = "place"
            r = _run([RL_PY, "-m", "place_rl.cli", "place", sch, base,
                      "--out", placed, "--model", "policy_rl.pt", "--grid-mm", "1.27",
                      "--xy-std", "0.08", "--legalize-iters", "200", "--lam-net", "0.1",
                      "--seed", str(args.seed)], cwd=PLACEMENT_RL, env=RL_ENV,
                     log_prefix="[chain/place] ")
            if step("place", r["returncode"] == 0 and os.path.exists(placed),
                    **merge_step_payload(r)):
                return 1
        elif not os.path.exists(placed):
            return step("place", False, error=f"复用失败，不存在 {placed}")
        else:
            step("place", True, status="reused", note=f"复用 {placed}")

        # 4. Routing（阶梯 clearances；result.json 的 final 即选中的板）
        def _final_board():
            rj = os.path.join(route_out, "result.json")
            if not os.path.exists(rj):
                return None
            try:
                with open(rj, encoding="utf-8") as f:
                    fin = json.load(f).get("final")
            except (ValueError, OSError):
                return None
            return os.path.join(PLACEMENT_RL, fin) if (fin and not os.path.isabs(fin)) else (fin or None)

        if run_route:
            if not os.path.exists(stackup):
                return step("route", False, error=f"复用失败，不存在 {stackup}")
            cur["step"] = "route"
            r = _run([RL_PY, "-m", "route_rl", "--board", placed,
                      "--stackup", stackup, "--out", route_out,
                      "--passes", "signal", "--clearances", f"signal={args.clearances}",
                      "--mp", str(args.mp)], cwd=PLACEMENT_RL, env=RL_ENV,
                     log_prefix="[chain/route] ")
            final_abs = _final_board()  # 跑完重读（旧代码跑前读，fresh 链必失败）
            if step("route", r["returncode"] == 0 and bool(final_abs) and os.path.exists(final_abs),
                    final=final_abs, **merge_step_payload(r)):
                return 1
        else:
            final_abs = _final_board()
            if not (final_abs and os.path.exists(final_abs)):
                return step("route", False, error="复用失败：route/result.json 缺失或无 final 板")
            step("route", True, status="reused", final=final_abs)

        # 5. 铺铜
        if run_pour:
            cur["step"] = "pour"
            full = args.full_pads or ",".join(design.get("board", {}).get("full_pads", []))
            r = _run([kpy, os.path.join(HERE, "scripts", "pour_2layer.py"),
                      "--board", final_abs, "--out", poured]
                     + (["--full-pads", full] if full else []),
                     log_prefix="[chain/pour] ")
            if step("pour", r["returncode"] == 0 and os.path.exists(poured),
                    **merge_step_payload(r)):
                return 1
        elif not os.path.exists(poured):
            return step("pour", False, error=f"复用失败，不存在 {poured}")
        else:
            step("pour", True, status="reused", note=f"复用 {poured}")

        # 6. DRC 终判（隔离副本：kicad-cli drc --refill-zones --save-board 只写副本）
        cur["step"] = "drc"
        with tempfile.TemporaryDirectory(prefix="d2s_drc_") as td:
            copy = os.path.join(td, "check.kicad_pcb")
            shutil.copy(poured, copy)
            rpt = os.path.join(td, "drc.rpt")
            r = _run([kicad_cli, "pcb", "drc", "--refill-zones", "--save-board",
                      "--output", rpt, copy], log_prefix="[chain/drc] ")
            # ⚠ KiCad 10 kicad-cli 无顶层 drc 子命令（Failed to parse 'drc'），须 pcb drc。
            text = open(rpt, encoding="utf-8", errors="replace").read() if os.path.exists(rpt) else ""
            viol = re.findall(r"Found\s+(\d+)\s+DRC violations", text)
            unco = re.findall(r"Found\s+(\d+)\s+unconnected", text)
            n_viol = int(viol[0]) if viol else -1
            n_unco = int(unco[0]) if unco else 0
            # 违规 kind 取报告每块的 [kind]: 头（子串匹配会误配 hole_clearance⊃clearance 等）。
            by_kind: dict = {}
            for m in re.finditer(r"^\[([a-z_0-9]+)\]:", text, re.M):
                k = m.group(1)
                by_kind[k] = by_kind.get(k, 0) + 1
            n_copper = sum(v for k, v in by_kind.items() if k in _DRC_COPPER)
            unknown = {k: v for k, v in by_kind.items()
                       if k not in _DRC_COPPER and k not in _DRC_TOLERATED}
            # 铜级 0 + 未布通 0 才通过；未知 kind 一律失败（fail-closed，新检查项须人工归类）。
            ok = (n_copper == 0 and not unknown and n_unco == 0 and n_viol >= 0 and text != "")
            if ok:
                shutil.copy(copy, poured)  # 通过后拷回 refill 副本（poured 即带填充终板）
            if step("drc", ok, drc_violations=n_viol, unconnected=n_unco,
                    copper_violations=n_copper, unknown_kinds=unknown,
                    by_kind=by_kind, **merge_step_payload(r)):
                return 1

        log(f"[chain] ✓ 全链完成 → {poured}")
        return 0
    except Exception as e:
        # 异常中断（非门禁判定失败）：manifest 也必须落盘——失败是可观测状态。
        # 已记录的 step 保持原样，异常本身追加为当前步的 failed 记录（含 traceback 尾部）。
        tail = traceback.format_exc().splitlines()[-8:]
        steps.append({"step": cur.get("step", "?"), "ok": False, "status": "failed",
                      "version": v, "error": f"{type(e).__name__}: {e}",
                      "traceback_tail": tail})
        try:
            with open(os.path.join(out, f"{stem}_chain_report.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"name": name, "stem": stem, "planned": planned,
                           "steps": steps}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        log(f"[chain] ✗ 异常中断于 {cur.get('step')} 步: {e}\n" + "\n".join(tail))
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="d2s", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sch", help="设计表 → 原理图 + 网表（ERC 门禁）")
    p.add_argument("--design", "-d", required=True)
    p.add_argument("--out", "-o", default=None,
                   help="项目目录（默认 KICAD_Projects/<项目>/）")
    p.add_argument("--name", default=None)
    p.add_argument("--version", type=int, default=None,
                   help="版本号（默认自动 = 目录内 max+1）")
    p.add_argument("--no-erc-gate", action="store_true")
    p.set_defaults(fn=cmd_sch)

    p = sub.add_parser("board", help="原理图 → 板骨架（板框/安装孔/锚点预放 + stackup）")
    p.add_argument("--sch", required=True,
                   help="版本从文件名推导（<项目>_vN.kicad_sch）")
    p.add_argument("--design", "-d", required=True)
    p.add_argument("--out", "-o", default=None, help="默认 = sch 所在目录")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("chain", help="设计表 → DRC 0 终板全链编排")
    p.add_argument("--design", "-d", required=True)
    p.add_argument("--out", "-o", default=None,
                   help="项目目录（默认 KICAD_Projects/<项目>/）")
    p.add_argument("--name", default=None)
    p.add_argument("--version", type=int, default=None,
                   help="版本号（--step-from sch 默认新号，重入默认复用最新）")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--clearances", default="205,215,225,235,245",
                   help="µm 阶梯（route_rl 甜点 235 已双板验证）")
    p.add_argument("--mp", type=int, default=60)
    p.add_argument("--full-pads", default=None, help="铺铜实连焊盘 ref（默认取设计表 board.full_pads）")
    p.add_argument("--step-from", default="sch",
                   choices=["sch", "board", "place", "route", "drc"],
                   help="从某步重入（默认从头；之前的步骤复用既有产物，缺产物即报错）")
    p.set_defaults(fn=cmd_chain)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
