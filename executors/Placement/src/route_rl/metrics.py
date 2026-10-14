"""状态/结果特征提取：全部取自引擎（DRC json + layer_hist），近似不进这里
（既定裁决：近似只进掩码、不进奖励/记录）。"""
from __future__ import annotations

import re
from collections import Counter

from .env import COPPER_KINDS
from .stackup import Stackup


def _nets_of_unconnected(u: dict) -> set[str]:
    items = [it.get("description", "") for it in u.get("items", [])]
    return {m.group(1) for it in items
            for m in [re.search(r"\[([^\[\]]+)\]", it)] if m}


def _heatmap(violations: list[dict], bins: int) -> list[int]:
    """违例位置 bins×bins 网格计数（行优先展平）。

    v0 以本轮违例自身的包围盒归一（无板框 bbox 可用时的一致口径），
    缺 pos 的违例跳过。分辨率足够供 S1 的粗粒度空间模式特征。"""
    xs, ys = [], []
    for v in violations:
        for it in v.get("items", []):
            p = it.get("pos")
            if p and "x" in p and "y" in p:
                xs.append(p["x"])
                ys.append(p["y"])
    grid = [0] * (bins * bins)
    if not xs:
        return grid
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = (x1 - x0) or 1
    sy = (y1 - y0) or 1
    for x, y in zip(xs, ys):
        ix = min(int((x - x0) / sx * bins), bins - 1)
        iy = min(int((y - y0) / sy * bins), bins - 1)
        grid[iy * bins + ix] += 1
    return grid


def summarize_drc(drc: dict, stackup: Stackup, heatmap_bins: int = 16) -> dict:
    """引擎 DRC → 结构化结果。sig/pwr 口径与 legacy 脚本一致：
    - pwr_plane：两端全为平面网（power_route 的 pwr_un）
    - sig：至少一端为非电源网（signal_route_bon 的 sig）
    """
    un = drc.get("unconnected_items") or []
    sig = pwr_plane = 0
    for u in un:
        nets = _nets_of_unconnected(u)
        if nets and nets <= stackup.plane_nets:
            pwr_plane += 1
        elif nets - stackup.power_nets:
            sig += 1
    c = Counter(v.get("type", "?") for v in (drc.get("violations") or []))
    copper = sum(n for k, n in c.items() if k in COPPER_KINDS)
    return {
        "un": len(un), "sig": sig, "pwr_plane": pwr_plane, "copper": copper,
        "viol": dict(c),
        "heatmap": _heatmap(drc.get("violations") or [], heatmap_bins),
    }


def state_features(outcome: dict, layer_hist: dict | None,
                   stackup: Stackup, pass_name: str, it: int,
                   last_action: dict | None) -> dict:
    """S1 状态特征 v0（多层项：layer_hist / pass 阶段编码 / 迭代序号 / 上轮动作）。
    供热力图等完整口径见 RoundRecord；本函数是喂控制器的紧凑版。"""
    return {
        "pass": pass_name,
        "pass_code": {"signal": 0, "power": 1, "finish": 2, "planes": 3}.get(pass_name, -1),
        "iter": it,
        "sig": outcome.get("sig"), "pwr_plane": outcome.get("pwr_plane"),
        "copper": outcome.get("copper"), "un": outcome.get("un"),
        "layer_hist": layer_hist,
        "n_layers": len(stackup.layers),
        "last_action": last_action,
    }
