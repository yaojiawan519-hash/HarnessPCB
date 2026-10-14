"""三元组（状态/动作/判定）持久化：JSONL 追加，逐轮可溯。

每行一个 RoundRecord：run_id / board / pass / iter / action / outcome /
score / 产物路径（dsn/ses/board 全留档）。数据集 = R1 离线 CQL 的输入。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import env


def new_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def versions_block() -> dict:
    """三方冻结基线；失败不阻断（记录 error）。"""
    try:
        return env.versions()
    except Exception as e:  # pragma: no cover
        return {"error": str(e)}


def append_round(run_dir: Path, record: dict) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with open(run_dir / "triples.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_manifest(run_dir: Path, manifest: dict) -> None:
    man = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "versions": versions_block(), **manifest}
    (run_dir / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
