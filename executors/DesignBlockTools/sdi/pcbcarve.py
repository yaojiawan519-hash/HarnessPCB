"""PCB 侧 carve —— 连布局带铜一起抠。

输入：一块别人画的板 + 一组件位号（块的成员）
输出：① 只含这些件 + 这些件之间铜的新 .kicad_pcb
     ② 一个卡片（件 / 相对坐标 / 朝向 / 口岸 / 断头点）

三条口径（都不是拍脑袋，写在这里防止漂）：
  * **件**：按位号点名，不按几何聚类——板是密排的，几何聚类会把 96 件并成 1 簇
    （esp-rust-board 实测，见项目记忆 pcb-carve-direction）。
  * **铜**：只保留"块的网"（有焊盘落在这组件里的网）且在**铜窗口**内的那部分。
    窗口 = 块的封装包围盒外扩 COPPER_PAD。窗口外的铜不属于这块（它是别的块的走线，
    或者根本是全板干线）；跨窗口的段在窗口处截断，**削剩的残迹丢弃**——
    那截长度只是 COPPER_PAD 的函数，不是人的设计。口岸不靠残迹，装板时从器件焊盘引出
    （2026-10-30 用户拍板）。丢之前先自检：残迹若承载焊盘之间的连通就照留。
    **铺铜不删、也不重画**：把覆盖这块的那几张 zone 原样剪到块的地盘再重灌——
    形状/层/优先级/间距都是人画的，连"给 SW 节点单独铺一块铜来载流"这种设计
    决定也一起带进块里。只扣走线、把铺铜删掉，块内必断（实测 25 条未连通）。
  * **保真**：件的位置/朝向逐件比、保留下来的铜逐段比（端点/层/线宽/网）。
    缺一件、少一段都是失败——不"差不多就行"。
  * **口岸门**：自己按**形状级**连通把每个网的铜分成岛（KiCad 的连通就是形状级的，
    照点重合写必错，见 copper_islands 的注释）。判据：**内部网必须是一整个岛**
    （裂开 = 块内真断），**口岸网的每个岛必须含焊盘**（不含焊盘的岛是悬空铜，接不出去）。
    不用 DRC 的坐标判——KiCad 的 unconnected 标记落在 ratsnest 锚点（块内结点）上，
    不在出口，按它判会永远蒙对。DRC 只打印出来当参考。
"""
import argparse
import collections
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pcbnew

try:  # 带 zone 的板：net/zone SWIG 调用无 wxApp 会 stdpbase 断言（仓库通用坑）
    import wx  # 须持引用——不持住则 GC 销毁 C++ app，后续 Get() 仍断言
    _app = wx.App(False)
    # ⚠ 10-30 踩到：老板那句 "Legacy zone fill strategy is not supported anymore" 会被 wx
    #   的图形日志攒着，等解释器退出（wxEntryCleanup → SetActiveTarget(NULL) → wxLogGui::Flush）
    #   才弹**模态对话框**。表现是脚本在后台挂死、其实活早就干完了（实测进程 0.4s CPU、
    #   卡在 -[NSAlert runModal]、stdout 全在缓冲里没出来）。门禁脚本绝不能挂在弹窗上：
    #   日志改走 stderr。
    wx.Log.SetActiveTarget(wx.LogStderr())
except Exception:
    pass

MM = 1e6
MARGIN = 5.0          # 新板边框离块外框的余量 mm
EDGE_CLEAR = 2.0      # 块外框到板边至少留这么宽（口岸走线要地方）
COPPER_PAD = 0.8      # 铜窗口 = 件包围盒外扩这么多 mm
COPPER_PAD_SLACK = 0.05   # 内部网自动加宽时的余量（量到的是铜的包围盒，留一点不留到边）
COPPER_PAD_CAP = 5.0      # 自动加宽上限；超过要人显式 --copper-pad
PLANE_AREA_FRAC = 0.4     # 铺铜框面积 ≥ 块地盘的这一比例 → 整板级平面（整张删）
VIA_MARGIN = 2.0          # 只追件框外这么远以内的过孔（见"铜窗口：过孔"那段）
MIN_LEN = 0.05        # 截断后短于这个长度的段丢弃
KICAD_CLI = os.environ.get('HWS_KICAD_CLI',
                           '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')
PORT_TOL = 0.05       # 离记录的断头多近才算"这就是那个口岸"
ISO_TOL = 0.005       # 判"端点接上东西了"的容差 mm（KiCad 的连通是形状级的，点可以差几个 µm）
ZONE_BITE = 0.6       # 判"铜贴到铺铜"时允许的间隙 mm：铺铜与焊盘之间是热焊盘间隙而不是开路
# 块单独拿出来看，口岸那一头永远悬着（对面在块外），所以这几类必然报。
DANGLING = ('unconnected_items', 'track_dangling', 'via_dangling')



def mm(v):
    return v / MM


def v2(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def sch_refs(sch_path):
    """块原理图里出现过的位号（用来分辨"源板没这份"和"位号写错了"）。"""
    t = Path(sch_path).read_text(encoding='utf-8', errors='replace')
    return set(re.findall(r'\(property "Reference" "([A-Z]{1,3}[0-9]+)"', t))


def member_set(board, members, sch=None):
    """(板上成员, 未上板成员)。

    **"原理图画了、板上没有这份"是源板的事实，不是错误**（2026-10-30 BP5 逼出来的）：
    `bpu-iobufffer` 页画了一只 10K 的 `R404`（`VREF_VOUT` 对地泄放），而同一工程
    `bpu-current_limiter` 页也有一只 `R404`；整板只有一份封装，那只是电流限制的
    反馈电阻（`VREG_OUT`/`Net-(U403-ADJ)`，133K）。两只重号 ⇒ 泄放那只**板上不存在**。
    这种件照样进块卡（原理图是它的真相），但不该参与任何板上比较，所以要单独点名：
    旧代码直接 `SystemExit`，等于让整块裁不出来。
    判据只用"块原理图里有没有这个位号"——**有 = 未上板（打印+落盘），没有 = 位号写错（照旧报错）**。
    """
    have = {f.GetReference(): f for f in board.GetFootprints()}
    missing = [r for r in members if r not in have]
    not_built: list = []
    if missing:
        if not sch:
            raise SystemExit(f'✗ 板上没有这些位号：{", ".join(missing)}（没给 --sch，无法判断是否未上板）')
        in_sch = sch_refs(sch)
        typo = [r for r in missing if r not in in_sch]
        if typo:
            raise SystemExit(f'✗ 板上没有、块原理图里也没有这些位号：{", ".join(typo)}')
        not_built = sorted(missing)
        print(f'  ⚠ 未上板（原理图有、源板没有这只封装）：{", ".join(not_built)}'
              f' —— 源板自己就没这份，不参与板上比较，卡里记 counts.not_built')
    return [have[r] for r in members if r in have], not_built


def block_box(items):
    xs1 = ys1 = float('inf')
    xs2 = ys2 = float('-inf')
    for it in items:
        bb = it.GetBoundingBox()
        xs1 = min(xs1, mm(bb.GetLeft()))
        ys1 = min(ys1, mm(bb.GetTop()))
        xs2 = max(xs2, mm(bb.GetRight()))
        ys2 = max(ys2, mm(bb.GetBottom()))
    return xs1, ys1, xs2, ys2


def snapshot(fps, ox, oy):
    out = []
    for f in sorted(fps, key=lambda f: f.GetReference()):
        p = f.GetPosition()
        out.append({
            'ref': f.GetReference(), 'value': f.GetValue(),
            'fp': f.GetFPID().GetLibItemName(),
            'dx': round(mm(p.x) - ox, 4), 'dy': round(mm(p.y) - oy, 4),
            'rot': round(f.GetOrientationDegrees() % 360, 2),
            'side': 'B' if f.IsFlipped() else 'F',
        })
    return out


def clip(x1, y1, x2, y2, rx1, ry1, rx2, ry2):
    """Liang–Barsky：把线段裁到矩形内。返回 None 或 (ax,ay,bx,by)。"""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rx1), (dx, rx2 - x1), (-dy, y1 - ry1), (dy, ry2 - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    return (x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy)


# ── 纸与标题栏 ──────────────────────────────────────────────────────────────
def retitle(pcb_path, name, ex2, ey2, src_name):
    """纸跟着块走 + 标题栏写清"这是块"和从哪来。

    原板的 `(paper "A4")` 会把 16×13 的块塞进 A4 一角——实测导出的图里块只占
    左上角巴掌大，右边全是空白和别人的图框。改成 User 纸 = 板子 + 边距，
    1:1 出图。
    ⚠ `company` 保留上游公司名不动：那是人家的图，改掉等于抹掉出处。
    """
    text = open(pcb_path, encoding='utf-8').read()
    w, h = ex2 + 2.0, ey2 + 2.0
    text = re.sub(r'\(paper\s+"[^"]*"(?:\s+[\d.]+){0,2}\)',
                  '(paper "User" %s %s)' % (f'{w:g}', f'{h:g}'), text, count=1)
    text = re.sub(r'\(title\s+"[^"]*"\)', '(title "%s")' % name, text, count=1)
    text = re.sub(r'(\(company\s+"[^"]*"\))',
                  r'\1\n\t\t(comment 1 "carved from %s")' % src_name, text, count=1)
    open(pcb_path, 'w', encoding='utf-8').write(text)


# ── sch↔pcb 同一块电路的门 ──────────────────────────────────────────────────
# 不比网名，比**"谁和谁同网"**：原理图里 FB 那根是无名的，PCB 里叫
# `Net-(C6-Pad2)`，名字对不上但电路是同一块。两边的分区集合相等 ⇔ 就是同一块电路。
def sch_partition(sch_path, refs):
    """原理图侧：{(ref,pin) 的集合}[]，只看分组不看名字。"""
    tools = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import extract                                    # noqa: PLC0415
    il = extract.extract(str(sch_path))
    parts = set()
    for n in il.get('nets') or []:
        pins = {(str(r), str(p)) for r, p in (n.get('pins') or []) if str(r) in refs}
        # **空脚不是网**（10-30，与 `sdi.blockpcb.read_sch` / `sdi.cardpcb` 同一条规则）：
        # 原理图给"没人接的脚"留一张**无名单脚网**，而板上那个焊盘 NetCode=0、
        # 根本不在 `pcb_partition` 里。不归一化，两边永远差这一只脚——
        # Watchy 的 micro-USB 座 J2.6 就是这么报出来的（"只在原理图: [('J2','6')]"）。
        if len(pins) == 1 and not n.get('name'):
            continue
        if pins:
            parts.add(frozenset(pins))
    return parts


def net_pad_totals(board):
    """整板每张网有几个**不同的脚**（按**网名**数：裁块后网号会重排，网名不会）。

    ⚠ 必须在**源板被改之前**取（`main` 后半段是把源板就地删成块板再另存的）。

    ⚠ 数的是 `(位号, 脚号)` 的**集合大小**，不是焊盘个数（2026-10-30 RS232 板逼出来的）：
    DB9 封装的屏蔽壳是**同一只封装里两个 pad 都叫 "0"**，KiCad 把它们并成一张
    `unconnected-(J1-PAD-Pad0)` 网 → 焊盘数 2、"两个不同的脚"数 1。按焊盘数算就会
    把这脚判成"有伙伴"（不是空脚），而原理图侧那脚只有一只 → 两边差一组。
    """
    pairs = {}
    for f in board.GetFootprints():
        for p in f.Pads():
            n = p.GetNetname()
            if p.GetNetCode() > 0 and n:
                pairs.setdefault(n, set()).add((f.GetReference(), str(p.GetNumber())))
    return {n: len(v) for n, v in pairs.items()}


def pcb_partition(board, refs, totals=None):
    """PCB 侧：同样的口径。

    空脚归一（10-30，与 `sch_partition` 那条对称）：源板上没人接的焊盘，
    KiCad 有时给 `NetCode=0`（真空白），有时给一张 `Net-(J3-Pad4)` 这种**自动名**网。
    两种写法是同一件事——"这脚没接"。不归一，两边分组必然差这几只脚
    （Watchy 的 epaper 块实测 J3 的 1/4/6/7/19 五个空脚只出现在 PCB 侧）。
    """
    groups, names = {}, {}
    total = totals if totals is not None else net_pad_totals(board)
    for f in board.GetFootprints():
        r = f.GetReference()
        if r not in refs:
            continue
        for p in f.Pads():
            if p.GetNetCode() <= 0:
                continue
            groups.setdefault(p.GetNetCode(), set()).add((r, str(p.GetNumber())))
            names[p.GetNetCode()] = p.GetNetname()
    # 自动名的两种写法都算空脚：老板 `Net-(J3-Pad4)`、新板 `unconnected-(J2-ID-Pad4)`。
    # **但只在整板真的只有这一个焊盘时才算**（2026-10-30 SIM 板补）：块只扣了一部分器件，
    # 一根跨块的线在块里只剩一只脚，名字同样是 KiCad 现编的 `Net-(D1-K)`——
    # 那根线**接着块外的 D1.1**，不是空脚。原来的写法只看块内脚数，把它当空脚丢掉，
    # 而原理图侧因为 `carve` 的 `expose:` 给它起了名（`SIM_CD`）所以留着 → 两边差一组。
    return {frozenset(v) for k, v in groups.items()
            if not (len(v) == 1 and total.get(names[k], 1) == 1
                    and names[k].startswith(('Net-(', 'unconnected-')))}


# ── 口岸门：判"没连通的只剩口岸" ────────────────────────────────────────────
def drc_report(pcb_path):
    """跑 kicad-cli DRC，返回 [{'type':…, 'pts':[(x,y),…]}]；没有 kicad-cli 返回 None。"""
    if not os.path.exists(KICAD_CLI):
        return None
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'drc.json')
        subprocess.run([KICAD_CLI, 'pcb', 'drc', '--format', 'json', '--severity-all',
                        '--output', out, pcb_path], capture_output=True, text=True)
        if not os.path.exists(out):
            return None
        with open(out, encoding='utf-8') as fh:
            d = json.load(fh)
    rows = []
    for v in list(d.get('violations') or []) + list(d.get('unconnected_items') or []):
        pts = [(i['pos']['x'], i['pos']['y']) for i in (v.get('items') or []) if 'pos' in i]
        rows.append({'type': v.get('type'), 'pts': pts})
    return rows


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _seg_seg_dist(a, b, c, d):
    """两线段的最短距离；相交则为 0。"""
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_seg_dist(a[0], a[1], c[0], c[1], d[0], d[1]),
               _seg_dist(b[0], b[1], c[0], c[1], d[0], d[1]),
               _seg_dist(c[0], c[1], a[0], a[1], b[0], b[1]),
               _seg_dist(d[0], d[1], a[0], a[1], b[0], b[1]))


def _pt_rect_dist(p, box):
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return (dx * dx + dy * dy) ** 0.5


def _pt_seg_dist(p, a, b):
    """点到线段的最短距离（判端点是不是落在别的段上）。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0.0:
        return ((p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return ((p[0] - (a[0] + t * dx)) ** 2 + (p[1] - (a[1] + t * dy)) ** 2) ** 0.5


def _seg_rect_dist(a, b, box):
    """线段到矩形的最短距离；穿过矩形则为 0。"""
    if (_pt_rect_dist(a, box) == 0.0) or (_pt_rect_dist(b, box) == 0.0):
        return 0.0
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    best = min(_pt_rect_dist(a, box), _pt_rect_dist(b, box))
    for k in range(4):
        best = min(best, _seg_seg_dist(a, b, corners[k], corners[(k + 1) % 4]))
    return best


def pads_partition(segs, pads, tol=PORT_TOL):
    """只按几何算"哪些焊盘连成一组"，用来判断**残迹能不能丢**。

    segs: [(网, (ax,ay), (bx,by), 半径)]；pads: [(网, 中心, 矩形)]
    返回 frozenset(frozenset(焊盘序号))——只关心分组，不关心组叫什么。
    丢残迹前后这个分区若相等，说明那些残迹不承载任何焊盘之间的连通，可以丢。
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(pads)):
        find(('p', i))
    for j, (ns, a, b, r) in enumerate(segs):
        find(('s', j))
        for i, (np_, _c, box) in enumerate(pads):
            if np_ == ns and _seg_rect_dist(a, b, box) <= r + tol:
                union(('s', j), ('p', i))
        for k in range(j + 1, len(segs)):
            nt, c, d, s = segs[k]
            if ns == nt and _seg_seg_dist(a, b, c, d) <= r + s + tol:
                union(('s', j), ('s', k))
    groups = {}
    for i in range(len(pads)):
        groups.setdefault(find(('p', i)), set()).add(i)
    return frozenset(frozenset(v) for v in groups.values())


def _seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 <= 1e-18:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return ((px - ax - t * vx) ** 2 + (py - ay - t * vy) ** 2) ** 0.5


def copper_islands(board, nets, win, tol=PORT_TOL):
    """把每个网的铜按**几何连通**分成岛，判"每个岛都有出路"。

    换掉原来那道"断点是否落在断头记录上"的门，原因（2026-10-30 实测）：
    KiCad 的 unconnected 标记落在**块内结点**上（ratsnest 的锚点），不在出口上。
    于是只要断头记录里混进块内结点，这道门就永远蒙对——
    删掉三段块内残迹时它照样判"块内真断 0 处"。没有分辨力的门比没有门更坏。

    新判据是构造性的、能失败也能通过：**岛的出口 = 触到铜窗口边界的走线端点/过孔**。
    块单独拿出来，每个岛必然有出口（对面在块外）；一个岛若整段都在块内，
    它就是把焊盘困住的真断。

    返回 {net: [(件数, 出口数, 代表点), …]}
    """
    wx1, wy1, wx2, wy2 = win
    items = []

    def on_edge(x, y):
        if not (wx1 - tol <= x <= wx2 + tol and wy1 - tol <= y <= wy2 + tol):
            return False
        return (min(abs(x - wx1), abs(x - wx2)) <= tol or
                min(abs(y - wy1), abs(y - wy2)) <= tol)

    zones = [(z, l) for z in board.Zones() if z.GetNetname() in nets and z.IsFilled()
             for l in z.GetLayerSet().Seq()]

    geo = []      # (网, 起点, 终点, 半径, item 索引)——KiCad 的连通是**形状级**的
    for t in board.GetTracks():
        n = t.GetNetname()
        if n not in nets:
            continue
        if isinstance(t, pcbnew.PCB_VIA):
            c = t.GetPosition()
            a = (c.x / MM, c.y / MM)
            items.append(('via', n, a, None))
            geo.append((n, a, a, mm(t.GetWidth(pcbnew.F_Cu)) / 2, len(items) - 1))
        else:
            s, e = t.GetStart(), t.GetEnd()
            a, b = (s.x / MM, s.y / MM), (e.x / MM, e.y / MM)
            items.append(('seg', n, a, b))
            geo.append((n, a, b, mm(t.GetWidth()) / 2, len(items) - 1))
    padrec = []      # (网, 矩形, item 索引)——焊盘按**矩形**算距离，不用半径糊
    for f in board.GetFootprints():
        for p in f.Pads():
            n = p.GetNetname()
            if n in nets:
                c = p.GetPosition()
                bb = p.GetBoundingBox()
                a = (c.x / MM, c.y / MM)
                items.append(('pad', n, a, None))
                # 同 `pads_geo`：y 方向必须 min/max 归一，`GetBottom()` 是 y 大的那头
                padrec.append((n, (min(bb.GetLeft(), bb.GetRight()) / MM,
                                   min(bb.GetTop(), bb.GetBottom()) / MM,
                                   max(bb.GetLeft(), bb.GetRight()) / MM,
                                   max(bb.GetTop(), bb.GetBottom()) / MM), len(items) - 1))
    for zi, (z, l) in enumerate(zones):
        items.append(('zone', z.GetNetname(), None, (zi, l)))

    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, it in enumerate(items):
        find(('it', i))

    # ⚠ 判连通不能按"端点坐标精确相等"：原板里两根同网走线端点可以差 0.002~0.005mm
    #   （实测 FB 网 13.399 vs 13.397），KiCad 照样算通，因为它按**形状重叠**判。
    #   早先按点重合判，把 FB/ENABLE 这种网整个判裂，误报 "困死"。
    for i in range(len(geo)):
        ni, a1, b1, r1, ii = geo[i]
        for j in range(i + 1, len(geo)):
            nj, a2, b2, r2, jj = geo[j]
            if ni == nj and _seg_seg_dist(a1, b1, a2, b2) <= r1 + r2 + tol:
                union(('it', ii), ('it', jj))
        for (np_, box, jj) in padrec:     # 走线/过孔碰到焊盘矩形
            if np_ == ni and _seg_rect_dist(a1, b1, box) <= r1 + tol:
                union(('it', ii), ('it', jj))
    zone_item = [i for i, it in enumerate(items) if it[0] == 'zone']
    polys = [(z.GetNetname(), z.GetFilledPolysList(l), zone_item[k])
             for k, (z, l) in enumerate(zones)]
    for i, (k1, n1, a, b) in enumerate(items):
        if k1 == 'zone' or n1 not in nets:
            continue
        for (n2, poly, j) in polys:      # 铜碰到铺铜填充区 = 同一个岛
            if n2 != n1:
                continue
            # ⚠ 铺铜对焊盘留了**热焊盘间隙**，焊盘中心落在填充多边形之外（是它的洞）。
            #   按 0 间距 Contains 判，焊盘和连它的铺铜永远判不成一体 → 整网被判裂。
            if any(poly.Collide(v2(px, py), int(ZONE_BITE * MM))
                   for (px, py) in ([a] + ([b] if b else []))):
                union(('it', i), ('it', j))

    isl = {}
    for i, (kind, n, a, b) in enumerate(items):
        if kind == 'zone':
            continue
        r = find(('it', i))
        rec = isl.setdefault((n, r), {'n': 0, 'exit': 0, 'at': a, 'pad': 0})
        rec['n'] += 1
        if kind == 'pad':
            rec['pad'] += 1
        if kind == 'seg':
            rec['exit'] += int(on_edge(*a)) + int(on_edge(*b))
        elif kind == 'via':
            rec['exit'] += int(on_edge(*a))
    out = {}
    for (n, _r), rec in isl.items():
        out.setdefault(n, []).append(rec)
    for n in out:
        out[n].sort(key=lambda r: -r['n'])
    return out


def stranded_islands(islands, ports, iface=()):
    """挑出真困死的岛。

    口径（10-30 用户拍板"残迹去掉、装板时从器件焊盘引出"之后）：
      * **内部网**（块外没有件）——必须**只有一个岛**。裂成两个岛就是块内真断，
        这条最有分辨力，负向测试过（掐掉 FB 网中间一段 → 1 岛裂成 2 岛，立刻报错）。
      * **口岸网**（块外还挂着件）——可以裂成多个岛（每个岛装板时各自从焊盘引出去），
        但**每个岛必须含焊盘**：不含焊盘的岛是悬空铜，没有任何地方能接它。
      * `iface`（`--iface` 声明的**接口网**）——按口岸网同一口径。
        道理一样：这块板的所有网都落在接插件脚上，块外没有同网件只是因为
        **这一页上没有别的器件**，不代表它不出块。判据不是名字好听，而是
        "每个岛都含焊盘"——含焊盘就一定能从脚上引出去。
    返回 [(网, 岛, 原因)]。
    """
    hard = set(iface)
    bad = []
    for n, rs in islands.items():
        if ports.get(n) or n in hard:
            bad += [(n, r, '这个岛没有焊盘，接不出去') for r in rs if r['pad'] == 0]
        elif len(rs) > 1:
            bad += [(n, r, f'内部网裂成 {len(rs)} 个岛') for r in rs]
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', required=True)
    ap.add_argument('--members', required=True, help='逗号分隔的位号')
    ap.add_argument('--name', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--placement-only', action='store_true', help='不抠铜')
    ap.add_argument('--sch', help='块自己的 .kicad_sch：给了就加一道"sch↔pcb 同一块电路"门')
    ap.add_argument('--not-built',
                    help='逗号分隔的位号：原理图画了、**板上这位号是另一只件**（源板重号），'
                         '本块这只没上板。名单进 counts.not_built，不参与板上比较。'
                         '（板上压根没有这个位号时会自动识别，不用写这里）')
    ap.add_argument('--keep-stub', action='store_true',
                    help='保留被窗口削剩的走线残迹。默认**丢掉**：残迹的长度只是 COPPER_PAD '
                         '的函数、不是人的设计；口岸在装板时从器件焊盘引出（10-30 用户拍板）。'
                         '会先自检"丢了会不会把焊盘之间连断"，会断的照样留着')
    ap.add_argument('--copper-pad', type=float, default=None,
                    help='铜窗口 = 件包围盒外扩多少 mm。默认 0.8，且**内部网**的铜如果伸出件框 '
                         '比这个远就自动加宽到"刚好装下"（打印量到的 mm）。给了这个值就照用，'
                         '不再自动加宽')
    ap.add_argument('--keep-pour', action='store_true',
                    help='保留人铺的铺铜。默认**不保留**（10-30 用户拍板）：块的铺铜会盖住整块地盘，'
                         '拼板时别的块的走线没地方过；铺铜是整板级的决定，拼完再铺一次')
    ap.add_argument('--iface',
                    help='逗号分隔的网名：这些网在源板上"块外没有件"只是因为这页没有别的器件，'
                         '实际上它们全部落在接插件脚上、出块（Blank Plank 的 IO0..IO7 / GND）。'
                         '声明后按口岸网判：岛可以裂，但**每个岛必须含焊盘**（能引出去）')
    a = ap.parse_args()
    drop_stub = not a.keep_stub

    members = [s.strip() for s in a.members.split(',') if s.strip()]
    src = pcbnew.LoadBoard(a.board)
    pad_totals = net_pad_totals(src)      # 源板的网尺，改板之前取（见 net_pad_totals）
    plane_nets = {z.GetNetname() for z in src.Zones()}   # 人铺过铜的网＝平面网
    fps, not_built = member_set(src, members, a.sch)
    declared = [s.strip() for s in (a.not_built or '').split(',') if s.strip()]
    bad_decl = [r for r in declared if r not in members]
    if bad_decl:
        raise SystemExit(f'✗ --not-built 里的位号不在 --members 里：{", ".join(bad_decl)}')
    if declared:
        print(f'  ⚠ 按命令行声明未上板：{", ".join(sorted(declared))}'
              f' —— 源板上这位号是另一只件（重号），本块这只不参与板上比较')
        not_built = sorted(set(not_built) | set(declared))
    board_members = [r for r in members if r not in set(not_built)]
    have = set(board_members)
    # 板上那只"撞号"的件必须从成员里摘掉，否则它的网/铜会被当成本块的
    fps = [f for f in fps if f.GetReference() in have]

    # 块的网 = 有焊盘落在这组件里的网
    block_nets = set()
    net_of = {}
    for f in fps:
        for p in f.Pads():
            n = p.GetNetname()
            if n:
                block_nets.add(n)
    owner = {}
    for f in src.GetFootprints():
        for p in f.Pads():
            n = p.GetNetname()
            if n:
                owner.setdefault(n, set()).add(f.GetReference())

    print(f'块 {a.name}：{len(fps)} 件 / {len(block_nets)} 网')
    ports = {n: sorted(owner.get(n, set()) - have) for n in block_nets
             if owner.get(n, set()) - have}
    for n in sorted(ports):
        print(f'  口岸 {n:<16} 块外还挂 {len(ports[n])} 件')
    iface = {s.strip() for s in (a.iface or '').split(',') if s.strip()}
    miss = iface - block_nets
    if miss:
        raise SystemExit(f'✗ --iface 里的网不在这块板上：{"、".join(sorted(miss))}')
    if iface:
        print(f'  接口网（--iface，按口岸网判：岛可裂但每个岛必须含焊盘）：'
              f'{"、".join(sorted(iface))}')

    x1, y1, x2, y2 = block_box(fps)
    ox, oy = (x1 + x2) / 2, (y1 + y2) / 2
    w, h = x2 - x1, y2 - y1
    # 铜窗口外扩量：默认 COPPER_PAD，但**内部网**（块外一件都不挂的网）的铜必须整根留下——
    # 它没有口岸，窗口割断它就是块内真断（UPduino 的 +1.2V_CORE / 两个 VCCIO 跳线网
    # 在件框外 1.64mm 处绕了一下，0.8 的窗口把三根内部网全割成两半）。
    # 所以这里先量"内部网的铜伸出件框多远"，窗口按这个值加宽，并把数字打印出来；
    # 伸得太远（> CAP）说明这块的内部网在源板上就是横穿整板的干线，得人给 `--copper-pad`。
    pad = COPPER_PAD if a.copper_pad is None else a.copper_pad
    need, who = 0.0, None
    need_pl, who_pl = 0.0, None      # 平面网那一路单独记（见下）
    board_planes = set()             # 整板级铺铜的网（铜伸出件框 > CAP），见下面的铺铜门
    for n in sorted(block_nets):
        if owner.get(n, set()) - have:        # 有口岸 → 不是内部网
            continue
        bx1 = by1 = float('inf')
        bx2 = by2 = float('-inf')
        for t in src.GetTracks():
            if t.GetNetname() != n:
                continue
            pts = [t.GetPosition()] if t.Type() == pcbnew.PCB_VIA_T else [t.GetStart(), t.GetEnd()]
            for p in pts:
                bx1, by1 = min(bx1, p.x / MM), min(by1, p.y / MM)
                bx2, by2 = max(bx2, p.x / MM), max(by2, p.y / MM)
        for p in src.GetPads():
            if p.GetNetname() != n:
                continue
            bb = p.GetBoundingBox()
            bx1, by1 = min(bx1, bb.GetLeft() / MM), min(by1, bb.GetTop() / MM)
            bx2, by2 = max(bx2, bb.GetRight() / MM), max(by2, bb.GetBottom() / MM)
        if bx1 == float('inf'):
            continue
        d = max(x1 - bx1, y1 - by1, bx2 - x2, by2 - y2)
        if n in plane_nets:                    # 平面网（人铺过铜的网）另账
            if d > need_pl:
                need_pl, who_pl = d, n
            if d > COPPER_PAD_CAP:
                board_planes.add(n)
        elif d > need:
            need, who = d, n
    # **平面网的铜不算"必须整根留下"**（2026-10-30 Blank Plank 逼出来的）。
    # 上面那条加宽是为了救"块内信号线在件框外绕了一下"——它没有口岸，割断就是真断。
    # 平面网不是这个形态：它铺满整板，块外那截铜是**铺铜的缝合孔/汇流排**，而铺铜按
    # 用户定的「块内不带铺铜」整张删、拼板时整板重铺接上。照老规矩把窗口撑到 26.59mm，
    # 等于把整块洞洞板搬进块里。下面那条"追过孔"的循环本来就跳过 plane_nets，同一个道理。
    #
    # 只在**超出上限**时才豁免：局部铺铜（usb_power 的 VBUS/+5V 那两张）的铜本来就在件框内，
    # 走老路；只有"伸出去 > CAP"这种整板级平面铜才走新路——老代码到那儿是硬报错，
    # 所以这条改动不可能改变任何已经裁过的块。
    if who_pl and need_pl > COPPER_PAD_CAP and a.copper_pad is None:
        print(f'  铜窗口：平面网 {who_pl} 的铜伸到件框外 {need_pl:.2f}mm——那是整板铺铜的'
              f'缝合孔/汇流排，**不撑窗口**（铺铜按规矩整张删，拼板时整板重铺接上）；'
              f'窗口按其余内部网算 {max(need, pad):.2f}mm')
        need_pl = 0.0
    if need_pl > need:
        need, who = need_pl, who_pl
    if need > pad:
        if a.copper_pad is None and need > COPPER_PAD_CAP:
            raise SystemExit(
                f'✗ 内部网 {who} 的铜伸出件框 {need:.2f}mm（上限 {COPPER_PAD_CAP}mm）'
                f'——窗口不能自动加这么宽，请显式给 --copper-pad')
        print(f'  铜窗口：内部网 {who} 的铜伸到件框外 {need:.2f}mm，窗口外扩 '
              f'{pad:.2f} → {need + COPPER_PAD_SLACK:.2f}mm（内部网必须整根留下）')
        pad = need + COPPER_PAD_SLACK

    # 第二种加宽：**过孔**（10-30，UPduino clock_12m 踩到）。
    # 口沿常见走法是"焊盘 → 0.x mm 短线 → 打孔换层"，孔心落在件框外一点点
    # （SJ16 的孔在件框外 0.93mm，默认 0.80 的窗口差 0.13mm 就够不着）。孔被裁掉后，
    # 焊盘这侧只剩一截够不着的短线；而"丢残迹"的判据只看**焊盘之间**的连通——
    # 单焊盘的网本来就没有"之间"，于是那截线被当垃圾丢掉，**这个脚静悄悄没了铜**。
    # 块自己不自知：信号裸脚那栏只查"块内 ≥2 个脚"的网，单脚网直接跳过。
    # 所以：块的网里，凡与窗口内某段铜/某个焊盘接得上的过孔，孔心必须进窗口。
    via_pad = pad
    for _round in range(16):
        rx1, ry1, rx2, ry2 = x1 - via_pad, y1 - via_pad, x2 + via_pad, y2 + via_pad
        # ⚠ 判"这个孔挨不挨得上铜"必须用**未裁剪**的段：裁剪后段的端点被挪到窗口边上，
        #   离孔几十微米还是几百微米全看窗口恰好划在哪——UPduino SJ16 的孔就这么
        #   差 0.01mm 判成"挨不上"，加宽一次都没触发。段有没有进窗口另用 clip 判。
        segs_in = []
        for t in list(src.GetTracks()):
            if t.Type() == pcbnew.PCB_VIA_T or t.GetNetname() not in block_nets:
                continue
            s, e = t.GetStart(), t.GetEnd()
            if clip(mm(s.x), mm(s.y), mm(e.x), mm(e.y), rx1, ry1, rx2, ry2) is None:
                continue                       # 整段都在窗口外
            segs_in.append((t.GetNetname(), mm(s.x), mm(s.y), mm(e.x), mm(e.y),
                            mm(t.GetWidth()) / 2))
        grew = via_pad
        for t in list(src.GetTracks()):
            if t.Type() != pcbnew.PCB_VIA_T:
                continue
            n = t.GetNetname()
            # ⚠ 只追**信号网**的过孔。轨（GND/+3V3…）的铜铺满整板，任何一个轨孔都挨着
            #   窗口里的轨铜——追下去就是"窗口一路长到板边"：flash_qspi 实测追到 14.13mm，
            #   撞上上限直接报错。轨脚没铜接本来就有 `naked_plane` 那栏在管（拼板铺铜接上），
            #   这里不管。
            if n not in block_nets or n in plane_nets:
                continue
            c = t.GetPosition()
            cx, cy = mm(c.x), mm(c.y)
            rv = mm(t.GetWidth(pcbnew.F_Cu)) / 2
            if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                continue
            near_trk = any(sn == n and _seg_dist(cx, cy, ax, ay, bx, by) <= r + rv + PORT_TOL
                           for sn, ax, ay, bx, by, r in segs_in)
            if not near_trk:                  # 也可能直接落在窗口内某个焊盘上
                for f in fps:
                    for p in f.Pads():
                        if p.GetNetname() != n:
                            continue
                        bb = p.GetBoundingBox()
                        px1, py1 = mm(min(bb.GetLeft(), bb.GetRight())), mm(min(bb.GetTop(), bb.GetBottom()))
                        px2, py2 = mm(max(bb.GetLeft(), bb.GetRight())), mm(max(bb.GetTop(), bb.GetBottom()))
                        if not (rx1 <= px1 and px2 <= rx2 and ry1 <= py1 and py2 <= ry2):
                            continue
                        if px1 - rv - PORT_TOL <= cx <= px2 + rv + PORT_TOL and \
                                py1 - rv - PORT_TOL <= cy <= py2 + rv + PORT_TOL:
                            near_trk = True
                            break
                    if near_trk:
                        break
            #   还要限定**走廊**：信号的网往往横穿整板，一个孔追出窗口、窗口再套住下一段铜，
            #   又会追出下一个孔——照这样追，flash_qspi 一路追到 8.53mm、fpga 追到 10.85mm，
            #   等于把整板拖进来。真正要救的形态只有"焊盘 → 几毫米短线 → 孔"，
            #   所以只追件框外 VIA_MARGIN 以内的孔，再远的一律当口岸（铜走到那儿就是块外了）。
            if max(x1 - cx, cx - x2, y1 - cy, cy - y2) > VIA_MARGIN:
                continue
            if not near_trk:
                continue                      # 这孔跟窗口里的铜无关，别管它
            grew = max(grew, x1 - (cx - rv), y1 - (cy - rv),
                       (cx + rv) - x2, (cy + rv) - y2)
        if grew <= via_pad + 1e-9:
            break
        via_pad = grew
    if via_pad > pad:
        if a.copper_pad is None and via_pad > COPPER_PAD_CAP:
            raise SystemExit(
                f'✗ 为了装下块网上的过孔，铜窗口要外扩 {via_pad:.2f}mm'
                f'（上限 {COPPER_PAD_CAP}mm）——请显式给 --copper-pad 定夺')
        # 数一下到底拉进来几个孔：落在"原窗口外、新窗口内"、且属于块网的孔
        n_via = sum(1 for t in list(src.GetTracks()) if t.Type() == pcbnew.PCB_VIA_T
                    and t.GetNetname() in block_nets
                    and x1 - via_pad <= mm(t.GetPosition().x) <= x2 + via_pad
                    and y1 - via_pad <= mm(t.GetPosition().y) <= y2 + via_pad
                    and not (x1 - pad <= mm(t.GetPosition().x) <= x2 + pad
                             and y1 - pad <= mm(t.GetPosition().y) <= y2 + pad))
        print(f'  铜窗口：块的网有 {n_via} 个过孔挨着块内的铜却落在窗口外，'
              f'窗口外扩 {pad:.2f} → {via_pad:.2f}mm（否则焊盘那侧只剩一截够不着的短线）')
        pad = via_pad
    wx1, wy1, wx2, wy2 = x1 - pad, y1 - pad, x2 + pad, y2 + pad
    # ⚠ 必须在 translate 之前抓快照——平移之后坐标就变了，卡片记的必须是**源板**的相对坐标。
    place = snapshot(fps, ox, oy)
    print(f'  件占地 {w:.2f}×{h:.2f}mm；铜窗口外扩 {pad:.2f}mm')

    # —— 挑铜 ——
    keep_trk, cut_points = [], []
    stub_idx = []          # keep_trk 里"被窗口削过"的段——残迹候选
    n_all = n_other = 0
    for t in list(src.GetTracks()):
        n_all += 1
        net = t.GetNetname()
        if net not in block_nets:
            n_other += 1
            continue
        if isinstance(t, pcbnew.PCB_VIA):
            c = t.GetPosition()
            cx, cy = mm(c.x), mm(c.y)
            if wx1 <= cx <= wx2 and wy1 <= cy <= wy2:
                # KiCad 10：PCB_VIA.GetWidth 必须带层参（无参走的是已废弃重载，会断言）
                keep_trk.append(('via', net, cx, cy, mm(t.GetWidth(pcbnew.F_Cu)),
                                 mm(t.GetDrillValue()),
                                 src.GetLayerName(t.TopLayer()),
                                 src.GetLayerName(t.BottomLayer()),
                                 # 过孔**接得上的所有层**（t[8]）。只记上下两层名是不够的：
                                 # 通孔在 4 层板上也连 In1/In2，判"过孔↔内层段相连"时拿上下层名
                                 # 去比必然不相等——UPduino 的 VCCIO 跳线网整根走在 In2，
                                 # 6 段被当成"没接上过孔"的死铜删掉，内部网当场裂成两半。
                                 # ⚠ 也别用 `range(TopLayer(), BottomLayer()+1)` 数 ID：这页板的
                                 #   `.kicad_pcb` 层号是**自定义的**——0=F.Cu、2=B.Cu、4=In1.Cu、
                                 #   6=In2.Cu（中间号是 F.Mask/B.Mask）。数区间会漏掉 In2。
                                 #   孔自己知道它接哪些层，问它。
                                 frozenset(src.GetLayerName(i) for i in t.GetLayerSet().Seq())))
                # ⚠ 层一律存**名字**，别存 ID：走线那边存的是 GetLayerName()（字符串），
                #   过孔若存 TopLayer()/BottomLayer()（整数），两者永远比不上，
                #   于是"过孔接走线"判不出来——去铺铜时会把 51 个过孔全当死铜删掉。
            continue
        s, e = t.GetStart(), t.GetEnd()
        sx, sy, ex, ey = mm(s.x), mm(s.y), mm(e.x), mm(e.y)
        r = clip(sx, sy, ex, ey, wx1, wy1, wx2, wy2)
        if r is None:
            n_other += 1
            continue
        ax, ay, bx, by = r
        if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 < MIN_LEN:
            n_other += 1
            continue
        touched = (abs(ax - sx) > 1e-9 or abs(ay - sy) > 1e-9 or
                   abs(bx - ex) > 1e-9 or abs(by - ey) > 1e-9)
        if touched:      # 被窗口截过 = 这里就是口岸断头
            # ⚠ 只记**真被挪动**的那一端。早先写成 `abs(bx-sx)>1e-9`（拿 b 跟 **s** 比），
            #   于是一头在外的段会把**没被切的内端**也记成断头——6 个记录里 3 个是块内结点。
            #   这直接让旧口岸门失去分辨力（KiCad 的 unconnected 标记正好落在块内结点上）。
            if abs(ax - sx) > 1e-9 or abs(ay - sy) > 1e-9:
                cut_points.append((net, round(ax, 4), round(ay, 4), t.GetLayerName()))
            if abs(bx - ex) > 1e-9 or abs(by - ey) > 1e-9:
                cut_points.append((net, round(bx, 4), round(by, 4), t.GetLayerName()))
        keep_trk.append(('seg', net, ax, ay, bx, by, mm(t.GetWidth()), t.GetLayerName()))
        if touched:
            stub_idx.append(len(keep_trk) - 1)

    # —— 残迹去留：先自检"丢了会不会把焊盘之间连断" ——
    pads_geo = []
    pads_lay = []      # 并行表：(网, 中心, 焊盘所在的铜层集合)——铺铜岛判据要按层比，见下
    for f in fps:
        for p in f.Pads():
            n = p.GetNetname()
            if n:
                pads_lay.append((n, (mm(p.GetPosition().x), mm(p.GetPosition().y)),
                                 frozenset(p.GetLayerSet().Seq())))
                c, bb = p.GetPosition(), p.GetBoundingBox()
                # ⚠ 10-30 踩到：KiCad 的 `BOX2I` 里 **`GetBottom()` 是 y 大的那头**
                #   （板坐标 y 朝下），照 (Left, Bottom, Right, Top) 装进
                #   (x1, y1, x2, y2) 会得到一个 **y1 > y2 的倒矩形**，
                #   而 `_pt_rect_dist` 那几个距离函数都假设 y1 < y2——
                #   点落在焊盘里也会算出 3.8mm 远。表现是"过孔明明压在 QFN 散热盘上，
                #   却被判成没有焊盘的死铜"（ESP32-S3 块 10 只缝合孔全中）。
                #   统一按 min/max 归一，下面所有消费者（残迹判据、裸脚判据、岛判据）共用。
                pads_geo.append((n, (mm(c.x), mm(c.y)),
                                 (min(mm(bb.GetLeft()), mm(bb.GetRight())),
                                  min(mm(bb.GetTop()), mm(bb.GetBottom())),
                                  max(mm(bb.GetLeft()), mm(bb.GetRight())),
                                  max(mm(bb.GetTop()), mm(bb.GetBottom())))))
    n_stub = 0
    n_kept_stub = 0
    if drop_stub and stub_idx:
        def _g(t):
            return (t[1], (t[2], t[3]), (t[4], t[5]), t[6] / 2)
        # ⚠ **逐段判，不是全丢或全留**（10-30 修）。原来的写法是"全部残迹一起丢，
        #   焊盘分区不变才丢，变了一点就一段都不丢"——于是只要有 1 段确实承载连通，
        #   全部残迹都被照留，块上留下一片 0.3~2.7mm 的悬空线头（ESP32 块实测 19 段，
        #   KiCad DRC `track_dangling` 19 条、`isolated_copper` 1 条）。
        #   现在改成：基准分区 = 带着全部残迹的分区；逐段试丢，丢了基准不变的才真丢，
        #   剩下的（真承载焊盘之间连通的）照留并报数。
        base = pads_partition([_g(t) for t in keep_trk if t[0] == 'seg'], pads_geo)

        # ⚠ **焊盘自己出来的那段永远不丢**（10-30，UPduino fpga_ice40up5k 踩到）。
        #   分区判据只看"焊盘和焊盘之间"，可一根脚在块里往往**只有一个焊盘**——
        #   它从焊盘出来、走几毫米就撞上窗口边（窗口外是隔壁块的排针孔），
        #   分区里它本来就是孤零零一组，丢了也"什么都不变"，于是被当垃圾丢掉，
        #   **这只脚在块里连一根铜都不剩**。那不是垃圾，那是这只脚的铜接口。
        #   净效果：fpga 的 U2.3/U2.4（IOB_9B/IOB_8A）被静悄悄抹掉，靠下面的硬门才抓住。
        def _at_pad(t):
            n, a, b, r = t[1], (t[2], t[3]), (t[4], t[5]), t[6] / 2
            return any(pn == n and _seg_rect_dist(
                a, b, (min(bx[0], bx[2]), min(bx[1], bx[3]),
                       max(bx[0], bx[2]), max(bx[1], bx[3]))) <= r + ISO_TOL
                for pn, _c, bx in pads_geo)

        pin_stub = {i for i in stub_idx if _at_pad(keep_trk[i])}
        drop = set()
        for i in stub_idx:
            if i in pin_stub:
                continue
            cand = drop | {i}
            g = [_g(t) for j, t in enumerate(keep_trk)
                 if t[0] == 'seg' and (j not in stub_idx or j not in cand)]
            if pads_partition(g, pads_geo) == base:
                drop.add(i)
        keep_trk = [t for i, t in enumerate(keep_trk) if i not in drop]
        n_stub = len(drop)
        n_kept_stub = len(stub_idx) - n_stub
        if pin_stub:
            print(f'  {len(pin_stub)} 段残迹是焊盘自己出来的铜接口（单脚网没有"之间"可言）—— 照留')
        if n_kept_stub - len(pin_stub):
            print(f'  ⚠ {n_kept_stub - len(pin_stub)} 段残迹承载焊盘之间的连通，丢了会断网 —— 照留')

    # —— 哪些铺铜会走人（必须在"孤立铜"之前定）——
    # 铺铜是平面网的主要连接手段。判"这段走线还接没接上东西"时，一张**马上要删掉**的铺铜
    # 如果还按"接上了"算，靠它撑着的短走线就会活下来，删完铺铜立刻变成一堆**没有焊盘的孤岛**
    # （DRV8825 实测 57 片，全是从焊盘到地平面的小短线）。所以先按最终去留定名单，
    # 下面的孤立铜清扫只认"会留下来的"铺铜。
    if not a.keep_pour:
        _win_area = max((wx2 - wx1) * (wy2 - wy1), 1e-6)
        for _z in src.Zones():
            _n = _z.GetNetname()
            if _n not in block_nets:
                continue
            if _n in ports or _n in iface:
                board_planes.add(_n)               # 口岸/接口网的铺铜整张删
                continue
            _zb = _z.GetBoundingBox()
            _f = ((_zb.GetRight() - _zb.GetLeft()) / MM
                  * (_zb.GetBottom() - _zb.GetTop()) / MM) / _win_area
            if _f >= PLANE_AREA_FRAC:
                board_planes.add(_n)
                print(f'  平面铺铜：{_n} 盖住块地盘 {_f * 100:.1f}%'
                      f'（≥ {PLANE_AREA_FRAC * 100:.0f}%，整板级）→ 整张删，拼板时整板重铺接上')

    # —— 孤立铜：两端都不接任何东西的段必删（10-30 用户拍板"只删真孤立铜，口岸切口保留"）——
    #   两类分开看，别混：
    #     **口岸切口** = 恰好一端悬空——悬空那头是块外，这是块的铜接口，装板时就在那儿接，必须留；
    #     **孤立铜**   = 两端全悬空——谁也连不上，只是块里的垃圾铜，必删。
    #   ⚠ 不能拿 DRC 条数当判据：KiCad 对这两类都报 `track_dangling`。得自己判端点接没接东西
    #     （焊盘 / 过孔 / 同网同层的其它段 / 同网铺铜范围）。
    n_iso = n_cut = 0
    naked = {}
    naked_plane, naked_other = {}, {}
    if keep_trk:
        _pads = {}
        for _n, _c, _bx in pads_geo:
            _pads.setdefault(_n, []).append(
                (min(_bx[0], _bx[2]), min(_bx[1], _bx[3]),
                 max(_bx[0], _bx[2]), max(_bx[1], _bx[3])))
        _via_g = [(t[1], t[2], t[3], t[4]) for t in keep_trk if t[0] == 'via']
        _zone_g = []
        # 只有**会留下来的**铺铜才算"接上了"：默认模式里平面网那张要走人，
        # 算它接上就等于让一段只靠地铺铜活的走线活下来（改完铺铜就成了悬空铜）。
        for _z in src.Zones():
            if _z.GetNetname() not in block_nets:
                continue
            if not a.keep_pour and (_z.GetNetname() in ports
                                    or _z.GetNetname() in board_planes):
                continue
            _zb = _z.GetBoundingBox()
            _zone_g.append((_z.GetNetname(),
                            {src.GetLayerName(l) for l in _z.GetLayerSet().Seq()},
                            (mm(_zb.GetLeft()), mm(_zb.GetTop()),
                             mm(_zb.GetRight()), mm(_zb.GetBottom()))))

        def _touches(pt, net, lay, rad, skip):
            """这个端点接上东西了吗。铺铜只按包围盒算（保守：宁可判"接上了"也别错删）。"""
            for x1_, y1_, x2_, y2_ in _pads.get(net, ()):
                if (x1_ - ISO_TOL <= pt[0] <= x2_ + ISO_TOL and
                        y1_ - ISO_TOL <= pt[1] <= y2_ + ISO_TOL):
                    return True
            for vn, vx, vy, vw in _via_g:
                if vn == net and math.hypot(pt[0] - vx, pt[1] - vy) <= vw / 2 + rad + ISO_TOL:
                    return True
            for j, o in enumerate(keep_trk):
                if j in skip or o[0] != 'seg' or o[1] != net or o[7] != lay:
                    continue
                if _pt_seg_dist(pt, (o[2], o[3]), (o[4], o[5])) <= o[6] + rad + ISO_TOL:
                    return True
            for zn, zl, (zx1, zy1, zx2, zy2) in _zone_g:
                if zn == net and lay in zl and zx1 <= pt[0] <= zx2 and zy1 <= pt[1] <= zy2:
                    return True
            return False

        def _free(pt, t, i):
            return 0 if _touches(pt, t[1], t[7], t[6] / 2, (i,)) else 1

        # 判据从"端点接没接东西"升级成**整团铜必须通向某个焊盘**（10-30 去铺铜之后必需）：
        # 过孔链可以互相接着、却一个焊盘也不通（缝合孔就是这样），按端点判它们全都"接上了"，
        # 留着就是块里的死铜。改成先按"同网且形状相碰"分团，团里一个焊盘都没有就整团删。
        # ⚠ 全是**半径**相加：元组里存的宽度是直径（走线 0.2 / 过孔 0.6）。拿直径相加
        #   会把判定距离放大一倍，缝合孔隔着 0.6mm 就被算成"连着"，整条链跟着一个焊盘活下来。
        def _pair(i, j):
            a, b = keep_trk[i], keep_trk[j]
            if a[1] != b[1]:
                return False
            if a[0] == 'seg' and b[0] == 'seg':
                return (a[7] == b[7] and _seg_seg_dist((a[2], a[3]), (a[4], a[5]),
                                                      (b[2], b[3]), (b[4], b[5]))
                        <= (a[6] + b[6]) / 2 + ISO_TOL)
            if a[0] == 'via' and b[0] == 'via':
                return math.hypot(a[2] - b[2], a[3] - b[3]) <= (a[4] + b[4]) / 2 + ISO_TOL
            s, v = (a, b) if a[0] == 'seg' else (b, a)
            if s[7] not in v[8]:        # 过孔跨的**整个层区间**（t[8]），不是只有上下两层
                return False
            return (_pt_seg_dist((v[2], v[3]), (s[2], s[3]), (s[4], s[5]))
                    <= (s[6] + v[4]) / 2 + ISO_TOL)

        def _has_pad(t):
            # ⚠ 这里同样只能用**半径**（`t[6] / 2`）：`t[6]` 存的是直径（走线 0.2 / 过孔 0.6）。
            #   拿直径当半径，1.168mm 的电源干线就凭空多出 0.58mm 判定余量——kaybee 块的
            #   `VOUT_BP_SIDE` 干线（一段 0.7mm 之外、另一头扎在已删铺铜里的浮铜）就是这么
            #   被判成"接上焊盘"活下来的，然后在口岸门禁里以"2 件铜/0 焊盘"炸出来。
            # ⚠ 10-30 BP5 加的一条：**留在块里的铺铜也算根**（和 `_touches` 一个口径）。
            #   VBUS 那两只过孔谁都不挨着，两头靠 F.Cu/B.Cu 两块铺铜搭桥；
            #   只认焊盘的话两孔先被删、铺铜随后成孤岛（B.Cu 那张整张删掉），
            #   内部网 `Net-(J202-VBUS-PadA4B9)` 当场裂成两半。
            lays = t[8] if t[0] == 'via' else (t[7],)
            if t[0] == 'via':
                ct, rad = (t[2], t[3]), t[4] / 2
            else:
                ct, rad = None, t[6] / 2
            for zn, zl, (zx1, zy1, zx2, zy2) in _zone_g:
                if zn != t[1] or not zl.intersection(lays):
                    continue
                if ct is not None:
                    if (zx1 - rad - ISO_TOL <= ct[0] <= zx2 + rad + ISO_TOL and
                            zy1 - rad - ISO_TOL <= ct[1] <= zy2 + rad + ISO_TOL):
                        return True
                elif _seg_rect_dist((t[2], t[3]), (t[4], t[5]),
                                    (zx1, zy1, zx2, zy2)) <= rad + ISO_TOL:
                    return True
            if t[0] == 'via':
                return _touches_pad(t[1], (t[2], t[3]))
            return any(_seg_rect_dist((t[2], t[3]), (t[4], t[5]), box) <= t[6] / 2 + ISO_TOL
                       for box in _pads.get(t[1], ()))

        def _touches_pad(net, pt):
            return any(bx[0] - ISO_TOL <= pt[0] <= bx[2] + ISO_TOL and
                       bx[1] - ISO_TOL <= pt[1] <= bx[3] + ISO_TOL
                       for bx in _pads.get(net, ()))

        while True:
            par = list(range(len(keep_trk)))

            def findt(x):
                while par[x] != x:
                    par[x] = par[par[x]]
                    x = par[x]
                return x

            for i in range(len(keep_trk)):
                for j in range(i + 1, len(keep_trk)):
                    if findt(i) != findt(j) and _pair(i, j):
                        par[findt(i)] = findt(j)
            rooted = {}
            for i, t in enumerate(keep_trk):
                if _has_pad(t):
                    rooted[findt(i)] = True
            gone = {i for i in range(len(keep_trk)) if not rooted.get(findt(i))}
            if not gone:
                break
            keep_trk = [t for i, t in enumerate(keep_trk) if i not in gone]
            n_iso += len(gone)
        _via_g[:] = [(t[1], t[2], t[3], t[4]) for t in keep_trk if t[0] == 'via']
        n_cut = sum(1 for i, t in enumerate(keep_trk)
                    if t[0] == 'seg' and _free((t[2], t[3]), t, i) + _free((t[4], t[5]), t, i) == 1)

        # —— 去了铺铜以后，哪些脚没有铜可接 ——
        #   铺铜是平面网（GND/+3V3）的主要连接手段，块不带铺铜之后这些脚**本来就该悬着**，
        #   拼板时由整板铺铜接上——不是缺陷，但要报数。**信号网有裸脚才是真缺口**：
        #   人的铜没把它接上，块搬到别的板上就少一段。
        _via_n = [(t[1], t[2], t[3]) for t in keep_trk if t[0] == 'via']
        _segs_n = [(t[1], (t[2], t[3]), (t[4], t[5]), t[6] / 2)
                   for t in keep_trk if t[0] == 'seg']
        _npads = collections.Counter()
        for n, c, bx in pads_geo:
            if n not in block_nets:
                continue
            _npads[n] += 1
            box = (min(bx[0], bx[2]), min(bx[1], bx[3]),
                   max(bx[0], bx[2]), max(bx[1], bx[3]))
            hit = any(sn == n and _seg_rect_dist(a, b, box) <= r + ISO_TOL
                      for sn, a, b, r in _segs_n)
            if not hit:
                hit = any(vn == n and box[0] - ISO_TOL <= vx <= box[2] + ISO_TOL and
                          box[1] - ISO_TOL <= vy <= box[3] + ISO_TOL
                          for vn, vx, vy in _via_n)
            if not hit:      # 留下来的那张本地铺铜也算一张"铜"
                hit = any(zn == n and zx1 <= c[0] <= zx2 and zy1 <= c[1] <= zy2
                          for zn, _zl, (zx1, zy1, zx2, zy2) in _zone_g)
            if not hit:
                naked.setdefault(n, []).append(c)
        naked_plane = {n: v for n, v in naked.items() if n in plane_nets}
        naked_other = {n: v for n, v in naked.items() if n not in plane_nets
                       and not n.startswith('unconnected-') and _npads[n] > 1}

        # —— 硬门：**源板上有铜接着、块里却没铜**的脚（10-30，UPduino SJ16 踩到）——
        #   `naked_other` 那栏只查"块内 ≥2 个脚"的网，单脚网直接跳过；而口沿上
        #   "焊盘 → 短路 → 打孔" 这种走法一旦孔被窗口裁掉，焊盘就恰恰只连着那截短线，
        #   短线又被残迹判据当垃圾丢掉——单脚网没有"焊盘之间"可言，于是**静悄悄断一个脚**。
        #   判据不看块内脚数，直接问源板：源板这根网的铜碰得到这个焊盘吗？
        #   碰得到、块里却碰不到 = 窗口把铜裁没了 = 真缺口，不许交出去。
        lost = {}
        for n, c, bx in pads_geo:
            if n not in block_nets or n in plane_nets or n.startswith('unconnected-'):
                continue
            if n not in naked or c not in naked[n]:
                continue                      # 块里有铜接着，过
            box = (min(bx[0], bx[2]), min(bx[1], bx[3]),
                   max(bx[0], bx[2]), max(bx[1], bx[3]))
            src_hit = False
            for t in src.GetTracks():
                if t.GetNetname() != n:
                    continue
                if t.Type() == pcbnew.PCB_VIA_T:
                    p = t.GetPosition()
                    if (box[0] - ISO_TOL <= mm(p.x) <= box[2] + ISO_TOL and
                            box[1] - ISO_TOL <= mm(p.y) <= box[3] + ISO_TOL):
                        src_hit = True
                        break
                else:
                    s, e = t.GetStart(), t.GetEnd()
                    if _seg_rect_dist((mm(s.x), mm(s.y)), (mm(e.x), mm(e.y)), box) \
                            <= mm(t.GetWidth()) / 2 + ISO_TOL:
                        src_hit = True
                        break
            if src_hit:
                lost.setdefault(n, []).append(c)
        if naked_plane:
            print(f'  块里没铜可接的脚（源板靠铺铜连的）{sum(len(v) for v in naked_plane.values())} 个'
                  f'（{len(naked_plane)} 网：' +
                  '、'.join(f'{n}×{len(v)}' for n, v in sorted(naked_plane.items())) +
                  '）—— 走线/过孔够不着，拼板时由整板铺铜接上')
        if naked_other:
            print(f'  ⚠ 信号网也有裸脚（走线没把它接上）{len(naked_other)} 网：' +
                  '、'.join(f'{n}×{len(v)}' for n, v in sorted(naked_other.items())))
        if lost:
            print(f'✗ 源板上有铜接着、块里却没铜的脚 {sum(len(v) for v in lost.values())} 个'
                  f'（{len(lost)} 网）：'
                  + '、'.join(f'{n}×{len(v)}' for n, v in sorted(lost.items())))
            print('   —— 窗口把这些脚的铜整段裁掉了（口沿短路→过孔是最常见的形态）。'
                  '别指望残迹判据：单脚网没有"焊盘之间"可言。给 --copper-pad 加大窗口重裁。')
            return 1

    # —— 借道别人家焊盘的接头（10-30，UPduino clock_12m 踩到）——
    #   块的网从自家焊盘出来，走一段 F.Cu、扎进**隔壁块的直插排针孔**换到 In2 再走。
    #   排针不在块里，孔也就跟着没了：块单看这一处，两截铜在一个空点上对着，是断的
    #   （KiCad DRC 报 unconnected）；拼板时那只孔一插上就通。这是"块边界穿过别人焊盘"
    #   的必然结果，不是裁错——但必须报出来，否则谁也不知道块的网在这里借过道。
    _mem = set(members)
    _fp_pads = []
    for f in src.GetFootprints():
        if f.GetReference() in _mem:
            continue
        for p in f.Pads():
            n = p.GetNetname()
            if n in block_nets:
                c = p.GetPosition()
                _fp_pads.append((f.GetReference(), p.GetNumber(), n, mm(c.x), mm(c.y)))
    borrow = set()
    for t in keep_trk:
        ends = ([(t[2], t[3])] if t[0] == 'via' else [(t[2], t[3]), (t[4], t[5])])
        for x, y in ends:
            for ref, pad, n, px, py in _fp_pads:
                if n == t[1] and abs(x - px) <= ISO_TOL * 2 and abs(y - py) <= ISO_TOL * 2:
                    borrow.add((n, f'{ref}.{pad}'))
    if borrow:
        #   轨（GND/电源）借道谁家的孔都不稀奇，逐条列只会淹掉信号那几条——分开报。
        _sig = sorted(b for b in borrow if b[0] not in plane_nets
                      and b[0].lstrip('/') not in ('GND', '+3V3', '+5VD', '+1V2'))
        if _sig:
            _head = '、'.join(f'{n}→{rp}' for n, rp in _sig[:6])
            print(f'  ⚠ 信号网借道**块外器件**的焊盘换层 {len(_sig)} 处：' + _head
                  + (f'…（共 {len(_sig)} 条，见 placement.json counts.borrowed_pads）'
                     if len(_sig) > 6 else '')
                  + '（那孔不在块里，块单看这里是断的；拼板时插上就通）')
        if len(_sig) < len(borrow):
            print(f'    （另有 {len(borrow) - len(_sig)} 处是轨借道别人的地/电源孔，不列）')

    segs = [t for t in keep_trk if t[0] == 'seg']
    vias = [t for t in keep_trk if t[0] == 'via']
    print(f'  铜：全板 {n_all} 段/孔 → 属于块网的留在窗口内 {len(segs)} 段 + {len(vias)} 孔'
          f'（丢掉 {n_other}）' + (f'；削剩残迹 {len(stub_idx)} 段 → 丢 {n_stub}'
                                  f'、留 {n_kept_stub}' if stub_idx else '')
          + f'；孤立铜删 {n_iso} 段/孔；口岸切口 {n_cut} 处（一端悬空=块的铜接口，留）')
    print(f'  走线出口（人的铜在这里离开块）{len(cut_points)} 处：'
          + '、'.join(f'{n}@{x},{y}' for n, x, y, _ in cut_points[:6])
          + ('…' if len(cut_points) > 6 else ''))

    # —— 删干净 ——
    # ⚠ 顺序要紧：先把所有容器**缓存**成 list，再逐类删。
    #   KiCad 10 的 SWIG 在 board.Remove(足迹) 之后，GetTracks() 会返回不可迭代的
    #   SwigPyObject（TypeError: not iterable），所以删完足迹再调 GetTracks 必崩。
    all_fps = list(src.GetFootprints())
    all_trk = list(src.GetTracks())
    all_zone = list(src.Zones())
    all_draw = list(src.GetDrawings())
    # ⚠ 坑：`id(f)` 不能用来判"同一件"——每次 GetFootprints() 都给新的 Python 代理对象，
    #   id() 必然不同，于是"保留集"永远匹配不上、把全部件删光（症状=保真门禁报件集全缺）。
    #   只能按位号判。
    keep_refs = set(board_members)
    for t in all_trk:
        src.Remove(t)
    for f in all_fps:
        if f.GetReference() not in keep_refs:
            src.Remove(f)
    for d in all_draw:
        src.Remove(d)
    print(f'  删除：图框 {len(all_draw)}')

    # —— 铺铜：不删，剪到块的地盘 ——
    # 电源/地在这类板上是**铺铜连的、不是走线连的**（2026-10-30 实测：只扣走线，
    # L1→C7 的 +3V3、U5→C5 的 +5V 全断，25 条未连通）。补的办法不是自己画个矩形，
    # 而是把**人铺的那几张原样剪到块的地盘**——形状/层/优先级/间距全是他的，
    # 连"给 SW 节点单独铺一块铜来载流"这种设计决定也一起带进块里。
    pad = max(MARGIN, EDGE_CLEAR + 0.5)
    dx = pad + w / 2 - ox
    dy = pad + h / 2 - oy
    # 块的地盘（**源板坐标**，剪完再平移）：和新板框同大，四边再让 0.3mm 免得贴边
    hw, hh = (w + pad) / 2 - 0.3, (h + pad) / 2 - 0.3
    bx1, by1, bx2, by2 = ox - hw, oy - hh, ox + hw, oy + hh
    clip_rect = pcbnew.SHAPE_POLY_SET()
    clip_rect.NewOutline()
    for px, py in ((bx1, by1), (bx2, by1), (bx2, by2), (bx1, by2)):
        clip_rect.Append(v2(px, py))
    keep_zone = []
    plane_nets |= {z.GetNetname() for z in all_zone}
    # 整板级铺铜：铜（走线/孔/焊盘）或铺铜框本身伸出件框 > CAP 的平面网。
    # 上面算铜窗口时只看走线/焊盘，这里把**铺铜框自己**也算一遍——有的板铺铜铺满整板
    # 而网上只有块内几个焊盘，光看铜看不出来。
    for z in all_zone:
        n = z.GetNetname()
        if n not in block_nets:
            continue
        bb = z.GetBoundingBox()
        d = max(x1 - bb.GetLeft() / MM, y1 - bb.GetTop() / MM,
                bb.GetRight() / MM - x2, bb.GetBottom() / MM - y2)
        if d > COPPER_PAD_CAP:
            board_planes.add(n)
    # 面积口径（2026-10-30 补，DRV8825 逼出来的）：上面那条是"伸出件框 > 5mm"，
    # **件包围盒（含丝印/文字）比板还大的板，它永远不响**——DRV8825 件框 40.65×36.11
    # 比板框 38×22 还大，GND 铺铜盖住板面 99.3%，可它相对件框是"缩在里面的"，
    # `d > CAP` 判不中，整张地平面就被当成"内部网的载流铺铜"留进了块里
    # （违反"块内不带铺铜，为拼接留走线空间"）。
    # 所以再按**面积**判一次：铺铜框面积 ≥ 块地盘的 40% 就是整板级平面。
    # 实测分离度（铺铜框 ÷ 剪过 0.3 的块地盘）：留下的合法载流铺铜 `usb_power`
    # VBUS 7.3% / +5V 17.2%、`bp5_usb_c_port` 2.6~3.9%；该删的整板地平面 DRV8825 **45.8%**。
    # 40% 落在两者之间（合法那侧留 2.3 倍余量），不是拍脑袋取的整十数。
    _win = max((bx2 - bx1) * (by2 - by1), 1e-6)
    for z in all_zone:
        n = z.GetNetname()
        if n not in block_nets:
            continue
        bb = z.GetBoundingBox()
        frac = ((bb.GetRight() - bb.GetLeft()) / MM
                * (bb.GetBottom() - bb.GetTop()) / MM) / _win
        if frac >= PLANE_AREA_FRAC:
            board_planes.add(n)      # 名单早在孤立铜之前就定过并打印了，这里只是复核
    # 10-30 用户拍板（原话"designblock 的 pcb 是不要覆铜的，为拼接时留出走线的空间"）：
    # 块的铺铜会盖住整块地盘，拼板时别的块的走线没地方过；铺铜是**整板级**决定，拼完再铺一次。
    # 但有一条例外，实测逼出来的：**内部网**的铺铜是块内自洽的连接手段
    # （buck 的 `/Buck_Coil` 就把 U5.SW 和 L1 只靠一张 F.Cu 铺铜连着，去了块自己就是断的），
    # 这种铺铜谁都替不了它，必须留。平面网（有块外件）一律不留。
    for z in all_zone:
        n = z.GetNetname()
        bb = z.GetBoundingBox()
        over = not (bb.GetRight() < bx1 * MM or bb.GetLeft() > bx2 * MM or
                    bb.GetBottom() < by1 * MM or bb.GetTop() > by2 * MM)
        keep = (n in block_nets and over and (a.keep_pour or n not in ports)
                and n not in iface and n not in board_planes)
        if keep:
            z.Outline().BooleanIntersection(clip_rect)
            keep_zone.append(z)
        else:
            src.Remove(z)
    kept = '、'.join(f'{z.GetNetname()}@{[src.GetLayerName(l) for l in z.GetLayerSet().Seq()][0]}'
                     for z in keep_zone)
    if a.keep_pour:
        print(f'  铺铜：全板 {len(all_zone)} 张 → 剪到块的是 {kept or "无"}'
              f'（丢 {len(all_zone) - len(keep_zone)} 张）')
    else:
        print(f'  铺铜：全板 {len(all_zone)} 张 → **只留内部网的 {kept or "无"}**，'
              f'平面网（{"、".join(sorted(plane_nets - {z.GetNetname() for z in keep_zone}))}）'
              f'一张不留，拼板时由整板铺铜接上')
    if board_planes:
        # 这些网在块里是"内部网"（块外没有同网件），按规则本该留铺铜；但它们在源板上
        # 铺的是**整板平面**，留下就等于把整张板的地网搬进块里、盖住拼板走线。按用户
        # 定的「块内不带铺铜」整张删，拼板时整板重铺接上（usb_power 的 VBUS/+5V 那种
        # 块内载流铺铜不受影响：它不伸出件框）。
        print(f'  铺铜：{"、".join(sorted(board_planes))} 的**铺铜框离件框 > {COPPER_PAD_CAP}mm**'
              f'（要么是整板平面，要么是别人家那块地方铺的）→ 整张删，拼板时整板重铺接上')

    for f in fps:
        p = f.GetPosition()
        f.SetPosition(v2(mm(p.x) + dx, mm(p.y) + dy))
    for z in keep_zone:
        z.Move(pcbnew.VECTOR2I(int(round(dx * MM)), int(round(dy * MM))))

    # —— 铜搬过来（平移到新板） ——
    netmap = {n: src.FindNet(n) for n in block_nets}
    for seg in segs:
        _, net, ax, ay, bx, by, wid, layer = seg
        t = pcbnew.PCB_TRACK(src)
        t.SetStart(v2(ax + dx, ay + dy))
        t.SetEnd(v2(bx + dx, by + dy))
        t.SetWidth(int(round(wid * MM)))
        t.SetLayer(src.GetLayerID(layer))
        n = netmap.get(net)
        if n is not None:
            t.SetNet(n)
        src.Add(t)
    for via in vias:
        _, net, cx, cy, wid, drill, la, lb, _span = via
        v = pcbnew.PCB_VIA(src)
        v.SetPosition(v2(cx + dx, cy + dy))
        v.SetWidth(int(round(wid * MM)))
        v.SetDrill(int(round(drill * MM)))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(src.GetLayerID(la), src.GetLayerID(lb))
        n = netmap.get(net)
        if n is not None:
            v.SetNet(n)
        src.Add(v)

    # —— 板框（离块外框 EDGE_CLEAR，给口岸桩留地方） ——
    ex1, ey1 = pad / 2, pad / 2
    ex2, ey2 = ex1 + w + pad, ey1 + h + pad
    for (ax, ay, bx, by) in ((ex1, ey1, ex2, ey1), (ex2, ey1, ex2, ey2),
                             (ex2, ey2, ex1, ey2), (ex1, ey2, ex1, ey1)):
        s = pcbnew.PCB_SHAPE(src)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(v2(ax, ay))
        s.SetEnd(v2(bx, by))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        src.Add(s)

    # —— 重新灌铜 ——
    # 剪完的地盘必须重填：zone 里的填充多边形还是**原板**算出来的，
    # 直接存盘会得到一张和现在这个板框对不上的旧铜（KiCad 打开才报错，太晚）。
    if keep_zone:
        src.BuildConnectivity()
        pcbnew.ZONE_FILLER(src).Fill(src.Zones())
        filled = [z for z in src.Zones() if z.IsFilled()]
        print(f'  灌铜：{len(filled)}/{len(keep_zone)} 张填好')

    # —— 孤立铺铜岛：填完铜后，接不到**同网焊盘/过孔**的填充岛一律删 ——
    #   为什么非删不可：块被剪小之后会出现"岛接着一段出口走线、而那截走线也只通往块外"
    #   的整团孤立铜。KiCad 的"岛移除=ALWAYS"只删"连走线/过孔都不接的岛"，这种它留着，
    #   于是 DRC 报 `isolated_copper`。ESP32 块实测：+3V3@B.Cu 整张填充落在铜窗口
    #   （件框外扩 COPPER_PAD）之外，一个人也接不到它。
    #   判据用铺铜自己的热焊盘间隙做容差（见 COPPER_PAD 段注释：焊盘中心在洞外，
    #   `Contains` 恒假，`Collide(pt, 0.6mm)` 才判得出热焊盘连没连）。
    #   ⚠ 10-30 BP5 逼出的一条：**必须按层比**。原来不分层，一块 B.Cu 的填充只要
    #   底下 0.6mm 内有同网的 F.Cu 贴片焊盘就算"接上了"——KiCad 却照样报
    #   `isolated_copper`（它只看同一层）。表现：VBUS 那张 B.Cu 铺铜整片悬空，
    #   F.Cu 上 J202 的两个 VBUS 焊盘离它 0.5mm，把整片从删岛名单里捞了出来。
    n_isl_fill = 0
    empty_zone = []
    # ⚠ 坐标系：`pads_geo`/`keep_trk` 是**源板坐标**，而铺铜填充是在平移之后的板上算的。
    #   不补 (dx,dy) 就拿源坐标去比成品坐标，谁都对不上，好铺铜会整张被判成孤立岛删掉
    #   （buck 的 `/Buck_Coil` 就这么被删过一次，症状=灌铜 1/1 张填好、落盘 0 张）。
    _pads_fx = [(n, (c[0] + dx, c[1] + dy), ls) for n, c, ls in pads_lay]
    _via_g2 = [(t[1], t[2] + dx, t[3] + dy) for t in keep_trk if t[0] == 'via']
    for z in keep_zone:
        net = z.GetNetname()
        for lay in z.GetLayerSet().Seq():
            if not z.HasFilledPolysForLayer(lay):
                continue
            ps = z.GetFilledPolysList(lay)
            keep_ps = pcbnew.SHAPE_POLY_SET()
            cut = 0
            for i in range(ps.OutlineCount()):
                o = ps.Outline(i)
                hit = any(o.Collide(v2(c[0], c[1]), int(ZONE_BITE * MM))
                          for n, c, ls in _pads_fx if n == net and lay in ls)
                hit = hit or any(o.Collide(v2(vx, vy), int(ZONE_BITE * MM))
                                 for n, vx, vy in _via_g2 if n == net)
                if not hit:
                    cut += 1
                    continue
                keep_ps.AddOutline(o)
                for h in range(ps.HoleCount(i)):
                    keep_ps.AddHole(ps.Hole(i, h))
            if cut:
                n_isl_fill += cut
                z.SetFilledPolysList(lay, keep_ps)
                if keep_ps.OutlineCount() == 0:
                    empty_zone.append(z)
    for z in empty_zone:
        keep_zone.remove(z)
        src.Remove(z)
    if n_isl_fill:
        print(f'  孤立铺铜岛：删 {n_isl_fill} 块填充'
              + (f'（其中 {len(empty_zone)} 张铺铜整张删掉）' if empty_zone else ''))

    os.makedirs(a.out, exist_ok=True)
    pcb_out = os.path.join(a.out, f'{a.name}.kicad_pcb')
    pcbnew.SaveBoard(pcb_out, src)
    retitle(pcb_out, a.name, ex2, ey2, os.path.basename(a.board))

    # 计数也落盘（10-30）：块卡里要写"铜 XX 段 / 孤立铜删 XX / 口岸切口 XX"这类数字，
    # 只打印不存的话，写完卡就再也复核不了——卡里那串数字要么凭记忆、要么重跑一遍。
    # 存进 placement.json，卡里的每个数都能从交付物重算（能算的不写）。
    by_layer = collections.Counter(t[7] for t in segs)      # ('seg', net, ax, ay, bx, by, 宽, 层名)
    counts = {
        'members': len(board_members),
        'not_built': list(not_built),
        'nets': len(block_nets),
        'copper_segs': len(segs), 'vias': len(vias), 'stub_kept': n_kept_stub,
        'isolated_deleted': n_iso, 'cut_ends': n_cut, 'exit_points': len(cut_points),
        'segs_by_layer': dict(sorted(by_layer.items())),
        'zones_kept': len(keep_zone),
        'naked_plane_pads': sum(len(v) for v in naked_plane.values()),
        'naked_signal_pads': {n: len(v) for n, v in sorted(naked_other.items())},
        'borrowed_pads': {f'{n}->{rp}': 1 for n, rp in sorted(borrow)},
    }
    card = {
        'name': a.name, 'source': os.path.basename(a.board),
        'origin_src_mm': [round(ox, 4), round(oy, 4)],
        'size_mm': [round(w, 4), round(h, 4)],
        'ports': ports,
        'cut_points': [{'net': n, 'x': x, 'y': y, 'layer': l} for n, x, y, l in cut_points],
        'counts': counts,
        'placement': place,
    }
    card_out = os.path.join(a.out, f'{a.name}.placement.json')
    with open(card_out, 'w', encoding='utf-8') as fh:
        json.dump(card, fh, ensure_ascii=False, indent=2, default=str)

    # —— 保真门禁 ——
    out = pcbnew.LoadBoard(pcb_out)
    o = {f.GetReference(): f for f in out.GetFootprints()}
    bad = []
    if set(o) != set(board_members):
        bad.append(f'件集不符：多 {sorted(set(o)-set(board_members))} 缺 {sorted(set(board_members)-set(o))}')
    else:
        bx1, by1, bx2, by2 = block_box(list(o.values()))
        nox, noy = (bx1 + bx2) / 2, (by1 + by2) / 2
        got = {p['ref']: p for p in snapshot(list(o.values()), nox, noy)}
        for p in card['placement']:
            g = got[p['ref']]
            if abs(g['dx'] - p['dx']) > 1e-3 or abs(g['dy'] - p['dy']) > 1e-3:
                bad.append(f"{p['ref']} 位移 ({p['dx']},{p['dy']})→({g['dx']},{g['dy']})")
            if abs(abs((g['rot'] - p['rot'] + 180) % 360 - 180)) > 1e-3:
                bad.append(f"{p['ref']} 朝向 {p['rot']}°→{g['rot']}°")

    o_trk = [t for t in out.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
    o_via = [t for t in out.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    if not a.placement_only:
        if len(o_trk) != len(segs):
            bad.append(f'铜段数 {len(segs)} → 落盘 {len(o_trk)}')
        if len(o_via) != len(vias):
            bad.append(f'过孔数 {len(vias)} → 落盘 {len(o_via)}')
        if len(list(out.Zones())) != len(keep_zone):
            bad.append(f'铺铜 {len(keep_zone)} → 落盘 {len(list(out.Zones()))}')

    print('\n保真门禁：', end='')
    if bad:
        print(f'✗ {len(bad)} 条不符')
        for b in bad:
            print('   ', b)
        return 1
    print(f'✅ {len(card["placement"])} 件位置/朝向全等'
          + ('' if a.placement_only else f'；{len(segs)} 段铜 + {len(vias)} 孔全在'
             f'；{len(keep_zone)} 张铺铜在'))

    # —— 口岸门禁：块内不许有断掉的网 / 悬空的铜 ——
    # 判据不用 DRC 的坐标（它落在 ratsnest 锚点上，不是出口），而是自己按几何连通
    # 分岛：内部网必须是一整个岛，口岸网的每个岛必须含焊盘。
    rows = drc_report(pcb_out)
    win_blk = (wx1 + dx, wy1 + dy, wx2 + dx, wy2 + dy)
    # `unconnected-*` 是 KiCad 给**没接的脚**起的自动名，不是电路（DB9 屏蔽壳那一对
    # 同号焊盘就是这种情况：一只封装里两个 pad "0"，KiCad 把它们并成一张 unconnected- 网，
    # 电气上谁也不连谁）。按口径它们不进岛门，条数打印出来。
    isl_nets = {n for n in block_nets if not n.startswith('unconnected-')}
    n_skip = len(block_nets) - len(isl_nets)
    if n_skip:
        print(f'  岛门跳过 {n_skip} 张 `unconnected-*` 网（KiCad 的"没接脚"自动名，不是电路）：'
              f'{"、".join(sorted(block_nets - isl_nets))}')
    isl = copper_islands(out, isl_nets, win_blk)
    bad_isl = stranded_islands(isl, ports, iface)
    n_isl = sum(len(v) for v in isl.values())
    detail = '、'.join(f'{n}×{len(v)}' for n, v in sorted(isl.items()) if len(v) > 1)
    hung = sum(len(r['pts']) for r in rows if r['type'] in DANGLING) if rows is not None else 0
    n_iso_cu = sum(len(r['pts']) for r in rows if r['type'] == 'isolated_copper') if rows else 0
    print(f'  口岸门禁：{len(isl)} 个网 → {n_isl} 个铜岛'
          + (f'（裂成多岛：{detail}）' if detail else '')
          + f'；口岸切口 {n_cut} 处（铜走到窗口边就停=块的铜接口，留）'
          + (f'；DRC 参考：断头/未连通 {hung} 处' if rows is not None else ''))
    # 裂成多岛这张表也落盘：卡里要写"某网在块内裂成 N 个铜岛、装板时由块外干线接上"，
    # 只打印的话这条数字就变成凭记忆（同 counts 的理由）。
    split = {n: len(v) for n, v in sorted(isl.items()) if len(v) > 1}
    with open(card_out, encoding='utf-8') as fh:
        _c = json.load(fh)
    _c['counts']['island_split'] = split
    with open(card_out, 'w', encoding='utf-8') as fh:
        json.dump(_c, fh, ensure_ascii=False, indent=2, default=str)
    # 10-30 用户拍板：口岸切口照留，**两端全空的铜一根不留**。
    if n_iso_cu:
        print(f'✗ 孤立铜 {n_iso_cu} 处（DRC isolated_copper）：块里不该有一根两端都够不着东西的铜')
        for r in rows:
            if r['type'] == 'isolated_copper':
                for x, y in r['pts'][:6]:
                    print(f'     @({x:.3f}, {y:.3f})')
        return 1
    if bad_isl:
        print('✗ 块内电路不完整：')
        for n, r, why in bad_isl:
            print(f'     {n:<14} {r["n"]} 件铜/{r["pad"]} 焊盘 '
                  f'@({r["at"][0]:.3f}, {r["at"][1]:.3f})  {why}')
        return 1
    print('  ✅ 内部网各是一个整岛；口岸网每个岛都含焊盘（装板时从焊盘引出）')

    # —— sch↔pcb 同一块电路 ——
    if a.sch:
        refs = set(board_members)
        ps_sch, ps_pcb = sch_partition(a.sch, refs), pcb_partition(out, refs, pad_totals)
        # **封装里没有同号焊盘的符号脚**：板上根本没有这只脚，拿它比"谁和谁同网"
        # 就是拿一样板上不存在的东西比。这是**源板自身**的缺陷，不是裁块裁出来的，
        # 例如 DDR5 UDIMM 的符号有 `SH`（屏蔽壳）脚接 GND，而封装
        # `DDR5 UDIMM GENERIC` 里对应的是三只**无名焊盘** `S1/S2/S3`（NetCode=0）。
        # 只从原理图那一侧摘掉这些脚（并打印），别的一概不动。
        pads = {(f.GetReference(), str(p.GetNumber()))
                for f in out.GetFootprints() if f.GetReference() in refs
                for p in f.Pads() if str(p.GetNumber())}
        sch_pins = {x for g in ps_sch for x in g}
        lost, kept = set(), set()
        for g in ps_sch:
            g2 = frozenset(x for x in g if x in pads)
            lost |= g - set(g2)
            if g2:
                kept.add(g2)
        ps_sch = kept
        if lost:
            print('  未上板的符号脚（封装里没有同号焊盘，不参与比较）：'
                  + ', '.join(f'{r}.{p}' for r, p in sorted(lost)))
        # 反向：**符号里没有同号脚的焊盘**——散热焊盘（`EP`/`21` 号）、屏蔽壳、
        # 定位脚这一类。符号不画它，图上就没有"它接谁"，拿焊盘侧的连接去比，
        # 差的一定是这一只脚而不是电路。2026-10-31 STRF 的 `U4`（nRF24L01P，QFN20
        # +EP）实测：符号 20 脚、封装 21 个焊盘，EP=21 在板上接 GND，于是 GND 组
        # 多了 `U4.21`，两侧"谁和谁同网"其实一致。与上面那条对称，只从 PCB 侧摘
        # 掉这些脚并打印，别的一概不动。
        extra = {x for g in ps_pcb for x in g if x not in sch_pins}
        if extra:
            print('  符号里没有的焊盘（散热盘/屏蔽壳一类，不参与比较）：'
                  + ', '.join(f'{r}.{p}' for r, p in sorted(extra)))
            ps_pcb = {frozenset(x for x in g if x not in extra) for g in ps_pcb}
            ps_pcb = {g for g in ps_pcb if g}
        only_sch, only_pcb = ps_sch - ps_pcb, ps_pcb - ps_sch
        n_pins = sum(len(g) for g in ps_pcb)
        print(f'  电路一致性：原理图 {len(ps_sch)} 组 / PCB {len(ps_pcb)} 组，'
              f'共 {n_pins} 个管脚')
        if only_sch or only_pcb:
            print('✗ 两边不是一个电路：')
            for g in list(only_sch)[:5]:
                print('   只在原理图:', sorted(g))
            for g in list(only_pcb)[:5]:
                print('   只在 PCB  :', sorted(g))
            return 1
        print('  ✅ 逐脚同网关系完全一致（不比网名，比"谁和谁同网"）')

    print(f'  板 {pcb_out}（{w+pad:.2f}×{h+pad:.2f}mm）')
    print(f'  卡片 {card_out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
