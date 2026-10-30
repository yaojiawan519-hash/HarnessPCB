"""CLI 入口。

用法示例（4 层板收尾 pass 冒烟）：
    PYTHONPATH=src .venv/bin/python -m route_rl \
        --board <板>.kicad_pcb \
        --stackup <叠层>.json \
        --out /tmp/rrl_smoke --passes finish --clearances finish=215 --mp 60
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .loop import run_pipeline
from .stackup import Stackup


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="route_rl",
        description="R0 六步循环：L0 pass 计划 → FreeRouting → 引擎 DRC → 三元组")
    ap.add_argument("--board", required=True, type=Path, help="输入 .kicad_pcb")
    ap.add_argument("--stackup", required=True, type=Path,
                    help="叠层声明 JSON（L0 输入）")
    ap.add_argument("--out", required=True, type=Path, help="运行目录")
    ap.add_argument("--passes", default="",
                    help="逗分 pass 子集（signal,power,finish,planes）；空=全计划")
    ap.add_argument("--clearances", default="",
                    help="分号分隔的 pass=clr1,clr2…，如 finish=215,210")
    ap.add_argument("--mp", type=int, default=60, help="FreeRouting max-passes")
    ap.add_argument("--attempts", type=int, default=1,
                    help="每动作复跑次数（best-of-N）")
    ap.add_argument("--engine", choices=("freerouting", "negotiated"),
                    default="freerouting",
                    help="执行器：negotiated=kicad_access 协商引擎子进程"
                         "直出板（①③④ 替换，⑤ DRC 考官不动）")
    ap.add_argument("--priority-nets", default="",
                    help="逗分网名：这几根网**先单独布一轮**，后续 pass 把它们"
                         "当障碍（时钟/晶振这类短、不许打孔的关键网）")
    ap.add_argument("--fan-early", action="store_true",
                    help="平面网扇出：细间距引脚场里的脚**布线前**就占位（keepout）。"
                         "默认等 priority 之后再规划（省走廊）；不传时行为与旧版一致")
    ap.add_argument("--fan-early-only", default="",
                    help="逗分 位号-脚号（如 U1-9,U1-8）：只给这几只平面网脚在"
                         "**布线前**占位。用法 = 先布一遍看 result.json 的 "
                         "fan_nospot，再只救那几只重布（keepout 越少越不挡线）")
    a = ap.parse_args()

    st = Stackup.from_json(a.stackup)
    passes = [p for p in a.passes.split(",") if p] or None
    cl: dict[str, list[int]] = {}
    for kv in a.clearances.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            cl[k] = [int(x) for x in v.split(",") if x]

    res = run_pipeline(a.board, st, a.out, passes, cl or None, a.mp,
                       a.attempts, a.engine,
                       priority_nets=[n for n in a.priority_nets.split(",") if n],
                       fan_early=a.fan_early,
                       fan_early_only=[x for x in a.fan_early_only.split(",") if x])
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
