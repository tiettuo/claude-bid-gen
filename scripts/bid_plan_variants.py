#!/usr/bin/env python3
"""bid_plan_variants.py — 为每个变体生成独立的标题计划 + 图表分配。

在 variants 步骤之后、write 步骤之前运行。
读取共享的 chapter_plan.md，为每个变体生成：
  1. V{XX}/chapter_plan_variant.md — H2 子标题同义改写
  2. V{XX}/chart_assignment.json — 图表类型分配（含组织架构模板、管理体系图类型）

用法: python bid_plan_variants.py <tender_dir>
"""

import json
import re
import sys
import random
import logging
from pathlib import Path

# ── 项目路径 ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bid_common import (
    make_script_logger,
    parse_chapter_plan,
    parse_plan_subitems,
)
from bid_llm_client import LLMClient


# ── LLM 子标题改写 ───────────────────────────────────────────────

_SUBITEM_REWRITE_PROMPT = """\
你是施工组织设计编写专家。以下是第{ch_num}章"{ch_title}"的标准子项列表：

{subitems_text}

请生成 {n_variants} 个不同的子标题方案。要求：
1. 各方案的子项数量必须按此指定：{count_targets}
2. 每个子标题与原文语义等价但措辞不同，不能出现与原文完全相同的措辞
3. 不同方案之间也不能互相照搬措辞
4. 如果子项数量有增减，增减的子项必须是原主题的合理拆分或合并
5. 每个"子标题"后附目标字数（总字数保持 {total_chars} 不变，各子项可 ±50 字浮动）

输出严格 JSON 格式：
{{
  "plans": [
    {{"subitems": [{{"title": "...", "chars": N}}, ...]}},
    ...
  ]
}}"""


def _rewrite_subitems_for_chapter(
    client: LLMClient,
    ch: dict,
    subitems: list[dict],
    n_variants: int,
    model_key: str,
    log: logging.Logger,
) -> list[list[dict]]:
    """调用 LLM 为一个章节生成 n_variants 个不同的子标题方案。"""
    base_count = len(subitems)
    total_chars = sum(s.get("chars", 0) for s in subitems)
    lo, hi = max(3, base_count - 2), base_count + 2

    # 为每个方案指定不同的子项数量，确保有差异
    count_targets = []
    for i in range(n_variants):
        # 循环使用 base-2, base-1, base, base+1, base+2
        offsets = [-2, -1, 0, 1, 2]
        offset = offsets[i % len(offsets)]
        target_count = max(3, min(base_count + 2, base_count + offset))
        count_targets.append(f"方案{i+1}={target_count}个")
    count_targets_str = "，".join(count_targets)

    subitems_text = "\n".join(
        f"- {s['title']}: {s.get('chars', 0)} 字" for s in subitems
    )

    prompt = _SUBITEM_REWRITE_PROMPT.format(
        ch_num=ch["num"],
        ch_title=ch["title"],
        subitems_text=subitems_text,
        n_variants=n_variants,
        count_targets=count_targets_str,
        total_chars=total_chars,
    )

    try:
        resp = client.chat(
            model_key=model_key,
            system="你是一位资深施工组织设计编写专家，擅长同一主题的多样化表述。",
            user=prompt,
            temperature=0.8,
        )
        text = resp.text.strip()
        # 提取 JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning(f"章 {ch['num']} LLM 返回无法解析，使用兜底方案")
            return _fallback_plans(subitems, n_variants)
        data = json.loads(m.group())
        plans = data.get("plans", [])
        if len(plans) < n_variants:
            plans += _fallback_plans(subitems, n_variants - len(plans))
        return plans[:n_variants]
    except Exception as e:
        log.warning(f"章 {ch['num']} LLM 调用失败: {e}，使用兜底方案")
        return _fallback_plans(subitems, n_variants)


def _fallback_plans(subitems: list[dict], n: int) -> list[list[dict]]:
    """兜底：基于 shuffle 生成 n 个变体方案（不调 LLM）。"""
    plans = []
    for i in range(n):
        rng = random.Random(42 + i * 1000)
        items = subitems[:]
        rng.shuffle(items)
        # 微调字数
        total = sum(s.get("chars", 0) for s in items)
        for j, s in enumerate(items):
            delta = rng.randint(-30, 30)
            s2 = dict(s)
            s2["chars"] = max(200, s.get("chars", 300) + delta)
            items[j] = s2
        # 修正总字数
        actual_total = sum(s["chars"] for s in items)
        if actual_total != total and items:
            diff = total - actual_total
            items[0]["chars"] = max(200, items[0]["chars"] + diff)
        plans.append({"subitems": items})
    return plans


# ── 图表分配 ──────────────────────────────────────────────────────

def _assign_charts_for_variant(
    variant: dict,
    base_chart_specs,
    org_templates: dict,
    chapters: list[dict],
) -> dict:
    """为单个变体生成图表分配（确定性，基于 section_order_seed）。"""
    seed = variant.get("section_order_seed", 0)
    rng = random.Random(seed ^ 0xABCD1234)

    assignment = {
        "variant_id": variant["id"],
        "org_template": {},
        "management_chart_types": {},
        "chart_overrides": {},
    }

    # 1. 组织架构模板选择（04 章专用）
    template_keys = list(org_templates["templates"].keys())
    chosen_key = template_keys[rng.randint(0, len(template_keys) - 1)]
    template = org_templates["templates"][chosen_key]

    # 选主模板或变体
    variants_list = template.get("variants", [])
    if variants_list and rng.random() < 0.5:
        chosen_variant = variants_list[rng.randint(0, len(variants_list) - 1)]
        tree = chosen_variant.get("children", template["children"])
    else:
        tree = template["children"]

    assignment["org_template"] = {
        "chapter": "04",
        "template_id": chosen_key,
        "template_label": template["label"],
        "root": template["root"],
        "children": tree,
    }

    # 2. 管理体系章节图表类型（08/10/11）
    mgmt_chapters = ["08", "10", "11"]
    combos = org_templates["management_chart_types"]["combinations"]
    chosen_combo = combos[rng.randint(0, len(combos) - 1)]
    assignment["management_chart_types"] = {
        ch: ctype for ch, ctype in zip(mgmt_chapters, chosen_combo)
    }

    # 3. 可选图表类型覆盖（基于 chart_specs.json 的 non-required 图表）
    if base_chart_specs:
        for ch_num_str, specs in base_chart_specs.items():
            overrides = []
            for spec in specs:
                if spec.get("required", False):
                    continue
                # 30% 概率换类型
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
                    new_type = alts[rng.randint(0, len(alts) - 1)]
                    overrides.append({
                        "section": spec.get("section", ""),
                        "original_type": cur_type,
                        "override_type": new_type,
                    })
                # 20% 概率删除该可选图表
                elif rng.random() < 0.25:
                    overrides.append({
                        "section": spec.get("section", ""),
                        "original_type": spec.get("type", ""),
                        "override_type": None,
                    })
            if overrides:
                assignment["chart_overrides"][ch_num_str] = overrides

    return assignment


# ── 写入变体大纲文件 ──────────────────────────────────────────────

def _format_chapter_section(
    chapter: dict,
    subitems_plan: list[dict],
) -> str:
    """格式化单个章节的子项分配文本块。"""
    ch_num_str = str(chapter["num"]).zfill(2)
    target = chapter.get("target_chars", 1950)
    chart_count = chapter.get("chart_count", 1)
    est_pages = chapter.get("est_pages", 3.5)

    lines = [
        f"### {ch_num_str} {chapter['title']}"
        f"({target} 字,{chart_count} 图表,{est_pages} 页)"
    ]

    for i, s in enumerate(subitems_plan, 1):
        title = s.get("title", f"子项{i}")
        # 去掉 LLM 输出里自带的编号前缀（如 "4.1 xxx" → "xxx"）
        title = re.sub(r"^\d+(?:\.\d+)*\s+", "", title)
        chars = s.get("chars", target // max(len(subitems_plan), 1))
        lines.append(f"- {ch_num_str}.{i} {title}: {chars} 字")

    return "\n".join(lines)


def _write_variant_plan(
    output_dir: Path,
    variant_id: str,
    chapter_sections: list[str],
    original_plan_path: Path,
) -> Path:
    """将变体的所有章节子项方案一次性写入 chapter_plan_variant.md。"""
    v_dir = output_dir / variant_id
    v_dir.mkdir(parents=True, exist_ok=True)
    out_path = v_dir / "chapter_plan_variant.md"

    lines = [
        f"# 变体 {variant_id} 章节子项分配",
        "",
        f"> 基于 {original_plan_path.name} 生成，仅包含子项分配。",
        "",
    ]
    lines.extend(chapter_sections)

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ── 主流程 ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法: python bid_plan_variants.py <tender_dir>")
        sys.exit(1)

    tender_dir = Path(sys.argv[1])
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    log = make_script_logger("plan_variants", output / "logs" / "plan_variants.log")

    # ── 加载输入 ──
    plan_path = extracted / "chapter_plan.md"
    variants_path = extracted / "variants.json"
    chart_specs_path = extracted / "chart_specs.json"
    org_templates_path = ROOT / "configs" / "org_templates.json"
    llm_config_path = ROOT / "configs" / "bid_llm_config.json"

    if not plan_path.exists():
        log.error(f"chapter_plan.md 不存在: {plan_path}")
        sys.exit(1)
    if not variants_path.exists():
        log.error(f"variants.json 不存在: {variants_path}")
        sys.exit(1)

    chapters = parse_chapter_plan(plan_path)
    all_subitems = parse_plan_subitems(plan_path)
    variants = json.loads(variants_path.read_text(encoding="utf-8"))
    if isinstance(variants, dict):
        variants = variants.get("variants", [variants])

    # chart_specs.json 可选
    base_chart_specs = {}
    if chart_specs_path.exists():
        raw = json.loads(chart_specs_path.read_text(encoding="utf-8"))
        for spec in raw:
            ch_key = str(spec.get("chapter", "")).zfill(2)
            base_chart_specs.setdefault(ch_key, []).append(spec)

    # org_templates
    org_templates = {"templates": {}, "management_chart_types": {"combinations": []}}
    if org_templates_path.exists():
        org_templates = json.loads(org_templates_path.read_text(encoding="utf-8"))

    # LLM 客户端
    llm_cfg = {}
    if llm_config_path.exists():
        llm_cfg = json.loads(llm_config_path.read_text(encoding="utf-8"))
    client = LLMClient(llm_cfg, log)

    n_variants = len(variants)
    log.info(f"开始生成 {n_variants} 个变体的标题计划...")

    # ── 为每个章节生成 N 个子标题方案 ──
    # 用便宜快速的模型（deepseek_chat 或 glm）
    rewrite_model = "deepseek_chat"
    model_pool = llm_cfg.get("models", {})
    if rewrite_model not in model_pool:
        rewrite_model = next(iter(model_pool), "deepseek")

    # 收集需要改写的章节（跳过 15 附图及附表、01 工程概况等标题固定的章节）
    skip_chapters = {"01", "15"}  # 这些章节的子标题由招标文件硬性规定
    chapter_plans = {}  # ch_num_str -> list of n_variants plans

    for ch in chapters:
        ch_num_str = str(ch["num"]).zfill(2)
        if ch_num_str in skip_chapters:
            # 直接用原始子项
            subitems = all_subitems.get(ch_num_str, [])
            chapter_plans[ch_num_str] = [
                {"subitems": subitems} for _ in range(n_variants)
            ]
            continue

        subitems = all_subitems.get(ch_num_str, [])
        if not subitems:
            chapter_plans[ch_num_str] = [
                {"subitems": []} for _ in range(n_variants)
            ]
            continue

        log.info(f"  章 {ch_num_str} {ch['title'][:20]}.. 生成 {n_variants} 个子标题方案")
        plans = _rewrite_subitems_for_chapter(
            client, ch, subitems, n_variants, rewrite_model, log
        )
        chapter_plans[ch_num_str] = plans

    # ── 为每个变体分配方案 + 写入文件 ──
    for vi, variant in enumerate(variants):
        vid = variant["id"]

        # 为该变体的每个章节选一个子标题方案，收集所有章节文本
        chapter_sections = []
        for ch in chapters:
            ch_num_str = str(ch["num"]).zfill(2)
            plans = chapter_plans.get(ch_num_str, [])
            if not plans:
                continue

            # 按 variant index 选方案（确定性）
            plan_idx = vi % len(plans)
            chosen_plan = plans[plan_idx]

            section_text = _format_chapter_section(
                ch, chosen_plan.get("subitems", [])
            )
            chapter_sections.append(section_text)

        # 一次性写入所有章节
        _write_variant_plan(output, vid, chapter_sections, plan_path)

        # 图表分配
        chart_assign = _assign_charts_for_variant(
            variant, base_chart_specs, org_templates, chapters
        )
        assign_path = output / vid / "chart_assignment.json"
        assign_path.write_text(
            json.dumps(chart_assign, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(
            f"  {vid}: org={chart_assign['org_template']['template_id']}, "
            f"mgmt_types={chart_assign['management_chart_types']}"
        )

    log.info(f"完成：{n_variants} 个变体的标题计划和图表分配已生成")


if __name__ == "__main__":
    main()
