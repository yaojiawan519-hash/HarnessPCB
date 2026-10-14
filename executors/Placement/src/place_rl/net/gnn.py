"""网表超图 GNN 编码器——DesignDocument §3.2 / M3。

把放置问题建成超图：元件 = 节点，net = 超边。节点特征含封装尺寸、引脚数、
类型与当前放置态；消息传递按「节点 → 超边 → 节点」聚合，逐层更新节点嵌入，
作为策略（元件选择 + 落点）与值函数共享的表示。

纯 torch 实现，无外部图库；为 CPU/MPS 上中规模板（≤ 数百元件）设计，
net 内聚用 Python 循环 + ``index_add_``，规模小到可忽略。
"""
from __future__ import annotations

import torch
import torch.nn as nn

__all__ = ["HypergraphGNN"]


class _HGLayer(nn.Module):
    """一轮超图消息传递：node → hyperedge（mean）→ node（mean）。"""

    def __init__(self, hidden: int):
        super().__init__()
        self.edge_lin = nn.Linear(hidden, hidden)   # 超边内成员特征聚合后投影
        self.node_lin = nn.Linear(hidden * 2, hidden)  # [自身 | 邻接超边消息]
        self.act = nn.ReLU()
        self.norm = nn.LayerNorm(hidden)

    def forward(self, h: torch.Tensor, nets: list[list[int]]) -> torch.Tensor:
        N = h.shape[0]
        dev = h.device

        # node → hyperedge：每个 net 对其成员节点特征取均值后投影
        edge_emb: list[torch.Tensor] = []
        for net in nets:
            idx = torch.tensor(net, device=dev, dtype=torch.long)
            pooled = h[idx].mean(dim=0)  # [hidden]
            edge_emb.append(self.edge_lin(pooled))

        # hyperedge → node：每个节点对其所属 net 的消息取均值
        msg = torch.zeros_like(h)
        cnt = torch.zeros(N, device=dev)
        for j, net in enumerate(nets):
            idx = torch.tensor(net, device=dev, dtype=torch.long)
            msg.index_add_(0, idx, edge_emb[j].unsqueeze(0).expand(len(net), -1))
            cnt.index_add_(0, idx, torch.ones(len(net), device=dev))
        msg = msg / cnt.clamp_min(1.0).unsqueeze(1)

        return self.norm(self.act(self.node_lin(torch.cat([h, msg], dim=-1))))


class HypergraphGNN(nn.Module):
    def __init__(self, in_dim: int, hidden: int = 128, layers: int = 3):
        super().__init__()
        self.enc = nn.Sequential(
            nn.Linear(in_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden)
        )
        self.convs = nn.ModuleList([_HGLayer(hidden) for _ in range(layers)])
        self.out_dim = hidden

    def forward(self, x: torch.Tensor, nets: list[list[int]]) -> torch.Tensor:
        """``x`` [N, in_dim] 节点原始特征；``nets`` 每个 net 的成员节点下标。

        返回 [N, hidden] 节点嵌入，供下游策略/值头消费。
        """
        h = self.enc(x)
        for conv in self.convs:
            h = conv(h, nets)
        return h
