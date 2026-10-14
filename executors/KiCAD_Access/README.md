# kicad-access

KiCad Access 工具集本体：模块化 PCB 自动化设计（`kca` CLI）。
方案：`../DesignDoc/HardwareStudio_实施方案.md`。

## 用法（M0）

```bash
cd KiCAD_Access
uv sync
uv run kca doctor      # 体检 kicad-cli / kicad-python / java / freerouting（含降级提示）
uv run pytest          # 回归测试
tools/smoke_freerouting.sh   # L2 链路冒烟：KiCad→DSN→freerouting→SES→KiCad（/tmp 副本上跑）
```

环境变量覆盖：`HWS_KICAD_CLI` / `HWS_KICAD_PY` / `HWS_JAVA` / `HWS_FREEROUTING_JAR`。

平台事实（2026-09-08 实测）：**KiCad 10 的 kicad-cli 已无 dsn 导出/SES 导入子命令**，
DSN⇄SES 只能走 KiCad.app 内置 Python 的 pcbnew 模块；freerouting v2.4.1 多线程
优化自述会产生间距违规，链路一律 `-mt 1`。

## 子命令 → 里程碑

| 命令 | 职责 | 里程碑 |
|------|------|--------|
| `new` | 脚手架新板项目 projects/<name>（doc / schematic{regions,build} / layout{regions,build}） | — |
| `doctor` | 工具链体检（已实装） | M0 |
| `ingest` | sch 层级解析 → IR | M1 |
| `place` | recipe 布局 + floorplan + 融合 | M2 |
| `route` | L1 确定性 + L2 freerouting | M3 |
| `build` | 生成 pcb / Gerber | M3/M4 |
| `report` | DRC / 质量报告 | M4 |

## 项目结构

新板用 `kca new <name>` 脚手架，每板一个自包含目录，三类工作物按目录隔离：

```
projects/<name>/
├── <name>.yaml           # 板规格（kca place/route/merge 唯一入口）
├── doc/design_spec.md    # 设计规格说明 / 文档
├── schematic/{regions,build}   # 原理图：regions=分区源，build=产物
└── layout/{regions,build}      # 布局：regions=分区板，build=产物
```

`regions/` 放分区工作物、`build/` 放机器产物，二者永不混放。旧板仍平铺
`projects/*.yaml`（`ethercat_can*.yaml` 三块），与目录式并存。

```bash
uv run kca new my_board          # → projects/my_board/
uv run kca place projects/my_board/my_board.yaml   # 填好 schematic + 板框后
```

代码分桶：`src/kicad_access/`（最终管线本体，随 `kca` 打包）vs `tools/`
（调试/支撑脚本，不打包）。详见 `CLAUDE.md`。
