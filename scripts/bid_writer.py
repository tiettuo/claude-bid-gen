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
# 运行时开关，由 main() 根据 --no-polish CLI 参数设置
SKIP_POLISH = False
# --chapter 指定不存在章号标记(空跑防假成功)
_CHAPTER_MISS = False
# 每章内子项并行上限（不超过 per_model_limit × 模型池大小）
SUBITEM_INNER_WORKERS = 4

# 管理制度类章节关键词(单一定义收敛到 bid_common)
from bid_common import get_config_dir  # noqa: E402
from bid_common import MGMT_CHAPTER_KEYWORDS as _MGMT_CHAPTER_KEYWORDS


# ─── 图表规格（chart_specs.json，可选） ────────────────────────────────
# 由 main() 加载,build_user_prompt / build_subitem_user_prompt 通过
# get_chart_specs_for_chapter() 查询本章对应规格,作为 prompt 注入。
_CHART_SPECS_BY_CHAPTER: dict[str, list[dict]] = {}
# 章节级禁止出图(chart_specs.json 顶层 suppress_chapters):合格制/页数硬顶项目
# 用图表换页数时,直接删 spec 不生效 —— 无 spec 的章会让**最后一个子项**成为隐式
# 图表承担者(_is_chart_owner 的兜底分支),反而漏出图。列入本表才真正整章禁止。
_CHART_SUPPRESS_CHAPTERS: set[str] = set()
# rules 文件所在 extracted 目录(load_authoring_context 记录,供 Block2 真源回读)
_RULES_FILE_DIR: str = ""
# 关键响应词(模块级,由 _load_writer_inputs 填充;4-block 与 legacy 整章路径统一消费,
# 历史缺陷:仅 legacy 子项路径注入 → 废标关键响应词正文无保证,compliance 反复触发且 --force 修不掉)
_RESPONSE_KEYWORDS: list = []


def load_chart_specs(extracted: Path, log: logging.Logger | None = None) -> int:
    """从 extracted/chart_specs.json 加载,按章节分组。返回 spec 总数。"""
    global _CHART_SPECS_BY_CHAPTER, _CHART_SUPPRESS_CHAPTERS
    _CHART_SPECS_BY_CHAPTER = {}
    _CHART_SUPPRESS_CHAPTERS = set()
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
    # 顶层为裸数组(旧版本/手改)时不得直接 .get 崩掉 writer
    specs_list = payload.get("specs", []) if isinstance(payload, dict) else payload
    if isinstance(payload, dict):
        raw_sup = payload.get("suppress_chapters") or []
        if isinstance(raw_sup, (list, tuple)):
            _CHART_SUPPRESS_CHAPTERS = {str(c).zfill(2) for c in raw_sup if str(c).strip()}
    for spec in (specs_list or []):
        if not isinstance(spec, dict):
            continue
        ch = str(spec.get("chapter", "")).zfill(2)
        if ch:
            _CHART_SPECS_BY_CHAPTER.setdefault(ch, []).append(spec)
    total = sum(len(v) for v in _CHART_SPECS_BY_CHAPTER.values())
    if log:
        req = sum(1 for ss in _CHART_SPECS_BY_CHAPTER.values() for s in ss if s.get("required"))
        log.info(f"chart_specs 已加载: {total} 张图(其中 {req} 张招标硬性要求)")
        if _CHART_SUPPRESS_CHAPTERS:
            log.info(f"  章节级禁止出图: {','.join(sorted(_CHART_SUPPRESS_CHAPTERS))}"
                     f"(整章不注入图表规格,且所有子项禁止输出 ```json 图块)")
    return total


def get_chart_specs_for_chapter(num: str) -> list[dict]:
    """查某章的 specs。"""
    return _CHART_SPECS_BY_CHAPTER.get(str(num).zfill(2), [])


def _specs_for_prompt(chapter: dict) -> list[dict]:
    """取某章的 chart specs 副本,并应用 chapter 本地图表覆盖规则。

    规则来源: _apply_variant_chart_context 写入的 chapter["_chart_rules"]
    (mgmt_override: 管理体系图类型; by_section: {节号: 类型/None跳过})。
    在副本上打 _override_type/_skip 标记,不触碰模块级全局(并发安全)。"""
    if str(chapter.get("num", "")).zfill(2) in _CHART_SUPPRESS_CHAPTERS:
        return []
    specs = get_chart_specs_for_chapter(chapter.get("num", ""))
    rules = chapter.get("_chart_rules") or {}
    if not rules:
        return specs
    out = []
    for spec in specs:
        s = dict(spec)
        ov = rules.get("mgmt_override")
        if ov and ("hierarchy" in s.get("type", "") or "体系" in s.get("data_hint", "")):
            s["_override_type"] = ov
        sec_ov = (rules.get("by_section") or {}).get(s.get("section", ""), "__missing__")
        if sec_ov != "__missing__":
            if sec_ov is None:
                s["_skip"] = True
            else:
                s["_override_type"] = sec_ov
        out.append(s)
    return out


def build_chart_spec_block(specs: list[dict], subitem_scope: bool = False) -> str:
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
        + ("\n★ 本子节**只**输出上面这几张图;本章其余图表由其他子节输出。"
           "重复输出同一类型图表会产生重复插图并撞页数硬顶。" if subitem_scope else "")
    )
    return "\n".join(lines) + "\n"


def _specs_for_subitem(specs: list[dict], assignment: dict, total: int) -> list[dict]:
    """把章级图表按子项序号均分,让每个子项只输出自己那几张图。

    子项级生成时若把**整章**图表清单注入每个子项 prompt,多个子项会各自输出同一
    ```json``` 块 → preprocess_md 按 type 计数产出 `_schedule`/`_schedule_1`… 重复插图,
    直接撞页数硬顶(废标风险)。这里按 idx 轮转分派,每张图只落在一个子项。
    """
    if not specs:
        return []
    n = int(total or 0)
    if n <= 1:
        return specs
    idx = int((assignment or {}).get("idx", 0) or 0)
    return [s for i, s in enumerate(specs) if i % n == idx]


# ─── authoring_rules.md 解析（Phase 4） ───────────────────────────────
# 由 main() 加载,build_user_prompt / build_subitem_user_prompt 通过
# _AUTHORING_CTX 直接读结构化字段(避免文本截断损耗)。
_AUTHORING_CTX: dict = {}
_DIFFICULTIES_DATA: dict = {}
_BRAIN_TEXT: str = ""
# 连贯性主线(工序A):chapter_narrative.json,plan_synthesize 派生。每章叙事职责 +
# 聚焦难点 + 主线回指声明。空表示走旧的难点全量 dump 逻辑(降级安全)。
_CHAPTER_NARRATIVE: dict = {}


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


def _val_from_fact(facts: dict, key: str):
    v = facts.get(key)
    if isinstance(v, dict):
        return v.get("value")
    return v


def _enrich_authoring_with_project_timing(
    extracted: Path, authoring: dict, log: logging.Logger | None = None
) -> None:
    """把 project_facts/project_derived 的工期基准灌入 authoring project 上下文。"""
    project = authoring.setdefault("project", {})
    facts: dict = {}
    derived: dict = {}
    facts_path = extracted / "project_facts.json"
    derived_path = extracted / "project_derived.json"
    try:
        if facts_path.exists():
            payload = json.loads(facts_path.read_text(encoding="utf-8"))
            facts = payload.get("facts", payload)
    except (json.JSONDecodeError, OSError):
        facts = {}
    try:
        if derived_path.exists():
            derived = json.loads(derived_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        derived = {}

    bid_deadline = _val_from_fact(facts, "bid_deadline")
    duration_days = _val_from_fact(facts, "duration_days")
    constraints = derived.get("schedule_constraints") or {}
    schedule_start = derived.get("schedule_start_date")
    schedule_end = derived.get("schedule_end_date")
    earliest_start = constraints.get("earliest_start_date") or schedule_start

    if bid_deadline and not project.get("bid_deadline"):
        project["bid_deadline"] = bid_deadline
    if duration_days and not project.get("duration_days"):
        project["duration_days"] = duration_days
    if schedule_start:
        project["schedule_start_date"] = schedule_start
    if schedule_end:
        project["schedule_end_date"] = schedule_end
    if earliest_start:
        project["earliest_start_date"] = earliest_start
    if constraints.get("min_start_rule"):
        project["min_start_rule"] = constraints.get("min_start_rule")
    if log and (schedule_start or earliest_start):
        log.info(
            "工期基准已注入 authoring: "
            f"bid_deadline={bid_deadline}, start={schedule_start}, end={schedule_end}"
        )


def load_authoring_context(extracted: Path, log: logging.Logger | None = None) -> bool:
    """加载 authoring_rules.md + project_brain.md + difficulties.json 到模块级缓存。

    返回是否进入 4-块新模式(authoring 解析成功且非空)。
    解析失败或文件不存在 → 回退到 legacy build_user_prompt 旧流程。
    """
    global _AUTHORING_CTX, _DIFFICULTIES_DATA, _BRAIN_TEXT, _CHAPTER_NARRATIVE

    # brain text(为 Block 1 提供 重难点 / 项目类型 等)
    brain_path = extracted / "project_brain.md"
    _BRAIN_TEXT = brain_path.read_text(encoding="utf-8") if brain_path.exists() else ""

    # difficulties
    diff_path = extracted / "difficulties.json"
    if diff_path.exists():
        try:
            _d = json.loads(diff_path.read_text(encoding="utf-8"))
            _DIFFICULTIES_DATA = _d if isinstance(_d, dict) else {}  # 数组形状防御
        except (json.JSONDecodeError, OSError):
            _DIFFICULTIES_DATA = {}
    else:
        _DIFFICULTIES_DATA = {}

    # chapter_narrative.json(工序A2 派生;不存在→空→走旧的全量 dump,降级安全)
    narr_path = extracted / "chapter_narrative.json"
    if narr_path.exists():
        try:
            _CHAPTER_NARRATIVE = json.loads(narr_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _CHAPTER_NARRATIVE = {}
    else:
        _CHAPTER_NARRATIVE = {}

    # authoring_rules.md(优先) 或 disqualification_rules.md(回退)
    global _RULES_FILE_DIR
    _RULES_FILE_DIR = str(extracted)
    try:
        from bid_common import preferred_rules_path
        rp = preferred_rules_path(extracted)
    except (ImportError, AttributeError):
        rp = extracted / "disqualification_rules.md"

    if rp.name == "authoring_rules.md" and rp.exists():
        try:
            text = rp.read_text(encoding="utf-8")
            _AUTHORING_CTX = parse_authoring_yaml(text)
            _enrich_authoring_with_project_timing(extracted, _AUTHORING_CTX, log)
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

    cfg_path = get_config_dir() / "province_db_codes.json"
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


def _brain_scope_fallback(brain_text: str) -> str:
    """工程构成派生失败时，从 project_brain.md 摘回退锚点。

    优先级：①基本信息里的**招标范围**原始行（最准）；②「工程构成」段落；
    ③含建设内容/规模关键词的任意段落。返回前剥掉 Markdown 标题行。"""
    if not brain_text:
        return ""
    seg = ""
    m = re.search(r"招标范围[*：:]\s*([^\n]{40,600})", brain_text)
    if m:
        seg = m.group(1)
    if not seg:
        m = re.search(r"##[ 一二三四五六七八九十]*、[^\n]*(?:工程构成|招标范围|建设内容)[^\n]*\n([\s\S]{0,900})", brain_text)
        seg = (m.group(1) if m else "").strip()
    if not seg:
        m = re.search(r"([\s\S]{0,120}(?:建设内容|建设规模)[\s\S]{0,400})", brain_text)
        seg = (m.group(1) if m else "").strip()
    # 勃掉标题行(含首行——工程构成节为空时会直接命中下一节标题)
    seg = re.sub(r"^(?:#{1,6}\s|\*\*).*$", "", seg, flags=re.MULTILINE)
    seg = re.sub(r"\n#{1,6}[^\n]*$", "", seg)
    seg = re.sub(r"\s*\n\s*\n+", "\n", seg).strip()[:500]
    return seg if len(seg) >= 60 else ""


def _build_block1_project(
    authoring: dict, brain_text: str, difficulties: dict,
    current_chapter_num: str | None = None,
) -> str:
    """Block 1: 项目认知 — 把项目事实摆清楚。

    current_chapter_num: 当前要写的章号(2 位,如 "07")。用于决定是否对本章强提醒
    "必须 1:1 围绕子工程展开"。其他章只列工程构成清单作为背景。
    """
    project = authoring.get("project") if isinstance(authoring.get("project"), dict) else {}
    work = [w for w in (authoring.get("work_structure") or []) if isinstance(w, dict)]
    constraints = [c for c in (authoring.get("project_specific_constraints") or []) if isinstance(c, dict)]
    _pn = authoring.get("key_personnel")
    personnel = _pn if isinstance(_pn, dict) else {}
    chapter_anchors = [a for a in (authoring.get("chapter_anchors") or []) if isinstance(a, dict)]
    project_type = extract_project_type(brain_text)

    重点 = difficulties.get("重点", []) if difficulties else []
    难点 = difficulties.get("难点", []) if difficulties else []

    lines = ["【Block 1：项目认知（写之前先弄清楚这是什么项目）】", "",
             "★ 本条为强约束：正文每节至少出现 1 次本项目独有信息（具体数字/地点/海拔/工期/子工程名）。",
             "禁止写出「任何项目都能用」的套话。如果一段话换个项目名不改也能用→直接删掉重写。",
             "★ 管理制度类章节（质量/安全/环保/绿色/文明施工/民工/工期保证）：每条制度后必须紧跟本项目落地方式（点位名/工程量数字/海拔/工期节点/重难点对策），只写通用条文视为不合格。",
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
    if project.get("bid_deadline"):
        lines.append(f"  · 投标截止/开标基准：{project['bid_deadline']}")
    if project.get("schedule_start_date") or project.get("schedule_end_date"):
        # ★ 日历日期只能出现在进度计划章的 ```json 数据块内:正文散文出现任何日历日期
        #   都会被 compliance 判「正文日期泄漏」并触发重写(且重写也修不掉)。
        #   bid_data_anchor 已用「仅供 JSON 数据块」标注规避此陷阱,这里必须同步。
        lines.append("  · 进度计划 JSON 基准（★ 仅供进度计划章的 ```json 数据块使用，正文严禁出现）：")
        if project.get("schedule_start_date"):
            rule = project.get("min_start_rule") or "投标截止后第5个工作日"
            lines.append(f"      - 计划开工：{project['schedule_start_date']}（{rule}，不得早于该日）")
        if project.get("schedule_end_date"):
            lines.append(f"      - 计划竣工：{project['schedule_end_date']}")
        lines.append("      - 正文一律用「总工期 N 日历天 / 开工后第 N 天 / 第 N 阶段」相对表达")
    if project.get("quality_target"):
        lines.append(f"  · 质量目标：{project['quality_target']}")
    if project.get("earliest_start_date"):
        if project.get("schedule_end_date"):
            _dur = f"{project['duration_days']}日历天" if project.get("duration_days") else ""
            end_note = f"；{_dur}计划竣工至 {project['schedule_end_date']}"
        else:
            end_note = ""
        lines.append(
            f"⚠ 正文一律禁止出现具体日历日期（YYYY年M月D日 / YYYY-MM-DD），工期一律用相对表达："
            f"「总工期 {project.get('duration_days') or 'N'} 日历天」「开工后第 N 天」「第 N 阶段完成」。"
            f"只有进度计划章节的 ```json 图数据块允许具体日期，且必须按计划开工基准 "
            f"{project['earliest_start_date']} 起算：不得早于该日{end_note}、跨度 ≤ 招标工期。"
        )
    lines.append("")

    # 工程构成
    if work:
        lines.append("工程构成（清单驱动 — 禁止编造无清单子项, 触发 B1.17 雷区）：")
        for w in work:
            sub = w.get("sub_project", "?")
            nature = w.get("nature", "")
            boq_items = [i for i in (w.get("boq_items") or []) if isinstance(i, dict)]
            if boq_items:
                # 清单类目型:子工程名 + 真实工程量明细(正文工艺/参数须落到这些数字上)
                lines.append(f"  · {sub}（{nature or '清单类目'}）")
                for it in boq_items[:12]:
                    if not isinstance(it, dict):
                        continue  # 非 dict 清单项(脏 authoring)防御
                    it_name = it.get("name", "")
                    # qty 缺失时不得渲染出字面量 "None" 进每章 prompt
                    _q = it.get("qty", "")
                    qty = "" if _q is None else _q
                    unit = it.get("unit", "") or ""
                    lines.append(f"      - {it_name} {qty}{unit}")
            else:
                area = w.get("area_m2", "-")
                unit = w.get("unit") or "㎡"
                extra = f"（{nature}）" if nature else ""
                lines.append(f"  · {sub} {area}{unit}{extra}")
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
        lines.append(
            "⚠ 负向约束：正文中的班组名/工序名/专业术语必须能在上述工程构成清单中找到依据；"
            "未出现在清单中的专业（如 钢结构/路基/隧道/桥梁 等清单外专业）严禁写入。"
        )
    else:
        # 工程构成派生失败 → 回退招标范围原文摘录，至少给 writer 一个项目锚点（防零锚点写作）
        scope_fb = _brain_scope_fallback(brain_text)
        if scope_fb:
            lines.append("工程构成（⚠ 自动派生失败——以下为招标范围原文摘录，子工程划分与工艺必须以此为唯一依据，禁止引入清单外专业）：")
            lines.append(scope_fb)
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

    # 现场施工条件约束(场地/交通/水电/周边/季节/材料设备) — 来自 difficulties.json 约束 6 大类。
    # 这是平面布置/施工部署/进度章节的"实料",避免写成"交通便利、水电齐备"这类套话。
    约束 = difficulties.get("约束", []) if difficulties else []
    site_lines = []
    for c in 约束:
        if isinstance(c, dict):
            cat = (c.get("category") or "").strip()
            content = (c.get("content") or "").strip()
        else:
            cat, content = "", str(c or "").strip()
        if not content:
            continue
        # 跳过"招标未明示"类空约束,不往正文里灌套话
        if content.startswith("招标文件未明示") or content in ("常规执行", "无"):
            continue
        site_lines.append(f"  · {cat}：{content}" if cat else f"  · {content}")
    if site_lines:
        lines.append("现场施工条件约束（平面布置/施工部署/进度/现场管理章节必须落到这些具体条件上，禁止写成通用套话）：")
        lines.extend(site_lines)
        lines.append("")

    # 重难点 — 连贯性主线注入(工序 A3)
    # 有 chapter_narrative 时:只注入本章聚焦的难点 + 主线回指声明(精准,控 context 稀释);
    # 无聚焦时:走旧的全量 dump 逻辑(降级安全,行为不变)。
    def _title_of(item):
        if isinstance(item, dict):
            return item.get("title") or item.get("name") or ""
        return str(item) if item else ""

    def _find_diff_detail(title: str) -> dict | None:
        """按标题在 _DIFFICULTIES_DATA.难点 里找完整条目(含 rationale/evidence)。"""
        for n in 难点:
            if isinstance(n, dict) and n.get("title", "") == title:
                return n
        return None

    # 本章叙事职责(chapter_narrative.json 派生,可能不存在→空)
    cur_num = str(current_chapter_num or "").zfill(2) if current_chapter_num else ""
    narr_chapters = (_CHAPTER_NARRATIVE.get("chapters") or {}) if _CHAPTER_NARRATIVE else {}
    this_narr = narr_chapters.get(cur_num, {}) if cur_num else {}
    focus_titles: list[str] = this_narr.get("difficulty_focus", []) or []
    mainline_echo = this_narr.get("mainline_echo", "") or ""

    # 主线回指声明(无论有无聚焦,只要 narrative 存在就注入,强化连贯)
    if mainline_echo:
        lines.append(mainline_echo)
        lines.append("")

    if focus_titles:
        # 精准模式:只列本章聚焦难点(限 3 条)+ 其 rationale(深化,而非标题罗列)
        lines.append("本章须重点回应的施工难点(从总体部署纲领分配,禁止泛泛而谈)：")
        for ft in focus_titles[:3]:
            lines.append(f"  · {ft}")
            detail = _find_diff_detail(ft)
            if isinstance(detail, dict):
                for r in (detail.get("rationale") or [])[:2]:
                    if r:
                        lines.append(f"      - {r}")
        lines.append("")
        # 技术重点仍全量列(重点不分章,是全局背景;只难点做聚焦)
        if 重点:
            lines.append("项目技术重点(全局背景,供本章参考)：")
            for r in 重点[:5]:
                t = _title_of(r)
                if t and t != "?":
                    lines.append(f"  · {t}")
            lines.append("")
    else:
        # 降级:无 chapter_narrative 或本章无聚焦 → 旧的全量 dump(行为不变)
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


def _as_dict(v) -> dict:
    """YAML 段形状防御:非 dict(标量/列表)一律 {}。"""
    return v if isinstance(v, dict) else {}



def _build_block2_rules(authoring: dict) -> str:
    """Block 2: rules 硬约束 — 违反 = 废标。

    format/blind 硬约束唯一真源为 disqualification_rules.md(见 bid_common
    canonical_rules_path);authoring_rules.md 对应段缺失/空时回读 disq 文件,
    防两文件失同步时向全部 prompt 注入过期废标级约束。"""
    fmt = _as_dict(authoring.get("format"))
    blind = _as_dict(authoring.get("blind"))
    if not fmt or not blind:
        try:
            from bid_common import canonical_rules_path
            _dq = canonical_rules_path(Path(_RULES_FILE_DIR)) if _RULES_FILE_DIR else None
        except Exception:  # noqa: BLE001
            _dq = None
        if _dq and _dq.exists():
            _dtx = _dq.read_text(encoding="utf-8", errors="ignore")
            _dq_auth = parse_authoring_yaml(_dtx)
            if not fmt:
                fmt = _as_dict(_dq_auth.get("format"))
            if not blind:
                blind = _as_dict(_dq_auth.get("blind"))
    red_lines = [r for r in (authoring.get("technical_red_lines") or []) if isinstance(r, dict)]
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
    ls_only = fmt.get("landscape_only_in_chapter")
    _ls_n = int(ls_only) if str(ls_only).strip().isdigit() else None
    if _ls_n:
        fmt_lines.append(f"  · ★ 横向页面仅允许出现在第 {_ls_n} 章（附图及附表）；"
                         f"第 1-{_ls_n - 1} 章正文一律纵向 A4 页面，"
                         f"不得出现横向页面，横向大图（施工总平面图/进度网络图）一律放第 {_ls_n} 章")
    elif ls_only:
        fmt_lines.append(f"  · ★ 横向页面仅允许出现在{ls_only}；其余正文一律纵向 A4 页面")
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
        for rl in red_lines[:12]:  # 全注入优先;仅极端(>12)截断防爆 prompt
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


_UNIFIED_DATA_ANCHOR = ""  # 运行时由 main 从 extracted/unified_data_anchor.md 加载（项目级数据，通用 writer 不硬编码）。
# 该文件是可选注入点：由 Agent（或人工）在 Step 2 确认 chapter_plan 后、Step 3 写作前手工写入
# extracted/unified_data_anchor.md，内容为全文各章必须一致的硬数据（总工期/规模/质量标准/子工程量/控制价等），
# 由 bid_data_anchor.py 在 write 步前自动生成（bid_run 会 --force 重生成，勿手工直接改；
# 人工补充请写在 <!-- MANUAL:BEGIN --> 与 <!-- MANUAL:END --> 之间，重生成时保留）。
# 用于强制跨章数据一致、禁止 LLM 编造。文件不存在时本钩子空转，管道行为不变。


def _eq_fmt(v) -> str:
    """equip_delta 值格式化:数字→带符号,字符串原样(手工 variants.json 容错)。"""
    if isinstance(v, (int, float)):
        return f"{v:+g}"
    return str(v)


def _build_block3_chapter(
    chapter: dict, variant: dict, skeleton: list[dict],
    subitems: list[dict], h1_title: str, band_lo: int, band_hi: int,
    target: int, coverage_line: str,
) -> str:
    """Block 3: 本章任务(整章模式)。

    合格制(passfail)：不预设二级标题，LLM 自由组织章节结构，最大化差异化。
    评分制(score/scoring)：严格对齐评分项，二级标题必须覆盖评分点。
    """
    equip_lines = "\n".join(f"  - {k}: {_eq_fmt(v)} 台" for k, v in (variant.get("equip_delta") or {}).items())
    skeleton_block = render_skeleton_for_prompt(skeleton)

    # 评标模式路由
    # authoring_rules.md 里字段形状有两个来源:「## 1 项目基本信息」块写在
    # evaluation.evaluation_method 下,「## 2 章节锚点」块写在顶层 evaluation_mode。
    # 只读其中一个会恒取默认值(passfail) → 评分制项目被当合格制写。
    _authoring = _as_dict(_AUTHORING_CTX)
    eval_mode = (_as_dict(_authoring.get("evaluation")).get("evaluation_method")
                 or _authoring.get("evaluation_mode")
                 or "passfail")
    is_scoring = str(eval_mode).lower() in ("score", "scoring")

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
    elif subitems:
        # ── 合格制 + 有变体子项（批量反串标）：仍强制按子项输出 H2 ──
        # 修复(2026-08-18):原 else 分支允许"自由组织",导致 H2 与
        # plan_variants 的差异化子项标题不一致 → reviewer 按变体计划
        # H2 1:1 核查必然报 subitem_mismatch(本轮 48 章 major 的根因)。
        # 合格制大纲只锁一级主题,二级标题由变体计划差异化驱动,故强制。
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
        # ── 合格制无子项：自由组织 ──
        lines.extend([
            "请自由组织本章结构。你可以自行决定分几个二级标题 `##`、"
            "是否需要三级标题 `###`、各节篇幅分配等。要求：",
            "  - 内容必须紧扣章节主题，专业且有深度",
            "  - 自然融入下方「评分关键词」中的关键内容",
            "  - 总字数达到目标即可，各小节篇幅自行把握",
            "  - 禁止使用空洞的模板化表述（如\"编制依据/实施方案/保障措施/应急响应\"）",
            "",
        ])

    lines.extend([
        "评分关键词（必须在正文实质性展开，不得照抄成 H2/H3 标题）：",
        f"  {coverage_line}",
        "",
        f"本章目标字数：{target} 字（±15% 带：{band_lo}–{band_hi} 字）",
        "图表不计入字数；该用表就用表、该用图就用图。",
        "正文禁止出现具体日历日期（YYYY年M月D日 / YYYY-MM-DD）：工期用相对表达（总工期 N 日历天 / 开工后第 N 天 / 第 N 阶段完成）；只有进度计划章节文末的 ```json 图数据块允许具体日期。",
        "",
        "variants 差异化（必须自然融入正文叙述，严禁回显本段标签名）：",
    ])
    _angle = variant.get("angle")
    if _angle:
        lines.append(
            f"  · ★ 本方案专属写作角度主线：{_angle} —— 全文叙事与技术措施编排须以此角度为主线，"
            "与其他方案形成实质区别；但总工期、工程规模、质量/安全标准等硬数据须严格与 project_brain "
            "一致，严禁偏移工期或编造不实内容"
        )
    # 进度差异化:消费 variants.schedule_offset_days(历史缺陷:字段写入但全库无下游,死数据)
    # 手工/脏 variants.json 里该字段可能是字符串或浮点 → 先容错成 int(同 _eq_fmt 口径)
    try:
        _sched_off = int(float(variant.get("schedule_offset_days") or 0))
    except (TypeError, ValueError):
        _sched_off = 0
    if _sched_off and any(kw in chapter["title"] for kw in ("进度", "工期", "保证")):
        lines.append(
            f"  · ★ 本方案进度节奏差异化：本章涉及的关键工序节点，相对常规编排整体偏移 {_sched_off:+d} 天"
            f"（即基准节点天数 N 在本方案按 N{'' if _sched_off < 0 else '+'}{_sched_off} 编排），"
            "总工期与开竣工间隔保持不变，体现本单位动员组织节奏差异"
        )
    lines.extend([
        f"  · 劳动力配置系数：{variant.get('labor_factor', 1.0)}",
        "  · 主要机械调整：",
        equip_lines if equip_lines else "    （无）",
        f"  · 优先引用规范版本：{variant.get('standard_year_preference','-')} 年版（不确定的规范不强加版本）",
    ])
    if _UNIFIED_DATA_ANCHOR:
        lines.append("")
        lines.append(_UNIFIED_DATA_ANCHOR)

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
    equip_lines = "\n".join(f"  - {k}: {_eq_fmt(v)} 台" for k, v in (variant.get("equip_delta") or {}).items())
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
        "正文禁止出现具体日历日期（YYYY年M月D日 / YYYY-MM-DD），工期用相对表达（总工期 N 日历天 / 第 N 天 / 第 N 阶段）。",
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
    ]
    _angle = variant.get("angle")
    if _angle:
        lines.append(
            f"  · ★ 本子节所属方案的写作角度主线：{_angle} —— 措施编排与叙事须贴合此主线；"
            "但总工期、工程规模、质量/安全标准等硬数据须严格与 project_brain 一致，"
            "严禁偏移工期或编造不实内容"
        )
    try:
        _sched_off = int(float(variant.get("schedule_offset_days") or 0))
    except (TypeError, ValueError):
        _sched_off = 0
    if _sched_off and any(kw in chapter["title"] for kw in ("进度", "工期", "保证")):
        lines.append(
            f"  · ★ 本方案进度节奏差异化：关键工序节点相对常规编排整体偏移 {_sched_off:+d} 天，"
            "总工期与开竣工间隔保持不变"
        )
    lines.extend([
        f"  · 劳动力系数：{variant.get('labor_factor', 1.0)}",
        "  · 主要机械调整：",
        equip_lines if equip_lines else "    （无）",
        f"  · 优先引用规范版本：{variant.get('standard_year_preference','-')} 年版（不确定的规范不强加版本）",
    ])
    if _UNIFIED_DATA_ANCHOR:
        lines.append("")
        lines.append(_UNIFIED_DATA_ANCHOR)
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
                    pri = min(pri, p)  # 不 break:取全部命中的最小优先级
            sections.append((pri, current_title, "\n".join(current_lines)))

    # 首个 ## 之前的行(文档标题/引导语)归入「总则」段,不再静默丢弃
    _pre: list[str] = []
    _body_start = 0
    for _k, _ln in enumerate(disq_text.splitlines()):
        if _ln.strip().startswith("##"):
            _body_start = _k
            break
        _pre.append(_ln)
    if _pre and any(l.strip() for l in _pre):
        sections.append((3, "总则", "\n".join(_pre)))
    _all_lines = disq_text.splitlines()
    for line in _all_lines[_body_start:] if _body_start else _all_lines:
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
        # 首段也要受限:否则无 "##" 标题的规则文件会被整篇当"总则"注入每章 prompt
        if total + len(content) > limit:
            _room = max(0, limit - total)
            if _room > 200:
                result_parts.append(content[:_room])
                total += _room
            break
        result_parts.append(content)
        total += len(content)

    return "\n\n".join(result_parts) if result_parts else disq_text[:2000]


def _parse_response_keywords(disq_text: str) -> list[dict]:
    """兼容代理:实现已上移 bid_common.parse_response_keywords。"""
    from bid_common import parse_response_keywords
    return parse_response_keywords(disq_text)


# hint 里的通用后缀词:剥掉后剩下的「核心词」才是章节主题,用于兜底匹配。
# 不剥的话 2-gram 会把「冬期施工/混凝土养护」这类词灌进所有带"施工/措施"的章。
_KW_HINT_GENERIC = ("施工", "方案", "措施", "体系", "管理", "计划", "技术", "工程",
                    "组织", "保证", "控制", "与", "及", "和", "的", "章")


def _hint_core(hint: str) -> str:
    """hint → 核心主题词(剥通用后缀)。核心为空表示只能靠精确匹配,不做兜底。"""
    s = str(hint or "")
    for g in _KW_HINT_GENERIC:
        s = s.replace(g, " ")
    return "".join(s.split())


def _match_keywords_for_chapter(keywords: list[dict], chapter_title: str) -> list[dict]:
    """找出与本章标题匹配的关键响应词。"""
    matched = []
    hinted: list[dict] = []
    for kw in keywords:
        hint = kw.get("chapter_hint", "")
        if not hint:
            # 无章节提示的响应词(如纯"质量标准合格"):承载章(进度/工期/概况/部署/
            # 质量/安全)全部注入,其余章不注入(防每个章 prompt 都膨胀)
            if any(h in chapter_title for h in ("进度", "工期", "概况", "部署", "质量", "安全")):
                matched.append(kw)
            continue
        hinted.append(kw)
        # 章节提示和章节标题做模糊匹配
        hint_core = hint.replace("章", "").strip()
        if hint_core and hint_core in chapter_title:
            matched.append(kw)
    # 携带 hint 但未精确命中的响应词(标题措辞与 hint 不同)→ 按 2-gram 重叠兜底注入。
    # ★ 本兜底不得被 "not matched" 门控:否则本章只要先命中任一无 hint 响应词
    #   (进度/工期/质量…),整批带 hint 的关键响应词就被静默丢弃
    #   (实测「施工进度与工期保证措施」+ hint「进度控制」时「180 日历天」不注入),
    #   且 compliance 同路径重写也修不掉。又不能无条件全注入(每章 prompt 膨胀)。
    _seen_titles = {str(k.get("keyword", "")) for k in matched}
    for kw in hinted:
        if str(kw.get("keyword", "")) in _seen_titles:
            continue
        _core = _hint_core(kw.get("chapter_hint", ""))
        # 核心词需整体出现在标题里(核心词剥后缀后已很短,整体匹配比 2-gram 精准得多)
        if _core and _core in chapter_title:
            matched.append(kw)
            _seen_titles.add(str(kw.get("keyword", "")))
    return matched


def _attachment_table_note(title: str) -> str:
    """附表类子项的硬性格式要求:必须是 Markdown 表格。

    「附表一 拟投入本标段的主要施工设备表」这类招标点名附表,评委是按表核对的;
    写成散文等于未响应(实测 V01/V02 曾把附表一~三写成纯段落 → DOCX 里 0 张表,
    而 V03 写成了规范表格)。列数受 docx 版心限制,必须 ≤6 列。
    """
    s = str(title or "")
    if "附表" not in s or "图" in s:
        return ""
    return ("\n★ 本子节是招标点名的**附表**,必须以 **Markdown 表格**呈现正文数据"
            "(表头 + 数据行,列数 ≤6,序号/名称/型号规格/数量/用途类列),"
            "不得只写散文段落;表格之后可用 1-2 句说明。\n")

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
    mgmt_kw = set(_MGMT_CHAPTER_KEYWORDS)
    if any(kw in chapter_title for kw in mgmt_kw):
        sec3 = ""
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
        "写作铁律："
        "① 每段正文至少含 1 处本项目独有信息（工程量数字/点位名/海拔/工期/规范号），能换项目名复用的段落视为废稿；"
        "② 制度措施类内容必须写“在本项目如何落地”（谁做/在哪做/用什么资源/何时做），禁止罗列通用条文；"
        "③ 所有工艺描述须与工程量清单项对应，清单没有的专业与工序一律不写。"
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

    # 附图及附表章:招标点名附表必须逐张以 Markdown 表格呈现
    # (整章路径同样要约束,否则模型只写散文 → DOCX 里 0 张表,附表一~三 等于未响应)
    _att_note = _attachment_table_note("附表" if any(
        k in str(chapter.get("title", "")) for k in ("附图", "附表")) else "")
    chart_spec_block = build_chart_spec_block(_specs_for_prompt(chapter))

    # 组织架构模板已在 _build_block3_chapter 内注入,此处不再重复拼接(历史:双重注入浪费 token)

    _kw_block = _build_keyword_block(_RESPONSE_KEYWORDS, h1_title)
    return f"""请为本投标项目撰写第 {chapter['num']} 章技术标内容。

{block1}

{block2}

{block3}

{block4}{_att_note}

{chart_spec_block}
{_kw_block}
现在开始撰写正文："""


def _legacy_build_user_prompt(
    chapter: dict, variant: dict, brain_excerpt: str, disq_excerpt: str,
    skeleton: list[dict], subitems: list[dict],
) -> str:
    """旧 prompt 拼装：authoring_rules.md 不可用时使用（向后兼容）。"""
    equip_lines = "\n".join(f"  - {k}: {_eq_fmt(v)} 台" for k, v in (variant.get("equip_delta") or {}).items())
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

    # 子项与骨架互斥(4-block 同语义):有子项分配时骨架仅作参考,不双注入
    skeleton_block = "" if subitems else render_skeleton_for_prompt(skeleton)
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

    # 附图及附表章:招标点名附表必须逐张以 Markdown 表格呈现(整章路径同样要约束,
    # 否则模型只写散文 → DOCX 里 0 张表,附表一~三 等于未响应)
    _att_note = ""
    if any(k in str(chapter.get("title", "")) for k in ("附图", "附表")):
        _att_note = ("\n★ 本章是招标点名的**附图及附表**,其中**附表一/二/三(施工设备表、"
                     "试验和检测仪器设备表、劳动力计划表)必须以 Markdown 表格呈现**"
                     "(表头+数据行,列数 ≤6),不得只写散文;附表四/五按图表 JSON 输出。\n")
    chart_spec_block = build_chart_spec_block(_specs_for_prompt(chapter))

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
- 劳动力配置系数：{variant.get('labor_factor', 1.0)}（原基础 × 该系数）
- 主要机械调整：
{equip_lines}
- 优先引用的现行标准版本：{variant.get('standard_year_preference','—')} 年版

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
      进度日期是**唯一允许出现具体日历日期的位置**：必须服从 Block 1 的计划开工基准，不得早于计划开工基准，跨度 ≤ 招标工期；正文(含 **Markdown 表格单元格**)一律不得出现具体日历日期（YYYY年M月D日 / YYYY-MM-DD）——
      进度/资源类表格请用「第N天 / 开工后第N天 / 第N阶段」相对表达，否则会被合规门禁判违规并反复重写。若无绝对基准，优先使用第1天/第N天相对日序号。
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
{_org_template_block}{_build_keyword_block(_RESPONSE_KEYWORDS, h1_title)}
{_att_note}
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

    _kw_block = _build_keyword_block(_RESPONSE_KEYWORDS, chapter["title"])
    # ★ 子项级:只注入本子节负责的图表,否则每个子项都输出整章图表 → 重复插图
    _all_specs = _specs_for_prompt(chapter)
    _my_specs = _specs_for_subitem(_all_specs, subitem_assignment, len(sibling_titles))
    chart_spec_block = build_chart_spec_block(_my_specs, subitem_scope=True)
    # ★ 组织架构模板同样只能给"承担该图的那个子项"。旧实现对本类章的**每个**子项
    #   都注入 org_chart 模板指令 → N 个子项各出一张组织架构图(实测 V01 章 04 出了
    #   4 张同样的图:1 张 + 3 张字节级相同,白吃 3 页并顶页数硬顶)。
    #   无图表 spec 的章(如 05 施工总进度计划)只让**最后一个**子项承担隐式图表。
    #   禁止出图的章必须排除在隐式承担者之外,否则最后一个子项仍会漏图
    _is_chart_owner = bool(_my_specs) or (
        not _all_specs
        and str(chapter.get("num", "")).zfill(2) not in _CHART_SUPPRESS_CHAPTERS
        and int(subitem_assignment.get("idx", 0) or 0) == max(0, len(sibling_titles) - 1))
    # 非图表承担子项明确禁止输出 json 图块:否则"进度/工期"等章每个子项都会自愿画一张
    # 横道图(实测 V03 章 05 两个子项各出一张),重复插图白吃页数并顶页数硬顶。
    _att_note = _attachment_table_note((subitem_assignment or {}).get("title", ""))
    # 整章禁止出图时给专用禁令:原有的“本章图表由承担该图的子节统一输出”在无承担者
    # 的场景下自相矛盾,反而怂恿子项自己补图。
    if str(chapter.get("num", "")).zfill(2) in _CHART_SUPPRESS_CHAPTERS:
        _no_chart_note = (
            "\n★ 本章**整章不得输出任何 ```json 图表块**"
            "(为满足招标页数硬顶,招标强制图表已集中在附图附表章)。"
            "需要结构化表达时用 Markdown 表格(列数 ≤6)。\n")
    elif not _is_chart_owner:
        _no_chart_note = ("\n★ 本子节**不得输出任何 ```json 图表块**"
                          "(本章图表由承担该图的子节统一输出;重复输出会造成重复插图并顶页数硬顶)。\n")
    else:
        _no_chart_note = ""
    # 组织架构模板注入(子项模式同样需要——组织章被"管理类强制子项级"改道后,
    # 模板只在子项 prompt 注入才会到达撰稿人;整章路径由 Block3 注入)
    _org_blk = ""
    _otj = chapter.get("_org_template_json")
    if _is_chart_owner and _otj and any(kw in str(chapter.get("title", "")) for kw in
                                        ("资源配备", "组织机构", "项目管理机构", "组织管理")):
        _org_blk = (f"\n★ 本章组织架构图必须使用以下{chapter.get('_org_template_label','')}"
                    f"模板结构（严格按此 JSON 结构生成 org_chart）：\n"
                    f"```json\n{_otj}\n```\n"
                    f"  岗位名称可根据项目实际微调，但层级结构和分支数必须与模板一致。\n")
    return f"""你只需要撰写第 {chapter['num']} 章中的**一个**二级子节。
章节完整内容将由多位撰稿人并行产出，最后由主笔编辑补写章节引言和过渡衔接。

{block1}

{block2}

{block3}

{block4}{_kw_block}{_org_blk}{_att_note}{_no_chart_note}

{chart_spec_block}
现在开始撰写本子节："""


def _legacy_build_subitem_user_prompt(
    chapter: dict, variant: dict, subitem_assignment: dict,
    sibling_titles: list[str],
    brain_excerpt: str, disq_excerpt: str,
    response_keywords: list[dict] | None = None,
) -> str:
    """旧子项 prompt 拼装：authoring_rules.md 不可用时使用（向后兼容）。"""
    equip_lines = "\n".join(f"  - {k}: {_eq_fmt(v)} 台" for k, v in (variant.get("equip_delta") or {}).items())
    blind = variant.get("blind", False)
    company_blind = (
        "\n【暗标硬约束】禁出现任何公司名/LOGO/业绩/人员真实姓名/获奖证书编号\n"
        if blind else ""
    )

    target = int(subitem_assignment["target_chars"])
    band_lo = int(target * 0.85)
    band_hi = int(target * 1.15)

    siblings_block = "\n".join(f"  - {t}" for t in sibling_titles) or "（无）"
    _att_note = _attachment_table_note((subitem_assignment or {}).get("title", ""))

    return f"""你只需要撰写第 {chapter['num']} 章中的**一个**二级子节。章节完整内容将由多位撰稿人并行产出，最后由主笔编辑补写章节引言和过渡衔接。
{_att_note}
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
- 劳动力系数：{variant.get('labor_factor', 1.0)}
- 主要机械调整：
{equip_lines}
- 优先引用标准版本：{variant.get('standard_year_preference','—')} 年版

【项目理解摘要】
{brain_excerpt}

【废标条款要点（必须规避）】
{disq_excerpt}
{company_blind}{_build_keyword_block(response_keywords or [], chapter['title'])}
【输出强约束】
1. 输出以 `## {subitem_assignment['title']}` 起头，随后是正文；不得输出 `#` 一级标题
2. 字数落在 {band_lo}–{band_hi} 字带内
3. 引用现行国家/行业规范（GB/T、JGJ、CJJ、TD/T、SL 等）带规范号，版本以 {variant.get('standard_year_preference','—')} 年版为准
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
    _cs = variant.get("chart_style")
    chart_style = _cs if isinstance(_cs, dict) else {}  # null/str/list 全防
    ns = chart_style.get("numbering_scheme") or {}
    numbering_block = (
        f"  - 一级编号：{ns.get('level1', '1.  2.  3.')}\n"
        f"  - 二级编号：{ns.get('level2', '(1)  (2)  (3)')}\n"
        f"  - 三级编号：{ns.get('level3', '①  ②  ③')}"
    )
    h1_title = chapter["title"]
    # 主笔 polish 阶段:需要看到本章全部图表块(已由各子项写入 draft),不按子项裁剪
    chart_spec_block = build_chart_spec_block(_specs_for_prompt(chapter))
    # 项目认知摘要注入:主笔补写引言/过渡时必须有项目事实可引(历史问题:polish 零上下文→通用衔接语)
    block1_digest = ""
    if _AUTHORING_CTX:
        try:
            b1 = _build_block1_project(
                _AUTHORING_CTX, _BRAIN_TEXT, _DIFFICULTIES_DATA,
                current_chapter_num=chapter["num"],
            )
            if b1:
                block1_digest = "【项目认知摘要（补写引言/过渡时必须引用其中具体事实）】\n" + b1[:900] + "\n\n"
        except Exception:  # noqa: BLE001
            pass
    # 动态引言字数:小章引言过大必然击穿长度守恒带(历史问题:382字章+400字引言→组装被弃)
    _target = int(chapter.get("target_chars") or 0)
    intro_chars = min(400, max(150, _target // 6))
    return f"""【章节标题】
第 {chapter['num']} 章 {h1_title}

【优先引用规范版本】
{variant.get('standard_year_preference','—')} 年版（以此版本为准统一全章规范引用）

【本份标书序号方案（必须严格执行）】
{numbering_block}
正文中所有步骤/要点/条目的编号必须替换为上述格式，不得混用其他编号风格。

{block1_digest}【整章拼接稿（来自 N 位撰稿人的并行产出，各子节只有技术干货，缺少引言和过渡）】
{draft_markdown}

【任务】
请按系统规则组装补写：补写章节引言（约{intro_chars}字）、补写子节间过渡衔接、统一术语、统一序号格式。引言与过渡必须引用上方项目认知摘要中的具体事实（工程量数字/点位名/海拔/重难点），禁止通用开场白。子节技术正文原封不动保留。**保留并按以下规格补齐图表 ```json``` 块**——若拼接稿缺图,必须在对应子节末追加。输出完整的整章 Markdown。{blind_tail}

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
    # 本函数作用域内没有 vmodel/vtemp(_build_auto_subitem_plan 里的局部名)→ 兜底用 variant 的值
    vmodel = variant.get("model", "")
    vtemp = variant.get("temperature", 0.6)

    if not subitem_assignments:
        # 无子项分配时由调用方回退单模型整章(此处返回特殊标记让上层走 _generate_one_single)
        log.warning(f"[{vid}] 章 {ch_num} 交叉模式但子项清单为空，回退单模型整章")
        return None  # None=请求回退(调用方判定),False=真失败

    log.info(
        f"[{vid} {variant['company_name']}] 章 {ch_num} 交叉模式："
        f"{len(subitem_assignments)} 子项并行 + 主笔 {polish_model} 润色"
    )

    # 图表上下文注入(组织架构模板/管理体系图覆盖)——历史 bug:仅单模型路径调用,
    # cross 路径静默丢弃 variant_chart_overrides → 04 章组织模板失效
    _apply_variant_chart_context(chapter, variant, variant_chart_overrides, log)

    sibling_titles = [s["title"] for s in subitem_assignments]

    def run_subitem(sub: dict) -> tuple[dict, str, Exception | None]:
        # prompt 构造/键访问也纳入容错(单个子项异常不中止整章 map,
        # 否则 inner.map 直接抛、其余已生成子项全部丢弃)
        try:
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
                resp = client.chat(
                    model_key=sub["model"],
                    system=sys_prompt,
                    user=prompt,
                    temperature=sub["temperature"],
                    enable_fallback=True,
                )
                return sub, resp.text.strip(), None
        except Exception as e:  # noqa: BLE001
            return sub, "", e

    # 子项内层并行
    inner_workers = min(len(subitem_assignments), SUBITEM_INNER_WORKERS)
    results: list[tuple[dict, str, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=inner_workers) as inner:
        results = list(inner.map(run_subitem, subitem_assignments))

    # 按_idx 排序拼接
    # 缺 idx/title 键的手工 plan 用枚举序兜底(防 KeyError 丢整章)
    # 副本上补缺键(元组不可变→重建),不污染共享 plan(重试时避免残留错位 idx)
    results = [(_sub | {"idx": _sub.get("idx", _i), "title": _sub.get("title") or f"子项{_i+1}"}, _txt, _err)
               for _i, (_sub, _txt, _err) in enumerate(results)]  # idx 仅补缺(枚举序=输入位置)
    def _safe_idx(d) -> int:
        try:
            return int(d.get("idx"))
        except (TypeError, ValueError):
            return 1 << 30  # 坏 idx 排末尾,不与正常序抢前
    results.sort(key=lambda x: _safe_idx(x[0]))
    sub_texts: list[str] = []
    _failed_subs = 0
    for sub, text, err in results:
        if err:
            log.error(f"  ✗ 子项 {sub['idx']} [{sub.get('model') or vmodel}] {sub['title'][:20]} 失败: {err}")
            _failed_subs += 1
            continue  # 不写占位文本(历史缺陷:占位会被 --force-continue 带进 assemble 产物)
        # 确保以 ## 标题起头
        if not text.startswith("## "):
            text = f"## {sub['title']}\n\n{text}"
        sub_texts.append(text)
        log.info(
            f"  · 子项 {sub['idx']} [{sub.get('model') or vmodel}@T{sub.get('temperature', vtemp)}] "
            f"{sub['title'][:18]} → {count_chinese_chars(text)} 字"
        )

    if _failed_subs:
        # 失败子项就地重试一轮(单点瞬态失败不应让整章含已成功付费调用全部丢弃)
        _sys_retry = ("你是投标文件子节撰稿人，专注写好分给你的这一个二级子节，"
                      "严格按给定标题、字数带和规范要求输出。")
        _vmodel_fb = plan.get("polish_model") or variant.get("model", "deepseek")
        _vtemp_fb = float(plan.get("polish_temperature") or variant.get("temperature", 0.6))
        for _ri in range(len(results)):
            sub, text, err = results[_ri]
            if not err:
                continue
            _fixed = ""
            try:
                _sem = get_sem(sub.get("model") or _vmodel_fb)
                with _sem:
                    _resp = client.chat(
                        model_key=sub.get("model") or _vmodel_fb, system=_sys_retry,
                        user=build_subitem_user_prompt(
                            chapter, variant, sub, sibling_titles,
                            brain_excerpt, disq_excerpt,
                            response_keywords=response_keywords),
                        temperature=sub.get("temperature", _vtemp_fb),
                        enable_fallback=True)
                    _fixed = _resp.text.strip()
            except Exception:  # noqa: BLE001
                continue
            if _fixed:
                results[_ri] = (sub, _fixed, None)
                log.info(f"  · 子项 {sub.get('idx', _ri)} 重试成功")
        # 重检
        _failed_subs = sum(1 for _, _, e in results if e)
        if _failed_subs:
            log.error(f"  ✗ {_failed_subs} 个子项重试后仍失败，本章不落盘(用 --force 重跑修复)")
            return False
        # 重试可能改变了 results → 重建 sub_texts(历史缺陷:仅更新 results,
        # 落盘用旧 sub_texts,重试成功的子节静默丢失)
        sub_texts = []
        for sub, text, err in results:
            if err:
                continue
            if not text.startswith("## "):
                text = f"## {sub.get('title', '')}\n\n{text}"
            sub_texts.append(text)
        if not sub_texts:
            log.error("  ✗ 重试后无任何有效子节")
            return False


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
            # 长度守恒带:小章(目标<1200字)因引言占比天然大,上限放宽到 1.6×
            # (历史问题: 382字章 + 200-400字引言 → 必超 1.25× → 润色永远被弃用)
            _hi_band = 1.6 if int(chapter.get("target_chars", 0) or 0) < 1200 else 1.25
            # 主笔是补写内容，产出应 >= 草稿；若反而变少说明删了子节内容，弃用
            if assembled_chars < draft_chars * 0.95:
                log.warning(
                    f"  ⚠ 组装后 {assembled_chars} 字 < 草稿 {draft_chars} 的 95%，"
                    f"主笔可能删减了子节内容，弃用组装结果"
                )
            elif assembled_chars > draft_chars * _hi_band:
                log.warning(
                    f"  ⚠ 组装后 {assembled_chars} 字 > 草稿 {draft_chars} 的 {int(_hi_band*100)}%，"
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
    # null 形状防御(chart_assignment.json 手改常见;None 上 in 运算抛 TypeError)
    org_template = (chart_overrides.get("org_template") or {})
    mgmt_types = (chart_overrides.get("management_chart_types") or {})
    chart_type_overrides = (chart_overrides.get("chart_overrides") or {})

    # 1. 组织架构模板：注入到组织类章(关键词与 bid_plan_variants._find_org_chapter
    #    同源:资源配备/组织机构/项目管理机构/组织管理,防章号≠04 时模板静默丢失)
    _is_org_ch = any(kw in str(chapter.get("title", "")) for kw in
                     ("资源配备", "组织机构", "项目管理机构", "组织管理"))
    if _is_org_ch and org_template:
        template_desc = json.dumps(org_template.get("children", []), ensure_ascii=False)
        chapter["_org_template_json"] = template_desc
        chapter["_org_template_label"] = org_template.get("template_label", "")

    # 2+3. 管理体系图/图表类型覆盖 → 写入 chapter 本地规则(不动模块级全局,
    #      防并发变体串扰:V01 的覆盖残留/竞态到 V02 的 prompt)
    rules: dict = {}
    if ch_num_str in mgmt_types:
        rules["mgmt_override"] = mgmt_types[ch_num_str]
        log.info(f"  [{variant['id']}] 章 {ch_num_str} 管理体系图类型覆盖为 {mgmt_types[ch_num_str]}")
    by_section = {}
    if ch_num_str in chart_type_overrides:
        for ov in chart_type_overrides[ch_num_str]:
            by_section[ov.get("section", "")] = ov.get("override_type")
    if by_section:
        rules["by_section"] = by_section
    if rules:
        chapter["_chart_rules"] = rules


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
            enable_fallback=True,  # 与子项路径一致:瞬态失败走降级链
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


def _single_call_safe_chars(client: LLMClient, model_key: str) -> int:
    """模型单次安全产出字数上限。

    取 config 的 output_char_cap(实测校准值,如 deepseek=8000)。
    章节目标字数超过此值,单次整章调用必然写不满 → 应转子项级。
    """
    m = client.models.get(model_key, {}) if hasattr(client, "models") else {}
    return int(m.get("output_char_cap", 8000) or 8000)


def _build_auto_subitem_plan(chapter: dict, variant: dict,
                             subitems: list[dict]) -> dict | None:
    """无显式 chapter_model_plan 时,按章节子项即时构建子项级计划。

    所有子项用变体主模型(单作者模式下保持 1 变体 = 1 模型);
    子项无分配则返回 None(无法拆 → 调用方回退整章)。
    """
    if not subitems:
        return None
    _ch_target = int(chapter.get("target_chars", 0) or 0)  # 章级基准,不缓存到共享 variant dict
    vmodel = variant.get("model", "deepseek")
    vtemp = variant.get("temperature", 0.6)
    assigns = []
    for idx, s in enumerate(subitems):
        _tc = int(s.get("chars") or s.get("target_chars") or 0)
        if _tc <= 0:
            # 无字数分配时按章目标均分(0-0 字数带=LLM 无约束)
            _tc = max(300, _ch_target // max(len(subitems), 1))
        assigns.append({
            "idx": idx,
            "title": s.get("title", ""),
            "target_chars": _tc,
            "model": vmodel,
            "temperature": vtemp,
        })
    return {
        "mode": "cross_model",
        "polish_model": vmodel,
        "polish_temperature": vtemp,
        "subitems": assigns,
    }


def generate_one(
    client: LLMClient, chapter: dict, variant: dict,
    brain_excerpt: str, disq_excerpt: str,
    out_path: Path, log: logging.Logger,
    system_prompt: str,
    subitems: list[dict],
    get_sem=None,
    response_keywords=None,
    variant_chart_overrides=None,
    chapter_plan_override=None,
) -> bool:
    """生成单个 variant × 单个章节

    chapter_plan_override(由 run_task 按字数自动判定时传入)或
    variant.chapter_model_plan 中 mode=cross_model → 走子项并行流程;
    否则走单模型整章流程。
    """
    _cmp = variant.get("chapter_model_plan")
    plan = chapter_plan_override or (_cmp.get(chapter["num"]) if isinstance(_cmp, dict) else None)
    if plan and plan.get("mode") == "cross_model" and get_sem is not None:
        _r = _generate_one_cross(
            client, chapter, variant, brain_excerpt, disq_excerpt,
            out_path, log, plan, get_sem,
            response_keywords=response_keywords,
            variant_chart_overrides=variant_chart_overrides,
        )
        if _r is None:  # 子项空 → 真实回退单模型整章
            pass
        else:
            return _r
    return _generate_one_single(
        client, chapter, variant, brain_excerpt, disq_excerpt,
        out_path, log, system_prompt, subitems,
        variant_chart_overrides=variant_chart_overrides,
    )


def _usable_models(client: LLMClient) -> list[str]:
    """当前 config 下真正可用的模型:存在 + 未停用 + API key 已配置。
    本地端点(127.0.0.1/localhost)免 key(与 bid_llm_client._get_openai_client 同口径)。"""
    usable = []
    for key, m in client.models.items():
        if m.get("disabled"):
            continue
        _ep = str(m.get("endpoint", ""))
        _is_local = "127.0.0.1" in _ep or "localhost" in _ep
        _ke = m.get("api_key_env") or ""
        if not _ke and not _is_local:
            continue  # 未声明 key 字段的远程模型:视为未配置
        if _ke and not os.environ.get(_ke) and not _is_local:
            continue
        usable.append(key)
    return usable


def _validate_variant_models(variants: list[dict], client: LLMClient,
                             log: logging.Logger, tender_dir: Path | None = None) -> bool:
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
        _vm = v.get("model")
        _plan_map = v.get("chapter_model_plan")
        _plan_bad = False
        if isinstance(_plan_map, dict):
            for pl in _plan_map.values():
                if not isinstance(pl, dict):
                    continue
                _mm = [pl.get("polish_model")] + [s.get("model") for s in (pl.get("subitems") or []) if isinstance(s, dict)]
                if any(m and m not in usable for m in _mm):
                    _plan_bad = True
                    break
        if _vm in usable and not _plan_bad:
            continue
        if _vm in usable and _plan_bad and _vm is not None:
            # (保留原判定;缺 model 键 None 落到下方完整替换分支指派主模型)
            # 主模型可用、仅 plan 内模型失效:只修 plan,不无谓整体降级主写手
            old = None
        else:
            old = v.get("model")
        if old is None:
            # 仅修 plan:全部失效模型换到主模型;_vm 为 None(缺键)时先指派可用主模型
            if _vm is None or _vm not in usable:
                _taken = {tv.get("model") for tv in variants if tv is not v}
                def _main_ok2(k):
                    return (k in usable and k not in _taken
                            and (client.models.get(k, {}) or {}).get("writer_role") != "system_only")
                _cands = ([k for k in client.fallback_order if _main_ok2(k)]
                          or [k for k in usable if _main_ok2(k)] or usable)
                v["model"] = _vm = _cands[0]
                log.warning(f"[{v['id']}] 缺 model 键,指派主模型 {v['model']}")
            _fallback_main = _vm
            _pl = v.get("chapter_model_plan") or {}
            for pl in _pl.values():
                if isinstance(pl, dict):
                    if pl.get("polish_model") and pl["polish_model"] not in usable:
                        pl["polish_model"] = _fallback_main
                    for s in pl.get("subitems", []) or []:
                        if isinstance(s, dict) and s.get("model") and s["model"] not in usable:
                            s["model"] = _fallback_main
            log.info(f"[{v['id']}] 仅修复 chapter_model_plan 内失效模型 → {_fallback_main}")
            continue
        taken = {tv.get("model") for tv in variants if tv is not v}
        # 主写手排除 system_only 角色模型(仅制度章定位,P/G 偏弱,
        # 经 fallback_order 升为主写手违背其定位)
        def _main_ok(k: str) -> bool:
            return (k in usable and k not in taken
                    and (client.models.get(k, {}) or {}).get("writer_role") != "system_only")
        candidates = ([k for k in client.fallback_order if _main_ok(k)]
                      or [k for k in usable if _main_ok(k)]
                      or [k for k in usable if k not in taken]
                      or usable)
        v["model"] = candidates[0]
        log.warning(f"[{v['id']}] 模型 {old!r} 当前不可用(停用/缺 key/不存在),"
                    f"自动替换为 {v['model']!r}")
        # chapter_model_plan 里的 polish_model/子项模型同步换到可用模型,
        # 防子项调用失败后把"（本子节生成失败,需后续补写）"占位文本写进正文
        plan_map = v.get("chapter_model_plan")
        if isinstance(plan_map, dict):
            for pl in plan_map.values():
                if not isinstance(pl, dict):
                    continue
                if pl.get("polish_model") and pl["polish_model"] not in usable:
                    pl["polish_model"] = v["model"]
                for s in pl.get("subitems", []) or []:
                    if isinstance(s, dict) and s.get("model") and s["model"] not in usable:
                        s["model"] = v["model"]
    # 持久化:替换结果写回 variants.json(--resume 重跑读到的是已修复模型名,
    # 否则内存修复丢失,重复走失败调用+降级链)。tender_dir 由调用方显式传入。
    try:
        _vj = None
        if tender_dir is not None:
            _cand = tender_dir / "extracted" / "variants.json"
            _vj = _cand if _cand.exists() else None
        if _vj:
            import json as _json
            _payload = _json.loads(_vj.read_text(encoding="utf-8"))
            _vs_list = _payload.get("variants") if isinstance(_payload, dict) else (
                _payload if isinstance(_payload, list) else [])
            _changed = False
            for _v in (_vs_list or []):
                if _v.get("id") in {v.get("id") for v in variants}:
                    _src = next((x for x in variants if x.get("id") == _v.get("id")), None)
                    if _src is None:
                        continue
                    if _v.get("model") != _src.get("model"):
                        _v["model"] = _src["model"]
                        _changed = True
                    # plan 内的模型修复落盘:仅内容实际不同才覆盖(防无谓重写/共享引用)
                    if _src.get("chapter_model_plan") and _v.get("chapter_model_plan") != _src["chapter_model_plan"]:
                        import copy as _copy
                        _v["chapter_model_plan"] = _copy.deepcopy(_src["chapter_model_plan"])
                        _changed = True
            if _changed:
                _out = _payload if isinstance(_payload, dict) else _vs_list
                _vj.write_text(_json.dumps(_out, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("  模型替换已写回 variants.json(持久化)")
    except Exception as _e:  # noqa: BLE001
        log.warning(f"  模型替换写回 variants.json 失败(不影响本次运行): {_e}")
    return True


def _parse_writer_args(argv=None):
    p = argparse.ArgumentParser(description="多模型章节生成工作器")
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--variant", help="仅生成指定 variant（如 V01）")
    p.add_argument("--chapter", help="仅生成指定章节号（如 01）")
    p.add_argument("--force", action="store_true", help="覆盖已生成文件")
    p.add_argument("--workers", type=int, default=4, help="并发 worker 数（默认 4）")
    p.add_argument("--per-model-limit", type=int, default=2, help="单模型并发上限（默认 2）")
    p.add_argument("--no-polish", action="store_true", help="跳过主笔润色，直接用子项拼章草稿为最终输出")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _load_writer_inputs(extracted: Path, log):
    """加载 writer 所需的全部输入。失败（缺 variants.json / chapter_plan 解析为空）
    时记录日志并返回 None；成功返回元组（顺序见 main 解包处，变量名保持不变）。"""
    variants_path = extracted / "variants.json"
    if not variants_path.exists():
        log.error(f"缺少 {variants_path}。请先运行 bid_variants.py")
        return None
    try:
        variants_payload = json.loads(variants_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"variants.json 解析失败: {e}（文件损坏？重跑 bid_variants.py 生成）")
        return None
    if isinstance(variants_payload, list):
        # 裸数组形状归一化为标准 payload(后续 ["variants"] 直接索引)
        variants_payload = {"variants": variants_payload}
    if not isinstance(variants_payload, dict) or not variants_payload.get("variants"):
        log.error("variants.json 缺少 variants 列表(重跑 bid_variants.py 生成)")
        return None

    try:
        chapters = parse_chapter_plan(extracted / "chapter_plan.md")
    except Exception as e:  # noqa: BLE001
        log.error(f"chapter_plan.md 解析异常: {e}")
        return None
    if not chapters:
        log.error("chapter_plan.md 解析不到任何章节")
        return None

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
        log.info("使用 authoring_rules.md 作为方案规则源")
    # 按重要性分段提取废标条款（不再简单截断前 1500 字）
    disq_excerpt = _extract_disq_by_priority(disq_text) if disq_text else "（无特殊废标条款）"
    # 提取关键响应词（必须在正文中出现的词句）
    global _RESPONSE_KEYWORDS
    response_keywords = _parse_response_keywords(disq_text)
    _RESPONSE_KEYWORDS = response_keywords
    if response_keywords:
        log.info(f"关键响应词已提取：{len(response_keywords)} 条")

    # 按项目类型动态构造角色提示（从 project_brain.md "项目类型"字段读取）
    project_type = extract_project_type(brain_text)
    system_prompt = build_system_prompt(project_type)
    log.info(f"项目类型识别: {project_type}")

    client = LLMClient.load(log=log)

    return (variants_payload, chapters, subitems_map, brain_text,
            disq_excerpt, response_keywords, system_prompt, client)


def _build_writer_tasks(target_variants, chapters, output: Path, args, log):
    """构造 (variant, chapter, out_path) 任务列表；返回 (tasks, skip_count)。
    每变体优先用自己的 chapter_plan_variant.md(结构独立,反串标),缺失回退共享 chapter_plan.md。
    任务按变体轮转交错 [V01-ch1, V02-ch1, V03-ch1, V01-ch2, ...]
    (feedback_bid_writer_scheduling 方案A):让前 N 个 worker 自然分到不同
    变体/模型,避免单模型信号量阻塞的 worker 占住槽位导致多模型串行。"""
    def _variant_chapters(v: dict) -> list[dict]:
        vp = output / v["id"] / "chapter_plan_variant.md"
        try:
            v_chs = parse_chapter_plan(vp) if vp.exists() else []
        except (FileNotFoundError, ValueError):
            v_chs = []  # 空/损坏的变体计划 → 回退基准
        if not v_chs:
            v_chs = chapters
        if args.chapter:
            v_chs = [c for c in v_chs if str(c["num"]).zfill(2) == args.chapter.zfill(2)]
        return v_chs

    skip_count = 0
    per_variant: list[list] = []
    for v in target_variants:
        v_dir = output / v["id"]
        v_dir.mkdir(exist_ok=True)
        v_chapters = _variant_chapters(v)
        if not v_chapters:
            log.error(f"[{v['id']}] 未解析到章节"
                      + (f"(指定的 --chapter {args.chapter} 不在该变体中)" if args.chapter else ""))
            if args.chapter:
                global _CHAPTER_MISS
                _CHAPTER_MISS = True  # 任意变体未命中(收尾时结合 fail_count 综合判定)
            continue
        v_tasks = []
        for ch in v_chapters:
            out_path = v_dir / ch["filename"]
            if out_path.exists() and not args.force:
                log.info(f"[{v['id']}] 章 {ch['num']} 已存在，跳过（用 --force 覆盖）")
                skip_count += 1
                continue
            v_tasks.append((v, ch, out_path))
        per_variant.append(v_tasks)

    # 变体轮转交错:章节序号为外层、变体为内层
    tasks = []
    depth = max((len(vt) for vt in per_variant), default=0)
    for i in range(depth):
        for vt in per_variant:
            if i < len(vt):
                tasks.append(vt[i])  # 浅变体耗尽后自然只剩深变体(轮转语义,无法避免也不需避免)
    return tasks, skip_count


def main() -> int:
    args = _parse_writer_args()

    global SKIP_POLISH, _CHAPTER_MISS
    SKIP_POLISH = args.no_polish
    _CHAPTER_MISS = False  # 同进程二次调用复位

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    output.mkdir(exist_ok=True)

    log_path = output / "writer.log"
    log = make_script_logger("bid_writer", log_path, verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_writer 启动  目录: {tender_dir}  时间: {datetime.now().isoformat(timespec='seconds')}")

    # 加载输入（缺关键文件则记录日志并退出）
    loaded = _load_writer_inputs(extracted, log)
    if loaded is None:
        return 2
    (variants_payload, chapters, subitems_map, brain_text,
     disq_excerpt, response_keywords, system_prompt, client) = loaded

    # 加载项目级统一数据锚点（如有），强制全文数据一致性、禁止 LLM 编造
    global _UNIFIED_DATA_ANCHOR
    _anchor_path = extracted / "unified_data_anchor.md"
    if _anchor_path.is_file():
        _UNIFIED_DATA_ANCHOR = _anchor_path.read_text(encoding="utf-8").strip()
        log.info(f"已加载统一数据锚点: {_anchor_path.name} ({len(_UNIFIED_DATA_ANCHOR)} 字符)")
    else:
        _UNIFIED_DATA_ANCHOR = ""  # 显式复位，防止单进程连续处理多个标段时上一项目锚点串入

    # 过滤 variants / chapters
    target_variants = variants_payload["variants"]
    if args.variant:
        target_variants = [v for v in target_variants if v["id"].upper() == args.variant.upper()]
        if not target_variants:
            log.error(f"未找到 variant: {args.variant}")
            return 2

    # 校验变体模型在当前 config 下可用(不可用则就地替换,避免整批章节白跑)
    if not _validate_variant_models(target_variants, client, log, tender_dir=tender_dir):
        return 2

    ok_count = fail_count = 0

    # 构造任务列表(逐变体取各自的章节结构)
    tasks, skip_count = _build_writer_tasks(target_variants, chapters, output, args, log)

    log.info(f"待生成: {len(target_variants)} 个变体, 共 {len(tasks)} 个章节任务"
             + (f"(另跳过 {skip_count})" if skip_count else ""))

    # 每模型限流锁
    # 确保每个模型只创建一次信号量
    _sem_lock = threading.Lock()
    _sems: dict[str, threading.Semaphore] = {}

    def get_sem(model_key: str) -> threading.Semaphore:
        with _sem_lock:
            if model_key not in _sems:
                _sems[model_key] = threading.Semaphore(args.per_model_limit)
            return _sems[model_key]

    def run_task(v, ch, out_path):
        ch = dict(ch)  # 共享基准章节 dict 的写隔离(_org_template_json/_chart_rules 不跨变体泄漏)
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
        _cmp = v.get("chapter_model_plan")
        plan = _cmp.get(ch["num"]) if isinstance(_cmp, dict) else None
        explicit_cross = bool(plan and plan.get("mode") == "cross_model")
        # 自动判定(无显式 cross 计划时):
        #   a) 章节目标字数 > 主模型单次安全上限 → 子项级(防大章单次写不满)
        #   b) 管理制度类章节 → 强制子项级(每子项独立拿全量 Block1 项目上下文,
        #      治历史 P/G<0.6 的制度章空心化;feedback: 全库管理类 P/G 均值 0.6-1.2)
        auto_plan = None
        if not explicit_cross:
            cap = _single_call_safe_chars(client, v.get("model", ""))
            need_subitem = int(ch.get("target_chars", 0) or 0) > cap
            if not need_subitem and subitems:
                if any(kw in ch.get("title", "") for kw in _MGMT_CHAPTER_KEYWORDS):
                    need_subitem = True
                    log.info(
                        f"[{v['id']}] 章 {ch['num']} 为管理类章节 → 强制子项级生成"
                        f"(每子项独立项目上下文, {len(subitems)} 子项)"
                    )
            if need_subitem:
                auto_plan = _build_auto_subitem_plan(ch, v, subitems)
                if auto_plan:
                    _cause = (f"目标 {ch.get('target_chars')} 字 > 单次上限 {cap}"
                              if int(ch.get("target_chars", 0) or 0) > cap
                              else "管理类章节强制子项级(每子项独立项目上下文)")
                    log.info(
                        f"[{v['id']}] 章 {ch['num']} {_cause} → "
                        f"子项级生成({len(auto_plan['subitems'])} 子项)"
                    )
                else:
                    log.warning(
                        f"[{v['id']}] 章 {ch['num']} 需子项级生成"
                        f"(触发: 目标字数超上限或管理类章节)但无子项分配，"
                        f"回退单模型整章调用——建议补齐 chapter_plan 子项分配"
                    )
        if explicit_cross or auto_plan:
            # 交叉/子项级：外层不占 sem，内层对每个子项独立占（否则主模型 sem 会与子项用主模型死锁）
            return generate_one(
                client, ch, v, brain_excerpt, disq_excerpt, out_path, log,
                system_prompt, subitems, get_sem=get_sem,
                response_keywords=response_keywords,
                variant_chart_overrides=variant_chart_overrides,
                chapter_plan_override=auto_plan,
            )
        # 单模型整章模式：外层占主模型 sem（章节字数在单次安全上限内）
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
                # 预算追踪:每章完成即落盘——写作是成本大头,熔断不能等全部写完才看到数据
                client.flush_stats_to_json(output / "logs" / "llm_stats.json", "writer")

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
    client.flush_stats_to_json(output / "logs" / "llm_stats.json", "writer")

    if _CHAPTER_MISS and tasks and fail_count == 0:
        # exit 4 = 部分变体未命中 --chapter(loop 视为 partial_chapter_miss 不重试)。
        # _CHAPTER_MISS 只在「该章号不在某变体计划里」时置位;已生成文件被 skip 不算未命中
        # (那种情况返回 0/正常码)。两次退出码语义:2=全部变体都未命中,4=仅部分未命中。
        log.warning(f"部分变体未命中 --chapter(exit=4);命中任务已生成 {len(tasks)} 章")
        return 4
    if fail_count == 0 and _CHAPTER_MISS and not tasks:
        # 仅当全部变体都未命中(tasks 空=一个都没生成)才判失败;
        # 部分命中时已生成的章节有效,只警告
        log.error("指定的 --chapter 未命中任何章节,视为失败(防静默假成功)")
        return 2
    if _CHAPTER_MISS and tasks:
        log.warning("部分变体未命中指定 --chapter 且有失败章节(exit=1)")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
