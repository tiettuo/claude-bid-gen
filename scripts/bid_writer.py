#!/usr/bin/env python3
"""
bid_writer.py - 章节生成工作器

读取 chapter_plan.md + project_brain.md + disqualification_rules.md + variants.json，
为每个 variant 的每个章节构造 prompt，调用对应大模型生成 md 文件，
写入 output/VXX/ 子目录。

用法:
  python bid_writer.py <tender_dir>                    # 生成所有 variant × 所有章节
  python bid_writer.py <tender_dir> --variant V01      # 仅某 variant
  python bid_writer.py <tender_dir> --chapter 01       # 仅某章
  python bid_writer.py <tender_dir> --variant V01 --chapter 01 --force   # 覆盖已生成
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

# 允许脚本直接运行（同目录 import）
sys.path.insert(0, str(Path(__file__).parent))
from bid_llm_client import LLMClient  # noqa: E402
from bid_common import (  # noqa: E402
    make_script_logger,
    parse_chapter_plan as _parse_chapter_plan,
    parse_plan_subitems,
    extract_keywords,
    derive_variant_skeleton,
    render_skeleton_for_prompt,
    _split_notes,
    count_chinese_chars,
)


# ─── 交叉多模型：子项并行 + 主笔润色 ────────────────────────────────
# 润色后字数偏离草稿超过此比例则弃用，回退用未润色草稿
POLISH_LENGTH_DEVIATION_THRESHOLD = 0.05
# 运行时开关，由 main() 根据 --no-polish CLI 参数设置
SKIP_POLISH = False
# 每章内子项并行上限（不超过 per_model_limit × 模型池大小）
SUBITEM_INNER_WORKERS = 4


# ─── 图表规格（chart_specs.json，可选） ────────────────────────────────
# 由 main() 加载,build_user_prompt / build_subitem_user_prompt 通过
# get_chart_specs_for_chapter() 查询本章对应规格,作为 prompt 注入。
_CHART_SPECS_BY_CHAPTER: dict[str, list[dict]] = {}


def load_chart_specs(extracted: Path, log: logging.Logger | None = None) -> int:
    """从 extracted/chart_specs.json 加载,按章节分组。返回 spec 总数。"""
    global _CHART_SPECS_BY_CHAPTER
    _CHART_SPECS_BY_CHAPTER = {}
    p = extracted / "chart_specs.json"
    if not p.exists():
        if log:
            log.info("chart_specs.json 不存在,LLM prompt 不注入精确图表规格(向后兼容)")
        return 0
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if log:
            log.warning(f"chart_specs.json 解析失败: {e}")
        return 0
    for spec in payload.get("specs", []):
        ch = str(spec.get("chapter", "")).zfill(2)
        if ch:
            _CHART_SPECS_BY_CHAPTER.setdefault(ch, []).append(spec)
    total = sum(len(v) for v in _CHART_SPECS_BY_CHAPTER.values())
    if log:
        req = sum(1 for ss in _CHART_SPECS_BY_CHAPTER.values() for s in ss if s.get("required"))
        log.info(f"chart_specs 已加载: {total} 张图(其中 {req} 张招标硬性要求)")
    return total


def get_chart_specs_for_chapter(num: str) -> list[dict]:
    """查某章的 specs。"""
    return _CHART_SPECS_BY_CHAPTER.get(str(num).zfill(2), [])


def build_chart_spec_block(specs: list[dict]) -> str:
    """把本章 chart specs 转成可塞进 prompt 的字符串。

    若 chart_specs.json 为空返回空字符串(prompt 走旧的 7 种 type 总览)。
    支持 _override_type 和 _skip 字段（来自 chart_assignment.json）。
    """
    if not specs:
        return ""
    # 延迟导入避免 bid_charts 加载失败时影响 writer
    try:
        from bid_charts import get_schema_prompt_snippet
    except ImportError:
        return ""
    lines = ["【本章必须输出的 ```json``` 图表块(由 chart_specs.json 派生,严格按规格)】"]
    for i, s in enumerate(specs, 1):
        if s.get("_skip"):
            continue
        flag = "★ 招标硬性要求" if s.get("required") else "建议补充"
        sec = s.get("section") or "末尾"
        ctype = s.get("_override_type") or s.get("type", "")
        hint = s.get("data_hint", "")
        lines.append(f"\n图 {i}({flag}) — 在 `{sec}` 节末附 ```json``` 块,类型 `{ctype}`,内容: {hint}")
        snippet = get_schema_prompt_snippet(ctype)
        if snippet:
            lines.append(snippet)
    lines.append(
        "\n★ 上述每张图都必须输出对应的 ```json``` 代码块,缺图视为本章未达标。"
        "数据要根据本项目实际工程构成填,不要照抄示例。"
    )
    return "\n".join(lines) + "\n"


# ─── authoring_rules.md 解析（Phase 4） ───────────────────────────────
# 由 main() 加载,build_user_prompt / build_subitem_user_prompt 通过
# _AUTHORING_CTX 直接读结构化字段(避免文本截断损耗)。
_AUTHORING_CTX: dict = {}
_DIFFICULTIES_DATA: dict = {}
_BRAIN_TEXT: str = ""


def parse_authoring_yaml(text: str) -> dict:
    """从 authoring_rules.md 抽取所有 ```yaml ... ``` 块并合并为一个 dict。"""
    if not text or yaml is None:
        return {}
    blocks = re.findall(r"```yaml\s*(.*?)```", text, re.DOTALL)
    merged: dict = {}
    for blk in blocks:
        try:
            data = yaml.safe_load(blk)
            if isinstance(data, dict):
                merged.update(data)
        except yaml.YAMLError:
            continue
    return merged


def load_authoring_context(extracted: Path, log: logging.Logger | None = None) -> bool:
    """加载 authoring_rules.md + project_brain.md + difficulties.json 到模块级缓存。

    返回是否进入 4-块新模式(authoring 解析成功且非空)。
    解析失败或文件不存在 → 回退到 legacy build_user_prompt 旧流程。
    """
    global _AUTHORING_CTX, _DIFFICULTIES_DATA, _BRAIN_TEXT

    # brain text(为 Block 1 提供 重难点 / 项目类型 等)
    brain_path = extracted / "project_brain.md"
    _BRAIN_TEXT = brain_path.read_text(encoding="utf-8") if brain_path.exists() else ""

    # difficulties
    diff_path = extracted / "difficulties.json"
    if diff_path.exists():
        try:
            _DIFFICULTIES_DATA = json.loads(diff_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _DIFFICULTIES_DATA = {}
    else:
        _DIFFICULTIES_DATA = {}

    # authoring_rules.md(优先) 或 disqualification_rules.md(回退)
    try:
        from bid_common import preferred_rules_path
        rp = preferred_rules_path(extracted)
    except (ImportError, AttributeError):
        rp = extracted / "disqualification_rules.md"

    if rp.name == "authoring_rules.md" and rp.exists():
        try:
            text = rp.read_text(encoding="utf-8")
            _AUTHORING_CTX = parse_authoring_yaml(text)
            if log:
                log.info(f"authoring_rules.md 已解析: {len(_AUTHORING_CTX)} 段 YAML, 字段={list(_AUTHORING_CTX.keys())}")
            return bool(_AUTHORING_CTX)
        except (OSError, yaml.YAMLError) as e:
            if log:
                log.warning(f"authoring_rules.md 解析失败: {e}, 回退 legacy 模式")

    _AUTHORING_CTX = {}
    return False


# 省级 DB 关键词:从 configs/province_db_codes.json 加载,模块级缓存
_PROVINCE_DB_RULES: list[tuple[str, list[str]]] = []


def _load_province_db_rules() -> list[tuple[str, list[str]]]:
    """加载 configs/province_db_codes.json 并缓存。失败时返回内置兜底(仅西藏)。"""
    global _PROVINCE_DB_RULES
    if _PROVINCE_DB_RULES:
        return _PROVINCE_DB_RULES

    cfg_path = Path(__file__).resolve().parent.parent / "configs" / "province_db_codes.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        rules = []
        for r in data.get("rules", []):
            code = r.get("code", "")
            keys = r.get("keywords", []) or []
            if code and keys:
                rules.append((code, keys))
        if rules:
            _PROVINCE_DB_RULES = rules
            return _PROVINCE_DB_RULES
    except (json.JSONDecodeError, OSError):
        pass

    # 兜底:configs 缺失时仍能识别西藏(本地主要项目地)
    _PROVINCE_DB_RULES = [
        ("DB54", ["西藏", "拉萨", "昌都", "江达", "林芝", "山南",
                  "日喀则", "那曲", "阿里"]),
    ]
    return _PROVINCE_DB_RULES


def _guess_province_db(location: str) -> str:
    """根据地点猜地标省级前缀(用于 Block 2 反编造提示)。

    关键词库见 configs/province_db_codes.json。
    """
    if not location:
        return ""
    for code, keys in _load_province_db_rules():
        if any(k in location for k in keys):
            return code
    return ""


def _find_work_structure_chapter(chapter_anchors: list) -> tuple[str, str] | None:
    """从 chapter_anchors 找"施工方案/技术措施"类的章节,返回 (章号, 标题)。

    招标第七章原文不同,章名各异,常见:"施工方案与技术措施" / "施工方法" / "施工工艺"。
    """
    KEYWORDS = ["施工方案", "技术措施", "施工方法", "施工工艺", "施工技术"]
    for ch in chapter_anchors or []:
        title = ch.get("title", "") or ""
        for kw in KEYWORDS:
            if kw in title:
                n = ch.get("n", "?")
                return (str(n).zfill(2), title)
    return None


def _build_block1_project(
    authoring: dict, brain_text: str, difficulties: dict,
    current_chapter_num: str | None = None,
) -> str:
    """Block 1: 项目认知 — 把项目事实摆清楚。

    current_chapter_num: 当前要写的章号(2 位,如 "07")。用于决定是否对本章强提醒
    "必须 1:1 围绕子工程展开"。其他章只列工程构成清单作为背景。
    """
    project = authoring.get("project", {}) or {}
    work = authoring.get("work_structure", []) or []
    constraints = authoring.get("project_specific_constraints", []) or []
    personnel = authoring.get("key_personnel", {}) or {}
    chapter_anchors = authoring.get("chapter_anchors", []) or []
    project_type = extract_project_type(brain_text)

    重点 = difficulties.get("重点", []) if difficulties else []
    难点 = difficulties.get("难点", []) if difficulties else []

    lines = ["【Block 1：项目认知（写之前先弄清楚这是什么项目）】", "",
             "★ 本条为强约束：正文每节至少出现 1 次本项目独有信息（具体数字/地点/海拔/工期/子工程名）。",
             "禁止写出「任何项目都能用」的套话。如果一段话换个项目名不改也能用→直接删掉重写。",
             ""]

    # 基本信息
    lines.append("项目基本信息：")
    if project.get("name"):
        lines.append(f"  · 项目名称：{project['name']}")
    if project_type and project_type != "各类工程":
        lines.append(f"  · 项目类型：{project_type}")
    if project.get("location"):
        lines.append(f"  · 建设地点：{project['location']}")
    if project.get("control_price_cny"):
        cp = project["control_price_cny"]
        cp_wan = f"{cp/10000:.2f} 万元" if isinstance(cp, (int, float)) else cp
        lines.append(f"  · 招标控制价：{cp} 元（约 {cp_wan}）")
    if project.get("duration_days"):
        lines.append(f"  · 总工期：{project['duration_days']} 日历天")
    if project.get("quality_target"):
        lines.append(f"  · 质量目标：{project['quality_target']}")
    lines.append("")

    # 工程构成
    if work:
        lines.append("工程构成（清单驱动 — 禁止编造无清单子项, 触发 B1.17 雷区）：")
        for w in work:
            sub = w.get("sub_project", "?")
            area = w.get("area_m2", "-")
            nature = w.get("nature", "")
            extra = f"（{nature}）" if nature else ""
            lines.append(f"  · {sub} {area}㎡{extra}")
        # 动态识别"施工方案/技术措施"章节,本章命中则强提醒,否则只列清单
        ws_ch = _find_work_structure_chapter(chapter_anchors)
        if ws_ch:
            ch_num, ch_title = ws_ch
            cur = str(current_chapter_num or "").zfill(2) if current_chapter_num else ""
            if cur and cur == ch_num:
                lines.append(f"⚠ 本章「{ch_title}」必须 1:1 围绕以上子工程展开（B1.17 + B1.14 双雷区）。")
            else:
                lines.append(f"（工程构成将在第 {ch_num} 章「{ch_title}」展开,本章按相关性引用即可）")
        elif current_chapter_num is None:
            lines.append("⚠ 施工方案类章节必须 1:1 围绕以上子工程展开（请按招标第七章相应位置执行）。")
        lines.append("")

    # 项目特定约束
    if constraints:
        lines.append("项目特定约束（相关章节必须响应，否则视为未紧扣项目）：")
        for c in constraints:
            cst = c.get("constraint", "?")
            chs = c.get("chapters_affected", []) or []
            addr = c.get("must_address", []) or []
            chs_str = "/".join(f"第{ch}章" for ch in chs) if chs else "相关章"
            lines.append(f"  · {cst}（影响 {chs_str}）")
            for a in addr:
                lines.append(f"      - {a}")
        lines.append("")

    # 重难点
    def _title_of(item):
        if isinstance(item, dict):
            return item.get("title") or item.get("name") or ""
        return str(item) if item else ""

    if 重点:
        lines.append("技术重点：")
        for r in 重点[:5]:
            t = _title_of(r)
            if t and t != "?":
                lines.append(f"  · {t}")
        lines.append("")
    if 难点:
        lines.append("施工难点：")
        for n in 难点[:5]:
            t = _title_of(n)
            if t and t != "?":
                lines.append(f"  · {t}")
        lines.append("")

    # 关键岗位
    if personnel:
        lines.append("关键岗位人员要求（第 4 章项目组织管理机构必须响应）：")
        for k, v in personnel.items():
            label = {
                "project_manager": "项目经理",
                "project_manager_similar": "项目经理类似业绩",
                "tech_lead": "技术负责人",
                "safety_officer": "安全员",
            }.get(k, k)
            v_str = str(v)[:160]
            lines.append(f"  · {label}：{v_str}")

    return "\n".join(lines).rstrip()


def _build_block2_rules(authoring: dict) -> str:
    """Block 2: rules 硬约束 — 违反 = 废标。"""
    fmt = authoring.get("format", {}) or {}
    blind = authoring.get("blind", {}) or {}
    red_lines = authoring.get("technical_red_lines", []) or []
    project = authoring.get("project", {}) or {}
    location = project.get("location", "") or ""

    lines = ["【Block 2：rules 硬约束（违反任何一条直接废标）】", ""]

    # 致命级
    page_cap = fmt.get("page_hard_cap")
    color_allowed = fmt.get("color_allowed", True)
    fatal_lines = []
    if page_cap:
        fatal_lines.append(f"  ★ 全文页数硬顶：**{page_cap} 页**（致命）。单章字数累加不得让全文超过此页数。")
    if color_allowed is False:
        fatal_lines.append("  ★ 不得彩色：整体文件不得出现彩色内容（致命）。表格底纹/字体/图表线条一律黑色。")
    if fatal_lines:
        lines.append("致命级硬约束：")
        lines.extend(fatal_lines)
        lines.append("")

    # 格式硬约束（writer 写作时需注意，assemble 阶段强制执行）
    fmt_lines = []
    if fmt.get("body_size_pt"):
        fmt_lines.append(f"  · 正文字体：{fmt.get('font_chinese','宋体')} "
                         f"{fmt.get('body_size_name','')}（{fmt.get('body_size_pt')}pt）")
    if fmt.get("line_spacing_value"):
        fmt_lines.append(f"  · 行距：{fmt.get('line_spacing_value')} 倍")
    margins = fmt.get("margin_cm")
    if isinstance(margins, dict):
        fmt_lines.append(f"  · 页边距：上下 {margins.get('top','-')} cm，左右 {margins.get('left','-')} cm")
    if fmt.get("first_line_indent_chars"):
        fmt_lines.append(f"  · 首行缩进：{fmt.get('first_line_indent_chars')} 字符")
    if fmt.get("table_font_size_pt"):
        fmt_lines.append(f"  · 表格内字体：{fmt.get('table_font_size_name','五号')}"
                         f"（{fmt.get('table_font_size_pt')}pt）")
    if fmt.get("line_weight_pt"):
        fmt_lines.append(f"  · 表格/图表线条粗细：{fmt.get('line_weight_pt')} 磅")
    landscape = fmt.get("landscape_pages")
    if landscape:
        fmt_lines.append(f"  · 横向页：{'/'.join(landscape)}（仅这些图横向，其余章节纵向）")
    if fmt_lines:
        lines.append("格式硬约束（assemble 强制执行）：")
        lines.extend(fmt_lines)
        lines.append("")

    # 暗标
    if blind.get("blind_bid"):
        lines.append("★ 暗标全章节硬约束：")
        lines.append("  · 全文禁公司名/曾用名/LOGO/项目业绩/人员真实姓名/获奖证书编号/分公司地址")
        lines.append("  · 章节编号用阿拉伯数字 01/02/…，不用符号框装饰")
        lines.append("")
    auth_constraints = blind.get("authoring_constraints", []) or []
    if auth_constraints:
        lines.append("暗标合规通用约束（不分暗标/非暗标）：")
        for ac in auth_constraints:
            lines.append(f"  · {ac}")
        lines.append("")

    # 技术性废标雷区
    if red_lines:
        lines.append("技术性废标雷区（写作时必须躲开）：")
        for rl in red_lines[:8]:
            rule = (rl.get("rule") or "")[:110].replace("\n", " ")
            cid = rl.get("id", "-")
            lines.append(f"  · [{cid}] {rule}")
        lines.append("")

    # 引用规范反编造
    lines.append("引用规范反编造规则（B1.17 雷区）：")
    lines.append("  · 只引真实存在的现行国家标准（GB/T XXXXX-YYYY）/ 行业标准（JGJ/CJJ/TD/T/SL）")
    db = _guess_province_db(location)
    if db:
        lines.append(f"  · 本项目位于 {location} → 地方标准只用 {db} 系列，禁引用其他省份 DBXX")
    lines.append("  · 不确定的规范号或年份**宁可不写**；版本年份不确定时只写规范号，不臆造\"XX 年版\"")
    lines.append("  · 各章引用的规范应紧扣本章主题，不要堆砌无关规范")

    return "\n".join(lines).rstrip()


def _build_block3_chapter(
    chapter: dict, variant: dict, skeleton: list[dict],
    subitems: list[dict], h1_title: str, band_lo: int, band_hi: int,
    target: int, coverage_line: str,
) -> str:
    """Block 3: 本章任务(整章模式)。

    合格制(passfail)：不预设二级标题，LLM 自由组织章节结构，最大化差异化。
    评分制(score/scoring)：严格对齐评分项，二级标题必须覆盖评分点。
    """
    equip_lines = "\n".join(f"  - {k}: {v:+d} 台" for k, v in variant.get("equip_delta", {}).items())
    skeleton_block = render_skeleton_for_prompt(skeleton)

    # 评标模式路由
    eval_mode = (_AUTHORING_CTX.get("evaluation") or {}).get("evaluation_mode", "passfail")
    is_scoring = eval_mode in ("score", "scoring")

    lines = [
        "【Block 3：本章任务（具体写什么）】",
        "",
        f"章节号：第 {chapter['num']} 章",
        f"章节标题（H1 必须字面一致 `# {h1_title}`）：{h1_title}",
        f"对应评分项：{chapter.get('score_item','-')}  (分值 {chapter.get('score','-')})",
        "",
    ]

    if is_scoring:
        # ── 评分制：严格对齐评分项，二级标题必须覆盖 ──
        if subitems:
            subitem_lines = "\n".join(
                f"  - ## {s['title']}（目标 {s['chars']} 字）" for s in subitems
            )
            lines.extend([
                f"本章必须输出且**只输出** {len(subitems)} 个 H2 二级子节"
                f"(按顺序,标题字面一致含序号不变,不得增减/重排/拆分)：",
                subitem_lines,
                "",
            ])
        else:
            lines.extend([
                "本章必须使用的 H2/H3 骨架(顺序锁定,一字不动,不得新增/删除/重排)：",
                skeleton_block,
                "",
            ])
    else:
        # ── 合格制：不预设二级标题，LLM 自由组织 ──
        lines.extend([
            "请自由组织本章结构。你可以自行决定分几个二级标题 `##`、"
            "是否需要三级标题 `###`、各节篇幅分配等。要求：",
            "  - 内容必须紧扣章节主题，专业且有深度",
            "  - 自然融入下方「评分关键词」中的关键内容",
            "  - 总字数达到目标即可，各小节篇幅自行把握",
            "  - 禁止使用空洞的模板化表述（如\"编制依据/实施方案/保障措施/应急响应\"）",
            "",
        ])
        # 如果有 subitems，仅作为参考方向提示（不强求）
        if subitems:
            ref_lines = "；".join(s['title'] for s in subitems)
            lines.extend([
                f"（参考方向：{ref_lines}。仅作参考，可自由调整）",
                "",
            ])

    lines.extend([
        "评分关键词（必须在正文实质性展开，不得照抄成 H2/H3 标题）：",
        f"  {coverage_line}",
        "",
        f"本章目标字数：{target} 字（±15% 带：{band_lo}–{band_hi} 字）",
        "图表不计入字数；该用表就用表、该用图就用图。",
        "",
        "variants 差异化（必须自然融入正文叙述，严禁回显本段标签名）：",
        f"  · 劳动力配置系数：{variant['labor_factor']}",
        "  · 主要机械调整：",
        equip_lines if equip_lines else "    （无）",
        f"  · 优先引用规范版本：{variant.get('standard_year_preference','-')} 年版（不确定的规范不强加版本）",
    ])

    # 组织架构模板注入（仅 04 章）
    org_template_json = chapter.get("_org_template_json")
    if org_template_json:
        lines.extend([
            "",
            f"★ 组织架构图必须使用以下{chapter.get('_org_template_label', '')}模板结构（严格按此 JSON 结构生成 org_chart）：",
            "```json",
            org_template_json,
            "```",
            "  岗位名称可根据项目实际微调，但层级结构和分支数必须与模板一致。",
        ])
    return "\n".join(lines).rstrip()


def _build_block3_subitem(
    chapter: dict, variant: dict, sub: dict, sibling_titles: list[str],
    band_lo: int, band_hi: int, target: int,
) -> str:
    """Block 3: 本子节任务(子项模式)。"""
    equip_lines = "\n".join(f"  - {k}: {v:+d} 台" for k, v in variant.get("equip_delta", {}).items())
    siblings_block = "\n".join(f"  - {t}" for t in sibling_titles) or "  （无）"

    lines = [
        "【Block 3：本子节任务（你只写一个 ## 子节）】",
        "",
        f"所在章节：第 {chapter['num']} 章 {chapter['title']}"
        f"  (对应评分项：{chapter.get('score_item','-')}，分值 {chapter.get('score','-')})",
        "",
        f"本子节标题（H2 必须字面一致 `## {sub['title']}`）：{sub['title']}",
        f"本子节目标字数：{target} 字（±15% 带：{band_lo}–{band_hi} 字）",
        "图表不计入字数。",
        "",
        "本章其他兄弟子节标题（避免内容挤进本子节）：",
        siblings_block,
        "",
        "内容边界（极重要）：",
        "  · 不写项目概况/章节引言/开场白（由主笔统一补写）",
        "  · 不写章节总结/小结/收尾",
        "  · 直接进入本子节技术实质内容（工艺/参数/措施/规范）",
        "  · 不重复其他兄弟子节内容",
        "",
        "variants 差异化（融入正文，严禁回显标签名）：",
        f"  · 劳动力系数：{variant['labor_factor']}",
        "  · 主要机械调整：",
        equip_lines if equip_lines else "    （无）",
        f"  · 优先引用规范版本：{variant.get('standard_year_preference','-')} 年版（不确定的规范不强加版本）",
    ]
    return "\n".join(lines).rstrip()


def _build_block4_output(band_lo: int, band_hi: int, is_subitem: bool = False) -> str:
    """Block 4: 输出格式约束（怎么写代码块/表格/装饰符）。

    真值源：bid_common.OUTPUT_FORMAT_RULES + DECO_STRIP_CHARS + DECO_WARN_CHARS
        + bid_charts._RENDERER_SCHEMAS。本函数不硬编码上述清单,要改请改源。
    """
    from bid_common import render_output_format_constraints_lines

    header = ["【Block 4：输出格式约束（违反则下游脚本会失败或扣分）】", ""]
    body: list[str] = [f"字数严格落在 {band_lo}–{band_hi} 字带内（图表不折算字数）"]
    # 真值源派生
    body.extend(render_output_format_constraints_lines(is_subitem=is_subitem))

    # 编号化:顶级行带编号,子级行(以"  -"起头)不增计数
    numbered = list(header)
    counter = 0
    for ln in body:
        if ln.startswith("  -") or ln.startswith("    -"):
            numbered.append(ln)
        else:
            counter += 1
            numbered.append(f"{counter}. {ln}")
    return "\n".join(numbered).rstrip()


def _extract_disq_by_priority(disq_text: str) -> str:
    """按重要性分段提取废标条款，确保关键信息不被截断。

    优先级：关键响应词 > 暗标配置 > 否决投标清单 > 格式参数
    总字数控制在 2000 字以内。
    """
    sections: list[tuple[int, str, str]] = []  # (priority, title, content)
    current_title = ""
    current_lines: list[str] = []
    priority_map = {
        "关键响应": 1,
        "否决": 2,
        "暗标": 3,
        "装饰": 3,
        "废标": 4,
        "格式参数": 5,
        "字体": 6,
        "heading": 6,
        "table": 6,
    }

    def flush():
        if current_title and current_lines:
            pri = 7
            for keyword, p in priority_map.items():
                if keyword in current_title.lower() or keyword in current_title:
                    pri = min(pri, p)
                    break
            sections.append((pri, current_title, "\n".join(current_lines)))

    for line in disq_text.splitlines():
        if line.strip().startswith("##"):
            flush()
            current_title = line.strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()

    sections.sort(key=lambda x: x[0])
    result_parts: list[str] = []
    total = 0
    limit = 2000
    for _pri, _title, content in sections:
        if total + len(content) > limit and total > 0:
            break
        result_parts.append(content)
        total += len(content)

    return "\n\n".join(result_parts) if result_parts else disq_text[:2000]


def _parse_response_keywords(disq_text: str) -> list[dict]:
    """从 disqualification_rules.md 的"关键响应点"段落提取必须出现的关键词。

    返回: [{"keyword": "180 日历天", "chapter_hint": "进度计划", "rule": "A-20"}, ...]
    """
    keywords = []
    in_section = False
    for line in disq_text.splitlines():
        if "关键响应" in line and line.strip().startswith("#"):
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("#"):
                break
            if line.strip().startswith("- "):
                entry = line.strip()[2:]
                kw = ""
                chapter_hint = ""
                rule = ""
                # 解析格式: "工期：180 日历天 —— 必须在进度计划章明确响应（A-20 触发）"
                if "：" in entry:
                    parts = entry.split("：", 1)
                    rest = parts[1]
                    if "——" in rest:
                        kw = rest.split("——")[0].strip()
                        desc = rest.split("——")[1].strip()
                        # 提取章节提示
                        if "在" in desc and "章" in desc:
                            start = desc.index("在") + 1
                            end = desc.index("章") + 1
                            chapter_hint = desc[start:end]
                        # 提取规则号
                        import re as _re
                        rule_m = _re.search(r"[A-Z]-?\d+", desc)
                        if rule_m:
                            rule = rule_m.group()
                    else:
                        kw = rest.strip()
                if kw:
                    keywords.append({"keyword": kw, "chapter_hint": chapter_hint, "rule": rule})
    return keywords


def _match_keywords_for_chapter(keywords: list[dict], chapter_title: str) -> list[dict]:
    """找出与本章标题匹配的关键响应词。"""
    matched = []
    for kw in keywords:
        hint = kw.get("chapter_hint", "")
        if not hint:
            continue
        # 章节提示和章节标题做模糊匹配
        hint_core = hint.replace("章", "").strip()
        if hint_core and hint_core in chapter_title:
            matched.append(kw)
    return matched


def _build_keyword_block(keywords: list[dict], chapter_title: str) -> str:
    """为本章匹配的关键响应词生成提示词块。"""
    matched = _match_keywords_for_chapter(keywords, chapter_title)
    if not matched:
        return ""
    lines = ["【关键响应词（必须在正文中原文出现，缺失可能导致废标）】"]
    for kw in matched:
        rule_tag = f"（{kw['rule']} 触发）" if kw.get("rule") else ""
        lines.append(f"  - 必须写明：{kw['keyword']} {rule_tag}")
    return "\n".join(lines) + "\n"


def parse_chapter_plan(plan_path: Path) -> list[dict]:
    rows = _parse_chapter_plan(plan_path)
    if not rows:
        raise FileNotFoundError(f"未找到章节规划或为空: {plan_path}")
    return rows


def extract_brain_context(brain_text: str, chapter_title: str) -> str:
    """从 project_brain.md 抽取与章节主题相关的内容。

    增强版（P1.2）：对制度管理类章节（质量/安全/环保/工期/民工/进度）额外注入
    brain 第三/四节的重难点，作为内容锚点防止写成通用套话。
    """
    lines = brain_text.splitlines()
    full = "\n".join(line for line in lines if line.strip())
    head = full[:2500]
    kws = set(extract_keywords(chapter_title))
    hits = []
    for line in lines:
        for kw in kws:
            if kw in line:
                hits.append(line.strip())
                break
    hits_text = "\n".join(hits[:30])

    # P1.2: 制度管理类章节额外注入重难点锚点
    mgmt_kw = {"质量", "安全", "环保", "绿色", "工期", "文明", "民工", "进度", "编制依据", "组织管理"}
    if any(kw in chapter_title for kw in mgmt_kw):
        sec3, sec4 = "", ""
        in_sec3 = False
        for line in lines:
            if "## 三、技术重点与施工难点" in line:
                in_sec3 = True
                continue
            if "## 四、施工约束条件" in line or (line.startswith("## ") and in_sec3):
                break
            if in_sec3 and line.strip() and not line.startswith("#"):
                sec3 += line + "\n"
                if len(sec3) > 1800:
                    break
        extra = ""
        if sec3.strip():
            extra += f"\n\n▲ 本章是制度管理类章节，必须结合以下项目重难点展开具体措施（禁止只写通用制度条文，每个条款必须有本项目落地方式）：\n{sec3[:1500]}"
        return f"{head}\n\n【本章相关片段】\n{hits_text}{extra}" if hits_text else f"{head}{extra}"
    return f"{head}\n\n【本章相关片段】\n{hits_text}" if hits_text else head


# ─── Prompt 构造 ───────────────────────────────────────────────────────

_PROJECT_TYPE_RE = re.compile(r"项目类型[：:]\s*([^\n（(]+)")


def extract_project_type(brain_text: str) -> str:
    """从 project_brain.md 读取"项目类型"字段；读不到时返回通用表述。"""
    if brain_text:
        m = _PROJECT_TYPE_RE.search(brain_text)
        if m:
            t = m.group(1).strip().strip("，,、 ")
            if t and t not in ("其他", "未知", "-"):
                return t
    return "各类工程"


def build_system_prompt(project_type: str) -> str:
    """按项目类型动态构造角色提示。"""
    return (
        f"你是一位拥有 20 年经验的资深投标文件编写工程师，精通 {project_type} 领域工程的施工组织设计。"
        "熟悉评标规则、深知废标风险。写作时严格遵循招标文件要求，结合项目实际情况，语言专业、逻辑严密、数据具体。"
    )


def build_user_prompt(
    chapter: dict, variant: dict, brain_excerpt: str, disq_excerpt: str,
    skeleton: list[dict], subitems: list[dict],
) -> str:
    """整章 prompt 构造。

    Phase 4 升级：authoring_rules.md 已加载时走 4 块结构（Block 1 项目认知 +
    Block 2 rules 硬约束 + Block 3 本章任务 + Block 4 输出格式）；
    否则回退到 _legacy_build_user_prompt 旧拼装方式（向后兼容）。
    """
    if _AUTHORING_CTX:
        return _build_4block_user_prompt(chapter, variant, skeleton, subitems)
    return _legacy_build_user_prompt(
        chapter, variant, brain_excerpt, disq_excerpt, skeleton, subitems
    )


def _build_4block_user_prompt(
    chapter: dict, variant: dict, skeleton: list[dict], subitems: list[dict],
) -> str:
    """Phase 4 新结构：4 块整章 prompt。"""
    h1_title = chapter["title"]
    target = chapter["target_chars"]
    band_lo = int(target * 0.85)
    band_hi = int(target * 1.15)

    coverage_keywords = _split_notes(chapter.get("notes", ""))
    coverage_line = "、".join(coverage_keywords) if coverage_keywords else "（无特定关键词）"

    block1 = _build_block1_project(
        _AUTHORING_CTX, _BRAIN_TEXT, _DIFFICULTIES_DATA,
        current_chapter_num=chapter["num"],
    )
    block2 = _build_block2_rules(_AUTHORING_CTX)
    block3 = _build_block3_chapter(
        chapter, variant, skeleton, subitems, h1_title, band_lo, band_hi, target, coverage_line,
    )
    block4 = _build_block4_output(band_lo, band_hi, is_subitem=False)

    chart_spec_block = build_chart_spec_block(get_chart_specs_for_chapter(chapter["num"]))

    # 组织架构模板注入（仅 04 章，且 chart_assignment.json 中有模板数据时）
    org_template_json = chapter.get("_org_template_json")
    if org_template_json:
        _org_template_block = (
            f"\n★ 组织架构图必须使用以下{chapter.get('_org_template_label', '')}模板结构"
            f"（严格按此 JSON 结构生成 org_chart）：\n"
            f"```json\n{org_template_json}\n```\n"
            f"  岗位名称可根据项目实际微调，但层级结构和分支数必须与模板一致。\n"
        )
    else:
        _org_template_block = ""

    return f"""请为本投标项目撰写第 {chapter['num']} 章技术标内容。

{block1}

{block2}

{block3}

{block4}

{chart_spec_block}
{_org_template_block}
现在开始撰写正文："""


def _legacy_build_user_prompt(
    chapter: dict, variant: dict, brain_excerpt: str, disq_excerpt: str,
    skeleton: list[dict], subitems: list[dict],
) -> str:
    """旧 prompt 拼装：authoring_rules.md 不可用时使用（向后兼容）。"""
    equip_lines = "\n".join(f"  - {k}: {v:+d} 台" for k, v in variant["equip_delta"].items())
    blind = variant.get("blind", False)
    if blind:
        company_blind = (
            "\n【暗标全章节硬约束（触发即废标）】\n"
            "- 全章节禁用：本公司名/曾用名/LOGO/项目业绩/人员真实姓名/获奖证书编号/项目所在地的分公司地址\n"
            "- 章节编号用阿拉伯数字 01/02/…，不用符号框装饰\n"
        )
    else:
        company_blind = ""

    h1_title = chapter["title"]

    skeleton_block = render_skeleton_for_prompt(skeleton)
    coverage_keywords = _split_notes(chapter.get("notes", ""))
    coverage_line = "、".join(coverage_keywords) if coverage_keywords else "（无特定关键词）"

    if subitems:
        subitem_lines = "\n".join(
            f"  - ## {s['title']}（目标 {s['chars']} 字，±15% 带）" for s in subitems
        )
        subitem_block = (
            "【第三圈子项清单（★ 每条 = 一个二级子节 `##`，按此顺序生成；边界模糊也不合并）】\n"
            f"{subitem_lines}\n"
        )
    else:
        subitem_block = (
            "【第三圈子项清单】chapter_plan 未提供本章子项分配，按上方 H2/H3 骨架生成\n"
        )

    target = chapter["target_chars"]
    band_lo = int(target * 0.85)
    band_hi = int(target * 1.15)

    chart_spec_block = build_chart_spec_block(get_chart_specs_for_chapter(chapter["num"]))

    # 组织架构模板注入（仅 04 章，且 chart_assignment.json 中有模板数据时）
    org_template_json = chapter.get("_org_template_json")
    if org_template_json:
        _org_template_block = (
            f"\n★ 组织架构图必须使用以下{chapter.get('_org_template_label', '')}模板结构"
            f"（严格按此 JSON 结构生成 org_chart）：\n"
            f"```json\n{org_template_json}\n```\n"
            f"  岗位名称可根据项目实际微调，但层级结构和分支数必须与模板一致。\n"
        )
    else:
        _org_template_block = ""

    return f"""请为本投标项目撰写第 {chapter['num']} 章技术标内容。

【章节标题（必须字面一致，作为一级标题 `#`）】
{h1_title}

【对应评分项】
{chapter['score_item']}  (分值 {chapter['score']})

【本章必须使用的二级/三级标题结构（严格按此顺序与层级输出，不得新增/删除/重排 H2）】
{skeleton_block}

{subitem_block}
【必须实质覆盖的评分关键词（可分散到任意小节，不得作为标题标签照抄）】
{coverage_line}

【目标字数（★ 圈层理论，非分值×300）】
目标 {target} 字，允许 ±15% 浮动带：{band_lo}–{band_hi} 字。**图表不计入字数**，该用表就用表、该用图就用图。

【差异化技术参数（必须自然融入正文叙述，严禁回显本段标签名）】
- 劳动力配置系数：{variant['labor_factor']}（原基础 × 该系数）
- 主要机械调整：
{equip_lines}
- 优先引用的现行标准版本：{variant['standard_year_preference']} 年版

【项目理解摘要】
{brain_excerpt}

【废标条款要点（必须规避）】
{disq_excerpt}
{company_blind}
【质量强约束】
1. 一级标题 `#` 必须与上方章节标题字面一致
2. 严格使用上方给定的 H2/H3 标题结构：顺序/文字/层级一字不动；不得新增或删除 H2
3. **每个第三圈子项必须 1:1 成为一个 `##` 二级子节**，子项标题顺序与字面不得改动；边界模糊的子项也不得合并或省略
4. 评分关键词必须在正文中被实质性展开（可分散到各小节），但不得把关键词照抄成 H2/H3 标题
5. 引用与项目类型对应的现行国家/行业规范（GB/T、JGJ、CJJ、TD/T、SL 等），带规范号
6. 紧扣项目重点与难点，不写与本项目无关的通用废话
7. 数据（工期、人数、机械数量、工艺参数）必须具体到数字
8. 字数须落在 {band_lo}–{band_hi} 字带内，图表不折算字数
9. **禁用 Mermaid 代码块**（```mermaid 会被组装阶段直接剥离，docx 渲染不出来）
10. **JSON 图表数据块**：在章节文末可附 ```json 代码块，组装阶段自动渲染为 PNG 图片。**必须包含 `"type"` 字段**，支持以下类型：
    - `"schedule"`：进度横道图。必须含 `tasks`（[{{name,start,end,phase?}}]）和 `milestones`（[{{name,date}}]），日期格式 `YYYY-MM-DD`。适用于进度/工期章节
    - `"org_chart"`：组织机构图。必须含 `root`（{{role,name,children:[...]}}），树形递归结构。适用于项目管理机构章节
    - `"hierarchy_flow"`：管理体系图。含 `layers`（[{{name,nodes:[...]}}]）和 `connections`（[{{from,to,label?}}]）。适用于质量/安全管理体系章节
    - `"process_flow"`：工艺流程图。含 `nodes`（[{{id,label,shape:box|diamond|oval}}]）和 `edges`（[{{from,to,label?}}]）。适用于关键工序/施工方法章节
    - `"resource_curve"`：资源投入曲线。含 `periods`（[...]）和 `series`（[{{name,values:[...]}}]），可选 `x_label`/`y_label`/`show_total_line`。适用于劳动力/材料/机械配置章节
    - `"aoa_network"`：双代号网络计划图。含 `events`（[1,2,3...]）和 `activities`（[{{name,from,to,duration,critical?:bool,dummy?:bool}}]）。事件编号为整数，from/to 指向事件编号；虚工序设 dummy:true+duration:0。适用于进度计划章节（与 schedule 横道图可同时使用）
    - `"site_layout"`：施工总平面布置图。含 `template`（"rectangular_center"/"L_shape"/"U_shape"）、`main_building`（主体建筑名称）、`zones`（{{north:[...],south:[...],east:[...],west:[...]}}每个方位放置的设施名列表）、可选 `cranes`（[{{label,position:"northeast_of_building"等}}]）。适用于平面布置章节
    - 每个 JSON 块只能是上述类型之一；不属于以上类型的 JSON 不要输出
11. **Markdown 表格列数 ≤ 6**：超过 6 列必须按阶段/分项/工种拆成多张小表；"备注/说明"列 ≤ 15 字；禁用 ASCII 字符画（█░ 等）
12. **禁用 Markdown 斜体** `*text*` / `_text_`：斜体在 docx 里是装饰效果，有"做标记"风险；强调一律用 `**粗体**`
13. **禁用所有装饰字符**（投标通行规范，不分暗标/非暗标）：
    - 禁几何装饰：【】〖〗 ★ ◆ ■ ● ◎ ▲ △ ▼ ▽ ◇ □ ☆ ※ • ➤
    - 禁勾选符号：✓ ✅ ✔ ☑ ✗ ☒（"自我核查/已完成"等段落用"已/通过/完成"文字表达，严禁打勾）
    - 禁流程箭头：→ ← ⇒ ⇐（因果/流程用"导致/进入/依次/则"等连词，严禁用箭头代替）
    - 强调只用 `**粗体**` 或阿拉伯数字编号，不用任何装饰符号
14. 纯 Markdown 输出，不要任何元注释或解释

{chart_spec_block}
{_org_template_block}
现在开始撰写正文："""


# ─── 子项 prompt（交叉多模型专用） ────────────────────────────────────

def build_subitem_user_prompt(
    chapter: dict, variant: dict, subitem_assignment: dict,
    sibling_titles: list[str],
    brain_excerpt: str, disq_excerpt: str,
    response_keywords: list[dict] | None = None,
) -> str:
    """为某个子项构造 prompt（只写 ## 子节，约 2000-4500 字）

    Phase 4 升级：authoring_rules.md 已加载时走 4 块结构；否则回退 legacy。
    """
    if _AUTHORING_CTX:
        return _build_4block_subitem_prompt(chapter, variant, subitem_assignment, sibling_titles)
    return _legacy_build_subitem_user_prompt(
        chapter, variant, subitem_assignment, sibling_titles,
        brain_excerpt, disq_excerpt, response_keywords,
    )


def _build_4block_subitem_prompt(
    chapter: dict, variant: dict, subitem_assignment: dict,
    sibling_titles: list[str],
) -> str:
    """Phase 4 新结构:4 块子项 prompt。"""
    target = int(subitem_assignment["target_chars"])
    band_lo = int(target * 0.85)
    band_hi = int(target * 1.15)

    block1 = _build_block1_project(
        _AUTHORING_CTX, _BRAIN_TEXT, _DIFFICULTIES_DATA,
        current_chapter_num=chapter["num"],
    )
    block2 = _build_block2_rules(_AUTHORING_CTX)
    block3 = _build_block3_subitem(
        chapter, variant, subitem_assignment, sibling_titles, band_lo, band_hi, target,
    )
    block4 = _build_block4_output(band_lo, band_hi, is_subitem=True)

    return f"""你只需要撰写第 {chapter['num']} 章中的**一个**二级子节。
章节完整内容将由多位撰稿人并行产出，最后由主笔编辑补写章节引言和过渡衔接。

{block1}

{block2}

{block3}

{block4}

现在开始撰写本子节："""


def _legacy_build_subitem_user_prompt(
    chapter: dict, variant: dict, subitem_assignment: dict,
    sibling_titles: list[str],
    brain_excerpt: str, disq_excerpt: str,
    response_keywords: list[dict] | None = None,
) -> str:
    """旧子项 prompt 拼装：authoring_rules.md 不可用时使用（向后兼容）。"""
    equip_lines = "\n".join(f"  - {k}: {v:+d} 台" for k, v in variant["equip_delta"].items())
    blind = variant.get("blind", False)
    company_blind = (
        "\n【暗标硬约束】禁出现任何公司名/LOGO/业绩/人员真实姓名/获奖证书编号\n"
        if blind else ""
    )

    target = int(subitem_assignment["target_chars"])
    band_lo = int(target * 0.85)
    band_hi = int(target * 1.15)

    siblings_block = "\n".join(f"  - {t}" for t in sibling_titles) or "（无）"

    return f"""你只需要撰写第 {chapter['num']} 章中的**一个**二级子节。章节完整内容将由多位撰稿人并行产出，最后由主笔编辑补写章节引言和过渡衔接。

【所在章节】
第 {chapter['num']} 章 {chapter['title']}  (对应评分项：{chapter['score_item']}，分值 {chapter['score']})

【本子节标题（必须字面一致，作为二级标题 `##`）】
{subitem_assignment['title']}

【本子节目标字数】
{target} 字（允许 ±15% 浮动带：{band_lo}–{band_hi} 字）。**图表不计入字数**，该用表用表。

【本章其他兄弟子节标题（请避免把这些子节该写的内容挤进本子节）】
{siblings_block}

【内容边界约束（极重要）】
- 不要写项目概况、工程背景介绍、章节引言或开场白，这些由主笔统一补写
- 不要写章节总结、小结、收尾段落
- 直接进入本子节的技术实质内容（工艺、参数、措施、规范）
- 不要重复其他兄弟子节的内容

【差异化技术参数（融入正文，严禁回显本段标签名）】
- 劳动力系数：{variant['labor_factor']}
- 主要机械调整：
{equip_lines}
- 优先引用标准版本：{variant['standard_year_preference']} 年版

【项目理解摘要】
{brain_excerpt}

【废标条款要点（必须规避）】
{disq_excerpt}
{company_blind}{_build_keyword_block(response_keywords or [], chapter['title'])}
【输出强约束】
1. 输出以 `## {subitem_assignment['title']}` 起头，随后是正文；不得输出 `#` 一级标题
2. 字数落在 {band_lo}–{band_hi} 字带内
3. 引用现行国家/行业规范（GB/T、JGJ、CJJ、TD/T、SL 等）带规范号，版本以 {variant['standard_year_preference']} 年版为准
4. 数据（人数、工期、机械数量、工艺参数）必须具体到数字
5. 禁用 Mermaid；可在正文末尾附一个 ```json 图表块（type 字段必填）
6. Markdown 表格 ≤ 6 列；禁用 ASCII 字符画
7. 禁用 Markdown 斜体 `*text*` 和 `_text_`，强调用 `**粗体**`
8. 禁用所有装饰字符：【】〖〗 ★ ◆ ■ ● ◎ ▲ △ ▼ ▽ ◇ □ ☆ ※ • ➤ ✓ ✅ ✔ ☑ ✗ ☒ → ← ⇒ ⇐
9. 纯 Markdown 输出，不要任何元注释或"以下是子节内容"之类的话

现在开始撰写本子节："""


# ─── 主笔润色 prompt（交叉多模型专用） ────────────────────────────────

POLISH_SYSTEM_PROMPT = """你是资深投标文件主笔编辑。你收到的是多位撰稿人并行产出的本章各 ## 子节拼接稿。
子节只有技术干货，缺少章节引言和子节之间的过渡。你的职责是**组装补写**，把零件拼成一篇完整的整章。

你需要做的事（按顺序）：
1. 【补写章节引言】在 H1 标题后、第一个 ## 子节前，补写 200-400 字的章节引言段落。
   引言应包含：本章对应的工程背景概述、本章编制依据与目标、各子节内容概要。
   引言不设独立标题，直接作为 H1 下的正文段落。
2. 【补写过渡衔接】在相邻 ## 子节之间各补写 1-2 句过渡语（承上启下），自然嵌入前一节末尾或后一节开头。
3. 【统一序号格式】不同撰稿人可能使用不同的编号风格（如 1./2.、(1)/(2)、①②、第一/第二 等），
   必须统一为用户指定的序号方案（见用户 prompt 中的【本份标书序号方案】）。
   将正文中所有步骤/要点/条目的编号替换为指定方案的对应层级格式。
4. 【统一术语】同一事物在不同子节中的叫法不统一时，以"优先引用版本"为准统一全章。
5. 【统一人称时态】统一用第三人称陈述。

严格遵守以下铁律：
1. 【子节内容不动】各 ## 子节的技术正文原封不动保留，不删段、不改写、不缩减。
2. 【标题绝对保留】所有 H1 `#`、H2 `##`、H3 `###` 的文字、顺序、层级一字不改。
   不得删除/合并/拆分任何标题。
3. 【禁止元语句】不得添加"综上所述""本章小结""以下章节将"等总结性空话。
4. 【输出格式】纯 Markdown 正文，保留原有 ```json 图表块、表格、粗体。
   不要代码围栏包裹整章；不要"以下是整合后的内容"之类的话。
5. 【装饰字符禁令】输出中不得出现：【】〖〗 ★ ◆ ■ ● ◎ ▲ △ ▼ ▽ ◇ □ ☆ ※ • ➤
   ✓ ✅ ✔ ☑ ✗ ☒ → ← ⇒ ⇐。强调只用 **粗体**。"""


def build_polish_user_prompt(chapter: dict, variant: dict, draft_markdown: str) -> str:
    blind_tail = ""
    if variant.get("blind"):
        blind_tail = (
            "\n【暗标追加约束】不得出现任何公司名、人名、LOGO、项目业绩编号、"
            "获奖证书编号。不得使用 Markdown 斜体 `*text*`。"
        )
    chart_style = variant.get("chart_style", {})
    ns = chart_style.get("numbering_scheme", {})
    numbering_block = (
        f"  - 一级编号：{ns.get('level1', '1.  2.  3.')}\n"
        f"  - 二级编号：{ns.get('level2', '(1)  (2)  (3)')}\n"
        f"  - 三级编号：{ns.get('level3', '①  ②  ③')}"
    )
    h1_title = chapter["title"]
    chart_spec_block = build_chart_spec_block(get_chart_specs_for_chapter(chapter["num"]))
    _org_template_block = ""  # polish 阶段不需要注入组织架构模板
    return f"""【章节标题】
第 {chapter['num']} 章 {h1_title}

【优先引用规范版本】
{variant['standard_year_preference']} 年版（以此版本为准统一全章规范引用）

【本份标书序号方案（必须严格执行）】
{numbering_block}
正文中所有步骤/要点/条目的编号必须替换为上述格式，不得混用其他编号风格。

【整章拼接稿（来自 N 位撰稿人的并行产出，各子节只有技术干货，缺少引言和过渡）】
{draft_markdown}

【任务】
请按系统规则组装补写：补写章节引言（200-400字）、补写子节间过渡衔接、统一术语、统一序号格式。
子节技术正文原封不动保留。**保留并按以下规格补齐图表 ```json``` 块**——若拼接稿缺图,必须在对应子节末追加。输出完整的整章 Markdown。{blind_tail}

{chart_spec_block}"""


# ─── 主流程 ───────────────────────────────────────────────────────────

def _generate_one_cross(
    client: LLMClient, chapter: dict, variant: dict,
    brain_excerpt: str, disq_excerpt: str,
    out_path: Path, log: logging.Logger,
    plan: dict,
    get_sem,
    response_keywords: list[dict] | None = None,
    variant_chart_overrides: dict | None = None,
) -> bool:
    """交叉多模型：子项并行 + 主笔润色"""
    vid = variant["id"]
    ch_num = chapter["num"]
    subitem_assignments = plan["subitems"]
    polish_model = plan["polish_model"]
    polish_temp = plan.get("polish_temperature", variant.get("temperature", 0.6))

    if not subitem_assignments:
        log.warning(f"[{vid}] 章 {ch_num} 交叉模式但子项清单为空，退化到单模型老流程")
        return False

    log.info(
        f"[{vid} {variant['company_name']}] 章 {ch_num} 交叉模式："
        f"{len(subitem_assignments)} 子项并行 + 主笔 {polish_model} 润色"
    )

    sibling_titles = [s["title"] for s in subitem_assignments]

    def run_subitem(sub: dict) -> tuple[dict, str, Exception | None]:
        prompt = build_subitem_user_prompt(
            chapter, variant, sub, sibling_titles, brain_excerpt, disq_excerpt,
            response_keywords=response_keywords,
        )
        sys_prompt = (
            "你是投标文件子节撰稿人，专注写好分给你的这一个二级子节，"
            "严格按给定标题、字数带和规范要求输出。"
        )
        sem = get_sem(sub["model"])
        with sem:
            try:
                resp = client.chat(
                    model_key=sub["model"],
                    system=sys_prompt,
                    user=prompt,
                    temperature=sub["temperature"],
                    enable_fallback=True,
                )
                return sub, resp.text.strip(), None
            except Exception as e:
                return sub, "", e

    # 子项内层并行
    inner_workers = min(len(subitem_assignments), SUBITEM_INNER_WORKERS)
    results: list[tuple[dict, str, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=inner_workers) as inner:
        results = list(inner.map(run_subitem, subitem_assignments))

    # 按 idx 排序拼接
    results.sort(key=lambda x: x[0]["idx"])
    sub_texts: list[str] = []
    for sub, text, err in results:
        if err:
            log.error(f"  ✗ 子项 {sub['idx']} [{sub['model']}] {sub['title'][:20]} 失败: {err}")
            sub_texts.append(f"## {sub['title']}\n\n（本子节生成失败，需后续补写）")
            continue
        # 确保以 ## 标题起头
        if not text.startswith("## "):
            text = f"## {sub['title']}\n\n{text}"
        sub_texts.append(text)
        log.info(
            f"  · 子项 {sub['idx']} [{sub['model']}@T{sub['temperature']}] "
            f"{sub['title'][:18]} → {count_chinese_chars(text)} 字"
        )

    # 拼整章草稿
    h1_title = chapter["title"]
    draft = f"# {h1_title}\n\n" + "\n\n".join(sub_texts)
    draft_chars = count_chinese_chars(draft)

    # 主笔组装（补写引言+过渡；失败或内容被删减时回退到草稿）
    final_text = draft
    if SKIP_POLISH:
        log.info(f"  拼章草稿 {draft_chars} 字，--no-polish 已跳过主笔组装")
    else:
        log.info(f"  拼章草稿 {draft_chars} 字，调 {polish_model} 组装补写…")
        polish_user = build_polish_user_prompt(chapter, variant, draft)
        polish_sem = get_sem(polish_model)
        try:
            with polish_sem:
                resp = client.chat(
                    model_key=polish_model,
                    system=POLISH_SYSTEM_PROMPT,
                    user=polish_user,
                    temperature=polish_temp,
                    enable_fallback=True,
                )
            assembled = resp.text.strip()
            assembled_chars = count_chinese_chars(assembled)
            # 主笔是补写内容，产出应 >= 草稿；若反而变少说明删了子节内容，弃用
            if assembled_chars < draft_chars * 0.95:
                log.warning(
                    f"  ⚠ 组装后 {assembled_chars} 字 < 草稿 {draft_chars} 的 95%，"
                    f"主笔可能删减了子节内容，弃用组装结果"
                )
            elif assembled_chars > draft_chars * 1.25:
                log.warning(
                    f"  ⚠ 组装后 {assembled_chars} 字 > 草稿 {draft_chars} 的 125%，"
                    f"主笔补写过多，弃用组装结果"
                )
            else:
                final_text = assembled
                added = assembled_chars - draft_chars
                log.info(f"  ✓ 组装完成 {assembled_chars} 字（补写 +{added} 字）")
        except Exception as e:
            log.warning(f"  ⚠ 主笔组装调用失败: {e}；使用草稿拼接作最终输出")

    # 暗标兜底
    company = variant.get("company_name", "")
    if variant.get("blind") and company:
        final_text = final_text.replace(company, "本公司")

    # 确保 H1 存在（润色层理论上保留 H1，但做一次兜底）
    if not final_text.startswith("# "):
        final_text = f"# {h1_title}\n\n{final_text}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(final_text, encoding="utf-8")

    final_chars = count_chinese_chars(final_text)
    target = chapter["target_chars"]
    if final_chars < target * 0.85:
        log.warning(f"  ⚠ 字数不足: {final_chars}/{target}  (实际/目标)")
    else:
        log.info(f"  ✓ 字数达标: {final_chars}/{target}")
    return True


# ── 变体图表上下文注入 ─────────────────────────────────────────────

def _apply_variant_chart_context(
    chapter: dict, variant: dict,
    chart_overrides: dict | None, log: logging.Logger,
) -> None:
    """将 chart_assignment.json 的信息注入 chapter/variant 上下文，
    让 writer prompt 能感知组织架构模板和管理体系图类型。

    通过临时修改全局 chart specs 实现。
    """
    if not chart_overrides:
        return

    ch_num_str = str(chapter["num"]).zfill(2)
    org_template = chart_overrides.get("org_template", {})
    mgmt_types = chart_overrides.get("management_chart_types", {})
    chart_type_overrides = chart_overrides.get("chart_overrides", {})

    # 1. 组织架构模板：在 04 章注入模板描述
    if ch_num_str == "04" and org_template:
        template_desc = json.dumps(org_template.get("children", []), ensure_ascii=False)
        chapter["_org_template_json"] = template_desc
        chapter["_org_template_label"] = org_template.get("template_label", "")

    # 2. 管理体系图类型覆盖
    if ch_num_str in mgmt_types:
        override_type = mgmt_types[ch_num_str]
        # 修改全局 chart specs 中对应章节的图表类型
        ch_specs = _CHART_SPECS_BY_CHAPTER.get(ch_num_str, [])
        for spec in ch_specs:
            if "hierarchy" in spec.get("type", "") or "体系" in spec.get("data_hint", ""):
                spec["_override_type"] = override_type
                log.info(f"  [{variant['id']}] 章 {ch_num_str} 管理体系图类型覆盖为 {override_type}")

    # 3. 可选图表类型覆盖
    if ch_num_str in chart_type_overrides:
        for ov in chart_type_overrides[ch_num_str]:
            section = ov.get("section", "")
            new_type = ov.get("override_type")
            ch_specs = _CHART_SPECS_BY_CHAPTER.get(ch_num_str, [])
            for spec in ch_specs:
                if spec.get("section", "") == section:
                    if new_type is None:
                        spec["_skip"] = True
                    else:
                        spec["_override_type"] = new_type


def _generate_one_single(
    client: LLMClient, chapter: dict, variant: dict,
    brain_excerpt: str, disq_excerpt: str,
    out_path: Path, log: logging.Logger,
    system_prompt: str,
    subitems: list[dict],
    variant_chart_overrides: dict | None = None,
) -> bool:
    """老流程：单模型整章调用（兼容无 chapter_model_plan 的旧 variants.json）"""
    # 优先使用变体专属大纲的 skeleton + subitems（从 chapter_plan_variant.md）
    variant_plan_dir = out_path.parent
    variant_plan_path = variant_plan_dir / "chapter_plan_variant.md"
    if variant_plan_path.exists():
        v_subitems_map = parse_plan_subitems(variant_plan_path)
        v_subitems = v_subitems_map.get(chapter["num"])
        if v_subitems:
            subitems = v_subitems
            skeleton = [{"level": 2, "title": s["title"], "children": []} for s in v_subitems]
        else:
            skeleton = derive_variant_skeleton(chapter, variant)
    else:
        skeleton = derive_variant_skeleton(chapter, variant)

    # 注入变体图表分配（组织架构模板 + 管理体系图类型覆盖）
    _apply_variant_chart_context(chapter, variant, variant_chart_overrides, log)

    prompt = build_user_prompt(chapter, variant, brain_excerpt, disq_excerpt, skeleton, subitems)
    log.info(
        f"[{variant['id']} {variant['company_name']}] 章 {chapter['num']} "
        f"{chapter['title'][:20]}.. 单模型模式 模型={variant['model']}"
    )

    try:
        resp = client.chat(
            model_key=variant["model"],
            system=system_prompt,
            user=prompt,
            temperature=variant.get("temperature", 0.6),
        )
    except Exception as e:
        log.error(f"  ✗ 生成失败: {e}")
        return False

    text = resp.text.strip()
    h1_title = chapter["title"]
    if not text.startswith("# "):
        text = f"# {h1_title}\n\n{text}"

    company = variant.get("company_name", "")
    if variant.get("blind") and company:
        text = text.replace(company, "本公司")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    n_chars = count_chinese_chars(text)
    target = chapter["target_chars"]
    if n_chars < target * 0.85:
        log.warning(f"  ⚠ 字数不足: {n_chars}/{target}  (实际/目标)")
    else:
        log.info(
            f"  ✓ 字数达标: {n_chars}/{target}  模型={resp.model}  "
            f"耗时={resp.usage.elapsed_seconds:.1f}s"
        )
    return True


def generate_one(
    client: LLMClient, chapter: dict, variant: dict,
    brain_excerpt: str, disq_excerpt: str,
    out_path: Path, log: logging.Logger,
    system_prompt: str,
    subitems: list[dict],
    get_sem=None,
    response_keywords=None,
    variant_chart_overrides=None,
) -> bool:
    """生成单个 variant × 单个章节

    有 chapter_model_plan 且 mode=cross_model 走子项并行+润色流程；
    否则走旧的单模型整章流程（向后兼容）。
    """
    plan = (variant.get("chapter_model_plan") or {}).get(chapter["num"])
    if plan and plan.get("mode") == "cross_model" and get_sem is not None:
        return _generate_one_cross(
            client, chapter, variant, brain_excerpt, disq_excerpt,
            out_path, log, plan, get_sem,
            response_keywords=response_keywords,
            variant_chart_overrides=variant_chart_overrides,
        )
    return _generate_one_single(
        client, chapter, variant, brain_excerpt, disq_excerpt,
        out_path, log, system_prompt, subitems,
        variant_chart_overrides=variant_chart_overrides,
    )


def _usable_models(client: LLMClient) -> list[str]:
    """当前 config 下真正可用的模型:存在 + 未停用 + API key 已配置。"""
    usable = []
    for key, m in client.models.items():
        if m.get("disabled"):
            continue
        if not os.environ.get(m.get("api_key_env", "")):
            continue
        usable.append(key)
    return usable


def _validate_variant_models(variants: list[dict], client: LLMClient,
                             log: logging.Logger) -> bool:
    """校验每个变体的模型在当前 config 下可用,不可用则就地自动替换。

    variants.json 在生成时定死模型,若此后 config 改了(模型停用/换 key/删除),
    writer 不复查会导致该变体整批章节白跑。启动即校验:不可用的变体换成一个
    可用且尽量不与其他变体重复的模型(保持变体间文风相异)。
    返回 False 表示无任何可用模型(致命)。
    """
    usable = _usable_models(client)
    if not usable:
        log.error("当前 config 无任何可用模型(检查 API key 与 disabled 标记)")
        return False
    for v in variants:
        if v.get("model") in usable:
            continue
        old = v.get("model")
        taken = {tv.get("model") for tv in variants if tv is not v}
        candidates = ([k for k in client.fallback_order if k in usable and k not in taken]
                      or [k for k in usable if k not in taken]
                      or usable)
        v["model"] = candidates[0]
        log.warning(f"[{v['id']}] 模型 {old!r} 当前不可用(停用/缺 key/不存在),"
                    f"自动替换为 {v['model']!r}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="多模型章节生成工作器")
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--variant", help="仅生成指定 variant（如 V01）")
    p.add_argument("--chapter", help="仅生成指定章节号（如 01）")
    p.add_argument("--force", action="store_true", help="覆盖已生成文件")
    p.add_argument("--workers", type=int, default=4, help="并发 worker 数（默认 4）")
    p.add_argument("--per-model-limit", type=int, default=2, help="单模型并发上限（默认 2）")
    p.add_argument("--no-polish", action="store_true", help="跳过主笔润色，直接用子项拼章草稿为最终输出")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    global SKIP_POLISH
    SKIP_POLISH = args.no_polish

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    output.mkdir(exist_ok=True)

    log_path = output / "writer.log"
    log = make_script_logger("bid_writer", log_path, verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_writer 启动  目录: {tender_dir}  时间: {datetime.now().isoformat(timespec='seconds')}")

    # 加载输入
    variants_path = extracted / "variants.json"
    if not variants_path.exists():
        log.error(f"缺少 {variants_path}。请先运行 bid_variants.py")
        return 2
    variants_payload = json.loads(variants_path.read_text(encoding="utf-8"))

    chapters = parse_chapter_plan(extracted / "chapter_plan.md")
    if not chapters:
        log.error("chapter_plan.md 解析不到任何章节")
        return 2

    # 加载图表规格(可选,缺失时退回到 prompt 中的 7 种 type 总览)
    load_chart_specs(extracted, log)

    # Phase 4: 加载 authoring_rules.md 结构化上下文(走 4 块新 prompt)
    use_4block = load_authoring_context(extracted, log)
    if use_4block:
        log.info("★ Phase 4 启用:走 4 块结构 prompt(Block 1 项目认知 + Block 2 rules 硬约束 + Block 3 本章任务 + Block 4 输出格式)")
    else:
        log.info("authoring_rules.md 不可用,回退 legacy prompt 模式")

    subitems_map = parse_plan_subitems(extracted / "chapter_plan.md")
    if subitems_map:
        log.info(f"第三圈子项分配已解析：{sum(len(v) for v in subitems_map.values())} 条子项，覆盖 {len(subitems_map)} 章")
    else:
        log.warning("chapter_plan.md 未提供第三圈子项分配段（## 子项分配），writer 将退化为仅按 H2/H3 骨架生成")

    brain_text = (extracted / "project_brain.md").read_text(encoding="utf-8") if (extracted / "project_brain.md").exists() else ""
    # Phase 3:优先读 authoring_rules.md(方案规则),不存在则回退 disqualification_rules.md(合规审计)
    from bid_common import preferred_rules_path
    _rules_path = preferred_rules_path(extracted)
    disq_text = _rules_path.read_text(encoding="utf-8") if _rules_path.exists() else ""
    if _rules_path.name == "authoring_rules.md":
        log.info(f"使用 authoring_rules.md 作为方案规则源")
    # 按重要性分段提取废标条款（不再简单截断前 1500 字）
    disq_excerpt = _extract_disq_by_priority(disq_text) if disq_text else "（无特殊废标条款）"
    # 提取关键响应词（必须在正文中出现的词句）
    response_keywords = _parse_response_keywords(disq_text)
    if response_keywords:
        log.info(f"关键响应词已提取：{len(response_keywords)} 条")

    # 按项目类型动态构造角色提示（从 project_brain.md "项目类型"字段读取）
    project_type = extract_project_type(brain_text)
    system_prompt = build_system_prompt(project_type)
    log.info(f"项目类型识别: {project_type}")

    client = LLMClient.load(log=log)

    # 过滤 variants / chapters
    target_variants = variants_payload["variants"]
    if args.variant:
        target_variants = [v for v in target_variants if v["id"].upper() == args.variant.upper()]
        if not target_variants:
            log.error(f"未找到 variant: {args.variant}")
            return 2

    # 校验变体模型在当前 config 下可用(不可用则就地替换,避免整批章节白跑)
    if not _validate_variant_models(target_variants, client, log):
        return 2

    ok_count = fail_count = skip_count = 0

    # 章节结构:每个变体优先用自己的 chapter_plan_variant.md(结构独立,反串标),
    # 缺失时回退共享 chapter_plan.md。
    def _variant_chapters(v: dict) -> list[dict]:
        vp = output / v["id"] / "chapter_plan_variant.md"
        v_chs = parse_chapter_plan(vp) if vp.exists() else []
        if not v_chs:
            v_chs = chapters
        if args.chapter:
            v_chs = [c for c in v_chs if c["num"] == args.chapter.zfill(2)]
        return v_chs

    # 构造任务列表(逐变体取各自的章节结构)
    tasks = []
    for v in target_variants:
        v_dir = output / v["id"]
        v_dir.mkdir(exist_ok=True)
        v_chapters = _variant_chapters(v)
        if not v_chapters:
            log.error(f"[{v['id']}] 未解析到章节"
                      + (f"(指定的 --chapter {args.chapter} 不在该变体中)" if args.chapter else ""))
            continue
        for ch in v_chapters:
            out_path = v_dir / ch["filename"]
            if out_path.exists() and not args.force:
                log.info(f"[{v['id']}] 章 {ch['num']} 已存在，跳过（用 --force 覆盖）")
                skip_count += 1
                continue
            tasks.append((v, ch, out_path))

    log.info(f"待生成: {len(target_variants)} 个变体, 共 {len(tasks)} 个章节任务"
             + (f"(另跳过 {skip_count})" if skip_count else ""))

    # 每模型限流锁
    model_semaphores: dict[str, threading.Semaphore] = defaultdict(
        lambda: threading.Semaphore(args.per_model_limit)
    )
    # 确保每个模型只创建一次信号量
    _sem_lock = threading.Lock()
    _sems: dict[str, threading.Semaphore] = {}

    def get_sem(model_key: str) -> threading.Semaphore:
        with _sem_lock:
            if model_key not in _sems:
                _sems[model_key] = threading.Semaphore(args.per_model_limit)
            return _sems[model_key]

    def run_task(v, ch, out_path):
        brain_excerpt = extract_brain_context(brain_text, ch["title"])
        # 优先使用变体专属子项分配
        variant_subitems_path = output / v["id"] / "chapter_plan_variant.md"
        if variant_subitems_path.exists():
            v_subitems_map = parse_plan_subitems(variant_subitems_path)
            subitems = v_subitems_map.get(ch["num"], subitems_map.get(ch["num"], []))
        else:
            subitems = subitems_map.get(ch["num"], [])
        # 加载变体图表分配（如果存在）
        chart_assign_path = output / v["id"] / "chart_assignment.json"
        variant_chart_overrides = None
        if chart_assign_path.exists():
            try:
                variant_chart_overrides = json.loads(chart_assign_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        plan = (v.get("chapter_model_plan") or {}).get(ch["num"])
        if plan and plan.get("mode") == "cross_model":
            # 交叉模式：外层不占 sem，内层对每个子项独立占（否则主模型 sem 会与子项用主模型死锁）
            return generate_one(
                client, ch, v, brain_excerpt, disq_excerpt, out_path, log,
                system_prompt, subitems, get_sem=get_sem,
                response_keywords=response_keywords,
                variant_chart_overrides=variant_chart_overrides,
            )
        # 单模型模式：外层占主模型 sem（保留旧行为）
        sem = get_sem(v["model"])
        with sem:
            return generate_one(
                client, ch, v, brain_excerpt, disq_excerpt, out_path, log,
                system_prompt, subitems, get_sem=None,
                response_keywords=response_keywords,
                variant_chart_overrides=variant_chart_overrides,
            )

    if tasks:
        workers = max(1, args.workers)
        log.info(f"并发 workers={workers}  单模型上限={args.per_model_limit}  任务数={len(tasks)}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_task, v, ch, op): (v, ch) for v, ch, op in tasks}
            for fut in as_completed(futures):
                v, ch = futures[fut]
                try:
                    ok = fut.result()
                except Exception as e:
                    log.error(f"[{v['id']}] 章 {ch['num']} 任务异常: {e}")
                    ok = False
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1

    # 统计
    stats = client.stats.summary()
    log.info("=" * 60)
    log.info(f"生成完成  成功: {ok_count}  失败: {fail_count}  跳过: {skip_count}")
    log.info(f"总 token: {stats['total_tokens']:,}  总耗时: {stats['total_seconds']:.1f}s")
    for m, d in stats["by_model"].items():
        log.info(f"  [{m}] calls={d['calls']}  tokens={d['total']:,}  seconds={d['seconds']:.1f}")

    # 写入统计 JSON 供后续参考
    (output / "writer_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
