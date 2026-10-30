"""DSN 文本变换（纯函数，逐条移植自已验证的 merge 管线脚本）。

变换顺序在 loop.compile_pass_dsn 里按各 legacy 脚本的原始顺序固定，
字节级等价性由 eval_out/*/l0_equiv_check.py 对拍保证——改这里必须重跑对拍。
"""
from __future__ import annotations

import re


def del_nets(txt: str, names: list[str]) -> str:
    """整棵删除 (net <name> ...) 子树（括号配平）。平面网的 stitch 语义入口：
    从信号布线里移除平面网，其连通改由平面生成器负责。（signal_route_bon）"""
    for nm in names:
        while True:
            k = txt.find(f"(net {nm}")
            if k < 0:
                break
            d = 0
            i = k
            while i < len(txt):
                d += 1 if txt[i] == "(" else (-1 if txt[i] == ")" else 0)
                if d == 0:
                    break
                i += 1
            txt = txt[:k] + txt[i + 1:]
    return txt


def lock_layer(txt: str, layer: str) -> str:
    """层锁死：该层 DSN 类型 signal→power，FreeRouting 不再在其上布线。
    依赖 KiCad 导出器的确定性缩进格式（钉死版本下成立）。"""
    return txt.replace(f"(layer {layer}\n      (type signal)",
                       f"(layer {layer}\n      (type power)")


def keep_only_nets(txt: str, names: list[str]) -> str:
    """只让这几根网可布：其余网块只摘掉 `(pins …)`，网名声明留着。

    为什么不整棵删网：`(net X)` 块被别处引用（class/keepout/规则）时会变成
    "未知网"，FreeRouting 直接报错；而 pins 清空的网等于没有脚可连，布线器
    自然不布，DSN 仍然自洽。返回值用于"关键网先布"这一 pass。

    只在 `(network` 之后动：前面 `(structure …)` 里也有叫 net 的东西。
    """
    net = txt.find("(network")
    if net < 0:
        return txt
    keep = set(names)
    pat = re.compile(r'\(net\s+("(?:[^"\\]|\\.)*"|\S+)(?=[\s)])')
    out, pos = [txt[:net]], net
    for m in pat.finditer(txt, net):
        k = m.start()
        if k < pos:                          # 已被上一个块吃掉
            continue
        name = m.group(1).strip('"')
        d, i = 0, k
        while i < len(txt):
            d += 1 if txt[i] == "(" else (-1 if txt[i] == ")" else 0)
            if d == 0:
                break
            i += 1
        if name in keep:
            continue
        out.append(txt[pos:k])
        out.append(f"(net {m.group(1)})")
        pos = i + 1
    out.append(txt[pos:])
    return "".join(out)


def protect_wiring(txt: str) -> tuple[str, int]:
    """既有 wiring 全部 (type route)→(type protect)（固定为障碍不得重布），
    返回 (新文本, 保护条数)。（power_route / finish_pass）"""
    n = txt.count("(type route)")
    return txt.replace("(type route)", "(type protect)"), n


def shrink_npth_keepout(txt: str, clearance_um: int,
                        hole_radius_um: int = 1625,
                        exported_radius_um: int = 3750) -> str:
    """NPTH keepout 收缩：KiCad 导出器给 Ø3.25mm 孔发 3.75mm 半径全层禁区，
    几何上只需 孔半径 + clearance。（finish_pass）"""
    new_r = hole_radius_um + int(clearance_um)
    return re.sub(rf'\(keepout "" \(circle (\S+) {exported_radius_um} ',
                  f'(keepout "" (circle \\1 {new_r} ', txt)


def force_clearance(txt: str, clearance_um: int) -> str:
    """全部裸 (clearance N) 抬到给定值（export_dsn 已传参时的双保险）。（signal_route_bon）"""
    return re.sub(r"\(clearance\s+\d+\)", f"(clearance {clearance_um})", txt)
