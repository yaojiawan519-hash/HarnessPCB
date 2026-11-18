---
name: engine-stress-eval
description: 引擎压力评估布局——自研 Python 布线引擎不做布线产出，做布局验证器：网组布通率生成调整数据、挣扎热力图判断阻塞区域、逐格归因点名挡路件、单网可通性判定转 FR 时机。做 PCB 布局迭代/布局评审/布通率诊断/热力图分析，或提到"压力测试/热力图/成片红/布通率/引擎验证"时使用。每条判据必须落到打印数+阈值门禁。
---

# 引擎压力评估布局（engine stress eval）

自研协商引擎（`kicad_access.route.engine`，A* + PathFinder 式撕线）**不用于出线，
用于给布局打分**：每轮 240s 产出过程级失败遥测，驱动"测→修→复测"布局迭代闭环，
布局收敛后交 FreeRouting 一次终审。

知识分级：【要求】= 用户拍板必须满足；【建议】= 特定条件采用；【样板】= HPM5E00
实测数值，换板复核。

## 目标与版本

- 解决的问题：布局质量过去只能"布完了才知道"（FR 一轮 35-90 分钟、只有终态、
  归因靠坐标聚类反推）——本方法把布局验证变成 6 分钟一轮、过程可见、
  归因到网到件的可度量闭环
- 版本：v0.1（2026-11-18）；审核状态：方法经 HPM5E00 全程实弹（热值 67→57、
  成片红三片全清、CAN 链归位、PA16 单网判定），文档待工程师复核
- 已验证案例：HPM5E00 ECAT+CANFD 转换器（BGA-196 + 双 PHY + CAN，4 层），
  2026-11-17/18 两天 20 轮迭代

## 分层验证体系（四层，各管一段）

| 层 | 工具 | 耗时 | 管什么 |
|---|---|---|---|
| **L0 静态规则** | `DesignBlockTools/pcb_check_layout_rules.py` | 毫秒 | 反(方位)/远(晶振)/绞(差分)/堵(走廊)/漏(骨架)/挡(逃逸) |
| **L1 引擎压力** | `kicad_access.route.engine` + 遥测 | 240s | 拥挤(热力图)/布通率/归因 |
| **L2 质量刻度** | `kicad_access.route.quality` | 秒 | 绕行系数 len_ratio/孔数/方向纪律 |
| **FR 终审** | route_rl 全链 | 1-2h | 真实完成度+铜级 DRC |

【要求】顺序 L0→L1→L2→FR：L0 红不进 L1，L1 成片红不进 FR。

## 方法一：网组布通率（调整数据生成器）

【要求】每轮压力测试后按网组统计 `per_net.connected` 比例，组是布局修复的
作战单位。`_tools/pcb_check_net_groups.py <report.json>`：

```
MII: 10/33 (30%)   ← 组低=这组有结构性问题
CAN: 7/7 (100%)    ← 组满=这组修复完成（布局验证）
```

- **组低且不碰 BGA = 真布局问题**（CAN 曾 100% 验证链路归位）
- **组低且碰 BGA = 先做单网判定**（见方法四）再定性
- 电源组低是口径性的（平面网走铺铜，FR planes pass 负责）

## 方法二：热力图判断阻塞区域

【要求】成片红 = 硬触发：罚值 ≥0.4（多轮撕线累积）且空间连片 → 必须检查并
调整布局，不是可选建议（用户拍板 2026-11-17）。工具链：

```
KPY pcb_render_heatmap.py <report.json> <board> out.svg   # 热斑叠元件轮廓
```

看图三读：
1. **成片红在哪** → 5mm 网格聚合贡献网（zone_attribution）→ 网名点出主语
2. **热斑压在哪个件上**（热格 ⊕ courtyard 求交）→ 挡路件点名
3. **挪开 → 复测**：成片红消散 + 该组布通率升 = 修对了

【样板】HPM5E00 三片成片红的归因链：
- U4 东侧 PA16/18/20 ×17 → 骨架漏通道逃逸（补 80 球）→ 热值 59.6→42.1
- U22 南侧时钟带 → R56/R40 放错 25mm 外 → 归位 → 40.9→32.4
- (40-45,10) 大热 → CAN 链放错侧斜穿（PE3/PE5 在西北链在东北）→ 12 件归位 → 消散

【建议】单点散斑（×1-2）不追：芯片引脚固有密度/电源走线，布局无解也不必解。

## 方法三：指标口径（不读错尺子）

【要求】三个口径分开读，混读必误判：

| 指标 | 量什么 | 对布局敏感度 |
|---|---|---|
| `p1_fail` | 静态锚封死（邻脚/骨架挡锚） | **零**（布局怎么挪都不动）|
| `struggle_heat` | 网间抢通道（动态拥挤） | 高（布局量尺）|
| 布通率 | 协商终态完成度 | 高但受引擎地板限制 |

引擎地板三层（布通率上不去先分辨是哪层）：
1. **锚悲观** → 铜种子第四回退兜住（骨架铜上的栅格点入堆；沿段采样+clean/dirty
   双档+远端优先，投影单点+严 point_clear 会被异网通道线封死）
2. **线宽口径** → 0.5mm QFN 出口被邻脚互挤+EP 内封、球阵 0.1mm 缝容不下
   0.15 线宽+0.245 净距：`--width 0.1 --clearance-um 200` 诊断口径
3. **协商拥挤** → 单网能通、一起抢 240s 谈不完 → **这不是布局问题，转 FR**

## 方法四：单网可通性判定（转 FR 时机）

【要求】布通率横盘时，抽失败组代表网单独 `route()` 一次：
- **单网秒通** = 布局无死路，剩余失败=协商拥挤=FR 的活 → 转 FR（HPM5E00
  PA16 实测：5 段 0 孔干净路径）
- **单网也无路径** = 查 err 里点名的几何位置，那里还有真堵点

【样板】HPM5E00 收口判定：64/160 未通但 PA16 代表秒通 → 转 FR。

## 迭代闭环与落盘

```
压力测试(240s 同参数) → 网组布通率 + 热力图 → 成片红?
  ├─ 是 → 归因 → 挪件(备份+DRC 门禁 0/0/0) → 复测
  └─ 单网判定通过 → FR 终审
每轮 episode 自动落盘 stress_dataset/episodes.jsonl（修法+moved 清单+前后指标）
```

【建议】episode 数据是修复模式对的语料（最高价值用法），跨板蒸馏成规则。

## 工具清单（本机路径，相对 PCBStudioV2/）

| 工具 | 用途 |
|---|---|
| `KiCAD_Access/.venv/bin/python -m kicad_access.route.engine --board X --report r.json --budget-s 240 --max-iter 8 --clearance-um 245 [--width 0.1 --clearance-um 200]` | 压力测试（report 含 process 遥测） |
| `executors/DesignBlockTools/（或项目 _tools/）——原 `executors/DesignBlockTools/pcb_check_net_groups.py r.json` | 网组布通率 |
| `executors/DesignBlockTools/（或项目 _tools/）——原 `executors/DesignBlockTools/pcb_render_heatmap.py r.json board out.svg` | 热力图渲染 |
| `executors/DesignBlockTools/（或项目 _tools/）——原 `<项目>/_tools/pcb_run_stress_cycle.py（板特定，随项目） --note "修法"` | 循环器（自动 episode 落盘+门禁） |
| `DesignBlockTools/pcb_check_layout_rules.py board --skel skel.json` | L0 六规则 |
| `KiCAD_Access/.venv/bin/python -m kicad_access.route.quality --board X` | L2 九格刻度 |

引擎遥测（report `process` 段）：`p1_fail`（锚死点名）/`iter`（逐轮快照）/
`rip`（撕线归因）/`struggle_heat`（热力图数据）/`zone_attribution`（热格→贡献网）。

## 坑（实弹学费）

- 引擎悲观读数当**相对信号**（哪里热）不当绝对判据（谁一定不通）
- 热值在细线口径下**不单调降**（更多网参与协商、撕线更频繁）——布通率才是
  硬指标，单网判定才是转 FR 判据
- 随行件螺旋搜索**必须锚定服务引脚**，否则放错服务对象（R56/R40 被丢到 25mm
  外的另一 PHY）
- 网名在裁剪/简化后会变（ref PA4 → cur JTAG_TDO），跨板对拍用 **pad 号反查**
- BGA 球逃逸三种方式（对角窗 dogbone / 通道 F.Cu 细线 / 边缘直出），骨架提取
  漏任何一种 = 那片球成片红

## 参考与案例

- 正案例：HPM5E00 转换器两天 20 轮（热值 67.3→56.8、CAN 斜穿归位、锚死 73→0、
  单网判定转 FR）
- 关联：[[routing-feeding-back-into-placement]]（方法全史）、
  [[l0-eval-rules-tool]]（L0 层）、chip-centric（摆件侧的方法）
- 分工定案：引擎=快速迭代验证器（一天几十轮），FR=终审+正式布线（布局定稿
  后一次）。两者分层，不互相替代。
