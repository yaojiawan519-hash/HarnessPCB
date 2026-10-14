"""领地合并（锚盘模型终相，2026-09-11）：叠加 → 互联段 → 删盘对接。

三步各是独立可审查的落盘点：

- **叠加** ``overlay_territories()``：placed 底 + pwr 落地铜 + 各领地区
  板铜。区板并行（全部从 pwr 底出发），合并 = 对各自 base 的多重集
  差分之和——无链式传染也就无链式差分。异网短路在叠加层先拦一道
  （互联段布线前的早失败）；区板缺失即拒，不提供跳过。
- **开盘** ``open_anchors()``：全锚盘（64 盘量级）落成真实封装——互联
  段的布线端点。锚盘布线期生成物语义不变：不进原理图不进 BOM。
- **删盘对接** ``drop_anchors()``：互联段全通后删锚盘封装。两端铜理应
  在盘心精确对接（FR 线起止盘心）；优化器截线到盘缘的残余（≤0.3mm）
  用盘心焊接段兜底——每锚盘把 0.35mm 内的本网端点焊回盘心，删盘后
  连通性与带盘时严格等价。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .anchors import _anchor_footprint, _top_footprint_spans
from .diff import FINE
from .model import RouteBoard, Segment, Via
from .regions import _append_blocks, _segment_block, _shorts, _via_block

_ANCHOR_LIB = "kca:AnchorPad_0_6mm"  # 锚盘封装名（开盘/删盘的识别标记）
_WELD_R = 0.35  # mm；删盘焊接半径（锚盘半径 0.3 + 截线容差）


@dataclass(frozen=True)
class OverlayResult:
    """叠加产物：全部并入铜 + 来源计数 + 落盘路径。"""

    out: Path
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]
    per_source: dict[str, tuple[int, int]]  # 来源（pwr/领地名）→ (线, 孔)


def overlay_territories(
    base: Path, out: Path, route_dir: Path, mods: list[str],
) -> OverlayResult:
    """placed 底 + pwr 落地铜 + 各领地区板铜 → 叠加板落盘。

    每块区板对**它自己的 base**（route 产板时的底）做多重集差分——区板
    并行无链式，差分也各自为政；pwr 板的 base 是 placed 本尊。缺板即
    ValueError（route 跳过的领地没有铜，合并不能替它圆场）。
    """
    base = Path(base)
    route_dir = Path(route_dir)
    base_b = RouteBoard(base)
    have = base_b.net_names()

    def _diff(src: str, board_p: Path, ref_b: RouteBoard) -> tuple[list[Segment], list[Via]]:
        if not board_p.is_file():
            raise ValueError(f"{src} 区板缺失（不提供跳过）: {board_p.name}")
        b = RouteBoard(board_p)
        segs, vs = Counter(b.segments), Counter(b.vias)
        out_s = [s for s in b.segments
                 for _ in range((segs - Counter(ref_b.segments)).get(s, 0))]
        out_v = [v for v in b.vias
                 for _ in range((vs - Counter(ref_b.vias)).get(v, 0))]
        for c in (*out_s, *out_v):
            if c.net not in have:
                raise ValueError(f"{src} 板追加铜引用网络 {c.net!r} 不在 base 板上")
        return out_s, out_v

    added: dict[str, tuple[list[Segment], list[Via]]] = {}
    pwr = route_dir / "pwr.kicad_pcb"
    if pwr.is_file():  # 无平面声明的板 route 不产 pwr（孔 0），跳过即空叠加
        added["pwr"] = _diff("pwr", pwr, base_b)
    for mod in mods:
        added[mod] = _diff(
            mod, route_dir / f"{mod}.kicad_pcb",
            RouteBoard(route_dir / f"{mod}.base.kicad_pcb"))

    # 跨板完全重复铜（同网同坐标布了两遍）：保一份，如实计数
    seen_s: dict[Segment, str] = {}
    seen_v: dict[Via, str] = {}
    dups = 0
    all_segs: list[Segment] = []
    all_vias: list[Via] = []
    for src, (segs, vs) in added.items():
        for s in segs:
            if s in seen_s:
                dups += 1
                continue
            seen_s[s] = src
            all_segs.append(s)
        for v in vs:
            if v in seen_v:
                dups += 1
                continue
            seen_v[v] = src
            all_vias.append(v)

    items = [(seen_s[c] if isinstance(c, Segment) else seen_v[c], c)
             for c in (*all_segs, *all_vias)]
    bad = _shorts(items, base_b)
    if bad:
        raise ValueError("异网短路（合并拒绝）：\n  " + "\n  ".join(bad))

    blocks = ([_segment_block(s) for s in all_segs]
              + [_via_block(v) for v in all_vias])
    Path(out).write_text(
        _append_blocks(base.read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    if dups:
        print(f"· 跨板重复铜 {dups} 条已去重（同网同坐标）")
    return OverlayResult(
        out=Path(out), segments=tuple(all_segs), vias=tuple(all_vias),
        per_source={k: (len(v[0]), len(v[1])) for k, v in added.items()},
    )


def open_anchors(board_path: Path, out: Path, anchors) -> Path:
    """叠加板 + 全锚盘（互联段端点）→ 互联布线底板。

    位号 ANC1.. 全板唯一（按 领地/网 排序保确定性）；坐标与 route 开盘
    同源（同一 plan_anchors 结果传入）。
    """
    blocks = [
        _anchor_footprint(f"ANC{i}", a)
        for i, a in enumerate(
            sorted(anchors, key=lambda x: (x.module, x.net)), 1)
    ]
    Path(out).write_text(
        _append_blocks(
            Path(board_path).read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    return Path(out)


def drop_anchors(board_path: Path, out: Path | None = None) -> tuple[int, list[Segment]]:
    """删锚盘封装 + 盘心焊接 → 终板（返回 删盘数, 焊接段）。

    每锚盘：剥封装块，把 0.35mm 内本网铜端点（区板片段 + 互联段，含
    截线到盘缘的残余）焊回盘心——删盘后连通性与带盘时严格等价。焊接
    段层别跟随被焊线段（SMD 盘 F.Cu，孔焊点落 F.Cu）。
    """
    board_path = Path(board_path)
    out = Path(out) if out is not None else board_path
    text = board_path.read_text(encoding="utf-8")

    anchors_info: list[tuple[str, float, float]] = []  # (net, cx, cy)
    keep: list[str] = []
    last = 0
    for start, end, _ref in _top_footprint_spans(text):
        block = text[start:end]
        if _ANCHOR_LIB not in block:
            continue
        m_at = re.search(
            r'\(\s*at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+\d+)?\s*\)', block)
        m_net = re.search(r'\(\s*net\s+"([^"]+)"\s*\)', block)
        if m_at is None or m_net is None:
            raise ValueError(f"锚盘块解析失败（at/net 缺失）: {block[:80]}…")
        anchors_info.append(
            (m_net.group(1), float(m_at.group(1)), float(m_at.group(2))))
        keep.append(text[last:start])
        last = end
    keep.append(text[last:])
    if not anchors_info:
        return 0, []  # 无锚盘（全 intra 板）——幂等直通

    board = RouteBoard(board_path)  # 铜（锚盘不产 segment/via，扫描不受删块影响）
    welds: list[Segment] = []
    for net, cx, cy in anchors_info:
        ends: dict[tuple[float, float], tuple[float, str]] = {}
        for s in board.segments:
            if s.net != net:
                continue
            for ex, ey in ((s.x1, s.y1), (s.x2, s.y2)):
                if (math.hypot(ex - cx, ey - cy) <= _WELD_R
                        and (round(ex, 3), round(ey, 3))
                        != (round(cx, 3), round(cy, 3))):
                    k = (round(ex, 3), round(ey, 3))
                    if k not in ends or s.width > ends[k][0]:
                        ends[k] = (s.width, s.layer)
        for v in board.vias:
            if v.net != net:
                continue
            if 1e-4 < math.hypot(v.x - cx, v.y - cy) <= _WELD_R:
                ends.setdefault(
                    (round(v.x, 3), round(v.y, 3)), (FINE.width, "F.Cu"))
        for (ex, ey), (w, lay) in sorted(ends.items()):
            welds.append(Segment(ex, ey, cx, cy, w, lay, net))

    body = "".join(keep)
    out.write_text(
        _append_blocks(body, [_segment_block(s) for s in welds]),
        encoding="utf-8",
    )
    return len(anchors_info), welds
