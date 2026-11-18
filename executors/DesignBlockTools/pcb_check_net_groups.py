#!/usr/bin/env python3
"""网组布通率查询（2026-11-17）——引擎 report per_net 按网组统计布通率。

用法（普通 python3）:
  python3 pcb_check_net_groups.py <report.json> [--groups "MII:PF|PB|PC|PD|PE,JTAG:JTAG,DIFF:TD_|RD_|RX|TX|USB0,CAN:CAN|U17"]

内置默认网组（HPM5E00）:
  MII    : PF/PB/PC/PD/PE 开头的 GPIO 网（PHY 数据线）
  JTAG   : JTAG_*
  差分   : TD_/RD_/RX+/TX+/USB0
  时钟   : XI/XO/XTAL/REFCLK/RXC/TXC/PA9
  CAN    : CAN/STB/TXD/RXD(U17)

输出：每组 布通数/总数（比例）+ 未布通网点名。exit 1 若任何组 <100%
且 --gate 传入。供 stress_cycle/复盘用：某组布通率低 = 那组有结构性问题。
"""
from __future__ import annotations

import argparse
import json
import sys

DEFAULT_GROUPS = {
    "MII":   ("PF", "PB", "PC", "PD", "PE"),
    "JTAG":  ("JTAG",),
    "差分":  ("TD_", "RD_", "RX+", "RX-", "TX+", "TX-", "USB0"),
    "时钟":  ("XI", "XO", "XTAL", "REFCLK", "RXC", "TXC", "PA9"),
    "CAN":   ("CANH", "CANL", "STB", "U17"),
    "电源":  ("+3", "+5", "VREF", "VANA", "SOC", "DCDC", "ECAT_3"),
}


def base(net: str) -> str:
    return net.split("/")[-1]


def match(net: str, keys) -> bool:
    b = base(net)
    return any(k in b for k in keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--gate", action="store_true", help="任何组<100% 则 exit 1")
    a = ap.parse_args()

    d = json.load(open(a.report))
    pn = d.get("per_net", {})
    rows = []
    n_bad = 0
    print(f"== 网组布通率 · {a.report.split('/')[-1]} ==")
    for gname, keys in DEFAULT_GROUPS.items():
        nets = {n: v for n, v in pn.items() if match(n, keys)}
        if not nets:
            continue
        ok = [n for n, v in nets.items() if v.get("connected")]
        bad = [n for n, v in nets.items() if not v.get("connected")]
        rate = len(ok) / len(nets)
        mark = "ok  " if rate == 1.0 else "FAIL"
        if rate < 1.0:
            n_bad += 1
        print(f"\n[{mark}] {gname}: {len(ok)}/{len(nets)} ({rate:.0%})")
        for n in sorted(bad)[:10]:
            print(f"    ✗ {base(n)}")
        if len(bad) > 10:
            print(f"    …另有 {len(bad)-10} 条")
    print(f"\n== {'全组 100%' if n_bad == 0 else f'{n_bad} 组未满'} ==")
    return 1 if (a.gate and n_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
