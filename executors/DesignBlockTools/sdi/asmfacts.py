"""板级"怎么拼"的事实：`board.assembly`——让一块板**只凭 spec 就复现得出来**。

## 为什么要有这个模块

用户 2026-10-28 给语言定的验收尺子是：**一个新用户只拿 `sdi/1` 文件，能不能造出同一块板**。
按这把尺子量，`STM32Carve4` 当时是**造不出来**的：它的板级事实有一半在命令行上——

    layout_reuse.py … --conn-edge E --conn-inset 0.65 --width 60 --aspect 3.0 \
                        --nest --nest-gap 1.0 --nest-court-gap 0.25

spec 里只有一个 `board.planes`。换个人拿同一份 spec 不带这些开关跑，出来的是另一块板
（不嵌套、接插件随便朝、焊盘压板边）。这些数不是"跑一次试验的参数"，是**这块板的事实**，
所以位置在 `board.assembly`，不在命令行。

## 归口与覆盖规则（一条硬规则）

- spec 写了 → **以 spec 为准**；
- spec 写了、命令行又给了个不一样的值 → **直接报错**（"spec 单独复现不了"就是这个错的后果）。
  要改就改 spec，命令行不许悄悄盖过它；
- spec 没写、命令行给了 → 用命令行的，但**打印一行 `!`**说明"这块板不可单凭 spec 复现"；
- 两边都没有 → 用默认值，同样打印出来（默认值也是事实的一部分）。

三条都打印、都记账，`layout_reuse` 把它们原样写进 `barm_report.json` 的 `board.assembly_source`：
规则要能事后看出"这个数是谁定的"。

## 键表（多一个少一个都不行）

`conn_edge` 接插件统一朝哪条边（N/E/S/W，空=按 spec 锚点）· `conn_inset` 接插件焊盘离板边 mm ·
`conn_order` 接插件沿边的先后（块名列表）· `nest` 允许块外接矩形重叠 ·
`nest_gap` 块间**铜**间距 mm · `nest_court_gap` 器件 courtyard 间距 mm ·
`width`/`height` 板宽上限/定尺板高（height=0 表示不定尺）· `aspect` 最长宽比 ·
`margin` 板框到块的余量 mm · `gap` 不嵌套时的块间距 mm。

**没有坐标类键**：这些是"板要多宽、嘴朝哪、缝留多少"，不是把某个器件挪到某处。
"""
from __future__ import annotations

DEFAULTS = {
    'conn_edge': '',          # 空 = 不强制，按各块 spec 锚点
    'conn_inset': 0.0,        # 焊盘压在板框上（B 臂原行为）；排针/端子要给 >=0.5
    'conn_order': [],         # 接插件沿边的先后（块名，从边起点到终点）
    'nest': False,
    'nest_gap': 1.0,
    'nest_court_gap': 0.25,
    'width': 60.0,
    'height': 0.0,
    'aspect': 3.0,
    'margin': 2.0,
    'gap': 4.0,
}

EDGES = ('N', 'E', 'S', 'W')

# 键 → (类型, 下限, 上限, 说明)。数值键一律走这张表，别在调用处散着判。
_NUM = {
    'conn_inset':     (0.0, None, '接插件焊盘离板边 mm'),
    'nest_gap':       (0.0, None, '块间铜间距 mm'),
    'nest_court_gap': (0.0, None, '器件 courtyard 间距 mm'),
    'width':          (1e-6, None, '板宽上限 mm'),
    'height':         (0.0, None, '定尺板高 mm（0=不定尺）'),
    'aspect':         (1.0 + 1e-9, None, '最长宽比'),
    'margin':         (0.0, None, '板框余量 mm'),
    'gap':            (0.0, None, '块间距 mm'),
}

HINT = ('`board.assembly` 可写：conn_edge / conn_inset / conn_order / nest / nest_gap / '
        'nest_court_gap / width / height / aspect / margin / gap')


def _num_err(k, v):
    lo, hi, why = _NUM[k]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return f'board.assembly.{k}: 要数字（{why}），收到 {v!r}'
    if lo is not None and v <= lo - 1e-12 and not (k == 'height' and v == 0):
        return f'board.assembly.{k}: {v} 太小（{why}，下限 {lo}）'
    if hi is not None and v > hi:
        return f'board.assembly.{k}: {v} 太大（{why}，上限 {hi}）'
    return None


def check(asm) -> list[str]:
    """查一张 `board.assembly` 表本身对不对。返回错误串（空 = 过）。"""
    if not isinstance(asm, dict):
        return [f'board.assembly: 要映射，收到 {type(asm).__name__}（{HINT}）']
    errs = []
    for k in asm:
        if k not in DEFAULTS:
            errs.append(f'board.assembly: 不认识的键 {k!r}（{HINT}；'
                        f'最像的是 {_nearest(k)}）')
    for k, v in asm.items():
        if k in _NUM:
            e = _num_err(k, v)
            if e:
                errs.append(e)
        elif k == 'nest':
            if not isinstance(v, bool):
                errs.append(f'board.assembly.nest: 要 true/false，收到 {v!r}')
        elif k == 'conn_edge':
            if v not in ('',) + EDGES:
                errs.append(f'board.assembly.conn_edge: 要 N/E/S/W（空=不强制），收到 {v!r}')
        elif k == 'conn_order':
            if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
                errs.append(f'board.assembly.conn_order: 要块名列表，收到 {v!r}')
    # 互相矛盾的两条：不定尺（height=0）却要求宽比……照旧允许；但 nest 关了还给缝就是噪音
    if asm.get('nest') is False and ('nest_gap' in asm or 'nest_court_gap' in asm):
        errs.append('board.assembly: nest 关掉时写 nest_gap/nest_court_gap 没有意义'
                    '（要么开 nest，要么删掉这两个键）')
    return errs


def _nearest(k):
    """给"打错一个字母"直说该改成什么——比让人对着键表逐个看快。"""
    best, bd = None, 99
    for c in DEFAULTS:
        d = _lev(k, c)
        if d < bd:
            best, bd = c, d
    return best if bd <= max(2, len(k) // 3) else '（没有相近的）'


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def resolve(spec_asm, cli) -> tuple[dict, list[str], list[str], list[str]]:
    """定下这块板最终用的板级事实。

    `spec_asm` = spec 里的 `board.assembly`（可为 None）；`cli` = 命令行**真的给了**的那些键
    （没给就不放进来，别塞默认值——那样分不清"人是没给"还是"人给了个和默认一样的"）。

    返回 `(facts, source, notes, errors)`：
    - `facts`  每个键的最终值；
    - `source` 每个键的来源：`spec` / `cli` / `default`（写进报告，事后能看出是谁定的）；
    - `notes`  该打印的 `!`/`·` 行（"spec 没写，用了命令行值"这类）；
    - `errors` 非空就别继续（spec 与命令行打架、键写错）。
    """
    spec_asm = dict(spec_asm or {})
    errs = check(spec_asm)
    cli = {k: v for k, v in (cli or {}).items() if v is not None}
    for k in cli:
        if k not in DEFAULTS:
            errs.append(f'命令行 --{k.replace("_", "-")} 不是板级事实键（{HINT}）')
    facts, source, notes = {}, {}, []
    for k, dflt in DEFAULTS.items():
        if k in spec_asm and k in cli:
            if _same(spec_asm[k], cli[k]):
                facts[k], source[k] = spec_asm[k], 'spec'
            else:
                errs.append(
                    f'✗ board.assembly.{k}: spec 写 {spec_asm[k]!r}，命令行给 {cli[k]!r}——'
                    f'两边不一致时以哪个为准说不清，板就没法只凭 spec 复现。'
                    f'要改就改 spec（命令行不许悄悄盖过它）')
                facts[k], source[k] = spec_asm[k], 'spec'
        elif k in spec_asm:
            facts[k], source[k] = spec_asm[k], 'spec'
        elif k in cli:
            facts[k], source[k] = cli[k], 'cli'
            notes.append(f'! board.assembly.{k} 没在 spec 里写，用了命令行值 {cli[k]!r}'
                         f'——这块板不可单凭 spec 复现，定稿请写进 spec')
        else:
            facts[k], source[k] = dflt, 'default'
        # 归一化：数字统一成 float，列表拷一份
        if k in _NUM:
            facts[k] = float(facts[k])
        elif k == 'conn_order':
            facts[k] = list(facts[k])
    return facts, source, notes, errs


def _same(a, b):
    """比较时放过 `1` vs `1.0` 这种类型差异；列表按内容比。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, list) and isinstance(b, list):
        return [str(x) for x in a] == [str(x) for x in b]
    return a == b


def describe(facts, source) -> str:
    """一行说清"这块板的板级事实是什么、谁定的"——打印用。"""
    bits = []
    for k in ('conn_edge', 'conn_inset', 'conn_order', 'nest', 'nest_gap',
              'nest_court_gap', 'width', 'height', 'aspect', 'margin', 'gap'):
        v = facts[k]
        if k == 'conn_order':
            v = ','.join(v) if v else '—'
        elif k == 'conn_edge':
            v = v or '—'
        elif isinstance(v, float):
            v = '%g' % v
        elif isinstance(v, bool):
            v = 'on' if v else 'off'
        mark = {'spec': '', 'cli': '!', 'default': '·'}[source[k]]
        bits.append('%s%s=%s' % (mark, k, v))
    return ('板级事实（spec / ! 命令行 / · 默认）：' + ' '.join(bits)
            + '\n  （来源逐项记在 barm_report.json 的 board.assembly_source）')
