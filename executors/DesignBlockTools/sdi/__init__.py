"""sdi —— 原理图/块卡引擎。

目录口径（2026-10-29 用户拍板：**块就直接放在 `DesignBlocks/` 里**，不要再套
`out/<批次>/sdi.kicad_blocks/<块>.kicad_block/` 三层脚手架）：

    PCBStudioV2/
      DesignBlocks/<块名>/          ← 所有块（裁出来的、手工的）都在这儿，一层
          <块名>.kicad_sch          原理图（块内坐标＝原页坐标）
          block.sdi.yaml            块卡（唯一手写权威）
          block.semantic.json       接口契约（产物）
          <块名>.table.json         逐 pin 连线表（产物）
          <块名>.json               旧口径（产物，兼容保留）
          _geom/                    板级几何产物
      DesignBlocks/_carve/<批次>/   ← 裁块过程账（report / recompose / 中间原理图）
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent     # DesignBlockTools/（工具树）
REPO = ROOT.parent                                 # PCBStudioV2/
BLOCKS = REPO / 'DesignBlocks'                     # 块的家

# 老位置：2026-10-29 之前块在 DesignBlockTools/PCBStudio.kicad_blocks/<名>.kicad_block/
# 只为了让老脚本/老规范还能跑；新东西一律用 BLOCKS。
_LEGACY = ROOT / 'PCBStudio.kicad_blocks'


def block_dir(name: str, root: Path | None = None) -> Path:
    """块目录。新口径 `DesignBlocks/<名>`，兼容老口径 `<名>.kicad_block`。"""
    base = Path(root) if root is not None else BLOCKS
    for cand in (base / name, base / f'{name}.kicad_block'):
        if cand.is_dir():
            return cand
    return base / name                      # 不存在时给新口径的路径，方便建


def has_block(name: str, root: Path | None = None) -> bool:
    if root is None:
        return any((d / name).is_dir() or (d / f'{name}.kicad_block').is_dir()
                   for d in (BLOCKS, _LEGACY))
    return block_dir(name, root).is_dir()


def find_block(name: str, root: Path | None = None) -> Path | None:
    """按新→老顺序找块；找不到返回 None（调用方决定是报错还是降级）。"""
    if root is not None:
        d = block_dir(name, root)
        return d if d.is_dir() else None
    for base in (BLOCKS, _LEGACY):
        d = block_dir(name, base)
        if d.is_dir():
            return d
    return None
