---
name: pcb-place
description: 单独跑放置引擎（place_rl）：原理图驱动 + 六原则默认开 + 可选意图 spec。要复放置、换 seed 对比、或只想重摆器件时用；整链跑板请用 pcb-chain。
---

# PCB 放置（place_rl place）

chain 的 place 步即本命令（参数一致）。单独调用用于：重放对比、
换 seed / 换意图 spec 实验、或只关心放置质量。

## 调用形态

```bash
cd <包根>/executors/Placement
./.venv/bin/python -m place_rl.cli place <sch> <base.kicad_pcb> \
  --out <placed.kicad_pcb> \
  --model policy_rl.pt --grid-mm 1.27 --xy-std 0.08 \
  --legalize-iters 200 --lam-net 0.1 --seed 42 \
  [--intent-file <intent.json>] \
  > place.log 2>&1
```

- `<sch>` / `<base>` 用 chain 产物（`<项目>_vN.kicad_sch` / `_vN_base.kicad_pcb`）。
- `--intent-file`：放置意图 spec（格式 `docs/输入格式说明.md` §二，
  范例 `demo/demo_blinker_intent.json`）；写 spec 前先过 pcb-intent-spec skill。
- 六原则旗标默认已开（LED 贴边 / 接口贴边 / 主芯片均匀）——与 chain 行为
  一致，不要乱关；`--extra-fixed` 可追加锁定位号。
- ⚠ 本命令 ≠ `kca place`（那是旧的 YAML floorplan 流程，别混用）。

## 验证方法

1. 日志尾的 HPWL / legalize 收敛信息、`--out` 板文件存在。
2. 给了 `--intent-file` 时读日志的意图落位上报（edge/along/rot 三级信号
   如实上报，未落位会写明原因——如实读，不猜）。
3. 终验靠下一步布线真数（放置好坏由布线完成度/孔数说话）：
   接 pcb-route skill 用同一 stackup 布一遍看 score。

## 铁律

- 输出永远写新文件（--out），不覆盖输入板；`.lck` 锁窗口不写。
- 换 seed 只影响初始扰动，锚点/意图/六原则是确定性的——对比实验时
  一次只动一个变量。
- 日志重定向再读，禁管道截断。
