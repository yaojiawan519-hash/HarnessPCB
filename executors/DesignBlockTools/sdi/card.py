#!/usr/bin/env python3
"""块卡：`block.sdi.yaml` 是**唯一手写的权威**，两份 json 由它重算。

一份块卡 = 一个 `defs:` 条目。字段表和语言里的 `defs:` 同构（见 `spec.DEF_KEYS`），
所以块卡和板级 spec 是同一种东西，只是块卡只写一个模块。

**进 yaml 的**（电路是什么 / 出处）：ports / rails / parts / conns / placement_prior /
slots / hot_loop / provenance，以及 `category` / `rationale` 这些散文。

**端口有两个名字，别混**（`sdi/library.py` 顶部那条注解就是讲这个）：

    ports[].name   接口名——语言里写 `can.TXD` 用的那个（契约）
    ports[].net    块内网名——连线表 `conns[].net` 用的那个；两者相同就省略

一个端口两套名字是现状而不是设计：can_node 的接口叫 TXD、块内网叫 CAN0_TX；
rs485_node 的接口叫 TXD、块内网就叫 TXD。合并成一份卡时**两个都得留**，
否则要么语言层写不了 `ldo.VOUT`，要么连线表对不上网。

**不进 yaml 的**（量出来的、或属于画法），全部住 `_geom/measured.json`：

    parts_xy                      原理图坐标（`parts[].x/y` 的来源）
    exit.<net>.faces/width_mm/…   出口面，从板上量的
    geometry_status               几何状态串

> 2026-10-28 迁移实测：`table.json` 的 `parts[].x/y` 是**原理图坐标**，`_geom_design.json`
> 的 `x/y` 是**归一化后的设计表坐标**（origin 已挪到 ~0）——两者不是一套，库内块差十几到
> 八十毫米，裁块恰好相同所以一开始没看出来。所以坐标只住 `_geom/measured.json` 一处，
> 由 `card init` 从现有 table 抄进去；将来 `sdi blockpcb` 可以从 `.kicad_sch` 重算覆盖。

`load()` 把 measured 拼回今天的两份 json 形状——所以 10 处读点只换数据来源，不改取法。
`roundtrip()` 是那条门：重算的两份 json 必须与磁盘上的逐字段一致。

`in_semantic: false` 是迁移期的临时标记：有 4 个块的 `table.json` 列了 semantic 里没有的
端口（can_node 4 个、ldo_3v3 2 个、mcu_min_stm32 2 个、usb_micro_if 3 个，共 11 个）。
这正是"同一件事写两处"的后果——合并后端口只有一份，但**不偷偷补进契约**，先标出来，
`card check` 每次打印这个数，等拍板补进 semantic 还是删掉。

用法：
    python3 -m sdi card <块目录或库根目录> --init      # 从现有两份 json 生成 yaml（一次性迁移）
    python3 -m sdi card <块目录或库根目录>              # 回读门：yaml 重算 == 磁盘上的 json
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

CARD = 'block.sdi.yaml'
TABLE_SCHEMA = 'pcbstudio.blocktable.v1'
SEM_SCHEMA = 'pcbstudio.block.v1.1'
TABLE_NOTE = '由 sdi.blockpcb 从裁块产物生成；parts/conns 与 d2s 全板设计表同构。'

MEASURED = Path('_geom') / 'measured.json'

# 端口上属于**契约**的字段（进 yaml）。`exit` 只装"这根网从哪个脚出去"，
# `faces`/`width_mm`/… 是板上量出来的，进 measured.json。
PORT_KEYS = ('name', 'kind', 'class', 'direction', 'diff_pair', 'pin', 'note',
             'voltage', 'alias')
EXIT_KEYS = ('pin', 'connector', 'connector_ref')
EXIT_MEASURED = ('faces', 'width_mm', 'depth_mm', 'area_mm2', 'dead')

# def 里除 ports/rails/parts/conns 之外原样透传的语义键
SEM_PASS = ('category', 'rationale', 'placement_prior', 'slots', 'hot_loop',
            'internal_nets', 'isolation_boundary', 'edge_free', 'notes', 'provenance')

SEM_ORDER = ('schema', 'category', 'rationale', 'power_rails', 'interface', 'slots',
             'placement_prior', 'hot_loop', 'internal_nets', 'isolation_boundary',
             'edge_free', 'notes', 'geometry_status', 'provenance')

# 两份 json 的表头是**产物头**，不是电路事实：不参与回读门的逐字段比对，
# `sdi blockpcb` 重算时按常量重写（老文件里这四处拼写还不一致：有的用
# `contract: pcbstudio.blocktable.v1`，有的缺 `$schema`）。
HEAD_KEYS = ('$schema', '$schema_note', 'contract')


class CardError(SystemExit):
    """块卡读不了 / 不完整。刻意硬退出：这些地方以前是静默跳过（见 CARD_MERGE_AUDIT.md）。"""


def _read_json(p: Path, what: str):
    if not p.is_file():
        raise CardError(f'✗ 缺 {what}：{p}')
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise CardError(f'✗ {what} 不是合法 JSON：{p}（{e}）')


def block_name(d: Path) -> str:
    return d.name[:-len('.kicad_block')] if d.name.endswith('.kicad_block') else d.name


# ---------------------------------------------------------------- 接口名归一
def _ends_map() -> dict:
    """`interfaces.json` 的 `ends`：{(块, 本地名): [规范名…]}。

    这份表是接口名统一（2026-10-26）的权威对照。同一个键在 `ifaces` 里可能出现两次
    （`ldo_3v3.VIN` 在 `pwr_vin` 下写 VBUS5V、在 `pwr_5v` 下写 +5V），所以存成列表，
    由"哪个真出现在本块连线里"来判——`interfaces.py` 的 last-wins 会静默取错。
    """
    p = Path(__file__).resolve().parent.parent / 'interfaces.json'
    if not p.is_file():
        return {}
    reg = json.loads(p.read_text(encoding='utf-8'))
    out: dict[tuple[str, str], list[str]] = {}
    for it in reg.get('ifaces') or []:
        for block, m in (it.get('ends') or {}).items():
            for local, net in m.items():
                out.setdefault((block, local), []).append(net)
    return out


def _net_of(block: str, name: str, nets: set, tnets: set, ends: dict) -> str:
    """把 semantic 里的端口名换成**本块连线里真实存在的网名**。

    老 semantic 用它自己的本地名（`TXD`/`VIN`/`PB9`/`USBD_P`），而 `table.json` 是
    接口名统一之后写的（`CAN0_TX`/`+3V3`）。同一件事两个名字，合并成一份时必须选一个：
    选网名——因为 `conns` 用网名，语言按网名把端口连到脚上。
    """
    if name in nets:
        return name
    for net in ends.get((block, name)) or []:
        if net in nets:
            return net
    if name in tnets:
        return name
    return name          # 幽灵：既不在连线里也没有对照，原样留着并报出来


# ---------------------------------------------------------------- 写：json → yaml
def from_jsons(bdir: Path) -> tuple[dict, list[str]]:
    """从现有的两份 json（+ 量测产物）拼出块卡 yaml 的数据。只搬家，不解释。"""
    n = block_name(bdir)
    sem = _read_json(bdir / 'block.semantic.json', 'semantic')
    table = _read_json(bdir / f'{n}.table.json', 'table')
    conns = [{k: c[k] for k in ('ref', 'pin', 'net') if k in c}
             for c in table.get('conns') or []]
    nets = {c.get('net') for c in conns}
    tby = {i.get('net'): i for i in table.get('interface') or []}
    ends = _ends_map()

    # 端口顺序：先按 table 的顺序（产品原来就这序），再补 semantic 独有的
    def _port(net, src):
        p = {'name': net}
        p.update({k: src[k] for k in PORT_KEYS if k in src and k != 'name'})
        ex = {k: (src.get('exit') or {})[k] for k in EXIT_KEYS
              if k in (src.get('exit') or {})}
        if ex:
            p['exit'] = ex
        return p

    by_net = {}
    for i in sem.get('interface') or []:
        name = i.get('name') or i.get('net')
        if not name:
            raise CardError(f'✗ {n}: semantic 里有一条接口既没有 name 也没有 net：{i}')
        net = _net_of(n, name, nets, set(tby), ends)
        if net in by_net:
            raise CardError(f'✗ {n}: semantic 的两条接口（{by_net[net][0]} 与 {name}）'
                            f'都归一到 {net}，合并后会撞，先人工核')
        by_net[net] = (name, i)

    # 端口顺序按 table（产品原来就这序）；字段以 semantic（电路契约）为准，
    # 出口/引脚以 table（连线产物）为准。
    ports = []
    for net, t in tby.items():
        name, i = by_net.pop(net, (None, None))
        p = {'name': name or net}
        if net != (name or net):
            p['net'] = net                      # 接口名 ≠ 块内网名时才写
        if i is not None:
            p.update({k: i[k] for k in PORT_KEYS if k in i and k != 'name'})
        else:
            p['in_semantic'] = False
        for k, dflt in (('kind', 'hlabel'), ('class', 'digital')):
            if k not in p:
                p[k] = t.get(k) or dflt
        if 'diff_pair' not in p and 'diff_pair' in t:
            p['diff_pair'] = t['diff_pair']
        ex = {k: (t.get('exit') or {})[k] for k in EXIT_KEYS if k in (t.get('exit') or {})}
        if ex:
            p['exit'] = ex
        ports.append(p)
    # semantic 里有、table 里没有的端口（极少数）：也留着，不能丢
    for net, (name, i) in by_net.items():
        p = {'name': name}
        if net != name:
            p['net'] = net
        p.update({k: i[k] for k in PORT_KEYS if k in i and k != 'name'})
        ports.append(p)
    table_only = [p['name'] for p in ports if p.get('in_semantic') is False]

    # 坐标不进 yaml：parts 只留电路事实
    parts = [{k: p[k] for k in ('ref', 'lib', 'value', 'footprint') if k in p}
             for p in table.get('parts') or []]

    # 电源轨只能有一份。实测 20 块里 19 块两边一致，`can_node` 不一致
    # （semantic 写 +5V，连线表和 table 都写 VBUS5V）——按**连线**判：
    # 一条 rail 必须真的出现在本块连线里，否则它就是幽灵网，供给/丢弃逻辑会漏。
    nets = {c.get('net') for c in conns}
    rails_sem = list(sem.get('power_rails') or [])
    rails_tbl = list(table.get('power_rails') or [])
    ok_sem = set(rails_sem) <= nets
    ok_tbl = set(rails_tbl) <= nets
    if ok_sem or not ok_tbl:
        rails = rails_sem or rails_tbl
        if not ok_sem:
            raise CardError(f'✗ {n}: power_rails {rails_sem} 不全是本块连线里的网'
                            f'（缺 {sorted(set(rails_sem) - nets)}），且 table 的 '
                            f'{rails_tbl} 也不是 → 两边都不可信，先人工核')
    else:
        rails = rails_tbl
        print(f'  ! {n}: semantic 的 power_rails {rails_sem} 里有不在连线中的网'
              f'（缺 {sorted(set(rails_sem) - nets)}），按连线校正为 {rails_tbl}')

    d = {'category': sem.get('category', ''),
         'rationale': sem.get('rationale', ''),
         'ports': ports,
         'rails': rails,
         'parts': parts,
         'conns': conns}
    for k in SEM_PASS:
        if k in sem:
            d[k] = sem[k]
    # `geometry_status` 是**量**出来的（摆完线、跑过 DRC 才知道），口径与 `Card.sem`
    # 一致：measured 有就以它为准。否则生产步骤刚写的 `pending` 会把上一轮量到的
    # `extracted 20.81×16.65mm…` 盖掉，yaml 里永远停在 pending。
    mp = bdir / MEASURED
    if mp.is_file():
        got = json.loads(mp.read_text(encoding='utf-8')).get('geometry_status')
        if got:
            d['geometry_status'] = got
    doc = {'project': {'kind': 'block', 'name': n, 'version': 'sdi/1'},
           'defs': {n: d}}
    return doc, table_only


def write_card(bdir: Path) -> tuple[Path, list[str]]:
    """写出 `block.sdi.yaml`，并把量出来的东西收进 `_geom/measured.json`。"""
    n = block_name(bdir)
    doc, table_only = from_jsons(bdir)
    p = bdir / CARD
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                default_flow_style=False, width=100),
                 encoding='utf-8')
    _capture_measured(bdir, n)
    return p, table_only


def _capture_measured(bdir: Path, n: str) -> None:
    """把原理图坐标 + 出口面 + geometry_status 收进 `_geom/measured.json`。

    这三样都是**从图上/板上量的**，不是人手写的，所以不进 yaml。合并之后它们由这里
    提供，`sdi blockpcb` 重跑时会用新量到的值覆盖。"""
    table = _read_json(bdir / f'{n}.table.json', 'table')
    sem = _read_json(bdir / 'block.semantic.json', 'semantic')
    p = bdir / MEASURED
    cur = json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
    cur['parts_xy'] = {q['ref']: [q.get('x', 0.0), q.get('y', 0.0)]
                       for q in table.get('parts') or [] if q.get('ref')}
    exit_m = {}
    for i in table.get('interface') or []:
        got = {k: (i.get('exit') or {})[k] for k in EXIT_MEASURED
               if k in (i.get('exit') or {})}
        if got:
            exit_m[i['net']] = got
    if exit_m:
        cur.setdefault('exit', {}).update(exit_m)
    if sem.get('geometry_status') and not cur.get('geometry_status'):
        # measured 优先：它记的是**量完的结论**，`block.semantic.json` 里的那份是生产
        # 步骤刚写的 `pending`。反过来覆盖的话，`sdi blockpcb` 跑完一次，刚量出来的
        # `extracted 20.81×16.65mm…` 立刻被打回 pending，yaml 就永远是 pending。
        # sem 只在 measured 还没有这份值时补位（老块迁移那一次）。
        cur['geometry_status'] = sem['geometry_status']
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- 读：yaml → 两份 json
class Card:
    """一块卡的两种视图，形状与今天的两份 json 一致（故名 sem / table）。"""

    def __init__(self, bdir: Path, doc: dict):
        self.dir = bdir
        self.name = block_name(bdir)
        proj = doc.get('project') or {}
        if proj.get('kind') != 'block':
            raise CardError(f"✗ {bdir / CARD}: project.kind 应为 'block'，"
                            f'实际 {proj.get("kind")!r}')
        defs = doc.get('defs') or {}
        if self.name not in defs:
            raise CardError(f'✗ {bdir / CARD}: defs 里没有 {self.name}（只有 {list(defs)}）')
        self.doc = doc
        self.d = d = defs[self.name]
        for k in ('ports', 'parts', 'conns'):
            if k not in d:
                raise CardError(f'✗ {bdir / CARD}: defs.{self.name} 缺 {k}')
        self.measured = self._measured()
        self.sem = self._sem()
        self.table = self._table()

    def _measured(self) -> dict:
        p = self.dir / MEASURED
        if not p.is_file():
            raise CardError(f'✗ {self.dir / CARD}: 缺 {MEASURED}'
                            '（坐标/出口面在那；老块先跑 sdi card --init）')
        return _read_json(p, str(MEASURED))

    def _sem(self) -> dict:
        d = self.d
        ports = []
        for p in d['ports']:
            if p.get('in_semantic') is False:
                continue                      # 迁移期标记：老 semantic 里没有这个端口
            q = {k: p[k] for k in PORT_KEYS if k in p}
            q.setdefault('kind', 'signal')
            q.setdefault('class', 'digital')
            ports.append(q)
        out = {'schema': SEM_SCHEMA,
               'category': d.get('category', ''),
               'rationale': d.get('rationale', ''),
               'power_rails': list(d.get('rails') or []),
               'interface': ports}
        # 其余语义键**有才出**：老 semantic 里 slots/placement_prior/geometry_status
        # 并非块块都有（`_extract_esp32s3` 那 6 块就没有），补默认值会凭空造字段。
        for k in ('slots', 'placement_prior', 'hot_loop'):
            if k in d:
                out[k] = d[k]
        for k in SEM_PASS:
            if k in d:
                out[k] = d[k]
        if 'geometry_status' in self.measured:
            out['geometry_status'] = self.measured['geometry_status']
        return {k: out[k] for k in SEM_ORDER if k in out} | \
               {k: v for k, v in out.items() if k not in SEM_ORDER}

    def _table(self) -> dict:
        d = self.d
        xy = self.measured.get('parts_xy') or {}
        ex_m = self.measured.get('exit') or {}
        ifaces = []
        for p in d['ports']:
            net = p.get('net') or p['name']          # 连线表只认块内网名
            # table 的 `kind` 是**画法**词（power 符号 / hlabel 层次标签），与端口的
            # 电路 kind 不是一套词；实测 129/131 条可由它推出（power→power，其余→hlabel），
            # 剩 xtal_cluster 两个时钟脚写成 signal（历史手误，无读者），
            # 所以这一栏不进回读门（见 roundtrip）。
            it = {'net': net,
                  'kind': 'power' if p.get('kind') == 'power' else 'hlabel',
                  'class': p.get('class') or 'digital'}
            ex = dict(p.get('exit') or {})
            ex.update(ex_m.get(net) or {})
            if ex:
                it['exit'] = ex
            if 'diff_pair' in p:
                it['diff_pair'] = p['diff_pair']
            for k in ('direction', 'note'):      # table 历史上有这两栏，有就照旧带上
                if k in p:
                    it[k] = p[k]
            ifaces.append(it)
        parts = []
        for p in d['parts']:
            if p.get('ref') not in xy:
                raise CardError(
                    f'✗ {self.dir / CARD}: 器件 {p.get("ref")} 在 {MEASURED} 里没有坐标'
                    '（块卡的器件表和量测表对不上，先重跑 sdi card --init）')
            x, y = xy[p['ref']]
            parts.append({**{k: p[k] for k in ('ref', 'lib', 'value', 'footprint')
                             if k in p}, 'x': x, 'y': y})
        return {'$schema': TABLE_SCHEMA,
                'block': self.name,
                'interface': ifaces,
                'power_rails': list(d.get('rails') or []),
                'parts': parts,
                'conns': [{k: c[k] for k in ('ref', 'pin', 'net') if k in c}
                          for c in d['conns']],
                '$schema_note': TABLE_NOTE}


def load(bdir) -> Card:
    bdir = Path(bdir)
    p = bdir / CARD
    if not p.is_file():
        raise CardError(f'✗ 缺块卡 {p}（老块先跑 python3 -m sdi card {bdir} --init）')
    try:
        doc = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError as e:
        raise CardError(f'✗ {p} 不是合法 YAML：{e}')
    return Card(bdir, doc)


def load_table(bdir) -> dict:
    return load(bdir).table


def emit(bdir) -> list[Path]:
    """从 yaml 重算两份 json 产物（轻量生产者；`sdi blockpcb` 才是全量那条）。

    缩进照旧（table 1、semantic 2），重算后两份文件不会整篇变色。"""
    c = load(bdir)
    bdir = Path(bdir)
    tbl = bdir / f'{c.name}.table.json'
    sem = bdir / 'block.semantic.json'
    tbl.write_text(json.dumps(c.table, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    sem.write_text(json.dumps(c.sem, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return [tbl, sem]


def load_sem(bdir) -> dict:
    return load(bdir).sem


def table_only_ports(bdir) -> list[str]:
    """只在连线表里、契约（老 semantic）里没有的端口。迁移期欠账，`card check` 每次都打。"""
    c = load(bdir)
    return [p['name'] for p in c.d['ports'] if p.get('in_semantic') is False]


# ---------------------------------------------------------------- 门：重算 == 磁盘
# 两份 json 今天互相打架的地方（合并时必须选一边），回读门允许这几类差异，
# 但**每一类都计数打印**，未归类的差异一律判不过。
ALLOWED = {
    'rails_vs_conns': '电源轨：semantic 写了不在连线里的网，按连线校正',
    'diff_pair_fill': '差分对：table 漏了 semantic 有的 diff_pair，重算时补上',
    'field_fill': 'direction/note：table 是 semantic 的**部分拷贝**（有的块有、有的没有），'
                  '合并后统一由 semantic 提供',
    'class_vocab': '端口 class：两套电源词（power_in/out 与 supply_in/out）并存，'
                   '合并后统一用 semantic 的 supply_*',
}

# 两套电源端口词表。只认这两对同义，别的 class 差异一律判不过。
CLASS_SYNONYM = {'power_in': 'supply_in', 'power_out': 'supply_out', 'power': 'supply'}


def _port_by(t: dict, key: str, val: str) -> dict:
    for i in t.get('interface') or []:
        if (i.get('net') or i.get('name')) == val:
            return i
    return {}


def _diff(got, want, path='', alias=None) -> list[tuple[str, str, str]]:
    """逐字段比，返回 (路径, op, 说明)；op ∈ {missing, extra, value}。

    `alias`：磁盘上的名字 → 重算后的名字。改名不该让整条记录"消失 + 冒出来"
    （那样字段级差异会被跳过），而是按改名对齐后逐字段比。"""
    if isinstance(got, dict) and isinstance(want, dict):
        out = []
        for k in sorted(set(got) | set(want)):
            if k not in got:
                out.append((f'{path}.{k}', 'missing', f'磁盘上有 {want[k]!r}，yaml 重算里没有'))
            elif k not in want:
                out.append((f'{path}.{k}', 'extra', f'yaml 重算有 {got[k]!r}，磁盘上没有'))
            else:
                out += _diff(got[k], want[k], f'{path}.{k}', alias)
        return out
    if isinstance(got, list) and isinstance(want, list):
        keyed = all(isinstance(x, dict) and ('net' in x or 'name' in x) for x in got + want) \
            and bool(got or want)
        if keyed:                                  # 接口表是集合，顺序无意义
            g = {(x.get('net') or x.get('name')): x for x in got}
            w = {(x.get('net') or x.get('name')): x for x in want}
            alias = alias or {}
            pairs = {k: alias[k] for k in w if k not in g and alias.get(k) in g}
            out, done = [], set(pairs.values())
            for k in sorted(set(g) | set(w)):
                if k in pairs:                     # 改了对上的同一个端口：按改名对齐再比字段
                    out += _diff(g[pairs[k]], w[k], f'{path}[{k}→{pairs[k]}]', alias)
                elif k not in g:
                    out.append((f'{path}[{k}]', 'missing', '磁盘上有，yaml 重算里没有'))
                elif k not in w:
                    if k not in done:
                        out.append((f'{path}[{k}]', 'extra', 'yaml 重算有，磁盘上没有'))
                else:
                    out += _diff(g[k], w[k], f'{path}[{k}]', alias)
            return out
        if len(got) != len(want):
            return [(path, 'value', f'条数 {len(got)}（重算） ≠ {len(want)}（磁盘）')]
        out = []
        for i, (a, b) in enumerate(zip(got, want)):
            out += _diff(a, b, f'{path}[{i}]', alias)
        return out
    if got != want:
        return [(path, 'value', f'重算 {got!r} ≠ 磁盘 {want!r}')]
    return []


def roundtrip(bdir: Path) -> tuple[bool, dict, list[tuple[str, str, str]]]:
    """回读门：yaml 重算的两份 json 必须与磁盘一致；只放行 `ALLOWED` 里那几类。

    返回 (是否通过, {类别: 条数}, 未归类差异)。"""
    n = block_name(bdir)
    c = load(bdir)
    raw = []
    ends = _ends_map()
    nets = {x['net'] for x in c.table['conns']}
    alias = {(local): net for (b, local), cands in ends.items() if b == n
             for net in cands if net in nets}
    sem_p = bdir / 'block.semantic.json'
    disk_sem = _read_json(sem_p, 'semantic') if sem_p.is_file() else {}
    if disk_sem:
        want = {k: v for k, v in disk_sem.items() if k not in HEAD_KEYS}
        got = {k: v for k, v in c.sem.items() if k not in HEAD_KEYS}
        raw += [('semantic' + p, op, d) for p, op, d in _diff(got, want, alias=alias)]
    tbl_p = bdir / f'{n}.table.json'
    disk_tbl = _read_json(tbl_p, 'table') if tbl_p.is_file() else {}
    if disk_tbl:
        want = {k: v for k, v in disk_tbl.items() if k not in HEAD_KEYS}
        got = {k: v for k, v in c.table.items() if k not in HEAD_KEYS}
        _drop_kind(want)          # 画法词（power/hlabel），yaml 不存，按规则重算
        _drop_kind(got)
        raw += [('table' + p, op, d) for p, op, d in _diff(got, want, alias=alias)]
    return _classify(n, c, disk_sem, disk_tbl, raw)


def _classify(n, c, disk_sem, disk_tbl, raw):
    nets = {x['net'] for x in c.table['conns']}
    disk_nets = {x['net'] for x in disk_tbl.get('conns') or []}
    ends = _ends_map()
    sem_ports = {}
    for p in c.d['ports']:
        sem_ports[p['name']] = p
        if p.get('net'):
            sem_ports[p['net']] = p         # table 侧按**网名**找，semantic 侧按接口名找
    allowed, bad = {}, []
    for path, op, detail in raw:
        cls = None
        m = re.match(r'(semantic|table)\.interface\[([^\]]+)\](?:\.(\w+))?$', path)
        if m and m.group(3):
            field = m.group(3)
            old, _, new = m.group(2).partition('→')
            new = new or old
            sem_p = sem_ports.get(new) or {}
            if field == 'diff_pair':
                if op in ('extra', 'value') and 'diff_pair' in sem_p:
                    cls = 'diff_pair_fill'
            elif field in ('direction', 'note') and op in ('extra', 'value') and field in sem_p:
                cls = 'field_fill'
            elif field == 'class':
                if CLASS_SYNONYM.get(_port_by(disk_tbl, 'net', old).get('class')) \
                        == _port_by(c.table, 'net', new).get('class'):
                    cls = 'class_vocab'
        elif re.match(r'semantic\.power_rails\[\d+\]$', path):
            if not set(disk_sem.get('power_rails') or []) <= disk_nets \
                    and set(c.sem['power_rails']) <= nets:
                cls = 'rails_vs_conns'
        if cls:
            allowed[cls] = allowed.get(cls, 0) + 1
        else:
            bad.append((path, op, detail))
    return (not bad), allowed, bad


def _describe(diffs, indent='      ') -> None:
    for path, op, d in diffs[:12]:
        print(f'{indent}{path} [{op}] {d}')


def _drop_kind(t: dict) -> None:
    for i in t.get('interface') or []:
        i.pop('kind', None)


# ---------------------------------------------------------------- 批量
def block_dirs(path) -> list[Path]:
    """接受一个块目录、或一个装着块的根目录。

    单个块目录的判据是"里面有 `block.sdi.yaml`"（2026-10-29 起块直接叫
    `DesignBlocks/<块名>/`，不再带 `.kicad_block` 后缀）；根目录按同样的判据挑子目录。
    老口径 `<名>.kicad_block/` 继续认。
    """
    p = Path(path)
    if p.name.endswith('.kicad_block') or (p / 'block.sdi.yaml').is_file():
        return [p]
    if p.is_dir():
        ds = sorted(x for x in p.iterdir()
                    if x.is_dir() and ((x / 'block.sdi.yaml').is_file()
                                       or x.name.endswith('.kicad_block')))
        if ds:
            return ds
    raise CardError(f'✗ {p}：既不是块目录（缺 block.sdi.yaml），也不是装块目录的父目录')


def auto_internal_yet_unnamed(c: Card) -> tuple[list[str], int]:
    """块内**无名**网（卡里被起成本地名 `_<块>_N<k>`）有没有全部登记进 `internal_nets`。

    为什么是门：这些网在原理图上没有标签，卡里的名字是**按块名**生成的。同一个块
    插两份，两个 `_vpp_boost_N1` 就会并成一张网——开关节点直接短在一起。拼装那步
    只认 `internal_nets` 逐实例改名，漏登记 = 漏改名。
    2026-10-30 在 Bus Pirate 5 的 vpp 块上发现：`classify()` 只收"图上有名字"的内部网，
    无名的那批一律漏掉；扫 50 份卡，**18 块**欠这条账。

    返回 (漏登记的网, 该块无名网总数)。
    """
    have = set(c.d.get('internal_nets') or [])
    auto = sorted({str(x.get('net')) for x in c.d.get('conns') or []
                   if str(x.get('net')).startswith(f'_{c.name}_N')})
    return [n for n in auto if n not in have], len(auto)


def fix_auto_internal(path) -> int:
    """把漏登记的无名内部网补进 yaml 的 `internal_nets`，再 emit + 回读。

    **只加不改**：已有的内部网原样保留（`classify()` 算出来的那些是有名网），
    补进去的是卡里已经存在、只是没登记的 `_<块>_N<k>`——名字从 `conns` 里取，
    不重新推导，所以补完 yaml 与两份 json 仍然逐字回读一致。
    """
    ds = block_dirs(path)
    fixed = 0
    for d in ds:
        c = load(d)
        lost, k = auto_internal_yet_unnamed(c)
        if not lost:
            continue
        yp = d / CARD
        raw = yaml.safe_load(yp.read_text(encoding='utf-8'))
        dd = raw['defs'][c.name]
        dd['internal_nets'] = list(dd.get('internal_nets') or []) + lost
        yp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False,
                                     default_flow_style=False, width=100),
                      encoding='utf-8')
        emit(d)
        ok, allowed, diffs = roundtrip(d)
        left, _ = auto_internal_yet_unnamed(load(d))
        print(f'{"✓" if ok and not left else "✗"} {c.name}：补 {len(lost)}/{k} 张无名内部网；'
              f'回读 {"一致" if ok else f"{len(diffs)} 处不一致"}')
        fixed += 0 if ok and not left else 1
    print(f'补登记完成：{"全部一致" if not fixed else f"{fixed} 块仍有问题"}')
    return 1 if fixed else 0


def _report_debt(d: Path) -> None:
    t = table_only_ports(d)
    if t:
        print(f'    ! 迁移期欠账：{len(t)} 个端口只在连线表、不在契约里 —— {", ".join(t)}')
        print('      （合并前它们就两边不一致；现在标在 yaml 的 in_semantic: false，'
              '待拍板补进契约还是删掉）')


def init(path) -> int:
    bad = 0
    for d in block_dirs(path):
        p, table_only = write_card(d)
        print(f'✓ {p}')
        ok, allowed, diffs = roundtrip(d)
        _report_allowed(allowed)
        if not ok:
            bad += 1
            print(f'    ✗ 回读门 {len(diffs)} 处**未归类**差异（yaml 没能完整表达旧 json）：')
            _describe(diffs)
        _report_debt(d)
    return 1 if bad else 0


def _report_allowed(allowed: dict) -> None:
    """已归类的差异**照样报数**：放行不等于没发生，每类都要看得见。"""
    for k, v in sorted(allowed.items()):
        print(f'    · {v} 处 {k}：{ALLOWED[k]}')


def check(path) -> int:
    ds = block_dirs(path)
    bad = 0
    bad_net = 0
    bad_prose = 0
    n_auto = 0
    for d in ds:
        ok, allowed, diffs = roundtrip(d)
        tail = '' if ok else f'：{len(diffs)} 处未归类不一致'
        if allowed:
            tail += '（' + '、'.join(f'{k} {v}' for k, v in sorted(allowed.items())) + '）'
        lost, k = auto_internal_yet_unnamed(load(d))
        n_auto += k
        if lost:
            tail += f'；无名内部网漏登记 {len(lost)}/{k}'
        print(('  ✓ ' if ok and not lost else '  ✗ ') + block_name(d) + tail)
        if not ok:
            _describe(diffs)
        bad += 0 if ok else 1
        bad_net += 1 if lost else 0
        _report_debt(d)
        mism = prose_counts_mismatch(d)
        if mism:
            bad_prose += 1
            print('    ✗ 散文数字与 counts 对不上：'
                  + '；'.join(f'{f} 写 {a} / 实 {b}' for f, a, b in mism)
                  + '（`python3 -m sdi.basis --apply --prose` 可同步）')
        elif not (load(d).sem.get('provenance') or {}).get('basis', '').strip():
            # 没有 basis 就没有可比对的散文——数字门对它是瞎的，所以"缺"本身算不过
            bad_prose += 1
            print('    ✗ 缺 `basis:`（出处/量级那一行），散文数字门无从比')
    print(f'{"✓" if not bad and not bad_net else "✗"} 块卡回读门：{len(ds) - bad} 过 / {bad} 不过'
          f'；无名内部网登记：{len(ds) - bad_net} 块齐 / {bad_net} 块漏（共 {n_auto} 张无名网）')
    print(('✓' if not bad_prose else '✗')
          + ' 散文数字门（卡里抄的铜/孔/死铜/切口——`basis:` + `geometry_status`/`notes`——'
            'vs `placement.json` 的 counts；带 `原值`/`交付时` 等历史标记的句子豁免；'
            f'缺 `basis:` 也算不过）：{len(ds) - bad_prose} 过 / {bad_prose} 不过')
    _report_counts_debt(ds)
    return 1 if bad or bad_net or bad_prose else 0


#: `basis:` 里抄的四个数 → `counts` 里对应的键。写成规则不写在散文里：这几栏算得出来，
#: 卡里再抄一遍就一定会漂（2026-10-30 实测：24 块重抠之后，卡里还写着旧引擎的段数，
#: 没有任何一道门看得见）。
PROSE_NUMS = (
    (re.compile(r'铜 (\d+) 段 \+ (\d+) 孔'), ('copper_segs', 'vias')),
    (re.compile(r'死铜删 (\d+)'), ('isolated_deleted',)),
    (re.compile(r'口岸切口 (\d+)'), ('cut_ends',)),
)


def prose_counts_mismatch(bdir) -> list[tuple[str, str, str]]:
    """卡里抄的数字 vs 板上 `counts`。对不上就报（字段名, 卡里写的, 实际的）。

    2026-10-31 起**整张卡**都过这把尺子，不再只看 `basis:`：`geometry_status` /
    `notes` 里同样抄着"铜 N 段 + M 孔"，只校 basis 时它们漂了没人管——全库实测 65 块
    对不上（重抠 + 三轮清死铜之后，卡还写着裁剪前的段数）。带历史标记的句子不算错
    （`原值`/`交付时`/`旧值`…，见 `sdi.basis.HISTORY_MARK`）。
    """
    d = Path(bdir)
    p = d / f'{d.name}.placement.json'
    if not p.is_file():
        return []
    try:
        counts = (json.loads(p.read_text(encoding='utf-8')) or {}).get('counts') or {}
    except ValueError:
        return []
    basis = _find_basis(load(d).doc)
    if not basis or not counts:
        return []
    out = []
    for pat, keys in PROSE_NUMS:
        m = pat.search(basis)
        if not m:
            continue
        for got, k in zip(m.groups(), keys):
            if k in counts and counts[k] is not None and int(got) != int(counts[k]):
                out.append((k, got, counts[k]))
    yml = d / 'block.sdi.yaml'
    if yml.is_file():
        from . import basis as _b
        for old, new, _s, _e in _b.prose_fixes(yml.read_text(encoding='utf-8'), counts):
            out.append(('散文', ' '.join(old.split()), ' '.join(new.split())))
    return out


def _find_basis(o):
    """块卡里 `basis:` 藏在 defs.<模块>.provenance 下；递归找第一处字符串。"""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == 'basis' and isinstance(v, str):
                return v
            found = _find_basis(v)
            if found:
                return found
    elif isinstance(o, list):
        for v in o:
            found = _find_basis(v)
            if found:
                return found
    return None


#: `counts` 里**只有 carve 当时能算**、事后补不出来的键（源板铺铜岛数、借块外焊盘的处数）。
#: 缺了不影响任何判据（`spur` 的通行证只会更严），但那是**没测过**，得看得见。
CARVE_ONLY_COUNTS = ('island_split', 'borrowed_pads')


def _report_counts_debt(ds) -> None:
    miss = {k: [] for k in CARVE_ONLY_COUNTS}
    nob = []
    for d in ds:
        p = Path(d) / f'{Path(d).name}.placement.json'
        if not p.is_file():
            nob.append(Path(d).name)       # 老 V3.x 血统的块：没有 placement.json
            continue
        try:
            counts = (json.loads(p.read_text(encoding='utf-8')) or {}).get('counts') or {}
        except ValueError:
            counts = {}
        for k in miss:
            if k not in counts:
                miss[k].append(Path(d).name)
    if nob:
        print(f'  · 没有 `placement.json` 的块 {len(nob)} 块（老 V3.x 血统；散文数字门对它们'
              f'**静默跳过**，`counts` 一栏也没有）：' + '、'.join(nob))
    if any(miss.values()):
        print('  · counts 缺 carve 当时的账（事后算不出来，只能重扣块）：'
              + '；'.join(f'{k} 缺 {len(v)} 块' for k, v in miss.items() if v)
              + f'（有 placement.json 的块 {sum(1 for d in ds if (Path(d) / (Path(d).name + ".placement.json")).is_file())}）')
