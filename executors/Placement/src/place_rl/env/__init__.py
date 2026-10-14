"""env：放置问题的统一表示（problem）与几何基元；RL env 后续在此包展开。"""

from .problem import Component, PlacementProblem, aabb, hpwl, overlap_area

__all__ = ["Component", "PlacementProblem", "aabb", "hpwl", "overlap_area"]
