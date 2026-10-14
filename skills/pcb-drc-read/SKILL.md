---
name: pcb-drc-read
description: 读 KiCad DRC 报告的正确口径：铜级 8 类硬项须 0、丝印/库级容忍、未知类 fail-closed 拦链。chain 的 DRC 步就是这个分类法；手工复核也用它。
---

# DRC 报告解读

chain 的 drc 步内部就是这个分类法（`executors/SchematicDesign/
design2schematic/d2s.py`）；手工跑 DRC 复核时用同一口径，别另立标准。

## 跑法

```bash
kicad-cli pcb drc --refill-zones --save-board \
  --output <rpt> <板的副本.kicad_pcb>
```

- ⚠ KiCad 10 没有顶层 `kicad-cli drc`——必须 `pcb drc`。
- **在副本上跑**（`--save-board` 会写回 + refill 改文件）；kicad-cli
  位置经 `~/.harnesspcb.json` 的 `HWS_KICAD_CLI` 解析（见 README 安装节）。
- 报告坐标是图纸 mm ÷ 100（0.57,0.59 → 图纸 57.15,59.69）——定位
  报错引脚时 ×100 再对坐标。

## 分类口径（三级）

**铜级硬项（须 0，一条都不容）**——`shorting` `clearance`
`solder_mask_bridge` `hole_clearance` `hole_to_hole` `track_width`
`via_dangling` `unconnected`。这些是电气/制造硬伤：短路、间距不足、
阻焊桥、孔距、线宽、悬空过孔、没连上。

**容忍项（如实报数、不挡链）**——丝印类（`silk_overlap`
`silk_over_copper` `silk_edge_clearance` `silkscreen` `silk`）、
`lib_footprint_mismatch`（库副本警告）、courtyard/文本/标号类。
注释级问题：不美观但不影响电气与制造。想清理见 pcb-silk-tidy。

**未知类 → fail-closed 拦链**。KiCad 升级会冒新检查项（如 KiCad 10
的 `silk_edge_clearance` 就是一例）；凡没归入上两类的 kind 一律判
失败，逼人工归类后才放行。**这不是 bug 是设计**——新项默认不明，
归类错比拦下更糟。归入哪一类须按检查项的电气/制造语义判断，改
`_DRC_TOLERATED`/`_DRC_COPPER` 集合（工作台源头，非发行副本）。

## 判读顺序

1. `unconnected` 和铜级是否双 0——是则板子电气完工。
2. 容忍项看构成：`lib_footprint_mismatch` 通常来自库符号/封装副本
   命名差异（生成板常见，无害）；丝印类看 pcb-silk-tidy 是否值得
   清一轮。
3. 出现没见过的 kind：先查它检查什么（KiCad 文档/报错原文），
   再归语义类——禁止「先加进容忍集跑通再说」。
