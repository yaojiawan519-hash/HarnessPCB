#!/usr/bin/env python3
"""挑一根「最坏的线」撕掉重布，用同一把尺子量前后——布线实验的最小闭环。

用户 2026-10-30：「也许我们可以挑选一根最坏的线重新布线」。所以这一步不是「把板做通」，
是**在真实铜上做单点对照实验**：同一块板、同一套板规，只换这一根线的走法，量出
长度/过孔/绕行比怎么变。换布线器、换参数、换网序，都能用这个闭环重复。

## 「一根线」是哪一根

按 `wires.py` 的**路径**（引脚↔引脚，中途经过岔口也照走），不按物理铜件——物理铜件
只到最近的岔口，撕它没有优化余地（两端固定的一小截），而用户要的是「这根引脚到那根
引脚连接得好不好」。路径里有 `waypoints`（中途必须还走到的岔口）和 `elements`
（从 A 到 B 排好的段+孔），重布时：

- **撕**：路径上的段全删；岔口上的**共用过孔保留**（别的分支靠它换层），度数 2 的
  过孔是自己的，一起删。
- **布**：从 A 出发，一段一段走到每个 waypoint，最后到 B。每个 waypoint 造一个
  只落在「那里本来有铜的那几层」上的假焊盘当终点，这样新线一定接得上原铜。
- **量**：`wires.analyse` 跑前后两版，逐项对比，再跑 kicad-cli DRC 确认没弄断。

## 最坏怎么算

`wires.py` 给出的三个数各按一项折算成「坏分」，权重写在代码里、逐条打印来源，
不是黑箱：

    badness = 0.5 × 长度mm + 6 × 过孔数 + 15 × max(0, 绕行比 − 1)

0.5 是「一寸长一寸坏」；6 是一个过孔 ≈12mm 铜的等价物（量产良率/成本口径）；
15 是「明明直线能到却绕了一圈」的价钱。

用法：
    python3 -m sdi.reroute <pcb> [--pick N] [--top 8] [--work DIR]
                                  [--unit path|wire] [--max-wp N]

不加 `--pick` 时只打分排行、不落地（先看榜再决定）。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / 'KiCAD_Access/src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'KiCAD_Access/src'))

KICAD_PY = ('/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/Frameworks/'
            'Python.framework/Versions/3.9/bin/python3.9')

try:
    from . import wires as W
except ImportError:
    sys.path.insert(0, str(HERE.parent))
    from sdi import wires as W


# ------------------------------------------------------------------ 打分
def badness(w: dict) -> dict:
    ln = w['length_mm']
    vias = w['vias']
    det = w.get('detour') or 1.0
    over = max(0.0, det - 1.0)
    return {'length': round(0.5 * ln, 2), 'vias': round(6.0 * vias, 2),
            'detour': round(15.0 * over, 2),
            'total': round(0.5 * ln + 6.0 * vias + 15.0 * over, 2)}


def rank(doc: dict, unit: str = 'path',
         net: str | None = None) -> list[tuple[float, str, dict]]:
    out = []
    for d in doc['nets']:
        if net and d['net'] != net:
            continue
        for w in d['paths' if unit == 'path' else 'wires']:
            out.append((badness(w)['total'], d['net'], w))
    out.sort(key=lambda t: -t[0])
    return out


def _pins(end: dict) -> str:
    if end['kind'] == 'pad':
        return '+'.join(end.get('pins') or ['pad'])
    return f'{end["kind"]}({end["at"][0]:.2f},{end["at"][1]:.2f})'


_end_label = _pins


# ------------------------------------------------------------------ KiCad python 侧
def _contain(line, a, b, tol=1e-3):
    """段 (a,b) 是否落在这根线的身上（共线且包含）→ 返回沿线长的 (t_lo, t_hi)。

    T 岔切分出来的「虚拟小段」在文件里没有对应的线——它是一根长线上的中段。
    要撕掉它，得把那根长线按它的两个端点切开，删中段、留两头的残段
    （共线切开，电学上与原来完全等价）。
    """
    x1, y1, x2, y2 = line
    dx, dy = x2 - x1, y2 - y1
    ln = math.hypot(dx, dy)
    if ln < 1e-9:
        return None
    ux, uy = dx / ln, dy / ln

    def proj(p):
        return (p[0] - x1) * ux + (p[1] - y1) * uy

    def off(p):
        return abs(-(p[0] - x1) * uy + (p[1] - y1) * ux)

    if off(a) > tol or off(b) > tol:
        return None
    ta, tb = proj(a), proj(b)
    if min(ta, tb) < -tol or max(ta, tb) > ln + tol:
        return None
    return (max(0.0, min(ln, min(ta, tb))), max(0.0, min(ln, max(ta, tb))))


def _rip(src: Path, dst: Path, plan: dict) -> tuple[int, int]:
    import pcbnew                                     # noqa: PLC0415

    board = pcbnew.LoadBoard(str(src))
    want = list(plan['segs'])
    want_via = list(plan['vias'])
    killed = 0
    # ⚠ 铜元素清单只取一次：KiCad 的 SWIG 在 board.Remove() 之后第二次调
    # board.GetTracks() 会抛 "SwigPyObject object is not iterable"（实测）。
    tracks = list(board.GetTracks())
    dead = set()

    def close(ax, ay, bx, by, tol=1e-3):
        return abs(ax - bx) <= tol and abs(ay - by) <= tol

    def geom(it):
        s, e = it.GetStart(), it.GetEnd()
        return (s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6)

    # 一轮搞定：虚拟小段是「包含关系」，精确相等只是它的特例（t 区间 = 整根）。
    # ⚠ 别分两轮：一根线若**恰好等于**某个想撕的段、同时又**包含**另一个想撕的
    # 虚拟小段，两轮写法会在第一轮把这根线删掉、第二轮就看不见它了（实测漏 1 个）。
    for k, it in enumerate(tracks):
        if isinstance(it, pcbnew.PCB_VIA):
            p = it.GetPosition()
            for v in want_via:
                if close(p.x / 1e6, p.y / 1e6, v['x'], v['y']):
                    board.Remove(it)
                    dead.add(k)
                    killed += 1
                    want_via.remove(v)
                    break
            continue
        if not isinstance(it, pcbnew.PCB_TRACK) or not want:
            continue
        lay = board.GetLayerName(it.GetLayer())
        x1, y1, x2, y2 = geom(it)
        hit = []
        for t in want:
            if t['layer'] != lay:
                continue
            r = _contain((x1, y1, x2, y2), (t['x1'], t['y1']), (t['x2'], t['y2']))
            if r:
                hit.append((t, r))
        if not hit:
            continue
        ln = math.hypot(x2 - x1, y2 - y1)
        ux, uy = (x2 - x1) / ln, (y2 - y1) / ln
        cuts = sorted({0.0, ln} | {v for _t, r in hit for v in r})
        lay_id, width, net = it.GetLayer(), it.GetWidth(), it.GetNet()
        board.Remove(it)
        dead.add(k)
        for a, b in zip(cuts, cuts[1:]):
            if b - a < 1e-3:                  # 不到 1µm 的残段接不了任何东西，DRC 只当悬空
                continue
            mid = (a + b) / 2
            gone = [t for t, r in hit if r[0] - 1e-6 <= mid <= r[1] + 1e-6]
            if gone:
                killed += 1
                for t in gone:
                    if t in want:
                        want.remove(t)
                continue
            nt = pcbnew.PCB_TRACK(board)
            nt.SetStart(pcbnew.VECTOR2I(int(round((x1 + a * ux) * 1e6)),
                                        int(round((y1 + a * uy) * 1e6))))
            nt.SetEnd(pcbnew.VECTOR2I(int(round((x1 + b * ux) * 1e6)),
                                      int(round((y1 + b * uy) * 1e6))))
            nt.SetWidth(width)
            nt.SetLayer(lay_id)
            nt.SetNet(net)
            board.Add(nt)

    board.Save(str(dst))
    if want or want_via:
        for t in want[:6]:
            sys.stderr.write(f'  [漏] 段 {t["layer"]} ({t["x1"]},{t["y1"]})→'
                             f'({t["x2"]},{t["y2"]}) w={t["w"]}\n')
        for v in want_via[:6]:
            sys.stderr.write(f'  [漏] 孔 ({v["x"]},{v["y"]})\n')
    return killed, len(want) + len(want_via)



def _apply(src: Path, dst: Path, plan: dict) -> int:
    import pcbnew                                     # noqa: PLC0415

    board = pcbnew.LoadBoard(str(src))
    net = board.FindNet(plan['net'])
    if net is None:
        raise SystemExit(f'板子上没有网 {plan["net"]}')

    def mm(v):
        return int(round(v * 1e6))

    n = 0
    for s in plan['segs']:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(s['x1']), mm(s['y1'])))
        t.SetEnd(pcbnew.VECTOR2I(mm(s['x2']), mm(s['y2'])))
        t.SetWidth(mm(s['w']))
        t.SetLayer(board.GetLayerID(s['layer']))
        t.SetNet(net)
        board.Add(t)
        n += 1
    for v in plan['vias']:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(v['x']), mm(v['y'])))
        via.SetWidth(mm(v['size']))
        via.SetDrill(mm(v['drill']))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(board.GetLayerID('F.Cu'), board.GetLayerID('B.Cu'))
        via.SetNet(net)
        board.Add(via)
        n += 1
    board.Save(str(dst))
    return n


def _kicad_step(mode: str, src: Path, dst: Path, plan_json: Path) -> str:
    plan = json.loads(plan_json.read_text(encoding='utf-8'))
    if mode == 'rip':
        killed, missing = _rip(src, dst, plan)
        return f'{killed} {missing}'
    return str(_apply(src, dst, plan))


# ------------------------------------------------------------------ 布
def _path_legs(path: dict) -> list[tuple[list[dict], dict | None]]:
    """路径 → 一腿一腿：[(这一腿的段, 这腿走到的岔口)]，最后一腿的岔口是 None。

    按**节点身份**切，不按坐标切：并查集把落在铜搭接距离内的两个端点并成同一个节点，
    但两段的原始端点坐标可以差几百分之一毫米（2026-10-30 实测 7 处），拿坐标去比会漏岔口、
    少出腿，岔路就接不回去了。
    """
    wps = {tuple(w['node']): w for w in path['waypoints']}
    legs, cur = [], []
    for e in path['elements']:
        if e['kind'] != 'seg':
            continue
        cur.append(e)
        w = wps.get(tuple(e['nb'])) if e.get('nb') else None
        if w is not None:
            legs.append((cur, w))
            cur = []
    if cur:
        legs.append((cur, None))
    return legs


def _dedup(segs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for s in segs:
        k = (s['layer'], round(s['x1'], 3), round(s['y1'], 3),
             round(s['x2'], 3), round(s['y2'], 3))
        k2 = (s['layer'], k[3], k[4], k[1], k[2])
        if k in seen or k2 in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def reroute(pcb: Path, path: dict, net: str, work: Path,
            clearance=0.2, width=0.25, via=(0.6, 0.3), cfg_over=None) -> dict:
    from kicad_access.route.astar import GridRouter, RouteConfig   # noqa: PLC0415
    from kicad_access.route.model import Pad, RouteBoard           # noqa: PLC0415

    cfg_over = cfg_over or {}
    net = net or path['net']
    legs = _path_legs(path)
    segs_rip = _dedup([{'layer': e['layer'], 'w': e['w'], 'x1': e['x1'], 'y1': e['y1'],
                        'x2': e['x2'], 'y2': e['y2']} for e in path['elements']
                       if e['kind'] == 'seg'])
    # 岔口上的过孔是**共用**的：别的分支靠它在两层之间换层，撕掉就把别人的铜断了
    vias_rip = [{'x': e['at'][0], 'y': e['at'][1], 'size': e['size'], 'drill': e['drill']}
                for e in path['elements'] if e['kind'] == 'via' and not e.get('shared')]
    n_sh = sum(1 for e in path['elements'] if e['kind'] == 'via' and e.get('shared'))
    plan = {'net': net, 'segs': segs_rip, 'vias': vias_rip}
    pj = work / 'rip.json'
    pj.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    ripped = work / 'ripped.kicad_pcb'
    if ripped.exists():
        ripped.unlink()
    r = subprocess.run([KICAD_PY, '-m', 'sdi.reroute', '--do-rip', str(pcb),
                        str(ripped), str(pj)], cwd=str(HERE.parent),
                       capture_output=True, text=True)
    if not ripped.is_file():
        raise SystemExit('撕线失败：\n' + (r.stderr or r.stdout)[-1200:])
    try:
        killed, missing = (int(x) for x in r.stdout.strip().splitlines()[-1].split())
    except ValueError:
        raise SystemExit('撕线结果读不出：\n' + (r.stderr or r.stdout)[-1200:])
    print(f'  撕线：删 {killed} 个铜元素（{len(segs_rip)} 段 + {len(vias_rip)} 孔，'
          f'保住 {n_sh} 个共用过孔）')
    if missing:
        leak = '\n'.join(ln for ln in r.stderr.splitlines() if '[漏]' in ln)
        raise SystemExit(f'✗ 有 {missing} 个铜元素没撕到——撕线不完整，重布结果不可信，'
                         f'已中止（板子未改动）\n{leak}')

    board = RouteBoard(ripped)
    w_by_net = {}
    for s in list(board.segments) + list(board.vias):
        w_by_net[s.net] = max(w_by_net.get(s.net, 0.0), getattr(s, 'width', 0.0))
    pads = {f'{p.ref}.{p.num}': p for p in board.pads if p.net == net}
    routers: dict[float, object] = {}

    def router_for(w):
        # 线宽按**这一腿**原来的宽度：同网别处可能有 1.0mm 总线，按网上限布会把
        # 细间距焊盘的出口全封死（实测 R5.2 栅格锚与轴向出口全灭）。
        if w in routers:
            return routers[w]
        widths = {n: max(w_by_net.get(n, 0.0), width) for n in board.net_names()}
        widths[net] = w
        knobs = dict(step=0.2, clearance=clearance, width=w,
                     via_size=via[0], via_drill=via[1], via_cost=0.4, turn_cost=0.25,
                     diag45=True, diag_node_cap=400_000, h_weight=1.2)
        knobs.update(cfg_over)
        cfg = RouteConfig(**knobs)
        routers[w] = GridRouter(board, cfg, widths, layers=('F.Cu', 'B.Cu'))
        return routers[w]

    def pad_for(stop: dict, tag: str):
        if stop['kind'] == 'pad':
            cands = [pads[k] for k in stop.get('pins', []) if k in pads]
            if not cands:
                raise SystemExit(f'{stop["pins"]} 不在重布板上（网 {net}）')
            x, y = stop['at']
            return min(cands, key=lambda p: math.hypot(p.x - x, p.y - y))
        # 岔口：造一个只落在「那里本来有铜的那几层」上的假焊盘。层选错新线就接不上
        lays = tuple(l for l in (stop.get('layers') or ['F.Cu']) if l in ('F.Cu', 'B.Cu'))
        return Pad(ref='REROUTE', num=tag, net=net, x=stop['at'][0], y=stop['at'][1],
                   w=width, h=width, shape='circle', layers=lays or ('F.Cu',))

    stops = [{'kind': 'pad', **path['from']}]
    for _segs, wp in legs[:-1]:
        stops.append({'kind': 'wp', **wp})
    stops.append({'kind': 'pad', **path['to']})

    # --unit wire 时两头的端点可能不是焊盘（岔口/断头），那张单子上没记层；
    # 拿挨着它的那截铜的层补上，否则会拿 F.Cu 去接一根 B.Cu 的线。
    seg_layers: dict[tuple, set] = {}
    for e in path['elements']:
        if e['kind'] != 'seg':
            continue
        for k in ((round(e['x1'], 3), round(e['y1'], 3)), (round(e['x2'], 3), round(e['y2'], 3))):
            seg_layers.setdefault(k, set()).add(e['layer'])
    for s in stops:
        if s['kind'] != 'pad' and not s.get('layers'):
            s['layers'] = sorted(seg_layers.get((round(s['at'][0], 3),
                                                 round(s['at'][1], 3)), {'F.Cu'}))

    new_segs, new_vias = [], []
    cur = pad_for(stops[0], 'a')
    for i, (segs_leg, _wp) in enumerate(legs):
        nxt = pad_for(stops[i + 1], f'wp{i}')
        # 线宽取**这一腿起步那一段**的宽度，不取整腿最大（2026-10-30 实测踩到）：
        # 一腿可以跨过线宽变化（细间距焊盘出口 0.25 → 电源干路 0.4），取 max 就等于
        # 拿 0.4 去挤 0.5mm 节距的焊盘——`mcu_stm32f411` 的 `3V3 U2.9` 正是这样
        # 被判「栅格锚与轴向出口全被封死」，而原位铜明明是 0.25 出去的。
        # 起步那一段才是必须从当前落点逃出来的那根；它多宽，这一腿就按多宽布。
        w_leg = segs_leg[0]['w']
        if len({s['w'] for s in segs_leg}) > 1:
            print(f'    腿 {i + 1}/{len(legs)} 线宽 {sorted({s["w"] for s in segs_leg})}'
                  f' → 按起步段 {w_leg}mm 布')
        try:
            segs, vias = router_for(w_leg).route(net, cur, nxt)
        except ValueError as e:
            raise SystemExit(f'第 {i + 1}/{len(legs)} 腿布不通'
                             f'（{_pins(stops[i])} → {_pins(stops[i + 1])}）：{e}')
        router_for(w_leg).commit(segs, vias)
        new_segs.extend(segs)
        new_vias.extend(vias)
        cur = nxt

    # 撕掉的过孔必须补回来：岔口的假焊盘只是**模型里的**两层焊盘，不落真铜。
    # 后一腿从这层走、前一腿从那层走，中间少了真过孔就是断的（实测 2026-10-30：
    # B.Cu 0.582mm ⇄ F.Cu 0.0934mm 一对未连通）。这里只补「本来有、被我撕掉」的，
    # 引擎自己新加的孔照旧算它的本事。
    lost = {(round(v['x'], 3), round(v['y'], 3)) for v in vias_rip}
    have = {(round(v.x, 3), round(v.y, 3)) for v in new_vias}

    def _touch(layer: str, x: float, y: float, size: float) -> bool:
        """新铜在这一点上有没有挨着（同层、圆心距 ≤ 半径和）。"""
        for s in new_segs:
            if s.layer != layer:
                continue
            dx, dy = s.x2 - s.x1, s.y2 - s.y1
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - s.x1) * dx + (y - s.y1) * dy) / L2))
            if math.hypot(x - (s.x1 + t * dx), y - (s.y1 + t * dy)) <= (s.width + size) / 2:
                return True
        return False

    back = 0
    for s in stops:
        if s['kind'] != 'wp' or not s.get('via'):
            continue
        k = (round(s['at'][0], 3), round(s['at'][1], 3))
        if k not in lost or k in have:
            continue
        # 两腿都落在同一层时这孔是多余的，补回去只会变成 via_dangling（实测 2026-10-30：
        # (33.363,4.179) 两边都是 F.Cu，B.Cu 一片空）。
        if not (_touch('F.Cu', s['at'][0], s['at'][1], s['via']['size']) and
                _touch('B.Cu', s['at'][0], s['at'][1], s['via']['size'])):
            continue
        from kicad_access.route.model import Via      # noqa: PLC0415
        new_vias.append(Via(x=s['at'][0], y=s['at'][1], size=s['via']['size'],
                            drill=s['via']['drill'], net=net))
        back += 1

    # 引擎会吐出几微米长的退化段（4e-05 / 2.8e-05 mm），DRC 逐条算 track_dangling。
    # 短于 1µm 的段不承载任何连接，直接丢掉。
    tiny = [s for s in new_segs if math.hypot(s.x2 - s.x1, s.y2 - s.y1) < 1e-3]
    if tiny:
        new_segs = [s for s in new_segs if s not in tiny]
    print(f'  重布：{len(legs)} 腿 → {len(new_segs)} 段 / {len(new_vias)} 孔'
          + (f'（补回被撕的过孔 {back} 个）' if back else '')
          + (f'（丢掉 {len(tiny)} 段退化段）' if tiny else ''))

    new = {
        'net': net,
        'segs': [{'x1': round(s.x1, 4), 'y1': round(s.y1, 4), 'x2': round(s.x2, 4),
                  'y2': round(s.y2, 4), 'w': s.width, 'layer': s.layer} for s in new_segs],
        'vias': [{'x': round(v.x, 4), 'y': round(v.y, 4), 'size': v.size,
                  'drill': v.drill} for v in new_vias],
    }
    aj = work / 'route.json'
    aj.write_text(json.dumps(new, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    final = work / 'rerouted.kicad_pcb'
    if final.exists():
        final.unlink()
    r = subprocess.run([KICAD_PY, '-m', 'sdi.reroute', '--do-apply', str(ripped),
                        str(final), str(aj)], cwd=str(HERE.parent),
                       capture_output=True, text=True)
    if not final.is_file():
        raise SystemExit('落线失败：\n' + (r.stderr or r.stdout)[-1200:])
    ln = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in new_segs)
    return {'pcb': final, 'segs': len(new_segs), 'vias': len(new_vias),
            'length_mm': round(ln, 3), 'legs': len(legs),
            'layers': sorted({s.layer for s in new_segs})}



# ------------------------------------------------------------------ 主流程
def _find_after(after: dict, net: str, path: dict):
    """重布之后把同一条连接找回来（按两头引脚名比，坐标会变）。"""
    key = (tuple(path['from'].get('pins') or ()), tuple(path['to'].get('pins') or ()))
    for d in after['nets']:
        if d['net'] != net:
            continue
        for p in d['paths']:
            k = (tuple(p['from'].get('pins') or ()), tuple(p['to'].get('pins') or ()))
            if k == key or k[::-1] == key:
                return p
    return None


def _drc_once(pcb: Path) -> dict:
    from . import blockpcb                              # noqa: PLC0415
    keys = ('track_dangling', 'via_dangling', 'clearance', 'unconnected', 'total')
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'drc.json'
        subprocess.run([blockpcb.kcli(), 'pcb', 'drc', '--severity-all', '--format',
                        'json', '-o', str(out), str(pcb)], capture_output=True, text=True)
        if not out.is_file():
            return {k: -1 for k in keys}
        doc = json.loads(out.read_text(encoding='utf-8'))
    c = collections.Counter(v.get('type') for v in doc.get('violations', []))
    c['unconnected'] = len(doc.get('unconnected_items', []))
    c['total'] = len(doc.get('violations', []))
    return {k: c.get(k, 0) for k in keys}


def _drc(pcb: Path, runs: int = 2) -> dict:
    """kicad-cli DRC → 分类计数。断头/连通/间距这三件事只认它，不认自己算的几何。

    跑两遍取**逐项最大**：首次调用偶尔少报几条（实测同一块板首调 15 条间距、再调 18 条），
    少报只会让门禁放行，所以取最大。
    """
    got = [_drc_once(pcb) for _ in range(runs)]
    return {k: max(g[k] for g in got) for k in got[0]}


def _drc_txt(d: dict) -> str:
    return (f'悬空{d["track_dangling"]} 孔悬空{d["via_dangling"]} 间距{d["clearance"]} '
            f'未连通{d["unconnected"]} 总{d["total"]}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='sdi.reroute',
                                 description='挑最坏的线撕掉重布，前后对比')
    ap.add_argument('pcb')
    ap.add_argument('--pick', type=int, default=0, help='重布第几条（1 = 最坏）')
    ap.add_argument('--try', dest='try_', type=int, default=None,
                    help='从 --pick 起最多试几条（默认：单条 1，--sweep 时 4）')
    ap.add_argument('--net', default='', help='只看这根网的坏分榜（如 MCU_NRST）')
    ap.add_argument('--sweep', type=int, default=0,
                    help='一轮一轮重布「当前最坏那条」，最多 K 轮（每轮重排榜）')
    ap.add_argument('--top', type=int, default=8)
    ap.add_argument('--work', default='')
    ap.add_argument('--clearance', type=float, default=0.2)
    ap.add_argument('--width', type=float, default=0.25)
    ap.add_argument('--unit', choices=['path', 'wire'], default='path',
                    help='path=引脚↔引脚的连接（默认）；wire=物理铜件')
    ap.add_argument('--max-wp', type=int, default=0, help='岔口多于此数的不重布（0=不限）')
    # 引擎旋钮：非默认值全打出来，好让「同一块板换个参数」变成可比实验
    ap.add_argument('--step', type=float, default=0.2, help='栅格步长 mm')
    ap.add_argument('--anchor-radius', type=float, default=0.6,
                    help='焊盘栅格锚定半径 mm（细间距逃逸不够大时会「锚点全封死」）')
    ap.add_argument('--via-size', type=float, default=0.6)
    ap.add_argument('--via-drill', type=float, default=0.3)
    ap.add_argument('--turn-cost', type=float, default=0.25)
    ap.add_argument('--via-cost', type=float, default=0.4)
    ap.add_argument('--h-weight', type=float, default=1.2)
    a = ap.parse_args(argv)
    knobs = {'step': a.step, 'anchor_radius': a.anchor_radius, 'via_cost': a.via_cost,
             'turn_cost': a.turn_cost, 'h_weight': a.h_weight}
    default = {'step': 0.2, 'anchor_radius': 0.6, 'via_cost': 0.4,
               'turn_cost': 0.25, 'h_weight': 1.2}
    changed = {k: v for k, v in knobs.items() if abs(v - default[k]) > 1e-9}

    # KiCad python 的两步子命令在 __main__ 里先拦截，这里只管主流程
    pcb = Path(a.pcb)
    if not pcb.is_file():
        sys.exit(f'找不到 {pcb}')
    doc = W.analyse(pcb)
    rk = rank(doc, a.unit, net=a.net or None)
    bs0 = doc['board_stats']
    print(f'{doc["board"]}: {bs0["nets"]} 网 / {bs0["wires"]} 线 / {bs0["paths"]} 路 / '
          f'坏分榜 {len(rk)} 名（按{a.unit == "path" and "连接" or "铜件"}）')
    print('  坏分 = 0.5×长度 + 6×过孔 + 15×max(0, 绕行比−1)')
    for i, (score, net, w) in enumerate(rk[:a.top], 1):
        b = badness(w)
        extra = (f' 岔口{len(w["waypoints"])}' if a.unit == 'path' else '')
        print(f'  {i:2d}. {score:7.2f}  {net:<16} {w["length_mm"]:6.2f}mm '
              f'{w["vias"]}孔 绕行{w.get("detour") or 0:5.2f}  '
              f'{_pins(w["from"])} → {_pins(w["to"])}{extra}'
              f'   （长{b["length"]} 孔{b["vias"]} 绕{b["detour"]}）')
    if not a.pick and not a.sweep:
        print('\n加 --pick N 撕掉第 N 条重布；加 --sweep K 一轮一轮重布「当前最坏那条」，'
              '最多 K 轮（每轮重排榜）。')
        return 0

    d0 = _drc(pcb)
    bs0 = doc['board_stats']
    root = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix='reroute_'))
    cur, rows, tried_all, skip = pcb, [], [], set()
    n_target = a.sweep or 1
    k0 = a.pick or 1
    n_try = a.try_ or (4 if a.sweep else 1)
    for rnd in range(n_target):
        dcur = doc if rnd == 0 else W.analyse(cur)
        rk = rank(dcur, a.unit, net=a.net or None)
        ks = 1 if a.sweep else k0
        got = None
        attempts = 0
        for k, (score, net, unit) in enumerate(rk, 1):
            if attempts >= n_try:
                break
            if k < ks:
                continue
            key = (net, tuple(unit['from'].get('pins') or ()),
                   tuple(unit['to'].get('pins') or ()))
            if key in skip:
                continue
            if a.max_wp and len(unit.get('waypoints') or ()) > a.max_wp:
                if rnd == 0:
                    tried_all.append((k, f'要过 {len(unit["waypoints"])} 个岔口 > --max-wp'))
                    print(f'  跳过第 {k} 条：{tried_all[-1][1]}')
                skip.add(key)
                continue
            attempts += 1
            tag = f'[第 {rnd + 1}/{n_target} 轮] ' if a.sweep else ''
            print(f'\n{tag}重布第 {k} 条（坏分 {score}）：{net} '
                  f'{_pins(unit["from"])} → {_pins(unit["to"])}'
                  f'（{unit["length_mm"]}mm / {unit["vias"]}孔 / '
                  f'绕行{unit.get("detour") or 0:.2f} / {len(unit.get("waypoints") or ())} 岔口）')
            if changed:
                print('  引擎参数（非默认）：' +
                      '、'.join(f'{key}={v}' for key, v in sorted(changed.items())))
            work = root / f'r{rnd + 1}'
            work.mkdir(parents=True, exist_ok=True)
            try:
                res = reroute(cur, unit, net, work, clearance=a.clearance, width=a.width,
                              via=(a.via_size, a.via_drill), cfg_over=knobs)
            except SystemExit as e:
                why = str(e).strip().splitlines()[0]
                tried_all.append((k, why))
                skip.add(key)
                print(f'  ✗ 第 {k} 条布不通：{why}')
                continue
            got = (k, score, net, unit, res)
            break
        if got is None:
            print('\n这一轮没有能重布的（都布不通或超 --max-wp）。')
            break
        _k, score, net, unit, res = got
        print(f'  重布结果：{res["legs"]} 腿 / {res["segs"]} 段 / {res["vias"]} 孔 / '
              f'{res["length_mm"]}mm / {res["layers"]}')
        after = W.analyse(res['pcb'])
        nw = _find_after(after, net, unit)
        rows.append({'net': net, 'pins': f'{_pins(unit["from"])} → {_pins(unit["to"])}',
                     'before': (unit['length_mm'], unit['vias']),
                     'after': (nw['length_mm'], nw['vias']) if nw else None})
        if nw:
            print(f'  这条连接  {unit["length_mm"]:6.2f}mm {unit["vias"]}孔  →  '
                  f'{nw["length_mm"]:6.2f}mm {nw["vias"]}孔')
        else:
            print('  ⚠ 重布后这条连接没找回来（可能被并进了别的分支），看整板数')
        cur = res['pcb']
        if not a.sweep:
            break

    if not rows:
        print('\n一条都没重布成功：')
        for k, why in tried_all:
            print(f'  第 {k} 条：{why}')
        return 2

    final = W.analyse(cur)
    bs1 = final['board_stats']
    print('\n前后对照：')
    for r in rows:
        aft = (f'{r["after"][0]:6.2f}mm {r["after"][1]}孔' if r['after']
               else '        （没找回）')
        print(f'  {r["net"]:<12} {r["pins"]:<24} {r["before"][0]:6.2f}mm '
              f'{r["before"][1]}孔  →  {aft}')
    print(f'  整板      {bs0["copper_mm"]}mm {bs0["vias"]}孔 分片{bs0["split_net_count"]} '
          f'断对{bs0["pairs_unreached"]}'
          f'  →  {bs1["copper_mm"]}mm {bs1["vias"]}孔 分片{bs1["split_net_count"]} '
          f'断对{bs1["pairs_unreached"]}')

    # 判据只认 kicad-cli DRC：几何自算的悬空数**会漏**（2026-10-30：八条 4e-05mm 的
    # 退化段 DRC 逐条算 track_dangling，自算的「悬空 0」一条没报出来）。
    d1 = _drc(cur)
    print(f'  DRC       {_drc_txt(d0)}  →  {_drc_txt(d1)}')
    worse = [k for k in ('track_dangling', 'via_dangling', 'unconnected', 'clearance')
             if d1[k] > d0[k]]
    bad = bool(worse) or bs1['split_net_count'] > bs0['split_net_count'] \
        or bs1['pairs_unreached'] > bs0['pairs_unreached']
    if bad:
        print(f'  ✗ 这一版把板子弄坏了（{("、".join(worse)) or "断对/分片变多"}）'
              '——实验结论：换法不可接受')
    else:
        print('  ✓ DRC 逐项都没变差')
    print(f'  产物：{cur}')
    return 1 if bad else 0


if __name__ == '__main__':
    # 子进程模式：--do-rip / --do-apply 由 _kicad_step 用位置参数调起
    if len(sys.argv) > 1 and sys.argv[1] in ('--do-rip', '--do-apply'):
        mode = 'rip' if sys.argv[1] == '--do-rip' else 'apply'
        print(_kicad_step(mode, Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
        raise SystemExit(0)
    raise SystemExit(main())