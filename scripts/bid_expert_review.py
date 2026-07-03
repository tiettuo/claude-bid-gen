#!/usr/bin/env python3
"""bid_expert_review.py — 评标专家：对照招标评审标准给已生成方案打分。

挂入 bid_run 的 similarity 之后、summary 之前。**只出报告、不阻断、不回 writer。**

三块逻辑：
1. 逐项瑕疵制打分（LLM）：读 evaluation_method.json 的 scoring_items，按章节同序映射，
   评标专家 persona 逐项找瑕疵→按招标原文阶梯给分。
2. 符合性/废标风险扫描（规则，复用 bid_reviewer）：占位符 / 写手指令泄漏 / 规范年份伪造 /
   金额失实（正文 vs 清单真值）/ 工期矛盾。
3. 跨章数据一致性（LLM）：同一参数跨章是否打架（尺寸/规格/吨位/材料/工期节点）。

输出：
- output/VXX/expert_review.json + expert_review.md   （单变体）
- output/expert_review_summary.json                   （全变体汇总+排名）
- output/reports/评标专家评分报告.md                  （最终交付报告）

退出码恒为 0（不阻断管道）；LLM 失败/缺评分表时降级产出部分报告。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bid_common import make_script_logger, parse_chapter_plan, get_bid_gen_home  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402
from bid_skeleton_extract import _parse_json_with_retry  # noqa: E402
import bid_reviewer  # 复用 scan_placeholders / scan_prompt_leak / _check_standards_in_text  # noqa: E402

DEFAULT_MODEL = "deepseek"  # feedback_bid_default_deepseek：GLM 偶发卡，deepseek 1-3s

# ── 评标专家 persona ──────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名拥有 20 年经验的资深工程评标专家，曾担任多个政府采购工程项目的评委。
你的任务：严格对照招标文件规定的评审标准，对投标人提交的施工组织设计方案逐项打分。

【瑕疵制评分】方案内容不存在瑕疵得满分；存在 N 处瑕疵按招标文件评分标准原文规定的阶梯扣分。
【瑕疵定义】符合以下任一即算 1 处瑕疵：
  1. 与本项目实际不符：出现本项目没有的工程内容（如本项目是钢结构长廊却写深基坑/高层脚手架/大体积混凝土/大型土方）、设备与人员虚胖（小项目配几十台大型设备或庞大项目部）、非本项目的工种
  2. 要素缺失：招标"包含不限于"要求的要素有缺项
  3. 数据错误/自相矛盾：工期/面积/金额/数量错误或前后矛盾、引用不存在的规范或错误版本年份
  4. 纯套话无项目针对性：全篇"严格遵守、确保质量"式空话，缺乏本项目具体参数（面积/工期/结构类型/地点）
  5. 伪造痕迹/占位符：占位人名（李四/张三/王五）/电话/XX/写手指令（此处修改/variants/组装阶段/主笔编辑/Mermaid）泄漏
  6. 逻辑硬伤：工序顺序错乱、不可行的施工方法、与招标硬约束冲突（如不停课项目却安排夜间强噪声作业）

【硬要求】
- 每处瑕疵必须引用方案原文摘录并带章节号/小节号定位，不得泛泛而谈。
- 严格基于实际方案内容判定，不要臆测方案没写的内容；方案没提供的要素才算缺失。
- 只输出一个 JSON 对象，不要任何 markdown 标记、代码围栏或解释文字。"""

SCORING_USER_TEMPLATE = """【项目背景】
{project_brief}

【已知真值】（正文出现的数值必须与此一致，否则计为"数据失实"瑕疵）
{amount_truth}

【本评分项】（招标文件评审标准原文）
评分项：{item_title}（满分 {item_score} 分）
评分标准原文：{criteria_original}

【待评审章节内容】（对应本评分项，markdown 原文）
{chapter_md}

【任务】按上述瑕疵制规则找出本章节的瑕疵，并按"评分标准原文"规定的扣分阶梯给出得分。
输出 JSON，结构如下：
{{
  "element_coverage": {{"要素名1": true, "要素名2": false}},
  "defects": [
    {{"type": "1-6 之一的瑕疵类型", "quote": "方案原文摘录", "anchor": "章节号或小节号定位", "reason": "为何是瑕疵（50 字内）"}}
  ],
  "defect_count": 缺陷总数,
  "score": 按 criteria_original 阶梯算出的得分,
  "summary": "一句话评价"
}}"""

CROSS_CHAPTER_USER_TEMPLATE = """【项目背景】
{project_brief}

以下是同一份投标方案的全部章节正文。请以评标专家视角，只找【跨章节】的数据矛盾与不一致
（同一参数在不同章节出现不同数值：如建筑物长度/钢结构规格与数量/材料材质与厚度/设备吨位与数量/
工期节点与天数/班组人数/用电容量/安全文明施工费金额 等）。
不要报告单章内部问题（那由逐项评分负责），只报告跨章矛盾。

【全章节内容】
{all_chapters}

输出 JSON：{{
  "cross_defects": [
    {{"param": "矛盾参数名", "conflicts": [{{"chapter": "章节号", "value": "数值或原文摘录"}}], "reason": "为何是矛盾（50 字内）"}}
  ]
}}"""


# ── 评分项 ↔ 章节映射 ────────────────────────────────────────────────
def load_scoring_items(extracted: Path, log) -> list[dict]:
    """读 evaluation_method.json，返回技术类 scoring_items（逐项打分依据）。"""
    em_path = extracted / "evaluation_method.json"
    if not em_path.exists():
        log.warning(f"未找到 {em_path}，无法逐项打分（降级：仅符合性扫描）")
        return []
    try:
        em = json.loads(em_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"evaluation_method.json 解析失败：{e}")
        return []
    items = em.get("scoring_items", []) or []
    # 只取技术类评分项（section 含"技术"或 title 非空且 score>0）
    tech = [it for it in items if (it.get("section", "") or "").find("技术") >= 0]
    if not tech:
        tech = [it for it in items if (it.get("score") or 0) > 0 and it.get("title_original")]
    log.info(f"加载 {len(tech)} 个技术评分项")
    return tech


def pair_chapters_scoring(chapters: list[dict], scoring_items: list[dict]) -> list[tuple[dict, dict]]:
    """评分制下章节与 scoring_items 同序一一对应（zip）。只保留评分锚定章。"""
    # 评分锚定章：score>0 或 score_item 含"评分项"
    scored = [ch for ch in chapters
              if (ch.get("score") or 0) > 0
              or "评分项" in (ch.get("score_item") or "")]
    if not scored:
        scored = chapters
    pairs = list(zip(scored, scoring_items))
    return pairs


# ── 符合性 / 废标风险扫描（规则，基于 md）─────────────────────────────
def extract_amount_truth(extracted: Path, log) -> dict:
    """从工程量清单控制价.txt 抽取金额真值（安全文明施工费/暂列金额/控制价）。"""
    truth: dict[str, float] = {}
    for name in ("工程量清单控制价.txt", "招标控制价.txt"):
        txt = extracted / name
        if txt.exists():
            text = txt.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if "安全文明施工费" in line and "safe_civilization_fee" not in truth:
                    nums = re.findall(r"\d[\d,]*\.?\d*", line)
                    if nums:
                        try:
                            truth["safe_civilization_fee"] = float(nums[-1].replace(",", ""))
                        except ValueError:
                            pass
                if "暂列金额" in line and "provisional_sum" not in truth:
                    nums = re.findall(r"\d[\d,]*\.?\d*", line)
                    # 取最后一个像金额的数（排除序号）
                    for n in reversed(nums):
                        v = n.replace(",", "")
                        try:
                            fv = float(v)
                            if fv > 100:  # 金额一般 >100
                                truth["provisional_sum"] = fv
                                break
                        except ValueError:
                            continue
            break
    return truth


def format_amount_truth(truth: dict, facts: dict) -> str:
    """格式化真值为 prompt 片段。"""
    lines = []
    if "safe_civilization_fee" in truth:
        lines.append(f"- 安全文明施工费 = {truth['safe_civilization_fee']} 元（正文须与此一致）")
    if "provisional_sum" in truth:
        lines.append(f"- 暂列金额 = {truth['provisional_sum']} 元（不得修改）")
    ctrl = ((facts.get("control_price") or {}).get("value")) if isinstance(facts.get("control_price"), dict) else None
    if ctrl:
        lines.append(f"- 招标控制价/采购预算 = {ctrl} 元（投标报价不得超过）")
    return "\n".join(lines) if lines else "（清单未解析到明确金额真值，请基于方案内部自洽性判断）"


def scan_amount_mismatch(text: str, truth: dict) -> list[str]:
    """正文出现的"安全文明施工费/暂列金额 ... 金额"与真值不符→瑕疵。"""
    hits: list[str] = []
    truth_map = {
        "safe_civilization_fee": ("安全文明施工费", truth.get("safe_civilization_fee")),
        "provisional_sum": ("暂列金额", truth.get("provisional_sum")),
    }
    for key, (label, tv) in truth_map.items():
        if tv is None:
            continue
        for m in re.finditer(rf"{label}[^。\n]{{0,30}}?(\d[\d,]*\.?\d*)\s*元", text):
            val = m.group(1).replace(",", "")
            try:
                if abs(float(val) - float(tv)) > 0.01:
                    hits.append(f"[金额失实] {label} 正文写 {m.group(1)} 元，清单真值 {tv} 元")
            except ValueError:
                continue
    return hits


def scan_compliance(chapters_text: list[tuple[str, str]], duration_days, log) -> list[dict]:
    """复用 bid_reviewer 规则 + 金额/工期对比，返回符合性问题列表。

    chapters_text: [(章节号, md 正文), ...]
    """
    issues: list[dict] = []
    all_text = "\n".join(t for _, t in chapters_text)

    # 1) 占位符 / 写手指令泄漏 / 规范伪造（复用 reviewer）
    for num, text in chapters_text:
        for hit in bid_reviewer.scan_placeholders(text):
            issues.append({"chapter": num, "kind": "占位符/伪造痕迹", "detail": hit})
        for hit in bid_reviewer.scan_prompt_leak(text):
            issues.append({"chapter": num, "kind": "写手指令泄漏", "detail": hit})
        std = bid_reviewer._check_standards_in_text(text)
        for d in std.get("details", []):
            issues.append({
                "chapter": num, "kind": "规范年份伪造",
                "detail": f"{d['standard']} 引用 {d['cited_year']}，现行 {d['true_year']}",
            })

    # 2) 金额失实（需清单真值，由调用方注入 truth 后单独跑；此处略，留 main 统一）
    return issues


def scan_duration(text: str, duration_days) -> list[str]:
    """正文【总工期】天数与招标工期不符 → 瑕疵。

    只匹配"总工期/合同工期/投标工期/建设工期"等明确指总工期的表述，
    排除阶段工期、工序工期（如"钢结构工期 18 日历天""第一阶段 12 日历天"），
    以免把分阶段安排误报成总工期矛盾。
    """
    if not duration_days:
        return []
    hits: list[str] = []
    seen: set[int] = set()
    for m in re.finditer(r"(?:总工期|合同工期|投标工期|建设工期)[^。\n]{0,10}?(\d{2,3})\s*(?:个?日历天|天)", text):
        try:
            d = int(m.group(1))
        except ValueError:
            continue
        if d in seen:
            continue
        if 10 < d < 1000 and d != duration_days and abs(d - duration_days) > 2:
            seen.add(d)
            hits.append(f"[工期矛盾] 总工期正文写 {d} 天，招标工期 {duration_days} 天")
    return hits


# ── LLM 评分 ──────────────────────────────────────────────────────────
def score_one_item(client, model, score_item, chapter_md, project_brief, amount_truth_str, log):
    """对单个评分项打分。返回 dict 或 None。"""
    title = score_item.get("title_original", "")
    score = score_item.get("score", "")
    criteria = score_item.get("criteria_original", "")
    user = SCORING_USER_TEMPLATE.format(
        project_brief=project_brief,
        amount_truth=amount_truth_str,
        item_title=title, item_score=score, criteria_original=criteria,
        chapter_md=chapter_md[:6000],  # 截断防超长
    )
    try:
        return _parse_json_with_retry(client, model, SYSTEM_PROMPT, user,
                                      log=log, max_tokens=4000)
    except Exception as e:
        log.warning(f"评分项「{title}」LLM 调用失败：{e}")
        return None


def cross_chapter_check(client, model, all_chapters_md, project_brief, log):
    """跨章一致性 LLM。返回 dict 或 None。"""
    user = CROSS_CHAPTER_USER_TEMPLATE.format(
        project_brief=project_brief,
        all_chapters=all_chapters_md[:12000],
    )
    try:
        return _parse_json_with_retry(client, model, SYSTEM_PROMPT, user,
                                      log=log, max_tokens=4000)
    except Exception as e:
        log.warning(f"跨章一致性 LLM 调用失败：{e}")
        return None


# ── 单变体评审 ────────────────────────────────────────────────────────
def load_project_brief(extracted: Path) -> str:
    """从 project_facts.json 取一句项目背景。"""
    fp = extracted / "project_facts.json"
    if not fp.exists():
        return ""
    try:
        facts = json.loads(fp.read_text(encoding="utf-8")).get("facts", {})
        parts = []
        for key in ("project_name", "project_scope", "location", "duration_days",
                    "control_price", "qualification"):
            v = facts.get(key)
            if isinstance(v, dict):
                v = v.get("value")
            if v:
                parts.append(f"{key}={v}")
        return "；".join(parts)[:400]
    except Exception:
        return ""


def detect_variants(output_dir: Path) -> list[tuple[str, Path]]:
    """检测变体：output/VXX/（批量）或 output/ 本身（单份）。"""
    vdirs = sorted([p for p in output_dir.glob("V*") if p.is_dir()
                    and re.fullmatch(r"V\d+", p.name)])
    if vdirs:
        return [(p.name, p) for p in vdirs]
    return [("single", output_dir)]


def review_variant(name: str, variant_dir: Path, extracted: Path,
                   scoring_items: list[dict], client, model,
                   project_brief: str, amount_truth_str: str,
                   amount_truth: dict, duration_days, log) -> dict:
    """评审单个变体，返回结果 dict。"""
    log.info(f"── 评审变体 {name} ──")
    # 章节计划：优先变体专属，回退基准
    plan = variant_dir / "chapter_plan_variant.md"
    if not plan.exists():
        plan = extracted / "chapter_plan.md"
    chapters = parse_chapter_plan(plan)
    if not chapters:
        log.warning(f"{name}：未解析到章节计划")
        chapters = []

    pairs = pair_chapters_scoring(chapters, scoring_items)
    item_results: list[dict] = []

    # 逐项打分
    for ch, si in pairs:
        num = ch.get("num", "")
        md_path = variant_dir / ch.get("filename", "")
        if not md_path.exists():
            # 兜底：按 num 前缀找
            cands = list(variant_dir.glob(f"{str(num).zfill(2)}_*.md"))
            if cands:
                md_path = cands[0]
        if not md_path.exists():
            log.warning(f"{name}：章节 {num} 的 md 缺失（{ch.get('filename')}）")
            item_results.append({"item": si.get("title_original", ""),
                                 "score": None, "defect_count": None,
                                 "defects": [], "error": "章节 md 缺失"})
            continue
        chapter_md = md_path.read_text(encoding="utf-8", errors="ignore")
        r = score_one_item(client, model, si, chapter_md, project_brief, amount_truth_str, log)
        if r is None:
            item_results.append({"item": si.get("title_original", ""),
                                 "score": None, "defect_count": None,
                                 "defects": [], "error": "LLM 评分失败"})
        else:
            item_results.append({
                "item": si.get("title_original", ""),
                "element_coverage": r.get("element_coverage", {}),
                "defects": r.get("defects", []),
                "defect_count": r.get("defect_count", len(r.get("defects", []))),
                "score": r.get("score"),
                "summary": r.get("summary", ""),
            })

    # 符合性扫描（规则）
    chapters_text: list[tuple[str, str]] = []
    all_md_parts: list[str] = []
    for ch in chapters:
        num = ch.get("num", "")
        md_path = variant_dir / ch.get("filename", "")
        if not md_path.exists():
            cands = list(variant_dir.glob(f"{str(num).zfill(2)}_*.md"))
            if cands:
                md_path = cands[0]
        if md_path.exists():
            txt = md_path.read_text(encoding="utf-8", errors="ignore")
            chapters_text.append((num, txt))
            all_md_parts.append(f"### 章 {num}\n{txt[:3000]}")
    compliance = scan_compliance(chapters_text, duration_days, log)
    full_text = "\n".join(t for _, t in chapters_text)
    for x in scan_amount_mismatch(full_text, amount_truth):
        compliance.append({"chapter": "-", "kind": "金额失实", "detail": x})
    for x in scan_duration(full_text, duration_days):
        compliance.append({"chapter": "-", "kind": "工期矛盾", "detail": x})

    # 跨章一致性（LLM）
    cross = cross_chapter_check(client, model, "\n".join(all_md_parts), project_brief, log)
    cross_defects = (cross or {}).get("cross_defects", []) if cross else []

    # 汇总
    valid_scores = [r["score"] for r in item_results if isinstance(r.get("score"), (int, float))]
    total = sum(valid_scores)
    max_total = sum(si.get("score", 0) for si in scoring_items) or None
    result = {
        "variant": name,
        "item_results": item_results,
        "compliance": compliance,
        "cross_defects": cross_defects,
        "tech_score": total,
        "tech_max": max_total,
        "fatal_count": sum(1 for c in compliance if c["kind"] in ("占位符/伪造痕迹", "写手指令泄漏", "金额失实")),
    }
    # 落变体报告
    write_variant_report(variant_dir, result, log)
    return result


# ── 报告输出 ──────────────────────────────────────────────────────────
def write_variant_report(variant_dir: Path, result: dict, log) -> None:
    """写 expert_review.json + expert_review.md 到变体目录。"""
    (variant_dir / "expert_review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md: list[str] = []
    md.append(f"# 评标专家评审 — {result['variant']}\n")
    md.append(f"**技术得分：{result['tech_score']}/{result['tech_max']}**  "
              f"致命瑕疵 {result['fatal_count']} 处\n")
    md.append("## 逐评分项打分\n")
    md.append("| 评分项 | 得分 | 瑕疵数 |")
    md.append("|---|---|---|")
    for r in result["item_results"]:
        sc = r.get("score")
        sc = "—" if sc is None else sc
        md.append(f"| {r.get('item','')} | {sc} | {r.get('defect_count','—')} |")
    if result["compliance"]:
        md.append("\n## 符合性 / 废标风险")
        for c in result["compliance"]:
            md.append(f"- [{c['chapter']}] **{c['kind']}** {c['detail']}")
    if result["cross_defects"]:
        md.append("\n## 跨章节数据矛盾")
        for d in result["cross_defects"]:
            vals = "；".join(f"{x.get('chapter','')}: {x.get('value','')}" for x in d.get("conflicts", []))
            md.append(f"- **{d.get('param','')}** → {vals}（{d.get('reason','')}）")
    (variant_dir / "expert_review.md").write_text("\n".join(md), encoding="utf-8")
    log.info(f"  写出 {variant_dir.name}/expert_review.{{json,md}}  "
             f"得分 {result['tech_score']}/{result['tech_max']}")


def write_summary(output_dir: Path, results: list[dict], log) -> None:
    """写 expert_review_summary.json + reports/评标专家评分报告.md。"""
    ranking = sorted(results, key=lambda r: (r.get("tech_score") or -1), reverse=True)
    summary = {
        "ranking": [{"variant": r["variant"], "tech_score": r["tech_score"],
                     "tech_max": r["tech_max"], "fatal_count": r["fatal_count"]}
                    for r in ranking],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (output_dir / "expert_review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md: list[str] = ["# 评标专家评分报告\n",
                     f"生成时间：{summary['generated_at']}\n",
                     "## 排名\n",
                     "| 排名 | 变体 | 技术得分 | 致命瑕疵 |"]
    md.append("|---|---|---|---|")
    for i, r in enumerate(ranking, 1):
        md.append(f"| {i} | {r['variant']} | {r['tech_score']}/{r['tech_max']} | {r['fatal_count']} |")
    md.append("")
    for r in ranking:
        md.append(f"\n## {r['variant']}（{r['tech_score']}/{r['tech_max']}，致命 {r['fatal_count']}）\n")
        md.append("### 逐项打分")
        md.append("| 评分项 | 得分 | 瑕疵数 |")
        md.append("|---|---|---|")
        for it in r["item_results"]:
            sc = it.get("score"); sc = "—" if sc is None else sc
            md.append(f"| {it.get('item','')} | {sc} | {it.get('defect_count','—')} |")
        if r["compliance"]:
            md.append("\n### 符合性 / 废标风险")
            for c in r["compliance"][:30]:
                md.append(f"- [{c['chapter']}] **{c['kind']}** {c['detail']}")
        if r["cross_defects"]:
            md.append("\n### 跨章节数据矛盾")
            for d in r["cross_defects"][:20]:
                vals = "；".join(f"{x.get('chapter','')}: {x.get('value','')}" for x in d.get("conflicts", []))
                md.append(f"- **{d.get('param','')}** → {vals}（{d.get('reason','')}）")
    (reports_dir / "评标专家评分报告.md").write_text("\n".join(md), encoding="utf-8")
    log.info(f"写出 {reports_dir/'评标专家评分报告.md'}")


# ── main ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="评标专家：对照招标评审标准给方案打分")
    ap.add_argument("tender_dir", help="招标项目目录（含 extracted/ 与 output/）")
    ap.add_argument("--variant", default=None, help="只评审指定变体（如 V01）；默认全部")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM model_key（默认 {DEFAULT_MODEL}）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    extracted = tender_dir / "extracted"
    output_dir = tender_dir / "output"
    if not output_dir.exists():
        print(f"未找到 output 目录：{output_dir}", file=sys.stderr)
        return 0  # 不阻断

    output_dir.mkdir(parents=True, exist_ok=True)
    log = make_script_logger("bid_expert_review",
                             output_dir / "logs" / "expert_review.log", args.verbose)

    scoring_items = load_scoring_items(extracted, log)
    project_brief = load_project_brief(extracted)
    amount_truth = extract_amount_truth(extracted, log)
    facts = {}
    fp = extracted / "project_facts.json"
    if fp.exists():
        try:
            facts = json.loads(fp.read_text(encoding="utf-8")).get("facts", {})
        except Exception:
            pass
    amount_truth_str = format_amount_truth(amount_truth, facts)
    duration_days = facts.get("duration_days")
    if isinstance(duration_days, dict):
        duration_days = duration_days.get("value")
    log.info(f"项目背景：{project_brief[:120]}")
    log.info(f"金额真值：{amount_truth}")

    # LLM client（评分项为空时仍可做符合性扫描，但不调 LLM）
    client = None
    if scoring_items:
        try:
            client = LLMClient.load(log=log)
        except Exception as e:
            log.warning(f"LLMClient 加载失败：{e}（将仅做规则符合性扫描）")

    variants = detect_variants(output_dir)
    if args.variant:
        variants = [(n, d) for n, d in variants if n == args.variant or d.name == args.variant]
    if not variants:
        log.warning("未检测到任何变体目录")
        return 0

    results: list[dict] = []
    for name, vdir in variants:
        try:
            r = review_variant(name, vdir, extracted, scoring_items, client, args.model,
                               project_brief, amount_truth_str, amount_truth, duration_days, log)
            results.append(r)
        except Exception as e:
            log.error(f"变体 {name} 评审异常：{e}", exc_info=args.verbose)
            results.append({"variant": name, "item_results": [], "compliance": [],
                            "cross_defects": [], "tech_score": None, "tech_max": None,
                            "fatal_count": 0, "error": str(e)})

    if results:
        write_summary(output_dir, results, log)
    if client is not None:
        try:
            client.flush_stats_to_json(output_dir / "logs" / "llm_stats.json", "expert_review")
        except Exception:
            pass

    return 0  # 恒不阻断


if __name__ == "__main__":
    sys.exit(main())
