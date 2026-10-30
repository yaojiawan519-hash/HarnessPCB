# 版本记录

## v0.2（2026-10-31）

**放置从"学出来的"换成"算出来的"，并首次把 108 块人画电路块随包发出去。**

**换掉了什么**

- **RL 放置线整体退役**：`place_rl/`、`policy.pt`、`policy_rl.pt`、`plr` 入口、
  `eval_out/` 及 4 个 RL 测试删除（母舰侧归档）。连带 `torch` 从
  `uv sync` 依赖里消失——装环境轻了一大截。
- **换成规则摆件 `DesignBlockTools/place_rules.py`**：四条可打印数字的规则
  （R1 接口一面对外 / R2 去耦贴脚 / R3 时钟件贴芯片 / R4 就近推开），确定性、
  无权重、无 seed。`--face` / `--gap` / `--ports` 是仅有的三个旋钮。
- ⚠ **意图 spec 停用**：`--intent-file`、`extra_fixed` 不再有任何效果。
  `skills/pcb-intent-spec` 改写成「写前清单 + 交付审查」。v0.1 的
  `demo_blinker_intent.json` 删除，格式存档在 `v0.1` tag 里。

**新增了什么**

- **`blocks/`：108 块人画电路块**（原理图 + 板 + 块卡 + 实测几何，13.3 MB /
  1093 个文件）。从真实开源板上一件一件、一截铜一截铜扣下来，作者原图原样搬。
  设计新板当菜单用，能整块复用的别重画。
- `blocks/INDEX.md`（人看）与 `blocks/index.json`（机读检索）；规矩与坑见
  `docs/块库说明.md`。
- **块库按许可证筛，不按路径**：只收 `license_tier: permissive`（MIT/Apache 等）。
  `internal`（用户自己的板 / 许可未核定）与无块卡老块**整批拒绝**，连复制都不做。
  新脚本 `tools/pack_blocks.py`（`--list` / `--check` / `--sync`），正文清洗把
  本机用户名换成 `HarnessPCB`，复制后逐文件断言，命中即整批失败。
- 随包执行器新增 `DesignBlockTools/sdi/`（扣块 / 块卡 / 门禁引擎）与
  `SchematicDesign/il/tools/`（sdi 的 IL 解析层；不收同级第三方语料）。

**顺手修的硬伤**

- `place_rules.py` 原来在 import 时就 `open("~/.harnesspcb.json")`，陌生机器
  （没写过配置文件）连 `--help` 都出不来（FileNotFoundError）。改成懒加载三级
  解析（env > 配置文件 > 标准探测），缺工具链时打印三条补法而不是抛栈。

**验收口径**

- `pack_from_studio.py --check` 与 `pack_blocks.py --check` 双零差异。
- 对**发出去的 `blocks/` 拷贝**（不是母舰库）跑 `sdi.checkall` 四道门，全过：
  - ① 卡↔板：0 块不过
  - ② 卡回读 108 过 / 0 不过；无名内部网 108 块齐 / 0 块漏（225 张）；散文数字门
    108 过 / 0 不过
  - ③ 短路：105 块过 / 0 不过（3 块无板）；全库**短路 0 处**、器件内铜桥 9 处
    （跳线/天线，不算账）、违规 2049 条、未连通 384 条
  - ④ 空线头：105 块干净 / 0 块有毛刺；全库空线头 632 = 口岸断头 616 +
    载流断头 16 + **毛刺 0**
- 冒烟：包内 `python3 -m sdi.checkall --help`、`d2s.py` 定位 `place_rules.py`、
  空 HOME 下 `place_rules.py --help` 退出 0。

**已知边界**

- 108 块里 **67 块 2 层、38 块仍是 4 层**：那 38 块内层有作者真画的铜，收层会把
  人的东西删掉，属待处理账（不是忘了）。
- 块里 GND / 电源轨是裂的（块不带铺铜），拼板必须整板重铺；口岸按焊盘记，
  别指望块内剪剩的出口铜对接上邻块。
- 3 块老 V3.x 血统没有 `placement.json`（只有原理图，无板）。

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
