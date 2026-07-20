#!/usr/bin/env python3
"""用 OfficeCLI 为最终 DOCX 生成结构快照与全篇联系表。"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from bid_common import make_script_logger
from bid_officecli_style import discover_docx
from bid_visual_rules import normalize_tender_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tender_dir")
    parser.add_argument("--variant", default="")
    parser.add_argument("--officecli", default="")
    args = parser.parse_args()
    tender = normalize_tender_dir(Path(args.tender_dir))
    officecli = args.officecli or shutil.which("officecli") or str(Path.home() / ".local/bin/officecli")
    if not Path(officecli).exists():
        return 2
    log = make_script_logger("visual_render", tender / "output" / "logs" / "visual_render.log")
    root = tender / "output" / "reports" / "visual"
    root.mkdir(parents=True, exist_ok=True)
    results = []
    code = 0
    for vid, docx in discover_docx(tender, args.variant.upper()):
        label = vid or "single"
        dest = root / label
        dest.mkdir(parents=True, exist_ok=True)
        item = {"variant_id": label, "docx": docx.name}
        for mode in ("outline", "issues", "stats"):
            p = subprocess.run([officecli, "view", str(docx), mode, "--json"],
                               capture_output=True, text=True, timeout=180)
            (dest / f"{mode}.json").write_text(p.stdout or p.stderr, encoding="utf-8")
            item[mode] = p.returncode
            code = max(code, 1 if p.returncode else 0)
        shot = dest / "contact_sheet.png"
        p = subprocess.run([officecli, "view", str(docx), "screenshot", "--grid", "auto",
                            "--render", "html", "-o", str(shot)],
                           capture_output=True, text=True, timeout=600)
        item["screenshot"] = p.returncode
        item["screenshot_path"] = str(shot) if shot.exists() else ""
        if p.returncode:
            log.warning(f"{label} 联系表失败: {(p.stderr or p.stdout)[:300]}")
            code = 1
        results.append(item)
    (root / "render_report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
