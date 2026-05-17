#!/usr/bin/env python3
"""
bid_status.py - /bid-gen 状态面板（只读）

扫描投标目录，打印当前处于哪一步、哪些 variant 写到多少章、
是否有 major/超阈值/API key 缺失等阻断项。

用法:
  python bid_status.py <tender_dir>
  watch -n 5 python bid_status.py <tender_dir>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


STEP2_FILES = ["project_brain.md", "chapter_plan.md", "disqualification_rules.md"]


def yn(flag: bool) -> str:
    return "✓" if flag else "✗"


def _chapter_count(v_dir: Path) -> int:
    return sum(1 for p in v_dir.glob("*.md")
               if p.stem[:1].isdigit() and "报告" not in p.name and "检查" not in p.name)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python bid_status.py <tender_dir>")
        return 2
    root = Path(sys.argv[1]).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 2

    ext = root / "extracted"
    out = root / "output"

    print(f"\n══ 投标目录 {root.name} ══\n")

    # Step 1: extract
    print("Step 1 (extract) ──")
    if ext.is_dir():
        found = list(ext.glob("*.md"))
        print(f"  {yn(bool(found))} extracted/ 存在，含 {len(found)} 个 md")
    else:
        print(f"  ✗ extracted/ 不存在")

    # Step 2: brain/plan/rules
    print("\nStep 2 (brain/plan/rules) ──")
    for f in STEP2_FILES:
        p = ext / f
        size = p.stat().st_size if p.exists() else 0
        print(f"  {yn(p.exists())} {f}  ({size:,} B)")

    # Step 2.5 variants
    print("\nStep 2.5 (variants) ──")
    vj = ext / "variants.json"
    is_batch = vj.exists()
    if is_batch:
        data = json.loads(vj.read_text(encoding="utf-8"))
        print(f"  ✓ variants.json  ({data.get('variant_count')} 家，blind={data.get('blind_bid')})")
    else:
        print(f"  · 单份模式（无 variants.json）")

    # Step 3: write - per variant
    print("\nStep 3 (write) / Step 4 (assemble) / Step 5 (compliance) ──")
    chapters_expected = 0
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from bid_common import parse_chapter_plan
        plan_rows = parse_chapter_plan(ext / "chapter_plan.md")
        chapters_expected = len(plan_rows)
    except Exception:
        pass
    print(f"  计划章节数: {chapters_expected}\n")

    header = f"  {'Variant':<8}{'章节':<10}{'Review':<12}{'合规':<10}{'DOCX':<30}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    blockers: list[str] = []
    if is_batch:
        for v in data["variants"]:
            vid = v["id"]
            v_dir = out / vid
            ch_count = _chapter_count(v_dir) if v_dir.is_dir() else 0
            ch_str = f"{ch_count}/{chapters_expected or '?'}"

            rev = v_dir / "review.json"
            rev_str = "-"
            if rev.exists():
                d = json.loads(rev.read_text(encoding="utf-8"))
                rev_str = d.get("overall_severity", "-")
                if rev_str == "major":
                    blockers.append(f"{vid} review=major")

            scan = v_dir / "compliance_scan.json"
            comp_str = "-"
            if scan.exists():
                d = json.loads(scan.read_text(encoding="utf-8"))
                ok = (not d.get("blind_violations")) and all(
                    r.get("present") for r in d.get("response_check", []) if r.get("required")
                )
                comp_str = "✓" if ok else "❌"
                if not ok:
                    blockers.append(f"{vid} 合规未过")

            docx = next(out.glob(f"{vid}_*.docx"), None)
            docx_str = docx.name[:28] if docx else "-"

            print(f"  {vid:<8}{ch_str:<10}{rev_str:<12}{comp_str:<10}{docx_str:<30}")
    else:
        ch_count = _chapter_count(out) if out.is_dir() else 0
        rev = out / "review.json"
        rev_str = "-"
        if rev.exists():
            d = json.loads(rev.read_text(encoding="utf-8"))
            rev_str = d.get("overall_severity", "-")
        scan = out / "compliance_scan.json"
        comp_str = "-"
        if scan.exists():
            d = json.loads(scan.read_text(encoding="utf-8"))
            ok = (not d.get("blind_violations")) and all(
                r.get("present") for r in d.get("response_check", []) if r.get("required")
            )
            comp_str = "✓" if ok else "❌"
        docx = next(out.glob("*.docx"), None) if out.is_dir() else None
        docx_str = docx.name[:28] if docx else "-"
        print(f"  {'单份':<8}{f'{ch_count}/{chapters_expected or chr(63)}':<10}"
              f"{rev_str:<12}{comp_str:<10}{docx_str:<30}")

    # Step 6: similarity
    if is_batch:
        print("\nStep 6 (similarity) ──")
        sim = out / "similarity_matrix.json"
        if sim.exists():
            mat = json.loads(sim.read_text(encoding="utf-8"))
            pairs = mat.get("pairs", [])
            th = {"overall": 0.40, "paragraph": 0.50}
            th.update(data.get("similarity_threshold", {}))
            over = [p for p in pairs
                    if p["overall"] > th["overall"] or p["paragraph_max"] > th["paragraph"]]
            print(f"  对比对数: {len(pairs)}  超阈值: {len(over)}")
            for p in over[:5]:
                print(f"    ❌ {p['a']}↔{p['b']}  整体={p['overall']:.1%}  段落={p['paragraph_max']:.1%}")
                blockers.append(f"相似度超阈 {p['a']}↔{p['b']}")
        else:
            print("  · 未运行")

    # checkpoint
    state = out / ".bid_run.state.json"
    if state.exists():
        print("\nbid_run 进度 ──")
        s = json.loads(state.read_text(encoding="utf-8"))
        for name, info in s.get("steps", {}).items():
            mark = {"ok": "✓", "fail": "✗", "running": "⋯", "skip": "·"}.get(info.get("status"), "?")
            print(f"  {mark} {name:<14}{info.get('status','-'):<10}exit={info.get('exit_code','-')}")

    # API key
    from bid_common import get_bid_gen_home as _ggh
    cfg = _ggh() / "configs" / "bid_llm_config.json"
    if cfg.exists():
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
            missing_env = [m["api_key_env"] for _, m in c.get("models", {}).items()
                           if m.get("api_key_env") and not os.environ.get(m["api_key_env"])]
            if missing_env:
                for e in missing_env:
                    blockers.append(f"env 未设置: {e}")
        except Exception:
            pass

    print("\n══ 阻断项 ══")
    if blockers:
        for b in blockers:
            print(f"  ❌ {b}")
        return 1
    print("  ✓ 无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
