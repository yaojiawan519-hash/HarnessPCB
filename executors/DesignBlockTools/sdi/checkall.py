#!/usr/bin/env python3
"""块库四道门的**一次跑完**（2026-10-30）

为什么要有这个：这四道门是各写各的，分别跑就必然会漏跑一道——2026-10-30 之前
`mcu_min_stm32` 的 16 处短路躺了三天，就是因为"看短路"这道门根本不存在；而
`usb_ftdi_bridge` 的 `/EE_DAT` 被删断，是因为没人把空线头门和 DRC 一起看。

四道门（都由 `sdi/` 下的模块提供，这里只调度、不改判据）：

| 门 | 看什么 | 模块 |
|---|---|---|
| ① 卡↔板 | 卡里的每根网、每只脚，板上是不是同一件事 | `sdi.cardpcb` |
| ② 卡回读 | 块卡 yaml↔measured 能不能互相推出来 + 无名内部网登记 | `sdi card` |
| ③ 短路 | `kicad-cli drc` 的 `shorting_items`（硬门） | `sdi.pcbdrc` |
| ④ 空线头 | `track_dangling` / `via_dangling`，只留记账点名的口岸断头 | `sdi.spur` |

跑法：
    python3 -m sdi.checkall ../DesignBlocks          # 四道门
    python3 -m sdi.checkall ../DesignBlocks --quick   # 跳过 DRC 与空线头（秒级，改卡时用）

⚠ 每道门都是**子进程**：`pcbdrc`/`spur` 要开 KiCad 自带的 python，混在一个进程里会被
SWIG 毒化（见 `sdi/cardpcb.py` 顶上的两进程说明）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

GATES = [
    ('① 卡↔板', [sys.executable, '-m', 'sdi.cardpcb']),
    ('② 卡回读', [sys.executable, '-m', 'sdi', 'card', '--root', '.']),
    ('③ 短路', [sys.executable, '-m', 'sdi.pcbdrc']),
    ('④ 空线头', [sys.executable, '-m', 'sdi.spur']),
]
QUICK_SKIP = {'③ 短路', '④ 空线头'}


def strict_recheck(root: Path) -> list:
    """④ 报红时，把报了毛刺的块逐个**试删复判**一遍，返回仍红的块名。

    `sdi.spur` 的只读体检拿 `mates >= 2` 预筛认"载流断头"，这个预筛会**少数**：一根
    只挨一个同网对象的短线，删掉照样断网。2026-10-31 `usb_c_serial_s3` 实测——默认判
    `/USB2_D-` F.Cu 6.3mm 是毛刺，`--fix --strict` 逐项试删证明删了就断（改判载流断头，
    `placement.json` 里 `dangling.binds: 1` 就是这么记的）。门禁与修改器必须用同一把
    尺子，否则会出现"门红但块是好的"，人会去改本来对的板。

    ⚠ `--strict` **只在 `--fix` 下起作用**（它改的是候选集，不是判据）。所以复判必须
      在**临时副本**里跑 `--fix --strict`，判据是"**一段都没删**、跑完干净"：
      真毛刺会被删掉（副本被改动 → 真块仍红），被证明载流的会被改判并原样留着
      （副本与真块逐字节相同 → 不算不过）。真块一个字节不动。

    只在门已经红的时候才付这笔代价（每个候选一次 kicad-cli DRC，慢）。
    """
    r = subprocess.run([sys.executable, '-m', 'sdi.spur', str(root)],
                       cwd=HERE, capture_output=True, text=True)
    if r.returncode == 0:
        return []
    import re
    import shutil
    import tempfile
    suspects = [m.group(1) for m in re.finditer(r'^\s*✗\s+(\S+)\s+空线头', r.stdout, re.M)]
    still = []
    for name in suspects:
        d = root / name
        with tempfile.TemporaryDirectory(prefix=f'checkall_{name}_') as td:
            tmp = Path(td) / name
            shutil.copytree(d, tmp)
            s = subprocess.run([sys.executable, '-m', 'sdi.spur', str(tmp),
                                '--fix', '--strict'], cwd=HERE,
                               capture_output=True, text=True)
            # 副本被改了 = 真删了毛刺，那真块里这段还在 → 仍红
            changed = [p.name for p in d.glob('*.kicad_pcb')
                       if (tmp / p.name).exists()
                       and (tmp / p.name).read_bytes() != p.read_bytes()]
        if s.returncode == 0 and not changed:
            print(f'  ~ {name}：默认判据把它算成毛刺，试删门证明删了会断网'
                  f'（载流断头），一段没动 → 不算不过', flush=True)
        else:
            still.append(name)
    return still


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='块库四道门一次跑完')
    ap.add_argument('path', nargs='?', default='../DesignBlocks', help='块库根目录')
    ap.add_argument('--quick', action='store_true', help='只跑卡侧两道门（跳过 DRC / 空线头）')
    a = ap.parse_args(argv)
    root = Path(a.path).resolve()

    results = []
    for name, cmd in GATES:
        if a.quick and name in QUICK_SKIP:
            results.append((name, None, '跳过（--quick）'))
            continue
        print(f'—— {name} ——', flush=True)
        r = subprocess.run([*cmd, str(root)], cwd=HERE)
        ok, note = r.returncode == 0, f'退出码 {r.returncode}'
        if not ok and name == '④ 空线头':
            still = strict_recheck(root)
            ok = not still
            note = f'退出码 {r.returncode} → 试删门复判 {"仍红：" + "、".join(still) if still else "全过"}'
        results.append((name, ok, note))

    print('—' * 60)
    for name, ok, note in results:
        flag = '·' if ok is None else ('✓' if ok else '✗')
        print(f'  {flag} {name}：{note}')
    bad = [n for n, ok, _ in results if ok is False]
    print(('✓ 四道门全过' if not bad else f'✗ 不过的门：{"、".join(bad)}'))
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
