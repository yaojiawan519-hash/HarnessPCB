#!/usr/bin/env python3
"""语言（`.sdi.yaml`）→ 原样平移拼装计划。

块的图形有三种来源，语言里都只是 `modules: {别名: 块名}`，区别只在块名指向哪儿：

    DesignBlockTools/PCBStudio.kicad_blocks/<块名>.kicad_block/    ← 我们自己的块卡（draw.py）
    defs:<块名>                                                   ← 就地定义（draw.py）
    library: {块名: <某 .kicad_sch 路径>}                          ← 外部人画原理图（本文件）

第三条是这次新加的：Reusable Blocks Common 那类"人家画好的单页原理图"，没有我们的块卡，
直接拿文件平移。语言那边一行都不用改——**电路是什么照旧由 `nets:` 说**，
这里的唯一工作是把它翻成"每个块把哪个标签改成哪个网名"。

由此得到一条真门禁（以前 JSON 计划里没有）：
**语言里写的 `别名.端口`，必须真在这块图上存在**。写错一个字母就报错，
而不是像改名表那样悄悄少改一个标签、让两根网在图上意外并起来。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from . import spec as S

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # DesignBlockTools/
ILTOOLS = ROOT.parent / 'SchematicDesign' / 'il' / 'tools'

# 块图形从哪儿来和语言无关，语言只说"用哪块、怎么连"。
# `surgery:` 是唯一允许改块内电路的地方（删件/改接脚），见 surgery.py。
LANG_KEYS = {'project', 'board', 'library', 'modules', 'nets', 'ifaces', 'require',
             'defs', 'surgery'}


def _extract():
    if str(ILTOOLS) not in sys.path:
        sys.path.insert(0, str(ILTOOLS))
    import extract                                              # noqa: PLC0415
    return extract


def ports_of(il):
    """一个块对外能接的"端口" = 它图上的标签文字 ∪ 电源符号名。

    外部原理图没有我们的 ports 元数据，端口就是人画的那些标签/电源符号；
    改名就是改这些文字，所以"能接的端口"和"能改的名字"必须是同一批。
    """
    extract = __import__('extract')
    out = {}
    for l in il['labels']:
        if l.get('text'):
            out[l['text']] = 'label'
    for s in il['symbols']:
        if not s.get('power'):
            continue
        nm = extract._power_name(s)
        if nm:
            out[nm] = 'power'
    return out


def _resolve(path_like):
    p = Path(path_like)
    if p.is_absolute():
        return p
    for base in (Path.cwd(), ROOT):
        if (base / p).is_file():
            return base / p
    return p


def load(path, verbose=True):
    """返回 (plan, report)。plan 与 kicadblocks 的 JSON 计划同构。"""
    sp = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    problems = [{'level': 'error', 'code': 'key', 'msg': f'语言不认识的顶层键 {k}'}
                for k in sp if k not in LANG_KEYS]

    lib = sp.get('library') or {}
    modules = sp.get('modules') or {}
    nets = sp.get('nets') or {}
    surgery = sp.get('surgery') or {}
    extract = _extract()
    from . import surgery as SG                                    # noqa: PLC0415

    # 端口 → 网名（同一端口出现在两根网里是语言自相矛盾，直接报错）
    port_net, dup = {}, []
    for net, ends in nets.items():
        for e in ends or []:
            alias, _, port = str(e).partition('.')
            if not port:
                problems.append({'level': 'error', 'code': 'endpoint',
                                 'msg': f'{net}: "{e}" 不是"别名.端口"的形式'})
                continue
            if (alias, port) in port_net and port_net[(alias, port)] != net:
                dup.append((alias, port, port_net[(alias, port)], net))
            port_net[(alias, port)] = net
    for a, p, n1, n2 in dup:
        problems.append({'level': 'error', 'code': 'port-twice',
                         'msg': f'{a}.{p} 同时被 {n1} 和 {n2} 引用——一个端口只能属于一根网'})

    blocks, tables, refmap = [], {}, {}
    counters: dict[str, int] = {}
    for alias, bname in modules.items():
        if bname in (sp.get('defs') or {}):
            problems.append({'level': 'error', 'code': 'defs',
                             'msg': f'{alias}: {bname} 是 defs 就地定义模块，走 sdi schematic（draw.py），'
                                    f'这条原样平移路线只吃 library: 外部文件或块卡'})
            continue
        f = S.lib_path(lib.get(bname)) if lib.get(bname) is not None else None
        if not f:
            card = ROOT / 'PCBStudio.kicad_blocks' / f'{bname}.kicad_block' / f'{bname}.kicad_sch'
            if card.is_file():
                f = card
            else:
                problems.append({'level': 'error', 'code': 'block',
                                 'msg': f'{alias}: 块 {bname} 既不在 library: 里，也不是库块卡'})
                continue
        f = _resolve(f)
        if not f.is_file():
            problems.append({'level': 'error', 'code': 'file', 'msg': f'{alias}: 找不到 {f}'})
            continue
        il = extract.extract(str(f))
        # 端口要按**手术后**的图算：`supply:`/`expose:` 会在脚上加一个网标，那个网标
        # 就是新端口；`drop:` 删掉的件不再占位。
        ops = surgery.get(alias) or {}
        if ops:
            SG.apply(il, ops)
        # 位号表必须和展开层（`expand` → design.json）算同一个东西，否则 .kicad_sch 的
        # 位号和 design.json / PCB 的位号错开一位，网表逐 pin 对不上。规则照抄
        # `expand`：按模块声明序、每块按图上符号出现序、每个字母前缀各自从 1 数。
        # 两边都按**手术后**的器件表数（`SpecLibrary.block()` 也已经把 drop 的件去掉了）。
        m = {}
        for s in il['symbols']:
            if s.get('power'):
                continue
            pre = ''.join(ch for ch in s['id'] if ch.isalpha())
            counters[pre] = counters.get(pre, 0) + 1
            m[s['id']] = f'{pre}{counters[pre]}'
        refmap[alias] = m
        ports = ports_of(il)
        tables[alias] = ports
        rename = {}
        for port in ports:
            net = port_net.get((alias, port))
            if net and net != port:
                rename[port] = net
        # 语言点名了这个块的端口，但图上没有 → 拼出来会缺一根网，必须当场报
        for (a, port) in port_net:
            if a == alias and port not in ports:
                problems.append({'level': 'error', 'code': 'port-missing',
                                 'msg': f'{alias}.{port} 在这块图上不存在'
                                        f'（图上有的：{", ".join(sorted(ports))}）'})
        blocks.append({'alias': alias, 'file': str(f), 'rename': rename,
                       'surgery': ops})

    # 只挂一个端点的网 = 板级出口（画出来是一个带网名的标签，等着接到板外去）。
    # 这是正常用法，不算问题——但要看得到，不然"忘了连"会被当成"故意引出"。
    terminals = sorted(n for n, e in nets.items() if len(e or []) < 2)
    for net in terminals:
        if not (nets.get(net) or []):
            problems.append({'level': 'error', 'code': 'empty-net', 'msg': f'{net} 没有端点'})

    for alias in surgery:
        if alias not in modules:
            problems.append({'level': 'error', 'code': 'surgery-alias',
                             'msg': f'surgery.{alias}: 未声明的模块别名'})

    used = {(a, p) for (a, p) in port_net}
    spare = {a: sorted(p for p in ports if (a, p) not in used) for a, ports in tables.items()}
    spare = {a: v for a, v in spare.items() if v}

    # 纸/间距不写进语言：那是几何，由引擎算。`refmap` 是语言给的位号表，
    # `kicadblocks`/`schwrite` 照着它改号，不自己再猜一套。
    plan = {'blocks': blocks, 'refmap': refmap}
    report = {'problems': problems, 'ports': {a: sorted(v) for a, v in tables.items()},
              'spare_ports': spare, 'terminals': terminals,
              'nets': {n: list(e or []) for n, e in nets.items()}}
    if verbose:
        for p in problems:
            print(f'  [{p["level"]}] {p["code"]}: {p["msg"]}')
        for b in blocks:
            print(f'  {b["alias"]:8s} {Path(b["file"]).name:34s} 改名 {len(b["rename"]):2d} 处'
                  f' {b["rename"]}'
                  + (f'  外科 {b["surgery"].get("drop") or "—"}'
                     f' → {b["surgery"].get("supply") or ""}' if b['surgery'] else ''))
    return plan, report
