---
name: pcb-chain
description: 设计表→原理图→板→放置→布线→铺铜→DRC 一条命令全链（陌生人主路径）。凡用户给出完整设计表要出板，先用本 skill。
---

# PCB 全链（d2s chain）

一条命令从设计表跑到 DRC 干净的成品板。参数已内置（v7 全链验收过的形态），
不需要拼旗标——这是新板默认走法；分步控制才用其他 skills。

## 前置

1. 环境体检全绿：`<包根>/executors/Placement/.venv/bin/kca doctor`
   （配好 `~/.harnesspcb.json` 七键后跑，见仓库 README「安装」）。
2. 设计表 ready（格式见 `docs/输入格式说明.md`，范例 `demo/demo_blinker.design.json`）。

## 调用形态

```bash
# <包根> = HarnessPCB 克隆的绝对路径
cd <项目目录>   # 产物落这里（--out），别在包根里跑
python3 <包根>/executors/SchematicDesign/design2schematic/d2s.py chain \
  -d <设计表.json> \
  -o <项目目录> \
  --clearances 205,215,225,235,245 \
  --mp 60 \
  > chain.log 2>&1
```

- `--clearances` µm 阶梯：布线步逐档试，全通即停（阶梯是双板验证过的甜点带）。
- `--full-pads R1,J1`：指定铺铜实连焊盘（默认取设计表 `board.full_pads`）。
- 重入：`--step-from {sch,board,place,route,drc}` 复用既有产物从某步续跑；
  `--step-from sch` 默认开新版本号 vN+1，其余默认复用最新。
- 跑完读 `chain.log` + `<项目>_vN_chain_report.json`（各步指标与产物路径）。

## 验证方法（三查）

1. **ERC/sch**：log 里 ERC 0 错 + 逐 pin 回读对账通过（`report.json` 的
   labels/回读计数）。
2. **布线**：log 的 route 段 `score [A,B,C]`——A=铜级违例、B=未布通、
   C=其他；`[0,0,x]` 为达标；结果目录 `result.json` 的 `final` 字段 =
   选定板（**跑完后从 JSON 重读**，不是猜）。
3. **DRC 铜级 0 / 未连 0**：终板 DRC 报告里铜级类（shorting / clearance /
   solder_mask_bridge / hole_clearance / track_width / via_dangling /
   unconnected）必须全 0；丝印/库级残留不算失败（分类口径见
   pcb-drc-read skill）。

## 铁律

- **长进程输出一律重定向日志再读**（`> log 2>&1`），严禁 `… | head` 等管道
  截断——SIGPIPE 会杀掉布线进程且收尾代码不跑。
- **pcbnew 锁窗口绝不写板**：目标 `.kicad_pcb` 旁存在 `.lck` 且对应进程
  活着时，不跑任何会写该板的步骤。
- 不跳 ERC 门（`--no-erc-gate` 只在明确知道后果时用）。
- 用户目录里的既有板是**只读参照**，绝不写回；本包产物只写 `--out` 指定目录。
- 布线引擎参数（独立调用时）以 `docs/参数快照_FLAG_FREEZE.md` 为唯一权威
  源，不手抄副本。
