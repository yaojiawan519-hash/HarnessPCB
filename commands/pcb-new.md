---
description: 从需求访谈到设计表再到全链出板（新板的完整设计流程）
argument-hint: [需求描述]
---

# 新板设计流程

$ARGUMENTS 是需求描述（没给就先访谈）。目标：产出设计表 + 意图 spec，
跑全链出 Gerber。

1. **需求访谈**（信息不全时问用户，一次问全）：
   板子干什么、接口/连接器清单及各自出边、电源从哪进（电压/变换）、
   主控或核心器件型号、尺寸约束、机械固定点。
2. **选器件**：用 KiCad 标准库（Device/Connector/MCU 等）凑齐 BOM；
   引脚名以符号库为准（可用 mcp kicad 服务器的 symbol 工具查，
   或翻 KiCad 安装目录 .kicad_sym）。
3. **写设计表**：按 docs/输入格式说明.md §一（九键 schema + multi +
   pwr_flags + 自动 NC 规则）；写完自查 conns 覆盖每个器件的每个用到的引脚。
4. **写意图 spec**（接口件≥2 个时建议写）：按 skills/pcb-intent-spec
   A 段清单逐条过（A8：pad_clear_mm ≥ 板边铜距 0.5+）。
5. **跑全链**：同 /harnesspcb:pcb-run 的步骤 2–5。
6. **按 B 段验收**：skills/pcb-intent-spec B 段逐条对数后向用户报告。

判定先大白话（板子成了没有、还差什么），再摆数字表与产物路径。
改需求就改设计表/intent 重跑（版本号自动 +1，旧版不覆盖）。
