"""写后回读对账 + 连接性指纹（借鉴 easyeda-agent 的 schguard / copy-check 方法学）。

ERC 只查"规则合规"，本模块查"连的就是设计表要的"：
  设计意图（connect_all 解析出的 {(ref, pin_number): net}）
  vs 网表读回（export_netlist 产物，KiCad XML 网表解析）
逐引脚零容忍对账，三个方向：
  missing   该连未连（标签放置失败 / 落点偏移，MCP 返回 ok ≠ 落地）
  extra     多余连接（误贴到别的引脚 / 无名网 Net-(…) —— 好板不该有无名网）
  mismatch  同引脚不同网
NC 引脚在 KiCad XML 网表中以 unconnected-(…) 网出现，属声明意图，过滤不计。

TopologyHash：排序后 "ref|pin|net" 行的 sha256——连接性指纹，写入
sch_report / chain_report，跨版本对比一眼看出连接变了没。
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET


def norm_net(name: str) -> str:
    """网名归一：去 KiCad 根图纸路径前缀 '/'（局部标签网是 /+3V3，电源符号网是 +3V3）。"""
    n = (name or "").strip()
    return n[1:] if n.startswith("/") else n


def _is_nc_net(name: str) -> bool:
    return norm_net(name).startswith("unconnected-(")


def parse_netlist(path: str) -> dict[tuple[str, str], str]:
    """KiCad XML 网表 → {(ref, pin): net}。NC 网整体跳过；其余（含无名网）保留，
    由 reconcile 判 extra——无名网出现即说明存在设计表之外的连线。"""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        raise RuntimeError(f"网表解析失败 {path}: {e}") from e
    out: dict[tuple[str, str], str] = {}
    for net in root.iter("net"):
        nm = net.get("name", "")
        if _is_nc_net(nm):
            continue
        for node in net.findall("node"):
            out[(node.get("ref"), node.get("pin"))] = norm_net(nm)
    if not out:
        raise RuntimeError(f"网表为空或无连接节点: {path}")
    return out


def topology_hash(conn: dict[tuple[str, str], str]) -> str:
    """连接性指纹：与引脚顺序 / 网表导出顺序无关，只随连接集合变化。"""
    lines = sorted(f"{r}|{p}|{n}" for (r, p), n in conn.items())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def reconcile(expected: dict[tuple[str, str], str], netlist_path: str,
              part_refs: set[str], log=lambda *a: None) -> dict:
    """expected: connect_all/place_pwr_flags 返回的 {(ref, pin_number): net}（意图）。
    part_refs: 设计表 parts 的 ref 集合——网表里属于设计器件的引脚逐一核对；
    之外的（PWR_FLAG 等）作为辅助件单独核对落网。"""
    actual_all = parse_netlist(netlist_path)
    exp = {k: v for k, v in expected.items() if k[0] in part_refs}
    act = {k: v for k, v in actual_all.items() if k[0] in part_refs}

    missing_k = exp.keys() - act.keys()
    extra_k = act.keys() - exp.keys()
    mismatch = {f"{r}.{p}": {"expected": exp[(r, p)], "actual": act[(r, p)]}
                for (r, p) in exp.keys() & act.keys() if exp[(r, p)] != act[(r, p)]}

    # 辅助件（PWR_FLAG）：ref 不在 parts，但声明的脚必须在声明的网上
    aux_bad = []
    for (r, p), want in sorted(expected.items()):
        if r in part_refs:
            continue
        got = actual_all.get((r, p))
        if got != want:
            aux_bad.append(f"{r}.{p} 期望 {want} 实际 {got if got else '(缺失)'}")

    missing = sorted(f"{r}.{p}→{exp[(r, p)]}" for (r, p) in missing_k)
    extra = sorted(f"{r}.{p}→{act[(r, p)]}" for (r, p) in extra_k)
    v = {
        "ok": not (missing or extra or mismatch or aux_bad),
        "expected_pins": len(exp), "actual_pins": len(act),
        "missing": missing[:100], "extra": extra[:100],
        "mismatch": dict(sorted(mismatch.items())[:100]),
        "aux": aux_bad[:100],
        "topology_hash_expected": topology_hash(exp),
        "topology_hash_actual": topology_hash(act),
    }
    log(f"  回读对账: expected={v['expected_pins']} actual={v['actual_pins']} "
        f"missing={len(missing)} extra={len(extra)} mismatch={len(mismatch)} "
        f"hash={v['topology_hash_actual'][:12]}…")
    return v


def format_findings(v: dict) -> str:
    """失败时的人读摘要（全部明细已在对账 dict 里）。"""
    parts = []
    if v["missing"]:
        parts.append("该连未连 %d: %s" % (len(v["missing"]), ", ".join(v["missing"][:8])))
    if v["extra"]:
        parts.append("多余连接 %d: %s" % (len(v["extra"]), ", ".join(v["extra"][:8])))
    if v["mismatch"]:
        head = ", ".join(f"{k} 期望 {m['expected']} 实际 {m['actual']}"
                         for k, m in list(v["mismatch"].items())[:8])
        parts.append(f"连错网 {len(v['mismatch'])}: {head}")
    if v["aux"]:
        parts.append("辅助件落网异常 %d: %s" % (len(v["aux"]), ", ".join(v["aux"][:8])))
    return "\n  ".join(parts) or "（无差异？——不应走到这里）"


def gate(expected: dict[tuple[str, str], str], netlist_path: str,
         part_refs: set[str], log=lambda *a: None) -> dict:
    """对账门禁：不一致即 raise（MCP 写入 ok ≠ 落地，一切以读回为准）。"""
    v = reconcile(expected, netlist_path, part_refs, log=log)
    if not v["ok"]:
        raise RuntimeError(
            f"回读对账失败（设计表意图 vs 网表读回，期望 {v['expected_pins']} 脚 / "
            f"实际 {v['actual_pins']} 脚）:\n  " + format_findings(v))
    return v
