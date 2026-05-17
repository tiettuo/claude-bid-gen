#!/usr/bin/env python3
"""
bid_reviewer.py - 机器层审校

对每个 variant 每个章节做机器审校，供 Claude 后续做人工判断：
  1. 字数达标（分值 × 300，允许下浮 15%）
  2. 一级标题与 chapter_plan 字面一致
  3. 评分要点关键词覆盖度
  4. 暗标禁用词扫描
  5. 废标关键响应词检查

输出:
  output/VXX/review.json   （机器层结构化结果）
  output/VXX/review.md     （人读摘要）
  output/review_summary.json （所有 variant 汇总，供 Claude 决策）

用法:
  python bid_reviewer.py <tender_dir>                  # 审所有 variant
  python bid_reviewer.py <tender_dir> --variant V01    # 仅某 variant
  python bid_reviewer.py <tender_dir> --single         # 单份模式（审 output/ 根目录）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    make_script_logger,
    detect_boilerplate,
    parse_chapter_plan,
    parse_plan_subitems,
    needs_blind_scan,
    extract_keywords,
    vary_chapter_title,
    derive_variant_skeleton,
    extract_headings,
    chapter_chars_floor,
    count_chinese_chars,
    DECO_STRIP_CHARS,
    DECO_WARN_CHARS,
)

# 装饰符扫描阈值：→← 这类语义符号超过此数才报警（偶尔因果用无可厚非）
_DECO_WARN_THRESHOLD = 10


# ─── 子项标题规范化（H2 对齐核查用） ─────────────────────────────────────
# 去掉层级编号（1.1、（一）、1)）、装饰性括号内容、空白和常见标点
_NUM_PREFIX_RE = re.compile(r"^(?:[\d]+[.、\s]+|[（(][一二三四五六七八九十\d]+[)）]\s*)+")
_TITLE_STRIP_RE = re.compile(r"[\s　、，,。；;()（）【】\[\]\-—：:]+")


def _norm_subitem_title(s: str) -> str:
    s = _NUM_PREFIX_RE.sub("", s or "")
    return _TITLE_STRIP_RE.sub("", s).lower()


# plan_synthesize 用的通用占位子项(如 "重点 1", "难点 2", "子项 3"),不参与字面匹配
_PLACEHOLDER_RE = re.compile(r"^(?:重点|难点|子项|要点|项)\s*[1-9一二三四五六七八九十]+$")


def _is_placeholder_subitem(title: str) -> bool:
    """判断子项标题是否为通用占位(如 '重点 1', '难点 2')。"""
    t = (title or "").strip()
    return bool(_PLACEHOLDER_RE.match(t))


def _fuzzy_has(norm_expected: str, norm_actuals: list[str], ratio_floor: float = 0.7) -> str | None:
    """匹配策略（任一命中即通过）：
      1) 双向子串（期望是实际的前后缀追加 / 截断）
      2) SequenceMatcher ratio ≥ ratio_floor（处理中间插词，如"触电应急预案" vs "触电事故应急预案"）
    返回命中的实际标题；否则 None。
    """
    if not norm_expected:
        return None
    from difflib import SequenceMatcher
    for a in norm_actuals:
        if not a:
            continue
        if norm_expected in a or a in norm_expected:
            return a
        if SequenceMatcher(None, norm_expected, a).ratio() >= ratio_floor:
            return a
    return None


def read_disqualification_terms(disq_path: Path) -> dict:
    """从废标规则提取：暗标禁用词、关键响应词"""
    result = {"forbidden": [], "required_response": [], "is_blind": False}
    if not disq_path.exists():
        return result
    text = disq_path.read_text(encoding="utf-8")
    result["is_blind"] = needs_blind_scan(disq_path.parent)
    # 简单启发式：找 "必须响应" / "明确承诺" 附近的短语
    for m in re.finditer(r"(?:必须|应当|须)\s*(?:响应|承诺|明确|包含)\s*[:：]?\s*([^\n。；]{2,30})", text):
        phrase = m.group(1).strip(" 、，,")
        if 2 <= len(phrase) <= 30:
            result["required_response"].append(phrase)
    # 暗标禁用词
    for m in re.finditer(r"(?:不得|不能|禁止)\s*(?:出现|含有|包含)\s*[:：]?\s*([^\n。；]{2,30})", text):
        phrase = m.group(1).strip(" 、，,")
        if 2 <= len(phrase) <= 30:
            result["forbidden"].append(phrase)
    return result


# ─── P/G 比 + 标准编造检测（P1.1）────────────────────────────────────

_GENERIC_MGMT_TERMS = [
    "施工", "质量", "安全", "管理", "措施", "保证", "控制", "检查", "验收",
    "规范", "标准", "要求", "执行", "落实", "加强", "做好", "确保", "完善",
    "建立", "制定", "贯彻", "坚持", "严格", "强化",
]

_STANDARD_RE = re.compile(
    r"(GB(?:\s*/\s*T)?|JGJ(?:\s*/\s*T)?|CJJ|DB54(?:\s*/\s*T)?)\s*(\d+)(?:[.\-](\d{4}))?"
)

def _load_standards_truth() -> dict:
    """加载标准版本真值表。"""
    truth_path = Path(__file__).resolve().parent.parent / "configs" / "standards_truth.json"
    if truth_path.exists():
        with open(truth_path) as f:
            return json.load(f).get("standards", {})
    return {}

_STANDARDS_TRUTH = _load_standards_truth()


def _check_pg_ratio(text: str, chapter: dict) -> dict:
    """检测项目专用词 vs 通用管理词的密度比（P/G 比）。

    项目专用词来源：章节标题中的关键词 + notes 中的关键词。
    """
    # 项目专用词：从章节标题和 notes 提取
    proj_kws = set()
    for src in [chapter.get("title", ""), chapter.get("notes", "")]:
        for kw in extract_keywords(str(src)):
            kw_clean = kw.strip()
            if len(kw_clean) >= 2 and kw_clean not in _GENERIC_MGMT_TERMS:
                proj_kws.add(kw_clean)

    if not proj_kws:
        return {"ratio": 1.0, "project_hits": 0, "generic_hits": 0, "severity": "ok",
                "detail": ""}

    project_hits = sum(text.count(kw) for kw in proj_kws)
    generic_hits = sum(text.count(t) for t in _GENERIC_MGMT_TERMS)
    pg = project_hits / max(generic_hits, 1)

    if pg < 0.2:
        sev, msg = "major", f"项目针对性极低（P/G={pg:.2f}）：{project_hits} 项目词 / {generic_hits} 通用词，几乎全是套话"
    elif pg < 0.4:
        sev, msg = "moderate", f"项目针对性偏低（P/G={pg:.2f}）：{project_hits}/{generic_hits}，需要更多项目特有细节"
    elif pg < 0.6:
        sev, msg = "minor", f"项目针对性偏弱（P/G={pg:.2f}）：{project_hits}/{generic_hits}，可增加项目具体参数"
    else:
        sev, msg = "ok", ""
    return {"ratio": round(pg, 3), "project_hits": project_hits, "generic_hits": generic_hits,
            "severity": sev, "detail": msg}


def _check_standards_in_text(text: str) -> dict:
    """检测章节文本中编造的标准年份版本。"""
    total = 0
    fabricated = 0
    details = []
    for m in _STANDARD_RE.finditer(text):
        prefix = m.group(1).replace(" ", "")
        num = m.group(2)
        year = m.group(3)
        total += 1
        if not year:
            continue
        # Build lookup keys
        for key in [f"{prefix}{num}", prefix + "/T" + num if "/T" not in prefix else prefix + num]:
            true_year = _STANDARDS_TRUTH.get(key)
            if true_year:
                if year != true_year:
                    fabricated += 1
                    details.append({"standard": key, "cited_year": year, "true_year": true_year})
                break
    return {"total": total, "fabricated": fabricated, "details": details}


# ─── 单章审校 ────────────────────────────────────────────────────────

def review_chapter(
    md_path: Path, chapter: dict, disq: dict,
    company_name: str = "", log: logging.Logger = None,
    variant: dict | None = None,
    subitems: list[dict] | None = None,
) -> dict:
    """对单个章节 md 做机器审校"""
    if log is None:
        log = logging.getLogger("bid_reviewer")

    expected_title = vary_chapter_title(chapter["title"], variant)
    result = {
        "file": md_path.name,
        "chapter_num": chapter["num"],
        "expected_title": expected_title,
        "exists": md_path.exists(),
        "issues": [],
        "severity": "ok",  # ok / minor / moderate / major
        "target_chars": chapter.get("target_chars", 0),  # 早返回路径也要有
        "actual_chars": 0,
    }
    if not result["exists"]:
        result["issues"].append({"type": "missing", "detail": "章节文件不存在"})
        result["severity"] = "major"
        return result

    text = md_path.read_text(encoding="utf-8")
    # 汉字口径（与 compliance 共享 bid_common.count_chinese_chars），不含 markdown 符号/标点/空格
    result["actual_chars"] = count_chinese_chars(text)
    result["target_chars"] = chapter["target_chars"]

    # 1. 一级标题检查
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    actual_title = m.group(1).strip() if m else ""
    result["actual_title"] = actual_title
    norm_a = re.sub(r"\s+", "", actual_title)
    norm_b = re.sub(r"\s+", "", expected_title)
    result["title_match"] = norm_a == norm_b
    if not result["title_match"]:
        result["issues"].append({
            "type": "title_mismatch",
            "detail": f"期望「{expected_title}」实际「{actual_title}」"
        })

    # 2. 字数检查（允许下浮 15% — 共享 bid_common.chapter_chars_floor）
    threshold = chapter_chars_floor(chapter["target_chars"])
    result["chars_pass"] = result["actual_chars"] >= threshold
    if not result["chars_pass"]:
        result["issues"].append({
            "type": "chars_short",
            "detail": f"实际 {result['actual_chars']} 字 < 目标 {threshold} 字（≥85%目标）"
        })

    # 2.5. 套话检测（v3）
    bp = detect_boilerplate(text)
    if bp["verdict"] != "ok":
        result["issues"].append({
            "type": "boilerplate",
            "severity": "moderate" if bp["verdict"] == "rewrite" else "minor",
            "detail": f"套话比例 {bp['ratio']:.0%}（{', '.join(h['label']+'×'+str(h['count']) for h in bp['hits'][:3])}）",
        })

    # 2.6. P/G 比检测（P1.1）
    pg_result = _check_pg_ratio(text, chapter)
    result["pg_ratio"] = pg_result["ratio"]
    result["pg_project_hits"] = pg_result["project_hits"]
    result["pg_generic_hits"] = pg_result["generic_hits"]
    if pg_result["severity"] != "ok":
        result["issues"].append({
            "type": "pg_low",
            "severity": pg_result["severity"],
            "detail": pg_result["detail"],
        })

    # 2.7. 标准引用编造检测（P1.1）
    std_result = _check_standards_in_text(text)
    result["standards_total"] = std_result["total"]
    result["standards_fabricated"] = std_result["fabricated"]
    if std_result["fabricated"]:
        result["issues"].append({
            "type": "std_fabrication",
            "severity": "moderate" if std_result["fabricated"] >= 3 else "minor",
            "detail": f"标准年份编造 {std_result['fabricated']}/{std_result['total']} 条：{'; '.join(d['standard']+'-'+d['cited_year']+'→'+d['true_year'] for d in std_result['details'][:3])}",
        })

    # 3. 评分要点关键词覆盖
    notes_kws = extract_keywords(chapter["notes"] or "")
    if notes_kws:
        hit = [kw for kw in notes_kws if kw in text]
        miss = [kw for kw in notes_kws if kw not in text]
        result["keyword_total"] = len(notes_kws)
        result["keyword_hit"] = len(hit)
        result["keyword_missing"] = miss
        result["keyword_coverage"] = round(len(hit) / len(notes_kws), 2)
        if len(miss) > 0 and result["keyword_coverage"] < 0.70:
            result["issues"].append({
                "type": "keyword_coverage_low",
                "detail": f"{result['keyword_coverage']:.0%} 覆盖（{len(hit)}/{len(notes_kws)}），缺失：{', '.join(miss[:10])}"
            })

    # 4. 暗标禁用词扫描
    blind_hits = []
    if disq.get("is_blind") and company_name:
        if company_name in text:
            blind_hits.append(("company_name", company_name))
    for term in disq.get("forbidden", []):
        if term and term in text:
            blind_hits.append(("forbidden", term))
    result["blind_violations"] = blind_hits
    if blind_hits:
        result["issues"].append({
            "type": "blind_violation",
            "detail": f"暗标违规 {len(blind_hits)} 处：{', '.join(t for _, t in blind_hits[:5])}"
        })

    # 5. H2 骨架一致性（仅批量模式且无子项分配时校验；有子项分配时第6项更精确）
    if variant and "section_order_seed" in variant and not subitems:
        expected = [n["title"] for n in derive_variant_skeleton(chapter, variant) if n["level"] == 2]
        actual = [t for lvl, t in extract_headings(text, levels=(2,)) if lvl == 2]
        norm = lambda s: re.sub(r"\s+", "", s)
        exp_n = [norm(t) for t in expected]
        act_n = [norm(t) for t in actual]
        result["skeleton_expected_h2"] = expected
        result["skeleton_actual_h2"] = actual
        result["skeleton_match"] = exp_n == act_n
        if not result["skeleton_match"]:
            # 长度或顺序不一致 → moderate；缺失≥半数 → major
            miss_count = len(set(exp_n) - set(act_n))
            if miss_count >= max(1, len(exp_n) // 2):
                sev = "major"
            else:
                sev = "moderate"
            result["issues"].append({
                "type": "skeleton_mismatch",
                "detail": f"H2 骨架偏离（期望 {len(exp_n)} 项，实际 {len(act_n)} 项，缺 {miss_count}）",
                "severity": sev,
            })

    # 6. H2 子项覆盖核查（chapter_plan 有"子项分配"段时）
    # 规则：可以多、不能少 —— actual ≥ expected 且每个期望子项都命中才算通过。
    # 多出的 H2 视为撰稿人自发补的细节，不报警；但总数 < 期望或具体子项未命中都触发 issue。
    if subitems:
        expected_titles = [s["title"] for s in subitems]
        actual_h2 = [t for lvl, t in extract_headings(text, levels=(2,)) if lvl == 2]
        result["subitems_expected"] = expected_titles
        result["subitems_actual"] = actual_h2

        # plan_synthesize 占位子项(如 "重点 1") 跳过字面匹配,仅看数量
        non_placeholder_expected = [t for t in expected_titles
                                     if not _is_placeholder_subitem(t)]
        placeholder_count = len(expected_titles) - len(non_placeholder_expected)

        act_norm = [_norm_subitem_title(t) for t in actual_h2]
        missing: list[str] = []
        for exp_t in non_placeholder_expected:
            if not _fuzzy_has(_norm_subitem_title(exp_t), act_norm):
                missing.append(exp_t)
        result["subitem_missing"] = missing
        result["subitem_placeholders"] = placeholder_count
        result["subitem_count_expected"] = len(expected_titles)
        result["subitem_count_actual"] = len(actual_h2)
        result["subitem_extras"] = max(0, len(actual_h2) - len(expected_titles))

        # 缺项判定:
        # - undercount: H2 实际数 < 期望数 → 主题缺失风险
        # - missing: 非占位类的期望子项 fuzzy 未命中
        undercount = len(actual_h2) < len(expected_titles)
        if missing or undercount:
            # 半数判定基于非占位子项;若全是占位子项,只看 undercount
            half_base = max(1, len(non_placeholder_expected) or len(expected_titles))
            half = max(1, half_base // 2)
            sev = "major" if len(missing) >= half else "moderate"
            parts = [f"期望 {len(expected_titles)} 项,实际 H2 {len(actual_h2)} 项"]
            if placeholder_count:
                parts.append(f"含 {placeholder_count} 项占位(跳过字面匹配)")
            if missing:
                parts.append(f"缺 {len(missing)}: {', '.join(missing[:5])}")
            elif undercount:
                parts.append("实际少于期望(疑似合并成复合标题)")
            result["issues"].append({
                "type": "subitem_mismatch",
                "detail": "子项覆盖不足(" + ";".join(parts) + ")",
                "severity": sev,
            })

    # 7. 装饰符号扫描（兜底提醒；装配前会自动清洗 DECO_STRIP_CHARS，但让用户看到模型在哪儿写了）
    strip_hits: dict[str, int] = {}
    for ch in DECO_STRIP_CHARS:
        n = text.count(ch)
        if n:
            strip_hits[ch] = n
    warn_hits: dict[str, int] = {}
    for ch in DECO_WARN_CHARS:
        n = text.count(ch)
        if n:
            warn_hits[ch] = n
    result["deco_strip_hits"] = strip_hits  # 装配时会自动清除
    result["deco_warn_hits"] = warn_hits    # 不会自动清除（语义符号）
    warn_total = sum(warn_hits.values())
    if strip_hits:
        result["issues"].append({
            "type": "decorative_chars",
            "detail": f"装饰符 {sum(strip_hits.values())} 处（装配时自动清洗）：{', '.join(f'{c}×{n}' for c, n in strip_hits.items())}",
            "severity": "minor",
        })
    if warn_total >= _DECO_WARN_THRESHOLD:
        result["issues"].append({
            "type": "decorative_arrows",
            "detail": f"流程箭头 {warn_total} 处（不自动清洗，建议改为'导致/进入/依次'等连词）：{', '.join(f'{c}×{n}' for c, n in warn_hits.items())}",
            "severity": "minor",
        })

    # 8. 严重性分级
    types_present = {i["type"] for i in result["issues"]}
    sk_sev = next((i.get("severity") for i in result["issues"] if i["type"] == "skeleton_mismatch"), None)
    sub_sev = next((i.get("severity") for i in result["issues"] if i["type"] == "subitem_mismatch"), None)
    bp_sev = next((i.get("severity") for i in result["issues"] if i["type"] == "boilerplate"), None)
    pg_sev = next((i.get("severity") for i in result["issues"] if i["type"] == "pg_low"), None)
    std_sev = next((i.get("severity") for i in result["issues"] if i["type"] == "std_fabrication"), None)
    if "blind_violation" in types_present or "missing" in types_present \
            or sk_sev == "major" or sub_sev == "major" or bp_sev == "moderate" \
            or pg_sev == "major" or std_sev == "moderate":
        result["severity"] = "major"
    elif "title_mismatch" in types_present or sk_sev == "moderate" or sub_sev == "moderate" \
            or pg_sev == "moderate":
        result["severity"] = "moderate"
    elif "chars_short" in types_present or "keyword_coverage_low" in types_present \
            or "decorative_chars" in types_present or "decorative_arrows" in types_present \
            or pg_sev == "minor" or std_sev == "minor":
        result["severity"] = "minor"
    else:
        result["severity"] = "ok"

    return result


# ─── Variant / 单份流程 ───────────────────────────────────────────────

def review_variant(
    chapter_dir: Path, variant: dict,
    chapters: list[dict], disq: dict, log: logging.Logger,
    subitems_map: dict[str, list[dict]] | None = None,
) -> dict:
    """审一个 variant 目录（或单份的 output/ 根目录）"""
    subitems_map = subitems_map or {}
    results = []
    for ch in chapters:
        md = chapter_dir / ch["filename"]
        ch_subitems = subitems_map.get(ch["num"], [])
        r = review_chapter(
            md, ch, disq, variant.get("company_name", ""), log,
            variant=variant, subitems=ch_subitems,
        )
        results.append(r)
        mark = {"ok": "✓", "minor": "·", "moderate": "⚠", "major": "✗"}[r["severity"]]
        sub_info = ""
        if ch_subitems:
            sub_info = f"  sub={r.get('subitem_count_actual', 0)}/{r.get('subitem_count_expected', 0)}"
        log.info(
            f"  {mark} {ch['num']} {ch['title'][:20]}  "
            f"chars={r.get('actual_chars', 0)}/{r['target_chars']}{sub_info}  severity={r['severity']}"
        )

    severities = [r["severity"] for r in results]
    overall = "major" if "major" in severities else \
              "moderate" if "moderate" in severities else \
              "minor" if "minor" in severities else "ok"

    return {
        "variant_id": variant.get("id", "single"),
        "company_name": variant.get("company_name", ""),
        "model": variant.get("model", ""),
        "overall_severity": overall,
        "chapter_count": len(results),
        "ok_count": sum(1 for r in results if r["severity"] == "ok"),
        "chapters": results,
    }


def write_variant_report(chapter_dir: Path, variant_result: dict) -> None:
    """在 variant 目录下写 review.md / review.json"""
    (chapter_dir / "review.json").write_text(
        json.dumps(variant_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"# 审校报告 - {variant_result['variant_id']} {variant_result['company_name']}",
        "",
        f"**总体评价：** {variant_result['overall_severity'].upper()}  "
        f"（{variant_result['ok_count']}/{variant_result['chapter_count']} 章无问题）",
        f"**模型：** {variant_result['model']}",
        "",
        "## 各章节详情",
        "",
        "| 章 | 标题 | 字数 | 关键词 | 子项H2 | 严重性 | 问题 |",
        "|----|------|------|--------|--------|--------|------|",
    ]
    sev_symbol = {"ok": "✅", "minor": "·", "moderate": "⚠", "major": "❌"}
    for c in variant_result["chapters"]:
        title = c["expected_title"][:18]
        chars = f"{c.get('actual_chars', 0)}/{c['target_chars']}"
        kw = f"{c.get('keyword_hit', '-')}/{c.get('keyword_total', '-')}" if "keyword_total" in c else "-"
        if "subitem_count_expected" in c:
            sub = f"{c['subitem_count_actual']}/{c['subitem_count_expected']}"
        else:
            sub = "-"
        sev = sev_symbol.get(c["severity"], "?")
        issues = "；".join(i["detail"][:40] for i in c["issues"]) or "—"
        lines.append(f"| {c['chapter_num']} | {title} | {chars} | {kw} | {sub} | {sev} | {issues} |")

    (chapter_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")


# ─── 主入口 ───────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="机器层审校工具")
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--variant", help="仅审指定 variant")
    p.add_argument("--single", action="store_true", help="单份模式：审 output/ 根目录")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"

    log = make_script_logger("bid_reviewer", output / "reviewer.log")
    log.info("=" * 60)
    log.info(f"bid_reviewer 启动  目录: {tender_dir}")

    chapters = parse_chapter_plan(extracted / "chapter_plan.md")
    if not chapters:
        log.error("未解析到 chapter_plan.md 章节")
        return 2

    subitems_map = parse_plan_subitems(extracted / "chapter_plan.md")
    if subitems_map:
        total = sum(len(v) for v in subitems_map.values())
        log.info(f"子项分配已解析：{total} 条，覆盖 {len(subitems_map)} 章 — 将启用 H2 1:1 核查")
    else:
        log.warning("chapter_plan.md 无「## 子项分配」段 — 跳过 H2 1:1 核查（仅单份模式可接受）")

    disq = read_disqualification_terms(extracted / "disqualification_rules.md")
    log.info(f"chapter_plan: {len(chapters)} 章  is_blind={disq['is_blind']}  "
             f"forbidden_terms={len(disq['forbidden'])}")

    all_results = []

    if args.single:
        # 单份模式
        log.info("【单份模式】审校 output/ 根目录")
        r = review_variant(output, {"id": "single"}, chapters, disq, log, subitems_map)
        write_variant_report(output, r)
        all_results.append(r)
    else:
        # 批量模式
        variants_path = extracted / "variants.json"
        if not variants_path.exists():
            log.error(f"需要 {variants_path}（或使用 --single）")
            return 2
        payload = json.loads(variants_path.read_text(encoding="utf-8"))
        variants = payload["variants"]
        if args.variant:
            variants = [v for v in variants if v["id"].upper() == args.variant.upper()]

        for v in variants:
            v_dir = output / v["id"]
            if not v_dir.is_dir():
                log.warning(f"跳过 {v['id']}：目录不存在")
                continue
            log.info(f"\n━━━ 审校 {v['id']} ({v['company_name']}) model={v['model']} ━━━")
            r = review_variant(v_dir, v, chapters, disq, log, subitems_map)
            write_variant_report(v_dir, r)
            all_results.append(r)

    # 汇总
    summary = {
        "total_variants": len(all_results),
        "all_ok": sum(1 for r in all_results if r["overall_severity"] == "ok"),
        "has_major": sum(1 for r in all_results if r["overall_severity"] == "major"),
        "has_moderate": sum(1 for r in all_results if r["overall_severity"] == "moderate"),
        "has_minor": sum(1 for r in all_results if r["overall_severity"] == "minor"),
        "variants": all_results,
    }
    (output / "review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.info("=" * 60)
    log.info(f"审校完成  共 {summary['total_variants']} 份  "
             f"ok={summary['all_ok']}  major={summary['has_major']}  "
             f"moderate={summary['has_moderate']}  minor={summary['has_minor']}")
    log.info(f"汇总文件: {output / 'review_summary.json'}")

    return 0 if summary["has_major"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
