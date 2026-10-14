"""footprint 库解析：lib:name → .kicad_mod 路径（M2-1）。

解析顺序（对齐 KiCad 语义，工程表覆盖全局）：
1. <project_dir>/fp-lib-table（工程表，含 ${KIPRJMOD} 本地库）
2. 全局表 ~/Library/Preferences/kicad/<ver>/fp-lib-table（macOS KiCad 10）
3. 系统库目录兜底直查（KiCad.app/SharedSupport/footprints/<lib>.pretty）

URI 只处理本机出现的两种：绝对路径与 ${KIPRJMOD}/… 前缀。
"""

from __future__ import annotations

from pathlib import Path

from .. import tools
from . import sexpr as sx
from .footprint import FootprintGeom, parse_footprint

_TABLE_SUBDIRS = (  # 全局表候选（按 KiCad 版本目录）
    Path.home() / "Library" / "Preferences" / "kicad" / "10.0",
    Path.home() / "Library" / "Preferences" / "kicad" / "9.0",
    Path.home() / ".config" / "kicad" / "10.0",
)


def _system_dirs() -> list[Path]:
    """kicad-cli 所在 app bundle 推导系统库目录 + 环境变量覆盖。"""
    import os

    dirs: list[Path] = []
    v = os.environ.get("HWS_KICAD_FOOTPRINTS")
    if v:
        dirs.append(Path(v))
    # kicad-cli = …/KiCad.app/Contents/MacOS/kicad-cli → …/SharedSupport/footprints
    cli = tools.resolved_kicad_cli()
    if cli is not None:
        cand = cli.parent.parent / "SharedSupport" / "footprints"
        if cand.exists():
            dirs.append(cand)
    return dirs


def _read_table(path: Path, kiprjmod: Path | None) -> dict[str, Path]:
    """fp-lib-table → {lib名: .pretty 目录}。坏行跳过（表是 KiCad 生成的）。"""
    libs: dict[str, Path] = {}
    if not path.exists():
        return libs
    try:
        doc = sx.loads(path.read_text())
    except Exception:
        return libs
    root = doc[0] if sx.tag(doc[0]) == "fp_lib_table" else doc
    for lib in sx.children(root, "lib"):
        name = next(sx.children(lib, "name"), None)
        uri = next(sx.children(lib, "uri"), None)
        if name is None or uri is None:
            continue
        u = str(sx._value(uri[1]))
        if u.startswith("${KIPRJMOD}") and kiprjmod is not None:
            u = str(kiprjmod / u[len("${KIPRJMOD}"):].lstrip("/"))
        p = Path(u)
        if p.exists():
            libs[str(sx._value(name[1]))] = p
    return libs


class FootprintLibs:
    """lib:name 解析器（带缓存）。resolve 失败返回 None 并记入 missing。"""

    def __init__(self, project_dir: Path | None = None):
        self.project_dir = project_dir
        self.libs: dict[str, Path] = {}
        self.missing: list[str] = []  # 解析失败的 lib:name（去重保序）
        self._cache: dict[str, FootprintGeom | None] = {}

        for table in self._tables():
            self.libs.update(_read_table(table, project_dir))
        # 系统目录兜底：直接找 <lib>.pretty，免表也能解析
        for d in _system_dirs():
            for pretty in sorted(d.glob("*.pretty")):
                self.libs.setdefault(pretty.stem, pretty)

    def _tables(self) -> list[Path]:
        out: list[Path] = []
        if self.project_dir is not None:
            out.append(self.project_dir / "fp-lib-table")
        out.extend(d / "fp-lib-table" for d in _TABLE_SUBDIRS)
        return out

    def path_of(self, fp: str) -> Path | None:
        """'lib:name' → .kicad_mod 文件路径。"""
        if ":" not in fp:
            return None
        lib, _, name = fp.partition(":")
        pretty = self.libs.get(lib)
        if pretty is None:
            return None
        p = pretty / f"{name}.kicad_mod"
        return p if p.exists() else None

    def load(self, fp: str) -> FootprintGeom | None:
        """'lib:name' → FootprintGeom（缓存；失败记 missing）。"""
        if fp in self._cache:
            return self._cache[fp]
        path = self.path_of(fp)
        geom = None
        if path is not None:
            try:
                geom = parse_footprint(path.read_text(), name=fp)
            except Exception:
                geom = None
        if geom is None and fp not in self.missing:
            self.missing.append(fp)
        self._cache[fp] = geom
        return geom
