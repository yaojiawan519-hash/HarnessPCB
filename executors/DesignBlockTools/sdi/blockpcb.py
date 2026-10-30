#!/usr/bin/env python3
"""把裁出来的块做成 **KiCad DesignBlocks 规格**的块：五件套 + 板级几何（③）。

规格见 `块规范模版.md`：块目录 = `DesignBlocks/<块名>/`（2026-10-29 起块直接当库中
一员，不再套 `<库名>.kicad_blocks/<块名>.kicad_block/`）

    <块名>.kicad_sch            原理图片段（裁块产物，原样搬进来）
    <块名>.kicad_pcb            ★ 本节要产的东西：块的相对几何（归一化到原点）
    <块名>.json                 KiCad 元数据 {description, keywords, fields}
    block.sdi.yaml              ★ 块卡（电路是什么）——写法权威，本节最后一步写它
    block.semantic.json         语义侧车（**生成物**，由 yaml 重出）
    <块名>.table.json           设计表片段（**生成物**，parts / conns / interface）
    _geom/measured.json         量出来的东西（坐标 / 出口面 / geometry_status）

2026-10-28 起 `block.sdi.yaml` 是块卡的唯一手写权威，两份 json 降级成产物
（`sdi.card`）：本节先把 json 写出来，再 `write_card` 出 yaml、`emit` 把 json 从
yaml 重出——三样同值，回读门（`sdi card`）才过得去。

几何这条路（③）分五步，全部由现成工具跑，本节只做"把块接上去"的胶水：

    1. `d2s board`      裁出来的 sch + 设计表 → 空板（footprint 同步 + 网赋值 + 叠层）
    2. `place_rules`    规则摆件：接口一面对外、去耦贴脚、时钟贴芯片、其余就近
    3. 收边            板框收到"器件包络 + 余量"——**PCB 尺寸尽可能小**，接插件贴边
    3.5 口岸桩           口岸脚预置直桩护住出口面（散热盘打孔在 4 的布线后段，见 ③''''）
    4. `route_rl`      L0 pass 计划 → FreeRouting → 引擎 DRC（4 层带 GND/电源平面）
    5. 归一化           平移到原点，写 `<块名>.kicad_pcb`

为什么不是"人摆"：块可以复用 N 次，几何必须是**可复现的规则产物**——同一块今天跑和
下个月跑要落同一个形状，否则拼板时的"块的嘴在哪条边"就没法预先知道。

用法：
    python3 -m sdi.blockpcb                       # 默认：carve_stm32f103usb 四块全跑
    python3 -m sdi.blockpcb --blocks rs485_sp3485 # 只跑一块
    python3 -m sdi.blockpcb --block-dir <块目录>   # 块目录模式（见 build_from_card）
    python3 -m sdi.blockpcb --block-dir ../DesignBlocks/mcu_stm32f411 \
        --gap 0.9 --edge-mm 1.5                   # 密块：放件距 + 换一圈能落过孔的板边走廊
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # DesignBlockTools
ILTOOLS = ROOT.parent / 'SchematicDesign' / 'il' / 'tools'
CARVE = ROOT / 'out' / 'carve_stm32f103usb'
LIBNAME = 'carve'

D2S = ROOT.parent / 'SchematicDesign' / 'design2schematic' / 'd2s.py'
PLACEMENT = ROOT.parent / 'Placement'
RL_PY = PLACEMENT / '.venv' / 'bin' / 'python'

BOARD_W, BOARD_H = 80.0, 80.0        # 起始画布（只给 d2s 一个落点，收边后会砍到包络）
EDGE_MARGIN = 0.6                    # 板边余量（铜到板边 >= 0.5mm，留一点）
# 板边 0.6mm 的后果：外围只剩 0.1mm 余量，**没有任何走线/落孔走廊**。密块（10-29
# mcu_stm32f411：21 件 / 118 焊盘 / 焊盘铜占板面 26%）就卡在这儿——件间 0.45mm 的
# courtyard 缝去掉两边 courtyard（各 ~0.25）剩 ~0.95mm 铜缝，够过一根 0.2mm 线、
# 不够落一个 0.6/0.3 过孔（要 1.0mm）。四层板靠过孔换层，孔落不下就布不通，
# 于是每一轮 FR 都白跑（那一轮 23.5 分钟里 20 分钟花在永不到 0 的重试上）。
# 所以余量做成可给参数：稀疏块照旧 0.6，密块用 `--edge-mm 1.5` 换一圈真走廊。
EDGE_TOL = 1.0                       # 接插件包络离最近板边 <= 这个数才算"贴边"
THERMAL_PAD_MM2 = 4.0                # 多大算"散热盘"（同网大面积盘，要打散热过孔阵列）
                                     # 4.0：0603/0805 电源电容的盘只有 ~2.1mm²（实测 411 的
                                     # C16/C10 各 2.1mm² 全被算成散热盘 → 门禁要求 4 孔、
                                     # 几何只装得下 1）；QFN 的 EP 31.4mm² 稳稳在上
THERMAL_FLOOR = 1                    # 每只散热盘至少几个过孔（不按面积加码；不超过
                                     # 几何装得下的数——见 THERMAL_CHECK 的 cap）
                                     # 2026-10-29 用户拍板：散热靠芯片上贴散热片，
                                     # PCB 只保 EP 电气接地，原口径 max(4, 面积/4)=7 孔作废
                                     # 注意：打孔在 route_rl/fanout.py 里（布线后落铜），
                                     # 这两个常数只供这里**量最终板**用；fanout 侧有同名
                                     # 常数，改口径要两边一起改（两边都打印数字可对照）
CLOCK_GAP_MM = 3.0                   # 晶体外框 → 芯片外框 间隙**上限**（坏例子 7.2）
# 间隙**下限**：再近就把芯片那一面的出脚通道堵死。走廊本身由放置层强制
# （place_rules.CORRIDOR = 2.0mm，按 courtyard 量）；这里的口径是封装包围盒，
# 比 courtyard 小 ~0.35mm，所以 1.2 ≈ 放置层 1.55mm 的净距。
CLOCK_MIN_MM = 1.2
CLOCK_PIN_MM = 3.0                   # 同走廊判定：±3mm 内同侧脚的网一起先布

# 两个布线吃紧时才动的旋钮（CLI `--gap` / `--mp`）。默认值 = f103 四块验证过的档位，
# 0.5mm 脚距 QFN 这种"脚全平齐、每只脚只能直着出去"的封装才需要放大。
PLACE_GAP: float | None = None        # None → place_rules 自己的默认 0.45mm
ROUTE_MP = 60                         # FreeRouting --mp

# 铜级 DRC（须 0）——与 d2s/Placement 的口径一致：短路/间距/孔/线宽/悬空孔/未连通/铜到边
COPPER = {'shorting_items', 'clearance', 'solder_mask_bridge', 'hole_clearance',
          'hole_to_hole', 'track_width', 'via_dangling', 'unconnected',
          'copper_edge_clearance'}

# 每块的语义（§二-7「设计意图一句话」）。写的是**从原理图读得出来的事实**，
# 不吹设计意图以外的牛：块是人画的，我们只负责说清它是什么、边在哪。
BLOCK_META = {
    'mcu_stm32f103': {
        'category': 'mcu',
        'rationale': 'STM32F103C8 最小系统簇：去耦贴 VDD 脚、8MHz 晶振贴 OSC 脚、'
                     'BOOT0/复位阻容随手放；数字侧向板内，调试/时钟在芯片近旁。',
        'placement_prior': {'anchor': 'board', 'max_mm': 0, 'rationale': '主芯片居中，'
                            '去耦按电源脚就近（R2），晶振贴 OSC 脚（R3），其余就近（R4）'},
    },
    'usb_dev_micro': {
        'category': 'interface',
        'rationale': 'USB Micro-B 口 + 22Ω 串阻 + D+/D- 走对：座子开口朝板边，'
                     '两串阻贴近座子，D+/D- 并行走。',
        'placement_prior': {'anchor': 'J1', 'max_mm': 15.0,
                            'rationale': 'USB 座必须贴板边且开口朝外（R1 接口一面对外），'
                                         '串阻就近（R4）'},
    },
    'ldo_ap2112k_3v3': {
        'category': 'power',
        'rationale': 'AP2112K-3.3 线性稳压：输入输出各一颗 1µF 贴脚，使能脚上拉，'
                     '热回路（IN–GND、OUT–GND）尽量短。',
        'placement_prior': {'anchor': 'U4', 'max_mm': 8.0,
                            'rationale': '输入/输出电容按电源脚就近贴（R2），输入侧朝板边'},
    },
    'rs485_sp3485': {
        'category': 'bus',
        'rationale': 'SP3485 半双工收发器 + 三脚端子（A/B/GND）：收发器居中，'
                     'A/B 直接拉到端子，去耦贴 VCC 脚。',
        'placement_prior': {'anchor': 'U3', 'max_mm': 20.0,
                            'rationale': '端子贴板边开口朝外，收发器靠数字侧'},
    },
}


def kpy() -> str:
    """KiCad 自带 python（pcbnew 只有它认）。三级解析同 kca：env > 机器配置 > 默认。"""
    p = os.environ.get('HWS_KICAD_PY')
    if p:
        return p
    cfg = Path.home() / '.harnesspcb.json'
    if cfg.exists():
        return json.loads(cfg.read_text())['HWS_KICAD_PY']
    raise SystemExit('找不到 KiCad python：设 HWS_KICAD_PY 或写 ~/.harnesspcb.json')


def kcli() -> str:
    p = os.environ.get('HWS_KICAD_CLI')
    if p:
        return p
    cfg = Path.home() / '.harnesspcb.json'
    if cfg.exists():
        v = json.loads(cfg.read_text()).get('HWS_KICAD_CLI')
        if v:
            return v
    raise SystemExit('找不到 kicad-cli：设 HWS_KICAD_CLI 或写 ~/.harnesspcb.json')


# --------------------------------------------------------------- 读裁出来的块
# 器件节点的头：`(symbol (lib_id "库:符号") …)`。
# ⚠ `lib_id` 不一定是第一个孩子——本地改过缓存副本的符号前面会多一行
# `(lib_name "…")`（2026-10-29 的 stm32_Example 页有 9 个这样的件）。早先写死
# `(symbol (lib_id …` 会把它们整件漏掉，件表少 9 件而"回读门"照样绿（两边用同一套
# 错读法比对，等于没比）。extract.is_inst 已改成"任意孩子是 lib_id"，这里对齐。
_INST_HEAD = r'\(symbol\s*(?:\(\s*lib_name\s+"[^"]*"\s*\)\s*)?\(\s*lib_id\s+"'
SYM_RE = re.compile(
    _INST_HEAD + r'([^"]+)"\s*\)\s*\(\s*at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
    r'(.*?)(?=' + _INST_HEAD + r'|\Z)', re.S)


def read_sch(sch: Path) -> dict:
    """从块的 .kicad_sch 读出件表、网表、端口。

    位号/封装/数值直接从文件里抓（IL 的 `extract` 只留 Reference/Value，不留 Footprint）；
    网表仍走 IL 的 `extract`，它已经把"引脚-导线-标签"连成网了。
    """
    if str(ILTOOLS) not in sys.path:
        sys.path.insert(0, str(ILTOOLS))
    import extract                                                # noqa: PLC0415

    text = sch.read_text(encoding='utf-8')
    parts = []
    for m in SYM_RE.finditer(text):
        lib, x, y, rot, body = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        ref = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', body)
        val = re.search(r'\(property\s+"Value"\s+"([^"]*)"', body)
        fp = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', body)
        if not ref or ref.group(1).startswith('#'):
            continue                                   # 电源符号不上板
        parts.append({'ref': ref.group(1), 'lib': lib,
                      'value': (val.group(1) if val else '') or '',
                      'footprint': (fp.group(1) if fp else '') or '',
                      'x': float(x), 'y': float(y), 'rot': float(rot)})

    # **多单元符号在文件里是几个 `(symbol … (unit N))` 节点，但板上是一只件**
    # （2026-10-30 BP5 逼出来的）：`R_Array` 四联网络每单元一个节点，`parts` 里
    # 同一只会出现四次（`bp5_io_buffer_0_3` 的 RN301-305 各 ×4、UPduino 的 U2 ×4、
    # Watchy 的 U8 ×2、kaybee 的 Q401 ×2）。卡里的 `parts` 是**件表**，一只件一行。
    # 合并口径：留首次出现的位号与坐标，值/封装/lib 取第一个非空的（有的单元节点
    # 只填了前一半属性，另一半是空的）。
    uniq, seen, n_dup = [], {}, 0
    for p in parts:
        q = seen.get(p['ref'])
        if q is None:
            seen[p['ref']] = p
            uniq.append(p)
            continue
        n_dup += 1
        for k in ('value', 'footprint', 'lib'):
            q[k] = q[k] or p[k]
    parts = uniq
    if n_dup:
        print(f'   多单元符号合并 {n_dup} 个单元节点（{len(parts)} 只件）', flush=True)

    il = extract.extract(str(sch))
    nets = {}
    unnamed = 0
    nc = []
    for n in il['nets']:
        nm = n.get('name')
        if not nm and len(n['pins']) == 1:
            # **无名 + 只有一只脚 = 空脚（NC），不是网**（10-30）。kicad-cli 的网表把
            # 悬空脚写成单脚网，照抄进卡里就等于宣布"这里有个网"，而板上那只焊盘连
            # 网名都没有——`sdi.cardpcb` 的门①会当场对不上（status_led 的 D9.2 就是）。
            # 有名字的单脚网照留：那是口岸（块内一只脚、块外还挂着人）。
            nc.append((n['pins'][0][0], n['pins'][0][1]))
            continue
        if not nm:
            unnamed += 1
            nm = f'_{sch.stem}_N{unnamed}'             # 无名网（块内部线）给个本地名
        nets[nm] = [{'ref': r, 'pin': p} for r, p in n['pins']]
    if nc:
        print(f'   空脚 {len(nc)} 只（无名单脚，不写进卡）：'
              + '、'.join(f'{r}.{p}' for r, p in nc), flush=True)
    ports = []
    for l in il['labels']:
        t = str(l.get('text') or '')
        if t and t not in ports:
            ports.append(t)
    rails = sorted({n['name'] for n in il['nets'] if n.get('power') and n.get('name')})
    return {'parts': parts, 'nets': nets, 'ports': ports, 'rails': rails}


def main_rail(rails: list) -> str | None:
    """内层第二条平面放哪根电源：优先 3.3V 族，其次 VBUS/5V。GND 固定走 In1。"""
    cand = [r for r in rails if r.upper() not in ('GND', 'FG', 'AGND', 'PGND')]
    for pat in ('3V3', '3.3V', 'VCC', 'VBUS', '5V'):
        for r in cand:
            if pat.upper() in r.upper():
                return r
    return cand[0] if cand else None


# ------------------------------------------------------------------- 五件套
def to_hier_labels(text: str, ports: list) -> str:
    """把块的端口标签改成**层级标签**（块规范 §二 硬性合同第 1 条）。

    裁块产物沿用原页的本地 `label`——本地标签一进大工程就和别处撞名，所以规格
    要求接口一律用 `hierarchical_label`（块将来是当子页插进去的）。**只改标签
    类型，坐标/字号/对齐一个不动**，所以块仍然"和原页长得一样"。
    """
    for p in ports:
        old = f'(label "{p}"'
        new = f'(hierarchical_label "{p}" (shape bidirectional)'
        if old not in text:
            raise SystemExit(f'✗ 端口 {p} 在原理图里找不到本地标签，没法升级成层级标签')
        text = text.replace(old, new)
    return text


def write_bundle(block: str, info: dict, bdir: Path, sch: Path, provenance: dict,
                 meta: dict | None = None, sch_text: str | None = None) -> str | None:
    """一个块的"五件套"（sch / 设计表 / 语义侧车 / KiCad 元数据 / 平面）。

    `meta` 换掉库内那张硬编码的 `BLOCK_META`（第三方裁的块不在表里）；
    `sch_text` 是**已经改好标签**的原理图文本——由调用方（`block_sch`）负责把
    全局标签拆成"出块→层级标签 / 块内→本地标签"，这里就不再无条件 `to_hier_labels`。
    """
    rails = info['rails']
    plane = main_rail(rails)
    meta = BLOCK_META.get(block, {}) if meta is None else meta
    # ① 原理图片段：原样搬进来，只把端口标签升级成层级标签（§二-1）
    if sch_text is None:
        sch_text = to_hier_labels(sch.read_text(encoding='utf-8'), info['ports'])
    (bdir / f'{block}.kicad_sch').write_text(sch_text, encoding='utf-8')

    # ② 设计表片段（d2s / 拼装层 / 门禁共用的形状）
    conns = [{'ref': c['ref'], 'pin': c['pin'], 'net': nm}
             for nm, pins in info['nets'].items() for c in pins]
    nets = set(info['ports'])
    iface = []
    for p in info['ports']:
        pins = info['nets'].get(p) or []
        mate = p[:-1] + ('-' if p.endswith('+') else '+') if p.endswith(('+', '-')) else None
        cls = 'bus_diff' if (mate and mate in nets) else 'digital'
        e = {'net': p, 'kind': 'hlabel', 'class': cls,
             'exit': {'pin': f"{pins[0]['ref']}-{pins[0]['pin']}" if pins else '',
                      'connector': any((x['ref'][:1] == 'J') for x in pins)}}
        if mate:
            e['diff_pair'] = mate
        iface.append(e)
    table = {'$schema': 'pcbstudio.blocktable.v1', 'block': block,
             'interface': iface, 'power_rails': rails,
             'parts': [{k: p[k] for k in ('ref', 'lib', 'value', 'footprint', 'x', 'y')}
                       for p in info['parts']],
             'conns': conns,
             '$schema_note': '由 sdi.blockpcb 从裁块产物生成；parts/conns 与 d2s 全板设计表同构。'}
    (bdir / f'{block}.table.json').write_text(
        json.dumps(table, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')

    # ③ 语义侧车（§四 v1.1）
    sem = {'schema': 'pcbstudio.block.v1.1',
           'category': meta.get('category', 'unknown'),
           'rationale': meta.get('rationale') or provenance.get('basis', ''),
           'power_rails': rails,
           'interface': [{'name': i['net'], 'kind': 'signal', 'class': i['class'],
                          'direction': 'bidirectional',
                          **({'diff_pair': i['diff_pair']} if i.get('diff_pair') else {})}
                         for i in iface],
           'slots': meta.get('slots', []),
           'placement_prior': meta.get('placement_prior', {}),
           'hot_loop': [],
           'geometry_status': 'pending',
           'provenance': provenance}
    if meta.get('internal_nets'):
        # 块内自造网（不外连）：出板时它们不参与跨块连接，摆放/布线仍要按普通网处理
        sem['internal_nets'] = list(meta['internal_nets'])
    (bdir / 'block.semantic.json').write_text(
        json.dumps(sem, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # ④ KiCad 元数据
    (bdir / f'{block}.json').write_text(json.dumps({
        'description': provenance.get('basis', ''),
        'keywords': ' '.join(sorted({p['value'] for p in info['parts'] if p['value']})[:8]).lower(),
        'fields': {'PCBSTUDIO': 'v1', 'PCBSTUDIO_BLOCK': block}},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return plane


def geom_design(block: str, info: dict, plane: str | None) -> dict:
    """给 d2s board 的设计表：4 层、In1=GND、In2=主电源平面。"""
    planes = {'In1.Cu': 'GND'}
    if plane:
        planes['In2.Cu'] = plane
    return {
        '$schema_note': f'pcbstudio.design.v1 —— 块级几何（{block}）',
        'project': {'name': block, 'title': f'{block} 块的相对几何'},
        'board': {'width': BOARD_W, 'height': BOARD_H, 'layers': 4, 'planes': planes,
                  'mounting_holes': 0, 'hole_d': 0.0, 'hole_edge_offset': 0.0},
        'parts': info['parts'],
        'conns': [{'ref': c['ref'], 'pin': c['pin'], 'net': nm}
                  for nm, pins in info['nets'].items() for c in pins],
        'power': {'rails': info['rails'], 'pwr_flags': []},
        'no_connect': [], 'keep_connected': [], 'anchors': [],
    }


# ------------------------------------------------------------------ 三个小步
SHRINK = r'''
import sys, json, pcbnew
src, out, margin = sys.argv[1], sys.argv[2], float(sys.argv[3])
b = pcbnew.LoadBoard(src)
def crt(f):
    try:
        c = f.GetCourtyard(pcbnew.F_CrtYd)
        if c.OutlineCount(): return c.BBox()
    except Exception: pass
    return f.GetBoundingBox(False, False)
bbs = [crt(f) for f in b.GetFootprints()]
x0 = min(q.GetLeft() for q in bbs)/1e6 - margin
x1 = max(q.GetRight() for q in bbs)/1e6 + margin
y0 = min(q.GetTop() for q in bbs)/1e6 - margin
y1 = max(q.GetBottom() for q in bbs)/1e6 + margin
for d in list(b.GetDrawings()):
    if d.GetLayer() == pcbnew.Edge_Cuts:
        b.Remove(d)
pts = [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
for i in range(4):
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(pcbnew.VECTOR2I(int(pts[i][0]*1e6), int(pts[i][1]*1e6)))
    s.SetEnd(pcbnew.VECTOR2I(int(pts[(i+1)%4][0]*1e6), int(pts[(i+1)%4][1]*1e6)))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(int(0.1*1e6))
    b.Add(s)
b.Save(out)
print(json.dumps({"w": round(x1-x0,2), "h": round(y1-y0,2)}))
'''

NC_TAG = r'''
import sys, pcbnew
src, out = sys.argv[1], sys.argv[2]
b = pcbnew.LoadBoard(src)
# 未连接的脚（"不接"叉）在板上是**没有网的焊盘**。它们在板上是实打实的铜，可
# Specctra DSN 只把焊盘写进 `(net ... (pins ...))`，空网焊盘不属于任何网 →
# FreeRouting 认为跟它们没有间距约束，过孔直接贴上去（10-27 MCU 块实测 50µm、
# USB 块 150µm，两条铜级）。
#
# 办法：**每个未连脚单独挂一个一次性的网**，布线时就按普通异网留间距；单脚网
# 没有第二个脚要连，布线器不会去走它。（第一版把 53 个脚挂到同一个 `__NC__`
# 网上，等于要求把它们全部连起来，布线器在板上拉出一堆废线——所以必须一脚一网。）
# 归一化那步再把网全部摘掉，产物里不留这些假网。
tagged = 0
for f in b.GetFootprints():
    for pad in f.Pads():
        if pad.GetNetname() != '':
            continue
        name = '__NC_%s_%s__' % (f.GetReference(), pad.GetNumber())
        net = pcbnew.NETINFO_ITEM(b, name)
        b.Add(net)
        pad.SetNet(net)
        tagged += 1
b.Save(out)
print('nc_pads_tagged', tagged)
'''

PORT_STUB = r'''
import sys, math, pcbnew
src, out, nets_s, length_mm = (sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
LEN = int(float(length_mm) * 1e6)
W = 200000
MIN = 600000            # 太短就不算桩（挡不了拐弯，白占铜）
CLEAR = 225000 + W // 2  # 走线中心到异网铜边的距离
ports = set(n for n in nets_s.split(',') if n)
b = pcbnew.LoadBoard(src)


def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


# 先把**其它网**的铜收成一份障碍表（焊盘=轴对齐矩形，走线=带宽度线段）。
# 桩是预先落下去的铜，它自己必须合法：10-29 mcu_stm32f103 里 U1-5(PD0) 就是
# 晶振脚，Y1 贴着 U1 只有 0.55mm——盲推 1.7mm 直接压到晶振上。
obs = []
for f in b.GetFootprints():
    for p in f.Pads():
        bb = p.GetBoundingBox()   # 轴对齐外框：0.5mm 节距的脚必须按矩形算，
        obs.append((p.GetNetname(), 'r',     # 按外接圆会把邻脚量成 1.06mm 远、
                    bb.GetX() + bb.GetWidth() / 2, bb.GetY() + bb.GetHeight() / 2,
                    bb.GetWidth() / 2, bb.GetHeight() / 2))   # 一根桩都落不下
for t in b.GetTracks():
    nm = t.GetNetname()
    if t.GetClass() == 'PCB_VIA':
        v = t.GetPosition()
        obs.append((nm, 'c', v.x, v.y, t.GetWidth() / 2))
    else:
        s, e = t.GetStart(), t.GetEnd()
        obs.append((nm, 's', s.x, s.y, e.x, e.y, t.GetWidth() / 2))


def free_to(x, y, net):
    for o in obs:
        if o[0] == net:
            continue
        if o[1] == 'r':
            ddx = max(0.0, abs(x - o[2]) - o[4])
            ddy = max(0.0, abs(y - o[3]) - o[5])
            d, half = math.hypot(ddx, ddy), 0
        elif o[1] == 'c':
            d, half = math.hypot(x - o[2], y - o[3]), o[4]
        else:
            d, half = seg_dist(x, y, o[2], o[3], o[4], o[5]), o[6]
        if d < CLEAR + half:
            return False
    return True


added = 0
for f in b.GetFootprints():
    pads = list(f.Pads())
    if len(pads) < 8:
        continue                 # 阻容/接插件不当"口岸源头"（R5/R6 也挂口岸网）
    fpc = f.GetPosition()
    for pad in pads:
        net = pad.GetNetname()
        if net not in ports:
            continue
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            continue
        c = pad.GetPosition()
        dx, dy = c.x - fpc.x, c.y - fpc.y
        # 出盘方向 = 本体中心 → 焊盘，取主轴（QFP/QFN 一列脚都朝同一条轴外）
        if abs(dx) >= abs(dy):
            ux, uy = (1 if dx >= 0 else -1), 0
        else:
            ux, uy = 0, (1 if dy >= 0 else -1)
        # 逐点探，撞到异网铜就截断（自己的焊盘是同网，不挡）
        reach = 0
        step = 50000
        while reach + step <= LEN:
            nxt = reach + step
            if not free_to(c.x + ux * nxt, c.y + uy * nxt, net):
                break
            reach = nxt
        if reach < MIN:
            print('STUBSHORT', f.GetReference(), pad.GetNumber(), net,
                  round(reach / 1e6, 2))
            continue
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(c.x, c.y))
        t.SetEnd(pcbnew.VECTOR2I(c.x + ux * reach, c.y + uy * reach))
        t.SetWidth(W)
        t.SetLayer(pad.GetLayer())
        t.SetNetCode(pad.GetNetCode())
        b.Add(t)
        added += 1
        print('STUB', f.GetReference(), pad.GetNumber(), net, round(reach / 1e6, 2))
b.Save(out)
print('port_stubs', added)
'''

THERMAL_CHECK = r'''
"""散热盘打孔体检（只读）：量最终板上"同网大面积盘里落了几枚同网过孔"。

打孔本身由 `route_rl/fanout.py` 的散热盘规则在**布线后**落铜（布线前落的铜会被
SES 回灌抹掉，见 blockpcb 里那一大段时序注释）。这里只负责量：
每只散热盘要求 >= 下限（=1，电气接地），且不超过几何装得下的数。不按面积加码：
散热走芯片上贴的散热片（2026-10-29 用户拍板），PCB 这边只保 EP 接地。
"""
import sys, json, pcbnew
(src, planes_s, pad_min, floor_base) = (sys.argv[1], sys.argv[2],
                                        float(sys.argv[3]), int(sys.argv[4]))
planes = set(n for n in planes_s.split(',') if n)
PAD_MIN_MM2 = pad_min
VIA_D, INSET = 600000, 150000
PITCH = 1200000
b = pcbnew.LoadBoard(src)
vias = [t for t in b.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
rows = []
for f in b.GetFootprints():
    for pad in f.Pads():
        net = pad.GetNetname()
        if net not in planes:
            continue
        if not (pad.IsOnLayer(pcbnew.F_Cu) or pad.IsOnLayer(pcbnew.B_Cu)):
            continue                       # 钢网/阻焊开窗不是铜
        sz = pad.GetSize()
        area = sz.x * sz.y / 1e12
        if area < PAD_MIN_MM2:
            continue
        n = 0
        for v in vias:
            if v.GetNetname() == net and pad.HitTest(v.GetPosition(), 0):
                n += 1
        # 几何上限：焊盘内缩后能排几个网格点
        nx = int(max(0, sz.x / 2 - VIA_D / 2 - INSET) // PITCH) * 2 + 1
        ny = int(max(0, sz.y / 2 - VIA_D / 2 - INSET) // PITCH) * 2 + 1
        cap = nx * ny
        rows.append({'pad': f'{f.GetReference()}-{pad.GetNumber()}', 'net': net,
                     'area': round(area, 1), 'vias': n, 'cap': cap,
                     'floor': max(1, min(floor_base, cap))})
print('THERMALCHK ' + json.dumps(rows, ensure_ascii=False))
'''

NORMALIZE = r'''
import sys, pcbnew
src, out = sys.argv[1], sys.argv[2]
b = pcbnew.LoadBoard(src)
# 原点 = **板框左上角**（不是器件位置的中心）：块的几何要能被拼板层当成"一张矩形图"
# 直接平移复用，原点必须在板角上，否则每个块的落位都要额外记一个偏移。
# 早期版本按"器件最小坐标"归一，结果板边跑到负坐标（-4.34），拼板时整块错位。
pts = []
for d in b.GetDrawings():
    if d.GetLayer() != pcbnew.Edge_Cuts or not hasattr(d, 'GetStart'):
        continue
    pts.append(d.GetStart()); pts.append(d.GetEnd())
if pts:
    minx = min(p.x for p in pts); miny = min(p.y for p in pts)
else:
    fps = list(b.GetFootprints())
    minx = min(f.GetPosition().x for f in fps)
    miny = min(f.GetPosition().y for f in fps)
# 摘掉布线期的假网：产物里未连接脚必须是"没有网"，不能带一个原理图里不存在的网。
for f in b.GetFootprints():
    for pad in f.Pads():
        if pad.GetNetname().startswith('__NC_'):
            pad.SetNetCode(0)
d = pcbnew.VECTOR2I(minx, miny)
for f in b.GetFootprints():
    f.SetPosition(f.GetPosition() - d)
for t in b.GetTracks():
    t.SetStart(t.GetStart() - d); t.SetEnd(t.GetEnd() - d)
for x in b.GetDrawings():
    try:
        x.SetStart(x.GetStart() - d); x.SetEnd(x.GetEnd() - d)
    except Exception:
        pass
for z in b.Zones():
    z.Move(pcbnew.VECTOR2I(-minx, -miny))
b.Save(out)
print('normalized', -minx/1e6, -miny/1e6)
'''


EDGE_REPORT = r'''
import sys, json, wx
wx.App(False)                     # 无 wxApp 时 zone/几何 SWIG 调用返回脏值
import pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
bb = b.GetBoardEdgesBoundingBox()
x0, y0 = bb.GetX()/1e6, bb.GetY()/1e6
x1, y1 = bb.GetRight()/1e6, bb.GetBottom()/1e6
out = {'w': round(x1-x0, 2), 'h': round(y1-y0, 2), 'conn': []}
for f in b.GetFootprints():
    c = f.GetCourtyard(pcbnew.F_CrtYd)
    q = c.BBox() if c.OutlineCount() else f.GetBoundingBox(False, False)
    gaps = {'W': q.GetX()/1e6 - x0, 'E': x1 - q.GetRight()/1e6,
            'N': q.GetY()/1e6 - y0, 'S': y1 - q.GetBottom()/1e6}
    side = min(gaps, key=gaps.get)
    out['conn'].append({'ref': f.GetReference(), 'edge': side,
                        'gap': round(gaps[side], 2)})
print('@@' + json.dumps(out))
'''


NET_REPORT = r'''
import sys, json, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
out = {}
for f in b.GetFootprints():
    ref = f.GetReference()
    for p in f.Pads():
        n = p.GetNetname()
        if n:
            out['%s.%s' % (ref, p.GetNumber())] = n
print('@@' + json.dumps(out))
'''


CLOCK_NETS = r'''
import sys, json, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
CLK = ('crystal', 'resonator', 'oscillator')
PWR = ('GND', 'FG', 'AGND', 'PGND', 'VSS')

def is_pwr(n):
    if not n or n.upper() in PWR or '__NC' in n:
        return True
    u = n.upper()
    return u.startswith('+') or u.startswith('V') or any(
        k in u for k in ('VDD', 'VCC', '3V3', 'VBUS', 'VIN', 'VOUT'))

fps = list(b.GetFootprints())
nets = set()
clk_refs = [f for f in fps
            if any(k in (f.GetFPIDAsString() + ' ' + f.GetValue()).lower() for k in CLK)]
for f in clk_refs:
    for p in f.Pads():
        n = p.GetNetname()
        if n and not is_pwr(n):
            nets.add(n)
# 同口岸的邻居也一起先布：时钟脚那几只脚占的是同一条出线走廊（0.5mm 节距的
# 一排脚只能从同一条缝里出来），只放时钟网独占走廊，邻居（NRST/BOOT0 这类）
# 就被挤死了（实测 NRST 剩 1 根布不通）。走廊是共享资源，就得一起排。
# hub 必须是与晶体共网的器件（不能只按"焊盘最多"挑：块里可能有两个大件）。
clk_ids = {f.GetReference() for f in clk_refs}
cand = [f for f in fps if f.GetReference() not in clk_ids
        and any(p.GetNetname() in nets for p in f.Pads())]
hub = max(cand, key=lambda f: len(list(f.Pads()))) if cand else None
if hub and clk_refs:
    c = hub.GetPosition()
    hp = [(p, p.GetPosition()) for p in hub.Pads()]
    side = lambda q: ('W' if q.x < c.x else 'E') if abs(q.x - c.x) > abs(q.y - c.y) else ('N' if q.y < c.y else 'S')
    anchor = [(side(q), q) for p, q in hp if p.GetNetname() in nets]
    tol = __PIN_TOL__                             # pcbnew 坐标是纳米，别拿 mm 去比
    for p, q in hp:
        s = side(q)
        if not any(s0 == s and (abs(q.y - q0.y) <= tol if s in 'EW' else abs(q.x - q0.x) <= tol)
                   for s0, q0 in anchor):
            continue
        n = p.GetNetname()
        if n and not is_pwr(n):
            nets.add(n)
print('@@' + json.dumps({'nets': sorted(nets),
                         'clock': sorted({p.GetNetname() for f in clk_refs for p in f.Pads()
                                          if p.GetNetname() and not is_pwr(p.GetNetname())})}))
'''


CLOCK_GATE = r'''
import sys, json, math, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
CLK = ('crystal', 'resonator', 'oscillator')
PWR = ('GND', 'FG', 'AGND', 'PGND', 'VSS')

def is_pwr(n):
    if not n or n.upper() in PWR:
        return True
    u = n.upper()
    return u.startswith('+') or u.startswith('V') or any(
        k in u for k in ('VDD', 'VCC', '3V3', 'VBUS', 'VIN', 'VOUT'))

def is_clk(f):
    return any(k in (f.GetFPIDAsString() + ' ' + f.GetValue()).lower() for k in CLK)

def bbox(f):
    r = f.GetBoundingBox(False, False)
    return (r.GetLeft() / 1e6, r.GetTop() / 1e6, r.GetRight() / 1e6, r.GetBottom() / 1e6)

def gap(a, c):
    dx = max(a[0] - c[2], c[0] - a[2], 0.0)
    dy = max(a[1] - c[3], c[1] - a[3], 0.0)
    return math.hypot(dx, dy)

fps = list(b.GetFootprints())
clk = [f for f in fps if is_clk(f)]
cnets = {p.GetNetname() for f in clk for p in f.Pads()
         if p.GetNetname() and not is_pwr(p.GetNetname())}
ids = {f.GetReference() for f in clk}
cand = [f for f in fps if f.GetReference() not in ids
        and any(p.GetNetname() in cnets for p in f.Pads())]
hub = max(cand, key=lambda f: len(list(f.Pads()))) if cand else None
hpad = {}
if hub:
    for p in hub.Pads():
        n = p.GetNetname()
        if n and not is_pwr(n):
            q = p.GetPosition()
            hpad.setdefault(n, []).append((q.x / 1e6, q.y / 1e6))
vias = {}
for t in b.GetTracks():
    if t.Type() == pcbnew.PCB_VIA_T:
        vias[t.GetNetname()] = vias.get(t.GetNetname(), 0) + 1
out = {'hub': hub.GetReference() if hub else None, 'gap': None, 'clock_nets': {}}
if hub:
    # 晶体离芯片多远：**外框间隙**（不是中心距，也不是单个焊盘距）——晶体是四脚
    # 贴片，两个信号脚在对角线上，总有一个焊盘离芯片脚 >4mm，那是封装自带的，
    # 拿它当尺子会把"已经贴死"的摆位判成不合格。真正的"离得远"是外框间隙。
    out['gap'] = round(min([gap(bbox(f), bbox(hub)) for f in clk]), 2) if clk else None
for f in clk:
    for p in f.Pads():
        n = p.GetNetname()
        if not n or is_pwr(n) or n in out['clock_nets']:
            continue
        q = p.GetPosition()
        x, y = q.x / 1e6, q.y / 1e6
        d = min([math.hypot(a - x, c - y) for a, c in hpad.get(n, [])], default=None)
        out['clock_nets'][n] = {'pad': '%s.%s' % (f.GetReference(), p.GetNumber()),
                                'mm': round(d, 2) if d is not None else None,
                                'vias': vias.get(n, 0)}
print('@@' + json.dumps(out))
'''


def net_gate(pcb: Path, sch: Path) -> dict:
    """电气真值门：原理图网表里每一对（位号, 脚）在板上必须挂同名网。

    这是之前缺的那把尺子。布线的门只数"未连通 / 铜级 DRC"，而**丢了网的焊盘**
    在布线器眼里等于空气：不受间距约束、不参与连通计数，于是"0 未连通 /
    0 铜级 DRC"照样全绿，去耦电容却整片悬空（10-27 实测：MCU 块丢了 8 个
    +3.3V 脚、8 个 GND 脚，USB 块整根 USB_D+ 都没了）。净结果的后果是拼板
    时跨块网找不到对端，块看着能用、其实是断的。

    口径：kicad-cli 的网表是唯一的"电路是什么"；`unconnected-*` 本来就不该
    挂网（留给临时网避让那一步），不算丢。
    """
    tmp = pcb.parent / '_netlist.xml'
    _run([kcli(), 'sch', 'export', 'netlist', '--format', 'kicadxml',
          '--output', str(tmp), str(sch)])
    import xml.etree.ElementTree as ET
    want = {}
    for net in ET.parse(tmp).getroot().findall('./nets/net'):
        raw = net.get('name') or ''
        if not raw or raw.startswith('unconnected-'):
            continue
        name = raw.lstrip('/').rsplit('/', 1)[-1]
        for node in net.findall('node'):
            ref, pin = node.get('ref'), node.get('pin')
            if ref and pin and not ref.startswith('#'):
                want[f'{ref}.{pin}'] = name
    tmp.unlink(missing_ok=True)

    r = _run([kpy(), '-c', NET_REPORT, str(pcb)], pcbnew_quirk=True)
    line = [l for l in r.stdout.splitlines() if l.startswith('@@')]
    got = json.loads(line[-1][2:])

    lost = [f'{k}→{v}' for k, v in want.items() if k not in got]
    wrong = [f'{k}: 应 {v} 实 {got[k]}' for k, v in want.items()
             if k in got and got[k] != v]
    return {'want': len(want), 'got': len(got), 'lost': lost, 'wrong': wrong}


def clock_nets(pcb: Path) -> tuple[list[str], list[str]]:
    """先布的网 = 时钟网 ∪ 与时钟脚同一条出线走廊的邻居网。

    用户规则："优先布时钟线"。为什么还要带上邻居：0.5mm 节距的一排脚只从**同一条
    缝**里出来，时钟霸占走廊把邻居挤死——实测只放 PD0/PD1 先布，NRST 剩 1 根布
    不通；把同侧 ±3mm 内的脚一起放进来，0 未连通且时钟网 0 孔。
    """
    r = _run([kpy(), '-c',
              CLOCK_NETS.replace('__PIN_TOL__', repr(CLOCK_PIN_MM * 1e6)),
              str(pcb)], pcbnew_quirk=True)
    line = [l for l in r.stdout.splitlines() if l.startswith('@@')]
    if not line:
        return [], []
    d = json.loads(line[-1][2:])
    return d.get('nets') or [], d.get('clock') or []


def clock_gate(pcb: Path) -> dict:
    """时钟规则量法：晶体外框 ↔ 芯片外框 间隙 / 每根时钟网的过孔数。

    这两行数就是"时钟件贴芯片、不许打孔"这条规则的**执行形态**。光写在文档里
    的规则会漂（R3 写在 docstring 里却从没实现，晶体被摆到 7.2mm 外、绕 3 孔）。
    另打印晶体信号焊盘 → 同网芯片脚的距离，只做参照不判定：四脚晶体的两个信号
    脚在对角线上，必有一个 >4mm，那是封装自带的，不是摆位问题。
    """
    r = _run([kpy(), '-c', CLOCK_GATE, str(pcb)], pcbnew_quirk=True)
    line = [l for l in r.stdout.splitlines() if l.startswith('@@')]
    return json.loads(line[-1][2:]) if line else {'clock_nets': {}}


def edge_report(pcb: Path) -> dict:
    """板框尺寸 + 每个器件的包络离最近板边多远（接插件必须贴边）。"""
    r = _run([kpy(), '-c', EDGE_REPORT, str(pcb)], pcbnew_quirk=True)
    line = [l for l in r.stdout.splitlines() if l.startswith('@@')]
    return json.loads(line[-1][2:])


def port_audit(pcb: Path, nets) -> dict:
    """量每只口岸焊盘的**走廊净宽**（异网走线/过孔离焊盘外框多远），见 escape.audit。

    要在 Placement 的 venv 里跑（`kicad_access` 在那边），所以走子进程。
    """
    out = pcb.with_name(pcb.stem + '_ports.json')
    _run([str(RL_PY), str(ROOT / 'escape.py'), '--board', str(pcb),
          '--nets', ','.join(nets), '--json', str(out)])
    return json.loads(out.read_text(encoding='utf-8'))


def seal_audit(pcb: Path) -> list:
    """量每只焊盘**还出不出得去**（见 seal.py）。有封死脚时退出码 1，不是错误。"""
    out = pcb.with_name(pcb.stem + '_seal.json')
    subprocess.run([str(RL_PY), str(ROOT / 'seal.py'), '--board', str(pcb),
                    '--json', str(out)], capture_output=True, text=True)
    if not out.exists():
        return []
    return json.loads(out.read_text(encoding='utf-8'))


def _run(cmd, pcbnew_quirk=False, echo=None, **kw) -> subprocess.CompletedProcess:
    """跑一步外部工具；失败就停（几何这条路不许"跳过不报"）。

    `pcbnew_quirk=` 容忍 SWIG 脚本的已知毛病：带 zone 的板子跑完 pcbnew 后
    `create wxApp` 断言、进程以 139 退出——**文件已经存好了**（10-27 实测）。
    判据放在调用方：只要 stdout 最后一行的 JSON 能解析，就算成功。

    `echo=` 正则：子进程**成功**时，stdout 里匹配的行照样打出来。此前这里是
    `capture_output` 之后成功就整段丢掉，route_rl 打印的「死脚 U2-9」「逃逸走廊
    门禁挡掉 176 个候选」「三种落孔策略各几只死脚」全被吞——2026-10-29 两轮
    mcu_stm32f411 就是这样只能靠事后对照实验反推病因的。
    """
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    ok = r.returncode == 0 or (pcbnew_quirk and r.returncode in (139, 134, -11, -6))
    if not ok:
        tail = (r.stdout or '')[-800:] + (r.stderr or '')[-800:]
        raise SystemExit(f'✗ {" ".join(str(c) for c in cmd[:3])}… 失败：\n{tail}')
    if echo is not None:
        for ln in (r.stdout or '').splitlines():
            if echo.search(ln):
                print('      ' + ln.rstrip(), flush=True)
    return r


#: route_rl 的哪些 stdout 行进构建日志（见 `_run` 的 echo= 注）。
ROUTE_ECHO = re.compile(r'\[fanout\]|死脚|NOSPOT|落孔策略|未布通|关键网|'
                        r'\b(priority|signal|power|finish)\b.*[：:]|keepout')

#: DRC 报告里 `[unconnected_items]` 那一块的正文（两条 `@(x,y): 东西` 行）。
_UNCONN_RE = re.compile(r'\[unconnected_items\][^\n]*\n((?:[ \t]+[^\n]*\n?)+)')


def _unconn_detail(txt: str) -> list[str]:
    """从 DRC 报告里抠出「谁和谁没连上」。

    报告原文长这样：
        [unconnected_items]: Missing connection between items
            Local override; error
            @(10.2475 mm, 12.9350 mm): Pad 9 [3V3] of U2 on F.Cu
            @(10.1729 mm,  9.0096 mm): Track [3V3] on F.Cu, length 0.1 mm

    此前这里只取 `Found N unconnected` 一个数字，哪根网、哪个脚全靠事后推。
    """
    out = []
    for blk in _UNCONN_RE.finditer(txt):
        items = []
        for ln in blk.group(1).splitlines():
            if '@(' in ln and ':' in ln:
                s = ln.split(':', 1)[1].strip()
                s = re.sub(r'\s+on\s+\S+.*$', '', s)     # 去掉 "on F.Cu, length …"
                items.append(s)
        if items:
            out.append(' ↔ '.join(items))
    return out


def build_one(block: str, sch: Path, bdir: Path, provenance: dict,
              info: dict | None = None, plane: str | None = None,
              card_mode: bool = False) -> dict:
    """一块 → 五件套 + 板级几何。

    `info` 给了就跳过 `write_bundle`（**块目录模式**：块卡是手写权威，五件套已经在
    块目录里了，重写一遍会把手写字段冲掉——见 `build_from_card`）。"""
    bdir.mkdir(parents=True, exist_ok=True)
    if info is None:
        info = read_sch(sch)
        plane = write_bundle(block, info, bdir, sch, dict(provenance))
        print(f'  {block}: {len(info["parts"])} 件 / {len(info["nets"])} 网 / '
              f'端口 {info["ports"]} / 平面 In2={plane or "—"}', flush=True)
    else:
        print(f'  {block}: {len(info["parts"])} 件 / {len(info["nets"])} 网 / '
              f'端口 {info["ports"]} / 平面 In2={plane or "—"}'
              f'（块目录模式，块卡不动）', flush=True)

    design = bdir / '_geom_design.json'
    design.write_text(json.dumps(geom_design(block, info, plane), ensure_ascii=False, indent=1))

    # 中间产物全塞 `_geom/`（和 PCBStudio.kicad_blocks 里现成的块同一套布局：
    # 块目录根上只留规格要的那几件，过程件进子目录，免得一个块摊开二十个文件）
    # ⚠ `_geom/measured.json` 是**量出来的资产**（原理图坐标/出口面/geometry_status），
    # 不是过程件——清过程件前先拿出来，否则块目录模式一跑就把坐标表删了。
    work = bdir / '_geom'
    measured_keep = None
    mp0 = work / '_card_measured.json'
    if card_mode:
        from . import card as _c0
        p0 = bdir / _c0.MEASURED
        if p0.is_file():
            measured_keep = p0.read_text(encoding='utf-8')
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    if measured_keep is not None:
        from . import card as _c1
        (bdir / _c1.MEASURED).write_text(measured_keep, encoding='utf-8')

    # ⓪ 电气验收（§三-②）：块 sch 自己过 ERC。块是"从人画页裁下来的一段"，
    #    ERC 报的多半是"这段单独看缺驱动/缺连接"——如实记数，不硬判死，但要报出来。
    rpt0 = bdir / '_geom' / 'erc.rpt'
    erc = subprocess.run([kcli(), 'sch', 'erc', '-o', str(rpt0), '--severity-all',
                          '--format', 'report', str(bdir / f'{block}.kicad_sch')],
                         capture_output=True, text=True)
    etext = rpt0.read_text(encoding='utf-8') if rpt0.exists() else (erc.stdout or '')
    ekinds = re.findall(r'^\[([a-z_]+)\]', etext, re.M)
    # 块单独开 ERC 必然带几类"上下文缺失"的报：层级标签在根页里没有父页（每个端口一条）、
    # 电源脚没有 PWR_FLAG 驱动。库里现成的块（rs485_node / mcu_min_stm32 / ldo_3v3）
    # 跑同一把尺子也是同样的报法，所以这里如实记账、不当失败。
    n_nc = ekinds.count('pin_not_connected')
    print(f'   ERC {len(ekinds)} 条：{", ".join(sorted(set(ekinds))) or "0"}'
          f'（其中 pin_not_connected {n_nc} = 端口 {len(info["ports"])} 个，'
          f'根页层级标签必然报）', flush=True)

    # ① 空板（footprint 同步 + 网赋值 + 4 层叠层）
    _run([sys.executable, str(D2S), 'board', '--sch', str(bdir / f'{block}.kicad_sch'),
          '--design', str(design), '--out', str(work)], cwd=str(D2S.parent))
    base = work / f'{block}_base.kicad_pcb'
    stackup = work / f'{block}_stackup.json'
    if not base.exists():
        raise SystemExit(f'✗ {block}: d2s board 没产出 {base.name}')

    # ② 规则摆件（接口一面对外 + 口岸脚朝外）
    placed = work / f'{block}_placed.kicad_pcb'
    cmd0 = [sys.executable, str(ROOT / 'place_rules.py'),
            '--board', str(base), '--out', str(placed),
            '--ports', ','.join(info['ports'])]
    if PLACE_GAP:
        cmd0 += ['--gap', str(PLACE_GAP)]
    r = _run(cmd0)
    head = [l for l in r.stdout.splitlines()
            if l.startswith('hub=') or l.startswith('口岸脚朝外') or l.startswith('  口岸脚 ')]
    print('\n'.join('   ' + l for l in head) or '   摆件完成', flush=True)

    # ③ 收边：PCB 尺寸尽可能小（接插件因此贴到板边）
    small = work / f'{block}_small.kicad_pcb'
    r = _run([kpy(), '-c', SHRINK, str(placed), str(small), str(EDGE_MARGIN)],
             pcbnew_quirk=True)
    size = json.loads(r.stdout.strip().splitlines()[-1])
    print(f'   收边 {size["w"]}×{size["h"]}mm', flush=True)

    # ③' 未连接脚挂临时网（否则布线器把它们当空气，过孔贴到脚上）
    tagged = work / f'{block}_small_nc.kicad_pcb'
    r = _run([kpy(), '-c', NC_TAG, str(small), str(tagged)], pcbnew_quirk=True)
    nc = [l for l in r.stdout.splitlines() if l.startswith('nc_pads_tagged')]
    if nc and nc[0].split()[-1] != '0':
        print(f'   未连接脚 {nc[0].split()[-1]} 个 → 挂临时网参与避让', flush=True)

    # ③''' 口岸脚直桩：出盘先**直着走 1.7mm** 再许拐弯。
    #   10-29 mcu_stm32f411 实测：口岸网在"先布"那轮独占整块板，布线器一律走最短
    #   路径——`IMU_SDA` 出盘 0.54mm 就往西横跑（y=7.123，横跨 x 7.13→9.68），把
    #   U2-44/45/46 三只脚的北向走廊一起扫掉；`IMU_SCL` 出盘 0.66mm 就斜向东北。
    #   结果 `escape` 体检里 U2-42 / U2-45 四条边**都没有出口面**，拼板时边界线进不来。
    #   试过给口岸焊盘套 rule area（见下条注释）：禁区无法按网豁免，会连口岸网自己的
    #   出线一起围死。改成**预置一段同网铜**——它既是自己那条网的起点（布线器从桩头
    #   接着走），又是别的网的障碍，等于在不动规则的前提下强行留出直线走廊。
    #   1.7mm 的依据：`escape` 的出口面按 r=1.2mm 取样，加上 QFP/QFN 盘的半长 0.44mm。
    stub_len = 1.7
    stubbed = work / f'{block}_small_nc_stub.kicad_pcb'
    r = _run([kpy(), '-c', PORT_STUB, str(tagged), str(stubbed),
              ','.join(info['ports']), str(stub_len)], pcbnew_quirk=True)
    sn = [l for l in r.stdout.splitlines() if l.startswith('port_stubs')]
    if sn and sn[0].split()[-1] != '0':
        print(f'   口岸脚直桩 {sn[0].split()[-1]} 根 × {stub_len}mm'
              f'（出盘先直走再许拐弯，护住出口面）', flush=True)
        tagged = stubbed

    # ③'''' 散热盘打孔**不在这里**——2026-10-29 实测踩了时序坑：
    #   10-29 mcu_stm32f411 的 U2 是 QFN-48-1EP，pad 49 = 5.6×5.6mm 的 GND 散热盘，
    #   占板面 7%、正压在板中央，整块板一个过孔都没落在盘里。当初在这里（布线前）
    #   打了 9 枚孔，结果**一枚也没进最终板**：SES 回灌会把平面网（GND/3V3）的
    #   via/wire 整棵重放，布线前落的铜第一轮就没了（stub_tv 有 9 枚 → priority_000
    #   里 0 枚，逐站数过）。`route_rl/fanout.py` 的文件头早就写了这条时序，它自己
    #   的落铜因此排在所有 FR pass 之后。
    #   现在打孔搬到 fanout 的散热盘规则里（同网大面积盘按网格落孔、孔盘在焊盘内
    #   不拉引线），**量法留在这里**当门禁：见 ⑤'''（量的是最终板上的孔数）。

    # ④ 布线（4 层：F/B 信号 + In1 GND 平面 + In2 电源平面）
    # clearance 阶梯：先试 245µm（既有甜点），上一档还留未布通才往下降，最低一档
    # 就停在板规 200µm。块小、件挤，单档 245 撞上"两个焊盘 + 一个扇出 keepout
    # 把唯一走廊堵死"时（10-27 USB 块 R6.1→J1.4 实测）就永远布不通；LadderController
    # 一旦拿到"完成度/铜级双 0"就早停，所以正常块不会多跑一轮。
    # ④' 时钟网（+ 同一走廊的邻居）先布。识别失败不挡流程——只是少了这层保证。
    clk, pure_clk = clock_nets(tagged)
    extra = [n for n in clk if n not in pure_clk]

    # ③'' 口岸走廊：**不建禁区，改成先把口岸网排进"先布"那一轮**。
    # 试过的做法是给口岸焊盘套一圈 rule area 禁区，让块自己的铜别贴死对外脚。实测
    # 走不通，而且是机制层面的：KiCad 的 rule area 无法按网豁免，禁区把**口岸网自己
    # 的出线**也一起围死 —— 要么剩未连通（只能退回无走廊），要么把 `Net-(J1-D±)`
    # 逼到内层（planes 直接 LAYER-DISCIPLINE 报错）。两次都拿不到合法板子。
    # 所以改成本来的目的：口岸网先布 + 后续网保护已布铜（route_rl `--priority-nets`
    # 的 protect_wiring），别的网就压不进口岸脚身边。
    # 真正让口岸脚"能出去"的是放置层的 `place_rules.port_face`（口岸脚朝块外）——
    # 10-28 实测根因就是朝向：`R3-2` 朝着自家 J1，翻 180° 后射线净空 0.70→6.00mm。
    # ③''' 接插件脚上的网也先进"先布"。接插件的焊盘间距是整板最挤的（TC2030 六脚
    # 1.27mm / JST-GH 1.25mm），出线走廊常常只有 1mm 出头；等 FreeRouting 按自己的
    # 网序排到它们时，走廊早被别的线占满。10-29 mcu_stm32f411 实测：SWDIO/SWCLK/SWO
    # 三根**一根铜都没有**（J2 内侧一列只能往西走，西边又被 C9/R2 的去耦与扇出
    # keepout 夹住）。平面网（3V3/GND）不进——那一圈由 planes pass 的铺铜负责。
    plane_nets = set(info.get('rails') or [])
    jnets = sorted({n for n, pins in info['nets'].items()
                    if n not in plane_nets
                    and any(str(q['ref']).startswith('J') for q in pins)})
    prio = list(dict.fromkeys(clk + list(info['ports']) + jnets))
    if prio:
        print(f'   先布 {len(prio)} 根：时钟 {"、".join(pure_clk) or "—"}'
              + (f'（同走廊邻居 {"、".join(extra)}）' if extra else '')
              + f' ＋ 口岸 {"、".join(info["ports"]) or "—"}'
              + (f' ＋ 接插件 {"、".join(jnets)}' if jnets else ''), flush=True)

    def _route(inp: Path, tag: str, fan_early: bool = False,
               fan_early_only: list[str] | None = None):
        """布一遍 + DRC。返回 (final, {kinds, copper, unconn, fan_nospot})。"""
        out = work / f'{block}_route{tag}'
        if out.exists():
            shutil.rmtree(out)
        cmd = [str(RL_PY), '-m', 'route_rl', '--board', str(inp),
               '--stackup', str(stackup), '--out', str(out), '--mp', str(ROUTE_MP),
               '--clearances',
               'priority=245,215,200;signal=245,215,200;'
               'power=245,215,200;finish=245,215,200']
        if fan_early:
            cmd += ['--fan-early']
        if fan_early_only:
            cmd += ['--fan-early-only', ','.join(fan_early_only)]
        if prio:
            cmd += ['--priority-nets', ','.join(prio)]
        _run(cmd, cwd=str(PLACEMENT), echo=ROUTE_ECHO)
        res = json.loads((out / 'result.json').read_text())
        fin = Path(res['final'])
        rp = work / f'{block}_drc{tag}.rpt'
        _run([kcli(), 'pcb', 'drc', '-o', str(rp), '--format', 'report',
              '--severity-all', str(fin)])
        t = rp.read_text(encoding='utf-8')
        ks = re.findall(r'^\[([a-z_]+)\]', t, re.M)
        mm = re.search(r'Found (\d+) unconnected', t)
        return fin, {'kinds': ks, 'copper': [k for k in ks if k in COPPER],
                     'unconn': int(mm.group(1)) if mm else -1,
                     'pairs': _unconn_detail(t),
                     'fan_nospot': res.get('fan_nospot') or []}

    def _say(drc, indent='   '):
        """把「没连上的那一对」点名打印出来——只报个数等于没报。"""
        for p in drc.get('pairs') or []:
            print(f'{indent}✗ 没连上：{p}', flush=True)

    final, drc = _route(tagged, '')
    print(f'   布线 → {final.name}：未连通 {drc["unconn"]}'
          f' / 铜级 {len(drc["copper"])}', flush=True)
    _say(drc)

    # ④' 平面网死脚重布（`--fan-early`）。扇出落孔位默认等 priority 跑完再找
    #    （省走廊，411 的 J2 教训）；代价是**细间距引脚场里相邻两个平面网脚只能
    #    有一个落孔**——10-29 mcu_stm32f103 实测：U1-8(GND) 先占住 0.5mm 脚距那
    #    一列的东侧走廊，U1-9(+3.3V) 再想走东侧就撞上它的过孔（0.5mm 间距 < 需
    #    0.6mm），西侧又被先布轮的 NRST 线横切，于是这颗焊盘**任何方向都出不去**，
    #    布线后 stitch 也救不了 → 未连通 1。改法：红了就再布一遍，让细间距脚在
    #    priority 之前就占位（keepout 挡住先布线），按 (未连通, 铜级) 取更好的。
    #    10-29 f103 实测是**跷跷板**：默认规划下后规划的 U1-9 死；只早占 U1-9 就换成
    #    U1-8 死（东侧走廊已被先布的 NRST 占掉，西侧被 PD1 的斜线封住）。根因不是哪只
    #    脚特殊，而是「谁先规划谁赢走廊」。所以按**占位地盘从大到小**各布一遍，按
    #    (未连通, 铜级) 取最好的，绿了就停：
    #      ①细间距脚全早占 —— 10-28 交付的绿板正是这么来的（U1-8 走东、U1-9 走西、
    #        NRST 被 keepout 挤到西侧绕行）。占位最大，三只脚最容易都安排开。
    #      ②只早占规划报「没有落孔位」的那几只 —— 占位最小，尽量不挡先布网。
    fan_kw: dict = {}
    if drc['unconn'] > 0:
        nl = drc.get('fan_nospot') or []
        tries = [('（细间距脚全早占）', {'fan_early': True})]
        if nl:
            tries.append((f'（只早占 {"、".join(nl)}）', {'fan_early_only': nl}))
        for i, (how, kw) in enumerate(tries):
            print(f'   ↻ 未连通 {drc["unconn"]} > 0 → 换「平面网脚布线前占位」重布一遍{how}',
                  flush=True)
            f2, drc2 = _route(tagged, f'_fan{i}', **kw)
            print(f'   重布 → 未连通 {drc2["unconn"]} / 铜级 {len(drc2["copper"])}',
                  flush=True)
            _say(drc2, '      ')
            if (drc2['unconn'], len(drc2['copper'])) < (drc['unconn'],
                                                        len(drc['copper'])):
                final, drc, fan_kw = f2, drc2, kw
            else:
                print('   重布没更好 → 保留前一块', flush=True)
            if drc['unconn'] == 0 and not drc['copper']:
                break

        # ③仍未绿就**同参数重跑**：FreeRouting 不保证逐位复现。10-29 mcu_stm32f103
        #   实测两条路（裁块模式 vs 块目录模式）摆件 14/14 完全一致、占位模式也一样，
        #   一个布出 未连通 0、另一个 1，差别只在 U1-9 那一下拐弯——既然变量是布线器
        #   自己，就重试取最好的那块，别去猜输入。
        for i in range(2):
            if drc['unconn'] == 0 and not drc['copper']:
                break
            print(f'   ↻ 仍未连通 {drc["unconn"]} → 同参数再布一遍'
                  f'（布线器不保证逐位复现，取最好的那块）', flush=True)
            f2, drc2 = _route(tagged, f'_rpt{i}', **fan_kw)
            print(f'   再布 → 未连通 {drc2["unconn"]} / 铜级 {len(drc2["copper"])}',
                  flush=True)
            _say(drc2, '      ')
            if (drc2['unconn'], len(drc2['copper'])) < (drc['unconn'],
                                                        len(drc['copper'])):
                final, drc = f2, drc2
            else:
                print('   再布没更好 → 保留前一块', flush=True)

    # ④ 封死脚体检（见 seal.py）＋ 按伤口重布。布的**过程中**看不出"这只脚出不来"，
    #    只有布完量自由面积才知道。10-29 mcu_stm32f411 实测：3 根未连通里有 2 只脚
    #    （U2.44 BOOT0 / U2.22 VCAP）是几何上已经出不来——挡它们的是**先布那轮**的线
    #    横切过 QFN 焊盘口：`IMU_SDA` 在盘尖外 0.09mm 处切掉 BOOT0 的出口（自由面积
    #    0.12mm² vs 焊盘本体 0.22mm²），`MCU_PA5`(B)/`PA7`(F) 把 VCAP 围成 2.88mm² 的
    #    死口袋。把 70 个扇出 keepout 全删了复测，两个数几乎不变 → 不是 keepout 的锅。
    #    试过静态规则"细间距器件的网全部先布"（U2 39 网 / 共 40 根）：411 从 [3,0,40]
    #    好转到 [2,0,37]，但 f103 四块里 mcu_stm32f103 未连通 0→1、usb_dev_micro 的
    #    R3-2 走廊被挤到 0.18mm（< 需 0.225）整轮崩 —— 一刀切否决（一帮两伤）。
    #    改成按量到的东西动手：只把**真封死**那只脚的网并进先布，重布一次，取更好的。
    #    判据是面积（自由面积 − 焊盘本体 <= 0.25mm²，与 seal.py 的 SEAL_AREA 同一口径）：
    #    换成"最远可达距离 < 1mm"会误伤 0.5mm 脚距 QFN 的正常脚（reach 0.81~0.85、
    #    自由面积还剩 1.0mm² 的邻居）。平面网不并进来——那一圈归 planes pass 铺铜。
    def _seal(pcb: Path):
        rows = seal_audit(pcb)
        return rows, [r for r in rows if r.get('sealed')]

    seal, sealed = _seal(final)
    if seal:
        print(f'   封死脚 {len(sealed)} / {len(seal)}（自由面积只比焊盘本体多 <= 0.25mm²，'
              f'同网铜不算障碍）'
              + ('：' + '、'.join(f'{r["pad"]}({r["net"]}) +{r["extra_mm2"]}mm²'
                                 for r in sealed[:6]) if sealed else ''), flush=True)
    add = list(dict.fromkeys(
        r['net'] for r in sealed
        if r['net'] and not r['net'].startswith('__NC_')
        and r['net'] not in plane_nets and r['net'] not in prio))
    if add:
        print(f'   ↻ 这些网并入先布重布：{"、".join(add)}（脚下有焊盘被封死）', flush=True)
        keep = prio
        prio = keep + add
        f2, drc2 = _route(tagged, '_seal', **fan_kw)
        seal2, sealed2 = _seal(f2)
        print(f'   重布 → {f2.name}：未连通 {drc2["unconn"]} / 铜级 '
              f'{len(drc2["copper"])} / 封死脚 {len(sealed2)}', flush=True)
        _say(drc2, '      ')
        if ((drc2['unconn'], len(drc2['copper']), len(sealed2))
                < (drc['unconn'], len(drc['copper']), len(sealed))):
            final, drc, seal, sealed = f2, drc2, seal2, sealed2
        else:
            prio = keep
            print('   重布没更好 → 保留前一块', flush=True)
    kinds, copper, unconn = drc['kinds'], drc['copper'], drc['unconn']

    # ④'' 口岸体检（见 escape.audit）：这只脚还进不进得来线。"够宽"用**出口面**判
    #    ——四条边里至少一条能把 0.2mm 线连着两侧 0.225 间隙直着送进来。为什么不是
    #    "异网铜离焊盘 ≥ 0.65"（先试过、已否）：串阻的口岸脚旁边必然有兄弟网那根线
    #    贴着过（R3-2 被 `Net-(J1-D-)` 压到 0.242mm，是 DRC 合法的极限），拿全向最近
    #    距离当门槛，合法板子也会红。最近距离照报，用来指认"是谁挡的"。
    aud = port_audit(final, info['ports']) if info['ports'] else {}
    no_pad = [n for n in info['ports'] if not any(v['net'] == n for v in aud.values())]
    if no_pad:
        raise SystemExit(f'✗ {block}: 端口网上找不到焊盘 {no_pad}（网名对不上板）')
    gaps = []
    for k, v in sorted(aud.items()):
        near = [x for x in (v['track'], v['via']) if x is not None]
        gaps.append((bool(v['faces']), min(near) if near else 9.9, k, v))
    gaps.sort()
    print('   口岸出口（这条边能不能进线；线/孔 = 异网铜最近多远，指认谁挡的）：'
          + '  '.join(f'{k} {"".join(v["faces"]) or "✗"}({g:.2f})' for _, g, k, v in gaps),
          flush=True)
    shut = [k for ok, _, k, _ in gaps if not ok]
    if shut:
        raise SystemExit(f'✗ {block}: 口岸脚没有出口面 {shut}（四条边都被异网铜挡住，'
                         f'拼板时边界线进不来）')

    # ⑤ 归一化 —— 先写**暂存**，全部门过完才落成 `<块>.kicad_pcb`。
    #   10-29 mcu_stm32f103 实测：块目录模式重跑出 未连通 1，而门里只有"口岸没出口面"
    #   "丢网"这类才 raise，未连通只体现在**返回码**上 → 不绿的板直接把手里那块绿板
    #   冲掉了。绿板是几十轮试出来的，不能被一次重跑换掉。
    pcb = work / f'{block}_norm.kicad_pcb'
    _run([kpy(), '-c', NORMALIZE, str(final), str(pcb)], pcbnew_quirk=True)
    # 布线期的假网 `__NC__` 摘掉焊盘之后还剩一行网声明，文本层删掉（不留幽灵网）
    txt = pcb.read_text(encoding='utf-8')
    txt = re.sub(r'[ \t]*\(net \d+ "__NC_[^"]*"\)\n', '', txt)
    if '__NC_' in txt:
        raise SystemExit(f'✗ {block}: 产物里还残留 __NC_（摘网失败）')
    pcb.write_text(txt, encoding='utf-8')
    print(f'   DRC {len(kinds)} 条 / 未连通 {unconn} / 铜级 {len(copper)}：'
          f'{", ".join(sorted(set(kinds))) or "—"}', flush=True)

    # ⑤' 电气真值：逐脚对网表（丢了网就是断的，别的门看不见）
    ng = net_gate(pcb, bdir / f'{block}.kicad_sch')
    print(f'   对网表 {ng["want"]} 脚：丢网 {len(ng["lost"])} / 网名不符 '
          f'{len(ng["wrong"])}', flush=True)
    if ng['lost'] or ng['wrong']:
        raise SystemExit(f'✗ {block}: 丢网 {ng["lost"][:6]} 网名不符 {ng["wrong"][:6]}')

    # ⑤'' 时钟规则量法（用户规则："晶体紧贴时钟脚、不许打孔"）。
    # 光写文档的规则会漂（R3 只在 docstring 里 → 晶体被摆到 7.2mm 外、绕 3 孔）。
    cg = clock_gate(pcb)
    if cg['clock_nets']:
        gap_mm = cg.get('gap')
        # 2026-10-30 起是**区间**不是上限：太远时钟线要绕（上限），太近芯片那一面的
        # 脚就再也引不出线（下限）——用户原话"同样的面积下 Y1 应该离芯片远一点，
        # 因为以后芯片还可能要引出线来"。
        ok_gap = (gap_mm is not None
                  and CLOCK_MIN_MM <= gap_mm <= CLOCK_GAP_MM)
        print(f'   晶体↔{cg["hub"]} 外框间隙 {gap_mm}mm'
              f'（区间 {CLOCK_MIN_MM}~{CLOCK_GAP_MM}mm）'
              + ('' if ok_gap else '  ✗'), flush=True)
        bad = [] if ok_gap else ['gap']
        for n, v in sorted(cg['clock_nets'].items()):
            ok = v['vias'] == 0
            print(f'   时钟网 {n:<16} 焊盘→脚 {v["mm"]}mm（参照） / {v["vias"]} 孔'
                  + ('' if ok else '  ✗'), flush=True)
            if not ok:
                bad.append(n)
        if bad:
            raise SystemExit(f'✗ {block}: 时钟规则不过 {bad}'
                             f'（要求 外框间隙 ≤{CLOCK_GAP_MM}mm 且时钟网 0 孔）')

    # ⑤''' 散热盘体检（量最终板，不量中间产物）：同网大面积盘里必须落够同网过孔，
    # 否则四层板的内层平面铺了白铺。打孔由 route_rl/fanout 在布线后落铜（见 ③''''）。
    tc = _run([kpy(), '-c', THERMAL_CHECK, str(pcb),
               ','.join(info.get('rails') or []),
               str(THERMAL_PAD_MM2), str(THERMAL_FLOOR)], pcbnew_quirk=True)
    tl = [l for l in tc.stdout.splitlines() if l.startswith('THERMALCHK ')]
    thermal = json.loads(tl[0][len('THERMALCHK '):]) if tl else []
    if thermal:
        print('   散热盘：' + '  '.join(
            f'{t["pad"]}({t["net"]}) {t["area"]}mm² → {t["vias"]} 孔'
            f'（下限 {t["floor"]}/装得下 {t["cap"]}）' for t in thermal), flush=True)
        low = [f'{t["pad"]} {t["vias"]}<{t["floor"]}' for t in thermal
               if t['vias'] < t['floor']]
        if low:
            raise SystemExit(f'✗ {block}: 散热盘过孔不够 {low}'
                             f'（同网大面积盘必须打孔接内层平面，否则四层板的内层铺了白铺）')
    else:
        print('   散热盘：无（没有 >= '
              f'{THERMAL_PAD_MM2}mm² 的平面网焊盘）', flush=True)

    # ⑥ 贴边体检：接插件包络必须挨着板边（用户要求"接插件在边缘"）。
    #    `edge_free` 里点名的接插件豁免——那是设计选择（比如排线随便落板内），不是放过。
    edge = edge_report(pcb)
    free = set(info.get('edge_free') or [])
    cons = [c for c in edge['conn'] if c['ref'][:1] == 'J']
    far = [c for c in cons if c['gap'] > EDGE_TOL and c['ref'] not in free]
    print(f'   板 {edge["w"]}×{edge["h"]} 接插件 '
          + (', '.join(f'{c["ref"]}@{c["edge"]} 边 {c["gap"]}mm'
                       + ('(免贴边)' if c['ref'] in free else '') for c in cons) or '—')
          + (f'  ⚠ 没贴边：{[c["ref"] for c in far]}' if far else ''), flush=True)

    # ⑥' 落盘：绿了才覆盖已有板（见 ⑤ 的注释）。目录里本来没板就照写，但要说清楚。
    tgt = bdir / f'{block}.kicad_pcb'
    green = unconn == 0 and not copper and not far
    wrote = green or not tgt.exists()
    if wrote:
        shutil.copyfile(pcb, tgt)
        if not green:
            print(f'   ! 不绿（未连通 {unconn} / 铜级 {len(copper)} / 没贴边 {len(far)}）'
                  f'但目录里本来没有板 → 先写出去，留待再跑', flush=True)
    else:
        # `_geom/`（= work）每次构建开头整个 rmtree（见本函数开头的
        # `if work.exists(): shutil.rmtree(work)`），所以**不绿的暂存板留在那儿，
        # 下一次重跑就没了**——2026-10-30 实测：好容易布出来的一块「未连通 2」
        # 板，启动重跑那一刻被清掉，连复盘都做不了。故不绿时另存到块目录下的
        # `_staged/`（在 rmtree 范围之外），文件名自带时间戳和指标。
        keep = bdir / '_staged' / (
            f'{pcb.stem}_{time.strftime("%m%d-%H%M")}'
            f'_un{unconn}_cu{len(copper)}_far{len(far)}.kicad_pcb')
        keep.parent.mkdir(exist_ok=True)
        shutil.copyfile(pcb, keep)
        print(f'   ⚠ 这次不绿（未连通 {unconn} / 铜级 {len(copper)} / 没贴边 {len(far)}），'
              f'保留目录里原有的 {tgt.name} 不动', flush=True)
        print(f'     这块板另存：{keep}', flush=True)
        print(f'     （暂存板 {pcb} 所在的 _geom/ 下次构建会被整个清空）', flush=True)

    # 回填 geometry_status + 重出块卡（2026-10-28 块卡合并）
    # `geometry_status` 是**量**出来的（只有摆完线、DRC 跑完才知道），所以落
    # `_geom/measured.json`，再让 `card.emit` 重出 `block.sdi.yaml` + 两份 json。
    # 以前这里直接改 `block.semantic.json`：那份现在是**产物**，改它等于改完就被下次
    # emit 冲掉——两条写路径迟早漂。
    from . import card as _card
    if not wrote:       # 板没换（见 ⑥'），卡/量测也必须原地不动，否则两者对不上
        print(f'   （板没换 → 块卡与 _geom/measured.json 不动）', flush=True)
        return {'block': block, 'w': size['w'], 'h': size['h'],
                'copper': len(copper), 'drc': len(kinds), 'unconn': unconn,
                'far': len(far), 'sealed': len(sealed),
                'parts': len(info['parts']), 'nets': len(info['nets'])}
    mp = bdir / _card.MEASURED
    measured = json.loads(mp.read_text(encoding='utf-8')) if mp.is_file() else {'block': block}
    status = (f'extracted {size["w"]}×{size["h"]}mm 4L '
              f'copper={len(copper)} kicad 10.0.5')
    measured['geometry_status'] = status
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if card_mode:
        # 块目录模式：`block.sdi.yaml` 是**手写**权威，`write_card` 会把它整篇从两份
        # json 重写出来（字段顺序/注释/历史写法全按产物口径归一）。量出来的只有
        # `geometry_status` 一行，就只换这一行，其余一个字不动。
        cp = bdir / _card.CARD
        txt, nsub = re.subn(r'(?m)^(\s*)geometry_status:.*$',
                            lambda m: f'{m.group(1)}geometry_status: {status}',
                            cp.read_text(encoding='utf-8'))
        if nsub != 1:
            raise SystemExit(f'✗ {block}: 块卡里 geometry_status 行数 {nsub} ≠ 1，没法回填')
        cp.write_text(txt, encoding='utf-8')
    else:
        _card.write_card(bdir)      # json（刚写的）→ yaml；geometry_status 取 measured 那份
    _card.emit(bdir)                # yaml → 两份 json，让产物与 yaml 同一个状态（回读门要这条）

    # 块的根目录只留规格要的文件；工程文件跟着走，方便直接在 KiCad 里打开看
    pro = work / f'{block}_base.kicad_pro'
    if pro.exists():
        shutil.copyfile(pro, bdir / f'{block}.kicad_pro')
    for junk in list(bdir.glob('*.kicad_prl')) + list(work.glob('*.kicad_prl')):
        junk.unlink()
    return {'block': block, 'w': size['w'], 'h': size['h'], 'copper': len(copper),
            'drc': len(kinds), 'unconn': unconn, 'far': len(far),
            'sealed': len(sealed),
            'parts': len(info['parts']), 'nets': len(info['nets'])}


def build_from_card(bdir) -> dict:
    """块目录模式：给 `DesignBlocks/<块>/` 出板级几何。

    和裁块路径的差别只在**五件套从哪来**：块目录里的 `block.sdi.yaml` 是**手写**权威，
    两份 json 是它的产物。再跑一遍 `write_bundle` 会①把手写字段整篇重写、②把块的本地
    标签改成层级标签——图元一变，"完全截取"门（`sdi.renderdiff`）立刻不过。所以这里
    只**读**卡，把卡的形状翻成 `read_sch` 那份 `info`，其余五步一个字不改。
    """
    from . import card as _card
    bdir = Path(bdir)
    c = _card.load(bdir)
    block = c.name
    d = c.d
    sch = bdir / f'{block}.kicad_sch'
    if not sch.is_file():
        raise SystemExit(f'✗ {block}: 块目录里没有 {sch.name}')
    nets: dict[str, list] = {}
    for x in d['conns']:
        nets.setdefault(x['net'], []).append({'ref': x['ref'], 'pin': str(x['pin'])})
    info = {'parts': c.table['parts'],      # ref/lib/value/footprint + 原理图坐标
            'nets': nets,
            # 端口在语言层叫接口名、在连线表里叫网名；口岸体检量的是**网**
            'ports': [p.get('net') or p['name'] for p in d['ports']],
            'rails': list(d.get('rails') or []),
            # 卡里点名「不必贴板边」的接插件（用户按用途定，不是算出来的）
            'edge_free': list(d.get('edge_free') or [])}
    return build_one(block, sch, bdir, dict(d.get('provenance') or {}),
                     info=info, plane=main_rail(info['rails']), card_mode=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='裁出来的块 → KiCad DesignBlocks 规格（含板级几何）')
    ap.add_argument('--blocks', default='',
                    help='逗号分隔块名（默认：carve 目录下全部四块）')
    ap.add_argument('--carve', default=str(CARVE), help='裁块产物目录')
    ap.add_argument('--block-dir', default='',
                    help='逗号分隔的块目录（DesignBlocks/<块>）：块卡模式，'
                         '块卡与原理图原样不动，只出板级几何')
    ap.add_argument('--gap', type=float, default=None,
                    help='摆件的件间净距 mm（默认走 place_rules 的 0.45；布线吃紧就放大）')
    ap.add_argument('--mp', type=int, default=60, help='FreeRouting 最大 passes（默认 60）')
    ap.add_argument('--edge-mm', type=float, default=None,
                    help='板边余量 mm（默认 0.6；密块用 1.5 换一圈能落过孔的走廊）')
    args = ap.parse_args(argv)

    global PLACE_GAP, ROUTE_MP, EDGE_MARGIN
    PLACE_GAP, ROUTE_MP = args.gap, args.mp
    if args.edge_mm is not None:
        EDGE_MARGIN = args.edge_mm

    if args.block_dir:
        dirs = [p for p in args.block_dir.split(',') if p]
        rows = [build_from_card(Path(p).expanduser().resolve()) for p in dirs]
        print('\n块 | 尺寸 | 件 | 网 | 未连通 | 铜级 DRC | 封死脚 | 全部 DRC | 接插件没贴边')
        for r in rows:
            print(f'{r["block"]:<16} {r["w"]:>6.2f}×{r["h"]:<6.2f} {r["parts"]:>3}'
                  f' {r["nets"]:>3} {r["unconn"]:>7} {r["copper"]:>9} {r["sealed"]:>7}'
                  f' {r["drc"]:>9} {r["far"]:>10}')
        return 0 if all(r['copper'] == 0 and r['unconn'] == 0 and r['far'] == 0
                        for r in rows) else 1

    carve = Path(args.carve)
    # 2026-10-29 清理：`out/`（裁块暂存）已从库里删掉——正式块在 `DesignBlocks/`。
    # 原先这里会 mkdir 出一个空目录，然后在"找不到源板"的地方莫名其妙地失败；
    # 现在直接说清楚该走哪条路。
    if not carve.is_dir():
        raise SystemExit(
            f'✗ 裁块暂存目录不存在：{carve}\n'
            f'  主流用法（块目录模式）：--block-dir ../DesignBlocks/<块名>\n'
            f'  确实要重走裁块流程：先用 `sdi carve` 把块产出到 {carve}')
    names = ([b for b in args.blocks.split(',') if b]
             or ['mcu_stm32f103', 'usb_dev_micro', 'ldo_ap2112k_3v3', 'rs485_sp3485'])
    libdir = carve / f'{LIBNAME}.kicad_blocks'
    libdir.mkdir(parents=True, exist_ok=True)

    src_board = 'KiCAD_Projects/STM32F103_USB/STM32F103_USB_v1.kicad_sch'
    prov = {'source': 'human:user-project',
            'basis': f'从用户项目板 {src_board} 裁出的块（块内坐标一字未动）',
            'board': 'STM32F103_USB_v1',
            'license': 'internal-only —— 用户项目板，不进公开发行包（块规范模版 §六 D 层）',
            'author': 'yaoyuan',
            'date': '2026-10-27', 'kicad': '10.0.5'}

    rows = []
    for b in names:
        sch = carve / b / f'{b}.kicad_sch'
        if not sch.exists():
            print(f'✗ 找不到 {sch}')
            return 1
        rows.append(build_one(b, sch, libdir / f'{b}.kicad_block', prov))

    print('\n块 | 尺寸 | 件 | 网 | 未连通 | 铜级 DRC | 封死脚 | 全部 DRC | 接插件没贴边')
    for r in rows:
        print(f'{r["block"]:<16} {r["w"]:>6.2f}×{r["h"]:<6.2f} {r["parts"]:>3} {r["nets"]:>3}'
              f' {r["unconn"]:>7} {r["copper"]:>9} {r["sealed"]:>7}'
              f' {r["drc"]:>9} {r["far"]:>10}')
    return 0 if all(r['copper'] == 0 and r['unconn'] == 0 and r['far'] == 0
                    for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
