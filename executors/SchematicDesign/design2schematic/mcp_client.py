"""KiCAD-MCP-Server 的 Python (stdlib-only) JSON-RPC/stdio 客户端。

从 Codex 版 gen_schematic.mjs 提炼：newline 分帧 + pending map + initialize 握手，
并用轮询版 wait_for_backend() 替代旧脚本的盲 sleep(8000)。
不引入任何第三方依赖（subprocess/threading/json）。

调用审计（借鉴 easyeda-agent 的 per-day JSONL）：每次 call() 追加一行到
~/.d2s/audit/<YYYY-MM-DD>.jsonl（D2S_AUDIT_DIR 覆盖目录、D2S_AUDIT_OFF=1 关闭），
记录方法/参数/结果/耗时——问题复盘与重放回归的原始证据。审计写失败只吞不抛，
绝不影响主流程。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from queue import Empty, Queue

# ---- 工具路径三级解析（P0 环境解耦 2026-10-14）----
# env（D2S_*）> 机器配置文件 ~/.harnesspcb.json（HWS_CONFIG 可改指；键名=
# 环境变量名，不进任何仓库/发行包）> 标准探测（PATH / 常见安装位）。
# 协议锚在 KiCAD_Access/src/kicad_access/tools.py，此处为小拷贝（本目录
# stdlib-only 且跨树三 venv，不能 import）；route_rl/env.py 同款。
import glob as _glob
import shutil as _shutil

_NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node")
_KICAD_CLI_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/usr/bin/kicad-cli",
)


def _config_get(key: str) -> str:
    p = os.environ.get("HWS_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".harnesspcb.json")
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
        if os.path.exists(c):
            return c
    return ""


def resolve_node() -> str:
    return _resolve("D2S_NODE", _NODE_CANDIDATES) or _shutil.which("node") or ""


def resolve_mcp_js() -> str:
    """KiCAD-MCP-Server 的 dist/index.js——无标准安装位，必须 env/config 指。"""
    return _resolve("D2S_MCP")


def resolve_kicad_cli() -> str:
    """d2s 专属键 D2S_KICAD_CLI 优先，其后与全链共用 HWS_KICAD_CLI
    （同一台 KiCad 在机器配置文件里只声明一次）。"""
    v = _resolve("D2S_KICAD_CLI") or _resolve("HWS_KICAD_CLI")
    if v:
        return v
    return _shutil.which("kicad-cli") or ""


def resolve_kicad_ext() -> str:
    """KiCad.app 的 Contents 目录：D2S_KICAD_EXT / HWS_KICAD_EXT 显式给，
    否则从已解析的 kicad-cli 反推（…/Contents/MacOS/kicad-cli → …/Contents）。"""
    v = _resolve("D2S_KICAD_EXT") or _resolve("HWS_KICAD_EXT")
    if v:
        return v
    cli = resolve_kicad_cli()
    if cli and os.path.basename(os.path.dirname(cli)) == "MacOS":
        return os.path.dirname(os.path.dirname(cli))
    return ""


def resolve_kicad_py() -> str:
    """app 内置 python（pour 用）：从 resolve_kicad_ext() 推导，取最高 3.x。
    只认纯 `python3.N` 名（KiCad 10 bundle 里另有 python3.9-intel64，字典序
    会压过 python3.9，在 Apple Silicon 上走 Rosetta——须排除）。"""
    ext = resolve_kicad_ext()
    if not ext:
        return ""
    pys = [p for p in _glob.glob(os.path.join(
                ext, "Frameworks", "Python.framework", "Versions", "*", "bin",
                "python3.*"))
           if re.fullmatch(r"python3\.\d+", os.path.basename(p))]
    return os.path.realpath(max(pys)) if pys else ""


# ---------- 调用审计 ----------
def _audit_dir() -> str:
    d = os.environ.get("D2S_AUDIT_DIR") or os.path.join(
        os.path.expanduser("~"), ".d2s", "audit")
    return d


def _audit(method: str, params: dict, rec: dict) -> None:
    """追加一行审计 JSONL。任何异常静默吞掉——审计绝不破坏主流程。"""
    if os.environ.get("D2S_AUDIT_OFF"):
        return
    try:
        d = _audit_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, datetime.date.today().isoformat() + ".jsonl")
        args = params.get("arguments") if method == "tools/call" else params
        line = {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "method": method,
            "tool": params.get("name") if method == "tools/call" else None,
            "args": _snip(json.dumps(args, ensure_ascii=False), 2000),
            **rec,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _snip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit}B)"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(
        self,
        mcp_js: str | None = None,
        node_bin: str | None = None,
        kicad_ext: str | None = None,
        log=lambda *a: None,
    ):
        self.mcp_js = mcp_js or resolve_mcp_js()
        self.node_bin = node_bin or resolve_node()
        self.ext = kicad_ext or resolve_kicad_ext()
        if not self.mcp_js:
            raise MCPError(
                "D2S_MCP 未解析（KiCAD-MCP-Server 的 dist/index.js 无标准位置，"
                "必须 env/机器配置文件指定；跑 kca doctor 看体检）")
        if not self.node_bin:
            raise MCPError("D2S_NODE 未解析（需 Node 跑 MCP server；kca doctor）")
        if not self.ext:
            raise MCPError(
                "D2S_KICAD_EXT 未解析（找不到 KiCad.app；export D2S_KICAD_EXT="
                "<…/KiCad.app/Contents> 或装标准位置 /Applications/KiCad）")
        self.log = log
        self._next_id = 1
        self._pending: dict[int, Queue] = {}
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            [self.node_bin, self.mcp_js],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "KICAD_PYTHON": f"{self.ext}/Frameworks/Python.framework/Versions/Current/bin/python3",
                "PYTHONPATH": f"{self.ext}/Frameworks/Python.framework/Versions/3.9/lib/python3.9/site-packages",
                "KICAD10_SYMBOL_DIR": f"{self.ext}/SharedSupport/symbols",
                "KICAD10_FOOTPRINT_DIR": f"{self.ext}/SharedSupport/footprints",
                "KICAD_CLI": f"{self.ext}/MacOS/kicad-cli",
                "LOG_LEVEL": "error",
            },
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # stderr 转发到我们的 stderr（不阻塞）
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # ---------- 底层 ----------
    def _read_loop(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # 服务器日志行
            q = self._pending.get(msg.get("id"))
            if q is not None:
                q.put(msg)

    def _drain_stderr(self):
        for chunk in iter(self.proc.stderr.readline, b""):
            sys.stderr.write("[srv] " + chunk.decode(errors="replace"))

    def call(self, method: str, params: dict, timeout_s: float = 180.0) -> dict:
        t0 = time.monotonic()
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        q: Queue = Queue()
        self._pending[rid] = q
        try:
            self.proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode()
            )
            self.proc.stdin.flush()
            msg = q.get(timeout=timeout_s)
        except (BrokenPipeError, Empty) as e:
            _audit(method, params, {
                "ok": False, "error": f"{type(e).__name__}: {e}",
                "ms": int((time.monotonic() - t0) * 1000),
            })
            raise MCPError(f"call {method} 失败: {e}") from e
        finally:
            self._pending.pop(rid, None)
        ms = int((time.monotonic() - t0) * 1000)
        if "error" in msg:
            _audit(method, params, {
                "ok": False, "error": _snip(str(msg["error"]), 2000), "ms": ms,
            })
            raise MCPError(f"{method}: {msg['error']}")
        _audit(method, params, {
            "ok": True, "ms": ms,
            "result": _snip(json.dumps(msg.get("result", {}), ensure_ascii=False,
                                       default=str), 4000),
        })
        return msg.get("result", {})

    # ---------- MCP 工具层 ----------
    def initialize(self, client_name="design2schematic", timeout_s: float = 30.0):
        result = self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1.0"},
            },
            timeout_s=timeout_s,
        )
        self.proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        self.proc.stdin.flush()
        return result.get("serverInfo", {})

    def tool(self, name: str, args: dict | None = None, timeout_s: float = 180.0, check: bool = True) -> str:
        result = self.call("tools/call", {"name": name, "arguments": args or {}}, timeout_s=timeout_s)
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        if check and (result.get("isError") or _looks_failed(text)):
            raise MCPError(f"{name} 失败: {text[:300]}")
        self.log(f"  ✓ {name}")
        return text

    def tool_soft(self, name: str, args: dict | None = None, timeout_s: float = 180.0) -> str:
        try:
            return self.tool(name, args, timeout_s=timeout_s)
        except MCPError as e:
            self.log(f"  (soft-fail) {e}")
            return ""

    def wait_for_backend(self, poll_s: float = 3.0, attempts: int = 30) -> bool:
        """轮询 get_backend_state 直到 Python 桥就绪（wxApp 预热可达 65s）。"""
        for i in range(attempts):
            time.sleep(poll_s)
            st = self.tool_soft("get_backend_state", {})
            if any(k in st.lower() for k in ("running", "ready")):
                self.log(f"  后端就绪 ({(i + 1) * poll_s:.0f}s)")
                return True
        self.log("  ⚠ 后端轮询超时，继续尝试")
        return False

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass


def _looks_failed(text: str) -> bool:
    """保守判定：协议 isError 是主信号，这里只兜显式失败话术。
    注意 "Placed 30, 0 failed" 是成功话术——只认非零失败计数。"""
    head = text[:400]
    if "✗" in head:
        return True
    low = head.lower()
    if re.search(r"failed to\b|traceback|\bexception\b", low):
        return True
    return bool(re.search(r"\b[1-9]\d*\s+failed\b", low))
