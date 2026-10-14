"""kca new —— 脚手架一块新板项目目录（projects/<name>/）。

与既有 ``projects/*.yaml``（平铺、先导板专用）不同，新板按目录隔离，
让设计师一眼区分三类工作物：

    projects/<name>/
    ├── README.md           目录结构说明（designer 快速上手）
    ├── <name>.yaml         板规格（kca place/route/merge 的 spec 入口）
    ├── doc/
    │   └── design_spec.md  设计规格说明 / 文档
    ├── schematic/
    │   ├── regions/        原理图分区源（sheet/模块划分）
    │   └── build/          原理图产物（netlist / ERC 报告等）
    └── layout/
        ├── regions/        布局分区板（kca route 的 per-region 输出）
        └── build/          布局产物（placed_unrouted / merged / Gerber）

脚手架只落目录 + 模板，不动任何既有板；「这块板与别块的差异」由
<name>.yaml 承载（与方案 §6 一致）。
"""

from __future__ import annotations

import re
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")

# 模板统一用 $NAME$ 哨兵 + str.replace 注入——spec 里满是字面花括号（YAML
# mapping），用 .format 会撞 KeyError。所有花括号都是要原样落盘的 YAML。
_SPEC_TMPL = """# $NAME$ 板规格 —— 本文件是 kca place / route / merge 的统一入口。
# 只写「这块板与别块的差异」：板框 · 层叠 · 融合带 · 模块锚点 · 关键网络。
# 引擎 / recipe 库跨板复用，板子本身不写代码。

project_info:
  name: $NAME$
  eda_target: kicad
  # schematic: ../../KiCAD_Projects/<你的板>.kicad_sch   # place 的 IR 入口，就绪后填

board:
  outline:
    shape: rectangle
    size: [80.0, 80.0]       # [宽, 高] mm，place 的起点（--shrink 可再收）
  layer_count: 4
  layers:                     # 内层平面角色：须 In1..In{N-2}.Cu，二者不可相同
    gnd_plane: In1.Cu
    power_plane: In2.Cu
  mount_holes: []             # [{size: 3.2, pos: [x, y]}]，四角定位孔
  fusion_band:
    default: 1.5              # 区域融合带宽（by_class 可覆盖 power/ethercat 等）

modules:                      # 至少一条；{sheet, placement: {anchor, edge|relative}, recipe?, refs?}
  - sheet: main               # 换成你的原理图 sheet 名（ingest 的层级 sheet）
    placement: {anchor: relative, relative_to: board_center}
    # recipe: mcu_core        # 就绪后挂 recipe（recipes 库）

routing: {}                   # region_order / critical / freerouting / width_floor 等
  # 例：
  # region_order: diff_first
  # critical:
  #   - {nets: [ETH0_TD_P, ETH0_TD_N], rule: diff_pair, impedance: 100, length_match: 8.0}
"""

_README_TMPL = """# $NAME$

本目录是一块 PCB 板的自包含工作区。三类工作物按目录隔离：

| 目录 | 放什么 | 对应管线阶段 |
|------|--------|--------------|
| `doc/` | 设计规格说明、设计文档 | 设计输入 |
| `schematic/regions/` | 原理图分区源（按 sheet / 功能模块划分） | ingest（M1） |
| `schematic/build/` | 原理图产物（netlist / ERC 报告） | ingest 输出 |
| `layout/regions/` | 布局分区板（每区一块 .kicad_pcb） | route（M3）输出 |
| `layout/build/` | 布局产物（placed_unrouted / merged / Gerber） | place→route→merge 输出 |
| `<name>.yaml` | 板规格（差异声明，管线唯一入口） | place / route / merge |

规则：`regions/` 放「分区工作物」，`build/` 放「构建产物」——手工设计进 regions，
机器生成进 build，二者永不混放。

上手：填 `$NAME$.yaml` 的 `schematic` + `board.outline.size` → `kca place $NAME$.yaml`。
"""

_DESIGN_SPEC_TMPL = """# $NAME$ 设计规格说明

> 模板——按需增删。写清「为什么要这样做」而非「做了什么」。

## 1. 概述
- 板子用途：
- EDA 目标：KiCad
- 关键约束（尺寸 / 层数 / 成本 / 工艺底）：

## 2. 规格参数
- 外形：mm × mm
- 层数 / 层叠：
- 电源 / 地平面：
- 对外接口：

## 3. 功能模块（schematic regions 划分依据）
| sheet / 模块 | 功能 | 关键器件 |
|--------------|------|----------|
|  |  |  |

## 4. 电源与层叠
- 电源树：
- 平面划分 / 优先级：

## 5. 关键网络（routing critical）
| 网络 | 规则 | 约束（阻抗 / 等长 / 线宽） |
|------|------|---------------------------|
|  |  |  |

## 6. 约束与能力边界
- 明确做不到 / 当前放弃的项，如实记录（不强连、不硬上）：
"""


def default_projects_dir() -> Path:
    """仓库根 projects/（wheel 只打包 src/，脚手架按仓库布局落位）。"""
    return Path(__file__).resolve().parents[2] / "projects"


def scaffold_project(name: str, projects_dir: Path | None = None) -> Path:
    """在 projects/<name>/ 下脚手架一块新板；返回项目根 Path。

    已存在同名目录抛 FileExistsError；非法项目名抛 ValueError。
    """
    if not name or not _NAME_RE.match(name):
        raise ValueError(
            f"项目名 {name!r} 非法：须以字母开头，仅含字母/数字/_/-，"
            f"不含路径分隔或空白"
        )

    root = (projects_dir or default_projects_dir()).expanduser().resolve()
    proj = root / name
    if proj.exists():
        raise FileExistsError(str(proj))

    # 目录骨架：doc / schematic{regions,build} / layout{regions,build}
    dirs = [
        proj / "doc",
        proj / "schematic" / "regions",
        proj / "schematic" / "build",
        proj / "layout" / "regions",
        proj / "layout" / "build",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=False)
        (d / ".gitkeep").write_text("")

    # 模板文件（$NAME$ 哨兵替换）
    (proj / f"{name}.yaml").write_text(_SPEC_TMPL.replace("$NAME$", name))
    (proj / "README.md").write_text(_README_TMPL.replace("$NAME$", name))
    (proj / "doc" / "design_spec.md").write_text(_DESIGN_SPEC_TMPL.replace("$NAME$", name))

    return proj
