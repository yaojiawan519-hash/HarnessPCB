"""ERC 门禁 + 网表导出。schematicRule.md：ERC 零错误是原理图定稿的硬条件。"""
from __future__ import annotations

import re

# MCP run_erc 实际输出（2026-09 实测）：
#   ERC result: 5 violation(s)
#     Errors: 2  Warnings: 3  Info: 0
#   1. [error] Pin not connected @ (0.3302, 1.1049)
#   2. [error] 類型為 電源輸出 … 的接腳已連接 @ (0.9017, 0.3048)
# 逐条 [error]/[warning] 标记是最直接的信号，作为主计数；汇总行与
# kicad-cli .rpt 格式（** ERC messages: N Errors N Warnings N）作回退。
RE_MARKER_ERR = re.compile(r"\[error\]", re.I)
RE_MARKER_WARN = re.compile(r"\[warning\]", re.I)
RE_COLON = re.compile(r"errors?:\s*(\d+)\s+warnings?:\s*(\d+)", re.I)
RE_RPT = re.compile(r"ERC messages:\s*(\d+)\s+Errors?\s+(\d+)\s+Warnings?\s+(\d+)", re.I)


def run(client, sch: str) -> tuple[int, int, str]:
    """返回 (errors, warnings, 原文)。计数解析失败时 errors=-1 表示未知。

    ⚠ 旧版按 "N errors" 词序匹配的回退在 colon 格式上误判
    （"Errors: 2  Warnings: 3" 被读成 errors=0 warnings=2，假通过），
    已删——解析不出宁可 -1 触发门禁失败，也不报 0。"""
    t = client.tool("run_erc", {"schematicPath": sch}, check=False)
    n_e, n_w = len(RE_MARKER_ERR.findall(t)), len(RE_MARKER_WARN.findall(t))
    if n_e or n_w:
        return n_e, n_w, t
    m = RE_COLON.search(t)
    if m:
        return int(m.group(1)), int(m.group(2)), t
    m = RE_RPT.search(t)
    if m:
        return int(m.group(2)), int(m.group(3)), t
    return -1, -1, t


def gate(client, sch: str, strict: bool = True, log=lambda *a: None) -> tuple[int, int]:
    """ERC 门禁：strict 下 errors>0 抛异常终止链。"""
    n_err, n_warn, text = run(client, sch)
    log(f"  ERC: errors={n_err} warnings={n_warn}")
    if n_err < 0:
        log("  ⚠ ERC 计数未能解析，原文前 600 字：\n" + text[:600])
        if strict:
            raise RuntimeError("ERC 输出无法解析，门禁失败")
    elif strict and n_err > 0:
        raise RuntimeError(f"ERC {n_err} 错误（门禁未过）:\n{text[:800]}")
    return n_err, n_warn


def export_netlist(client, sch: str, out_path: str) -> str:
    return client.tool("export_netlist",
                       {"schematicPath": sch, "outputPath": out_path, "format": "KiCad"})
