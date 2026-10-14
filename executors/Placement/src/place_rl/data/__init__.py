"""data：放置问题来源——合成生成器（训练主力）与真实板转换（留出集）。"""

from .synthetic import GenConfig, generate
from .real import problem_from_ir

__all__ = ["GenConfig", "generate", "problem_from_ir"]
