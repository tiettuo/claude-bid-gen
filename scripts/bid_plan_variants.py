#!/usr/bin/env python3
"""bid_plan_variants.py — 为每个变体生成差异化的「二级标题(子项)」规划。

在 variants 步骤之后、write 步骤之前运行。读共享的 chapter_plan.md(基准),
为每个变体派生一份 chapter_plan_variant.md。

★ 核心思想(2026-05-20 重构):
  ── 一级标题(章标题)──
    所有变体**逐字沿用基准 chapter_plan.md 的章标题**(= 评标表/大纲原文),
    绝不改写、不加前缀、不换同义词、不合并、不拆分、不重排。
    评分制下评委按评标表逐项定位章节,改字面 = 主题缺失 = 丢分
    (见 feedback_writer_h1_drift / feedback_bid_pass_fail_anchor)。

  ── 二级标题(子项)──
    由 LLM 读懂每章「评分标准」原文后,按**每个变体不同的切分维度**重新拟定:
      V01 按流程时序、V02 按管理职能、V03 按对象/专业维度 …(维度池见 _DECOMP_STRATEGIES)
    子项数量可不同,但每套都必须完整覆盖该章评分标准的全部要点。
    这样 N 份方案二级结构天然不同,而一级标题全部对齐评标表、不丢分。

  ── 反串标分工 ──
    本脚本只管「二级标题差异化」;
    编号体系差异 → bid_charts 的编号风格轮转(B/C/L…);
    正文差异     → 写作模型 / 温度 / 数据指纹;
    封面/排版/元数据差异 → bid_assemble。

产出 V{XX}/:
  1. chapter_plan_variant.md — 该变体章节规划(章标题同基准,子项为本变体专属)
  2. chart_assignment.json   — 图表类型分配

用法: python bid_plan_variants.py <tender_dir> [--target-pages N]
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bid_common import make_script_logger, parse_chapter_plan, parse_plan_subitems
from bid_llm_client import LLMClient


# ── 子项切分维度池 ─────────────────────────────────────────────────
# 每个变体按序号分到一种不同的切分逻辑,使 N 份方案二级结构天然分化。
# (name, description) — description 直接写进 LLM prompt。
_DECOMP_STRATEGIES: list[tuple[str, str]] = [
    ("流程时序",
     "按工作开展的全流程时间顺序切分:前期准备 → 组织实施 → 过程控制 → 检查验收 → "
     "收尾完善;子项体现先后递进的工作链条。"),
    ("管理职能",
     "按管理职能与要素切分:组织机构与职责、制度与流程规范、过程质量控制、"
     "资源保障、风险与应急;子项体现职能维度的分工。"),
    ("对象专业",
     "按工程对象 / 专业系统 / 设备类别等维度切分:分单体、分专业、分设备类别并列展开;"
     "子项体现对象的并列划分。"),
    ("要点对应",
     "按评分标准的关键要点逐条对应切分:把评分标准里出现的每个关键词 / 要求各自独立成节,"
     "确保对评分点逐条响应、不遗漏。"),
    ("目标递进",
     "按『目标定位 → 实施措施 → 资源与制度保障 → 检查考核 → 责任承诺』的递进逻辑切分。"),
    ("问题导向",
     "按『本项目特征与难点 → 针对性对策 → 实施与控制 → 效果保证』的问题—对策逻辑切分。"),
]


def assign_strategy(variant_index: int) -> tuple[str, str]:
    """按变体序号确定性分配切分维度,N 变体互不相同(N ≤ 维度池大小时无碰撞)。"""
    return _DECOMP_STRATEGIES[int(variant_index) % len(_DECOMP_STRATEGIES)]


# ── 工具 ──────────────────────────────────────────────────────────

def _is_tier_a(ch: dict) -> bool:
    """评分锚定章:评分项列非空 或 score>0。"""
    si = (ch.get("score_item") or "").strip()
    return bool(si and si not in ("—", "-", "无")) or ch.get("score", 0) > 0


def _est_pages(target_chars: int, chart_count: int) -> float:
    """字数 + 图表估算页数(约 625 字/页[14pt 四号 1.5 倍],每图 0.4 页)。"""
    return round(target_chars / 625.0 + chart_count * 0.4, 1)


# ── 评分标准 / 项目背景加载 ────────────────────────────────────────

def _load_criteria(extracted: Path, base_chapters: list[dict],
                   log: logging.Logger) -> dict[str, str]:
    """读 evaluation_method.json 的评分标准原文,按章序号映射 {num: criteria}。

    评分制下章节与 scoring_items 同序一一对应。缺失时返回空 dict,
    decompose 会回退到基准子项作为内容参考。
    """
    em_path = extracted / "evaluation_method.json"
    if not em_path.exists():
        return {}
    try:
        em = json.loads(em_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"evaluation_method.json 解析失败({e}),评分标准回退基准子项")
        return {}
    items = em.get("scoring_items", []) or []
    out: dict[str, str] = {}
    for ch, it in zip(base_chapters, items):
        crit = (it.get("criteria_original") or "").strip()
        if crit:
            out[str(ch["num"]).zfill(2)] = crit
    return out


def _load_project_brief(extracted: Path) -> str:
    """从 project_facts.json 取一句项目背景,供 LLM 切分时保持项目专属性。"""
    fp = extracted / "project_facts.json"
    if not fp.exists():
        return ""
    try:
        facts = json.loads(fp.read_text(encoding="utf-8")).get("facts", {})
        name = ((facts.get("project_name") or {}).get("value") or "").strip()
        scope = ((facts.get("project_scope") or {}).get("value") or "").strip()
        brief = "；".join(x for x in (name, scope) if x)
        return brief[:240]
    except Exception:
        return ""


# ── 变体章节构建(章标题/数量/顺序 = 基准,只缩放字数)──────────────

def build_variant_chapters(base_chapters: list[dict], variant: dict,
                           scale: float) -> list[dict]:
    """克隆基准章节为某变体的工作副本。

    ★ 章标题、章数量、章顺序、评分项、文件名 全部 1:1 沿用基准,绝不变换。
    仅按变体缩放因子 + 逐章 jitter 重设字数预算(字数差异也是反串标的一环)。
    """
    seed = variant.get("section_order_seed") or (hash(variant.get("id", "")) & 0xFFFFFFFF)
    rng = random.Random(seed)
    chapters: list[dict] = []
    for base in base_chapters:
        num = str(base["num"]).zfill(2)
        jitter = 1.0 + rng.uniform(-0.08, 0.08)
        target_chars = max(1500, int(base.get("target_chars", 2000) * scale * jitter))
        chart_count = int(base.get("chart_count", 0))
        chapters.append({
            "num": num,
            "title": base["title"],                       # ★ 逐字照基准,不改写
            "filename": base.get("filename") or f"{num}_{base['title']}.md",
            "score_item": base.get("score_item", ""),
            "score": base.get("score", 0),
            "target_chars": target_chars,
            "chart_count": chart_count,
            "est_pages": _est_pages(target_chars, chart_count),
            "_scope": [s["title"] for s in base.get("_subitems", [])],
        })
    return chapters


def verify_scoring_coverage(variant_chapters: list[dict],
                            base_score_items: set[str],
                            log: logging.Logger) -> None:
    """校验每个评分项仍有章节覆盖。本机制下章节不变换,属冗余安全闸。"""
    covered = {
        (c.get("score_item") or "").strip()
        for c in variant_chapters
        if (c.get("score_item") or "").strip() not in ("", "—", "-")
    }
    missing = base_score_items - covered
    if missing:
        raise RuntimeError(f"变体章节丢失评分项覆盖: {sorted(missing)}")
    log.info(f"    评分覆盖校验通过:{len(base_score_items)} 个评分项均有章节")


# ── LLM 子项分解(核心)───────────────────────────────────────────

_DECOMPOSE_PROMPT = """\
你是资深施工组织设计编写专家。现在为投标文件「{vid}」拟定各章的**二级子标题**。

【项目背景】{project_brief}

【本变体统一的切分维度 —— {strategy_name}】
{strategy_desc}

【硬性要求】
1. 一级章标题已固定(下方给出),**绝对不要改动章标题**,只为每章拟二级子标题。
2. 每章子项必须**完整覆盖该章「评分标准」的全部要点**,任何评分关键词都不得遗漏。
3. 子项切分必须严格贯彻本变体的【{strategy_name}】维度,使其与按其它维度
   切分的版本**结构上明显不同**(不是换同义词,是换组织逻辑)。
4. 子项数量由内容决定(一般 5-9 项,大分值章可更多),不强求与他人一致。
5. 子标题须结合项目实际,专业、具体、可落地;**严禁**「编制依据 / 实施方案 /
   保障措施 / 应急响应」这类放之四海皆准的空泛兜底标题。
6. 各章子项目标字数之和 = 该章「目标字数」(每项可 ±50 字浮动)。
7. 子标题不带任何序号前缀。

【章节与评分标准】
{chapters_brief}

严格只输出 JSON,不要任何解释:
{{"chapters": [{{"num": "01", "subitems": [{{"title": "...", "chars": N}}, ...]}}, ...]}}"""


def decompose_variant(client: LLMClient, model: str, variant_id: str,
                      strategy: tuple[str, str], chapters: list[dict],
                      criteria_map: dict[str, str], project_brief: str,
                      log: logging.Logger) -> dict[str, list[dict]]:
    """调 LLM 为一个变体的所有章节生成「按指定维度切分」的子项。

    返回 {num: [{title, chars}]}。失败则返回空 dict,由 _fallback_subitems 兜底。
    """
    sname, sdesc = strategy
    briefs = []
    for c in chapters:
        crit = criteria_map.get(c["num"], "")
        ref = "、".join(c["_scope"][:12]) if c["_scope"] else ""
        lines = [f"- 第{c['num']}章「{c['title']}」(目标字数 {c['target_chars']})"]
        if crit:
            lines.append(f"  评分标准原文:{crit}")
        if ref:
            lines.append(f"  内容参考(基准子项,可按本变体维度拆解重组):{ref}")
        briefs.append("\n".join(lines))

    prompt = _DECOMPOSE_PROMPT.format(
        vid=variant_id, strategy_name=sname, strategy_desc=sdesc,
        project_brief=project_brief or "工程建设投标项目",
        chapters_brief="\n".join(briefs),
    )
    try:
        resp = client.chat(
            model_key=model,
            system="你是资深施工组织设计编写专家,擅长按指定维度对章节主题做差异化的子项切分。",
            user=prompt,
            temperature=0.85,
        )
        m = re.search(r"\{[\s\S]*\}", resp.text)
        if not m:
            raise ValueError("LLM 返回无 JSON")
        data = json.loads(m.group())
        result: dict[str, list[dict]] = {}
        for ch in data.get("chapters", []):
            num = str(ch.get("num", "")).zfill(2)
            subs = [
                {"title": re.sub(r"^\d+(?:\.\d+)*[、.\s]*", "", str(s.get("title", ""))).strip(),
                 "chars": int(s.get("chars", 0) or 0)}
                for s in ch.get("subitems", []) if s.get("title")
            ]
            if subs:
                result[num] = subs
        return result
    except Exception as e:
        log.warning(f"  变体 {variant_id} 子项 LLM 分解失败({e}),用基准子项兜底")
        return {}


def _fallback_subitems(ch: dict) -> list[dict]:
    """LLM 失败时的兜底:直接用基准子项,字数按目标均分。"""
    scope = ch.get("_scope") or [ch["title"]]
    n = max(1, len(scope))
    per = max(300, ch["target_chars"] // n)
    return [{"title": t, "chars": per} for t in scope]


def _normalize_subitems(subs: list[dict], target_chars: int) -> list[dict]:
    """把子项字数之和校正到 target_chars。"""
    if not subs:
        return subs
    total = sum(s["chars"] for s in subs) or 1
    factor = target_chars / total
    for s in subs:
        s["chars"] = max(200, int(s["chars"] * factor))
    diff = target_chars - sum(s["chars"] for s in subs)
    subs[0]["chars"] = max(200, subs[0]["chars"] + diff)
    return subs


# ── 渲染变体规划文件 ──────────────────────────────────────────────

def render_variant_plan(variant_id: str, strategy: tuple[str, str],
                        chapters: list[dict]) -> str:
    """渲染该变体完整章节规划(表格 + 子项分配),与 chapter_plan.md 同构。"""
    lines = [
        f"# 变体 {variant_id} 章节规划",
        "",
        f"> 由 bid_plan_variants.py 从 chapter_plan.md 派生。",
        f"> 一级标题逐字沿用评标表原文(三家一致);二级子项按【{strategy[0]}】维度切分(本变体专属)。",
        "",
        "## 大章节分配",
        "",
        "| 章节号 | 文件名 | 标题 | 评分项 | 分值 | 子项数 | 目标字数 | 字数带 ±15% | 图表数 | 页数估 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    total_chars = total_pages = 0
    for c in chapters:
        si = (c.get("score_item") or "").strip() or "—"
        score = c.get("score", 0)
        score_s = f"{score:g}" if score else "0"
        tc = c["target_chars"]
        lo, hi = int(tc * 0.85), int(tc * 1.15)
        subs = c.get("subitems", [])
        sub_n = len(subs)
        ep = c.get("est_pages", _est_pages(tc, c["chart_count"]))
        total_chars += tc
        total_pages += ep
        lines.append(
            f"| {c['num']} | {c['filename']} | {c['title']} | {si} | {score_s} | "
            f"{sub_n} | {tc} | {lo}-{hi} | {c['chart_count']} | {ep} |"
        )
    lines += [
        f"| **合计** | | | | | | **{total_chars}** | | | **{round(total_pages, 1)}** |",
        "",
        "## 子项分配",
        "",
    ]
    for c in chapters:
        subs = c.get("subitems", [])
        lines.append(
            f"### {c['num']} {c['title']}"
            f"({c['target_chars']} 字,{c['chart_count']} 图表,"
            f"{c.get('est_pages', 0)} 页)"
        )
        for i, s in enumerate(subs, 1):
            lines.append(f"- {c['num']}.{i} {s['title']}: {s['chars']} 字")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── 图表分配(沿用原逻辑)──────────────────────────────────────────

def _assign_charts_for_variant(variant: dict, base_chart_specs, org_templates: dict,
                               chapters: list[dict],
                               work_labels: list[str] | None = None) -> dict:
    """为单个变体生成图表分配(确定性,基于 section_order_seed)。"""
    seed = variant.get("section_order_seed", 0)
    rng = random.Random(seed ^ 0xABCD1234)
    assignment = {
        "variant_id": variant["id"],
        "org_template": {},
        "management_chart_types": {},
        "chart_overrides": {},
    }
    templates = org_templates.get("templates", {})
    if templates:
        template_keys = list(templates.keys())
        chosen_key = template_keys[rng.randint(0, len(template_keys) - 1)]
        template = templates[chosen_key]
        variants_list = template.get("variants", [])
        if variants_list and rng.random() < 0.5:
            chosen_variant = variants_list[rng.randint(0, len(variants_list) - 1)]
            tree = chosen_variant.get("children", template["children"])
        else:
            tree = template["children"]
        tree = _projectize_org_tree(tree, work_labels or [])
        assignment["org_template"] = {
            "chapter": _find_org_chapter(chapters),
            "template_id": chosen_key,
            "template_label": template["label"],
            "root": template["root"],
            "children": tree,
        }
    mgmt_chapters = _find_management_chapters(chapters)
    combos = org_templates.get("management_chart_types", {}).get("combinations", [])
    if combos:
        chosen_combo = combos[rng.randint(0, len(combos) - 1)]
        assignment["management_chart_types"] = {
            ch: ctype for ch, ctype in zip(mgmt_chapters, chosen_combo)
        }
    if base_chart_specs:
        for ch_num_str, specs in base_chart_specs.items():
            overrides = []
            for spec in specs:
                if spec.get("required", False):
                    continue
                if rng.random() < 0.3:
                    alternatives = {
                        "hierarchy_flow": ["process_flow", "org_chart"],
                        "process_flow": ["hierarchy_flow"],
                        "org_chart": ["hierarchy_flow", "process_flow"],
                        "schedule": ["resource_curve"],
                        "site_layout": ["process_flow"],
                    }
                    cur_type = spec.get("type", "process_flow")
                    alts = alternatives.get(cur_type, ["process_flow"])
                    overrides.append({
                        "section": spec.get("section", ""),
                        "original_type": cur_type,
                        "override_type": alts[rng.randint(0, len(alts) - 1)],
                    })
                elif rng.random() < 0.25:
                    overrides.append({
                        "section": spec.get("section", ""),
                        "original_type": spec.get("type", ""),
                        "override_type": None,
                    })
            if overrides:
                assignment["chart_overrides"][ch_num_str] = overrides
    return assignment


def _find_org_chapter(chapters: list[dict]) -> str:
    for ch in chapters:
        if any(k in ch.get("title", "") for k in ("施工方案", "技术措施", "施工部署", "组织")):
            return str(ch["num"]).zfill(2)
    return str(chapters[0]["num"]).zfill(2) if chapters else "01"


def _find_management_chapters(chapters: list[dict]) -> list[str]:
    found = []
    for ch in chapters:
        if any(k in ch.get("title", "") for k in ("质量", "安全", "环保", "环境")):
            found.append(str(ch["num"]).zfill(2))
    return found[:3]


_STALE_WORK_WORDS = ("棚圈", "牧道", "桥涵", "暖棚", "装配安装")


def _load_work_labels(extracted: Path) -> list[str]:
    """从 boq.json 生成项目专属作业队标签。"""
    boq_path = extracted / "boq.json"
    if not boq_path.exists():
        return []
    try:
        boq = json.loads(boq_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    labels, seen = [], set()
    for cat in boq.get("categories", []):
        name = cat.get("name") or cat.get("category") or ""
        name = re.sub(r"^未分类-?", "", name).strip("- ")
        if name and name not in seen:
            seen.add(name)
            labels.append(f"{name}施工队")
    return labels[:6]


def _projectize_org_tree(children: list[dict], work_labels: list[str]) -> list[dict]:
    """把组织模板里的行业残留作业队替换成本项目清单工程构成。"""
    if not work_labels:
        return children
    labels_iter = iter(work_labels)

    def next_label(default: str) -> str:
        try:
            return next(labels_iter)
        except StopIteration:
            return default

    def walk(nodes: list[dict]) -> list[dict]:
        out = []
        for node in nodes:
            n = dict(node)
            label = str(n.get("label", ""))
            if any(w in label for w in _STALE_WORK_WORDS):
                n["label"] = next_label(label)
            elif label.endswith(("工区", "施工队", "作业队")) and n.get("children"):
                n["label"] = next_label(label)
            n["children"] = walk(n.get("children", []))
            out.append(n)
        return out

    return walk(children)


# ── 主流程 ────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="为每个变体生成差异化二级标题的章节规划(一级标题锁定评标表原文)")
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--target-pages", type=float, default=None,
                   help="目标总页数(覆盖 chapter_plan 基准);缺省则沿用基准字数")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir)
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    log = make_script_logger("plan_variants", output / "logs" / "plan_variants.log")

    plan_path = extracted / "chapter_plan.md"
    variants_path = extracted / "variants.json"
    if not plan_path.exists():
        log.error(f"chapter_plan.md 不存在: {plan_path}")
        return 1
    if not variants_path.exists():
        log.error(f"variants.json 不存在: {variants_path}")
        return 1

    base_chapters = parse_chapter_plan(plan_path)
    base_subitems = parse_plan_subitems(plan_path)
    for c in base_chapters:
        c["_subitems"] = base_subitems.get(str(c["num"]).zfill(2), [])
    if not base_chapters:
        log.error("chapter_plan.md 解析不到章节")
        return 1

    variants = json.loads(variants_path.read_text(encoding="utf-8"))
    if isinstance(variants, dict):
        variants = variants.get("variants", [variants])
    n_variants = len(variants)

    base_score_items = {
        (c.get("score_item") or "").strip()
        for c in base_chapters
        if _is_tier_a(c) and (c.get("score_item") or "").strip() not in ("", "—", "-")
    }

    criteria_map = _load_criteria(extracted, base_chapters, log)
    project_brief = _load_project_brief(extracted)
    log.info(f"评分标准命中 {len(criteria_map)}/{len(base_chapters)} 章"
             f"{'(其余回退基准子项作参考)' if len(criteria_map) < len(base_chapters) else ''}")

    # chart_specs / org_templates / work_labels
    base_chart_specs: dict[str, list] = {}
    chart_specs_path = extracted / "chart_specs.json"
    if chart_specs_path.exists():
        raw = json.loads(chart_specs_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("specs", [])
        for spec in raw:
            if isinstance(spec, dict):
                base_chart_specs.setdefault(
                    str(spec.get("chapter", "")).zfill(2), []).append(spec)
    org_templates = {"templates": {}, "management_chart_types": {"combinations": []}}
    org_path = ROOT / "configs" / "org_templates.json"
    if org_path.exists():
        org_templates = json.loads(org_path.read_text(encoding="utf-8"))
    work_labels = _load_work_labels(extracted)
    if work_labels:
        log.info(f"清单驱动作业队标签: {work_labels}")

    # 目标总页数 → 全局缩放因子(缺省沿用基准字数)
    base_pages = sum(c.get("est_pages", 0) for c in base_chapters) or 1
    user_scale = 1.0
    if args.target_pages:
        user_scale = args.target_pages / base_pages
        log.info(f"用户指定目标页数 {args.target_pages}(基准 {base_pages:.0f}),"
                 f"全局缩放 ×{user_scale:.2f}")

    client = LLMClient.load(log=log)
    rewrite_model = "deepseek_chat"
    if rewrite_model not in (client.config.get("models", {})):
        rewrite_model = client.config.get("default_model", "deepseek")

    log.info(f"开始为 {n_variants} 个变体生成差异化二级标题"
             f"(一级标题锁定评标表原文,模型={rewrite_model})...")

    # 1. 每变体:章节 = 基准(不变换),只缩放字数;并分配切分维度
    variant_structures: list[tuple[dict, list[dict], tuple[str, str]]] = []
    for i, variant in enumerate(variants):
        # 变体缩放全部 ≤ 1.0:基准即上限,任何变体都不超基准字数
        # (页数硬顶项目防超顶;字数仍有差异 → 反串标)
        v_scale = user_scale * (1.0 + [0.0, -0.05, -0.02, -0.07, -0.04][i % 5])
        chapters = build_variant_chapters(base_chapters, variant, v_scale)
        verify_scoring_coverage(chapters, base_score_items, log)
        strategy = assign_strategy(i)
        log.info(f"  {variant['id']}: 切分维度【{strategy[0]}】, 字数 ×{v_scale:.2f}, "
                 f"{len(chapters)} 章(同基准)")
        variant_structures.append((variant, chapters, strategy))

    # 2. 并行 LLM 子项分解(每变体一次调用,按各自维度切分)
    def _decompose(item):
        variant, chapters, strategy = item
        return variant["id"], decompose_variant(
            client, rewrite_model, variant["id"], strategy, chapters,
            criteria_map, project_brief, log,
        )

    decomposed: dict[str, dict[str, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, n_variants)) as ex:
        for vid, result in ex.map(_decompose, variant_structures):
            decomposed[vid] = result

    # 3. 落子项 + 渲染 + 图表分配
    for variant, chapters, strategy in variant_structures:
        vid = variant["id"]
        sub_map = decomposed.get(vid, {})
        for c in chapters:
            subs = sub_map.get(c["num"]) or _fallback_subitems(c)
            c["subitems"] = _normalize_subitems(subs, c["target_chars"])

        v_dir = output / vid
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "chapter_plan_variant.md").write_text(
            render_variant_plan(vid, strategy, chapters), encoding="utf-8"
        )
        chart_assign = _assign_charts_for_variant(
            variant, base_chart_specs, org_templates, chapters, work_labels
        )
        (v_dir / "chart_assignment.json").write_text(
            json.dumps(chart_assign, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total_c = sum(c["target_chars"] for c in chapters)
        total_s = sum(len(c.get("subitems", [])) for c in chapters)
        log.info(f"  {vid}【{strategy[0]}】: {len(chapters)} 章 / {total_s} 子项 / "
                 f"{total_c} 字 已写入")

    log.info(f"完成:{n_variants} 个变体的差异化二级标题章节规划已生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
