"""M6 闭环布线评估——DesignDocument §3.7 / README M6。

把「只看 HPWL」升级为「真的布一遍线」：放置完成 → 导出 DSN → FreeRouting
（无头 jar）→ SES 回读 → 统计**后布线线长、过孔数、DRC 违规数、布通率、
重叠率**。这套指标既用于最终验收，也是奖励中可布线性项 Ĉ_t 的地面真相。

外部工具链（kicad-cli / KiCad 内置 Python / java / freerouting.jar）路径解析
复用 kicad_access 的 tools 门禁，均支持环境变量覆盖，未挂载时显式降级报错
而非静默失败。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kicad_access import tools as hws_tools

from .policy.place import PlaceConfig, place_real_board

__all__ = ["EvalConfig", "route_board", "post_route_metrics", "eval_real_board"]

# 与 place_real_board 的固定件/封装一致：EvalConfig 只透传放置参数。
_KICAD_OP = Path(__file__).with_name("_pcbnew_ops.py")

# DRC 违规分类：铜级（电气，须为 0）vs 装饰级/库级（与放置无关，仅报告）。
_COPPER_KINDS = {"shorting_items", "clearance", "solder_mask_bridge",
                 "hole_clearance", "track_width", "via_dangling", "unconnected"}


@dataclass
class EvalConfig:
    model: str | Path = "policy_rl.pt"
    hidden: int = 128
    layers: int = 3
    grid_mm: float = 1.27
    xy_std: float = 0.08
    legalize_iters: int = 200
    lam_net: float = 0.1
    seed: int = 0
    led_edge: bool = False      # 原则②LED 贴边（透传 PlaceConfig，默认关）
    iface_edge: bool = False    # 原则①接口贴边（透传 PlaceConfig，默认关）
    lam_edge_ic: float = 0.0    # 原则①支撑 IC 贴边（默认 0=关）
    lam_ic_spread: float = 0.0  # 原则⑥主芯片均匀（默认 0=关）
    lam_decoup_dedicated: float = 0.0  # 专属轨去耦电容单独系数（默认 0=沿用 lam_decoup）
    lam_decoup: float | None = None     # 共享轨去耦电容系数覆盖（None=沿用 0.15）
    max_passes: int = 40        # FreeRouting 单次 --max-passes
    attempts: int = 1           # best-of-N（取未布通最少的一次）
    timeout_s: float = 600.0
    keep_artifacts: bool = False
    out_dir: str | Path | None = None
    routing_clearance_mm: float = 0.245  # 布线 clearance（mm）：抬到板规 0.2mm 之上，
        # FreeRouting 贴边挤压会产出 0.177–0.199mm 的临界 clearance 违规；0.245mm 留
        # 0.045mm 裕量，DRC 仍按板规 0.2mm 判、安全通过。探针（同板扫 210–250µm，
        # 每值复跑，两天各重扫）确定 0.245 为甜点：0.245 两天都稳在 0 未布通 + 0 铜级，
        # 0.24 悬在 clearance 悬崖边随机器状态翻车（09-17 0、09-18 3），0.25 起
        # 可布线性悬崖（40 未布通，可复现）。0 = 不抬（沿用 DSN 原值）。


def _require_tool(name: str) -> hws_tools.ToolStatus:
    st = {"kicad-cli": hws_tools.kicad_cli_status,
          "kicad-python": hws_tools.kicad_python_status,
          "java": hws_tools.java_status,
          "freerouting": hws_tools.freerouting_status}[name]()
    if not st.ok or not st.path:
        raise RuntimeError(f"工具 {name} 不可用：{st.detail}  {st.hint}")
    return st


def _pcbnew(op: str, *args: str) -> str:
    """用 KiCad 内置 Python 跑 _pcbnew_ops.py，返回 stdout 中的 JSON 对象。

    pcbnew/SWIG 会往 stdout 掺内存泄漏提示（BOX2 无析构），故按首个 ``{`` 到
    末尾 ``}`` 截取 JSON，而不是整段 parse。
    """
    py = _require_tool("kicad-python").path
    r = subprocess.run([str(py), str(_KICAD_OP), op, *args],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"pcbnew {op} 失败:\n{r.stderr[-2000:]}")
    out = r.stdout
    i, j = out.find("{"), out.rfind("}")
    return out[i:j + 1] if i >= 0 and j > i else out.strip()


def route_board(
    placed_pcb: str | Path,
    out_pcb: str | Path,
    *,
    max_passes: int = 40,
    timeout_s: float = 600.0,
    attempts: int = 1,
    clearance_um: int = 0,
    workdir: str | Path | None = None,
) -> dict:
    """DSN 导出 → FreeRouting（best-of-N）→ SES 回读 → 布线板。

    FreeRouting 无头 jar 单次结果对放置扰动敏感（偶尔产出零长残段短路或漏布一条
    网），故 ``attempts>1`` 时跑多次（每次不同随机种子）、按 (未布通, 铜级违规)
    字典序取最优一次，再回读其 SES 产最终布线板——让布线质量对放置方差鲁棒，
    而非靠不断回调块内聚系数去迁就单次布线的偶然性。返回 (布线板路径, 诊断)。
    """
    placed_pcb, out_pcb = Path(placed_pcb), Path(out_pcb)
    java = _require_tool("java").path
    fr = _require_tool("freerouting").path
    attempts = max(1, int(attempts))

    with tempfile.TemporaryDirectory(prefix="plr_eval_") as td:
        td = Path(td)
        dsn = td / "in.dsn"
        _pcbnew("export_dsn", str(placed_pcb), str(dsn), str(clearance_um))
        if workdir is not None:
            workdir = Path(workdir); workdir.mkdir(parents=True, exist_ok=True)
            shutil.copy(dsn, workdir / "route.dsn")

        best: tuple | None = None  # (score, ses, routed_tmp, cleanup, log)
        last_err = ""
        for attempt in range(attempts):
            ses = td / f"out_{attempt}.ses"
            cmd = [str(java), "-Djava.awt.headless=true", "-jar", str(fr),
                   "-de", str(dsn), "-do", str(ses),
                   "-mp", str(max_passes), "-mt", "1"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"FreeRouting 超时（>{timeout_s:.0f}s）") from e
            log = r.stdout.splitlines()[-1] if r.stdout.splitlines() else ""
            if not ses.is_file() or ses.stat().st_size == 0:
                last_err = "\n".join((r.stderr or r.stdout or "").splitlines()[-15:])
                continue

            # SES 回读到中间板，再做清理（加宽窄线 + 剪悬空过孔）产出候选布线板
            routed_raw = td / f"routed_raw_{attempt}.kicad_pcb"
            routed_tmp = td / f"routed_{attempt}.kicad_pcb"
            _pcbnew("import_ses", str(ses), str(placed_pcb), str(routed_raw))
            cl = json.loads(_pcbnew("cleanup", str(routed_raw), str(routed_tmp)))
            if attempts == 1:
                best = ((0, 0), ses, routed_tmp, cl, log)
                break
            # best-of-N：按 (未布通, 铜级违规) 字典序选最优（未布通是硬门槛，
            # 其次才看短路/clearance 等铜级违规）。
            m = post_route_metrics(routed_tmp)
            score = (m["unconnected_items"], m["drone_copper"])
            if best is None or score < best[0]:
                best = (score, ses, routed_tmp, cl, log)

        if best is None:
            raise RuntimeError(
                f"FreeRouting 未产出有效 SES（{attempts} 次尝试均失败）：\n{last_err}")
        if workdir is not None:
            shutil.copy(best[1], workdir / "route.ses")
        shutil.copy(best[2], out_pcb)
    return {"routed_pcb": str(out_pcb), "router_log": best[4],
            "cleanup": best[3], "n_attempts": attempts}


def post_route_metrics(routed_pcb: str | Path) -> dict:
    """后布线指标：线长 / 过孔 / 布通率 / DRC 分类 / 重叠。

    布线板几何统计走 pcbnew（线长/孔/线宽直方）；DRC 走 kicad-cli（权威），
    按「铜级（须 0）/ 装饰级 / 库级」分类汇总。
    """
    routed_pcb = Path(routed_pcb)
    geo = json.loads(_pcbnew("stats", str(routed_pcb)))

    cli = _require_tool("kicad-cli").path
    with tempfile.TemporaryDirectory(prefix="plr_drc_") as td:
        rep = Path(td) / "drc.json"
        subprocess.run([str(cli), "pcb", "drc", "--format", "json",
                        "-o", str(rep), str(routed_pcb)],
                       capture_output=True, text=True, timeout=300, check=True)
        drc = json.loads(rep.read_text())

    viols = drc.get("violations") or []
    counts: dict[str, int] = {}
    for v in viols:
        counts[v.get("type", "?")] = counts.get(v.get("type", "?"), 0) + 1
    unconnected = len(drc.get("unconnected_items") or [])

    copper = sum(n for k, n in counts.items() if k in _COPPER_KINDS)
    silk = sum(n for k, n in counts.items()
               if k.startswith("silk") or k in ("silk_overlap", "silk_over_copper"))
    lib = sum(n for k, n in counts.items()
              if k.startswith("lib_") or k in ("lib_footprint_mismatch",
                                               "lib_footprint_issues"))

    return {
        "wirelength_mm": geo["wirelength_mm"],
        "n_vias": geo["n_vias"],
        "n_traces": geo["n_traces"],
        "track_width_mm": geo["track_width_mm"],
        "unconnected_items": unconnected,
        "completion": 1.0 if unconnected == 0 else 0.0,
        "drone_total": len(viols),
        "drone_copper": copper,
        "drone_silk": silk,
        "drone_lib": lib,
        "drone_breakdown": counts,
    }


def eval_real_board(
    sch: str | Path,
    pcb: str | Path,
    out_dir: str | Path,
    cfg: EvalConfig | None = None,
    fixed: set[str] | None = None,
) -> dict:
    """真实板闭环：策略放置 → 布线 → 后布线指标。

    产出三块板到 out_dir：placed-unrouted、routed、以及放置/布线诊断。
    返回 dict 汇总（放置 HPWL/重叠 + 后布线线长/孔/布通率/DRC）。
    """
    cfg = cfg or EvalConfig()
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    placed = out_dir / "placed.kicad_pcb"
    routed = out_dir / "routed.kicad_pcb"

    pcfg = PlaceConfig(grid_mm=cfg.grid_mm, xy_std=cfg.xy_std, hidden=cfg.hidden,
                       gnn_layers=cfg.layers, legalize_iters=cfg.legalize_iters,
                       lam_net=cfg.lam_net, seed=cfg.seed, led_edge=cfg.led_edge,
                       iface_edge=cfg.iface_edge,
                       lam_edge_ic=cfg.lam_edge_ic, lam_ic_spread=cfg.lam_ic_spread,
                       lam_decoup_dedicated=cfg.lam_decoup_dedicated,
                       lam_decoup=cfg.lam_decoup)
    d = place_real_board(sch, pcb, placed, cfg.model, pcfg, fixed=fixed)

    rt = route_board(placed, routed, max_passes=cfg.max_passes,
                     attempts=cfg.attempts, timeout_s=cfg.timeout_s,
                     clearance_um=int(round(cfg.routing_clearance_mm * 1000)),
                     workdir=out_dir if cfg.keep_artifacts else None)
    m = post_route_metrics(routed)

    return {
        "placed": str(placed),
        "routed": str(routed),
        "board": d["board"],
        "n_total": d["n_total"],
        "n_movable": d["n_movable"],
        "n_fixed": d["n_fixed"],
        "hpwl_placed": d["hpwl_final"],
        "overlap_placed": d["overlap_final"],
        "bounds_placed": d["bounds_final"],
        "cleanup": rt["cleanup"],
        **m,
    }
