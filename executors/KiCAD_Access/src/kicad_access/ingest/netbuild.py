"""sch → net 表：并查集连通性抽取 + 全局命名空间合并 + net 分类。

连通语义（对齐 KiCad sch）：
- wire 自身链式连通（顶点两两 union）；
- pin / 标签可落在 wire 顶点**或线段中点**（T 型结）——
  M1-3 校准发现某页 2 个 GND 标签正挂在线段中点；
- junction（实心圆点）让所有穿过该点的 wire 互连；
- 无 junction 的交叉线不连通；
- global label 与电源符号值跨页全局合并；local label 页内命名。
"""

from __future__ import annotations

from ..kicad.schematic import Design, Sheet, SchSymbol
from .ir import DesignIR, DeviceIR, ModuleIR, NetIR, PinRef

# 电源轨识别：电源符号直接挂名；global label 里这些前缀/词也按电源归类
_POWER_HINTS = ("GND", "VCC", "VBUS", "+3V3", "+5V", "+12V", "+24V", "VIN", "VDD", "VSS")

_EPS = 0.01  # mm，坐标吸附容差


def _key(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 3), round(p[1], 3))


def _on_segment(px: float, py: float, w) -> bool:
    """点是否在 wire 线段上（含端点），容差 _EPS。"""
    dx, dy = w.x2 - w.x1, w.y2 - w.y1
    len2 = dx * dx + dy * dy
    if len2 < 1e-12:
        return abs(px - w.x1) <= _EPS and abs(py - w.y1) <= _EPS
    t = ((px - w.x1) * dx + (py - w.y1) * dy) / len2
    if t < -_EPS or t > 1 + _EPS:
        return False
    cx, cy = w.x1 + t * dx, w.y1 + t * dy
    return abs(px - cx) <= _EPS and abs(py - cy) <= _EPS


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple, tuple] = {}

    def find(self, k: tuple) -> tuple:
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: tuple, b: tuple) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_ir(design: Design) -> DesignIR:
    warnings: list[str] = []

    # ---- 1) 器件 / 模块表（去重多 unit 实例，pin 按 pin_numbers 过滤合并）----
    devices: dict[str, DeviceIR] = {}
    modules: dict[str, ModuleIR] = {}
    for mod, sheet in design.modules.items():
        modules[mod] = ModuleIR(name=mod, sheet_file=sheet.path.name, devices=[])
    power_pins: list[tuple[str, str, tuple[float, float], str]] = []  # (mod, ref, pos, value)
    for sym in design.all_symbols():
        if sym.is_power:
            pp = sheet_pins(design, sym)
            for pos in pp.values():
                power_pins.append((sym.sheet, sym.reference, pos, sym.value))
            continue
        entry = devices.get(sym.reference)
        if entry is None:
            entry = DeviceIR(
                ref=sym.reference,
                value=sym.value,
                footprint=sym.footprint,
                lib_id=sym.lib_id,
                module=sym.sheet,
                in_bom=sym.in_bom,
                dnp=sym.dnp,
                at=(sym.x, sym.y),
            )
            devices[sym.reference] = entry
            modules[sym.sheet].devices.append(sym.reference)
        pins = sheet_pins(design, sym)
        if sym.pin_numbers:
            pins = {n: p for n, p in pins.items() if n in set(sym.pin_numbers)}
        entry.pins.update(pins)
        if not entry.footprint and sym.footprint:
            entry.footprint = sym.footprint
    for mod in modules.values():
        mod.devices.sort()
    if not any(d.footprint for d in devices.values()):
        warnings.append("sch 无 Footprint 字段：PCB 交叉核对将无从谈起")

    # ---- 2) 页内连通：每页独立并查集，cluster → (pin 列表, local 标签) ----
    # 全局名空间：name → set(cluster_id)，跨页合并同名的全局 cluster
    global_join: dict[str, set[tuple[str, int]]] = {}
    clusters: dict[tuple[str, int], dict] = {}  # (模块, cid) → {pins, local_names}
    unnamed_seq = 0

    for mod, sheet in design.modules.items():
        uf = _UnionFind()
        # wire 顶点链式 union（KiCad：线-线只在共端点处连通）
        verts: set[tuple[float, float]] = set()
        for w in sheet.wires:
            a, b = _key((w.x1, w.y1)), _key((w.x2, w.y2))
            verts |= {a, b}
            uf.union(a, b)
        # 显式 junction：所有穿过该点的线互连（无 junction 的交叉/中段搭接不连通）
        jkeys: set[tuple[float, float]] = set()
        for jx, jy in sheet.junctions:
            jk = _key((jx, jy))
            jkeys.add(jk)
            # 无 wire 经过的孤立 junction 也注册成节点：attach 循环命中 jkeys
            # 即视为"已在 uf"，若此处不注册，汇总 by_root 查不到会 KeyError
            # （watchy 板 #PWR064 针脚正压孤立 junction 点，2026-10-09 实测）
            uf.find(jk)
            touched = False
            for w in sheet.wires:
                if _on_segment(jx, jy, w):
                    uf.union(jk, _key((w.x1, w.y1)))
                    touched = True
            if not touched:
                warnings.append(f"[{mod}] junction @ ({jx}, {jy}) 无 wire 经过（孤立结点）")
        # pin / 标签附着 + 浮动检测
        # KiCad 连接语义（M1-4 对 oracle 校准）：
        # - pin / 电源符号：只在 wire 端点或 junction 处相连。压线中段不算——
        #   某页 C36/1、C35/2 与另一页 J2/SH 均压中段无 junction，
        #   KiCad 判 unconnected，宽松吸附曾把它们误并进 +3V3；
        # - 标签：线上任意位置均可贴（中段标签合法命名）。
        attach: list[tuple[tuple[float, float], str, str, bool, str]] = []  # (pos, ref, pin, is_global, name)
        for ref, dev in devices.items():
            if dev.module != mod:
                continue
            for num, pos in dev.pins.items():
                attach.append((pos, ref, num, False, ""))
        for pw_mod, _ref, pos, value in power_pins:
            if pw_mod != mod:
                continue
            attach.append((pos, f"#PWR:{value}", "", False, ""))
        for lab in sheet.labels:
            attach.append(((lab.x, lab.y), "", "", lab.global_, lab.name))

        # 同点共挂检测（pin 上直接压标签/电源符号/另一 pin = 已连通，不算孤立）
        attach_count: dict[tuple[float, float], int] = {}
        for pos, *_ in attach:
            attach_count[_key(pos)] = attach_count.get(_key(pos), 0) + 1

        for pos, ref, pin, is_global, name in attach:
            k = _key(pos)
            if ref:
                # 端点/junction 命中时 k 本身已是 uf 节点（wire/junction 循环注册过）
                hit = k in verts or k in jkeys
            else:
                # 标签：贴线任意位置，把锚点 union 到线端点入簇
                hit = False
                for w in sheet.wires:
                    if _on_segment(pos[0], pos[1], w):
                        uf.union(k, _key((w.x1, w.y1)))
                        hit = True
            if not hit:
                uf.find(k)  # 孤立点也成单点 cluster，标签共点时仍参与命名
                if ref and not ref.startswith("#PWR") and attach_count[k] == 1:
                    warnings.append(f"[{mod}] {ref}.{pin} 未接线（孤立 pin）")
                elif not ref and attach_count[k] == 1:
                    warnings.append(f"[{mod}] 标签 {name} 悬空 @ {pos}")

        # 汇总 cluster
        by_root: dict[tuple, int] = {}
        sheet_clusters: dict[int, dict] = {}
        for k in list(uf.parent):
            r = uf.find(k)
            if r not in by_root:
                by_root[r] = len(by_root)
                sheet_clusters[by_root[r]] = {"pins": [], "local": set(), "global": set(), "power": set()}
        for pos, ref, pin, is_global, name in attach:
            k = _key(pos)
            r = uf.find(k)
            cid = by_root[r]
            c = sheet_clusters[cid]
            if ref.startswith("#PWR:"):
                pname = name or ref.split(":", 1)[1]
                if pname != "PWR_FLAG":  # ERC 标记符号：不参与 net 命名/别名
                    c["power"].add(pname)
            elif ref:
                c["pins"].append(PinRef(ref=ref, pin=pin))
            elif is_global:
                c["global"].add(name)
            else:
                c["local"].add(name)

        for cid, c in sheet_clusters.items():
            key = (mod, cid)
            clusters[key] = c
            for g in c["global"]:
                global_join.setdefault(g, set()).add(key)
            for p in c["power"]:
                global_join.setdefault(p, set()).add(key)

    # ---- 3) 跨页合并 → net 命名与分类 ----
    # 同一簇挂多个全局名 = 别名：所有该名的簇整体并成一个 net（对齐 KiCad：
    # 本工程 can 页 2 处簇同挂 GND 与 +3V3，KiCad 据此把 GND 并进 +3V3——
    # 这是 sch 真实缺陷，须按别名合并复现 oracle，同时高声警告）。
    nets: list[NetIR] = []
    consumed: set[tuple] = set()

    # 名字层面的并查集：簇上共现的多个全局名 → 同一别名组
    name_uf = _UnionFind()
    for key, c in clusters.items():
        names = c["global"] | c["power"]
        for a in sorted(names):
            name_uf.union(sorted(names)[0], a)
    alias_members: dict[str, list[tuple]] = {}
    for gname in global_join:
        alias_members.setdefault(name_uf.find(gname), []).extend(global_join[gname])

    def emit(members: list[tuple], name: str) -> NetIR:
        seen: dict[tuple[str, str], PinRef] = {
            (p.ref, p.pin): p for key in members for p in clusters[key]["pins"]
        }
        pins = [seen[k] for k in sorted(seen)]
        mods = sorted({key[0] for key in members})
        kind = "power" if _is_power_name(name) else ("intra" if len(mods) == 1 else "system")
        return NetIR(name=name, pins=pins, modules=mods, kind=kind)

    for root in sorted(alias_members):
        members = sorted(set(alias_members[root]))
        aliases = sorted(n for n in global_join if name_uf.find(n) == root)
        name = aliases[0]  # 主名取字典序最小（本工程 +3V3 < GND，与 KiCad 选名一致）
        if len(aliases) > 1:
            warnings.append(
                f"⚠ net 别名短接: {' ≡ '.join(aliases)}（别名组合并 {len(members)} 簇，"
                f"主名取 {name}；sch 中存在同簇双标签，应修复）"
            )
        nets.append(emit(members, name))
        consumed.update(members)
    # 剩余簇：local 标签名 → N$；单 pin 无名簇 = 未连接 pin（对齐 oracle 的
    # unconnected- 分类，只告警不产 net）
    for key in sorted(clusters):
        if key in consumed:
            continue
        c = clusters[key]
        if c["local"]:
            name = sorted(c["local"])[0]
        elif len(c["pins"]) >= 2:
            unnamed_seq += 1
            name = f"N${unnamed_seq}"
        else:
            continue
        nets.append(emit([key], name))
        consumed.add(key)

    nets.sort(key=lambda n: (n.kind, n.name))
    return DesignIR(
        root_sch=str(design.root_sch),
        modules=modules,
        devices=devices,
        nets=nets,
        warnings=warnings,
    )


def _is_power_name(name: str) -> bool:
    n = name.upper()
    return any(h in n for h in _POWER_HINTS) or n.startswith("+")


def sheet_pins(design: Design, sym: SchSymbol) -> dict[str, tuple[float, float]]:
    """实例 pin 绝对坐标（Design 层转发，module sheet 才持有库 pin 表）。"""
    sheet: Sheet = design.modules.get(sym.sheet) or design.root
    return sheet.pin_positions(sym)
