"""place-rl：PCB 元器件自动放置（RL），KiCad 兼容。"""

from . import reuse  # noqa: F401  适配层先加载，便于统一兜底 import 错误

__version__ = "0.1.0"
__all__ = ["reuse", "__version__"]
