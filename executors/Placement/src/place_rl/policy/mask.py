"""Pos Mask——DesignDocument §3.3 掩码动作 / M4。

把画布离散成 ``grid_mm`` 网格，用 2D 卷积（全 1 核）在 O(1) 内判断某个中心格
放置 w×h 外框后是否与已放件重叠，并夹取板内范围，产出「合法中心格」布尔掩码。
Wire Mask（net 包围盒邻近收窄）在 M4 后续接上；此处先保证无重叠 + 不越界。
"""
from __future__ import annotations

import math

import torch
import torch.nn.functional as F

__all__ = ["GridPosMask"]


class GridPosMask:
    def __init__(self, W: float, H: float, grid_mm: float):
        self.W, self.H, self.grid_mm = W, H, grid_mm
        self.nx = max(1, int(math.ceil(W / grid_mm)))
        self.ny = max(1, int(math.ceil(H / grid_mm)))
        self.occ = torch.zeros(self.ny, self.nx, dtype=torch.float32)

    def mark(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """把外框 (x0,y0)-(x1,y1) 覆盖的格子标为已占。

        用 ``math.floor``（而非 ``int`` 截断）与 :meth:`legal_mask` 的核公式精确
        对齐：中心格 ``col`` 的元件覆盖 ``col+O_w .. col+O_w+K_w-1``，其中
        ``O_w = ⌊½−w/2g⌋``。宽件（w>g）左缘略越出 0 时，``int`` 会把负索引截向
        0 导致少标一格，正是 grid_mm=1.27 下 mask 漏标、解码出现重叠的根因。
        """
        c0 = min(self.nx - 1, max(0, math.floor(x0 / self.grid_mm)))
        c1 = min(self.nx - 1, max(0, math.floor(x1 / self.grid_mm)))
        r0 = min(self.ny - 1, max(0, math.floor(y0 / self.grid_mm)))
        r1 = min(self.ny - 1, max(0, math.floor(y1 / self.grid_mm)))
        self.occ[r0:r1 + 1, c0:c1 + 1] = 1.0

    def legal_mask(self, w: float, h: float) -> torch.Tensor:
        """中心格合法掩码 [ny, nx]：外框无重叠且整体在板内。

        与 mark() 精确一致：中心格 (row, col) 的元件外框覆盖的格列为
        ``col + O_w .. col + O_w + K_w - 1``，其中 ``K_w = ⌊½+w/2g⌋−⌊½−w/2g⌋+1``、
        ``O_w = ⌊½−w/2g⌋``（w 恰为网格整数倍时 straddle 一格，K=wc+1）。
        用一次 F.pad + 有效卷积在 O(1) 内判重。
        """
        W2 = w / (2 * self.grid_mm)
        H2 = h / (2 * self.grid_mm)
        K_w = math.floor(0.5 + W2) - math.floor(0.5 - W2) + 1
        O_w = math.floor(0.5 - W2)
        K_h = math.floor(0.5 + H2) - math.floor(0.5 - H2) + 1
        O_h = math.floor(0.5 - H2)
        pad_l, pad_r = -O_w, K_w - 1 + O_w
        pad_t, pad_b = -O_h, K_h - 1 + O_h
        occ = F.pad(self.occ.view(1, 1, self.ny, self.nx),
                    (pad_l, pad_r, pad_t, pad_b))
        cnt = F.conv2d(occ, torch.ones(1, 1, K_h, K_w))
        free = cnt[0, 0] == 0.0
        # 板内：中心格 (col+0.5)·g 放 w×h 外框后整体须落在 [0,W]×[0,H]。
        # 逐格精确判定外框两端，而非 floor(w/2g) 近似（后者会让左缘略越出 0）。
        g = self.grid_mm
        cols = torch.arange(self.nx, dtype=torch.float32)
        rows = torch.arange(self.ny, dtype=torch.float32)
        cx = (cols + 0.5) * g
        cy = (rows + 0.5) * g
        inb_x = ((cx - w / 2) >= 0.0) & ((cx + w / 2) <= self.W)
        inb_y = ((cy - h / 2) >= 0.0) & ((cy + h / 2) <= self.H)
        inb = inb_y[:, None] & inb_x[None, :]
        return free & inb
