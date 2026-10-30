# HarnessPCB

**在自己的 Claude Code 里设计 PCB**：喂三段意图报告（原理图设计表 / 放置意图 /
布线意图），LLM 决策层调度执行器完成全链，产出可送厂的 Gerber。

> 状态：🟢 P0–P5 已完成（执行器抽取 + 双仓字节级复现 + 8 Skills + plugin 骨架
> + MCP server 随包 + 数据剥离审计 + MIT 许可；**P5 = 退役 RL 放置线、换规则摆件、
> 108 块人画电路块随包**），当前 **v0.2**。

## 快速开始（一屏）

前置：KiCad 10、Java 25+、FreeRouting v2.3.0（jar 从
[GitHub Releases](https://github.com/freerouting/freerouting/releases) 下，
放大磁盘任意位置）、Node 18+、[uv](https://docs.astral.sh/uv/)。

```bash
git clone <本仓库> && cd HarnessPCB

# 1. 装依赖（生成 executors/Placement/.venv；v0.2 起不再需要 torch——放置是纯规则）
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
  --clearances 205,215,225,235,245 --mp 60
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
| `pcb-place` / `pcb-route` | 单独重跑放置（规则摆件，调 `--face`/`--gap`/`--ports`）/ 布线（调阶梯、看真数） |
| `pcb-route-session` | 剩余死网手术：candidates/blockers/单网重布/快照回滚 |
| `pcb-intent-spec` | 放置的写前清单 + 交付审查对照（原「意图 spec」，spec 本身已停用） |
| `pcb-drc-read` | DRC 报告口径（铜级 9 类 / 容忍项 / fail-closed） |
| `pcb-silk-tidy` | 丝印清理五条方法学（美观项，不挡验收） |
| `pcb-fab-export` | Gerber + 钻孔 + 贴片坐标导出打包 |

## 块库（108 块）

`blocks/` 里是从真实开源板**一件一件、一截铜一截铜扣下来**的电路：原理图、封装、
人手画的走线原封不动。设计新板时当**菜单**用——能整块复用的别重画。

```bash
ls blocks/                      # 每块一个目录
cat blocks/INDEX.md             # 人看：块名 / 类别 / 件数 / 铜量 / 来源板 / 许可
python3 -c "import json;[print(b['name'],b['category']) for b in json.load(open('blocks/index.json'))['blocks']]"
```

每块带块卡（端口 / 轨 / 内部网 / 出处与许可）与实测几何。**口岸按焊盘记**：
拼板时从焊盘重新拉线，不指望块里切剩的出口铜对接上。GND 与电源轨在块里是裂开的
（块不带铺铜），拼板必须整板重铺。规矩与坑详见 `docs/块库说明.md`。

只收可公开发行的块（MIT/Apache 等）；用户自己的板与许可未核定的块**不在本仓库**。

## 仓库结构

| 目录 | 内容 |
|---|---|
| `skills/` | 8 个操作 Skills（上表） |
| `commands/` | 插件斜杠命令（pcb-demo / pcb-run / pcb-new） |
| `.claude-plugin/` + `.mcp.json` | Claude Code plugin 清单与 MCP 声明 |
| `mcp/` | 随包 KiCad MCP server（单文件 bundle + python 接口层，源自 KiCAD-MCP-Server v2.7.0，MIT，见 `mcp/LICENSE-KiCAD-MCP-Server`） |
| `executors/` | 白名单抽取的执行器（d2s / 规则摆件 place_rules / route 冻结梯子 / actions / 块工具链 sdi + IL 解析层），`MANIFEST.json` 逐文件 sha256 |
| `blocks/` | **108 块人画电路块**（原理图 + 板 + 块卡 + 实测几何），可整块复用；见 `docs/块库说明.md` |
| `docs/` | 输入格式说明、块库说明、参数快照 FLAG_FREEZE |
| `demo/` | 合成 demo 板（设计表 + 期望产物说明） |
| `tools/` | 抽取脚本（`pack_from_studio.py` + allowlist、`pack_blocks.py`） |

## 同步机制（贡献者向）

```bash
python3 tools/pack_from_studio.py --check   # 看差异（缺/多/变动）
python3 tools/pack_from_studio.py --sync    # 重生成 executors/ + MANIFEST.json

python3 tools/pack_blocks.py --list         # 谁进包、谁被许可证挡下
python3 tools/pack_blocks.py --check        # 块库差异
python3 tools/pack_blocks.py --sync         # 重生成 blocks/ + 索引 + MANIFEST-blocks.json
```

块库**按许可证筛**而不是按路径：只收 `license_tier: permissive`，`internal`
（用户自己的板 / 许可未核定）与无块卡的老块整批拒绝——公开仓放不起这个错。

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
