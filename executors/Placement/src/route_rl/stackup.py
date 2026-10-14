"""L0 叠层语义层：叠层声明 → pass 计划（把 merge 法内化为标准输出）。

层角色：
- signal：允许布信号走线
- plane(net=...)：平面层。是导体不是障碍——平面网的连通定义改为
  "每个不在该层上的焊盘经 ≥1 过孔落面（stitch）"，而非走线；
  pass 计划里平面网从信号布线中移除（DSN 删网），层本身对信号布线锁死
- mixed：v1 预留（未用；盲埋孔板型扩展时启用）

声明优先级：显式 JSON > 板内 zone 推断（R0 后期实现）> 网络类约定（提案）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LayerSpec:
    name: str
    role: str                 # "signal" | "plane" | "mixed"
    net: str | None = None    # plane 层的平面网名

    @property
    def is_signal(self) -> bool:
        return self.role == "signal"


@dataclass(frozen=True)
class PassSpec:
    """单个 pass 的 DSN 构造开关 + 评分序。全部变换的实证状态见规划文档 §4.3。"""
    name: str                            # signal / power / finish / planes
    delete_plane_nets: bool = False      # 删平面网（stitch 语义）
    lock_non_signal: bool = False        # 非 signal 层锁死
    protect_wiring: bool = False         # 既有 wiring 全 protect
    shrink_npth: bool = False            # NPTH keepout 收缩
    force_clearance_txt: bool = False    # DSN 文本再抬 clearance（signal legacy 行为）
    default_clearance_um: int = 245
    # 评分序（越小越好）；前两键 = (完成度, 铜级)，也用作早停判据
    score_keys: tuple[str, ...] = ("sig", "copper", "un")


@dataclass
class Stackup:
    board_id: str
    layers: list[LayerSpec]
    aux_power_nets: list[str] = field(default_factory=list)
    via_ladder: list[list[int]] = field(default_factory=lambda: [[300, 600]])
    npth_hole_radius_um: int = 1625
    npth_exported_radius_um: int = 3750

    # ---------- 派生 ----------
    @property
    def signal_layers(self) -> list[str]:
        return [l.name for l in self.layers if l.is_signal]

    @property
    def plane_layers(self) -> list[LayerSpec]:
        return [l for l in self.layers if l.role == "plane"]

    @property
    def plane_nets(self) -> set[str]:
        return {l.net for l in self.plane_layers}

    @property
    def power_nets(self) -> set[str]:
        """计信号未布通时视同电源的网（平面网 ∪ 声明的辅助电源网）。"""
        return self.plane_nets | set(self.aux_power_nets)

    # ---------- pass 计划编译 ----------
    def pass_plan(self) -> list[PassSpec]:
        """merge 法的标准 pass 序列；2 层板退化为单信号 pass。"""
        if not self.plane_layers:
            return [PassSpec("signal", score_keys=("sig", "copper", "un"))]
        return [
            # A 信号：删平面网 + 锁非信号层（已验证：signal_route_bon）
            PassSpec("signal", delete_plane_nets=True, lock_non_signal=True,
                     force_clearance_txt=True,
                     score_keys=("sig", "copper", "un")),
            # B 电源：信号 wiring 全 protect，平面网纯走线布通（power_route）
            PassSpec("power", protect_wiring=True,
                     score_keys=("pwr_plane", "copper", "un")),
            # C 收尾：protect + 锁层 + NPTH 缩径，clearance 阶梯由控制器给
            PassSpec("finish", protect_wiring=True, lock_non_signal=True,
                     shrink_npth=True,
                     score_keys=("sig", "copper", "pwr_plane")),
            # D 平面生成：后处理器，不走 FreeRouting（merge_planes）
            PassSpec("planes"),
        ]

    @classmethod
    def from_json(cls, path: str | Path) -> "Stackup":
        d = json.loads(Path(path).read_text())
        layers = [LayerSpec(**l) for l in d["layers"]]
        return cls(board_id=d["board_id"], layers=layers,
                   aux_power_nets=d.get("aux_power_nets", []),
                   via_ladder=d.get("via_ladder", [[300, 600]]),
                   npth_hole_radius_um=d.get("npth_hole_radius_um", 1625),
                   npth_exported_radius_um=d.get("npth_exported_radius_um", 3750))

    # ---------- zone 推断（声明优先级第二级） ----------
    @classmethod
    def infer_from_board(cls, path: str | Path) -> "Stackup":
        """从板内 zone 推断叠层。只对「已带平面 zone 的板」有效——merge 流程的
        中间态（未倒 zone）推不出平面，必须显式声明；这是它在优先级里
        排在 JSON 之后的根本原因。

        规则 v0（与 hardware-studio 平面叠层板对拍得出）：
        - 铜层取自 (layers …) 声明；F.Cu/B.Cu 恒 signal；
        - 内层：存在单层 zone 且其 polygon bbox ≥ 全板 zone 包络 60% →
          plane(取该层最大 zone 的 net)；小电源岛不改变层角色；
        - aux_power_nets 文本层无可靠推断 → 留空。
        """
        txt = Path(path).read_text()

        def _copper_layers() -> list[str]:
            m = re.search(r"\(layers(.*?)\n\s*\)", txt, re.S)
            names = re.findall(r'\(\d+ "([^"]+\.Cu)" \w+', m.group(1)) if m else []
            return names or ["F.Cu", "B.Cu"]

        # 单层 zone：(net "N") + (layer "X")；多层 zone ((layers "A" "B")) 不参与
        zones: list[tuple[str, str, float]] = []   # (net, layer, polygon bbox 面积)
        for zm in re.finditer(r"\n\t\(zone\n(.*?)\n\t\)", txt, re.S):
            blk = zm.group(1)
            net = re.search(r'\(net "([^"]+)"\)', blk)
            lay = re.search(r'\(layer "([^"]+)"\)', blk)
            if not (net and lay):
                continue
            poly = blk.split("(filled_polygon")[0]
            pts = [(float(a), float(b)) for a, b in
                   re.findall(r"\(xy ([\d.\-]+) ([\d.\-]+)", poly)]
            if len(pts) < 3:
                continue
            xs, ys = zip(*pts)
            zones.append((net.group(1), lay.group(1),
                          (max(xs) - min(xs)) * (max(ys) - min(ys))))

        env = max((a for _, _, a in zones), default=0.0)
        roles: dict[str, tuple[str, str | None]] = {}
        for name in _copper_layers():
            if name in ("F.Cu", "B.Cu"):
                roles[name] = ("signal", None)
                continue
            big = [(n, a) for n, l, a in zones if l == name and env and a >= 0.6 * env]
            if big:
                net = max(big, key=lambda t: t[1])[0]
                roles[name] = ("plane", net)
            else:
                roles[name] = ("signal", None)

        return cls(board_id=Path(path).stem,
                   layers=[LayerSpec(nm, r, net) for nm, (r, net) in roles.items()])
