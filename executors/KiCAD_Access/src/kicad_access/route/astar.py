"""L1 栅格 A* 布线器（M3-3）：negotiate.py 原型的确定性重写。

四项加固（原型 → 本版，皆原型实测暴露的漏洞）：
1. 节点间隙 → 扫掠边间隙：原型只查落点，扫过的边可能斜穿障碍；本版每步
   扩展检查整条扫掠线段（4 向直移，角点由相邻两边的公共端点覆盖）；
2. 焊盘中心按栅取整 → 多源锚定：原型 ``round(x/STEP)`` 假定焊盘在栅格
   上，真板焊盘全在任意坐标 → 错误起终点；本版取焊盘中心半径 R 内的
   清空栅格节点作锚，短接线（stub）接入，A* 起点多源、终点多目标；
3. 3×3 邻域扫描 → 全 bbox 索引：原型固定 3 格窗对大障碍不完备；本版
   障碍按 bbox⊕margin 预登记入 1mm 格，单格（点查询）/边格（边查询）
   命中即精确——margin ≥ 最大查询半径（半线宽/半孔径 + 间隙），障碍
   ⊆ bbox 保证漏不掉；
4. 近似盘距 → 精确 SDF：圆盘 = 线心距 − r；roundrect = 收缩盒距 − r
   （凸集 ⊕ 圆盘的距离恒 = 集距 − r）；rect = 精确线段-盒距（端点入
   盒 → 负，否则四条盒边 seg_seg_dist 取 min——平行边中段接触是真实
   最小值，只取端点/角投影会高估漏判）；oval/trapezoid/custom 保守 AABB。

确定性：堆键 (f, ix, iy, layer, dir) 全序，同分走查路径唯一。无谈判、
无 rip-up：同网豁免、异网阻挡，失败即 ValueError——L1 是策略层，重排
拓扑归 l1 调用方决定。过孔代价/转向代价压制锯齿与无谓换层。
"""

from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass

from .model import Pad, RouteBoard, Segment, Via, pad_dist, seg_dist, seg_seg_dist

_LAYERS = ("F.Cu", "B.Cu")  # 默认信号层栈（2 层板；N 层板显式传入，
#                           # 如 6 层 F/In2/In3/B——平面层不参与布线）
_DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))  # 右/下/左/上（顺时针，序即确定性）
# Q5 45° 走线族（2026-10-01）：diag45 开时步进集扩 8 邻（斜向追加在尾部，
# 轴向 0-3 索引原封不动——保 inert 锁与 via-pitch 井 `di in (0,2)` 的轴判）。
_SQ2 = math.sqrt(2.0)
_DIRS8 = _DIRS + ((1, 1), (-1, 1), (-1, -1), (1, -1))  # +右下/左下/左上/右上
# 转向角表 [来向][去向] = 转弯角/45°（k∈0..4）。手工验证的硬编码——不是
# 45°·min(|i−j|, 8−|i−j|)：斜向尾部追加后该公式对「轴向↔斜向」对会算错
#（|0−4|=4 → 180°，实际 45°）。行 = 来向（顺时针序同 _DIRS8），列 = 去向。
_TURN_T = (
    (0, 2, 4, 2, 1, 3, 3, 1),
    (2, 0, 2, 4, 1, 1, 3, 3),
    (4, 2, 0, 2, 3, 1, 1, 3),
    (2, 4, 2, 0, 3, 3, 1, 1),
    (1, 1, 3, 3, 0, 2, 4, 2),
    (3, 1, 1, 3, 2, 0, 2, 4),
    (3, 3, 1, 1, 4, 2, 0, 2),
    (1, 3, 3, 1, 2, 4, 2, 0),
)
_EPS = 1e-9
_RELAX_CELL = 0.5  # mm：细间距免责区粗栅格（见 RouteConfig.pad_clearance）


@dataclass(frozen=True)
class RouteConfig:
    """栅格与工艺参数——清一色取自模板实测（见模块级设计记录）。

    clearance 0.254 是模板 zone 声明值（模板里唯一显式间隙数）；过孔
    0.6/0.3 避开参考板 200 孔全数欠规的坑（199×3 via/track/drill 违规）。
    """

    step: float = 0.2
    clearance: float = 0.254
    width: float = 0.25  # 默认线宽；spec min_width 按网覆盖（如 +24V_RAW 0.5）
    via_size: float = 0.6
    via_drill: float = 0.3
    via_cost: float = 0.4  # mm 等效：换层惩罚（≈2 栅步）
    turn_cost: float = 0.25  # mm 等效：转向惩罚，压直角锯齿
    anchor_radius: float = 0.6  # 焊盘栅格锚定半径（≈3 栅步；细间距长条
    #                           # 盘的逃逸另走 _pad_exits 轴向出口）
    # Q1 层方向先验（2026-09-30）：人工板每层守一个主方向（interf_u
    # 实测 B.Cu 横 87.1% / F.Cu 纵 82.6%），过孔=换向器；FR 反着来
    # （孔贵方向免费）→ 同层钻迷宫 bends 1.59。先验 = GridRouter 按层序
    # 交替指派主方向（偶序层 F.Cu 守纵、奇序层 B.Cu 守横），主方向步进
    # ×par_factor（<1 打折）、逆方向 ×perp_factor（>1 加罚）——换向的最优
    # 解变成「过孔去另一层」（engine 侧配 via_cost 降价）。缺省 1.0/1.0
    # = 先验 inert，L1/D1/DRC 消费者行为逐字节不变。
    par_factor: float = 1.0
    perp_factor: float = 1.0
    # Q1 进阶·间距软排斥（2026-09-30）：cv 是九格里唯一没被方向先验触动的
    # 秩序格（0.385 vs 人工 0.171）——成因是协商解「贴着挤」（净距=
    # clearance 下限）与「独行绕开」（净距 2mm+）并存。软排斥 = 平底井：
    # 净距 < space_band 的步进按接近程度线性加罚（满罚 space_cost），
    # ≥ band 免费 → 最优解贴 band 边缘走 → 平行列距收敛到公共值。
    # 缺省 0.0 = 关闭，既有消费者行为逐字节不变。band 会扩索引 margin
    #（见 GridRouter.__init__）保证 band 内查询精确。
    space_band: float = 0.0  # mm：理想铜缘净距带宽；0 = 不启用
    space_cost: float = 0.6  # mm 等效：贴到 0 净距时的满罚强度
    # Q3 节距吸引井（2026-09-30）：SDF 平底井只削分布左尾（贴挤），收不紧
    # 整体分布——人工的均一是「并排的线主动排到同一节距」。井 = 目标节距
    # p* 邻域（±pitch_band）的对称折扣谷：净距落在井内的步进按贴近程度打折
    # （谷底 ×pitch_factor）→ 平行段被吸向公共节距。p* 是板上状态（已布
    # 前缀净距的中位数，engine 在网间更新，见 GridRouter.pitch_star），
    # config 只带静态形状。井底恒落 space_band 之外（route() 内 band 内只
    # 罚不折——折扣绝不奖励贴挤）。缺省 0.0/1.0 = 井 inert，既有消费者
    # 行为逐字节不变。
    pitch_band: float = 0.0    # mm：p* 邻域半宽；0 = 不启用
    pitch_factor: float = 1.0  # 谷底折扣（步进乘子，< 1）
    pitch_cap: float = 0.0     # mm：p* 上界（仅用于索引 margin 预膨胀）
    # Q3' via 落点节距井（2026-10-01）：间距 cv 的病灶不是孔多而是孔落点无
    # 节距意识（人工 both_via 类内 cv 0.065 vs 引擎 0.344；Q3-C 证明井挂
    # 步进边会砸完成度）。井只挂换层边：候选孔位到目标层最近平行段的垂距
    # 落在 k·p* 邻域（±via_pitch_band）→ 折扣该边的 via_cost（谷底满折
    # (1-via_pitch_factor)·via_cost）；无邻居（新开行）→ 0 中性。p* 按层
    # ×方向桶暖机（GridRouter.via_pitch_star，engine 网间更新）。参照不
    # 分网——quality spacing_cv 口径同源（同网并排段也是节距参照）。
    # 缺省 band 0.0 = 井 inert，既有消费者行为逐字节不变。
    via_pitch_band: float = 0.0    # mm：k·p* 邻域半宽；0 = 不启用
    via_pitch_factor: float = 0.85  # 谷底折扣强度（1-该值×via_cost 为满折）
    via_pitch_cap: float = 2.5      # mm：垂距探索上界（reach = cap+band）
    # Q5 45° 走线族（2026-10-01）：diag45 = 斜向步进开关（_DIRS8；斜步
    # step·√2、方向先验中性）；turn45/turn135_factor = 分级转向罚乘子
    #（k=1 45° 拐 / k=3-4 135°·180° 折返，相对 k=2 90° 拐的 turn_cost）；
    # via_corner_cost = 刚过孔后首步转向的附加罚（「弯跨孔上」收、共线
    # 直穿孔免——实现「孔不落拐点」）。缺省关 + 0.3/2.0/0.3 = 全 inert，
    # 既有消费者（L1/D1/协商层）行为逐字节不变。
    diag45: bool = False
    turn45_factor: float = 0.3
    turn135_factor: float = 2.0
    via_corner_cost: float = 0.3
    # Q5 调档（2026-10-01 首扫回修）：diag_cost_factor = 斜步每毫米乘子
    # （>1 压长斜跑、短倒角仍便宜——人工模式是「轴向长跑 + 拐角斜切」，
    # 不是满板斜线；首扫斜线 35.5% vs 人工 24%，层内两族混居把方向纪律
    # 打到 60）。h_weight = 加权 A* 启发式膨胀（>1 贪心化换搜索墙钟——
    # 8 邻大板首扫 Phase1 烧穿 600s 预算的修法）。两者仅 diag45 分支消费；
    # 缺省 1.0/1.0 = 乘 1.0 IEEE 精确恒等，inert 锁不受扰。
    diag_cost_factor: float = 1.0
    h_weight: float = 1.0
    # Q5 单网软帽（2026-10-01 二轮回修）：8 向 A* 节点弹出数超此值抛
    # DiagNodeCapExceeded，engine 捕获后该网整网回退四向 legacy 桌——
    # 保完成度（interf_u 硬总线网 8 向单网可烧数百秒，预算只在网间
    # 检查拦不住），斜线按网取舍。0 = 不设帽。仅 diag45 分支消费；
    # 计数门 self._diag，off 路径零成本。
    diag_node_cap: int = 0
    # Q7 束联铺·兄弟井（2026-10-06）：束内网联合铺设的软领随——轴平行
    # 候选步与最近束内兄弟已布段的垂距落在 k·p*（p* = 每束 (层,向) 桶
    # 节距，engine 网间采样 ≥2 样本一次定死）±bundle_band 内 → 步进折扣
    # （谷底 ×bundle_factor，井沿线性衰减）。与 Q3 SDF 节距井互斥（井折
    # 不叠加，见 route() well_cut）；折扣 < 步进分量、h 同缩保可采纳。
    # 缺省 0.0/1.0 = 井 inert，既有消费者行为逐字节不变。
    bundle_band: float = 0.0    # mm：k·p* 邻域半宽；0 = 不启用
    bundle_factor: float = 1.0  # 谷底折扣（步进乘子，< 1）
    # F1 武装独立软帽（Q7 修复轮 3，2026-10-07）：束井武装的 8 向尝试
    # 弹出数超此值即抛 DiagNodeCapExceeded（engine 既有降级梯接管——
    # 永久撤吸引 + 无井重布）。插桩烧钱表依据：21 降级网全撞 300k 全帽
    # 白烧 Σ261s（P3 实际工作的 54%），armed-ok 14 网里 13 网成功在
    # ≤134k pops——独立小帽半途止损，150k 只误伤 MA12（253k 成功）。
    # 0 = 不启用（继承 diag_node_cap）；无 ctx 路径零成本 inert。
    bundle_arm_cap: int = 0
    # V3.1 ① 狗骨逃逸（2026-10-07）：细间距盘短桩出脚 + 就地过孔换层的
    # 落点候选种子（route() 两端对称注入）。落点 = 盘长轴带 [tip+0.4,
    # tip+reach] 双向（含封装体内向）× 横偏 |off|≤lat 的栅格节点，逐点过
    # 轴向桩/横向段 edge_clear + _via_clear（全层）。常开种子——via_cost
    # 兜底，同层可达时狗骨天然不划算；cap 按 (桩长,轴向距,|横|,横向符号,
    # 层,nix,niy) 排序取前 N 保确定性。缺省 False = 全 inert，既有消费
    # 者行为逐字节不变。
    dogbone: bool = False
    dogbone_reach: float = 1.6  # mm：盘尖端外轴向带远端
    dogbone_lat: float = 0.8    # mm：盘轴两侧横偏带上限
    dogbone_cap: int = 64       # 每盘候选上限；0 = 不限
    # V3.1 ①-A 颈缩桩宽（2026-10-08）：None = 桩与网络同宽（Step 1 语义，
    # 输出逐字节不变）；设值（如 0.14）→ 狗骨种子按 W/2+净距 为半宽枚举
    # （窄桩才挤得进 0.4mm 节距盘间），物化时头/尾桩段按此宽生成、路径
    # 中段维持网络宽。须 ≥ 工艺底 FINE.width 0.127 且不出索引 margin
    # （route() 校验；CLI 层同校验）。
    dogbone_width: float | None = None
    # 细间距盘免责净距（2026-10-10，用户解冻令「设置 PCB 走线参数达人工
    # 水平」）：人工板实测 = 全局净距之外对细间距焊盘逐处 override
    # （BusPirate5 盘级 0.08mm ×16 处，全局走 KiCad 默认 0.2mm）。None =
    # 不启用（inert，行为逐字节不变）；设值 → 索引自动检测触发盘（同层
    # 最近异网盘 bbox 边缘缝 < 最大线宽 + 2×全局净距 = 全局净距几何上
    # 无解的盘：QFN 0.4/QFP 0.5 节距命中、SOIC 1.27 不命中），触发盘
    # relax_radius 圆域内的净距查询按 clearance − pad_clearance 折减。
    # A* 扫掠/锚点/轴向出口/狗骨/过孔/engine 自检全收口 ObstacleIndex
    # 两个入口，一处折减全链路同口径。
    pad_clearance: float | None = None
    relax_radius: float = 1.0  # mm：触发盘周围免责区半径


# ---------------------------------------------------------------- 精确距离

class DiagNodeCapExceeded(Exception):
    """Q5：单网 8 向 A* 扩展数超 diag_node_cap（engine 捕获后该网回退
    四向）。故意不继承 ValueError——route() 的调用方把 ValueError 当
    「布不通」吞掉，此异常必须穿透到 engine._route_net 的回退分支。"""


class _BundleCtx:
    """Q7 束联铺兄弟井上下文（engine._bundle_ctx 装配，route() 消费）。

    tbl：    (层, 水平) → [(垂距坐标, 沿轴 lo, 沿轴 hi)]——束内已提交
             兄弟的轴平行段表（桶内排序，精确几何局部表，束外铜不进表，
             不走 ObstacleIndex）；
    coords： tbl 同键的垂距坐标列（升序）——_sibling_gap 二分定位用；
    pstar：  (层, 水平) → 该束该桶节距 p*（engine 采样定死，键已去束名）。
    """

    __slots__ = ("tbl", "coords", "pstar")

    def __init__(self) -> None:
        self.tbl: dict[tuple[str, bool],
                       list[tuple[float, float, float]]] = {}
        self.coords: dict[tuple[str, bool], list[float]] = {}
        self.pstar: dict[tuple[str, bool], float] = {}


def _seg_box_dist(x1: float, y1: float, x2: float, y2: float,
                  bx1: float, by1: float, bx2: float, by2: float) -> float:
    """线段到轴对齐盒的带号距离（端点在盒内 → 负；否则 ≥ 0 精确）。

    盒外的最小值必在「端点↔盒边」或「盒边中段↔盒边」实现——四条盒边
    的 seg_seg_dist 已含端点投影与规范相交，取 min 即精确。
    """
    for px, py in ((x1, y1), (x2, y2)):
        if bx1 < px < bx2 and by1 < py < by2:
            return -min(px - bx1, bx2 - px, py - by1, by2 - py)
    seg = Segment(x1, y1, x2, y2, 0.0, "", "")
    edges = (
        Segment(bx1, by1, bx2, by1, 0.0, "", ""),
        Segment(bx2, by1, bx2, by2, 0.0, "", ""),
        Segment(bx2, by2, bx1, by2, 0.0, "", ""),
        Segment(bx1, by2, bx1, by1, 0.0, "", ""),
    )
    return min(seg_seg_dist(seg, e) for e in edges)


def _pad_box(p: Pad) -> tuple[float, float, float, float]:
    return p.x - p.w / 2, p.y - p.h / 2, p.x + p.w / 2, p.y + p.h / 2


def _relax_zone(board: RouteBoard, thr: float,
                radius: float) -> tuple[frozenset[tuple[int, int]], int]:
    """细间距触发盘检测 + 免责区栅格（RouteConfig.pad_clearance 装配用）。

    thr = 最大线宽 + 2×全局净距：同层最近异网盘 bbox 边缘缝 < thr ⇒ 该盘
    用全局净距几何上无解（0.4mm 节距 QFN：缝 0.2 < 0.603 命中；1.27mm
    SOIC：缝 0.67 > 0.603 不命中——判据自洽，只有"必须松才能活"的盘被
    松）。返回 (免责区 0.5mm 粗栅格 cell 集, 触发盘数)。异层盘不冲突、
    同网盘（含 EP 阵列）豁免。
    """
    if not board.pads:
        return frozenset(), 0
    cell = _RELAX_CELL
    # 盘按 bbox 覆盖的全部格入桶（不是只按中心格）：狭长盘（金手指/长条）
    # 边缘缝 < thr 时中心可相距甚远，中心桶 + 定长邻域会漏触发。
    by_cell: dict[tuple[int, int], list[Pad]] = {}
    for p in board.pads:
        bx1, by1, bx2, by2 = _pad_box(p)
        for cx in range(int(math.floor(bx1 / cell)),
                        int(math.floor(bx2 / cell)) + 1):
            for cy in range(int(math.floor(by1 / cell)),
                            int(math.floor(by2 / cell)) + 1):
                by_cell.setdefault((cx, cy), []).append(p)
    out: set[tuple[int, int]] = set()
    n_hit = 0
    for p in board.pads:  # 逐盘（多格登记的去重由外层保证）
        near: list[Pad] = []
        bx1, by1, bx2, by2 = _pad_box(p)
        for cx in range(int(math.floor((bx1 - thr) / cell)),
                        int(math.floor((bx2 + thr) / cell)) + 1):
            for cy in range(int(math.floor((by1 - thr) / cell)),
                            int(math.floor((by2 + thr) / cell)) + 1):
                near.extend(by_cell.get((cx, cy), ()))
        hit = False
        for q in near:
            if q is p or q.net == p.net:
                continue
            if not (set(p.layers) & set(q.layers)):
                continue  # 异层铜不冲突
            qx1, qy1, qx2, qy2 = _pad_box(q)
            gx = max(qx1 - bx2, bx1 - qx2, 0.0)
            gy = max(qy1 - by2, by1 - qy2, 0.0)
            if math.hypot(gx, gy) < thr:
                hit = True
                break
        if hit:
            n_hit += 1
            for cx in range(int(math.floor((bx1 - radius) / cell)),
                            int(math.floor((bx2 + radius) / cell)) + 1):
                for cy in range(int(math.floor((by1 - radius) / cell)),
                                int(math.floor((by2 + radius) / cell)) + 1):
                    out.add((cx, cy))
    return frozenset(out), n_hit


def _seg_pad_dist(x1: float, y1: float, x2: float, y2: float, p: Pad) -> float:
    """线段到焊盘铜的带号距离——按形状精确（oval/custom 保守 AABB）。"""
    if p.shape == "circle":
        return seg_dist(p.x, p.y, Segment(x1, y1, x2, y2, 0.0, "", "")) - p.w / 2
    if p.radius > _EPS:  # roundrect = 收缩盒 ⊕ 圆盘
        r = p.radius
        bx1 = p.x - (p.w / 2 - r)
        by1 = p.y - (p.h / 2 - r)
        return _seg_box_dist(x1, y1, x2, y2,
                             bx1, by1, bx1 + (p.w - 2 * r), by1 + (p.h - 2 * r)) - r
    bx1, by1, bx2, by2 = _pad_box(p)  # rect 精确 / oval·trapezoid·custom 保守
    return _seg_box_dist(x1, y1, x2, y2, bx1, by1, bx2, by2)


def _bbox_of(obs: Pad | Segment | Via) -> tuple[float, float, float, float]:
    if isinstance(obs, Segment):
        r = obs.width / 2  # 铜比端点连线宽出半线宽（Via/Pad 分支铜精确同理）
        return (min(obs.x1, obs.x2) - r, min(obs.y1, obs.y2) - r,
                max(obs.x1, obs.x2) + r, max(obs.y1, obs.y2) + r)
    if isinstance(obs, Via):
        r = obs.size / 2
        return obs.x - r, obs.y - r, obs.x + r, obs.y + r
    return _pad_box(obs)


def _point_obs_dist(x: float, y: float, obs: Pad | Segment | Via) -> float:
    """点到障碍铜缘的带号距离。"""
    if isinstance(obs, Pad):
        return pad_dist(x, y, obs)
    if isinstance(obs, Segment):
        return seg_dist(x, y, obs) - obs.width / 2
    return math.hypot(x - obs.x, y - obs.y) - obs.size / 2


# ---------------------------------------------------------------- 障碍索引

class ObstacleIndex:
    """1mm 格 bbox 索引：障碍按 bbox⊕margin 登记入格，查询命中即精确。

    精确性条件：margin ≥ 全部查询半径的最大值（登记侧预膨胀，查询侧
    无需再扩）。障碍 ⊆ 自身 bbox，故「查询点距障碍 < R」⟹「距 bbox <
    R」⟹ 查询点所在格与膨胀 bbox 相交 ⟹ 障碍已在该格登记。
    """

    CELL = 1.0  # mm

    def __init__(self, board: RouteBoard, margin: float,
                 layers: tuple[str, ...] = _LAYERS, *,
                 relax_cells: frozenset[tuple[int, int]] = frozenset(),
                 relax_delta: float = 0.0):
        """layers = 信号层全集（通孔铜环存在的层序）——平面层（纯 zone）
        从不查询，不入层表。relax_cells/relax_delta = 细间距盘免责区
        （GridRouter 按 RouteConfig.pad_clearance 装配；空集/0 = inert，
        既有查询行为逐字节不变）。"""
        self._margin = margin
        self._layers = tuple(layers)
        self._relax_cells = relax_cells
        self._relax_delta = relax_delta
        self._cells: dict[tuple[str, int, int], list[Pad | Segment | Via]] = {}
        for p in board.pads:
            # 通孔盘铜环贯穿全部信号层（is_th = layers 多于一面）
            self._insert(p, self._layers if len(p.layers) > 1 else p.layers)
        for s in board.segments:
            self._insert(s, (s.layer,))
        for v in board.vias:
            self._insert(v, self._layers)  # 通孔贯穿全层

    def _insert(self, obs: Pad | Segment | Via, layers: tuple[str, ...]) -> None:
        x1, y1, x2, y2 = _bbox_of(obs)
        m = self._margin
        for cx, cy in self._cells_over(x1 - m, y1 - m, x2 + m, y2 + m):
            for layer in layers:
                self._cells.setdefault((layer, cx, cy), []).append(obs)

    def _cells_over(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> list[tuple[int, int]]:
        c = self.CELL
        return [(cx, cy)
                for cx in range(int(math.floor(x1 / c)), int(math.floor(x2 / c)) + 1)
                for cy in range(int(math.floor(y1 / c)), int(math.floor(y2 / c)) + 1)]

    def _at_point(self, layer: str, x: float, y: float) -> list[Pad | Segment | Via]:
        c = self.CELL
        return self._cells.get(
            (layer, int(math.floor(x / c)), int(math.floor(y / c))), [])

    def _along_edge(self, layer: str, x1: float, y1: float,
                    x2: float, y2: float) -> list[Pad | Segment | Via]:
        seen: dict[int, Pad | Segment | Via] = {}
        for cx, cy in self._cells_over(min(x1, x2), min(y1, y2),
                                       max(x1, x2), max(y1, y2)):
            for obs in self._cells.get((layer, cx, cy), ()):
                seen[id(obs)] = obs
        return list(seen.values())

    # ---------------------------------------------------------------- 查询

    def point_clear(self, x: float, y: float, layer: str, net: str,
                    radius: float) -> bool:
        """点（锚点/过孔圆心在某层投影）以 radius 的铜盘不碰异网障碍。"""
        if self._relax_cells:  # 细间距免责区内净距折减（pad_clearance）
            c = _RELAX_CELL
            if (int(math.floor(x / c)), int(math.floor(y / c))) in self._relax_cells:
                radius -= self._relax_delta
        for obs in self._at_point(layer, x, y):
            if obs.net == net:
                continue  # 同网豁免
            if _point_obs_dist(x, y, obs) < radius - _EPS:
                return False
        return True

    def edge_clear(self, x1: float, y1: float, x2: float, y2: float,
                   layer: str, net: str, radius: float) -> bool:
        """扫掠线段（含端点）以 radius 的铜带不碰异网障碍。"""
        # 与原实现的判据逐字节同义：拦 iff 净距 < -_EPS（[-_EPS,0) 的
        # 浮点贴邻放行），inf（无障碍）自然为清。
        return self._edge_scan(x1, y1, x2, y2, layer, net, radius) >= -_EPS

    def edge_gap(self, x1: float, y1: float, x2: float, y2: float,
                 layer: str, net: str, radius: float) -> float | None:
        """扫掠铜带（radius）到最近异网障碍铜缘的净距；邻域无障碍 → None。

        间距软排斥的 SDF 查询：调用方保证该边已过 edge_clear（净距 ≥ 0），
        本方法回报「离最近的别人还有多远」。精确性条件同 edge_clear：
        查询半径 radius + band ≤ 索引 margin（GridRouter 构造时已按
        space_band 扩 margin）。
        """
        d = self._edge_scan(x1, y1, x2, y2, layer, net, radius)
        return None if d == math.inf else d

    def _edge_scan(self, x1: float, y1: float, x2: float, y2: float,
                   layer: str, net: str, radius: float) -> float:
        """edge_clear/edge_gap 共用：净距带号值（<0 = 侵入非法，None 语义
        由调用方包装——无障碍时返回 +inf）。"""
        best = math.inf
        if self._relax_cells:  # 中点采样：常规边长 ≤ 栅步/对角（≤0.29mm）、
            # 狗骨桩 ≤1.6mm，对 relax_radius ≥1.0 的区边界误差可忽略
            c = _RELAX_CELL
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if (int(math.floor(mx / c)), int(math.floor(my / c))) in self._relax_cells:
                radius -= self._relax_delta
        for obs in self._along_edge(layer, x1, y1, x2, y2):
            if obs.net == net:
                continue  # 同网豁免
            if isinstance(obs, Pad):
                d = _seg_pad_dist(x1, y1, x2, y2, obs)
            elif isinstance(obs, Segment):
                d = seg_seg_dist(
                    Segment(x1, y1, x2, y2, 0.0, "", ""),
                    Segment(obs.x1, obs.y1, obs.x2, obs.y2, 0.0, "", ""),
                ) - obs.width / 2
            else:
                d = seg_dist(obs.x, obs.y,
                             Segment(x1, y1, x2, y2, 0.0, "", "")) - obs.size / 2
            d -= radius
            if d < best:
                best = d
                if best < -_EPS:  # 非法短路（edge_clear 语义）
                    break
        return best

    def add(self, segs: list[Segment], vias: list[Via]) -> None:
        """已提交铜入索引（后续网络把它们当障碍）。"""
        for s in segs:
            self._insert(s, (s.layer,))
        for v in vias:
            self._insert(v, self._layers)

    def remove(self, segs: list[Segment], vias: list[Via]) -> None:
        """add 的对称回滚（按对象身份，值等价铜不受误伤）。

        协商布序用：先布网堵死后布网时，撕回先布铜换序重布
        （diff.py 对内双序试布即此消费者）。
        """
        entries = [*((s, (s.layer,)) for s in segs),
                   *((v, self._layers) for v in vias)]
        for obs, lays in entries:
            x1, y1, x2, y2 = _bbox_of(obs)
            m = self._margin
            for cx, cy in self._cells_over(x1 - m, y1 - m, x2 + m, y2 + m):
                for layer in lays:
                    cell = self._cells.get((layer, cx, cy))
                    if not cell:
                        continue
                    for i, o in enumerate(cell):
                        if o is obs:
                            del cell[i]
                            break


# ---------------------------------------------------------------- 栅格 A*

def _simplify(pts: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """同层共线段去中间点（层切换点/拐点保留）。"""
    out: list[tuple[float, float, str]] = []
    for p in pts:
        if len(out) >= 2:
            (ax, ay, al), (bx, by, bl) = out[-2], out[-1]
            if al == bl == p[2]:
                cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
                if abs(cross) < _EPS and (bx - ax) * (p[0] - bx) >= -_EPS \
                        and (by - ay) * (p[1] - by) >= -_EPS:
                    out[-1] = p
                    continue
        out.append(p)
    return out


class GridRouter:
    """N 层栅格 A*：route() 单网两点，顺序 commit（无 rip-up）。

    用法：router = GridRouter(board, cfg, widths, layers)；layers 为有序
    信号层栈（默认 F/B 两层；6 层板传 ("F.Cu","In2.Cu","In3.Cu","B.Cu")
    之类——平面层不进栈，通孔环在全部信号层都有铜）。widths 必须申报
    本区全部要布网络的线宽（spec min_width）——索引 margin 按最大半径
    预膨胀，未申报的宽线会漏障，route() 里有守卫拦下。每条拓扑边
    ``segs, vias = router.route(net, pad_a, pad_b)`` 后 ``commit()``。
    同网铜豁免（连通优先），异网铜阻挡；失败 ValueError 点名网络/焊盘。
    """

    def __init__(self, board: RouteBoard, config: RouteConfig,
                 widths: dict[str, float] | None = None,
                 layers: tuple[str, ...] = ()):
        self.board = board
        board.inherit_nets()  # EP 阵列继承网络（幂等：已继承则 0）
        self.cfg = config
        self.widths = dict(widths or {})
        self.layers = tuple(layers) or _LAYERS
        self._max_radius = max(
            config.via_size / 2, config.width / 2,
            *(w / 2 for w in self.widths.values()),
        ) + config.clearance
        self._via_radius = config.via_size / 2 + config.clearance
        if self._via_radius > self._max_radius + _EPS:
            raise ValueError("过孔半径超出索引 margin（配置自相矛盾）")
        o = board.outline
        inset = config.clearance + config.width / 2  # 板缘内缩
        st = config.step
        self._ix_lo = int(math.ceil((o[0] + inset) / st))
        self._ix_hi = int(math.floor((o[2] - inset) / st))
        self._iy_lo = int(math.ceil((o[1] + inset) / st))
        self._iy_hi = int(math.floor((o[3] - inset) / st))
        if self._ix_lo > self._ix_hi or self._iy_lo > self._iy_hi:
            raise ValueError(f"板框 {o} 内缩后无栅格可用")
        # 间距软排斥（Q1 进阶）：索引 margin 再扩 space_band——edge_gap 的
        # 查询距离 = half_w + band（要看 band 内最近的异网铜缘），精确性
        # 条件 R ≤ margin 由此满足。band=0 时 margin 数值不变（+0.0 恒等），
        # 既有消费者行为逐字节不变。
        self._space_band = config.space_band
        # 节距吸引井（Q3）：井激活时 edge_gap 要看到 p*+pitch_band 远的异网
        # 铜缘（p* ≤ pitch_cap），margin 再扩 cap+band 保精确；井 inert
        # （band=0）时 +0.0 数值恒等，既有消费者行为逐字节不变。
        self._pitch_band = config.pitch_band
        self.pitch_star = 0.0  # 目标节距 p*（engine 网间更新；0 = 井休眠）
        # Q3' via 落点井：每 (layer, horiz) 桶的目标节距 p*（engine 网间
        # 暖机写入；空 dict = 井休眠）。探针精确性无需扩 margin——沿垂距
        # reach 的邻段必与探针 bbox 相交（look 0.5 < margin ≥ 0.554 恒真，
        # 见 _via_landing_discount）。
        self.via_pitch_star: dict[tuple[str, bool], float] = {}
        # Q7 束联铺兄弟井（2026-10-06）：ctx 由 engine 在 Phase 3 重布束
        # 网前装配（束内已提交兄弟段表 + 每束 p*），None = 井休眠（inert，
        # 代价路径逐字节同旧）。探针走局部段表二分精确几何，不经
        # ObstacleIndex，margin 无需扩。
        self.bundle_ctx: _BundleCtx | None = None
        self._bundle_reach = 2.5 + config.bundle_band
        # V3.1 ① 狗骨记账（2026-10-07）：seeds = 各网累计注入的落点候选数
        # （跨轮累计，"ever" 语义）；used = 至少有一条路径走了狗骨哨兵的网
        # （回溯侧登记）。report 读数即试金石 A 的直接记账；旗标关时
        # _dogbone_seeds 不被调用，两容器恒空（inert）。
        self.dog_seeds: dict[str, int] = {}
        self.dog_used: set[str] = set()
        # 细间距盘免责净距（2026-10-10）：触发盘检测 + 免责区装配（见
        # RouteConfig.pad_clearance）。pad_clearance=None → 空集/0 快路径，
        # 行为逐字节不变。margin 无需扩——折减只缩小查询半径，索引登记
        # 侧的过扫描仍然覆盖。
        relax_cells: frozenset[tuple[int, int]] = frozenset()
        relax_delta = 0.0
        n_triggered = 0
        if config.pad_clearance is not None:
            if not 0.0 < config.pad_clearance < config.clearance:
                raise ValueError(
                    f"pad_clearance={config.pad_clearance} 须在 (0, "
                    f"clearance={config.clearance}) 开区间内")
            if config.relax_radius <= 0.0:
                raise ValueError(
                    f"relax_radius={config.relax_radius} 须 > 0")
            w_max = max([config.width, *self.widths.values()])
            relax_cells, n_triggered = _relax_zone(
                board, w_max + 2.0 * config.clearance, config.relax_radius)
            relax_delta = config.clearance - config.pad_clearance
        # report 读数（engine 侧透传）：触发盘数/免责区格数/折减量
        self.relax_stats: dict[str, float | int] = {
            "triggered_pads": n_triggered,
            "zone_cells": len(relax_cells),
            "delta_mm": relax_delta,
        }
        self.index = ObstacleIndex(
            board,
            self._max_radius + config.space_band
            + (config.pitch_cap + config.pitch_band
               if config.pitch_band > 0.0 else 0.0),
            self.layers,
            relax_cells=relax_cells, relax_delta=relax_delta)
        # 层方向先验（Q1）：_pref_x[i] = 第 i 层主方向是否为横向（±x）。
        # 偶序层（F.Cu）守纵、奇序层（B.Cu）守横，多层交替。_step[层][方向]
        # = 步进代价（先验 inert 时恒 = step，浮点逐字节同值）；_h_scale =
        # 最小步进/mm 折扣——启发式乘之保可采纳（打折步进存在时曼哈顿
        # 原值会高估）。perp_factor ≥ 1 时 _h_scale = min(par, 1.0)。
        self._pref_x = tuple(i % 2 == 1 for i in range(len(self.layers)))
        c = config
        # Q5：diag45 开时步进集 = _DIRS8（斜步 step·√2，方向先验中性——
        # 斜向不是任何层的主/逆方向）；关时 = _DIRS，步价表达式原样（数值
        # 逐字节同旧代码）。转向罚分级：开 = 45° 拐便宜（×turn45_factor）、
        # 90° = turn_cost、135°/180° 贵（×turn135_factor）；关 = 全转向
        # 同价（k∈{0,2,4} 映 0/tc/tc，与旧 `ndi != di` 判据数值同义）。
        self._diag = c.diag45
        # Q5 网级累计弹出计数器：每次 heappop +1，engine 跨 MST 边读差值
        # 做"整网累计帽"（单边帽只约束一条边，VCC 类多边大网每边贴着帽
        # 烧、累计可达数百万弹出而帽永不触发——网级帽堵这个洞）。
        # 纯计数无人写在无 diag45 路径零影响（inert）。
        self.pops = 0
        self._dirs = _DIRS8 if c.diag45 else _DIRS
        tc = c.turn_cost
        self._turn_cost = ((0.0, tc * c.turn45_factor, tc,
                            tc * c.turn135_factor, tc) if c.diag45
                           else (0.0, tc, tc, tc, tc))
        if c.diag45:
            self._step = tuple(
                tuple(c.step * _SQ2 * c.diag_cost_factor if ndi >= 4
                      else c.step * (c.par_factor
                                     if (dx != 0) == self._pref_x[i]
                                     else c.perp_factor)
                      for ndi, (dx, _dy) in enumerate(_DIRS8))
                for i in range(len(self.layers)))
            # Q5 单网回退（diag_mode）：legacy 四向桌与 8 向桌同时常备，
            # 数值表达式与下方 diag45=关分支逐字节同源——回退网拿到的是
            # 与「从没开过 diag45」完全相同的布线。
            self._dirs8: tuple = _DIRS8
            self._step8 = self._step
            self._turn8 = self._turn_cost
            self._dirs4 = _DIRS
            self._step4 = tuple(
                tuple(c.step * (c.par_factor if (dx != 0) == self._pref_x[i]
                                else c.perp_factor)
                      for dx, _dy in _DIRS)
                for i in range(len(self.layers)))
            self._turn4 = (0.0, tc, tc, tc, tc)
        else:
            self._step = tuple(
                tuple(c.step * (c.par_factor if (dx != 0) == self._pref_x[i]
                                else c.perp_factor)
                      for dx, _dy in _DIRS)
                for i in range(len(self.layers)))
        self._h_scale = min(c.par_factor, c.perp_factor, 1.0)
        # 井折扣下最小步进 = step×方向因子×pitch_factor，h 再乘折扣才可采纳
        # （井 inert 时 pitch_factor 缺省 1.0，乘积数值不变）。
        if c.pitch_band > 0.0:
            self._h_scale *= c.pitch_factor

    def diag_mode(self, on: bool) -> None:
        """Q5 单网回退开关（engine._route_net 消费）：True = 8 向斜线桌，
        False = 四向 legacy 桌。运行期所有 diag 分支（_dirs/_step/
        _turn_cost/_h/via-corner 状态位）都挂 self._diag 旗，换桌即全切。
        仅 diag45=真构造的路由器可用（双桌在 __init__ 常备）。"""
        if not self.cfg.diag45:
            raise RuntimeError("diag_mode() 仅 diag45 路由器可用")
        self._diag = on
        self._dirs = self._dirs8 if on else self._dirs4
        self._step = self._step8 if on else self._step4
        self._turn_cost = self._turn8 if on else self._turn4

    def width(self, net: str) -> float:
        """网线宽：spec min_width 覆盖 ≥ 默认。"""
        return max(self.widths.get(net, 0.0), self.cfg.width)

    # ---------------------------------------------------------------- 锚定

    def _anchors(self, pad: Pad, net: str,
                 half_w: float) -> dict[str, list[tuple[int, int, float]]]:
        """焊盘中心半径 R 内的清空栅格节点（stub 长 = 中心到节点距）。

        返回 {layer: [(ix, iy, stub_len), …]}（stub 升序）；锚点与短接线
        都已过间隙检查。焊盘不在栅格上也能锚定（加固点 2）。
        """
        st, r_anchor = self.cfg.step, self.cfg.anchor_radius
        out: dict[str, list[tuple[int, int, float]]] = {}
        for layer in pad.layers:
            if layer not in self.layers:
                continue
            lst: list[tuple[int, int, float]] = []
            ix_lo = int(math.floor((pad.x - r_anchor) / st))
            ix_hi = int(math.floor((pad.x + r_anchor) / st))
            iy_lo = int(math.floor((pad.y - r_anchor) / st))
            iy_hi = int(math.floor((pad.y + r_anchor) / st))
            for ix in range(ix_lo, ix_hi + 1):
                for iy in range(iy_lo, iy_hi + 1):
                    if not (self._ix_lo <= ix <= self._ix_hi
                            and self._iy_lo <= iy <= self._iy_hi):
                        continue
                    x, y = ix * st, iy * st
                    stub = math.hypot(x - pad.x, y - pad.y)
                    if stub > r_anchor + _EPS:
                        continue
                    if not self.index.point_clear(x, y, layer, net, half_w):
                        continue
                    if stub > _EPS and not self.index.edge_clear(
                            pad.x, pad.y, x, y, layer, net, half_w):
                        continue  # 短接线扫过的铜带不清
                    lst.append((ix, iy, stub))
            lst.sort(key=lambda t: (t[2], t[0], t[1]))
            if lst:
                out[layer] = lst
        return out

    def _pad_exits(self, pad: Pad, net: str,
                   half_w: float) -> dict[str, list[tuple[float, float, float]]]:
        """盘的四向轴向出口点（非栅格坐标）：{层: [(x, y, stub), …]}。

        细间距长条盘（QFP 0.5mm 节距）的栅格锚死穴：横向盘间缝 0.2mm
        任何线宽穿不过、盘轴可行窗常 <0.05mm 且相位与全局栅格恒错开
        ——ecc_2layer U1 40 网「锚点封死」根因（2026-09-23）。出口点在
        盘尖外 half_w 处、stub 沿盘轴纯轴向（侧隙由 edge_clear 精确判
        定），首跳入格由 route() 完成。仅作栅格锚为空时的回退——普通
        盘栅格锚更省，行为不变。
        """
        o = self.board.outline
        out: dict[str, list[tuple[float, float, float]]] = {}
        for layer in pad.layers:
            if layer not in self.layers:
                continue
            lst: list[tuple[float, float, float]] = []
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ex = pad.x + dx * (pad.w / 2 + half_w)
                ey = pad.y + dy * (pad.h / 2 + half_w)
                if not (o[0] + half_w - _EPS <= ex <= o[2] - half_w + _EPS
                        and o[1] + half_w - _EPS <= ey <= o[3] - half_w + _EPS):
                    continue  # 出口越板框
                if not self.index.point_clear(ex, ey, layer, net, half_w):
                    continue
                if not self.index.edge_clear(pad.x, pad.y, ex, ey, layer,
                                             net, half_w):
                    continue  # 轴向短接线侧隙（斜邻盘）不清
                lst.append((ex, ey, math.hypot(ex - pad.x, ey - pad.y)))
            if lst:
                out[layer] = lst
        return out

    def _dogbone_seeds(self, pad: Pad, net: str,
                       half_w: float) -> list[tuple[int, int, int, float, bool]]:
        """V3.1 ① 狗骨逃逸落点候选（引擎枚举，LLM 未来可直接选择的表）。

        细间距盘（QFP 0.5mm / QFN 时钟对）的同层逃逸死穴：盘间缝任何
        线宽穿不过、栅格锚全被封死（_pad_exits 家族同病灶）。狗骨 =
        盘心轴向短桩出脚 + 落点就地过孔换层，A* 从对侧层入栅格。落点带
        （@clr 230µm 口径）：盘长轴尖端外 [tip+0.4, tip+reach] 双向
        （外向 + 封装体内向——体内常无铜）× 盘轴两侧 |横偏| ≤ lat 的
        栅格节点。逐点校验（全过才入表）：轴向桩段 edge_clear → 横向段
        edge_clear（无横偏跳过）→ _via_clear（全层 + 板内）。返回按
        (桩长, 轴向距, |横|, 横向符号, 层, nix, niy) 排序取前 cap 条：
        [(nix, niy, 盘层序, 桩长, 有无横拐)]——确定性枚举，同一板状态
        重放同表。
        """
        st = self.cfg.step
        reach, lat = self.cfg.dogbone_reach, self.cfg.dogbone_lat
        lst: list[tuple[float, float, float, int, int, int, int, bool]] = []
        for layer in pad.layers:
            if layer not in self.layers:
                continue
            lix = self.layers.index(layer)
            axis_x = pad.w >= pad.h  # 平局归 x 轴（重建侧同判）
            tip = (pad.w if axis_x else pad.h) / 2
            ax_c = pad.x if axis_x else pad.y
            la_c = pad.y if axis_x else pad.x
            ad_lo, ad_hi = tip + 0.4, tip + reach
            ag_lo = int(math.ceil((ax_c - ad_hi) / st))
            ag_hi = int(math.floor((ax_c + ad_hi) / st))
            lg_lo = int(math.ceil((la_c - lat) / st))
            lg_hi = int(math.floor((la_c + lat) / st))
            for ag in range(ag_lo, ag_hi + 1):
                ad = abs(ag * st - ax_c)
                if ad < ad_lo - _EPS or ad > ad_hi + _EPS:
                    continue
                for lg in range(lg_lo, lg_hi + 1):
                    off = lg * st - la_c
                    if abs(off) > lat + _EPS:
                        continue
                    nix, niy = (ag, lg) if axis_x else (lg, ag)
                    if not (self._ix_lo <= nix <= self._ix_hi
                            and self._iy_lo <= niy <= self._iy_hi):
                        continue
                    vx, vy = nix * st, niy * st
                    cx, cy = (vx, pad.y) if axis_x else (pad.x, vy)
                    if not self.index.edge_clear(pad.x, pad.y, cx, cy,
                                                 layer, net, half_w):
                        continue  # 轴向桩段侧隙不清
                    has_lat = abs(off) > _EPS
                    if has_lat and not self.index.edge_clear(
                            cx, cy, vx, vy, layer, net, half_w):
                        continue  # 横向拐角段不清
                    if not self._via_clear(vx, vy, net):
                        continue  # 落点过孔全层净距
                    lst.append((ad + abs(off), ad, abs(off),
                                0 if off <= 0.0 else 1, lix, nix, niy,
                                has_lat))
        lst.sort()
        if self.cfg.dogbone_cap > 0:
            lst = lst[:self.cfg.dogbone_cap]
        return [(nix, niy, lix, stub, has)
                for stub, _ad, _ao, _sg, lix, nix, niy, has in lst]

    # ---------------------------------------------------------------- A*

    def route(self, net: str, a: Pad, b: Pad,
              hist: dict[tuple[int, int, int], float] | None = None,
              ) -> tuple[list[Segment], list[Via]]:
        """同网两焊盘：锚定多源 A* → (segments, vias)。失败 ValueError。

        hist：协商层（engine.py）的历史惩罚，键 = (ix, iy, 层序) 本路由
        栅格坐标——叠加进节点进入代价，被争抢过的格位逐轮变贵
        （PathFinder 语义）。缺省 None = 无惩罚，L1/D1/DRC 消费者行为
        逐字节不变。惩罚 ≥ 0，启发式仍可采纳。
        """
        if a.net != net or b.net != net:
            raise ValueError(f"焊盘 {a.key}/{b.key} 不在网络 {net!r} 上")
        w = self.width(net)
        half_w = w / 2 + self.cfg.clearance
        if half_w > self._max_radius + _EPS:
            raise ValueError(
                f"{net} 线宽 {w} 超出索引 margin——widths 未申报该网？")
        starts = self._anchors(a, net, half_w)
        goals = self._anchors(b, net, half_w)
        a_exits = self._pad_exits(a, net, half_w)  # 栅格锚空时的回退
        b_exits = self._pad_exits(b, net, half_w)
        # V3.1 ① 狗骨落点候选表（旗标关时空表 inert；注入梯队见下——
        # 仅栅格锚全灭的端注入）。①-A 颈缩：桩宽 W 的种子按 W/2+净距
        # 枚举（None = 同网络宽，Step 1 语义）——窄半宽只影响种子几何，
        # 不进主路径 half_w。
        dog_a: list = []
        dog_b: list = []
        if self.cfg.dogbone:
            dog_half = (half_w if self.cfg.dogbone_width is None
                        else self.cfg.dogbone_width / 2 + self.cfg.clearance)
            if dog_half > self._max_radius + _EPS:
                raise ValueError(
                    f"{net} 狗骨桩宽 {self.cfg.dogbone_width} 超出索引 "
                    f"margin（dog_half={dog_half:.3f}mm）")
            dog_a = self._dogbone_seeds(a, net, dog_half)
            dog_b = self._dogbone_seeds(b, net, dog_half)
        if not starts and not a_exits and not dog_a:
            raise ValueError(f"{net} 焊盘 {a.key} 栅格锚（半径 "
                             f"{self.cfg.anchor_radius}mm）与轴向出口"
                             f"（half_w={half_w:.3f}mm）全被封死")
        if not goals and not b_exits and not dog_b:
            raise ValueError(f"{net} 焊盘 {b.key} 栅格锚与轴向出口全被封死")
        goal_stub = {(ix, iy, self.layers.index(li)): s
                     for li, lst in goals.items() for (ix, iy, s) in lst}
        bx, by = b.x, b.y
        st = self.cfg.step
        # 目标侧出口回退：出口的 4 邻格当 goal 格（值 = stub+hop，供 r_goal
        # 界与语义一致；goal_exit 记真出口坐标供重建补段）。已有栅格锚的
        # 格优先直达盘心（更短），出口只添新格。
        goal_exit: dict[tuple[int, int, int], tuple[float, float]] = {}
        if not goals:
            for li, lst in b_exits.items():
                lix = self.layers.index(li)
                for ex, ey, stub in lst:
                    rx, ry = round(ex / st), round(ey / st)
                    for ndx, ndy in _DIRS:
                        k = (rx + ndx, ry + ndy, lix)
                        if k in goal_stub or not (
                                self._ix_lo <= k[0] <= self._ix_hi
                                and self._iy_lo <= k[1] <= self._iy_hi):
                            continue
                        hop = math.hypot(k[0] * st - ex, k[1] * st - ey)
                        if not self.index.edge_clear(ex, ey, k[0] * st,
                                                     k[1] * st, li, net,
                                                     half_w):
                            continue
                        goal_stub[k] = stub + hop
                        goal_exit[k] = (ex, ey)
        # V3.1 ① 狗骨目标侧（对称）：落点格当 goal 格（值 = 桩长(+拐角
        # 罚)+via_cost），goal_dog 记落点坐标+盘层供重建补「落点@j →
        # 落点@盘层 → (拐角) → 盘心」。梯队同出口回退——仅 `not goals`
        # （栅格锚全灭）时注入：主循环「首弹即停」按 g+h 出堆而 h 被
        # r_goal 压低，锚点在时混入大尾巴狗骨格会让小 g 大尾巴的次优
        # 狗骨路径抢先弹出（易板实测复现）；锚点在则零注入，字节级
        # inert。栅格锚/出口已占的格不覆盖（直达盘心更省）。
        goal_dog: dict[tuple[int, int, int], tuple[float, float, str]] = {}
        if self.cfg.dogbone and not goals:
            for nix, niy, lix, stub, has_lat in dog_b:
                for j in range(len(self.layers)):
                    if j == lix:
                        continue
                    k = (nix, niy, j)
                    if k in goal_stub:
                        continue
                    goal_stub[k] = (stub
                                    + (self.cfg.turn_cost if has_lat else 0.0)
                                    + self.cfg.via_cost)
                    goal_dog[k] = (nix * st, niy * st, self.layers[lix])
        # 启发式扣目标侧最大尾段（格锚 stub / 出口 stub+hop / 狗骨桩长
        # +拐角+via_cost——出口与狗骨尾段可远于 anchor_radius）：保持可采纳
        r_goal = max(goal_stub.values()) if goal_stub else self.cfg.anchor_radius

        # 堆键 (f, ix, iy, li, di)——全序，同分路径唯一（确定性）
        open_h: list[tuple[float, int, int, int, int]] = []
        best: dict[tuple[int, int, int, int], float] = {}
        parent: dict[tuple[int, int, int, int],
                     tuple[int, int, int, int] | tuple[str, float, float]
                     | tuple[str, float, float, str] | None] = {}

        def _h(ix: int, iy: int) -> float:
            # 曼哈顿 − 目标侧最大尾段：可采纳；×_h_scale 因主方向打折步进
            # 而收缩（先验 inert 时 =1.0，原值逐字节不变）
            if self._diag:
                # Q5 octile：8 向下界（≤ 曼哈顿）。可采纳：每条边代价 ≥
                # _h_scale×其 octile 长（轴向 cost/mm = par/perp ≥ _h_scale
                # = min(par,perp,1)；斜向 cost/mm = diag_cost_factor ≥
                # 1.0 ≥ _h_scale）+ 三角不等式。mm 域：
                # dx+dy+(√2−2)·min(dx,dy)。
                dx, dy = abs(ix * st - bx), abs(iy * st - by)
                d = dx + dy + (_SQ2 - 2.0) * (dx if dx < dy else dy)
            else:
                d = abs(ix * st - bx) + abs(iy * st - by)
            # Q7 教训（bl-a1 70/109 事故）：束井**不**随 factor 缩 h——
            # h_weight>1 本就是故意弃可采纳的贪心调速（interf 档 2.0），
            # 武装网缩 h 等效调速减半、展开数爆炸，Phase 3 逐网预算守卫
            # 中途熔断、order 尾部网全数「预算耗尽」。生产 via-pitch 落点
            # 井同此口径（不缩 h，靠折扣 < 步进分量保 ng 单调无负环）。
            h = self._h_scale * max(0.0, d - r_goal)
            # Q5 调速：h_weight > 1 = 加权 A*（弃可采纳换贪心速度，8 邻
            # 大板墙钟修法）；仅 diag45 分支消费，off 路径返回值逐字节不变。
            return h * self.cfg.h_weight if self._diag else h

        for li, lst in starts.items():
            lix = self.layers.index(li)
            for ix, iy, stub in lst:
                key = (ix, iy, lix, -1)  # 起点无来向：首步免转向罚
                best[key] = stub
                parent[key] = None
                heapq.heappush(open_h, (stub + _h(ix, iy), ix, iy, lix, -1))
        # 起点侧出口回退：出口 → 4 邻格首跳（hop 逐条 edge_clear）；parent
        # 记出口哨兵 ("exit", x, y)，重建时补「盘心 → 出口 → 首格」。
        if not starts:
            for li, lst in a_exits.items():
                lix = self.layers.index(li)
                for ex, ey, stub in lst:
                    rx, ry = round(ex / st), round(ey / st)
                    for ndx, ndy in _DIRS:
                        nix, niy = rx + ndx, ry + ndy
                        if not (self._ix_lo <= nix <= self._ix_hi
                                and self._iy_lo <= niy <= self._iy_hi):
                            continue
                        key = (nix, niy, lix, -1)
                        if key in best:
                            continue
                        if not self.index.edge_clear(ex, ey, nix * st,
                                                     niy * st, li, net,
                                                     half_w):
                            continue
                        ng = stub + math.hypot(nix * st - ex,
                                               niy * st - ey)
                        best[key] = ng
                        parent[key] = ("exit", ex, ey)
                        heapq.heappush(open_h,
                                       (ng + _h(nix, niy), nix, niy, lix, -1))
        # V3.1 ① 狗骨种子（旗标门控）：落点栅格节点直接入堆（键 = 目标层
        # j、di=-1 同锚点语义——首步免转向罚），g = 桩长(+拐角罚)+via_cost
        # + hist(目标层格)，父哨兵 ("dog", vx, vy, 盘层)。梯队同出口回退
        # ——仅 `not starts`（栅格锚全灭）时注入：起点多种子取 min 本就
        # 成本如实（不染终止判据），门控纯为 inert 契约——锚点在则零注
        # 入，旗标开合逐字节同输出。via_cost 兜底——同层可达时狗骨天然
        # 不划算；同层封死/重度拥堵时狗骨胜出。
        if self.cfg.dogbone and not starts:
            for nix, niy, lix, stub, has_lat in dog_a:
                for j in range(len(self.layers)):
                    if j == lix:
                        continue
                    key = (nix, niy, j, -1)
                    g0 = (stub + (self.cfg.turn_cost if has_lat else 0.0)
                          + self.cfg.via_cost
                          + (hist.get((nix, niy, j), 0.0) if hist else 0.0))
                    if key in best and best[key] <= g0:
                        continue
                    best[key] = g0
                    parent[key] = ("dog", nix * st, niy * st, self.layers[lix])
                    heapq.heappush(open_h, (g0 + _h(nix, niy), nix, niy, j, -1))
            self.dog_seeds[net] = self.dog_seeds.get(net, 0) + len(dog_a)

        goal_key: tuple[int, int, int, int] | None = None
        # 节距吸引井（Q3）：井激活 = 配置了井宽且引擎已给出非零 p*（暖机期
        # pitch_star=0 → 井休眠，行为同纯 SDF）。井 inert（band=0）时全程
        # False，下方代价路径逐字节同旧代码。
        use_well = self._pitch_band > 0.0 and self.pitch_star > 0.0
        # Q3' via 落点井：激活 = 配了井宽且至少一个桶已暖机。井 inert
        # （band=0）或 dict 空（L1/D1 等既有消费者从不暖机）时全程 False，
        # 换层边代价逐字节同旧代码。
        use_via_well = (self.cfg.via_pitch_band > 0.0
                        and bool(self.via_pitch_star))
        # Q7 束联铺兄弟井：激活 = engine 装配了 ctx（仅 Phase 3 重布束
        # 网）。None 时全程 False，下方代价路径逐字节同旧代码。
        bc = self.bundle_ctx
        use_bundle = bc is not None
        # Q5 单网软帽：diag_node_cap > 0 且 8 向态时数节点弹出，超帽抛
        # 异常给 engine 回退四向（off / 无帽路径零成本，inert）。
        _cap = self.cfg.diag_node_cap
        # F1（Q7 修复轮 3）：武装尝试（ctx 激活）用更小的独立帽——井诱导
        # 搜索不收敛时半途止损，撞帽走既有降级梯而非烧满全帽。ctx 为
        # None 或 cap=0 时此分支不动 _cap，逐字节同旧。
        if use_bundle and self.cfg.bundle_arm_cap > 0:
            _cap = min(_cap, self.cfg.bundle_arm_cap)
        _count_cap = self._diag and _cap > 0
        _pops = 0
        while open_h:
            if _count_cap and (_pops := _pops + 1) > _cap:
                raise DiagNodeCapExceeded(
                    f"{net}: 8 向 A* 扩展超帽（>{_cap} 节点）")
            _f, ix, iy, li, di = heapq.heappop(open_h)
            self.pops += 1
            if (ix, iy, li) in goal_stub:
                goal_key = (ix, iy, li, di)
                break
            g = best[(ix, iy, li, di)]
            px, py = ix * st, iy * st
            # Q5 post-via 态归一：di ∈ 8..15 = 刚过孔（vdi = idi+8），真来向
            # = di−8；diag 关时 di 恒 <8，idi = di 数值恒等。
            idi = di - 8 if di >= 8 else di
            for ndi, (ndx, ndy) in enumerate(self._dirs):
                nix, niy = ix + ndx, iy + ndy
                if not (self._ix_lo <= nix <= self._ix_hi
                        and self._iy_lo <= niy <= self._iy_hi):
                    continue
                # Q5 转向罚分级：k = _TURN_T[来向][去向]（45° 单位）。diag 关
                # 时 k∈{0,2,4} 映 0/tc/tc，与旧 `turn_cost if di>=0 and
                # ndi != di` 数值逐字节同义；via_corner 罚只挂「刚过孔后
                # 首步转向」（弯跨孔上），共线直穿孔零罚。
                tp = 0.0 if idi < 0 else self._turn_cost[_TURN_T[idi][ndi]]
                if di >= 8:
                    tp += self.cfg.via_corner_cost
                ng = (g + self._step[li][ndi] + tp
                      + (hist.get((nix, niy, li), 0.0) if hist else 0.0))
                nkey = (nix, niy, li, ndi)
                # 井激活时下方折扣最多把 ng 压低 (1−pf)×step——预剪枝必须
                # 用这个下界判（用原 ng 会把「现贵但折后更优」的路径错杀，
                # 破坏最优性）。井 inert 时下界 = ng，与旧判据逐字节同义。
                ng_lo = (ng - (1.0 - self.cfg.pitch_factor)
                         * self._step[li][ndi] if use_well else ng)
                # Q7 兄弟井同式下界：井互斥（well_cut）故两折扣不叠加，
                # 取两者之更深者即保守下界；井 inert 时数值不变。
                if use_bundle:
                    ng_lo = min(ng_lo, ng - (1.0 - self.cfg.bundle_factor)
                                * self._step[li][ndi])
                if nkey in best and best[nkey] <= ng_lo:
                    continue
                if not self.index.edge_clear(px, py, nix * st, niy * st,
                                             self.layers[li], net, half_w):
                    continue
                # 间距软排斥（Q1 进阶）：平底井罚——铜缘净距 < band 按接近
                # 程度线性加罚（贴到 0 满罚），≥ band 免费 → 最优解贴 band
                # 边缘走 → 平行列距收敛到公共值（cv 的成因「贴着挤/独行
                # 绕开」并存由此压平）。edge_gap 回的是含 clearance 扫掠带
                # 的裕量，+ clearance 折回铜缘净距（band 语义 = 铜缘距：
                # band 0.75 → 中心距 ≈ 0.75+0.25=1.0）。非负罚不破坏启发
                # 式可采纳；罚后复查剪枝（best 存含罚代价，下界仍保守）。
                # 节距吸引井（Q3）同查询双用：净距落在 p*±pitch_band 井内
                # 给步进折扣（谷底 ×pitch_factor，井沿线性衰减到 0）——
                # 平行段被吸向公共节距 p*。井只在 band 外生效（上面 band
                # 内只罚不折——折扣绝不奖励贴挤）；折扣 < 步进分量（ng 单
                # 调不降、无负环），_h_scale 已 ×pitch_factor 保可采纳；
                # 折后复查剪枝（best 存折后代价，防回写更差值）。
                well_cut = False
                if self._space_band > 0.0 or use_well:
                    slack = self.index.edge_gap(
                        px, py, nix * st, niy * st,
                        self.layers[li], net, half_w)
                    if slack is not None:
                        gap = slack + self.cfg.clearance  # 铜缘净距
                        if gap < self._space_band:
                            ng += self.cfg.space_cost * (
                                1.0 - gap / self._space_band)
                            if nkey in best and best[nkey] <= ng:
                                continue
                        elif use_well:
                            dev = abs(gap - self.pitch_star)
                            if dev < self._pitch_band:
                                ng -= (1.0 - self.cfg.pitch_factor) \
                                    * self._step[li][ndi] \
                                    * (1.0 - dev / self._pitch_band)
                                well_cut = True
                                if nkey in best and best[nkey] <= ng:
                                    continue
                # Q7 束联铺软领随井（D4）：轴平行步到最近束内兄弟已布段
                # 的垂距落在 k·p*（本束 (层,向) 桶）±bundle_band 内 →
                # 步进折扣（谷底 ×bundle_factor，井沿线性衰减到 0）。井
                # 互斥：Q3 SDF 节距井已折的边不再折（叠加会破 ng_lo 下
                # 界）；折扣 < 步进分量（ng 单调不降、无负环）、h 不缩
                # （加权 A* 本就弃可采纳——见 _h 注释）；折后复查剪枝
                # （best 存折后代价）。仅 ndi<4 纯轴步认井（斜步不正交，
                # 无节距语义）。
                if use_bundle and not well_cut and ndi < 4:
                    d = self._sibling_gap(bc, li, ndi, px, py,
                                          nix * st, niy * st)
                    if d != math.inf:
                        p_star = bc.pstar.get((self.layers[li],
                                               ndi in (0, 2)))
                        if p_star:
                            k = max(1, round(d / p_star))
                            dev = abs(d - k * p_star)
                            if dev < self.cfg.bundle_band:
                                ng -= (1.0 - self.cfg.bundle_factor) \
                                    * self._step[li][ndi] \
                                    * (1.0 - dev / self.cfg.bundle_band)
                                if nkey in best and best[nkey] <= ng:
                                    continue
                best[nkey] = ng
                parent[nkey] = (ix, iy, li, di)
                heapq.heappush(open_h, (ng + _h(nix, niy), nix, niy, li, ndi))
            # 换层（原地点过孔）：通孔贯穿全部信号层——孔盘对每层异网铜
            # 全清（环处处有铜）才放行；一个孔可跳任意层（同一钻）
            if self._via_clear(px, py, net):
                for j in range(len(self.layers)):
                    if j == li:
                        continue
                    # Q5 post-via 标记：diag 开且真有来向 → vdi = idi+8（过孔
                    # 后首步转向收 via_corner 罚 =「弯跨孔上」；共线直穿孔
                    # 零罚）。diag 关时 vdi = idi = di，键与旧代码逐字节一致。
                    # 普通态与 post-via 态过孔落到同一 vkey（同 idi+8）——
                    # best 取 min g 恰好正确（两者后续代价结构同构）。
                    vdi = idi + 8 if (self._diag and idi >= 0) else idi
                    vkey = (ix, iy, j, vdi)
                    vg = (g + self.cfg.via_cost
                          + (hist.get((ix, iy, j), 0.0) if hist else 0.0))
                    # Q3' via 落点井：孔位被吸向目标层 k·p* 网格。折扣
                    # ≤ (1−factor)·via_cost < via_cost（vg ≥ g 恒成立，
                    # 无负环）；h 不含 via 项，可采纳不破坏；折扣只依赖
                    # (px,py,j,idi) 本身（同键同折扣），best 比较即精确。
                    # 起点无来向附着段 → 不折；井桶按归一真来向取（idi）。
                    if use_via_well and idi >= 0:
                        vg -= self._via_landing_discount(px, py, j, idi)
                    if not (vkey in best and best[vkey] <= vg):
                        best[vkey] = vg
                        parent[vkey] = (ix, iy, li, di)
                        heapq.heappush(
                            open_h, (vg + _h(ix, iy),
                                     ix, iy, j, vdi))

        if goal_key is None:
            raise ValueError(
                f"{net} {a.key} → {b.key} 无可行路径（栅格 {self.cfg.step}mm，"
                f"间隙 {self.cfg.clearance}mm，异网铜封路）")

        # 回溯网格点序列（含首尾锚点），再接两端 stub
        chain: list[tuple[int, int, int, int]] = []
        k: tuple[int, int, int, int] | None = goal_key
        while k is not None:
            chain.append(k)
            pk = parent[k]
            # 出口哨兵 ("exit", x, y) 是链头，止走（首元素 str 分流网格键）
            k = pk if pk is not None and not isinstance(pk[0], str) else None
        chain.reverse()
        pts: list[tuple[float, float, str]] = []
        p0 = parent[chain[0]]
        # V3.1 ① 狗骨链头：首格在目标层 j，重建「盘心@盘层 → (拐角)@盘层
        # → 落点@盘层」+ 首格（落点@j）——_materialize 在层变处生成 via。
        # 拐角/横偏由盘几何+落点坐标确定性重算（哨兵只存落点+盘层）。
        head_end = 0  # 狗骨头段末下标+1；0 = 无狗骨头（①-A 颈缩物化用）
        if p0 is not None and p0[0] == "dog":
            pl = p0[3]
            axis_x = a.w >= a.h  # 与 _dogbone_seeds 同判（平局归 x 轴）
            vx, vy = p0[1], p0[2]
            if axis_x:
                has_lat = abs(vy - a.y) > _EPS
            else:
                has_lat = abs(vx - a.x) > _EPS
            pts.append((a.x, a.y, pl))
            if has_lat:
                corner = (vx, a.y) if axis_x else (a.x, vy)
                pts.append((corner[0], corner[1], pl))
            pts.append((vx, vy, pl))
            self.dog_used.add(net)
            head_end = len(pts)
        else:
            pts.append((a.x, a.y, self.layers[chain[0][2]]))
            if p0 is not None and isinstance(p0[0], str):  # 首格经出口入格
                pts.append((p0[1], p0[2], self.layers[chain[0][2]]))
        pts.extend((cix * st, ciy * st, self.layers[cli])
                   for cix, ciy, cli, _ in chain)
        # 目标侧三分支：狗骨尾段（落点@j → 落点@盘层 → (拐角) → 盘心@盘
        # 层）/ 出口尾段 / 直达盘心，各补终点。
        tail_start = len(pts)  # 狗骨尾段首下标；== len(pts) = 无狗骨尾
        gd = goal_dog.get(goal_key[:3])
        if gd is not None:
            vx, vy, pl = gd
            axis_x = b.w >= b.h  # 与 _dogbone_seeds 同判（平局归 x 轴）
            if axis_x:
                has_lat = abs(vy - b.y) > _EPS
            else:
                has_lat = abs(vx - b.x) > _EPS
            pts.append((vx, vy, pl))
            if has_lat:
                corner = (vx, b.y) if axis_x else (b.x, vy)
                pts.append((corner[0], corner[1], pl))
            pts.append((bx, by, pl))
            self.dog_used.add(net)
        elif goal_key[:3] in goal_exit:  # 目标侧出口尾段
            ex, ey = goal_exit[goal_key[:3]]
            pts.append((ex, ey, self.layers[goal_key[2]]))
            pts.append((bx, by, self.layers[goal_key[2]]))
        else:
            pts.append((bx, by, self.layers[goal_key[2]]))
        # V3.1 ①-A 颈缩物化：桩宽独立于网络宽时，头/尾狗骨段按桩宽生成、
        # 中段维持网络宽——分片各自 _materialize，片界成对点（落点@盘层 ↔
        # 落点@目标层）保证换层 via 落在原位。分片可能比单次调用的共线
        # 简化多出段（片界不跨并），几何逐段等价。桩宽 = 网络宽（None）
        # 或路径无狗骨段时走单次调用，与 Step 1 输出逐字节一致。
        dw = self.cfg.dogbone_width
        if dw is None or (head_end == 0 and tail_start == len(pts)):
            return self._materialize(pts, net)
        segs: list[Segment] = []
        vias: list[Via] = []
        if head_end:
            s, v = self._materialize(pts[:head_end + 1], net, dw)
            segs.extend(s)
            vias.extend(v)
        mid_hi = tail_start if tail_start < len(pts) else len(pts)
        if mid_hi > head_end:
            s, v = self._materialize(pts[head_end:mid_hi], net)
            segs.extend(s)
            vias.extend(v)
        if tail_start < len(pts):
            s, v = self._materialize(pts[tail_start - 1:], net, dw)
            segs.extend(s)
            vias.extend(v)
        return segs, vias

    def _via_inside(self, x: float, y: float) -> bool:
        """过孔盘不越板框。"""
        r = self.cfg.via_size / 2
        o = self.board.outline
        return (o[0] + r - _EPS <= x <= o[2] - r + _EPS
                and o[1] + r - _EPS <= y <= o[3] - r + _EPS)

    def _via_clear(self, x: float, y: float, net: str) -> bool:
        """过孔盘对全部信号层异网铜清空且不越板框（铜环贯穿全层）。"""
        return self._via_inside(x, y) and all(
            self.index.point_clear(x, y, layer, net, self._via_radius)
            for layer in self.layers)

    def _nearest_parallel_gap(self, px: float, py: float, j: int,
                              di: int,
                              exclude: "Segment | None" = None) -> float:
        """候选点 (px,py) 到层 j 上最近平行段的垂距（inf = 无邻居或超
        reach）。桶方向 = 来向 di 水平与否；延伸不覆盖孔位的段不算（对齐
        才有节距意义）。井折扣（Q3'）与 engine 的 p* 采样共用这一量测。
        exclude：采样传附着段自身——孔在其轴线端点上，不排则自命中率 0
        恒最小、样本窗 (0.2, cap] 永远空（井评估候选点无铜，不受影响）。

        精确性：垂距 ≤ reach 且覆盖窗重叠的邻段必与探针线段 bbox 相交，
        其膨胀 bbox（margin ≥ 0.554 > look 0.5）亦然 → _along_edge 返回
        超集，无需为此扩索引 margin。"""
        horiz = di in (0, 2)
        reach = self.cfg.via_pitch_cap + self.cfg.via_pitch_band
        look = 0.5  # 邻段延伸覆盖窗（quality MIN_OV 语义，< margin 保精确）
        if horiz:  # 水平附着 → 竖直探针找同层水平段
            probe = self.index._along_edge(
                self.layers[j], px, py - reach, px, py + reach)
        else:
            probe = self.index._along_edge(
                self.layers[j], px - reach, py, px + reach, py)
        d = math.inf
        for o in probe:
            if not isinstance(o, Segment):
                continue  # 盘/孔不是节距参照
            if o is exclude:
                continue  # 附着段自身：孔在其轴线上，dd 恒 0
            if horiz:
                if abs(o.y2 - o.y1) > _EPS:
                    continue  # 非水平段
                if (min(o.x1, o.x2) > px + look
                        or max(o.x1, o.x2) < px - look):
                    continue
                dd = abs(py - o.y1)
            else:
                if abs(o.x2 - o.x1) > _EPS:
                    continue
                if (min(o.y1, o.y2) > py + look
                        or max(o.y1, o.y2) < py - look):
                    continue
                dd = abs(px - o.x1)
            if dd < d:
                d = dd
        return d

    def _sibling_gap(self, bc: "_BundleCtx", li: int, ndi: int,
                     x1: float, y1: float, x2: float, y2: float) -> float:
        """Q7 兄弟井垂距：候选轴平行步 (x1,y1)→(x2,y2) 到 (层,向) 桶内
        最近束内兄弟段的垂距（inf = 无覆盖邻居）。桶 = ctx 预排序段表
        （束内已提交兄弟，精确几何局部表——不走 ObstacleIndex）；coords
        二分定位 + 双向扫描，超当前最优/reach 早退。沿窗覆盖判 0.5
        （quality MIN_OV 语义，_nearest_parallel_gap 同式）。"""
        horiz = ndi in (0, 2)
        key = (self.layers[li], horiz)
        rows = bc.tbl.get(key)
        if not rows:
            return math.inf
        reach = self._bundle_reach
        perp = y1 if horiz else x1
        lo, hi = ((min(x1, x2), max(x1, x2)) if horiz
                  else (min(y1, y2), max(y1, y2)))
        coords = bc.coords[key]
        i = bisect.bisect_left(coords, perp)
        d = math.inf
        j = i - 1
        while j >= 0:
            if abs(coords[j] - perp) >= min(d, reach):
                break
            _c, slo, shi = rows[j]
            if not (slo > hi + 0.5 or shi < lo - 0.5):
                dd = abs(perp - coords[j])
                if dd < d:
                    d = dd
            j -= 1
        j = i
        n = len(rows)
        while j < n:
            if abs(coords[j] - perp) >= min(d, reach):
                break
            _c, slo, shi = rows[j]
            if not (slo > hi + 0.5 or shi < lo - 0.5):
                dd = abs(perp - coords[j])
                if dd < d:
                    d = dd
            j += 1
        return d

    def _via_landing_discount(self, px: float, py: float, j: int,
                              di: int) -> float:
        """Q3' via 落点节距井：候选孔位 (px,py) 到目标层 j 上最近平行段的
        垂距落在 k·p* 井内 → 折扣该换层边的 via_cost（谷底满折
        (1−via_pitch_factor)×via_cost，井沿线性衰减到 0）。桶 = (层, 来向
        水平与否)——来向水平则附着段水平，与 quality spacing_cv 的
        (layer, horiz) 桶同键。参照不分网（quality 口径同源：同网并排段
        也是节距参照）。无邻居（新开行）→ 0 中性。
        Q5：入参 di 允许 post-via 态（≥8），入口归一成真来向；斜来向
        （4..7）不认井——井探针纯正交（v1 已知局限，PLAN 记录在案）。"""
        di = di - 8 if di >= 8 else di
        if di < 0 or di > 3:
            return 0.0
        horiz = di in (0, 2)
        p_star = self.via_pitch_star.get((self.layers[j], horiz))
        if not p_star or p_star <= 0.0:
            return 0.0
        d = self._nearest_parallel_gap(px, py, j, di)
        if d == math.inf or d > (self.cfg.via_pitch_cap
                                 + self.cfg.via_pitch_band):
            return 0.0  # 无邻居（新开行）/ 超出探索范围 → 中性
        k = max(1, round(d / p_star))  # 主吸 k=1（0a：人工单点聚集）
        dev = abs(d - k * p_star)
        band = self.cfg.via_pitch_band
        if dev >= band:
            return 0.0
        return ((1.0 - self.cfg.via_pitch_factor) * self.cfg.via_cost
                * (1.0 - dev / band))

    def _materialize(self, pts: list[tuple[float, float, str]], net: str,
                     w: float | None = None
                     ) -> tuple[list[Segment], list[Via]]:
        """点列 → 共线简化 → segment/via（层切换点 = 过孔）。w = 段宽
        （None = 网络宽，既有行为；V3.1 ①-A 颈缩头/尾桩段传桩宽）。"""
        width = self.width(net) if w is None else w
        segs: list[Segment] = []
        vias: list[Via] = []
        simple = _simplify(pts)
        for (x1, y1, l1), (x2, y2, l2) in zip(simple, simple[1:]):
            if l1 != l2:
                vias.append(Via(x2, y2, self.cfg.via_size,
                                self.cfg.via_drill, net))
            elif abs(x2 - x1) + abs(y2 - y1) > _EPS:
                segs.append(Segment(x1, y1, x2, y2, width, l1, net))
        return segs, vias

    def commit(self, segs: list[Segment], vias: list[Via]) -> None:
        """已布铜入障（顺序布线：后布的网络绕开先布的）。"""
        self.board.segments.extend(segs)
        self.board.vias.extend(vias)
        self.index.add(segs, vias)

    def rollback(self, segs: list[Segment], vias: list[Via]) -> None:
        """commit 的对称回滚（索引与 board 同步退回，按对象身份）。"""
        self.index.remove(segs, vias)
        for s in segs:
            for i, o in enumerate(self.board.segments):
                if o is s:
                    del self.board.segments[i]
                    break
        for v in vias:
            for i, o in enumerate(self.board.vias):
                if o is v:
                    del self.board.vias[i]
                    break
