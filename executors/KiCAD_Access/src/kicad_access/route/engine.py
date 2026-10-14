"""协商式 rip-up-reroute 引擎（PathFinder 骨架 ⊕ astar 底座，2026-09-23）。

scripts/negotiate.py 手搓原型的正式化：协商循环骨架（独立布线 → 全量
commit → 冲突检测 → 全拆 → 优先级重布）保留，原型的全部硬编码
（RIP_NETS/TRAPPED/VIA_IN_PAD_BALLS 网表、板框常数、CLS=0.10）换成
RouteBoard 板模型 + pads_by_net 自动网表 + RouteConfig 工艺参数驱动：

- **网表自动化**：板上一切「≥2 盘、无 zone、且未连通」的网络
  （freeroute._pads_connected 判已连通——finish pass 的既有电源铜自动
  跳过）。有 zone 的网跳过：fill 语义保证连通（同 drc 断裂检查口径）。
- **盘中孔机制的一般化**：底板已有铜（含盘中孔固定过孔）一律视为障碍
  ——ObstacleIndex 构造即登记原板全部铜，无需点名任何坐标。
- **撕布 = GridRouter.rollback**（索引与板同步退回，按对象身份）；
  **历史惩罚经 route(hist=…) 进 A* 代价**（astar 本版新增的可选参数，
  协商层独享，L1 缺省行为不变）。被撕冲突网的栅格位单调加罚避震荡。

收敛判据（原型缺件 1/3）：全连通且零冲突 → ok；max_iter/预算用尽 →
回滚到历史最优快照（（不可用网数, 冲突对象数）字典序最小——不可用
含冲突网，短路与飞线同罪）再净化残余冲突网（铜整网弃除，输出零短路、
留诚实飞线），如实报 partial——RL 侧照常打分，不圆场。
性能预案（缺件 3/3）：纯 Python，轮间查墙钟预算
（budget_s），超支停布转收尾；单次 route() 无节点上限（astar 无 pop
limit），大板慢网靠预算兜底。缺件 2/3（kicad-cli DRC 验收环）不在
本模块：route_rl.loop 引擎分支复用既有 ⑤ 步，口径与 FreeRouting 基线
天然一致。

已知限制（与 FreeRouting DSN 流同源）：平面 zone 不入障（ObstacleIndex
只登 pad/segment/via）——4 层板过孔穿平面靠 kicad-cli 终判把关。

CLI（kicad_access 源码树，editable venv 或 PYTHONPATH=KiCAD_Access/src）：
     python -m kicad_access.route.engine --board in.kicad_pcb \
         --out out.kicad_pcb [--clearance-um 230] [--layers F.Cu,B.Cu]
         [--width 0.25] [--max-iter 15] [--budget-s 600] [--stagnation 0]
         [--dir-prior] [--par-factor 0.85] [--perp-factor 1.5] \
         [--via-cost 0.2] [--space] [--space-band 0.75] [--space-cost 0.6] \
         [--pitch] [--pitch-band 0.15] [--pitch-factor 0.85] \
         [--pitch-star 1.0] [--pitch-cap 2.5] [--pitch-samples 50] \
         [--bundle-order] [--diag45] [--turn45-factor 0.3] \
         [--turn135-factor 2.0] [--via-corner-cost 0.3] \
         [--diag-cost 1.0] [--h-weight 1.0] [--diag-node-cap 0] \
         [--report r.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .astar import (DiagNodeCapExceeded, GridRouter, RouteConfig,
                    _BundleCtx)
from .drc import run_drc
from .freeroute import _pads_connected
from .model import Pad, RouteBoard, Segment, Via
from .regions import _append_blocks, _segment_block, _via_block


# ---------------------------------------------------------------- 拓扑
def _mst_edges(pads: list[Pad]) -> list[tuple[Pad, Pad, float]]:
    """Prim 最小生成树（欧氏盘心距）——多盘网的连接拓扑。

    确定性：Prim 选取按 (dist, 解析序索引)；返回边按 (长度, 双方 key)
    升序——短边先布。O(n²) 对单网盘数（≤几十）绰绰有余。
    """
    n = len(pads)
    used = [False] * n
    dist = [math.inf] * n
    src = [-1] * n
    dist[0] = 0.0
    edges: list[tuple[Pad, Pad, float]] = []
    for _ in range(n):
        k = min((i for i in range(n) if not used[i]),
                key=lambda i: (dist[i], i))
        used[k] = True
        if src[k] >= 0:
            edges.append((pads[src[k]], pads[k], dist[k]))
        for j in range(n):
            if not used[j]:
                d = math.hypot(pads[j].x - pads[k].x, pads[j].y - pads[k].y)
                if d < dist[j]:
                    dist[j], src[j] = d, k
    return sorted(edges, key=lambda e: (e[2], e[0].key, e[1].key))


@dataclass
class NetPath:
    """单网协商状态：已 commit 的铜（空 = 未布）+ 失败原因 + 冲突计数。"""

    segs: list[Segment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    err: str | None = None
    conflicts: int = 0

    def copper(self) -> bool:
        return bool(self.segs or self.vias)


class NegotiatedRouter:
    """PathFinder 协商循环架在 GridRouter 上（设计见模块 docstring）。"""

    def __init__(self, board: RouteBoard, *, clearance_mm: float,
                 layers: tuple[str, ...] = (), width: float | None = None,
                 via_size: float | None = None,
                 via_drill: float | None = None,
                 pad_clearance_mm: float | None = None,
                 relax_radius: float = 1.0,
                 max_iter: int = 15, hist_penalty: float = 0.10,
                 budget_s: float = 600.0, stagnation_k: int = 0,
                 dir_prior: bool = False, par_factor: float = 0.85,
                 perp_factor: float = 1.5, via_cost: float | None = None,
                 space: bool = False, space_band: float = 0.75,
                 space_cost: float = 0.6, pitch: bool = False,
                 pitch_band: float = 0.15, pitch_factor: float = 0.85,
                 pitch_star: float = 2.0, pitch_cap: float = 2.5,
                 pitch_samples: int = 50, via_pitch: bool = False,
                 via_pitch_band: float = 0.1,
                 via_pitch_factor: float = 0.85,
                 via_pitch_star: float = 0.0,
                 via_pitch_cap: float = 2.5,
                 via_pitch_samples: int = 20, bundle_order: bool = False,
                 diag45: bool = False, turn45_factor: float = 0.3,
                 turn135_factor: float = 2.0, via_corner_cost: float = 0.3,
                 diag_cost: float = 1.0, h_weight: float = 1.0,
                 diag_node_cap: int = 0, refine: bool = False,
                 refine_k: int = 8,
                 bundle_lay: bool = False, bundle_band: float = 0.3,
                 bundle_factor: float = 0.5,
                 bundle_pitch_lo: float = 1.0,
                 bundle_arm_cap: int = 150000,
                 bundle_arm_reserve_s: float = 120.0,
                 dogbone: bool = False, dogbone_reach: float = 1.6,
                 dogbone_lat: float = 0.8, dogbone_cap: int = 64,
                 dogbone_rescue: bool = False,
                 dogbone_width: float | None = None,
                 dogbone_rescue_budget_s: float = 0.0,
                 dogbone_rescue_joint: bool = False):
        """dir_prior：Q1 层方向先验（astar.RouteConfig.par/perp_factor 的
        协商层入口）——开启时过孔缺省降价 0.4 → 0.2（「打两孔换一笔到底」
        便宜过同层绕迷宫，对 FR bends 1.59 病灶）；显式 via_cost 优先。
        space：Q1 间距软排斥（RouteConfig.space_band/space_cost 的协商层
        入口）——净距 < band 线性加罚（贴 0 满罚 space_cost），≥ band 免费
        → 平行列距收敛到公共值（压 spacing_cv 的「贴着挤/独行绕开」并存）。
        pitch：Q3 节距吸引井（RouteConfig.pitch_* 的协商层入口）——净距
        落在 p*±pitch_band 给步进折扣，平行段吸向公共节距（SDF 只能
        「别贴太近」，井补「主动对齐」）。p* 两模式：pitch_samples=0 固定
        ≡ pitch_star；>0 自适应 = 已布前缀净距中位数（暖机期 p*=0 井
        休眠，采满 N 样本才激活——避免 CLI 初值污染采样正反馈）。
        via_pitch：Q3' via 落点节距井（RouteConfig.via_pitch_* 的协商层
        入口）——井只挂换层边：孔位到目标层最近平行段垂距落 k·p* 邻域
        → 折扣该边 via_cost（C 的教训内化：井挂步进边砸完成度，每条
        路径换层 0–3 次、作用面有界）。p* 按 (层, 方向) 桶两模式：
        via_pitch_samples=0 且 star>0 固定设全桶；>0 自适应 = 新孔
        垂距样本每桶中位数（暖机期桶空井休眠）。缺省 False 井 inert，
        既有消费者逐字节不变。
        bundle_order：Q3 束序——同元件对（两端盘 ref 集合相同）的网连着
        布，先布者定节距模式、后布者跟齐；缺省 False 旧序逐字节不变。
        diag45：Q5 45° 走线族——步进集扩 8 邻、斜步 step·√2、octile
        启发式、转向罚分级（45° 拐 ×turn45_factor、90° 拐 = turn_cost、
        135° 尖拐 ×turn135_factor）+ via-corner 罚（刚过孔即转向收
        via_corner_cost——孔不落拐点）。缺省 False 四向现状，关闭时
        步进/代价/启发式/罚全部回退，输出与改前逐字节等价（inert 锁）。
        diag_cost：斜步每毫米乘子（仅 --diag45 消费；>1 压长斜跑、短倒角
        仍便宜——人工模式「轴向长跑+拐角斜切」；默认 1.0 恒等）。
        h_weight：加权 A* 启发式膨胀（仅 --diag45 消费；>1 贪心化换搜索
        墙钟——8 邻大板 Phase1 烧预算的修法；默认 1.0 = 最优搜索）。
        diag_node_cap：Q5 单网软帽（仅 --diag45 消费）——8 向 A* 单网
        节点弹出数超此值，该网整网回退四向 legacy 桌（数值逐字节同
        diag45 关）并记入回退集（后续协商轮不再重试 8 向）。interf 实测
        病根：预算只在网间检查，硬总线网（/PC-A* 长并行族）8 向单网可独
        烧数百秒。0 = 不设帽（默认）。
        refine：两轮第二轮效率精修（refine_pass）——接受准则字典序扩为
        （完成度, 冲突, 孔数, 线长×10），协商轮完成度持平时孔少线短的
        快照胜出；冲突态收敛/停滞后转精修轮（选择性 rip-up：只撕浪费
        大的 refine_k 个网干净重布，网级 (孔, 线长) 字典序严格更优才留，
        否则重放旧铜）。缺省 False：二元代价 + 无精修轮，行为与改前
        逐字节一致（inert 锁）。
        refine_k：精修轮每轮按浪费代理（孔×2 + 线长/10）降序撕布的
        网数（仅 refine 消费；默认 8）。
        bundle_lay：Q7 整束联铺——S1 ref 对签名分束（2 ≤ 束 ≤ 32，单例
        解散），束内车道网（2 盘）按引脚序隐式车道连布（D3），Phase 3
        重布起软领随井把轴平行步吸向束内兄弟的 k·p* 节距格（D4）；p* 每
        束×层×向采 ≥2 样本一次定死（D2，先布者定节拍、后布者跟齐）。
        兜底三级：吸引失败清井重试一次 → 网级永久降级 → 既有墙钟。
        缺省 False 全 inert，序/路径逐字节不变。
        bundle_band/bundle_factor：井半宽（k·p* 邻域）与谷底步进折扣
        （仅 bundle_lay 消费）。bundle_pitch_lo：每束 p* 钳位下限
        （engine 侧状态不进 cfg；井底恒在 space_band 外——折扣不奖励
        贴挤）。
        bundle_arm_cap（F1，修复轮 3）：武装尝试独立弹出软帽（0 = 继承
        diag_node_cap）；撞帽走既有降级梯。烧钱表：降级网全撞 300k 全帽
        Σ261s = P3 的 54%，armed-ok 13/14 ≤134k → 默认 150k。
        bundle_arm_reserve_s（F2，修复轮 3）：剩余墙钟低于此值不再武装
        新网（束网直接普通布）——队尾防饿死闸，与 budget_s 同属墙钟
        分支。interf 队尾 25 网普通布约 42s → 默认 120s 留一倍余量。
        dogbone：V3.1 ① 狗骨逃逸（RouteConfig.dogbone_* 的协商层入口）
        ——细间距盘短桩出脚+就地过孔换层的落点候选注入 route() 两端，
        梯队同出口回退——仅栅格锚全灭的端生效，锚点在则零注入（字节级
        inert；Q6 21 硬块网的解药；同层可达时 via_cost 兜底天然不划算，
        封死/拥堵时胜出）。reach/lat/cap：落点轴向带远端（盘尖外 mm，
        双向含
        封装体内向）/横向带上限（盘轴两侧偏移 mm）/每盘候选上限。缺省
        False 全 inert，既有消费者逐字节不变。"""
        if clearance_mm <= 0:
            raise ValueError(f"clearance_mm 须为正，得 {clearance_mm}")
        self.board = board
        board.inherit_nets()          # EP 阵列继承网络（幂等；须在取网表前）
        cfg_kw = {"clearance": clearance_mm}
        if width is not None:
            cfg_kw["width"] = width
        # 工艺参数直通（2026-10-10 用户解冻令「设置 PCB 走线参数达人工
        # 水平」）：via_size/via_drill 原先无 CLI 暴露（恒 0.6/0.3，人工板
        # 实测 0.6548/0.35 进不来）；pad_clearance_mm/relax_radius =
        # 细间距盘免责净距（人工板对 QFN/QFP 盘逐处 override 的机制化，
        # 见 RouteConfig.pad_clearance）。协商层 _conflict_count/
        # _rescue_route_clear 走 ObstacleIndex → 自动同折减口径；自检
        # run_drc 仍按全局净距报（诚实口径——人工板同款 502 条间隙读数
        # 的语义，REPORT 侧解读）。缺省 None = 全 inert 逐字节不变。
        if via_size is not None:
            cfg_kw["via_size"] = via_size
        if via_drill is not None:
            cfg_kw["via_drill"] = via_drill
        if pad_clearance_mm is not None:
            cfg_kw["pad_clearance"] = pad_clearance_mm
            cfg_kw["relax_radius"] = relax_radius
        if via_cost is not None:
            cfg_kw["via_cost"] = via_cost
        if dir_prior:
            cfg_kw["par_factor"] = par_factor
            cfg_kw["perp_factor"] = perp_factor
            if via_cost is None:
                cfg_kw["via_cost"] = 0.20
        if space:
            cfg_kw["space_band"] = space_band
            cfg_kw["space_cost"] = space_cost
        if pitch:
            cfg_kw["pitch_band"] = pitch_band
            cfg_kw["pitch_factor"] = pitch_factor
            cfg_kw["pitch_cap"] = pitch_cap
        if via_pitch:
            cfg_kw["via_pitch_band"] = via_pitch_band
            cfg_kw["via_pitch_factor"] = via_pitch_factor
            cfg_kw["via_pitch_cap"] = via_pitch_cap
        if bundle_lay:
            cfg_kw["bundle_band"] = bundle_band
            cfg_kw["bundle_factor"] = bundle_factor
            if bundle_arm_cap:
                cfg_kw["bundle_arm_cap"] = bundle_arm_cap
        if diag45:
            cfg_kw["diag45"] = True
            cfg_kw["turn45_factor"] = turn45_factor
            cfg_kw["turn135_factor"] = turn135_factor
            cfg_kw["via_corner_cost"] = via_corner_cost
            if diag_cost != 1.0:
                cfg_kw["diag_cost_factor"] = diag_cost
            if h_weight != 1.0:
                cfg_kw["h_weight"] = h_weight
            if diag_node_cap:
                cfg_kw["diag_node_cap"] = diag_node_cap
        if dogbone:
            cfg_kw["dogbone"] = True
            cfg_kw["dogbone_reach"] = dogbone_reach
            cfg_kw["dogbone_lat"] = dogbone_lat
            cfg_kw["dogbone_cap"] = dogbone_cap
        if dogbone_width is not None:
            cfg_kw["dogbone_width"] = dogbone_width
        self.dir_prior = dir_prior
        self.space = space
        self.pitch = pitch
        self.bundle_order = bundle_order
        self.diag45 = diag45
        self.diag_cost = diag_cost
        self.h_weight = h_weight
        self.diag_node_cap = diag_node_cap
        self.dogbone = dogbone
        # V3.1 ①-A 二遍法补针（拍板 A，2026-10-08）：正常遍不开狗骨，
        # 出厂净化后只对失败网逐网开狗骨+颈缩重布（见 run() 收尾）。
        # rescue 预算独立计数（0 = 共享 --budget-s 残额）——不动正常遍
        # 口径，rescue 臂与基线可比。
        self.dogbone_rescue = dogbone_rescue
        self.dogbone_width = dogbone_width
        self.dogbone_rescue_budget_s = dogbone_rescue_budget_s
        # V3.1 ② 联立布（2026-10-08）：补针遍逐网 commit 的自相残杀修复。
        # 实测（④ 探针参照臂）：watchy /32K_P、ACC_INT_2 在纯净基线铜面
        # 可连，实跑死于先救回的同族相邻网铜挤死盘口。开 = 两阶段：失败
        # 网全体对同一静态铜面盲布（互不见对方铜）→ 按原序 commit，缓存
        # 路由对已 commit 联立铜重验净距，冲突者当场重布（回退顺序臂
        # 语义——联立对每网只增不减）。默认关（顺序臂可复现）。
        self.dogbone_rescue_joint = dogbone_rescue_joint
        # Q5 软帽回退集：8 向单网超帽的网（后续轮直接走四向，不再重试）
        self._diag_fb: set[str] = set()
        self.refine = refine
        self.refine_k = refine_k
        # 精修失败集：撕布后未严格更优的网（后续精修轮不再重试——
        # 高浪费位常驻 refine_k 前排，不排除则病网每轮重烧 A* 挤占预算）
        self._refine_failed: set[str] = set()
        # 井状态：固定模式（samples=0）p* 恒定；自适应模式暖机期 p* =
        # pitch_star（0 则井休眠），_pitch_update 采满样本后切中位数。
        self._pitch_fixed = pitch_star if pitch and pitch_samples == 0 \
            else None
        self._pitch_gaps: list[float] = []
        self._pitch_samples_want = pitch_samples
        self._pitch_cap = pitch_cap
        # Q3' via 井状态：固定模式（samples=0 且 star>0）全桶恒定；自适应
        # 模式暖机期桶空井休眠，_via_pitch_update 采满每桶样本设中位数。
        self.via_pitch = via_pitch
        self._via_pitch_fixed = (via_pitch_star
                                 if via_pitch and via_pitch_samples == 0
                                 and via_pitch_star > 0 else None)
        self._via_pitch_gaps: dict[tuple[str, bool], list[float]] = {}
        self._via_pitch_samples_want = via_pitch_samples
        self._via_pitch_cap = via_pitch_cap
        # Q7 束联铺状态：签名 → 有序成员表 / 网 → (束, 车道序)；每束 p*
        # （(束,层,向) → mm，≥2 样本一次定死）+ 原始样本 + 采过对去重；
        # armed = Phase 3 重布起武装（首布不吸）；demoted = 网级永久撤
        # 吸引（D5②，_diag_fb 先例）。
        self._bundle_lay = bundle_lay
        self._bundle_pitch_lo = bundle_pitch_lo
        self._bundle_members: dict[tuple[str, ...], list[str]] = {}
        self._bundle_rank: dict[str, tuple[tuple[str, ...], int]] = {}
        self._bundle_pitch: dict[tuple[tuple[str, ...], str, bool],
                                 float] = {}
        self._bundle_gaps: dict[tuple[tuple[str, ...], str, bool],
                                list[tuple[float, float]]] = {}
        self._bundle_sampled: set[frozenset[str]] = set()
        self._bundle_demoted: set[str] = set()
        self._bundle_armed = False
        # F2 预算闸（修复轮 3）：剩余墙钟 < 储备值后武装关门，束网直接
        # 普通布（井留给跑不完的现实）。_t0 在 __init__ 先置一次、run()
        # 开跑重置——直调 _route_net 的既有单测不破门。
        self._bundle_arm_reserve = bundle_arm_reserve_s
        self._bundle_arm_skipped: list[str] = []
        self._t0 = time.time()
        self.max_iter = max_iter
        self.hist_penalty = hist_penalty
        self.budget_s = budget_s
        # stagnation 早停：协商循环连续 K 轮无改进即停（0=关）。
        # 依 2026-09-23 归因实验：iter_costs 迭代 1 即达平台，其后 ~73%
        # 预算空转；早停只省墙钟，最优快照照常 restore，质量不变。
        self.stagnation_k = stagnation_k
        self.hist: dict[tuple[int, int, int], float] = {}

        # 网表：≥2 盘、无 zone、未连通（已连通的既有铜不重布）
        zone_nets = {z.net for z in board.zones}
        self._pads = board.pads_by_net()
        self._mst: dict[str, list[tuple[Pad, Pad, float]]] = {}
        self.targets: list[str] = []
        for net in sorted(self._pads):
            pads = self._pads[net]
            if net in zone_nets or len(pads) < 2:
                continue
            segs = [s for s in board.segments if s.net == net]
            vias = [v for v in board.vias if v.net == net]
            if (segs or vias) and _pads_connected(board, segs, vias, net):
                continue
            self._mst[net] = _mst_edges(pads)
            self.targets.append(net)
        self.paths: dict[str, NetPath] = {n: NetPath() for n in self.targets}
        self.router = GridRouter(board, RouteConfig(**cfg_kw), layers=layers)
        if pitch:
            # 固定模式 p* 恒定（_pitch_update 直通）；自适应模式暖机期
            # p* = 0（井休眠，Phase 1/2 同纯 SDF 布线）——首轮扫描实测
            # 用 CLI 值起步会正反馈锁死：p*=2.0 吸引导出远净距绕行路径
            # → 采到的样本净距偏远 → p* 稳在 1.6 → 全网绕行（24/109、
            # budget 强停）。采满样本后 _pitch_update 才设实测中位数。
            self.router.pitch_star = self._pitch_fixed or 0.0
        if via_pitch and self._via_pitch_fixed is not None:
            # Q3' 固定模式：全部 (层, 方向) 桶设同一 p*（层枚举走
            # router.layers，不硬编码层名——N 层板同样生效）。
            for lay in self.router.layers:
                for horiz in (True, False):
                    self.router.via_pitch_star[(lay, horiz)] = \
                        self._via_pitch_fixed
        if bundle_lay:
            self._bundle_plan()

    # ---------------------------------------------------------------- 内部
    def _defkey(self, net: str) -> tuple[float, str]:
        return (sum(d for _a, _b, d in self._mst[net]), net)

    @staticmethod
    def _wmedian(samples: list[tuple[float, float]]) -> float:
        """加权中位数（重叠加权；Q7 D2，与 step0b 同式）。"""
        W = sum(w for _, w in samples)
        acc = 0.0
        for d, w in sorted(samples):
            acc += w
            if acc >= W / 2.0:
                return d
        return sorted(samples)[-1][0]

    def _bundle_plan(self) -> None:
        """束联铺规划（D1/D3）：S1 ref 对签名分束，2≤|束|≤32 才联铺
        （单例解散、超帽不收）。车道网（2 盘网）走 D3 引脚序车道排，
        参照网（多盘网）按默认序殿后——只供 p* 采样/兄弟参照，不排
        车道。存 _bundle_members（签名→有序成员）与 _bundle_rank。"""
        bundles: dict[tuple[str, ...], list[str]] = {}
        for net in self.targets:
            sig = tuple(sorted({p.ref for p in self._pads[net]}))
            bundles.setdefault(sig, []).append(net)
        for sig in sorted(bundles):
            nets = bundles[sig]
            if not 2 <= len(nets) <= 32:
                continue
            lanes = [n for n in nets if len(self._pads[n]) == 2]
            refs = sorted((n for n in nets if len(self._pads[n]) != 2),
                          key=self._defkey)
            ordered = self._bundle_lane_order(sig, lanes) + refs
            self._bundle_members[sig] = ordered
            for rank, n in enumerate(ordered):
                self._bundle_rank[n] = (sig, rank)

    def _bundle_lane_order(self, sig: tuple[str, ...],
                           lanes: list[str]) -> list[str]:
        """D3 引脚序车道：A=sig[0]、B=sig[-1]，车道网两端各认一独盘；
        走廊轴 = |mean Δx| ≥ |mean Δy| 取水平（车道坐标 = A 盘 y），否则
        纵（坐标 = A 盘 x）；按 A 端坐标排、网名 tie-break。退化（<2
        车道 / 盘认不全）回退默认序，确定性不变。"""
        if len(sig) < 2 or len(lanes) < 2:
            return sorted(lanes, key=self._defkey)
        a_ref, b_ref = sig[0], sig[-1]
        pa: dict[str, tuple[float, float]] = {}
        pb: dict[str, tuple[float, float]] = {}
        for n in lanes:
            for p_ in self._pads[n]:
                if p_.ref == a_ref:
                    pa[n] = (p_.x, p_.y)
                elif p_.ref == b_ref:
                    pb[n] = (p_.x, p_.y)
        if len(pa) != len(lanes) or len(pb) != len(lanes):
            return sorted(lanes, key=self._defkey)
        ax = sum(pb[n][0] - pa[n][0] for n in lanes) / len(lanes)
        ay = sum(pb[n][1] - pa[n][1] for n in lanes) / len(lanes)
        coord = (lambda n: pa[n][1]) if abs(ax) >= abs(ay) \
            else (lambda n: pa[n][0])
        return sorted(lanes, key=lambda n: (coord(n), n))

    def _order(self) -> list[str]:
        """重布序：MST 总长升序（短网先布——快、少阻挡），平分按网名。

        束序模式（Q3，bundle_order=True）：同元件对（两端盘 ref 集合相同）
        的网聚成束连着布——并排的网先布者定节距模式、后布者跟齐（井的
        跨网协商载体，等价 QFP 出线/总线手指这类天然平行组）。束间按
        束内最短 MST 升序、束内按 (MST 总长, 网名)；确定性排序不变。
        缺省 False 走旧分支，序与旧代码逐字节一致。"""

        def key(net: str) -> tuple[float, str]:
            return (sum(d for _a, _b, d in self._mst[net]), net)

        if self._bundle_lay and self._bundle_members:
            # Q7 束联铺序：束连续（束内 D3 车道序），束位 = 首成员在
            # 默认 key 序中的位置（束间按束内最短 MST 升序），散网原位
            # 穿插——确定性不变。
            out: list[str] = []
            done: set[tuple[str, ...]] = set()
            for net in sorted(self.targets, key=key):
                r = self._bundle_rank.get(net)
                if r is None:
                    out.append(net)
                elif r[0] not in done:
                    done.add(r[0])
                    out.extend(self._bundle_members[r[0]])
            return out
        if not self.bundle_order:
            return sorted(self.targets, key=key)
        bundles: dict[tuple[str, ...], list[str]] = {}
        for net in self.targets:
            sig = tuple(sorted({p.ref for p in self._pads[net]}))
            bundles.setdefault(sig, []).append(net)
        out: list[str] = []
        for sig in sorted(bundles,
                          key=lambda s: (min(key(n)[0] for n in bundles[s]),
                                         s)):
            out.extend(sorted(bundles[sig], key=key))
        return out

    def _route_net(self, net: str,
                   hist: dict[tuple[int, int, int], float] | None = None
                   ) -> None:
        """布一网（Q7 束联铺 D5① 重试包装 + Q5 软帽回退分派）。

        束联铺：Phase 3 武装后束网先装兄弟井 ctx 再布；两条逃生门——
        ① 吸引下布不通（p.err 置位——ValueError 在 _route_edges 内已
        吞）→ 清 ctx 无吸引重试一次；② 武装 8 向撞 diag 帽（井诱导
        pop 爆炸）→ 永久降级入 _bundle_demoted + 无井重布——绝不走
        "带井四向"回退（legacy 桌无帽可撞，井折扣在上面是无界续灌，
        Step 2 探针实测的预算杀手正源）。finally 恒清 ctx。"""
        # F2 预算闸先于装井：剩余墙钟不足储备 → 本网放弃武装直接普通布
        # （只降质量不降完成度；记 _bundle_arm_skipped 供 report 读数）。
        if self._bundle_armed and not self._arm_budget_ok():
            self._bundle_arm_skipped.append(net)
            ctx = None
        else:
            ctx = self._bundle_ctx(net) if self._bundle_armed else None
        if ctx is None:
            self._route_net_once(net, hist)
            return
        self.router.bundle_ctx = ctx
        try:
            try:
                self._route_net_once(net, hist, propagate_cap=True)
            except DiagNodeCapExceeded:
                self._bundle_demoted.add(net)
                self.router.bundle_ctx = None
                self._route_net_once(net, hist)
                return
            if self.paths[net].err:
                self.router.bundle_ctx = None
                self._route_net_once(net, hist)
        finally:
            self.router.bundle_ctx = None

    def _arm_budget_ok(self) -> bool:
        """F2 预算闸（Q7 修复轮 3）：剩余墙钟低于储备值后武装关门
        （束网直接普通布，保队尾不因武装浪费饿死——插桩烧钱表：武装
        白烧 Σ261s 把 25 个队尾网挤出预算）。与 --budget-s 同属墙钟
        分支（既有非确定类）；仅 bundle_lay 的 Phase 3 武装路径消费。"""
        return (self.budget_s - (time.time() - self._t0)
                > self._bundle_arm_reserve)

    def _route_net_once(self, net: str,
                        hist: dict[tuple[int, int, int], float] | None = None,
                        *, propagate_cap: bool = False,
                        ) -> None:
        """布一网（MST 各边逐条 A*；任一边失败整网弃——未布通不算数）。

        Q5 软帽回退（仅 diag45 板）：8 向单网扩展超 diag_node_cap 的网
        记入 _diag_fb 并整网改走四向 legacy 桌（数值逐字节同 diag45 关，
        完成度有保底），后续协商轮不再重试 8 向——帽上浪费省掉。
        propagate_cap=True（Q7 武装尝试专用）：撞帽不落地回退而是上抛
        给 _route_net 走降级+无井重布（逃生门②）。"""
        if not self.diag45:
            self._route_edges(net, hist)
            return
        if net not in self._diag_fb:
            try:
                self._route_edges(net, hist,
                                  pops_mark=self.router.pops)
                return
            except DiagNodeCapExceeded:
                if propagate_cap:
                    raise
                self._diag_fb.add(net)
        self.router.diag_mode(False)
        try:
            self._route_edges(net, hist)
        finally:
            self.router.diag_mode(True)

    def _route_edges(self, net: str,
                     hist: dict[tuple[int, int, int], float] | None = None,
                     pops_mark: int | None = None,
                     ) -> None:
        """逐条 A* 布 MST 边（原 _route_net 主体；ValueError = 布不通记
        p.err，DiagNodeCapExceeded 穿透给 _route_net 走回退）。

        pops_mark（Q5 网级累计帽）：8 向分支传入起算弹出数——单边帽
        只约束一条 MST 边，VCC 类多边大网每边贴着帽烧、累计可达数
        百万弹出而单边帽永不触发；边循环检查 router.pops 累计差值
        超帽同样抛 DiagNodeCapExceeded（整网回退四向）。"""
        p = self.paths[net]
        segs: list[Segment] = []
        vias: list[Via] = []
        try:
            for a, b, _d in self._mst[net]:
                s, v = self.router.route(net, a, b, hist)
                segs.extend(s)
                vias.extend(v)
                if (pops_mark is not None and self.diag_node_cap
                        and self.router.pops - pops_mark
                        > self.diag_node_cap):
                    raise DiagNodeCapExceeded(
                        f"{net}: 8 向 A* 网级累计超帽"
                        f"（>{self.diag_node_cap} 节点）")
        except ValueError as e:
            p.segs, p.vias, p.err = [], [], str(e)
            return
        p.segs, p.vias, p.err = segs, vias, None

    def _rescue_route_clear(self, net: str, segs: list[Segment],
                            vias: list[Via]) -> bool:
        """联立布 Phase 2 重验：缓存路由对当前铜面（含已 commit 的联立
        铜）仍净距干净与否。判据与 A* 落格全同——edge_clear 同网豁免 +
        过孔全层 _via_clear，radius = 半宽 + 净距（铜只增不减，重验干净
        ⇔ 此刻现布同一路径合法，commit 后铜级不变量不破）。"""
        clr = self.router.cfg.clearance
        for s in segs:
            if not self.router.index.edge_clear(
                    s.x1, s.y1, s.x2, s.y2, s.layer, net,
                    s.width / 2 + clr):
                return False
        for v in vias:
            if not self.router._via_clear(v.x, v.y, net):
                return False
        return True

    def _pitch_update(self, net: str) -> None:
        """井的 p* 自适应（Q3）：采新布段的平行净距样本，中位数定 p*。

        固定模式（samples=0）直通返回。样本沿新 commit 的段每 ~1mm 点查
        一次 edge_gap（与 SDF/井同索引，增量零索引成本）；只收井区间
        (band_lo, cap] 内的净距——band 内是贴挤（井罚管）、cap 外太远
        （无平行邻居）。采满 N 样本后 p* = clamp(median, band_lo+
        pitch_band+0.05, cap)，井从暖机值切到实测节距。"""

        if not self.pitch or self._pitch_fixed is not None:
            return
        p = self.paths[net]
        if not p.copper():
            return
        band_lo = self.router._space_band  # 井罚下沿（= space_band）
        cap = self._pitch_cap
        clr = self.router.cfg.clearance
        for s in p.segs:
            length = (((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
                      ** 0.5)
            n = max(1, int(length))  # 每 ~1mm 一个采样点
            for i in range(n):
                t = (i + 0.5) / n
                x = s.x1 + (s.x2 - s.x1) * t
                y = s.y1 + (s.y2 - s.y1) * t
                slack = self.router.index.edge_gap(
                    x, y, x, y, s.layer, net, s.width / 2)
                if slack is None:
                    continue
                gap = slack + clr
                if band_lo < gap <= cap:
                    self._pitch_gaps.append(gap)
        if len(self._pitch_gaps) >= self._pitch_samples_want:
            self.router.pitch_star = min(
                max(statistics.median(self._pitch_gaps),
                    band_lo + self.router._pitch_band + 0.05),
                cap)

    def _via_pitch_update(self, net: str) -> None:
        """via 井的 p* 桶自适应（Q3'）：采新孔到最近平行段的垂距样本，
        每桶中位数定 p*。桶 = (附着段层, 段水平与否)——与井折扣/
        quality spacing_cv 的 (layer, horiz) 桶同键。

        固定模式（samples=0）直通返回。样本 = 每个新孔 × 每条端点落在
        孔位的正交附着段 → _nearest_parallel_gap（与井折扣同一量测，
        "采的 d、吸的 d、quality 量的 gap" 三者同源）。样本窗
        (0.2, cap]——下沿滤掉附着段自身垂距 0，上沿滤掉无平行邻居。
        采满 N 样本的桶 p* = clamp(median, 0.5, cap)。饿桶（该层无
        该方向段）永不激活 = 正确行为（无参照不吸引）。"""

        if not self.via_pitch or self._via_pitch_fixed is not None:
            return
        p = self.paths[net]
        if not p.vias:
            return
        layers = self.router.layers
        lo, cap = 0.2, self._via_pitch_cap
        for v in p.vias:
            for s in p.segs:
                at1 = abs(s.x1 - v.x) < 1e-6 and abs(s.y1 - v.y) < 1e-6
                at2 = abs(s.x2 - v.x) < 1e-6 and abs(s.y2 - v.y) < 1e-6
                if not (at1 or at2):
                    continue  # 端点不在孔位 = 非附着段
                dx, dy = abs(s.x2 - s.x1), abs(s.y2 - s.y1)
                if (dx < 1e-9 and dy < 1e-9) or (dx > 1e-9 and dy > 1e-9):
                    continue  # 零长段/斜段——井口径纯正交
                horiz = dy < 1e-9
                d = self.router._nearest_parallel_gap(
                    v.x, v.y, layers.index(s.layer),
                    0 if horiz else 1, exclude=s)
                if lo < d <= cap:
                    self._via_pitch_gaps.setdefault(
                        (s.layer, horiz), []).append(d)
        for key, gaps in self._via_pitch_gaps.items():
            if len(gaps) >= self._via_pitch_samples_want:
                self.router.via_pitch_star[key] = min(
                    max(statistics.median(gaps), 0.5), cap)

    def _bundle_ctx(self, net: str):
        """装配 Q7 兄弟井 ctx（D4 参照集 = 束内已提交兄弟的轴平行段）。

        降级网（D5②）/散网返回 None；四向回退网（_diag_fb）同样不武
        装——legacy 桌上没有 8 向帽可撞，井折扣在上面是无界续灌（Step 2
        实测 CLKLCA/PC-RST 两预算杀手正是此路）；段表空或该束 p* 全空
        也 None——无参照不吸引。表按 (层, 水平) 分桶、桶内排序，coords
        供 astar 二分定位。"""
        r = self._bundle_rank.get(net)
        if (r is None or net in self._bundle_demoted
                or net in self._diag_fb):
            return None
        sig = r[0]
        tbl: dict[tuple[str, bool], list[tuple[float, float, float]]] = {}
        for mate in self._bundle_members[sig]:
            if mate == net:
                continue
            for s in self.paths[mate].segs:
                dx, dy = s.x2 - s.x1, s.y2 - s.y1
                if (dx > 1e-9) == (dy > 1e-9):
                    continue            # 零长/斜段——井口径纯正交
                horiz = dy < 1e-9
                row = ((s.y1, min(s.x1, s.x2), max(s.x1, s.x2)) if horiz
                       else (s.x1, min(s.y1, s.y2), max(s.y1, s.y2)))
                tbl.setdefault((s.layer, horiz), []).append(row)
        if not tbl:
            return None
        ctx = _BundleCtx()
        for k, rows in tbl.items():
            rows.sort()
            ctx.tbl[k] = rows
            ctx.coords[k] = [row[0] for row in rows]
        ctx.pstar = {k[1:]: v for k, v in self._bundle_pitch.items()
                     if k[0] == sig}
        if not ctx.pstar:
            return None
        return ctx

    def _bundle_pitch_update(self, net: str) -> None:
        """束联铺 p* 采样（D2）：新提交网 × 束内已提交兄弟，同层同向
        平行对 → (垂距, 重叠加权) 样本入 (束,层,向) 桶；≥2 样本一次定死
        （clamp[pitch_lo, 2.5] + 0.2 网格化，"先布者定节拍"）。对采去重
        （frozenset）：Phase 2 后布者见先布者铜、Phase 3 撕全后只有先
        布者有铜——两种相位下每对恰采一次。"""
        if not self._bundle_lay:
            return
        r = self._bundle_rank.get(net)
        if r is None:
            return
        p = self.paths[net]
        if not p.copper():
            return
        sig = r[0]
        new = [(s, s.y1 == s.y2) for s in p.segs
               if (s.x1 != s.x2) != (s.y1 != s.y2)]
        if not new:
            return
        for mate in self._bundle_members[sig]:
            if mate == net:
                continue
            if not self.paths[mate].copper():
                continue
            pair = frozenset((net, mate))
            if pair in self._bundle_sampled:
                continue
            self._bundle_sampled.add(pair)
            old = [(s, s.y1 == s.y2) for s in self.paths[mate].segs
                   if (s.x1 != s.x2) != (s.y1 != s.y2)]
            for sa, ha in new:
                for sb, hb in old:
                    if sa.layer != sb.layer or ha != hb:
                        continue
                    d = abs(sa.y1 - sb.y1) if ha else abs(sa.x1 - sb.x1)
                    ov = (min(sa.x2, sb.x2) - max(sa.x1, sb.x1) if ha
                          else min(sa.y2, sb.y2) - max(sa.y1, sb.y1))
                    if 0.2 <= d <= 2.5 and ov > 0:
                        self._bundle_gaps.setdefault(
                            (sig, sa.layer, ha), []).append((d, ov))
        for (bsig, layer, horiz), samples in self._bundle_gaps.items():
            key = (bsig, layer, horiz)
            if bsig == sig and key not in self._bundle_pitch \
                    and len(samples) >= 2:
                self._bundle_pitch[key] = round(
                    round(min(2.5, max(self._bundle_pitch_lo,
                                       self._wmedian(samples))) / 0.2)
                    * 0.2, 2)

    def _conflict_count(self, net: str, p: NetPath) -> int:
        """已 commit 铜对索引的异网违规数（同网豁免——自身在索引不误伤）。"""
        clr = self.router.cfg.clearance
        n = 0
        for s in p.segs:
            if not self.router.index.edge_clear(
                    s.x1, s.y1, s.x2, s.y2, s.layer, net,
                    s.width / 2 + clr):
                n += 1
        vr = self.router._via_radius
        for v in p.vias:
            if not all(self.router.index.point_clear(
                    v.x, v.y, layer, net, vr)
                    for layer in self.router.layers):
                n += 1
        return n

    def _conflicted_nets(self) -> list[str]:
        """冲突网扫描（副作用：更新各网 conflicts 计数，供快照代价用）。"""
        out: list[str] = []
        for net in self.targets:
            p = self.paths[net]
            if p.err is not None or not p.copper():
                p.conflicts = 0
                if p.err is None:
                    p.err = "无铜产物"
                out.append(net)
                continue
            p.conflicts = self._conflict_count(net, p)
            if p.conflicts:
                out.append(net)
        return out

    def _hist_add(self, p: NetPath) -> None:
        """被撕冲突网的栅格位加历史惩罚（单调递增）。

        键 = (ix, iy, 层序)——与 GridRouter.route 的 hist 参数同一坐标
        系；via 位在全部信号层加罚（孔环贯穿）。
        """
        st = self.router.cfg.step
        layers = self.router.layers
        cells: set[tuple[int, int, int]] = set()
        for s in p.segs:
            li = layers.index(s.layer)
            ix1, ix2 = sorted((round(s.x1 / st), round(s.x2 / st)))
            iy1, iy2 = sorted((round(s.y1 / st), round(s.y2 / st)))
            for ix in range(ix1, ix2 + 1):
                for iy in range(iy1, iy2 + 1):
                    cells.add((ix, iy, li))
        for v in p.vias:
            ix, iy = round(v.x / st), round(v.y / st)
            for li in range(len(layers)):
                cells.add((ix, iy, li))
        for c in cells:
            self.hist[c] = self.hist.get(c, 0.0) + self.hist_penalty

    def _rip(self, net: str, penalise: bool) -> None:
        p = self.paths[net]
        if not p.copper():
            return
        if penalise:
            self._hist_add(p)
        self.router.rollback(p.segs, p.vias)
        p.segs, p.vias = [], []

    def _snapshot(self) -> dict[str, tuple[list, list, str | None, int]]:
        return {n: (list(p.segs), list(p.vias), p.err, p.conflicts)
                for n, p in self.paths.items()}

    def _cost(self, snap: dict) -> tuple[int, ...]:
        """快照代价 =（不可用网数, 冲突对象数[, 孔数, 线长×10]）字典序。

        不可用 = 未布通 **或带冲突**——短路网与飞线网同罪（制造级短路比
        飞线更糟）。只数未布的话，Phase 1 全量重叠提交（0 未布、N 冲突）
        在任何"带 1 条未布的干净快照"面前字典序粘死，协商形同虚设。

        refine 开（两轮第二轮 M1）追位 (孔, 线长×10)：完成度持平时孔少
        线短的快照胜出。秩序水位（cv/成束）不进字典序——那是成品板全量
        量尺，轮内无廉价读数，且已由井折扣/SDF 罚内化进布线成本；改由
        出口判据守。线长取欧氏（diag45 斜段真实长度），0.1mm 粒度取整。
        """
        bad = sum(1 for s in snap.values() if s[2] is not None or s[3] > 0)
        conf = sum(s[3] for s in snap.values())
        if not self.refine:
            return (bad, conf)
        vias = sum(len(s[1]) for s in snap.values())
        length10 = round(sum(
            ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5
            for v in snap.values() for s in v[0]) * 10)
        return (bad, conf, vias, length10)

    def _refine_round(self, t0: float) -> int:
        """精修一轮（两轮第二轮 M2）：撕布浪费前 refine_k 网干净重布。

        浪费代理 = 孔×2 + 线长/10（孔双权——主目标是压孔）。候选 = 已布
        通且无冲突的网（err/冲突网不碰——精修不给病网再烧预算），排除
        失败集（未严格更优的网不再重试，防高浪费位常驻前排重烧 A*）。
        接受准则（网级）：新路径零冲突且 (孔, 线长×10) 字典序**严格**更优
        才保留，否则重放旧铜——板级 (完成度, 冲突, 孔, 线长) 单调不恶化。
        返回本轮改进网数。
        """

        def _len10(segs) -> int:
            return round(sum(((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2)
                             ** 0.5 for s in segs) * 10)

        cands = [n for n in self.targets
                 if self.paths[n].err is None
                 and self.paths[n].copper()
                 and self.paths[n].conflicts == 0
                 and n not in self._refine_failed]
        cands.sort(key=lambda n: len(self.paths[n].vias) * 2
                   + _len10(self.paths[n].segs) / 10.0, reverse=True)
        improved = 0
        for net in cands[:self.refine_k]:
            if time.time() - t0 > self.budget_s:
                break
            p = self.paths[net]
            old_segs, old_vias = list(p.segs), list(p.vias)
            old_metric = (len(old_vias), _len10(old_segs))
            self._rip(net, penalise=False)
            self._route_net(net)            # 干净重布（不带 hist）
            if p.copper():
                self.router.commit(p.segs, p.vias)
                if (self._conflict_count(net, p) == 0
                        and (len(p.vias), _len10(p.segs)) < old_metric):
                    p.conflicts = 0
                    improved += 1
                    continue
            # 未更优/布不通/带冲突：重放旧铜（失败集记名，下轮不再试）
            if p.copper():
                self.router.rollback(p.segs, p.vias)
            p.segs, p.vias, p.err, p.conflicts = \
                old_segs, old_vias, None, 0
            self.router.commit(p.segs, p.vias)
            self._refine_failed.add(net)
        return improved

    def _restore(self, snap: dict) -> None:
        """现行铜全退 → 快照铜重放（对象不可变，浅拷贝列表即安全）。"""
        for net in self.targets:
            p = self.paths[net]
            if p.copper():
                self.router.rollback(p.segs, p.vias)
        for net in self.targets:
            segs, vias, err, conf = snap[net]
            p = self.paths[net]
            p.segs, p.vias, p.err, p.conflicts = \
                list(segs), list(vias), err, conf
            if p.copper():
                self.router.commit(p.segs, p.vias)

    # ---------------------------------------------------------------- 主循环
    def run(self) -> dict:
        t0 = time.time()
        self._t0 = t0        # F2 武装预算闸的墙钟零点（随 run 重置）
        order = self._order()

        # Phase 1：独立布线（互不为障——PathFinder 初相，冲突交给协商）
        for net in order:
            if time.time() - t0 > self.budget_s:
                self.paths[net].err = "预算耗尽"
                continue
            self._route_net(net)
        # Phase 2：全量 commit（确定性序——先布铜成为后布铜的障碍）；
        # 每次 commit 后采井样本——后布的网看到前缀实测节距（Q3）。
        for net in order:
            p = self.paths[net]
            if p.copper():
                self.router.commit(p.segs, p.vias)
                self._pitch_update(net)
                self._via_pitch_update(net)
                self._bundle_pitch_update(net)

        # Phase 3：协商循环——冲突网全拆全布、hist 单调增压；最优快照收尾
        conflicted = self._conflicted_nets()
        best, best_cost = self._snapshot(), None  # cost 稍后统一算
        best_cost = self._cost(best)
        iterations = 0
        iter_costs: list[list[int]] = [[*best_cost]]   # 轨迹（调参诊断用）
        stop_reason: str | None = None
        stagnation = 0
        while (conflicted and iterations < self.max_iter
               and time.time() - t0 <= self.budget_s):
            iterations += 1
            conflict_set = set(conflicted)
            for net in self.targets:            # 全拆（仅冲突网落 hist 惩罚）
                self._rip(net, penalise=net in conflict_set)
            # 全拆后铜面全变：井样本作废重采（p* 本身保留——连续性暖启）。
            self._pitch_gaps.clear()
            self._via_pitch_gaps.clear()
            # Q7：束对样本同废重采、每束 p* 暖启保留；重布自此武装兄弟
            # 井（D4 生效时机 = Phase 3——首布无兄弟参照不吸）。
            self._bundle_gaps.clear()
            self._bundle_sampled.clear()
            self._bundle_armed = True
            for net in order:
                if time.time() - t0 > self.budget_s:
                    if self.paths[net].err is None:
                        self.paths[net].err = "预算耗尽"
                    continue
                self._route_net(net, self.hist)
                p = self.paths[net]
                if p.copper():
                    self.router.commit(p.segs, p.vias)
                    self._pitch_update(net)
                    self._via_pitch_update(net)
                    self._bundle_pitch_update(net)
            if self._bundle_lay:
                # D5② 网级降级：无吸引重试后仍布不通（非预算耗尽）的
                # 束网永久撤吸引——下轮起当散网布（_diag_fb 先例）。
                for net in order:
                    q = self.paths[net]
                    if (q.err is not None and q.err != "预算耗尽"
                            and net in self._bundle_rank):
                        self._bundle_demoted.add(net)
            conflicted = self._conflicted_nets()
            snap = self._snapshot()
            c = self._cost(snap)
            iter_costs.append([*c])
            if c < best_cost:
                best, best_cost = snap, c
                stagnation = 0
            else:
                stagnation += 1
                if self.stagnation_k and stagnation >= self.stagnation_k:
                    stop_reason = "stagnation"
                    break
        if stop_reason is None:
            stop_reason = ("converged" if not conflicted
                           else "max_iter" if iterations >= self.max_iter
                           else "budget")

        # ---- 精修态（两轮第二轮 M2）：冲突态收敛/停滞后选择性 rip-up。
        # pic 类轮 1 即 converged、interf 类 /MA14 挂 err 停滞——两种收敛
        # 形态下 (bad, conf) 都无改进空间，全拆全布只是掷骰子（interf 轮 2
        # 50 网倒退实证）；精修轮只撕浪费大的网，好网铜不动。失败集 +
        # stagnation（连续 K 轮零改进）双刹车，budget/max_iter 共享。
        refine_rounds = 0
        refine_improved = 0
        if (self.refine and iterations < self.max_iter
                and time.time() - t0 <= self.budget_s):
            self._restore(best)
            stagnation = 0
            while iterations < self.max_iter and time.time() - t0 <= self.budget_s:
                iterations += 1
                refine_rounds += 1
                improved = self._refine_round(t0)
                refine_improved += improved
                conflicted = self._conflicted_nets()
                snap = self._snapshot()
                c = self._cost(snap)
                iter_costs.append([*c])
                if c < best_cost:
                    best, best_cost = snap, c
                if improved:
                    stagnation = 0
                else:
                    stagnation += 1
                    if self.stagnation_k and stagnation >= self.stagnation_k:
                        stop_reason = "refine_stagnation"
                        break
        self._restore(best)

        # 出厂净化：残余冲突网的铜整网弃除——短路是制造级缺陷，输出留
        # 飞线比留短路铜诚实（RL 侧照常打分）。删障碍不会新增违规，一轮
        # 即净；while 是 belt-and-suspenders。
        dropped: list[str] = []
        while True:
            conflicted = [n for n in self._conflicted_nets()
                          if self.paths[n].copper()]
            if not conflicted:
                break
            for net in conflicted:
                p = self.paths[net]
                p.err = f"冲突未解({p.conflicts})"
                dropped.append(net)
                self._rip(net, penalise=False)

        # V3.1 ①-A 二遍法补针（拍板 A，2026-10-08）：正常遍不开狗骨（既有
        # 格局与硬约束②「非硬网完成度不回退」由构造保住）→ 出厂净化后只
        # 对失败网逐网开狗骨+颈缩重布。此刻铜面 = 最终静态障碍：成功网铜
        # 不动、失败网铜已被净化撕净（含「冲突未解」丢弃网——它们同样获
        # 救机会）。补针成功即 commit（后续补针网绕行）；失败维持飞线
        # （_route_edges 全或无语义，失败网无铜残留，无需防御性回滚）。
        # hist 惩罚照带（可采纳，把补针挤离协商热区）；Phase 3 束上下文
        # 对补针网不适用（_bundle_armed 关）。预算：独立 rescue 预算
        # >0 时延长墙钟，=0 时共享 --budget-s 残额（耗尽即止，剩余失败
        # 网维持飞线——诚实输出）。补针铜按全层索引构造合法（A* 只走
        # edge_clear 格），conflicts 清零记账，真闸在报告尾部 self_drc。
        rescued: list[str] = []
        joint_conflicts: list[str] = []  # 联立缓存撞已 commit 铜的网
        if self.dogbone_rescue:
            failed = [n for n in self.targets if self.paths[n].err is not None]
            if failed:
                # RouteConfig 是 frozen dataclass——replace 换新配置对象
                # 只翻狗骨开关；桩宽构造期已随 cfg_kw 进 cfg。收尾后不再
                # 布线，无需换回。
                self.router.cfg = replace(self.router.cfg, dogbone=True)
                self._bundle_armed = False
            deadline = (t0 + self.budget_s + self.dogbone_rescue_budget_s
                        if self.dogbone_rescue_budget_s > 0
                        else t0 + self.budget_s)
            if self.dogbone_rescue_joint:
                # V3.1 ② 联立布两阶段（2026-10-08，REPORT_step4_failfast
                # §三：顺序臂逐网 commit 的自相残杀挤死 watchy /32K_P、
                # ACC_INT_2——纯净铜面可连 7 > 实跑救回 5）。
                # Phase 1：失败网全体对同一静态铜面盲布，互不见对方铜、
                # 不 commit（缓存 segs/vias 引用——re-route 只换属性引用
                # 不改原表，缓存安全）。Phase 2：按原序 commit，缓存路由
                # 先经 _rescue_route_clear 重验（铜只增不减，干净 ⇔ 此刻
                # 现布合法）；冲突者当场重布 = 回退顺序臂语义（同一铜面
                # 状态下与顺序臂同判），联立对每网只增不减。预算耗尽撞
                # 在 commit 前的缓存铜不入板、诚实记 err。
                joint: list[tuple[str, list[Segment], list[Via]]] = []
                for net in failed:
                    if time.time() > deadline:
                        break  # 预算残额耗尽：剩余失败网维持飞线
                    self._route_net(net, self.hist)
                    p = self.paths[net]
                    if p.err is None and p.copper():
                        joint.append((net, p.segs, p.vias))
                for net, segs, vias in joint:
                    if self._rescue_route_clear(net, segs, vias):
                        self.router.commit(segs, vias)
                        self.paths[net].conflicts = 0
                        rescued.append(net)
                    elif time.time() <= deadline:
                        joint_conflicts.append(net)
                        self._route_net(net, self.hist)  # 冲突回退：当场重布
                        p = self.paths[net]
                        if p.err is None and p.copper():
                            self.router.commit(p.segs, p.vias)
                            p.conflicts = 0
                            rescued.append(net)
                    else:
                        joint_conflicts.append(net)
                        p = self.paths[net]
                        p.segs, p.vias, p.err = [], [], \
                            "补针联立冲突且预算耗尽（缓存铜未入板）"
            else:
                for net in failed:
                    if time.time() > deadline:
                        break  # 预算残额耗尽：剩余失败网维持飞线
                    self._route_net(net, self.hist)
                    p = self.paths[net]
                    if p.err is None and p.copper():
                        self.router.commit(p.segs, p.vias)
                        p.conflicts = 0
                        rescued.append(net)

        # 报告（restore + 净化后的 self.paths 即出厂状态）
        unrouted = [n for n in self.targets if self.paths[n].err is not None]
        conf_total = sum(self.paths[n].conflicts for n in self.targets)
        segs = [s for n in self.targets for s in self.paths[n].segs]
        vias = [v for n in self.targets for v in self.paths[n].vias]
        per_net = {}
        for n in self.targets:
            p = self.paths[n]
            per_net[n] = {
                "err": p.err, "conflicts": p.conflicts,
                "segs": len(p.segs), "vias": len(p.vias),
                "connected": _pads_connected(
                    self.board,
                    [s for s in self.board.segments if s.net == n],
                    [v for v in self.board.vias if v.net == n], n),
            }
        status = ("ok" if not unrouted and conf_total == 0
                  else "partial" if len(unrouted) < len(self.targets)
                  else "failed")
        return {
            "status": status, "clearance_mm": self.router.cfg.clearance,
            "layers": list(self.router.layers),
            "dir_prior": self.dir_prior,
            "via_cost": self.router.cfg.via_cost,
            "space": self.space,
            "space_band": self.router.cfg.space_band,
            "space_cost": self.router.cfg.space_cost,
            "pitch": self.pitch,
            "pitch_star": round(self.router.pitch_star, 3),  # p* 终值
            "pitch_band": self.router.cfg.pitch_band,
            "pitch_samples": len(self._pitch_gaps),
            "via_pitch": self.via_pitch,
            # Q3' 桶键 "层|h|v" → p* 终值 / 已采样本数（每桶独立暖机）
            "via_pitch_star": {
                f"{k[0]}|{'h' if k[1] else 'v'}": round(v, 3)
                for k, v in self.router.via_pitch_star.items()},
            "via_pitch_band": self.router.cfg.via_pitch_band,
            "via_pitch_samples": {
                f"{k[0]}|{'h' if k[1] else 'v'}": len(v)
                for k, v in self._via_pitch_gaps.items()},
            "bundle_order": self.bundle_order,
            # Q7 束联铺：旗标/束数/车道网数/降级网/p* 表（键式同 via_pitch）
            "bundle_lay": self._bundle_lay,
            "bundle_bundles": len(self._bundle_members),
            "bundle_lane_nets": sum(1 for n in self._bundle_rank
                                    if len(self._pads[n]) == 2),
            "bundle_demoted": sorted(self._bundle_demoted),
            # F1/F2 读数（修复轮 3）：武装软帽（None=未启用）+ 预算闸跳过
            # 武装的网（墙钟分支，时序敏感——仅诊断用，勿作确定性对比键）。
            "bundle_arm_cap": (self.router.cfg.bundle_arm_cap or None),
            "bundle_arm_skipped": list(self._bundle_arm_skipped),
            "bundle_pstar": {
                f"{'|'.join(k[0])}|{k[1]}|{'h' if k[2] else 'v'}":
                    round(v, 3)
                for k, v in sorted(self._bundle_pitch.items())},
            "diag45": self.diag45,
            "diag_cost": self.diag_cost,
            "h_weight": self.h_weight,
            "diag_node_cap": self.diag_node_cap,
            "diag_fallback_nets": sorted(self._diag_fb),
            # 两轮第二轮（refine_pass）：旗标/轮数/改进网数/失败集
            "refine": self.refine, "refine_k": self.refine_k,
            "refine_rounds": refine_rounds,
            "refine_improved": refine_improved,
            "refine_failed_nets": sorted(self._refine_failed),
            # V3.1 ① 狗骨逃逸记账：旗标回显 / 各网注入种子数（跨轮累计，
            # "ever" 语义）/ 实际走了狗骨的网清单（试金石 A 的直接读数）
            "dogbone": self.dogbone,
            "dogbone_seeds": dict(sorted(self.router.dog_seeds.items())),
            "dogbone_used": sorted(self.router.dog_used),
            # V3.1 ①-A 补针记账：旗标/桩宽/救回网清单（试金石 A 直接读数）
            "dogbone_rescue": self.dogbone_rescue,
            "dogbone_rescue_width": self.dogbone_width,
            "dogbone_rescue_joint": self.dogbone_rescue_joint,
            "dogbone_rescue_joint_conflicts": joint_conflicts,
            "dogbone_rescued": rescued,
            # 工艺参数回显（2026-10-10）：via 尺寸 + 细间距免责净距
            "via_size": self.router.cfg.via_size,
            "via_drill": self.router.cfg.via_drill,
            "pad_clearance": self.router.cfg.pad_clearance,
            "relax_delta_mm": round(self.router.relax_stats["delta_mm"], 4),
            "relax_triggered_pads": self.router.relax_stats["triggered_pads"],
            "relax_zone_cells": self.router.relax_stats["zone_cells"],
            "nets_total": len(self.targets),
            "nets_routed": len(self.targets) - len(unrouted),
            "nets_unrouted": unrouted,
            "nets_conflicted": dropped, "iter_costs": iter_costs,
            "stop_reason": stop_reason,
            "conflicts": conf_total,
            "iterations": iterations,
            "converged": not unrouted and conf_total == 0,
            "segments": len(segs), "vias": len(vias),
            "hist_cells": len(self.hist),
            "length_mm": round(sum(abs(s.x2 - s.x1) + abs(s.y2 - s.y1)
                                   for s in segs), 1),
            "per_net": per_net,
            "self_drc": [str(v) for v in run_drc(
                self.board, clearance=self.router.cfg.clearance)],
            "wall_s": round(time.time() - t0, 1),
        }


def write_board(src: Path, out: Path,
                paths: dict[str, NetPath]) -> Path:
    """引擎铜落盘：base 板文本 + 各网 segs/vias 块（merge 同款原语）。"""
    blocks = ([_segment_block(s) for n in sorted(paths)
               for s in paths[n].segs]
              + [_via_block(v) for n in sorted(paths)
                 for v in paths[n].vias])
    out.write_text(
        _append_blocks(Path(src).read_text(encoding="utf-8"), blocks),
        encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route.engine",
        description="协商式 rip-up-reroute 引擎（PathFinder ⊕ GridRouter）")
    ap.add_argument("--board", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--clearance-um", type=int, default=230,
                    help="铜间隙 µm（RL 动作口径；默认 230）")
    ap.add_argument("--layers", default=None,
                    help="逗号分隔信号层栈（默认 F.Cu,B.Cu；N 层板显式给全）")
    ap.add_argument("--width", type=float, default=None,
                    help="线宽 mm（缺省 RouteConfig.width=0.25）")
    ap.add_argument("--via-size", type=float, default=None,
                    help="过孔盘径 mm（缺省 RouteConfig 0.6；人工板实测"
                         " 0.6548）")
    ap.add_argument("--via-drill", type=float, default=None,
                    help="过孔孔径 mm（缺省 RouteConfig 0.3；人工板实测"
                         " 0.35；须 < --via-size）")
    ap.add_argument("--pad-clearance-um", type=float, default=None,
                    help="细间距盘免责净距 µm（人工 BusPirate5 盘级 "
                         "override 实测 80）：触发盘 = 同层最近异网盘缝 < "
                         "最大线宽+2×全局净距（QFN 0.4/QFP 0.5 节距命中、"
                         "SOIC 1.27 不命中），其 --relax-radius 圆域内净距"
                         "按 全局−该值 折减；A*/锚点/出口/狗骨/过孔/协商"
                         "自检全同口径；None = 不启用（inert）")
    ap.add_argument("--relax-radius", type=float, default=1.0,
                    help="触发盘免责区半径 mm（配合 --pad-clearance-um，"
                         "默认 1.0）")
    ap.add_argument("--max-iter", type=int, default=15)
    ap.add_argument("--budget-s", type=float, default=600.0)
    ap.add_argument("--dir-prior", action="store_true",
                    help="层方向先验：偶序层守纵/奇序层守横交替，主方向"
                         "步进打折、逆方向加罚；换向最优解=过孔（缺省降价"
                         "0.4→0.2）——对 FR「孔省了线碎了」病灶")
    ap.add_argument("--par-factor", type=float, default=0.85,
                    help="主方向步进折扣（配合 --dir-prior）")
    ap.add_argument("--perp-factor", type=float, default=1.5,
                    help="逆主方向步进放大（配合 --dir-prior）")
    ap.add_argument("--via-cost", type=float, default=None,
                    help="过孔代价 mm 等效（--dir-prior 时缺省 0.2，"
                         "否则 RouteConfig 缺省 0.4）")
    ap.add_argument("--space", action="store_true",
                    help="间距软排斥：净距 < band 的步进按接近程度线性加罚"
                         "（贴 0 满罚 space_cost），≥ band 免费 → 平行列距"
                         "收敛到公共值（压 spacing_cv；索引 margin 自动扩 band）")
    ap.add_argument("--space-band", type=float, default=0.75,
                    help="理想铜缘净距带宽 mm（配合 --space；默认 0.75）")
    ap.add_argument("--space-cost", type=float, default=0.6,
                    help="贴到 0 净距时的满罚强度 mm 等效（配合 --space；"
                         "默认 0.6）")
    ap.add_argument("--pitch", action="store_true",
                    help="节距吸引井（Q3）：净距落在 p*±pitch-band 的对称"
                         "折扣谷（谷底步进 ×pitch-factor、井沿线性衰减到 0），"
                         "平行段被吸向公共节距；井底恒在 --space-band 之外，"
                         "折扣绝不奖励贴挤")
    ap.add_argument("--pitch-band", type=float, default=0.15,
                    help="井半宽 mm（净距偏离 p* 超过此值无折扣；默认 0.15）")
    ap.add_argument("--pitch-factor", type=float, default=0.85,
                    help="井底步进折扣（配合 --pitch；默认 0.85）")
    ap.add_argument("--pitch-star", type=float, default=1.0,
                    help="目标节距 p*（mm）：samples=0 恒用此值；samples>0 "
                         "暖机期井休眠、采满样本后切实测中位数。首轮实测 "
                         "2.0 远超板内真实分布（SDF band 0.75、平行净距多 "
                         "0.75–1.2）→ 绕行灾难，默认改 1.0")
    ap.add_argument("--pitch-cap", type=float, default=2.5,
                    help="p* 及采样上界 mm（井只吃到此距离内的平行信号；"
                         "默认 2.5）")
    ap.add_argument("--pitch-samples", type=int, default=50,
                    help="自适应样本阈值（0 = 固定 p* 模式；默认 50）")
    ap.add_argument("--via-pitch", action="store_true",
                    help="via 落点节距井（Q3'）：井只挂换层边——孔位到"
                         "目标层最近平行段垂距落 k·p* 邻域折扣该边 "
                         "via_cost（主吸 k=1），无邻居中性；p* 按 "
                         "(层, 方向) 桶自适应（每桶采满 samples 样本设"
                         "中位数，暖机期桶空井休眠）")
    ap.add_argument("--via-pitch-band", type=float, default=0.1,
                    help="井半宽 mm（k·p* 邻域；配合 --via-pitch，"
                         "默认 0.1）")
    ap.add_argument("--via-pitch-factor", type=float, default=0.85,
                    help="井底折扣强度（谷底减 (1-该值)×via_cost；"
                         "默认 0.85）")
    ap.add_argument("--via-pitch-star", type=float, default=0.0,
                    help="固定 p*（mm）：samples=0 时设全 4 桶恒用此值；"
                         "samples>0 暖机期桶空井休眠、采满设中位数。"
                         "默认 0 = 仅自适应")
    ap.add_argument("--via-pitch-cap", type=float, default=2.5,
                    help="垂距探索上界 mm（reach = cap+band；默认 2.5）")
    ap.add_argument("--via-pitch-samples", type=int, default=20,
                    help="每桶自适应样本阈值（0 = 固定 p* 模式；默认 20）")
    ap.add_argument("--bundle-order", action="store_true",
                    help="束序路由（Q3）：同元件对的网聚束连布，先布者定"
                         "节距模式、后布者跟齐；缺省 MST 总长升序不变")
    ap.add_argument("--diag45", action="store_true",
                    help="45° 走线族（Q5）：步进集扩 8 邻、斜步 ×√2、"
                         "octile 启发式、转向罚分级（45° 拐便宜/135° 尖拐"
                         "贵）、via-corner 罚（孔不落拐点）；缺省关 = 四向"
                         "现状，输出逐字节不变（inert 锁）")
    ap.add_argument("--turn45-factor", type=float, default=0.3,
                    help="45° 拐转向罚系数（×turn_cost；配合 --diag45，"
                         "默认 0.3）")
    ap.add_argument("--turn135-factor", type=float, default=2.0,
                    help="135° 尖拐转向罚系数（×turn_cost；配合 --diag45，"
                         "默认 2.0）")
    ap.add_argument("--via-corner-cost", type=float, default=0.3,
                    help="刚过孔即转向（非共线续接）的加罚 mm 等效"
                         "（配合 --diag45，默认 0.3）")
    ap.add_argument("--diag-cost", type=float, default=1.0,
                    help="斜步每毫米乘子（配合 --diag45；>1 压长斜跑、"
                         "短倒角仍便宜；默认 1.0 恒等）")
    ap.add_argument("--h-weight", type=float, default=1.0,
                    help="加权 A* 启发式膨胀（配合 --diag45；>1 贪心化"
                         "换搜索墙钟；默认 1.0 = 最优搜索）")
    ap.add_argument("--diag-node-cap", type=int, default=0,
                    help="Q5 单网软帽：8 向 A* 单网节点弹出数超此值，该网"
                         "整网回退四向 legacy 桌并记入回退集（后续轮不再"
                         "重试 8 向）；0 = 不设帽（默认）")
    ap.add_argument("--stagnation", type=int,
                    default=int(os.environ.get("RRL_ENGINE_STAGNATION", "0")),
                    help="协商循环连续 N 轮无改进即早停（0=关；"
                         "env RRL_ENGINE_STAGNATION 可设缺省）")
    ap.add_argument("--refine", action="store_true",
                    help="两轮第二轮：接受准则扩四元 (完成度, 冲突, 孔, "
                         "线长×10) + 冲突收敛后精修轮（选择性 rip-up）。"
                         "缺省关——二元代价、无精修轮，行为与改前逐字节"
                         "一致（inert 锁）")
    ap.add_argument("--refine-k", type=int, default=8,
                    help="精修轮每轮撕布的网数（按浪费代理 孔×2+线长/10 "
                         "降序取前 K；仅 --refine 消费）")
    ap.add_argument("--bundle-lay", action="store_true",
                    help="束联铺（Q7）：同元件对束整束联铺——车道序（D3）"
                         "+ Phase 3 兄弟井软领随（D4，步边折扣拉向 k·p* 格）"
                         "+ 每束 p* ≥2 样本一次定死（D2）。三级兜底：失败"
                         "无吸引重试 / 网级永久降级 / 墙钟不变。缺省关 = 全"
                         "inert（序/成本/输出逐字节同旧）")
    ap.add_argument("--bundle-band", type=float, default=0.3,
                    help="兄弟井半宽 mm（垂距偏离 k·p* 超此值无折扣；"
                         "配合 --bundle-lay，默认 0.3——0.2 网格上容 ±0.2）")
    ap.add_argument("--bundle-factor", type=float, default=0.5,
                    help="井底步进折扣（配合 --bundle-lay；默认 0.5，"
                         "h 同缩保可采纳）")
    ap.add_argument("--bundle-pitch-lo", type=float, default=1.0,
                    help="每束 p* 下限 mm（井底恒在 space_band 外——折扣"
                         "绝不奖励贴挤；配合 --bundle-lay，默认 1.0）")
    ap.add_argument("--bundle-arm-cap", type=int, default=150000,
                    help="F1 武装尝试独立弹出软帽（配合 --bundle-lay；撞帽"
                         "走既有降级梯=永久撤吸引+无井重布）。插桩烧钱表：21 "
                         "降级网全撞 300k 全帽白烧 Σ261s=P3 的 54 %%，armed-ok "
                         "13/14 成功在 ≤134k → 默认 150000；0 = 继承 "
                         "--diag-node-cap")
    ap.add_argument("--bundle-arm-reserve-s", type=float, default=120.0,
                    help="F2 武装预算闸：剩余墙钟低于此值不再武装新网（束网"
                         "直接普通布，防队尾饿死）。interf 队尾 25 网普通布约 "
                         "42s → 默认 120 留一倍余量；0 = 关闸。与 --budget-s "
                         "同属墙钟分支")
    ap.add_argument("--dogbone", action="store_true",
                    help="V3.1 ① 狗骨逃逸：细间距盘短桩出脚+就地过孔换层"
                         "的落点候选常开注入（Q6 21 硬块网的解药；同层可达"
                         "时 via_cost 兜底不划算，封死/拥堵时胜出）")
    ap.add_argument("--dogbone-reach", type=float, default=1.6,
                    help="狗骨落点轴向带远端（盘尖外 mm，双向含封装体内"
                         "向；配合 --dogbone，默认 1.6）")
    ap.add_argument("--dogbone-lat", type=float, default=0.8,
                    help="狗骨落点横向带上限（盘轴两侧偏移 mm，错位躲同层"
                         "拥堵；配合 --dogbone，默认 0.8）")
    ap.add_argument("--dogbone-cap", type=int, default=64,
                    help="每盘狗骨落点候选上限（按桩长序取前 N；配合 "
                         "--dogbone，默认 64；0 = 不限）")
    ap.add_argument("--dogbone-rescue", action="store_true",
                    help="V3.1 ①-A 二遍法补针（默认关）：正常遍不开狗骨"
                         "（既有格局与硬约束②由构造保住），出厂净化后只对"
                         "失败网逐网开狗骨重布，成功即收、失败维持飞线")
    ap.add_argument("--dogbone-width", type=float, default=None,
                    help="狗骨桩颈缩宽 mm（配合 --dogbone/-rescue；种子按 "
                         "W/2+净距枚举、头尾桩段按此宽物化；须 ≥ 工艺底 "
                         "0.127；默认 None = 桩与网络同宽）")
    ap.add_argument("--dogbone-rescue-budget-s", type=float, default=0.0,
                    help="补针遍独立预算秒（>0 = 在 --budget-s 之外延长"
                         "墙钟；0 = 共享 --budget-s 残额，耗尽即止——不动"
                         "正常遍口径，rescue 臂与基线可比）")
    ap.add_argument("--dogbone-rescue-joint", action="store_true",
                    help="V3.1 ② 联立布（默认关，配合 --dogbone-rescue）："
                         "失败网全体先对同一静态铜面盲布（互不见对方铜——"
                         "逐网 commit 的自相残杀实测挤死 watchy /32K_P、"
                         "ACC_INT_2），再按原序 commit；缓存路由撞已 commit "
                         "联立铜者重验失败当场重布（回退顺序臂语义）")
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    if a.dogbone_width is not None and a.dogbone_width <= 0:
        ap.error("--dogbone-width 须为正数（mm）")
    if a.via_size is not None and a.via_size <= 0:
        ap.error("--via-size 须为正数（mm）")
    if a.via_drill is not None and a.via_drill <= 0:
        ap.error("--via-drill 须为正数（mm）")
    if (a.via_size is not None and a.via_drill is not None
            and a.via_drill >= a.via_size):
        ap.error("--via-drill 须小于 --via-size")
    if (a.pad_clearance_um is not None
            and not 0.0 < a.pad_clearance_um / 1000.0
            < a.clearance_um / 1000.0):
        ap.error("--pad-clearance-um 须在 (0, --clearance-um) 开区间内")
    if a.relax_radius <= 0:
        ap.error("--relax-radius 须为正数（mm）")

    layers = tuple(a.layers.split(",")) if a.layers else ()
    board = RouteBoard(a.board)
    eng = NegotiatedRouter(
        board, clearance_mm=a.clearance_um / 1000.0, layers=layers,
        width=a.width, max_iter=a.max_iter, budget_s=a.budget_s,
        stagnation_k=a.stagnation, dir_prior=a.dir_prior,
        par_factor=a.par_factor, perp_factor=a.perp_factor,
        via_cost=a.via_cost, space=a.space, space_band=a.space_band,
        space_cost=a.space_cost, pitch=a.pitch,
        pitch_band=a.pitch_band, pitch_factor=a.pitch_factor,
        pitch_star=a.pitch_star, pitch_cap=a.pitch_cap,
        pitch_samples=a.pitch_samples, via_pitch=a.via_pitch,
        via_pitch_band=a.via_pitch_band,
        via_pitch_factor=a.via_pitch_factor,
        via_pitch_star=a.via_pitch_star, via_pitch_cap=a.via_pitch_cap,
        via_pitch_samples=a.via_pitch_samples,
        bundle_order=a.bundle_order,
        diag45=a.diag45, turn45_factor=a.turn45_factor,
        turn135_factor=a.turn135_factor,
        via_corner_cost=a.via_corner_cost,
        diag_cost=a.diag_cost, h_weight=a.h_weight,
        diag_node_cap=a.diag_node_cap,
        refine=a.refine, refine_k=a.refine_k,
        bundle_lay=a.bundle_lay, bundle_band=a.bundle_band,
        bundle_factor=a.bundle_factor, bundle_pitch_lo=a.bundle_pitch_lo,
        bundle_arm_cap=a.bundle_arm_cap,
        bundle_arm_reserve_s=a.bundle_arm_reserve_s,
        dogbone=a.dogbone, dogbone_reach=a.dogbone_reach,
        dogbone_lat=a.dogbone_lat, dogbone_cap=a.dogbone_cap,
        dogbone_rescue=a.dogbone_rescue, dogbone_width=a.dogbone_width,
        dogbone_rescue_budget_s=a.dogbone_rescue_budget_s,
        dogbone_rescue_joint=a.dogbone_rescue_joint,
        via_size=a.via_size, via_drill=a.via_drill,
        pad_clearance_mm=(a.pad_clearance_um / 1000.0
                          if a.pad_clearance_um is not None else None),
        relax_radius=a.relax_radius)
    rep = eng.run()
    write_board(a.board, a.out, eng.paths)
    rep["board_out"] = str(a.out)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(
            json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"engine: {rep['status']} nets={rep['nets_routed']}/"
          f"{rep['nets_total']} conflicts={rep['conflicts']} "
          f"iters={rep['iterations']} segs={rep['segments']} "
          f"vias={rep['vias']} {rep['wall_s']}s -> {a.out}")


if __name__ == "__main__":
    main()
