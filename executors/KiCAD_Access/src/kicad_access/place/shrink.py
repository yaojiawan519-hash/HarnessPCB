"""板尺寸收缩流（M3-3b）：从声明尺寸起步，质量门把关下逼近最小可用板。

板尺寸由规格说了算（spec.outline.size 是**起点**，不继承模板——参考板
的 60×60 不是圣旨）。每个候选尺寸过三道质量门，全过才算可用：

  1) floorplan 成功且 coverage 完整（器件一个不少）；
  2) 无重叠诊断（ctyd 重叠 / core 侵入均为 0）；
  3) 全部器件几何在板框内（edge_align 在窄边上会静默溢出，必须显式查）。

W、H 独立二分（另一维固定），步长 ``step``；一轮 W+H 为一个 sweep，
最多 ``max_sweeps`` 轮（后缩的一维可能腾出前维的余地，再扫一轮收敛）。
floorplan 对尺寸不严格单调，二分是受控近似——逐轮记录留痕，最终尺寸
必然过门（最终 spec 由最后一次**通过**的结果确定）。

安装孔按"就近角 + 保持角距"映射到新尺寸：孔在 spec 坐标系声明，收缩
后仍贴角（孔当前尚未落板，M4 生成 footprint 时按映射后位置放置）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .floorplan import FloorplanResult, run_floorplan
from ..ingest.ir import DesignIR
from ..kicad.footprint import FootprintGeom
from ..spec import BoardSpec, MountHole


@dataclass
class ShrinkRound:
    """一轮候选尺寸的门检记录。"""

    size: tuple[float, float]
    ok: bool
    detail: str = ""  # 失败原因（"" = 通过）

    def __str__(self) -> str:  # CLI 逐轮打印
        mark = "✓" if self.ok else "✗"
        w, h = self.size
        note = f"（{self.detail}）" if self.detail else ""
        return f"{mark} {w:g}×{h:g}mm{note}"


def map_holes(
    holes: list[MountHole], old: tuple[float, float], new: tuple[float, float]
) -> list[MountHole]:
    """孔位随板收缩贴角迁移：就近角的两向角距保持不变。

    例：80×80 板上 (3, 3)（左下角距 3,3）→ 54×52 板仍 (3, 3)；
    (57, 3)（右下，角距 23,3）→ (51, 3)。中缝孔（离两角等距）就近角
    取小者，行为确定。
    """
    ow, oh = old
    nw, nh = new
    out: list[MountHole] = []
    for hole in holes:
        ix = min(hole.pos[0], ow - hole.pos[0])
        iy = min(hole.pos[1], oh - hole.pos[1])
        x = ix if hole.pos[0] <= ow / 2 else nw - ix
        y = iy if hole.pos[1] <= oh / 2 else nh - iy
        out.append(MountHole(hole.size, (x, y)))
    return out


def _gate(
    spec: BoardSpec, ir: DesignIR, geoms: dict[str, FootprintGeom], engine
) -> tuple[bool, str, FloorplanResult | None]:
    """三道门：coverage / 重叠诊断 / 全在框内。返回 (ok, 失败原因, 结果)。"""
    try:
        # 尺寸搜索关融合重排兜底：死锁 FusionError 是「太紧」的地板信号，
        # 不能被 repack 散点式硬塞掩盖（否则板收到只剩散点、质量门拦不住）。
        res = run_floorplan(spec, ir, geoms, engine, allow_repack=False)
    except Exception as e:  # floorplan 炸 = 该尺寸不可用（如 edge 放不下）
        return False, f"floorplan 失败: {e}", None
    n, total = res.coverage
    if n != total:
        return False, f"coverage {n}/{total}", res
    if res.diagnostics:
        return False, f"{len(res.diagnostics)} 条重叠诊断", res
    w, h = spec.size
    outside = [
        ref
        for ref, p in res.placements.items()
        if (g := geoms.get(ref)) is not None
        and (
            (b := g.bbox_at(p.x, p.y, p.rot))[0] < -1e-6
            or b[1] < -1e-6
            or b[2] > w + 1e-6
            or b[3] > h + 1e-6
        )
    ]
    if outside:
        return False, f"{len(outside)} 器件出框: {sorted(outside)[:3]}", res
    return True, "", res


def _respec(
    spec: BoardSpec, size: tuple[float, float]
) -> BoardSpec:
    """候选尺寸 → 新 spec（尺寸 + 孔位贴角映射；其余逐字段原样）。"""
    return replace(
        spec,
        size=size,
        mount_holes=map_holes(spec.mount_holes, spec.size, size),
    )


def _snap(v: float, step: float) -> float:
    """候选边长吸附到 step 网格（浮点二分中点不出碎数）。"""
    return max(round(v / step) * step, step)


def _bisect_dim(
    spec: BoardSpec,
    ir: DesignIR,
    geoms: dict[str, FootprintGeom],
    engine,
    dim: int,  # 0=W, 1=H
    floor: float,
    step: float,
    rounds: list[ShrinkRound],
    cache: dict[tuple[float, float], tuple[bool, str, FloorplanResult | None]],
) -> tuple[float, FloorplanResult | None]:
    """单维二分：hi=已过门当前值，向 floor 逼近最小过门值（step 网格）。"""
    other = 1 - dim
    hi = spec.size[dim]
    lo = floor
    best: FloorplanResult | None = None

    def try_size(v: float) -> tuple[float, bool, str, FloorplanResult | None]:
        size: list[float] = [0.0, 0.0]
        size[dim], size[other] = v, spec.size[other]
        key = (round(size[0], 4), round(size[1], 4))
        if key not in cache:
            cache[key] = _gate(_respec(spec, tuple(size)), ir, geoms, engine)
            ok, detail, _ = cache[key]
            rounds.append(ShrinkRound(tuple(size), ok, detail))
        return key, *cache[key]

    key, ok, _, res_at_floor = try_size(lo)
    if ok:  # 地板尺寸都能放 → 直接到底
        return lo, res_at_floor
    while hi - lo > step:
        mid = _snap((lo + hi) / 2.0, step)
        if mid <= lo or mid >= hi:  # 网格卡死（区间内无新格点）
            break
        _, ok, _, res = try_size(mid)
        if ok:
            hi = mid
            best = res
        else:
            lo = mid
    return hi, best


def shrink_floorplan(
    spec: BoardSpec,
    ir: DesignIR,
    geoms: dict[str, FootprintGeom],
    engine,
    step: float = 0.5,
    floor: float = 20.0,
    max_sweeps: int = 3,
) -> tuple[BoardSpec, list[ShrinkRound], FloorplanResult | None]:
    """收缩主入口。起点必须过门；不过则原样返回（rounds[0] 记原因，结果为 None）。"""
    ok, detail, res = _gate(spec, ir, geoms, engine)
    rounds = [ShrinkRound(spec.size, ok, detail)]
    if not ok:
        return spec, rounds, None

    cache: dict[tuple[float, float], tuple[bool, str, FloorplanResult | None]] = {
        (round(spec.size[0], 4), round(spec.size[1], 4)): (True, "", res)
    }
    cur = spec
    for _ in range(max_sweeps):
        before = cur.size
        for dim in (0, 1):  # 先 W 后 H；后一维缩了可能腾出前一维余地
            w, r = _bisect_dim(cur, ir, geoms, engine, dim, floor, step, rounds, cache)
            if w < cur.size[dim] - 1e-9:
                size = list(cur.size)
                size[dim] = w
                cur = _respec(cur, tuple(size))
        if cur.size == before:  # 本 sweep 两维都没动 → 收敛
            break
    # 终点组合在最后一维二分里是联合过门的；重跑拿权威结果（含 placements）
    ok, detail, final = _gate(cur, ir, geoms, engine)
    if not ok:  # 理论不可达（非单调异常）：回退起点，rounds 留痕
        rounds.append(ShrinkRound(cur.size, False, f"终点复检失败回退: {detail}"))
        return spec, rounds, res
    return cur, rounds, final
