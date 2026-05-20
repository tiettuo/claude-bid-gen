#!/usr/bin/env python3
"""
bid_facts_extract.py - Tier 1 项目事实抽取

从 extracted/<主文档>.txt 抽出 13 个项目事实字段,正则优先 + LLM 兜底。
每个字段带 source 元信息(file/page/char_offset/method),供下游审计。

字段:
  project_name / project_code / location / client / agent /
  funding_source / control_price / duration_days /
  start_date / end_date / quality_target / qualification / project_scope

输出:extracted/project_facts.json

策略:
  - 70% 字段纯正则(投标人须知前附表 1.1.X 格式稳定)
  - 控制价从「招标控制价/最高限价/限价」关键词附近抓
  - quality_target / qualification / project_scope 段落型,LLM 兜底
  - 与控制价文件交叉验证(若可读取)

用法:
  python bid_facts_extract.py /path/to/tender/
  python bid_facts_extract.py /path/to/tender/ --no-llm  # 仅正则
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

OK = "✓"
WARN = "⚠"
FAIL = "✗"


# ────────────────────────────────────────────────────────────────────
# 1. 主文档识别 + 页码索引(沿用)
# ────────────────────────────────────────────────────────────────────

_AUX_PATTERNS = [
    r"清单", r"控制价", r"自查表", r"承诺函", r"合同范本",
    r"技术规范书", r"工程量", r"图纸", r"地勘", r"勘探",
    r"设备表", r"仪器", r"附件\d", r"_t\d",
]
_MAIN_PATTERNS = [r"招标文件", r"招标公告", r"招标项目"]


def pick_main_tender_text(extracted_dir: Path) -> Path | None:
    candidates = list(extracted_dir.glob("*.txt"))
    if not candidates:
        return None
    def is_main(p: Path) -> bool:
        n = p.name
        if any(re.search(pat, n) for pat in _AUX_PATTERNS):
            return False
        return any(re.search(pat, n) for pat in _MAIN_PATTERNS)
    mains = [p for p in candidates if is_main(p)]
    if mains:
        mains.sort(key=lambda p: p.stat().st_size, reverse=True)
        return mains[0]
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


_PAGE_HEADER_RE = re.compile(r"^\[第(\d+)页(?:\s*-\s*视觉OCR:[^\]]+)?[^\]]*\]\s*$", re.M)


def page_at_offset(full_text: str, offset: int) -> int | None:
    """二分查找:offset 落在哪一页。"""
    matches = list(_PAGE_HEADER_RE.finditer(full_text))
    if not matches:
        return None
    last_page = None
    for m in matches:
        if m.end() > offset:
            return last_page
        last_page = int(m.group(1))
    return last_page


# ────────────────────────────────────────────────────────────────────
# 2. 正则模式库(13 字段)
# ────────────────────────────────────────────────────────────────────

# 表格格式:`| 1.1.4 | 项目名称 | <值> |` 或 `| 项目名称 | <值> |`
# 注:视觉 OCR 出的 markdown 表格 cell 内可能跨多行,用 re.S + 非贪婪 + lookahead `|` 或 行末
def _table_cell(field_keyword: str) -> str:
    """构造表格单元格匹配模式:抓 keyword 后下一个 cell 的内容。"""
    return rf"{field_keyword}[^|\n]*\|\s*([^|]+?)(?=\n\s*\||\s*\|\s*\n|\n\n|$)"


def _line_field(field_keyword: str) -> str:
    """行格式:`项目名称:<值>`"""
    return rf"{field_keyword}\s*[:：]\s*([^\n|]{{3,200}}?)(?=\n|\||$)"


# 13 字段规则(每字段多个 fallback 正则,按命中顺序取第一个)
FIELD_PATTERNS: dict[str, list[str]] = {
    "project_name": [
        _table_cell(r"项目名称"),
        _table_cell(r"工程名称"),
        _line_field(r"项目名称"),
        _line_field(r"工程名称"),
    ],
    "project_code": [
        r"项目编号[:：]\s*(S\d{15,21})",
        r"招标编号[:：]\s*(\S{8,30})",
        r"\b(S\d{16,21})\b",  # 兜底:文档中任意位置的 S 编号
    ],
    "location": [
        _table_cell(r"建设地点"),
        _table_cell(r"建设地址"),
        _table_cell(r"工程地点"),
        _line_field(r"建设地点"),
    ],
    "client": [
        r"本招标项目[^\n。]*?招标人为\s*([^\n。；;，,]{4,80})",
        r"招标人为\s*([^\n。；;，,]{4,80})",
        # 优先抓"名称："后的第一段(去除地址/电话/联系人冗余)
        r"招标人[^|\n]*\|[^|]*?名称[:：]\s*([^\n地址电话邮箱联系人]+?)(?=\n|地址|电话|邮箱|联系人|\|)",
        r"招标人\s*[:：]\s*名称[:：]\s*([^\n|地址电话联系人]{4,80})",
        _table_cell(r"招标人"),
    ],
    "agent": [
        r"招标代理机构[:：为\s]*([^\n。；;，,]{4,80})",
        r"招标代理机构[^|\n]*\|[^|]*?名称[:：]\s*([^\n地址电话邮箱联系人]+?)(?=\n|地址|电话|邮箱|联系人|\|)",
        r"招标代理机构\s*[:：]\s*名称[:：]\s*([^\n|地址电话联系人]{4,80})",
        _table_cell(r"招标代理机构"),
    ],
    "funding_source": [
        r"建设资金为\s*([^\n。；;，,]{2,80})",
        _table_cell(r"资金来源"),
        _line_field(r"资金来源"),
    ],
    "control_price": [
        # 强匹配:必须带"为/金额/:"等明确赋值符号,排除"项目招标控制价低于1500万的"条件描述
        # 至少 5 位数字防止误抽小金额(实际工程不会 < 10000 元)
        r"招标控制价为[:：]?\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"招标控制价金额[为:：]?\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"招标控制价[(\(]?小写[)\)]?\s*[:：]\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"最高投标限价为[:：]?\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"最高限价为[:：]?\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"投标限价(?:不得超过)?\s*([\d,,]{5,15}\.?\d*)\s*元",
        r"建设资金为\s*([\d.]+\s*万元)",
    ],
    "duration_days": [
        r"计划工期[:：]\s*(\d{1,4})\s*日历?天",
        r"工期[:：]\s*(\d{1,4})\s*日历?天",
        r"总工期[:：]\s*(\d{1,4})\s*日历?天",
        r"(\d{2,4})\s*日历天\s*(?:\(|（)?(?:不含冬季|含)",
    ],
    "start_date": [
        r"计划开工日期\s*[:：]\s*(\d{4}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}日?)",
        r"开工日期\s*[:：]\s*(\d{4}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}日?)",
    ],
    "end_date": [
        r"计划竣工日期\s*[:：]\s*(\d{4}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}日?)",
        r"竣工日期\s*[:：]\s*(\d{4}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}日?)",
    ],
    "quality_target": [
        _table_cell(r"质量要求"),
        _table_cell(r"质量目标"),
        _table_cell(r"质量标准"),
        r"质量(?:标准|目标|要求)\s*[:：]\s*([^\n|,，。]+)",
    ],
    "qualification": [
        # 资质要求多见"房屋建筑工程施工总承包X级"短语
        r"((?:房屋建筑|市政公用|公路|水利水电|建筑装修|机电安装|钢结构|地基基础)?工程?施工总承包\s*(?:[壹贰叁肆伍]|一|二|三|四|五|[1-5])级(?:及以上)?)",
        r"建筑工程专业\s*\(?(?:注册专业)?\)?\s*([壹贰叁肆伍一二三四五]级及以上)",
        r"资质要求[:：]?\s*([^\n。]{10,80})",
    ],
    "project_scope": [
        _table_cell(r"招标范围"),
        _table_cell(r"工作内容"),
        _line_field(r"招标范围"),
    ],
}


# ────────────────────────────────────────────────────────────────────
# 3. 抽取核心
# ────────────────────────────────────────────────────────────────────

def _clean(value: str) -> str:
    """清洗抽取值:去多余空白/换行/<br>/markdown 表格残留。"""
    if not value:
        return ""
    v = value.strip()
    v = re.sub(r"<br\s*/?>", " ", v)
    v = re.sub(r"\s*\n\s*", "", v)  # 去内部换行
    v = re.sub(r"\s+", " ", v)
    v = v.strip(" |：:。.；;,，")
    return v


def _normalize_price(s: str) -> float | None:
    """把'1,905,652.18'或'190.57万元'清洗成 float(元)。"""
    if not s:
        return None
    s = s.replace(",", "").replace(",", "").strip()
    if "万" in s:
        m = re.search(r"(\d+\.?\d*)", s)
        return float(m.group(1)) * 10000 if m else None
    m = re.match(r"^(\d+\.?\d*)$", s)
    if m:
        return float(m.group(1))
    return None


def _normalize_date(s: str) -> str | None:
    """日期标准化为 YYYY-MM-DD。"""
    if not s:
        return None
    m = re.match(r"(\d{4})[-/年]\s*(\d{1,2})[-/月]\s*(\d{1,2})日?", s.strip())
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            return None
    return None


_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:见)?(?:投标人须知前附表|须知前附表|前附表|项目名称|工程名称|本项目|本工程|"
    r"\(?项目名称\)?|（项目名称）|/|无|未明确)$"
)


def _is_bad_project_name(value: str | None) -> bool:
    """识别招标文件模板占位词，避免污染封面/指纹/正文。"""
    if not value:
        return True
    v = _clean(value)
    if len(v) < 6 or len(v) > 120:
        return True
    if _PLACEHOLDER_VALUE_RE.search(v):
        return True
    if re.search(r"见\s*投标人须知", v):
        return True
    return False


def _project_name_candidates(full_text: str) -> list[tuple[str, int, str]]:
    """从首页、招标公告、工程名称等位置收集项目名候选。"""
    patterns = [
        (r"项\s*目\s*名\s*称\s*[:：]\s*([^\n|]{6,120})", "label_project_name"),
        (r"工程名称\s*[:：]\s*([^\n|<]{6,120})", "label工程名称"),
        (r"本招标项目\s*([^\n。；;]{6,120}?项目)\s*已由", "announcement"),
        (r"^([^\n|]{6,120}?项目)\s*$", "line_project"),
    ]
    candidates: list[tuple[str, int, str]] = []
    for pat, source in patterns:
        for m in re.finditer(pat, full_text[:120000], flags=re.M):
            name = _clean(m.group(1))
            if _is_bad_project_name(name):
                continue
            # 清掉少量尾部模板词
            name = re.sub(r"\s*(?:工程|标段)?\s*$", lambda mm: mm.group(0) if "项目" not in name else "", name).strip()
            candidates.append((name, m.start(1), source))
    return candidates


def _repair_project_name(facts: dict, full_text: str, source_file: str, log) -> str | None:
    """当 regex 抽到模板占位值时，用多候选排序修正项目名称。"""
    cur = facts.get("project_name", {}).get("value")
    if not _is_bad_project_name(cur):
        return None

    candidates = _project_name_candidates(full_text)
    if not candidates:
        return None

    def score(item: tuple[str, int, str]) -> tuple[int, int]:
        name, offset, source = item
        s = 0
        if source == "label_project_name":
            s += 80
        elif source == "announcement":
            s += 70
        elif source == "label工程名称":
            s += 60
        if "项目" in name:
            s += 20
        if any(k in name for k in ("招标", "开标", "评标", "投标")):
            s -= 40
        # 越靠前越优先
        return (s, -offset)

    name, offset, source = max(candidates, key=score)
    facts["project_name"] = {
        "value": name,
        "raw": cur,
        "source": {
            "file": source_file,
            "page": page_at_offset(full_text, offset),
            "char_offset": offset,
            "method": f"candidate_repair:{source}",
        },
    }
    log.warning(f"  {WARN} project_name 原值疑似占位({cur!r})，已修正为 {name!r}")
    return name


def extract_field_regex(text: str, patterns: list[str]) -> tuple[str | None, int]:
    """按 patterns 顺序找第一个命中。返回 (value, char_offset)。"""
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.S):
            v = m.group(1).strip()
            if not v:
                continue
            # 过滤明显错误的命中(全数字/超长/含 markdown 控制符)
            if len(v) > 500:
                continue
            if v in ("|", "---", "...", "省略"):
                continue
            return v, m.start(1)
    return None, -1


def _truncate_to_pages(full_text: str, n: int) -> str:
    """截前 n 页文本(用 [第N页] 标记切)。"""
    matches = list(_PAGE_HEADER_RE.finditer(full_text))
    if not matches:
        return full_text[: n * 3000]
    for m in matches:
        if int(m.group(1)) > n:
            return full_text[: m.start()]
    return full_text


def extract_facts(full_text: str, source_file: str, log) -> dict:
    """从主文档抽 13 字段,每字段带 source 元信息。

    搜索窗口分层:
      - client/agent/funding_source/project_scope:前 30 页(投标人须知前附表)
      - control_price:前 50 页(10.12 条款)
      - 其余字段:前 50 页
    用窗口限制是为了避免合同范本(后半部分)的占位符或重复表述误导抽取。
    """
    facts: dict[str, dict] = {}
    # 各字段的搜索范围(页数)
    PAGE_SCOPE: dict[str, int] = {
        "project_name":   30,
        "project_code":   50,
        "location":       30,
        "client":         30,
        "agent":          30,
        "funding_source": 30,
        "control_price":  50,
        "duration_days":  30,
        "start_date":     30,
        "end_date":       30,
        "quality_target": 30,
        "qualification": 100,  # 资质条款分散,放宽
        "project_scope":  30,
    }

    # 字段后处理函数:value → cleaned/typed
    POSTPROCESS = {
        "control_price": lambda v: _normalize_price(v),
        "duration_days": lambda v: int(v) if v.isdigit() else None,
        "start_date": _normalize_date,
        "end_date": _normalize_date,
    }

    for field, patterns in FIELD_PATTERNS.items():
        scope_pages = PAGE_SCOPE.get(field, 50)
        scoped = _truncate_to_pages(full_text, scope_pages)
        raw, offset = extract_field_regex(scoped, patterns)
        if not raw:
            facts[field] = {"value": None, "source": None, "method": "miss"}
            continue
        cleaned = _clean(raw)
        # 类型化处理
        if field in POSTPROCESS:
            typed = POSTPROCESS[field](cleaned)
        else:
            typed = cleaned

        page = page_at_offset(full_text, offset)
        facts[field] = {
            "value": typed,
            "raw": cleaned if cleaned != typed else None,
            "source": {
                "file": source_file,
                "page": page,
                "char_offset": offset,
                "method": "regex",
            },
        }
    return facts


# ────────────────────────────────────────────────────────────────────
# 3b. LLM 兜底:对正则未命中的字段,调 DeepSeek 一次性补抽
# ────────────────────────────────────────────────────────────────────

FIELD_DESCRIPTIONS = {
    "project_name":   "项目名称(完整官方名称)",
    "project_code":   "项目编号/采购编号/招标编号",
    "location":       "建设地点/工程地址(精确到县/镇)",
    "client":         "采购人/招标人/建设单位(完整公司名)",
    "agent":          "采购代理机构/招标代理(完整公司名)",
    "funding_source": "资金来源(如政府采购/财政拨款/自筹)",
    "control_price":  "最高限价/控制价/招标控制价(纯数字,单位元)",
    "duration_days":  "工期(日历天整数;若为月按30天换算)",
    "start_date":     "计划开工日期(YYYY-MM-DD;未明示则 null)",
    "end_date":       "计划竣工日期(YYYY-MM-DD;未明示则 null)",
    "quality_target": "质量目标/质量等级(如合格/达到现行验收规范)",
    "qualification":  "供应商资质要求(完整描述,如「建筑工程总承包三级及以上」)",
    "project_scope":  "招标范围/工程内容简述(1-3 句概述)",
}


def extract_facts_llm_fallback(full_text: str, missing_fields: list[str],
                                source_file: str, log) -> dict:
    """调 DeepSeek 一次 prompt 补抽正则未命中的字段。

    返回 {field: {"value": ..., "source": {...}, "method": "llm_fallback"}}。
    若某字段 LLM 也未抽到,value=None,method 保持 "miss"。
    """
    if not missing_fields:
        return {}

    # 取前 60 页(约 3-4 万字)送 LLM,避免超 context
    snippet = full_text[:80000] if len(full_text) > 80000 else full_text

    field_desc_lines = "\n".join(
        f"- {f}: {FIELD_DESCRIPTIONS.get(f, f)}" for f in missing_fields
    )

    system = (
        "你是专业的招标文件信息抽取助手。从招标文件全文中抽取指定字段值。"
        "字段在文中确实未提及则输出 null。"
        "只输出 JSON,不要任何 markdown 标记、解释、前言、后记。"
    )
    user = (
        f"招标文件(主文档)全文如下(可能含 [第N页] 标记):\n\n"
        f"{snippet}\n\n"
        f"---\n请抽取以下字段:\n{field_desc_lines}\n\n"
        f"输出 JSON,字段名严格使用上述英文键名,例如:\n"
        f'{{"control_price": 169500.00, "duration_days": 40, "start_date": null}}\n\n'
        f"特别注意:\n"
        f"- 数字字段(control_price/duration_days)输出纯数字,不带单位、不带千分位、不带引号\n"
        f"- 日期字段输出 \"YYYY-MM-DD\" 字符串,未明示输出 null\n"
        f"- 文字字段输出完整原文表述,不要概括或同义改写\n"
    )

    try:
        client = LLMClient.load(log=log)
        resp = client.chat(
            model_key="deepseek",
            system=system,
            user=user,
            temperature=0.2,
            max_tokens=2000,
        )
    except Exception as e:
        log.warning(f"  {WARN} LLM 兜底调用失败: {e}")
        return {}

    raw = resp.text.strip()
    # 去 markdown 包围
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        llm_data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"  {WARN} LLM 兜底 JSON 解析失败: {e}")
        log.debug(f"  LLM 原文前 300 字: {raw[:300]}")
        return {}

    result = {}
    for field in missing_fields:
        v = llm_data.get(field)
        if v in (None, "", "null", "无", "未明示", "未明确", "/"):
            result[field] = {"value": None, "source": None, "method": "miss"}
            continue
        # 后处理类型化
        if field == "control_price":
            v = _normalize_price(str(v)) if not isinstance(v, (int, float)) else float(v)
        elif field == "duration_days":
            if isinstance(v, str) and v.isdigit():
                v = int(v)
            elif not isinstance(v, int):
                v = None
        elif field in ("start_date", "end_date"):
            v = _normalize_date(str(v))
        else:
            v = _clean(str(v))

        if v is None:
            result[field] = {"value": None, "source": None, "method": "miss"}
        else:
            result[field] = {
                "value": v,
                "source": {
                    "file": source_file,
                    "page": None,
                    "char_offset": None,
                    "method": "llm_fallback",
                },
            }
    return result


# ────────────────────────────────────────────────────────────────────
# 4. 交叉验证
# ────────────────────────────────────────────────────────────────────

def cross_validate(facts: dict, ext: Path, log) -> list[str]:
    """与控制价文件、文件名等交叉验证。返回 warnings。"""
    warnings = []

    # 1. project_code 与文件名一致性
    code = facts.get("project_code", {}).get("value")
    if code:
        # 在 extracted 目录其他 txt 里查
        all_txt = " ".join(p.read_text(encoding="utf-8")[:5000]
                           for p in ext.glob("*.txt"))
        if all_txt.count(code) < 2:
            warnings.append(
                f"project_code={code} 在其他文件中未交叉出现,可能误抽"
            )

    # 2. 工期 与 (end - start) 对账
    dur = facts.get("duration_days", {}).get("value")
    sd = facts.get("start_date", {}).get("value")
    ed = facts.get("end_date", {}).get("value")
    if dur and sd and ed:
        from datetime import date as _date
        try:
            s = _date.fromisoformat(sd)
            e = _date.fromisoformat(ed)
            actual_days = (e - s).days + 1
            if abs(actual_days - dur) > 5:
                warnings.append(
                    f"工期对账不符:声明 {dur} 天,日期跨度 {actual_days} 天(差 {actual_days - dur})"
                )
        except Exception:
            pass

    # 3. 控制价与单独控制价文件交叉(若存在)
    cp = facts.get("control_price", {}).get("value")
    if cp:
        for p in ext.glob("*控制价*.txt"):
            content = p.read_text(encoding="utf-8")[:50000]
            # 简单查找该数字是否出现
            cp_str = f"{int(cp):,}".replace(",", ",")
            if str(int(cp)) not in content and cp_str not in content:
                warnings.append(
                    f"控制价 {cp} 在 {p.name} 中未交叉验证到"
                )
                break

    return warnings


# ────────────────────────────────────────────────────────────────────
# 5. 主流程
# ────────────────────────────────────────────────────────────────────

# drives 字段:每个 fact 驱动哪些章节/输入
DRIVES = {
    "project_name":   ["brain.section1", "chapter01.封面", "docx.metadata"],
    "project_code":   ["brain.section1", "audit.code_match"],
    "location":       ["brain.section1", "derive.altitude_class"],
    "client":         ["brain.section1", "封面"],
    "agent":          ["brain.section1"],
    "funding_source": ["brain.section1", "chapter01.项目概况"],
    "control_price":  ["brain.section1", "derive.size_class", "rejection.B1.10"],
    "duration_days":  ["brain.section1", "chapter05.进度计划", "rejection.B1.16"],
    "start_date":     ["chapter05.进度计划", "derive.season_set"],
    "end_date":       ["chapter05.进度计划", "derive.spans_winter"],
    "quality_target": ["brain.section1", "chapter08.质量管理"],
    "qualification":  ["brain.section1", "compliance.qualification_check"],
    "project_scope":  ["brain.section1", "brain.section2"],
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--no-llm", action="store_true",
                   help="跳过 LLM 兜底(仅正则)")
    p.add_argument("--force", action="store_true", help="覆盖已有 project_facts.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}", file=sys.stderr)
        return 2

    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在,先跑 bid_extract.py", file=sys.stderr)
        return 2

    log = make_script_logger("bid_facts_extract", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_facts_extract  目录: {tender_dir}")

    out_path = ext / "project_facts.json"
    if out_path.exists() and not args.force:
        log.info(f"  project_facts.json 已存在,跳过(--force 重抽)")
        return 0

    main_txt = pick_main_tender_text(ext)
    if not main_txt:
        log.error("找不到主文档")
        return 2
    full_text = main_txt.read_text(encoding="utf-8")
    log.info(f"  主文档: {main_txt.name} ({len(full_text):,} 字符)")

    # 1. 正则抽
    facts = extract_facts(full_text, main_txt.name, log)

    regex_hit = sum(1 for v in facts.values() if v.get("value") is not None)
    miss = len(facts) - regex_hit
    log.info(f"  正则抽取:命中 {regex_hit}/{len(facts)},缺失 {miss}")

    # 1b. LLM 兜底 (--no-llm 跳过)
    llm_hit = 0
    if not args.no_llm and miss > 0:
        missing_fields = [k for k, v in facts.items() if v.get("value") is None]
        log.info(f"  LLM 兜底:对 {len(missing_fields)} 个未命中字段调 DeepSeek 补抽...")
        llm_results = extract_facts_llm_fallback(
            full_text, missing_fields, main_txt.name, log
        )
        for field, entry in llm_results.items():
            if entry.get("value") is not None:
                facts[field] = entry
                llm_hit += 1
        log.info(f"  LLM 兜底:补抽 {llm_hit}/{len(missing_fields)} 字段")

    _repair_project_name(facts, full_text, main_txt.name, log)

    hit = sum(1 for v in facts.values() if v.get("value") is not None)
    miss = len(facts) - hit

    # 写 drives
    for k, v in facts.items():
        v["drives"] = DRIVES.get(k, [])

    # 2. 交叉验证
    warnings = cross_validate(facts, ext, log)
    for w in warnings:
        log.warning(f"  {WARN} {w}")

    # 3. 简要打印
    for k, v in facts.items():
        val = v.get("value")
        src = v.get("source") or {}
        if val is not None:
            short = (str(val)[:50] + "…") if len(str(val)) > 50 else str(val)
            log.info(f"    {OK} {k:<16} = {short}  [p{src.get('page','?')}]")
        else:
            log.warning(f"    {WARN} {k:<16} = MISS")

    # 4. 写文件
    payload = {
        "facts": facts,
        "summary": {
            "total_fields": len(facts),
            "hit": hit,
            "miss": miss,
            "regex_hit": regex_hit,
            "llm_hit": llm_hit,
            "warnings": warnings,
        },
        "_meta": {
            "source_file": main_txt.name,
            "extracted_by": "bid_facts_extract.py",
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name}")
    log.info("=" * 60)

    return 0 if miss == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
