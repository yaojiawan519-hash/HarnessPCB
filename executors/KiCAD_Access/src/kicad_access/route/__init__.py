"""M3 布线：L1 确定性布线（电源树/差分对/BGA 扇出）+ L2 freerouting 封装。

模块切分（对照 reference/pcb_scripts 重写加固，参考不直抄）：
- ``model``   布线态板模型：pads/segments/vias/zones 读取 + 旋转烘焙 + 距离场
- ``anchors`` 领地 + 锚盘规划（锚盘布线模型）：跨区网在领地边界开盘
- ``regions`` net→模块归属 + per-region 板生成/合并（人工审查关卡载体）
- ``astar``   两层栅格 A* + 间隙模型（negotiate.py 原型重写）
- ``l1``      确定性布线策略（电源树/差分对/扇出顺序与拓扑）
- ``planes``  内层平面（M3-3c）：GND/电源铺铜 + SMD 落孔 + BGA 盘中孔
- ``diff``    差分对（M3-4）：ETH/CAN 对连通 + 等长把关（FINE 栅格）
- ``dsn``/``ses``  Specctra 导出/回写（freerouting 对接口）
- ``stitch``  GND 缝合孔（M3-6）：内层地平面 → 全板网格过孔
- ``drc``     自有 DRC 子集（短路/间隙/宽度/断裂——合并闭环）
"""

from .anchors import (
    Anchor, draw_territories, plan_anchors, territory_nets, territories,
    write_territory_board,
)
from .astar import GridRouter, ObstacleIndex, RouteConfig
from .diff import FINE, PairRule, PairStat, pairs_from_spec, route_diff
from .dsn import DsnBundle, export_dsn, parse_ses
from .drc import DrcViolation, run_drc
from .freeroute import FreerouteError, UnroutedNets, real_runner, route_l2
from .l1 import NetReport, route_power, widths_from_spec
from .merge import OverlayResult, drop_anchors, open_anchors, overlay_territories
from .model import Pad, RouteBoard, Segment, Via, Zone, pad_dist, seg_dist, seg_seg_dist, via_dist
from .planes import plan_power_drops, route_planes, route_planes_final
from .regions import (
    MergeResult, Region, append_copper, draw_region_boxes, merge_regions,
    plan_regions, write_region_board,
)
from .stitch import StitchResult, stitch_gnd

__all__ = [
    "Anchor", "draw_territories", "plan_anchors", "territory_nets",
    "territories", "write_territory_board",
    "GridRouter", "ObstacleIndex", "RouteConfig",
    "FINE", "PairRule", "PairStat", "pairs_from_spec", "route_diff",
    "DsnBundle", "export_dsn", "parse_ses",
    "DrcViolation", "run_drc",
    "FreerouteError", "UnroutedNets", "real_runner", "route_l2",
    "NetReport", "route_power", "widths_from_spec",
    "OverlayResult", "drop_anchors", "open_anchors", "overlay_territories",
    "Pad", "RouteBoard", "Segment", "Via", "Zone",
    "pad_dist", "seg_dist", "seg_seg_dist", "via_dist",
    "plan_power_drops", "route_planes", "route_planes_final",
    "MergeResult", "Region", "append_copper", "draw_region_boxes",
    "merge_regions", "plan_regions", "write_region_board",
    "StitchResult", "stitch_gnd",
]
