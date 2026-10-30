#!/usr/bin/env python3
"""语言层：读 `.sdi.yaml`、校验结构、跑三条 lint。

三条硬规则（计划书 §3.3，都是为了不让语言烂成坐标表）：
- **schema**：顶层只有九个键，端点必须能在模块接口表里查到，接口必须在注册表里且有对应块的一端。
  `library:` 是第三种块来源（外部人画 `.kicad_sch`，没有块卡），和 `defs:` 平级；
  它的"端口"就是图上那些能改名的标签/电源符号。`surgery:` 是唯一允许改块内电路的地方。
- **lint-1 拒坐标**：`require` 里出现 x/y/at/to/pos/coord/rotation 的**数值**直接报错。
  `at: edge.top` 这种语义说法合法，`at: [30, 4]` 不合法——后者正是历史装配表
  `anchors: [{x: 30.0, y: 4.0}]` 的样子。
- **lint-2 意图必须有量法**：`require` 的每个键必须在 REQUIRE 注册表里，且带着验收函数名。
  写不出量法的意图不收，这是结果八/九白烧两天换来的规矩。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

TOP_KEYS = {'project', 'defs', 'board', 'library', 'modules', 'nets', 'ifaces', 'require',
            'surgery'}
# `board.holes` 是**孔数**，`hole_d` / `hole_edge_offset` 是**孔径与孔心到板边**
# ——d2s 出板框时按这两个数打孔，漏写就悄悄用 4.0/4.0（2026-10-29 补进键表：
# 这两个数原先只在手写的 assembly.json 里，换块板用 M3 就没人记得改）。
BOARD_KEYS = {'layers', 'holes', 'hole_d', 'hole_edge_offset', 'size', 'planes',
              'assembly', 'connectors', 'internal_connectors', 'pours', 'plane_nets'}

# `board.planes`：网 → 内层铜。这是**板级事实**，不是布线时的命令行开关。
# 早先它分居三处（A 轨报告里的 plane_nets、`route_boundary --skip`、人的记性），
# 忘一个就把平面网当信号网交给布线器，永远布不通。写进语言以后 `sdi check` 当场查。
PLANE_LAYER_RE = r'^In(\d+)$'

# `library:` 条目的许可层级。**默认 internal**：块可能是从用户项目板裁的，
# 说不清来源就不许外发——这条默认值就是"用户目录里的项目板绝不进公开语料"
# 的机器形态。`public` 必须显式写。块卡侧认 `provenance.license_tier`，
# 再退回 `provenance.source` 前缀（`own:` = 自己画的）。
LIB_KEYS = {'path', 'license'}
LICENSE_TIERS = ('public', 'internal')
DEFAULT_LICENSE = 'internal'
BANNED_NUMERIC = {'x', 'y', 'at', 'to', 'pos', 'coord', 'coords', 'rotation', 'angle', 'xy'}

# 外科改图（`surgery:`）允许的操作。这条和 `defs:` 一样是"语言能改什么"的白名单：
# 只有删件、改接脚、提端口，没有"挪位置""改朝向"——那些属于画法，不属于电路。
# `expose` 是 2026-10-27 四路 RS485 加的：裁出来的块只有原页用过的脚是端口，
# 这块板要多用几路串口，就得把那些空脚提成端口——同样是"电路是什么"，所以在这张白名单里。
SURGERY_OPS = {'drop', 'supply', 'expose', 'why'}

# 就地定义模块（defs）的字段表。刻意和 block.semantic.json / block.table.json 同构：
# 定义通过门禁后可以直接落成一张块卡，不需要第二种格式。
# 2026-10-28 块卡合并（`sdi/card.py`）：块卡就是 `project.kind: block` 的一份 yaml，
# 一个 `defs:` 条目装原本分居两份 json 的信息，所以这里补齐了 semantic.json 里
# 用到的六个键（category/slots/hot_loop/internal_nets/isolation_boundary/notes）。
# **坐标仍然不在表里**：`parts` 只有 ref/lib/value/footprint，x/y 住 `_geom/measured.json`
# 的 `parts_xy`（原理图坐标；`_geom_design.json` 里那套是归一化后的设计表坐标，不是一回事）。
DEF_KEYS = {'rationale', 'ports', 'rails', 'parts', 'conns', 'placement_prior',
            'build', 'provenance', 'license', 'category', 'slots', 'hot_loop',
            'internal_nets', 'isolation_boundary', 'notes'}
# 端口字段。`exit` 只装**契约**（这个端口从哪个脚出：pin / connector / connector_ref）；
# 出口面 faces/width_mm/… 是从板上量出来的，住 `_geom/measured.json`，不进语言。
# `net` 是**块内网名**：接口名（`name`）与它常常不是一个词（can_node 的 TXD ↔ CAN0_TX），
# 两者相同就省略。`in_semantic: false` 是迁移期标记（只在连线表里出现的端口）。
DEF_PORT_KEYS = {'name', 'net', 'kind', 'class', 'direction', 'diff_pair', 'pin', 'note',
                 'voltage', 'alias', 'exit', 'in_semantic'}
DEF_PART_KEYS = {'ref', 'lib', 'value', 'footprint'}
DEF_CONN_KEYS = {'ref', 'pin', 'net'}
PRIOR_KEYS = {'anchor', 'max_mm', 'rationale', 'toward'}

# 意图注册表：键 → (含义, 验收函数)。验收函数是 M2/M3 里真的会去量的那个东西，
# 这里先登记"谁量、量什么"，没有量法的键不许进语言。
REQUIRE: dict[str, dict[str, tuple[str, str]]] = {
    'placement': {
        'block':     ('对象：模块别名', 'gate: 模块存在且已落位'),
        'at':        ('贴边语义（edge.top/bottom/left/right）', 'gate: 该块连接器焊盘到指定板边的距离'),
        'within':    ('与 of 指定块的最近间距上限 (mm)', 'gate: 两块 bbox 最近间距 ≤ 值'),
        'of':        ('within 的参照块别名', 'gate: ——（within 的参数，不单独计量）'),
        'not_overlap': ('与指定块不许压盖', 'gate: 逐笔图形压盖计数 = 0'),
    },
    'routing': {
        'iface':     ('接口实例（差分对等）', 'gate: 该接口所有信号实测'),
        'pair':      ('差分对的信号名二元组', 'gate: 两线等长差 / 间距实测'),
        'width':     ('线宽下限 (mm)', 'gate: 该网最窄处宽度'),
        'gap':       ('差分间距 (mm)', 'gate: 两线边缘间距实测'),
        'net':       ('网名', 'gate: ——（width/keepout 的宾语）'),
        'min_width': ('线宽下限 (mm)', 'gate: 该网最窄处宽度'),
        'keepout':   ('禁止布线的区域（块别名，取 hot_loop）', 'gate: 区域内铜段计数 = 0'),
    },
    'schematic': {
        'style':     ('画法流派 wire / label', 'gate: 拉线率 wired_frac 落在该流派分布内'),
        'block':     ('对象：模块别名', 'gate: ——（compact 的宾语）'),
        'compact':   ('块内紧凑（不许拉大间距）', 'gate: 块 bbox 面积 / 器件覆盖面积 ≥ 阈值'),
    },
}


@dataclass
class Problem:
    level: str      # error / warn
    code: str
    msg: str

    def __str__(self) -> str:
        return f'[{self.level}] {self.code}: {self.msg}'


def load(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _has_number(v) -> bool:
    if _is_number(v):
        return True
    if isinstance(v, (list, tuple)):
        return any(_has_number(x) for x in v)
    if isinstance(v, dict):
        return any(_has_number(x) for x in v.values())
    return False


def split_endpoint(ep: str) -> tuple[str, str]:
    if not isinstance(ep, str) or '.' not in ep:
        raise ValueError(f'端点要写成「别名.端口」，收到 {ep!r}')
    a, p = ep.split('.', 1)
    return a.strip(), p.strip()


# ── `library:` 的两种写法 ──────────────────────────────────────────────
# 兼容老写法（纯路径字符串）和新写法 `{path, license}`：读路径的地方一律走
# `lib_path()`，判许可的地方走 `lib_tier()`，谁也不许自己去解 dict。
def lib_path(v) -> str:
    """`library:` 某一条的路径（字符串写法或 `{path: …}` 都吃）。"""
    if isinstance(v, dict):
        return str(v.get('path') or '')
    return str(v or '')


def lib_tier(v, card_tier: str | None = None) -> str:
    """`library:` 某一条的许可层级：显式写的 > 块卡上的 > 默认 internal。"""
    if isinstance(v, dict) and v.get('license'):
        return str(v['license'])
    if card_tier in LICENSE_TIERS:
        return card_tier
    return DEFAULT_LICENSE


def card_license_tier(lib, name: str) -> str | None:
    """块卡自己报的许可层级：先看 `provenance.license_tier`，再退回
    `provenance.source` 前缀（`own:` = 我们自己画的 → public，其余一律不动）。"""
    try:
        b = lib.block(name)
    except Exception:                                        # noqa: BLE001
        return None
    sem = getattr(b, 'sem', None) or {}
    pv = sem.get('provenance') or {}
    t = pv.get('license_tier')
    if t in LICENSE_TIERS:
        return str(t)
    src = str(pv.get('source') or '')
    if src.startswith('own:'):
        return 'public'
    if src:
        return 'internal'
    return None


def license_rollup(spec: dict, modules: dict, lib) -> dict:
    """整块板的许可层级 = 所有块来源里最严的那档（有一块 internal，整板就是 internal）。

    这条账要跟着 design.json 走，出公开发行包的那步才有东西可拒。
    """
    sources: dict[str, str] = {}
    for alias, bname in modules.items():
        if bname in (spec.get('defs') or {}):
            d = spec['defs'][bname] or {}
            sources[bname] = str(d.get('license') or 'public')     # 语言里就地写的 = 自己的
        elif bname in (spec.get('library') or {}):
            sources[bname] = lib_tier(spec['library'][bname], card_license_tier(lib, bname))
        else:
            sources[bname] = card_license_tier(lib, bname) or DEFAULT_LICENSE
    tier = 'internal' if any(t != 'public' for t in sources.values()) else 'public'
    return {'tier': tier, 'sources': sources,
            'internal': sorted(n for n, t in sources.items() if t != 'public')}


def lint(spec: dict, lib) -> list[Problem]:
    out: list[Problem] = []

    def err(code, msg):
        out.append(Problem('error', code, msg))

    def warn(code, msg):
        out.append(Problem('warn', code, msg))

    # ── schema：顶层键 ───────────────────────────────────────────────────
    unknown = set(spec) - TOP_KEYS
    if unknown:
        err('schema/keys', f'顶层出现未定义的键 {sorted(unknown)}；只有 {sorted(TOP_KEYS)}')
    for k in ('project', 'modules'):
        if k not in spec:
            err('schema/required', f'缺顶层键 {k}')
    if out:
        return out

    modules: dict[str, str] = spec['modules'] or {}
    if not isinstance(modules, dict) or not modules:
        err('schema/modules', 'modules 必须是非空映射 {别名: 模块名}')
        return out

    # ── board：键表 + `board.assembly`（板级事实）────────────────────────
    out.extend(lint_boardshape(spec))

    # ── defs：就地定义的新模块（库里没有的）─────────────────────────────
    defs = spec.get('defs') or {}
    if not isinstance(defs, dict):
        err('schema/defs', 'defs 必须是映射 {模块名: 定义}')
        defs = {}
    for dname, d in defs.items():
        out.extend(lint_def(dname, d, lib))

    # ── library：外部人画页（顺带把许可层级收进机器可读的一栏）─────────────
    out.extend(lint_library(spec))

    # ── schema：模块存在 ─────────────────────────────────────────────────
    for alias, bname in modules.items():
        if not lib.has_block(bname):
            err('schema/module', f'{alias}: 库里没有模块 {bname}，defs 里也没定义')

    def port_ok(alias: str, port: str, where: str) -> bool:
        bname = modules.get(alias)
        if bname is None:
            err('schema/alias', f'{where}: 未声明的模块别名 {alias}')
            return False
        if not lib.has_block(bname):
            return False
        if port not in lib.block(bname).ports:
            err('schema/port', f'{where}: 模块 {bname}（别名 {alias}）没有端口 {port}')
            return False
        return True

    # ── nets ────────────────────────────────────────────────────────────
    for net, eps in (spec.get('nets') or {}).items():
        if not isinstance(eps, list):
            err('schema/net', f'net {net}: 端点表必须是列表')
            continue
        for ep in eps:
            try:
                a, p = split_endpoint(ep)
            except ValueError as e:
                err('schema/endpoint', f'net {net}: {e}')
                continue
            port_ok(a, p, f'net {net}')

    # ── surgery：外科改图（删件 / 改接脚）────────────────────────────────
    out.extend(lint_surgery(spec, modules, lib))

    # ── ifaces ──────────────────────────────────────────────────────────
    for iid, members in (spec.get('ifaces') or {}).items():
        iface = lib.ifaces.get(iid)
        if iface is None:
            err('schema/iface', f'接口注册表里没有 {iid}；可选 {sorted(lib.ifaces)}')
            continue
        if not isinstance(members, list) or len(members) < 2:
            err('schema/iface', f'iface {iid}: 成员必须 ≥2 个别名')
            continue
        for a in members:
            if a not in modules:
                err('schema/alias', f'iface {iid}: 未声明的模块别名 {a}')
                continue
            bname = modules[a]
            if bname not in (iface.get('ends') or {}):
                err('schema/iface-ends',
                    f'iface {iid}: 模块 {bname} 在 interfaces.json 里没有 ends 定义，'
                    f'展开规则缺失（不许猜）')
        # 信号覆盖：每个信号至少一个成员提供；只有一个成员且未声明 off_board → 孤线
        railish = {'GND'}
        for a in members:
            if modules.get(a) and lib.has_block(modules[a]):
                railish |= set(lib.block(modules[a]).rails)
        for sig, canon in (iface.get('signals') or {}).items():
            if canon in railish:
                continue      # 电源网：靠"同名即同网"自动并，不要求在 ends 里成对登记
            ends_map = iface.get('ends') or {}
            prov = [a for a in members
                    if (modules.get(a) in ends_map
                        and canon in set((ends_map[modules[a]] or {}).values()))]
            if not prov:
                err('schema/iface-signal',
                    f'iface {iid}: 信号 {sig}（{canon}）没有任何成员接')
            elif len(prov) == 1 and sig not in (iface.get('off_board') or []):
                warn('iface/orphan',
                     f'iface {iid}: 信号 {sig}（{canon}）只有 {prov[0]} 一端，且未声明 off_board')

    # ── 电源轨：模块声明的轨要么是地，要么在 nets 里出现 ──────────────────
    rail_users: dict[str, list[str]] = {}
    for alias, bname in modules.items():
        if not lib.has_block(bname):
            continue
        for r in lib.block(bname).rails:
            rail_users.setdefault(r, []).append(alias)
    declared_nets = set(spec.get('nets') or {})
    # 某个轨如果被接口语句接管了（can 的 +5V 由 pwr_vin 改写成 VBUS5V），就不算未声明
    iface_taken = set()
    for iid in (spec.get('ifaces') or {}):
        for bname, ends in ((lib.ifaces.get(iid) or {}).get('ends') or {}).items():
            for port in ends:
                iface_taken.add((bname, port))
    # `GND: [pwr.VRAW-]` 这种写法把 VRAW- 并进了板地，也不算未声明
    merged_away = set()
    for _nm, eps in (spec.get('nets') or {}).items():
        for ep in eps or []:
            try:
                a, p = split_endpoint(ep)
            except ValueError:
                continue
            merged_away.add((modules.get(a), p))
    for r, users in rail_users.items():
        if r == 'GND' or r in declared_nets:
            continue
        if all((modules[u], r) in iface_taken for u in users):
            continue
        if all((modules[u], r) in merged_away for u in users):
            continue
        warn('rail/undeclared',
             f'电源轨 {r}（{users}）未在 nets 里声明，仍按同名自动并网')

    # ── 平面网：`board.planes` 要落在内层，且必须是语言里存在的网 ──────────
    out.extend(lint_planes(spec, modules, lib, set(rail_users)))

    # ── require：lint-1 拒坐标 + lint-2 必须有量法 + 引用存在 ─────────────
    req = spec.get('require') or {}
    for section, items in req.items():
        if section not in REQUIRE:
            err('lint-2/registry', f'require.{section} 不是注册过的意图种类；可选 {sorted(REQUIRE)}')
            continue
        if not isinstance(items, list):
            err('schema/require', f'require.{section} 必须是列表')
            continue
        reg = REQUIRE[section]
        for it in items:
            if not isinstance(it, dict):
                err('schema/require', f'require.{section} 的元素必须是映射，收到 {it!r}')
                continue
            for k, v in it.items():
                if k not in reg:
                    err('lint-2/registry',
                        f'require.{section}.{k} 没有登记量法；先写验收函数再加语法')
                    continue
                if k in BANNED_NUMERIC and _has_number(v):
                    err('lint-1/coords',
                        f'require.{section}.{k} = {v!r} 是坐标/数值写法；'
                        f'改成语义说法（如 edge.top / within: 5），几何由引擎算')
                # 引用存在性
                if k in ('block', 'of') and isinstance(v, str) and v not in modules:
                    err('schema/require-ref', f'require.{section}.{k}: 未声明的模块别名 {v}')
                if k == 'net' and isinstance(v, str) and v not in declared_nets:
                    err('schema/require-ref', f'require.{section}.net: 网 {v} 未在 nets 里声明')
                if k == 'iface' and isinstance(v, str) and v not in (spec.get('ifaces') or {}):
                    err('schema/require-ref', f'require.{section}.iface: 接口 {v} 未在 ifaces 里定义')
                if k == 'pair' and isinstance(v, str) and v not in declared_nets:
                    warn('schema/require-ref', f'require.{section}.pair: {v} 是信号名不是网名，'
                                               f'按所属接口解析')
    return out


def lint_library(spec: dict) -> list[Problem]:
    """`library:` 两条：**结构**（路径在、只认 path/license）和**许可层级**（只认两档）。

    为什么要当场查许可：块来自用户项目板这件事，早先只写在 `block.semantic.json` 的
    一句散文和报告脚注里，语言层根本不知道。拿这种块去打公开发行包，没有任何一步拦得住。
    现在这一栏是机器可读的，`sdi build --public` 会拒。
    """
    out: list[Problem] = []
    ext = spec.get('library') or {}
    if not isinstance(ext, dict):
        return [Problem('error', 'schema/library', 'library 必须是映射 {名字: 路径}')]
    for name, v in ext.items():
        if isinstance(v, dict):
            extra = set(v) - LIB_KEYS
            if extra:
                out.append(Problem('error', 'schema/library',
                                   f'library.{name}: 未定义字段 {sorted(extra)}；只有 {sorted(LIB_KEYS)}'))
            if not lib_path(v):
                out.append(Problem('error', 'schema/library', f'library.{name}: 缺 path'))
            lic = v.get('license')
            if lic is not None and lic not in LICENSE_TIERS:
                out.append(Problem('error', 'license/tier',
                                   f'library.{name}: license={lic!r} 不在 {LICENSE_TIERS} 里'
                                   f'（说不清来源就写 internal）'))
        elif not isinstance(v, str) or not v.strip():
            out.append(Problem('error', 'schema/library',
                               f'library.{name}: 要写成路径字符串或 {{path, license}}'))
    return out


def lint_boardshape(spec: dict) -> list[Problem]:
    """`board:` 的键表 + `board.assembly`（2026-10-28 板级事实搬进语言）。

    为什么要有：`BOARD_KEYS` 以前**只声明没执行**——`expand.to_design` 把 `board` 整份
    原样塞进 design.json，于是 `board:` 下写什么、拼错什么都能一路流到拼板器。现在
    搬进来的是"这块板是多少毫米、嘴朝哪、缝留多少"，写错一个字母的结果是**出来另一块板**，
    而且不报错——正是"规则要进代码路径 + 打印数 + 有门"那条规矩要拦的东西。
    """
    out: list[Problem] = []
    board = spec.get('board')
    if board is None:
        return out
    if not isinstance(board, dict):
        return [Problem('error', 'schema/board', 'board 必须是映射')]
    for k in board:
        if k not in BOARD_KEYS:
            out.append(Problem('error', 'schema/board',
                               f'board.{k}: 不认识的键；可选 {sorted(BOARD_KEYS)}'))
    from . import asmfacts                      # 键表/校验只有一份实现
    for e in asmfacts.check(board.get('assembly') or {}):
        out.append(Problem('error', 'schema/assembly', e))
    # 定尺类数字：层数、孔数、孔径、孔心到边。写错一个 → 出来另一块钢板。
    layers = board.get('layers')
    if layers is not None and (isinstance(layers, bool) or not isinstance(layers, int)
                               or layers < 2):
        out.append(Problem('error', 'schema/board',
                           f'board.layers: 要 >=2 的整数，收到 {layers!r}'))
    holes = board.get('holes')
    if holes is not None and (isinstance(holes, bool) or not isinstance(holes, int)
                              or holes < 0):
        out.append(Problem('error', 'schema/board',
                           f'board.holes: 要 >=0 的整数，收到 {holes!r}'))
    for k in ('hole_d', 'hole_edge_offset'):
        v = board.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            out.append(Problem('error', 'schema/board',
                               f'board.{k}: 要正数 mm，收到 {v!r}'))
    if (board.get('hole_d') or board.get('hole_edge_offset')) and not board.get('holes'):
        out.append(Problem('warn', 'schema/board',
                           'board 写了 hole_d / hole_edge_offset 但 holes 是空的'
                           '——没有一个孔会打出来'))
    # `board.connectors`：哪些件是这块板的"物理边界"。写成 `模块别名.位号`，不收坐标。
    mods = spec.get('modules') or {}
    conns = board.get('connectors')
    if conns is not None:
        if not isinstance(conns, list):
            out.append(Problem('error', 'schema/connectors',
                               'board.connectors 要写成列表（例 [mcu.J3, usb.J1]）'))
            conns = []
        for i, item in enumerate(conns):
            if not isinstance(item, str) or '.' not in item:
                out.append(Problem('error', 'schema/connectors',
                                   f'board.connectors[{i}]: 要写成 `模块别名.位号`，收到 {item!r}'))
                continue
            alias = item.split('.', 1)[0]
            if alias not in mods:
                out.append(Problem('error', 'schema/connectors',
                                   f'board.connectors[{i}]: 模块别名 {alias!r} 不在 modules 里'
                                   f'（有 {sorted(mods)}）'))
    # 矛盾的一对：说"接插件一律朝 E"，却没说哪几只接插件 → 拼板时一个都不会贴边
    if (board.get('assembly') or {}).get('conn_edge') and not conns:
        out.append(Problem('warn', 'schema/connectors',
                           'board.assembly.conn_edge 指定了朝向，但 board.connectors 是空的'
                           '——没有一只接插件会被贴到那条边上'))
    return out


def lint_planes(spec: dict, modules: dict, lib, rails: set[str]) -> list[Problem]:
    """`board.planes: {网: In<k>}` —— 平面网必须真的存在、真的落得进内层。

    四条：网在语言里出现过（`nets` 里、或块的电源轨、或 GND）、层名是 `In<k>`、
    `k` 落在 1..layers-2、一层只放一张平面（两层铜叠同一张平面 = 短路）。
    """
    out: list[Problem] = []
    planes = (spec.get('board') or {}).get('planes')
    if planes is None:
        return out
    if not isinstance(planes, dict):
        return [Problem('error', 'schema/planes', 'board.planes 必须是映射 {网: In<k>}')]
    declared = set(spec.get('nets') or {}) | set(rails) | {'GND'}
    layers = (spec.get('board') or {}).get('layers')
    nlayer = int(layers) if isinstance(layers, int) else None
    used: dict[str, str] = {}
    for net, layer in planes.items():
        if net not in declared:
            out.append(Problem('error', 'schema/planes',
                               f'board.planes.{net}: 语言里没有这根网（nets / 块的电源轨都没有）'))
        if not isinstance(layer, str):
            out.append(Problem('error', 'schema/planes',
                               f'board.planes.{net}: 层名要写成 In1/In2，收到 {layer!r}'))
            continue
        m = re.match(PLANE_LAYER_RE, layer.strip())
        if not m:
            out.append(Problem('error', 'schema/planes',
                               f'board.planes.{net}: {layer!r} 不是内层名（In1/In2…）；'
                               f'平面只铺内层铜'))
            continue
        k = int(m.group(1))
        if k < 1:
            out.append(Problem('error', 'schema/planes',
                               f'board.planes.{net}: {layer} 是外层，平面只铺内层'))
        if nlayer is not None and k > nlayer - 2:
            out.append(Problem('error', 'schema/planes',
                               f'board.planes.{net}: 板是 {nlayer} 层，内层只有 In1..In{nlayer - 2}'))
        if layer in used:
            out.append(Problem('error', 'schema/planes',
                               f'board.planes: {used[layer]} 和 {net} 都想铺 {layer} 一层'))
        used[layer] = net
    return out


def lint_surgery(spec: dict, modules: dict, lib) -> list[Problem]:
    """`surgery:` 的门禁：只准删件和改接脚，而且件必须在、网必须已经声明。

    为什么单独一条：外科改图是**唯一**允许改块内电路的地方，它一旦写错，错的是
    "这块板是什么"——比画错一根线严重得多。所以三条都当场查：
    件在不在（位号写错 → 你以为删了其实没删）、脚在不在、目标网在不在 `nets:` 里
    （接一根语言里不存在的网，等于偷偷加了一根网）。
    """
    out: list[Problem] = []

    def err(code, msg):
        out.append(Problem('error', code, msg))

    surg = spec.get('surgery')
    if surg is None:
        return out
    if not isinstance(surg, dict):
        err('schema/surgery', 'surgery 必须是映射 {块别名: {drop: [...], supply: {...}}}')
        return out

    declared_nets = set(spec.get('nets') or {})
    for alias, ops in surg.items():
        if alias not in modules:
            err('schema/surgery', f'surgery.{alias}: 未声明的模块别名；可选 {sorted(modules)}')
            continue
        bname = modules[alias]
        if not lib.has_block(bname):
            continue                       # 块本身不存在，前面已经报过
        if not isinstance(ops, dict):
            err('schema/surgery', f'surgery.{alias}: 必须是映射')
            continue
        extra = set(ops) - SURGERY_OPS
        if extra:
            err('schema/surgery', f'surgery.{alias}: 未定义的操作 {sorted(extra)}；'
                                  f'只有 {sorted(SURGERY_OPS)}（坐标/朝向属于画法，不许写在这里）')
        for k, v in ops.items():
            if _has_number(v):
                err('lint-1/coords', f'surgery.{alias}.{k} = {v!r} 里带数值；'
                                     f'外科改图只认位号/引脚号/网名')

        # 能删的位号 = BOM 器件 ∪ 图上所有符号（电源符号不在 BOM 里，但删得掉）。
        # 用**手术前**的块视图判：`lib.block()` 已经把我们自己删掉的东西去掉了，
        # 拿它判会把每一句 `drop` 都判成"块里没这个位号"。
        blk = lib.raw_block(bname)
        refs = {str(p.get('ref')) for p in (getattr(blk, 'parts', None) or [])}
        refs |= {str(r) for r in (getattr(blk, 'all_refs', None) or set())}
        for r in (ops.get('drop') or []):
            if str(r) not in refs:
                err('schema/surgery', f'surgery.{alias}.drop: 块 {bname} 里没有位号 {r}'
                                      f'（有 {", ".join(sorted(refs))}）')
        for ref_pin, net in (ops.get('supply') or {}).items():
            ref, _, pin = str(ref_pin).partition('.')
            if not pin:
                err('schema/surgery', f'surgery.{alias}.supply: "{ref_pin}" 要写成「位号.引脚号」')
                continue
            if ref not in refs:
                err('schema/surgery', f'surgery.{alias}.supply: 块 {bname} 里没有位号 {ref}')
            elif (blk.ref_pins if hasattr(blk, 'ref_pins') else set()) \
                    and (ref, pin) not in blk.ref_pins:
                err('schema/surgery', f'surgery.{alias}.supply: {ref} 没有引脚 {pin}'
                                      f'（有 {sorted(p for r, p in blk.ref_pins if r == ref)}）')
            if str(net) not in declared_nets:
                err('schema/surgery', f'surgery.{alias}.supply: 网 {net} 没在 nets: 里声明'
                                      f'——改接必须有目标，不许接一根语言里不存在的网')

        # `expose`：把块里一个空脚提成新端口。比 supply 多查三件事——
        # **脚必须是空的**（本来接着东西的脚不叫"提端口"，那是改接，该写 supply；
        # 混着写会让一根网被两个标签并起来）、**端口名不能撞**（块图上已有同名标签，
        # 或者两个 expose 抢同一个名字）、**名字要在 nets: 里出现**（端口是给语言用的，
        # 起了名却不接线，等于偷偷加了一根网）。
        # 这条 lint 拿到的是**已经被 expose 改过**的块对象（SpecLibrary 建库时就加了端口），
        # 所以判"脚空不空""端口撞不撞"必须先把我们加的那部分减掉，否则每一句 expose
        # 都会被自己加出来的东西判成错。
        mine = dict(getattr(blk, 'exposed', None) or {})
        taken = {(str(r), str(p)) for r, p in _conn_pins(blk, skip=set(mine))}
        endpoints = {str(e) for eps in (spec.get('nets') or {}).values() for e in (eps or [])}
        seen_ports: dict[str, str] = {}
        if ops.get('expose') and not isinstance(ops.get('expose'), dict):
            err('schema/surgery', f'surgery.{alias}.expose: 必须是映射 {{位号.脚号: 端口名}}')
            continue
        for ref_pin, port in (ops.get('expose') or {}).items():
            ref, _, pin = str(ref_pin).partition('.')
            if not pin:
                err('schema/surgery', f'surgery.{alias}.expose: "{ref_pin}" 要写成「位号.引脚号」')
                continue
            if ref not in refs:
                err('schema/surgery', f'surgery.{alias}.expose: 块 {bname} 里没有位号 {ref}')
            elif (blk.ref_pins if hasattr(blk, 'ref_pins') else set()) \
                    and (ref, pin) not in blk.ref_pins:
                err('schema/surgery', f'surgery.{alias}.expose: {ref} 没有引脚 {pin}'
                                      f'（有 {sorted(p for r, p in blk.ref_pins if r == ref)}）')
            elif (ref, pin) in taken:
                err('schema/surgery', f'surgery.{alias}.expose: {ref}.{pin} 已经接着别的网了，'
                                      f'不能当新端口（改接请用 supply）')
            if str(port) in getattr(blk, 'ports', {}) and str(port) not in mine:
                err('schema/surgery', f'surgery.{alias}.expose: 端口 {port} 块图上已经有同名标签，'
                                      f'直接用即可，不用 expose')
            if str(port) in seen_ports:
                err('schema/surgery', f'surgery.{alias}.expose: 端口 {port} 被 '
                                      f'{seen_ports[str(port)]} 和 {ref_pin} 同时占用')
            seen_ports[str(port)] = str(ref_pin)
            # 端口是给语言用的：语言里必须有一条 `nets:` 端点写着 `别名.端口`，
            # 否则它只是个没归属的名字（偷偷多出来的网）。
            if f'{alias}.{port}' not in endpoints:
                err('schema/surgery', f'surgery.{alias}.expose: 端口 {port} 在 nets: 里没有被用到'
                                      f'——起了名的端口必须有归属的网（先写一句 '
                                      f'`{port}: [{alias}.{port}, …]`）')
    return out


def _conn_pins(blk, skip=frozenset()):
    """块里**接着东西**的 (位号, 脚号)：那些脚不能拿来 expose。

    `skip` 里的网名是本次 expose 自己加出来的，判"原本空不空"时要减掉。
    """
    out = []
    for net, pins in (getattr(blk, 'pin_nets', None) or {}).items():
        if net == 'NC' or net in skip:
            continue
        out += [(str(r), str(p)) for r, p in pins]
    return out


def lint_def(dname: str, d: dict, lib) -> list[Problem]:
    """就地定义模块的门禁：这份定义够不够拿来生成 schematic + footprint。

    AI 会照它造几何，所以这里查的是**造得出来吗**，不是"好不好看"：
    端口有没有接上、器件有没有封装、引脚有没有串线、有没有偷偷写坐标。
    """
    out: list[Problem] = []

    def err(code, msg):
        out.append(Problem('error', code, msg))

    def warn(code, msg):
        out.append(Problem('warn', code, msg))

    base = getattr(lib, 'lib', lib)
    if base.has_block(dname):
        warn('defs/duplicate', f'defs.{dname}: 库里已经有同名块卡，直接用库里的即可；'
                               f'就地定义会把库里的版本遮住')
    if not isinstance(d, dict):
        err('schema/defs', f'defs.{dname} 必须是映射')
        return out
    unknown = set(d) - DEF_KEYS
    if unknown:
        err('schema/defs', f'defs.{dname}: 未定义字段 {sorted(unknown)}；只有 {sorted(DEF_KEYS)}')
    if not d.get('rationale'):
        warn('defs/rationale', f'defs.{dname}: 没写 rationale（这块解决什么问题），'
                               f'评审时没法判断选型是否合理')

    ports = d.get('ports') or []
    if not ports:
        err('defs/ports', f'defs.{dname}: 至少要有一个端口')
    names = []
    for p in ports:
        if not isinstance(p, dict) or 'name' not in p:
            err('defs/ports', f'defs.{dname}: 端口要写成 {{name: ...}}，收到 {p!r}')
            continue
        names.append(p['name'])
        extra = set(p) - DEF_PORT_KEYS
        if extra:
            err('defs/ports', f'defs.{dname}.{p["name"]}: 端口字段 {sorted(extra)} 未定义')
    if len(set(names)) != len(names):
        err('defs/ports', f'defs.{dname}: 端口名重复 {names}')

    rails = d.get('rails') or []
    if not isinstance(rails, list):
        err('defs/rails', f'defs.{dname}: rails 必须是列表')

    parts = d.get('parts') or []
    if not parts:
        err('defs/parts', f'defs.{dname}: 没有器件，造不出 schematic')
    refs = []
    for p in parts:
        if not isinstance(p, dict):
            err('defs/parts', f'defs.{dname}: 器件要写成映射，收到 {p!r}')
            continue
        extra = set(p) - DEF_PART_KEYS
        if extra:
            err('defs/parts', f'defs.{dname}: 器件字段 {sorted(extra)} 未定义')
        for k in ('ref', 'lib', 'footprint'):
            if not p.get(k):
                err('defs/parts', f'defs.{dname}: 器件 {p.get("ref", "?")} 缺 {k}'
                                  + ('（没有封装就生成不了 PCB）' if k == 'footprint' else ''))
        refs.append(p.get('ref'))

    conns = d.get('conns') or []
    if not conns:
        err('defs/conns', f'defs.{dname}: 没有连接表，造不出 schematic')
    seen_pin = set()
    nets = set()
    for c in conns:
        if not isinstance(c, dict):
            err('defs/conns', f'defs.{dname}: 连接要写成映射，收到 {c!r}')
            continue
        extra = set(c) - DEF_CONN_KEYS
        if extra:
            err('defs/conns', f'defs.{dname}: 连接字段 {sorted(extra)} 未定义')
        ref, pin, net = c.get('ref'), c.get('pin'), c.get('net')
        if ref not in refs:
            err('defs/conns', f'defs.{dname}: 连接引用了不存在的器件 {ref}')
        key = (ref, str(pin))
        if key in seen_pin:
            err('defs/conns', f'defs.{dname}: {ref}.{pin} 接了两次（一个引脚只能在一根网上）')
        seen_pin.add(key)
        nets.add(net)

    for p in names:
        if p not in nets:
            err('defs/ports', f'defs.{dname}: 端口 {p} 没在任何连接里出现（悬空端口）')
    for r in rails:
        if r not in nets:
            err('defs/rails', f'defs.{dname}: 电源轨 {r} 没在任何连接里出现')

    prior = d.get('placement_prior') or {}
    if prior:
        extra = set(prior) - PRIOR_KEYS
        if extra:
            err('defs/placement_prior', f'defs.{dname}: 先验字段 {sorted(extra)} 未定义；'
                                        f'只允许语义说法（anchor/max_mm/toward），不许坐标')
    return out
