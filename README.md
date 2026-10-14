# HarnessPCB

**在自己的 Claude Code 里设计 PCB**：喂三段意图报告（原理图设计表 / 放置意图 /
布线意图），LLM 决策层调度执行器完成全链，产出可送厂的 Gerber。

> 状态：🟡 **骨架阶段**（2026-10-14 建立，P 线 P0–P4 进行中）。
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
| `executors/` | 白名单抽取的执行器（d2s / plr place / route 冻结梯子 / actions / 工具脚本） | P0/P1 |
| `docs/` | 输入格式说明、quickstart、铁律 | P2/P3 |
| `demo/` | 合成 demo 板（一条命令端到端冒烟） | P3 |
| `.claude-plugin/` | Claude Code plugin 清单 | P3 |
| `tools/` | `pack_from_studio.py` 抽取脚本 + allowlist | P0 |

## 开发模式（重要）

本仓库是**发行仓**：只收两类内容——

1. **同步产物**：`executors/` 由抽取脚本从开发工作台（母舰）生成，
   **禁止手改**（下次同步会被覆盖）；参数快照同理，靠再生成防漂移；
2. **首发原创内容**：`skills/`、`docs/`、`demo/`、`.claude-plugin/` 的
   权威源在本仓库，直接在此编写。

一切代码开发与测试在母舰工作台进行，单向同步进本仓库，绝不反向修改。

## 外部依赖

KiCad 10（pcbnew Python API）、FreeRouting v2.3.0（需 Java）、Node 版
KiCAD-MCP-Server；版本口径与获取方式在 P0/P4 定稿后写入 `docs/`。
