# HarnessPCB

**在自己的 Claude Code 里设计 PCB**：喂三段意图报告（原理图设计表 / 放置意图 /
布线意图），LLM 决策层调度执行器完成全链，产出可送厂的 Gerber。

> 状态：🟡 **P0 已完成**（环境解耦 + 全链验收 + 首次抽取同步），P1–P4 进行中。
> 使用说明在 P3 quickstart 定稿；在此之前本仓库主要是开发骨架。

## 目标形态（建成之后）

```
clone 本仓库 → 装依赖（uv sync）→ 装 Claude Code plugin
→ 按 docs/输入格式说明 喂设计表 → 出 Gerber
```

## 仓库结构

| 目录 | 内容 | 填充阶段 |
|---|---|---|
| `skills/` | 8 个操作 Skills（pcb-place / pcb-route / pcb-route-session / pcb-chain / pcb-intent-spec / pcb-drc-read / pcb-silk-tidy / pcb-fab-export） | P2 |
| `executors/` | 白名单抽取的执行器（d2s / plr place / route 冻结梯子 / actions / 工具脚本） | **P0 已首灌**（93 文件） |
| `MANIFEST.json` | 每文件 sha256 + 源路径 + 生成时间（内容物审计底稿） | P0 |
| `docs/` | 输入格式说明、quickstart、铁律 | P2/P3 |
| `demo/` | 合成 demo 板（一条命令端到端冒烟） | P3 |
| `.claude-plugin/` | Claude Code plugin 清单 | P3 |
| `tools/` | `pack_from_studio.py` 抽取脚本 + allowlist | P0 |

## 同步机制（P0 起生效）

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

一切代码开发与测试在母舰工作台进行，单向同步进本仓库，绝不反向修改。

## 外部依赖与环境解析

KiCad 10（pcbnew Python API）、FreeRouting v2.3.0（需 Java 25+）、Node 版
KiCAD-MCP-Server。所有工具路径**三级解析**：环境变量（`HWS_*` / `D2S_*`）
> 机器配置文件 `~/.harnesspcb.json`（`HWS_CONFIG` 可改指）> 标准探测
（PATH / 常见安装位）。`kca doctor` 一条命令体检全链缺项；版本口径与
获取方式在 P4 定稿后写入 `docs/`。
