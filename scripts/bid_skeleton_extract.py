#!/usr/bin/env python3
"""
bid_skeleton_extract.py - Step 1.5 骨架优先提取

在 bid_extract.py 之后、bid_plan_verify.py 之前运行。从 extracted/*.txt 中
强制抽取「评标办法 / 必含大纲 / 废标条款」三类骨架信息为 JSON,作为
brain.md / chapter_plan.md / disqualification_rules.md 的源头依据。

为什么需要这一步:
  三个历史项目(仓储类项目A/学校类/仓储类项目B)的 brain.md 都不同程度漏抄了招标
  文件第七章的「主要技术内容 16 项」必含大纲——但原文确实在 extracted/
  txt 里。根因是 Step 2 让 Claude 自由提取,凭印象写 brain.md。本步骤
  把「骨架」从「细节」中分离出来,先用 LLM 出结构化 JSON,再让 Claude
  把 JSON 翻译成 brain.md(机器校验对账)。

输出:
  extracted/document_map.md         主文档章节-页码索引(纯文本启发式,无 LLM)
  extracted/evaluation_method.json  评标方法 + 评分项原文 + 否决项
  extracted/mandatory_outline.json  必含大纲一级主题 + 附图附表
  extracted/disqualification.json   全部废标/否决条款,每条带 source_page
  extracted/skeleton_extract.log    运行日志

退出码:
  0  全部通过,三份 JSON 已生成
  1  软警告(如 outline 为"未明确",或 LLM 输出条款数偏少)
  2  硬失败(主文档识别失败 / LLM 全部不可用 / JSON 反复解析失败)

用法:
  python bid_skeleton_extract.py /path/to/tender/
  python bid_skeleton_extract.py /path/to/tender/ --model glm
  python bid_skeleton_extract.py /path/to/tender/ --only b2  # 只跑某一项

默认模型:
  骨架抽取(B1 评分表 / B2 必含大纲 / B3 废标条款)固定优先使用 deepseek,
  不再跟随全局 default_model。可用 --model 临时覆盖。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402


OK = "✓"
WARN = "⚠"
FAIL = "✗"
SKELETON_DEFAULT_MODEL = "deepseek"


# ────────────────────────────────────────────────────────────────────
# 1. 主文档识别 + 页码索引
# ────────────────────────────────────────────────────────────────────

# 不是主文档的关键词(优先级排除)
_AUX_PATTERNS = [
    r"清单", r"控制价", r"自查表", r"承诺函", r"合同范本",
    r"技术规范书", r"工程量", r"图纸", r"地勘", r"勘探",
    r"设备表", r"仪器", r"附件\d", r"_t\d",
]
# 主文档强匹配关键词
_MAIN_PATTERNS = [r"招标文件", r"招标公告", r"招标项目"]


def pick_main_tender_text(extracted_dir: Path, log) -> Path | None:
    """启发式挑出"招标主文档"对应的 .txt。

    策略:文件名含'招标文件'+不含辅助关键词;失败时退化到最大的 .txt。
    """
    candidates = list(extracted_dir.glob("*.txt"))
    if not candidates:
        log.error("extracted/ 下没有任何 .txt 文件")
        return None

    def is_main(p: Path) -> bool:
        name = p.name
        if any(re.search(pat, name) for pat in _AUX_PATTERNS):
            return False
        if any(re.search(pat, name) for pat in _MAIN_PATTERNS):
            return True
        return False

    mains = [p for p in candidates if is_main(p)]
    if mains:
        # 多个时取最大
        mains.sort(key=lambda p: p.stat().st_size, reverse=True)
        log.info(f"主文档识别: {mains[0].name} ({mains[0].stat().st_size:,} 字节)")
        return mains[0]

    # 兜底:取最大的 .txt
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    log.warning(f"未匹配到强主文档,退化到最大 txt: {candidates[0].name}")
    return candidates[0]


# 匹配 [第N页] 或 [第N页 - 视觉OCR:xxx] 或 [第N页:扫描页...]
_PAGE_HEADER_RE = re.compile(r"^\[第(\d+)页(?:\s*-\s*视觉OCR:([^\]]+))?[^\]]*\]\s*$", re.M)


@dataclass
class PageInfo:
    page_num: int           # 1-based
    is_vision: bool
    vision_model: str       # qwen_vl / glm_ocr / paddle_fallback / "" 普通
    text_start: int         # 在主文档 txt 中的字符偏移
    text_end: int           # 不含


def build_page_index(full_text: str) -> list[PageInfo]:
    """解析 bid_extract 写入的 [第N页] 段头,得到每页的字符区间。"""
    pages: list[PageInfo] = []
    matches = list(_PAGE_HEADER_RE.finditer(full_text))
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        vision_model = (m.group(2) or "").strip()
        is_vision = bool(vision_model)
        text_start = m.end()
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        pages.append(PageInfo(
            page_num=page_num,
            is_vision=is_vision,
            vision_model=vision_model,
            text_start=text_start,
            text_end=text_end,
        ))
    return pages


def page_at_offset(pages: list[PageInfo], offset: int) -> int | None:
    """二分查找:某字符偏移所在的页码。"""
    lo, hi = 0, len(pages)
    while lo < hi:
        mid = (lo + hi) // 2
        if offset < pages[mid].text_start:
            hi = mid
        elif offset >= pages[mid].text_end:
            lo = mid + 1
        else:
            return pages[mid].page_num
    return None


# ────────────────────────────────────────────────────────────────────
# 2. document_map.md(纯启发式,不调 LLM)
# ────────────────────────────────────────────────────────────────────

# 招标文件标准章节(GB 标准格式 + 西藏地方变体)
_CHAPTER_PATTERNS = [
    r"^第[一二三四五六七八九十]+章\s+\S",              # 第一章 招标公告
    r"^第\s*\d+\s*章\s+\S",                          # 第1章 招标公告
]
_CHAPTER_RE = re.compile("|".join(_CHAPTER_PATTERNS), re.M)


def build_document_map(full_text: str, pages: list[PageInfo]) -> list[dict]:
    """识别主文档的"第X章"标题位置,返回 [{title, first_page, char_offset}]。"""
    chapters: list[dict] = []
    for m in _CHAPTER_RE.finditer(full_text):
        # 取这一行
        line_end = full_text.find("\n", m.start())
        if line_end == -1:
            line_end = len(full_text)
        title = full_text[m.start():line_end].strip()
        # 过滤误识别(如表格里随便一句"第七章规定")
        if len(title) > 50 or not re.match(r"^第[一二三四五六七八九十\d]+\s*章", title):
            continue
        page = page_at_offset(pages, m.start())
        chapters.append({
            "title": title,
            "first_page": page,
            "char_offset": m.start(),
        })

    # 去重:相同 title + 相邻 char_offset(< 100 字符)合并
    deduped: list[dict] = []
    for c in chapters:
        if deduped and deduped[-1]["title"] == c["title"] and \
                c["char_offset"] - deduped[-1]["char_offset"] < 100:
            continue
        deduped.append(c)
    return deduped


def write_document_map(
    main_txt: Path, chapters: list[dict], pages: list[PageInfo], out_path: Path
):
    """写 document_map.md:章节-页码索引 + 视觉 OCR 页统计。"""
    total_pages = max((p.page_num for p in pages), default=0)
    vision_pages = sorted({p.page_num for p in pages if p.is_vision})

    lines = [
        "# 招标主文档结构索引",
        "",
        f"**主文档:** `{main_txt.name}`  ",
        f"**总页数:** {total_pages}  ",
        f"**视觉 OCR 页:** {len(vision_pages)} 页",
        "",
        "## 章节-页码索引",
        "",
    ]
    if not chapters:
        lines.append("(未识别到第X章标题,主文档可能用其他章节命名方式)")
    else:
        lines.append("| # | 章节标题 | 起始页 |")
        lines.append("|---|---------|-------|")
        for i, c in enumerate(chapters, 1):
            lines.append(f"| {i} | {c['title']} | p{c['first_page']} |")

    if vision_pages:
        lines.extend([
            "",
            "## 视觉 OCR 页",
            "",
            "(下列页因含废标/暗标/评分/大纲关键词或为扫描页,已经过 Qwen-VL/GLM-OCR 重抽,"
            "其原文比 pdfplumber 更可信。)",
            "",
            "页码:" + ", ".join(f"p{n}" for n in vision_pages),
        ])

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# 3. LLM 提取:B1/B2/B3 三个强制 prompt
# ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一名资深招投标分析师,负责从招标文件原文中精准抽取结构化骨架信息。

铁律:
1. 只输出 JSON,不要任何解释、markdown 代码块标记或额外文字。
2. 每条输出必须可在原文中找到出处。原文里没说的字段填 null 或空数组,绝不编造。
3. 引用原文时,必须把页码标到字段里(source_page),取自原文的 [第N页] 标记。
4. 同一份招标文件不同条款来自不同页是正常的;若大量条款都标在同一页,你的提取大概率有问题。
"""


def _strip_json_fence(text: str) -> str:
    """LLM 偶尔会输出 ```json ... ``` 包围,去掉。"""
    text = text.strip()
    if text.startswith("```"):
        # 去第一行 ``` 或 ```json 和最后一行 ```
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _parse_json_with_retry(client: LLMClient, model: str, system: str, user: str,
                          max_attempts: int = 2, log=None,
                          max_tokens: int = 12000) -> dict | None:
    """调 LLM 拿 JSON,失败时重试一次,提示更严格。

    max_tokens 默认 12000(B1/B2 够用);B3 需枚举全文 28+ 条废标条款,输出会撑爆
    12000 → 被截断 → JSONDecodeError,故 B3 调用处传更大值。
    注意:bid_llm_client._call_with_retry 用 `mt = max_tokens or m.get("max_tokens")`,
    传入值直接透传给 API,不会被模型 config 的 max_tokens 夹住。骨架默认模型 deepseek
    (deepseek-v4-flash)config max_tokens 已是 16000,支持本处更大值;若改用其它模型
    (多数 config 仅 8000),调用方须自行确认目标模型真实 API 输出上限,勿盲目放大。
    """
    cur_max_tokens = max_tokens
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.chat(
                model_key=model,
                system=system,
                user=user,
                temperature=0.2,  # 低温,稳定
                max_tokens=cur_max_tokens,
            )
            raw = _strip_json_fence(resp.text)
            return json.loads(raw)
        except json.JSONDecodeError as e:
            if log:
                log.warning(f"  [{model}] JSON 解析失败 (尝试 {attempt}/{max_attempts}): {e}")
                log.debug(f"  原文前 500 字: {raw[:500] if 'raw' in dir() else resp.text[:500]}")
            if attempt < max_attempts:
                # 第二次:既加严格提示,也抬高 max_tokens(常见根因就是输出被截断)
                cur_max_tokens = int(cur_max_tokens * 1.5)
                user = user + "\n\n上次输出无法解析为 JSON,请严格遵守"
                user += "「只输出 JSON,无任何 markdown 标记」的要求重新生成。"
        except Exception as e:
            if log:
                log.warning(f"  [{model}] 调用异常: {e}")
            if attempt >= max_attempts:
                raise
    return None


def choose_skeleton_model(args_model: str | None, client_config: dict) -> str:
    """骨架抽取默认使用 DeepSeek；命令行 --model 优先。"""
    return args_model or SKELETON_DEFAULT_MODEL


# ----- B1: 评标办法 -----

B1_USER_TEMPLATE = """以下是某招标文件的全文(每页用 [第N页] 标记开头),请抽取「评标办法」骨架。

# 输出 JSON schema(严格遵守字段名)

{{
  "evaluation_method": "score" | "passfail",
  "method_chinese": "综合评估法 / 简易综合评估法 / 经评审最低投标价法 / ...",
  "total_tech_score": <数字>,
  "tech_score_is_passfail": <true|false>,
  "sections_found": ["技术", "项目管理"],
  "scoring_items": [
    {{
      "id": "1.1",
      "section": "技术" | "项目管理" | "投标报价" | "其他因素" | "未分类",
      "title_original": "施工方案与技术措施",
      "score": 3,
      "criteria_original": "工程质量保证措施计划是否全面...",
      "source_page": 78,
      "source_page_method": "ocr" | "text"
    }}
  ],
  "veto_items": [
    {{
      "text_original": "投标报价超过控制价的",
      "source_page": 21,
      "source_page_method": "ocr" | "text"
    }}
  ],
  "notes": "如有特殊说明,放这里;否则填 null"
}}

# 关键判断规则

- evaluation_method = "passfail":施工组织设计为「合格制」「不计分」「合格/不合格」
  这种情况下 total_tech_score = 0 且 tech_score_is_passfail = true,
  scoring_items 应包含商务标各项(报价/信用/业绩等)。
- evaluation_method = "score":施工组织设计在评分表里有具体分值。
- 招标文件常见在「第三章 评标办法」「评标办法前附表」「评分细则」等章节;
  请认真阅读这些段落原文,不要凭印象写。
- source_page 必须来自原文 [第N页] 标记,不是你估计的。同一项原文跨多页时取首页。
- source_page_method:若该页 [第N页] 后跟 "- 视觉OCR:..." 标记则填 "ocr",否则 "text"。

# ★ section 字段归类规则(必须遵守,否则校验拦截)

scoring_items 必须逐条标注 section 字段,归类到 5 种之一:
  - "技术":施工方案、质量、安全、环保、进度、劳动力、机械、保修、总平面、重难点 等
  - "项目管理":人员资质(建造师/技术负责人/五大员)、类似业绩、管理机构 等
  - "投标报价":投标报价相关项
  - "其他因素":企业信用、财务状况、奖项、履约记录 等
  - "未分类":确实无法归到以上 4 类的(尽量少用)

**归类依据**:看原文章节号前缀(如 2.2.4(1)=技术 / 2.2.4(2)=项目管理 / 2.2.4(3)=报价)
和 notes 字段的备注(常有"技术20+项目管理25+投标报价45+其他10"这类总结)。
不要把报价/信用/人员资质归到"技术"——它们不是施工组织设计要写的内容。

sections_found:列出你在评分表中实际找到的所有 section 类型,如 ["技术","项目管理","投标报价","其他因素"]。

# 招标文件原文(可能很长,完整阅读)

{tender_text}




只输出 JSON。"""
def extract_b1_evaluation(tender_text: str, client: LLMClient, model: str, log) -> dict:
    log.info("[B1] 抽取评标办法...")
    user = B1_USER_TEMPLATE.format(tender_text=tender_text)
    payload = _parse_json_with_retry(client, model, SYSTEM_PROMPT, user, log=log)
    if not payload:
        raise RuntimeError("B1 评标办法抽取失败")
    return payload


_COMMERCIAL_KEYWORDS = (
    "报价", "投标价", "总价", "信用", "业绩", "类似工程",
    "项目经理", "班子", "财务", "履约", "资质", "奖项"
)
_TECH_KEYWORDS = (
    "施工组织设计", "施组", "技术方案", "技术标", "总平面",
    "进度计划", "质量保证", "安全文明", "项目管理", "重难点"
)


def auto_detect_hybrid(payload: dict) -> tuple[bool, list[str]]:
    """R5 防御:检测「商务标评分 + 施组合格制」混合制场景,强制归 passfail。

    判定条件(任一成立即视为混合制):
    1. tech_score_is_passfail=true 且 scoring_items 含 commercial_score>0 的项
    2. evaluation_method=score 但 scoring_items 中标题含"施工组织设计/施组/技术标"
       且 score=0,同时其他项 score>0
    3. method_chinese 含"合格制""不计分"

    副作用:
    - 写 `_hybrid_mode=true` 让下游(brain_translate / chapter_plan)识别
    - 写 `_hybrid_commercial_items` 列出商务标项目供 brain 第五节抄录
    - 强制 evaluation_method=passfail
    """
    issues: list[str] = []
    method = payload.get("evaluation_method")
    items = payload.get("scoring_items") or []
    if not items:
        return False, []

    def _is_tech(title: str) -> bool:
        return any(k in (title or "") for k in _TECH_KEYWORDS)

    def _is_commercial(title: str) -> bool:
        return any(k in (title or "") for k in _COMMERCIAL_KEYWORDS)

    commercial_items = [i for i in items if _is_commercial(i.get("title_original", ""))]
    tech_items = [i for i in items if _is_tech(i.get("title_original", ""))]
    method_chinese = (payload.get("method_chinese") or "").lower()
    method_chinese_signals = ("合格制" in method_chinese or "不计分" in method_chinese
                              or "passfail" in method_chinese)

    is_hybrid = False
    reason = ""

    # 信号 1:tech_passfail=true + 商务标有分
    if payload.get("tech_score_is_passfail") is True:
        commercial_score = sum((i.get("score") or 0) for i in commercial_items)
        if commercial_score > 0:
            is_hybrid = True
            reason = f"tech_score_is_passfail=true 且商务标合计 {commercial_score} 分"

    # 信号 2:技术标项 score=0 + 其他项 score>0
    if not is_hybrid and tech_items:
        tech_score_total = sum((i.get("score") or 0) for i in tech_items)
        other_score_total = sum((i.get("score") or 0) for i in items if i not in tech_items)
        if tech_score_total == 0 and other_score_total > 0:
            is_hybrid = True
            reason = (f"技术标项(施组/技术方案等)合计 0 分,"
                      f"非技术项合计 {other_score_total} 分 → 商务评分+施组合格制")

    # 信号 3:method_chinese 显式声明合格制/不计分
    if not is_hybrid and method_chinese_signals and method == "score":
        is_hybrid = True
        reason = f"method_chinese='{payload.get('method_chinese')}'含合格制/不计分信号"

    if not is_hybrid:
        return False, []

    # 触发硬告警 + 强制 passfail
    msg = (
        f"★ 混合制项目识别:{reason}。evaluation_method 已强制改为 'passfail';"
        f"brain.md/chapter_plan 必须按合格制写,章节锚点 1:1 对齐 mandatory_outline。"
    )
    issues.append(msg)

    if method == "score":
        payload["evaluation_method"] = "passfail"
    payload["tech_score_is_passfail"] = True
    payload["_hybrid_mode"] = True
    payload["_hybrid_reason"] = reason
    payload["_hybrid_commercial_items"] = [
        {
            "title": i.get("title_original"),
            "score": i.get("score"),
            "source_page": i.get("source_page"),
        }
        for i in commercial_items if (i.get("score") or 0) > 0
    ]
    return True, issues


def _validate_scoring_sections(payload: dict, log) -> tuple[bool, list[str]]:
    """C 方案:编号规则核验 LLM 的 section 归类。

    评分表条目编号通常是固定格式:
      - 2.2.4(1)-x → 技术
      - 2.2.4(2)-x → 项目管理
      - 2.2.4(3)-x → 投标报价
      - 2.2.4(4)-x → 其他因素
      - 1.x → 技术
      - 2.x → 项目管理
      - 3.x → 投标报价
      - 4.x → 其他因素

    LLM 归类和编号规则不一致时:以编号规则为准,记录差异日志。
    任何 section 字段缺失的条目自动补填编号规则值。
    返回 (has_conflict, messages)。
    """
    import re

    SECTION_BY_GROUP = {"1": "技术", "2": "项目管理", "3": "投标报价", "4": "其他因素"}
    SECTION_BY_PREFIX = {"1": "技术", "2": "项目管理", "3": "投标报价", "4": "其他因素"}

    def _expected_section(item_id: str) -> str | None:
        """从评分项编号推断 section。

        西藏范本常用 `2.2.4(1)-1` 这类编号,真正的分组在括号内,
        不能拿开头的 `2` 当作项目管理。旧逻辑正是在这里把技术 20 分误归类。
        """
        m = re.search(r"2\.2\.4\s*[\(（]\s*([1-4])\s*[\)）]", item_id)
        if m:
            return SECTION_BY_GROUP.get(m.group(1))

        # 兼容 `2.2.4(3)` 无尾号、全角括号等已经由上方覆盖。
        # 其他常见简表才按首段 1/2/3/4 解释。
        prefix = item_id.split(".")[0]
        return SECTION_BY_PREFIX.get(prefix)

    messages: list[str] = []
    has_conflict = False
    items = payload.get("scoring_items") or []

    fixed_count = 0
    conflict_count = 0
    for item in items:
        item_id = (item.get("id") or "").strip()
        if not item_id:
            continue
        expected = _expected_section(item_id)
        if not expected:
            continue  # 无法按编号规则判断,信任 LLM

        llm_section = item.get("section")
        if not llm_section:
            item["section"] = expected
            fixed_count += 1
        elif llm_section != expected:
            item["section"] = expected  # 编号规则优先
            conflict_count += 1
            title = item.get("title_original", "")
            messages.append(
                f"section 冲突: {item_id} '{title[:30]}' LLM→{llm_section} 编号规则→{expected} (已用编号规则)"
            )

    if fixed_count:
        messages.append(f"补填 section: {fixed_count} 条 (LLM 未填,按编号规则自动归类)")
    if conflict_count:
        has_conflict = True
        messages.append(f"section 冲突: {conflict_count} 条 LLM 归类与编号规则不一致,已用编号规则覆盖")

    # sections_found 自动推算
    sections_seen = set(i.get("section", "未分类") for i in items if i.get("section"))
    payload["sections_found"] = sorted(sections_seen)

    if log:
        for m in messages:
            log.warning(f"[B1-section] {m}")

    return has_conflict, messages


def validate_b1(payload: dict) -> tuple[bool, list[str]]:
    """B1 schema 校验 + 规范化,返回 (ok, warnings)。

    规范化规则(R5 防御):tech_score_is_passfail=true 时强制
    evaluation_method="passfail",即便 LLM 因为商务标有具体分值而判为
    "score"。对下游 brain.md/chapter_plan 而言,只有"施组本身是否计分"
    决定章节锚点策略,商务标怎么评不影响。

    ★ C 方案新增:编号规则核验 LLM 的 scoring_item.section 归类,
    冲突时编号规则优先(技术/项目管理/投标报价/其他因素)。
    """
    warnings = []
    method = payload.get("evaluation_method")
    if method not in ("score", "passfail"):
        # LLM 没识别出评标方法(超长文档/罕见表述);默认 passfail 兜底,不阻断
        warnings.append(
            f"LLM 未识别 evaluation_method (返回 {method!r}),默认 passfail 兜底;"
            f"如实际是评分制,人工把 brain 第五节填准"
        )
        payload["evaluation_method"] = "passfail"
        payload.setdefault("tech_score_is_passfail", True)
        payload.setdefault("total_tech_score", 0)
        payload.setdefault("scoring_items", [])
        payload.setdefault("veto_items", [])
        method = "passfail"

    # 规范化 a:tech_score_is_passfail=true 且 method=score 时直接修正
    if payload.get("tech_score_is_passfail") is True and method == "score":
        warnings.append(
            "evaluation_method 自动从 'score' 修正为 'passfail' "
            "(tech_score_is_passfail=true 优先,即商务标有分但施组合格制)"
        )
        payload["evaluation_method"] = "passfail"
        method = "passfail"

    # 规范化 b:auto_detect_hybrid 全方位检测混合制(R5 多信号防御)
    hybrid_triggered, hybrid_msgs = auto_detect_hybrid(payload)
    if hybrid_triggered:
        warnings.extend(hybrid_msgs)
        method = payload["evaluation_method"]  # 可能被 hybrid 改了

    items = payload.get("scoring_items") or []
    if not items:
        warnings.append("scoring_items 为空")
    else:
        total = sum(i.get("score") or 0 for i in items)
        declared = payload.get("total_tech_score")
        if method == "score" and declared and abs(total - declared) > 0.5:
            warnings.append(f"分值合计 {total} ≠ total_tech_score {declared}")
    if not payload.get("veto_items"):
        warnings.append("veto_items 为空(招标文件几乎不可能没有否决项,请人工核对)")

    # C 方案:编号规则核验 section 归类
    _has_section_conflict, section_msgs = _validate_scoring_sections(payload, None)
    warnings.extend(section_msgs)
    return True, warnings


# ----- B2: 必含大纲 -----

B2_USER_TEMPLATE = """以下是某招标文件的全文,请抽取「施工组织设计编制大纲(必含一级主题清单)」。

# 背景

许多招标文件在「第七章 技术标准和要求」或「技术规范书」「主要技术内容」等章节中,
明确规定了投标人施组应包含的一级主题清单。例如:

> 主要技术内容包括:1、工程概况 2、编制依据 3、总体施工部署 4、项目组织管理机构
> 5、施工总进度计划 6、施工总平面布置 7、施工方案与技术措施 8、质量管理体系与措施
> 9、工期保证技术组织措施 10、安全文明施工体系与措施 11、绿色施工管理体系与措施
> 12、季节性施工措施 13、地下管线及其他地上地下设施的保护加固措施 14、民工工资保证措施
> 15、附图及附表 16、其他

合格制项目下,这就是事实上的章节锚点;评分制项目下,这是评分表之外的「必含底线」。

# 输出 JSON schema

{{
  "found": true | false,
  "source_chapter": "第七章 技术标准和要求",
  "source_page_start": 149,
  "source_page_end": 159,
  "source_page_method": "ocr" | "text",
  "topics": [
    {{
      "index": 1,
      "title_original": "工程概况",
      "subsections_original": ["项目主要情况", "施工条件"],
      "notes": "可选,如该主题有特殊要求"
    }}
  ],
  "attachments": [
    {{
      "label": "附表一",
      "title_original": "拟投入本标段的主要施工设备表"
    }}
  ],
  "fallback_reason": "若 found=false,说明为何没找到;否则填 null"
}}

# 识别规则

- 在原文中按下列任一关键词所在章节查找,**完整抄录主题清单**(不概括/合并):
  「施工组织设计编制要求」「施工组织设计编制大纲」「施工组织总设计」
  「施组编制」「主要技术内容」「技术标准和要求」「技术规范书」「技术规范」
  「投标文件构成」「投标文件组成」「施工组织设计应包括」「施工组织设计应包含」
  「施工组织设计内容」
- topics 必须按原文顺序,index 从 1 开始;每条 title_original **逐字抄原文**,
  哪怕原文有错别字也保留(后续 brain 阶段才修订)。
- subsections_original 是该主题下的二级节(若原文给了),没给就是空数组。
- 附图附表的 label 用原文(附表一/附表四/附图1...),title 抄原文。
- 若全文确实没有任何明确的大纲约定(只有评分表),found=false,fallback_reason 写明
  "招标文件未明确规定施组编制大纲,章节锚点回落到评分表/GB/T 50502 通用结构"。

# 招标文件原文

{tender_text}

只输出 JSON。"""


def extract_b2_outline(tender_text: str, client: LLMClient, model: str, log) -> dict:
    log.info("[B2] 抽取必含大纲...")
    user = B2_USER_TEMPLATE.format(tender_text=tender_text)
    payload = _parse_json_with_retry(client, model, SYSTEM_PROMPT, user, log=log)
    if not payload:
        raise RuntimeError("B2 必含大纲抽取失败")
    return payload


def validate_b2(payload: dict) -> tuple[bool, list[str]]:
    warnings = []
    if not payload.get("found"):
        warnings.append("LLM 报告未找到必含大纲(found=false),将回落到评分表/通用结构")
        return True, warnings
    topics = payload.get("topics") or []
    if len(topics) < 5:
        warnings.append(f"topics 数量偏少 ({len(topics)} < 5),请人工核对原文")
    if not payload.get("source_page_start"):
        warnings.append("source_page_start 缺失")
    return True, warnings


# ----- B3: 废标条款全表 -----

B3_USER_TEMPLATE = """以下是某招标文件的全文,请抽取**全部**废标/否决/无效投标条款 **以及隐含硬约束条款**。

# 输出 JSON schema

{{
  "clauses": [
    {{
      "category": "报价" | "资格" | "形式" | "格式" | "评审" | "硬约束" | "其他",
      "constraint_type": "page_limit" | "word_limit" | "format" | "deadline" | "scope" | "color" | "binding" | "qualification" | "price" | null,
      "text_original": "投标报价超过招标控制价的",
      "consequence": "否决投标" | "废标" | "无效" | "不予受理" | "限制投标" | "视为不响应" | "其他",
      "source_file_hint": "如原文里有具体条款编号(B1.10/3.1.2/10.16 等)就抄,否则填 null",
      "source_page": 75,
      "source_page_method": "ocr" | "text"
    }}
  ],
  "summary": {{
    "total_count": <数字>,
    "by_category": {{"报价": 3, "资格": 5, "形式": 4, "格式": 8, "评审": 2, "硬约束": 3}}
  }}
}}

# 两类必抽条款

## 类一:结果性废标条款(字面出现"否决/废标/无效"等)
- **关键词清单(13 个)**:「否决投标」「废标」「无效投标」「不予受理」「限制参加」
  「视为撤销」「视为不响应」「重大偏差」「实质性偏差」「直接否决」「一票否决」
  「按废标处理」「视为无效」
- 这类 category 通常是 "报价/资格/形式/格式/评审",constraint_type 通常填 null
  (除非该条同时也是硬约束,见下)

## 类二:**隐含硬约束条款**(字面不出现废标词,但违反必触发兜底否决)— ★必抽,历史漏抽点

招标文件常把页数/字数/装订/格式硬性参数写成"不得超过 X""必须 Y",**字面不出现废标关键词**,
LLM 按结果词关键词搜索会漏抽。但这些约束一旦违反,会触发兜底条款"未响应实质性要求 → 否决投标"
(招标常见 p55-70 评标办法前附表)。

### 约束性关键词(13 个,与对象词组合使用)
- **数量约束**:「不得超过」「不得少于」「至少」「至多」「最多」「最少」「不少于」「不多于」
- **强制约束**:「必须」「应当」「应于...前」「不得」「禁止」
### 对象词(与约束词搭配出现时必抽)
「页数」「页」「字数」「字」「颜色」「彩色」「装订」「字体」「字号」「行距」
「时间」「天」「日」「小时」「比例」「金额」「百分」「保证金」「有效期」「响应期」
「业绩」「资质」「资格」「人员」「证书」「项目经理」「技术负责人」

### 必抽典型案例(基于历史项目)
- **"中小型项目总页数不得超过 70 页"** (西藏 简易综合评估法 p20-30) — page_limit
- **"投标有效期不得少于 90 日历天"** — deadline
- **"投标保证金有效期应与投标有效期一致"** — deadline
- **"项目经理须为本单位注册一级建造师"** — qualification
- **"投标文件不得彩色印刷,装订采用胶装"** — color/binding
- **"投标报价不得超过招标控制价"** — price (也算结果性废标)

### 这类条款的 constraint_type 选值
- `page_limit` 总页数/章节页数硬顶
- `word_limit` 字数硬顶
- `format` 字体/字号/行距/排版要求
- `deadline` 时间/有效期/截止日期
- `scope` 投标范围/工作内容
- `color` 颜色/彩色要求
- `binding` 装订方式
- `qualification` 人员资质/单位资质
- `price` 报价限制

# 抽取规则

- **不漏一条**。即便条款写得很罗嗦,也要拆成独立条目。
- 每条来自不同页是正常的。**严禁把所有条款都标到同一页**——若你发现自己想这样做,
  说明你没认真读原文,请重新查找每条的真实出处。
- 招标文件常见条款分布:
  * 投标人须知前附表(报价/工期/有效期/页数等强制条款)→ 通常 p1-30 ★含大量硬约束
  * 评标办法第3章(否决投标条件)→ 通常 p50-100
  * 评标办法前附表 + 详细评审章 → "未响应实质性要求"自动否决的兜底
  * 投标文件格式第8章(签字盖章/装订/暗标格式)→ 通常 p100-200 ★含格式硬约束
- consequence 字段从原文真实选词:有的项目会写"按无效投标处理",有的写"否决投标",
  按原文抄,不要统一改写。**硬约束类条款** consequence 多为"视为不响应"或"不予受理"。

# 招标文件原文

{tender_text}

只输出 JSON。"""


# ----- B3 隐含硬约束 grep fallback(双保险:即便 LLM 漏抽也能补) -----

# 约束词 × 对象词组合正则。每条 (pattern, constraint_type, description)
_IMPLICIT_CONSTRAINT_PATTERNS: list[tuple[str, str, str]] = [
    # 页数硬顶
    (r"(?:总?页数|篇幅|正文)\s*(?:不得超过|不超过|不得多于|限定为?|应不大于|不大于)\s*\d+\s*页",
     "page_limit", "页数硬顶"),
    (r"(?:不得超过|不超过|不得多于|限定为?|应不大于|不大于)\s*\d+\s*页",
     "page_limit", "页数硬顶(简式)"),
    # 字数硬顶
    (r"(?:不得超过|不超过|不得多于|应不大于|不大于)\s*\d+\s*(?:字|个字)",
     "word_limit", "字数硬顶"),
    # 颜色/装订
    (r"不得(?:使用)?彩色(?:印刷|打印)?", "color", "不得彩色"),
    (r"(?:必须|应当?|采用)\s*(?:胶装|线装|无线胶装|平装)", "binding", "装订方式硬性要求"),
    # 字号/字体/行距(R5/R7 防御)
    (r"(?:字号|字体|行距|页边距)\s*(?:不得|必须|应当?|采用|为)[\s\S]{0,30}?(?:(?:小)?[一二三四五]号|宋体|黑体|仿宋|\d+(?:\.\d+)?\s*(?:cm|mm|磅|pt))",
     "format", "字体/字号/行距硬性"),
    # 时间硬约束(投标有效期/响应期)
    (r"(?:投标有效期|响应有效期|质保期|工期)\s*(?:不得少于|不少于|至少|不得短于)\s*\d+\s*(?:日|天|月|年|个月)",
     "deadline", "时间硬约束"),
    (r"(?:应于|须于|必须于)[\s\S]{0,20}?前(?:递交|送达|到达|提交|完成)",
     "deadline", "截止时间"),
    # 资质硬约束(抓整句直至句末标点)
    (r"(?:项目经理|技术负责人|总工程师)\s*(?:须|必须|应当?)[^。；;\n]{1,80}",
     "qualification", "关键人员资质"),
    (r"(?:投标人|供应商|承包人)\s*(?:须|必须|应当?)\s*(?:具有|具备|持有|拥有)[^。；;\n]{1,80}?(?:资质|证书|许可证|备案)",
     "qualification", "单位资质"),
    # 报价硬约束
    (r"(?:投标报价|报价|总价)\s*(?:不得超过|超过|高于|大于)\s*(?:招标控制价|最高限价|预算)",
     "price", "报价上限"),
    # ── v3.1 扩展约束词（P0.5）──
    # 暗标全文适用
    (r"(?:技术标|施工组织设计)\s*(?:采用|为|实行)\s*暗标", "blind_format", "暗标要求"),
    # 签字盖章
    (r"(?:须|必须|应当?|应)\s*(?:签字|盖章|签章|签字或盖章|签字并盖章)", "format", "签字盖章硬要求"),
    # 密封要求
    (r"(?:投标文件|标书)\s*(?:须|必须|应当?)\s*密封", "format", "密封要求"),
    # 副本/正本数量
    (r"(?:副本|正本)\s*(?:数量|份数)?\s*(?:为|不少于|至少)\s*\d+\s*份", "format", "正本副本份数"),
    # 否则触发链
    (r"(?:否则|逾期|未\s*(?:响应|提交|提供|满足|达到))\s*(?:视为|按|以)\s*(?:不响应|无效|废标|否决|放弃|撤回)",
     "hard_constraint", "否则触发链"),
    # 业绩硬约束
    (r"(?:类似|同类|近[三五]年)\s*(?:业绩|项目|工程)\s*(?:不少于|至少|不得少于)\s*\d+\s*(?:项|个)",
     "qualification", "业绩数量硬约束"),
    (r"(?:合同金额|中标金额|单项合同额)\s*(?:不少于|不低于|至少)\s*[\d.,]+\s*(?:万元|元)",
     "qualification", "业绩金额硬约束"),
    # 强制性章节/内容要求
    (r"(?:施工组织设计|技术标)\s*(?:应包含|应包括|必须包含|必须包括|须涵盖)",
     "scope", "必含内容要求"),
    (r"(?:投标文件|技术标)\s*(?:应包括|应包含|组成).{0,20}?(?:包括|包含)\s*[：:]",
     "scope", "投标文件组成"),
    # 页数区间格式（如"50-70页"）
    (r"\d{2,3}\s*[-–—]\s*\d{2,3}\s*页", "page_limit", "页数区间"),
    (r"(?:页数|总页数|篇幅)\s*(?:为|限定为|控制在?)\s*\d{2,3}\s*页",
     "page_limit", "页数限定(等值)"),
    # 打印要求
    (r"(?:单面|双面|黑白|彩色)\s*打印", "format", "打印方式"),
    # 目录要求
    (r"(?:须|必须|应当?)\s*(?:有|包含|编制)\s*目录", "format", "目录硬要求"),
    # 盖章动作
    (r"(?:加盖|盖)\s*(?:公章|单位章|法人章|骑缝章)", "format", "盖章要求"),
    # 有效期的数值约束
    (r"(?:不得少于|不少于|至少|最低)\s*\d+\s*(?:日|天|日历天)", "deadline", "天数下限"),
    # 扫描件/原件要求
    (r"(?:须|必须|应当?)\s*(?:提供|提交|附)\s*(?:原件|扫描件|公证件)", "format", "原件/扫描件要求"),
]

# 用于回显去重(完全相同 text+page 视为重复)
def _dedup_key(text: str, page: int | None) -> str:
    return f"{(text or '').strip()[:80]}|{page or 0}"


def _grep_implicit_constraints(tender_text: str, existing_keys: set[str], log) -> list[dict]:
    """从原文逐行匹配硬约束正则,返回 LLM 漏抽的条款。

    每条按 [第N页] 标记定位 source_page。已存在(text+page 匹配)的不重复添加。
    """
    found: list[dict] = []
    # 拆原文为带页号的行
    cur_page: int | None = None
    cur_method: str = "text"
    page_re = re.compile(r"\[第(\d+)页\](?:\s*-\s*视觉OCR)?", re.UNICODE)
    for raw_line in tender_text.splitlines():
        m = page_re.search(raw_line)
        if m:
            cur_page = int(m.group(1))
            cur_method = "ocr" if "视觉OCR" in raw_line else "text"
            continue
        if not raw_line.strip():
            continue
        # 同一行已被某个更具体 pattern 命中过的位置区间,后续 pattern 不再触发
        # (避免"总页数不得超过 70 页"和"不得超过 70 页"双抓)
        line_hit_spans: list[tuple[int, int]] = []
        for pat, ctype, desc in _IMPLICIT_CONSTRAINT_PATTERNS:
            for hit in re.finditer(pat, raw_line):
                start, end = hit.span()
                if any(s <= start < e or s < end <= e for s, e in line_hit_spans):
                    continue
                text_original = hit.group(0).strip()
                # 至少 6 字以避免误抓
                if len(text_original) < 6:
                    continue
                key = _dedup_key(text_original, cur_page)
                if key in existing_keys:
                    # 即便此条已在 existing 中,也要占用 span 防止后续短 pattern 复抓
                    line_hit_spans.append((start, end))
                    continue
                existing_keys.add(key)
                line_hit_spans.append((start, end))
                found.append({
                    "category": "硬约束",
                    "constraint_type": ctype,
                    "text_original": text_original,
                    "consequence": "视为不响应",
                    "source_file_hint": None,
                    "source_page": cur_page,
                    "source_page_method": cur_method,
                    "_grep_fallback": True,
                    "_grep_pattern_desc": desc,
                })
    if found and log:
        log.info(f"[B3] grep fallback 补抽 {len(found)} 条隐含硬约束")
        for c in found[:10]:
            log.info(f"    + [{c['constraint_type']}] p{c['source_page']}: {c['text_original'][:60]}")
    return found


# ----- B3 结果性废标 grep fallback(类一双保险:LLM 漏抽结果词条款时补回)-----

# 结果性废标关键词:只收"废标作为结果动作"的短语,不收把"废标"当名词的句子
# (如"废标条件""判断是否为废标"是元描述,不是真条款)。
_RESULT_DISQ_KEYWORDS = (
    "否决投标", "否决其投标", "予以否决", "直接否决", "一票否决",
    "投标作废", "作废标处理", "按废标处理",
    "投标无效", "投标文件无效", "视为无效投标",
    "不予受理", "视为不响应", "视为撤销",
)

# 元描述/章节标题噪声标记:命中则跳过(描述废标制度本身,而非具体废标条件)。
_META_NOISE_MARKERS = ("处理后", "组成部分", "集中列示", "是否为废标", "总结和重申")

_SENT_SPLIT_RE = re.compile(r"[。；;！\n]")


def _norm_clause(s: str) -> str:
    """归一化条款文本用于模糊去重:只保留汉字。"""
    return re.sub(r"[^一-鿿]", "", s or "")


def _is_duplicate_clause(sent: str, reference_norms: list[str]) -> bool:
    """grep 抓到的句子是否与已有条款重复(归一化后子串包含 / 高相似度)。"""
    sn = _norm_clause(sent)
    if len(sn) < 8:
        return True  # 太短,无实质内容,丢弃
    for rn in reference_norms:
        if not rn:
            continue
        if sn in rn or rn in sn:
            return True
        if SequenceMatcher(None, sn, rn).ratio() > 0.82:
            return True
    return False


def _result_consequence(text: str) -> str:
    if "不予受理" in text:
        return "不予受理"
    if "限制参加" in text:
        return "限制投标"
    if "无效" in text:
        return "无效"
    return "废标"


def _grep_result_disqualifications(
    tender_text: str, reference_clauses: list[dict], log
) -> list[dict]:
    """从原文逐行抓结果性废标条款(类一),返回 LLM 漏抽的条款。

    与 _grep_implicit_constraints(类二)对称:类二有 30+ 正则兜底,类一原先全靠
    LLM 单次调用,recall 波动会静默丢条款。命中结果词即抓所在整句,再按归一化
    文本对已有 clauses 做模糊去重,只保留 LLM 漏抽的残差。
    """
    found: list[dict] = []
    seen_norms = [_norm_clause(c.get("text_original", "")) for c in reference_clauses]
    cur_page: int | None = None
    cur_method: str = "text"
    page_re = re.compile(r"\[第(\d+)页\](?:\s*-\s*视觉OCR)?", re.UNICODE)
    for raw_line in tender_text.splitlines():
        m = page_re.search(raw_line)
        if m:
            cur_page = int(m.group(1))
            cur_method = "ocr" if "视觉OCR" in raw_line else "text"
            continue
        line = raw_line.strip()
        if not line or not any(kw in line for kw in _RESULT_DISQ_KEYWORDS):
            continue
        for sent in _SENT_SPLIT_RE.split(line):
            sent = sent.strip(" \t\r　、")
            if len(sent) < 10:
                continue
            if not any(kw in sent for kw in _RESULT_DISQ_KEYWORDS):
                continue
            if any(noise in sent for noise in _META_NOISE_MARKERS):
                continue
            if _is_duplicate_clause(sent, seen_norms):
                continue
            seen_norms.append(_norm_clause(sent))  # 防 grep 结果内部互相重复
            found.append({
                "category": "评审",
                "constraint_type": None,
                "text_original": sent[:240],
                "consequence": _result_consequence(sent),
                "source_file_hint": None,
                "source_page": cur_page,
                "source_page_method": cur_method,
                "_grep_fallback": True,
                "_grep_pattern_desc": "结果性废标条款",
            })
    if found and log:
        log.info(f"[B3] grep fallback 补抽 {len(found)} 条结果性废标条款")
        for c in found[:10]:
            log.info(f"    + [结果性废标] p{c['source_page']}: {c['text_original'][:60]}")
    return found


def extract_b3_disqualification(tender_text: str, client: LLMClient, model: str, log) -> dict:
    log.info("[B3] 抽取废标条款...")
    user = B3_USER_TEMPLATE.format(tender_text=tender_text)
    # B3 要枚举全文废标条款,输出远超 B1/B2;大型招标文件(如 218 页西藏项目)会撑爆
    # 默认 12000 token → 截断 → JSONDecodeError。这里抬到 16000(deepseek-v4-flash 支持)。
    payload = _parse_json_with_retry(
        client, model, SYSTEM_PROMPT, user, log=log, max_tokens=16000
    )

    llm_failed = not payload
    if llm_failed:
        # LLM 抽取失败(常见根因是输出超 max_tokens 被截断)→ 不直接 abort,
        # 改用 grep-only 兜底:类二隐含硬约束 + 类一结果性废标全靠正则补回。
        # 实测对 218 页西藏项目 grep-only 可产出 34 条可用条款(含"总页数≤70页")。
        log.warning(
            "[B3] LLM 废标条款抽取失败(可能被 max_tokens 截断),"
            "降级为 grep-only 兜底,仅靠正则从原文补抽条款。"
        )
        payload = {"clauses": [], "summary": {}, "_grep_only_fallback": True}

    clauses = payload.setdefault("clauses", [])
    grep_count = 0

    # 类二 grep fallback:补充 LLM 漏抽的隐含硬约束(R3 双保险)
    existing_keys = {
        _dedup_key(c.get("text_original", ""), c.get("source_page"))
        for c in clauses
    }
    implicit = _grep_implicit_constraints(tender_text, existing_keys, log)
    if implicit:
        clauses.extend(implicit)
        grep_count += len(implicit)

    # 类一 grep fallback:补充 LLM 漏抽的结果性废标条款(与类二对称,对已有条款模糊去重)
    result_disq = _grep_result_disqualifications(tender_text, clauses, log)
    if result_disq:
        clauses.extend(result_disq)
        grep_count += len(result_disq)

    # grep-only 降级时若连正则也抓不到任何条款,才真正判定失败(LLM 与 grep 双失败)。
    if llm_failed and not clauses:
        raise RuntimeError("B3 废标条款抽取失败:LLM 失败且 grep 兜底 0 条")

    # 同步 summary(grep 有补抽,或走 grep-only 降级时,都需重建 summary)
    if grep_count or llm_failed:
        summary = payload.setdefault("summary", {})
        summary["total_count"] = len(clauses)
        by_cat: dict[str, int] = {}
        for c in clauses:
            cat = c.get("category") or "其他"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        summary["by_category"] = by_cat
        payload["_grep_fallback_count"] = grep_count

    return payload


def validate_b3(payload: dict, total_pages: int) -> tuple[bool, list[str]]:
    warnings = []
    clauses = payload.get("clauses") or []
    if len(clauses) < 5:
        warnings.append(f"废标条款数量偏少 ({len(clauses)} < 5),招标文件几乎不可能这么少")

    # 隐含硬约束统计(R3 防御观测)
    hard_constraints = [c for c in clauses if c.get("constraint_type")]
    if not hard_constraints:
        warnings.append(
            "未抽到任何 constraint_type 字段非空的硬约束条款 — "
            "西藏简易综合评估法项目几乎都有'70 页硬顶'类条款,请核对 prompt 与原文是否包含约束词"
        )

    # 同一页号 ≥ 5 条触发警告(R4 根因)
    page_counter: dict[int, int] = {}
    no_page = 0
    for c in clauses:
        p = c.get("source_page")
        if p is None:
            no_page += 1
            continue
        page_counter[p] = page_counter.get(p, 0) + 1
    over = [(p, n) for p, n in page_counter.items() if n >= 5]
    if over:
        msg_parts = [f"p{p}×{n}" for p, n in sorted(over)]
        warnings.append(
            f"废标条款来源页过度集中(同页 ≥ 5 条):{', '.join(msg_parts)} — "
            f"R4 根因复现可能性高,人工核对"
        )
    if no_page:
        warnings.append(f"{no_page} 条废标条款无 source_page,需补全")

    if total_pages and page_counter:
        unique_pages = len(page_counter)
        if unique_pages == 1:
            warnings.append(
                f"全部废标条款都标在同一页,几乎肯定有误(R4 根因) — 重跑 B3 或换模型"
            )
    return True, warnings


# ────────────────────────────────────────────────────────────────────
# 4. 主流程
# ────────────────────────────────────────────────────────────────────

JOBS = {
    "b1": ("evaluation_method.json", extract_b1_evaluation, validate_b1),
    "b2": ("mandatory_outline.json", extract_b2_outline, validate_b2),
    "b3": ("disqualification.json", extract_b3_disqualification, None),  # b3 校验需 total_pages
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", help="投标根目录(包含 extracted/ 子目录)")
    p.add_argument("--model", default=None,
                   help="LLM 模型 key(默认从 bid_llm_config.json 读 default_model)")
    p.add_argument("--only", choices=list(JOBS.keys()), action="append",
                   help="只跑指定任务(可重复,如 --only b1 --only b2)")
    p.add_argument("--force", action="store_true",
                   help="覆盖已有 JSON(默认存在则跳过)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}", file=sys.stderr)
        return 2

    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在,请先跑 bid_extract.py: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_skeleton_extract", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_skeleton_extract  目录: {tender_dir}")

    # 1. 找主文档
    main_txt = pick_main_tender_text(ext, log)
    if not main_txt:
        return 2
    full_text = main_txt.read_text(encoding="utf-8")
    pages = build_page_index(full_text)
    total_pages = max((p.page_num for p in pages), default=0)
    log.info(f"  主文档总字符 {len(full_text):,},页数 {total_pages},"
             f"视觉 OCR 页 {sum(1 for p in pages if p.is_vision)}")

    # 2. document_map.md(纯启发式)
    chapters = build_document_map(full_text, pages)
    map_path = ext / "document_map.md"
    write_document_map(main_txt, chapters, pages, map_path)
    log.info(f"{OK} document_map.md 已写入 ({len(chapters)} 个章节)")

    # 3. LLM 三件套
    try:
        client = LLMClient.load(log=log)
    except Exception as e:
        log.error(f"LLM 配置加载失败: {e}")
        log.error("提示:首次使用请运行 python bid_llm_client.py --init")
        return 2

    model = choose_skeleton_model(args.model, client.config)
    if model not in client.models:
        log.error(f"未知模型 {model!r},可选: {list(client.models.keys())}")
        return 2
    log.info(f"骨架抽取模型: {model} (默认 {SKELETON_DEFAULT_MODEL},可用 --model 覆盖)")

    only = set(args.only) if args.only else set(JOBS.keys())

    # 跳过已存在(除非 --force)
    to_run = []
    for key in ("b1", "b2", "b3"):
        if key not in only:
            continue
        out_name, _, _ = JOBS[key]
        out_path = ext / out_name
        if out_path.exists() and not args.force:
            log.info(f"  {out_name} 已存在,跳过(--force 覆盖)")
            continue
        to_run.append(key)

    if not to_run:
        log.info("无任务可跑")
        return 0

    log.info(f"将并行调用 {model} 跑 {len(to_run)} 个任务: {', '.join(to_run)}")

    all_warnings: list[str] = []
    hard_fail = False

    # 并行调用 LLM(每家 provider 有内部 semaphore)
    with ThreadPoolExecutor(max_workers=len(to_run)) as exe:
        futures = {}
        for key in to_run:
            out_name, extractor, _ = JOBS[key]
            futures[exe.submit(extractor, full_text, client, model, log)] = (key, out_name)

        results: dict[str, dict] = {}
        for fut in as_completed(futures):
            key, out_name = futures[fut]
            try:
                results[key] = fut.result()
                log.info(f"  {OK} {key} 抽取完成")
            except Exception as e:
                log.error(f"  {FAIL} {key} 抽取失败: {e}")
                hard_fail = True

    # 4. 校验 + 写文件
    for key, payload in results.items():
        out_name = JOBS[key][0]
        out_path = ext / out_name

        if key == "b1":
            ok, warns = validate_b1(payload)
        elif key == "b2":
            ok, warns = validate_b2(payload)
        elif key == "b3":
            ok, warns = validate_b3(payload, total_pages)
        else:
            ok, warns = True, []

        if not ok:
            log.error(f"  {FAIL} {key} schema 校验失败: {warns}")
            hard_fail = True
            continue

        # 注入元数据
        payload["_meta"] = {
            "source_file": main_txt.name,
            "total_pages": total_pages,
            "extracted_by": "bid_skeleton_extract.py",
            "model": model,
        }
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"  {OK} 写入 {out_name}")
        for w in warns:
            log.warning(f"    {WARN} {w}")
            all_warnings.append(f"[{key}] {w}")

    log.info("=" * 60)
    if hard_fail:
        log.error(f"{FAIL} 骨架提取硬失败,请查看日志")
        return 2

    if all_warnings:
        log.warning(f"{WARN} 完成,但有 {len(all_warnings)} 项警告需人工核对:")
        for w in all_warnings:
            log.warning(f"    - {w}")
        return 1

    log.info(f"{OK} 骨架提取全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
