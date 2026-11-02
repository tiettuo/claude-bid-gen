#!/usr/bin/env python3
"""
bid_rules_synthesize.py - 合成 disqualification_rules.md

读 facts/evaluation/outline/disqualification + 招标主文档全文,
组装 disqualification_rules.md(YAML 块结构)。

输出包含 8 个 YAML 块:
  project / evaluation / procurement / blind_bid / format / density /
  schedule_milestones / must_include_sections + 一个废标条款摘要列表

用法:
  python bid_rules_synthesize.py /path/to/tender/
  python bid_rules_synthesize.py /path/to/tender/ --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402
from bid_facts_extract import pick_main_tender_text  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"

DEFAULT_FORMAT = {
    "paper": "A4",
    "margin_cm": {"top": 2.5, "bottom": 2.5, "left": 2.5, "right": 2.5},
    "font_chinese": "宋体",
    "font_english": "Times New Roman",
    "body_size_pt": 12,
    "body_size_name": "小四",
    "line_spacing_rule": "multiple",
    "line_spacing_value": 1.5,
    "paragraph_first_line_indent_cm": 0.74,
    "heading_h1_size_pt": 16,
    "heading_h1_size_pt_source": "default",   # 非招标抽取值 → assemble 据此让 H1 跟随正文
    "heading_h2_size_pt": 14,
    "heading_h2_size_pt_source": "default",
    "heading_h3_size_pt": 12,
    "heading_h3_size_pt_source": "default",
    "table_font_size_pt": 11,
    "page_number": "footer_center",
    "toc_required": True,
    "cover_required": True,
}

DEFAULT_DENSITY = {
    "font_size_pt": 12,
    "line_spacing": "1.5倍",
    "chars_per_page_min": 850,
    "chars_per_page_mid": 900,
    "chars_per_page_max": 950,
    "figure_ratio_estimate": 0.25,
}


# ─── 正则抽取项目特定数字字段 ───────────────────────────────────────────

PATTERNS_SAFETY_FEE = [
    r"安全文明施工费[^。\n]*?[(（]?小写[)）]?[:：\s]*([0-9,]+\.\d{1,2})",
    r"安全文明施工费.*?暂定金额[^。\n]*?([0-9,]+\.\d{1,2})",
    r"其中[:：].*?安全文明施工费.*?([0-9,]+\.\d{1,2})",
]
PATTERNS_BID_BOND = [
    r"投标保证金[^。\n]*?(?:金额)?[:：\s]*(?:人民币)?\s*([0-9,]+(?:\.\d+)?)\s*元",
    r"投标保证金[（(]?元[）)]?\s*[\|｜][^0-9\n]{0,8}([0-9,]+(?:\.\d+)?)",  # 不跨远句吞年份/无关数字
]
PATTERNS_VALIDITY = [
    r"投标有效期[^。\n]{0,20}?(\d+)\s*(?:个)?(?:日历)?(?:天|日)",
    r"响应文件.*?有效期为.*?截止时间起\s*(\d+)\s*天",
    r"比选有效期.*?(\d+)\s*天",
]
PATTERNS_PERF_BOND = [
    r"履约(?:担保|保证金).*?(?:金额|为)[^。\n]*?合同(?:金额|价款)?.*?(\d+)\s*%",
    r"履约(?:担保|保证金).*?(\d+)%",
]
PATTERNS_DEFECT = [
    r"(?:工程)?(?:缺陷|质量)(?:责任|保修)期\s*为?\s*(\d+)\s*年",
    r"质保期\s*(?:为)?\s*(\d+)\s*年",
]

# 中文字号 → 磅值映射(GB/T 9851 + Word 实测)
FONT_NAME_TO_PT = {
    "小六": 6.5, "六号": 7.5,
    "小五": 9, "五号": 10.5,
    "小四": 12, "四号": 14,
    "小三": 15, "三号": 16,
    "小二": 18, "二号": 22,
    "小一": 24, "一号": 26,
    "小初": 36, "初号": 42,
}

PATTERNS_FONT_SIZE = [
    # 优先级 1:正文/响应文件等上下文(强信号)
    (r"(?:字体采用|正文采用|本文采用|施工组织设计|响应文件|投标文件)[^\n。]{0,20}?(\d+(?:\.\d+)?)\s*(?:pt|磅)", "pt"),
    (r"(?:字体采用|正文采用|本文采用|施工组织设计|响应文件|投标文件)[^\n。]{0,20}?(小六|六号|小五|五号|小四|四号|小三|三号|小二|二号)", "name"),
    # 优先级 2:配置文件硬键
    (r"font_size_body\s*[:：]\s*(小六|六号|小五|五号|小四|四号|小三|三号)", "name"),
    # 优先级 3:宋体/仿宋附近字号(允许"黑色"等修饰词);需排除"表格""图表"上下文
    (r"(?:宋体|仿宋|楷体|黑体)[^号]{0,6}?(小六|六号|小五|五号|小四|四号|小三|三号|小二|二号)", "name"),
]


def _is_table_context(text: str, pos: int) -> bool:
    """判断 pos 附近是否是表格/图表/标题专属上下文。
    标题词只在「黑体/标题字」语境跳过;若同一窗口出现"正文/宋体"等正文信号则不跳
    (防"…标题黑体，正文宋体小四"被整窗误杀 → 密度回退默认,页数预算失真)。"""
    snippet = text[max(0, pos - 20):pos]
    if any(k in snippet for k in ["表格", "图表", "表中", "图内"]):
        return True
    if any(k in snippet for k in ["标题", "各级标题", "题目"]):
        # 窗口内另有正文信号 → 这是复合句,字号归属需具体看,不盲跳
        if not any(k in snippet for k in ["正文", "宋体", "仿宋", "楷体"]):
            return True
    return False

PATTERNS_LINE_SPACING = [
    (r"行距\s*[:：]?\s*(\d+(?:\.\d+)?)\s*倍", "multiple"),
    (r"(\d+(?:\.\d+)?)\s*倍\s*行距", "multiple"),
    (r"行距\s*固定\s*(\d+)\s*(?:pt|磅)", "fixed"),
]


def _normalize_num(s: str, as_float: bool = False) -> float | int | None:
    s = s.replace(",", "").strip()
    try:
        if as_float:
            return float(s)
        return int(float(s))
    except ValueError:
        return None


def extract_format_spec(text: str) -> dict:
    """从招标文件抽取字号 / 行距硬约束。未抽到则返回空 dict 走默认值。"""
    out = {}
    scan_text = text[:50000]
    # 字号:按优先级扫描;同优先级内取第一个非表格上下文的匹配
    for pat, kind in PATTERNS_FONT_SIZE:
        for m in re.finditer(pat, scan_text):
            # 排除表格上下文
            if _is_table_context(scan_text, m.start()):
                continue
            val = m.group(1)
            if kind == "pt":
                try:
                    pt = float(val)
                    if 8 <= pt <= 26:
                        out["font_size_pt"] = pt
                        break
                except ValueError:
                    pass
            elif kind == "name":
                pt = FONT_NAME_TO_PT.get(val)
                if pt:
                    out["font_size_pt"] = pt
                    out["font_size_name"] = val
                    break
        if "font_size_pt" in out:
            break
    # 行距
    for pat, rule in PATTERNS_LINE_SPACING:
        m = re.search(pat, text[:50000])
        if m:
            v = float(m.group(1))
            if rule == "multiple" and 1.0 <= v <= 3.0:
                out["line_spacing_rule"] = "multiple"
                out["line_spacing_value"] = v
                break
            elif rule == "fixed" and 10 <= v <= 60:
                out["line_spacing_rule"] = "fixed"
                out["line_spacing_value"] = v
                break
    return out


def regex_extract_rule_facts(text: str) -> dict:
    """从主文档抽 5 个 rules 专用字段。"""
    out = {}

    for pat in PATTERNS_SAFETY_FEE:
        m = re.search(pat, text)
        if m:
            out["safety_fee_cny"] = _normalize_num(m.group(1), as_float=True)
            break

    for pat in PATTERNS_BID_BOND:
        m = re.search(pat, text)
        if m:
            v = _normalize_num(m.group(1))
            if v and 100 <= v <= 100_000_000:
                out["bid_bond_cny"] = v
                break

    for pat in PATTERNS_VALIDITY:
        m = re.search(pat, text)
        if m:
            v = _normalize_num(m.group(1))
            if v and 30 <= v <= 365:
                out["bid_validity_days"] = v
                break

    for pat in PATTERNS_PERF_BOND:
        m = re.search(pat, text)
        if m:
            v = _normalize_num(m.group(1))
            if v and 1 <= v <= 30:
                out["performance_bond_ratio"] = v / 100.0
                break

    for pat in PATTERNS_DEFECT:
        m = re.search(pat, text)
        if m:
            v = _normalize_num(m.group(1))
            if v and 1 <= v <= 10:
                out["defect_period_years"] = v
                break

    return out


def llm_fallback_rule_facts(text: str, missing_keys: list[str], log) -> dict:
    """对正则未命中的 rules 字段调 LLM 兜底。"""
    if not missing_keys:
        return {}

    descriptions = {
        "safety_fee_cny": "安全文明施工费暂定金额(纯数字,单位元,如 5808.04)",
        "bid_bond_cny": "投标保证金金额(纯整数,单位元,如 3390)",
        "bid_validity_days": "投标/响应文件有效期(整数天数,如 90)",
        "performance_bond_ratio": "履约担保金额占合同金额比例(小数,如 0.10 表示 10%)",
        "defect_period_years": "工程缺陷责任期(整数年,如 1)",
    }
    snippet = text[:50000]
    fields_lines = "\n".join(f"- {k}: {descriptions.get(k, k)}" for k in missing_keys)

    system = (
        "你是招标文件信息抽取助手。从招标文件中抽取指定数字字段。"
        "若字段未明示,输出 null。只输出 JSON,无 markdown 标记。"
    )
    user = (
        f"招标文件:\n{snippet}\n\n---\n请抽取:\n{fields_lines}\n\n"
        f'输出 JSON 例:\n{{"safety_fee_cny": 5808.04, "bid_bond_cny": 3390, '
        f'"bid_validity_days": 90, "performance_bond_ratio": 0.10, "defect_period_years": 1}}\n'
    )

    try:
        client = LLMClient.load(log=log)
        resp = client.chat(
            model_key="deepseek" if "deepseek" in (client.config.get("models", {}) or {}) else client.config.get("default_model", "deepseek"),
            system=system,
            user=user,
            temperature=0.1,
            max_tokens=500,
        )
    except Exception as e:
        log.warning(f"  LLM 兜底失败: {e}")
        return {}

    raw = resp.text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"  LLM 兜底 JSON 解析失败: {e}")
        return {}

    return {k: v for k, v in d.items() if k in missing_keys and v is not None}


# ─── 扩展正则:authoring 专用 ─────────────────────────────────────────────

PATTERNS_PAGE_HARD_CAP_SMALL = [
    # 中小型/小型专属档(只有 size 命中"小"档时优先匹配)
    r"中小型(?:项目)?[^。\n]{0,40}?总?页数?不?得?超过\s*(\d+)\s*页",
    r"小型(?:项目)?[^。\n]{0,40}?总?页数?不?得?超过\s*(\d+)\s*页",
]
PATTERNS_PAGE_HARD_CAP_LARGE = [
    # 大/中/特大型专属档(只有 size 命中"大"档时优先匹配)
    r"大型(?:项目)?[^。\n]{0,40}?总?页数?不?得?超过\s*(\d+)\s*页",
    r"中型(?:项目)?[^。\n]{0,40}?总?页数?不?得?超过\s*(\d+)\s*页",
]
PATTERNS_PAGE_HARD_CAP_GENERIC = [
    # 通用兜底(任何规模都适用)
    r"本项目[^。\n]{0,40}?总?页数?不?得?超过\s*(\d+)\s*页",
    r"施工组织设计[^。\n]{0,30}?限[^。\n]{0,10}?(\d+)\s*页",
    # 「控制在 N 页内/内」是西藏模板另一种高频写法(某西藏小体量项目
    # "整体施工组织设计文件的总页数控制在500页内（含500页）" 曾整条漏抽 →
    # 页数硬顶缺失 → 目标页数回落默认值,字数预算与实际篇幅脱节)。
    r"总?页数控制[在]?\s*(\d+)\s*页",
    r"控制在\s*(\d+)\s*页[以内]*",
    r"总?页数(?:不?得?超过|不超过|上限为?)\s*(\d+)\s*页",
]


# 暗标显式问答:西藏前附表固定句式「施工组织设计是否采用“暗标”评审方式： | ☑不采用」。
# 关键词粗扫只要命中"暗标"二字即判 true,会把"不采用"误判为采用。
_RE_BLIND_ASK = re.compile(
    r"暗标[^。\n]{0,30}?[：:]\s*[|｜]?\s*[☑√■☐□✔]?\s*(不采用|采用|否|是)"
)
_RE_BLIND_NONE = re.compile(r"是否有暗标评审[^。\n]{0,5}?[：:]\s*无")

# 规模档 → 默认目标页数(与 bid_plan_synthesize.SIZE_TO_PAGES 同源口径)。
# 用于把"招标页数硬顶"与"目标页数"解耦:硬顶只是上限。
_SIZE_DEFAULT_PAGES = {
    "small": 35, "small_medium": 70, "medium": 120,
    "large": 220, "extra_large": 260, "unknown": 70,
}
PATTERNS_MARGIN = [
    r"页边距[上下左右]?\s*(?:采用|为)?\s*([\d.]+)\s*厘米",
    r"边距[^。\n]{0,15}([\d.]+)\s*(?:cm|厘米)",
    # 分项/非对称:上X下X左X右X(取四值均值→统一边距;逐边不支持时至少不丢硬约束)
    r"上[\s:：]*([\d.]+)[\s厘米cm]*下[\s:：]*([\d.]+)[\s厘米cm]*左[\s:：]*([\d.]+)[\s厘米cm]*右[\s:：]*([\d.]+)",
]
PATTERNS_INDENT_CHAR = [
    r"首行缩进[，,]?\s*度量采用\s*(\d+)\s*字符",
    r"首行缩进\s*(\d+)\s*字符",
]
PATTERNS_TABLE_FONT = [
    r"表格(?:或图表)?[内中]?(?:为|用|采用)?\s*(?:宋体|仿宋|楷体|黑体)?\s*(六号|小五|五号|小四|四号)\s*字?",
    r"表格.{0,10}字号.{0,5}(六号|小五|五号|小四|四号)",
]
PATTERNS_COLOR_RESTRICTION = [
    r"不?得?出现\s*彩色",
    r"整体文件.{0,10}不?得?\s*彩色",
    r"不?使?用\s*彩色",
    r"全文\s*黑白",
]
PATTERNS_LINE_WEIGHT = [
    # 非贪婪 + 明确数字边界
    r"线条[^。\n]{0,10}?(\d+(?:\.\d+)?)\s*磅",
]
PATTERNS_LANDSCAPE = [
    # 只匹配纯中文图名(避免括号/英文/数字/空格污染)
    r"([一-龥]{2,8}图(?:[、,]\s*[一-龥]{2,8}图)*?)\s*横向(?:编辑|排版|页)",
    r"([一-龥]{2,8}图)\s*横向",
]


def extract_authoring_format_spec(text: str, log, project_size: str = "unknown") -> dict:
    """为 authoring_rules.md 抽取扩展格式字段（带 source 标注）。

    project_size: bid_derive.size_class 的输出 (small/small_medium/medium/large/extra_large/unknown)
        驱动 page_hard_cap 的选档策略 — 小档优先匹"中小型/小型 N 页",大档优先匹"大型/中型 N 页"。
    返回 {field: {value, source}}; source = "tender:pXX" 或 "default"。
    """
    out: dict = {}

    # 页数硬顶（致命级）- 按 project_size 选档
    if project_size in ("small", "small_medium"):
        size_patterns = PATTERNS_PAGE_HARD_CAP_SMALL
    elif project_size in ("medium", "large", "extra_large"):
        size_patterns = PATTERNS_PAGE_HARD_CAP_LARGE
    else:
        # unknown:保守按小档优先(70 < 100,小档更安全)
        size_patterns = PATTERNS_PAGE_HARD_CAP_SMALL
    for pat in (*size_patterns, *PATTERNS_PAGE_HARD_CAP_GENERIC):
        m = re.search(pat, text)
        if m:
            v = _normalize_num(m.group(1))
            if v and 30 <= v <= 500:
                out["page_hard_cap"] = {"value": v, "source": f"tender_regex({project_size})"}
                break

    # 页边距
    margin_matches = []
    margin_sided_done = False  # 分项命中即定,跳过单值兜底(但不得 return——后面还有
    # 首行缩进/表格字号/color_allowed/线宽/横页等致命字段要抽,早期 return 会把它们全丢掉)
    for pat in PATTERNS_MARGIN:
        if margin_sided_done:
            break
        for m in re.finditer(pat, text[:80000]):
            # 分项规格(上/下/左/右四捕获组)→逐边值;单值→统一
            if m.lastindex and m.lastindex >= 4:
                try:
                    _sides = {k: _normalize_num(m.group(g), as_float=True)
                              for g, k in zip(range(1, 5), ("top", "bottom", "left", "right"))}
                    if all(v and 1.0 <= v <= 5.0 for v in _sides.values()):
                        out["margin_cm"] = {"value": _sides, "source": "tender_regex"}
                        margin_sided_done = True
                        break
                except (TypeError, ValueError):
                    pass
                continue
            v = _normalize_num(m.group(1), as_float=True)
            if v and 1.0 <= v <= 5.0:
                margin_matches.append(v)
    if margin_matches and not margin_sided_done:
        # 取最频繁的
        from collections import Counter
        most = Counter(margin_matches).most_common(1)[0][0]
        out["margin_cm"] = {"value": {"top": most, "bottom": most, "left": most, "right": most},
                            "source": "tender_regex"}

    # 首行缩进（字符）
    for pat in PATTERNS_INDENT_CHAR:
        m = re.search(pat, text[:80000])
        if m:
            v = _normalize_num(m.group(1))
            if v and 1 <= v <= 4:
                out["first_line_indent"] = {"value": {"unit": "char", "chars": v},
                                            "source": "tender_regex"}
                break

    # 表格字号
    for pat in PATTERNS_TABLE_FONT:
        m = re.search(pat, text[:80000])
        if m:
            name = m.group(1)
            pt = FONT_NAME_TO_PT.get(name)
            if pt:
                out["table_font"] = {"value": {"size_name": name, "size_pt": pt},
                                     "source": "tender_regex"}
                break

    # 不得彩色
    for pat in PATTERNS_COLOR_RESTRICTION:
        if re.search(pat, text[:80000]):
            out["color_allowed"] = {"value": False, "source": "tender_regex"}
            break

    # 线条粗细
    for pat in PATTERNS_LINE_WEIGHT:
        m = re.search(pat, text[:80000])
        if m:
            v = _normalize_num(m.group(1), as_float=True)
            if v and 0.1 <= v <= 5.0:
                out["line_weight_pt"] = {"value": v, "source": "tender_regex"}
                break

    # 横向页特例
    landscape_pages = set()
    for pat in PATTERNS_LANDSCAPE:
        for m in re.finditer(pat, text[:80000]):
            raw = m.group(1) if m.lastindex else m.group(0)
            # 切分多个图名
            for part in re.split(r"[、,，]", raw):
                p = part.strip()
                if p and p.endswith("图") and len(p) <= 12:
                    landscape_pages.add(p)
    if landscape_pages:
        out["landscape_pages"] = {"value": sorted(landscape_pages), "source": "tender_regex"}

    if log:
        log.info(f"  authoring 扩展格式抽取: {len(out)} 字段命中 {list(out.keys())}")
    return out


# ─── work_structure / key_personnel 抽取 ────────────────────────────────

PATTERNS_SUBPROJECT_AREA = [
    # 形如"新建员工宿舍 893.64㎡"
    r"(?:新建|建设|拟建|改造|扩建)?\s*([一-龥]{2,16}?)\s*([\d,]+(?:\.\d+)?)\s*(?:㎡|平方米|m²|m2)",
]
PATTERNS_PROJECT_MANAGER = [
    r"项目经理[^。\n]{0,30}?(一级|二级)建造师",
    r"项目经理[^。\n]{0,40}?建[筑安造]\s*([A-Z])\s*证",
]
PATTERNS_SAFETY_C_CERT = [
    r"安全员[^。\n]{0,30}?建[安筑]\s*([A-Z])\s*证",
]


def extract_work_structure(project_scope: str) -> list:
    """从 project_scope 文本切分子工程清单（含面积）。"""
    out = []
    if not project_scope:
        return out
    seen = set()
    for pat in PATTERNS_SUBPROJECT_AREA:
        for m in re.finditer(pat, project_scope):
            name = m.group(1).strip()
            area = _normalize_num(m.group(2), as_float=True)
            # 简单过滤无效名称
            if not name or len(name) < 2 or area is None or area < 1:
                continue
            if name in seen:
                continue
            # 剔除常见前导词(仅整词命中才剥,防"建筑装饰工程"→"筑装饰工程")
            for stop in ["拟建", "新建", "改建", "扩建", "改造"]:
                if name.startswith(stop):
                    name = name[len(stop):]
                    break
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"sub_project": name, "area_m2": area})
    return out


def extract_work_structure_from_boq(boq: dict | None, max_categories: int = 8,
                                    max_items_per_category: int = 12) -> list:
    """从 boq.json 派生 work_structure（清单类目 → 子工程，条目 → 工程量明细）。

    boq.json: {categories: [{name, items: [{name, qty, unit, code}]}]}
    输出条目: {sub_project, nature, boq_items: [{name, qty, unit}]}
    与 extract_work_structure(面积型) 互补:前者是「单体建筑 + 面积」,
    后者是「分部分项类目 + 真实工程量明细」,是 writer Block1「清单驱动」的实料。
    """
    # 数组形状防御:boq.json 顶层可能是 list(旧提取器),非 dict 时 get 会 AttributeError
    if not isinstance(boq, dict):
        return []
    if not boq or not boq.get("categories"):
        return []
    out = []
    for cat in boq["categories"][:max_categories]:
        if not isinstance(cat, dict):  # 脏 boq(类目写成字符串)→ 跳过,不让 AttributeError 掀翻 Step 2
            continue
        name = (cat.get("name") or "").strip()
        items = cat.get("items") or []
        if not name or not items:
            continue
        boq_items = []
        for it in items[:max_items_per_category]:
            if not isinstance(it, dict):
                continue
            it_name = (it.get("name") or "").strip()
            qty = it.get("qty")
            unit = it.get("unit") or ""
            if not it_name:
                continue
            boq_items.append({"name": it_name, "qty": qty, "unit": unit})
        if not boq_items:
            continue
        out.append({
            "sub_project": name,
            "nature": "清单类目",
            "boq_items": boq_items,
        })
    return out


def extract_response_keywords(disq_data: dict, facts: dict) -> list:
    """从废标条款+工期/质量硬数据挖「关键响应词」(authoring_rules ## 10 段的生产者)。

    产出 [{label, keyword, chapter_hint}]:
    - 工期: facts.duration_days → "N 日历天"(进度计划章)
    - 质量目标: facts.quality_target → 合格/合格率(质量管理章)
    - 废标条款里的「必须响应/承诺/包含」短语(章节提示默认相关章)
    """
    out = []
    # facts 的值形如 {"value":…,"source":…};不脱包会生成
    # "{'value': 450, ...} 日历天" 这类垃圾串进 writer 的关键响应词块。
    dur = _val(facts, "duration_days")
    if dur:
        out.append({"label": "工期响应", "keyword": f"{dur} 日历天", "chapter_hint": "进度计划"})
    qt = _val(facts, "quality_target")
    if qt and 2 <= len(str(qt)) <= 20:
        out.append({"label": "质量目标", "keyword": str(qt).strip(), "chapter_hint": "质量管理"})
    blob = "\n".join(
        (c.get("text_original", "") or "") + "\n" + (c.get("text", "") or "")
        for c in (disq_data.get("clauses") or [])
    )
    for m in re.finditer(
            # 触发词与“响应类动词”不得跨字吞进去(旧写法中步 [^\n。；]{0,12}? 会把
            # 「明确包含：质量目标」的动词部分一并捕入关键词 → authoring §10 注入垃圾词、
            # reviewer 误报 response_keyword_miss,并挤掉 out[:12] 里的真实响应词)
            r"(?:必须|应当|须)\s*(?:明确|完整|详细|逐项|一并)?\s*(?:响应|承诺|包含)\s*[:：]?\s*"
            r"([^\n。；，,、：:]{2,24})",
            blob):
        ph = m.group(1).strip(" 、，,：:")
        _VAGUE = ("以下内容", "以下要求", "相关内容", "上述内容", "相应内容", "如下")
        # 含动词/冒号的碎片不是响应词主体(旧正则残留),丢弃
        if re.match(r"^(明确|包含|响应|承诺|要求|内容)", ph):
            continue
        if (2 <= len(ph) <= 24 and not any(ph == k["keyword"] for k in out)
                and not ph.startswith(_VAGUE)):
            out.append({"label": "废标响应", "keyword": ph, "chapter_hint": ""})
    return out[:12]


def extract_key_personnel(text: str) -> dict:
    """从招标主文档抽关键岗位人员要求。"""
    out = {}
    snippet = text[:80000]

    for pat in PATTERNS_PROJECT_MANAGER:
        m = re.search(pat, snippet)
        if m:
            out["project_manager"] = m.group(0)[:80]
            break

    m = re.search(r"项目经理[^。\n]{0,80}?(?:类似)?(?:项目|业绩)[^。\n]{0,80}", snippet)
    if m:
        out["project_manager_similar"] = m.group(0)[:200]

    m = re.search(r"技术负责人[^。\n]{0,80}", snippet)
    if m:
        out["tech_lead"] = m.group(0)[:160]

    for pat in PATTERNS_SAFETY_C_CERT:
        m = re.search(pat, snippet)
        if m:
            out["safety_officer"] = m.group(0)[:80]
            break

    return out


# ─── 技术性废标筛选 ─────────────────────────────────────────────────────

# 技术性废标关键词:与方案写作强相关
TECHNICAL_RED_LINE_KEYWORDS = [
    "工程量清单", "项目编码", "项目名称", "项目特征",
    "技术规范", "技术标准",
    "雷同", "串通", "实质性要求",
    "施工组织设计", "施组",
    "工程量不一致", "清单不一致",
]


def filter_technical_red_lines(disq_data: dict, max_count: int = 10) -> list:
    """从 90 条废标里筛选与方案写作强相关的 5-10 条。"""
    clauses = disq_data.get("clauses", []) if disq_data else []
    out = []
    for c in clauses:
        t = c.get("text_original", "") or c.get("text", "") or ""
        cid = c.get("id") or c.get("clause_id") or ""
        if any(k in t for k in TECHNICAL_RED_LINE_KEYWORDS):
            out.append({
                "id": cid or "-",
                "rule": t[:200],
                "source_page": c.get("source_page", "?"),
            })
        if len(out) >= max_count:
            break
    return out


# ─── 审计警告收集 ───────────────────────────────────────────────────────

def collect_audit_warnings(ext: Path) -> list:
    """从 skeleton_extract.log + 各 JSON 收集警告信号。"""
    warnings = []
    log_path = ext / "skeleton_extract.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            for ln in lines:
                if "⚠" in ln or "[WARNING]" in ln:
                    msg = ln.split("]", 2)[-1].strip().lstrip("⚠ ").strip()
                    if msg:
                        warnings.append({"source": "skeleton_extract.log", "msg": msg[:200]})
        except Exception:
            pass
    return warnings[:20]  # 限制条数


# ─── 启发式判断 ─────────────────────────────────────────────────────────

def detect_procurement_type(text: str) -> str:
    """招标方式启发式判定。"""
    if "竞争性比选" in text:
        return "竞争性比选(电子标)" if "电子" in text[:5000] else "竞争性比选"
    if "竞争性磋商" in text:
        return "竞争性磋商"
    if "邀请招标" in text:
        return "邀请招标"
    if "单一来源" in text:
        return "单一来源采购"
    return "公开招标"


def detect_blind_bid(disq: dict, text: str) -> tuple[bool, bool, str]:
    """暗标判定。返回 (blind_bid, no_company_info, blind_scope)。"""
    keywords_strict = ["暗标", "暗标评审", "禁止出现公司名", "技术标暗标"]
    text_blind = any(k in text[:30000] for k in keywords_strict)

    disq_clauses = disq.get("clauses", []) if disq else []
    disq_text_all = "".join(
        (c.get("text_original", "") + c.get("text", "")) for c in disq_clauses
    )
    blind_in_disq = "暗标" in disq_text_all

    # 「无公司信息」独立检测:招标仅禁止出现公司信息而无"暗标"字样时,
    # 也要启用公司名/LOGO 扫描(历史风险: 恒 False → needs_blind_scan 禁用 → 泄漏)
    keywords_nocompany = [
        "不得出现公司名称", "不得出现投标人名称", "不得含有公司信息",
        "不得出现任何公司信息", "不得标注公司名称", "不得出现单位名称",
        "不得出现公司信息",
    ]
    no_company = any(k in text[:30000] for k in keywords_nocompany) \
        or any(k in disq_text_all for k in keywords_nocompany)

    _m_ask = _RE_BLIND_ASK.search(text)
    if _m_ask:
        # ★ 前附表明示优先:命中问答即定稿,不再回退关键词粗扫
        if _m_ask.group(1) in ("不采用", "否"):
            return (False, False, "招标文件前附表明示不采用暗标评审")
        return (True, no_company, "招标文件前附表明示采用暗标评审")
    if _RE_BLIND_NONE.search(text):
        return (False, False, "招标文件明示无暗标评审")

    if text_blind or blind_in_disq:
        return (True, no_company, "招标文件规定暗标(已检出关键词)")
    if no_company:
        return (False, True, "招标文件禁止出现公司信息(未明示暗标)")
    return (False, False, "招标文件未规定暗标")


def detect_consortium_subcon(text: str) -> tuple[bool, bool]:
    """联合体 / 分包是否允许。"""
    joint_allowed = True
    subcon_allowed = True
    if re.search(r"不接受联合体|本项目不接受.{0,5}联合体|不允许联合体", text):
        joint_allowed = False
    if re.search(r"不接受.{0,5}分包|本项目不接受合同分包|不允许分包", text):
        subcon_allowed = False
    return (joint_allowed, subcon_allowed)


def lowest_or_score(eval_method: dict) -> str:
    """method_code 推导。"""
    m = eval_method.get("method_chinese", "") or ""
    if "最低" in m:
        return "lowest_price"
    if "综合" in m:
        return "comprehensive"
    if "性价比" in m:
        return "value_ratio"
    return "unknown"


# ─── 渲染 ───────────────────────────────────────────────────────────────

def _yq(value) -> str:
    """YAML 双引号字符串转义——招标原文含 ASCII 双引号时,(如 6" 管 / D=300" 阀)
    裸插值会生成非法 YAML,parse_authoring_yaml 会用 safe_load 静默丢弃整块
    (chapter_anchors 丢了 → 必含大纲锚点失效、writer 无章可依)。
    同时折叠三反引号(会截断 ```yaml 围栏正则)与换行(多行值单独成行会破坏列表缩进)。"""
    s = str(value)
    s = s.replace("```", "`").replace("\r", " ").replace("\n", " ")
    # 反斜杠必须先于双引号转义:YAML 双引号标量里 \t/\n/\u 等是转义序列,
    # 裸反斜杠会生成非法标量 → safe_load 抛错 → parse_authoring_yaml 静默丢弃整块。
    s = s.replace(chr(92), chr(92) + chr(92))
    return s.replace(chr(34), chr(92) + chr(34))


def _attachment_label(att) -> str:
    """把 mandatory_outline.json 的 attachments 项归一化为 "附表一：拟投入本标段的主要施工设备表"。

    下游 bid_compliance 只认字符串(按前 4 个汉字在正文里搜),dict 原样落盘会永远搜不到。
    """
    if isinstance(att, dict):
        label = str(att.get("label", "") or "").strip()
        title = str(att.get("title_original") or att.get("title", "") or "").strip()
        if label and title:
            return f"{label}：{title}"
        return label or title
    return str(att).strip()


def _yaml_block(d: dict, indent: int = 0) -> str:
    """简易 YAML 序列化(只支持本场景的标量+一级嵌套+数组)。"""
    lines = []
    for k, v in d.items():
        prefix = " " * indent
        if isinstance(v, dict):
            # 值统一走 _yaml_scalar:Python str(None) 会写成字符串 "None"(而非 null),
            # 嵌套 dict 里的 None 会被下游当成真字符串消费
            inner = "{" + ", ".join(f"{ik}: {_yaml_scalar(iv)}" for ik, iv in v.items()) + "}"
            lines.append(f"{prefix}{k}: {inner}")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{prefix}{k}: []")
            else:
                lines.append(f"{prefix}{k}:")
                for item in v:
                    # 统一走 _yaml_scalar:旧写法不转义反斜杠也不处理换行,
                    # 「\」+「:」类条目会生成非法 YAML,被 safe_load 静默丢掉整个块
                    # (format/must_include/blind 一起失效)
                    lines.append(f"{prefix}  - {_yaml_scalar(item)}")
        elif isinstance(v, bool):
            lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{prefix}{k}: null")
        elif isinstance(v, str):
            # 转义:引号入串必转义(否则非法 YAML 被 safe_load 静默吞整块);含 :/# 前缀也引。
            # 换行/三反引号用 _yq 折叠——裸 \n 会让整个 YAML 块 ScannerError 被静默丢弃
            # (project/format/density/blind/must_include 全失效)。
            _esc = _yq(v)  # " → \" 且折叠换行 + 三反引号
            # YAML 指示符开头(- ? * & | > ! % @ ` [ ] { } , #)必须加引号,
            # 否则 "- 要点" 被当序列项、"> x" 被当块标量 → safe_load 吞掉整块
            _ind = bool(re.match(r"^[-?*&|>!%@`\[\]{},#]", v))
            # _esc != v.strip() 覆盖前导制表符等全部空白(_yq 只折叠 \n,不处理 \t)
            if (_ind or '"' in v or ":" in v or "#" in v
                    or _esc != v.strip() or _esc != v):
                lines.append(f'{prefix}{k}: "{_esc}"')
            else:
                lines.append(f"{prefix}{k}: {v}")
        else:
            lines.append(f"{prefix}{k}: {v}")
    return "\n".join(lines)


# 多变体反串标排版套预设。bid_variants 给变体分配 A/B/C… ID,
# bid_assemble._apply_format_set 据此套用。只差异化 字体/标题字体/行距/页边距;
# 不含字号——字号若招标强制须三家一致,且改字号大幅影响页数。
_FORMAT_SET_PRESETS: dict[str, dict] = {
    "A": {"description": "宋体正文+黑体标题+1.5倍行距",
          "font_name_cn": "宋体", "font_name_h1": "黑体",
          "line_spacing_rule": "multiple", "line_spacing": 1.5,
          "margin_top_cm": 2.5, "margin_bottom_cm": 2.5,
          "margin_left_cm": 2.8, "margin_right_cm": 2.8},
    "B": {"description": "仿宋正文+宋体标题+固定28磅行距",
          "font_name_cn": "仿宋", "font_name_h1": "宋体",
          "line_spacing_rule": "exact", "line_spacing": 28,
          "margin_top_cm": 2.6, "margin_bottom_cm": 2.6,
          "margin_left_cm": 3.1, "margin_right_cm": 3.1},
    "C": {"description": "楷体正文+黑体标题+1.6倍行距",
          "font_name_cn": "楷体", "font_name_h1": "黑体",
          "line_spacing_rule": "multiple", "line_spacing": 1.6,
          "margin_top_cm": 3.0, "margin_bottom_cm": 2.7,
          "margin_left_cm": 2.6, "margin_right_cm": 2.6},
    "D": {"description": "宋体正文+黑体标题+固定26磅行距",
          "font_name_cn": "宋体", "font_name_h1": "黑体",
          "line_spacing_rule": "exact", "line_spacing": 26,
          "margin_top_cm": 2.7, "margin_bottom_cm": 2.5,
          "margin_left_cm": 3.0, "margin_right_cm": 2.7},
    "E": {"description": "仿宋正文+黑体标题+1.5倍行距",
          "font_name_cn": "仿宋", "font_name_h1": "黑体",
          "line_spacing_rule": "multiple", "line_spacing": 1.5,
          "margin_top_cm": 2.8, "margin_bottom_cm": 2.8,
          "margin_left_cm": 2.7, "margin_right_cm": 2.7},
    "F": {"description": "楷体正文+宋体标题+固定28磅行距",
          "font_name_cn": "楷体", "font_name_h1": "宋体",
          "line_spacing_rule": "exact", "line_spacing": 28,
          "margin_top_cm": 2.5, "margin_bottom_cm": 2.7,
          "margin_left_cm": 2.9, "margin_right_cm": 2.9},
}


def _render_format_sets_block(format_cfg: dict | None = None) -> list[str]:
    """渲染 format_sets yaml 块(供 bid_assemble._parse_format_sets 消费)。

    ★ 强制格式项目(招标锁字体/字号/行距/页边距,典型信号 color_allowed=false):
      生成 A/B/C 三套【字体/行距/页边距完全相同】,仅 KEY 不同 → bid_assemble 据 KEY 给
      三家套不同【封面版式】(cover_style),既差异化封面又不违反招标强制格式。
      (历史坑:默认 6 套不同字体会让三家排版各异 → 直接违反强制格式废标)
    否则(招标未规定格式):沿用 _FORMAT_SET_PRESETS 的 6 套差异化字体。"""
    fc = format_cfg or {}
    mandated = fc.get("color_allowed") is False
    lines = ["## 排版套(format_sets — 多变体反串标差异化)", ""]
    if mandated:
        m = fc.get("margin_cm") or {"top": 2.54, "bottom": 2.54, "left": 2.54, "right": 2.54}
        # ★ 中文字体键名可能是 font_chinese 或 font_name_cn(assemble 口径);
        #   原来只读 font_chinese,而 format_cfg 从未写入该键 → 恒回退硬编码「宋体」,
        #   会覆盖 assemble._extract_h1_style 从规则文本抽出的招标标题字体
        #   (如「标题黑体」)→ 强制标题黑体的项目产出宋体标题(排版废标风险)。
        cn = fc.get("font_chinese") or fc.get("font_name_cn") or "宋体"
        # 招标强制全文统一字体(如「宋体黑色四号字」)时,标题必须跟正文同字体——
        # 历史 bug:H1 落回硬编码「黑体」,而 assemble 把 format_set 当招标规定锁死,
        # 成品章标题全成黑体三号,直接违反排版要求。(黑体只在招标另有标题字体规定时才用)
        h1 = (fc.get("font_name_h1") or fc.get("font_name_h1_cn")
              or fc.get("heading_font") or fc.get("title_font") or cn)
        lsr = fc.get("line_spacing_rule", "multiple")
        lsv = fc.get("line_spacing_value", 1.5)
        lines += [
            "> ★ 招标强制统一格式(color_allowed=false):A/B/C 三套字体/行距/页边距完全相同,",
            "> 仅 KEY 不同 → bid_assemble 据 KEY 套不同封面版式(A居中/B横线/C左对齐落款)。",
            "> 三家排版实测一致(合规),反串标靠内容+图表+封面版式。",
            "",
            "```yaml",
            "format_sets:",
        ]
        descs = {"A": "封面居中式", "B": "封面横线分隔式", "C": "封面项目名突出左对齐落款式"}
        for key in ("A", "B", "C"):
            lines += [
                f"  {key}:",
                f"    description: 招标强制格式({descs[key]})-字体行距页边距三家一致",
                f"    font_name_cn: {cn}",
                f"    font_name_h1: {h1}",
                f"    line_spacing_rule: {lsr}",
                f"    line_spacing: {lsv}",
                f"    margin_top_cm: {m.get('top', 2.54)}",
                f"    margin_bottom_cm: {m.get('bottom', 2.54)}",
                f"    margin_left_cm: {m.get('left', 2.54)}",
                f"    margin_right_cm: {m.get('right', 2.54)}",
            ]
        lines += ["```", ""]
        return lines
    lines += [
        "> bid_variants 给每个变体分配 A/B/C… ID,bid_assemble 据此套用不同字体/行距/页边距。",
        "> ★ 若招标强制规定字体/字号/行距/页边距,删除本块、或把对应字段改成三家一致。",
        "",
        "```yaml",
        "format_sets:",
    ]
    for key, fs in _FORMAT_SET_PRESETS.items():
        lines.append(f"  {key}:")
        for k, v in fs.items():
            lines.append(f"    {k}: {v}")
    lines += ["```", ""]
    return lines


def render_rules_md(project: dict, evaluation: dict, procurement: dict,
                    blind: dict, format_cfg: dict, density: dict,
                    schedule: dict, must_include: dict,
                    disqualification_summary: dict) -> str:
    """渲染 disqualification_rules.md 全文。"""

    parts = [
        "# 废标条款 + 格式参数 + 章节锚点",
        "",
        "## 基本参数",
        "",
        "```yaml",
        "project:",
        _yaml_block(project, indent=2),
        "",
        "evaluation:",
        _yaml_block(evaluation, indent=2),
        "",
        "procurement:",
        _yaml_block(procurement, indent=2),
        "```",
        "",
        "## 暗标设置",
        "",
        "```yaml",
        _yaml_block(blind, indent=0),
        "```",
        "",
        f"> 暗标判定来源:`bid_rules_synthesize.detect_blind_bid()` 关键词扫描。",
        "",
        "## 排版格式硬约束",
        "",
        "```yaml",
        "format:",
        _yaml_block(format_cfg, indent=2),
        "```",
        "",
        "> 招标文件未明示字号/行距 → 按工程类比选惯例(宋体小四+1.5倍)。",
        "",
        *_render_format_sets_block(format_cfg),
        "## 字数密度",
        "",
        "```yaml",
        "density:",
        _yaml_block(density, indent=2),
        "```",
        "",
        "## 否决投标关键节点",
        "",
        f"按 disqualification.json 共 {disqualification_summary.get('total', '?')} 条废标项,自动分类:",
        "",
    ]
    for cat, items in disqualification_summary.get("by_category", {}).items():
        parts.append(f"**{cat}({len(items)} 条)：**")
        for it in items[:5]:
            parts.append(f"- {it[:120]}")
        if len(items) > 5:
            parts.append(f"- ...({len(items) - 5} 条略,详见 brain 第六节)")
        parts.append("")

    parts += [
        "## 关键工期节点",
        "",
        "```yaml",
        "schedule_milestones:",
        _yaml_block(schedule, indent=2),
        "```",
        "",
        "## must_include_sections(章节锚点)",
        "",
        "```yaml",
        "must_include_sections:",
        _yaml_block(must_include, indent=2),
        "```",
        "",
    ]
    return "\n".join(parts).rstrip() + "\n"


def _yaml_scalar(v) -> str:
    """YAML 标量渲染(安全引号):用于 list 元素/嵌套值,避开 Python repr 生成非法 YAML。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        # 嵌套 list 必须递归渲染成 flow sequence;整串 str() 会变成字符串
        # (如 format.landscape_pages 会从真数组退化成 "['平面布置图', ...]")
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_yaml_scalar(x)}" for k, x in v.items()) + "}"
    s = str(v)
    # YAML 指示符开头的标量必须加引号,否则 "- 要点" 会被当序列项 →
    # "sequence entries are not allowed here" 直接让 safe_load 吞掉整块
    _starts_indicator = bool(re.match(r"^[-?*&|>!%@`\[\]{},#]", s))
    if (_starts_indicator or s != s.strip()
            or any(c in s for c in ('"', "\n", "\t", "`", ":", ",", "[", "]", "{", "}", "#", "'"))):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _format_yaml_with_source(field: str, value, source: str = "default", indent: int = 2) -> list[str]:
    """渲染 `field: value` + `field_source: source` 两行(向后兼容老解析器)。

    字符串值含引号/换行/反引号时转义并加引号,防非法 YAML 被 safe_load 静默吞块。
    """
    prefix = " " * indent
    lines = []
    if isinstance(value, dict):
        # 值走 _yaml_scalar:含 :/" /换行的嵌套值原样插值会生成非法 YAML,
        # 被 safe_load 静默吞掉整块(margin_cm 这类嵌套 dict 是重灾区)
        inner = "{" + ", ".join(f"{k}: {_yaml_scalar(v)}" for k, v in value.items()) + "}"
        lines.append(f"{prefix}{field}: {inner}")
    elif value is None:
        # 不得直接 f"{value}" → YAML 里会写成字面量字符串 "None"
        lines.append(f"{prefix}{field}: null")
    elif isinstance(value, bool):
        lines.append(f"{prefix}{field}: {'true' if value else 'false'}")
    elif isinstance(value, (list, tuple)):
        # 旧写法走 Python repr:元素含单引号时生成非法 YAML,
        # 被 safe_load 静默吞掉整个 format 块(致命级字段全丢)
        inner = ", ".join(_yaml_scalar(v) for v in value)
        lines.append(f"{prefix}{field}: [{inner}]")
    elif isinstance(value, str):
        # ★ 必须先转义反斜杠再转义双引号(顺序颠倒会把转义符本身再转一遍);
        #   漏转反斜杠/制表符会生成非法 YAML → safe_load 静默吞掉整个 format 块
        _esc = (value.replace("\\", "\\\\").replace("\t", "\\t")
                     .replace(chr(34), chr(92) + chr(34)))
        # 与 _yaml_block 同口径:'#' 也需引号——YAML 会把 "x # y" 的 # 之后当注释截断
        if (any(c in value for c in (chr(34), "\n", "\t", "\\", "`", ":", "#"))
                or value.startswith(" ")
                or re.match(r"^[-?*&|>!%@`\[\]{},#]", value)):
            lines.append(f'{prefix}{field}: "{_esc}"')
        else:
            lines.append(f"{prefix}{field}: {value}")
    else:
        lines.append(f"{prefix}{field}: {value}")
    lines.append(f"{prefix}{field}_source: {source}")
    return lines
def render_authoring_rules_md(
    project: dict,
    evaluation: dict,
    blind: dict,
    format_cfg: dict,
    format_sources: dict,
    density: dict,
    must_include: dict,
    chapter_topics: list,
    attachments: list,
    work_structure: list,
    key_personnel: dict,
    project_specific: list,
    technical_red_lines: list,
    audit_warnings: list,
    response_keywords: list | None = None,
) -> str:
    """渲染 authoring_rules.md(技术方案编写规则)。

    与 disqualification_rules.md 共享 format/density/blind/must_include YAML 块
    (向后兼容老消费方),额外增加 6 个方案专用段落。
    """
    parts = [
        "# 技术方案编写规则（authoring_rules）",
        "",
        "> **定位：写施工组织设计时直接消费的规则集**(writer/assemble/charts 单点数据源)。",
        "> 与 disqualification_rules.md 共享 format/density/blind/must_include YAML 块(向后兼容);",
        "> 额外加 6 个方案专用段：chapter_anchors / required_technical_content / work_structure /",
        "> technical_red_lines / project_specific_constraints / key_personnel。",
        "> 每个 format 字段带 `_source` 标注:tender_regex / default / derived。",
        "",
        "## 1. 项目基本信息",
        "",
        "```yaml",
        "project:",
        _yaml_block(project, indent=2),
        "",
        "evaluation:",
        _yaml_block(evaluation, indent=2),
        "```",
        "",
        "## 2. 章节锚点（合格制 1:1 复制大纲，禁止合并/省略）",
        "",
        "```yaml",
        f"evaluation_mode: {evaluation.get('evaluation_method', 'passfail')}",
        f"total_tech_score: {evaluation.get('total_tech_score', 0)}",
        "enforcement: " + ('"缺一项即不响应实质性要求 → 废标"' if evaluation.get('evaluation_method') == 'passfail' else '"按评分表打分"'),
        f"source: {must_include.get('source', '招标文件大纲条款')}",
        "",
        "chapter_anchors:",
    ]
    for topic in chapter_topics:
        # index 缺失时不得裸插 "?"(YAML 里 ? 是指示符 → {n: ?, ...} 非法,整块被丢弃)
        n = topic.get("index")
        n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else "null"
        title = _yq(topic.get("title_original") or "")
        subs = topic.get("subsections_original", []) or []
        subs_yaml = "[" + ", ".join(f'"{_yq(s)}"' for s in subs) + "]" if subs else "[]"
        parts.append(f'  - {{n: {n}, title: "{title}", required_subsections: {subs_yaml}}}')
    parts.append("")
    parts.append("required_attachments:")
    for att in attachments:
        if isinstance(att, dict):
            label = _yq(att.get("label") or "")
            t = _yq(att.get("title_original") or att.get("title") or "")
            parts.append(f'  - {{label: "{label}", title: "{t}"}}')
        else:
            parts.append(f'  - "{_yq(att)}"')
    parts.append("```")
    parts.append("")

    # 格式硬约束（每字段带 source）
    parts += [
        "## 3. 格式硬约束",
        "",
        "> ★ 致命级字段(page_hard_cap / color_allowed)不得使用 default 值;",
        "> 必须从招标原文抽到或人工填充,否则触发 B1.17 / 排版废标风险。",
        "",
        "```yaml",
        "format:",
    ]
    # 按字段渲染 + source
    for key, val in format_cfg.items():
        if key.endswith("_source"):
            continue
        src = format_sources.get(key, "default")
        parts.extend(_format_yaml_with_source(key, val, src, indent=2))
    parts.append("```")
    parts.append("")

    # authoring_constraints 从 bid_common 单一真值源派生(避免硬编码)
    try:
        from bid_common import render_output_format_constraints_lines
        constraint_lines = render_output_format_constraints_lines(is_subitem=False)
    except Exception:
        constraint_lines = [
            "装饰字符 【】★◆■●→ 禁用",
            "Markdown 斜体禁用，强调用 **粗体**",
            "Mermaid 代码块禁用",
        ]
    parts += [
        "## 4. 暗标禁忌",
        "",
        "```yaml",
        "blind:",
        _yaml_block(blind, indent=2),
        "  authoring_constraints:",
    ]
    for cl in constraint_lines:
        # ★ 约束行一律作为「引号字符串」进 YAML:
        #   - 三反引号(```json```)会截断 parse_authoring_yaml 的 ```yaml 围栏正则;
        #   - `  - `schedule`：...` 这类反引号起头行若裸进 YAML 列表,反引号是 YAML
        #     保留指示符,整块 blind 会被 yaml.safe_load 丢弃(blind_bid 也一并丢)。
        #   因此:折叠三反引号 + 全量加引号,去掉裸列表项分支。
        esc = cl.strip().replace(chr(34), chr(92) + chr(34)).replace("```", "`")
        parts.append(f'    - "{esc}"')
    parts += [
        "```",
        "",
        "## 5. 技术性废标雷区（从废标条款精选与方案写作强相关的）",
        "",
        "```yaml",
        "technical_red_lines:",
    ]
    if not technical_red_lines:
        parts.append("  []")
    else:
        for rl in technical_red_lines:
            parts.append(f'  - id: "{_yq(rl.get("id", "-"))}"' )
            parts.append(f'    rule: "{_yq(rl.get("rule", ""))}"' )
            _sp = rl.get("source_page")
            parts.append(f'    source_page: {_sp if isinstance(_sp, (int, float)) and not isinstance(_sp, bool) else "null"}')
    parts.append("```")
    parts.append("")

    parts += [
        "## 6. 工程构成对应表（清单驱动方案展开）",
        "",
        "```yaml",
        "work_structure:",
    ]
    if not work_structure:
        parts.append("  []")
        # ★ 告警必须在 ```yaml 围栏【外】:围栏内写 "> ⚠…" 会让 safe_load 报错,
        #   整块(含其它字段)被 parse_authoring_yaml 静默丢弃
        _ws_warn = ["",
                    "> ⚠ 工程构成自动派生失败(boq.json 缺失或类目为空)——writer 将退回招标范围摘录作锚点；",
                    "> 建议人工填充每个子工程的 nature / method_chapters_required,或先修复 Step 1 清单提取。",
                    "> 历史统计:56% 项目因此失去清单锚点,管理类章节 P/G 掉到 0.5 以下。"]
    else:
        _ws_warn = []
        for w in work_structure:
            parts.append(f'  - sub_project: "{_yq(w.get("sub_project", ""))}"')
            if w.get("area_m2"):
                parts.append(f'    area_m2: {w.get("area_m2", 0)}')
            if w.get("unit"):
                parts.append(f'    unit: "{_yq(w["unit"])}"')
            if w.get("nature"):
                parts.append(f'    nature: "{_yq(w["nature"])}"')
            if w.get("method_chapters_required"):
                parts.append(f'    method_chapters_required: {w["method_chapters_required"]}')
            boq_items = w.get("boq_items") or []
            if boq_items:
                parts.append("    boq_items:")
                for it in boq_items:
                    name = _yq(it.get("name", ""))
                    unit = _yq(it.get("unit", ""))
                    qty = it.get("qty")
                    qty_str = "null" if qty is None else str(qty)
                    parts.append(f'      - {{name: "{name}", qty: {qty_str}, unit: "{unit}"}}')
    parts.append("```")
    parts += _ws_warn
    parts.append("")

    parts += [
        "## 7. 项目特定约束（高原/季节/资金/合同模式等）",
        "",
        "```yaml",
        "project_specific_constraints:",
    ]
    if not project_specific:
        parts.append("  []")
    else:
        for c in project_specific:
            parts.append(f'  - constraint: "{_yq(c.get("constraint", ""))}"')
            chapters = c.get("chapters_affected", [])
            parts.append(f"    chapters_affected: {chapters}")
            if c.get("must_address"):
                parts.append("    must_address:")
                for ma in c["must_address"]:
                    parts.append(f'      - "{_yq(ma)}"')
            if c.get("source"):
                parts.append(f'    source: "{_yq(c["source"])}"')
    parts.append("```")
    parts.append("")

    parts += [
        "## 8. 关键岗位人员配备",
        "",
        "```yaml",
        "key_personnel:",
    ]
    if not key_personnel:
        parts.append("  {}")
        # 同上:告警放围栏外
        _kp_warn = ["", "> 人员要求自动派生失败,请人工填充 project_manager / tech_lead / safety_officer。"]
    else:
        _kp_warn = []
        for k, v in key_personnel.items():
            parts.append(f'  {k}: "{_yq(v)}"')
    parts.append("```")
    parts += _kp_warn
    parts.append("")

    parts += [
        "## 9. 字数密度",
        "",
        "```yaml",
        "density:",
        _yaml_block(density, indent=2),
        "```",
        "",
    ]

    # ── 关键响应段(v3 新增生产者:writer _RESPONSE_KEYWORDS / reviewer required_response
    #    依赖本段;历史缺陷:全库无生产者 → 两端检查恒空转)──
    parts += [
        "## 10. 关键响应点（正文必须出现,缺失视为未响应）",
        "",
    ]
    if response_keywords:
        for rk in response_keywords:
            hint = f" —— 必须在{rk.get('chapter_hint','相关')}章明确响应" if rk.get("chapter_hint") else ""
            parts.append(f"- {rk.get('label','响应')}：{rk['keyword']}{hint}")
    else:
        parts.append("- （未从废标条款挖到强制响应短语;compliance 的正则式响应词检查仍生效）")
    parts.append("")

    parts += [
        "## must_include_sections（向后兼容字段，与 chapter_anchors 等价）",
        "",
        "```yaml",
        "must_include_sections:",
        _yaml_block(must_include, indent=2),
        "```",
        "",
    ]

    if audit_warnings:
        parts += [
            "## 审计警告（待人工核查）",
            "",
            "```yaml",
            "audit_warnings:",
        ]
        for w in audit_warnings:
            parts.append(f'  - source: "{w.get("source", "")}"')
            parts.append(f'    msg: "{w.get("msg", "")}"')
        parts.append("```")
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append("**消费方：** writer/assemble/charts 应优先读本文件;compliance 仍读 disqualification_rules.md。")
    parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def categorize_disqualification(disq: dict) -> dict:
    """废标条款分类(资格/符合/报价/程序)。"""
    clauses = disq.get("clauses", []) if disq else []
    by_cat = {"A 类(资格性)": [], "B 类(符合性)": [], "C 类(报价类)": [], "D 类(程序性)": []}
    KW_A = ["资格", "营业执照", "资质", "建造师", "授权委托", "法定代表"]
    KW_B = ["签字", "盖章", "完整性", "联合体", "分包", "实质性响应", "符合性"]
    KW_C = ["报价", "限价", "暂定金额", "金额", "单价", "千元", "万元"]
    for c in clauses:
        t = c.get("text_original", "") or c.get("text", "") or ""
        cat = c.get("category", "")
        if any(k in t for k in KW_A) or cat == "qualification":
            by_cat["A 类(资格性)"].append(t)
        elif any(k in t for k in KW_C) or cat == "price":
            by_cat["C 类(报价类)"].append(t)
        elif any(k in t for k in KW_B):
            by_cat["B 类(符合性)"].append(t)
        else:
            by_cat["D 类(程序性)"].append(t)
    return {"total": len(clauses), "by_category": by_cat}


# ─── 主入口 ─────────────────────────────────────────────────────────────

def _val(facts: dict, key: str, default=None):
    v = facts.get(key)
    if isinstance(v, dict):
        return v.get("value", default)
    return v if v is not None else default


def synthesize(tender_dir: Path, force: bool = False, log=None) -> int:
    ext = tender_dir / "extracted"
    _skip_disq_rewrite = False
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    out_path = ext / "disqualification_rules.md"
    auth_out = ext / "authoring_rules.md"
    # 幂等短路条件:两份规则文件都已存在。仅 disq 存在而 authoring 缺失(首次
    # authoring 阶段失败)时必须继续跑,否则 authoring_rules.md 永远不再生成,
    # 下游静默退化为只用 disq 文件
    if out_path.exists() and auth_out.exists() and not force:
        log.info(f"  规则文件已存在(disq+authoring),跳过(幂等;需重建加 --force)")
        return 0
    if out_path.exists() and not force and not auth_out.exists():
        log.info(f"  disq 已存在但 authoring_rules.md 缺失——仅补生成 authoring 部分")
        _skip_disq_rewrite = True  # 不覆盖(可能手工修正过的)disq,只走 authoring

    if not (ext / "project_facts.json").exists():
        print(f"project_facts.json 不存在(先跑 facts 步)", file=sys.stderr)
        return 2
    try:
        facts_doc = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"project_facts.json 解析失败: {e}", file=sys.stderr)
        return 2
    facts = facts_doc.get("facts", {})

    def _load_json_or(path: Path, default):
        """上游降级(extract/skeleton skip)时三份骨架 JSON 可能缺失:缺失→默认值+警告,
        不让 FileNotFoundError 崩掉整步。损坏/顶层非 dict(旧提取器写过数组)同样降级——
        否则下面 eval_data.get(...) 抛未捕获的 AttributeError/JSONDecodeError。"""
        if not path.exists():
            log.warning(f"  {path.name} 不存在(上游降级?),该块按空数据处理")
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"  {path.name} 解析失败({e}),该块按空数据处理")
            return default
        if not isinstance(data, dict):
            log.warning(f"  {path.name} 顶层非对象(实际 {type(data).__name__}),该块按空数据处理")
            return default
        return data

    eval_data = _load_json_or(ext / "evaluation_method.json", {})
    outline_data = _load_json_or(ext / "mandatory_outline.json", {"topics": [], "attachments": []})
    disq_data = _load_json_or(ext / "disqualification.json", {"clauses": []})

    main_txt = pick_main_tender_text(ext)
    main_text = main_txt.read_text(encoding="utf-8") if main_txt else ""

    # 0. 抽字号/行距硬约束(招标规定 > 默认)
    fmt_spec = extract_format_spec(main_text)
    if fmt_spec:
        log.info(f"  招标格式规定抽取: {fmt_spec}")

    # 1. 抽 rules 专用字段(正则 + LLM 兜底)
    rule_facts = regex_extract_rule_facts(main_text)
    target_keys = ["safety_fee_cny", "bid_bond_cny", "bid_validity_days",
                   "performance_bond_ratio", "defect_period_years"]
    missing = [k for k in target_keys if k not in rule_facts]
    if missing:
        log.info(f"  rules 字段正则命中 {len(target_keys) - len(missing)}/{len(target_keys)},缺 {len(missing)} 用 LLM 兜底")
        llm_extra = llm_fallback_rule_facts(main_text, missing, log)
        rule_facts.update(llm_extra)
    log.info(f"  rules 字段最终命中 {len(rule_facts)}/{len(target_keys)}")

    # 2. 组装各 YAML 块
    project = {
        "name": _val(facts, "project_name", ""),
        "code": _val(facts, "project_code", ""),
        "client": _val(facts, "client", ""),
        "agent": _val(facts, "agent", ""),
        "location": _val(facts, "location", ""),
        "control_price_cny": _val(facts, "control_price"),
        "safety_fee_cny": rule_facts.get("safety_fee_cny"),
        "bid_bond_cny": rule_facts.get("bid_bond_cny"),
        "duration_days": _val(facts, "duration_days"),
        "bid_validity_days": rule_facts.get("bid_validity_days", 90),
        "performance_bond_ratio": rule_facts.get("performance_bond_ratio", 0.10),
        "defect_period_years": rule_facts.get("defect_period_years", 1),
        "quality_target": _val(facts, "quality_target", ""),
    }

    evaluation = {
        "method": eval_data.get("method_chinese", ""),
        "method_code": lowest_or_score(eval_data),
        "evaluation_method": eval_data.get("evaluation_method", "passfail"),
        "total_tech_score": eval_data.get("total_tech_score", 0),
    }

    joint, subcon = detect_consortium_subcon(main_text)
    procurement = {
        "type": detect_procurement_type(main_text),
        "joint_bidding_allowed": joint,
        "subcontracting_allowed": subcon,
        "consortium_allowed": joint,
    }

    bb, no_ci, scope = detect_blind_bid(disq_data, main_text)
    blind = {
        "blind_bid": bb,
        "no_company_info": no_ci,
        "blind_scope": scope,
    }

    # 用招标抽到的字号/行距覆盖默认值,driver format + density 一致
    format_cfg = dict(DEFAULT_FORMAT)
    density = dict(DEFAULT_DENSITY)
    if fmt_spec.get("font_size_pt"):
        format_cfg["body_size_pt"] = fmt_spec["font_size_pt"]
        density["font_size_pt"] = fmt_spec["font_size_pt"]
    if fmt_spec.get("font_size_name"):
        format_cfg["body_size_name"] = fmt_spec["font_size_name"]
    if fmt_spec.get("line_spacing_value"):
        format_cfg["line_spacing_rule"] = fmt_spec.get("line_spacing_rule", "multiple")
        format_cfg["line_spacing_value"] = fmt_spec["line_spacing_value"]
        density["line_spacing"] = f"{fmt_spec['line_spacing_value']}倍" if fmt_spec.get("line_spacing_rule") != "fixed" else "固定"

    # 根据真实字号查表更新 chars_per_page
    try:
        from bid_derive import chars_per_page, recommend_target_chars
        _ls_kind = str(format_cfg.get("line_spacing_rule", "multiple"))
        _ls_val = float(format_cfg.get("line_spacing_value", 1.5))
        if _ls_kind == "fixed":
            # 固定磅值在密度表里是独立档(如 16pt/固定 28 磅 → 700-800 字/页)。
            # 早期把 28 磅换算成"等效倍数"去查 multiple 档,16pt 会落到 1.5 倍档
            # (450-580),字数预算低估约 31%。改为原样查表;表里没这个字号时
            # 由 chars_per_page 自身的同字号/同 kind 退化匹配兜底。
            _ls_kind = "fixed_pt"
        lo, hi = chars_per_page(
            float(density["font_size_pt"]),
            _ls_kind,
            _ls_val,
        )
        density["chars_per_page_min"] = lo
        density["chars_per_page_max"] = hi
        density["chars_per_page_mid"] = (lo + hi) // 2
        rec = recommend_target_chars(
            target_pages=35,
            font_size_pt=float(density["font_size_pt"]),
            line_spacing_kind=_ls_kind,
            line_spacing_value=_ls_val,
            figure_ratio=density["figure_ratio_estimate"],
        )
        density["target_total_chars"] = rec.get("target_chars_mid", 23400)
        density["target_total_pages"] = 35
        density["max_safe_chars"] = rec.get("target_chars_hi", 24700)
    except Exception as e:
        log.warning(f"  字数密度查表失败: {e}")
        density["target_total_chars"] = 23400
        density["target_total_pages"] = 35
        density["max_safe_chars"] = 24700

    schedule = {
        "total_duration_days": _val(facts, "duration_days", 0),
        "defect_warranty_years": rule_facts.get("defect_period_years", 1),
    }

    outline_topics = outline_data.get("topics", [])
    must_include = {
        "source": outline_data.get("source_chapter", "招标文件大纲条款"),
        "enforcement": "实质性响应要求,缺一即视为不响应实质性要求" if evaluation["evaluation_method"] == "passfail" else "评分项,按评分表打分",
        "sections": [t.get("title_original", "") for t in outline_topics if t.get("title_original")],
        # attachments 在 mandatory_outline.json 里是 [{'label','title_original','source_page'}];
        # 这里归一化为 "附表一：拟投入本标段的主要施工设备表" 字符串——
        # 下游 bid_compliance.check_outline_coverage 按前 4 个汉字在正文里做包含搜索,
        # 旧写法落成 str(dict) 后该核查永远搜不到(全表误报 "附表未在章节中引用").
        "required_attachments": [_attachment_label(a) for a in outline_data.get("attachments", [])],
    }

    # ★ 致命格式字段(page_hard_cap/color_allowed/margin/table_font/line_weight/landscape)
    # 必须在渲染 disqualification_rules.md 之前覆盖进 format_cfg——compliance/assemble 读此文件,
    # 若沿用默认(margin2.5/无color_allowed/table11)→图表不黑白、超页不拦、页边距错。
    # (历史 bug:此覆盖原只在后面的 authoring_rules 块做,disqualification 拿到的是默认值)
    proj_size = "unknown"
    ext_fmt = {}
    format_sources_map = {}
    try:
        from bid_derive import size_class as _size_class
        # facts 实际键名为 control_price(见 1187 行),错用 control_price_cny 会导致
        # 控制价恒 0 → size_class 恒 unknown → 页数硬顶选档策略失效
        cp = _val(facts, "control_price", 0)
        try:
            cp_f = float(cp)
        except (TypeError, ValueError):
            cp_f = 0.0
        proj_size = _size_class(cp_f if cp_f > 0 else None)
        log.info(f"  项目规模判档: {proj_size} (控制价 {cp_f:,.0f} 元)")
        ext_fmt = extract_authoring_format_spec(main_text, log, project_size=proj_size)
    except Exception as e:
        log.warning(f"  扩展格式字段抽取失败,沿用默认: {e}")
    # 字段名别名:extract_format_spec 写的是 font_size_* 而 format_cfg 用 body_size_*,
    # 不做映射会把已抽取字段误标 default(audit 告警失真)
    _FMT_KEY_ALIAS = {"body_size_name": "font_size_name", "body_size_pt": "font_size_pt"}
    for _k in format_cfg.keys():
        _hit = bool(fmt_spec) and (
            fmt_spec.get(_k) is not None
            or fmt_spec.get(_FMT_KEY_ALIAS.get(_k, _k)) is not None)
        format_sources_map[_k] = ("tender_regex" if (_k in (
            "body_size_pt", "body_size_name", "line_spacing_value", "line_spacing_rule") and _hit)
            else "default")
    if "page_hard_cap" in ext_fmt:
        format_cfg["page_hard_cap"] = ext_fmt["page_hard_cap"]["value"]
        format_sources_map["page_hard_cap"] = ext_fmt["page_hard_cap"]["source"]
        # ★ 硬顶是"上限"不是"目标":旧写法直接 target=cap,遇到宽松硬顶
        #   (如某西藏小体量项目"总页数控制在500页内" + 小额项目)会把目标
        #   页数/字数预算放大到 500 页 / 40 万字。改为 min(规模档默认值, cap):
        #   紧硬顶(70/100)时与原行为一致,宽松硬顶时保持规模档位不发散。
        _size_default = _SIZE_DEFAULT_PAGES.get(proj_size, 70)
        _target_pages = min(int(ext_fmt["page_hard_cap"]["value"]), _size_default)
        density["target_total_pages"] = _target_pages
        # 页数硬顶联动重算字数预算(历史缺陷:只覆盖页数不重算字数,
        # 硬顶 25 页会超页、100 页则浪费篇幅——字数与页数脱节)
        try:
            _rec = recommend_target_chars(
                target_pages=_target_pages,
                font_size_pt=float(density["font_size_pt"]),
                line_spacing_kind=("fixed_pt" if str(format_cfg.get("line_spacing_rule")) == "fixed"
                                   else str(format_cfg.get("line_spacing_rule", "multiple"))),
                line_spacing_value=float(format_cfg.get("line_spacing_value", 1.5)),
                figure_ratio=density["figure_ratio_estimate"],
            )
            density["target_total_chars"] = _rec.get("target_chars_mid", density["target_total_chars"])
            density["max_safe_chars"] = _rec.get("target_chars_hi", density["max_safe_chars"])
            log.info(f"  页数硬顶联动: {ext_fmt['page_hard_cap']['value']} 页硬顶 → "
                     f"目标 {_target_pages} 页 / {density['target_total_chars']:,} 字 / "
                     f"安全上限 {density['max_safe_chars']:,} 字")
        except Exception as e:  # noqa: BLE001
            log.warning(f"  页数硬顶联动重算失败(沿用 35 页基准): {e}")
    if "color_allowed" in ext_fmt:
        format_cfg["color_allowed"] = ext_fmt["color_allowed"]["value"]
        format_cfg["no_color"] = not ext_fmt["color_allowed"]["value"]
        format_sources_map["color_allowed"] = ext_fmt["color_allowed"]["source"]
    if "margin_cm" in ext_fmt:
        format_cfg["margin_cm"] = ext_fmt["margin_cm"]["value"]
        format_sources_map["margin_cm"] = ext_fmt["margin_cm"]["source"]
    if "first_line_indent" in ext_fmt:
        fli = ext_fmt["first_line_indent"]["value"]
        chars = fli.get("chars", 2)
        body_pt = format_cfg.get("body_size_pt", 14)
        format_cfg["first_line_indent_chars"] = chars
        format_cfg["paragraph_first_line_indent_cm"] = round(chars * body_pt * 2.54 / 72, 2)
        format_sources_map["first_line_indent_chars"] = ext_fmt["first_line_indent"]["source"]
        format_sources_map["paragraph_first_line_indent_cm"] = ext_fmt["first_line_indent"]["source"]
    if "table_font" in ext_fmt:
        tf = ext_fmt["table_font"]["value"]
        format_cfg["table_font_size_pt"] = tf["size_pt"]
        format_cfg["table_font_size_name"] = tf["size_name"]
        format_sources_map["table_font_size_pt"] = ext_fmt["table_font"]["source"]
    if "line_weight_pt" in ext_fmt:
        format_cfg["line_weight_pt"] = ext_fmt["line_weight_pt"]["value"]
        format_sources_map["line_weight_pt"] = ext_fmt["line_weight_pt"]["source"]
    if "landscape_pages" in ext_fmt:
        format_cfg["landscape_pages"] = ext_fmt["landscape_pages"]["value"]
        format_sources_map["landscape_pages"] = ext_fmt["landscape_pages"]["source"]

    disq_summary = categorize_disqualification(disq_data)

    md = render_rules_md(project, evaluation, procurement, blind,
                         format_cfg, density, schedule, must_include,
                         disq_summary)

    if not _skip_disq_rewrite:
        out_path.write_text(md, encoding="utf-8")
        log.info(f"  ✓ 写入 {out_path.name} ({len(md):,} 字符)")
    else:
        log.info(f"  {out_path.name} 已存在(手工修正过?),本次不覆盖——仅补生成 authoring")

    # ─── Phase 2: 同时输出 authoring_rules.md ────────────────────────
    # try/except 只包外部 IO / 数据解析的边角异常(FileNotFoundError / JSONDecodeError / KeyError)
    # 代码 bug 级异常(TypeError / AttributeError / ValueError)应抛出阻断下游
    auth_path = ext / "authoring_rules.md"
    try:
        # proj_size / ext_fmt / format_sources_map / format_cfg 的致命字段覆盖
        # 已在 disqualification_rules 渲染前统一完成(见上文),此处直接复用,保证两文件一致。

        # 章节锚点 topics (含 subsections_original)
        chapter_topics = outline_data.get("topics", [])
        attachments = outline_data.get("attachments", [])

        # work_structure 从 project_scope(面积型) + boq.json(清单类目型) 合并派生。
        # 历史:仅 regex 切 project_scope 一句话,清单里的分部分项工程量根本没进 writer,
        # 导致 writer Block1「工程构成(清单驱动)」名不副实、施工方案章写不出真实工艺。
        project_scope = _val(facts, "project_scope", "")
        work_structure = extract_work_structure(project_scope)
        boq = None
        boq_path = ext / "boq.json"
        if boq_path.exists():
            try:
                boq = json.loads(boq_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                boq = None
        existing_subs = {w.get("sub_project") for w in work_structure}
        for w in extract_work_structure_from_boq(boq):
            if w.get("sub_project") not in existing_subs:
                work_structure.append(w)

        # key_personnel 从主文档抽
        key_personnel = extract_key_personnel(main_text)

        # 项目特定约束 (从 derived.json)
        derived = {}
        if (ext / "project_derived.json").exists():
            derived = json.loads((ext / "project_derived.json").read_text(encoding="utf-8"))
        project_specific = []
        alt = derived.get("altitude_m")
        if alt and alt >= 2500:
            project_specific.append({
                "constraint": f"高海拔 {alt}m ({derived.get('altitude_class', '')})",
                "chapters_affected": [10, 11, 12],
                "must_address": ["高原医疗保障应急预案", "设备降容使用", "施工时段限制", "材料强度修正"],
                "source": "derived altitude",
            })
        if derived.get("spans_winter"):
            project_specific.append({
                "constraint": "工期跨冬施工",
                "chapters_affected": [5, 9, 12],
                "must_address": ["冬期施工专项方案", "进度策略调整", "材料保供"],
                "source": "derived spans_winter=true",
            })

        # 技术性废标雷区 (从 90 条精选)
        technical_red_lines = filter_technical_red_lines(disq_data, max_count=8)

        # 审计警告
        audit_warnings = collect_audit_warnings(ext)
        # 加上 format 段 default 占比警告
        default_count = sum(1 for v in format_sources_map.values() if v == "default")
        total_count = len(format_sources_map)
        if total_count and default_count / total_count > 0.5:
            audit_warnings.append({
                "source": "rules_synthesize",
                "msg": f"format 段 {default_count}/{total_count} 字段为 default 值，需人工核查招标原文",
            })
        if format_cfg.get("page_hard_cap") is None or format_sources_map.get("page_hard_cap") == "default":
            audit_warnings.append({
                "source": "rules_synthesize",
                "msg": "page_hard_cap 未从招标抽到，下游 chapter_plan 字数预算可能错。请人工核查页数硬顶。",
            })

        auth_md = render_authoring_rules_md(
            project, evaluation, blind, format_cfg, format_sources_map,
            density, must_include, chapter_topics, attachments,
            work_structure, key_personnel, project_specific,
            technical_red_lines, audit_warnings,
            response_keywords=extract_response_keywords(disq_data, facts),
        )
        auth_path.write_text(auth_md, encoding="utf-8")
        log.info(f"  ✓ 写入 {auth_path.name} ({len(auth_md):,} 字符)")
        log.info(f"    扩展格式字段命中: {list(ext_fmt.keys())}")
        log.info(f"    work_structure: {len(work_structure)} 项 / project_specific: {len(project_specific)} 项 / "
                 f"technical_red_lines: {len(technical_red_lines)} 项")
    except (FileNotFoundError, json.JSONDecodeError, KeyError,
            AttributeError, TypeError, ValueError) as e:
        # 外部数据问题降级但必须显式失败(exit 1)——静默 return 0 会造成 Step 2
        # 假成功、writer 静默降级 legacy prompt(注释第 1429 行的分层语义保留:
        # 这里列出的都是"数据形状"类,真正代码 bug(RuntimeError 等)不在捕获列表)
        log.warning(f"  authoring_rules.md 生成失败 (外部数据问题): {e}")
        import traceback
        log.warning(traceback.format_exc())
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    log = make_script_logger("bid_rules_synthesize", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_rules_synthesize  目录: {tender_dir}")
    code = synthesize(tender_dir, force=args.force, log=log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
