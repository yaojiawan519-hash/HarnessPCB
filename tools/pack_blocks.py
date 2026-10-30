#!/usr/bin/env python3
"""pack_blocks.py — 工作台块库（DesignBlocks/）→ 发行仓 blocks/，**按许可证筛**。

为什么不走 `pack_from_studio.py` 的白名单：白名单是**路径**口径，而这里要按
**内容**筛 —— 每块 `block.sdi.yaml` 里的 `license_tier`。公开仓的铁律是
只收 `permissive`：

  - `permissive`  —— 上游 MIT/Apache 等，可公开发行（随块卡记上游版权人）
  - `internal`    —— 用户自己的板 / 许可未核定：**绝不进包**
  - 无块卡        —— 老 V3.x 血统的引擎生成块，没有许可证记录：**绝不进包**

没得商量：一旦混进去就是把别人的板公开发布了，收不回来。

机制与 pack_from_studio 同款：`--check` 逐文件 sha256 对 MANIFEST-blocks.json，
`--sync` 整删 blocks/ 重生成 + 重写索引（INDEX.md / index.json）。

同时做两件事：
  1. **正文清洗**：块卡 `author:` 里的本机用户名换成 `HarnessPCB`；复制后逐文件
     断言不带用户名、不带 /Users/ 本机路径，命中就整批失败（不静默）。
  2. **索引**：按类别生成 INDEX.md（人看）+ index.json（机读），供"块库当菜单"
     的检索用。

用法：
  python3 tools/pack_blocks.py --check        # 看差异
  python3 tools/pack_blocks.py --sync         # 复制 + 写索引
  python3 tools/pack_blocks.py --list         # 只列：谁进包、谁被挡、为什么
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BLOCKS = REPO / "blocks"
MANIFEST = REPO / "MANIFEST-blocks.json"

# 目录递归跳过：缓存 / 本机偏好 / 试删门残留（spur 的 trial 副本不是交付物）/
# 版本库与编辑器本地历史（2026-10-31 实测：`mcu_esp32c3_mini1/.history` 里是一整个
# git 仓库，21 个文件连 .git 对象一起进了包，git add 时才报"embedded git repository"）
SKIP_NAMES = {"__pycache__", ".DS_Store", ".git", ".history", ".svn", ".venv"}
SKIP_SUFFIXES = (".pyc", ".kicad_prl", ".lck")
SKIP_PATTERNS = (".trial.",)

# 门禁 / 编辑器在块目录里生成的旁产物：不属于交付物，`--check` 不当差异报，
# 但 `--sync` 会一并清掉（否则 `git add -A` 会把它们带进仓库）。
# `.kicad_prl` 由 kicad-cli 打开工程时写出（跑一次 `sdi.checkall` 就多 105 个），
# `~*.kicad_pro.lck` 是 KiCad 的锁文件，`.lck` 已在 SKIP_SUFFIXES 里。
BYPRODUCT_SUFFIXES = (".kicad_prl",)

# 正文清洗：本机用户名一律换成项目名（P4 数据剥离口径的延伸）
TEXT_SUFFIXES = (".yaml", ".yml", ".json", ".md", ".kicad_sch", ".kicad_pcb", ".kicad_pro")
RENAMES = (("yaoyuan", "HarnessPCB"),)
FORBIDDEN = ("yaoyuan", "/Users/", "PCBStudioV2/DesignBlocks")


def default_studio() -> Path:
    return REPO.parent / "PCBStudioV2"


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def card_of(block_dir: Path) -> dict | None:
    """读块卡；只取索引/筛选用得着的字段，缺字段不炸。"""
    f = block_dir / "block.sdi.yaml"
    if not f.exists():
        return None
    try:
        import yaml
    except ImportError:                       # pragma: no cover
        sys.exit("需要 pyyaml：python3 -m pip install pyyaml")
    d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    name = d.get("project", {}).get("name") or block_dir.name
    spec = (d.get("defs") or {}).get(name) or {}
    prov = spec.get("provenance") or {}
    return {
        "name": name,
        "category": spec.get("category", ""),
        "tier": prov.get("license_tier", ""),
        "license": prov.get("license", ""),
        "board": prov.get("board", ""),
        "source": prov.get("source", ""),
        "date": str(prov.get("date", "")),
        "parts": len(spec.get("parts") or []),
        "ports": len(spec.get("ports") or []),
        "rails": list(spec.get("rails") or []),
    }


def counts_of(block_dir: Path, name: str) -> dict:
    p = block_dir / f"{name}.placement.json"
    if not p.exists():
        return {}
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("counts") or {})
    except ValueError:
        return {}


def wanted(path: Path) -> bool:
    # 逐级看：跳过的是**目录**（`.git/`、`.history/`、`__pycache__/`）时，
    # 只看文件名判不出来——`.history` 里的 `HEAD`、`README.txt` 名字都干干净净。
    if any(part in SKIP_NAMES for part in path.parts):
        return False
    if path.suffix in SKIP_SUFFIXES:
        return False
    return not any(pat in path.name for pat in SKIP_PATTERNS)


def scrub(data: bytes, rel: str) -> bytes:
    """正文清洗：文本文件按 RENAMES 换名并断言不含禁用串；二进制原样。"""
    if Path(rel).suffix not in TEXT_SUFFIXES:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in RENAMES:
        text = text.replace(old, new)
    for bad in FORBIDDEN:
        if bad in text:
            raise SystemExit(f"✗ 数据剥离失败：{rel} 仍含 {bad!r}")
    return text.encode("utf-8")


def collect(studio: Path) -> tuple[list[str], list[dict], list[dict]]:
    """→ (收录块名, 收录块卡列表, 被挡列表[含原因])"""
    src = studio / "DesignBlocks"
    if not src.is_dir():
        sys.exit(f"块库不存在：{src}")
    picked: list[dict] = []
    blocked: list[dict] = []
    for d in sorted(src.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        card = card_of(d)
        if card is None:
            blocked.append({"name": d.name, "why": "无块卡（老 V3.x 血统，许可证不可考）"})
            continue
        if card["tier"] != "permissive":
            blocked.append({"name": d.name, "why": f"license_tier={card['tier'] or '(空)'}"})
            continue
        card["counts"] = counts_of(d, card["name"])
        card["dir"] = d.name
        picked.append(card)
    return [c["dir"] for c in picked], picked, blocked


def build_index(picked: list[dict]) -> tuple[str, dict]:
    rows = sorted(picked, key=lambda c: (c["category"], c["name"]))
    by_cat: dict[str, list[dict]] = {}
    for c in rows:
        by_cat.setdefault(c["category"] or "(未分类)", []).append(c)

    lines = [
        "# 块库索引",
        "",
        f"共 **{len(rows)} 块**，全部 `license_tier: permissive`（可公开发行）。"
        "每块 = 一块人画好的电路，含原理图、板（2 层、不带铺铜）、块卡（端口/轨/内部网/放置先验）"
        "与测量产物（`_geom/`）。",
        "",
        "**口岸按焊盘记**：拼板时从焊盘重新拉线，不指望块里切剩的出口铜能对接上。"
        "详细口径见 `docs/块库说明.md`。",
        "",
    ]
    for cat, items in by_cat.items():
        lines += [f"## {cat}（{len(items)}）", "",
                  "| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |",
                  "|---|---|---|---|---|---|"]
        for c in items:
            cnt = c["counts"]
            copper = (f"{cnt.get('copper_segs', '?')} 段"
                      f" + {cnt.get('vias', 0)} 孔") if cnt else "—"
            lic = (c["license"] or "").split("—")[0].strip() or "permissive"
            lines.append(
                f"| [`{c['name']}`]({c['dir']}/) | {c['parts']} | {cnt.get('nets', '?')} "
                f"| {copper} | {c['board']} | {lic} |")
        lines.append("")

    index = {
        "count": len(rows),
        "license_tier": "permissive",
        "blocks": [
            {
                "name": c["name"], "dir": c["dir"], "category": c["category"],
                "parts": c["parts"], "ports": c["ports"], "rails": c["rails"],
                "counts": c["counts"], "board": c["board"],
                "license": c["license"], "source": c["source"], "date": c["date"],
            }
            for c in rows
        ],
    }
    return "\n".join(lines).rstrip() + "\n", index


def generate(studio: Path) -> tuple[dict[str, bytes], dict, list[dict]]:
    """→ (目标相对路径 → 字节, 块卡索引, 被挡列表)。纯函数，--check/--sync 共用。"""
    dirs, picked, blocked = collect(studio)
    out: dict[str, bytes] = {}
    src_root = studio / "DesignBlocks"
    for name in dirs:
        d = src_root / name
        for f in sorted(d.rglob("*")):
            if not f.is_file() or not wanted(f):
                continue
            rel = f.relative_to(src_root).as_posix()
            out[rel] = scrub(f.read_bytes(), rel)
    md, index = build_index(picked)
    out["INDEX.md"] = scrub(md.encode("utf-8"), "INDEX.md")
    out["index.json"] = scrub(
        (json.dumps(index, ensure_ascii=False, indent=1) + "\n").encode("utf-8"),
        "index.json")
    return out, index, blocked


def manifest_of(files: dict[str, bytes]) -> dict:
    return {
        "note": "块库抽取清单（pack_blocks.py 生成，禁止手改）；来源=工作台 DesignBlocks/，"
                "只收 license_tier=permissive",
        "count": len({k.split('/')[0] for k in files}),
        "files": len(files),
        "entries": {k: {"sha256": sha256(v), "bytes": len(v)} for k, v in sorted(files.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="块库 → 发行仓 blocks/（按许可证筛）")
    ap.add_argument("--check", action="store_true", help="只看差异，不动盘")
    ap.add_argument("--sync", action="store_true", help="整删 blocks/ 重生成")
    ap.add_argument("--list", action="store_true", help="只列收录/被挡清单")
    ap.add_argument("--studio", default=str(default_studio()))
    a = ap.parse_args()
    if not (a.check or a.sync or a.list):
        ap.error("给 --check / --sync / --list 之一")

    studio = Path(a.studio).resolve()
    files, index, blocked = generate(studio)

    if a.list:
        print(f"收录 {index['count']} 块（permissive）：")
        for c in index["blocks"]:
            print(f"  ✓ {c['name']:<32} {c['category']:<14} {c['parts']:>2} 件")
        print(f"\n挡下 {len(blocked)} 块：")
        for b in blocked:
            print(f"  ✗ {b['name']:<32} {b['why']}")
        return 0

    if a.check:
        cur = {}
        if MANIFEST.exists():
            cur = json.loads(MANIFEST.read_text(encoding="utf-8")).get("entries", {})
        new = manifest_of(files)["entries"]
        missing = sorted(set(new) - set(cur))
        extra = sorted(set(cur) - set(new))
        changed = sorted(k for k in set(new) & set(cur) if new[k]["sha256"] != cur[k]["sha256"])
        # 盘上多出来的东西也要看：只比清单的话，块目录里混进的垃圾（锁文件、
        # `.history/`、门禁旁产物）永远报不出来，`git add -A` 一把带进仓库。
        disk = {p.relative_to(BLOCKS).as_posix() for p in BLOCKS.rglob("*") if p.is_file()} \
            if BLOCKS.is_dir() else set()
        byproducts = {k for k in disk - set(new) if k.endswith(BYPRODUCT_SUFFIXES)}
        stray = sorted(disk - set(new) - byproducts)
        for k in missing:
            print(f"  缺失  blocks/{k}")
        for k in extra:
            print(f"  多余  blocks/{k}")
        for k in changed:
            print(f"  变动  blocks/{k}")
        for k in stray:
            print(f"  盘上多余  blocks/{k}")
        n = len(missing) + len(extra) + len(changed) + len(stray)
        note = f"；门禁旁产物 {len(byproducts)} 个（.kicad_prl，--sync 会清）" if byproducts else ""
        print(f"应收 {len(files)} 文件 / {index['count']} 块，差异 {n}"
              f"（{'需 --sync' if n else '已同步'}）；挡下 {len(blocked)} 块{note}")
        return 1 if n else 0

    # --sync：整删重生成（blocks/ 是生成物，手改必被覆盖）
    if BLOCKS.exists():
        shutil.rmtree(BLOCKS)
    for rel, data in files.items():
        dst = BLOCKS / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    MANIFEST.write_text(
        json.dumps(manifest_of(files), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    size = sum(len(v) for v in files.values()) / 1e6
    print(f"已同步 {len(files)} 文件 / {index['count']} 块 → {BLOCKS}（{size:.1f} MB）")
    print(f"清单 → {MANIFEST}；挡下 {len(blocked)} 块（internal / 无块卡）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
