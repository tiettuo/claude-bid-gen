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
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger, repair_technical_criteria_alignment  # noqa: E402
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
    # 西藏简易综合评估法常见 16 章合格制大纲。施工方案承载清单实体，
    # 必须明显高于管理/附表类章节；页数受限时仍保持全部 16 个锚点。
    "工程概况": 5,
    "编制依据": 3,
    "总体施工部署": 6,
    "项目组织管理机构": 5,
    "施工总进度计划": 6,
    "施工总平面布置": 4,
    "施工方案与技术措施": 27,
    "质量管理体系与措施": 10,
    "工期保证技术组织措施": 6,
    "安全文明施工体系与措施": 8,
    "绿色施工管理体系与措施": 6,
    "季节性施工措施": 5,
    "地下管线及其他地上地下设施的保护加固措施": 2,
    "民工工资保证措施": 2,
    "附图及附表": 2,
    "其他": 3,
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
    """规划章节子项。招标大纲已列子节时必须 1:1 保留；仅无子节时调 LLM 补充。

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

    # 合格制项目按大纲逐项核查：原文子节无论数量多少都不得合并。
    # 页数受限时通过压缩每项字数解决，不得删除评审锚点。
    if n_orig > 0:
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

    # 无子节 → LLM 按主题语义设计 4-6 个子节。
    if n_orig == 0:
        descriptor = "为本章节设计 4-6 个 H2 子节(紧扣章节主题与项目实际,不要用'编制依据/实施方案/保障措施/应急响应'这种泛化通用名)"
        secs_text = "  (大纲未列子节,请按章节主题语义+项目实际设计)"
    else:
        raise AssertionError("有原文子节时应在上方 1:1 返回")

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
                model_key=os.environ.get("BID_PLAN_MODEL", "deepseek"), system=system, user=user_attempt,
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


def score_item_subitems(
    item: dict,
    target_chars: int,
    chapter_num: int,
    project_brief: str,
    work_complexity: int = 0,
) -> list[dict]:
    """评分制章节按评分细则拆子项，确保章节规划 1:1 对齐评分表。"""
    title = item.get("title_original", "")
    criteria = item.get("criteria_original", "") or ""
    score = item.get("score") or 0

    target_count = _score_item_subitem_count(title, criteria, score, work_complexity)
    context = f"{title} {criteria} {project_brief}"
    project_objects = _project_object_subitems(context)

    if "施工方案" in title or "技术措施" in title:
        candidates = (
            ["工程特点、重难点及总体技术路线", "施工部署、作业区划分及工序衔接"]
            + project_objects
            + ["施工现场周边环境保护措施", "质量标准、责任分解及违约承诺"]
        )
    elif "质量" in title:
        candidates = [
            "质量目标、责任体系及岗位分工",
            "材料设备进场验收与样板引路",
            "关键工序质量控制与检验批验收",
            "隐蔽工程、成品保护及资料闭环",
            "质量违约责任承诺",
        ]
    elif "安全" in title:
        candidates = [
            "安全文明施工目标与责任体系",
            "关键地点、关键工序安全控制",
            "机械用电、临边作业及消防管理",
            "交通导改、交叉作业及应急处置",
            "安全文明施工标准及违约承诺",
        ]
    elif "环保" in title or "环境" in title:
        candidates = [
            "环保目标、责任体系及现场分区",
            "扬尘、噪声、污水及固废控制",
            "水土保持、既有设施与植被保护",
            "绿色施工、节能降耗及村民协调",
            "环保违约责任承诺",
        ]
    elif "进度" in title:
        candidates = [
            "总工期安排与施工阶段划分",
            "关键线路、关键节点及横道计划",
            "劳动力、材料、机械与进度协同",
            "雨季、交叉作业条件下的进度纠偏措施",
            "工期违约责任承诺",
        ]
    elif any(k in title for k in ("劳动力", "机械", "资源配备", "设备配置")):
        candidates = [
            "施工组织架构与岗位职责",
            "劳动力投入计划及专业班组配置",
            "机械设备投入计划及进退场安排",
            "材料供应、运输组织及储备计划",
            "劳动力与机械动态调配措施",
            "资源投入保证措施",
        ]
    elif "保修" in title:
        candidates = [
            "保修范围、期限及响应机制",
            "绿化养护、给排水及电气保修措施",
            "回访巡检、问题闭环与资料移交",
            "保修责任及违约承诺",
        ]
    else:
        # 兜底仍从评分描述取词，但不生成 0 分通用章。
        candidates = [
            "评分要求识别与响应目标",
            "实施内容与控制措施",
            "责任分工与资源保障",
            "检查验收与违约承诺",
        ]

    names = candidates[:max(3, min(target_count, len(candidates)))]

    each = target_chars // len(names)
    remainder = target_chars - each * len(names)
    out = []
    for i, name in enumerate(names, 1):
        chars = each + (remainder if i == len(names) else 0)
        out.append({
            "title": f"{chapter_num}.{i} {name}",
            "chars": chars,
            "covers": [criteria[:60]] if criteria else [],
        })
    return out


def _project_object_subitems(context: str) -> list[str]:
    """按项目真实对象生成施工方案子项，避免把历史项目专属词写入新项目。"""
    candidates: list[str] = []
    if "防坠长廊" in context:
        candidates.extend([
            "防坠长廊钢柱、钢架制作安装措施",
            "防坠长廊网面及耐力板安装措施",
        ])
    if any(k in context for k in ("顶面遮雨", "耐力板", "遮雨")):
        candidates.append("顶面遮雨系统与节点防水施工措施")
    if any(k in context for k in ("铁丝网", "网面")):
        candidates.append("铁丝网面安装及边缘收口控制")
    if any(k in context for k in ("基础", "混凝土", "垫层", "土方", "回填")):
        candidates.append("土方开挖、基础及混凝土工程施工方法")
    if any(k in context for k in ("沥青", "路缘石", "路面", "草皮")):
        candidates.append("路面拆除恢复及场地修复措施")
    if any(k in context for k in ("人居环境", "园林", "绿化", "草皮")):
        candidates.append("场地整治、排水衔接及附属设施施工措施")
        candidates.append("园林绿化、苗木栽植及养护施工措施")

    if not candidates:
        candidates = [
            "主要分部分项工程施工方法",
            "关键节点施工技术控制",
            "既有设施保护及恢复措施",
        ]
    return list(dict.fromkeys(candidates))


def _score_item_subitem_count(title: str, criteria: str, score: float, work_complexity: int = 0) -> int:
    """根据分值、评分描述复杂度、工程构成复杂度决定子项数量。"""
    if score <= 2:
        count = 3
    elif score <= 3:
        count = 4
    elif score <= 5:
        count = 5
    else:
        count = 6

    # 评分描述里并列要求越多，子项越多；短句不强行展开。
    separators = len(__import__("re").findall(r"[、；;，,]|以及|并|且|是否", criteria or ""))
    if separators >= 8:
        count += 1
    elif separators <= 2 and score <= 3:
        count -= 1

    # 施工方案承载工程构成，工程类别多时应展开更多。
    if any(k in title for k in ("施工方案", "技术措施")) and work_complexity >= 4:
        count += 2
    elif any(k in title for k in ("施工方案", "技术措施")):
        count += 1

    # 资源章节通常同时覆盖劳动力、机械、材料、调配、保证措施。
    if "劳动力" in title or "机械" in title:
        count = max(count, 5)

    # 进度章节至少要有计划、关键线路、资源协同、纠偏/承诺。
    if "进度" in title:
        count = max(count, 4)

    return max(3, min(count, 8))


def _boq_work_complexity(ext: Path) -> int:
    """用清单专业类目数量估算工程构成复杂度。"""
    boq_path = ext / "boq.json"
    if not boq_path.exists():
        return 0
    try:
        boq = json.loads(boq_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    roots = set()
    for cat in boq.get("categories", []) or []:
        name = cat.get("name") or cat.get("category") or ""
        if not name:
            continue
        roots.add(name.split("-", 1)[0])
    return len(roots)


def _boq_item_brief(ext: Path, limit: int = 12) -> str:
    """提取少量清单项目名称，供评分制子项规划识别真实施工对象。"""
    boq_path = ext / "boq.json"
    if not boq_path.exists():
        return ""
    try:
        boq = json.loads(boq_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    names: list[str] = []
    for cat in boq.get("categories", []) or []:
        for item in (cat.get("items") or []):
            name = str(item.get("name") or item.get("project_name") or "").strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                return "、".join(names)
    return "、".join(names)


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
    if eval_method == "score":
        parts.append("- 章节锚点:技术评分项 1:1 成章；非评分项只能并入相关章节子项，不单列 0 分大章")
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
        pages = chars / chars_per_page_mid + figs * 0.85  # 渲染图每张≈0.85-1页(feedback_chart_page_cost)
        total_chars += chars
        total_subitems += len(subs)
        total_figs += figs
        total_pages_calc += pages
        ref = f"合格制 #{idx}" if eval_method == "passfail" else f"技术评分项 #{idx}"
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

    # 连贯性主线:加载 deployment_outline(工序A1 产出,可能不存在→None,下游降级)
    deploy_path = ext / "deployment_outline.json"
    deploy_or_none = (
        json.loads(deploy_path.read_text(encoding="utf-8")) if deploy_path.exists() else None
    )

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
    work_complexity = _boq_work_complexity(ext)
    fig_ratio = PROJECT_TYPE_FIG_RATIO.get(project_type, 0.25)

    # 目标页数:用户给 / 自动判档
    if target_pages is None:
        target_pages = SIZE_TO_PAGES.get(project_size, SIZE_TO_PAGES["unknown"])["default"]
        if eval_method == "score" and total_tech_score and total_tech_score <= 20:
            target_pages = min(target_pages, 45)
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
    scope_val = facts.get("project_scope", {}).get("value", "") if isinstance(facts.get("project_scope"), dict) else facts.get("project_scope", "")
    boq_brief = _boq_item_brief(ext)
    project_brief = (
        f"{facts.get('project_name', {}).get('value', '') if isinstance(facts.get('project_name'), dict) else facts.get('project_name', '')} "
        f"控制价 {control_price} 元 / 工期 {duration_days} 天 / 类型 {project_type} / 评标 {eval_method} / "
        f"范围 {scope_val} / 清单 {boq_brief}"
    )[:600]

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
        scoring_items = repair_technical_criteria_alignment(eval_data.get("scoring_items", []), log)
        tech_items = [i for i in scoring_items if i.get("section") == "技术"]
        if not tech_items:
            tech_items = scoring_items  # 旧数据无 section 字段时回退全量
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
                "score_item": item,
            })

    def _run_one(pt: dict) -> dict:
        if eval_method == "score" and pt.get("score_item"):
            subitems = score_item_subitems(
                item=pt["score_item"],
                target_chars=pt["target_chars"],
                chapter_num=pt["chapter_num"],
                project_brief=project_brief,
                work_complexity=work_complexity,
            )
            log.info(f"  章 {pt['chapter_num']} 按评分表拆分: {pt['title']} → {len(subitems)} 子项")
        else:
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

    # 连贯性主线注入(工序 A2):若 deployment_outline.json 存在,派生每章叙事职责
    # → extracted/chapter_narrative.json。writer Block1 据此做难点精准注入,
    # chapter_intro 据此写引言回指。文件不存在时 writer 走旧的全量 dump,完全降级安全。
    try:
        _derive_chapter_narrative(ext, chapters_meta, ext / "deployment_outline.json", log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"  chapter_narrative 派生失败(非阻断,writer 将走旧逻辑): {e}")

    # 第0章「编制总说明」(工序 A0):基于 deployment_outline 生成叙事锚点章。
    # 不调 LLM,纯模板拼接。config intro_chapter.enabled=false 时不生成。
    try:
        _generate_intro_chapter(ext, facts, eval_data, deploy_or_none, log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"  第0章生成失败(非阻断): {e}")

    return 0


def _derive_chapter_narrative(
    ext: Path,
    chapters_meta: list[dict],
    deployment_path: Path,
    log,
) -> None:
    """工序 A2: 读 deployment_outline.json,为每章派生「叙事维度 + 难点聚焦 + 主线回指」。

    输出 extracted/chapter_narrative.json:
      {
        "_deployment_statement": "...(全文主线声明,chapter_intro 回指用)",
        "chapters": {
          "01": {
            "narrative_dimension": "施工总目标",
            "difficulty_focus": ["<难点标题1>", "<难点标题2>"],
            "mainline_echo": "★ 本章叙事职责:从「施工总目标」维度展开,呼应部署纲领。"
          },
          ...
        }
      }

    匹配逻辑:对每个章节标题,用 bigram 重合度(复用 bid_run._pick_chapter_for_keyword
    同款算法)在 difficulty_to_dimension 里找该章最该承载的难点;找不全则该章聚焦为空
    (writer 降级到无聚焦)。narrative_dimension 取该章聚焦难点的主维度众数,无聚焦时
    按章节序号在 5 维里轮转兜底。
    """
    if not deployment_path.exists():
        log.info("  deployment_outline.json 不存在,跳过 chapter_narrative 派生(走旧逻辑)")
        return

    deploy = json.loads(deployment_path.read_text(encoding="utf-8"))
    statement = deploy.get("deployment_statement", "")
    d2d = deploy.get("difficulty_to_dimension", []) or []
    if not statement and not d2d:
        log.info("  deployment_outline 为空,跳过 chapter_narrative 派生")
        return

    DEPLOY_DIMS = ["施工总目标", "施工区段划分", "施工总顺序", "资源统筹原则", "组织机构框架"]

    def _bigram_score(probe: str, title: str) -> int:
        p = "".join(c for c in probe if "一" <= c <= "鿿")
        return sum(1 for i in range(len(p) - 1) if p[i:i + 2] in title)

    chapters_out: dict[str, dict] = {}
    for idx, ch in enumerate(chapters_meta):
        num = f"{ch.get('chapter_num', idx + 1):02d}" if isinstance(
            ch.get("chapter_num"), int
        ) else f"{idx + 1:02d}"
        title = ch.get("title", "")

        # 该章匹配哪些难点:difficulty 标题与章节标题 bigram 重合
        matched = []
        best_dim = None
        best_score = 0
        for d in d2d:
            if not isinstance(d, dict):
                continue
            diff_title = d.get("difficulty", "") or ""
            if not diff_title:
                continue
            s = _bigram_score(diff_title, title)
            if s > 0:
                matched.append(diff_title)
            # 跟踪该章最匹配难点的维度(用作 narrative_dimension)
            if s > best_score:
                best_score = s
                best_dim = d.get("primary_dimension")

        # 无匹配 → 按章节序号在 5 维轮转兜底(保证每章有维度,叙事不悬空)
        if not best_dim or best_dim not in DEPLOY_DIMS:
            best_dim = DEPLOY_DIMS[idx % len(DEPLOY_DIMS)]

        # 聚焦难点去重 + 限 3 条(避免 prompt 膨胀,控 C4)
        seen = set()
        focus = []
        for m in matched:
            if m not in seen:
                seen.add(m)
                focus.append(m)
            if len(focus) >= 3:
                break

        echo = (
            f"★ 本章叙事职责:从「{best_dim}」维度展开"
            + (f",重点回应难点「{focus[0]}」" if focus else ",呼应总体部署纲领")
            + "。"
        )
        chapters_out[num] = {
            "narrative_dimension": best_dim,
            "difficulty_focus": focus,
            "mainline_echo": echo,
        }

    payload = {
        "_deployment_statement": statement,
        "_deployment_dimensions": deploy.get("deployment_dimensions", []),
        "chapters": chapters_out,
    }
    out = ext / "chapter_narrative.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    n_with_focus = sum(1 for c in chapters_out.values() if c["difficulty_focus"])
    log.info(
        f"  ✓ 写入 chapter_narrative.json: {len(chapters_out)} 章 / "
        f"{n_with_focus} 章有难点聚焦 / 主线声明 {len(statement)} 字"
    )


def _generate_intro_chapter(
    ext: Path,
    facts: dict,
    eval_data: dict,
    deploy: dict | None,
    log,
) -> None:
    """工序 A0: 生成第0章「编制总说明」(叙事锚点章,不调 LLM,纯模板拼接)。

    内容:
      §一 编制依据(从 facts + 评标办法派生)
      §二 项目概况与总体部署 ← 叙事锚点(deployment_statement + 5 维部署)
      §三 施工主线与重点难点响应 ← 贯穿声明(难点→章节响应分布表)
      §四 编制说明(章节数/字数概要)

    不计入页数预算(类似目录页,是编制说明)。
    config bid_coherence.json intro_chapter.enabled=false 时不生成。
    """
    # config 开关
    try:
        from bid_common import get_bid_gen_home
        cfg_path = get_bid_gen_home() / "configs" / "bid_coherence.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    if not (cfg.get("intro_chapter", {}) or {}).get("enabled", True):
        log.info("  intro_chapter.enabled=false,跳过第0章生成")
        return

    # 无 deployment_outline → 无法生成有意义的部署纲领,跳过(降级)
    if not deploy or not deploy.get("deployment_statement"):
        log.info("  deployment_outline 缺失或无 deployment_statement,跳过第0章")
        return

    statement = deploy.get("deployment_statement", "")
    dims = deploy.get("deployment_dimensions", []) or []
    d2d = deploy.get("difficulty_to_dimension", []) or []

    # §一 编制依据
    def _fv(key):
        v = facts.get(key)
        if isinstance(v, dict):
            return str(v.get("value", "") or "")
        return str(v or "")

    project_name = _fv("project_name")
    project_scope = _fv("project_scope")
    eval_method = eval_data.get("evaluation_method", "")

    lines = ["# 编制总说明", ""]
    lines.append("## 一、编制依据")
    lines.append("")
    lines.append(f"本施工组织设计依据以下文件编制:")
    lines.append(f"- {project_name or '本项目'}招标文件及施工图纸")
    lines.append(f"- 现行国家、行业、地方有关施工及验收规范、标准")
    lines.append(f"- 招标文件规定的{'评分办法' if eval_method == 'score' else '合格制'}评审要求")
    lines.append(f"- 项目现场实际条件与本企业同类工程施工经验")
    lines.append("")

    # §二 项目概况与总体部署(叙事锚点)
    lines.append("## 二、项目概况与总体部署")
    lines.append("")
    if project_name or project_scope:
        lines.append(f"**项目概况：** {project_name}。{project_scope}")
        lines.append("")
    lines.append("**总体施工部署纲领（全文施工主线）：**")
    lines.append("")
    lines.append(f"> {statement}")
    lines.append("")
    if dims:
        lines.append("本项目总体施工部署围绕以下五个维度展开，后续各章节均服从此部署纲领：")
        lines.append("")
        for d in dims:
            if isinstance(d, dict):
                lines.append(f"- **{d.get('dim', '')}**：{d.get('summary', '')}")
        lines.append("")

    # §三 施工主线与重点难点响应(贯穿声明)
    lines.append("## 三、施工主线与重点难点响应")
    lines.append("")
    lines.append("本项目以总体施工部署为纲领、以施工进度为主线贯穿全文。针对已识别的重点难点，")
    lines.append("各章节响应分布如下（确保每个难点均有专章系统性回应，杜绝泛泛而谈）：")
    lines.append("")
    if d2d:
        lines.append("| 序号 | 重点/难点 | 主要承载维度 | 响应角度 |")
        lines.append("|---|---|---|---|")
        for i, d in enumerate(d2d, 1):
            if not isinstance(d, dict):
                continue
            lines.append(
                f"| {i} | {d.get('difficulty', '')} | "
                f"{d.get('primary_dimension', '')} | {d.get('angle', '')} |"
            )
        lines.append("")

    # §四 编制说明
    lines.append("## 四、编制说明")
    lines.append("")
    lines.append("本施工组织设计严格按招标文件规定的章节大纲/评分表组织，各章节标题与评审要点")
    lines.append("一一对应。文中涉及的所有工期、质量、安全、环保承诺均为本企业的郑重承诺。")
    lines.append("")

    md = "\n".join(lines)
    out = ext / "00_编制总说明.md"
    out.write_text(md, encoding="utf-8")
    log.info(f"  ✓ 写入第0章 {out.name} ({len(md):,} 字符,不计入页数预算)")


def _figure_count(topic: str) -> int:
    """根据主题估图表数。"""
    fig_map = {
        "工程概况": 0,
        "编制依据": 0,
        "总体施工部署": 1,
        "项目组织管理机构": 1,
        # 招标要求的进度网络图/总平面图集中放在「附图及附表」，
        # 正文章只做文字引用，避免附图错放。
        "施工总进度计划": 0,
        "施工总平面布置": 0,
        "施工方案与技术措施": 3,
        "质量管理体系与措施": 1,
        "工期保证技术组织措施": 1,
        "安全文明施工体系与措施": 1,
        "绿色施工管理体系与措施": 1,
        "季节性施工措施": 1,
        "地下管线及其他地上地下设施的保护加固措施": 0,
        "民工工资保证措施": 0,
        "附图及附表": 2,
        "其他": 0,
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
