#!/usr/bin/env python3
"""
bid_brain_translate.py - 从 JSON 骨架翻译生成 brain.md

把 bid_skeleton_extract.py + bid_facts_extract.py + bid_derive.py 等
产出的 7 份 JSON 翻译成 v2 合规的 project_brain.md。

第 1-4 节:用 facts/derived/special_clauses 数据自动组织
第 5 节:1:1 翻译 evaluation_method.json
第 6 节:1:1 翻译 disqualification.json(按 category 分类)
第 7 节:1:1 翻译 mandatory_outline.json(必含主题 + 附表)

如果旧 brain.md 已存在,会读取其第 3 节(技术重点与施工难点)的人工分析
保留下来——LLM 抽不出的项目独有的难点判断。

用法:
  python bid_brain_translate.py /path/to/tender/
  python bid_brain_translate.py /path/to/tender/ --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger, repair_technical_criteria_alignment  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"


def _load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _val(facts: dict, key: str, default=""):
    """从 facts 取字段值。"""
    v = facts.get(key)
    if isinstance(v, dict):
        return v.get("value") or default
    return v or default


def extract_old_section(old_brain: str, section_num: str) -> str:
    """从旧 brain.md 抽取指定节的正文(用于保留人工分析)。"""
    pat = rf"##\s*{section_num}[、.\s][^\n]*\n(.*?)(?=\n##\s|\Z)"
    m = re.search(pat, old_brain, re.S)
    return m.group(1).strip() if m else ""


def section_1_basic_info(facts: dict, derived: dict | None,
                         template_match: dict | None) -> str:
    """第 1 节:项目基本信息(从 tier1/tier2 数据自动组织)"""
    lines = ["## 一、项目基本信息", ""]
    fields = [
        ("项目名称", _val(facts, "project_name")),
        ("招标编号", _val(facts, "project_code")),
        ("建设地点", _val(facts, "location")),
        ("招标人", _val(facts, "client")),
        ("招标代理机构", _val(facts, "agent")),
        ("资金来源", _val(facts, "funding_source")),
        ("招标范围", _val(facts, "project_scope")),
        ("招标控制价", f"{_val(facts, 'control_price'):,.2f} 元" if _val(facts, "control_price") else ""),
        ("工期要求", f"{_val(facts, 'duration_days')} 日历天" if _val(facts, "duration_days") else ""),
        ("计划开工", _val(facts, "start_date")),
        ("计划竣工", _val(facts, "end_date")),
        ("质量要求", _val(facts, "quality_target")),
        ("资质要求", _val(facts, "qualification")),
    ]
    for name, val in fields:
        if val:
            lines.append(f"- **{name}**:{val}")

    if derived:
        lines.append("")
        lines.append("**派生指标(tier 2):**")
        if derived.get("altitude_m"):
            lines.append(f"- 海拔:{derived['altitude_m']} m({derived.get('altitude_class', '')})")
        if derived.get("season_set"):
            lines.append(f"- 有效施工季:{'/'.join(derived['season_set'])} (跨冬:{derived.get('spans_winter')})")
        if derived.get("size_class"):
            lines.append(f"- 项目规模:{derived['size_class']}")

    if template_match and template_match.get("hit"):
        tpl_id = (template_match.get("template", {}) or {}).get("template_id", "?")
        lines.append("")
        lines.append(f"> **范本命中**:{tpl_id} — 16 章主题 / B1.x 31 条 / 1.4.3 24 项 / 10.16 格式 已自动加载")
    return "\n".join(lines) + "\n"


def section_2_composition(facts: dict, special: dict | None, old_brain: str,
                          boq: dict | None = None) -> str:
    """第 2 节:项目类型与工程构成。优先用 BoQ 自动表格;无则回落旧 brain 或 project_scope。"""
    lines = ["## 二、项目类型与工程构成", ""]

    # 项目范围(总述)
    scope = _val(facts, "project_scope")
    if scope:
        lines.append(f"**项目范围:** {scope}")
        lines.append("")

    # BoQ 工程量表(优先,来自 bid_boq_extract.py)
    if boq and boq.get("categories"):
        lines.append("**主要分部分项工程量(来自工程量清单):**")
        lines.append("")
        for cat in boq["categories"]:
            items = cat.get("items", [])
            if not items:
                continue
            lines.append(f"### {cat['name']}")
            lines.append("")
            lines.append("| 序 | 项目名称 | 工程量 | 单位 |")
            lines.append("|---|---|---|---|")
            for i, it in enumerate(items, 1):
                lines.append(f"| {i} | {it.get('name', '')} | {it.get('qty', '')} | {it.get('unit', '')} |")
            lines.append("")
        return "\n".join(lines) + "\n"

    # 回落:旧 brain 第 2 节
    old = extract_old_section(old_brain, "二")
    if old:
        lines.append(old)
    return "\n".join(lines) + "\n"


def section_3_difficulties(derived: dict | None, special: dict | None,
                           old_brain: str, difficulties: dict | None = None) -> str:
    """第 3 节:重点难点。优先用 difficulties.json (DeepSeek 合成);回退到旧 brain + tier2。"""
    lines = ["## 三、技术重点与施工难点", ""]

    # 优先用 difficulties.json (bid_difficulty_synthesize.py 输出)
    if difficulties:
        emphasis = difficulties.get("重点", [])
        difficulty = difficulties.get("难点", [])
        for i, item in enumerate(emphasis, 1):
            lines.append(f"**重点 {i}:{item.get('title', '')}**")
            for r in item.get("rationale", []):
                lines.append(f"- {r}")
            ev = item.get("evidence", "")
            if ev:
                lines.append(f"- *依据:* {ev}")
            lines.append("")
        for i, item in enumerate(difficulty, 1):
            lines.append(f"**难点 {i}:{item.get('title', '')}**")
            for r in item.get("rationale", []):
                lines.append(f"- {r}")
            ev = item.get("evidence", "")
            if ev:
                lines.append(f"- *依据:* {ev}")
            lines.append("")
    else:
        # 回退:旧 brain 提取(legacy 兼容)
        old = extract_old_section(old_brain, "三")
        if old:
            lines.append(old)

    # tier2 派生增强(海拔/跨冬,无论哪条路径都追加)
    if derived:
        ac = derived.get("altitude_class")
        if ac in ("very_high", "extreme"):
            lines.append("")
            lines.append(f"**派生重点(海拔等级 {ac}):** 依 tier 2 派生数据,本项目须按高原专项编写"
                         f"(高原医疗保障 / 设备降容 / 施工时段限制 / 应急预案)。")
        if derived.get("spans_winter"):
            lines.append(f"")
            lines.append(f"**派生难点(跨冬施工):** 工期跨 11-3 月停工区,需冬期施工专项 + 进度策略调整。")
    return "\n".join(lines) + "\n"


def section_4_constraints(old_brain: str, special: dict | None,
                          disq: dict | None = None,
                          difficulties: dict | None = None) -> str:
    """第 4 节:施工约束。优先用 difficulties.json + tier4 + B3。

    优先级:
    1. difficulties.json 的「约束」(DeepSeek 6 大类合成)
    2. tier4 项目特殊条款(政府/邮政/教育/区外)
    3. B3 隐含硬约束(disqualification.json 按 constraint_type 自动归纳)
    fallback:旧 brain 第 4 节
    """
    lines = ["## 四、施工约束条件", ""]

    # 优先用 difficulties.json
    if difficulties:
        constraints = difficulties.get("约束", [])
        for c in constraints:
            cat = c.get("category", "")
            content = c.get("content", "")
            lines.append(f"- **{cat}**:{content}")
        lines.append("")
    else:
        old = extract_old_section(old_brain, "四")
        if old:
            lines.append(old)

    # ─── A. tier4 项目特殊条款(政府/邮政/教育/区外)──
    if special:
        clauses = special.get("clauses", [])
        constraint_cats = {"政府", "区外", "教育", "邮政"}
        relevant = [c for c in clauses if c.get("category") in constraint_cats and c.get("is_mandatory")]
        if relevant:
            lines.append("")
            lines.append("**Tier 4 项目特殊约束(自动追加):**")
            for c in relevant:
                page = c.get("source_page", "?")
                lines.append(f"- [{c.get('category')}] {c.get('topic')}:{c.get('text_original','')[:120]}"
                             f"  `[来源: p{page}]`")

    # ─── B. B3 隐含硬约束(constraint_type 非空,Phase 1.1 新增)──
    if disq:
        all_clauses = disq.get("clauses", [])
        hard = [c for c in all_clauses if c.get("constraint_type")]
        if hard:
            # 按 constraint_type 分组
            groups: dict[str, list[dict]] = {}
            for c in hard:
                groups.setdefault(c["constraint_type"], []).append(c)
            type_labels = {
                "page_limit": "页数硬顶",
                "word_limit": "字数硬顶",
                "format": "格式硬约束(字号/行距/字体)",
                "deadline": "时间硬约束(有效期/截止)",
                "scope": "范围约束",
                "color": "颜色约束",
                "binding": "装订约束",
                "qualification": "资质硬约束(人员/单位)",
                "price": "报价约束",
            }
            lines.append("")
            lines.append("**B3 隐含硬约束(按 constraint_type 自动归纳;违反 → 视为不响应):**")
            for ctype in sorted(groups.keys(), key=lambda k: list(type_labels.keys()).index(k) if k in type_labels else 99):
                label = type_labels.get(ctype, ctype)
                lines.append(f"")
                lines.append(f"*{label}:*")
                for c in groups[ctype]:
                    page = c.get("source_page", "?")
                    src = "[grep]" if c.get("_grep_fallback") else "[B3]"
                    lines.append(f"- {src} {c.get('text_original', '')[:100]}  `[来源: p{page}]`")
    return "\n".join(lines) + "\n"


def section_5_scoring(eval_payload: dict, source_file: str) -> str:
    """第 5 节:评分表(1:1 翻译 evaluation_method.json)"""
    lines = [
        "## 五、技术标评分表(从 evaluation_method.json 翻译,禁止重写)",
        "",
        f"> 数据源:`extracted/evaluation_method.json` (来源:{source_file})",
        "",
        f"评标方法：{eval_payload.get('method_chinese', '?')}",
        f"技术标总分：{eval_payload.get('total_tech_score', 0)} "
        f"({'施工组织设计合格制不计分' if eval_payload.get('tech_score_is_passfail') else '评分制'})",
        "",
        "### 评分表原文",
        "",
    ]
    # C 方案:按 section 分块显示(技术/项目管理/投标报价/其他因素)
    sections_order = ["技术", "项目管理", "投标报价", "其他因素", "未分类"]
    all_items = eval_payload.get("scoring_items", [])
    items_by_section: dict[str, list] = {}
    for item in all_items:
        sec = item.get("section") or "未分类"
        items_by_section.setdefault(sec, []).append(item)

    if len(items_by_section) <= 1 or (len(items_by_section) == 1 and "未分类" in items_by_section):
        # 旧格式或无 section → 单表
        lines.append("| 序号 | 评分项标题(原文) | 分值 | 评分要点/档次描述 | 来源 |")
        lines.append("|------|------------------|------|------------------|------|")
        for item in all_items:
            method_tag = "OCR" if item.get("source_page_method") == "ocr" else ""
            page = item.get("source_page", "?")
            src = f"p{page} {method_tag}".strip()
            title = (item.get("title_original") or "").replace("|", "/")
            criteria = (item.get("criteria_original") or "")[:80].replace("|", "/").replace("\n", " ")
            lines.append(f"| {item.get('id', '')} | {title} | "
                         f"{item.get('score', '')} | {criteria} | {src} |")
    else:
        # 新格式:按 section 分子表
        for sec in sections_order:
            if sec not in items_by_section:
                continue
            sec_items = items_by_section[sec]
            sec_score = sum(i.get("score", 0) for i in sec_items)
            lines.extend([
                f"#### {sec}部分({len(sec_items)} 项,合计 {sec_score} 分)",
                "",
                "| 序号 | 评分项标题(原文) | 分值 | 评分要点/档次描述 | 来源 |",
                "|------|------------------|------|------------------|------|",
            ])
            for item in sec_items:
                method_tag = "OCR" if item.get("source_page_method") == "ocr" else ""
                page = item.get("source_page", "?")
                src = f"p{page} {method_tag}".strip()
                title = (item.get("title_original") or "").replace("|", "/")
                criteria = (item.get("criteria_original") or "")[:80].replace("|", "/").replace("\n", " ")
                lines.append(f"| {item.get('id', '')} | {title} | "
                             f"{item.get('score', '')} | {criteria} | {src} |")
            lines.append("")

    veto = eval_payload.get("veto_items", [])
    if veto:
        lines.extend(["", "**否决项(强制要求):**", ""])
        for v in veto:
            page = v.get("source_page", "?")
            method_tag = " 视觉OCR" if v.get("source_page_method") == "ocr" else ""
            lines.append(f"- {v.get('text_original', '')} `[来源: 招标文件.pdf p{page}{method_tag}]`")
    return "\n".join(lines) + "\n"


def section_6_disqualification(disq: dict, source_file: str) -> str:
    """第 6 节:废标条款(1:1 翻译 disqualification.json,按 category 分类)"""
    lines = [
        "## 六、废标条款(从 disqualification.json 翻译,每条带 source_page)",
        "",
        f"> 数据源:`extracted/disqualification.json` (来源:{source_file})",
        f"> 共 {len(disq.get('clauses', []))} 条,分布在 {len(set(c.get('source_page') for c in disq.get('clauses', []) if c.get('source_page')))} 个不同页码",
        "",
    ]
    by_cat: dict[str, list] = {}
    for c in disq.get("clauses", []):
        cat = c.get("category") or "其他"
        by_cat.setdefault(cat, []).append(c)

    cat_order = ["报价", "资格", "形式", "格式", "硬约束", "评审", "其他"]
    for cat in cat_order:
        if cat not in by_cat:
            continue
        lines.extend([f"### {cat}类({len(by_cat[cat])} 条)", ""])
        for c in by_cat[cat]:
            page = c.get("source_page", "?")
            method_tag = " 视觉OCR" if c.get("source_page_method") == "ocr" else ""
            hint = c.get("source_file_hint")
            prefix = f"[{hint}] " if hint else ""
            text = (c.get("text_original") or "").replace("\n", " ")
            lines.append(f"- {prefix}{text} `[来源: 招标文件.pdf p{page}{method_tag}]`")
        lines.append("")
    return "\n".join(lines)


def section_7_outline(outline: dict, source_file: str) -> str:
    """第 7 节:必含大纲(1:1 翻译 mandatory_outline.json)"""
    lines = [
        "## 七、施工组织设计编制大纲(从 mandatory_outline.json 翻译,禁止概括/合并/省略)",
        "",
        f"> 数据源:`extracted/mandatory_outline.json` (来源:{source_file})",
    ]
    if outline.get("found"):
        lines.append(f"> 来源章节:{outline.get('source_chapter','?')} "
                     f"p{outline.get('source_page_start','?')}-p{outline.get('source_page_end','?')} "
                     f"({outline.get('source_page_method','?')})")
        # 子节标题用 verify 期望的精确 'necessary topics' 字面
        lines.extend(["", "### 必含一级主题", "",
                      "| 序号 | 主题（原文） | 关键子节/附表（原文） |",
                      "|------|-------------|---------------------|"])
        for t in outline.get("topics", []):
            subs = "、".join(t.get("subsections_original") or []) or (t.get("notes") or "")
            lines.append(f"| {t.get('index')} | {t.get('title_original')} | {subs} |")

        attachments = outline.get("attachments", [])
        if attachments:
            lines.extend(["", "### 附图及附表", ""])
            for a in attachments:
                lines.append(f"- {a.get('label')}:{a.get('title_original')}")
    else:
        lines.extend(["", outline.get("fallback_reason",
                      "招标文件未明确规定施工组织设计编制大纲,章节锚点回落到评分表/通用结构。")])
    return "\n".join(lines) + "\n"


def assemble_brain(ext: Path, log=None) -> str:
    """组装新 brain.md。"""
    facts_d = (_load(ext / "project_facts.json") or {}).get("facts", {})
    derived = _load(ext / "project_derived.json")
    template = _load(ext / "template_match.json")
    eval_p = _load(ext / "evaluation_method.json") or {}
    # 评分表纯文本错位修复:与 plan_synthesize 同源,保证 brain 第5节(writer 看到的
    # 评分要点)与 chapter_plan 子项分配对同一份修正结果,不写回 evaluation_method.json。
    if eval_p.get("scoring_items"):
        eval_p["scoring_items"] = repair_technical_criteria_alignment(
            eval_p["scoring_items"], log)
    disq = _load(ext / "disqualification.json") or {}
    outline = _load(ext / "mandatory_outline.json") or {}
    special = _load(ext / "special_clauses.json")
    difficulties = _load(ext / "difficulties.json")
    boq = _load(ext / "boq.json")
    source_file = (eval_p.get("_meta", {}) or {}).get("source_file", "招标文件.pdf")

    # 旧 brain.md(用于保留人工内容)
    old_path = ext / "project_brain.md"
    old_brain = old_path.read_text(encoding="utf-8") if old_path.exists() else ""

    parts = [
        "# 项目理解文档",
        "",
        "> v2 方法论生成:第 1-4 节由 facts/derived/special 数据 + 旧 brain 人工内容组织,",
        "> 第 5/6/7 节直接从 evaluation_method/disqualification/mandatory_outline JSON 翻译。",
        "> 不可手工修改第 5/6/7 节(会被 bid_plan_verify 对账拦截);要改请改 JSON 后重新跑。",
        "",
        section_1_basic_info(facts_d, derived, template),
        section_2_composition(facts_d, special, old_brain, boq),
        section_3_difficulties(derived, special, old_brain, difficulties),
        section_4_constraints(old_brain, special, disq, difficulties),
        section_5_scoring(eval_p, source_file),
        section_6_disqualification(disq, source_file),
        section_7_outline(outline, source_file),
    ]
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_brain_translate", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_brain_translate  目录: {tender_dir}")

    # 检查必备 JSON
    required = ["evaluation_method.json", "mandatory_outline.json", "disqualification.json"]
    missing = [f for f in required if not (ext / f).exists()]
    if missing:
        log.error(f"缺失必备 JSON: {missing}")
        log.error("请先跑 bid_skeleton_extract.py")
        return 2

    out_path = ext / "project_brain.md"
    if out_path.exists() and not args.force:
        log.info(f"  project_brain.md 已存在,需用 --force 覆盖")
        return 1

    brain = assemble_brain(ext, log)
    out_path.write_text(brain, encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name} ({len(brain):,} 字符)")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
