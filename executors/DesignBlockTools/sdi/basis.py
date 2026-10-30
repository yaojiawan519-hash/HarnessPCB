#!/usr/bin/env python3
"""块卡 `basis:` 里抄的铜/孔/死铜/切口数，按板上 `placement.json` 的 `counts` 校正。

为什么要有这一步：这几个数算得出来，历史上是手抄进散文的，抄完就漂——2026-10-30 重抠
33 块 + 三轮空线头裁剪之后，卡里还写着裁剪前的段数，65 块对不上（`sdi card` 的
"散文数字门"抓的就是这一栏）。

**只改 `basis:` 那一块文本**：`notes:` 里同样的数字可能夹着刻意留下的历史值
（"carve 原值 N 段"、"首测 1348 段"），全文替换会把账本改成谎言。notes 里的旧数
单独列出来，人看着改。

改完顺手 `card.emit` 重算两份 json 产物——yaml 是权威，json 是产物；只改 yaml 不 emit
会让"卡回读门"红一片（2026-10-30 实测 65 块一起红）。

跑法（纯 python，不需要 KiCad）：
    python3 -m sdi.basis                 # 只报哪里对不上
    python3 -m sdi.basis --apply         # 改 `basis:` + emit
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import card

LIB = Path(__file__).resolve().parent.parent.parent / 'DesignBlocks'

# ⚠ 这几条正则跑在**原文**上（要拿 span 去替换），而 yaml 的折行会把 "铜 24 段 +\n 5 孔"
# 从中间劈开：字面空格的写法在折行处匹配不上 → 本工具报"0 块对不上"，`sdi card` 的散文
# 数字门（读的是解析后的字符串）却报 1 块不过——两把尺子给出相反结论（2026-10-31
# `power_5v_ldo` 实测）。所以标记之间一律用 `\s+`。
SUB = ((r'铜\s+(\d+)\s+段\s*\+\s*(\d+)\s+孔', ('copper_segs', 'vias'), '铜 {} 段 + {} 孔'),
       (r'死铜删\s+(\d+)', ('isolated_deleted',), '死铜删 {}'),
       (r'口岸切口\s+(\d+)', ('cut_ends',), '口岸切口 {}'))


def basis_span(text: str):
    """(start, end) 覆盖 `basis:` 这个键的值（含折行续行）。找不到返回 None。"""
    m = re.search(r'^([ \t]*)basis:[ \t]*', text, re.M)
    if not m:
        return None
    ind = len(m.group(1))
    start, pos = m.start(), m.end()
    while pos < len(text):
        nl = text.find('\n', pos)
        if nl < 0:
            return start, len(text)
        nxt = nl + 1
        end = text.find('\n', nxt)
        line = text[nxt:end if end >= 0 else len(text)]
        if line.strip() and (len(line) - len(line.lstrip())) <= ind:
            return start, nxt
        pos = nxt
    return start, len(text)


def fixes(text: str, counts: dict):
    """(basis 里该改的 [(旧串, 新串)], 其他栏目里对不上的旧串)。"""
    sp = basis_span(text)
    if not sp:
        return [], []
    seg = text[sp[0]:sp[1]]
    subs = []
    for pat, keys, fmt in SUB:
        m = re.search(pat, seg)
        if not m or any(counts.get(k) is None for k in keys):
            continue
        if tuple(int(g) for g in m.groups()) != tuple(counts[k] for k in keys):
            subs.append((m.group(0), fmt.format(*(counts[k] for k in keys))))
    rest = text[:sp[0]] + text[sp[1]:]
    other = [m.group(0) for pat, keys, _ in SUB for m in re.finditer(pat, rest)
             if any(counts.get(k) is not None for k in keys)
             and (len(m.groups()) != len(keys)
                  or tuple(int(g) for g in m.groups()) != tuple(counts[k] for k in keys))]
    return subs, other


#: 卡里出现这些词，说明那个数字是**故意留下的历史值**（"carve 交付 588 段"），
#: 不是当前状态 → 不改、也不该被门禁判红。2026-10-31 起约定：写历史就带标记。
HISTORY_MARK = ('原值', '交付时', '旧值', '首测', '裁前', '此前', '回滚前')


def _historical(text: str, m, span: int = 60) -> bool:
    a, b = max(0, m.start() - span), min(len(text), m.end() + span)
    return any(w in text[a:b] for w in HISTORY_MARK)


def prose_fixes(text: str, counts: dict):
    """`basis:` **以外**的散文里对不上的数字 → [(旧串, 新串, start, end)]。

    2026-10-31：`basis` 只有一处，扫起来容易；`geometry_status` / `notes` 里同样的
    "铜 N 段 + M 孔" 有近百处，漂了没人管（全库 65 块）。这里按原文 span 取改点，
    带历史标记的那句跳过。
    """
    sp = basis_span(text)
    if not sp:
        return []
    out = []
    for pat, keys, fmt in SUB:
        for m in re.finditer(pat, text):
            if sp[0] <= m.start() < sp[1] or len(m.groups()) != len(keys):
                continue
            if any(counts.get(k) is None for k in keys):
                continue
            if tuple(int(g) for g in m.groups()) == tuple(counts[k] for k in keys):
                continue
            if _historical(text, m):
                continue
            out.append((m.group(0), fmt.format(*(counts[k] for k in keys)), m.start(), m.end()))
    out.sort(key=lambda x: x[2], reverse=True)   # 从后往前改，span 不失效
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='按 counts 校正块卡散文里的板侧数字')
    ap.add_argument('--root', default=str(LIB), help='块库根目录')
    ap.add_argument('--apply', action='store_true', help='改 `basis:` 并 emit（默认只报）')
    ap.add_argument('--prose', action='store_true',
                    help='连 `basis:` 以外的散文一起改（跳过硬了历史标记的那句）')
    a = ap.parse_args(argv)

    root = Path(a.root)
    dirs = [d for d in sorted(root.iterdir()) if d.is_dir() and (d / 'block.sdi.yaml').is_file()] \
        if root.is_dir() else []
    if not dirs:
        raise SystemExit(f'✗ {root} 下没有块目录')

    n_fix = n_prose = n_hist = 0
    for d in dirs:
        pj, yml = d / f'{d.name}.placement.json', d / 'block.sdi.yaml'
        if not pj.is_file():
            continue
        counts = (json.loads(pj.read_text(encoding='utf-8')) or {}).get('counts') or {}
        text = yml.read_text(encoding='utf-8')
        subs, other = fixes(text, counts)
        pfix = prose_fixes(text, counts)
        hist = len(other) - len(pfix)          # 带历史标记、故意留着的那些
        if hist:
            n_hist += 1
            print(f'  ~ {d.name:22s} 带历史标记的旧数（不动）：'
                  + '、'.join(' '.join(o.split()) for o in other)
                  + '（`basis:` 里那处仍会计入下面的改法）')
        if not subs and not pfix:
            continue
        if subs:
            n_fix += 1
            print(f'  {d.name:22s} basis ' + '；'.join(f'{" ".join(x.split())} → {y}'
                                                       for x, y in subs))
        if pfix:
            n_prose += 1
            print(f'  {d.name:22s} 其它 ' + '；'.join(f'{" ".join(o.split())} → {y}'
                                                      for o, y, _, _ in pfix))
        if not a.apply:
            continue
        # 先按 span 改散文（从后往前，span 不失效），再在改过的文本里重定位 `basis:`
        for old, new, s, e in pfix:
            text = text[:s] + new + text[e:]
        sp = basis_span(text)
        if subs and sp:
            seg = text[sp[0]:sp[1]]
            for old, new in subs:
                seg = seg.replace(old, new)
            text = text[:sp[0]] + seg + text[sp[1]:]
        yml.write_text(text, encoding='utf-8')
        card.emit(d)
    print(f'\n{"" if a.apply else "（只报不改）"}basis 对不上的块 {n_fix} 块；'
          f'其它栏目对不上的块 {n_prose} 块；带历史标记未动的块 {n_hist} 块')
    return 0


if __name__ == '__main__':
    sys.exit(main())
