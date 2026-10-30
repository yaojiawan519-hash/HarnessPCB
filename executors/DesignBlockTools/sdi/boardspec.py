"""`.sdi.yaml` → `pcbstudio.assembly.v1`：**语言是权威，拼板链读的是它的产物。**

## 为什么要有这个模块

2026-10-28 用户更正："统一的 yaml 还没有写出来吧"。当时一块板的描述分居两处、两份都手写：

    specs/stm32f103usb.sdi.yaml                 ← 电路（library / modules / nets）
    out/carve_stm32f103usb/assembly/asm.spec.json ← 板（board / blocks / anchors）

两份各自手改，谁改了另一份不会跟着变；而且第二份里那些数（`conn_edge`/`conn_inset`/
`nest_gap`/`width`/`aspect`）本来就是**这块板的事实**，不该是另一份文件的私有财产。
现在 `board:` 写在语言里，`asm.spec.json` 降级成 `assemble_carved.py` 写出来的产物。

## 派生规则（一条都不许在命令行上兜底）

| 产物字段 | 从语言哪里来 |
|---|---|
| `blocks` | `modules:` 的**实例序去重**（模块值）。这个序就是拼装序、也是原理图摆位序 |
| `board.width/height` | `board.assembly.width`（板宽上限）/ `board.assembly.height`（0=不定尺，由装箱定） |
| `board.layers/planes` | 原样搬（`board.planes` 同时是布线时跳过平面网的依据） |
| `board.assembly` | 原样搬（11 个板级事实，`layout_reuse.py` 用它定板，命令行不许盖） |
| `anchors` | `board.connectors`（写成 `模块别名.位号`） |
| `board.internal_connectors` | `board.internal_connectors`：**板上但不在板边**的接插件（见下） |
| `board.pours` | 拼完整板**真的铺出来**的网（`pour_2layer.py` 只铺这几网） |
| `board.plane_nets` | 源板靠平面连、块里必然散坨、**这一版不铺**的网（拼板门禁豁免，靠走线连） |
| `net_map` / `keep_nets` | **空**：裁块的位号全局唯一、块内网名唯一，不需要改名表 |

## 关于 `anchors` 的 x/y

填哨兵 `999/0`，**不是坐标**。它的意思只有一句："这只接插件必须顶在板边，摆哪儿交给装箱"。
朝向由 `board.assembly.conn_edge` 定；A 轨板上那只件的绝对朝向**不搬**——块是另一次重布
出来的，照搬会把针脚拧到块的另一侧（`layout_reuse.conn_rot_plan` 的 docstring 里记着这个病）。

## 板边接插件 vs 板上接插件（2026-10-30 加）

`board.connectors` 只列**要顶在板边、人从板外插进去**的那些。

板上还有另一类 `J*`：焊在板子中间、不从板边进出的——Tag-Connect 压接座（探针从上往下压）、
排针、测试点。它们**不能**写进 `board.connectors`：那里每一条都会让整块被搬到板边、按
`conn_inset` 顶死。`mcu_stm32f411.J2`（TC2030 SWD 座）正是这个反例——它的块里铜一直铺到
J2 焊盘外 2.85mm，顶到板边会把铜推出板外 2.2mm，装箱直接失败（`SLACK` 只有 0.2）。

所以另开一格 `board.internal_connectors`，**只干一件事**：让 `assemble_blocks.py` 那道
「每个 `J*` 都要有交代」的硬门知道它已被点名。它**不产生锚点、不影响装箱和朝向**。
"""
from __future__ import annotations

import re

SCHEMA = 'pcbstudio.assembly.v1'

SENTINEL = 999.0          # 见上面"关于 anchors 的 x/y"

#: 2 层板的铜层（`board.pours` 列表写法 = 两层都铺）
LAYERS_2 = ('F.Cu', 'B.Cu')
#: 内层名（跟 `sdi/spec.py` 的 `PLANE_LAYER_RE` 同义，本地再写一份免得跨模块依赖）
_PLANE_LAYER_RE = re.compile(r'^In\d+$')


class BoardSpecError(SystemExit):
    """语言里缺拼板要的东西。刻意硬退出：这些数以前靠命令行开关补齐，缺了也没人发现。"""


def _board(spec, src):
    board = spec.get('board')
    if not isinstance(board, dict):
        raise BoardSpecError(
            f'✗ {src}: 没有 `board:` 段——拼板要的板级事实（layers/planes/assembly/connectors）'
            f'写在这里，不写在命令行上')
    return board


def block_order(spec, src) -> list[str]:
    """拼装顺序 = `modules:` 的实例序（去重）。块库里的块名，不是模块别名。"""
    mods = spec.get('modules') or {}
    if not mods:
        raise BoardSpecError(f'✗ {src}: `modules:` 是空的，没有块可拼')
    out: list[str] = []
    for alias, bname in mods.items():
        if not isinstance(bname, str) or not bname:
            raise BoardSpecError(f'✗ {src}: modules.{alias} 要写成块名，收到 {bname!r}')
        if bname not in out:
            out.append(bname)
    return out


def _pairs(spec, src, key) -> list[tuple[str, str]]:
    """`board.<key>: [mcu.J3, usb.J1, …]` → [(块名, 块内位号)]。两条列表共用这套校验。"""
    board = _board(spec, src)
    mods = spec.get('modules') or {}
    items = board.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise BoardSpecError(f'✗ {src}: board.{key} 要写成列表（例 [mcu.J3, usb.J1]）')
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or '.' not in item:
            raise BoardSpecError(
                f'✗ {src}: board.{key}[{i}] 要写成 `模块别名.位号`（例 mcu.J3），收到 {item!r}')
        alias, ref = item.split('.', 1)
        bname = mods.get(alias)
        if not bname:
            raise BoardSpecError(
                f'✗ {src}: board.{key}[{i}] 的模块别名 {alias!r} 不在 modules 里'
                f'（有 {sorted(mods)}）')
        if not ref:
            raise BoardSpecError(f'✗ {src}: board.{key}[{i}] 缺位号（{item!r}）')
        out.append((bname, ref))
    return out


def connectors(spec, src) -> list[dict]:
    """`board.connectors: [mcu.J3, usb.J1, …]` → 拼板器的 `anchors`（x/y 是哨兵）。"""
    return [{'block': b, 'ref': r, 'x': SENTINEL, 'y': 0.0, 'rotation': 0.0}
            for b, r in _pairs(spec, src, 'connectors')]


def internal_connectors(spec, src) -> list[dict]:
    """`board.internal_connectors: [mcu.J2, …]` → 板上但**不在板边**的接插件。

    只给拼板链那道「每个 `J*` 都要有交代」的门当凭据；不产生锚点。
    """
    return [{'block': b, 'ref': r} for b, r in _pairs(spec, src, 'internal_connectors')]


def _net_names(board, src, key) -> list[str]:
    items = board.get(key)
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
        raise BoardSpecError(f'✗ {src}: board.{key} 要写成网名列表（例 [GND, 3V3]）')
    return list(items)


def pours(spec, src) -> dict[str, list[str]]:
    """`board.pours` → `{网: [铺在哪几层]}`，**真的整板铺出来**的那几网。

    两种写法：

        pours: [GND]                        # 列表 = 两层都铺（F.Cu + B.Cu）
        pours: {GND: [F.Cu], 3V3: [B.Cu]}   # 映射 = 每网指定层（2 层板的常规打法）

    跟 `board.planes` 是同一件事的两种载体：`planes` 是 4 层板的"某网铺在内层某一层"，
    `pours` 是 2 层板的"某网铺在外层某一层（或两层都铺）"。

    ⚠ **同一层上两张满铺的 zone 只有一张能活**：优先级高的那张把整层吃掉，低的那张
    只剩缝——2026-10-30 实测 `[GND, 3V3]` 都两层铺，3V3 抢光 F.Cu，36 只 GND 焊盘散成
    16 坨、3V3 自己 23 只 1 坨全通。所以两条电源轨要**各占一层**，或者只铺一张。

    铺铜兑现的是"网在层上的面"，前提是每只焊盘都能碰到那张面——只在别的层上的 SMD
    脚得先打过孔（`fanout_plane_pads.py` 按同一份语言补）。
    """
    board = _board(spec, src)
    items = board.get('pours')
    if items is None:
        return {}
    if isinstance(items, list):
        if not all(isinstance(v, str) for v in items):
            raise BoardSpecError(f'✗ {src}: board.pours 列表里只能是网名')
        return {n: list(LAYERS_2) for n in items}
    if not isinstance(items, dict):
        raise BoardSpecError(f'✗ {src}: board.pours 要么是网名列表，要么是 {{网: [层]}}')
    out = {}
    for net, layers in items.items():
        if isinstance(layers, str):
            layers = [layers]
        if not isinstance(layers, list) or not layers:
            raise BoardSpecError(f'✗ {src}: board.pours.{net} 要写铺在哪几层（例 [F.Cu]）')
        for lay in layers:
            if lay not in LAYERS_2 and not _PLANE_LAYER_RE.match(str(lay)):
                raise BoardSpecError(f'✗ {src}: board.pours.{net} 的层 {lay!r} 不认识'
                                     f'（只认 F.Cu / B.Cu / In<n>）')
        out[str(net)] = list(layers)
    return out


def plane_nets(spec, src) -> list[str]:
    """`board.plane_nets: [GND, 3V3]` → 源板靠**平面/铺铜**连、块里必然散坨的网。

    只干一件事：把它们从拼板器那道「块内一组」的门禁里摘出来（散坨是源板的画法
    决定的，不是块坏了）。连通性另算——在 `pours` 里的由整板重铺兑现，不在的由
    布线器按整网布通兑现。

    `pours` 里的网天然属于这一类，不必重复写（拼板器取两者的并集）。
    """
    return _net_names(_board(spec, src), src, 'plane_nets')


def to_assembly(spec: dict, src: str = '<spec>') -> dict:
    """整份 `.sdi.yaml` → `pcbstudio.assembly.v1`。缺什么就报什么，不给默认值。"""
    board = _board(spec, src)
    asm = board.get('assembly')
    if not isinstance(asm, dict):
        raise BoardSpecError(f'✗ {src}: board.assembly 没写——板宽/朝向/缝都定不下来')

    layers = board.get('layers')
    if not isinstance(layers, int) or layers < 2:
        raise BoardSpecError(f'✗ {src}: board.layers 要是 >=2 的整数，收到 {layers!r}')

    width = asm.get('width')
    if not isinstance(width, (int, float)) or isinstance(width, bool):
        raise BoardSpecError(f'✗ {src}: board.assembly.width 要写（板宽上限 mm）')
    height = asm.get('height', 0)
    if not isinstance(height, (int, float)) or isinstance(height, bool):
        raise BoardSpecError(f'✗ {src}: board.assembly.height 要是数字（0=不定尺）')

    return {
        '$schema': SCHEMA,
        '$generated_from': src,
        '$generated_by': ('sdi.boardspec.to_assembly —— 手改无效，改 .sdi.yaml 再重跑 '
                          'assemble_carved.py / assemble_blocks.py'),
        'project': spec.get('project') or {},
        'board': {
            'width': float(width),
            'height': float(height),
            'layers': layers,
            'mounting_holes': int(board.get('holes') or 0),
            # 孔径 / 孔心到板边：d2s 出板框按这两个数打孔，缺省 4.0（M3 要写 3.2）
            'hole_d': float(board.get('hole_d') or 4.0),
            'hole_edge_offset': float(board.get('hole_edge_offset') or 4.0),
            'planes': dict(board.get('planes') or {}),
            # 11 个板级事实原样进拼板链；`layout_reuse` 认它、命令行不许盖
            'assembly': dict(asm),
            # 板上但不在板边的接插件（Tag-Connect 压接座之类）：只喂拼板的 J* 硬门
            'internal_connectors': internal_connectors(spec, src),
            # **靠铺铜连的网**（2 层的 GND / 电源轨）：块里这些焊盘的铜本来就是靠
            # 源板的铺铜（内层平面或整板铺地）连的，块不搬铺铜 → 到块里必然散成多坨。
            # 拼板时整板重铺（`pour_2layer.py` 按这一格铺），所以连通性由重铺保证。
            'pours': pours(spec, src),
            # 同一类网里**这一版不铺**的（2 层板只容得下一张铺铜，见 `pours` 注释）：
            # 拼板门禁同样豁免，连通性交给布线器。
            'plane_nets': plane_nets(spec, src),
        },
        'blocks': block_order(spec, src),
        'net_map': {},
        'keep_nets': [],
        'anchors': connectors(spec, src),
    }
