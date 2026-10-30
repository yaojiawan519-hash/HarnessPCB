#!/usr/bin/env python3
"""块卡 ↔ 块自己的 PCB：卡片说的电路，板上真的是那样吗。

这张门补的是**第三条腿**。`sdi/carve`（原理图）与 `sdi/pcbcarve`（板）各自有门，
`card.py` 的回读门只证明"yaml 能重算出两份 json"——**三者都没验"卡片和板是不是同一块电路"**。
10-30 实测过一次真错：usb_typec_if 的 VBUS 端口出口写成 `J1-A4_B9`（那是二极管**之前**
的连接器侧焊盘，属于块内网 `Net-(D6-Pad2)`），而 VBUS 网在块内只有 `D6-1`。
"出口脚存在"是成立的，**"出口脚在这根网上"才成立**——所以这道门按网的归属判，不按焊盘存在判。

查四件事（每条都打印数）：
  ① conns 逐脚同网：把卡片和板子各自按"哪些脚连在一起"分组，两组集合必须**相等**。
     比分组不比网名——网名是产品，分组才是电路事实（同 `pcbcarve` 那条"电路一致性"门）。
  ② 端口出口脚落网：`ports[].exit.pin` 那个焊盘必须**真的在** `ports[].net` 这根网上。
  ③ rails ⊆ conns 的网名：rail 写在卡里却不在连线里 = 幽灵网，供给/丢弃逻辑会漏。
  ④ 器件表对齐：卡里的 ref 集合 == 板上的位号集合（少一件 = 卡过期）。

用法：
    python3 -m sdi.cardpcb ../DesignBlocks            # 扫所有带 .kicad_pcb 的块
    python3 -m sdi.cardpcb ../DesignBlocks/usb_typec_if

⚠ 需要 pcbnew（只在 KiCad 自带的 python 里），而那个 python **没有 yaml**。
   所以本脚本用能读 yaml 的解释器跑，把"读板"这一步**甩给 KiCad python 的子进程**，
   它只吐一个 JSON：{groups: 网→[ref.pin], pads: ref→[pin], parts: [ref]}。
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

CARD = 'block.sdi.yaml'

# KiCad 装在外挂盘上，不在 PATH 里（见记忆 kicad-pcbnew-placement-api）
KICAD_PY = [
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3',
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9',
    '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3',
]

DUMP = r'''
import wx; _app = wx.App(False)
import pcbnew, json, sys, collections
b = pcbnew.LoadBoard(sys.argv[1])
grp = collections.defaultdict(list)
pads = {}
parts = []
for fp in b.GetFootprints():
    r = fp.GetReference(); parts.append(r); pads[r] = []
    for p in fp.Pads():
        pads[r].append(p.GetNumber())
        n = p.GetNetname()
        if n and not n.startswith('unconnected-'):   # NC 脚的单脚网不是电路事实
            grp[n].append('%s.%s' % (r, p.GetNumber()))
grp = {k: sorted(set(v)) for k, v in grp.items()}
# **空脚不是网**（10-30，同 `sdi.blockpcb.read_sch` 那条规则的另一面）：
# `Net-(U2-Pad2)` 这种名字是 KiCad 给"没人接的焊盘"现编的，不是人写的网名；
# 单脚 + 这种名字 = 空脚。脚数 >1 的照算（那才是真网）。
# 老 KiCad（5/6 版转上来的板，如 STM32F4_Reference）会给每个 NC 焊盘都编一个这样的网，
# 新板则留空字符串——两种表示说的是同一件事，不归一化，卡里就得写一堆假网。
# ⚠ 但"单脚"只说明**块里**只剩这一只脚：块是裁出来的，一根跨块的线在块里本来就只剩
# 一只脚（SIM 板的 `Net-(D1-K)` = J1.SW2 ↔ 块外的 D1.1）。所以不在这里删，
# 交给读卡的那一侧判断（`nc_pads` 只记账，删不删由 `check_block` 定）。
_nc = sorted(k for k, v in grp.items() if k.startswith(('Net-(', 'unconnected-'))
             and len(v) == 1)
json.dump({'groups': grp, 'pads': pads, 'parts': sorted(parts),
           'auto_single': _nc, 'nc_pads': len(_nc)},
          open(sys.argv[2], 'w'))
'''


def _kicad_py() -> str:
    for p in KICAD_PY:
        if os.path.exists(p):
            return p
    raise SystemExit('✗ 找不到 KiCad 自带的 python（pcbnew 只在它里面）。'
                     '改 cardpcb.KICAD_PY 补路径。')


def board_truth(pcb: Path) -> dict:
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        out = f.name
    try:
        r = subprocess.run([_kicad_py(), '-c', DUMP, str(pcb), out],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f'✗ 读板失败：{r.stderr.strip()[-400:]}')
        return json.loads(Path(out).read_text())
    finally:
        Path(out).unlink(missing_ok=True)


def _canon(n: str) -> str:
    """网名的**层级路径前缀**不是电路事实（同 `sdi.blockpcb` 那条规则的镜像）。

    KiCad 把局部网导出到板上时会带上前任图页的路径：原理图里写 `CDONE`，
    板上叫 `/CDONE`；层级设计里还可能是 `/子图/网名`。块卡里从来只写裸名，
    所以比名字之前先把路径剥掉。**只剥路径，不做别的归一化**——
    真正的别名（原理图 `+3.3V` ↔ 板上 `+3V3`）是两个不同的名字，
    ⑤ 该红就得红，剥完还剩差异就说明名字真对不上。

    唯一的第二刀是 **KiCad5 转义**（2026-10-30 SIM 板）：源板文件里那根线存成
    `Net-/I{slash}O`，因为 KiCad 的网名字段里 `/` 会被当路径分隔符，老版本就把它写成
    `{slash}`。这与"路径前缀"同性质——**同一个名字的两种拼法**，不是别名。
    解了几个由调用方打印出来，静默改名就是门在骗人。
    """
    # ⚠ 三条顺序上的讲究（2026-10-30 两个坑一次踩完）：
    #  ① 只对**以 `/` 开头**的名字剥路径——KiCad 的层级局部网一律长这样（`/CDONE`、
    #     `/子图/名`），而`I/O` 这种名字里的斜杠是名字本身，剥了会变成 `O`。
    #  ② 剥路径要在**原文**上做：源文件里名字内的斜杠永远写成 `{slash}`，不会误切。
    #  ③ 解转义放最后：解出来的 `/` 不能再当分隔符用。
    n = (n or '').strip()
    if n.startswith('/'):
        n = n.lstrip('/').rsplit('/', 1)[-1]
    return _unescape(n)


_ESC = {'slash': '/', 'backslash': '\\', 'dblquote': '"', 'lt': '<', 'gt': '>',
        'bar': '|', 'comma': ',', 'space': ' ', 'tilde': '~'}


def _unescape(s: str) -> str:
    if not s or '{' not in s:
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '{':
            j = s.find('}', i)
            if j > i and s[i + 1:j] in _ESC:
                out.append(_ESC[s[i + 1:j]])
                i = j + 1
                continue
            if j == i + 1 and i + 1 < len(s) and s[i + 1] == '{':
                out.append('{')
                i += 2
                continue
        out.append(c)
        i += 1
    return ''.join(out)


def _auto(n: str) -> bool:
    """KiCad 现编的自动网名（`Net-(D1-Pad2)` / `unconnected-(J2-ID-Pad4)`）= 这脚**没有名字**。"""
    return (n or '').startswith(('Net-(', 'unconnected-'))


def check_block(bdir: Path) -> tuple[bool, str]:
    n = bdir.name
    card_p, pcb_p = bdir / CARD, bdir / f'{n}.kicad_pcb'
    if not card_p.is_file():
        return True, '（无块卡，跳过）'
    if not pcb_p.is_file():
        return True, '（无 .kicad_pcb，跳过）'
    doc = yaml.safe_load(card_p.read_text(encoding='utf-8')) or {}
    d = (doc.get('defs') or {}).get(n)
    if not d:
        return False, f'✗ {CARD} 里没有 defs.{n}'
    t = board_truth(pcb_p)

    # **端口出口脚**：后面三条判据都要它。
    # `expose:` 起名的那类端口，板上根本没有人写的网名（源页上那根线本来就没标签，
    # KiCad 现编成 `Net-(D1-K)`）——所以 ②/⑤ 允许"卡里的名字 vs 板上的自动名"这一种不等，
    # 但必须打印出来。反过来，一块**单脚自动名网**只要不属于任何端口出口，就是真空脚。
    port_exit = {}
    for p in d.get('ports') or []:
        ex = (p.get('exit') or {}).get('pin')
        if ex and '-' in ex:
            r, pad = ex.rsplit('-', 1)
            port_exit[f'{r}.{pad}'] = p
    nc_pads = 0
    for k in list(t.get('auto_single') or []):
        v = t['groups'].get(k) or []
        if len(v) == 1 and v[0] not in port_exit:
            del t['groups'][k]
            nc_pads += 1
    auto_port = sorted(f"{port_exit[v[0]]['name']}→{k}"
                       for k, v in t['groups'].items()
                       if _auto(k) and len(v) == 1 and v[0] in port_exit)

    # 板上网名先剥层级路径（见 _canon）。剥了几个必须打出来——静默改名就是门在骗人。
    # 剥完撞名说明两根不同的网共用一个叶子名，那是块边界或网表真出了问题，不许糊过去。
    leaf = collections.defaultdict(set)
    for k in t['groups']:
        leaf[_canon(k)].add(k)
    clash = {k: sorted(v) for k, v in leaf.items() if len(v) > 1}
    if clash:
        return False, (f'✗ 板上几根网剥掉路径后同名，分不清谁是谁：{clash}'
                       '（要么它们真该合并，要么块边界划错了）')
    canon_hits = sorted(k for k in t['groups'] if _canon(k) != k)

    bad = []
    # ① conns 逐脚同网（比分组，不比网名）
    cg = collections.defaultdict(list)
    for c in d.get('conns') or []:
        cg[c['net']].append('%s.%s' % (c['ref'], c['pin']))
    card_pins = {(c['ref'], str(c['pin'])) for c in d.get('conns') or []}
    conn_refs = {c['ref'] for c in d.get('conns') or []}
    # **封装自带、卡里根本没有的焊盘**：FT232H 的散热盘（`usb_ftdi_bridge` 的 U3.49 +
    # 一串空号子焊盘）、USB 座的屏蔽壳只在这一类里。原理图上没有这只脚，卡永远不会写它；
    # 它跟谁同网是**几何**（源板靠整板铺铜连），不是电路。混进分组比 ① 就永远是红的。
    # 只剔"器件在卡里、这只脚卡里没提过"的——整个器件都不在卡里是另一回事，不剔。
    fp_only_pads = {'%s.%s' % (r, p) for r, ps in (t.get('pads') or {}).items()
                    if r in conn_refs for p in ps if (r, str(p)) not in card_pins}
    pcb_g = {frozenset({x for x in v if x not in fp_only_pads}) for v in t['groups'].values()}
    pcb_g.discard(frozenset())
    # **封装里没有同号焊盘的符号脚**：板上根本没有这只脚，拿它比"谁和谁同网"就是拿
    # 一样板上不存在的东西比。这是**源板自身**的缺陷（不是裁块裁出来的）：DDR5 UDIMM
    # 的符号有 `SH` 屏蔽壳脚接 GND，而封装 `DDR5 UDIMM GENERIC` 里对应的是三只**无名
    # 焊盘** `S1/S2/S3`（NetCode=0），板上没有 `SH`。与 `sdi.pcbcarve` 的 sch↔pcb 门
    # 同一条规则——两边口径必须一致，否则同一个缺陷一处放行一处报红。
    have = {(r, str(p)) for r, ps in (t.get('pads') or {}).items() for p in ps}
    lost = set()
    cg2 = collections.defaultdict(list)
    for net, pins in cg.items():
        for rp in pins:
            r, _, p = rp.partition('.')
            if (r, p) in have:
                cg2[net].append(rp)
            else:
                lost.add(rp)
    cg = cg2
    # **卡上的空脚不是网**——这是板上 `auto_single` 那条规则的另一侧，两边必须同口径。
    # 原理图上"只接了它自己一只脚"的无名线，carve 会给它起本地名 `_<块>_N<k>`；板上对应
    # 焊盘是 NetCode 0，那张网根本不存在。不去掉，① 就永远把它算成"卡里多"：
    # 2026-10-30 的 usb_power（J1.8/11）、imu_mpu6050（U3.3/6）就这么红的；
    # mcu_stm32f411 那 21 只空脚同因（当时是手工从卡里删的，重跑 block_sch 会回来）。
    # 只认"单脚 + 本块自动名 + 不是端口出口"；脚数 >1 的照算（那才是真网）。
    # **`NC` 也算**（2026-10-30）：元件手册里写着 NC 的脚（AMS1117 的 SOT-23-5 第 4 脚、
    # micro-B 座的 ID 脚）在卡里被写成一根单脚网 `NC`，板上就是"无网焊盘"——同一件事的
    # 两种写法。只放单脚、且不是端口出口的那一种；`NC` 上挂两只以上照样当网比。
    card_nc = 0
    for net in list(cg):
        pins = cg[net]
        if (len(pins) == 1 and pins[0] not in port_exit
                and (net.startswith(f'_{n}_N') or net == 'NC')):
            del cg[net]
            card_nc += 1
    card_g = {frozenset(v) for v in cg.values() if v}
    if pcb_g != card_g:
        # 报错要报**差在哪几脚**。原先两边各打 `sorted(组)[:3]`，两组的头三只恰好都是
        # 公共脚 → 打出"板上多 [['C1.2', …]] / 卡里多 [['C1.2', …]]"这种两边一模一样的话
        # （2026-10-30 `usb_ftdi_bridge` 就这么把人看懵）。改成先找卡上最像的那一组，再算差集。
        card_by_set = {frozenset(v): net for net, v in cg.items() if v}

        def _near(s, pool):
            return max(pool, key=lambda c: len(c & s)) if pool else frozenset()

        parts = []
        for p in sorted(pcb_g - card_g, key=lambda s: -len(s))[:2]:
            c = _near(p, card_g)
            parts.append('%s：板上多 %s / 板上缺 %s' % (
                card_by_set.get(c, '?'), sorted(p - c)[:6], sorted(c - p)[:6]))
        for c in sorted(card_g - pcb_g, key=lambda s: -len(s))[:2]:
            p = _near(c, pcb_g)
            parts.append('%s：卡里多 %s / 卡里缺 %s（与板上最像的一组比）' % (
                card_by_set.get(c, '?'), sorted(c - p)[:6], sorted(p - c)[:6]))
        bad.append('① 分组不一致：' + '；'.join(parts))

    # ② 端口出口脚必须落在这根网上
    net_of_pad = {}
    for net, refs in t['groups'].items():
        for rp in refs:
            net_of_pad[rp] = net
    miss = []
    for p in d.get('ports') or []:
        ex = (p.get('exit') or {}).get('pin')
        if not ex or '-' not in ex:
            continue
        net = p.get('net') or p['name']
        ref, pad = ex.rsplit('-', 1)
        got = net_of_pad.get(f'{ref}.{pad}')
        # 板上是**自动名**（源页上这根线本来就没标签）= 板上没有名字，卡里的名字就是权威
        if _auto(got):
            continue
        if _canon(got) != _canon(net):
            miss.append(f'{p["name"]}: 出口 {ex} 在网 {got}，卡里写的是 {net}')
    if miss:
        bad.append('② 出口脚落错网：' + '；'.join(miss))

    # ③ rails ⊆ conns
    nets = {c['net'] for c in d.get('conns') or []}
    ghost = [r for r in d.get('rails') or [] if r not in nets]
    if ghost:
        bad.append(f'③ rail 不在连线里（幽灵网）：{ghost}')

    # ④ 器件表对齐
    # **未上板的件**：源板自己就没有这份封装（两只 `R404` 重号，板只留了电流限制那只，
    # 见 `sdi.pcbcarve.member_set`）。它在卡里、不在板上是**事实**，④ 不许当"卡里多"。
    # 名单由 pcbcarve 落在 `placement.json` 的 `counts.not_built`，不在这里凭记忆写。
    nj = Path(pcb_p).parent / f'{n}.placement.json'
    not_built = set()
    if nj.is_file():
        not_built = set((json.loads(nj.read_text(encoding='utf-8')).get('counts') or {})
                        .get('not_built') or [])
    card_refs = {p['ref'] for p in d.get('parts') or []}
    extra = card_refs - set(t['parts'])
    if extra != not_built or set(t['parts']) - card_refs:
        bad.append(f'④ 器件表不一致：卡里多 {sorted(extra - not_built)} '
                   f'/ 板上多 {sorted(set(t["parts"]) - card_refs)}'
                   + (f'（未上板 {sorted(not_built)} 已放行）' if not_built else ''))

    # ⑤ 轨/端口的**网名**必须是板上真有的名字（10-30 新增）
    #   ① 只比"谁和谁同网"，网名换了个写法照样绿——mcu_stm32f405 就是这么溜过去的：
    #   卡里写 `+3.3V`（原理图电源符号的名字），板上叫 `+3V3`（作者布板时用的名字）。
    #   拼板时是按名字接网的，名字对不上，两根轨就当两条网，电源根本接不上。
    #   这条门专门查名字（⑤ 与 ② 分工：② 查"出口脚在不在那根网上"，⑤ 查"这个名字板上有没有"）。
    board_names = set(leaf)
    alias = [r for r in (d.get('rails') or []) if r not in board_names]
    port_alias = []
    for p in d.get('ports') or []:
        nm = p.get('net') or p['name']
        if nm in board_names:
            continue
        # 出口脚落在**自动名**网上：板上没有名字（源页这根线本来就没标签，
        # 名字是 `carve` 的 `expose:` 现起的）。这一条与 ② 同一个口径。
        ex = (p.get('exit') or {}).get('pin')
        got = None
        if ex and '-' in ex:
            r, pad = ex.rsplit('-', 1)
            got = net_of_pad.get(f'{r}.{pad}')
        if _auto(got):
            continue
        port_alias.append(f'{p["name"]}→{nm}')
    if alias:
        bad.append(f'⑤ 轨名板上没有：{alias}（板上 3V3 类网名有 '
                   f'{sorted(n for n in board_names if "3" in n)}）')
    if port_alias:
        bad.append(f'⑤ 端口网名板上没有：{port_alias}')

    summary = (f'{len(pcb_g)} 组 / 端口 {len(d.get("ports") or [])} / '
               f'件 {len(card_refs)} / 网 {len(nets)}'
               + (f' / 网名去路径 {len(canon_hits)}'
                  f'（如 {canon_hits[0]}）' if canon_hits else '')
               + (f' / 未上板符号脚 {sorted(lost)}（封装里没有同号焊盘，不参与比较）'
                  if lost else '')
               + (f' / 未上板的件 {sorted(not_built)}（板上这位号是另一只件或压根没有，'
                  f'卡里有、板上没有）' if not_built else '')
               + (f' / 板上无名字的端口 {auto_port}（源页那根线本来没标签，名字来自 carve 的 expose）'
                  if auto_port else '')
               + (f' / 板上空脚 {nc_pads}' if nc_pads else '')
               + (f' / 卡里没有的焊盘 {len(fp_only_pads)}（封装自带：散热盘/屏蔽壳/无名子盘，'
                  f'跟谁同网是几何不是电路，不参与比较）' if fp_only_pads else '')
               + (f' / 卡上空脚 {card_nc}（无名单脚网或 NC，不是电路）' if card_nc else ''))
    return (not bad), (summary if not bad else summary + '\n      ' + '\n      '.join(bad))


def blocks(path) -> list[Path]:
    p = Path(path)
    if (p / CARD).is_file() or (p / f'{p.name}.kicad_pcb').is_file():
        return [p]
    return sorted(x for x in p.iterdir() if x.is_dir() and not x.name.startswith('_'))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for b in blocks(sys.argv[1]):
        ok, msg = check_block(b)
        print(('  ✓ ' if ok else '  ✗ ') + b.name + '：' + msg)
        bad += 0 if ok else 1
    print(f'{"✓" if not bad else "✗"} 块卡↔PCB 门：{bad} 不过')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
