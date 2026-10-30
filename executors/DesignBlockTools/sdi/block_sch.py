#!/usr/bin/env python3
"""**只交原理图**的块：裁块产物 → KiCad DesignBlock（块卡 + 两份 json），不出板。

为什么另开一条路：`sdi blockpcb` 是一件事干到底的（空板 → 摆件 → 收边 → 布线 →
DRC → 归一化），入口要的是"已经能上板"的块。从第三方页裁出来的块常常只想要电路
——用户原话"直接生成新的 yaml 文件"。这条路只做电路那半截，板上那半截交白卷
（`geometry_status` 如实写"只交了原理图"），免得拿一份 `pending` 冒充量过。

**标签作用域：`label_style` 两种，默认 `keep`**（2026-10-29 用户拍板：块要长得和原图一样）

    keep  块里的标签节点一个字节都不动 → 图和原页那一块完全一致（连通判据在 IL 里，
          不靠标签类型，所以对电路零影响）。代价：块内网是 global_label，同一个块插
          两份当两个子页时内部网会串名——拼装那步按卡里的 `internal_nets` 逐实例改名
          （`layout_reuse.py` 本来就这么做），不走手工插子页就没问题。
    hier  出块网 → hierarchical_label、块内网 → label。作用域最干净，但块内标签丢掉了
          原来的框（global_label 画成一个带箭头的框，本地 label 是白文），一眼能看出
          和原图不一样——所以它不再是默认。

**端口和电源轨是算出来的，不是猜的**：

    出块网 = 整页 IL 里这张网的成员 ⊋ 块内 IL 里同一张网的成员

也就是"还连着块外"。这条判据同时替掉两条以前靠标签猜的错法：
`read_sch` 把页上**所有**标签当端口（内部网 NRST/SWCLK 一起算进去），把
**所有**电源符号当轨（块内自造的 +3.3VA 也算，它其实是块内 L2 造的，不是外部供给）。

门：
  ① 写出来的 `.kicad_sch` **逐 pin 分区一致**——拿 kicad-cli 的网表当第二套实现，
     不看自家的 extract（共用一套代码就等于自证）
  ② 块卡回读（`card.roundtrip`：yaml 重算出来的两份 json == 磁盘上那两份）

用法：
    python3 -m sdi.block_sch specs/block_mcu_stm32f405.yaml
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from . import blockpcb, card

ROOT = Path(__file__).resolve().parent.parent          # DesignBlockTools
ILTOOLS = ROOT.parent / 'SchematicDesign' / 'il' / 'tools'


def _extract():
    if str(ILTOOLS) not in sys.path:
        sys.path.insert(0, str(ILTOOLS))
    import extract                                     # noqa: PLC0415
    return extract


def net_map(il: dict) -> dict:
    """IL 的 nets → {网名: {pins: {(ref, pin)}, power: bool}}。

    无名网归到 `None` 一个桶里（块内部线，不参与出块判定）。
    """
    out: dict = {}
    for n in il['nets']:
        d = out.setdefault(n.get('name'), {'pins': set(), 'power': bool(n.get('power'))})
        d['pins'].update((r, p) for r, p in n['pins'])
    return out


def classify(page: dict, blk: dict, external: set | None = None,
             external_power: set | None = None) -> tuple[list, list, list, dict]:
    """(出块信号网, 电源轨, 块内网, 别名表)——只认"还连着块外"这一条判据。

    **网的身份按脚认，不按名字认。** 一张网可以挂好几个名字（KiCad 叫别名）：
    页上 `VOUT`（全局标签）/ `+5V`（电源符号）/ `BULK_VIN`（全局标签）是同一张网，
    kicad-cli 只出一张网、名字取 `BULK_VIN`。块是原页切下来的一块，切到块里
    往往**只剩其中一个名字**（U1 的 VCCB 那只脚旁边只落着 `+5V` 电源符号）。
    旧代码按名字比"还连着块外吗"：块里的 `+5V` 在整页上根本找不到（页上那张网
    已经叫 `BULK_VIN`），于是 outside 算成空 → 把 5V 供电轨误判成**块内网**。
    拼装时块内网要逐实例改名，供电就这么断了；而卡里 rails 少了这条，
    `sdi.cardpcb` 门③/⑤（rail ⊆ 连线 / rail 名板上要有）才会发现。

    所以：块的每张网（脚集合）在页上找**包含**它的那张网，用页上的名字（= 板上
    的名字，卡里 conns 用的就是它）当规范名；块里那个不同的名字记进别名表，
    由调用方把 `info['nets']` 的键改过来。块的脚集合一定是页上某张网的子集
    （切只能把网切断，不能把两张网并起来），所以这个查找是确定的。
    """
    P = []                                             # 页：[(脚集合, 网名, 是否电源)]
    for n in page['nets']:
        # **无名网也要进 P**（2026-10-30 SIM 板补）：页上`Net-(D1-K)` 这种 KiCad 现编的
        # 名字 IL 不记（`n['name']` 是空），于是"块的脚集合 ⊂ 页上某张网"这条查找
        # 直接落空，outside 算成空 → 一根**真的跨块的线**被判成块内网。症状很隐蔽：
        # `carve` 用 `expose:` 给它起了名（门④ 封口过），块卡却把它记进 `internal_nets`，
        # 拼装时逐实例改名，两块之间的这根线就断了。无名网照收，名字留 None，
        # 找不到名字时用块里的名字当规范名（不产生别名）。
        if n['pins']:
            P.append(({(r, p) for r, p in n['pins']}, n.get('name') or None,
                      bool(n.get('power'))))
    exits, rails, internal, alias = [], [], [], {}
    for nm, d in net_map(blk).items():
        if not nm or not d['pins']:
            continue                                   # 无名网是块内部线，不是接口
        host = next((h for h in P if d['pins'] <= h[0]), None)
        canon = (host[1] if host else None) or nm
        if canon != nm:
            alias[nm] = canon
        outside = (host[0] - d['pins']) if host else set()
        # **别页也在用这个名字 ⇒ 这张网在本页之外还接着**（2026-10-30，层级工程）。
        # BP5 主板是"全局标签当总线"的画法（`micro-rp2040` 页 59 个 global_label、
        # 0 个 hierarchical_label）：BUFDIR0 在本页只落在 U103 一只脚上，另一头在
        # `bpu-iobufffer` 页。按"页上还有没有别人"判会把这二十几根总线和十根
        # 单脚层次网全判成块内网，拼装时逐实例改名——线就断了。
        off_page = bool(external) and canon in external
        # 别页把它画成电源符号 ⇒ 这是一条轨（见 `external_globals(with_power=True)`）
        is_power = bool(d['power'] or (host and host[2])
                        or (external_power and canon in external_power))
        if is_power:
            (rails if (outside or off_page) else internal).append(canon)
        elif outside or off_page:
            exits.append(canon)
        else:
            internal.append(canon)
    return sorted(set(exits)), sorted(set(rails)), sorted(set(internal)), alias


GLOBAL = re.compile(r'\(global_label "([^"]+)"( \(shape \w+\))?')
OTHER = re.compile(r'\((?:hierarchical_)?label "([^"]+)"')


def restyle(text: str, exits: list, internal: list) -> tuple[str, dict]:
    """global_label → 出块=层级标签 / 块内=本地标签。只改节点头，坐标字号对齐不动。"""
    # 先在**原文**上找非 global 的标签节点：替换之后新写进去的 `(label "X"` /
    # `(hierarchical_label "X"` 自己也会被这条正则匹配到，等于自己告自己。
    left = {m.group(1) for m in OTHER.finditer(text)}
    seen: dict = {}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
        if name in exits:
            # 方向一律 bidirectional：原页作者给每个标签都写 `(shape input)`，
            # 那是他画图的习惯，不是电路事实（USART3_TX 明显是往外发的）。
            # 断言一个错的方向比不断言更糟。
            return f'(hierarchical_label "{name}" (shape bidirectional)'
        if name in internal:
            return f'(label "{name}"'                # 本地标签不带 shape
        raise SystemExit(f'✗ 标签 {name} 既不是出块网也不是块内网（分类漏了它）')

    text = GLOBAL.sub(sub, text)
    for n in exits:
        if n not in seen:
            print(f'  ! 出块网 {n} 在块里没有标签节点（靠电源符号出块？）', flush=True)
    bad = left & (set(exits) | set(internal))
    if bad:
        raise SystemExit(f'✗ 这些名字还有非 global 的标签节点，改一种会漏一种：{sorted(bad)}')
    return text, seen


GLOBAL_LBL = re.compile(r'\(global_label "((?:[^"\\]|\\.)*)"')


def external_globals(page: Path, with_power: bool = False):
    """同目录**别的页**里出现过的全局标签名 + 电源符号名（= 这张网在本页之外还接着）。

    只扫同目录的 `*.kicad_sch`（KiCad 一个工程一个目录），不认层级：BP5 主板是
    "全局标签当总线"的画法，`micro-rp2040` 页 0 个 hierarchical_label、59 个
    global_label，跨页全靠名字。名字在这一页只有一只脚、在另一页也有——那它就是接口。

    **电源符号名同属这一类**（2026-10-30 补）：`+VUSB` 在 KiCad 里跨页按名字直连，
    跟全局标签一个性质，但它不是 `global_label` 节点，只扫标签会整类漏掉。
    实测 `bp5_current_sense`：`+VUSB` 在 `bpu-current_limiter` 页只落在本块那 5 只脚上
    （页内没别人用）→ 判成块内网 → 拼装时逐实例改名，USB 5V 就断在块口；
    而它在 `micro-rp2040`/`leds`/`IO-expand-595-mux` 三页都有电源符号。
    判据仍按**名字**：块内自造的轨（RP2040 的 `+1V1`、本板 `+3.3VA`）只在生成它的那页
    出现，照旧算块内网。

    `with_power=True` 时多返回一集合：**其中哪些名字在别页是电源符号**。
    这是给 `classify()` 分的第二刀——同一条轨，不同页可能一张画电源符号、一张画全局标签
    （EtherCAT_CAN_Converter 实测：`mcu` 页 `+3V3`/`GND` 是 `power:+3V3`/`power:GND`，
    `can`/`debug`/`eth` 三页全是 `global_label "+3V3"`）。只看本页的 `power` 标记，
    这三块会把 GND 判成**信号口岸**，`rails:` 空着 → 拼板不给它铺铜扇出，
    `spur` 也不把贴铺铜的短线当附件。名字来源仍是别页的**结构标记**（IL 的 power 位），
    不是拿名字猜。
    """
    names: set = set()
    power: set = set()
    extract = _extract()
    for p in sorted(page.parent.glob('*.kicad_sch')):
        if p == page:
            continue
        try:
            txt = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        names |= {m.group(1) for m in GLOBAL_LBL.finditer(txt)}
        try:
            il = extract.extract(str(p))
        except Exception:                              # noqa: BLE001 —— 老格式页读不了就跳过
            continue
        pw = {n['name'] for n in il['nets'] if n.get('power') and n.get('name')}
        names |= pw
        power |= pw
    return (names, power) if with_power else names


def pins_partition(sch: Path) -> set:
    """kicad-cli 网表 → 引脚分区（一堆 frozenset，不看网名，只看"谁和谁同网"）。"""
    tmp = sch.parent / f'_{sch.stem}.net.xml'
    subprocess.run([blockpcb.kcli(), 'sch', 'export', 'netlist', '--format', 'kicadxml',
                    '--output', str(tmp), str(sch)], check=True, capture_output=True)
    part = set()
    for net in ET.parse(tmp).getroot().findall('./nets/net'):
        pins = {(n.get('ref'), n.get('pin')) for n in net.findall('node')
                if n.get('ref') and not n.get('ref').startswith('#')}
        if pins:
            part.add(frozenset(pins))
    tmp.unlink(missing_ok=True)
    return part


def erc(sch: Path) -> dict:
    rpt = sch.parent / '_erc.rpt'
    subprocess.run([blockpcb.kcli(), 'sch', 'erc', '-o', str(rpt), '--severity-all',
                    '--format', 'report', str(sch)], capture_output=True, text=True)
    kinds = re.findall(r'^\[([a-z_]+)\]', rpt.read_text(encoding='utf-8'), re.M) \
        if rpt.exists() else []
    rpt.unlink(missing_ok=True)
    counts = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    return counts


def _resolve(p) -> Path:
    """规范里的路径：绝对路径 / 当前目录 / 工具树 / 仓库根，按序找。返回原值找不到时。"""
    q = Path(p)
    if q.is_absolute():
        return q
    for base in (Path.cwd(), ROOT, ROOT.parent):
        if (base / q).exists():
            return base / q
    return q


def pack(spec_path: Path) -> dict:
    sp = yaml.safe_load(spec_path.read_text(encoding='utf-8'))
    block = sp['block']
    page = _resolve(sp['page'])
    carve = _resolve(sp['carve'])
    src = carve / block / f'{block}.kicad_sch'
    if not src.is_file():
        raise SystemExit(f'✗ 找不到裁块产物 {src}（先跑 sdi.carve）')

    # 块卡和块住在同一个目录里（2026-10-29：不再套 `<lib>/<块>.kicad_block/`）
    bdir = carve / block
    bdir.mkdir(parents=True, exist_ok=True)

    extract = _extract()
    ext, ext_pwr = external_globals(page, with_power=True)
    exits, rails, internal, alias = classify(extract.extract(str(page)),
                                             extract.extract(str(src)), ext, ext_pwr)
    print(f'{block}: 出块 {exits}', flush=True)
    if alias:
        # 别名网：块里那个标签名只是这张网的一个名字，卡里统一用板上的名字
        print(f'{" " * len(block)}  网名别名 ' + ', '.join(
            f'{a} → {c}' for a, c in sorted(alias.items())), flush=True)
    print(f'{" " * len(block)}  电源轨 {rails} / 块内网 {internal}', flush=True)

    # ① 标签样式：默认原样搬（图与原页那一块一致），`label_style: hier` 才改作用域
    style = str(sp.get('label_style') or 'keep')
    src_text = src.read_text(encoding='utf-8')
    if style == 'keep':
        text, seen = src_text, {}
    elif style == 'hier':
        text, seen = restyle(src_text, exits, internal)
    else:
        raise SystemExit(f'✗ label_style 只能是 keep / hier，规范里写的是 {style!r}')
    out = bdir / f'{block}.kicad_sch'
    out.write_text(text, encoding='utf-8')
    print(f'   标签样式 {style}：' + ('一个字不动（图=原页那一块）' if style == 'keep'
                                     else f'改 {sum(seen.values())} 个节点'), flush=True)

    # 门① 逐 pin 分区：搬过来（或改作用域）不该动电路
    a, b = pins_partition(src), pins_partition(out)
    print(f'   门① 分区 {len(a)}/{len(b)} 网，只在一侧 {len(a - b)}/{len(b - a)}', flush=True)
    if a != b:
        raise SystemExit(f'✗ 门① 不通过：动了电路（只在原块 {sorted(map(sorted, a - b))[:2]}'
                         f' / 只在新块 {sorted(map(sorted, b - a))[:2]}）')

    # ② 块卡数据（端口/轨用算出来的，不用 read_sch 那套"所有标签都算端口"）
    info = blockpcb.read_sch(out)
    # 别名网改名：块里叫 `VOUT`/`+5V` 的，卡里用板上的名字 `BULK_VIN`（conns 的网名
    # 来自 PCB，本来就是这个）。同一张网在块里落成两段（标签各在一处）时在这里合并。
    for a, c in alias.items():
        if a in info['nets']:
            info['nets'].setdefault(c, [])
            info['nets'][c] = info['nets'][c] + info['nets'].pop(a)
    info['ports'] = exits
    info['rails'] = rails
    miss = [n for n in exits + rails if n not in info['nets']]
    if miss:
        raise SystemExit(f'✗ 端口/轨在块网表里找不到 {miss}')

    meta = {k: sp[k] for k in ('category', 'rationale', 'placement_prior') if k in sp}

    # 无名网（原理图上没标签的块内部线）在卡里被起成本地名 `_<块>_N<k>`，
    # **它们同样是内部网**：拼装时也必须逐实例改名，否则两份 vpp_boost 插进同一块板，
    # 两个 `_vpp_boost_N1` 会并成一张网（开关节点短在一起）。2026-10-30 补：
    # 以前 `classify()` 只收"图上有名字"的内部网，无名的那批一律漏掉。
    auto_internal = sorted(n for n in info['nets']
                           if str(n).startswith(f'_{block}_N'))
    internal = internal + [n for n in auto_internal if n not in internal]
    meta['internal_nets'] = internal
    print(f'   内部网 {len(internal)} 条（有名 {len(internal) - len(auto_internal)}'
          f' + 无名 {len(auto_internal)}）', flush=True)
    prov = dict(sp.get('provenance') or {})
    blockpcb.write_bundle(block, info, bdir, out, prov, meta=meta, sch_text=text)

    # ③ 几何那一栏如实写"没量"（块卡里这栏进 measured.json）
    mp = bdir / card.MEASURED
    measured = json.loads(mp.read_text(encoding='utf-8')) if mp.is_file() else {'block': block}
    measured['geometry_status'] = sp.get('geometry_status') or \
        'schematic-only —— 只交了原理图，板级几何（摆放/布线/DRC）未做'
    measured['parts_xy'] = {p['ref']: [round(p['x'], 2), round(p['y'], 2)]
                            for p in info['parts']}
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    card.write_card(bdir)      # 两份 json → yaml（唯一手写权威）
    card.emit(bdir)            # yaml → 两份 json
    ok, diffs, _ = card.roundtrip(bdir)
    print(f'   门② 块卡回读 {len(diffs)} 处不一致 → (block.sdi.yaml)', flush=True)

    # ④ 电气自检：如实记账，不当失败门。**但要把"改标签带来的那几条"单独标出来**，
    # 否则下次看一眼数字变了会以为是改坏了：global→hier 之后每个出块端口在根页里
    # 找不到父页，KiCad 就多报一条 pin_not_connected（实测正好 +端口数）。
    e0, e = erc(src), erc(out)
    delta = {k: e.get(k, 0) - e0.get(k, 0) for k in set(e) | set(e0)}
    delta = {k: v for k, v in delta.items() if v}
    print(f'   ERC {sum(e.values())} 条：{", ".join(f"{k}×{v}" for k, v in sorted(e.items())) or "0"}'
          f'（改标签前后只差 {delta or "0"}；层级标签在根页没有父页必然多报这条）', flush=True)
    return {'block': block, 'dir': bdir, 'exits': exits, 'rails': rails,
            'internal': internal, 'parts': len(info['parts']), 'nets': len(info['nets']),
            'gate1': True, 'gate2': ok and not diffs, 'erc': e}


def main(argv=None) -> int:
    ap = __import__('argparse').ArgumentParser(description='裁块产物 → 只含原理图的块卡')
    ap.add_argument('spec')
    a = ap.parse_args(argv)
    r = pack(Path(a.spec))
    print(f'\n{"✓" if r["gate2"] else "✗"} {r["block"]}: {r["parts"]} 件 / {r["nets"]} 网 / '
          f'端口 {len(r["exits"])} + 轨 {len(r["rails"])} → {r["dir"]}')
    return 0 if r['gate1'] and r['gate2'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
