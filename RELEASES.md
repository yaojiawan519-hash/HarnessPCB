# 版本记录

## v0.1（2026-10-15）

首个可用发行。在 Claude Code（或裸 CLI）里从设计表一路跑到可送厂 Gerber。

**能做什么**

- 设计表（JSON）→ 原理图（ERC 0 + 逐 pin 回读对账）→ 板 → RL 放置（含三段
  意图 spec：接口贴边 / 电源簇 / LED 就近）→ 布线（冻结梯子 205–245µm +
  FreeRouting v2.3.0）→ 双面铺铜 → DRC 分级（铜级 9 类 / 丝印库级容忍）→
  Gerber + Excellon
- 8 个 Skills + 3 个斜杠命令（pcb-demo / pcb-run / pcb-new），可装成
  Claude Code 插件
- 合成 demo 板（demo_blinker）随包，`/harnesspcb:pcb-demo` 一条命令验证环境

**验收口径**

- 陌生环境冒烟：临时克隆 + 新配置文件（不读 `~/.harnesspcb.json`）→
  `uv sync` → `kca doctor` 全绿 → demo 全链 exit 0 → DRC 铜级 0 / 未连 0
  → Gerber 全层出料
- 内容物审计：无用户项目板、无传染许可语料、无本机绝对路径（grep 零命中）；
  `MANIFEST.json` 逐文件 sha256

**许可**：包内代码与文档 MIT；随包 MCP server 源自 KiCAD-MCP-Server（MIT，
原许可保留）；FreeRouting（GPL-3.0）不随包，由使用者自行下载。

**已知边界**（如实声明）

- KiCad 10 仅 macOS Apple Silicon 实测；FreeRouting 钉死 v2.3.0（Java 25+）
- 细脚距（0.4–0.5mm）器件逃逸是已知难点，引擎侧有免责净距旋钮但非全能
  （详见 docs/参数快照_FLAG_FREEZE.md）
- 布线完成度依赖冻结梯子扫描，极端拥堵板可能留死网（Skills 提供
  pcb-route-session 手术工具）
