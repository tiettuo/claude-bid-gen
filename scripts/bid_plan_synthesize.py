#!/usr/bin/env python3
"""
bid_plan_synthesize.py - 合成 chapter_plan.md

读 facts/evaluation/outline/disqualification + 项目规模 + 字数密度查表,
生成三圈 chapter_plan.md:
  第一圈:总量预算(规模判档 + 目标页数 + 字数推算)
  第二圈:大章节分配(合格制按大纲 / 评分制按评分表)
  第三圈:子项分配(预设映射表 + 兜底)

合格制 vs 评分制:
  passfail → 章节锚点 = mandatory_outline 主题(1:1 字面)
  score    → 章节锚点 = evaluation_method.scoring_items 标题

用法:
  python bid_plan_synthesize.py /path/to/tender/
  python bid_plan_synthesize.py /path/to/tender/ --target-pages 50
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402
from bid_derive import recommend_target_chars, size_class  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"

# ─── 项目规模 → 目标页数推荐表(4 维矩阵的简化版)─────────────────────────

SIZE_TO_PAGES = {
    "small":         {"min": 30, "default": 35, "max": 60},   # < 500 万
    "small_medium":  {"min": 50, "default": 70, "max": 90},   # 500 万 - 3000 万
    "medium":        {"min": 80, "default": 120, "max": 180}, # 3000 万 - 1 亿
    "large":         {"min": 150, "default": 220, "max": 290},# 1 亿 - 5 亿
    "extra_large":   {"min": 200, "default": 260, "max": 290},# >= 5 亿
    "unknown":       {"min": 50, "default": 70, "max": 100},
}


# ─── 标准 11 主题 → 子项预设表(合格制常用)─────────────────────────────

STANDARD_SUBTOPICS: dict[str, list[str]] = {
    "项目人员配备": [
        "项目经理(资格证 + 业绩 + 任职承诺)",
        "技术负责人(资格证 + 中级及以上职称 + 社保)",
        "其他主要管理人员(施工员、质量员、安全员、材料员、资料员)",
        "授权代理人及组织机构框图",
    ],
    "工期安排": [
        "工期目标与编制依据",
        "施工阶段划分",
        "各分项进度安排与关键线路",
        "进度计划横道图说明",
    ],
    "施工技术方案": [
        "编制依据与施工总体部署",
        "拆除/前期作业部分施工方案",
        "主体结构部分施工方案",
        "围护与装饰部分施工方案",
        "电气安装施工方案",
        "弱电/智能化安装施工方案",
    ],
    "根据现场实际情况拟定的施工措施": [
        "场地条件应对措施",
        "场地狭小/拆改一体推进措施",
        "与采购人逐道工序签证制衔接措施",
        "单位性质特殊的针对性应对",
    ],
    "工期保证措施": [
        "组织保证措施",
        "技术保证措施",
        "资源保证措施",
        "合同与奖罚保证措施",
    ],
    "资源配备计划": [
        "劳动力配置计划",
        "主要材料供应计划",
        "资金与周转保证",
    ],
    "施工设备配置情况": [
        "主要施工机具配置",
        "测量检测与安全设备配置",
    ],
    "质量目标及保证措施": [
        "质量目标",
        "质量保证体系",
        "各分项工程质量控制要点",
        "关键部位专项质量措施",
        "成品保护与质量通病防治",
    ],
    "安全生产目标及措施": [
        "安全目标",
        "安全管理体系",
        "危险源辨识与控制",
        "专项安全措施",
        "应急预案",
    ],
    "环境保护及文明施工要求": [
        "环保目标与体系",
        "扬尘/噪声/污水/固废分项措施",
        "文明施工与建渣外运",
    ],
    "重难点分析": [
        "重点 1",
        "重点 2",
        "难点 1",
        "难点 2",
        "难点 3",
    ],
}


# ─── 工程类型识别(用于图表占比估算)──────────────────────────────────

def detect_project_type(facts: dict, outline_topics: list[dict]) -> str:
    """从 facts + outline 关键词识别工程类型。"""
    scope = ""
    name = ""
    p = facts.get("project_scope")
    if isinstance(p, dict):
        scope = p.get("value") or ""
    n = facts.get("project_name")
    if isinstance(n, dict):
        name = n.get("value") or ""
    combo = (scope + " " + name).lower()

    if any(k in combo for k in ["屋顶", "屋面", "改造", "装饰", "装修"]):
        return "renovation"  # 改造装修类:图表中等
    if any(k in combo for k in ["道路", "市政", "管网", "排水", "管廊"]):
        return "municipal"   # 市政类:图表较多
    if any(k in combo for k in ["水利", "水库", "灌区", "渠道"]):
        return "water"       # 水利类:图表多
    if any(k in combo for k in ["园林", "绿化", "景观"]):
        return "landscape"   # 园林类:图表多
    if any(k in combo for k in ["住宅", "办公", "学校", "医院", "厂房", "仓储", "民房"]):
        return "building"    # 房建类:图表多
    return "general"


PROJECT_TYPE_FIG_RATIO = {
    "renovation": 0.25,
    "municipal": 0.30,
    "water": 0.30,
    "landscape": 0.30,
    "building": 0.28,
    "general": 0.25,
}


# ─── 重量分配(合格制按内容权重)──────────────────────────────────────

DEFAULT_WEIGHTS = {
    "项目人员配备": 7,
    "工期安排": 9,
    "施工技术方案": 22,
    "根据现场实际情况拟定的施工措施": 9,
    "工期保证措施": 9,
    "资源配备计划": 8,
    "施工设备配置情况": 6,
    "质量目标及保证措施": 10,
    "安全生产目标及措施": 10,
    "环境保护及文明施工要求": 5,
    "重难点分析": 5,
}


def assign_weights(topics: list[str]) -> dict[str, int]:
    """给主题分配权重(合格制下,总和 100)。"""
    weights = {}
    total_known = 0
    unknown = []
    for t in topics:
        w = DEFAULT_WEIGHTS.get(t)
        if w is not None:
            weights[t] = w
            total_known += w
        else:
            unknown.append(t)

    # 未识别主题平均分配剩余权重
    if unknown:
        remain = max(100 - total_known, len(unknown) * 5)
        per = remain // len(unknown)
        for t in unknown:
            weights[t] = per

    # 缩放到 100
    s = sum(weights.values())
    if s != 100 and s > 0:
        factor = 100.0 / s
        weights = {k: max(1, int(round(v * factor))) for k, v in weights.items()}
        # 修正小数误差
        diff = 100 - sum(weights.values())
        if diff != 0:
            biggest = max(weights, key=weights.get)
            weights[biggest] += diff

    return weights


def get_subtopics(topic: str) -> list[str]:
    """取主题对应的子项(无映射 → 4 个通用兜底)。

    [DEPRECATED 自 Phase 6:llm_plan_subitems 是主入口,本函数仅作 LLM 兜底]
    """
    if topic in STANDARD_SUBTOPICS:
        return STANDARD_SUBTOPICS[topic]
    return ["编制依据与目标", "实施方案", "保障措施", "应急响应"]


def llm_plan_subitems(
    topic: str,
    subsections_original: list[str],
    target_chars: int,
    chapter_num: int,
    project_brief: str,
    log,
) -> list[dict]:
    """LLM 思考一次,把 outline.subsections_original 合并/拆分成 4-8 个合理子项。

    返回 [{"title": "N.M 子项标题", "chars": int, "covers": [原文子节, ...]}, ...]
    """
    n_orig = len(subsections_original or [])

    def _fallback() -> list[dict]:
        f = get_subtopics(topic)
        each = target_chars // len(f)
        remainder = target_chars - each * len(f)
        return [{
            "title": f"{chapter_num}.{i+1} {t}",
            "chars": each + (remainder if i == len(f)-1 else 0),
            "covers": [],
        } for i, t in enumerate(f)]

    # 子节数刚好 4-8:直接 1:1 用原文(省 LLM token)
    if 4 <= n_orig <= 8:
        each = target_chars // n_orig
        remainder = target_chars - each * n_orig
        out = []
        for i, sec in enumerate(subsections_original, 1):
            chars = each + (remainder if i == n_orig else 0)
            out.append({
                "title": f"{chapter_num}.{i} {sec}",
                "chars": chars,
                "covers": [sec],
            })
        log.info(f"  章 {chapter_num} 子项:{n_orig} 子节 1:1 用原文(无需 LLM)")
        return out

    # 无子节 → LLM 按主题语义设计 4-6 个子节(替代兜底"编制依据/实施方案/保障措施/应急响应"泛化项)
    # 1-3 子节 → LLM 补全到 4 子节(基于原文 + 主题语义)
    # ≥9 子节 → LLM 合并到 4-7 项
    if n_orig == 0:
        descriptor = "为本章节设计 4-6 个 H2 子节(紧扣章节主题与项目实际,不要用'编制依据/实施方案/保障措施/应急响应'这种泛化通用名)"
        secs_text = "  (大纲未列子节,请按章节主题语义+项目实际设计)"
    elif n_orig >= 9:
        descriptor = "把相关子节合并为 4-7 项"
        secs_text = "\n".join(f"  - {s}" for s in subsections_original)
    else:
        descriptor = "保留原文子节并按主题语义补全到 4 子节,字数均衡"
        secs_text = "\n".join(f"  - {s}" for s in subsections_original)

    system = (
        "你是投标文件章节规划助手。给定一章主题 + 招标第七章规定的子节原文 + 章节目标字数,"
        "你的任务是输出最终的 H2 子节切分方案。"
    )
    user = f"""章节主题: {topic}
章节序号: {chapter_num}
本章目标字数: {target_chars} 字
项目简介: {project_brief}

招标第七章规定的子节原文({n_orig} 项):
{secs_text}

请{descriptor},输出 4-8 个 H2 子项。规则:
{"1. (本章大纲无子节,无需 covers 字段约束;按主题语义自由设计)" if n_orig == 0 else '1. 必须 100% 覆盖原文所有子节("covers" 字段标明覆盖哪些原文子节)'}
2. 同类/相关的原文子节可合并为一个子项(如多个污染控制 → "污染防治体系")
3. 每子项字数 ≥ 300 字
4. 子项字数之和 = {target_chars}
5. title 字段写"{chapter_num}.M 子项标题"格式(M 从 1 递增)
6. **严禁用 "编制依据/实施方案/保障措施/应急响应" 这种 4 项泛化通用模板**(评委一看就知道是 AI 兜底);必须按本章主题语义+项目实际(高海拔/跨冬/4 子工程)设计具体子项
7. 子项标题要专业、具体、紧扣章节主题(参考示例:章 04「项目组织管理机构」可拆为"组织架构/岗位职责/人员配备/协调机制";章 05「施工总进度计划」可拆为"工期安排/关键线路/进度网络/进度保证措施")

只输出 JSON 数组,无 markdown 标记:
[
  {{"title": "{chapter_num}.1 子项名", "chars": 350, "covers": ["原文A", "原文B"]}},
  ...
]
"""

    # 调 LLM,最多 2 次重试(JSON 解析失败时再问一遍)
    items = None
    raw_last = ""
    for attempt in (1, 2):
        try:
            client = LLMClient.load(log=log)
            user_attempt = user if attempt == 1 else (
                user + "\n\n⚠ 上次输出的 JSON 解析失败。请严格只输出 JSON 数组本身,首字符必须是 `[`,末字符必须是 `]`,不要任何说明文字、代码围栏或思考过程。"
            )
            resp = client.chat(
                model_key="deepseek", system=system, user=user_attempt,
                temperature=0.1, max_tokens=4000,
            )
        except Exception as e:
            log.warning(f"  章 {chapter_num} LLM 调用失败 (attempt {attempt}): {e}")
            continue

        raw = (resp.text or "").strip()
        raw_last = raw
        # 剥 ```json 围栏
        if raw.startswith("```"):
            ls = raw.split("\n")
            if ls[0].startswith("```"):
                ls = ls[1:]
            if ls and ls[-1].strip().startswith("```"):
                ls = ls[:-1]
            raw = "\n".join(ls).strip()
        # 尝试找数组起始 [
        bracket_start = raw.find("[")
        bracket_end = raw.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            raw = raw[bracket_start:bracket_end + 1]

        try:
            items = json.loads(raw)
            if not isinstance(items, list) or not items:
                raise ValueError("非数组或为空")
            break  # 成功,跳出 retry
        except Exception as e:
            log.warning(
                f"  章 {chapter_num} LLM JSON 解析失败 (attempt {attempt}) {e}; "
                f"raw 首 200 字: {raw_last[:200]!r}"
            )
            items = None

    if items is None:
        log.warning(f"  章 {chapter_num} 2 次 retry 均失败,回退兜底")
        return _fallback()

    # 字数归一到 target_chars
    total_llm = sum(int(it.get("chars", 0)) for it in items)
    if total_llm > 0 and abs(total_llm - target_chars) / target_chars > 0.05:
        scale = target_chars / total_llm
        for it in items:
            it["chars"] = max(300, int(int(it.get("chars", 0)) * scale))

    # 覆盖性校验
    covered = set()
    for it in items:
        for c in it.get("covers", []) or []:
            covered.add(c)
    missing = [s for s in subsections_original if s not in covered]
    if missing:
        log.warning(f"  章 {chapter_num} LLM 漏覆盖 {len(missing)} 个原文子节: {missing[:3]}{'...' if len(missing) > 3 else ''}")

    log.info(f"  章 {chapter_num} LLM 切分: {n_orig} 原文 → {len(items)} 子项 (合并 {n_orig - len(items)} 项)")
    return items


# ─── 渲染 ──────────────────────────────────────────────────────────────

def render_chapter_plan(
    project_name: str, project_size: str, control_price: float | None,
    duration_days: int | None, project_type: str,
    eval_method: str, total_tech_score: int,
    target_pages: int, target_chars: int, text_pages: int, figure_pages: int,
    chars_per_page_mid: int, max_safe_chars: int,
    topic_chapters: list[dict],
) -> str:
    """组装 chapter_plan.md 全文。"""

    parts = []
    parts.append("# 章节规划表")
    parts.append("")

    # 第一圈:总量预算
    parts.append("## 总量预算(第一圈)")
    parts.append("")
    size_zh = {
        "small": "极小项目", "small_medium": "中小型项目", "medium": "中型项目",
        "large": "大型项目", "extra_large": "特大型项目", "unknown": "未知规模",
    }.get(project_size, project_size)
    parts.append(f"**项目规模判档:** {size_zh}")
    parts.append(f"**判档依据:**")
    parts.append(f"- 合同控制价:{control_price:.0f} 元" if control_price else "- 合同控制价:未明示")
    parts.append(f"- 工期:{duration_days} 日历天" if duration_days else "- 工期:未明示")
    parts.append(f"- 工程类型:{project_type}")
    parts.append(f"- 评标方式:{eval_method}")
    parts.append(f"- 章节数:{len(topic_chapters)}")
    parts.append("")
    parts.append(f"**目标页数:** {target_pages} 页 (文字 {text_pages} 页 + 图表 {figure_pages} 页)")
    parts.append(f"**字数密度:** {chars_per_page_mid} 字/页(中位)")
    parts.append(f"**目标总字数:** {target_chars:,} 字(超页底线 {max_safe_chars:,} 字)")
    parts.append("")

    # 第二圈:大章节
    parts.append("## 大章节分配(第二圈)")
    parts.append("")
    if eval_method == "passfail":
        col_label = "对应主题"
    else:
        col_label = "对应评分项"
    parts.append(f"| 章节号 | 文件名 | 章节标题 | {col_label} | 权重 | 子项数 | 目标字数 | 字数带 ±15% | 图表数 | 页数估 |")
    parts.append("|---|---|---|---|---|---|---|---|---|---|")

    total_chars = 0
    total_subitems = 0
    total_figs = 0
    total_pages_calc = 0.0

    for idx, ch in enumerate(topic_chapters, 1):
        num = f"{idx:02d}"
        title = ch["title"]
        # 文件名安全化
        safe = title.replace("/", "_").replace(" ", "")
        fname = f"{num}_{safe}.md"
        weight = ch["weight"]
        subs = ch["subtopics"]
        chars = ch["target_chars"]
        lo = int(chars * 0.85)
        hi = int(chars * 1.15)
        figs = ch["figures"]
        pages = chars / chars_per_page_mid + figs * 0.4
        total_chars += chars
        total_subitems += len(subs)
        total_figs += figs
        total_pages_calc += pages
        ref = f"合格制 #{idx}" if eval_method == "passfail" else f"第{idx}项"
        parts.append(
            f"| {num} | {fname} | {title} | {ref} | {weight} | {len(subs)} | "
            f"{chars:,} | {lo:,}-{hi:,} | {figs} | {pages:.1f} |"
        )

    parts.append(f"| **合计** | | | | **{sum(c['weight'] for c in topic_chapters)}** | **{total_subitems}** | **{total_chars:,}** | | **{total_figs}** | **{total_pages_calc:.1f}** |")
    parts.append("")
    parts.append(f"> 实际约 {total_pages_calc:.0f} 页正文 + 5-6 页封面/目录/附录 ≈ {target_pages} 页总页数。")
    parts.append("")

    # 第三圈:子项(Phase 6:每个子项是 dict {title, chars, covers},title 已带 N.M 序号)
    parts.append("## 子项分配(第三圈)")
    parts.append("")
    for idx, ch in enumerate(topic_chapters, 1):
        num = f"{idx:02d}"
        chars = ch["target_chars"]
        subs = ch["subtopics"]
        parts.append(f"### {num} {ch['title']}({chars:,} 字,{ch['figures']} 图表,{chars/chars_per_page_mid + ch['figures']*0.4:.1f} 页)")
        for s in subs:
            if isinstance(s, dict):
                # Phase 6 新格式 {title: "N.M xxx", chars: int, covers: [...]}
                t = s.get("title", "")
                c = s.get("chars", chars // max(len(subs), 1))
                covers = s.get("covers", []) or []
                if covers:
                    covers_str = "、".join(covers[:3]) + ("..." if len(covers) > 3 else "")
                    parts.append(f"- {t}:{c:,} 字 _(覆盖原文: {covers_str})_")
                else:
                    parts.append(f"- {t}:{c:,} 字")
            else:
                # 兼容旧格式 list[str]
                per = chars // len(subs)
                parts.append(f"- {idx}.{subs.index(s)+1} {s}:{per:,} 字")
        parts.append("")

    parts.append("**核对:**")
    parts.append(f"- 各章权重之和 = {sum(c['weight'] for c in topic_chapters)} ✓")
    parts.append(f"- 字数合计 {total_chars:,} ≤ 超页底线 {max_safe_chars:,} {'✓' if total_chars <= max_safe_chars else '✗'}")
    parts.append(f"- 页数估 {total_pages_calc:.1f} ≤ 目标 × 1.05 ({target_pages*1.05:.1f}) {'✓' if total_pages_calc <= target_pages*1.05 else '✗'}")
    return "\n".join(parts) + "\n"


# ─── 主入口 ─────────────────────────────────────────────────────────────

def _val(facts: dict, key: str, default=None):
    v = facts.get(key)
    if isinstance(v, dict):
        return v.get("value", default)
    return v if v is not None else default


def _read_format_from_rules(rules_md: Path) -> tuple[float, float]:
    """从 disqualification_rules.md 读 density.font_size_pt + line_spacing_value。
    返回 (font_size_pt, line_spacing_value);未找到返回 (12.0, 1.5) 默认。
    """
    if not rules_md.exists():
        return (12.0, 1.5)
    import re as _re
    t = rules_md.read_text(encoding="utf-8")
    fs = 12.0
    ls = 1.5
    # 用 (?<![_a-zA-Z]) 排除 table_font_size_pt / heading_h1_size_pt 等同名子串
    m = _re.search(r"(?<![_a-zA-Z])font_size_pt:\s*(\d+(?:\.\d+)?)", t)
    if m:
        try:
            fs = float(m.group(1))
        except ValueError:
            pass
    m = _re.search(r"(?<![_a-zA-Z])line_spacing_value:\s*(\d+(?:\.\d+)?)", t)
    if m:
        try:
            ls = float(m.group(1))
        except ValueError:
            pass
    return (fs, ls)


def synthesize(tender_dir: Path, target_pages: int | None = None,
               force: bool = False, log=None) -> int:
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    out_path = ext / "chapter_plan.md"
    if out_path.exists() and not force:
        log.info(f"  chapter_plan.md 已存在,需 --force")
        return 1

    facts_doc = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))
    facts = facts_doc.get("facts", {})
    eval_data = json.loads((ext / "evaluation_method.json").read_text(encoding="utf-8"))
    outline_data = json.loads((ext / "mandatory_outline.json").read_text(encoding="utf-8"))

    # 取项目基础信息
    project_name = _val(facts, "project_name", "")
    control_price = _val(facts, "control_price")
    duration_days = _val(facts, "duration_days")
    project_size = size_class(float(control_price) if control_price else None)
    eval_method = eval_data.get("evaluation_method", "passfail")
    total_tech_score = eval_data.get("total_tech_score", 0)

    # 工程类型识别
    outline_topics = outline_data.get("topics", [])
    project_type = detect_project_type(facts, outline_topics)
    fig_ratio = PROJECT_TYPE_FIG_RATIO.get(project_type, 0.25)

    # 目标页数:用户给 / 自动判档
    if target_pages is None:
        target_pages = SIZE_TO_PAGES.get(project_size, SIZE_TO_PAGES["unknown"])["default"]
    log.info(f"  规模={project_size}  类型={project_type}  目标页数={target_pages}")

    # 字数密度(从 rules.md 读字号/行距,无则默认 12pt+1.5)
    # Phase 3:优先 authoring_rules.md(方案规则),回退 disqualification_rules.md
    from bid_common import preferred_rules_path
    font_size_pt, line_spacing = _read_format_from_rules(preferred_rules_path(ext))
    log.info(f"  字号={font_size_pt}pt 行距={line_spacing}倍")
    rec = recommend_target_chars(
        target_pages=target_pages,
        font_size_pt=font_size_pt,
        line_spacing_kind="multiple",
        line_spacing_value=line_spacing,
        figure_ratio=fig_ratio,
    )
    target_chars = rec["target_chars_mid"]
    chars_per_page_mid = rec["density_mid"]
    text_pages = rec["text_pages"]
    figure_pages = rec["figure_pages"]
    max_safe_chars = rec["target_chars_hi"]

    # 决定章节锚点:合格制 → outline,评分制 → scoring_items,都空 → GB/T 50502 通用
    chapters_meta: list[dict] = []
    if eval_method == "passfail" and outline_topics:
        topics = [t.get("title_original", "") for t in outline_topics if t.get("title_original")]
        anchor_source = "大纲"
    elif eval_method == "score" and eval_data.get("scoring_items"):
        topics = None  # 走分值分配
        anchor_source = "评分表"
    else:
        # 兜底:GB/T 50502 通用 11 章(合格制场景)
        topics = list(DEFAULT_WEIGHTS.keys())
        anchor_source = "GB/T 50502 通用(outline+scoring 均空)"
        log.warning(f"  outline_topics 和 scoring_items 都为空,回落 GB/T 50502 通用 11 章")

    # Phase 6: 用 mandatory_outline.subsections_original + LLM 思考切分子项
    # topic → outline subsection list 索引(只在合格制走大纲时有)
    outline_subs_map = {}
    if eval_method == "passfail" and outline_topics:
        for t in outline_topics:
            outline_subs_map[t.get("title_original", "")] = t.get("subsections_original", []) or []

    # 项目简介(给 LLM 上下文,200 字内)
    project_brief = (
        f"{facts.get('project_name', {}).get('value', '') if isinstance(facts.get('project_name'), dict) else facts.get('project_name', '')} "
        f"控制价 {control_price} 元 / 工期 {duration_days} 天 / 类型 {project_type} / 评标 {eval_method}"
    )[:200]

    # 并行规划子项:每章一次 LLM 调用,16 章串行 ~80s → 并行 ~8s
    plan_tasks: list[dict] = []
    if topics is not None:
        weights = assign_weights(topics)
        for ch_idx, t in enumerate(topics, 1):
            w = weights[t]
            ch_target_chars = int(target_chars * w / 100)
            num_match = __import__("re").match(r"(\d+)", t)
            display_num = int(num_match.group(1)) if num_match else ch_idx
            plan_tasks.append({
                "title": t,
                "weight": w,
                "target_chars": ch_target_chars,
                "outline_subs": outline_subs_map.get(t, []),
                "chapter_num": display_num,
            })
    else:
        # C 方案:评分制下仅取 section="技术" 的项生成章节(商务标项不写进施组)
        tech_items = [i for i in eval_data.get("scoring_items", []) if i.get("section") == "技术"]
        if not tech_items:
            tech_items = eval_data.get("scoring_items", [])  # 旧数据无 section 字段时回退全量
            log.warning("  scoring_items 无 section 字段,回退到全量(建议重跑 skeleton 抽取)")
        # 计算技术标实际总分(从过滤后的项加权)
        tech_total = sum(i.get("score", 0) for i in tech_items) or total_tech_score
        for ch_idx, item in enumerate(tech_items, 1):
            title = item.get("title_original", "")
            score = item.get("score", 0)
            ch_target_chars = int(target_chars * score / max(tech_total, 1))
            plan_tasks.append({
                "title": title,
                "weight": score,
                "target_chars": ch_target_chars,
                "outline_subs": outline_subs_map.get(title, []),
                "chapter_num": ch_idx,
            })

    def _run_one(pt: dict) -> dict:
        subitems = llm_plan_subitems(
            topic=pt["title"],
            subsections_original=pt["outline_subs"],
            target_chars=pt["target_chars"],
            chapter_num=pt["chapter_num"],
            project_brief=project_brief,
            log=log,
        )
        return {
            "title": pt["title"],
            "weight": pt["weight"],
            "target_chars": pt["target_chars"],
            "subtopics": subitems,
            "figures": _figure_count(pt["title"]),
        }

    with ThreadPoolExecutor(max_workers=min(8, len(plan_tasks) or 1)) as pool:
        chapters_meta = list(pool.map(_run_one, plan_tasks))

    if not chapters_meta:
        log.error("  生成不出章节锚点(逻辑 bug)")
        return 2

    log.info(f"  章节锚点:{len(chapters_meta)} 章 (锚点来源: {anchor_source})")

    md = render_chapter_plan(
        project_name=project_name,
        project_size=project_size,
        control_price=float(control_price) if control_price else None,
        duration_days=duration_days,
        project_type=project_type,
        eval_method=eval_method,
        total_tech_score=total_tech_score,
        target_pages=target_pages,
        target_chars=target_chars,
        text_pages=text_pages,
        figure_pages=figure_pages,
        chars_per_page_mid=chars_per_page_mid,
        max_safe_chars=max_safe_chars,
        topic_chapters=chapters_meta,
    )

    out_path.write_text(md, encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name} ({len(md):,} 字符)")
    return 0


def _figure_count(topic: str) -> int:
    """根据主题估图表数。"""
    fig_map = {
        "项目人员配备": 1,
        "工期安排": 2,
        "施工技术方案": 3,
        "根据现场实际情况拟定的施工措施": 0,
        "工期保证措施": 0,
        "资源配备计划": 2,
        "施工设备配置情况": 1,
        "质量目标及保证措施": 1,
        "安全生产目标及措施": 1,
        "环境保护及文明施工要求": 0,
        "重难点分析": 0,
    }
    return fig_map.get(topic, 1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--target-pages", type=int, default=None,
                   help="目标页数(覆盖自动判档)")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    log = make_script_logger("bid_plan_synthesize", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_plan_synthesize  目录: {tender_dir}")
    code = synthesize(tender_dir, target_pages=args.target_pages,
                      force=args.force, log=log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
