"""L2 freerouting 封装（M3-5）：DSN → Freerouting CLI（best-of-N）→ SES 回导。

L1 已布铜经 dsn.export_dsn 以 ``(attr protect)`` 锁为障碍，FR 只补本
区网；同一 DSN 以递增 max-passes 跑 N 次（spec ``routing.freerouting
{attempts, max_passes}``，调度 ``mp×(k+1)``），按 (未布通网数, 孔数,
总铜长) 取优、先到先得平局；全部尝试后仍有未布通网 → ValueError 点名
（不静默留断线）。

外部引擎缺项（java/freerouting.jar）不是本模块的错——CLI 层 tools 门禁
先拦（按设计降级跳过该区）；真跑失败（无 SES/超时）抛 FreerouteError
带 FR 输出尾部，best-of-N 换下一尝试继续。

runner 注入点：测试传假 runner（合成 SES），真机走 real_runner
（``java -jar freerouting.jar -de -do -mp -mt 1``；``-mt 1`` 必带——
FR 多线程路由已知会产生间隙违规）。
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import tools
from .dsn import export_dsn, parse_ses
from .l1 import NetReport
from .model import RouteBoard, Segment, Via, seg_dist, seg_rect_dist, seg_seg_dist
from .regions import Region

_EPS = 1e-3  # mm；连通核验的几何搭接容差（≈ SES 坐标换算精度）


class FreerouteError(RuntimeError):
    """单次 freerouting 尝试失败（无 SES / 超时 / 启动失败）。"""


class UnroutedNets(ValueError):
    """best-of-N 全试完后仍未布通的网（携带网名）。

    CLI 据此显式降级：剔除死网重跑本区（≤3 轮），遗留进**手工待布
    清单**打印并改返回码——绝不静默留断线（用户授权的兜底路线）。"""

    def __init__(self, msg: str, unrouted: tuple[str, ...]):
        super().__init__(msg)
        self.unrouted = unrouted


def real_runner(dsn_text: str, max_passes: int, *, timeout_s: float = 600.0) -> str:
    """真机 runner：临时目录落 DSN → java 子进程 → SES 文本。"""
    java, fr = tools.java_status(), tools.freerouting_status()
    if not (java.ok and fr.ok and java.path and fr.path):
        raise FreerouteError("java/freerouting.jar 缺项（CLI tools 门禁应先拦）")
    with tempfile.TemporaryDirectory(prefix="hws_fr_") as td:
        dsn, ses = Path(td) / "in.dsn", Path(td) / "out.ses"
        dsn.write_text(dsn_text, encoding="utf-8")
        cmd = [
            str(java.path),
            # macOS 上 jar 会拉起 Freerouting GUI（赞助横幅/设置页弹窗
            # 轮番骚扰，还带 TSM/IMK 噪声日志）——强制纯无头，实测不影响
            # 路由与 SES 落盘
            "-Djava.awt.headless=true",
            "-jar", str(fr.path),
            "-de", str(dsn), "-do", str(ses),
            "-mp", str(max_passes), "-mt", "1",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as e:
            raise FreerouteError(
                f"freerouting 超时（>{timeout_s:.0f}s, mp={max_passes}）") from e
        except OSError as e:
            raise FreerouteError(f"freerouting 启动失败: {e}") from e
        # v2.4.1 无头落盘竞态实证（2026-09-10）：路由全通（日志 0 unrouted）
        # 但 SES 概率性 0 字节（同命令 8 跑 2 过，上游未修）——空文件同样
        # 按失败显式报错，绝不静默返回 "" 让上层误判"SES 无铜"
        if not ses.is_file() or ses.stat().st_size == 0:
            tail = "\n".join(
                (r.stderr or r.stdout or "").splitlines()[-15:]) or "(无输出)"
            raise FreerouteError(
                f"freerouting 未产出有效 SES（mp={max_passes}，空文件——"
                "无头落盘竞态；本机 jar 已钉 v2.3.0，若复发查 jar 版本）:\n"
                f"{tail}")
        return ses.read_text(encoding="utf-8")


@dataclass(frozen=True)
class _Attempt:
    max_passes: int
    score: tuple[int, int, float]  # (未布通网数, 孔数, 总铜长 mm)
    unrouted: tuple[str, ...]
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]


def _pads_connected(
    board: RouteBoard, segs: list[Segment], vias: list[Via], net: str,
) -> bool:
    """本网全盘经铜归一个连通分量（并查集）。

    FR 线起止于盘/孔中心（整数精确）：先精确端点匹配；未命中走两层
    几何兜底——① 盘/孔身搭接（点到线段距 ≤ 半线宽 + 盘体半径 + 容差，
    孔跨层）防优化器截线到盘缘、及人工板线端只画到长脚盘缘的假阴性；
    ② 线端搭线身（端点到另一段中段的距 ≤ 双方半线宽和）覆盖人工板
    T 接形态（junction 在图形层、几何不共端点）。板上**既有同网铜**
    一并入并查集（互联段的领地片段是 protect 铜，SES 只回新线——不含
    既有铜会把"经片段已连的真脚"误判成孤立盘）；只判结果连通，新铜
    与既有铜等效。"""
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def _key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 3), round(y, 3))

    net_segs = [s for s in segs if s.net == net]
    pre_segs = [s for s in board.segments if s.net == net]
    # 既有弧也按弦并入（人工板主形态：stickhub +3.3V 61 段 + 20 弧靠弧
    # 端点互联成一片，丢弧曾把 KiCad DRC 0 未连接的真板误读成 35 断网）
    arc_segs = [a.chord() for a in board.arcs if a.net == net]
    all_segs = net_segs + pre_segs + arc_segs
    ends = [(_key(s.x1, s.y1), _key(s.x2, s.y2)) for s in all_segs]
    for k1, k2 in ends:
        union(k1, k2)
    net_vias = [v for v in vias if v.net == net]
    pre_vias = [v for v in board.vias if v.net == net]
    # 通孔环在每层都有铜：与线段的几何搭接按本网线实际所在层判定
    # （2 层板 = F/B，恒与旧行为一致；N 层板内层线不漏判）
    via_lays = frozenset({s.layer for s in all_segs} | {"F.Cu", "B.Cu"})
    # 盘/孔搭接判据：线碰到**盘体**就算连（KiCad 同网铜叠=连通）。人工板
    # 的线端常只画到长脚盘（LongPads ~1mm 长身）边缘附近——距盘心
    # 0.25mm 级，落在盘身体内但超半线宽容差（pic_programmer 实测 4 盘
    # 0.036-0.257mm 全部惜败）。circle 盘/孔用圆判据（半径=短边/2 精确，
    # bbox 角落对圆是假阳性）；其余形状（鸥翼 1.75×0.4 等细长盘）用
    # 矩形 containment——端点落在矩形盘身内即连，min(w,h)/2 内切圆会
    # 把身内端点误判成断裂（video U11.140 实测距心 0.496 > 0.301 阈值
    # 但 |dx|<hw 且 |dy|<hh 在身内）。oval/roundrect/trapezoid/custom
    # 以 bbox 近似（外接，假阳性方向可接受）。
    items: list[tuple[str, str, float, float, frozenset[str], float, float, bool]] = [
        (p.ref, p.num, p.x, p.y, frozenset(p.layers), p.w / 2, p.h / 2, p.shape == "circle")
        for p in board.pads_by_net()[net]
    ] + [("", f"via@{v.x:.3f},{v.y:.3f}", v.x, v.y, via_lays,
          v.size / 2, v.size / 2, True)
         for v in net_vias + pre_vias]
    # 同封装同号盘 = 同一电气节点（KiCad 语义：custom 图形盘 + 锚孔成对
    # 出现、多形状拼盘，连通性在封装内不走铜）。tiny_tapeout JP8 实测：
    # custom 1.0×0.5 盘与 TH 圆盘同号，线只接到 TH 盘、custom 盘距线端
    # 0.35mm 无铜搭接——丢此规则整网误判断裂。
    same_node: dict[tuple[str, str], tuple[float, float]] = {}
    for ref, num, x, y, *_ in items:
        union(same_node.setdefault((ref, num), _key(x, y)), _key(x, y))
    for ref, num, x, y, layers, hw, hh, is_circle in items:
        k = _key(x, y)
        find(k)  # 孤立盘也入集（自身成根 → 断裂可见）
        # 不做"精确命中即跳过"短路：孔盘可在两层各接一组铜——一侧精确
        # 命中、另一侧只搭盘身（pic_programmer Net-(D4-A)：D5.1 B.Cu 端点
        # 精确、F.Cu 群距 0.127mm 搭身），盘是桥，短路会拆散 F/B 两群。
        # 精确命中距=0 ≤ 任意阈值，统一走同一判据；不 break——盘身可
        # 同时搭到不同分量的多段，须逐一建桥。
        for s, (k1, _k2) in zip(all_segs, ends):  # 几何搭接兜底
            hit = (seg_dist(x, y, s) <= s.width / 2 + hw + _EPS if is_circle
                   else seg_rect_dist(s, x, y, hw, hh) <= s.width / 2 + _EPS)
            if s.layer in layers and hit:
                union(k, k1)
    # 孔-盘直接搭接（via-in-pad）：孔环落在盘身内即连。盘-段/孔-段两
    # 循环都够不着这种桥（tiny_tapeout /RP2040/QSPI_SD0：via@72.165,
    # 105.835 落 J11.4 B.Cu 盘身内 eff −0.31，孔把 F.Cu 链与 B.Cu 盘
    # 桥起；+3V3 同款 via@79.412 落 J11.1 曾拆出 3 盘孤岛）。孔 item
    # 以 ref="" 标记（真位号不会为空）。
    for vx, vy, vlays, vhw, _vhh, _c in [
        (x, y, lays, hw, hh, c) for r, _n, x, y, lays, hw, hh, c in items if r == ""
    ]:
        for r2, _n2, px, py, plays, phw, phh, pcirc in items:
            if r2 == "":
                continue
            if not (vlays & plays):
                continue
            d = (math.hypot(vx - px, vy - py) - min(phw, phh) if pcirc else
                 seg_rect_dist(Segment(vx, vy, vx, vy, 0.0, "F.Cu", net),
                               px, py, phw, phh))
            if d <= vhw + _EPS:
                union(_key(vx, vy), _key(px, py))
    # 线线搭接兜底（人工板形态，FR 板不触发）：KiCad 语义同层铜叠即
    # 连通——手工布线的接头常是端点落在另一段**中段**（T 接，图形层画
    # junction、几何不共端点）。同层全对 seg_seg_dist 判定（相交/共线
    # 重叠 = 0）。注：曾以为 stickhub 的断网是线身十字交叉所致，实证
    # 真因是 arc 轨道未解析（见上 arc_segs）——此兜底管 T 接，交叉场景
    # 由其天然覆盖但非为其而设。
    by_layer: dict[str, list[Segment]] = {}
    for s in all_segs:
        by_layer.setdefault(s.layer, []).append(s)
    for lays in by_layer.values():
        for i, s in enumerate(lays):
            for t in lays[i + 1:]:
                if seg_seg_dist(s, t) <= (s.width + t.width) / 2 + _EPS:
                    union(_key(s.x1, s.y1), _key(t.x1, t.y1))
    roots = {find(_key(p.x, p.y)) for p in board.pads_by_net()[net]}
    return len(roots) == 1


def _dogbone_keepouts(board: RouteBoard, spec) -> tuple[tuple[float, float], ...]:
    """差分/成对 critical 网在 BGA 球上的狗骨孔位——每球只预约**目的地
    象限的 1 个对角点**（球心 ± 半节距，方向 = 本网非 BGA 盘质心所在
    象限）。

    FR 布内层时看不见后续差分区的孔位需求，先到先得会偷走逃逸孔
    （v6 实证：system 区 In2 线压 G1 狗骨 0.107mm < 0.327 需求）——
    孔位以 TH 圆 blocker 预约，FR 的线与孔都不得占；差分区自己的 A*
    不经 DSN，不受影响。BGA 场内线被钉死在通道中线（偏移余量仅
    ~0.01mm），blocker 落在交叉口即封路——4 个对角点全约会把 FR 的
    穿场通道全堵死（v7 实证 system 区 2 网死），而 A* 逃逸本来就朝
    目的地走，背向预约纯属浪费：只约 1 个，其余交叉口还给 FR。
    节距从 BGA 球心坐标推断（两两最近邻最小值），不绑具体器件。

    spec ``routing.bga_dogbone_reserve: false`` 可整体关闭（默认开）——
    场内资源紧张时预约本身会挤死 system 网络时，交给 route_diff 的对内
    双序 + A* 四对角自由度自救（v7 实证预约换走 ETH0_LED1 的教训）。
    """
    if not spec.routing.get("bga_dogbone_reserve", True):
        return ()
    pair_nets = {n for c in spec.routing.get("critical", [])
                 if c.get("rule") in ("diff_pair", "pair")
                 for n in (c.get("nets") or [])}
    if not pair_nets:
        return ()
    bga = board.bga_refs()
    all_balls = [(p.x, p.y) for p in board.pads if p.ref in bga]
    if len(all_balls) < 2:
        return ()
    outside: dict[str, list[tuple[float, float]]] = {}  # 网 → 非 BGA 盘坐标
    for p in board.pads:
        if p.ref not in bga and p.net in pair_nets:
            outside.setdefault(p.net, []).append((p.x, p.y))
    # 节距 = 全场球心两两最近邻的最小距离
    pitch = min(
        min(math.hypot(x - ox, y - oy) for ox, oy in all_balls if (ox, oy) != (x, y))
        for x, y in all_balls)
    half = pitch / 2
    sites: set[tuple[float, float]] = set()
    for p in board.pads:
        dest = outside.get(p.net)
        if p.ref not in bga or p.net not in pair_nets or not dest:
            continue
        cx = sum(x for x, _ in dest) / len(dest)
        cy = sum(y for _, y in dest) / len(dest)
        dx = -half if cx < p.x else half  # 目的地象限的对角点（平局取正向）
        dy = -half if cy < p.y else half
        sites.add((round(p.x + dx, 3), round(p.y + dy, 3)))
    return tuple(sorted(sites))


def route_l2(
    board: RouteBoard, region: Region, spec,
    *,
    widths: dict[str, float] | None = None,
    runner=None,
    workdir: str | Path | None = None,
    timeout_s: float = 600.0,
    pin_refs: frozenset[str] | None = None,
) -> tuple[list[Segment], list[Via], list[NetReport]]:
    """L2 区域布线：DSN 导出 → best-of-N freerouting → SES 铜回导。

    返回 (segments, vias, reports)；有未布通网（全部尝试后）→
    ValueError 点名；全部尝试都失败 → FreerouteError 汇总。自定义线宽
    网（spec min_width 命中本区）暂不支持 → ValueError（M3-6 处理）。
    workdir 给定时落 ``l2_<区>.dsn`` / ``l2_<区>_<i>.ses`` 供人工查证。
    pin_refs（互联模式）直传 export_dsn：锚盘做唯一网络 pin、全量既有
    铜转障碍（merge 互联段，语义见 export_dsn docstring）。
    """
    custom = {n: w for n, w in (widths or {}).items() if n in set(region.nets)}
    if custom:
        raise ValueError(
            f"区域 {region.name} 含自定义线宽网 {sorted(custom)}"
            "——L2 统一线宽暂不支持（M3-6）"
        )

    fr_cfg = spec.routing.get("freerouting") or {}
    attempts = max(1, int(fr_cfg.get("attempts", 3)))
    base_mp = max(1, int(fr_cfg.get("max_passes", 60)))
    schedule = [base_mp * (k + 1) for k in range(attempts)]

    by_net = board.pads_by_net()
    missing = sorted(n for n in region.nets if n not in by_net)
    if missing:
        raise ValueError(f"区域 {region.name} 的网络在板上无焊盘: {missing}")
    route_nets = [n for n in sorted(region.nets) if len(by_net[n]) >= 2]
    if not route_nets:
        return [], [], []

    # 层表来自 spec（2/4 层 = F/B 缺省；N 层 = signal_layers）；
    # 狗骨预约 = 差分网 BGA 逃逸孔位（FR 不得偷占，见 _dogbone_keepouts）
    bundle = export_dsn(board, region,
                        layers=tuple(getattr(spec, "signal_layers", ())
                                     or ()) or ("F.Cu", "B.Cu"),
                        keepout_sites=_dogbone_keepouts(board, spec),
                        pin_refs=pin_refs)
    inv = {v: k for k, v in bundle.net_map.items()}
    allowed = set(route_nets)

    if workdir is not None:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / f"l2_{region.name}.dsn").write_text(
            bundle.text, encoding="utf-8")

    run = runner or real_runner
    best: _Attempt | None = None
    errors: list[str] = []
    for i, mp in enumerate(schedule):
        try:
            ses_text = run(bundle.text, mp)
        except (FreerouteError, OSError, subprocess.SubprocessError) as e:
            errors.append(f"mp={mp}: {e}")
            continue
        if workdir is not None:
            (workdir / f"l2_{region.name}_{i}.ses").write_text(
                ses_text, encoding="utf-8")
        raw_segs, raw_vias = parse_ses(ses_text, via_padstacks=bundle.via_padstacks)
        if not raw_segs and not raw_vias and route_nets:
            # FR 对 DSN 解析失败会静默回退内置 tutorial 板再"布通"，SES
            # network_out 为空、退出码 0——按尝试失败记，不当未布通误报
            errors.append(
                f"mp={mp}: SES 无任何布线铜——FR 疑似 DSN 解析失败回退 "
                "tutorial 板（人工复跑 java -jar … -de l2_*.dsn 看解析告警）")
            continue
        segs = [Segment(s.x1, s.y1, s.x2, s.y2, s.width, s.layer, inv[s.net])
                for s in raw_segs if inv.get(s.net) in allowed]
        vias = [Via(v.x, v.y, v.size, v.drill, inv[v.net])
                for v in raw_vias if inv.get(v.net) in allowed]
        unrouted = tuple(
            n for n in route_nets if not _pads_connected(board, segs, vias, n))
        length = round(sum(
            math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs), 3)
        cand = _Attempt(mp, (len(unrouted), len(vias), length),
                        unrouted, tuple(segs), tuple(vias))
        if best is None or cand.score < best.score:  # 平局先到先得
            best = cand
        if not unrouted and not vias:  # 已达理论最优（0 未布通 0 孔）提前收
            break

    if best is None:
        raise FreerouteError(
            f"区域 {region.name} 全部 {attempts} 次尝试失败:\n  "
            + "\n  ".join(errors))
    if best.unrouted:
        raise UnroutedNets(
            f"区域 {region.name} best-of-{attempts}（mp={best.max_passes}）"
            f"仍未布通 {len(best.unrouted)} 网: {list(best.unrouted)}",
            best.unrouted)

    segs, vias = list(best.segments), list(best.vias)
    reports = []
    for net in route_nets:
        n_segs = [s for s in segs if s.net == net]
        reports.append(NetReport(
            net, len(by_net[net]), len(by_net[net]) - 1,  # 边 = 连通所需连接数下界
            len(n_segs), sum(1 for v in vias if v.net == net),
            round(sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in n_segs), 3),
        ))
    return segs, vias, reports
