#!/usr/bin/env python3
"""
bid_plan_verify.py — Step 2 机器校验

在用户确认 chapter_plan 之前跑一遍，硬性阻断以下问题：
  1. 评分总分一致性：sum(chapter_plan 各章分值) == project_brain 技术标总分
  2. 废标条款来源标注：project_brain《六、废标条款》每条末尾必须有 [来源: 文件名 pXX]
  3. 章节字数总和 ∈ 目标总字数 × [0.85, 1.15]（三圈第一层↔第二层一致）
  4. 每章子项字数之和 ≈ 章节目标字数（第二层↔第三层一致，允许 ±5% 浮动）
  5. 估算总页数（Σ字数/每页字数 + Σ图表×0.4）≤ 目标总页数 × 1.05
  6. 骨架 JSON 存在（bid_skeleton_extract.py 输出，evaluation_method/mandatory_outline/disqualification）
  7. brain ↔ 骨架对账：brain.md 第五/六/七节的字段数量与对应 JSON 一致
  8. 来源页码集中度：brain.md 第六节 [来源: pXX] 不得过度集中（同页 ≥ 5 条触发警告）

用法:
    python3 bid_plan_verify.py /path/to/tender/

退出码:
    0  全部通过
    1  有警告（来源标注缺失等软性问题）
    2  硬性失败（总分不符、章节缺失等）
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    parse_chapter_plan,
    parse_plan_budget,
    parse_plan_subitems,
    make_script_logger,
)


OK = "✓"
WARN = "⚠"
FAIL = "✗"


def extract_total_score(brain_text: str) -> float | None:
    """从 project_brain.md 抓 '技术标总分：XX' 的数字。"""
    m = re.search(r"技术标总分[：:\s]*([\d.]+)", brain_text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def extract_disqualification_section(brain_text: str) -> str:
    """截取《六、废标条款》小节正文（到下一个一/二级标题或文件尾）。"""
    m = re.search(
        r"##\s*六[、.\s]*废标条款[^\n]*\n(.*?)(?=\n#{1,2}\s|\Z)",
        brain_text,
        re.S,
    )
    return m.group(1).strip() if m else ""


def extract_outline_section(brain_text: str) -> str:
    """截取《七、施工组织设计编制大纲》小节正文。

    匹配多种可能的标题写法（编制大纲/编制要求/施组编制等），保持通用性。
    """
    m = re.search(
        r"##\s*七[、.\s]*(?:施工组织设计编制大纲|施组编制|编制大纲|编制要求|技术标准和要求)[^\n]*\n(.*?)(?=\n##\s|\Z)",
        brain_text,
        re.S,
    )
    return m.group(1).strip() if m else ""


def extract_outline_topics(outline_section: str) -> list[str]:
    """从《七、》小节抽出"必含一级主题"清单。

    只在 `### 必含一级主题` / `### 一级主题` / `### 主题清单` 子节下解析；
    `### 附图及附表` / `### 兜底说明` 等子节被显式跳过，避免把附表名误识别为主题。

    支持两种格式：
      1. Markdown 表格行 `| 1 | 主题原文 | ... |`
      2. 编号列表 `1. 主题原文` / `- 主题原文` / `1、主题原文`
    若小节内根本没有 `### 子节标题`（直接列主题），也按整段当主题表解析。
    """
    topics: list[str] = []
    has_sub_heading = bool(re.search(r"^###\s+", outline_section, re.M))
    in_topics_block = not has_sub_heading  # 无子节时整段都是主题区
    saw_separator = False

    for line in outline_section.splitlines():
        line = line.rstrip()
        if not line:
            continue

        # 三级子节切换
        m_h = re.match(r"^###\s+(.+?)\s*$", line)
        if m_h:
            head = m_h.group(1)
            if re.match(r"^(?:必含一级主题|一级主题|主题清单|主题)$", head):
                in_topics_block = True
                saw_separator = False
            else:
                in_topics_block = False
            continue

        if not in_topics_block:
            continue

        # 表格识别
        if line.startswith("|"):
            if re.match(r"^\|[\s:|-]+\|$", line):
                saw_separator = True
                continue
            if not saw_separator:  # 表头行
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                # 第一列若是序号(数字)，主题在第二列；否则主题在第一列
                topic = cells[1] if re.match(r"^\d+$", cells[0]) else cells[0]
                if topic and topic not in ("...", "主题（原文）", "主题(原文)", "主题"):
                    topics.append(topic)
            continue

        # 编号列表 / 无序列表
        m = re.match(r"^\s*(?:[*\-]|\d+[.、)]+)\s*\*{0,2}([一-鿿].{1,40}?)\*{0,2}\s*(?:[（(].*[）)])?\s*$", line)
        if m:
            topic = m.group(1).strip().rstrip("：:")
            if topic and topic not in ("...", "示例") and len(topic) >= 2:
                topics.append(topic)

    # 去重保序
    seen = set()
    unique: list[str] = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def normalize_for_match(s: str) -> str:
    """标题语义匹配的归一化：去空白/标点/编号前缀。"""
    s = re.sub(r"^\d+[.、\s]+", "", s)
    s = re.sub(r"[\s　\-—()（）《》【】、,，。.:：;；!！?？\"'']+", "", s)
    return s.lower()


def topic_covered(topic: str, chapter_titles: list[str]) -> bool:
    """语义匹配：归一化后的双向子串包含 + 前缀关键词约束的关键字重叠。

    主题前 2 个汉字（首要名词）必须出现在章节标题中，再加关键字重叠 ≥ 50% 才算命中。
    避免"民工工资保证措施"被误匹配到"质量保证措施"（共享"保证措施"但首要名词不同）。
    """
    nt = normalize_for_match(topic)
    if not nt:
        return False
    chars = [c for c in nt if "一" <= c <= "鿿"]
    if len(chars) < 2:
        # 主题汉字少于 2 个，只做严格子串匹配
        return any(nt in normalize_for_match(t) or normalize_for_match(t) in nt
                   for t in chapter_titles if t)
    prefix = "".join(chars[:2])

    for title in chapter_titles:
        ntitle = normalize_for_match(title)
        if not ntitle:
            continue
        # 1. 双向子串
        if nt in ntitle or ntitle in nt:
            return True
        # 2. 前缀关键词在章节标题中 + 关键字重叠 ≥ 50%
        if prefix in ntitle:
            kt = set(chars)
            ki = set(c for c in ntitle if "一" <= c <= "鿿")
            if kt and ki:
                overlap = len(kt & ki) / min(len(kt), len(ki))
                if overlap >= 0.5:
                    return True
    return False


def extract_clauses(section: str) -> list[str]:
    """从废标条款小节抽出每一条（无序列表项或编号项）。"""
    clauses = []
    for line in section.splitlines():
        line = line.rstrip()
        if re.match(r"^\s*[-*]\s+\S", line) or re.match(r"^\s*\d+[.、)]\s*\S", line):
            text = re.sub(r"^\s*[-*\d.、)]+\s*", "", line)
            if len(text) >= 6:
                clauses.append(text)
    return clauses


SOURCE_PAT = re.compile(r"\[来源[：:].+?\]")
# 来源标注里抓页码：[来源: 文件名 p23] / [来源: 文件名 p23 视觉OCR] / [来源: 文件名 p23-25]
SOURCE_PAGE_PAT = re.compile(r"\[来源[：:][^\]]*?\bp(\d+)(?:[\-–](\d+))?[^\]]*?\]")


def check_source_annotation(clauses: list[str]) -> tuple[list[str], list[str]]:
    """返回 (已标注, 未标注) 两个列表。"""
    with_src, without_src = [], []
    for c in clauses:
        (with_src if SOURCE_PAT.search(c) else without_src).append(c)
    return with_src, without_src


def extract_source_pages(clauses: list[str]) -> tuple[Counter, int]:
    """从废标条款列表中抽出每条的来源页号（首页），返回 (Counter, 无页号条数)。"""
    counter: Counter = Counter()
    no_page = 0
    for c in clauses:
        m = SOURCE_PAGE_PAT.search(c)
        if m:
            counter[int(m.group(1))] += 1
        else:
            no_page += 1
    return counter, no_page


def load_skeleton_json(ext: Path, name: str) -> tuple[dict | None, str | None]:
    """读取骨架 JSON。返回 (payload, error_msg)。"""
    p = ext / name
    if not p.exists():
        return None, f"缺失 {name}（请运行 bid_skeleton_extract.py 生成）"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, f"{name} JSON 解析失败：{e}"


def count_brain_scoring_rows(brain_text: str) -> int:
    """数 brain.md 第五节评分表中的数据行数（不含表头/分隔）。"""
    m = re.search(
        r"##\s*五[、.\s]*[^\n]*\n(.*?)(?=\n##\s|\Z)",
        brain_text, re.S,
    )
    if not m:
        return 0
    section = m.group(1)
    rows = 0
    saw_separator = False
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s:|-]+\|$", line):
            saw_separator = True
            continue
        if not saw_separator:
            continue
        # 排除合计行
        if re.search(r"合\s*计|总\s*计|小\s*计", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if any(cells):
            rows += 1
    return rows


def verify(tender_dir: Path, strict: bool | None = None) -> int:
    """校验 brain.md / chapter_plan.md / disqualification_rules.md。

    Args:
        tender_dir: 投标根目录(含 extracted/)
        strict: True 强制启用合格制严格模式;None 自动从 evaluation_method.json
                的 method=passfail 推断;False 关闭严格模式。
                strict 模式下:
                  - 大纲主题在 chapter_plan 缺失 → error(原 warning)
                  - brain 第七节漏抄主题 → error(原 warning)
                  - 章节标题字面不匹配 mandatory_outline → error(原默认无)
                  - 来源页过度集中(同页 ≥ 5 条) → error(原 warning)
    """
    ext = tender_dir / "extracted"
    log = make_script_logger("bid_plan_verify", ext / "run.log")

    # strict 自动推断:从 evaluation_method.json 看是不是合格制(R5/R-anchor 防御)
    if strict is None:
        try:
            eval_path = ext / "evaluation_method.json"
            if eval_path.exists():
                eval_data = json.loads(eval_path.read_text(encoding="utf-8"))
                if eval_data.get("evaluation_method") == "passfail":
                    strict = True
                    print(f"{WARN} 检测到 evaluation_method=passfail,自动启用 strict 模式")
                    print(f"      合格制下,章节锚点必须 1:1 对齐必含大纲(字面 + 数量)")
        except Exception as e:
            log.debug(f"strict 自动推断失败: {e}")
    if strict is None:
        strict = False

    brain_path = ext / "project_brain.md"
    plan_path = ext / "chapter_plan.md"
    rules_path = ext / "disqualification_rules.md"

    errors: list[str] = []
    warnings: list[str] = []

    for p in (brain_path, plan_path, rules_path):
        if not p.exists():
            errors.append(f"缺失文件: {p.name}")
    if errors:
        for e in errors:
            log.error(e)
            print(f"{FAIL} {e}")
        return 2

    brain_text = brain_path.read_text(encoding="utf-8")
    rows = parse_chapter_plan(plan_path)

    # ─── 校验 1: 总分一致性 ───
    declared = extract_total_score(brain_text)
    summed = sum(r["score"] for r in rows)

    print("── 评分总分核验 ──")
    if declared is None:
        warnings.append("project_brain.md 未找到 '技术标总分' 字段")
        print(f"{WARN} project_brain.md 未声明 技术标总分")
    else:
        diff = abs(summed - declared)
        if diff < 0.01:
            print(f"{OK} chapter_plan 分值合计 {summed} = brain 声明 {declared}")
        elif declared == 0 and summed > 0:
            # 合格制项目(brain.total=0) + chapter_plan 仍带商务标分值 → v1/v2 切换期残留
            # 章节由必含大纲驱动而非分值,容忍 v1 时期 chapter_plan 的旧预算字段
            msg = (f"brain 声明合格制(总分 0),但 chapter_plan 章节分值合计 {summed} > 0;"
                   f"v1 时期 chapter_plan 残留,合格制下应忽略章节分值,以必含大纲驱动")
            warnings.append(msg)
            print(f"{WARN} {msg}")
        else:
            msg = f"分值不符:chapter_plan 合计 {summed},brain 声明 {declared}(差 {diff})"
            errors.append(msg)
            print(f"{FAIL} {msg}")

    if not rows:
        errors.append("chapter_plan.md 解析不到任何章节行")
        print(f"{FAIL} chapter_plan 无章节")
    else:
        print(f"    共 {len(rows)} 章，最高分值 {max(r['score'] for r in rows)}，"
              f"最低分值 {min(r['score'] for r in rows)}")

    # ─── 校验 1.5: 三圈字数/页数一致性（新 10 列格式才跑） ───
    is_new_layout = rows and any(r.get("subitem_count") for r in rows)
    if is_new_layout:
        budget = parse_plan_budget(plan_path)
        subitems = parse_plan_subitems(plan_path)

        print("\n── 三圈字数一致性核验 ──")
        total_chars = budget.get("target_total_chars")
        if total_chars:
            sum_chars = sum(r["target_chars"] for r in rows)
            lo, hi = total_chars * 0.85, total_chars * 1.15
            if lo <= sum_chars <= hi:
                print(f"{OK} 章节字数合计 {sum_chars:,} ∈ 总预算 {total_chars:,.0f} × [0.85, 1.15]")
            else:
                msg = f"章节字数合计 {sum_chars:,} 超出总预算带 [{lo:,.0f}, {hi:,.0f}]"
                errors.append(msg)
                print(f"{FAIL} {msg}")
        else:
            warnings.append("总量预算未声明'目标总字数'")
            print(f"{WARN} 未找到总量预算'目标总字数'")

        print("\n── 子项覆盖核验 ──")
        missing_sub, sum_mismatch = [], []
        for r in rows:
            if r["subitem_count"] <= 0:
                continue
            chap_items = subitems.get(r["num"], [])
            if not chap_items:
                missing_sub.append(f"{r['num']} {r['title']}（需 {r['subitem_count']} 子项）")
                continue
            if len(chap_items) != r["subitem_count"]:
                warnings.append(
                    f"章节 {r['num']} 子项数不符：声明 {r['subitem_count']}，实列 {len(chap_items)}"
                )
            sum_sub = sum(it["chars"] for it in chap_items)
            lo, hi = r["target_chars"] * 0.95, r["target_chars"] * 1.05
            if not (lo <= sum_sub <= hi):
                sum_mismatch.append(
                    f"{r['num']} {r['title']}：子项合计 {sum_sub:,} ≠ 章节目标 {r['target_chars']:,} (±5%)"
                )
        if missing_sub:
            for m in missing_sub:
                errors.append(f"缺子项分配: {m}")
                print(f"{FAIL} 缺子项分配: {m}")
        if sum_mismatch:
            for m in sum_mismatch:
                warnings.append(m)
                print(f"{WARN} {m}")
        if not missing_sub and not sum_mismatch:
            print(f"{OK} {len(rows)} 章子项覆盖完整，每章合计 ≈ 章节目标")

        print("\n── 总页数硬顶核验 ──")
        target_pages = budget.get("target_pages")
        # cpp(每页字数密度)缺失时按 disqualification_rules.md 的字号行距查 Word 实测表;
        # 历史教训:之前默认 980 字/页 是按"满版扣 15%"理论值,远超 14pt+1.5倍 的 Word 实测 550-700,
        # 导致目标字数虚高、写完即超页。
        cpp = budget.get("chars_per_page")
        cpp_warning: str | None = None
        try:
            from bid_derive import (
                chars_per_page as _density_lookup,
                parse_format_spec_from_rules as _parse_fmt,
            )
            fmt_spec = _parse_fmt(rules_path.read_text(encoding="utf-8"))
            if fmt_spec.get("font_size_pt") and fmt_spec.get("line_spacing_value"):
                lo, hi = _density_lookup(
                    fmt_spec["font_size_pt"], fmt_spec["line_spacing_kind"],
                    fmt_spec["line_spacing_value"]
                )
                density_mid = (lo + hi) / 2
                if cpp is None:
                    cpp = density_mid
                    print(f"    chars_per_page 缺失 → 按 {fmt_spec['font_size_pt']:.0f}pt + "
                          f"{fmt_spec['line_spacing_value']}{'倍' if fmt_spec['line_spacing_kind']=='multiple' else '磅'} "
                          f"查表中位值 {density_mid:.0f} 字/页")
                elif cpp > hi * 1.10:
                    cpp_warning = (
                        f"chapter_plan 声明 chars_per_page={cpp:.0f} 显著高于实测上限 "
                        f"{hi}(按 {fmt_spec['font_size_pt']:.0f}pt+"
                        f"{fmt_spec['line_spacing_value']}查表),目标字数高估,超页风险大"
                    )
        except Exception as e:
            log.debug(f"密度查表失败,使用 cpp 原值: {e}")
        if cpp is None:
            cpp = 980.0  # 终极兜底(应该走不到这里,因为查表覆盖了常见格式)
        if cpp_warning:
            warnings.append(cpp_warning)
            print(f"{WARN} {cpp_warning}")
        if target_pages:
            n_charts = sum(r["chart_count"] for r in rows)
            est_text_pages = sum(r["target_chars"] for r in rows) / cpp
            # ★ 每张渲染图表(graphviz/matplotlib PNG)在 docx 实测约占 ~0.9 整页(带标题+前后空白),
            #   远高于旧系数 0.4——历史超页废标根因(普兰食堂首装 76-78 页超 70)。取 0.9 才能提前拦。
            est_chart_pages = n_charts * 0.9
            est_total = est_text_pages + est_chart_pages
            cover_toc = 4  # 封面+目录约 4 页
            est_with_front = est_total + cover_toc
            if est_with_front <= target_pages * 1.05:
                print(f"{OK} 估算 {est_with_front:.1f} 页 ≤ 目标 {target_pages:.0f} × 1.05")
                print(f"    文字 {est_text_pages:.1f} + 图表 {est_chart_pages:.1f}({n_charts}张×0.9) + 封面目录 {cover_toc} 页")
            else:
                msg = (f"估算 {est_with_front:.1f} 页(文字{est_text_pages:.0f}+图表{est_chart_pages:.0f}/{n_charts}张+封面目录{cover_toc})"
                       f"超出目标 {target_pages:.0f}×1.05 — 建议削减图表数(每张≈0.9页)或下调章节字数")
                errors.append(msg)
                print(f"{FAIL} {msg}")
        else:
            warnings.append("总量预算未声明'目标页数'")
            print(f"{WARN} 未找到总量预算'目标页数'")

    # ─── 校验 1.8: 章节锚点覆盖（施组编制大纲） ───
    print("\n── 章节锚点覆盖核验（施组编制大纲）──")
    outline_sec = extract_outline_section(brain_text)
    if not outline_sec:
        print(f"{WARN} brain.md 未找到《七、施工组织设计编制大纲》小节")
        warnings.append("brain.md 缺少《七、施组编制大纲》小节（可选填，但建议补全）")
    else:
        topics = extract_outline_topics(outline_sec)
        # 兜底标识：本节为"未明确"
        is_fallback = "未规定施组编制大纲" in outline_sec or "未明确" in outline_sec
        if not topics and is_fallback:
            print(f"{OK} 大纲为兜底状态（招标文件未明确规定，章节锚点回落到评分表/通用结构）")
        elif not topics:
            print(f"{WARN} 解析不到大纲一级主题（小节存在但内容为空或格式异常）")
            warnings.append("brain.md《七、》小节内容无法解析为主题清单")
        else:
            chapter_titles = [r["title"] for r in rows]
            missing = [t for t in topics if not topic_covered(t, chapter_titles)]
            covered = len(topics) - len(missing)
            severity_symbol = FAIL if (strict and missing) else (WARN if missing else OK)
            print(f"{severity_symbol} 大纲 {len(topics)} 个一级主题，"
                  f"chapter_plan 已覆盖 {covered}，缺失 {len(missing)}")
            for t in missing:
                short = t[:40] + ("…" if len(t) > 40 else "")
                msg = f"大纲主题未在 chapter_plan 中找到对应章节: {short}"
                if strict:
                    errors.append(msg)
                    print(f"    {FAIL} [strict] {short}")
                else:
                    warnings.append(msg)
                    print(f"    {WARN} {short}")

            # strict 模式额外:章节标题字面匹配核验(允许全等或一方包含另一方)
            if strict and topics:
                literal_mismatches = []
                for r in rows:
                    chap_title = r["title"]
                    chap_norm = normalize_for_match(chap_title)
                    # 评分项栏(score)非 0 的章节是评分制残留,不强制匹配大纲
                    is_score_only = r.get("score", 0) > 0 and not any(
                        normalize_for_match(t) in chap_norm or chap_norm in normalize_for_match(t)
                        for t in topics
                    )
                    if is_score_only:
                        literal_mismatches.append((chap_title, "评分项独占章节,无对应大纲主题"))
                if literal_mismatches:
                    for ct, why in literal_mismatches[:3]:
                        msg = f"strict:章节 {ct[:40]} 字面未匹配任一大纲主题({why})"
                        warnings.append(msg)
                        print(f"    {WARN} [strict-info] {msg}")

    # ─── 校验 2: 废标条款来源标注 ───
    print("\n── 废标条款来源标注核验 ──")
    section = extract_disqualification_section(brain_text)
    brain_clauses: list[str] = []
    if not section:
        warnings.append("project_brain.md 未找到《六、废标条款》小节")
        print(f"{WARN} 未找到《六、废标条款》小节")
    else:
        brain_clauses = extract_clauses(section)
        if not brain_clauses:
            warnings.append("《六、废标条款》小节内无可识别的条款列表项")
            print(f"{WARN} 小节内无列表项（每条应为 '- ' 或 '1. ' 开头）")
        else:
            with_src, without_src = check_source_annotation(brain_clauses)
            print(f"{OK} 共 {len(brain_clauses)} 条，已标注来源 {len(with_src)}，"
                  f"未标注 {len(without_src)}")
            for c in without_src:
                short = c[:60] + ("…" if len(c) > 60 else "")
                warnings.append(f"废标条款未标注来源: {short}")
                print(f"    {WARN} {short}")

    # ─── 校验 2.5: 来源页码集中度（R4 根因） ───
    if brain_clauses:
        print("\n── 来源页码集中度核验 ──")
        page_counter, no_page_n = extract_source_pages(brain_clauses)
        if no_page_n:
            print(f"{WARN} {no_page_n} 条无 pXX 页号")
        if page_counter:
            unique_pages = len(page_counter)
            most = page_counter.most_common(1)[0]
            if unique_pages == 1 and len(brain_clauses) >= 5:
                msg = (f"全部 {len(brain_clauses)} 条废标条款都指向 p{most[0]}，"
                       f"几乎肯定有误（R4 根因复现）")
                errors.append(msg)
                print(f"{FAIL} {msg}")
            else:
                # 文档分页规模决定集中度阈值。R4 防御本意是防 LLM 把所有条款
                # 都标到同一页(unique_pages==1 已在上面拦截);只要分布>1页,
                # 集中度高在大文档是自然的(评标办法/废标条款本来就集中)。
                total_pages = 0
                try:
                    outline_p = ext / "mandatory_outline.json"
                    if outline_p.exists():
                        op = json.loads(outline_p.read_text(encoding="utf-8"))
                        total_pages = (op.get("_meta", {}) or {}).get("total_pages", 0) or 0
                except Exception:
                    pass
                # 阈值:不分页 docx (total=0) 跳过(页码全是估值,集中是正常的);
                # 小文档(<30)放宽到 10;中文档(30-100)放宽到 20;大文档放宽到 30
                if total_pages == 0:
                    # 不分页文档,只需 unique_pages > 1 即可(上面已检查),跳过集中度判定
                    print(f"{OK} 不分页 docx,{unique_pages} 个页码估值,跳过集中度判定")
                    threshold = 0  # 标识跳过
                    over = []
                else:
                    if total_pages < 30:
                        threshold = 10
                    elif total_pages < 100:
                        threshold = 20
                    else:
                        threshold = 30
                    over = [(p, n) for p, n in page_counter.items() if n >= threshold]
                    if over:
                        parts = [f"p{p}×{n}" for p, n in sorted(over)]
                        msg = f"来源页过度集中（同页 ≥ {threshold} 条）：{', '.join(parts)}"
                        if strict:
                            errors.append(msg)
                            print(f"{FAIL} [strict] {msg}")
                        else:
                            warnings.append(msg)
                            print(f"{WARN} {msg}")
                    else:
                        hint = f"(阈值={threshold},文档总页={total_pages})"
                        print(f"{OK} {unique_pages} 个不同页码,最高 p{most[0]} × {most[1]} {hint}")

    # ─── 校验 3: 骨架 JSON 存在性 ───
    print("\n── 骨架 JSON 核验（bid_skeleton_extract 输出）──")
    eval_payload, eval_err = load_skeleton_json(ext, "evaluation_method.json")
    outline_payload, outline_err = load_skeleton_json(ext, "mandatory_outline.json")
    disq_payload, disq_err = load_skeleton_json(ext, "disqualification.json")

    skeleton_present = all([eval_payload, outline_payload, disq_payload])
    if not skeleton_present:
        for err in (eval_err, outline_err, disq_err):
            if err:
                warnings.append(err)
                print(f"{WARN} {err}")
        print(f"{WARN} 跳过 brain↔skeleton 对账（骨架 JSON 不全）")
    else:
        print(f"{OK} 三份骨架 JSON 已就位")

        # ─── 校验 3.1: 评分项数量对账 ───
        print("\n── brain↔evaluation_method 对账 ──")
        method = eval_payload.get("evaluation_method")
        items = eval_payload.get("scoring_items") or []
        json_score_count = len(items)
        json_total = sum((i.get("score") or 0) for i in items)
        brain_rows = count_brain_scoring_rows(brain_text)

        print(f"    JSON: method={method}, scoring_items={json_score_count}, sum_score={json_total}")
        print(f"    brain.md 第五节数据行数={brain_rows}")
        if brain_rows == 0:
            warnings.append("brain.md 第五节解析不到评分表行（可能未抄录）")
            print(f"{WARN} brain.md 第五节缺评分表")
        elif method == "score" and declared and abs(json_total - declared) > 0.5:
            msg = (f"骨架声明分值合计 {json_total} ≠ brain 声明 {declared}（"
                   f"建议人工核对 evaluation_method.json）")
            warnings.append(msg)
            print(f"{WARN} {msg}")
        elif json_score_count and brain_rows < json_score_count - 1:
            msg = (f"brain 第五节行数 {brain_rows} 明显少于 JSON 评分项数 {json_score_count}，"
                   f"可能漏抄")
            warnings.append(msg)
            print(f"{WARN} {msg}")
        else:
            print(f"{OK} 评分项数量大致对应")

        # ─── 校验 3.2: 必含大纲对账 ───
        print("\n── brain↔mandatory_outline 对账 ──")
        if outline_payload.get("found"):
            json_topics = outline_payload.get("topics") or []
            json_topic_count = len(json_topics)
            outline_sec = extract_outline_section(brain_text)
            brain_topics = extract_outline_topics(outline_sec) if outline_sec else []
            print(f"    JSON: found=true, topics={json_topic_count}")
            print(f"    brain.md 第七节解析到主题数={len(brain_topics)}")
            if not brain_topics:
                msg = (f"骨架抓到 {json_topic_count} 个必含主题，但 brain.md 第七节未抄录"
                       f"（R1 根因复现）")
                errors.append(msg)
                print(f"{FAIL} {msg}")
            elif len(brain_topics) < json_topic_count:
                missing_count = json_topic_count - len(brain_topics)
                json_titles = [t.get("title_original", "") for t in json_topics]
                missing_titles = [
                    t for t in json_titles
                    if not any(normalize_for_match(t) == normalize_for_match(bt) or
                               normalize_for_match(t) in normalize_for_match(bt) or
                               normalize_for_match(bt) in normalize_for_match(t)
                               for bt in brain_topics)
                ]
                msg = (f"brain 第七节抄了 {len(brain_topics)} 项，骨架声明 {json_topic_count} 项，"
                       f"漏 {missing_count}：{missing_titles[:3]}")
                if strict:
                    errors.append(msg)
                    print(f"{FAIL} [strict] {msg}")
                else:
                    warnings.append(msg)
                    print(f"{WARN} {msg}")
            else:
                print(f"{OK} brain 与骨架主题数对得上")
        else:
            reason = outline_payload.get("fallback_reason", "")
            print(f"{OK} 骨架报告未明确大纲，fallback_reason: {reason[:60]}")

        # ─── 校验 3.3: 废标条款数量对账 ───
        print("\n── brain↔disqualification 对账 ──")
        json_clauses = disq_payload.get("clauses") or []
        json_clause_count = len(json_clauses)
        brain_clause_count = len(brain_clauses)
        print(f"    JSON: clauses={json_clause_count}")
        print(f"    brain.md 第六节: clauses={brain_clause_count}")
        if json_clause_count and brain_clause_count < json_clause_count * 0.5:
            msg = (f"brain 第六节只抄了 {brain_clause_count} 条废标，"
                   f"骨架抓到 {json_clause_count} 条，覆盖率 < 50%")
            warnings.append(msg)
            print(f"{WARN} {msg}")
        elif json_clause_count == 0:
            print(f"{WARN} 骨架废标 JSON 为空（可能 LLM 抽取失败）")
        else:
            print(f"{OK} brain 与骨架废标条款数大致对应")

    # ─── 反馈记忆规则校核（v3: machine-enforced feedback）───
    print("\n── 反馈记忆规则校核 ──")
    # 加载 chapter_plan 数据
    plan_rows = parse_chapter_plan(str(ext / "chapter_plan.md")) if (ext / "chapter_plan.md").exists() else []
    plan_titles = [r.get("title", "") for r in plan_rows]
    plan_chapter_count = len(plan_rows)

    # 规则 1: 合格制章节锚点 1:1 (feedback_bid_pass_fail_anchor)
    if skeleton_present:
        method = eval_payload.get("evaluation_method")
        tsip = eval_payload.get("tech_score_is_passfail", False)
        if method == "passfail" or tsip:
            if outline_payload.get("found") and plan_chapter_count:
                outline_topics = outline_payload.get("topics") or []
                if len(plan_rows) < len(outline_topics):
                    msg = (f"合格制章节锚点不足：chapter_plan 只有 {plan_chapter_count} 章，"
                           f"但必含大纲声明 {len(outline_topics)} 个一级主题"
                           f"（feedback_bid_pass_fail_anchor — 合格制必须 1:1，禁止合并/省略）")
                    errors.append(msg)
                    print(f"{FAIL} {msg}")
                else:
                    print(f"{OK} 合格制章节锚点 1:1 ({plan_chapter_count}章 ≥ {len(outline_topics)}主题)")

    # 规则 2: 混合制强制 passfail (feedback_bid_mixed_method_passfail)
    if skeleton_present:
        tsip = eval_payload.get("tech_score_is_passfail", False)
        bm = brain_text
        if tsip:
            # 检查 brain 第五节是否正确标注了 passfail
            if re.search(r"技术标总分[：:\s]*0\s*分", bm) or "合格制" in (bm.split("## 五、")[1].split("## 六")[0] if "## 五、" in bm and "## 六" in bm else ""):
                print(f"{OK} brain 第五节正确标注 passfail")
            else:
                msg = ("商务标有分但施组合格制 → brain 第五节必须明确标注 passfail"
                       "（feedback_bid_mixed_method_passfail）")
                warnings.append(msg)
                print(f"{WARN} {msg}")

    # 规则 3: 附图附表归属 (feedback_bid_attachment_chapter)
    if skeleton_present and outline_payload.get("found"):
        attachments = outline_payload.get("attachments") or []
        if attachments and plan_titles:
            # 找"附图及附表"章节
            has_attach_chapter = any("附图" in t or "附表" in t for t in plan_titles)
            if not has_attach_chapter:
                msg = (f"大纲要求 {len(attachments)} 个附图/附表，但 chapter_plan 无专用附图章节"
                       f"（feedback_bid_attachment_chapter — 附图必须集中在专属章节）")
                warnings.append(msg)
                print(f"{WARN} {msg}")
            else:
                print(f"{OK} 附图附表章节存在（{len(attachments)} 个附表）")

    # 规则 4: 页数硬顶判档 (feedback_bid_size_judge_matrix)
    if skeleton_present and plan_rows:
        budget = parse_plan_budget(str(ext / "chapter_plan.md")) if (ext / "chapter_plan.md").exists() else {}
        pages = budget.get("target_pages", 0)
        drules = (ext / "disqualification_rules.md").read_text(encoding="utf-8") if (ext / "disqualification_rules.md").exists() else ""
        m_hard = re.search(r"page_count_hard_limit[:\s]*(\d+)", drules)
        hard_limit = int(m_hard.group(1)) if m_hard else 0
        if hard_limit and pages and pages > hard_limit:
            msg = (f"目标页数 {pages} 超过招标硬顶 {hard_limit} 页"
                   f"（feedback_bid_size_judge_matrix — 判档保守优先，超页必废）")
            errors.append(msg)
            print(f"{FAIL} {msg}")
        elif hard_limit:
            print(f"{OK} 目标页数 {pages} ≤ 硬顶 {hard_limit}")

    # 规则 5: 清单→章节覆盖（v3: boq.json 反向校验）
    from bid_common import check_boq_chapter_coverage
    boq_cov = check_boq_chapter_coverage(ext, plan_titles)
    if boq_cov.get("ratio", 1.0) < 1.0:
        missing = boq_cov.get("missing", [])
        if missing:
            msg = (f"清单 {len(boq_cov.get('covered',[]))+len(missing)} 个类目中有 {len(missing)} 个未被章节覆盖："
                   f"{', '.join(missing[:5])}")
            if boq_cov["ratio"] < 0.5:
                errors.append(msg); print(f"{FAIL} {msg}")
            else:
                warnings.append(msg); print(f"{WARN} {msg}")
    elif boq_cov.get("ratio", 1.0) == 1.0 and not boq_cov.get("note"):
        print(f"{OK} 清单类目全部被章节覆盖 ({len(boq_cov.get('covered',[]))} 项)")

    # 规则 6: 工期一致性（v3: 进度图工期 vs 声明工期）
    drules_text = (ext / "disqualification_rules.md").read_text(encoding="utf-8") if (ext / "disqualification_rules.md").exists() else ""
    m_dur = re.search(r"duration_days[:\s]*(\d+)", drules_text)
    declared_dur = int(m_dur.group(1)) if m_dur else 0
    if declared_dur:
        schedule_mismatches = []
        for row in plan_rows:
            ch_file = row.get("filename", "")
            ch_path = output_dir = Path(str(ext).replace("extracted", "output")) / ch_file
            if not ch_path.exists():
                continue
            ch_text = ch_path.read_text(encoding="utf-8")
            for m in re.finditer(r'"type"\s*:\s*"schedule".*?"total_duration_days"\s*:\s*(\d+)', ch_text, re.DOTALL):
                chart_dur = int(m.group(1))
                if abs(chart_dur - declared_dur) > 5:
                    schedule_mismatches.append(f"{ch_file}: 进度图工期 {chart_dur}天 ≠ 声明 {declared_dur}天")
        if schedule_mismatches:
            msg = f"进度图工期与声明工期不一致: {'; '.join(schedule_mismatches[:3])}"
            warnings.append(msg); print(f"{WARN} {msg}")
        else:
            # Don't print OK if no charts found - that's normal
            pass

    # ─── 汇总 ───

    # ─── 汇总 ───
    print("\n── 汇总 ──")
    if errors:
        for e in errors:
            log.error(e)
        print(f"{FAIL} 硬性失败 {len(errors)} 项，必须修复后再进 Step 3")
        return 2
    if warnings:
        for w in warnings:
            log.warning(w)
        print(f"{WARN} 警告 {len(warnings)} 项（未标注来源的条款须人工复核）")
        return 1
    print(f"{OK} Step 2 机器校验全部通过")
    return 0


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--strict", action="store_true",
                   help="启用合格制严格模式(默认从 evaluation_method.json 自动推断)")
    p.add_argument("--no-strict", action="store_true",
                   help="强制关闭 strict(即使是合格制)")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.exists():
        print(f"{FAIL} 目录不存在: {tender_dir}")
        return 2

    strict: bool | None
    if args.no_strict:
        strict = False
    elif args.strict:
        strict = True
    else:
        strict = None  # 自动推断
    return verify(tender_dir, strict=strict)


if __name__ == "__main__":
    sys.exit(main())
