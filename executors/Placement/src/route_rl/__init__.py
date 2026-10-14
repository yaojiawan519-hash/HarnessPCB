"""route_rl：RL 布线工具（规划见 documents/RoutingRL_多层板实施规划.md）。

R0 = 埋点 + 评测台 + L0 叠层语义层：
- stackup.py   L0 叠层声明 → pass 计划（merge 法内化）
- dsn_edit.py  DSN 文本变换（纯函数，逐条来自已验证脚本）
- loop.py      六步循环 + Controller 接口（S1 动作源预留）+ 三元组记录
- metrics.py   状态/结果特征（全部取自引擎 DRC）
- planes.py    平面生成器（merge 终步）
- env.py       四件套路径与子进程封装
"""
