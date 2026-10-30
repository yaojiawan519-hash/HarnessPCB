#!/usr/bin/env python3
"""从人工画的原理图里切"功能块"——构图经验的第一层。

切法（全部确定性，不打分不训练）：
  1. 器件分两类：核心（IC / 连接器 / 晶振 / 开关 / 三极管 / 光耦）和卫星（R/C/L/FB/保险丝/二极管/LED）。
  2. 卫星挂到"信号图"上最近的核心（电源网不参与 BFS，否则所有东西都被 GND 连成一坨）。
     挂不上任何一个核心的（比如去耦电容只接电源）按几何距离归到最近的核心。
  3. 核心之间**不合并**——人工画图时 MCU 就是自己一块，旁边接什么都算它的外围。
  4. 每块产出：成员 / 内部网 / 边界网（= 这块对外的"端口"）/ 外接核心 / 包围盒。

用法:
  python3 blocks.py <page.kicad_sch> [...]        # 打印块清单
  python3 blocks.py --all                         # 用 CORPUS 里的人工页切块（块库）
  python3 blocks.py --json out.json <pages...>    # 指定页，存成块库候选
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import json, sys, collections
import extract, wireup

HERE = _os.path.dirname(_os.path.abspath(__file__))
KICAD = _os.path.join(HERE, '..', '..', '..', 'KiCAD_Projects')
THIRD = _os.path.join(HERE, '..', 'corpus')      # 第三方开源页（每页旁边有 LICENSE + SOURCE.md）

# 语料 = 本仓库里人工画的原理图页（整板 + 层次子图）。列表写死，块库才可复现。
CORPUS = [
    'STM32F103_USB/STM32F103_USB_v1.kicad_sch',
    'ESP32S3_TestBoard/ESP32S3_TestBoard_v3.kicad_sch',
    'USB2RS485/USB2RS485_v7.kicad_sch',
    'USB_EtherCAT/USB_EtherCAT_v3.kicad_sch',
    'BlockBoard_CANUSB/BlockBoard_CANUSB_v1.kicad_sch',
    'stm32_plc/mcu_core.kicad_sch',
    'stm32_plc/power.kicad_sch',
    'stm32_plc/eth.kicad_sch',
    'stm32_plc/di.kicad_sch',
    'stm32_plc/do.kicad_sch',
    'stm32_plc/debug.kicad_sch',
    'stm32_plc/rs485.kicad_sch',
]


# 第三方开源语料：只收许可宽松（Apache-2.0 / CERN-OHL-P）的页，路径相对 corpus/。
# 强互惠（CERN-OHL-S）、CC-BY-SA、CC-BY-NC 的板子一律不进——日后要公开发布块卡会牵扯不清。
THIRD_CORPUS = [
    'royalblue54L_feather/Debugger.kicad_sch',
    'royalblue54L_feather/nRF54L15.kicad_sch',
    'royalblue54L_feather/nPM1300.kicad_sch',
    'tiny_tapeout/rp2040.kicad_sch',
    'jetson-agx-thor-baseboard/level_shifter_x8.kicad_sch',
    'jetson-agx-thor-baseboard/peripherals.kicad_sch',
    'jetson-agx-thor-baseboard/usb_dp.kicad_sch',
    'jetson-agx-thor-baseboard/usb_hub.kicad_sch',
    'jetson-agx-thor-baseboard/power.kicad_sch',
    'jetson-agx-thor-baseboard/dcdc.kicad_sch',
    'jetson-agx-thor-baseboard/recovery_usb.kicad_sch',
]


def corpus_pages():
    local = [_os.path.join(KICAD, p) for p in CORPUS if _os.path.exists(_os.path.join(KICAD, p))]
    third = [_os.path.join(THIRD, p) for p in THIRD_CORPUS if _os.path.exists(_os.path.join(THIRD, p))]
    return local + third


def page_label(path):
    """块卡里的页名一律是 `项目/文件名`（本地页、第三方页同一口径）。

    以前本地页只留 basename，导致没法按项目挑参考页（`ASSEMBLE_REFS=stm32_plc` 选不中），
    也没法一眼看出这张卡是谁画的。
    """
    p = _os.path.abspath(path)
    return _os.path.relpath(p, _os.path.dirname(_os.path.dirname(p))).replace(_os.sep, '/')

PASSIVE = {'R', 'R_Small', 'C', 'C_Small', 'C_Polarized', 'L', 'FerriteBead',
           'FerriteBead_Small', 'Fuse', 'Polyfuse', 'D', 'D_Schottky', 'D_TVS',
           'D_Zener', 'D_Small', 'LED', 'LED_Small', 'D_Schottky_Small'}
# 第三方板的符号名带封装/料号（R_Small_US、C_100n_0402、LED_ARGB……），
# 只按上面那张精确表认不出来，得按前缀认（前缀后面必须跟下划线，避免误伤 Crystal/Conn 这类）。
PASSIVE_PREFIX = ('R_', 'C_', 'L_', 'D_', 'FB_', 'Ferrite', 'Fuse', 'Polyfuse', 'LED')

# 核心器件按名字归"功能类别"，块名就从这里来。
# 顺序有意义：**第一个命中的赢**，所以专用件（USB PD / 负载开关 / 存储……）必须排在
# 通用件（电源变换的 'TPS'、端子的 'Conn'）前面，否则会被通用关键词抢走。
ROLE_RULES = [
    # —— 专用件优先 ——
    ('USB PD/切换', ('TPS6598', 'TUSB1046', 'TUSB1064', 'HD3SS3220', 'FUSB30', 'STUSB',
                   'PI3USB', 'CBTL08', 'NB7NQ')),
    ('USB Hub',    ('USB7252', 'USB251', 'USB471', 'USB5744', 'TUSB8041', 'FE1.1', 'CY7C656',
                   'GL850', 'CH334', 'CH335', 'USB_HUB')),
    ('电平转换',   ('NTS010', 'NTS020', 'TXB010', 'TXS010', 'SN74LVC1T45', 'SN74LVC2T45',
                   'SN74AXC', 'PCA9306', 'TCA9406', 'MAX3378', '74LVC4245', '74AVC', 'LSF01')),
    # 注意别写 'AP22'：AP2204/AP2205 是 LDO，不是负载开关（实测会把 STM32 页的 U4 抢走）
    ('负载开关',   ('AP226', 'AP255', 'AP250', 'TPS229', 'TPS211', 'LTC4412', 'SY628', 'FPF',
                   'MIC20', 'SIP32', 'SGM25')),
    ('充电管理',   ('BQ2', 'BQ7', 'TP4056', 'MCP738', 'LT3652', 'BQ25')),
    ('存储',       ('W25Q', 'W25X', 'SST26', 'SST25', 'IS25', 'MX25', 'GD25', 'S25FL', 'MT25',
                   'AT24', '24C', '93C', 'EMC24')),
    ('安全芯片',   ('SLB96', 'ATECC', 'OPTIGA', 'STSAFE', 'SE05', 'ATSHA')),
    ('电流监测',   ('INA2', 'INA1', 'ACS7', 'LT6106', 'ZXCT')),
    ('GPIO扩展',   ('PCAL', 'PCA95', 'TCA95', 'MCP23', 'PCF85', 'AW9523')),
    ('FPGA',       ('XCZU', 'XC7', 'XC4', 'XC3S', 'XC2C', 'XCR3', 'EP4C', 'ICE40', 'ECP5',
                   'LFE5', 'LCMXO')),
    # —— 通用件 ——
    ('MCU',        ('STM32', 'ESP32', 'RP2040', 'RP2350', 'GD32', 'ATmega', 'NRF5', 'CH32',
                   'PIC16', 'PIC18', 'PIC12', 'ATSAM', 'MSP430', 'LPC1', 'LPC5', 'MCF5', 'STM8',
                   'BM15')),          # BM15x = nRF54 模块
    ('晶振',       ('Crystal', 'Resonator', 'Oscillator', 'OSC_', 'KV7050', 'SG-80', 'ECS-')),
    ('ESD防护',   ('USBLC6', 'PRTR5V', 'ESDA', 'TPD', 'SM712', 'RCLAMP', 'SRV05', 'PESD')),
    ('USB入口',    ('USB_B', 'USB_C', 'USB_A', 'USB_', 'USB-C')),
    ('RS485收发',  ('MAX3485', 'LTC2850', 'SP485', 'SN65', 'ISO3082', 'THVD', 'MAX3430', 'MAX202',
                   'MAX3232')),
    ('CAN收发',    ('TJA1051', 'TJA1042', 'SN65HVD', 'MCP2551', 'SIT1050', 'PCA82C251', 'TCAN')),
    ('电源变换',   ('AMS1117', 'AP21', 'AP1117', 'AP7366', 'AP2112', 'TPS', 'LM1117', 'LM317',
                 'LM78', '7805', 'NCP7', 'TLV7', 'MP23', 'ITX', 'Regulator',
                 'LDO', 'LM2596', 'MT2492', 'SGM', 'XC62', 'RT90', 'ADP1', 'ADP2', 'LTC3',
                 'BD9', 'SY8', 'ISL8', 'IRPS', 'TDA21', 'SIC43', 'MAX1', 'TPS62', 'NCV8',
                 'NPM1')),          # nPM1300 = PMIC
    ('以太网',     ('HR911105A', 'LAN8720', 'LAN87', 'DP83848', 'DP83', 'KSZ', 'RTL8', 'YT85',
                   'AR80', 'RJ45', 'MagJack', 'GLP')),
    ('隔离',       ('PC817', 'TLP', 'optocoupler', 'ISO', 'Si86', 'ADuM', 'π12', 'ACPL')),
    ('端子',       ('Screw_Terminal', 'Conn_01x', 'Conn_02x', 'Terminal', 'Conn', 'Socket',
                   'PinHeader', 'JST', 'Molex', 'HC-1.25', 'PH2', 'XH2', 'LEMO', 'DB9', 'DB25',
                   'Bus_M', 'Jack')),
    ('按键',       ('SW_Push', 'SW_SPST', 'SW_')),
    ('MOS',        ('AO34', 'IRLML', 'IRF', 'Si23', 'DMG', 'DMC', 'SISS', 'CSD', 'BSS1', 'BSS8',
                   '2N7002', 'PMV', 'NTR2', 'AON', 'SUD', 'NTM', 'FDS', 'RJK', 'IPB')),
]


def role(lib, value=''):
    base = lib.split(':')[-1]
    hay = f'{base} {value}'.lower()
    for name, keys in ROLE_RULES:
        if any(k.lower() in hay for k in keys):
            return name
    if base in PASSIVE or base.startswith(PASSIVE_PREFIX):
        return 'passive'
    return 'other'


def dev_type(sym):
    """粗粒度类型：用于跨页比对块签名。"""
    base = sym['lib'].split(':')[-1]
    val = ((sym.get('fields') or {}).get('Value') or {}).get('text') or ''
    r = role(sym['lib'], val)
    if r != 'passive':
        # 测试点单独一类：它不是设计意图，混进 other 会把块签名搅浑
        if r == 'other' and ('TestPoint' in base or str(sym.get('id') or '').startswith('TP')):
            return 'TP'
        return r
    for p in ('C_Polarized',):
        if base.startswith(p):
            return 'Cpol'
    if base.startswith('C'):
        return 'C'
    if base.startswith('R'):
        return 'R'
    if base.startswith('Ferrite'):
        return 'FB'
    if base.startswith('Fuse') or base.startswith('Polyfuse'):
        return 'F'
    if base.startswith('LED'):
        return 'LED'
    if base.startswith('D_'):
        return 'D'
    if base.startswith('L'):
        return 'L'
    return base


def load(page):
    """只读 IL：器件（含引脚坐标）和网表都从 extract 产出的 sdi.v1 里拿。"""
    il = extract.extract(page)
    devs, power = {}, set()
    for s in il['symbols']:
        val = ((s.get('fields') or {}).get('Value') or {}).get('text')
        # 位号以 '#' 开头的是 KiCad 的非器件符号（#PWR 电源、#FLG 标志、#SYM 图案）。
        # 第三方板的电源符号常在自己的库里（antmicropower:+1V8），靠 lib 前缀认不出来，
        # 只能靠位号这条硬规矩。
        if s.get('power') or str(s.get('id') or '').startswith('#'):
            power.add(val or s['lib'].split(':', 1)[1])
            continue
        pts = {n: (p[0], p[1]) for n, p in (s.get('pins_at') or {}).items()}
        devs[s['id']] = {
            'ref': s['id'], 'lib': s['lib'], 'type': dev_type(s),
            'value': val or '', 'pins': pts,
            'at': (s['at'][0], s['at'][1]),
            'core': len(pts) >= 3 and dev_type(s) not in
                    {'R', 'C', 'Cpol', 'FB', 'F', 'D', 'LED'},
        }
    # 本体中心（页面坐标）：方位一律相对**本体**算，不相对符号原点 ——
    # 原点在本体角上的符号会把"上面"算成"下面"。切块器与拼装器共用同一口径。
    ctr = wireup.body_center(il)
    for r in devs:
        devs[r]['ctr'] = ctr.get(r, devs[r]['at'])
    # 网表：pin -> net
    # IL 里同名网已经合并过；无名网的各连通块各算一个（和原来一致）
    pin_net, nets = {}, {}
    for i, rec in enumerate(il['nets']):
        name = rec['name'] or f'N${i}'
        pins = [(ref, pin) for ref, pin in rec['pins'] if ref in devs]
        if not pins:
            continue
        nets.setdefault(name, []).extend(pins)
        for k in pins:
            pin_net[k] = name
    return devs, nets, power, il


# ⚠️ 默认关：劈块本身是对的（Σ块面积 0.64×），但它把小页摆密之后，**接线器藏着的并网 bug**
# 现形了——STM32F103 已经修掉（导线顶点撞别人的线），usb_dp 还有一处没定位（pass C 给不同
# 连通块安了同名标签）。网表门是硬门，23/23 没守住之前不许开默认。
SPLIT = _os.environ.get('BLOCKS_SPLIT', '0') != '0'
SPLIT_SHRINK = float(_os.environ.get('BLOCKS_SPLIT_SHRINK') or 0.6)   # 两半面积和 < 原块 × 这数才劈


def _bb_area(mem, devs):
    xs = [devs[r]['at'][0] for r in mem]
    ys = [devs[r]['at'][1] for r in mem]
    return max(max(xs) - min(xs), 1e-6) * max(max(ys) - min(ys), 1e-6)


def _split_sprawl(mem, devs, depth=0):
    """沿最大的空缝把摊开的块劈成两半；返回若干"紧凑长方形"成员表。

    只看几何，不看网——真要按网再切一次等于重做聚类，而这里的病灶是**几何摊开**，
    劈开的刀口就是人画图时本来就留的那道缝。
    """
    if len(mem) < 4 or depth >= 3:
        return [mem]
    area = _bb_area(mem, devs)
    best = None
    for ax in (0, 1):
        order = sorted(mem, key=lambda r: (devs[r]['at'][ax], r))
        for k in range(2, len(order) - 1):
            gap = devs[order[k]]['at'][ax] - devs[order[k - 1]]['at'][ax]
            if gap <= 0.8:                    # 小于一个栅格的不算缝
                continue
            a, b = order[:k], order[k:]
            got = _bb_area(a, devs) + _bb_area(b, devs)
            if best is None or (gap, -got) > best[0]:
                best = ((gap, -got), a, b, got)
    if not best:
        return [mem]
    _, a, b, got = best
    if got >= area * SPLIT_SHRINK:
        return [mem]
    return _split_sprawl(a, devs, depth + 1) + _split_sprawl(b, devs, depth + 1)


def build(page):
    devs, nets, power, il = load(page)
    pwr = set(power) | {'GND', 'VCC', 'VDD', '+3V3', '_GND', 'GNDA', 'PWR_FLAG'}

    def is_power(n):
        return n in pwr or n.split('/')[-1] in pwr or n.lstrip('+').replace('.', '') in \
            {p.lstrip('+').replace('.', '') for p in pwr}

    # 信号图（只走非电源网）
    adj = collections.defaultdict(set)
    for name, pins in nets.items():
        if is_power(name) or len(pins) > 12:      # 大网（总线/地）不当传播路径
            continue
        refs = sorted({r for r, _ in pins})
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                adj[refs[i]].add(refs[j])
                adj[refs[j]].add(refs[i])

    cores = [r for r, d in devs.items() if d['core']]
    # 一颗大芯片独占"主机"位置：连接数最多的核心
    hub = max(cores, key=lambda r: len(devs[r]['pins'])) if cores else None  # 脚最多的一般是主控

    # 卫星 BFS 到最近核心
    def geo(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    owner = {c: c for c in cores}
    dist = {c: 0 for c in cores}
    frontier = list(cores)
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj[u]:
                if v in dist:
                    continue
                dist[v] = dist[u] + 1
                # 等距时按几何距离挑：晶振的负载电容不该被 MCU 抢走
                cands = [w for w in adj[v] if dist.get(w) == dist[u]]
                owner[v] = min(cands, key=lambda w: geo(devs[v]['at'], devs[w]['at']))
                nxt.append(v)
        frontier = nxt

    # 挂不上的（只接电源的）按几何最近核心
    def geo_nearest(p):
        best, bd = None, 1e9
        for c in cores:
            q = devs[c]['at']
            d = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
            if d < bd:
                best, bd = c, d
        return best

    for r, d in devs.items():
        if r in owner:
            continue
        c = geo_nearest(d['at'])
        if c:
            owner[r] = c

    # **核心套核心要并上去**：R317 自己收了一组外围件（C305/C33/C37/R10），
    # 同时 R317 又被 U7 收走。不并的话，这一组的锚点会被 U7 块搬走、四个外围件
    # 留在另一处 —— 一个块被撕成相距 430mm 的两截（dcdc 实测），页面上平白多两片孤岛，
    # "块=紧凑长方形"这个前提从根上就破了。并完每个器件恰好属于一个块。
    def _root(c):
        seen = set()
        while owner.get(c, c) != c and c not in seen:
            seen.add(c)
            c = owner[c]
        return c

    for r in list(owner):
        p = _root(owner[r])
        if p != owner[r]:
            owner[r] = p

    # 孤立单件块（没挂上核心的零散 R/C）不独立成块，并进最近的核心
    cnt = collections.Counter(owner.values())
    for r in list(owner):
        if r in cores:
            continue
        if cnt[owner[r]] == 1 and devs[r]['type'] in {'R', 'C', 'Cpol', 'FB', 'F', 'D', 'LED',
                                                     'TP', 'other'}:
            alt = geo_nearest(devs[r]['at'])
            if alt:
                cnt[owner[r]] -= 1
                owner[r] = alt
                cnt[alt] += 1

    groups = collections.defaultdict(list)
    for r, c in owner.items():
        groups[c].append(r)

    # **块必须是个紧凑的长方形**（用户 2026-10-27 的规格）。图聚类只看"谁和谁连着"，
    # 不管"连得多远"：实测 155 个块里，大块（>400mm²）被器件本体覆盖的面积中位只有 0.32，
    # 最松的（peripherals Q1：316×103mm 里 4 个件）只有 0.007 —— 那不是块，是页面上的
    # 一片地区。摊得这么开，块内/块间都拉不出直线，只能挂标签，"能连就连"从根上做不到。
    # 修法：只沿**最大的那道空缝**劈开（不引入随机、不引入新阈值），
    # 劈完两半的包围盒面积和比原来小 40% 以上才认，递归到劈不动为止。
    if SPLIT:
        newg = {}
        for c, mem in list(groups.items()):
            for half in _split_sprawl(mem, devs):
                # 没被劈开的那一半必须**原样**保留锚点，否则整库块卡都跟着变（不可比）。
                # 劈出来的新块用自己的头：脚最多的那颗；平手取离半块质心最近的。
                if c in half:
                    k = c
                else:
                    mx = sum(devs[r]['at'][0] for r in half) / len(half)
                    my = sum(devs[r]['at'][1] for r in half) / len(half)
                    k = max(half, key=lambda r: (len(devs[r].get('pins') or ()),
                                                 -abs(devs[r]['at'][0] - mx)
                                                 - abs(devs[r]['at'][1] - my), r))
                newg[k] = half
        groups = newg

    # 层次/全局标签是"这根网要跨出本页"，那就是块的对外端口。
    # 只看器件成员的话，一张全是层次标签的子图会算出"边界网 0 个"——端口全丢了。
    port_nets = {l['text'] for l in il['labels'] if l.get('kind') in ('hier', 'global')}

    out = []
    for c, members in groups.items():
        # 成员顺序必须定死：集合迭代顺序受哈希种子影响，会让"方位交替"在两次运行间翻面
        members = sorted(members, key=lambda r: (devs[r]['type'], r))
        mset = set(members)
        inner, bound, nmem = [], [], {}
        for name, pins in nets.items():
            inb = {r for r, _ in pins} & mset
            if not inb:
                continue
            if len(inb) == len({r for r, _ in pins}) and name not in port_nets:
                inner.append(name)
                # 签名要"网名无关"，所以必须留下**每个网连着哪几个成员**这个纯结构事实
                nmem[name] = sorted(inb)
            else:
                bound.append(name)
        xs = [devs[r]['at'][0] for r in members]
        ys = [devs[r]['at'][1] for r in members]
        sig = collections.Counter(devs[r]['type'] for r in members)
        out.append({
            'page': page_label(page),
            'anchor': c,
            'anchor_role': role(devs[c]['lib'], devs[c]['value']) if c in devs else '?',
            'members': sorted(members, key=lambda r: (devs[r]['type'], r)),
            'sig': dict(sorted(sig.items())),
            'types': {r: devs[r]['type'] for r in members},
            'inner_nets': sorted(inner),
            'net_members': nmem,
            'boundary_nets': sorted(bound),
            'bbox': [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
            # 块内位置参数：每个外围件相对锚点的位移 / 距离
            # 距离不可跨页迁移（同一块在不同板的间距差 2~5 倍），方位（左/右/上/下）可以
            'rel': {r: [round(devs[r]['at'][0] - devs[c]['ctr'][0], 1),
                        round(devs[r]['at'][1] - devs[c]['ctr'][1], 1)]
                    for r in members if r != c},
            'spacing': {r: round(((devs[r]['at'][0] - devs[c]['ctr'][0]) ** 2 +
                                  (devs[r]['at'][1] - devs[c]['ctr'][1]) ** 2) ** 0.5, 1)
                        for r in members if r != c},
            'is_hub': c == hub,
        })
    out.sort(key=lambda b: (-len(b['members']), b['anchor']))
    return out


def sig_key(b):
    """跨页比对用的签名：只留器件类型组合 + 块名类别。"""
    return (b['anchor_role'], tuple(sorted(b['sig'].items())))




COARSE = {'C': 'C', 'Cpol': 'C', 'R': 'R', 'D': 'D', 'LED': 'LED',
          'FB': 'FB', 'F': 'F', 'L': 'L'}


def coarse(b, r):
    t = b['types'][r]
    return COARSE.get(t, t)


def profile(b):
    """块的"外围配置"：锚点类别 + 外围成员粗细类别集合（不计锚点自身）。"""
    per = {coarse(b, r) for r in b['members'] if r != b['anchor']}
    return (b['anchor_role'], tuple(sorted(per)))


def jac(a, b):
    A, B = set(a[1]), set(b[1])
    return len(A & B) / max(1, len(A | B))


def library_report(allb, min_members=2, thresh=0.6):
    print('\n=== 块库候选：外围配置在 ≥2 页重复出现（Jaccard≥%.1f，块内 ≥%d 件）'
          % (thresh, min_members))
    cand = [b for b in allb if len(b['members']) >= min_members]
    seen = set()
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i], cand[j]
            if a['page'] == b['page'] or a['anchor_role'] != b['anchor_role']:
                continue
            k = jac(profile(a), profile(b))
            if k < thresh:
                continue
            key = (a['anchor_role'], profile(a)[1], profile(b)[1])
            if key in seen:
                continue
            seen.add(key)
            print(f"  {a['anchor_role']:8s} {a['page'][:22]:22s}{a['anchor']:5s}"
                  f"{a['sig']}  ⇄  {b['page'][:22]:22s}{b['anchor']:5s}{b['sig']}   相似度 {k:.2f}")
    if not seen:
        print('  （没有跨页重复的外围配置）')


def main():
    args = sys.argv[1:]
    as_json = None
    if args and args[0] == '--json':
        as_json = args[1]
        args = args[2:]
    if not args or args[0] == '--all':
        args = corpus_pages()
    allb = []
    for p in args:
        bs = build(p)
        allb.extend(bs)
        print(f'\n=== {_os.path.basename(p)}  {len(bs)} 块')
        for b in bs:
            star = '*' if b['is_hub'] else ' '
            mem = ' '.join(f"{r}:{b['types'][r]}" for r in b['members'][:14])
            more = '' if len(b['members']) <= 14 else f' …+{len(b["members"])-14}'
            print(f'  {star}[{b["anchor_role"]:6s}] {b["anchor"]:6s} n={len(b["members"]):2d} '
                  f'边界网={len(b["boundary_nets"]):2d}  {mem}{more}')
    library_report(allb)
    if as_json:
        json.dump(allb, open(as_json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'\n写入 {as_json}')


if __name__ == '__main__':
    main()
