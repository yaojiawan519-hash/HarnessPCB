"""Specctra DSN 导出 + SES 回读（M3-5）——freerouting 纯文本对接口。

不经 kicad-python：DSN 由 RouteBoard 直书、SES 自解析，全链路可离线
测试（真实 FR 子进程在 freeroute 层注入）。方言经真机 freerouting.jar
逐项实证（build/fr_probe mini/mini2/oracle）：

- ``(resolution um 10)`` + µm 整数坐标：FR 内部自洽缩放，SES 回读值
  恒为 DSN 值 ×10 → **mm = SES 值 / 10000**；
- 障碍铜（L1 已布铜、异网盘）：**铜形 pin 伪元件**（image 内 netless
  pin，同 MP 安装孔语义）。实证（build/route_pf2 链 + ko 探针）：本机
  Freerouting 布线引擎**无视** wiring ``(attr protect)`` 与 structure
  ``keepout`` 矩形——新线零间距横穿 protect 线，SES 照常输出，仅在优
  化评分里记 violation；唯一被尊重的障碍是铜形 pin（板上异网盘/MP 孔
  从未被穿）。故异网外层线段 → AABB 外扩半线宽的矩形 pin（含端帽，
  斜线保守），异网孔 → 双层圆 pin，全部收进单伪元件；未入任何网的盘
  同为 netless pin（BGA 异网球无需声明即遮挡）；
- image pin 用**绝对坐标**、placement 恒 ``(place REF 0 0 front 0)``
  ——与 KiCad 相对+旋转方案几何等价；
- 保护过孔同样 ``(via PADSTACK x y (net N) (attr protect))``（mini2
  实证），孔盘只入 library、不入 structure 的可布孔表；
- SES ``network_out`` 只含 FR 新布铜（保护铜不回读——本就躺在 base
  板上）；多点 path = 折线相邻点成段，wire 起止于盘/孔中心（整数
  精确），连通核验可用精确坐标匹配 + 几何搭接兜底。

非圆盘一律以矩形包络近似（oval/roundrect ⊂ rect）：障碍方向只大不
小 = 间隙偏保守，不会布出擦伤铜。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .diff import FINE
from .model import Pad, RouteBoard, Segment, Via
from .regions import Region

_SAFE = re.compile(r"[^A-Za-z0-9_]")
_OUTER = ("F.Cu", "B.Cu")  # 缺省层表（2/4 层板）；N 层板传 spec.signal_layers
_BLK_SUBLEN = 0.6  # mm；斜线障碍子段长上限（见 _seg_blockers）
_ANCHOR_TOUCH = 0.35  # mm；端点距锚心此内视为"片段止于锚盘"（互联模式）
_ANCHOR_BACK = 0.52  # mm；同网片段转障碍时端点后撤距（锚环 0.3+间隙+半线宽）


def _seg_blockers(
    s: Segment,
) -> list[tuple[float, float, float, float, str]]:
    """异网线段 → AABB 障碍矩形序列（中心 x, 中心 y, 宽, 高, 层）。

    斜线整段 AABB 会吞掉旁边真实间隙充裕的盘（真板实证 2026-09-10：
    4.45mm 45° 线 → 3.27mm 方框罩住 0.36mm 外的盘，FR 视该盘埋在
    障碍里、所在网永久不可布，逐段二分锁定此一障碍即复现/移除即
    全通）——细分成 ≤ ``_BLK_SUBLEN`` 子段各取 AABB，并集仍 ⊇ 真实
    铜包络（保守不减），膨胀从 ±半段长缩到 ±0.3mm。轴线段 AABB 本
    就紧致，保持单矩形不变。
    """
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    n = 1 if (dx == 0 or dy == 0) else max(
        1, math.ceil(math.hypot(dx, dy) / _BLK_SUBLEN))
    out: list[tuple[float, float, float, float, str]] = []
    for i in range(n):
        x1, x2 = sorted((s.x1 + dx * i / n, s.x1 + dx * (i + 1) / n))
        y1, y2 = sorted((s.y1 + dy * i / n, s.y1 + dy * (i + 1) / n))
        out.append(((x1 + x2) / 2, (y1 + y2) / 2,
                    x2 - x1 + s.width, y2 - y1 + s.width, s.layer))
    return out


@dataclass(frozen=True)
class DsnBundle:
    """export_dsn 产物：DSN 文本 + 回读所需的映射表。"""

    text: str
    net_map: dict[str, str] = field(default_factory=dict)  # 原网名 → DSN 网名
    via_padstacks: dict[str, tuple[float, float]] = field(
        default_factory=dict)  # DSN 孔盘名 → (size, drill) mm


def _dsn_name(name: str, used: set[str]) -> str:
    """网名/引用名 → DSN 安全标识（[A-Za-z0-9_]，首字符字母/下划线）。

    KiCad 网名如 ``+24V_RAW`` / ``/pwr/3V3`` 含 + / 等特殊字符，直写
    会破坏 S 表达式 token——替换 + 首字符修正 + 冲突后缀，回读经
    net_map 反查，原名可含任意字符。
    """
    s = _SAFE.sub("_", name.strip()) or "N"
    if not (s[0].isalpha() or s[0] == "_"):
        s = "N" + s
    base, k = s, 1
    while s in used:
        s = f"{base}_{k}"
        k += 1
    used.add(s)
    return s


def _um(mm: float) -> int:
    """mm → DSN µm 整数（resolution um 10 方言，见模块 docstring）。"""
    return int(round(mm * 1000))


def _dims(w: float, h: float) -> str:
    return f"{_um(w)}x{_um(h)}"


def export_dsn(
    board: RouteBoard, region: Region,
    *,
    width: float = FINE.width,
    clearance: float = FINE.clearance,
    via_size: float = FINE.via_size,
    via_drill: float = FINE.via_drill,
    layers: tuple[str, ...] = _OUTER,
    keepout_sites: tuple[tuple[float, float], ...] = (),
    pin_refs: frozenset[str] | None = None,
) -> DsnBundle:
    """板 + 区域 → DSN 文本：区域网为可布网，其余一切铜/盘皆障碍。

    layers = 布线层表（栈序）：TH 盘/孔铜环、VIA padstack 在**每层**发
    shape（FR 在 In2 上也得看见通孔盘），既有铜按层过滤；平面层（纯
    zone）不进层表——zone 填充是 KiCad fill 语义，不构成布线障碍。

    keepout_sites = 预留过孔位（mm 坐标）：以 via_size⊕2×clearance 的
    TH 圆 blocker 发到**每层**——FR 的线与孔都不得占。消费者：狗骨
    预约（差分网 BGA 逃逸孔位，见 freeroute._dogbone_keepouts）——
    FR 布内层时不知道后续差分区要打孔，先到先得会偷走孔位（v6 实证
    In2 线压狗骨 0.107mm），预防优于事后协商。

    - 区域网（≥2 盘）入 network 声明 pins，交 FR 布线；
    - 区域网的 <2 盘网不入 network（无可布连接）；
    - 异网既有铜 → 铜形 pin 伪元件（硬障碍，见模块 docstring）；
    - 同网既有铜 → wiring protect（FR 布线时连接既有铜，不被撕）。

    pin_refs（**互联模式**，merge 互联段专用）：锚盘位号集合。给定后
    语义整体切换——protect 无视实证（2026-09-11：互联新线整排压过
    领地铜 → 406 短路）下互联段唯一正确打法：

    - network pins 只含锚盘（真脚已由领地片段连通，再入 pins 会被
      FR 重布成双份）；真脚留作无网 image pin = 障碍；
    - **同网既有铜同样转障碍 pin**，wiring 段不发；
    - 领地片段终点=锚心，原样转障碍会把锚盘埋进自家矩形（盘被埋
      即永不可布，见 _seg_blockers docstring）——片段端点沿段身后
      撤 _ANCHOR_BACK，锚盘 0.6 环+间隙恰封住后撤让出的窗口（缝
      <0.03mm << 间隙 0.127，他网线钻不进）；锚上/锚旁孔直接省略
      （锚环 ≡ FINE 孔盘同径自遮挡）。
    """
    used: set[str] = set()
    board_name = _dsn_name(region.name, used)

    # 网名先行注册（排序保确定性：冲突后缀不受遍历序影响）
    by_net = board.pads_by_net()
    anchor_pts: tuple[tuple[float, float], ...] = ()
    anchor_by_net: dict[str, list[Pad]] = {}
    if pin_refs is not None:
        for p in board.pads:
            if p.ref in pin_refs:
                anchor_by_net.setdefault(p.net, []).append(p)
        anchor_pts = tuple(sorted(
            {(round(p.x, 3), round(p.y, 3))
             for p in board.pads if p.ref in pin_refs}))
        route_nets = sorted(n for n in set(region.nets)
                            if len(anchor_by_net.get(n, ())) >= 2)
    else:
        route_nets = sorted(n for n in set(region.nets)
                            if len(by_net.get(n, ())) >= 2)
    route_set = set(route_nets)
    net_map: dict[str, str] = {}
    for n in route_nets:
        net_map[n] = _dsn_name(n, used)

    # ---- padstack / image（盘 → 绝对 pin 坐标）
    padstacks: dict[tuple, str] = {}
    ps_lines: list[str] = []

    def _padstack(p: Pad) -> str:
        pad_lays = layers if p.is_th else tuple(l for l in p.layers if l in layers)
        key = (p.shape, round(p.w, 4), round(p.h, 4), pad_lays)
        if key in padstacks:
            return padstacks[key]
        tag = "TH" if p.is_th else pad_lays[0][0]  # F / B
        name = _dsn_name(f"P_{p.shape}_{_dims(p.w, p.h)}_{tag}", used)
        shapes = []
        for lay in pad_lays:
            if p.shape == "circle":
                shapes.append(f"(shape (circle {lay} {_um(p.w)}))")
            else:  # 非圆一律矩形包络（只大不小的保守障碍，见 docstring）
                hw, hh = _um(p.w / 2), _um(p.h / 2)
                shapes.append(f"(shape (rect {lay} {-hw} {-hh} {hw} {hh}))")
        ps_lines.append(
            f"    (padstack {name}\n"
            + "".join(f"      {s}\n" for s in shapes)
            + "      (attach off)\n    )"
        )
        padstacks[key] = name
        return name

    # pin id 规则：FR 要求 image 内非空且唯一——空盘号（J2/J3 的
    # np_thru_hole 安装孔 num=""）合成 MP<n>（无网 → 纯障碍），同号盘
    # （RJ45 的 SH×2、U3 的 2×2）后缀 _2/_3 去重；network 侧引用同一
    # 映射，两侧恒一致。
    ref_map: dict[str, str] = {}
    pin_ids: dict[int, str] = {}  # id(Pad) → image 内唯一 pin id
    by_ref: dict[str, list[Pad]] = {}
    for p in board.pads:
        by_ref.setdefault(p.ref, []).append(p)
    images: list[str] = []
    placements: list[str] = []
    for ref in sorted(by_ref):
        ref_map[ref] = _dsn_name(ref, used)
        lines: list[str] = []
        seen: dict[str, int] = {}
        mp = 0
        for p in sorted(by_ref[ref], key=lambda q: str(q.num)):
            pid = _SAFE.sub("_", str(p.num).strip())
            if not pid:
                mp += 1
                pid = f"MP{mp}"
            k = seen.get(pid, 0)
            seen[pid] = k + 1
            if k:
                pid = f"{pid}_{k + 1}"
            pin_ids[id(p)] = pid
            lines.append(f"      (pin {_padstack(p)} {pid} {_um(p.x)} {_um(p.y)})\n")
        img = f"IMG_{ref_map[ref]}"
        images.append(f"    (image {img}\n{''.join(lines)}    )\n")
        placements.append(
            f"    (component {img}\n      (place {ref_map[ref]} 0 0 front 0)\n    )\n"
        )

    # 可布孔（structure 声明；通孔 shape 发到每层）
    via_padstacks = {"VIA": (via_size, via_drill)}
    ps_lines.append(
        f"    (padstack VIA\n"
        + "".join(f"      (shape (circle {lay} {_um(via_size)}))\n"
                  for lay in layers)
        + "      (attach off)\n    )"
    )
    # 同网既有孔盘（只入 library；同网既有铜见 wiring；互联模式不发）
    pv_names: dict[int, str] = {}
    for v in (board.vias if pin_refs is None else ()):
        if v.net not in route_set or _um(v.size) in pv_names:
            continue
        d = _um(v.size)
        pv_names[d] = _dsn_name(f"PV_{d}", used)
        ps_lines.append(
            f"    (padstack {pv_names[d]}\n"
            + "".join(f"      (shape (circle {lay} {d}))\n" for lay in layers)
            + "      (attach off)\n    )"
        )

    # ---- 异网既有铜 → 铜形 pin 伪元件（硬障碍，方言见模块 docstring）
    blk_ps: dict[tuple, str] = {}

    def _blk_padstack(w: float, h: float, layer: str) -> str:
        tag = "TH" if layer == "TH" else layer[0]  # F / B / TH(双层圆)
        key = (round(w, 4), round(h, 4), tag)
        if key in blk_ps:
            return blk_ps[key]
        shape = "circle" if tag == "TH" else "rect"
        name = _dsn_name(f"BLK_{shape}_{_dims(w, h)}_{tag}", used)
        if tag == "TH":  # 通孔障碍：每层都有环（含内层信号层）
            shapes = "".join(f"(shape (circle {lay} {_um(w)}))\n      "
                             for lay in layers).rstrip()
        else:
            hw, hh = _um(w / 2), _um(h / 2)
            shapes = f"(shape (rect {layer} {-hw} {-hh} {hw} {hh}))"
        ps_lines.append(
            f"    (padstack {name}\n      {shapes}\n      (attach off)\n    )"
        )
        blk_ps[key] = name
        return name

    blockers: list[tuple[float, float, float, float, str]] = []

    def _anchor_at(x: float, y: float) -> tuple[float, float] | None:
        for ax, ay in anchor_pts:
            if math.hypot(x - ax, y - ay) <= _ANCHOR_TOUCH:
                return ax, ay
        return None

    def _back_off(x: float, y: float, ax: float, ay: float,
                  ox: float, oy: float) -> tuple[float, float] | None:
        """片段端点 (x,y) 近锚心 (ax,ay)：沿段向另一端 (ox,oy) 后撤。

        段长不足后撤距（反转/零长风险）→ None：该片段整段贴锚，锚盘
        0.6 环自身就是此处的障碍，跳过不缺遮挡。
        """
        dx, dy = ox - x, oy - y
        length = math.hypot(dx, dy)
        if length <= _ANCHOR_BACK + 0.05:
            return None
        t = _ANCHOR_BACK / length
        return x + dx * t, y + dy * t

    for s in board.segments:
        if s.layer not in layers:
            continue
        if pin_refs is None and s.net in route_set:
            continue  # 常规模式：同网铜走 wiring；互联模式同网铜也是障碍
        x1, y1, x2, y2 = s.x1, s.y1, s.x2, s.y2
        if anchor_pts:
            a = _anchor_at(x1, y1)
            if a is not None:
                r = _back_off(x1, y1, *a, x2, y2)
                if r is None:
                    continue
                x1, y1 = r
            a = _anchor_at(x2, y2)
            if a is not None:
                r = _back_off(x2, y2, *a, x1, y1)
                if r is None:
                    continue
                x2, y2 = r
        blockers.extend(_seg_blockers(
            Segment(x1, y1, x2, y2, s.width, s.layer, s.net)))
    for v in board.vias:
        if pin_refs is None and v.net in route_set:
            continue
        if anchor_pts and _anchor_at(v.x, v.y) is not None:
            continue  # 锚上/锚旁孔：锚环 0.6 ≡ FINE 孔盘，同径自遮挡
        blockers.append((v.x, v.y, v.size, v.size, "TH"))
    # 预留孔位（狗骨预约）：TH 圆 blocker 每层生效——线/孔皆不得占
    ko_d = via_size + 2 * clearance
    for kx, ky in keepout_sites:
        blockers.append((kx, ky, ko_d, ko_d, "TH"))
    if blockers:
        blk = _dsn_name("FR_BLOCK", used)
        blk_img = f"IMG_{blk}"
        pins = "".join(
            f"      (pin {_blk_padstack(w, h, ly)} W{i} {_um(cx)} {_um(cy)})\n"
            for i, (cx, cy, w, h, ly) in enumerate(sorted(
                blockers, key=lambda b: (b[4], b[0], b[1])), 1))
        images.append(f"    (image {blk_img}\n{pins}    )\n")
        placements.append(
            f"    (component {blk_img}\n      (place {blk} 0 0 front 0)\n    )\n"
        )

    # ---- network：区域网带 pins（互联模式 pins 只含锚盘）
    net_lines = []
    for n in route_nets:
        src = anchor_by_net[n] if pin_refs is not None else by_net[n]
        pins = " ".join(f"{ref_map[p.ref]}-{pin_ids[id(p)]}"
                        for p in sorted(src, key=lambda q: q.key))
        net_lines.append(f"    (net {net_map[n]}\n      (pins {pins})\n    )\n")

    # ---- wiring：同网既有铜 protect（FR 连接它们；异网铜已是 pin 障碍）
    # 互联模式不发（同网铜已全转障碍 pin，见 pin_refs docstring）
    wires = [] if pin_refs is not None else [
        f"    (wire (path {s.layer} {_um(s.width)} "
        f" {_um(s.x1)} {_um(s.y1)} {_um(s.x2)} {_um(s.y2)})"
        f" (net {net_map[s.net]}) (attr protect))\n"
        for s in board.segments
        if s.layer in layers and s.net in route_set
    ] + [
        f"    (via {pv_names[_um(v.size)]} {_um(v.x)} {_um(v.y)}"
        f" (net {net_map[v.net]}) (attr protect))\n"
        for v in board.vias if v.net in route_set
    ]

    x1, y1, x2, y2 = board.outline
    bnd = "  ".join(  # 闭合五点矩形（缺第 4 角会变三角形，FR 或拒或裁板）
        str(_um(v)) for v in (x1, y1, x2, y1, x2, y2, x1, y2, x1, y1))
    text = (
        f"(pcb {board_name}\n"
        '  (parser\n    (string_quote ")\n'
        '    (space_in_quoted_tokens on)\n'
        '    (host_cad "kicad-access")\n  )\n'
        "  (resolution um 10)\n  (unit um)\n"
        "  (structure\n"
        + "".join(
            f"    (layer {lay}\n      (type signal)\n      (property\n"
            f"        (index {i})\n      )\n    )\n"
            for i, lay in enumerate(layers))
        + f"    (boundary\n      (path pcb 0  {bnd})\n    )\n"
        "    (via VIA)\n"
        f"    (rule\n      (width {_um(width)})\n      (clearance {_um(clearance)})\n    )\n"
        "  )\n"
        f"  (placement\n{''.join(placements)}  )\n"
        f"  (library\n{''.join(images)}"
        + "".join(f"{l}\n" for l in ps_lines)
        + "  )\n"
        f"  (network\n{''.join(net_lines)}  )\n"
        + (f"  (wiring\n{''.join(wires)}  )\n" if wires else "")
        + ")\n"
    )
    return DsnBundle(text=text, net_map=net_map, via_padstacks=via_padstacks)


# ---------------------------------------------------------------- SES 回读


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':  # 引号串（本方言不发出，容错解析 FR 回写）
            j = text.find('"', i + 1)
            j = n if j < 0 else j
            yield ("str", text[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield ("str", text[i:j])
            i = j


def _parse_sexpr(text: str):
    """S 表达式 → 嵌套 list（atom = str，节点首元素 = tag）。"""
    stack: list[list] = [[]]
    for tok in _tokens(text):
        if tok == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(tok[1])
    return stack[0]


def _find_all(node: list, tag: str) -> list[list]:
    out = []
    for k in node:
        if isinstance(k, list) and k and k[0] == tag:
            out.append(k)
        elif isinstance(k, list):
            out.extend(_find_all(k, tag))
    return out


def parse_ses(
    text: str,
    *,
    via_padstacks: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[Segment], list[Via]]:
    """SES 文本 → (segments, vias)，坐标换算 mm = SES 值 / 10000。

    只读 ``routes → network_out``：每 net 的 wire path（多点折线相邻
    点成段）与 via（net 继承外层 net 节点名）。保护铜不会出现在
    network_out（实证），调用方再按区域网过滤 + net_map 反查原名。
    """
    if via_padstacks is None:
        via_padstacks = {"VIA": (FINE.via_size, FINE.via_drill)}
    default_via = via_padstacks.get("VIA", (FINE.via_size, FINE.via_drill))

    segments: list[Segment] = []
    vias: list[Via] = []
    for network in _find_all(_parse_sexpr(text), "network_out"):
        for net in (k for k in network[1:] if isinstance(k, list) and k and k[0] == "net"):
            name = str(net[1])
            for wire in (k for k in net[2:] if isinstance(k, list) and k and k[0] == "wire"):
                for path in (k for k in wire[1:] if isinstance(k, list) and k and k[0] == "path"):
                    layer, w = str(path[1]), round(float(path[2]) / 10000, 4)
                    pts = [round(float(v) / 10000, 4) for v in path[3:]]
                    for (x1, y1), (x2, y2) in zip(
                        zip(pts[0::2], pts[1::2]), zip(pts[2::2], pts[3::2])
                    ):
                        segments.append(Segment(x1, y1, x2, y2, w, layer, name))
            for via in (k for k in net[2:] if isinstance(k, list) and k and k[0] == "via"):
                size, drill = via_padstacks.get(str(via[1]), default_via)
                vias.append(Via(round(float(via[2]) / 10000, 4),
                                round(float(via[3]) / 10000, 4),
                                size, drill, name))
    return segments, vias
