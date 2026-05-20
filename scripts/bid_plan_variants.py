#!/usr/bin/env python3
"""bid_plan_variants.py — 为每个变体生成结构上真正不同的章节规划 + 图表分配。

在 variants 步骤之后、write 步骤之前运行。读取共享的 chapter_plan.md(基准),
为每个变体生成一份**结构独立**的章节规划:章节数量、标题、顺序、字数预算、
子项分解都因变体而异 —— 用于多家单位投同一标时的反串标。

反串标设计(中等档):
  - 章节分两层:Tier A 评分锚定章(必保留,可改标题/顺序/预算)、
    Tier B 自由章(可合并/拆分)
  - 每变体按 section_order_seed 做确定性结构变换:合并/拆分自由章 →
    章节数不同;同义池改章节标题;jitter 字数预算;子项数打破固定 offset
  - 生成后 _verify_scoring_coverage 校验每个评分项仍有章节覆盖,缺则报错

产出 V{XX}/:
  1. chapter_plan_variant.md — 该变体完整章节规划(表格 + 子项分配),
     格式与 chapter_plan.md 同构,可被 parse_chapter_plan / parse_plan_subitems 解析
  2. chart_assignment.json — 图表类型分配

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


# ── 章节标题同义池(按基准标题关键词匹配)────────────────────────────
# Tier A 评分章的同义词必须保留主题关键词,确保评委能定位评分项。
_TITLE_SYNONYMS: list[tuple[tuple[str, ...], list[str]]] = [
    (("编制说明", "编制依据"),
     ["编制说明及编制依据", "编制依据与总体说明", "编制总说明"]),
    (("工程概况",),
     ["工程概况", "项目概况与工程特点", "工程总体概况"]),
    (("建设条件", "周边环境"),
     ["建设条件与周边环境分析", "项目建设条件分析", "现场条件与环境分析"]),
    (("施工部署",),
     ["施工部署", "施工总体部署", "施工组织总体部署"]),
    (("施工方案", "技术措施"),
     ["施工方案与技术措施", "主要施工方案及技术措施", "施工技术方案与措施"]),
    (("进度计划", "工期"),
     ["施工进度计划及保证措施", "施工进度计划与工期保证措施", "工期计划及进度保证措施"]),
    (("质量管理", "质量保证"),
     ["质量管理体系与措施", "工程质量管理体系及保证措施", "质量保证体系与质量措施"]),
    (("安全管理", "安全生产"),
     ["安全管理体系与措施", "安全生产管理体系及措施", "施工安全保证体系与措施"]),
    (("环保管理", "环境保护"),
     ["环保管理体系与措施", "环境保护管理体系及措施", "施工环境保护体系与措施"]),
    (("文明施工", "绿色施工"),
     ["文明施工与绿色施工", "文明施工及绿色施工措施", "绿色文明施工管理措施"]),
    (("劳动力", "机械设备"),
     ["劳动力及机械设备投入计划", "人力与机械设备投入计划", "劳动力、机械设备配置计划"]),
    (("管理措施", "综合管理"),
     ["主要管理措施", "项目综合管理措施", "主要施工管理措施"]),
    (("季节性", "季节施工"),
     ["季节性施工措施", "季节性施工保证措施", "不利季节施工措施"]),
    (("高原", "高海拔"),
     ["高原施工专项方案", "高海拔施工专项措施", "高原特殊环境施工方案"]),
    (("保修", "回访"),
     ["工程竣工后的保修措施", "竣工保修及售后服务措施", "工程质量保修与回访措施"]),
    (("附图", "附表"),
     ["附图及附表", "施工图表及附表", "附图、附表汇编"]),
]

# ── Tier B 自由章合并规则:(可被并入章关键词, 优先并入目标关键词)────
_MERGE_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("文明施工", "绿色施工"), ("施工部署", "施工方案", "施工组织")),
    (("季节性", "季节施工", "不利季节"), ("高原", "专项", "特殊环境")),
    (("主要管理措施", "综合管理", "管理措施"), ("施工部署", "项目管理")),
]

# ── 自由章拆分规则:可拆章关键词 → 拆出标题列表 ──────────────────────
_SPLIT_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (("工程概况",), ["工程概况", "建设条件与周边环境分析"]),
    (("施工部署",), ["施工部署", "施工资源组织与协调"]),
]

# 每变体目标章节数偏移(相对基准),保证 V01/V02/V03... 章节数确有差异
_CHAPTER_COUNT_SPREAD = [0, -2, 2, -1, 1, -3, 3]


# ── 工具 ──────────────────────────────────────────────────────────

def _is_tier_a(ch: dict) -> bool:
    """评分锚定章:评分项列非空(有 score_item 或 score>0)。"""
    si = (ch.get("score_item") or "").strip()
    return bool(si and si not in ("—", "-", "无")) or ch.get("score", 0) > 0


def _slug(filename: str) -> str:
    """从基准文件名取拼音 slug:'04_shigongfangan.md' → 'shigongfangan'。"""
    stem = re.sub(r"\.md$", "", filename or "")
    stem = re.sub(r"^\d+_", "", stem)
    return stem or "zhang"


def _pick_title(base_title: str, variant_index: int) -> str:
    """从同义池为章节挑标题。

    按 variant_index 旋转取词,保证同一章在 N 个变体里拿到**不同**同义词
    (同义池 ≥ 变体数时无碰撞);offset 由章节标题稳定派生,使不同章节的
    旋转相位错开。无匹配池则原样返回。
    """
    for keys, pool in _TITLE_SYNONYMS:
        if any(k in base_title for k in keys):
            offset = sum(ord(ch) for ch in base_title)
            return pool[(offset + variant_index) % len(pool)]
    return base_title


def _est_pages(target_chars: int, chart_count: int) -> float:
    """字数 + 图表估算页数(约 500 字/页,每图 0.4 页)。"""
    return round(target_chars / 500.0 + chart_count * 0.4, 1)


# ── 结构变换 ──────────────────────────────────────────────────────

def _new_chapter(base: dict) -> dict:
    """从基准章节克隆一个可变换的工作副本,带 _scope(子项范围参考)。"""
    return {
        "title": base["title"],
        "score_item": base.get("score_item", ""),
        "score": base.get("score", 0),
        "target_chars": int(base.get("target_chars", 2000)),
        "chart_count": int(base.get("chart_count", 0)),
        "subitem_count": int(base.get("subitem_count", 0)) or len(base.get("_subitems", [])),
        "slug": _slug(base.get("filename", "")),
        "tier": "A" if _is_tier_a(base) else "B",
        "_scope": [s["title"] for s in base.get("_subitems", [])],
    }


def _apply_merges(chapters: list[dict], need: int, rng: random.Random,
                  log: logging.Logger) -> list[dict]:
    """把 Tier B 章合并进相关章,减少 need 个章节。"""
    done = 0
    for src_keys, tgt_keys in _MERGE_RULES:
        if done >= need:
            break
        src_idx = next(
            (i for i, c in enumerate(chapters)
             if c["tier"] == "B" and any(k in c["title"] for k in src_keys)),
            None,
        )
        if src_idx is None:
            continue
        tgt_idx = next(
            (i for i, c in enumerate(chapters)
             if i != src_idx and any(k in c["title"] for k in tgt_keys)),
            None,
        )
        if tgt_idx is None:
            continue
        src = chapters[src_idx]
        tgt = chapters[tgt_idx]
        tgt["target_chars"] += src["target_chars"]
        tgt["chart_count"] += src["chart_count"]
        tgt["_scope"] = tgt["_scope"] + src["_scope"]
        tgt["subitem_count"] = max(tgt["subitem_count"], 3) + 2
        log.info(f"    合并:「{src['title']}」并入「{tgt['title']}」")
        chapters.pop(src_idx)
        done += 1
    if done < need:
        log.info(f"    可用合并规则不足,实际合并 {done}/{need}")
    return chapters


def _apply_splits(chapters: list[dict], need: int, work_labels: list[str],
                  rng: random.Random, log: logging.Logger) -> list[dict]:
    """拆分章节,增加 need 个章节。优先拆自由章,其次按专业拆施工方案。"""
    done = 0
    # 1. Tier B 拆分规则
    for split_keys, new_titles in _SPLIT_RULES:
        if done >= need:
            break
        idx = next(
            (i for i, c in enumerate(chapters)
             if c["tier"] == "B" and any(k in c["title"] for k in split_keys)),
            None,
        )
        if idx is None:
            continue
        chapters[idx:idx + 1] = _split_one(chapters[idx], new_titles)
        log.info(f"    拆分:「{new_titles[0]}」拆出「{new_titles[1]}」")
        done += 1
    # 2. 施工方案按专业拆(Tier A,所有拆出章保留 score_item)
    if done < need and len(work_labels) >= 3:
        idx = next(
            (i for i, c in enumerate(chapters)
             if c["tier"] == "A" and "施工方案" in c["title"]
             and c["subitem_count"] >= 6),
            None,
        )
        if idx is not None:
            profs = _profession_titles(work_labels)
            if len(profs) >= 2:
                chapters[idx:idx + 1] = _split_one(
                    chapters[idx], profs, keep_score=True, keep_title=True)
                log.info(f"    拆分:施工方案按专业拆为 {profs}")
                done += len(profs) - 1
    if done < need:
        log.info(f"    可用拆分规则不足,实际拆分 {done}/{need}")
    return chapters


def _profession_titles(work_labels: list[str]) -> list[str]:
    """从作业队标签生成 2~3 个专业施工方案章标题。"""
    cleaned = []
    seen = set()
    for lbl in work_labels:
        name = re.sub(r"(施工队|作业队|工区)$", "", lbl).split("-")[0].strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    titles = [f"{n}施工方案与技术措施" for n in cleaned[:3]]
    return titles if len(titles) >= 2 else []


def _split_one(ch: dict, new_titles: list[str], keep_score: bool = False,
               keep_title: bool = False) -> list[dict]:
    """把一个章节拆成 len(new_titles) 个,平分预算/图表/子项范围。

    keep_title=True 时拆出章标题不再走同义改写(用于已含专业前缀的标题)。
    """
    n = len(new_titles)
    scope = ch["_scope"] or [ch["title"]]
    out = []
    for i, title in enumerate(new_titles):
        seg = scope[i::n] or [title]
        sub_n = max(3, ch["subitem_count"] // n + (1 if i == 0 else 0))
        out.append({
            "title": title,
            "score_item": ch["score_item"] if (keep_score or i == 0) else "",
            "score": ch["score"] if (keep_score or i == 0) else 0,
            "target_chars": ch["target_chars"] // n,
            "chart_count": ch["chart_count"] // n + (1 if i == 0 and ch["chart_count"] % n else 0),
            "subitem_count": sub_n,
            "slug": ch["slug"] + ("" if i == 0 else chr(ord("a") + i)),
            "tier": ch["tier"],
            "_scope": seg,
            "_keep_title": keep_title,
        })
    return out


def _reorder_tail(chapters: list[dict], rng: random.Random) -> list[dict]:
    """对中后段自由章做温和重排(编制说明保持首、附图保持尾)。"""
    tail_kw = ("文明施工", "绿色施工", "季节性", "高原", "高海拔", "管理措施", "综合管理")
    movable = [i for i, c in enumerate(chapters)
               if c["tier"] == "B" and any(k in c["title"] for k in tail_kw)]
    if len(movable) < 2:
        return chapters
    picked = [chapters[i] for i in movable]
    rng.shuffle(picked)
    for slot, ch in zip(movable, picked):
        chapters[slot] = ch
    return chapters


def _rebudget(chapters: list[dict], scale: float, rng: random.Random) -> None:
    """按变体缩放因子 + 逐章 jitter 重设字数预算。"""
    for c in chapters:
        jitter = 1.0 + rng.uniform(-0.08, 0.08)
        c["target_chars"] = max(1500, int(c["target_chars"] * scale * jitter))
        c["est_pages"] = _est_pages(c["target_chars"], c["chart_count"])


def _seeded_subitem_count(base: int, rng: random.Random) -> int:
    """打破固定 ±1 offset:在 [base-2, base+2] 内独立随机。"""
    base = base or 4
    return max(3, rng.randint(base - 2, base + 2))


def build_variant_structure(
    base_chapters: list[dict], variant: dict, variant_index: int, delta: int,
    target_scale: float, work_labels: list[str], log: logging.Logger,
) -> list[dict]:
    """为一个变体生成结构独立的章节列表(未含子项明细)。"""
    seed = variant.get("section_order_seed") or hash(variant.get("id", "")) & 0xFFFFFFFF
    rng = random.Random(seed)

    chapters = [_new_chapter(c) for c in base_chapters]

    # 1. 合并 / 拆分 → 章节数变化
    if delta < 0:
        chapters = _apply_merges(chapters, -delta, rng, log)
    elif delta > 0:
        chapters = _apply_splits(chapters, delta, work_labels, rng, log)

    # 2. 章节标题同义改写(按 variant_index 旋转,N 变体取不同词;
    #    专业拆分章已含专业前缀,_keep_title 跳过)
    for c in chapters:
        if not c.get("_keep_title"):
            c["title"] = _pick_title(c["title"], variant_index)

    # 3. 中后段自由章温和重排
    chapters = _reorder_tail(chapters, rng)

    # 4. 字数预算缩放 + jitter
    _rebudget(chapters, target_scale, rng)

    # 5. 子项数打破固定 offset
    for c in chapters:
        c["subitem_count"] = _seeded_subitem_count(c["subitem_count"], rng)

    # 6. 重新编号 + 生成文件名
    for i, c in enumerate(chapters, 1):
        c["num"] = f"{i:02d}"
        c["filename"] = f"{i:02d}_{c['slug']}.md"

    return chapters


def verify_scoring_coverage(
    variant_chapters: list[dict], base_score_items: set[str], log: logging.Logger,
) -> None:
    """校验每个评分项仍有章节覆盖,缺失即抛错(防变体结构变换丢分)。"""
    covered = {
        (c.get("score_item") or "").strip()
        for c in variant_chapters
        if (c.get("score_item") or "").strip() not in ("", "—", "-")
    }
    missing = base_score_items - covered
    if missing:
        raise RuntimeError(
            f"变体结构变换后评分项失去章节覆盖: {sorted(missing)} —— "
            f"合并/拆分规则有误,请检查"
        )
    log.info(f"    评分覆盖校验通过:{len(base_score_items)} 个评分项均有章节")


# ── LLM 子项分解 ─────────────────────────────────────────────────

_DECOMPOSE_PROMPT = """\
你是施工组织设计编写专家。下面是某投标文件「{vid}」的章节列表,请为**每一章**
按指定子项数把该章主题分解成有逻辑层次的二级子标题,并分配目标字数。

要求:
1. 子项数量严格按每章指定的"需 N 项"
2. 子标题按本章主题语义切分,不得用"编制依据/实施方案/保障措施/应急响应"这类
   空泛的万能兜底;要结合该章实际内容
3. 各章子项目标字数之和 = 该章"目标字数"(每项可 ±50 字浮动)
4. 子标题简洁专业,不带序号前缀

章节列表:
{chapters_brief}

严格输出 JSON:
{{"chapters": [{{"num": "01", "subitems": [{{"title": "...", "chars": N}}, ...]}}, ...]}}"""


def decompose_variant(
    client: LLMClient, model: str, variant_id: str,
    chapters: list[dict], log: logging.Logger,
) -> dict[str, list[dict]]:
    """调 LLM 为一个变体的所有章节生成子项分解。返回 {num: [{title,chars}]}。"""
    briefs = []
    for c in chapters:
        scope = "、".join(c["_scope"][:8]) if c["_scope"] else c["title"]
        briefs.append(
            f"- 第{c['num']}章「{c['title']}」需 {c['subitem_count']} 项,"
            f"目标字数 {c['target_chars']},覆盖范围:{scope}"
        )
    prompt = _DECOMPOSE_PROMPT.format(
        vid=variant_id, chapters_brief="\n".join(briefs),
    )
    try:
        resp = client.chat(
            model_key=model,
            system="你是资深施工组织设计编写专家,擅长按章主题做差异化的子项切分。",
            user=prompt,
            temperature=0.8,
        )
        m = re.search(r"\{[\s\S]*\}", resp.text)
        if not m:
            raise ValueError("LLM 返回无 JSON")
        data = json.loads(m.group())
        result: dict[str, list[dict]] = {}
        for ch in data.get("chapters", []):
            num = str(ch.get("num", "")).zfill(2)
            subs = [
                {"title": re.sub(r"^\d+(?:\.\d+)*\s*", "", str(s.get("title", ""))).strip(),
                 "chars": int(s.get("chars", 0) or 0)}
                for s in ch.get("subitems", []) if s.get("title")
            ]
            if subs:
                result[num] = subs
        return result
    except Exception as e:
        log.warning(f"  变体 {variant_id} 子项 LLM 分解失败({e}),用均分兜底")
        return {}


def _fallback_subitems(ch: dict) -> list[dict]:
    """LLM 失败时的兜底:按 _scope 均分,无 scope 时按章标题切。"""
    n = ch["subitem_count"]
    scope = ch["_scope"] or [ch["title"]]
    titles = [scope[i % len(scope)] for i in range(n)]
    per = max(200, ch["target_chars"] // n)
    return [{"title": t, "chars": per} for t in titles]


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

def render_variant_plan(variant_id: str, chapters: list[dict]) -> str:
    """渲染该变体完整章节规划(表格 + 子项分配),与 chapter_plan.md 同构。"""
    lines = [
        f"# 变体 {variant_id} 章节规划",
        "",
        "> 由 bid_plan_variants.py 从 chapter_plan.md 派生,本变体结构独立"
        "(章节数/标题/顺序/字数/子项均为本变体专属)。",
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
        sub_n = len(subs) if subs else c["subitem_count"]
        ep = c.get("est_pages", _est_pages(tc, c["chart_count"]))
        total_chars += tc
        total_pages += ep
        lines.append(
            f"| {c['num']} | {c['filename']} | {c['title']} | {si} | {score_s} | "
            f"{sub_n} | {tc} | {lo}-{hi} | {c['chart_count']} | {ep} |"
        )
    lines += [
        f"| **合计** | | | | | | **{total_chars}** | | | **{round(total_pages,1)}** |",
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


# ── 图表分配(沿用原逻辑,喂入变体专属章节)──────────────────────────

def _assign_charts_for_variant(
    variant: dict, base_chart_specs, org_templates: dict,
    chapters: list[dict], work_labels: list[str] | None = None,
) -> dict:
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
    p = argparse.ArgumentParser(description="为每个变体生成结构独立的章节规划")
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--target-pages", type=float, default=None,
                   help="目标总页数(覆盖 chapter_plan 基准),变体在此上下浮动")
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
    base_total_chars = sum(int(c.get("target_chars", 0)) for c in base_chapters)

    # chart_specs / org_templates / work_labels
    base_chart_specs: dict[str, list] = {}
    chart_specs_path = extracted / "chart_specs.json"
    if chart_specs_path.exists():
        raw = json.loads(chart_specs_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("specs", [])
        for spec in raw:
            if isinstance(spec, dict):
                base_chart_specs.setdefault(str(spec.get("chapter", "")).zfill(2), []).append(spec)
    org_templates = {"templates": {}, "management_chart_types": {"combinations": []}}
    org_path = ROOT / "configs" / "org_templates.json"
    if org_path.exists():
        org_templates = json.loads(org_path.read_text(encoding="utf-8"))
    work_labels = _load_work_labels(extracted)
    if work_labels:
        log.info(f"清单驱动作业队标签: {work_labels}")

    # 目标总页数 → 缩放因子
    base_pages = sum(c.get("est_pages", 0) for c in base_chapters) or 1
    user_scale = 1.0
    if args.target_pages:
        user_scale = args.target_pages / base_pages
        log.info(f"用户指定目标页数 {args.target_pages}(基准 {base_pages:.0f}),全局缩放 ×{user_scale:.2f}")

    client = LLMClient.load(log=log)
    rewrite_model = "deepseek_chat"
    if rewrite_model not in (client.config.get("models", {})):
        rewrite_model = client.config.get("default_model", "deepseek")

    log.info(f"开始为 {n_variants} 个变体生成结构独立的章节规划...")

    # 1. 每变体结构变换
    variant_structures: list[tuple[dict, list[dict]]] = []
    for i, variant in enumerate(variants):
        delta = _CHAPTER_COUNT_SPREAD[i % len(_CHAPTER_COUNT_SPREAD)]
        # 变体总量缩放:V01≈基准,其余 ±5%
        v_scale = user_scale * (1.0 + [0.0, -0.05, 0.05, -0.03, 0.03][i % 5])
        log.info(f"  {variant['id']}: 目标章节数偏移 {delta:+d}, 总量 ×{v_scale:.2f}")
        chapters = build_variant_structure(
            base_chapters, variant, i, delta, v_scale, work_labels, log
        )
        verify_scoring_coverage(chapters, base_score_items, log)
        log.info(f"    → {len(chapters)} 章")
        variant_structures.append((variant, chapters))

    # 2. 并行 LLM 子项分解(每变体一次调用)
    def _decompose(item):
        variant, chapters = item
        return variant["id"], decompose_variant(
            client, rewrite_model, variant["id"], chapters, log
        )

    decomposed: dict[str, dict[str, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, n_variants)) as ex:
        for vid, result in ex.map(_decompose, variant_structures):
            decomposed[vid] = result

    # 3. 落子项 + 渲染 + 图表分配
    for variant, chapters in variant_structures:
        vid = variant["id"]
        sub_map = decomposed.get(vid, {})
        for c in chapters:
            subs = sub_map.get(c["num"]) or _fallback_subitems(c)
            # 子项数对齐声明值
            want = c["subitem_count"]
            if len(subs) > want:
                subs = subs[:want]
            elif len(subs) < want:
                subs += _fallback_subitems({**c, "subitem_count": want - len(subs)})
            c["subitems"] = _normalize_subitems(subs, c["target_chars"])

        v_dir = output / vid
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "chapter_plan_variant.md").write_text(
            render_variant_plan(vid, chapters), encoding="utf-8"
        )
        chart_assign = _assign_charts_for_variant(
            variant, base_chart_specs, org_templates, chapters, work_labels
        )
        (v_dir / "chart_assignment.json").write_text(
            json.dumps(chart_assign, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total_p = round(sum(c.get("est_pages", 0) for c in chapters), 1)
        log.info(f"  {vid}: {len(chapters)} 章 / 约 {total_p} 页 已写入")

    log.info(f"完成:{n_variants} 个变体的结构独立章节规划已生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
