#!/usr/bin/env python3
"""
bid_plan_suggest.py — chapter_plan 草稿生成器(Phase 4.1)

减少 Step 2 人工瓶颈:从 mandatory_outline.json + evaluation_method.json
+ project_derived.json + disqualification_rules.md 自动生成 chapter_plan_draft.md,
Claude 在 Step 2 只需"审阅 + 调整"草稿,而不是从零写。

算法:
1. 读 evaluation_method.json 判断 score / passfail
2. 读 mandatory_outline.json 取 topics(合格制下章节锚点 = 主题 1:1)
3. 评分制下:章节锚点 = scoring_items 评分项;大纲主题补漏
4. 读 disqualification_rules.md(若存在)取字号/行距 → bid_derive 查 Word 实测密度
5. 读 project_derived.json 取 size_class 给页数判档(70 页 / 100 页 / 200 页)
6. 分配字数:
   - 合格制:按主题数等分章节字数,再按 ±15% 浮动
   - 评分制:按分值比例分章节字数
7. 输出 chapter_plan_draft.md(yaml 风格表),供 Claude 审阅

退出码:
  0  草稿生成成功
  1  缺少前置 JSON(facts/derived/skeleton),无法生成
  2  目录或文件读取异常

用法:
  python bid_plan_suggest.py /path/to/tender/
  python bid_plan_suggest.py /path/to/tender/ --target-pages 70 --figure-ratio 0.25
  python bid_plan_suggest.py /path/to/tender/ --force  # 覆盖已有 chapter_plan_draft.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_common import make_script_logger  # noqa: E402
from bid_derive import (  # noqa: E402
    chars_per_page,
    parse_format_spec_from_rules,
    recommend_target_chars,
)


OK, WARN, FAIL = "✓", "⚠", "✗"


def _target_pages_from_size(size_class: str, override: int | None) -> int:
    """根据 size_class 给默认目标页数(西藏简易综合评估法常见档位)。"""
    if override:
        return override
    mapping = {
        "small": 50,            # 极小型,大多数情况 50 页足够
        "small_medium": 70,     # 中小型,西藏 70 页硬顶最常见
        "medium": 100,
        "large": 200,
        "extra_large": 290,     # 大型,留 10 页安全余量
        "unknown": 70,          # 不知规模时按中小型保守
    }
    return mapping.get(size_class, 70)


def suggest_chapter_plan(tender_dir: Path, args, log) -> int:
    ext = tender_dir / "extracted"

    # ─── 1. 读前置文件 ───
    eval_path = ext / "evaluation_method.json"
    outline_path = ext / "mandatory_outline.json"
    derived_path = ext / "project_derived.json"
    # Phase 3:优先 authoring_rules.md(方案规则),回退 disqualification_rules.md
    from bid_common import preferred_rules_path
    rules_path = preferred_rules_path(ext)

    for p, name in [(eval_path, "evaluation_method.json"),
                    (outline_path, "mandatory_outline.json")]:
        if not p.exists():
            log.error(f"{FAIL} 缺少 {name},先跑 bid_skeleton_extract.py")
            return 1

    eval_data = json.loads(eval_path.read_text(encoding="utf-8"))
    outline_data = json.loads(outline_path.read_text(encoding="utf-8"))
    derived = json.loads(derived_path.read_text(encoding="utf-8")) if derived_path.exists() else {}

    method = eval_data.get("evaluation_method", "score")
    is_passfail = (method == "passfail")
    log.info(f"评标方法: {method}{' (合格制)' if is_passfail else ' (评分制)'}")

    # ─── 2. 字号/行距 → 字数密度 ───
    font_pt = 14.0
    line_kind = "multiple"
    line_value = 1.5
    if rules_path.exists():
        rules_text = rules_path.read_text(encoding="utf-8")
        spec = parse_format_spec_from_rules(rules_text)
        if spec.get("font_size_pt"):
            font_pt = spec["font_size_pt"]
        if spec.get("line_spacing_kind"):
            line_kind = spec["line_spacing_kind"]
        if spec.get("line_spacing_value"):
            line_value = spec["line_spacing_value"]
    density_lo, density_hi = chars_per_page(font_pt, line_kind, line_value)
    density_mid = (density_lo + density_hi) // 2
    log.info(f"印刷规则: {font_pt}pt + {line_value}{'倍' if line_kind=='multiple' else '磅'} → "
             f"密度 {density_lo}-{density_hi} 字/页(中位 {density_mid})")

    # ─── 3. 目标页数 ───
    size_class = derived.get("size_class", "unknown")
    target_pages = _target_pages_from_size(size_class, args.target_pages)
    figure_ratio = args.figure_ratio
    rec = recommend_target_chars(target_pages, font_pt, line_kind, line_value, figure_ratio)
    log.info(f"目标:{target_pages} 页(size_class={size_class}),图表占比 {figure_ratio:.0%},"
             f"目标字数 {rec['target_chars_mid']:,}(区间 {rec['target_chars_lo']:,}-{rec['target_chars_hi']:,})")

    # ─── 4. 章节锚点 ───
    topics = outline_data.get("topics") or []
    scoring_items = eval_data.get("scoring_items") or []

    if is_passfail:
        # 合格制:章节 1:1 对齐 outline topics
        if not topics:
            log.error(f"{FAIL} 合格制下 mandatory_outline.topics 为空,无法生成草稿")
            return 1
        chapters = [
            {
                "num": f"{i+1:02d}",
                "title": t.get("title_original", ""),
                "score": 0,
                "subitems": t.get("subsections_original") or [],
                "source": "mandatory_outline",
            }
            for i, t in enumerate(topics)
        ]
        target_chars_per_chap = rec["target_chars_mid"] // len(chapters)
        for ch in chapters:
            ch["target_chars"] = target_chars_per_chap
    else:
        # 评分制:按评分项分章,大纲主题补漏未覆盖项
        chapters = []
        for i, item in enumerate(scoring_items):
            score = item.get("score") or 0
            if score == 0:
                continue
            chapters.append({
                "num": f"{i+1:02d}",
                "title": item.get("title_original", ""),
                "score": score,
                "subitems": [],
                "source": "scoring_items",
            })
        total_score = sum(c["score"] for c in chapters) or 1
        for ch in chapters:
            ch["target_chars"] = int(rec["target_chars_mid"] * ch["score"] / total_score)
        # outline 补漏:大纲有但评分表没的主题
        for i, t in enumerate(topics):
            t_title = t.get("title_original", "")
            if not any(t_title and t_title in c["title"] for c in chapters):
                chapters.append({
                    "num": f"{len(chapters)+1:02d}",
                    "title": t_title,
                    "score": 0,
                    "subitems": t.get("subsections_original") or [],
                    "source": "outline_补漏",
                    "target_chars": rec["target_chars_mid"] // (len(chapters) + 1) // 2,
                })

    # ─── 5. 输出 chapter_plan_draft.md ───
    out_path = ext / "chapter_plan_draft.md"
    if out_path.exists() and not args.force:
        log.info(f"{WARN} {out_path.name} 已存在(--force 覆盖)")
        return 0

    lines: list[str] = []
    lines.append("# 章节规划表(草稿) — Claude 审阅 + 调整后改名为 chapter_plan.md")
    lines.append("")
    lines.append("> 由 `bid_plan_suggest.py` 自动生成,基于 mandatory_outline.json + evaluation_method.json + project_derived.json + disqualification_rules.md。")
    lines.append("> Claude 审阅时:核对标题字面、调整子项数、调整图表数、按需修改字数预算。")
    lines.append("")
    lines.append("## 总量预算(第一圈)")
    lines.append("")
    lines.append(f"**项目规模判档:** {size_class}")
    lines.append(f"**判档依据:** project_derived.size_class(基于控制价)")
    lines.append(f"**建议目标页数:** {target_pages} 页")
    lines.append(f"**字数密度推算(Word 实测):**")
    lines.append(f"- 印刷规则:{font_pt}pt + {line_value}{'倍' if line_kind=='multiple' else '磅'} 行距")
    lines.append(f"- 查表 → **{density_mid} 字/页**(区间 {density_lo}-{density_hi})")
    lines.append(f"- 图表占比 **{figure_ratio:.0%}** → 图表页 {rec['figure_pages']},文字页 {rec['text_pages']}")
    lines.append(f"**目标总字数:** {rec['text_pages']} × {density_mid} ≈ **{rec['target_chars_mid']:,} 字**")
    lines.append(f"**字数区间(保守-最大):** {rec['target_chars_lo']:,}-{rec['target_chars_hi']:,}")
    lines.append("")
    lines.append("## 大章节分配(第二圈)")
    lines.append("")
    lines.append("| 章节号 | 文件名 | 标题 | 评分项 | 分值 | 子项数 | 目标字数 | 字数带 ±15% | 图表数 | 页数估 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for ch in chapters:
        # 文件名 = num + 章节标题首词
        safe_title = "".join(c for c in ch["title"][:8] if c not in r'\/:*?"<>|')
        fname = f"{ch['num']}_{safe_title}.md"
        subitem_count = len(ch["subitems"]) or 1
        chars = ch["target_chars"]
        lo = int(chars * 0.85)
        hi = int(chars * 1.15)
        pages = chars / density_mid + 0  # 默认 0 图表,Claude 自己加
        lines.append(
            f"| {ch['num']} | {fname} | {ch['title']} | {ch['source']} | "
            f"{ch['score']} | {subitem_count} | {chars:,} | {lo:,}-{hi:,} | 0 | {pages:.1f} |"
        )
    total_chars_actual = sum(c["target_chars"] for c in chapters)
    total_score = sum(c["score"] for c in chapters)
    lines.append(f"| **合计** | | | | **{total_score}** | | **{total_chars_actual:,}** | | **0** | **{total_chars_actual/density_mid:.0f}** |")
    lines.append("")
    lines.append("## 子项分配(第三圈) — 待 Claude 展开")
    lines.append("")
    for ch in chapters:
        lines.append(f"### {ch['num']} {ch['title']}({ch['target_chars']:,} 字, 0 图表, {ch['target_chars']/density_mid:.0f} 页)")
        if ch["subitems"]:
            avg = ch["target_chars"] // max(len(ch["subitems"]), 1)
            for i, sub in enumerate(ch["subitems"]):
                lines.append(f"- {ch['num']}.{i+1} {sub}:{avg:,} 字")
        else:
            lines.append(f"- (TODO: Claude 根据评分细则展开子项,字数总和 = {ch['target_chars']:,})")
        lines.append("")
    lines.append("**总分核对:** 各章分值之和 = " + str(total_score))
    lines.append(f"**页数核对:** Σ(字数/{density_mid}) + Σ(图表×0.4) = {total_chars_actual/density_mid:.0f} 页 ≤ 目标 {target_pages} ✓" if total_chars_actual/density_mid <= target_pages * 1.05 else f"**页数核对:** ⚠ Σ(字数/{density_mid}) = {total_chars_actual/density_mid:.0f} 页 超出目标 {target_pages}")
    lines.append("")
    lines.append("---")
    lines.append("**审阅清单:**")
    lines.append("- [ ] 标题字面是否 1:1 抄录原文(尤其合格制下)?")
    lines.append("- [ ] 评分制下:章节顺序是否对齐评分表打分顺序?")
    lines.append("- [ ] 图表数 = 0 是草稿默认,需按工程类型调整(施工方案密集型 25-30%)")
    lines.append("- [ ] 子项标题是否符合评分细则(评分制)/ 大纲二级节(合格制)?")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"{OK} {out_path.name} 已生成,{len(chapters)} 章,目标 {total_chars_actual:,} 字")
    log.info(f"  下一步:Claude 审阅 {out_path.name},满意后改名为 chapter_plan.md")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--target-pages", type=int, default=None, help="覆盖默认页数判档")
    p.add_argument("--figure-ratio", type=float, default=0.25, help="图表页占比(默认 0.25)")
    p.add_argument("--force", action="store_true", help="覆盖已有 chapter_plan_draft.md")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"{FAIL} 目录不存在: {tender_dir}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_plan_suggest",
                             tender_dir / "extracted" / "plan_suggest.log",
                             verbose=args.verbose)
    return suggest_chapter_plan(tender_dir, args, log)


if __name__ == "__main__":
    sys.exit(main())
