#!/usr/bin/env python3
"""pack_from_studio.py — 工作台（PCBStudioV2）→ 发行仓（HarnessPCB）单向抽取。

机制（发行包计划 §六，2026-10-14 拍板）：
  --check   先看差异（缺/多/内容变动，逐文件 sha256 对 MANIFEST.json）
  --sync    同步：executors/ 整体重生成 + MANIFEST.json 记每文件
            sha256 + 源路径 + 日期（兼作 P4 内容物审计底稿）
  开发永远在工作台；发行仓 executors/ 是生成物，禁止手改（手改必被下次
  同步覆盖）。里程碑在发行仓打 tag（v0.1…）。

用法：
  python3 tools/pack_from_studio.py --check
  python3 tools/pack_from_studio.py --sync
  python3 tools/pack_from_studio.py --check --studio /path/to/PCBStudioV2

安全护栏：发行仓根须同时存在 .git 与 README.md（防指错目录误清）；
--sync 会先整删 executors/ 再拷贝（重生成语义）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent                       # 发行仓根
EXECUTORS = REPO / "executors"
MANIFEST = REPO / "MANIFEST.json"

# FLAG_FREEZE 参数快照（发行包计划 §6.2：同步时生成、带源文件 sha256 戳）。
# Skills/文档「引用不复制」——推荐命令行的唯一权威源即此快照；手抄参数=漂移源。
FLAG_FREEZE_SRC_REL = "KiCAD_Access/eval_out/FLAG_FREEZE.md"
FLAG_FREEZE_SNAPSHOT = REPO / "docs" / "参数快照_FLAG_FREEZE.md"

# 全局跳过（结构上排除：RL 权重/过程产物/缓存，§6.1）
SKIP_NAMES = {"__pycache__", ".venv", ".git", ".DS_Store", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".lck", ".pt", ".npz")


def default_studio() -> Path:
    """默认工作台根：发行仓的平级兄弟目录 PCBStudioV2。"""
    return REPO.parent / "PCBStudioV2"


def parse_allowlist(path: Path) -> tuple[list[str], list[str]]:
    """→ (包含路径列表, 排除路径列表)。目录以 / 结尾；! 前缀 = 排除。"""
    includes: list[str] = []
    excludes: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            excludes.append(line[1:].strip())
        else:
            includes.append(line)
    if not includes:
        raise SystemExit(f"allowlist 为空：{path}")
    return includes, excludes


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def collect_files(studio: Path, includes: list[str], excludes: list[str]) -> dict[str, Path]:
    """展开 allowlist → { 发行仓相对路径: 工作台源文件 }。"""
    out: dict[str, Path] = {}

    def excluded(rel: str) -> bool:
        return any(rel == e or rel.startswith(e.rstrip("/") + "/")
                   for e in excludes)

    def add_file(src: Path, rel: str) -> None:
        if rel in out:
            raise SystemExit(f"allowlist 重复覆盖：{rel}")
        if excluded(rel):
            return
        out[rel] = src

    for inc in includes:
        if inc.endswith("/"):
            top = studio / inc.rstrip("/")
            if not top.is_dir():
                raise SystemExit(f"allowlist 目录不存在：{inc}")
            for p in sorted(top.rglob("*")):
                if not p.is_file():
                    continue
                if any(part in SKIP_NAMES for part in p.parts):
                    continue
                if p.suffix in SKIP_SUFFIXES:
                    continue
                add_file(p, p.relative_to(studio).as_posix())
        else:
            f = studio / inc
            if not f.is_file():
                raise SystemExit(f"allowlist 文件不存在：{inc}")
            add_file(f, inc)
    return out


def build_freeze_snapshot(studio: Path, git_rev: str) -> str:
    """FLAG_FREEZE.md → 发行仓快照文本（头部戳 + 正文 verbatim，不含墙钟时间，
    保证 --check 的确定性：只有源文件或工作台 rev 变了快照才变）。"""
    src = studio / FLAG_FREEZE_SRC_REL
    if not src.is_file():
        raise SystemExit(f"FLAG_FREEZE 源不存在：{src}")
    digest = sha256(src)
    body = src.read_text(encoding="utf-8")
    header = (
        f"> 参数快照（生成物，勿手改——由 tools/pack_from_studio.py --sync 重生成）\n"
        f"> 源：{FLAG_FREEZE_SRC_REL}（sha256 {digest[:16]}…）\n"
        f"> 工作台 rev：{git_rev or '未知'} · 快照义务见发行包计划 §6.2\n"
        f"> Skills/文档引用推荐参数一律指向本文件，不抄副本。\n\n"
    )
    return header + body


def write_freeze_snapshot(studio: Path, git_rev: str) -> bool:
    """写 docs/参数快照_FLAG_FREEZE.md；返回内容是否有变化。"""
    FLAG_FREEZE_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    text = build_freeze_snapshot(studio, git_rev)
    changed = (not FLAG_FREEZE_SNAPSHOT.exists()
               or FLAG_FREEZE_SNAPSHOT.read_text(encoding="utf-8") != text)
    if changed:
        FLAG_FREEZE_SNAPSHOT.write_text(text, encoding="utf-8")
    return changed


def check(files: dict[str, Path], studio: Path, git_rev: str) -> int:
    """对比 executors/ 现状与应收集合；返回差异数。"""
    diffs = 0
    have = {p.relative_to(EXECUTORS).as_posix(): p
            for p in EXECUTORS.rglob("*") if p.is_file()} if EXECUTORS.is_dir() else {}

    for rel, src in sorted(files.items()):
        dst = EXECUTORS / rel
        if not dst.exists():
            print(f"  缺失  {rel}")
            diffs += 1
        elif sha256(dst) != sha256(src):
            print(f"  变动  {rel}")
            diffs += 1
    for rel in sorted(set(have) - set(files)):
        print(f"  多余  {rel}")
        diffs += 1

    # FLAG_FREEZE 快照同样纳入三态校验
    want = build_freeze_snapshot(studio, git_rev)
    if not FLAG_FREEZE_SNAPSHOT.exists():
        print(f"  缺失  docs/{FLAG_FREEZE_SNAPSHOT.name}")
        diffs += 1
    elif FLAG_FREEZE_SNAPSHOT.read_text(encoding="utf-8") != want:
        print(f"  变动  docs/{FLAG_FREEZE_SNAPSHOT.name}")
        diffs += 1

    print(f"应收 {len(files)} 文件，差异 {diffs}"
          + ("（已同步）" if diffs == 0 else "（需 --sync）"))
    return diffs


def sync(files: dict[str, Path], studio: Path) -> None:
    # 护栏：确认这是发行仓而非任意目录
    if not (REPO / ".git").exists() or not (REPO / "README.md").exists():
        raise SystemExit(f"发行仓根校验失败（缺 .git 或 README.md）：{REPO}")
    if EXECUTORS.exists():
        shutil.rmtree(EXECUTORS)
    entries = {}
    for rel, src in sorted(files.items()):
        dst = EXECUTORS / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        entries[rel] = {
            "sha256": sha256(dst),
            "source": rel,  # 相对工作台根（绝不记绝对路径，防用户名外泄）
            "bytes": src.stat().st_size,
        }
    try:
        git_rev = subprocess.run(
            ["git", "-C", str(studio), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        git_rev = ""
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "studio": studio.name,  # 只记目录名，不记绝对路径
        "studio_rev": git_rev,
        "n_files": len(entries),
        "files": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    changed = write_freeze_snapshot(studio, git_rev)
    print(f"已同步 {len(entries)} 文件 → {EXECUTORS}")
    print(f"MANIFEST → {MANIFEST}")
    print(f"FLAG_FREEZE 快照 → {FLAG_FREEZE_SNAPSHOT}"
          + ("（已更新）" if changed else "（未变）"))


def main() -> None:
    ap = argparse.ArgumentParser(description="工作台→发行仓 白名单抽取")
    ap.add_argument("--check", action="store_true", help="只看差异，不写盘")
    ap.add_argument("--sync", action="store_true", help="执行同步（重生成 executors/ + MANIFEST）")
    ap.add_argument("--studio", type=Path, default=None,
                    help="工作台根（默认：发行仓平级 ../PCBStudioV2）")
    args = ap.parse_args()

    if not args.check and not args.sync:
        args.check = True  # 裸跑 = --check

    studio = (args.studio or default_studio()).resolve()
    if not studio.is_dir():
        raise SystemExit(f"工作台根不存在：{studio}")

    includes, excludes = parse_allowlist(HERE / "allowlist.txt")
    files = collect_files(studio, includes, excludes)
    try:
        git_rev = subprocess.run(
            ["git", "-C", str(studio), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        git_rev = ""

    if args.check:
        n = check(files, studio, git_rev)
        sys.exit(1 if n else 0)
    sync(files, studio)


if __name__ == "__main__":
    main()
