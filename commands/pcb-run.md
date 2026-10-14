---
description: 跑一块用户板全链（设计表 → 原理图 → 放置 → 布线 → 铺铜 → DRC → Gerber）
argument-hint: <design.json> [--intent <intent.json>]
---

# 跑设计表全链

$ARGUMENTS 里给设计表路径（intent spec 可选；格式见 docs/输入格式说明.md）。

1. **前置检查**：设计表 JSON 可解析；按 docs/输入格式说明.md §一 逐键检查
   （project/board/parts/conns/power 必填；重复电源引脚 multi；电源轨
   pwr_flags 规则）。给了 intent 则按 skills/pcb-intent-spec 的 A 段清单过一遍。
2. **环境体检**：`executors/Placement/.venv/bin/kca doctor` 全绿才继续。
3. **全链**：按 skills/pcb-chain 的标准命令跑（含 --intent-file 透传，如提供）；
   输出目录用独立项目目录，长进程输出重定向日志再读。
4. **读结果**：按 skills/pcb-drc-read 口径判读（铜级 9 类须 0 / 未连 0；
   丝印与库级容忍项如实报数）；死网按 skills/pcb-route-session 归因。
5. **出 Gerber**：全绿后按 skills/pcb-fab-export 导出。

报告判定用大白话先给结论（成/不成/差在哪），数字表随后：ERC、放置读数、
布线档位与完成度、DRC 铜级/未连、fab 包路径。
