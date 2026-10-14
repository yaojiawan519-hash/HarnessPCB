"""外部工具链路径解析（M0；P0 环境解耦 2026-10-14）。

解析三级：环境变量（HWS_*/D2S_*）> 机器配置文件（~/.harnesspcb.json，
HWS_CONFIG 可改指）> 标准路径探测（PATH / 常见安装位）。代码内不再有
本机专属默认值——母舰的非标准安装位（外置盘 KiCad 等）写进机器配置文件，
配置文件不进任何仓库/发行包（每台机器自己的环境声明）。

同款解析逻辑在另两处各有一份小拷贝（三树三 venv、d2s 是 stdlib-only，
不能跨树 import）：Placement/src/route_rl/env.py、
SchematicDesign/design2schematic/mcp_client.py。改协议三处同步。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 机器配置文件（P0）：{ "<环境变量名>": "<路径>", ... }
# ---------------------------------------------------------------------------
CONFIG_ENV = "HWS_CONFIG"  # 改指配置文件位置（测试用）
CONFIG_FILENAME = ".harnesspcb.json"

# 标准候选（陌生机器常见安装位，按优先序；禁止本机专属路径）
KICAD_CLI_CANDIDATES: tuple[str, ...] = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/usr/bin/kicad-cli",
)
JAVA_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/opt/openjdk/bin/java",
    "/opt/homebrew/opt/openjdk@26/bin/java",
    "/usr/local/bin/java",
)
NODE_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
)
# 唯一"家相对"默认：jar 无系统安装位，约定放 ~/.kicad-mcp/
# ⚠️ 钉 v2.3.0：v2.4.1 无头 -do 落盘竞态——路由全通但 SES 概率性 0 字节
# （2026-09-10 实证 8 跑 2 过；v2.4.1 备份在同目录 freerouting-2.4.1.jar）
DEFAULT_FREEROUTING_JAR = Path.home() / ".kicad-mcp" / "freerouting.jar"


@dataclass
class ToolStatus:
    name: str
    path: Path | None
    ok: bool
    detail: str
    hint: str = ""  # 降级提示：缺它时哪些功能不可用、怎么补救


def config_path() -> Path:
    v = os.environ.get(CONFIG_ENV)
    if v:
        return Path(v)
    return Path.home() / CONFIG_FILENAME


def load_config() -> dict:
    """读机器配置文件；不存在/坏 JSON 一律空 dict（配置缺失=标准探测兜底）。"""
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def resolve(
    env: str, which_name: str = "", candidates: tuple[str, ...] = ()
) -> tuple[Path | None, str]:
    """三级解析：env > 机器配置文件（键名=环境变量名）> 标准探测。

    显式指定的路径不查存在性（status 函数负责呈现与探测，env 指错路要
    红出来而不是静默滑到候选）；候选取首个存在者。返回 (路径, 来源标签)，
    无命中返回 (None, "missing")。
    """
    v = os.environ.get(env)
    if v:
        return Path(v), "env"
    v = load_config().get(env)
    if v:
        return Path(v), "config"
    if which_name:
        w = shutil.which(which_name)
        if w:
            return Path(w), "standard(PATH)"
    for c in candidates:
        if Path(c).exists():
            return Path(c), "standard"
    return None, "missing"


# ---------------------------------------------------------------------------
# kicad-cli / kicad-python（后者从前者推导）
# ---------------------------------------------------------------------------
def _kicad_py_from_cli(cli: Path) -> Path | None:
    """KiCad.app 标准包结构：…/Contents/MacOS/kicad-cli → Frameworks 内置
    python（KiCad 10 内嵌 3.9；取 Versions 下最高 python3.N）。
    只认纯 `python3.N` 名（bundle 里另有 python3.9-intel64，字典序会压过
    python3.9 且在 Apple Silicon 上走 Rosetta——须排除）。"""
    fw = cli.parent.parent / "Frameworks" / "Python.framework" / "Versions"
    if not fw.is_dir():
        return None  # Linux 包管理安装无 bundle 结构
    import re
    pys = [p for p in glob.glob(str(fw / "*" / "bin" / "python3.*"))
           if re.fullmatch(r"python3\.\d+", os.path.basename(p))]
    return Path(max(pys)).resolve() if pys else None


def resolved_kicad_cli() -> Path | None:
    """已解析且存在的 kicad-cli 路径（供 fplib 等推导 app 内资源目录）。"""
    path, _ = resolve("HWS_KICAD_CLI", "kicad-cli", KICAD_CLI_CANDIDATES)
    return path if path is not None and path.exists() else None


def kicad_cli_status() -> ToolStatus:
    """kicad-cli：找不到属预期降级（纯文本功能不受影响）。"""
    path, src = resolve("HWS_KICAD_CLI", "kicad-cli", KICAD_CLI_CANDIDATES)
    if path is None or not path.exists():
        return ToolStatus(
            "kicad-cli",
            path,
            False,
            f"未找到（来源 {src}）",
            "DRC/出图/Gerber 不可用；ingest/place 等纯文本功能不受影响。"
            "export HWS_KICAD_CLI=<kicad-cli 路径>，或在 "
            f"{config_path()} 写 {{\"HWS_KICAD_CLI\": \"...\"}}",
        )
    try:
        ver = subprocess.run(
            [str(path), "version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        ver = "版本查询失败"
    return ToolStatus("kicad-cli", path, True, f"[{src}] {ver}")


def kicad_python_status() -> ToolStatus:
    """KiCad 内置 Python（pcbnew 模块）：KiCad 10 起 DSN 导出/SES 回读的唯一通道。"""
    path, src = resolve("HWS_KICAD_PY")
    if path is None:
        cli, cli_src = resolve("HWS_KICAD_CLI", "kicad-cli", KICAD_CLI_CANDIDATES)
        derived = _kicad_py_from_cli(cli) if cli else None
        if derived and derived.exists():
            path, src = derived, f"derived(cli:{cli_src})"
    if path is None or not path.exists():
        return ToolStatus(
            "kicad-python",
            path,
            False,
            f"未找到（来源 {src}）",
            "DSN 导出/SES 回读不可用（KiCad 10 kicad-cli 已无 dsn 子命令），"
            "L2 freerouting 链路断。export HWS_KICAD_PY=<python3.9> 或在 "
            f"{config_path()} 写 \"HWS_KICAD_PY\"",
        )
    try:
        r = subprocess.run(
            [str(path), "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ver = (r.stdout or "").strip() or f"pcbnew 导入失败: {(r.stderr or '').strip()[:80]}"
    except (OSError, subprocess.TimeoutExpired):
        ver = "pcbnew 导入失败"
    ok = bool(ver) and "pcbnew 导入失败" not in ver  # 能出版本号即模块可用
    return ToolStatus(
        "kicad-python", path, ok, f"[{src}] {ver}",
        "" if ok else "pcbnew 模块不可用，DSN/SES 链路断",
    )


# ---------------------------------------------------------------------------
# java / freerouting
# ---------------------------------------------------------------------------
def java_status() -> ToolStatus:
    """freerouting 需要 Java 25+（openjdk@26；⚠️ openjdk@21 跑不动 v2.4.1）。"""
    path, src = resolve("HWS_JAVA", "java", JAVA_CANDIDATES)
    if path is None:
        return ToolStatus("java", None, False, "未找到", "L2 Freerouting 布线不可用")
    if not path.exists():
        return ToolStatus(
            "java", path, False, f"{path} 不存在", "L2 Freerouting 布线不可用"
        )
    r = subprocess.run(
        [str(path), "-version"], capture_output=True, text=True, timeout=15
    )
    first = (r.stderr or r.stdout).strip().splitlines()
    ver = first[0] if first else "版本输出为空"
    return ToolStatus("java", path, True, f"[{src}] {ver}")


def freerouting_status() -> ToolStatus:
    jar, src = resolve("HWS_FREEROUTING_JAR")
    if jar is None:  # env/config 未指 → 约定默认位
        jar, src = DEFAULT_FREEROUTING_JAR, "standard(~/.kicad-mcp)"
    if not jar.exists():
        return ToolStatus(
            "freerouting.jar", None, False, f"未找到（{jar}，来源 {src}）",
            "L2 布线不可用。从 freerouting.org 取 v2.3.0 放该位置，"
            "或 export HWS_FREEROUTING_JAR=<jar 路径>",
        )
    size_mb = jar.stat().st_size / 1e6
    return ToolStatus("freerouting.jar", jar, True, f"[{src}] {size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# 全链附加检查（P0：d2s 原理图链的 node + MCP server + 配置文件本身）
# ---------------------------------------------------------------------------
def node_status() -> ToolStatus:
    """d2s 原理图链需要 Node 跑 KiCAD-MCP-Server。"""
    path, src = resolve("D2S_NODE", "node", NODE_CANDIDATES)
    if path is None or not path.exists():
        return ToolStatus(
            "node", path, False, f"未找到（来源 {src}）",
            "d2s 原理图→板链路不可用（KiCAD-MCP-Server 跑不起来）；"
            "kca 自身不受影响。安装 Node 或 export D2S_NODE=<node 路径>",
        )
    try:
        ver = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        ver = "版本查询失败"
    return ToolStatus("node", path, True, f"[{src}] {ver}")


def d2s_mcp_status() -> ToolStatus:
    """KiCAD-MCP-Server 的 dist/index.js。无标准安装位——必须 env/config 指。"""
    path, src = resolve("D2S_MCP")
    if path is None:
        return ToolStatus(
            "d2s-mcp-server", None, False, "未指定（无标准位置）",
            "d2s 原理图链需要。export D2S_MCP=<KiCAD-MCP-Server/dist/index.js>"
            f" 或在 {config_path()} 写 \"D2S_MCP\"",
        )
    if not path.exists():
        return ToolStatus(
            "d2s-mcp-server", path, False, f"{path} 不存在（来源 {src}）",
            "d2s 原理图链需要。检查 KiCAD-MCP-Server 构建产物",
        )
    return ToolStatus("d2s-mcp-server", path, True, f"[{src}] {path.name}")


def config_report() -> list[tuple[str, str]]:
    """配置文件体检：路径、在否、里面配了哪些键（值含本机路径属预期）。"""
    p = config_path()
    rows: list[tuple[str, str]] = []
    if p.exists():
        keys = ", ".join(sorted(load_config().keys())) or "（空）"
        rows.append((str(p), f"存在；键：{keys}"))
    else:
        rows.append((str(p), "不存在（全部走 env/标准探测，属正常）"))
    return rows


def all_tools() -> list[ToolStatus]:
    return [
        kicad_cli_status(),
        kicad_python_status(),
        java_status(),
        freerouting_status(),
    ]


def full_chain_tools() -> list[ToolStatus]:
    """全链附加项（doctor 的「d2s 链」段；kca 自身不强依赖）。"""
    return [node_status(), d2s_mcp_status()]
