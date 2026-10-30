#!/usr/bin/env python3
"""模块库读取层：块表 + 接口注册表，回答"这个端口在这个块里是哪根网"。

一条容易搞混的事（本层的全部意义）：**端口名不等于块内网名**。
- `can_node` 的端口 `TXD`，块内网叫 `CAN0_TX`（历史块卡已经规范化过）；
- `rs485_node` 的端口 `TXD`，块内网就叫 `TXD`（未规范化，靠 `ends` 表改名）。
所以解析顺序是：先试 `ends` 里给的目标名在不在块内网里，再退回端口名本身。
2026-10-27 对 10 个块逐个验证过：这条顺序 100% 命中，没有需要人工兜底的端口。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import spec as S


def _resolve(root: Path, path_like) -> Path:
    """`library:` 里的路径可写相对（当前目录 → 库根 → 仓库根，按序找）。

    第三档是给 2026-10-29 的新家用的：块现在住在仓库根 `DesignBlocks/<块名>/`，
    规范里就照实写 `DesignBlocks/mcu_stm32f411/mcu_stm32f411.kicad_sch`，
    不用为了迁就加载器写成 `../DesignBlocks/…`。
    """
    from . import REPO
    p = Path(path_like)
    if p.is_absolute() or p.is_file():
        return p
    for base in (Path.cwd(), Path(root), Path(REPO)):
        if (base / p).is_file():
            return base / p
    return p


class Block:
    """一个块卡：接口契约（semantic）+ 逐 pin 连线表（table）。"""

    def __init__(self, root: Path, name: str, bdir: Path | None = None):
        from . import find_block
        d = Path(bdir) if bdir is not None else find_block(name)
        if d is None:
            raise FileNotFoundError(f'块库里没有 {name}')
        self.name = name
        self.dir = d
        # 2026-10-28：块卡合并后 yaml 是唯一手写权威，两份 json 是产物；
        # 这里经由 `sdi.card` 读，缺卡/缺字段会硬报错（以前是裸 open 崩，或静默跳过）。
        from . import card as _card
        c = _card.load(d)
        self.sem = c.sem
        self.table = c.table
        self.ports = {i['name']: i for i in self.sem['interface']}
        self.rails = list(self.sem['power_rails'])
        self.parts = self.table['parts']
        self.pin_nets: dict[str, list[tuple[str, str]]] = {}
        for c in self.table['conns']:
            self.pin_nets.setdefault(c['net'], []).append((c['ref'], str(c['pin'])))
        self.nets = set(self.pin_nets)

    def __repr__(self) -> str:
        return f'<Block {self.name} {len(self.parts)}p {len(self.nets)}net>'


class Library:
    def __init__(self, root: str | Path | None = None, blocks: str | Path | None = None):
        from . import BLOCKS
        self.root = Path(root or Path(__file__).resolve().parent.parent)
        # 块的家：默认仓库根 `DesignBlocks/`（2026-10-29 起的唯一口径）
        self._explicit_blocks = blocks is not None
        self.blocks_root = Path(blocks) if blocks is not None else BLOCKS
        reg = json.loads((self.root / 'interfaces.json').read_text(encoding='utf-8'))
        self.ifaces: dict[str, dict] = {i['id']: i for i in reg['ifaces']}
        self.stub_patterns = [re.compile(p) for p in reg.get('stub_patterns', [])]
        # 出板信号（端子/连接器那一侧）：这些网只有一块用是正常的
        self.off_board = {
            (i.get('signals') or {}).get(sig)
            for i in reg['ifaces'] for sig in (i.get('off_board') or [])
        } - {None}
        self._blocks: dict[str, Block] = {}
        # iface -> block -> {端口: 规范网名}
        self.iface_ends: dict[str, dict[str, dict[str, str]]] = {
            i: (v.get('ends') or {}) for i, v in self.ifaces.items()
        }

    # ── 块 ────────────────────────────────────────────────────────────────
    def block(self, name: str) -> Block:
        if name not in self._blocks:
            # "库里没这个块"是**普通异常**（调用方按需降级，例如 library: 里给了
            # 外部原理图时去查块卡许可）；而"块在、卡坏了"是 `card.CardError`，
            # 刻意是 SystemExit，穿透所有 except Exception 直接中止。
            if not self.has_block(name):
                raise FileNotFoundError(
                    f'块库里没有 {name}（{self.blocks_root}）')
            self._blocks[name] = Block(self.root, name, self._blockdir(name))
        return self._blocks[name]

    def _blockdir(self, name: str) -> Path:
        from . import find_block
        d = find_block(name, self.blocks_root if self._explicit_blocks else None)
        if d is None:
            raise FileNotFoundError(f'块库里没有 {name}（{self.blocks_root}）')
        return d

    def has_block(self, name: str) -> bool:
        from . import find_block
        return find_block(name, self.blocks_root if self._explicit_blocks else None) is not None

    def raw_block(self, name: str) -> Block:
        """块卡没有外科改图，原图视图就是它自己（见 `SpecLibrary.raw_block`）。"""
        return self.block(name)

    def ends_targets(self, block: str, port: str) -> list[str]:
        out = []
        for ends in self.iface_ends.values():
            m = ends.get(block) or {}
            if port in m and m[port] not in out:
                out.append(m[port])
        return out

    def port_net(self, block: str, port: str) -> str:
        """端口对应的块内网名（见模块 docstring 的解析顺序）。"""
        b = self.block(block)
        for c in self.ends_targets(block, port):
            if c in b.nets:
                return c
        if port in b.nets:
            return port
        raise KeyError(f'{block} 没有端口 {port} 对应的块内网')

    def is_stub(self, name: str) -> bool:
        return any(p.search(name) for p in self.stub_patterns)


class DefBlock:
    """语言里**就地定义**的模块（`defs:` 里那段）——库中还没有，先按定义用起来。

    为什么要有它（2026-10-27 用户加的要求）：模块库不可能先备齐所有东西。语言应该能说
    "我要一个 4 通道 I2C ADC，端口是 SDA/SCL/VDD/GND，器件是 ADS1115"，然后由 AI 照这份
    定义生成 schematic 和 PCB 封装，验收通过后入库成正式块卡。定义里**同样不许写坐标**：
    只写端口、器件、连接、放置先验（anchor/max_mm），几何由引擎造。

    和 `Block` 保持同一个鸭子类型（ports/rails/pin_nets/parts/nets），所以展开层不用分支。
    """

    def __init__(self, name: str, d: dict):
        self.name = name
        self.defn = d
        self.sem = {
            'interface': d.get('ports') or [],
            'power_rails': d.get('rails') or [],
            'placement_prior': d.get('placement_prior') or {},
        }
        self.ports = {i['name']: i for i in (d.get('ports') or [])}
        self.rails = list(d.get('rails') or [])
        self.parts = d.get('parts') or []
        self.conns = d.get('conns') or []
        self.pin_nets: dict[str, list[tuple[str, str]]] = {}
        for c in self.conns:
            self.pin_nets.setdefault(c['net'], []).append((c['ref'], str(c['pin'])))
        self.nets = set(self.pin_nets)

    def __repr__(self) -> str:
        return f'<DefBlock {self.name} {len(self.parts)}p {len(self.nets)}net>'


class ExtBlock:
    """外部人画原理图（`library:` 里登记的 `.kicad_sch`）：没有块卡，接口就是图上那些标签。

    这是我们语言里的第三种块来源（前两种：库块卡、defs 就地定义）。它同样保持
    `Block` 的鸭子类型（ports/rails/nets），所以 lint 一行都不用改。
    **端口 = 图上能改名的那些文字**（标签 + 电源符号名）——外部图没有端口元数据，
    人名什么就只能接什么，这既是约束也是它的好处：写错一个字母就会当场报。
    """

    def __init__(self, name: str, path: str | Path):
        self.name = name
        self.path = Path(path)
        il = _extract(self.path)
        ports, rails = {}, []
        for l in il['labels']:
            if l.get('text'):
                ports[l['text']] = {'name': l['text'], 'kind': 'label'}
        for s in il['symbols']:
            if not s.get('power'):
                continue
            nm = _power_name(s)
            if nm:
                ports[nm] = {'name': nm, 'kind': 'power'}
                if nm not in rails:
                    rails.append(nm)
        # 逐 pin 连线表：外部图没有 table.json，但连通性就在图里，直接抽出来。
        # 有了它，ExtBlock 和 Block 完全同构，lint / 展开 / 门1 都不用为它开分支。
        #
        # 多单元器件（LM393 三个单元共用一个位号）：`extract` 现在按放置符号的
        # `(unit N)` 取脚，各单元的脚号本来就不同（1,2,3 / 5,6,7 / 4,8），
        # 所以 (位号, 脚号) 这个键不会再撞。（2026-10-27 之前这里给重名位号加 `#k`
        # 后缀是因为读脚时没分单元；根因在 `geom.pins_for_unit`，已修。）
        pin_nets: dict[str, list[tuple[str, str]]] = {}
        for n in _build_nets(il):
            if not n.get('name'):
                continue
            pin_nets.setdefault(n['name'], []).extend(
                (a, str(b)) for a, b in n['pins'])
        # 人画的页里到处是**没起名的簇**（SWD 两根线、复位 RC、BOOT0 下拉、LDO 输入…）：
        # 它们是真实的网，只是人在图上没写字。以前这里 `continue` 掉了，等于"IL 不知道
        # 电路里存在这些连接"——和"IL 是电路是什么的权威来源"直接冲突，也让门3 拿语言的
        # 10 组去比图上的 20 组，假红。这里给它们一个块内合成名 `~N`，位置稳定（按脚排序）。
        anon = []
        for n in _build_nets(il):
            if n.get('name'):
                continue
            ps = sorted((a, str(b)) for a, b in n['pins'])
            if len(ps) >= 2:          # 单脚匿名 ≠ 网，就是个没接的脚，别造名字
                anon.append(ps)
        for i, ps in enumerate(sorted(anon)):
            pin_nets[f'~{i}'] = ps
        self.ports = ports
        self.rails = rails
        self.sem = {'interface': list(ports.values()), 'power_rails': rails,
                    'placement_prior': {}}
        self.parts = [{'ref': s['id'], 'lib': s.get('lib') or ''}
                      for s in il['symbols'] if not s.get('power')]
        # 外科改图要能删**电源符号**（比如只给某个仿真源接地的那个 GND）：
        # `parts` 是 BOM 口径（不含电源符号），这里单列一份"图上所有位号"。
        self.all_refs = {s['id'] for s in il['symbols']}
        # 和 `all_refs` 同理：`supply:` 要查"这个位号有没有这个脚"，图上有答案。
        self.ref_pins = {(s['id'], str(n)) for s in il['symbols']
                         for n in (s.get('pins_at') or {})}
        self.pin_nets = pin_nets
        self.nets = set(ports) | set(pin_nets)
        self.exposed: dict[str, list[tuple[str, str]]] = {}   # `surgery.expose` 加出来的端口
        self.dropped: set[str] = set()                        # `surgery.drop` 删掉的位号
        self.named_nets: set[str] = set()                     # `surgery.supply` 直接点名的网

    def drop_refs(self, refs) -> None:
        """外科删件（`surgery.drop`）在**块视图**上也生效：删掉的件不算件，它的脚不算脚。

        以前只有画那张 `.kicad_sch` 的腿会删，展开层照旧把 SPICE 源 V1/V2 算进网表，
        于是 design.json（喂 PCB 的那份）带着图上根本不存在的器件，而门1 又拿它回头
        比块——两边各执一词。删件是"这块板是什么"，所以块视图必须跟着变。
        """
        refs = {str(r) for r in refs}
        if not refs:
            return
        self.dropped |= refs
        self.parts = [p for p in self.parts if str(p.get('ref')) not in refs]
        for nm in list(self.pin_nets):
            kept = [(r, p) for r, p in self.pin_nets[nm] if str(r) not in refs]
            if kept:
                self.pin_nets[nm] = kept
            else:
                del self.pin_nets[nm]
        self.nets = set(self.ports) | set(self.pin_nets)

    def reconnect(self, ref: str, pin: str, net: str) -> None:
        """外科改接脚（`surgery.supply`）：把这个脚从原来的网摘下来，挂到 `net` 上。

        和 `drop_refs` 同理：改接是"这块板是什么"，画那张图的那条腿（`surgery.apply`
        在脚端点上贴网标）改了，展开层（喂给 PCB 的 design.json）也必须跟着改，否则
        网表里这个脚还挂在原来那根网上，和图上逐 pin 对不上——2026-10-27 拿
        usb_tempguard 对网表时抓到的就是这一处（运放 V+ 图上接了 5V，网表里还悬着）。
        """
        ref, pin = str(ref), str(pin)
        for nm in list(self.pin_nets):
            kept = [(r, p) for r, p in self.pin_nets[nm]
                    if not (str(r) == ref and str(p) == pin)]
            if len(kept) == len(self.pin_nets[nm]):
                continue
            if kept:
                self.pin_nets[nm] = kept
            else:
                del self.pin_nets[nm]
        self.pin_nets.setdefault(net, []).append((ref, pin))
        self.named_nets.add(net)
        self.nets = set(self.ports) | set(self.pin_nets)

    def add_port(self, name: str, pins: list[tuple[str, str]]) -> None:
        """加一个"语言提出来"的端口（`surgery.expose`，2026-10-27 四路 RS485）。

        裁出来的块只有原页用过的脚是端口；这块板要多用一路串口，就得把那个空脚提成
        端口。**端口只是个名字**——几何（在脚端点上贴网标）由 `surgery.apply` 算，
        这里只让展开层知道"这个块对外还有这么个口"，两块要对上同一个名字。
        """
        if name in self.ports:
            return
        self.ports[name] = {'name': name, 'kind': 'exposed'}
        self.pin_nets.setdefault(name, []).extend(pins)
        self.nets.add(name)
        # lint 必须照**原图**判"这个脚本来就空着吗""端口名撞不撞"，
        # 而它拿到的是同一个被我们改过的对象——所以把"哪些口是我们自己加的"记下来。
        self.exposed[name] = [(str(r), str(p)) for r, p in pins]

    def __repr__(self) -> str:
        return f'<ExtBlock {self.name} {len(self.ports)}port>'


def _extract(path: Path):
    import sys
    tools = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import extract                                              # noqa: PLC0415
    return extract.extract(str(path))


def _power_name(sym):
    import sys
    tools = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import extract                                              # noqa: PLC0415
    return extract._power_name(sym)


def _build_nets(il):
    import sys
    tools = Path(__file__).resolve().parent.parent.parent / 'SchematicDesign' / 'il' / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import extract                                              # noqa: PLC0415
    return extract.build_nets(il)


class SpecLibrary:
    """`Library` + `defs:` 就地定义 + `library:` 外部原理图的统一视图。

    查名字的顺序：就地定义 → 外部文件 → 库块卡。
    """

    def __init__(self, lib: Library, defs: dict | None = None, ext: dict | None = None,
                 surgery: dict | None = None, modules: dict | None = None):
        self.lib = lib
        self.defs = defs or {}
        # `library:` 有两种写法（纯路径 / `{path, license}`），这里统一成
        # `{名字: {path, license}}`，下游只认 `lib_path()` 和 `lib_tier()`。
        # 许可层级是**板级事实**（内部块不许进公开包），所以归到库视图里一次解完。
        self.ext = {k: {'path': S.lib_path(v),
                        'license': S.lib_tier(v, S.card_license_tier(lib, k))}
                    for k, v in (ext or {}).items()}
        self.ifaces = lib.ifaces
        self.iface_ends = lib.iface_ends
        self.stub_patterns = lib.stub_patterns
        self.off_board = lib.off_board
        self._def_blocks = {k: DefBlock(k, v or {}) for k, v in self.defs.items()}
        self._ext_blocks: dict[str, ExtBlock] = {}
        # `surgery.expose` 要能被**展开层**看见，否则语言里写 `mcu.USART2_TX` 会报
        # "图上没有这个端口"——可它明明是同一份语言刚声明要提出来的。
        # surgery 按**别名**写，这里翻成按**块名**（同一块被几个别名共用时取并集：
        # 端口是块的属性，多开得比少开安全，缺的那侧由几何腿的门禁当场抓）。
        self._expose: dict[str, list[tuple[str, str]]] = {}
        self._drop: dict[str, set[str]] = {}
        self._supply: dict[str, list[tuple[str, str, str]]] = {}
        mods = modules or {}
        for alias, ops in (surgery or {}).items():
            bname = mods.get(alias)
            if not bname or not isinstance(ops, dict):
                continue
            for ref_pin, port in (ops.get('expose') or {}).items():
                ref, _, pin = str(ref_pin).partition('.')
                if ref and pin:
                    self._expose.setdefault(bname, []).append((str(port), ref, pin))
            for ref_pin, net in (ops.get('supply') or {}).items():
                ref, _, pin = str(ref_pin).partition('.')
                if ref and pin:
                    self._supply.setdefault(bname, []).append((ref, pin, str(net)))
            # 同一块被几个别名共用时取并集（删件同理：多删比少删安全，
            # 缺的那侧由几何腿的门禁当场抓）。
            self._drop.setdefault(bname, set()).update(str(r) for r in (ops.get('drop') or []))

    def has_block(self, name: str) -> bool:
        return (name in self._def_blocks or name in self.ext
                or self.lib.has_block(name))

    def block(self, name: str):
        if name in self._def_blocks:
            return self._def_blocks[name]
        if name in self.ext:
            if name not in self._ext_blocks:
                eb = ExtBlock(name, _resolve(self.lib.root, self.ext[name]['path']))
                for ref in sorted(self._drop.get(name, ())):
                    eb.drop_refs([ref])
                for ref, pin, net in self._supply.get(name, []):
                    eb.reconnect(ref, pin, net)
                for port, ref, pin in self._expose.get(name, []):
                    eb.add_port(port, [(ref, pin)])
                self._ext_blocks[name] = eb
            return self._ext_blocks[name]
        return self.lib.block(name)

    def raw_block(self, name: str):
        """**手术前**的块视图。门禁判"位号/引脚存在吗"必须照原图，不然删掉的东西
        就查不到了（`surgery.drop: [V1]` 会被判成"块里没有 V1"）。"""
        if name in self.ext:
            return ExtBlock(name, _resolve(self.lib.root, self.ext[name]['path']))
        return self.block(name)

    def ends_targets(self, block: str, port: str) -> list[str]:
        if block in self._def_blocks or block in self.ext:
            return []           # 就地定义 / 外部文件没有 ends；端口名就是网名
        return self.lib.ends_targets(block, port)

    def port_net(self, block: str, port: str) -> str:
        b = self.block(block)
        if block in self._def_blocks:
            if port in b.nets:
                return port
            raise KeyError(f'就地定义模块 {block} 的端口 {port} 没接线')
        if block in self.ext:
            if port in b.nets:
                return port
            raise KeyError(f'外部模块 {block} 图上没有端口 {port}')
        for c in self.lib.ends_targets(block, port):
            if c in b.nets:
                return c
        if port in b.nets:
            return port
        raise KeyError(f'{block} 没有端口 {port} 对应的块内网')

    def is_stub(self, name: str) -> bool:
        return self.lib.is_stub(name)
