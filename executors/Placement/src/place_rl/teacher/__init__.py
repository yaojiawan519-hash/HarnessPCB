"""teacher：SA 自蒸馏教师（行为克隆目标 + 评估基线）。"""

from .sa import SAConfig, SAResult, solve

__all__ = ["SAConfig", "SAResult", "solve"]
