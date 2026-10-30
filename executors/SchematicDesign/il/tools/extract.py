#!/usr/bin/env python3
"""kicad_sch -> sdi.v2 (geometry IR + 可检索的样本元信息)。**整个流程里只有这个文件读 .kicad_sch。**

sdi.v1 = sdi.v0 + 三样让 IL 自足的东西：

  symbols[*].pins_at   每个引脚在图纸上的绝对坐标（v0 只有引脚编号）
  libsym               用到的符号图形（引脚位置 + 矩形/折线/圆/弧），渲染要靠它
  nets                 网表：谁和谁连（v0 里根本没有）

有了这三样，下游（切块 / 拼装 / 走线 / 渲染 / 体检）不需要再回头读原文件；
任何改动之后调用 refresh(il) 就能把 pins_at 和 nets 重新算回一致状态。
"""
import json, math, os, re, sys

import geom
from sexpr import parse, H, find, one, at, nums  # noqa: F401  （对外沿用 extract.parse 等旧接口）

SCHEMA = 'sdi.v2'


# ---------- 缺库符号的补借 ----------
# 子图常常只用了全局标签，自己的符号库里没有 power:* 的定义；
# 但 GND / 电源符号的**图形**是标准库里的公共事实，不属于"这一页画了什么"，
# 所以从同一批人工页里借一份过来（找不到精确名就按形状类别借）。
LIB_DONORS = ()
PWR_ALIAS = {'+3V3': '+3.3V', '3V3': '+3.3V', 'VDD': '+3.3V', 'VCC': '+5V',
             'P5V': '+5V', 'VBUS': '+5V', 'VIN': '+5V', '5V': '+5V',
             'AGND': 'GND', 'GNDA': 'GND', 'PGND': 'GND', '_GND': 'GND'}


def lib_ids_used(root):
    out = set()

    def walk(n):
        for c in n[1:]:
            if not isinstance(c, list):
                continue
            if H(c) == 'lib_id' and len(c) > 1:
                out.add(c[1])
            walk(c)

    walk(root)
    return out


def power_libs(root):
    """库定义里带 `(power)` 的符号 = 电源符号（KiCad 原生标记）。

    这比猜库名前缀可靠：第三方板的电源符号在自己的库里（antmicropower:+1V8、
    nordic:VDD…），靠 `power:` 前缀认不出来，于是 8 个 GND 会算成 8 张互不相连的网。
    连两道硬门都发现不了——两边用的是同一个错模型，门是绿的但什么也没证明。
    """
    out = set()
    libs = one(root, 'lib_symbols')
    if not libs:
        return out
    for sym in find(libs, 'symbol'):
        if len(sym) > 1 and any(isinstance(c, list) and H(c) == 'power' for c in sym[1:]):
            out.add(sym[1])
    return out


def power_pin_names(root):
    """库符号 → 它那只电源脚的名字。**KiCad 用它定全局网名，不看 Value 字段。**

    2026-10-30 实测（BP5 `bpu-current_limiter` 页）：rescue 库里的
    `BusPirate-5-rev10-rescue:+3.3V-power`，实例 Value 写 `+3.3V`、电源脚名写 `+3V3`，
    `kicad-cli sch export netlist` 出来那张网叫 **`+3V3`**。IL 按 Value 起名，块卡里
    就多出一条**板上不存在的轨** `+3.3V`——`sdi.cardpcb` 门⑤（轨名板上要有）当场红。
    平时 Value 和脚名一致（`power:GND` 两边都是 `GND`、Altium 导入的
    `GND_POWER_GROUND` 也是 `GND`），所以这个坑只在 rescue/改过名的符号上冒出来。
    """
    out = {}
    libs = one(root, 'lib_symbols')
    if not libs:
        return out

    def first_pin_name(n):
        for c in n[1:]:
            if not isinstance(c, list):
                continue
            if H(c) == 'pin':
                pn = one(c, 'name')
                # `~` 是 KiCad 的"这个名字是空的"标记（`power:+3.3V` 这些标准符号的
                # 电源脚就写成 `~`，名字全靠 Value）。当成网名会把整页的 3.3V 并成 `~`。
                if pn and len(pn) > 1 and isinstance(pn[1], str) and pn[1] not in ('~', ''):
                    return pn[1]
            got = first_pin_name(c)
            if got:
                return got
        return None

    for sym in find(libs, 'symbol'):
        if not (len(sym) > 1 and isinstance(sym[1], str) and ':' in sym[1]):
            continue
        nm = first_pin_name(sym)
        if nm:
            out[sym[1]] = nm
    return out


# 只起"标记"作用的电源符号：不命名网、也不把网标成电源
NO_NAME_POWER = {'PWR_FLAG'}


def _libsym_from_root(root):
    """S 表达式库 → {lib: {'pins': {num: [x,y]}, 'units': {单元: {num: [x,y]}}, 'prims': [...]}}。

    `pins` 是所有单元的并集（画图/概览用）；**算某个放置实例的脚必须走 `units`**，
    见 `geom.pins_for_unit` 的说明（多单元器件按并集取脚会造出幽灵脚）。
    """
    pins = geom.lib_pins(root)
    units = geom.lib_pins_units(root)
    hp = geom.lib_hidden_power(root)
    prims = geom.lib_geometry(root)
    out = {}
    for name, pr in prims.items():
        out[name] = {'pins': {k: [v[0], v[1]] for k, v in (pins.get(name) or {}).items()},
                     'units': {str(u): {k: [v[0], v[1]] for k, v in pm.items()}
                               for u, pm in (units.get(name) or {}).items()},
                     'hidden_power': {str(u): dict(pm) for u, pm in (hp.get(name) or {}).items()},
                     'prims': pr}
    for name, pm in pins.items():           # 只有引脚没有图形的库也要收
        out.setdefault(name, {'pins': {k: [v[0], v[1]] for k, v in pm.items()},
                              'units': {str(u): {k: [v[0], v[1]] for k, v in m.items()}
                                        for u, m in (units.get(name) or {}).items()},
                              'hidden_power': {str(u): dict(pm)
                                               for u, pm in (hp.get(name) or {}).items()},
                              'prims': []})
    return out


def libsym_for(il, lib):
    """按实例的 `lib_id` 找 `libsym` 里的库符号——**键的三种写法都要认**。

    KiCad 6+ 的正常写法是 `库:符号`（`Device:R`），`lib_symbols` 的键也是它，直接命中。
    但**老工程转出来的页**（KiCad 5 缓存库 → 8）会把键写成**裸名 + `_<n>` 后缀**
    （`MMBT3904_1`），而实例的 `lib_id` 还写着 `Transistor_BJT:MMBT3904`——两边对不上。

    2026-10-30 在 Bus Pirate 5 的 vpp-maker 上实测：4 个 MMBT3904/3906 因此 `pins_at`
    全空、IL 里一根脚都没有 → 原理图侧少 3 个管脚 → sch↔pcb 电路一致性门冤枉判"两边不是
    一个电路"（PCB 侧 Q4.1/2/3 齐全，原理图侧一只都看不到）。**找不到就静默给空字典**，
    正是这类"少几个脚也照跑"的病根，所以这里退一步再找，还找不到才算真缺。
    """
    g = il.get('libsym') or {}
    if lib in g:
        return g[lib]
    base = str(lib).split(':')[-1]
    if base in g:
        return g[base]
    # 裸名带后缀：`MMBT3904` → `MMBT3904_1`（有多个同名就取排序第一个，并记在 meta 里靠人核）
    cands = sorted(k for k in g
                   if str(k).split(':')[-1] == base
                   or re.match(re.escape(base) + r'_\d+$', str(k).split(':')[-1]))
    return g[cands[0]] if cands else {}


def borrow_libsym(il, donors=None, extra=()):
    """把 IL 里缺的符号定义从人工页补进来（就地修改，返回补了哪些）。"""
    ds = LIB_DONORS if donors is None else donors
    want = ({s['lib'] for s in il['symbols'] if s.get('lib')} |
            set(extra)) - set(il['libsym'])
    if not want or not ds:
        return set()
    pool = {}
    for d in ds:
        try:
            droot = parse(open(d, encoding='utf-8', errors='replace').read())
        except OSError:
            continue
        for name, g in _libsym_from_root(droot).items():
            pool.setdefault(name, g)
    got = set()
    for name in sorted(want):
        tgt = name
        if tgt not in pool and name.startswith('power:'):
            base = PWR_ALIAS.get(name.split(':', 1)[1], name.split(':', 1)[1])
            tgt = f'power:{base}'
        if tgt in pool:
            il['libsym'][name] = json.loads(json.dumps(pool[tgt]))
            got.add(name)
    return got


# ---------- 语法小工具（沿用 v0 的取字段习惯） ----------

def hidden(p):
    if len(p) > 3 and any(isinstance(c, list) and H(c) == 'hide' and c[1:2] == ['yes'] for c in p[1:]):
        return True
    e = one(p, 'effects')
    return bool(e and any(isinstance(c, list) and H(c) == 'hide' and c[1:2] == ['yes'] for c in e[1:]))


def is_inst(n):
    """页面上的器件实例：`(symbol … (lib_id "库:符号") …)`。

    ⚠ `lib_id` **不一定排在第一个孩子**。KiCad 给"在本工程里改过缓存副本"的符号会
    先写一行 `(lib_name "…")`，于是节点变成 `(symbol (lib_name …) (lib_id …) …)`。
    早先这里判的是 `n[1]` 就是 lib_id，结果这类符号被**静默跳过**：2026-10-29 的
    stm32_Example 页上就吞了 9 个（C11–C15、R5–R7、C6），门① 却报"覆盖 ✓"。
    """
    return H(n) == 'symbol' and any(isinstance(c, list) and H(c) == 'lib_id' for c in n[1:])


def _field_value(sym_node):
    for p in find(sym_node, 'property'):
        if len(p) > 2 and p[1] == 'Value':
            return p[2]
    return None


def _instance_ref(sym_node):
    """符号**实例表**里的位号（只在 `property "Reference"` 未标注时兜底用）。

    KiCad 7/8 的页把位号写两处：`(property "Reference" ...)` 是**缓存**，
    `(instances (project … (path "/…/…" (reference "LED719"))))` 才是**按图纸实例的真值**。
    2026-10-30 实测 BP5 `leds` 页：十颗 SK6821-mini-e 的缓存全是 `LED?`（kicad-cli 也报
    "annotation errors"），实例表里却写着 LED702…LED719。**只读缓存，这十颗灯在 IL 里就是
    十个一模一样的 `LED?`**——块能裁出来，却对不上板上的封装（`pcbcarve` 按位号找件）。

    多实例的页（同一页被两张图纸引用）会给出**两个不同**的位号，实测全语料 7 只
    （`micro-rp2040` 的 J202/#PWR0128 之类）。这份兜底**不碰它们**——缓存非 `?` 时优先用缓存，
    所以这张网的行为跟以前一模一样。取不到非 `?` 的位号就返回 None（宁可留着 `LED?` 报错）。
    """
    inst = one(sym_node, 'instances')
    if not inst:
        return None
    paths = []                       # `find` 只认直接子节点：path 藏在 instances → project 下面
    for proj in find(inst, 'project'):
        paths += find(proj, 'path')
    best = None                      # (深度, ref)
    for p in paths:
        if len(p) < 2 or not isinstance(p[1], str):
            continue
        depth = p[1].count('/')
        for r in find(p, 'reference'):
            if len(r) > 1 and isinstance(r[1], str) and r[1] and '?' not in r[1]:
                if best is None or depth > best[0]:
                    best = (depth, r[1])
    return best[1] if best else None


# ---------- 转换 ----------

def extract(path):
    root = parse(open(path, encoding='utf-8', errors='replace').read())
    pwlibs = power_libs(root)
    pwpin = power_pin_names(root)
    paper = one(root, 'paper')
    il = {'schema': SCHEMA,
          'page': {'paper': paper[1] if paper else None},
          'symbols': [], 'wires': [], 'junctions': [], 'labels': [],
          'no_connects': [], 'texts': [],
          'libsym': _libsym_from_root(root), 'nets': []}
    for n in root[1:]:
        if not isinstance(n, list):
            continue
        h = H(n)
        if is_inst(n):
            lib = one(n, 'lib_id')[1]
            flds = {}
            ref = None
            val_raw = None            # Value 的**文字**，不管它藏没藏
            for p in find(n, 'property'):
                if len(p) < 3:
                    continue
                nm, val = p[1], p[2]
                if nm == 'Reference':
                    ref = val
                if nm == 'Value' and val:
                    val_raw = val
                if not hidden(p) and val not in ('',):
                    flds[nm] = {'text': val, 'at': at(p)}
            a = at(n) or [0, 0, 0]
            if not ref or '?' in ref:      # 缓存没标注 → 去实例表里取真值（见 `_instance_ref`）
                ref = _instance_ref(n) or ref
            mir = one(n, 'mirror')
            mir = mir[1] if mir else None
            u = one(n, 'unit')
            try:
                unit = int(u[1]) if u else 1
            except (TypeError, ValueError, IndexError):
                unit = 1
            il['symbols'].append({
                'id': ref, 'lib': lib, 'at': a,
                'rot': a[2] if len(a) > 2 else 0,
                'mirror': mir,
                'unit': unit,          # 多单元器件：这个实例是第几个单元（缺省 1）
                'fields': flds,
                'value': val_raw,      # 藏起来的 Value 也要（电源符号靠它起网名）
                'pins': [p[1] for p in find(n, 'pin') if len(p) > 1 and isinstance(p[1], str)],
                'pins_at': {}, 'power': lib.startswith('power:') or lib in pwlibs,
                'power_pin': pwpin.get(lib),   # 电源脚名——**KiCad 拿它当网名**（见 power_pin_names）
                'hidden_power': {}})           # 隐藏 power_in 脚 {脚号: 脚名}（见 `_hidden_power`）
        elif h == 'wire':
            pts = one(n, 'pts')
            if pts:
                xy = [[float(p[1]), float(p[2])] for p in find(pts, 'xy')]
                if len(xy) >= 2:
                    il['wires'].append({'pts': xy})
        elif h == 'junction':
            il['junctions'].append(at(n))
        elif h in ('label', 'global_label', 'hierarchical_label'):
            it = {'kind': {'label': 'local', 'global_label': 'global',
                           'hierarchical_label': 'hier'}[h],
                  'text': n[1], 'at': at(n)}
            sh = one(n, 'shape')
            if sh:
                it['shape'] = sh[1]
            # 字号/对齐也记下来：外科新贴的网标要长得和这块图自己的标签一模一样
            # （2026-10-27 用户："添加的标号很丑"——新标签按 1.27 写，原图是 0.61）。
            eff = one(n, 'effects')
            if eff:
                fnt = one(eff, 'font')
                sz = one(fnt, 'size') if fnt else None
                if sz and len(sz) > 2:
                    try:
                        it['size'] = float(sz[1])
                    except (TypeError, ValueError):
                        pass
                jst = one(eff, 'justify')
                if jst and len(jst) > 1:
                    it['justify'] = [str(x) for x in jst[1:]]
            il['labels'].append(it)
        elif h == 'no_connect':
            il['no_connects'].append(at(n))
        elif h == 'text':
            il['texts'].append({'text': n[1], 'at': at(n)})
    refresh(il)
    annotate(il, path)
    return il


# ---------- 元信息与约束：这页是什么，能被谁选中当参考 ----------
# sdi.v2 加的两块不是给画图用的，是给"检索样本"用的：以后要从语料里挑一页当参考，
# 得能按题目（title/description）、画法（school）、规模（器件/网数）、电气事实（电源轨）
# 去筛。这些都是**从页里读出来的硬事实**，不是我们的判断。

GROUND_NAMES = {'GND', 'GNDA', 'AGND', 'PGND', '0V', 'VSS', 'VSSA', 'GNDD', 'EARTH', '_GND'}

# 功能画像：按网名认这页涉及哪些电路族。**这是按名字猜的线索，不是语义理解** ——
# 所以每条都带命中的网名样本，一眼能看出是不是猜错（比如把 DE/RE 认成 RS485）。
# 顺序即优先级：一根网只算第一个命中的族。
FAMILY_KEYS = [
    ('USB',        ('USB', 'VBUS', 'USB_DP', 'USB_DM')),
    ('RS485',      ('RS485', 'RS-485', '485')),
    ('CAN',        ('CAN_', 'CANTX', 'CANRX', 'CANH', 'CANL')),
    ('以太网',      ('ETH', 'RGMII', 'RMII', 'MDIO', 'MDC', 'TX_P', 'TX_N', 'RX_P', 'RX_N', 'PHY')),
    ('I2C',        ('I2C', 'SCL', 'SDA')),
    ('SPI',        ('SPI', 'SCK', 'MISO', 'MOSI')),
    ('UART',       ('UART', 'TXD', 'RXD')),
    ('调试/复位',   ('SWD', 'SWCLK', 'SWDIO', 'NRST', 'RESET', 'JTAG', 'BOOT0')),
    ('晶振/时钟',   ('CLK', 'XTAL', 'OSC')),
    ('GPIO/ADC',   ('GPIO', 'ADC', 'AIN')),
    ('电源变换',    ('_SW', '_FB', '_PHASE', '_BOOT', '_OUT', '_VDD', '_VDRV', '_EN', 'PGOOD')),
]


def _functions(il):
    """这页涉及哪些电路族：{"USB": {nets, sample}, ...}。只看网名，带样本便于核对。"""
    hit = {}
    for r in il['nets']:
        name = r.get('name')
        if not name or r.get('power'):
            continue
        up = name.upper()
        for fam, keys in FAMILY_KEYS:
            if any(k in up for k in keys):
                h = hit.setdefault(fam, {'nets': 0, 'sample': []})
                h['nets'] += 1
                if len(h['sample']) < 3:
                    h['sample'].append(name)
                break
    return dict(sorted(hit.items(), key=lambda kv: -kv[1]['nets']))


# 主要芯片：靠**参考号前缀**认，不靠库名或数值猜。
# KiCad 的约定是 IC 用 `U`（个别第三方库用 `IC`），电阻电容电感二极管晶体连接器各有自己的字母，
# 所以"U 打头 + 至少 3 个脚"就已经把芯片挑出来了：Q（MOSFET）是分立器件不是"这页叫什么片子"，
# TP/JP 是测试点跳线，Y 是晶体。名字优先取 Value，Value 空着的第三方库退回库符号名。
CHIP_PREFIX = ('U', 'IC')
NOT_CHIP_LIB = ('Connector', 'TestPoint', 'MountingHole', 'Jumper', 'NetTie')


def _chips(il):
    """这页上有哪些主要芯片：[{name, refs, lib, pins}]，按引脚数从多到少（主芯片在前）。

    `refs` 把同型号的几片并成一条（两路 RS485 的 MAX3485 只出现一次，refs=[U3,U4]）。"""
    libpins = {k: len((v or {}).get('pins') or {}) for k, v in (il.get('libsym') or {}).items()}
    got = {}
    for s in il['symbols']:
        if s.get('power'):
            continue
        ref = s.get('id') or ''
        m = re.match(r'[A-Za-z]+', ref)
        if not m or m.group(0).upper() not in CHIP_PREFIX:
            continue
        lib = s.get('lib') or ''
        if any(n in lib for n in NOT_CHIP_LIB):
            continue
        pins = libpins.get(lib, 0)
        if pins < 3:
            continue
        val = ((s.get('fields') or {}).get('Value') or {}).get('text') or ''
        name = val.strip() or lib.split(':')[-1]
        e = got.setdefault(name, {'name': name, 'refs': [], 'lib': lib, 'pins': pins})
        e['refs'].append(ref)
        e['pins'] = max(e['pins'], pins)
    out = list(got.values())
    for e in out:
        e['refs'].sort()
    out.sort(key=lambda e: (-e['pins'], e['name']))
    return out


def _title_block(root):
    tb = one(root, 'title_block')
    out = {}
    if not tb:
        return out
    for c in tb[1:]:
        if not isinstance(c, list):
            continue
        h = H(c)
        if h in ('title', 'date', 'rev', 'company') and len(c) > 1:
            out[h] = c[1]
        elif h == 'comment' and len(c) > 2:
            out.setdefault('comments', []).append(c[2])
    return out


def _source_of(path):
    """同目录下 SOURCE.md / LICENSE 里的来源与许可（语料自带，不猜）。"""
    out = {}
    d = os.path.dirname(os.path.abspath(path))
    src = os.path.join(d, 'SOURCE.md')
    if os.path.exists(src):
        for line in open(src, encoding='utf-8', errors='replace'):
            m = re.match(r'\s*[-*]\s*\*\*(.+?)\*\*[：:]\s*(.+)', line)
            if m:
                out.setdefault(m.group(1).strip(), m.group(2).strip())
    lic = os.path.join(d, 'LICENSE')
    if os.path.exists(lic):
        head = open(lic, encoding='utf-8', errors='replace').read(600)
        first = next((ln.strip() for ln in head.splitlines() if ln.strip()), '')
        out.setdefault('许可', first[:120])
    return out


def _coord_gcd(il):
    """所有坐标的最大公约数（mm）——人画图会落在网格上，这个数就是那套网格。"""
    g = 0
    for s in il['symbols']:
        for v in s['at'][:2]:
            q = int(round(abs(v) * 100))
            if q:
                g = math.gcd(g, q)
    return round(g / 100.0, 3) if g else None


def annotate(il, path=None):
    """把 meta / constraints 写进 IL。只读 IL 自身（外加同目录的来源文件），不看别的页。"""
    tb = {}
    if path and os.path.exists(path):
        try:
            tb = _title_block(parse(open(path, encoding='utf-8', errors='replace').read()))
        except Exception:
            tb = {}
    src = _source_of(path) if path else {}
    devs = [s for s in il['symbols'] if not s.get('power')]
    pts = [p for s in il['symbols'] for p in (s.get('pins_at') or {}).values()]
    rails, ground = [], []
    for r in il['nets']:
        if r.get('power') and r.get('name'):
            (ground if r['name'] in GROUND_NAMES else rails).append(r['name'])
    rails, ground = sorted(set(rails)), sorted(set(ground))
    school = 'label' if not il['wires'] else 'wire'
    stem = os.path.splitext(os.path.basename(path or ''))[0]
    proj = os.path.basename(os.path.dirname(os.path.abspath(path))) if path else ''
    title = tb.get('title') or (f'{proj} / {stem}' if proj else stem)
    works = '导线 %d 根' % len(il['wires']) if il['wires'] else '一根导线不画、全靠标签'
    desc = (f'{title}：{len(devs)} 个器件、{len(il["nets"])} 张网'
            f'（电源轨 {"/".join(rails) or "无"}）；{works}，属'
            f'{"标签派" if school == "label" else "实线派"}画法；'
            f'纸张 {(il.get("page") or {}).get("paper") or "未知"}。')
    funcs = _functions(il)
    chips = _chips(il)
    if funcs:
        fam = '、'.join(funcs)
        desc = desc[:-1] + f'；涉及 {fam}。'
    if chips:
        # 芯片名是检索时最先想到的线索（"哪页有 MAX3485 的画法"），所以同时进 description
        # 和结构化字段：前者给人和 LLM 直接读，后者给脚本按名字精确筛。
        names = '、'.join(c['name'] for c in chips[:6])
        more = f' 等 {len(chips)} 款' if len(chips) > 6 else ''
        desc = desc[:-1] + f'；主要芯片 {names}{more}。'
    il['meta'] = {
        'id': (f'{proj}/{stem}' if proj else stem),
        'title': title,
        'description': desc,
        'chips': chips,
        'source': {'project': proj, 'file': os.path.basename(path or ''),
                   'company': tb.get('company'), 'rev': tb.get('rev'), 'date': tb.get('date'),
                   'comments': tb.get('comments') or [], **src},
        'sheet': {'paper': (il.get('page') or {}).get('paper')},
        'school': school,
        'functions': funcs,
        'stats': {'symbols': len(devs), 'nets': len(il['nets']), 'wires': len(il['wires']),
                  'labels': len(il['labels']), 'junctions': len(il['junctions'])},
    }
    il['constraints'] = {
        'drawing': {'school': school,
                    'paper': (il.get('page') or {}).get('paper'),
                    'coord_gcd_mm': _coord_gcd(il),
                    'power_as': ('symbol' if any(s.get('power') for s in il['symbols'])
                                 else ('label' if il['labels'] else 'none'))},
        'electrical': {'rails': rails, 'ground': ground,
                       'nets': len(il['nets']), 'symbols': len(devs),
                       'single_pin_nets': sum(1 for r in il['nets'] if len(r['pins']) == 1),
                       # 标签派的电源不在 rails 里（没有电源符号可认），全在这一串全局标签名上。
                       # 只记"画了哪些全局标签"这个事实，不猜哪个是电源。
                       'global_labels': sorted({l['text'] for l in il['labels']
                                                if l.get('kind') == 'global' and l.get('text')})},
        'placement': {'extent_mm': ([round(max(p[0] for p in pts) - min(p[0] for p in pts), 1),
                                     round(max(p[1] for p in pts) - min(p[1] for p in pts), 1)]
                                    if pts else None)},
        'notes': [],        # 人工补充的约束（先留空，别自动往里塞判断）
    }
    return il


# ---------- IL 自足：引脚坐标 + 网表都能从 IL 自己算出来 ----------

def prims(il, lib):
    """符号图形原语（渲染 / 包围盒用）。"""
    return ((il.get('libsym') or {}).get(lib) or {}).get('prims') or []


def libpins(il, unit=None, lib=None):
    """库名 → {脚号: (x, y)}。

    - 不给 unit/lib：所有单元的并集（老口径，画图/概览用）
    - 给 lib + unit：**只取这个单元该有的脚**（多单元器件的正确口径）
    """
    if lib is not None and unit is not None:
        g = libsym_for(il, lib)
        return geom.pins_for_unit(g.get('units') or {}, unit)
    return {k: {n: tuple(v) for n, v in (g.get('pins') or {}).items()}
            for k, g in (il.get('libsym') or {}).items()}


def symbol_pins(il, s, cache=None):
    """一个器件的引脚绝对坐标：优先用 IL 里存的 pins_at。

    `cache` 是老接口（`libpins(il)` 的并集表），**只在 IL 里没有该符号的单元信息时
    才用**——多单元器件按并集取脚会造出幽灵脚，见 `geom.pins_for_unit`。
    """
    if s.get('pins_at'):
        return {n: (p[0], p[1]) for n, p in s['pins_at'].items()}
    g = libsym_for(il, s['lib'])
    if g.get('units'):
        lp = geom.pins_for_unit(g['units'], s.get('unit', 1))
    else:
        lp = (cache if cache is not None else libpins(il)).get(s['lib']) or {}
    return geom.pins_at(lp, s['at'], s.get('mirror'))


# KiCad 5 的老文件把标签名里的特殊字符存成 `{slash}` 这类转义（`I/O` 在文件里写作
# `I{slash}O`，`D+` 不受影响、但 `~` 会变 `{~}`）。kicad-cli 导出网表时**会解码**，
# IL 不解码就两头对不上：Bus Pirate 5 的 SIM 适配板上那张网，kicad-cli 叫 `I/O`、
# IL 叫 `I{slash}O`，块卡端口名照抄 IL 的话，`sdi.cardpcb` 门⑤（端口网名板上要有）
# 当场红。**只解码成网名用，不改文件**——块仍然是原页那一块的逐字副本。
# 表按 KiCad 的 `UnescapeString` 来；只收能对上号的那几个。
_ESC = {'slash': '/', 'backslash': '\\', 'dblquote': '"', 'lt': '<', 'gt': '>',
        'bar': '|', 'comma': ',', 'space': ' ', 'tilde': '~'}


def unescape_name(s: str) -> str:
    """`I{slash}O` → `I/O`。没有转义的原样返回（绝大多数名字走这条）。"""
    if not s or '{' not in s:
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '{':
            j = s.find('}', i)
            if j > i:
                tok = s[i + 1:j]
                if tok in _ESC:
                    out.append(_ESC[tok])
                    i = j + 1
                    continue
                if tok == '' and i + 1 < len(s) and s[i + 1] == '{':
                    out.append('{')
                    i += 2
                    continue
        out.append(c)
        i += 1
    return ''.join(out)


def _power_name(s):
    base = s['lib'].split(':')[-1]
    if base in NO_NAME_POWER:        # PWR_FLAG：只声明"这根网有电"，KiCad 里它不命名网
        return None
    # **先取电源脚名**（2026-10-30）：KiCad 7 格式的页上，全局网名就是那只 power_in
    # 脚的**脚名**，Value 只是显示文字。实测 `bpu-current_limiter` 页（version 20230121）：
    # rescue 库里 `+3.3V-power` 的 Value=`+3.3V`、脚名=`+3V3`，kicad-cli 网表叫 `+3V3`；
    # 按 Value 起名会凭空造出一条板上没有的轨（板上也是 `+3V3`），`sdi.cardpcb` 门⑤ 当场红。
    #
    # ⚠ **这不是"复刻 KiCad 所有版本的起名规则"**：KiCad 10 格式的页（如
    # `STM32F4_REV2.kicad_sch`，version 20260306）实测是 Value 说了算——
    # 把那只脚的脚名改成 `+9V9` 网名照旧 `+3.3V`，把 Value 改成 `+3.3VZ` 网名才跟着变。
    # 统一按脚名取，是因为①老的 KiCad 7 页 KiCad 自己就按脚名、②本项目语料里的**板**
    # 都叫 `+3V3`（F4 那块的板也是）。哪天某页的脚名和板名对不上，`sdi.cardpcb` 门⑤
    # （轨名板上要有）会当场红——不会静默错下去。
    pn = s.get('power_pin')
    if pn:
        return pn
    # 退路：名字取 Value 的**文字**（哪怕它藏起来了），取不到才退回库名。
    # 2026-10-29 实测：本页 Altium 的 GND 符号 Value='GND' 是隐藏的，早先只看得见
    # 非隐藏字段 → 退回库名 `GND_POWER_GROUND`，而 kicad-cli 网表里这张网叫 `GND`。
    v = s.get('value') or ((s.get('fields') or {}).get('Value') or {}).get('text')
    return v or base


def connectivity(wires, node_pts):
    """按 KiCad 的连接判定把导线和端点并成片，返回 DSU（全仓只此一份判据）。

    wires：每条导线的折点序列（可传 IL 的 wire 字典，也可传点表）；node_pts：所有端点
    ——引脚、标签、结点、NC。两条规则：

      ① 端点落在导线上（含两个端点，EPS 内）就算接上；
      ② 两个端点彼此重合（EPS 内）就算接上。人工页里电源符号常常直接压在器件脚下、
         中间一根导线都没有，只认导线会把它们拆成孤网（F405 页实测：C13.2/C14.2/Q1.1
         的 GND 符号压在脚上 → 按导线并网得 89 个网，KiCad 只有 78 个）。
    """
    dsu = geom.DSU()
    segs = []
    for w in wires:
        pts = w['pts'] if isinstance(w, dict) else w
        for i in range(len(pts) - 1):
            segs.append((pts[i], pts[i + 1]))
    keys = {geom.key(q) for q in node_pts}
    for a, b in segs:
        dsu.union(geom.key(a), geom.key(b))
        for k in keys:
            if geom.on_seg(k, a, b):
                dsu.union(k, geom.key(a))
    ps = sorted(keys)
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            if b[0] - a[0] > geom.ABUT:
                break
            if abs(b[1] - a[1]) <= geom.ABUT and geom.dist(a, b) <= geom.ABUT:
                dsu.union(a, b)
    return dsu


def build_nets(il):
    """从 IL 的几何重建网表（不读任何文件）。

    判定规则见 `connectivity`（落在导线上 + 端点重合，与 KiCad 同）；
    同名标签合并；**电源符号就是以它的网名为名的全局标签**（网名怎么取见 `_power_name`）。
    """
    node_pts = set()
    for s in il['symbols']:
        for p in symbol_pins(il, s).values():
            node_pts.add(geom.key(p))
    for l in il['labels']:
        node_pts.add(geom.key((l['at'][0], l['at'][1])))
    for p in il['junctions']:
        if p:
            node_pts.add(geom.key(p))
    for p in il['no_connects']:
        if p:
            node_pts.add(geom.key(p))

    dsu = connectivity(il['wires'], node_pts)

    # ── 网名 + 别名 ────────────────────────────────────────────────────────────
    # 一张网可能有**好几个名字**：一条线上挂两个不同名的标签，在 KiCad 里就是
    # "这两个名字指同一张网"（别名），不是两张网。网名按驱动优先级取：
    # **全局标签 > 电源符号 > 层次标签 > 本地标签**，同级取字典序最小。
    #
    # 2026-10-30 实测（adapter-ram-ddr5 页，kicad-cli 当裁判）：`VOUT`（全局）、
    # `+5V`（电源符号）、`BULK_VIN`（全局）三个名字挂在同一根两段导线上，
    # kicad-cli 只出一张网 `BULK_VIN`；把 BULK_VIN 改名 ZZTOP → 变 `VOUT`
    # （同级字母序）；把 VOUT 改名 VOUTA → 变 `BULK_VIN`（`+5V` 里有 `+`，
    # ASCII 上排在字母前却输了 ⇒ 不是纯字母序，是"全局标签压电源符号"）。
    # 旧代码只给每个连通片记**一个**名字（电源符号先到先得、标签 setdefault 丢掉），
    # 于是那一页的 LDO 输出被拆成 VOUT / +5V / BULK_VIN 三张网——门⑤ 看着自洽，
    # 可 sch↔pcb 那道"谁和谁同网"立刻就穿帮（板上它们是一张网）。
    #
    # 2026-10-29 的另一半（stm32_Example 页）：AMS1117 输出那段线同时有本地标签
    # `VOUT` 和 `3V3` 电源符号，KiCad 把它全叫 `3V3` ⇒ 电源符号压本地标签，
    # 优先级表里也是这么排的。
    prio = {'global': 3, 'power': 2, 'hier': 1, 'local': 0}

    cands, power_roots = {}, set()
    for s in il['symbols']:
        if not s.get('power'):
            continue
        nm = _power_name(s)
        if not nm:                    # 标记类电源符号（PWR_FLAG）不参与命名
            continue
        nm = unescape_name(nm)
        pts = symbol_pins(il, s)
        p = pts.get('1') or (s['at'][0], s['at'][1])
        r = dsu.find(geom.key(p))
        cands.setdefault(r, []).append((prio['power'], nm))
        power_roots.add(r)
    for l in il['labels']:
        r = dsu.find(geom.key((l['at'][0], l['at'][1])))
        cands.setdefault(r, []).append((prio.get(l.get('kind'), 0), unescape_name(l['text'])))

    # 隐藏的 power_in 脚 = **不画线的电源符号**（KiCad 的"不可见电源脚"规矩）：
    # 脚名就是网名，所有同名的这类脚天南地北也算一张网。它们在图上没有导线可抓，
    # 几何提取只能靠这一条补上（2026-10-30 `bp5_lcd_display` 的 LCD 5/6/12 三只 GND 脚）。
    for s in il['symbols']:
        hp = s.get('hidden_power') or {}
        if not hp:
            continue
        pts = symbol_pins(il, s)
        for num, nm in hp.items():
            p = pts.get(num)
            if not p:
                continue
            r = dsu.find(geom.key(p))
            cands.setdefault(r, []).append((prio['power'], unescape_name(nm)))
            power_roots.add(r)


    # 名字与连通片两边都并进同一个团：同名当然是一张网；一个片上挂了几个名字时，
    # 那几个名字也是同一张网（别名）——只并"名字"这一侧就自动把别名连起来了。
    grp = geom.DSU()
    for r, cs in cands.items():
        for _, nm in cs:
            grp.union(('r', r), ('n', nm))

    # 团名 = 团里最强的那条驱动；同级取字典序最小的名字（与 KiCad 实测一致）
    best = {}
    for r, cs in cands.items():
        g = grp.find(('r', r))
        for p_, nm in cs:
            cur = best.get(g)
            if cur is None or (-p_, nm.lower(), nm) < (-cur[0], cur[1].lower(), cur[1]):
                best[g] = (p_, nm)
    power_groups = {grp.find(('r', r)) for r in power_roots}

    # 2026-10-30：**层次标签 = 这张网要出本页**。父页用 `(sheet … pin …)` 把它接到
    # 别的子页上，所以它和"块外还有件"是同一种身份（都是接口），不能当块内网。
    # 层级工程的子页单独看时，这类网往往**只有一只脚**（BP5 主板 micro-rp2040 页的
    # BUFDIR0..7 / BUFIO0..7 就只落在 U103 一只脚上），按"页上还有没有别人"判会把
    # 二十几根总线全判成块内网，拼装时逐实例改名——那些线就全断了。
    hier_roots = {grp.find(('r', dsu.find(geom.key((l['at'][0], l['at'][1])))))
                  for l in il['labels'] if l.get('kind') == 'hier'}

    recs = {}
    for s in il['symbols']:
        if s.get('power'):
            continue
        for n, p in symbol_pins(il, s).items():
            r = dsu.find(geom.key(p))
            k = grp.find(('r', r)) if r in cands else ('u', r)
            rec = recs.setdefault(k, {'name': best[k][1] if k in best else None,
                                      'power': k in power_groups, 'pins': [], 'wires': [],
                                      'hier': k in hier_roots})
            rec['pins'].append([s['id'], n])
    for w in il['wires']:
        r = dsu.find(geom.key(w['pts'][0]))
        k = grp.find(('r', r)) if r in cands else ('u', r)
        if k in recs:
            recs[k]['wires'].append([[round(x, 2), round(y, 2)] for x, y in w['pts']])

    out = list(recs.values())
    for rec in out:
        rec['pins'] = sorted([list(p) for p in rec['pins']])
    out.sort(key=lambda r: (r['name'] is None, str(r['name']), -len(r['pins'])))
    return out


def _hidden_power(il, s):
    """这个符号实例的**隐藏 power_in 脚**：{脚号: 脚名}（按 `(unit N)` 取，不同单元各算）。

    KiCad 的不可见电源脚**不画线也算接在以脚名为名的全局网**上——`geom.lib_hidden_power`
    收库里的定义，这里按实例的单元号取一遍。取不到几何（lib_id 对不上缓存库）时给空表：
    宁可少并一张已知的电源网，也不能凭空造脚。
    """
    g = libsym_for(il, s.get('lib'))
    hp = g.get('hidden_power') or {}
    if not hp:
        return {}
    uni = {str(k): v for k, v in hp.items()}
    u = str(s.get('unit') or 1)
    if u in uni:
        merged = dict(uni.get('0') or {})
        merged.update(uni[u])
        return merged
    merged = {}                        # 单元号对不上：合并（宁可多并脚，不能漏电源）
    for k in sorted(uni, key=str):
        merged.update(uni[k])
    return merged


def refresh_pins(il):
    """按库几何重算每个器件的 pins_at（改了 at / 转角的器件必须走这一步）。

    注意不能"旧坐标 + 平移量"——**转了角度**的器件，引脚位置不是平移关系。
    多单元器件按 `(unit N)` 取脚，不能按并集（并集会给每个单元都安上别的单元的脚）。
    """
    for s in il['symbols']:
        g = libsym_for(il, s['lib'])
        if not (g.get('pins') or g.get('units')):
            continue
        pins = geom.pins_for_unit(g.get('units') or {}, s.get('unit', 1)) \
            if g.get('units') else {n: tuple(v) for n, v in (g.get('pins') or {}).items()}
        s['pins_at'] = {n: [round(p[0], 2), round(p[1], 2)]
                        for n, p in geom.pins_at(pins, s['at'], s.get('mirror')).items()}
        s['hidden_power'] = _hidden_power(il, s)
    return il


def refresh(il):
    """把 pins_at / nets 重算回一致状态（改动 IL 之后必须调用）。

    有 libsym 就按库几何重算引脚坐标（保证改了 at 也不会漏改引脚）；
    没有库几何的符号保留原 pins_at（借不到图形时至少不丢信息）。
    """
    refresh_pins(il)
    il['nets'] = build_nets(il)
    if 'meta' in il or 'constraints' in il:
        annotate(il)          # 网表/坐标变了，元信息里的统计与电气事实要跟着变
    return il


def net_of_pin(il):
    """{(ref, pin): 网名}；无名网给一个稳定的占位名。"""
    out = {}
    for i, rec in enumerate(il['nets']):
        name = rec['name'] or f'N${i}'
        for ref, pin in rec['pins']:
            out[(ref, pin)] = name
    return out


def stats(il):
    ns, nw = len(il['symbols']), len(il['wires'])
    pins = sum(len(s.get('pins_at') or {}) for s in il['symbols'])
    return {'symbols': ns, 'wires': nw, 'wire/sym': round(nw / ns, 2) if ns else 0,
            'junctions': len(il['junctions']), 'labels': len(il['labels']),
            'label/pin': round(len(il['labels']) / pins, 2) if pins else 0,
            'power_syms': sum(1 for s in il['symbols'] if s['power']),
            'nets': len(il['nets']),
            'nc': len(il['no_connects'])}


if __name__ == '__main__':
    il = extract(sys.argv[1])
    compact = len(sys.argv) > 2 and sys.argv[2] == '--compact'
    if compact:
        for k in ('wires', 'junctions', 'labels', 'no_connects', 'nets'):
            il[k] = il[k][:6]
        for s in il['symbols']:
            s['fields'] = {k: v for k, v in list(s['fields'].items())[:2]}
        il['symbols'] = il['symbols'][:8]
        il['libsym'] = {k: {'pins': v['pins'], 'prims': v['prims'][:3]}
                        for k, v in list(il['libsym'].items())[:3]}
    print(json.dumps(il, ensure_ascii=False, separators=(',', ':')))
    print('STATS ' + json.dumps(stats(extract(sys.argv[1])), ensure_ascii=False), file=sys.stderr)
