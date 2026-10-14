# HarnessPCB

**在自己的 Claude Code 里设计 PCB**：喂三段意图报告（原理图设计表 / 放置意图 /
布线意图），LLM 决策层调度执行器完成全链，产出可送厂的 Gerber。

> 状态：🟢 P0–P4 已完成（执行器抽取 + 双仓字节级复现 + 8 Skills + plugin 骨架
> + MCP server 随包 + 数据剥离审计 + MIT 许可），当前 **v0.1**。

## 快速开始（一屏）

前置：KiCad 10、Java 25+、FreeRouting v2.3.0（jar 从
[GitHub Releases](https://github.com/freerouting/freerouting/releases) 下，
放大磁盘任意位置）、Node 18+、[uv](https://docs.astral.sh/uv/)。

```bash
git clone <本仓库> && cd HarnessPCB

# 1. 装依赖（生成 executors/Placement/.venv，含政策模型推理）
cd executors/Placement && uv sync && cd ../..

# 2. 写本机工具路径（通常只需改 java / jar 两键；MCP server 已随包）
cat > ~/.harnesspcb.json <<'EOF'
{
  "HWS_KICAD_CLI": "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
  "HWS_KICAD_PY":  "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
  "HWS_KICAD_EXT": "/Applications/KiCad/KiCad.app/Contents",
  "HWS_JAVA": "/opt/homebrew/opt/openjdk@26/bin/java",
  "HWS_FREEROUTING_JAR": "/path/to/freerouting.jar",
  "D2S_MCP": "<克隆目录>/mcp/server/index.mjs",
  "D2S_NODE": "/opt/homebrew/bin/node"
}
EOF

# 3. 体检（全绿才继续；三级解析：环境变量 > 此文件 > 标准探测）
executors/Placement/.venv/bin/kca doctor

# 4. 跑包内 demo 全链（设计表 → 原理图 → 放置 → 布线 → 铺铜 → DRC）
python3 executors/SchematicDesign/design2schematic/d2s.py chain \
  -d demo/demo_blinker.design.json -o demo/out \
  --clearances 205,215,225,235,245 --mp 60 \
  --intent-file demo/demo_blinker_intent.json
```

demo 是什么、期望产物链，见 `demo/README.md`。全绿后按
`skills/pcb-fab-export` 出 Gerber。设计表写法看 `docs/输入格式说明.md`。

## 装成 Claude Code 插件

上面是裸 CLI 形态。装成插件后，Skills 与斜杠命令自动可用：

```bash
# 方式 A：本地路径（开发/试用）
claude plugin marketplace add /path/to/HarnessPCB
claude plugin install harnesspcb@harnesspcb

# 方式 B：git URL（仓库推上远端后）
claude plugin marketplace add <git-url>
claude plugin install harnesspcb@harnesspcb
```

装好后在本项目里：

| 命令 | 干什么 |
|---|---|
| `/harnesspcb:pcb-demo` | 跑包内 demo 全链，验证环境 |
| `/harnesspcb:pcb-run <design.json>` | 跑一块用户板全链（可带 `--intent`） |
| `/harnesspcb:pcb-new [需求]` | 从需求访谈 → 设计表 → 意图 → 全链出板 |

8 个 Skills（`pcb-chain` / `pcb-place` / `pcb-route` / `pcb-route-session` /
`pcb-intent-spec` / `pcb-drc-read` / `pcb-silk-tidy` / `pcb-fab-export`）
随插件自动装配；裸 CLI 形态则把 `skills/` 拷（或 symlink）进项目
`.claude/skills/`。各 skill 何时用见下表。

## Skills 一览（8 个）

| Skill | 何时用 |
|---|---|
| `pcb-chain` | 一条命令全链（sch→board→place→route→pour→DRC），默认入口 |
| `pcb-place` / `pcb-route` | 单独重跑放置 / 布线（换 seed、调阶梯、看真数） |
| `pcb-route-session` | 剩余死网手术：candidates/blockers/单网重布/快照回滚 |
| `pcb-intent-spec` | 写放置意图前检查清单 + 交付验收对照 |
| `pcb-drc-read` | DRC 报告口径（铜级 9 类 / 容忍项 / fail-closed） |
| `pcb-silk-tidy` | 丝印清理五条方法学（美观项，不挡验收） |
| `pcb-fab-export` | Gerber + 钻孔 + 贴片坐标导出打包 |

## 仓库结构

| 目录 | 内容 |
|---|---|
| `skills/` | 8 个操作 Skills（上表） |
| `commands/` | 插件斜杠命令（pcb-demo / pcb-run / pcb-new） |
| `.claude-plugin/` + `.mcp.json` | Claude Code plugin 清单与 MCP 声明 |
| `mcp/` | 随包 KiCad MCP server（单文件 bundle + python 接口层，源自 KiCAD-MCP-Server v2.7.0，MIT，见 `mcp/LICENSE-KiCAD-MCP-Server`） |
| `executors/` | 白名单抽取的执行器（d2s / plr place / route 冻结梯子 / actions / 工具脚本 + 政策模型），`MANIFEST.json` 逐文件 sha256 |
| `docs/` | 输入格式说明、参数快照 FLAG_FREEZE |
| `demo/` | 合成 demo 板（设计表 + 意图 spec + 期望产物说明） |
| `tools/` | `pack_from_studio.py` 抽取脚本 + allowlist |

## 同步机制（贡献者向）

```bash
python3 tools/pack_from_studio.py --check   # 看差异（缺/多/变动）
python3 tools/pack_from_studio.py --sync    # 重生成 executors/ + MANIFEST.json
```

`executors/` 由抽取脚本从开发工作台（母舰）生成，**禁止手改**（下次同步会被
覆盖）；`skills/`、`docs/`、`demo/`、`commands/`、`.claude-plugin/` 的权威源
在本仓库，直接在此编写。一切代码与测试在母舰工作台进行，单向同步进本仓库，
绝不反向修改。allowlist 从 Skills 调用面自下而上反推（`tools/allowlist.txt`
是唯一清单源），评估产物 / 用户板 / RL 权重 / 研究脚本结构上就进不来。

## 外部依赖与版本口径

| 依赖 | 版本口径 | 用途 |
|---|---|---|
| KiCad | 10（macOS Apple Silicon 实测；他平台未验） | pcbnew Python API、kicad-cli |
| FreeRouting | **v2.3.0 钉死**（需 Java 25+，调用内部钉单线程） | 布线 |
| KiCAD-MCP-Server | v2.7.0（已随包 bundle，MIT） | 原理图步的 KiCad 桥 |
| Node | 18+（实测 25） | 起 MCP server |
| uv | 任意近期版 | 建 Python 环境 |

## 许可

- 本仓库代码与文档：**MIT**（见根目录 `LICENSE`）。
- 随包 `mcp/`：源自 KiCAD-MCP-Server（MIT），原许可文件保留于
  `mcp/LICENSE-KiCAD-MCP-Server`。
- **FreeRouting 不随包分发**：它是 GPL-3.0，与本包 MIT 不兼容，故 jar
  由使用者自行从官方 [GitHub Releases](https://github.com/freerouting/freerouting/releases)
  下载，包内只含调用接口（`HWS_FREEROUTING_JAR` 指向你自己的 jar）。

所有工具路径**三级解析**：环境变量（`HWS_*` / `D2S_*`）> 机器配置文件
`~/.harnesspcb.json`（`HWS_CONFIG` 可改指）> 标准探测（PATH / 常见安装位）。
`kca doctor` 一条命令体检全链缺项。推荐档参数以
`docs/参数快照_FLAG_FREEZE.md` 为唯一权威源。
