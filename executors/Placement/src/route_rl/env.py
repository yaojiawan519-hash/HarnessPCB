"""执行环境与子进程封装：pcbnew 桥（_pcbnew_ops）/ kicad-cli DRC / FreeRouting。

四件套路径沿袭 HWS_* 环境变量门禁（见 Placement/CLAUDE.md）。P0 环境解耦
（2026-10-14）起解析三级：环境变量 > 机器配置文件 ~/.harnesspcb.json
（HWS_CONFIG 可改指；键名=环境变量名）> 标准路径探测。代码内不再有本机
专属默认值。协议锚在 KiCAD_Access/src/kicad_access/tools.py，此处为小拷贝
（三树三 venv 不能跨树 import；d2s mcp_client.py 同款）。FreeRouting 钉死
v2.3.0（v2.4.1 无头 -do 有 SES 写竞态），`-mt 1` 必带（否则结果不定）。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_KICAD_CLI_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/usr/bin/kicad-cli",
)
_JAVA_CANDIDATES = (
    "/opt/homebrew/opt/openjdk/bin/java",
    "/opt/homebrew/opt/openjdk@26/bin/java",
    "/usr/local/bin/java",
)


def _config_get(key: str) -> str:
    p = os.environ.get("HWS_CONFIG") or os.path.expanduser("~/.harnesspcb.json")
    try:
        with open(p) as f:
            v = json.load(f).get(key)
        return str(v) if v else ""
    except (OSError, ValueError):
        return ""


def _resolve(env: str, candidates: tuple[str, ...] = ()) -> str:
    """env > 机器配置文件 > 标准候选（首个存在者）。显式值不查存在性。"""
    v = os.environ.get(env) or _config_get(env)
    if v:
        return v
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def _kicad_py_from_cli(cli: str) -> str:
    """KiCad.app 标准包结构：…/MacOS/kicad-cli → Frameworks 内置 python。
    只认纯 `python3.N` 名（bundle 里另有 python3.9-intel64，字典序会压过
    python3.9 且在 Apple Silicon 上走 Rosetta——须排除）。"""
    fw = Path(cli).parent.parent / "Frameworks" / "Python.framework" / "Versions"
    if not fw.is_dir():
        return ""
    pys = [p for p in glob.glob(str(fw / "*" / "bin" / "python3.*"))
           if re.fullmatch(r"python3\.\d+", os.path.basename(p))]
    return os.path.realpath(max(pys)) if pys else ""


KPY = _resolve("HWS_KICAD_PY") or _kicad_py_from_cli(
    _resolve("HWS_KICAD_CLI", _KICAD_CLI_CANDIDATES))
JAVA = _resolve("HWS_JAVA", _JAVA_CANDIDATES)
FR_JAR = _resolve(
    "HWS_FREEROUTING_JAR",
    (str(Path.home() / ".kicad-mcp" / "freerouting.jar"),))
KICAD_CLI = _resolve("HWS_KICAD_CLI", _KICAD_CLI_CANDIDATES)
OPS = Path(__file__).resolve().parents[1] / "place_rl" / "_pcbnew_ops.py"

# 铜级 DRC 分类（须 0）：与 place_rl/_COPPER_KINDS 一致
COPPER_KINDS = frozenset({"shorting_items", "clearance", "solder_mask_bridge",
                          "hole_clearance", "track_width", "via_dangling",
                          "unconnected"})


def _require(tool: str, env_name: str) -> str:
    """三级解析落空守卫：显式报缺（含 doctor 指引），不拿空串进 subprocess。"""
    if not tool:
        raise RuntimeError(
            f"{env_name} 未解析（env/机器配置/标准探测均未命中）。"
            "跑 kca doctor 体检，或 export 对应环境变量 / 配 ~/.harnesspcb.json")
    return tool


def pcbnew(op: str, *a: str, timeout: int = 300) -> str:
    """跑 _pcbnew_ops 单操作。stdout 可能混入 SWIG 内存泄漏提示，按首 { 末 } 截取。"""
    r = subprocess.run([_require(KPY, "HWS_KICAD_PY"), str(OPS), op, *a],
                       capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"pcbnew {op} fail:\n{r.stderr[-1500:]}")
    o = r.stdout
    i, j = o.find("{"), o.rfind("}")
    return o[i:j + 1] if i >= 0 and j > i else o


def pcbnew_json(op: str, *a: str, timeout: int = 300) -> dict:
    txt = pcbnew(op, *a, timeout=timeout)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {"raw": txt[:2000]}


def freerouting(dsn: Path, ses: Path, max_passes: int = 60,
                timeout: int | None = None) -> subprocess.CompletedProcess:
    """FreeRouting v2.3.0 无头单跑。timeout 缺省读 RRL_FR_TIMEOUT（默认 900s）——
    密板（6 层 BGA / 80×80 LQFP 870 网）单线程 -mt 1 可超 900s，跑大板时上调。"""
    if timeout is None:
        timeout = int(os.environ.get("RRL_FR_TIMEOUT", "900"))
    return subprocess.run(
        [_require(JAVA, "HWS_JAVA"), "-Djava.awt.headless=true", "-jar",
         _require(FR_JAR, "HWS_FREEROUTING_JAR"),
         "-de", str(dsn), "-do", str(ses), "-mp", str(max_passes), "-mt", "1"],
        capture_output=True, text=True, timeout=timeout)


def kicad_drc(pcb: Path, refill: bool = False, timeout: int = 600) -> dict:
    """引擎 DRC。refill=True 时 --refill-zones --save-board（持久化铜皮后再判，
    否则平面类指标失真）；refill 判完把重填后的板拷回原路径。

    ⚠️ 伴生工程隔离：kicad-cli 加载板时会读同目录同名 .kicad_pro 的
    net_settings——demo 工程的 netclass 判距（pic 0.25/0.28、interf 0.254）
    实测把板规 0.2 下的 3 条判成 33。pcbnew 的 import_ses/cleanup 保存时
    恰好会在轮产物旁写工程文件，故一律拷板到空临时目录单板判决：判距
    永远取板文件自身 setup（0.2mm 板规），与事后人工单板复判一致。"""
    pcb = Path(pcb)
    rep = Path(tempfile.mkstemp(suffix=".json")[1])
    box = Path(tempfile.mkdtemp(prefix="rrl_drc_"))       # 空目录 → 无伴生工程
    judge = box / pcb.name
    shutil.copy2(pcb, judge)
    cmd = [_require(KICAD_CLI, "HWS_KICAD_CLI"), "pcb", "drc", "--format", "json"]
    if refill:
        cmd += ["--refill-zones", "--save-board"]
    try:
        subprocess.run(cmd + ["-o", str(rep), str(judge)], capture_output=True,
                       text=True, check=True, timeout=timeout)
        if refill:
            shutil.copy2(judge, pcb)      # 重填结果带回原路径（board_out 出厂）
        return json.loads(rep.read_text())
    finally:
        rep.unlink(missing_ok=True)
        shutil.rmtree(box, ignore_errors=True)


def _ver(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip().splitlines()[0][:120]
    except Exception as e:  # 版本探测失败不阻断运行
        return f"unavailable: {e}"


def versions() -> dict:
    """三方冻结基线（jar / Java / KiCad）；任一变更须重生成数据集（S1 纪律）。"""
    sha = ""
    try:
        sha = hashlib.sha256(Path(FR_JAR).read_bytes()).hexdigest()[:16]
    except OSError:
        pass
    return {
        "jar_sha256_16": sha,
        "java": _ver([JAVA, "-version"]),
        "kicad": _ver([KICAD_CLI, "version"]),
        "python": platform.python_version(),
    }
