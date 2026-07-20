#!/usr/bin/env python3
"""用 OfficeCLI 对已装配 DOCX 应用可重放的视觉 profile。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from bid_common import make_script_logger
from bid_assemble import _apply_docx_metadata, parse_format_rules
from bid_common import canonical_rules_path
from bid_visual_rules import compile_visual_rules, load_variants, normalize_tender_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("OFFICECLI_RESIDENT_FLUSH", "each")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _validate_report(officecli: str, docx: Path) -> dict:
    proc = _run([officecli, "validate", str(docx), "--json"], timeout=180)
    raw = proc.stdout or proc.stderr
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"returncode": proc.returncode, "count": 1 if proc.returncode else 0,
                "signatures": {raw[-500:]}, "kinds": {raw[-500:]},
                "raw": raw[-4000:]}
    warnings = data.get("warnings", []) if isinstance(data, dict) else []
    signatures = set()
    kinds = set()
    current = ""
    path = ""
    for item in warnings:
        msg = str(item.get("message", "")) if isinstance(item, dict) else str(item)
        if msg.startswith("[Schema]"):
            if current:
                signatures.add(f"{current}|{path}")
                kinds.add(current)
            current, path = msg, ""
        elif msg.startswith("Path:"):
            path = msg
    if current:
        signatures.add(f"{current}|{path}")
        kinds.add(current)
    count = 0
    if warnings:
        match = re.search(r"Found (\d+) validation error", str(warnings[0].get("message", "")))
        count = int(match.group(1)) if match else len(signatures)
    return {"returncode": proc.returncode, "count": count,
            "signatures": signatures, "kinds": kinds, "raw": raw[-4000:]}


def _validation_delta(before: dict, after: dict) -> list[str]:
    """返回新增校验错误；OfficeCLI 的 1000 条截断场景按错误类型比较。"""
    if before.get("count", 0) >= 1000 and after.get("count", 0) >= 1000:
        return sorted(after.get("kinds", set()) - before.get("kinds", set()))
    return sorted(after.get("signatures", set()) - before.get("signatures", set()))


def _query_results(officecli: str, docx: Path, selector: str) -> list[dict]:
    result = _run([officecli, "query", str(docx), selector, "--json"])
    if result.returncode != 0:
        raise RuntimeError(f"OfficeCLI query 失败({selector}): {result.stderr or result.stdout}")
    data = json.loads(result.stdout)
    payload = data.get("data", {})
    return [x for x in payload.get("results", []) if isinstance(x, dict)]


def _query_paths(officecli: str, docx: Path, selector: str) -> list[str]:
    return [x["path"] for x in _query_results(officecli, docx, selector) if x.get("path")]


def _cell_border_props(grammar: str, border: str, row: int, col: int,
                       max_row: int, max_col: int) -> dict:
    """逐单元格编译边框，避开 OfficeCLI 1.0.136 的 tblPr 子节点顺序问题。"""
    props = {"border.all": "none"}
    if grammar == "full_grid":
        return {"border.all": border}
    horizontal = grammar in {"horizontal_rules", "frame_horizontal", "light_horizontal"}
    frame = grammar in {"frame_horizontal", "outer_frame"}
    if row == 1 and grammar in {"horizontal_rules", "frame_horizontal", "top_bottom", "outer_frame", "light_horizontal"}:
        props["border.top"] = border
    if (horizontal or row == max_row) and grammar != "outer_frame":
        props["border.bottom"] = border
    if row == max_row and grammar == "outer_frame":
        props["border.bottom"] = border
    if frame and col == 1:
        props["border.left"] = border
    if frame and col == max_col:
        props["border.right"] = border
    return props


def build_commands(officecli: str, docx: Path, rules: dict) -> list[dict]:
    allowed = rules["allowed"]
    commands: list[dict] = []
    style_paths: dict[str, str] = {}
    for item in _query_results(officecli, docx, "style"):
        fmt = item.get("format", {}) if isinstance(item.get("format"), dict) else {}
        aliases = {item.get("text", ""), fmt.get("id", ""), fmt.get("name", "")}
        for alias in aliases:
            normalized = re.sub(r"[^a-z0-9]", "", str(alias).lower())
            if normalized:
                style_paths.setdefault(normalized, str(item.get("path", "")))
    heading_map = {
        "Heading2": ("h2_align", "h2_space_before_pt", "h2_space_after_pt"),
        "Heading3": ("h3_align", "h3_space_before_pt", "h3_space_after_pt"),
    }
    for style, (align_key, before_key, after_key) in heading_map.items():
        style_path = style_paths.get(style.lower())
        if not style_path:
            continue
        props = {}
        if align_key in allowed:
            props["align"] = allowed[align_key]
        if before_key in allowed:
            props["spaceBefore"] = f"{allowed[before_key]}pt"
        if after_key in allowed:
            props["spaceAfter"] = f"{allowed[after_key]}pt"
        if props:
            commands.append({"command": "set", "path": style_path, "props": props})

    cell_paths = _query_paths(officecli, docx, "cell")
    width = max(1, round(float(rules["format"]["line_thickness_pt"]) * 8))
    color = rules["format"]["line_color"]
    border = f"single;{width};{color}"
    cells = []
    for path in cell_paths:
        match = re.search(r"(/body/tbl\[(\d+)\])/tr\[(\d+)\]/tc\[(\d+)\]$", path)
        if match:
            cells.append((path, match.group(1), int(match.group(3)), int(match.group(4))))
    bounds: dict[str, tuple[int, int]] = {}
    for _path, table, row, col in cells:
        old = bounds.get(table, (0, 0))
        bounds[table] = (max(old[0], row), max(old[1], col))
    for path, table, row, col in cells:
        props = {}
        if "table_padding_twips" in allowed:
            pad = str(int(allowed["table_padding_twips"]))
            props.update({"marginTop": pad, "marginBottom": pad,
                          "marginLeft": pad, "marginRight": pad})
        grammar = str(allowed.get("table_grammar", ""))
        if grammar:
            props.update(_cell_border_props(grammar, border, row, col, *bounds[table]))
        if props:
            commands.append({"command": "set", "path": path, "props": props})

    if "image_align" in allowed:
        for image_path in _query_paths(officecli, docx, "image"):
            parent = image_path.split("/r[")[0]
            # OfficeCLI 1.0.136 may reorder existing w:pPr children when a
            # paragraph inside a table cell is updated.  Legacy bid documents
            # commonly contain w:ind there, which then becomes invalid OOXML.
            # Keep cell content untouched; table differentiation is already
            # provided by cell margins and borders.
            if "/p[" in parent and "/tc[" not in parent:
                commands.append({"command": "set", "path": parent,
                                 "props": {"align": allowed["image_align"]}})
    return commands


def discover_docx(tender_dir: Path, variant: str = "") -> list[tuple[str, Path]]:
    output = tender_dir / "output"
    batch = sorted(output.glob("V[0-9][0-9]_*.docx"))
    if batch:
        out = [(p.name.split("_", 1)[0], p) for p in batch]
    else:
        out = [("", p) for p in sorted(output.glob("*.docx"))]
    return [(v, p) for v, p in out if not variant or v == variant]


def apply_one(tender_dir: Path, variant_id: str, docx: Path, officecli: str,
              dry_run: bool, log: logging.Logger) -> dict:
    rules = compile_visual_rules(tender_dir, variant_id)
    before = _sha256(docx)
    # 临时产物归隐藏工作目录，不进 logs/（feedback_bid_output_layout §1）
    work_dir = tender_dir / "output" / ".visual_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / f"{docx.stem}.visual.tmp.docx"
    shutil.copy2(docx, tmp)
    baseline_validation = _validate_report(officecli, tmp)
    commands = build_commands(officecli, tmp, rules)
    result = {
        "variant_id": variant_id or "single", "docx": docx.name,
        "profile": rules["profile"], "description": rules["description"],
        "before_sha256": before, "commands": commands,
        "rejected": rules["rejected"], "dry_run": dry_run,
        "validation_before_count": baseline_validation["count"],
    }
    try:
        if dry_run:
            result["status"] = "dry_run"
            return result
        batch_path = work_dir / f"{docx.stem}.officecli.batch.json"
        batch_path.write_text(json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8")
        if commands:
            proc = _run([officecli, "batch", str(tmp), "--input", str(batch_path),
                         "--stop-on-error", "--json"], timeout=300)
            result["batch_stdout"] = proc.stdout[-4000:]
            result["batch_stderr"] = proc.stderr[-2000:]
            if proc.returncode != 0:
                raise RuntimeError(f"OfficeCLI batch 失败: {proc.stderr or proc.stdout}")
        _run([officecli, "close", str(tmp)], timeout=60)
        final_validation = _validate_report(officecli, tmp)
        new_errors = _validation_delta(baseline_validation, final_validation)
        result["validation_after_count"] = final_validation["count"]
        result["validation_new_errors"] = new_errors[:20]
        result["validate"] = final_validation["raw"]
        if new_errors:
            raise RuntimeError(
                f"OfficeCLI 修改新增 {len(new_errors)} 类 OpenXML 校验错误: "
                + "; ".join(new_errors[:3])
            )

        # OfficeCLI 保存后重新执行既有元数据与指纹清理，避免新增同源指纹。
        variants = load_variants(tender_dir)
        variant = next((v for v in variants if v.get("id") == variant_id), None)
        fmt = parse_format_rules(canonical_rules_path(tender_dir / "extracted"), log)
        # 视觉层只负责元数据与指纹收尾。字体/字号仍以装配结果和招标
        # 硬规则为准，不能复用 Word 页数检查后的字体修复副作用。
        _apply_docx_metadata(tmp, variant, log, fmt=fmt, reapply_fonts=False)
        os.replace(tmp, docx)
        result["after_sha256"] = _sha256(docx)
        result["status"] = "ok"
        return result
    except Exception:
        result["status"] = "failed"
        raise
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="OfficeCLI 投标 DOCX 视觉编排")
    parser.add_argument("tender_dir")
    parser.add_argument("--variant", default="")
    parser.add_argument("--officecli", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    tender = normalize_tender_dir(Path(args.tender_dir))
    officecli = args.officecli or shutil.which("officecli") or str(Path.home() / ".local/bin/officecli")
    if not Path(officecli).exists():
        print("OfficeCLI 未安装。运行: curl -fsSL https://d.officecli.ai/install.sh | bash", file=sys.stderr)
        return 2
    log = make_script_logger("visual_style", tender / "output" / "logs" / "visual_style.log")
    targets = discover_docx(tender, args.variant.upper())
    if not targets:
        log.error("未找到待处理 DOCX")
        return 2
    report = {"officecli_version": _run([officecli, "--version"]).stdout.strip(), "results": []}
    try:
        for vid, docx in targets:
            log.info(f"视觉编排: {vid or 'single'} → {docx.name}")
            report["results"].append(apply_one(tender, vid, docx, officecli, args.dry_run, log))
    except Exception as e:
        log.error(str(e), exc_info=True)
        report["error"] = str(e)
        code = 1
    else:
        code = 0
    reports = tender / "output" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "visual_style_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
