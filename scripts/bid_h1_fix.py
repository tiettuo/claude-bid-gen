#!/usr/bin/env python3
"""
bid_h1_fix.py - post-writer 自动修正章节 H1 字面

下游 LLM(DeepSeek/GLM/Qwen)写章节时倾向给 H1 加"施工"前缀或同义改写
(参 feedback_writer_h1_drift)。合格制项目下 H1 必须 1:1 招标原文,
否则评委判"主题缺失"= 失去成交资格。

本脚本扫 output/V*/0?_*.md(单份:output/0?_*.md),首行 H1 与 chapter_plan.md
对应章节标题做字面 diff,不一致即覆写为 chapter_plan 标题。

用法:
  python bid_h1_fix.py /path/to/tender/
  python bid_h1_fix.py /path/to/tender/ --dry-run  # 只报告不修改
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"


def parse_chapter_plan_titles(plan_path: Path) -> dict[str, str]:
    """解析 chapter_plan.md 大章节表,返回 {章节号: 招标原文标题}。
    示例行:`| 01 | 01_项目人员配备.md | 项目人员配备 | ... |`
    """
    titles: dict[str, str] = {}
    if not plan_path.exists():
        return titles
    text = plan_path.read_text(encoding="utf-8")
    # 匹配章节表行
    pattern = re.compile(r"^\|\s*(\d{2})\s*\|\s*\d{2}_[^|]+\|\s*([^|]+?)\s*\|", re.MULTILINE)
    for m in pattern.finditer(text):
        ch_num = m.group(1)
        title = m.group(2).strip()
        if title and not title.startswith("**"):  # 跳过合计行
            titles[ch_num] = title
    return titles


def fix_chapter_h1(md_path: Path, expected: str, dry_run: bool = False) -> tuple[bool, str]:
    """检查 md_path 首行 H1 是否等于 expected。返回 (changed, current_title)。"""
    if not md_path.exists():
        return (False, "")
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n", 1)
    if not lines or not lines[0].startswith("# "):
        return (False, "")
    current = lines[0][2:].strip()
    if current == expected:
        return (False, current)
    # 字面不一致,修正
    if not dry_run:
        new_text = f"# {expected}\n" + (lines[1] if len(lines) > 1 else "")
        md_path.write_text(new_text, encoding="utf-8")
    return (True, current)


def fix_directory(out_dir: Path, plan_titles: dict[str, str],
                  dry_run: bool, log) -> int:
    """扫一个目录(output/ 或 output/V01/)下的章节文件,修正 H1。"""
    changed_count = 0
    checked_count = 0
    for md_path in sorted(out_dir.glob("[0-9][0-9]_*.md")):
        ch_num = md_path.name[:2]
        expected = plan_titles.get(ch_num)
        if not expected:
            log.warning(f"  {md_path.name}: chapter_plan 无对应章节号 {ch_num}")
            continue
        changed, current = fix_chapter_h1(md_path, expected, dry_run)
        checked_count += 1
        if changed:
            verb = "[dry-run] 会修正" if dry_run else "✓ 修正"
            log.info(f"  {verb} {md_path.name}: '{current}' → '{expected}'")
            changed_count += 1
    log.info(f"  {out_dir.name}: 检查 {checked_count} 章,修正 {changed_count} 章")
    return changed_count


def fix(tender_dir: Path, dry_run: bool = False, log=None) -> int:
    """主入口。"""
    ext = tender_dir / "extracted"
    base_titles = parse_chapter_plan_titles(ext / "chapter_plan.md")

    output = tender_dir / "output"
    if not output.is_dir():
        log.warning("  output/ 不存在,跳过")
        return 0

    total_changed = 0
    variant_dirs = sorted(output.glob("V[0-9][0-9]"))
    if variant_dirs:
        # 批量模式:每个变体优先用自己的 chapter_plan_variant.md。变体结构独立,
        # 章节标题已按反串标改写,用基准 chapter_plan.md 会把它改回去、毁掉差异。
        for vd in variant_dirs:
            v_plan = vd / "chapter_plan_variant.md"
            titles = (parse_chapter_plan_titles(v_plan) if v_plan.exists() else {}) or base_titles
            if not titles:
                log.error(f"  {vd.name}: 无章节标题来源(变体计划与 chapter_plan.md 均缺),跳过")
                continue
            src = "变体计划" if v_plan.exists() and parse_chapter_plan_titles(v_plan) else "chapter_plan.md(基准)"
            log.info(f"  {vd.name}: H1 校验基准 = {src}({len(titles)} 章)")
            total_changed += fix_directory(vd, titles, dry_run, log)
    else:
        # 单份模式: output/
        if not base_titles:
            log.error("  chapter_plan.md 未找到章节标题")
            return 2
        total_changed += fix_directory(output, base_titles, dry_run, log)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--dry-run", action="store_true", help="只报告不修改")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    log = make_script_logger("bid_h1_fix", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_h1_fix  目录: {tender_dir}")
    code = fix(tender_dir, dry_run=args.dry_run, log=log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
