---
description: 跑包内合成 demo 板全链（从零验证环境到出 Gerber）
---

# 跑 demo 全链

按以下步骤在本机执行，验证环境与全链可用。所有路径相对本插件仓库根
（即 demo/ 与 executors/ 所在目录；下称 `$PKG`）。

1. **环境体检**（若 executors/Placement/.venv 不存在，先按 README quickstart 第 1 步 `uv sync`）：
   运行 `executors/Placement/.venv/bin/kca doctor`，逐项核对输出；有缺项按
   README「外部依赖」节补齐后重跑，全绿才继续。
2. **全链**：按 skills/pcb-chain 的标准命令跑 demo：
   ```bash
   python3 $PKG/executors/SchematicDesign/design2schematic/d2s.py chain \
     -d $PKG/demo/demo_blinker.design.json -o $PKG/demo/out \
     --clearances 205,215,225,235,245 --mp 60 \
     --intent-file $PKG/demo/demo_blinker_intent.json
   ```
   长进程输出一律重定向到日志文件再读（严禁管道截断）。
3. **读结果**：按 skills/pcb-drc-read 口径读 DRC 报告（铜级 9 类须 0），
   按 skills/pcb-chain 核对六步产物（sch/board/place/route/pour/drc）。
4. **出 Gerber**：按 skills/pcb-fab-export 导出 fab 包，报告产物路径。

最后用大白话报告判定：全链是否绿、哪个档位布通、铜级/未连是否双 0、
fab 包在哪；有问题给出定位到的步骤与日志摘录。
