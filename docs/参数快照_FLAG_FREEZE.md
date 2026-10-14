> 参数快照（生成物，勿手改——由 tools/pack_from_studio.py --sync 重生成）
> 源：KiCAD_Access/eval_out/FLAG_FREEZE.md（sha256 9bf3f220fbc99e01…）
> 工作台 rev：未知 · 快照义务见发行包计划 §6.2
> Skills/文档引用推荐参数一律指向本文件，不抄副本。

# 引擎旗标冻结（FLAG_FREEZE，2026-10-02）

V2 RL 排期位置：Q5 ✅ → 两轮第二轮 refine_pass ✅（NEGATIVE 收口）→
**旗标冻结（本文档）** → Q4 奖励设计 + route_v2 重采 → V2 RL 开训
（训练启动前须用户点头）。

> **2026-10-07 修订注（物理命名，仅路径引用级）**：目录已更名
> `RoutingRL/`→`Routing/`、`PlacementRL/`→`Placement/`（V3 拍板；`.venv` 经
> sed 修复绝对路径，未重建、未 uv sync）。本文档内镜像/脚本路径引用随之更新；
> **推荐档 CLI 命令文本一字未动**；更名后
> `diff -rq -x __pycache__ KiCAD_Access/src/kicad_access/route Routing/route`
> 复核两树全等（附录 MD5 无需重录——文件内容未变，仅目录名变）。

冻结 = 两件事：

1. **引擎代码冻结**：`KiCAD_Access/src/kicad_access/route/` 与镜像
   `Routing/route/`（2026-10-02 补齐 quality.py 后 diff 全等，
   checksums 见附录）在 route_v2 重采完成前**不动**。任何改动须显式解冻
   并重过 inert 等价锁（refine_pass 四轮 inert 先例，见
   `refine_pass/PLAN.md`）。
2. **推荐旗标定格**：本文档是两块定档板推荐命令行的**唯一权威源**，
   与 Q5 PLAN 定档节冲突处以本文档为准（勘误见下）。

## 推荐命令行（定格）

### COMMON —— vc10 底座 + Q1 方向先验 + Q3' 换层井罚 + Q5 斜线

```
--clearance-um 230 --max-iter 15 --dir-prior --space --space-band 0.75
--space-cost 0.6 --via-cost 1.0 --via-pitch --via-pitch-factor 0.0
--via-pitch-band 0.3 --diag45 --diag-cost 1.7
```

（未显式传的旋钮走 argparse 缺省并属冻结配置的一部分：
par_factor 0.85 / perp_factor 1.5 / turn45_factor 0.3 / turn135_factor 2.0 /
via_corner_cost 0.3 / via_pitch_star 0.0=按方向自适应 / via_pitch_cap 2.5 /
via_pitch_samples 20；`--pitch`（Q3 C 搜索期整路径井罚）保持关闭——C 已
NEGATIVE。）

### pic_programmer（33 网小板）

```
COMMON + --h-weight 1.0 --budget-s 600
```

不带 --stagnation：该板轮 1 即收敛（stop=converged），无冲突轮可停。

### interf_u（109 网总线板）

```
COMMON + --h-weight 2.0 --diag-node-cap 300000 --stagnation 1 --budget-s 650
```

--stagnation 1（K=1：连续 1 轮无改进即停并锁回最优快照）：Q5 F/G 臂单变量
实验系列同款（F 臂 stop=stagnation 实证携带）；budget 650 时输出中性
（轮 2 被 budget 起步即砍，与 stagnation 截断回到同一轮 1 最优快照——
refine_pass inert 对拍带此旗标逐字节复现通过）。预算加大时它防轮 2 空烧。

`--layers F.Cu,B.Cu` 与 `--board/--out/--report` 按运行填写。

**复现器**：`/tmp/refine_pass/v2/run_experiments.sh` 的 COMMON 与臂旗标即
本表来源（其基线对拍在 refine_pass 中两板逐字节复现通过 = 本表旗标经过
验证；实验臂另加的 `--refine --refine-k 8` 与加大 budget 不属冻结配置）。

## 基线数字（/tmp/q5_diag/*.json 引擎自记，权威）

| 板 | JSON | 完成度 | vias | segs | len(mm) | 墙钟 | stop | iter_costs |
|---|---|---|---|---|---|---|---|---|
| pic | `pic_dc17.json` | 33/33 | 49 | 427 | 1646.8 | 195.7s | converged | [[11,56],[0,0]] |
| interf | `interf_net_b650.json` | 108/109 | 206 | 1312 | 4750.9 | 664.2s | budget | [[96,475],[1,0]] |

JSON 同时记录：h_weight（pic 1.0 / interf 2.0）、diag_cost 1.7、
diag_node_cap（interf 300000；30 网触发 8→4 向回退）、via_pitch_band 0.3、
via_pitch_star 自适应（interf {F.Cu\|v:1.4, B.Cu\|h:1.0}；pic 空）、
self_drc 两板均 []、conflicts 均 0。未布通：interf /MA14
（"异网铜封路"，known-issue 同 Q1）；pic 无。

## JSON 不记录、以命令行为准的旗标

via_pitch_factor / max_iter / budget_s / stagnation / par_factor /
perp_factor / turn45_factor / turn135_factor / via_corner_cost / width /
refine(-k)。复现实验只认命令行 + 本文档，别从 JSON 反推这些值。

## --refine 明确排除（两轮第二轮 NEGATIVE 收口）

孔仅 −0.5%（interf 206→205）/ −6.1%（pic 49→46），远不及 −10% 判据；
"孔为布线期层分工结构代价"第三次独立证实。`--refine` 不进推荐档、
不出现在任何冻结命令行；代码保留（四元快照/精修轮骨架 V2 可复用）。

## 勘误（Q5 PLAN 定档节笔误，本文档为准）

- **pic h-weight 实为 1.0，非 2.0**：PLAN 定档行与记忆原记 2.0，但
  `pic_dc17.json` h_weight=1.0，且 refine_pass 以 1.0 做过两次逐字节
  复现（inert OLD/NEW 树）。2.0 是 interf 的值，抄写串行。
- **pic 完成度 33/33，非 108/109**：PLAN 定档行"→ 108/109"为 interf 行
  抄写笔误（pic 全网只有 33 条）。

## 已知问题携带（重采时别当回归）

- interf +4 条 copper_edge_clearance＝既有异形板边建模缺口
  （model.py `_read_outline` 只取 bbox），挂 M4，四向基线同洞。
- /MA14 异网铜封路未布通（Q1 起挂 known-issue）。
- 墙钟读数受环境负载污染（VSCode 失控进程 PID 877 曾吃 317.9% CPU，
  负载 2.7–3.3×）：budget 逐板标定须在干净机器上核，本表 600/650 为
  无负载定档值。

## ⚠️ route_v2 重采前置待办（采集侧代码缺口，非引擎）

`Placement/src/route_rl/loop.py` `_engine_round` 子进程只转发 6 个基础
参数（`--board --out --clearance-um --layers --budget-s --report`）——
**全部质量旗标（dir-prior/space/via-pitch/diag45/h-weight/diag-node-cap/
stagnation）都没转发**。照现状开 route_v2 重采，采集跑的是裸缺省引擎
（无 Q1–Q5 质量栈）。重采前必须补转发（改采集侧 loop.py，不动引擎本体，
不破冻结）。相关 env：`RRL_ENGINE_BUDGET_S`（缺省 900）、
`RRL_ENGINE_STAGNATION`（缺省 0）、`RRL_KICAD_ACCESS_SRC`（权威树推导，
勿指向 venv 冻结副本）。

其余语料板（KiCad 官方 demo 板）的逐板档位（budget / diag-node-cap 标定）
在重采阶段按同一 COMMON 底座扩展，不在本次冻结范围。

## 附录：镜像 checksums（首录 2026-10-02；最近重录 2026-10-10 45° 旋转烘焙解冻，两树一致；历次取代值见下文叙事与本日附录续）

```
engine.py     e2f675905bea45dd915b75aeb72a2ce1
astar.py      937a41e2c87d2535e73e90c6104c9ea8
model.py      e208fbdb7f824f404e92415421cdd9d0
snap.py       f4fb830832db4cdf806d580f76650ce2
freeroute.py  3e57efd120bb9a04b1492867ffe02cca
drc.py        17cbadc23f65a199b1430c1c0388a4c0
```

2026-10-02 首录值（Q7 前）：engine `8bd5e2cff43f6542bfe01c2f0d827988` /
astar `ff4d823dae169fb595ca92c83d48fcaa`。Q7 Step 1（2026-10-05 立项授权
解冻）改此二文件加 `--bundle-lay` 旗标族（默认关）；解冻纪律四步全过：
惰性双板 `cmp` 逐字节相等（pic + interf IDENTICAL）、pytest 0 Q7 回归
（34/34 单测绿）、镜像手工拷贝 `diff -rq -x __pycache__` 两树全等。
**Q7 修复轮（2026-10-06）**：Step 1 把武装网 h 乘 bundle_factor（照搬 OFF
在生产的 Q3 井先例）与 h_weight>1 的故意贪心互毁——interf 有效 h_weight
2.0→1.0、展开爆炸、逐网预算熔断（探针 bl-a1 70/109 触发 fail-fast）。
修法 = 删 h 缩放，井纯走步边折扣（与生产 via-pitch 落点井同口径）。
解冻四步重过：双板 cmp 逐字节 IDENTICAL、pytest 34/34（正锁测试按
修复后机制重定标为「井内走廊偏好」+控制组对照）、镜像 diff -rq 全等、
本表 astar MD5 重录。**Q7 修复轮 2（同日晚，上表即现值）**：修复轮 1
后探针 bl-a1 仍 68/109——根因是撞 `diag_node_cap` 的武装网落进
`_diag_fb` 四向**无帽**重灌（legacy 表无节点上限，~2.4ms/pop 无界泛洪，
7 杀手网吃掉 Phase 3 墙钟 92%）。修法三处（全部门控在旗标后）：
撞帽武装网改为降级 + 无井重布（逃生门 ②）、`propagate_cap` 传播通道、
`_diag_fb` 网永不武装。解冻四步：pytest 35/35（新增撞帽降级单测）；
pic 双腿风暴中 262/298s IDENTICAL；**interf 走无预算收敛路径**
（`--budget-s 100000` 双腿同 CLI）108/109 segs1312 vias206
**逐字节 IDENTICAL**——墙钟 5571s vs 2115s 差 2.6× 不动字节，实证
budget 停机点=墙钟函数、收敛路径才是负载免疫的对拍正解（冻结 CLI 的
budget 档对拍在负载漂移机不可逐字节化，两次 84/22、33/98 作废定案）；
镜像 diff -rq 全等、本表 engine MD5 重录。**Q7 修复轮 3（2026-10-07，
上表即现值）**：修复轮 2 后探针 bl-a1 85/109 仍差门 15 网——干净插桩量级
表定因：21 张 ARM-DEMOTE 网全数撞满 `diag_node_cap` 300000（Σ261s 白烧 =
Phase 3 的 54%），armed-ok 14 网仅 MA12 一张超 150k pops。修法两条
（全部门控在 `--bundle-lay` 后，默认关=inert）：F1 `--bundle-arm-cap`
150000 pops（武装尝试专属 pop 帽，撞帽走既有降级梯）+ F2
`--bundle-arm-reserve-s` 120.0s（装井前墙钟储备闸，只降质量不降完成度）。
解冻四步：双板 cmp 逐字节 IDENTICAL（interf 收敛路径 913/925s、pic 冻结
原档 192/188s）、pytest 37/37（新增 F1 帽选择锁 + F2 储备闸锁两测）、
镜像 diff -rq 全等、本表 engine+astar MD5 重录。**推荐档 CLI 不变**——
Q7 若出推荐档，须经用户签认后另行修订本文档。**V3.1 ① 狗骨逃逸
（2026-10-07，上表即现值）**：V3 总体计划 V3.1 动作包①
（`eval_out/v31_action_pack/PLAN.md`）改 engine.py + astar.py 两文件，
加 `--dogbone` 族旗标（`--dogbone` / `--dogbone-reach` / `--dogbone-lat`
/ `--dogbone-cap`，默认关=inert）——细间距盘短桩出脚+就地过孔换层的
落点候选注入 route() 两端，梯队同出口回退（仅栅格锚全灭的端注入，
锚在则零注入）。中途发现并修复：goal 侧若无条件混入，异质尾巴（狗骨
格值 ~2.63 vs 栅格锚尾巴 ~0.17）会让小 g 大尾巴的次优狗骨路径在
「首弹即停」终止下抢先弹出（易板实测复现）——门控收紧为 `not goals`
/ `not starts`，锚在则字节级 inert。解冻四步：双板 cmp 逐字节
IDENTICAL（pic 冻结原档 203/201s、interf 收敛路径 987/948s；JSON 报告
差 62B/腿 = 新增三个 inert 报告键 `dogbone:false` / `dogbone_seeds:{}`
/ `dogbone_used:[]` + 墙钟/输出路径元数据，segs/vias 427/49、1312/206
两侧全同）、pytest 40/40（新增 3 测：易板 inert 逐字节、封死 QFP 盘
逃逸+净索引铜距审计、落点带死时的能力边界上报）、镜像 diff -rq 全等、
本表 engine+astar MD5 重录。**推荐档 CLI 不变**——`--dogbone` 族不进
冻结命令行；V3.1 若出推荐档，须经用户签认后另行修订本文档。**V3.1 ①-A
颈缩+二遍法补针（2026-10-08，上表即现值）**：拍板 A 落地（REPORT_step2
§五）——engine.py 收尾加补针遍（出厂净化后、报告前：正常遍不开狗骨 →
只对失败网逐网开狗骨+颈缩重布，成功即 commit、失败维持飞线；此刻铜面
= 最终静态障碍，冲突未解丢弃网同样获救），RouteConfig 加 `dogbone_width`
（None=桩与网络同宽=Step 1 语义）；astar.py 狗骨种子按桩半宽
W/2+净距枚举（窄桩挤得进 0.4mm 节距盘间），物化分片（头/尾桩段按桩宽、
中段网络宽，片界成对点保换层 via 原位；桩宽 None 或无狗骨段 → 单次调用
= 字节级 inert）。新增旗标 `--dogbone-rescue` / `--dogbone-width` /
`--dogbone-rescue-budget-s`（默认关/None/0=inert，0=共享 --budget-s 残额）。
实现中发现：RouteConfig 是 frozen dataclass——补针翻开关走
`dataclasses.replace` 换新 cfg 对象（桩宽构造期已随 cfg_kw 进 cfg）。
解冻四步：tinyfpga_bx+watchy 双板 cmp 逐字节 IDENTICAL（冻结 COMMON 无
--dogbone，9.0s/29.4s；JSON 报告含墙钟不作 cmp）、pytest 43/43
（新增 3 测：颈缩 inert 逐字节、颈缩分片物化（两档宽+桩链盘心→via 落点+
确定性重放）、engine 收尾补针（对照关=诚实失败、开=救回+记账+正常网不动））；
全量套件另有 10 failed + 12 errors 均系环境缺板与 Q4 期 `_cost(snap)` 签名
陈旧测试（改前镜像同签名，与本次无关）、镜像 diff -rq 全等、本表
engine+astar MD5 重录。**推荐档 CLI 不变**——`--dogbone` 族（含新三旗标）
不进冻结命令行；V3.1 若出推荐档，须经用户签认后另行修订本文档。

其余文件（__init__/anchors/diff/dsn/l1/merge/planes/regions/stitch/
quality）以 `diff -rq -x __pycache__` 两树全等为准（quality.py 系本次
补同步——refine_pass 给 snap.py 加 `from .quality import quality` 时漏传
镜像，引擎入口不依赖它，仅 snap 直用路径受影响，已修复）。

**V3.1 ② 联立布（2026-10-08，上表即现值）**：REPORT_step2 §九勘误定位
救援遍逐网 commit 自相残杀（先救回的同族相邻网铜挤死后来者盘口，
watchy /32K_P、ACC_INT_2 纯净铜面可连）。engine.py 只改 engine 一个文件：
救援遍加 `--dogbone-rescue-joint`（默认关）——两阶段：失败网全体先对
同一静态铜面盲布（互不见对方铜）缓存路由，再按原序重验 commit
（`_rescue_route_clear` 判据与 A* 落格全同：edge_clear 同网豁免 +
_via_clear 全层，radius=半宽+净距；铜只增不减，重验干净⇔此刻现布合法）；
撞已 commit 联立铜者当场重布（回退顺序臂语义，联合臂对每网只增不减）；
预算耗尽且冲突的缓存铜不 commit、诚实报错。报告回显
`dogbone_rescue_joint` / `dogbone_rescue_joint_conflicts` 两键（inert 臂
亦有回显故报告不可逐字节 cmp，比铜面/数据字段）。解冻四步：tinyfpga_bx+
watchy 双板冻结 CLI cmp 逐字节 IDENTICAL（8.8s/28.7s，JSON 仅差两个新
inert 回显键+输出路径）、pytest 全量签名同改前（route 文件 44/44 新增
1 测：inert 铜面全同 / 联立臂救回+零冲突 / monkeypatch 重验恒 False 走
冲突回退仍救回+记账）、镜像 diff -rq 全等、本表 engine MD5 重录
（87e2c35f…→d8687762…，astar 未动）。**推荐档 CLI 不变**——
`--dogbone` 族（含 --dogbone-rescue-joint）不进冻结命令行；V3.1 若出
推荐档，须经用户签认后另行修订本文档。

## 附录续（2026-10-08 深夜）：V3.1 尾巴交付——actions.py 纯新增，引擎六文件复核未动

交付物 = `route/actions.py`（`RoutingSession` 动作协议 Routing 版 + 快照
原语，V3.2 依赖；设计定案见 `eval_out/v31_action_pack/PLAN.md` 定案节，
交付报告 REPORT_tail.md）。**零改动 engine.py/astar.py**，解冻四步按
「更名先例」（内容未变不重跑）走 MD5+隔离证明口径：

1. **惰性对拍**：上表六文件 MD5 复核与冻结记录**逐字节一致**
   （engine d8687762…/astar 1c368c35…/model/snap/freeroute/drc 全同）；
   且导入单向隔离——全 src 树无任何模块引用 actions（actions→engine
   单向），冻结 CLI 入口（engine.py main）不触及新文件，被执行字节
   集合未变，双板重跑无信息量（budget 档在负载漂移机本就不可逐字节
   化，见上 Q7 修复轮 2 定案），不重跑。
2. **pytest**：新增 tests/test_route_actions.py **11/11 绿**；全套基线
   10 失败 + 12 错误均为存底漂移（真板 FileNotFoundError 族 + 旧模块
   CLI + 2 处 route_engine `_cost` 签名演化），经 stash 对照证明（移出
   新文件重跑 → 失败集逐条相同）与本次交付无关。
3. **镜像**：`Routing/route/actions.py` 已补拷（MD5 5f988a9da00772ab
   d7185a237c2f3c34，两树同值）；`diff -rq -x __pycache__ KiCAD_Access/
   src/kicad_access/route Routing/route` **两树全等**。
4. **附录记账**：engine.py/astar.py MD5 维持冻结值不重录；actions.py
   新增行记 `5f988a9da00772abd7185a237c2f3c34`（30337 B / 669 行）。

动作面（act() / JSONL 审计）与冻结 CLI 分轨：审计重放走
`ROUTE_AUDIT_DIR`，冻结命令行不受影响。

## 附录续（2026-10-10）：工艺参数解冻——engine.py + astar.py 加
`--width/--via-size/--via-drill/--pad-clearance-um/--relax-radius`

授权 = 用户明示（"要修改我们的引擎，可以设置 PCB 走线参数。达到类似
人工 PCB 的水平"）；参数取值标定源 = BusPirate5 人工板实测（线宽主力
0.203 / via 0.6548/0.35 / 盘级局部净距 override 0.08×16 处 / 全局净距
0.2）。改动面：engine.py 签名+CLI+报告键（via_size/via_drill/
pad_clearance/relax_delta_mm/relax_triggered_pads/relax_zone_cells）；
astar.py RouteConfig 加 `pad_clearance`/`relax_radius` 两字段 +
`_relax_zone` 触发检测（盘按 bbox 覆盖入 0.5mm 桶、bbox⊕thr 扫邻域、
bbox 间隙 < thr=线宽+2×净距 才触发；触发盘的 `relax_radius` 圆域内
净距按 全局−delta，delta=clearance−pad_clearance）+ ObstacleIndex
`relax_cells/relax_delta` 两口径挂点（`point_clear`/`_edge_scan` 头部
查格减 radius）。

解冻四步全过：

1. **公告范围**：本条 + 上表 engine/astar 两行重录（2026-10-08 ①-A 值
   engine d8687762… / astar 1c368c35… 被本次取代，首录与 Q7 史值见上
   文叙事保留）。model/snap/freeroute/drc/actions 五文件未动。
2. **双树同步**：`diff -rq -x __pycache__` 两树全等；MD5 两树同值
   engine `e2f675905bea45dd915b75aeb72a2ce1`（81880 B / 1485 行）、
   astar `937a41e2c87d2535e73e90c6104c9ea8`（77645 B / 1388 行）。
3. **测试**：新增 3 测试（`test_pad_relax_default_dead_gap_alive_relaxed`
   = 死缝 0.5 缺省死/relax 活+触发数+双口径复核、
   `test_pad_relax_inert_when_gap_not_triggered` = 缝 1.2 开参数也
   逐项一致、`test_pad_relax_validation` = 越界 raise）3/3 绿
   （test_route_astar.py 现 47 collected）；全套 10F/12E 与 10-08
   tail 存底漂移逐条一致（fixture FileNotFoundError 族 + 旧 `_cost`
   签名 + 老 CLI 文案断言，无 pad relax 语义关联）。
4. **确定性对拍**：buspirate5 冻结旗标（COMMON 全量内联）重跑，新参数
   全缺省，输出板 MD5 = `607bbb9b06f1fba3deb1c17b38431ac6` 与冻结基线
   `v31_action_pack/baseline/buspirate5__base.kicad_pcb` **逐字节一致**
   ——新参数默认 inert 铁证。踩坑记录：zsh 无词切分，后台脚本 `$COMMON`
   整串成一个 argv（rc=2 unrecognized arguments），旗标须全内联。

**推荐命令行不变**（本文 COMMON 一字未动）；`--width` 为既有旗标本次
补标定注释，`--via-size/--via-drill/--pad-clearance-um/--relax-radius`
为新增、缺省全等于旧行为。人类参数臂（本解冻的实验读数，非冻结项）：
同板 `--clearance-um 200 --width 0.203 --via-size 0.6548 --via-drill 0.35
--pad-clearance-um 80` + COMMON 其余不动，结果见
`eval_out/complex_eval/buspirate5/REPORT.md` 附录。

## 附录续（2026-10-10）：45° 旋转烘焙解冻——model.py `_read_pad` 斜放盘 AABB

授权 = 用户明示（"修 model.py 的 45° 旋转烘焙！"）。缺陷：`_read_pad`
对盘旋转角的处理仅一行"竖放交换宽高"（`if 45.0 < prot % 180.0 < 135.0:
w,h = h,w`）——严格介于 45°~135° 才交换，45° 本身不触发任何变换 →
斜放盘按未旋转占位建模。BusPirate5 U103（QFP-48 斜放 45°）0.875×0.2 盘
真外接应 0.762×0.762，y 向低估 0.56mm → track 压真铜面短路（KiCad 实测
间隙 0.0018mm，人类参数臂 VREG_ADJ_MCU 短路条的根因；基线档同一危险带
存在，只是该网未布通没踩中）。新旧归属：老代码简化，非此前任何解冻引入。

改动面：`model.py` `_read_pad` 一处——按 `q = prot % 180.0` 三路：0° 恒等；
`abs(q−90)≤1e-9` 精确交换（与旧路径逐位一致）；其余任意斜角烘焙旋转 AABB
`w'=w·|cosθ|+h·|sinθ|、h'=w·|sinθ|+h·|cosθ|`（radius=rratio×min 随新
min 取值，属预期）。角度 token 语义不变（板绝对角，封装旋转不参与）。
engine.py/astar.py/snap/freeroute/drc/actions 零改动。

解冻四步：

1. **公告范围**：上表 model.py 行重录（2026-10-02 首录值
   `ea9d755f4a39e6ea37325df25113bed3` 被本次取代，见本条）。engine/
   astar 本日工艺参数解冻值 `e2f675…/937a41…` 维持不变。
2. **双树同步**：`diff` 两树全等；MD5 两树同值 model.py
   `e208fbdb7f824f404e92415421cdd9d0`（26189 B）。
3. **测试**：新增 `test_smd_pad_45_bakes_true_aabb`（45/135/315 → 两向
   同 (0.875+0.2)·cos45°；radius 随新 min；0°/90° 逐位不变），
   test_route_model.py 21/21 绿；全套 **10F/218P/7S/12E** 与 10-10 晨间
   基线 10F/217P/7S/12E 漂移逐条一致，+1 pass 即新测试，零新增失败。
4. **确定性对拍（双解析等价性扫描）**：以"现源码把新块换回旧块"重建旧版
   模块，importlib 双载入，同板双解析逐 Pad diff（raw 正则法因重复焊盘号
   撞键不可靠，弃用）。扫 18 池板 + interf_u_base + fix_base +
   fr_clean：**13 板 IDENTICAL**（含冻结参照板 interf_u/fix_base →
   推荐档解析输入逐位不变、行为可证不变；另 cantact/complex_hierarchy/
   esp32_touchdown/esp_rust_board/flat_hierarchy/tinyfpga_bx/tinyreflow/
   upduino/watchy/am32/bms_c1 全部焊盘正交）；**7 板 CHANGED 且全部为
   非正交盘长大到真 AABB**（buspirate5 82/866 全 45°、crkbd 44/942、
   fomu 3/148、mppt_2420_hc 2/440@165°、orangecrab 1/893@225°、
   piantor 27/310、tokay 3/515）——即修复目的本身。正交板布线输出=解析
   输入的纯函数，无需重跑；7 板行为改变须重跑记档（buspirate5 见
   `complex_eval/buspirate5/REPORT.md` §九；旧参考 607bbb9b… 被取代）。

## 附录续（2026-10-11）：挂账⑤段级派活——segment.py 纯新增，冻结六文件零改动

授权 = 用户拍板（"我拍板！"，挂账⑤三腿之首：派活单位 整网→段、计分
网连通→焊盘连接率；REPORT §十二）。交付物 = `route/segment.py`
（551 行 / 26917 B）：`SegmentRouter` 包 `NegotiatedRouter`——Phase A
全网 MST 边全局池按 (边长, 网, 盘 key) 升序逐边布、即布即
`router.commit` + 井采样三连（engine Phase 2 同款调用序）；Phase B
真实铜面孤岛桥接（`_pad_roots` 并查集，判据镜像
freeroute.`_pads_connected`，单测锁等价）；Q5 软帽回退为边级
（`DiagNodeCapExceeded`→`_diag_fb`→四向重试，engine `_route_net_once`
认可口径）；报告含 engine 键面 parity + `pads_connected/pad_coverage/
phase_a/phase_b` 调度器增量键。语义差异（有意）：无协商轮（逐边 commit
铜面只增、冲突按构造 0）、hist/refine/stagnation/max_iter 形参 inert。
CLI 旗标面与 route.engine 全同（协商轮旗标标 inert），可直接替换命令行。

解冻四步：

1. **冻结零改动**：engine.py/astar.py/model.py/actions.py 维持
   2026-10-10 解冻值不重录（本交付零触碰）；segment.py 为纯新增文件。
2. **pytest**：新增 tests/test_route_segment.py **5/5 绿**
   （镜像等价锁四形态/三网闭环/Phase A skip/边失败网不死+Phase B 桥接
   尝试/CLI 冒烟）；全套 **10F/223P/7S/12E** vs 10-10 基线 10F/218P/7S/12E
   ——漂移集逐条一致（含 test_route_engine `_cost` 两处存底签名漂移，
   stash 对照排除），+5 pass 即新测试，零新增失败。
3. **镜像**：`Routing/route/segment.py` 已补拷，MD5 两树同值
   `f0ee0083272f7976757149730dbd937d`（26917 B）；`diff -rq` 仅
   `__pycache__` 陈旧字节码差异，源码全等。
4. **附录记账**：segment.py 新增行记上 MD5；engine/astar 不重录。

## 附录续（2026-10-12 深夜）：纯记账——引擎重布细脚距参数对（零代码改动）

意图②收尾（r3_engine REPORT §十一）捞出的参考参数，**不改任何冻结文件**，
仅记录在此供 RoutingSession 后处理场景取用：

- **参数对 clearance 230µm + width 0.2mm**：width 0.25（half_w 0.37）穿不进
  LQFP 0.5mm 脚距脚缝，0.2（half_w 0.345）+ 230 恰好能穿——引擎在 FreeRouting
  已布铜里重布细脚距网的前提参数对。clr 245 配 0.2 仍偏紧（XTALI 需 4 孔）。
- 证据：REPORT §十一 五步证据链（后处理/预布两路全试，NEGATIVE 定论；
  预布真执行框架 route_intent.py 落位 `eval_out/v32_backfill/r3_engine/`）。

## 附录续（2026-10-14）：补记账——segment.py 深夜追修的 MD5 漏录（零代码改动）

P0 环境解耦收口做八文件 MD5 复核时发现：10-11 附录续记 segment.py
`f0ee0083…`（26917 B / 551 行）之后，**同晚 22:12** 挂账⑤收官的
S 臂 47.7GB 爆内存追修（`--edge-slice-s 45` 墙钟切片，REPORT §十三
权威；当时已做回归锁+两树镜像）没有回来补录附录。现补记：

- **两树现状**：`KiCAD_Access/src/kicad_access/route/segment.py` 与
  `Routing/route/segment.py` 逐字节一致（镜像纪律完好），MD5 均为
  **`c74bca10a485aa715effa0fc4d445a83`**（29759 B / 621 行）。
- 本条取代上一条的 `f0ee0083…`（该值对应追修前版本）；其余七文件
  （engine/astar/model/snap/freeroute/drc/actions）2026-10-14 复核
  与各自冻结值逐字节一致，零改动。
- 顺带基线快照：今日全套 pytest **10F/228P/7S/12E**，与 10-11 存档
  基线 10F/223P/7S/12E 同型（漂移集一致，+5P 为其后的新增测试）。
