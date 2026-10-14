"""意图驱动的边沿接口放置（通用引擎机制，非单板脚本）。

把「RJ45 放上边靠拢、电源端子放上边右侧、JTAG 左边、按键右边、CAN 端子下边、
全部开口朝外、凸出板外 1mm」这类人类放置意图表达为逐件 JSON spec
（``{"edge": "top", "along": 0.2}``），引擎补齐三件通用几何事：

1. **开口朝外**：连接器焊盘在内侧、开口（插线孔/插座口）在外侧。三级信号：

   - 焊盘质心相对外框中心的偏移决定性（明显且轴向，如 RJ45 偏 3.2mm）→
     四旋转里取开口方向与板边外法向最对齐者，纯几何、与模板无关；
   - 对称焊盘（端子排/轻触按钮质心≈体心）但模板里贴边安放 → 从模板样板
     推断开口方向（朝向是器件类型属性：模板把开口朝板外，换目标边照样
     转对）；
   - 真无信号（模板居中的对称件，如板中排针）→ 沿用模板 rot，如实上报。

2. **凸出钳位**：外框外沿凸出板边 ``protrude_mm``；焊盘至少 ``pad_clear_mm``
   留在板内，余量不足时钳小（排针/按钮近齐平，RJ45/端子开口侧无铜可真凸）。
3. **沿边位置**：``along``∈[0,1] = 外框中心沿边比例位置（软意图）；同边意图件
   与边带内固定障碍（安装孔）按庭院盒沿边推开 ``min_gap_mm``，互不重叠。

产物 = footprint 原点系位姿 ``(ox, oy, rot)``（与 ``place_real_board`` 的
``fixed_local`` 同约定），在 ``place_real_board`` 里并入锁定集：意图件位姿是
**设计输入**，其余器件仍走 sch 驱动独立放置。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..env.problem import _rot_off

# y-down 本地系（0,0)-(W,H) 的板边外法向
_EDGE_NORMAL = {
    "top": (0.0, -1.0),
    "bottom": (0.0, 1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
}

@dataclass
class IntentItem:
    edge: str  # top / bottom / left / right
    along: float  # 外框中心沿边比例位置（0=左/上端，1=右/下端）
    rot: float | None = None  # 显式朝向覆盖（高级用法；缺省走三级信号）


@dataclass
class PowerClusterSpec:
    """电源簇意图块：「电源元器件放置在电源输入端子的下方」。"""
    anchor: str              # 锚端子位号（意图件最优——边沿权威）
    depth_mm: float = 20.0   # 端子内沿 → 板内纵深
    margin_mm: float = 6.0   # 切向（沿边）两侧外扩
    gap_mm: float = 1.0      # 端子内沿与带起始的间隙


@dataclass
class IntentSpec:
    items: dict[str, IntentItem] = field(default_factory=dict)
    protrude_mm: float = 1.0  # 外框外沿凸出板边量（意图值，焊盘安全优先）
    pad_clear_mm: float = 0.3  # 焊盘至少留在板内的余量
    min_gap_mm: float = 0.25  # 同边庭院盒最小间隙
    obstacle_band_mm: float = 12.0  # 距板边该带内的固定障碍参与沿边避让
    power_cluster: PowerClusterSpec | None = None  # 电源簇块（默认 None=关）


def load_intent(path: str | Path) -> IntentSpec:
    """JSON 意图 spec → IntentSpec。

    格式::

        {"protrude_mm": 1.0, "pad_clear_mm": 0.3,
         "items": {"J2": {"edge": "top", "along": 0.2}, ...},
         "power_cluster": {"anchor": "J1", "depth_mm": 20.0}}

    ``power_cluster``（可选）= 电源元器件聚集到锚端子「下方」（朝板内一侧）
    的矩形带，成员判定与锚定见 ``env.problem.PowerCluster`` 一节。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items: dict[str, IntentItem] = {}
    for ref, it in data.get("items", {}).items():
        items[ref] = IntentItem(
            edge=it["edge"], along=float(it.get("along", 0.5)),
            rot=float(it["rot"]) if it.get("rot") is not None else None,
        )
    pcd = data.get("power_cluster")
    power_cluster = None
    if isinstance(pcd, dict) and pcd.get("anchor"):
        power_cluster = PowerClusterSpec(
            anchor=str(pcd["anchor"]),
            depth_mm=float(pcd.get("depth_mm", 20.0)),
            margin_mm=float(pcd.get("margin_mm", 6.0)),
            gap_mm=float(pcd.get("gap_mm", 1.0)),
        )
    return IntentSpec(
        items=items,
        protrude_mm=float(data.get("protrude_mm", 1.0)),
        pad_clear_mm=float(data.get("pad_clear_mm", 0.3)),
        min_gap_mm=float(data.get("min_gap_mm", 0.25)),
        obstacle_band_mm=float(data.get("obstacle_band_mm", 12.0)),
        power_cluster=power_cluster,
    )


def opening_dir(g) -> tuple[float, float] | None:
    """焊盘质心信号：开口方向（轴向单位向量），对称/模糊返回 None。

    开口一侧没有焊盘：``开口 = 外框中心 − 焊盘质心``。要求偏移明显
    （≥0.5mm）且轴向（次要分量 ≤ 主要分量 ×0.5），否则不猜。
    """
    if not g.pads:
        return None
    ob = g.outline()
    pcx = sum(p.x for p in g.pads) / len(g.pads)
    pcy = sum(p.y for p in g.pads) / len(g.pads)
    vx = (ob[0] + ob[2]) / 2 - pcx
    vy = (ob[1] + ob[3]) / 2 - pcy
    if abs(vx) < 0.5 and abs(vy) < 0.5:
        return None
    if abs(vx) >= abs(vy):
        if abs(vy) > 0.5 * abs(vx):
            return None
        return (math.copysign(1.0, vx), 0.0)
    if abs(vx) > 0.5 * abs(vy):
        return None
    return (0.0, math.copysign(1.0, vy))


def _template_edge(g, ox: float, oy: float, rot: float, W: float, H: float,
                   band: float) -> str | None:
    """模板位姿贴的最近板边（外框距边 < band 才算贴边安放），否则 None。"""
    x0, y0, x1, y1 = g.bbox_at(ox, oy, rot)
    d = {"top": y0, "bottom": H - y1, "left": x0, "right": W - x1}
    edge = min(d, key=d.get)
    return edge if d[edge] < band else None


def face_out_rot(g, tmpl_ox: float, tmpl_oy: float, tmpl_rot: float,
                 edge: str, W: float, H: float,
                 band: float = 3.0) -> tuple[float, str]:
    """三级信号选朝外 rot：返回 (rot, 信号名)。

    ``tmpl_*`` 必须是**本地系**位姿（板框左下为原点，与 W/H 同尺度）。
    pad-centroid   焊盘质心决定性，纯几何选四旋转最对齐板边外法向者；
    template-edge  对称焊盘但模板贴边安放 → 样板开口方向转对到目标边；
    template-rot   真无信号（模板居中的对称件）→ 沿用模板 rot。
    """
    od = opening_dir(g)
    sig = "pad-centroid"
    if od is None:
        te = _template_edge(g, tmpl_ox, tmpl_oy, tmpl_rot, W, H, band)
        if te is None:
            return (tmpl_rot, "template-rot")
        # 样板把开口朝板外 → 该外法向是**板系**开口方向； footprint 本地系
        # 还要撤销样板自身的旋转（否则差一个 tmpl_rot，模板非 0 旋转的件会放反）。
        nx, ny = _EDGE_NORMAL[te]
        od = _rot_off(nx, ny, -tmpl_rot)
        sig = "template-edge"
    n = _EDGE_NORMAL[edge]
    best_rot, best = 0.0, -2.0
    for rot in (0.0, 90.0, 180.0, 270.0):
        rx, ry = _rot_off(od[0], od[1], rot)
        score = rx * n[0] + ry * n[1]
        if score > best:
            best_rot, best = rot, score
    return (best_rot, sig)


def _pad_box_at(g, rot: float):
    """放置态（原点在 0,0）焊盘包围盒（含焊盘半宽；rot 奇数 90° 时 w/h 互换）。"""
    xs: list[float] = []
    ys: list[float] = []
    for p in g.pads:
        ax, ay = _rot_off(p.x, p.y, rot)
        hw, hh = (p.w / 2, p.h / 2) if rot % 180 == 0 else (p.h / 2, p.w / 2)
        xs += [ax - hw, ax + hw]
        ys += [ay - hh, ay + hh]
    return min(xs), min(ys), max(xs), max(ys)


def edge_poses(spec: IntentSpec, by_ref, libs, W: float, H: float,
               obstacles: list[tuple[str, tuple[float, float, float, float]]] | None = None,
               origin: tuple[float, float] = (0.0, 0.0),
               ) -> tuple[dict[str, tuple[float, float, float]], list[dict]]:
    """意图 spec → 原点系位姿 + 逐件报告。

    ``by_ref``/``libs`` 与 place_real_board 同源（板上 footprint + 封装库，
    by_ref 位姿为**板绝对系**）；``origin`` = 板框左下绝对坐标，用于把模板
    位姿换到与 W/H 同尺度的本地系再判贴边。``obstacles`` = 边带内不动的
    固定障碍盒（安装孔等，本地原点系 AABB，不含意图件自身）。
    返回 ``(poses, report)``：poses 可直接并入 fixed_local。
    """
    obstacles = obstacles or []
    ox0, oy0 = origin
    poses: dict[str, tuple[float, float, float]] = {}
    report: list[dict] = []
    # 逐件算位姿（凸出钳位 + 朝外选 rot）
    placed: dict[str, dict] = {}
    for ref in sorted(spec.items):
        it = spec.items[ref]
        f = by_ref.get(ref)
        if f is None:
            report.append({"ref": ref, "error": "板上无此位号"})
            continue
        g = libs.load(f.name) if f.name else None
        if g is None or g.outline() == (0.0, 0.0, 0.0, 0.0):
            report.append({"ref": ref, "error": f"几何缺失 {f.name}"})
            continue
        rot, signal = face_out_rot(g, f.x - ox0, f.y - oy0, f.rot, it.edge, W, H)
        if it.rot is not None:
            rot, signal = it.rot, "explicit"
        x0, y0, x1, y1 = g.bbox_at(0.0, 0.0, rot)
        bcx, bcy = (x0 + x1) / 2, (y0 + y1) / 2  # AABB 中心相对原点偏移
        hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        # 焊盘安全钳位：开口侧外框外沿 → 最近焊盘边的余量（正=焊盘在外框内）
        px0, py0, px1, py1 = _pad_box_at(g, rot)
        n = _EDGE_NORMAL[it.edge]
        margin = {
            (0.0, -1.0): py0 - y0, (0.0, 1.0): y1 - py1,
            (-1.0, 0.0): px0 - x0, (1.0, 0.0): x1 - px1,
        }[n]
        protr = min(spec.protrude_mm, max(0.0, margin - spec.pad_clear_mm))
        clamp = "none" if protr >= spec.protrude_mm - 1e-9 else "pad-safety"
        # 外框中心（意图 along 是软建议，先按比例落位）
        if it.edge in ("top", "bottom"):
            cx = it.along * W
            cy = (-protr + hh) if it.edge == "top" else (H + protr - hh)
        else:
            cx = (-protr + hw) if it.edge == "left" else (W + protr - hw)
            cy = it.along * H
        placed[ref] = {
            "item": it, "geom": g, "rot": rot, "signal": signal,
            "hw": hw, "hh": hh, "cx": cx, "cy": cy, "protr": protr,
            "clamp": clamp, "margin": margin,
        }
    # 沿边推开：同边意图件 + 边带内障碍，庭院盒互不重叠（along 软意图）
    _separate_tangential(placed, spec, W, H, obstacles)
    for ref, p in placed.items():
        ox = p["cx"] - _bbox_center_off(p["geom"], p["rot"])[0]
        oy = p["cy"] - _bbox_center_off(p["geom"], p["rot"])[1]
        poses[ref] = (ox, oy, p["rot"])
        it = p["item"]
        along_f = p["cx"] / W if it.edge in ("top", "bottom") else p["cy"] / H
        report.append({
            "ref": ref, "edge": it.edge, "along_spec": it.along,
            "along_final": round(along_f, 4), "rot": p["rot"],
            "rot_signal": p["signal"], "opening": opening_dir(p["geom"]) or
            ("sample" if p["signal"] == "template-edge" else None),
            "protrude_spec": spec.protrude_mm, "protrude_eff": round(p["protr"], 3),
            "clamp": p["clamp"], "pad_margin": round(p["margin"], 3),
            "warn": p.get("warn"),
        })
    return poses, report


def _bbox_center_off(g, rot: float) -> tuple[float, float]:
    """AABB 中心相对 footprint 原点的偏移（90° 整数倍旋转下 = 旋转后的外框中心）。"""
    x0, y0, x1, y1 = g.bbox_at(0.0, 0.0, rot)
    return (x0 + x1) / 2, (y0 + y1) / 2


def _separate_tangential(placed: dict, spec: IntentSpec, W: float, H: float,
                         obstacles) -> None:
    """同边沿边推开（多轮直至无重叠或 24 轮）：意图件互让、对障碍让障碍。"""
    for edge in ("top", "bottom", "left", "right"):
        L = W if edge in ("top", "bottom") else H
        entries = []  # (lo, hi, movable, key)
        for ref, p in placed.items():
            if p["item"].edge != edge:
                continue
            half = p["hw"] if edge in ("top", "bottom") else p["hh"]
            c = p["cx"] if edge in ("top", "bottom") else p["cy"]
            entries.append([c - half, c + half, True, ref])
        # 边带内障碍（安装孔等）不动
        for oref, (ox0, oy0, ox1, oy1) in obstacles:
            near = {"top": oy0, "bottom": H - oy1, "left": ox0, "right": W - ox1}[edge]
            if near >= spec.obstacle_band_mm:
                continue
            lo, hi = (ox0, ox1) if edge in ("top", "bottom") else (oy0, oy1)
            entries.append([lo, hi, False, oref])
        if not entries:
            continue
        for _ in range(24):
            entries.sort(key=lambda e: e[0])
            moved = False
            for a, b in zip(entries, entries[1:]):
                need = a[1] + spec.min_gap_mm - b[0]
                if need <= 1e-6:
                    continue
                moved = True
                # 推开 = 整盒平移（lo/hi 同移），盒宽不变
                if a[2] and b[2]:
                    a[0] -= need / 2; a[1] -= need / 2
                    b[0] += need / 2; b[1] += need / 2
                elif a[2]:
                    a[0] -= need; a[1] -= need
                elif b[2]:
                    b[0] += need; b[1] += need
                else:
                    continue  # 障碍互叠不是这里的事
            if not moved:
                break
        else:
            for ref, p in placed.items():  # 24 轮仍有叠：如实上报
                if p["item"].edge == edge and p.get("warn") is None:
                    p["warn"] = "沿边空间不足，24 轮推开未收敛"
        # 回写中心并钳回板内（切向不出板）
        for e in entries:
            if not e[2]:
                continue
            c = (e[0] + e[1]) / 2
            half = (e[1] - e[0]) / 2
            c = min(max(c, half), L - half)
            p = placed[e[3]]
            if edge in ("top", "bottom"):
                p["cx"] = c
            else:
                p["cy"] = c


def power_cluster_region(pc: PowerClusterSpec, spec: IntentSpec, by_ref, libs,
                         W: float, H: float,
                         anchor_pose: tuple[float, float, float],
                         ) -> tuple[float, float, float, float] | None:
    """锚端子外框 → 沿**板内侧**法向挤出的簇带矩形（本地系）。

    「下方」的几何本质 = 朝板内一侧：top 边端子 → 带在其 +y（板内，屏幕
    「下方」），bottom/left/right 边端子同理取各自板内侧——换边自动成立，
    不写死任何边。锚是意图件时边沿取 spec 的（权威）；否则按外框最近板边
    推断。切向按 ``margin_mm`` 两侧外扩，法向 ``gap_mm`` 起深 ``depth_mm``；
    钳回板内，空/退化带返回 None（调用方如实上报、簇不建立）。
    """
    f = by_ref.get(pc.anchor)
    if f is None:
        return None
    g = libs.load(f.name) if f.name else None
    if g is None or g.outline() == (0.0, 0.0, 0.0, 0.0):
        return None
    ax, ay, arot = anchor_pose
    x0, y0, x1, y1 = g.bbox_at(ax, ay, arot)
    it = spec.items.get(pc.anchor)
    if it is not None:
        edge = it.edge
    else:  # 非意图件锚：外框最近板边
        d = {"top": y0, "bottom": H - y1, "left": x0, "right": W - x1}
        edge = min(d, key=d.get)
    if edge in ("top", "bottom"):
        rx0, rx1 = x0 - pc.margin_mm, x1 + pc.margin_mm
        if edge == "top":    # 板内 = +y：带在端子「下方」
            ry0, ry1 = y1 + pc.gap_mm, y1 + pc.gap_mm + pc.depth_mm
        else:                # bottom：板内 = -y
            ry0, ry1 = y0 - pc.gap_mm - pc.depth_mm, y0 - pc.gap_mm
    else:
        ry0, ry1 = y0 - pc.margin_mm, y1 + pc.margin_mm
        if edge == "left":   # 板内 = +x
            rx0, rx1 = x1 + pc.gap_mm, x1 + pc.gap_mm + pc.depth_mm
        else:                # right：板内 = -x
            rx0, rx1 = x0 - pc.gap_mm - pc.depth_mm, x0 - pc.gap_mm
    rx0, rx1 = max(rx0, 0.0), min(rx1, W)  # 钳回板内
    ry0, ry1 = max(ry0, 0.0), min(ry1, H)
    if rx1 - rx0 < 1e-6 or ry1 - ry0 < 1e-6:
        return None
    return (rx0, ry0, rx1, ry1)
