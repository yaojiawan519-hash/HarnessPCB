---
name: pcb-route
description: 单独跑布线引擎（route_rl）：clearance 阶梯逐档试、FreeRouting 打底、引擎收尾净化。要复布线、调阶梯、或放置实验后看真数时用；整链跑板请用 pcb-chain。
---

# PCB 布线（route_rl）

chain 的 route 步即本命令。阶梯策略：从紧到松逐档试布线，全通即停——
既不牺牲多余间距也不死磕最紧档。

## 调用形态

```bash
cd <包根>/executors/Placement
./.venv/bin/python -m route_rl \
  --board <placed.kicad_pcb> \
  --stackup <stem>_stackup.json \
  --out <route目录> \
  --passes signal \
  --clearances signal=205,215,225,235,245 \
  --mp 60 \
  > route.log 2>&1
```

- stackup 用 chain board 步生成的 `<stem>_stackup.json`（2 层 + via 梯子
  0.3 孔 / 0.6 外径）。
- `--mp 60` = FreeRouting max-passes（内部已钉 v2.3.0 + 单线程，不手调）。
- 阶梯是双板验证的甜点带；改阶梯只改 `signal=` 后的 µm 列表。
- 进阶引擎旗标（45° / via-pitch / h-weight 等）以
  `docs/参数快照_FLAG_FREEZE.md` 为唯一权威源，不手抄。

## 验证方法

1. 日志尾 `score [A,B,C]`：A=铜级违例、B=未布通、C=其他；`[0,0,x]` 达标。
2. **选定板从 `<route目录>/result.json` 的 `final` 字段重读**（不是猜文件名；
   final 实测为绝对路径——但别依赖这一点，重读 JSON 即可）。
3. 布线达标 ≠ 完工：铜级终判在铺铜后 DRC（pcb-chain / pcb-drc-read）。

## 铁律

- 日志重定向再读，禁 `… | head` 等管道截断——SIGPIPE 会杀布线进程。
- route 目录是运行产物（含中间档），别手改里面的板。
- `.lck` 锁窗口不写任何板文件。
