#!/usr/bin/env python3
"""Step -1: ingest a tender ZIP archive and normalize it into a bid-gen tender dir.

Handles the messy reality of Chinese 电子招投标 packages:

  <项目>.zip
    ├── *.pdf                     招标公告 / 公平竞争自查表 / 控制价清单
    ├── *.XZZ                     西藏电子招标文件 (zip → File.zip → 招标项目/招标文件.pdf)
    ├── *.GKZJ                    控制价文件 (zip → File.zip → 招标项目/控制价清单文件.pdf)
    ├── *.ZBWJ / *.ZBF / *.ZBJ    其他地区电子招标文件容器 (same nested-zip layout)
    ├── 工程量清单*.zip            清单 pdf/xlsx/XZZB
    └── 图纸*.zip                  dwg/dxf drawings

All archives are extracted with GBK filename recovery (Windows-created zips
declare no UTF-8 flag and Python would otherwise emit mojibake).

Output layout (``<out-dir>/<project-name>/``)::

    招标文件.pdf                   ← normalized canonical name (required by pipeline)
    控制价清单文件.pdf
    招标公告.pdf
    公平竞争自查表.pdf
    工程量清单/                    ← promoted out of the nested zip
    图纸/                          ← promoted out of the nested zip
    原始文件/                      ← every original archive, untouched
    ingest_manifest.json           ← what came from where

Then run ``python3 scripts/bid_run.py "<out-dir>/<project-name>"``.

Exit codes: 0 ok / 2 hard failure (bad archive, no <tender_dir> to build).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Container extensions that are really zip archives holding the real documents.
CONTAINER_EXTS = {".xzz", ".gkzj", ".zbwj", ".zbf", ".zbj", ".xzzb", ".zip"}
# Filenames inside a container that we look for, in priority order.
TENDER_DOC_NAMES = ("招标文件.pdf",)
CONTROL_PRICE_NAMES = ("控制价清单文件.pdf", "招标清单文件.pdf")

ANNOUNCE_RE = re.compile(r"招标公告")
FAIRNESS_RE = re.compile(r"公平竞争|自查表")
BOQ_ZIP_RE = re.compile(r"工程量清单|清单")
DRAWING_ZIP_RE = re.compile(r"图纸|图集|最终图|施工图|竣工图|设计图|drawing", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# GBK-safe archive handling
# --------------------------------------------------------------------------- #
def fix_zip_name(name: str, flag_bits: int) -> str:
    """Recover GBK filenames from Windows-created zips without the UTF-8 flag."""
    if flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            return name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name


def zip_entries(zf: zipfile.ZipFile):
    """Yield ``(fixed_name, ZipInfo)`` for every entry."""
    for info in zf.infolist():
        yield fix_zip_name(info.filename, info.flag_bits), info


def safe_extract(zf: zipfile.ZipFile, dest: Path, log) -> list[Path]:
    """Extract a zip (GBK-safe, traversal-safe). Returns written file paths."""
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    failed: list[str] = []
    for name, info in zip_entries(zf):
        name = name.replace("\\", "/").lstrip("/")
        if not name or name.endswith("/"):
            continue
        parts = [p for p in name.split("/") if p not in ("", ".", "..")]
        if not parts:
            continue
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            # A single corrupt/oversized entry must not abort the whole ingest.
            failed.append(name)
            if target.exists() and target.stat().st_size == 0:
                target.unlink(missing_ok=True)
            log(f"  ! skipped corrupt entry {name} ({type(exc).__name__})")
            continue
        written.append(target)
    log(f"  extracted {len(written)} files → {dest}")
    if failed:
        log(f"  ! {len(failed)} entries skipped (corrupt archive members)")
    return written


def extract_archive(path: Path, dest: Path, log, depth: int = 0) -> list[Path]:
    """Extract ``path``; recurse into nested archives up to 3 levels."""
    if depth > 3:
        return []
    try:
        with zipfile.ZipFile(path) as zf:
            files = safe_extract(zf, dest, log)
    except (zipfile.BadZipFile, OSError) as exc:
        log(f"  ! not a zip ({type(exc).__name__}), copied as-is: {path.name}")
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / path.name
        shutil.copy2(path, target)
        return [target]
    nested = dest / "_nested"
    for f in list(files):
        if f.suffix.lower() in CONTAINER_EXTS and not f.name.startswith("."):
            sub = nested / f.stem
            log(f"  ↳ nested container {f.name}")
            extract_archive(f, sub, log, depth + 1)
    return files


# --------------------------------------------------------------------------- #
# Promotion helpers
# --------------------------------------------------------------------------- #
def find_first(root: Path, pattern: re.Pattern, suffix: str | None = None) -> Path | None:
    if not root.exists():
        return None
    cands = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if suffix and p.suffix.lower() != suffix.lower():
            continue
        if pattern.search(p.name):
            cands.append(p)
    if not cands:
        return None
    # Shallowest path wins; then largest file.
    cands.sort(key=lambda p: (len(p.parts), -p.stat().st_size))
    return cands[0]


def promote(src: Path, dst: Path, log) -> bool:
    """Copy ``src`` to canonical location ``dst`` (idempotent)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        log(f"  = {dst.name} (already present)")
        return True
    shutil.copy2(src, dst)
    log(f"  + {dst.name}  ← {src.relative_to(src.anchor) if src.is_absolute() else src}")
    return True


def effective_root(src: Path) -> Path:
    """Descend through single-child wrapper dirs (zip often nests 图纸/图纸/)."""
    cur = src
    while True:
        kids = [p for p in cur.iterdir() if not p.name.startswith(("_", "."))]
        if len(kids) == 1 and kids[0].is_dir():
            cur = kids[0]
        else:
            return cur


def promote_dir(src: Path, dst: Path, log) -> int:
    """Copy a directory tree into ``dst``, skipping the ``_nested`` scratch dir."""
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if "_nested" in rel.parts:  # scratch dir from nested-container recursion
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != p.stat().st_size:
            shutil.copy2(p, target)
        n += 1
    log(f"  + {dst.name}/  ({n} files)")
    return n


def guess_project_name(zip_path: Path) -> str:
    name = zip_path.stem
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)  # 项目名(1).zip
    name = re.sub(r"[_-]?\d{6,8}$", "", name)  # trailing dates
    return name.strip() or "tender"


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest a tender ZIP into a bid-gen tender dir")
    ap.add_argument("zip_path", help="path to the tender .zip")
    ap.add_argument("--out-dir", default=None,
                    help="parent directory for <project-name>/ (default: zip's parent)")
    ap.add_argument("--name", default=None, help="override project folder name")
    ap.add_argument("--force", action="store_true", help="re-extract even if manifest exists")
    args = ap.parse_args()

    zip_path = Path(args.zip_path).expanduser().resolve()
    if not zip_path.exists():
        print(f"[fail] zip not found: {zip_path}")
        return 2
    if not zipfile.is_zipfile(zip_path):
        print(f"[fail] not a zip archive: {zip_path}")
        return 2

    project = args.name or guess_project_name(zip_path)
    out_root = Path(args.out_dir).expanduser().resolve() if args.out_dir else zip_path.parent
    tender_dir = out_root / project
    raw_dir = tender_dir / "原始文件"

    def log(msg: str) -> None:
        print(msg, flush=True)

    log(f"══ ingest ══\n  zip    : {zip_path.name}\n  target : {tender_dir}")

    manifest_path = tender_dir / "ingest_manifest.json"
    if manifest_path.exists() and not args.force:
        log("  (manifest exists — pass --force to re-extract; promoting anyway)")

    tender_dir.mkdir(parents=True, exist_ok=True)

    # 1. stage everything into 原始文件/ (original archives preserved)
    staging = tender_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    log("── extract ──")
    extract_archive(zip_path, staging, log)

    # preserve originals
    raw_dir.mkdir(parents=True, exist_ok=True)
    if not (raw_dir / zip_path.name).exists():
        shutil.copy2(zip_path, raw_dir / zip_path.name)
    for p in staging.iterdir():
        if p.is_dir():
            continue
        dst = raw_dir / p.name
        if not dst.exists():
            shutil.copy2(p, dst)
    log(f"  原始文件/: {len(list(raw_dir.iterdir()))} archives")

    # 2. promote canonical artifacts
    log("── promote ──")
    warnings: list[str] = []

    t = find_first(staging, re.compile("|".join(map(re.escape, TENDER_DOC_NAMES))), ".pdf")
    if t:
        promote(t, tender_dir / "招标文件.pdf", log)
    else:
        warnings.append("未找到 招标文件.pdf —— 请手工确认容器格式")

    c = find_first(staging, re.compile("|".join(map(re.escape, CONTROL_PRICE_NAMES))), ".pdf")
    if c and c.resolve() != (t.resolve() if t else None):
        promote(c, tender_dir / "控制价清单文件.pdf", log)
    elif not c:
        warnings.append("未找到 控制价清单文件.pdf（部分项目确实没有）")

    a = find_first(staging, ANNOUNCE_RE, ".pdf")
    if a:
        promote(a, tender_dir / "招标公告.pdf", log)

    f = find_first(staging, FAIRNESS_RE, ".pdf")
    if f:
        promote(f, tender_dir / "公平竞争自查表.pdf", log)

    # 工程量清单 / 图纸 come out of their own nested zips
    for p in sorted(staging.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in CONTAINER_EXTS:
            continue
        if "_nested" in p.relative_to(staging).parts:
            continue  # only promote from the top level of what the user dropped in
        sub = staging / "_nested" / p.stem
        if BOQ_ZIP_RE.search(p.stem) and sub.exists():
            promote_dir(effective_root(sub), tender_dir / "工程量清单", log)
        elif DRAWING_ZIP_RE.search(p.stem) and sub.exists():
            promote_dir(effective_root(sub), tender_dir / "图纸", log)

    # 3. manifest + cleanup
    manifest = {
        "source_zip": str(zip_path),
        "project_name": project,
        "tender_dir": str(tender_dir),
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "artifacts": sorted(p.name for p in tender_dir.iterdir()
                            if p.is_file() and p.suffix.lower() == ".pdf"),
        "subdirs": sorted(p.name for p in tender_dir.iterdir()
                          if p.is_dir() and not p.name.startswith(("_", "."))),
        "warnings": warnings,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(staging, ignore_errors=True)

    log("── result ──")
    for p in sorted(tender_dir.iterdir()):
        if p.name.startswith(("_", ".")):
            continue
        mark = "/" if p.is_dir() else f"  ({p.stat().st_size/1024:.0f} KB)"
        log(f"  {'DIR ' if p.is_dir() else 'FILE'} {p.name}{mark}")
    for w in warnings:
        log(f"  ⚠ {w}")

    ok = (tender_dir / "招标文件.pdf").exists()
    log("")
    if ok:
        log(f"✓ 下一步:  python3 scripts/bid_run.py \"{tender_dir}\" --doctor")
        log(f"           python3 scripts/bid_run.py \"{tender_dir}\"")
        return 0
    log("✗ 招标文件.pdf 缺失 —— 无法进入 Step 1")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
