# place-rl（Placement/）

PCB 元器件自动放置工具（RL），与 KiCad 兼容。顺序式 RL 骨架，
GNN 超图编码 + 两级动作分解 + Pos/Wire 掩码，SA 自蒸馏作教师 →
行为克隆 → PPO 微调，闭环评估接 FreeRouting（历史设计文档已归档，
入口/参数现行口径见 `../HarnessPCB_入口清单.md` 与 `CLAUDE.md`）。

复用 `../KiCAD_Access` 的数据层（ingest/footprint/place/route），
通过 `src/place_rl/reuse.py` 单一适配层隔离 kca 内部漂移。

## 里程碑

| 里程碑 | 内容 | 入口 |
| --- | --- | --- |
| M0 | 脚手架 + 数据层复用 + 指标冒烟 | `plr inspect <sch> --pcb <pcb>` |
| M1 | HPWL（增量）+ Rudy 拥塞 | `src/place_rl/metrics/` |
| M2 | SA 教师 / 基线 | `plr teach` |
| M3 | GNN 策略 + 行为克隆 | `plr bc` |
| M4 | 掩码 + 复合奖励 PPO | `plr train` |
| M5 | 经典合法化精修 | `plr legalize` |
| M6 | 闭环布线评估 | `plr eval` |

## 环境

```bash
cd Placement               # 目录名即 Placement/（2026-10-07 由 place-rl/ 改名）
uv sync                    # 主依赖含 torch（policy_rl.pt 推理）+ kicad-access 路径依赖
uv sync --group rl --group dev   # 再训（PPO/d3rlpy）或跑 pytest 时加
uv run plr inspect /path/to/board.kicad_sch --pcb /path/to/board.kicad_pcb
```

⚠️ `.venv` 绝对路径绑定：目录不可移动、不要在母舰工作台副本上重建（发行包
`executors/Placement/` 下的 `.venv` 不受此限，按 README `uv sync` 生成即可）。
