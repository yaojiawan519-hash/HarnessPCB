---
name: pcb-route-session
description: 布线会话动作协议（RoutingSession）：逐网探查 candidates/blockers、重布 route、拆线 rip_up、快照回滚、script 重放验证。链式布线卡在个别死网时的手术刀；不是批量布线（那是 pcb-route）。
---

# 布线会话（route actions）

对已布/半布的板做**逐网手术**：查哪些网没布通、卡在哪、重布单网、
不满意回滚。链式 route 步（pcb-route）跑完后剩余死网的分析与抢救用它。

## 调用形态

```bash
cd <包根>/executors/KiCAD_Access
PYTHONPATH=src ../Placement/.venv/bin/python -m kicad_access.route.actions \
  --board <routed.kicad_pcb> \
  --act '{"tool":"routing","op":"candidates","target":{},"constraints":{"limit":50},"rationale":"查死网清单"}' \
  --report /tmp/act_report.json \
  > /tmp/actions.log 2>&1
```

- 动作走 `--act '<单条 JSON>'` 或 `--script <JSONL>`（每行一条，`#` 注释行跳过）。
- 动作四件套：`{"tool":"routing","op":…,"target":…,"constraints":…,"rationale":…}`
  （tool/rationale 只进审计日志，不参与执行）。
- `--out` 缺省写 `<board>_act.kicad_pcb`；会话收尾自动 write_out。
- 审计：设 `ROUTE_AUDIT_DIR=<目录>` 开 per-session JSONL（`ROUTE_AUDIT_OFF=1` 关）。

## 八个 op（其余 op 一律 refusal）

| op | target | constraints | 作用 |
|----|--------|-------------|------|
| `candidates` | `{}` | `limit` | 未布通网清单 + 每网焊盘数 |
| `blockers` | `{net}` | `limit` | 该网死因归因（焊盘级 → 器件级障碍） |
| `route` | `{net}` | `hist_penalty` `max_vias` `layers`* | 重布单网；`max_vias` 超限自动回滚该网 |
| `rip_up` | `{net}` | `hist_penalty` | 拆线；`"adopt": true`（target 键）= 文本外科拆+对账后采纳 |
| `snapshot` | `{}` | — | 打全量快照，返回 snapshot id |
| `rollback` | `{snapshot}` | — | 回滚到快照（byte-exact 恢复） |
| `eval` | `{}` | — | 完成度/违例读数 |
| `write_out` | `{}` | — | 落盘（CLI 收尾自动调） |

\* `constraints.layers` 当前 refusal（需动引擎层参，未解冻）——别用。

`expand_window` / `preserve` 两个 op 已终局关闭：调用即 refusal 并附
研究报告出处（净距不足时扩窗救不了；走廊保留失去量化靶子）。
refusal **附原因就是设计行为**，如实读，不要换着姿势硬试。

## 推荐工作流

1. `candidates` 拿死网清单 → 2. 对每网 `blockers` 归因（区分「细脚距
   逃逸」与「顺序残余」）→ 3. `snapshot` → 4. `route`（带 `max_vias`）
   → 5. `eval` 看真数 → 6. 不好就 `rollback`，好就继续下一网 → 收尾
   `write_out`。
7. **验证轨道**：把成功序列存成 JSONL，`--script` 重放一遍——确定性
   引擎下重放结果必须一致，这是复现实验的凭据。

## 铁律

- 先 snapshot 再动手；每网一次一变量。
- 读写都经会话（别手改板文件再喂回来——文本外科与内存模型会失配）。
- 日志重定向再读，禁管道截断。
