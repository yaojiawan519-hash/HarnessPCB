#!/usr/bin/env python3
"""展开层：`.sdi.yaml` → 逐 pin 网表（+ 可写回 d2s 的 design.json）。

输入只有"电路是什么"：用了哪些模块、哪几根线接在一起。**没有任何坐标**——器件位置、块内
几何都在块卡里，落位是 L3 的事。

展开五步（顺序不能换）：
1. **ref 重编号**：与 `assemble_blocks.py` 同一算法（按模块声明序、字母前缀各自起 1），
   这样同一块板用语言重写和用旧 JSON spec 拼装，ref 完全对齐，可以逐 pin 对比（门2）。
2. **块内网起名**：电源轨用轨名（全局）；接口端口用 `ends` 给规范名；端口没有 `ends`
   就退成 `别名/端口`；块内私有网用 `别名__网名`（沿用 `<块名>__` 的旧约定，只为可读）。
3. **电源轨自动并网**：所有声明了 `+3V3` 的块，它们的 `+3V3` 节点并成一根网。
4. **语言语句**：`nets` 逐条并节点并**命名**（语言给的网名最高优先），`ifaces` 按
   `interfaces.json` 的 signals/ends 逐信号展开。
5. **活性**：电源轨 <2 成员 = 拼装错误（fail-closed）；单成员网丢弃（= NC）；
   语言声明过却只剩单成员的，直接报错（声明了却没接上，是意图失败，不是可以悄悄丢的）。

命名冲突一律报错，不自动改名——同一条线上出现两个名字，说明语言里有两句话在打架。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .library import Library
from .spec import split_endpoint

REF_PREFIX = re.compile(r'^([A-Za-z]+)')


@dataclass
class Netlist:
    nets: dict[str, list[str]] = field(default_factory=dict)      # 网名 -> ['别名.ref.pin', ...]
    pin_net: dict[str, str] = field(default_factory=dict)          # '别名.ref.pin' -> 网名
    refmap: dict[str, dict[str, str]] = field(default_factory=dict)
    parts: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    kept_single: list[str] = field(default_factory=list)
    rails: list[str] = field(default_factory=list)
    aliases: dict[str, set] = field(default_factory=dict)          # 正名 -> 别名集合
    warnings: list[str] = field(default_factory=list)

    def resolve(self, name: str) -> str | None:
        """网名 → 正名。别名（同一个网被第二句话又叫了一次）也认。"""
        if name in self.nets:
            return name
        for canon, al in self.aliases.items():
            if name in al:
                return canon
        return None

    def to_design(self, spec: dict) -> dict:
        conns = []
        for name, pins in self.nets.items():
            for k in pins:
                _, ref, pin = k.split('.', 2)
                conns.append({'ref': ref, 'pin': pin, 'net': name})
        for ref, pin in self.no_connect:
            conns.append({'ref': ref, 'pin': pin, 'net': None})
        conns.sort(key=lambda c: (c['ref'], str(c['pin'])))
        return {
            '$schema_note': ('pcbstudio.design.v1 —— 由 sdi 语言展开生成；'
                             '坐标未解算（parts 的 x/y 仍是块内相对值，落位属 L3）'),
            'project': spec.get('project') or {},
            'board': spec.get('board') or {},
            'parts': self.parts,
            'conns': conns,
            'power': {'rails': self.rails, 'pwr_flags': []},
            'no_connect': [],
            'keep_connected': [],
        }

    no_connect: list[tuple[str, str]] = field(default_factory=list)


class _DSU:
    def __init__(self):
        self.p = {}

    def add(self, x):
        self.p.setdefault(x, x)
        return x

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra
        return ra


def expand(spec: dict, lib: Library) -> Netlist:
    modules: dict[str, str] = spec['modules']
    order = list(modules)
    dsu = _DSU()
    name: dict = {}         # 节点自身默认名
    forced: dict = {}       # 根节点被语言指定的正名
    forced_src: dict = {}   # 这个名字的出处：rail / iface / nets（决定优先级）
    alias_pairs: dict = {}  # 正名 -> 别名集合
    errors: list[str] = []
    nl = Netlist()

    # ── 1. ref 重编号（与 assemble_blocks.py 对齐）──────────────────────────
    # 外科删件（`surgery.drop`）由 `SpecLibrary.block()` 直接体现在块视图里：
    # 删掉的件不在 `parts`，它的脚也不在 `pin_nets`——所以这里照常数、照常接，
    # 不用再为它开分支（网表里不会出现图上没有的器件）。
    counters: dict[str, int] = {}
    for alias in order:
        b = lib.block(modules[alias])
        m = {}
        for p in b.parts:
            pre = REF_PREFIX.match(p['ref']).group(1)
            counters[pre] = counters.get(pre, 0) + 1
            m[p['ref']] = f'{pre}{counters[pre]}'
            q = dict(p)
            q['ref'] = m[p['ref']]
            q['alias'] = alias
            nl.parts.append(q)
        nl.refmap[alias] = m

    # ── 2. 块内网节点 + 默认名 ─────────────────────────────────────────────
    port_of_net: dict = {}      # (alias, 块内网) -> [端口名]
    for alias in order:
        b = lib.block(modules[alias])
        net_ports: dict[str, list[str]] = {}
        for port in b.ports:
            try:
                net_ports.setdefault(lib.port_net(modules[alias], port), []).append(port)
            except KeyError:
                pass
        for ln, ps in net_ports.items():
            port_of_net[(alias, ln)] = ps
        for ln in b.pin_nets:
            if ln == 'NC':
                continue
            nid = (alias, ln)
            dsu.add(nid)
            if ln in b.rails or ln in getattr(b, 'named_nets', ()):
                # `rails` = 块自己声明的电源轨；`named_nets` = 语言用 `surgery.supply`
                # 直接点名的网（"这个脚改接 VBUS_5V"，名字是语言给的，不是块给的）。
                name[nid] = ln
            else:
                ports = net_ports.get(ln, [])
                canon = set()
                for p in ports:
                    canon |= set(lib.ends_targets(modules[alias], p))
                if len(canon) == 1:
                    name[nid] = next(iter(canon))
                elif ports:
                    name[nid] = f'{alias}/' + '/'.join(sorted(ports))
                else:
                    name[nid] = f'{alias}__{ln}'

    def force(root, nm, why, src='nets'):
        """给一根网定名。**同名即同网；异名只在"正名 vs 别名"时才允许。**

        优先级：电源轨 / 接口规范名（物理事实） > `nets` 语句名（本板叫法）。
        同类相撞 = 语言自相矛盾，报错不猜。
        """
        root = dsu.find(root)
        prev = forced.get(root)
        if prev is None:
            forced[root] = nm
            forced_src[root] = src
            return
        if prev == nm:
            return

        def prio(n, s):
            # 板地最高：`GND: [pwr.VRAW-]` 这种写法是"端子回线就是板地"，语义上必须赢过
            # VRAW- 自己的轨名，否则板地会被拆成两根网。
            if n == 'GND':
                return 3
            return {'rail': 2, 'iface': 2, 'nets': 1}[s]

        if prio(nm, src) > prio(prev, forced_src[root]):
            alias_pairs.setdefault(nm, set()).add(prev)
            forced[root] = nm
            forced_src[root] = src
        elif prio(nm, src) < prio(prev, forced_src[root]):
            alias_pairs.setdefault(prev, set()).add(nm)
        else:
            errors.append(f'命名冲突：{why} 想把一根网叫 {nm}，但它已经叫 {prev}；'
                          f'语言里有两句话在打架')

    def node(alias, port):
        ln = lib.port_net(modules[alias], port)
        nid = (alias, ln)
        if nid not in dsu.p:
            raise KeyError(f'{alias}.{port} 落在块内网 {ln} 上，但该网没有引脚')
        return nid

    # ── 3. 电源轨自动并网 ──────────────────────────────────────────────────
    # 轨名同样要过 ends 解析：can_node 的语义轨叫 `+5V`，块内网却叫 `VBUS5V`
    # （库自检 2026-10-27 抓到的真账不平）。规范名一律以 ends 为准。
    rail_nodes: dict[str, list] = {}
    rail_of: dict = {}          # (alias, 块内网) -> 规范轨名
    for alias in order:
        b = lib.block(modules[alias])
        for r in b.rails:
            try:
                ln = lib.port_net(modules[alias], r)
            except KeyError:
                continue
            tg = lib.ends_targets(modules[alias], r)
            canon = tg[0] if len(tg) == 1 else r
            rail_nodes.setdefault(canon, []).append((alias, ln))
            rail_of[(alias, ln)] = canon
            if ln != r:
                name[(alias, ln)] = canon
    for r, ns in rail_nodes.items():
        root = ns[0]
        for other in ns[1:]:
            root = (dsu.union(root, other), dsu.find(root))[1]
        force(root, r, f'电源轨 {r}', 'rail')

    # ── 4a. nets 语句 ─────────────────────────────────────────────────────
    declared_ends = {k: list(v or []) for k, v in (spec.get('nets') or {}).items()}
    declared = set(declared_ends)
    for nm, eps in (spec.get('nets') or {}).items():
        ids = [node(*split_endpoint(ep).__getitem__(slice(0, 2))) for ep in eps]
        if ids:
            root = ids[0]
            for other in ids[1:]:
                root = (dsu.union(root, other), dsu.find(root))[1]
            force(root, nm, f'net {nm}')
        else:
            # 空端点表 = 只声明这条轨存在（电源轨/端子），节点已经按轨名建好了
            cands = rail_nodes.get(nm) or []
            if not cands:
                errors.append(f'net {nm} 没有任何端点，也不是任何模块声明的电源轨')

    # ── 4b. ifaces 语句 ───────────────────────────────────────────────────
    for iid, members in (spec.get('ifaces') or {}).items():
        iface = lib.ifaces[iid]
        ends_all = iface.get('ends') or {}
        for sig, canon in (iface.get('signals') or {}).items():
            ids = []
            for alias in members:
                bname = modules[alias]
                ends = ends_all.get(bname) or {}
                for port, target in ends.items():
                    if target != canon:
                        continue
                    try:
                        ids.append(node(alias, port))
                    except KeyError as e:
                        errors.append(f'iface {iid}/{sig}: {e}')
            if not ids:
                continue
            root = ids[0]
            for other in ids[1:]:
                root = (dsu.union(root, other), dsu.find(root))[1]
            force(root, canon, f'iface {iid}/{sig}', 'iface')

    if errors:
        raise ExpandError(errors)

    # ── 4c. 同名即同网：把所有拿到同一个名字的组并起来 ─────────────────────
    # 这条是"电源轨自动并网"的一般形式：`+3V3` 这个网名同时出现在 mcu/can/usb 的轨声明
    # 和 `+3V3: [ldo.VOUT]` 这条语句里，四处必须是一根网。名字就是网的身份证。
    by_name: dict[str, list] = {}
    for nid in list(dsu.p):
        root = dsu.find(nid)
        nm = forced.get(root) or name[nid]
        by_name.setdefault(nm, []).append(nid)
    for nm, nids in by_name.items():
        root = nids[0]
        for other in nids[1:]:
            root = (dsu.union(root, other), dsu.find(root))[1]
        forced[dsu.find(root)] = nm

    # ── 5. 收口：分组成网 + 活性 ───────────────────────────────────────────
    groups: dict = {}
    for nid in list(dsu.p):
        groups.setdefault(dsu.find(nid), []).append(nid)
    rail_names = set(rail_nodes)
    for root, nids in groups.items():
        nm = forced.get(root) or name[nids[0]]
        if root not in forced:
            # 没被语言指名的组，只可能是单节点；若合并过说明有代码路径没 set_forced
            if len({name[n] for n in nids}) > 1:
                raise ExpandError([f'内部错误：{nm} 合并了多个不同名的节点却没命名'])
        pins = []
        for alias, ln in nids:
            for ref, pin in lib.block(modules[alias]).pin_nets[ln]:
                pins.append(f'{alias}.{nl.refmap[alias][ref]}.{pin}')
        pins.sort()
        if len(pins) < 2:
            if nm in declared:
                # 语言里只写了**一个端点**的网 = 板级出口（这名字就是要送上板外去的）。
                # 写了两个端点却只接上一个，才是"意图没实现"——那必须报错。
                if lib.is_stub(nm) or len(declared_ends.get(nm) or []) <= 1:
                    nl.kept_single.append(nm)
                else:
                    errors.append(f'net {nm} 声明了但只接上 1 个引脚（意图没实现）')
                    continue
            else:
                nl.dropped.append(nm)
                continue
        if nm in rail_names and len(pins) < 2:
            errors.append(f'电源轨 {nm} 成员 {len(pins)} < 2（接错了？）')
        nl.nets[nm] = pins
        for k in pins:
            nl.pin_net[k] = nm

    # NC 引脚：net=None，留给 d2s 的 auto_nc
    for alias in order:
        b = lib.block(modules[alias])
        for ref, pin in b.pin_nets.get('NC', []):
            nl.no_connect.append((nl.refmap[alias][ref], pin))

    nl.rails = [r for r in rail_nodes if r in nl.nets and r != 'GND']
    if 'GND' in nl.nets:
        nl.rails.append('GND')

    # 端口悬空：整根网只有一个块的端口撑着 —— 十有八九是忘了接（本板 buck.VIN 就是这样
    # 被抓出来的：24V 没接进降压级）。BOOT0/EN 这类本块内部的备用脚在 stub_patterns 里，跳过。
    # 语言里**只写了一个端点**的网是明说的板级出口（USBD_P、TEMP_FILT…），不是漏接。
    terminals = {nm for nm, eps in declared_ends.items() if len(eps) <= 1}
    for root, nids in groups.items():
        if len(nids) != 1:
            continue
        alias, ln = nids[0]
        ports = port_of_net.get((alias, ln)) or []
        if not ports or all(lib.is_stub(p) for p in ports):
            continue
        if any(t in lib.off_board for p in ports for t in lib.ends_targets(modules[alias], p)):
            continue          # 出板信号（A/B、H/L、VBUS）：只有一块用是正常的
        nm = nl.pin_net.get(next(iter(
            [f'{alias}.{nl.refmap[alias][r]}.{p}'
             for r, p in lib.block(modules[alias]).pin_nets[ln]]), ''), '')
        if nm and nm in terminals:
            continue          # 语言已宣告为板级出口
        if nm:
            nl.warnings.append(f'端口悬空：{alias}.{"/".join(ports)}（{nm}）只连着本块，'
                               f'没有接任何其它模块')

    # 别名收口：只留"正名确实存在、且别名不是另一根网的正名"的那些
    for canon, al in alias_pairs.items():
        if canon not in nl.nets:
            continue
        nl.aliases[canon] = {a for a in al if a not in nl.nets}

    if errors:
        raise ExpandError(errors)
    return nl


class ExpandError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__('; '.join(errors))
        self.errors = errors
