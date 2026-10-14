"""领地 + 锚盘规划（锚盘布线模型，2026-09-11 拍板）。

替代"planes/system 也是区"的净类分区域——merge 单位从网络组回归领地：

- **领地** = 模块在 placed 板上的焊盘 bbox + 边距。区板只布本模块的
  intra 网（差分/电源树策略作为区内算法分派不变，五类分派降级为区板
  内部调度，不再是 merge 单位）；
- **跨区网**（kind=system）不进任何区板：每个参与领地在自己领地边界
  上开一个**锚盘**（单盘空元件，布线期生成物，不进原理图不进 BOM），
  区内布到锚盘为止，互联段（全网表 + 冻结局铜）布锚盘到锚盘；
- **电源/地走平面层**（gnd_plane/power_plane），planes 不是区是背景
  阶段；降孔归各区自己的局部动作；
- 锚盘**动态可调**：区板彼此独立（无链式传染），挪盘只重开一块区板；
  最终合并删盘，两端铜在盘心对接即连通。

锚盘落位 v1 启发式（纯几何确定性，不依赖布线器）：本网在本领地引脚
质心 → 朝对端引脚质心方向作射线，与领地 bbox 的交点即锚位；同边
锚盘沿边排开保间距；离最近焊盘净距不足沿边滑动避让。
"""

from __future__ import annotations

import re
import uuid as _uuid_mod
from dataclasses import dataclass, replace
from pathlib import Path

from .model import RouteBoard
from .regions import _append_blocks, _f

MARGIN = 2.0  # 领地 = 模块焊盘 bbox 外扩边距（mm）
GAP = 0.8     # 同边锚盘最小间距（mm）
CLEAR = 0.45  # 锚盘心到最近焊盘铜缘净距（mm），锚盘半径 0.3 已含
_R_ANCHOR = 0.3  # 锚盘半径（0.6mm 方盘）


@dataclass(frozen=True)
class Anchor:
    """跨区网在领地边界上的锚点：区板内该网的终点 = 互联段该网的起点。"""

    net: str
    module: str
    x: float
    y: float
    edge: str  # 领地 bbox 退出边："top"|"bottom"|"left"|"right"（y-down）


def territories(
    ir, board: RouteBoard, *, margin: float = MARGIN,
) -> dict[str, tuple[float, float, float, float]]:
    """模块 → 领地 bbox（板上焊盘 bbox + 边距）；无板上焊盘的模块不产领地。"""
    by_mod: dict[str, list[tuple[float, float]]] = {}
    for p in board.pads:
        dev = ir.devices.get(p.ref)
        if dev is not None and dev.module:
            by_mod.setdefault(dev.module, []).append((p.x, p.y))
    out: dict[str, tuple[float, float, float, float]] = {}
    for mod, pts in sorted(by_mod.items()):
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        out[mod] = (min(xs) - margin, min(ys) - margin,
                    max(xs) + margin, max(ys) + margin)
    return out


def plan_anchors(
    ir, board: RouteBoard,
    terrs: dict[str, tuple[float, float, float, float]],
) -> tuple[list[Anchor], list[str]]:
    """全部跨区网（kind=system）→ 锚盘表 + 警告清单（避让失败/退化情形）。

    每 (网, 领地) 恰一盘：网在本领地的引脚质心朝对端引脚质心方向作
    射线，交领地 bbox 于边界即锚位。无领地器件上的引脚（根页连接器
    等）只参与方向计算，不开盘——互联段直接以其焊盘为端点（后续增量
    实装）。返回 (anchors, warnings)。
    """
    warns: list[str] = []
    pad_at = {(p.ref, p.num): p for p in board.pads}
    anchors: list[Anchor] = []

    for n in (x for x in ir.nets if x.kind == "system"):
        by_mod: dict[str, list[tuple[float, float]]] = {}
        stray = 0
        for pin in n.pins:
            p = pad_at.get((pin.ref, pin.pin))
            dev = ir.devices.get(pin.ref)
            if p is None or dev is None or not dev.module:
                stray += 1
                continue
            by_mod.setdefault(dev.module, []).append((p.x, p.y))
        grounded = {m: pts for m, pts in by_mod.items() if m in terrs}
        if len(grounded) < 2:
            warns.append(f"{n.name}: 有效领地 {len(grounded)} < 2（无锚盘，互联段直连）")
            continue
        for mod, own_pts in sorted(grounded.items()):
            others = [pt for m, pts in grounded.items() if m != mod for pt in pts]
            own, par = _centroid(own_pts), _centroid(others)
            d = (par[0] - own[0], par[1] - own[1])  # 方向 = 对端质心 − 本端质心
            pos, edge = _ray_exit(own, d, terrs[mod])
            anchors.append(Anchor(n.name, mod, pos[0], pos[1], edge))
        if stray:
            warns.append(f"{n.name}: {stray} 脚在无领地器件上（只参与方向，不开盘）")

    ox1, oy1, ox2, oy2 = board.outline
    anchors = [_clamp_board(a, ox1 + 0.5, oy1 + 0.5, ox2 - 0.5, oy2 - 0.5) for a in anchors]
    anchors = _spread_along_edge(anchors)
    anchors, misses = _slide_clear(anchors, board.pads)
    warns += [f"锚盘避让未成: {a.net} @ {a.module}({_f(a.x)},{_f(a.y)})" for a in misses]
    return anchors, warns


def draw_territories(
    board_path: Path, out: Path | None = None, *,
    terrs: dict[str, tuple[float, float, float, float]],
    anchors: list[Anchor],
) -> Path:
    """Eco1.User 画领地框 + 模块名 + 锚盘标记（方块 + 网名）——诊断叠层。

    方言逐字段对齐 draw_region_boxes（KiCad8 gr_text effects/font），
    不进铜不进制造。
    """
    blocks: list[str] = []

    def _uuid() -> str:
        return str(_uuid_mod.uuid4())

    for name, (x1, y1, x2, y2) in sorted(terrs.items()):
        for a, b, c, d in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                           (x2, y2, x1, y2), (x1, y2, x1, y1)):
            blocks.append(
                f'\t(gr_line\n'
                f'\t\t(start {_f(a)} {_f(b)})\n'
                f'\t\t(end {_f(c)} {_f(d)})\n'
                f'\t\t(stroke\n'
                f'\t\t\t(width 0.15)\n'
                f'\t\t\t(type default)\n'
                f'\t\t)\n'
                f'\t\t(layer "Eco1.User")\n'
                f'\t)'
            )
        blocks.append(
            f'\t(gr_text "{name}"\n'
            f'\t\t(at {_f(x1)} {_f(y1 - 1.5)} 0)\n'
            f'\t\t(layer "Eco1.User")\n'
            f'\t\t(uuid "{_uuid()}")\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size 1.5 1.5) (thickness 0.15))\n'
            f'\t\t)\n'
            f'\t)'
        )
    out_off = {"top": (0.0, -1.0), "bottom": (0.0, 1.0),
               "left": (-1.0, 0.0), "right": (1.0, 0.0)}
    for a in anchors:
        hx1, hy1 = a.x - _R_ANCHOR, a.y - _R_ANCHOR
        hx2, hy2 = a.x + _R_ANCHOR, a.y + _R_ANCHOR
        for b, c, d, e in ((hx1, hy1, hx2, hy1), (hx2, hy1, hx2, hy2),
                           (hx2, hy2, hx1, hy2), (hx1, hy2, hx1, hy1)):
            blocks.append(
                f'\t(gr_line\n'
                f'\t\t(start {_f(b)} {_f(c)})\n'
                f'\t\t(end {_f(d)} {_f(e)})\n'
                f'\t\t(stroke\n'
                f'\t\t\t(width 0.12)\n'
                f'\t\t\t(type default)\n'
                f'\t\t)\n'
                f'\t\t(layer "Eco1.User")\n'
                f'\t)'
            )
        dx, dy = out_off[a.edge]
        blocks.append(
            f'\t(gr_text "{a.net}"\n'
            f'\t\t(at {_f(a.x + dx)} {_f(a.y + dy)} 0)\n'
            f'\t\t(layer "Eco1.User")\n'
            f'\t\t(uuid "{_uuid()}")\n'
            f'\t\t(effects\n'
            f'\t\t\t(font (size 0.8 0.8) (thickness 0.12))\n'
            f'\t\t)\n'
            f'\t)'
        )
    board_path = Path(board_path)
    out = Path(out) if out is not None else board_path
    out.write_text(
        _append_blocks(board_path.read_text(encoding="utf-8"), blocks),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------- 区板


def territory_nets(ir, module: str, anchors: list[Anchor]) -> tuple[str, ...]:
    """领地 T 的区网表：本模块 intra 网 + 在 T 开了盘的跨区网片段。

    跨区网在区板里的端点 = 本模块器件的焊盘 + 本领地锚盘（外模块焊盘
    被 write_territory_board 剥成无网障碍）；无锚盘的跨区网整个留给
    互联段（互联段直连的 warned 形态）。
    """
    own = {a.net for a in anchors if a.module == module}
    intra = {n.name for n in ir.nets
             if n.kind == "intra" and n.modules[0] == module}
    return tuple(sorted(intra | own))


def write_territory_board(
    board_path: Path, out: Path, *, ir, module: str, anchors: list[Anchor],
) -> Path:
    """placed 底（含电源落地铜）→ 领地 T 区板（区板并行的并行点）：

    1. **剥网**：外模块器件上跨区网（kind=system）焊盘的网络引用整元
       删除——铜还在（DSN 障碍不变），但不再是本区该网的端点，FR 网络
       声明只剩本地片段；
    2. **开盘**：本领地锚盘落成真实单盘 smd 封装（0.6mm 方盘，F.Cu，
       exclude_from_pos/bom——布线期生成物，不进原理图不进 BOM，终相
       删盘对接）。

    各领地区板都从同一底板出发（无链式传染）：挪一个锚盘只重开一块
    区板，这是锚盘模型对"动态可调"的兑现。
    """
    board_path, out = Path(board_path), Path(out)
    text = board_path.read_text(encoding="utf-8")
    system = {n.name for n in ir.nets if n.kind == "system"}
    for a in anchors:
        if '"' in a.net or "\\" in a.net:
            raise ValueError(f"网名 {a.net!r} 含引号/反斜杠，不能落盘成文本")
    # 剥网是全量规则：跨区网只在本模块器件（片段真实端点）+ 本领地锚盘
    # 上存活——外模块封装一律剥净，哪怕该网在本区开了盘（FR 网络声明
    # 多一个外区端点就会横穿领地，平行区板全作废）
    strip_nets = sorted(system)

    # 剥网：外模块 footprint 块内逐网删除 (net "名") / (net 5 "名") 元组
    spans = _top_footprint_spans(text)
    pieces: list[str] = []
    last = 0
    stripped = 0
    for start, end, ref in spans:
        dev = ir.devices.get(ref)
        if dev is not None and dev.module == module:
            continue  # 本模块器件：跨区网端点保留（锚盘之外的真实终点）
        block = text[start:end]
        cut = block
        for net in strip_nets:
            cut = re.sub(
                rf'\(\s*net\s+(?:\d+\s+)?"{re.escape(net)}"\s*\)', "", cut)
        if cut != block:
            stripped += 1
        pieces.append(text[last:start])
        pieces.append(cut)
        last = end
    pieces.append(text[last:])
    body = "".join(pieces)

    # 开盘：本领地锚盘 → 单盘封装（位号 ANC1..，Value=网名，方言对齐
    # placed 板 pad：名字式 net 元组 + uuid；gr_text/uuid 教训：块带全 uuid）
    blocks = [_anchor_footprint(f"ANC{i}", a)
              for i, a in enumerate(
                  sorted((x for x in anchors if x.module == module),
                         key=lambda x: x.net), 1)]
    out.write_text(_append_blocks(body, blocks), encoding="utf-8")
    if stripped:
        print(f"  {module} 区板：剥外模块器件 {stripped} 个封装上的跨区网，"
              f"开盘 {len(blocks)} 个")
    return out


# ---------------------------------------------------------------- 内部


def _anchor_footprint(ref: str, a: Anchor) -> str:
    """锚盘单盘封装块（KiCad8 方言，逐字段对齐 placed 板 footprint/pad）。"""
    return (
        f'\t(footprint "kca:AnchorPad_0_6mm"\n'
        f'\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "{_uuid_mod.uuid4()}")\n'
        f'\t\t(at {_f(a.x)} {_f(a.y)} 0)\n'
        f'\t\t(property "Reference" "{ref}"\n'
        f'\t\t\t(at 0 0 0)\n'
        f'\t\t\t(layer "F.SilkS")\n'
        f'\t\t\t(uuid "{_uuid_mod.uuid4()}")\n'
        f'\t\t\t(effects (font (size 0.8 0.8) (thickness 0.15)))\n'
        f'\t\t)\n'
        f'\t\t(property "Value" "{a.net}"\n'
        f'\t\t\t(at 0 0 0)\n'
        f'\t\t\t(layer "F.Fab")\n'
        f'\t\t\t(hide yes)\n'
        f'\t\t\t(uuid "{_uuid_mod.uuid4()}")\n'
        f'\t\t\t(effects (font (size 0.8 0.8) (thickness 0.15)))\n'
        f'\t\t)\n'
        f'\t\t(attr smd exclude_from_pos_files exclude_from_bom)\n'
        f'\t\t(pad "1" smd rect\n'
        f'\t\t\t(at 0 0)\n'
        f'\t\t\t(size 0.6 0.6)\n'
        f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n'
        f'\t\t\t(net "{a.net}")\n'
        f'\t\t\t(uuid "{_uuid_mod.uuid4()}")\n'
        f'\t\t)\n'
        f'\t)'
    )


def _top_footprint_spans(text: str) -> list[tuple[int, int, str]]:
    """根层 (footprint …) 块的字符跨度 + 位号（文本剥网的定位器）。

    深度扫描找根（kicad_pcb）直接子层的 footprint 块——不依赖缩进方言；
    位号取 (property "Reference" "…")，无属性方言板回 "?"（按外模块处理，
    剥网幂等无害）。
    """
    spans: list[tuple[int, int, str]] = []
    depth, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
            if depth == 2 and text.startswith("footprint", i + 1):
                d, j = 1, i + 1
                while j < n:
                    if text[j] == "(":
                        d += 1
                    elif text[j] == ")":
                        d -= 1
                        if d == 0:
                            break
                    j += 1
                block = text[i:j + 1]
                m = re.search(r'\(\s*property\s+"Reference"\s+"([^"]+)"', block)
                spans.append((i, j + 1, m.group(1) if m else "?"))
                i, depth = j + 1, 1  # 整块已消费，深度回根内层
                continue
        elif ch == ")":
            depth -= 1
        i += 1
    return spans


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))


def _ray_exit(
    c: tuple[float, float], d: tuple[float, float],
    box: tuple[float, float, float, float],
) -> tuple[tuple[float, float], str]:
    """领地内一点沿方向 d 的射线交 bbox 的退出点 + 边名（y-down）。"""
    x1, y1, x2, y2 = box
    dx, dy = d
    if dx == 0.0 and dy == 0.0:
        dx, dy = 0.0, 1.0
    cands: list[tuple[float, tuple[float, float], str]] = []
    if dx > 0:
        cands.append(((x2 - c[0]) / dx, (x2, c[1]), "right"))
    if dx < 0:
        cands.append(((x1 - c[0]) / dx, (x1, c[1]), "left"))
    if dy > 0:
        cands.append(((y2 - c[1]) / dy, (c[0], y2), "bottom"))
    if dy < 0:
        cands.append(((y1 - c[1]) / dy, (c[0], y1), "top"))
    _, pt, edge = min(cands, key=lambda z: z[0])
    return pt, edge


def _clamp_board(
    a: Anchor, x1: float, y1: float, x2: float, y2: float,
) -> Anchor:
    return a if x1 <= a.x <= x2 and y1 <= a.y <= y2 else replace(
        a, x=min(max(a.x, x1), x2), y=min(max(a.y, y1), y2))


def _spread_along_edge(anchors: list[Anchor]) -> list[Anchor]:
    """同 (领地, 边) 的锚盘沿边轴排序，间距 < GAP 依次后推（顺边展开）。"""
    by_edge: dict[tuple[str, str], list[int]] = {}
    for i, a in enumerate(anchors):
        by_edge.setdefault((a.module, a.edge), []).append(i)
    out = list(anchors)
    for (mod, edge), idxs in by_edge.items():
        if len(idxs) < 2:
            continue
        horizontal = edge in ("top", "bottom")
        idxs.sort(key=lambda i: out[i].x if horizontal else out[i].y)
        prev: Anchor | None = None
        for i in idxs:
            a = out[i]
            if prev is not None:
                if horizontal and a.x - prev.x < GAP:
                    a = replace(a, x=prev.x + GAP)
                elif not horizontal and a.y - prev.y < GAP:
                    a = replace(a, y=prev.y + GAP)
            out[i] = a
            prev = a
    return out


def _slide_clear(
    anchors: list[Anchor], pads, *, step: float = 0.5, tries: int = 24,
) -> tuple[list[Anchor], list[Anchor]]:
    """锚盘离最近焊盘铜缘净距 < CLEAR → 沿边 ±step 滑动找净位。

    双向各 tries 步内找不到 → 保留原位并列入未成清单（人工/挪盘环兜底）。
    """
    def _ok(x: float, y: float) -> bool:
        for p in pads:
            if abs(p.x - x) > 3.0 and abs(p.y - y) > 3.0:  # 粗筛
                continue
            need = max(p.w, p.h) / 2 + _R_ANCHOR + CLEAR
            if (p.x - x) ** 2 + (p.y - y) ** 2 < need * need:
                return False
        return True

    out: list[Anchor] = []
    misses: list[Anchor] = []
    for a in anchors:
        horizontal = a.edge in ("top", "bottom")
        if _ok(a.x, a.y):
            out.append(a)
            continue
        hit: Anchor | None = None
        for sgn in (1, -1):
            for k in range(1, tries + 1):
                off = sgn * k * step
                cand = (replace(a, x=a.x + off) if horizontal
                        else replace(a, y=a.y + off))
                if _ok(cand.x, cand.y):
                    hit = cand
                    break
            if hit is not None:
                break
        if hit is None:
            misses.append(a)
        out.append(hit if hit is not None else a)
    return out, misses
