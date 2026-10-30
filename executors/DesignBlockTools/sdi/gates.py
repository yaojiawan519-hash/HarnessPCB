#!/usr/bin/env python3
"""门禁层：语言说过的每句话，都要在展开结果里找得到。

**门1 回读**（`readback`）：把语言当合同，逐条核展开结果——
`nets` 的每个端点、`ifaces` 的每根信号、每个模块的每条块内网，都得落在该在的那根网上，
且没有一根引脚被两个网抢。这能抓的正是历史上真实出过的错：导线共用顶点把两根网并了、
标签同名把两个连通块连了、劈块时锚点丢了。

**门2 对真实板回放**（`compare_partition`）：拿语言重画一块**已经用旧 JSON 拼装过**的板，
比"谁和谁在一个网"的分区。名字可能不同（旧板用引脚名当网名），但**分区必须一模一样**——
这就把"语言有没有改变电路"这件事，变成了可以逐 pin 判定的硬结论。
"""
from __future__ import annotations

import json
from pathlib import Path

from .expand import Netlist
from .spec import split_endpoint

# 块卡的原理图几何要从 schematic 侧的工具里读（`extract` 是唯一读 .kicad_sch 的地方）
_ILTOOLS = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'


def readback(spec: dict, nl: Netlist, lib) -> list[str]:
    errs: list[str] = []
    modules = spec['modules']

    def keys_of(alias: str, port: str) -> list[str]:
        bname = modules[alias]
        b = lib.block(bname)
        ln = lib.port_net(bname, port)
        out = []
        for ref, pin in b.pin_nets[ln]:
            out.append(f'{alias}.{nl.refmap[alias][ref]}.{pin}')
        return out

    # 1) nets 语句逐条回读
    for nm, eps in (spec.get('nets') or {}).items():
        want = []
        for ep in eps:
            a, p = split_endpoint(ep)
            want.extend(keys_of(a, p))
        canon = nl.resolve(nm)
        got = set(nl.nets.get(canon) or [])
        missing = [k for k in want if k not in got]
        if missing:
            errs.append(f'net {nm}: 语言声明的端点 {missing} 不在展开结果的这根网上')

    # 2) ifaces 逐信号回读
    for iid, members in (spec.get('ifaces') or {}).items():
        iface = lib.ifaces[iid]
        ends_all = iface.get('ends') or {}
        for sig, canon in (iface.get('signals') or {}).items():
            want = []
            for alias in members:
                bname = modules[alias]
                for port, target in (ends_all.get(bname) or {}).items():
                    if target == canon:
                        want.extend(keys_of(alias, port))
            if not want:
                continue
            got = set(nl.nets.get(canon) or [])
            missing = [k for k in want if k not in got]
            if missing:
                errs.append(f'iface {iid}/{sig}（{canon}）: 端点 {missing} 没接上')

    # 3) 块内网完整：一个块自己的网不许被拆到两根网上
    #    （单引脚网被丢弃是正常活性规则——未用 GPIO 就是这样下线的，不算拆分）
    for alias, bname in modules.items():
        b = lib.block(bname)
        for ln, pins in b.pin_nets.items():
            if ln == 'NC':
                continue
            mine = {f'{alias}.{nl.refmap[alias][r]}.{p}' for r, p in pins}
            hit = {nl.pin_net[k] for k in mine if k in nl.pin_net}
            if len(hit) > 1:
                errs.append(f'{alias}.{bname}: 块内网 {ln} 被拆到了多根网 {sorted(hit)}')
            elif len(pins) >= 2 and len(hit) == 0:
                errs.append(f'{alias}.{bname}: 块内网 {ln}（{len(pins)} 个引脚）整根被丢了')

    # 4) 一根引脚只能在一根网上
    seen: dict[str, str] = {}
    for nm, pins in nl.nets.items():
        for k in pins:
            if k in seen and seen[k] != nm:
                errs.append(f'引脚 {k} 同时出现在 {seen[k]} 和 {nm} 上')
            seen[k] = nm
    return errs


def _partition_from_nets(nets: dict[str, list[str]]) -> set[frozenset]:
    out = set()
    for nm, pins in nets.items():
        if len(pins) < 2:
            continue
        out.add(frozenset(pins))
    return out


def partition(nl: Netlist) -> set[frozenset]:
    """别名去掉后的"谁和谁同网"分区：{'U1.5','Y1.1','C7.1'} …"""
    nets = {}
    for nm, pins in nl.nets.items():
        nets[nm] = ['.'.join(k.split('.')[1:]) for k in pins]   # 去掉别名
    part = _partition_from_nets(nets)
    if len(nl.no_connect) >= 2:
        part.add(frozenset(f'{r}.{p}' for r, p in nl.no_connect))
    return part


def compare_partition(nl: Netlist, ref_design: str | Path) -> dict:
    """与一份旧拼装结果（pcbstudio.design.v1）比分区。"""
    d = json.loads(Path(ref_design).read_text(encoding='utf-8'))
    ref_nets: dict[str, list[str]] = {}
    for c in d['conns']:
        nm = c['net'] if c['net'] is not None else '<NC>'
        ref_nets.setdefault(nm, []).append(f"{c['ref']}.{c['pin']}")
    want = set()
    for nm, pins in ref_nets.items():
        if nm == '<NC>':
            if len(pins) >= 2:
                want.add(frozenset(pins))
        elif len(pins) >= 2:
            want.add(frozenset(pins))
    got = partition(nl)
    return {
        'only_ref': sorted([sorted(x) for x in want - got]),
        'only_ours': sorted([sorted(x) for x in got - want]),
        'common': len(want & got),
        'ref_nets': len(ref_nets),
        'our_nets': len(got),
        'ref_pins': sum(len(v) for v in ref_nets.values()),
        'our_pins': sum(len(v) for v in nl.nets.values()),
    }


def _sch_pins(sch: Path) -> dict[str, dict[str, str]] | None:
    """从 `.kicad_sch` 读每个器件的 {引脚号: 引脚名}。读不了就返回 None（跳过检查）。

    连线表必须写**引脚号**（KiCad 的规范标识，封装焊盘也用它）。2026-10-27 抓到
    `power_entry` 写成了引脚名（D1 的 `A/K`、D2 的 `A1/A2`）：它的符号和封装都只有 1/2，
    这块板以前从没被用过，所以一直没暴露——换新板才撞见。
    """
    try:
        import sys
        d = str(_ILTOOLS)
        if d not in sys.path:
            sys.path.insert(0, d)
        import extract                                    # noqa: PLC0415
        from sexpr import parse, H                        # noqa: PLC0415
    except Exception:
        return None
    try:
        il = extract.extract(str(sch))
        root = parse(sch.read_text(encoding='utf-8'))
    except Exception:
        return None

    libpins: dict[str, dict[str, str]] = {}

    def walk(n):
        if not isinstance(n, list):
            return
        if H(n) == 'lib_symbols':
            for sym in n[1:]:
                if not isinstance(sym, list) or len(sym) < 2:
                    continue
                m: dict[str, str] = {}

                def pins(node):
                    if not isinstance(node, list):
                        return
                    if H(node) == 'pin':
                        nm = num = None
                        for c in node[1:]:
                            if isinstance(c, list):
                                if H(c) == 'name':
                                    nm = c[1]
                                elif H(c) == 'number':
                                    num = c[1]
                        if num:
                            m[num] = nm or ''
                        return
                    for c in node[1:]:
                        pins(c)

                pins(sym)
                libpins[sym[1]] = m
            return
        for c in n[1:]:
            walk(c)

    walk(root)
    out = {}
    for s in il['symbols']:
        if s.get('power'):
            continue
        m = libpins.get(s['lib']) or {}
        out[s['id']] = {str(k): m.get(str(k), '') for k in (s.get('pins_at') or {})}
    return out


def doctor(lib) -> list[str]:
    """库自检：接口注册表和 10 张块卡自己的账对不对得上。

    只查**库内部矛盾**（不涉及某块板）：接口登记的端口存不存在、信号有没有两端、
    块声明的轨有没有出现在它自己的连线表里、器件的封装有没有绑上、
    **连线表的引脚号在原理图符号里存不存在**。
    """
    out: list[str] = []
    base = getattr(lib, 'lib', lib)

    for iid, iface in base.ifaces.items():
        for bname, ends in (iface.get('ends') or {}).items():
            if not base.has_block(bname):
                out.append(f'[error] iface {iid}: ends 指到不存在的块 {bname}')
                continue
            b = base.block(bname)
            for port in ends:
                if port not in b.ports and port not in b.rails:
                    out.append(f'[warn] iface {iid}: ends 里 {bname}.{port} 既不是端口也不是电源轨')
        # 信号两端（电源网由"同名即同网"自动并，不要求两端都登记）
        rail_names = set()
        for bn in (iface.get('ends') or {}):
            if base.has_block(bn):
                rail_names |= set(base.block(bn).rails)
        for sig, canon in (iface.get('signals') or {}).items():
            if canon == 'GND' or canon in rail_names:
                continue
            prov = [b for b, ends in (iface.get('ends') or {}).items()
                    if canon in set((ends or {}).values())]
            if len(prov) < 2 and sig not in (iface.get('off_board') or []):
                out.append(f'[warn] iface {iid}: 信号 {sig}（{canon}）只有 {prov} 提供，且未声明 off_board')

    blocks = sorted(p.name.replace('.kicad_block', '')
                    for p in (base.root / 'PCBStudio.kicad_blocks').glob('*.kicad_block'))
    for bname in blocks:
        b = base.block(bname)
        for r in b.rails:
            try:
                base.port_net(bname, r)
            except KeyError:
                out.append(f'[error] {bname}: 声明了电源轨 {r}，但连线表里既没有这根网、'
                           f'interfaces.json 的 ends 也没给它对照')
        for port in b.ports:
            try:
                base.port_net(bname, port)
            except KeyError:
                out.append(f'[error] {bname}: 端口 {port} 在连线表里找不到对应网')
        for p in b.parts:
            if not p.get('footprint'):
                out.append(f'[error] {bname}: 器件 {p.get("ref")} 没有封装')
        if b.sem.get('geometry_status') == 'extracted':
            f = b.dir / f'{bname}.kicad_pcb'
            if not f.is_file():
                out.append(f'[error] {bname}: geometry_status=extracted 但缺 {f.name}')
        # 连线表的引脚号必须在原理图符号里真实存在（2026-10-27 新加：power_entry 用引脚名当号）
        sch = b.dir / f'{bname}.kicad_sch'
        if sch.is_file():
            pins = _sch_pins(sch)
            if pins is not None:
                for c in b.table['conns']:
                    ref, pin = c['ref'], str(c['pin'])
                    got = pins.get(ref)
                    if got is None:
                        out.append(f'[warn] {bname}: 连线表引用了原理图里没有的器件 {ref}')
                        continue
                    if pin in got:
                        continue
                    named = [num for num, nm in got.items() if nm == pin]
                    hint = (f'（{pin} 是引脚**名** {got[named[0]]!r}，引脚号应为 {named[0]}）'
                            if named else f'（符号只有引脚 {sorted(got)}）')
                    out.append(f'[error] {bname}: 连线表 {ref}.{pin} 不是引脚号{hint}')
    return out
