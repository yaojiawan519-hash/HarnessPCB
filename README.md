# HarnessPCB

**在自己的 Claude Code 里设计 PCB**：喂三段意图报告（原理图设计表 / 放置意图 /
布线意图），LLM 决策层调度执行器完成全链，产出可送厂的 Gerber。

> 状态：🟢 **P2 已完成**（8 Skills + demo 全链冒烟 + 输入格式文档）；
> P3（plugin 骨架 + quickstart）、P4（许可核实 + v0.1 发版）进行中。

## 快速开始（当前形态）

1. **装依赖**：`cd executors/Placement && uv sync`（生成 `.venv`，
   含 torch/政策模型推理）；FreeRouting v2.3.0 + Java 25+、KiCad 10、
   Node（KiCAD-MCP-Server）按 README 末节解析。
2. **配置路径**：把本机工具位置写进 `~/.harnesspcb.json`（七键，样板
   见下），然后体检：
   ```bash
   cd executors/Placement && ./.venv/bin/kca doctor
   ```
   全绿即环境就绪（三级解析：环境变量 > `~/.harnesspcb.json` > 标准探测，
   少数全默认安装的机器可跳过配置文件）。
3. **装配 Skills**：把 `skills/` 下 8 个目录拷（或 symlink）进你的项目
   `.claude/skills/`，Claude Code 即可按 skill 名调用。
4. **喂设计表跑全链**：
   ```bash
   python3 executors/SchematicDesign/design2schematic/d2s.py chain \
     -d <你的.design.json> -o <项目目录> --clearances 205,215,225,235,245 --mp 60
   ```
   设计表怎么写看 `docs/输入格式说明.md`；合成范例 `demo/`；真实工程范例
   `executors/SchematicDesign/design2schematic/templates/usb2rs485.design.json`。
5. **出 Gerber**：全链绿后按 `skills/pcb-fab-export` 导出投厂。

`~/.harnesspcb.json` 七键样板：

```json
{
  "HWS_KICAD_CLI": "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
  "HWS_KICAD_PY":  "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
  "HWS_KICAD_EXT": "/Applications/KiCad/KiCad.app/Contents",
  "HWS_JAVA": "/path/to/java",
  "HWS_FREEROUTING_JAR": "/path/to/freerouting.jar",
  "D2S_MCP": "/path/to/KiCAD-MCP-Server/dist/index.js",
  "D2S_NODE": "/opt/homebrew/bin/node"
}
```

（键名 = 环境变量名；一份配置喂本机所有项目，不进任何仓库。）

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

| 目录 | 内容 | 状态 |
|---|---|---|
| `skills/` | 8 个操作 Skills（上表） | **P2 已完成** |
| `executors/` | 白名单抽取的执行器（d2s / plr place / route 冻结梯子 / actions / 工具脚本 + 政策模型） | P0 首灌，滚动同步 |
| `MANIFEST.json` | 每文件 sha256 + 源路径（内容物审计底稿） | P0 |
| `docs/` | 输入格式说明、参数快照 FLAG_FREEZE | **P2 已完成**（quickstart P3） |
| `demo/` | 合成 demo 板（设计表 + 意图 spec，全链冒烟已验证） | **P2 已完成** |
| `.claude-plugin/` | Claude Code plugin 清单 | P3 |
| `tools/` | `pack_from_studio.py` 抽取脚本 + allowlist | P0 |

## 同步机制

```bash
python3 tools/pack_from_studio.py --check   # 看差异（缺/多/变动）
python3 tools/pack_from_studio.py --sync    # 重生成 executors/ + MANIFEST.json
```

`executors/` 保持工作台相对路径镜像（如 `executors/Placement/src/route_rl/`），
导入关系零改动；`--check` 差异非零时 exit 1，可作同步前的例行审计。
allowlist 从 Skills 调用面自下而上反推（`tools/allowlist.txt` 是唯一清单源），
评估产物 / 用户板 / RL 权重 / 研究脚本结构上就进不来。

## 开发模式（重要）

本仓库是**发行仓**：只收两类内容——

1. **同步产物**：`executors/` 由抽取脚本从开发工作台（母舰）生成，
   **禁止手改**（下次同步会被覆盖）；参数快照同理，靠再生成防漂移；
2. **首发原创内容**：`skills/`、`docs/`、`demo/`、`.claude-plugin/` 的
   权威源在本仓库，直接在此编写。

一切代码与测试在母舰工作台进行，单向同步进本仓库，绝不反向修改。

## 外部依赖与环境解析

KiCad 10（pcbnew Python API）、FreeRouting v2.3.0（需 Java 25+，调用
内部钉单线程）、Node 版 KiCAD-MCP-Server。所有工具路径**三级解析**：
环境变量（`HWS_*` / `D2S_*`）> 机器配置文件 `~/.harnesspcb.json`
（`HWS_CONFIG` 可改指）> 标准探测（PATH / 常见安装位）。`kca doctor`
一条命令体检全链缺项。推荐档参数以 `docs/参数快照_FLAG_FREEZE.md`
为唯一权威源。
