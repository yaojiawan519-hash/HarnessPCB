#!/usr/bin/env python3
"""块交付板的 **DRC 记账门**（2026-10-30）：短路**必须归零**，否则得在欠账本上点名。

为什么要有这一道：`sdi.cardpcb` 只比"卡和板说的是不是同一件事"，`sdi.spur` 只盯空线头，
**没有一道门看短路**。于是 `mcu_min_stm32` 板上 16 处电源铜压在 MCU 引脚上、`ldo_3v3` 把
LDO 输入输出短在一起，全库巡检一直是绿的——2026-10-30 补网名时才撞见（见
`NET_ALIGN_REPORT.md` §四）。短路这种账不能靠"谁想起来跑一次 DRC"。

判据就一条，不留解释空间：

* **`shorting_items` 必须是 0。**
* **器件自带的铜桥不算**：焊盘跳线（solder jumper）、PCB 天线馈点、net tie 的图形铜本来
  就无网、把两只不同网的铜连起来——那是**设计意图**里"让人拿烙铁短接"的那对脚，KiCad 只能
  报短路。判据两条，满足其一即算桥：① 短的两头同属一只器件、且至少一头是无网铜；
  ② 有一头是**器件图形**（`Polygon`/`Graphic … of <位号>`）——天线馈点压的常常是走线，
  走线不属于任何器件，只按 ① 会判成真短路。**算桥的只报数、不进门槛。**
* 老账（引擎自布、改名之前就有的）写进 `DesignBlocks/_drc_debt.yaml` 点名，**写的数就是上限**：
  实测 ≤ 记账＝算老账（打印、退 0）；实测 > 记账＝**新账**（退 1）。
* 别的 DRC 类型（丝印、间距、悬空铜、层叠…）**只报数不进门槛**——那些是继承来的噪声，
  每一块都不同，按总数判只会把门禁变成噪声源。要收紧时按类型单独升级（见 `--fail-on`）。

两个跑法上的事实：

* **必须带 `--refill-zones`**：块里存的铜皮是上次铺的结果，不重铺等于拿旧账判新板。
* **一次全库 ~20 分钟**（123 块，纯 kicad-cli）。所以读数按板文件指纹（大小 + mtime）缓存在
  `<块>/_geom/drc.json`；文件没动过就直接引用上一次的读数，门禁秒出。要重算是 `--force`。

跑法：
    python3 -m sdi.pcbdrc ../DesignBlocks              # 全库（有缓存就快）
    python3 -m sdi.pcbdrc ../DesignBlocks --force      # 全部重跑 DRC
    python3 -m sdi.pcbdrc ../DesignBlocks --only can_node,ldo_3v3
    python3 -m sdi.pcbdrc ../DesignBlocks --fail-on clearance   # 顺带把间距也当门
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

KICAD_CLI = [
    '/Volumes/Yao2026/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
    '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
]
DEBT = '_drc_debt.yaml'
#: 短路永远是错，其余类型默认只报数（按需用 `--fail-on` 升级）。
GATED = ('shorting_items',)


def _cli() -> str:
    for c in KICAD_CLI:
        if os.path.exists(c):
            return c
    raise SystemExit(f'✗ 找不到 kicad-cli（候选：{KICAD_CLI}）')


def fingerprint(pcb: Path) -> str:
    st = pcb.stat()
    return f'{st.st_size}:{st.st_mtime_ns}'


#: 器件自带的"图形铜"在 DRC 里读作 `[<no net>]`——**焊盘跳线（solder jumper）、PCB 天线馈点、
#: 网络连接片（net tie）** 的铜桥就长这样：一块无网多边形把两只不同网的焊盘连起来，这正是
#: 它们的设计意图（用户拿烙铁去短接的那对焊盘）。KiCad 只会老老实实报 `shorting_items`。
#: 判据：**短的两头同属一只器件**，且至少一头是无网铜 → 算"桥"，报数但不进门槛。
#: 实测样本：`fpga_ice40up5k` 的 SJ31/SJ34（走 ①）、`mcu_esp32s3` 的 AE1 天线 FEED（走 ②）。
NO_NET = {'<no net>', '', None}


def _ref(desc: str | None) -> str:
    """从 `Pad 1 [X] of SJ31 on F.Cu` / `Polygon [<no net>] of SJ31 on F.Cu` 取器件位号。"""
    if not desc:
        return ''
    tail = desc.split(' of ', 1)[-1]
    return tail.split(' on ', 1)[0].strip()


def classify(short: dict) -> str:
    nets, refs = short['nets'], {_ref(i) for i in short['items']}
    # ① 两头同属一只器件 + 至少一头无网铜 → 焊盘跳线/网络连接片
    if any(n in NO_NET for n in nets) and len(refs) == 1 and '' not in refs:
        return 'bridge'
    # ② 有一头是**器件自带的图形铜**（`Polygon [<no net>] of X`）→ 也是桥。
    #    2026-10-30 补：原来只认"两头都在同一只器件里"，被 `mcu_esp32s3` 的 AE1 天线
    #    馈点撞破——那里图形多边形压的是**走线**（不是焊盘），走线当然不属于任何器件，
    #    规则就判成真短路。这块板的源板 `Watchy.kicad_pcb` 上同样 3 处短路一字不差
    #    （已用 kicad-cli 核过），说明是**照抄来的器件图形**，不是我们布的铜。
    for i in short['items'] or []:
        head = (i or '').split(' [', 1)[0].strip()
        if head in ('Polygon', 'Graphic') and _ref(i):
            return 'bridge'
    return 'real'


def run_drc(pcb: Path) -> dict:
    """跑一次 kicad-cli DRC，返回 {指纹, 总违规, 未连通, 按类型计数, 短路逐条}。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'drc.json'
        r = subprocess.run([_cli(), 'pcb', 'drc', '--severity-all', '--refill-zones',
                            '--format', 'json', '-o', str(out), str(pcb)],
                           capture_output=True, text=True)
        if r.returncode or not out.is_file():
            raise SystemExit(f'✗ kicad-cli DRC 失败（{pcb}）：'
                             f'{(r.stderr or r.stdout).strip()[-400:]}')
        d = json.loads(out.read_text(encoding='utf-8'))
    viol = d.get('violations') or []
    kinds = collections.Counter(v.get('type') for v in viol)
    shorts = []
    for v in viol:
        if v.get('type') != 'shorting_items':
            continue
        inner = v.get('description', '').split('(', 1)[-1].rstrip(')')   # `nets A and B`
        inner = inner[len('nets '):] if inner.startswith('nets ') else inner
        pair = inner.split(' and ')
        s = {'nets': pair if len(pair) == 2 else [inner],
             'items': [i.get('description') for i in (v.get('items') or [])]}
        s['kind'] = classify(s)
        shorts.append(s)
    real = [s for s in shorts if s['kind'] == 'real']
    return {'pcb': fingerprint(pcb), 'violations': len(viol), 'unconnected':
            len(d.get('unconnected_items') or []), 'kinds': dict(sorted(kinds.items())),
            'shorts': real, 'bridges': [s for s in shorts if s['kind'] == 'bridge']}


def cached(pcb: Path) -> dict | None:
    f = pcb.parent / '_geom' / 'drc.json'
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001 —— 读数文件坏了就当没有
        return None
    return d if d.get('pcb') == fingerprint(pcb) else None


def store(pcb: Path, rep: dict) -> Path:
    f = pcb.parent / '_geom' / 'drc.json'
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return f


def debt(root: Path) -> dict:
    """欠账本：{块名: {shorts: 上限, why: 原因}}。没这本子就是"一块都不许短"。"""
    f = root / DEBT
    if not f.is_file():
        return {}
    import yaml
    doc = yaml.safe_load(f.read_text(encoding='utf-8')) or {}
    out = {}
    for k, v in doc.items():
        if k.startswith('_'):      # `_note:` 之类的注释键
            continue
        out[k] = {'shorts': int((v or {}).get('shorts') or 0),
                  'why': (v or {}).get('why') or ''}
    return out


def boards(path: Path, only: set[str]) -> list[Path]:
    if path.is_file():
        return [path]
    if (path / f'{path.name}.kicad_pcb').is_file():
        got = [path / f'{path.name}.kicad_pcb']      # 直接点单块目录
    else:
        got = sorted(p for d in path.iterdir() if d.is_dir() and not d.name.startswith('_')
                     for p in [d / f'{d.name}.kicad_pcb'] if p.is_file())
    if only:
        got = [p for p in got if p.stem in only]
    if not got:
        raise SystemExit(f'✗ {path} 下没有 <块名>.kicad_pcb'
                         + ('（--only 过滤后为空）' if only else ''))
    return got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='块交付板 DRC 记账门（短路必须为 0 或点名欠账）')
    ap.add_argument('path', help='块库根目录或单个 .kicad_pcb')
    ap.add_argument('--force', action='store_true', help='不吃缓存，全部重跑 DRC')
    ap.add_argument('--only', default='', help='只处理这些块（逗号分隔）')
    ap.add_argument('--fail-on', default='',
                    help='额外当门的 DRC 类型（逗号分隔，如 clearance,silk_over_copper）')
    ap.add_argument('--json', dest='json_out', help='机读汇总落盘')
    a = ap.parse_args(argv)

    root = Path(a.path).parent if Path(a.path).is_file() else Path(a.path)
    only = {x.strip() for x in a.only.split(',') if x.strip()}
    extra = {x.strip() for x in a.fail_on.split(',') if x.strip()}
    gate = set(GATED) | extra
    owed = debt(root)
    files = boards(Path(a.path), only)

    bad, cached_n, tot = [], 0, collections.Counter()
    rows = {}
    for pcb in files:
        name = pcb.stem
        rep = None if a.force else cached(pcb)
        if rep is None:
            rep = run_drc(pcb)
            store(pcb, rep)
        else:
            cached_n += 1
        rows[name] = rep
        n_short = len(rep['shorts'])
        tot['shorts'] += n_short
        tot['bridges'] += len(rep.get('bridges') or [])
        tot['violations'] += rep['violations']
        tot['unconnected'] += rep['unconnected']
        row = {k: rep['kinds'].get(k, 0) for k in gate}
        over = {k: v for k, v in row.items() if v > 0}
        # 短路按欠账本判：实测 ≤ 记账 = 老账；超出 = 新账
        if 'shorting_items' in over:
            lim = (owed.get(name) or {}).get('shorts', 0)
            if n_short <= lim:
                over.pop('shorting_items')
            else:
                bad.append(f'{name}：短路 {n_short} 处（欠账本记 {lim}）')
        if extra:
            for k in extra:
                if rep['kinds'].get(k, 0):
                    bad.append(f'{name}：{k} {rep["kinds"][k]} 处')
        flag = '✗' if (over or (extra and any(rep['kinds'].get(k) for k in extra))) else '·'
        old = (owed.get(name) or {}).get('shorts')
        tail = '' if not n_short else (f'（欠账 {old}，'
                                       + ('算老账）' if old is not None else '**新账**）'))
        nb = len(rep.get('bridges') or [])
        print(f'{flag} {name:24s} 违规 {rep["violations"]:4d} / 未连通 {rep["unconnected"]:3d}'
              f' / 短路 {n_short}{tail}' + (f' / 桥 {nb}' if nb else ''))

    print('—' * 60)
    print(f'{"✓" if not bad else "✗"} 块板 DRC 门：{len(files) - len(bad)} 块过 / {len(bad)} 块不过；'
          f'全库 短路 {tot["shorts"]} 处、器件内铜桥 {tot["bridges"]} 处（跳线/天线，不算账）、'
          f'违规 {tot["violations"]} 条、未连通 {tot["unconnected"]} 条'
          f'（{cached_n} 块读数来自缓存）')
    for b in bad:
        print('   ✗', b)
    if owed:
        print(f'   欠账本 {DEBT}：{len(owed)} 块（'
              + '、'.join(f'{k} {v["shorts"]}' for k, v in sorted(owed.items())) + '）')
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=1) + '\n',
                                    encoding='utf-8')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
