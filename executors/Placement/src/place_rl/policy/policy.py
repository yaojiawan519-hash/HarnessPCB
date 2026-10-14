"""放置策略网络——DesignDocument §3.3 / M3。

两级动作分解：第一步「选下一个放哪个元件」（顺序），第二步「放哪、什么朝向」。
掩码（Pos Mask / Wire Mask）在 M4 接上；M3 先做行为克隆，落点用连续高斯、
朝向用 0/90/180/270 四分类，共享 GNN 表示。
"""
from __future__ import annotations

import torch
import torch.nn as nn

from ..net.gnn import HypergraphGNN

__all__ = ["PlacementPolicy", "NODE_FEAT_DIM", "ROT_BINS"]

NODE_FEAT_DIM = 12  # 尺寸2 + 引脚1 + 度数1 + 类型3 + 已放1 + 位姿4
ROT_BINS = 4  # 0 / 90 / 180 / 270


class PlacementPolicy(nn.Module):
    def __init__(
        self,
        in_dim: int = NODE_FEAT_DIM,
        hidden: int = 128,
        gnn_layers: int = 3,
        rot_bins: int = ROT_BINS,
    ):
        super().__init__()
        self.gnn = HypergraphGNN(in_dim, hidden, gnn_layers)
        # 元件选择头：节点嵌入 → 标量 logit（未放件掩码后 softmax）
        self.select = nn.Sequential(
            nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1)
        )
        # 落点头：选中节点嵌入 + 全局图摘要 + 参考锚点 → 位置偏移 + 朝向 logits
        self.place = nn.Sequential(
            nn.Linear(hidden * 2 + 2, hidden), nn.ReLU(), nn.Linear(hidden, hidden)
        )
        self.xy = nn.Linear(hidden, 2)          # 相对锚点的偏移，∈ (-1,1)²
        self.rot = nn.Linear(hidden, rot_bins)
        self.log_std = nn.Parameter(torch.zeros(1))  # 高斯落点共享对数标准差
        self.value = nn.Linear(hidden, 1)            # 值头（M4 PPO 用）

    def encode(self, x: torch.Tensor, nets: list[list[int]]) -> torch.Tensor:
        return self.gnn(x, nets)

    def select_logits(self, h: torch.Tensor) -> torch.Tensor:
        return self.select(h).squeeze(-1)  # [N]

    def value_head(self, h: torch.Tensor) -> torch.Tensor:
        return self.value(h.mean(dim=0)).squeeze(-1)  # 标量状态价值

    def place_dist(
        self, h: torch.Tensor, selected: int, anchor: torch.Tensor
    ) -> tuple[torch.Tensor, ...]:
        """给定节点嵌入、选中下标与参考锚点，返回落点分布参数。

        ``anchor`` 为 [2] 归一化参考点（已放邻居质心，无邻居则板中心）。
        返回 ``(xy_offset, rot_logits, log_std)``；``xy_offset`` ∈ (-1,1)²，
        实际落点 = anchor + offset（归一化）。
        """
        ctx = h.mean(dim=0)  # 全局图摘要（可替换为注意力池化）
        ph = self.place(torch.cat([h[selected], ctx, anchor]))
        xy = torch.tanh(self.xy(ph))
        rot_logits = self.rot(ph)
        return xy, rot_logits, self.log_std
