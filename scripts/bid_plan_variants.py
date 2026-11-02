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
import hashlib
import json
import logging
import random
import re
from bid_common import get_config_dir  # noqa: E402
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bid_common import (
    make_script_logger,
    parse_chapter_plan,
    parse_plan_budget,
    parse_plan_subitems,
)
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
    ("资源协同",
     "按资源配置与协同维度切分:人力资源配置、机械设备协同、材料供应保障、"
     "技术与信息支撑、各方协调联动;子项体现资源要素的统筹与协同调度。"),
]


def assign_strategy(variant_index: int, variant_id: str = "",
                    seed: int | None = None,
                    taken: set | None = None) -> tuple[str, str]:
    """按变体序号确定性分配切分维度。

    ≤7 家：序号互异无碰撞；>7 家叠加 variant 哈希偏移；seed 参与(重种后换轴)。
    taken 传入时优先避开已用维度;池耗尽(>7 家必然发生)时沿用哈希结果
    (此时靠 seed/模型/温度叠加区分,维度重复不可避免)。"""
    i = int(variant_index)
    if i >= len(_DECOMP_STRATEGIES) and variant_id:
        _off = int(hashlib.md5(variant_id.encode("utf-8")).hexdigest()[:6], 16)
        i = i + _off
    if isinstance(seed, (int, float)):  # 0 是合法显式值;None/str 跳过偏移
        i = i + (int(seed) % len(_DECOMP_STRATEGIES))
    strat = _DECOMP_STRATEGIES[i % len(_DECOMP_STRATEGIES)]
    if taken is not None:
        if strat[0] in taken:
            for s in _DECOMP_STRATEGIES:
                if s[0] not in taken:
                    strat = s
                    break
        taken.add(strat[0])
    return strat


# ── 工具 ──────────────────────────────────────────────────────────

def _is_tier_a(ch: dict) -> bool:
    """评分锚定章:评分项列非空 或 score>0。"""
    si = (ch.get("score_item") or "").strip()
    return bool(si and si not in ("—", "-", "无")) or ch.get("score", 0) > 0


def _est_pages(target_chars: int, chart_count: int,
               chars_per_page: float = 425.0) -> float:
    """按当前项目字号/行距密度估算字数页 + 图表页。"""
    density = chars_per_page if chars_per_page and chars_per_page > 0 else 425.0
    return round(target_chars / density + chart_count * 0.85, 1)


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
    if not isinstance(em, dict):  # 数组形状防御
        log.warning("evaluation_method.json 顶层非 dict,评分标准回退基准子项")
        return {}
    # ★ 合格制项目(含"商务标有分+施组合格制"的混合制):技术标无评分项,
    # scoring_items 里全是报价/信用/业绩等商务项。若按序号 zip 到技术章节,
    # 会把商务评分标准当成技术章的内容依据 → 变体 H2 切分跑偏
    # (见 memory/feedback_bid_mixed_method_passfail.md)。合格制下章节锚点来自必含大纲,
    # base_chapters 的 _scope 已带大纲子节,直接返回空 → decompose 回退基准子项。
    if em.get("tech_score_is_passfail") or em.get("evaluation_method") == "passfail":
        log.info("  合格制项目:章节锚点来自必含大纲,不映射商务评分项作章节评分标准")
        return {}
    items = em.get("scoring_items", []) or []
    if not isinstance(items, list):
        log.warning("scoring_items 非 list,评分标准回退基准子项")
        items = []
    out: dict[str, str] = {}
    if len(items) != len(base_chapters):
        log.warning(
            f"scoring_items({len(items)}) 与章节数({len(base_chapters)})不一致,"
            f"按序号位置对齐(超出部分忽略,不足章无标准)——人工核验评分表与章节映射"
        )
    for ch, it in zip(base_chapters, items):
        if not isinstance(it, dict):
            continue  # 畸形元素跳过,不崩整个变体 H2 差异化
        crit = (it.get("criteria_original") or "").strip()
        if crit:
            out[str(ch["num"]).zfill(2)] = crit
    return out


# ── 合格制批量:目录差异化 ─────────────────────────────────────
# 招标大纲的二/三级节标题必须 1:1 保留(memory/feedback_bid_pass_fail_anchor.md),
# 但**三家不能写成一模一样**:评标实践里"章节标题逐字相同"是围标/串标的高危表征。
# 策略:标题集合锁定(不增不减不改顺序),仅做「编号体系 + 措辞风格」变体化,
# 且必须保留原大纲关键术语(评委按关键词仍能找到每一节)。
_TITLE_STYLES: list[tuple[str, str]] = [
    ("原文", "原样保留招标大纲措辞"),
    ("限制语", "在保留原主题词前提下加 2-6 字限定语(…说明/…安排/…与要求/…界定),"),
    ("工程化", "改为动宾或名词短语的工程标题(如「编制依据与原则」「质量目标与责任分解」),"),
]
_NUM_STYLES = ["1.1", "一、", "（一）"]
# 与先前变体撞车时的确定性回退后缀(全部非空,按变体序号轮转)
_FALLBACK_SUFFIX = ["（实施要点）", "（含实施细则）", "（补充说明）"]
# 内层编号:必须带分隔符(、/./空格),否则「3号桥台施工」「5S现场管理」会被误剥
_INNER_NUM_RE = re.compile(
    r"^(?:第[一二三四五六七八九十]+[节部分章]|\d+(?:\.\d+)+|\d+[、.．]\s*|\d+\s+|"
    r"[一二三四五六七八九十]+[、.．]\s*|（[一二三四五六七八九十]+）)\s*")
_CN_NUM = "一二三四五六七八九十"


def _wording_style_index(variant_index: int) -> int:
    """措辞风格序号:第 1 家用原文(0),其余家在非原文风格间轮转。

    与编号体系(_NUM_STYLES,按 variant_index 模 3)交叉,保证 V04/V07 等同风格
    变体不会与 V01/V02 目录逐字相同。
    """
    n_alt = max(1, len(_TITLE_STYLES) - 1)
    return 0 if variant_index == 0 else 1 + ((variant_index - 1) % n_alt)


def title_number_head(style_index: int, i: int, chapter_num: str) -> str:
    """变体专属二级标题编号前缀(i 从 1 起)。"""
    style = _NUM_STYLES[style_index % len(_NUM_STYLES)]
    if style == "1.1":
        # 章节号可能是非纯数字(手改/特殊编号) → 直接 int() 会抛 ValueError
        # 中断整个变体计划生成;取不到数字时退回原样字符串
        _m = re.match(r"\s*(\d+)", str(chapter_num or ""))
        return f"{int(_m.group(1)) if _m else chapter_num}.{i}"
    cn = _CN_NUM[i - 1] if i <= 10 else ("十" + _CN_NUM[i - 11] if i < 20 else str(i))
    return f"{cn}、" if style == "一、" else f"（{cn}）"


def strip_inner_number(title: str) -> str:
    """去标题内层编号(第X节 / 1、 / 一、 / （一）),保留实质词。"""
    t = (title or "").strip()
    for _ in range(3):
        n = _INNER_NUM_RE.sub("", t).strip()
        if n == t:
            break
        t = n or t
    return t


def _h1_ok(orig: str, new: str) -> bool:
    """H1 改写校验:原标题必须仍是新标题的子串(去空白/标点后比较)。"""
    def _n(s):
        return re.sub(r"[\s　]+", "", str(s or ""))
    o, n = _n(orig), _n(new)
    if not o or not n:
        return False
    if o not in n:
        return False
    if len(n) > len(o) + 14:          # 过度膨胀(评委扫目录时易漏)
        return False
    if "（补充说明）" in n or "（含实施细则）" in n or "（实施要点）" in n:
        return False
    return True



def style_passfail_chapter_titles(chapters: list[dict], variant_index: int,
                                  taken: list[list[str]], client, model: str,
                                  log: logging.Logger) -> list[dict]:
    """合格制一级章标题差异化:章节数/顺序锁定,措辞按变体区分。

    合格制下大纲一级主题是评委逐项核查"是否响应实质性要求"的锚点,因此**术语集合绝不可变**
    (memory/feedback_bid_pass_fail_anchor.md);但三家标题字面逐字相同又会被判"投标文件
    异常一致"(评标实践中的围标高危表征,用户两次反馈)。故取折中:

    - 第 1 家用招标原文;
    - 第 2/3 家加限定语/工程化表述,但**原标题必须整串仍是新标题的子串**(去空白比较) ——
      评委按大纲字面搜章必命中,合规风险为零;
    - 长度上限 原题+14 字、禁「（补充说明）」式括号后缀;
    - 未过校验或与先前变体撞车 → 再问一次 → 仍不行回退原文。
    """
    base = [c.get("title", "") for c in chapters]
    if _wording_style_index(variant_index) == 0 or client is None or not base:
        return chapters
    occ = set()
    for seq in taken:
        occ.update(x for x in seq if x)
    lines = [f"{i}. {t}" + ("  ← 已有投标人用过,必须改写" if t in occ else "")
             for i, t in enumerate(base, 1)]
    prompt = (
        f"下列是投标施工组织设计的 {len(base)} 个一级章标题(即招标文件施工组织设计编制大纲的主题)。"
        "请逐条改写章标题:在**保留原主题词**的前提下加限定语或改为更具体的工程化表述"
        "(如「工程概况」→「工程概况与建设条件」,「安全文明施工体系与措施」→「安全生产与文明施工体系及措施」)。\n"
        "硬约束:① 改写后的标题必须**包含原主题词的前两字并保留 ≥一半的原文用词**,否则评委会找不到对应章;"
        "② 条数与顺序完全一致;③ 标有「已有投标人用过」的必须换不同措辞;\n"
        "只输出 JSON: {\"titles\": [...]}。\n\n" + "\n".join(lines)
    )
    try:
        resp = client.chat(
            model_key=model,
            system="你是施工组织设计标题拟制专家,擅长在保留招标大纲主题词的前提下差异化拟题。",
            user=prompt, temperature=0.85)
        txt = resp.text
        # ★ 去掉模型回显的序号前缀(提示里按 "1. xxx" 编号,模型常把序号带进标题,
        #   渲染后变成「第八章 7. 质量管理体系与措施实施要点」;见 memory/feedback_bid_passfail_title_diff.md)
        cand = [strip_inner_number(str(x)) for x in
                (json.loads(txt[txt.find("{"): txt.rfind("}") + 1]).get("titles") or [])]
        if len(cand) != len(base):
            log.warning(f"  合格制章标题改写条数不符({len(cand)}/{len(base)}),回退原文")
            return chapters
        out = []
        for c, o, n in zip(chapters, base, cand):
            # (旧实现:撞车即 o + 括号后缀 —— 已废弃,改为下面的重试 + 回退原文)
            # 与先前变体撞车 或 未过字面校验 → 先用原文占位,交给下面的重试
            out.append({**c, "title": n if (_h1_ok(o, n) and n not in occ) else o})
        # 撞车/未达标条目再问一次(禁止回显、禁止括号后缀)
        need = [(i, base[i]) for i, cc in enumerate(out) if cc["title"] == base[i]]
        if need and len(need) < len(base):
            _used_lines = "\n".join(f"- {x}" for x in sorted(occ)[:40])
            _retry = "\n".join(f"{i}. {o}" for i, o in need)
            try:
                r2 = client.chat(
                    model_key=model,
                    system="你是施工组织设计标题拟制专家。",
                    user=(
                        "请在**完整保留**下列原标题的前提下加限定语或工程化表述,"
                        "使标题与「已用标题」都不同。硬约束:① 原标题必须原样作为改写后标题的"
                        "一部分(评委按大纲字面找章);② 不得加「（补充说明）」「（含实施细则）」"
                        "这类括号后缀;③ 只输出 JSON: {\"titles\": [...]}。\n\n"
                        f"已用标题:\n{_used_lines}\n\n待改写:\n{_retry}"),
                    temperature=0.9)
                _t2 = r2.text
                _c2 = [strip_inner_number(str(x)) for x in (
                    json.loads(_t2[_t2.find("{"): _t2.rfind("}") + 1]).get("titles") or [])]
                if len(_c2) == len(need):
                    for (i, o), n2 in zip(need, _c2):
                        if n2 not in occ and _h1_ok(o, n2):
                            out[i] = {**out[i], "title": n2}
            except Exception as e:  # noqa: BLE001
                log.debug(f"  合格制章标题避撞重试失败({e}),保留原文")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning(f"  合格制章标题改写失败({e}),回退原文标题")
        return chapters


def style_passfail_titles(subs: list[dict], variant_index: int,
                          taken: list[list[str]], client, model: str,
                          log: logging.Logger, chapter_num: str = "") -> list[dict]:
    """合格制子项标题差异化:数量/顺序/术语锁定,措辞与编号体系按变体区分。

    style 0 或 LLM 不可用/失败 → 回退原文措辞(仍带变体编号体系)。
    taken 为先前变体已生成的标题序列,用于避免两家措辞撞车。
    """
    base = [strip_inner_number(s.get("title", "")) for s in subs]
    style_idx = _wording_style_index(variant_index)
    style_name, style_desc = _TITLE_STYLES[style_idx]
    if style_idx == 0 or client is None or not base:
        new = base
    else:
        occupied = set()
        for seq in taken:
            occupied.update(x for x in seq if x)
        lines = []
        for i, t in enumerate(base, 1):
            mark = "  ← 已有投标人用过此措辞,必须改写" if t in occupied else ""
            lines.append(f"{i}. {t}{mark}")
        prompt = (
            f"下列是某投标文件施工组织设计的二级子项标题。请逐条改写,风格:{style_name}——"
            f"{style_desc}要求同一份文件内不得重复使用同一后缀。\n"
            "硬约束:① 必须保留原条目的专有名词与工艺术语(如「土石方开挖」「质量目标管理」"
            "「编制依据」「脚手架工程」),不得换义、不得漏项;② 超过 20 字的长句压缩为 8-16 字,"
            "关键术语必须保留;③ 条数/顺序与输入完全一致,不得合并/拆分/增删;"
            "④ 标有「已有投标人用过」的条目必须换成不同措辞。\n"
            "只输出 JSON: {\"titles\": [\"改写1\", ...]}(不带序号前缀)。\n\n"
            + "\n".join(lines)
        )
        try:
            resp = client.chat(
                model_key=model,
                system="你是施工组织设计标题拟制专家,擅长在保留招标大纲术语的前提下差异化拟题。",
                user=prompt,
                temperature=0.85,
            )
            txt = resp.text
            data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            cand = [strip_inner_number(str(x)) for x in (data.get("titles") or [])]
            if len(cand) == len(base):
                new = []
                for o, c in zip(base, cand):
                    # 丢失原意的改写(汉字重合过低)一律回退原文
                    ho = "".join(ch for ch in o if "一" <= ch <= "鿿")
                    hc = "".join(ch for ch in c if "一" <= ch <= "鿿")
                    if not hc or (ho and len(set(ho) & set(hc)) / len(set(ho)) < 0.45):
                        new.append(o)          # 丢义的回显 → 保留原文
                    else:
                        new.append(c)
                # ★ 撞车条目:再问一次(明确禁止回显),仍撞车则保留原文。
                #   旧实现直接 o + "（含实施细则）/（补充说明）" → 同一份目录反复出现同一后缀
                #   (实测 V03 七次),既是机器痕迹,标题也不再等于大纲术语。
                _collide = [(i, o) for i, (o, c) in enumerate(zip(base, new)) if c in occupied]
                if _collide and client is not None:
                    _used_lines = "\n".join(f"- {x}" for x in sorted(occupied)[:60])
                    _retry_lines = "\n".join(f"{i}. {o}" for i, o in _collide)
                    try:
                        resp2 = client.chat(
                            model_key=model,
                            system="你是施工组织设计标题拟制专家。",
                            user=(
                                f"下列标题已被其他投标人用过(见「已用标题」),必须换成**不同措辞**"
                                f"再拟一次。硬约束:① 不得与任一「已用标题」相同或近似;"
                                f"② 必须保留原条目的专有名词与工艺术语;③ 不得加「（补充说明）」"
                                f"「（含实施细则）」这类括号后缀——那一眼就是机器生成;"
                                f"④ 只输出 JSON: {{\"titles\": [...]}}。\n\n"
                                f"已用标题:\n{_used_lines}\n\n待改写:\n{_retry_lines}"),
                            temperature=0.9)
                        _txt2 = resp2.text
                        _cand2 = [strip_inner_number(str(x)) for x in (
                            json.loads(_txt2[_txt2.find("{"): _txt2.rfind("}") + 1]).get("titles") or [])]
                        if len(_cand2) == len(_collide):
                            for (i, o), c2 in zip(_collide, _cand2):
                                ho = "".join(ch for ch in o if "一" <= ch <= "鿿")
                                hc = "".join(ch for ch in c2 if "一" <= ch <= "鿿")
                                if c2 and c2 not in occupied and hc and (
                                        not ho or len(set(ho) & set(hc)) / len(set(ho)) >= 0.45):
                                    new[i] = c2
                    except Exception as e:  # noqa: BLE001
                        log.debug(f"  合格制标题避撞重试失败({e}),保留原文")
            else:
                log.warning(f"  合格制标题改写条数不符({len(cand)}/{len(base)}),回退原文措辞")
                new = base
        except Exception as e:  # noqa: BLE001
            log.warning(f"  合格制标题改写失败({e}),回退原文措辞(仅编号体系差异化)")
            new = base
    out = []
    for i, (s, t) in enumerate(zip(subs, new), 1):
        t = strip_inner_number(t)
        if chapter_num and variant_index > 0:
            t = f"{title_number_head(variant_index, i, chapter_num)}{t}"
        out.append({**s, "title": t})
    return out


def is_passfail_lock(extracted: Path, log: logging.Logger) -> bool:
    """合格制项目判定(含混合制:商务标有分 + 施组合格制不计分)。

    用于决定是否锁定基准子项集合:合格制下大纲二/三级节标题是评委逐条核查的锚点
    (memory/feedback_bid_pass_fail_anchor.md 升级版),不能被 LLM 按变体维度
    重组掉;但措辞/编号要按变体差异化(见 style_passfail_titles)。
    """
    p = extracted / "evaluation_method.json"
    if not p.exists():
        return False
    try:
        em = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log.warning(f"evaluation_method.json 解析失败({e}),按评分制处理")
        return False
    if not isinstance(em, dict):
        return False
    return bool(em.get("tech_score_is_passfail")) or em.get("evaluation_method") == "passfail"


def _load_project_brief(extracted: Path) -> str:
    """从 project_facts.json 取一句项目背景,供 LLM 切分时保持项目专属性。"""
    fp = extracted / "project_facts.json"
    if not fp.exists():
        return ""
    try:
        facts = json.loads(fp.read_text(encoding="utf-8")).get("facts", {})
        name = ((facts.get("project_name") or {}).get("value") or "").strip()
        scope = ((facts.get("project_scope") or {}).get("value") or "").strip()
        boq_items = _load_boq_item_names(extracted)
        boq_brief = f"清单主要工作：{'、'.join(boq_items[:12])}" if boq_items else ""
        brief = "；".join(x for x in (name, scope, boq_brief) if x)
        return brief[:600]
    except Exception:
        return ""


# ── 变体章节构建(章标题/数量/顺序 = 基准,只缩放字数)──────────────

def build_variant_chapters(base_chapters: list[dict], variant: dict,
                           scale: float,
                           chars_per_page: float = 425.0) -> list[dict]:
    """克隆基准章节为某变体的工作副本。

    ★ 章标题、章数量、章顺序、评分项、文件名 全部 1:1 沿用基准,绝不变换。
    仅按变体缩放因子 + 逐章 jitter 重设字数预算(字数差异也是反串标的一环)。
    """
    seed = variant.get("section_order_seed")
    if seed is None:  # 0 是合法显式值,仅 None 走哈希兜底
        seed = int(hashlib.md5(str(variant.get("id", "")).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    chapters: list[dict] = []
    for base in base_chapters:
        num = str(base["num"]).zfill(2)
        jitter = 1.0 + rng.uniform(-0.08, 0.08)
        # ★ 字数预算随基准缩放(jitter 仅 ±8%),下限不硬抬到 1500——
        # 历史 bug:60 页目标下基准小章(如 13/14 章 382 字)被 1500 下限
        # 抬升,导致变体总量 ~28K 字、Word 实测超 70 页硬顶而废标。
        target_chars = max(300, int(base.get("target_chars", 2000) * scale * jitter))
        chart_count = int(base.get("chart_count", 0))
        chapters.append({
            "num": num,
            "title": base["title"],                       # ★ 逐字照基准,不改写
            "filename": base.get("filename") or f"{num}_{base['title']}.md",
            "score_item": base.get("score_item", ""),
            "score": base.get("score", 0),
            "target_chars": target_chars,
            "chart_count": chart_count,
            "est_pages": _est_pages(target_chars, chart_count, chars_per_page),
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
4. 子项数量 ≈ 该章「目标字数」÷ 600(四舍五入,**最少 2 项,最多 9 项**):
   目标 1,000 字 → 2 项(每项约 500 字);3,000 字 → 5 项;5,000 字 → 8 项。
   ★ 不要多给。子项超过这个数量且单项 < 450 字时,系统会按 450 字地板把相邻子项
   合并,合并后标题会变成超长的「A、B、C」链式标题(实测出过 91 字标题),
   反而破坏可读性——把多余要点放进正文,不要拆成碎子项。
5. ★ 子标题必须**短句化**:每个子标题 **≤ 24 个汉字**,是一个能当 Word 三级标题用的词/短语
   (如「基坑土石方开挖与验槽」「冬期混凝土浇筑养护措施」)。
   **严禁**把多个主题用顿号串成一个长标题(反面例:
   「组织机构与职责分工、法规文件收集执行流程、标准规范控制制度、批准文件落实机制」
   —— 这种 60+ 字的标题在 Word 里要占半页,评委看目录时无法读)。
   一个子项只对应一个主题;要点覆盖靠**增加子项**和正文展开,不靠标题里堆顿号。
   若本章目标字数 ÷ 子项数 > 700 字,说明子项拆得不够细,应继续拆。
6. 子标题须结合项目实际,专业、具体、可落地;**严禁**「编制依据 / 实施方案 /
   保障措施 / 应急响应」这类放之四海皆准的空泛兜底标题。
7. 各章子项目标字数之和 = 该章「目标字数」(每项可 ±50 字浮动)。
8. 子标题不带任何序号前缀。

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
    def _fmt_safe(s: str) -> str:
        """值不参与 format 二次解析,原样返回(占位符由 kwargs 填充,值中 {} 无害)。"""
        return str(s)

    briefs = []
    for c in chapters:
        crit = _fmt_safe(criteria_map.get(c["num"], ""))
        ref = "、".join(c["_scope"][:12]) if c["_scope"] else ""
        lines = [f"- 第{c['num']}章「{c['title']}」(目标字数 {c['target_chars']})"]
        if crit:
            lines.append(f"  评分标准原文:{crit}")
        if ref:
            lines.append(f"  内容参考(基准子项,可按本变体维度拆解重组):{ref}")
        briefs.append("\n".join(lines))

    prompt = _DECOMPOSE_PROMPT.format(
        vid=variant_id, strategy_name=_fmt_safe(sname), strategy_desc=_fmt_safe(sdesc),
        project_brief=_fmt_safe(project_brief or "工程建设投标项目"),
        chapters_brief="\n".join(briefs),
    )
    try:
        resp = client.chat(
            model_key=model,
            system="你是资深施工组织设计编写专家,擅长按指定维度对章节主题做差异化的子项切分。",
            user=prompt,
            temperature=0.85,
        )
        _jb = _first_balanced_json(resp.text)
        if not _jb or _jb == "{}":
            raise ValueError("LLM 返回无 JSON")
        data = json.loads(_jb)
        result: dict[str, list[dict]] = {}
        for ch in data.get("chapters", []):
            num = str(ch.get("num", "")).zfill(2)
            subs = []
            for s in ch.get("subitems", []):
                if not s.get("title"):
                    continue
                try:
                    _c = int(s.get("chars", 0) or 0)
                except (TypeError, ValueError):
                    _c = 0  # 单项脏数据置 0(后续 _normalize 均分),不整章丢弃
                subs.append({
                    "title": re.sub(r"^\d+(?:\.\d+)*\s*(?:[、,，]|\.(?=\s))\s*|^\d+(?:\.\d+)*\s+",
                               "", str(s.get("title", ""))).strip(),
                    "chars": _c,
                })
            if subs:
                result[num] = subs
        if not result:
            log.warning(f"  变体 {variant_id} 子项分解返回空(LLM 输出缺少 chapters/子项),"
                        f"回退基准子项 — 二级标题差异化本轮未生效")
        return result
    except Exception as e:
        log.warning(f"  变体 {variant_id} 子项 LLM 分解失败({e}),用基准子项兜底")
        return {}


def _first_balanced_json(text: str) -> str:
    """取第一个花括号平衡的 JSON 块(贪婪 {[\\s\\S]*} 会吞解释文本致解析失败)。"""
    start = text.find("{")
    while start != -1:
        depth = 0; in_str = False; esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': in_str = False
                continue
            if c == '"': in_str = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return text[start:] if start != -1 else "{}"


def _fallback_subitems(ch: dict) -> list[dict]:
    """LLM 失败时的兜底:直接用基准子项,字数按目标均分。"""
    import re as _re2
    scope = [_re2.sub(r"^[0-9]+(?:\.[0-9]+)*\s*(?:[、,，]|\.(?=\s))?\s+", "", s).strip() for s in (ch.get("_scope") or [])]
    scope = scope or [ch["title"]]
    n = max(1, len(scope))
    per = max(80, ch["target_chars"] // n)
    return [{"title": t, "chars": per} for t in scope]


def _is_attachment_chapter(title: str) -> bool:
    """是否为「附图及附表」类章节(大纲点名的附图附表汇总章)。"""
    t = title or ""
    return "附图" in t or "附表" in t


def _attachment_subitems(ch: dict, attachments: list) -> list[dict] | None:
    """附图及附表章:子项 = 大纲点名的附图附表(1:1,三家一致),不套切分维度。
    切分维度(管理职能等)会把附图章写成"职责/绘制流程"散文而漏掉实际附表 → 合格制不响应。
    返回 None 表示大纲未列附表,回退常规切分。"""
    titles = []
    for a in attachments or []:
        if isinstance(a, dict):
            label = (a.get("label") or "").strip()
            name = (a.get("title_original") or a.get("title") or "").strip()
            if name:
                titles.append(f"{label} {name}".strip() if label else name)
        elif isinstance(a, str) and a.strip():
            titles.append(a.strip())
    if not titles:
        return None
    per = max(80, ch["target_chars"] // len(titles))
    return [{"title": t, "chars": per} for t in titles]


# 子项字数地板：低于此值的相邻子项合并，防"每节两句"的空心化
# （历史教训：全库 82 子项掠 19118 字 → 233字/项 → 管理章 P/G 掉到 0.49）
SUBITEM_MIN_CHARS = 450


def _normalize_subitems(subs: list[dict], target_chars: int,
                        merge_floor: bool = True) -> list[dict]:
    """把子项字数之和校正到 target_chars；字数地板以下的相邻小项自动合并。"""
    if not subs:
        return subs
    # 脏数据防御:LLM 偶尔给 chars=0/缺失。直接按 0 缩放会把全部字数差额压到首个 H2
    # (实测 5000 字 3 子项 → [4600,200,200]),故先把 0 项换成有效项的均值再缩放。
    pos = [s["chars"] for s in subs if s.get("chars")]
    _fill = int(sum(pos) / len(pos)) if pos else 1
    for s in subs:
        if not s.get("chars"):
            s["chars"] = _fill
    total = sum(s["chars"] for s in subs) or 1
    factor = target_chars / total
    for s in subs:
        s["chars"] = max(1, int(s["chars"] * factor))
    # ★ 地板不得高于“章节目标 ÷ 子项数”:否则小目标+多子项时 Σ子项 无法收敛到
    #   target_chars(实测 12 子项/500 字 = 1.44 倍,20 子项/500 字 = 2.40 倍) → 撑爆页数硬顶
    _per = max(1, target_chars // max(len(subs), 1))
    _floor_base = min(200 if merge_floor else 60, max(1, _per))
    diff = target_chars - sum(s["chars"] for s in subs)
    subs[0]["chars"] = max(_floor_base, subs[0]["chars"] + diff)
    # 地板膨胀回写平衡:多个 200 字地板项会使总量超 target(上浮无人拦 → 页数超帽风险),
    # 从最大项削回(保底 250),必要时再由下方合并循环减项
    _excess = sum(s["chars"] for s in subs) - target_chars
    while _excess > 0 and len(subs) > 1:
        big = max(range(len(subs)), key=lambda i: subs[i]["chars"])
        if subs[big]["chars"] <= _floor_base:
            break
        _take = min(_excess, subs[big]["chars"] - _floor_base)
        subs[big]["chars"] -= _take
        _excess -= _take
    # 字数地板:最小子项 <450 且还能合并(>2 项)时,把它并入较短的邻居
    # (标题合并为 "A与B",评分要点经合并仍保留;附表章不参与合并)
    if merge_floor:
        # 合并判据:目标字数掠项数 < 450(每项太薄无意义) → 合并相邻最小两项,
        # 直到每项均值达地板或只剩 2 项(300字2项这类极限场景保持 2 项)
        while len(subs) > 2 and target_chars // len(subs) < SUBITEM_MIN_CHARS:
            idx = min(range(len(subs)), key=lambda i: subs[i]["chars"])
            if idx == 0:
                j = 1
            elif idx == len(subs) - 1:
                j = idx - 1
            else:
                j = idx - 1 if subs[idx - 1]["chars"] <= subs[idx + 1]["chars"] else idx + 1
            a, b = (subs[min(idx, j)], subs[max(idx, j)])
            sep = "、" if ("与" in a["title"] or "与" in b["title"]) else "与"
            merged = {"title": f"{a['title']}{sep}{b['title']}", "chars": a["chars"] + b["chars"]}
            pos = min(idx, j)
            subs = subs[:pos] + [merged] + subs[pos + 2:]
        # 收尾:仅剩 2 项仍被地板钳超 15%(如 300 字章 2×200=400)→ 并成单 H2
        if (len(subs) == 2 and target_chars < 900
                and sum(s["chars"] for s in subs) > target_chars * 1.15):
            subs = [{"title": f"{subs[0]['title']}与{subs[1]['title']}",
                     "chars": sum(s["chars"] for s in subs)}]
    # 合并后再归一:地板膨胀的总超量按比例回缩到 target(每项保底 250);
    # 若回缩后仍超(多项被地板钳住),把超出量从最大项硬扣(允许其低于常规但保底 200)
    _tot = sum(s["chars"] for s in subs)
    if _tot > target_chars * 1.05 and len(subs) >= 1:
        _f = target_chars / _tot
        for s in subs:
            s["chars"] = max(_floor_base, int(s["chars"] * _f))
        _diff2 = target_chars - sum(s["chars"] for s in subs)
        if _diff2 and subs:
            _big2 = max(range(len(subs)), key=lambda k: subs[k]["chars"])
            subs[_big2]["chars"] = max(_floor_base, subs[_big2]["chars"] + _diff2)
        # 仍超(全部触地板)→ 硬扣最大项至 200 保底,总量优先达标
        _tot2 = sum(s["chars"] for s in subs)
        _over = _tot2 - target_chars
        while _over > 0:
            _big3 = max(range(len(subs)), key=lambda k: subs[k]["chars"])
            _can = subs[_big3]["chars"] - _floor_base
            if _can <= 0:
                break
            _take = min(_over, _can)
            subs[_big3]["chars"] -= _take
            _over -= _take
    return subs


# ── 渲染变体规划文件 ──────────────────────────────────────────────

def render_variant_plan(variant_id: str, strategy: tuple[str, str],
                        chapters: list[dict]) -> str:
    """渲染该变体完整章节规划(表格 + 子项分配),与 chapter_plan.md 同构。"""
    sname = strategy[0]
    lines = [
        f"# 变体 {variant_id} 章节规划",
        "",
        "> 由 bid_plan_variants.py 从 chapter_plan.md 派生。",
        (
            "> 一级标题逐字沿用招标大纲主题词并作限定/工程化表述(各家措辞不同),"
            "二/三级节标题集合 1:1 保留、措辞与编号体系按变体差异化。"
            if sname == "大纲锁定" else
            "> 一级标题逐字沿用评标表原文(三家一致);二级子项按"
            f"【{sname}】维度切分(本变体专属)。"
        ),
        f"> 切分维度【{sname}】",  # 显式标记行(--variant 定向重生成读它避撞轴)
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
            # 标题已自带编号(合格制变体化编号体系 一、/（一）)时不再叠加 "num.i",
            # 否则写手会输出 "## 01.1 一、标题" 双重编号
            _pref = "" if _INNER_NUM_RE.match(s["title"]) else f"{c['num']}.{i} "
            lines.append(f"- {_pref}{s['title']}: {s['chars']} 字")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── 图表分配(沿用原逻辑)──────────────────────────────────────────

def _assign_charts_for_variant(variant: dict, base_chart_specs, org_templates: dict,
                               chapters: list[dict],
                               work_labels: list[str] | None = None,
                               log=None) -> dict:
    """为单个变体生成图表分配(确定性,基于 section_order_seed)。"""
    seed = variant.get("section_order_seed", 0)
    if seed is None or not isinstance(seed, (int, float)):
        seed = int(hashlib.md5(str(variant.get("id", "")).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(int(seed) ^ 0xABCD1234)
    assignment = {
        "variant_id": variant.get("id", ""),
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
            tree = chosen_variant.get("children", template.get("children") or [])
        else:
            tree = template.get("children") or []
        tree = _projectize_org_tree(tree, work_labels or [])
        assignment["org_template"] = {
            "chapter": _find_org_chapter(chapters),
            "template_id": chosen_key,
            "template_label": template.get("label", ""),
            "root": template.get("root") or {},
            "children": tree,
        }
    mgmt_chapters = _find_management_chapters(chapters)
    combos = org_templates.get("management_chart_types", {}).get("combinations", [])
    if combos:
        chosen_combo = combos[rng.randint(0, len(combos) - 1)]
        if len(chosen_combo) < len(mgmt_chapters):
            import itertools as _it
            _cyc = list(_it.islice(_it.cycle(chosen_combo), len(mgmt_chapters)))
            _wmsg = f"管理体系图组合({len(chosen_combo)})少于管理章({len(mgmt_chapters)}),尾部轮转复用"
            (log.warning(_wmsg) if log else print(f"  ⚠ {_wmsg}"))
        else:
            _cyc = chosen_combo
        assignment["management_chart_types"] = {
            ch: ctype for ch, ctype in zip(mgmt_chapters, _cyc)
        }
    if base_chart_specs:
        for ch_num_str, specs in base_chart_specs.items():
            overrides = []
            for spec in specs:
                if spec.get("required", False):
                    continue
                # 进度横道图是工期响应的核心图形语义：可换布局，不可随机改成资源曲线或删除。
                if spec.get("type") == "schedule":
                    continue
                if rng.random() < 0.3:
                    alternatives = {
                        "hierarchy_flow": ["process_flow", "org_chart"],
                        "process_flow": ["hierarchy_flow"],
                        "org_chart": ["hierarchy_flow", "process_flow"],
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
    # ★ 关键词必须与 bid_writer 侧判定词同源(资源配备/组织机构/项目管理机构/组织管理),
    # 否则本函数选中的章号写进 chart_assignment.json 后 writer 判定不命中 → 模板被静默丢弃。
    # 命中不了时返回 ""(不写无效章号),不编造章号。
    for ch in chapters:
        if any(k in ch.get("title", "") for k in ("资源配备", "组织机构", "项目管理机构")):
            return str(ch["num"]).zfill(2)
    for ch in chapters:
        if "组织管理" in ch.get("title", ""):
            return str(ch["num"]).zfill(2)
    return ""


def _find_management_chapters(chapters: list[dict]) -> list[str]:
    found = []
    for ch in chapters:
        if any(k in ch.get("title", "") for k in ("质量", "安全", "环保", "环境")):
            found.append(str(ch["num"]).zfill(2))
    return found[:3]


_STALE_WORK_WORDS = ("棚圈", "牧道", "桥涵", "暖棚", "装配安装")


def _load_boq_item_names(extracted: Path) -> list[str]:
    """从清单提取文本中抓项目名称，供变体规划和组织模板项目化。"""
    names: list[str] = []
    seen: set[str] = set()
    for path in sorted(extracted.glob("*工程量清单*.txt")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            m = re.match(r"^\s*\d+\s*\|\s*[0-9A-Za-z]+\s*\|\s*([^|]+?)\s*\|", line)
            if not m:
                continue
            name = m.group(1).strip()
            if name and name not in seen and "工程" not in name[:2]:
                seen.add(name)
                names.append(name)
    return names


def _load_work_labels(extracted: Path) -> list[str]:
    """从 boq.json 生成项目专属作业队标签。"""
    boq_path = extracted / "boq.json"
    labels, seen = [], set()
    if boq_path.exists():
        try:
            boq = json.loads(boq_path.read_text(encoding="utf-8"))
        except Exception:
            boq = {}
        if not isinstance(boq, dict):  # 数组形状防御
            boq = {}
        for cat in boq.get("categories", []):
            if not isinstance(cat, dict):
                continue
            name = cat.get("name") or cat.get("category") or ""
            name = re.sub(r"^未分类-?", "", name).strip("- ")
            if name and name not in seen:
                seen.add(name)
                labels.append(f"{name}施工队")

    # Excel 清单项目通常只有提取文本、没有 boq.json。按具体工作项归并成
    # 项目专属作业队，避免组织图泄漏历史项目的棚圈/牧道名称。
    if not labels:
        item_names = _load_boq_item_names(extracted)
        buckets = [
            (("拆除", "清运"), "拆除清运作业队"),
            (("混凝土路面", "碎石"), "混凝土路面修补队"),
            (("沥青", "透层", "粘层"), "沥青路面修补队"),
            (("土方", "路基"), "土方基层作业队"),
        ]
        blob = " ".join(item_names)
        for keys, label in buckets:
            if any(k in blob for k in keys) and label not in seen:
                seen.add(label)
                labels.append(label)
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
            # 标签耗尽:宁可占位待生成期 LLM 润色,不留行业残留(棚圈/牧道历史标签)
            return f"{work_labels[-1]}作业组" if work_labels else "综合作业队"

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
    p.add_argument("--variant", default=None,
                   help="仅重新生成指定变体(如 V02)的标题计划;供 similarity gate 重种后定向刷新")
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

    try:
        variants = json.loads(variants_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"variants.json 解析失败: {e}")
        return 2
    if isinstance(variants, dict):
        _vs = variants.get("variants")
        # 缺键时不把整个 payload 当单变体(其无 id/chapter_model_plan 结构,必 KeyError)
        variants = _vs if isinstance(_vs, list) and _vs else []
    if not isinstance(variants, list) or not variants:
        log.error("variants.json 无有效变体(缺 variants 键或空表)")
        return 1
    _orig_index = {str(v.get("id", "")): i for i, v in enumerate(variants)}
    if args.variant:
        variants = [v for v in variants if str(v.get("id", "")).upper() == args.variant.upper()]
        if not variants:
            log.error(f"--variant {args.variant} 在 variants.json 中不存在")
            return 2
    n_variants = len(variants)

    base_score_items = {
        (c.get("score_item") or "").strip()
        for c in base_chapters
        if _is_tier_a(c) and (c.get("score_item") or "").strip() not in ("", "—", "-")
    }

    criteria_map = _load_criteria(extracted, base_chapters, log)
    project_brief = _load_project_brief(extracted)

    # 合格制:锁定基准子项(大纲二/三级节标题 1:1 保留),变体差异化下沉到
    # 正文/编号/元数据层(见 is_passfail_lock)
    passfail_lock = is_passfail_lock(extracted, log)
    if passfail_lock:
        log.info("合格制项目:锁定基准子项(大纲二/三级节标题 1:1),"
                 "变体差异化下沉到正文/编号/元数据层")

    # 附图及附表章:锁定为大纲点名的附图附表(1:1),不套切分维度(否则被写成"职责"散文漏附表)
    attachments = []
    outline_p = extracted / "mandatory_outline.json"
    if outline_p.exists():
        try:
            _ol = json.loads(outline_p.read_text(encoding="utf-8"))
            attachments = (_ol.get("attachments", []) or []) if isinstance(_ol, dict) else []
        except (ValueError, json.JSONDecodeError, OSError):
            attachments = []
    if attachments:
        log.info(f"附图及附表章将锁定为大纲点名的 {len(attachments)} 张附图附表(三家一致,不套切分维度)")
    log.info(f"评分标准命中 {len(criteria_map)}/{len(base_chapters)} 章"
             f"{'(其余回退基准子项作参考)' if len(criteria_map) < len(base_chapters) else ''}")

    # chart_specs / org_templates / work_labels
    base_chart_specs: dict[str, list] = {}
    chart_specs_path = extracted / "chart_specs.json"
    if chart_specs_path.exists():
        try:
            raw = json.loads(chart_specs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"chart_specs.json 解析失败(按无图表规格继续): {e}")
            raw = []
        if isinstance(raw, dict):
            raw = raw.get("specs", [])
        if not isinstance(raw, list):  # null/标量形状防御
            raw = []
        for spec in raw:
            if isinstance(spec, dict):
                base_chart_specs.setdefault(
                    str(spec.get("chapter", "")).zfill(2), []).append(spec)
    org_templates = {"templates": {}, "management_chart_types": {"combinations": []}}
    org_path = get_config_dir() / "org_templates.json"
    if org_path.exists():
        try:
            _ot = json.loads(org_path.read_text(encoding="utf-8"))
            if isinstance(_ot, dict):
                org_templates = _ot
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"org_templates.json 解析失败(用内置默认): {e}")
    work_labels = _load_work_labels(extracted)
    if work_labels:
        log.info(f"清单驱动作业队标签: {work_labels}")

    # 目标总页数 → 全局缩放因子(缺省沿用基准字数)
    # 变体 target_pages 是相对于共享计划声明的“目标总页数”缩放，而不是
    # 相对于章节表的正文估算页数。后者通常不含图表预留、封面和目录，拿它
    # 作分母会把 170 页主方案反向放大到约 230 页。
    base_budget = parse_plan_budget(plan_path)
    base_budget_pages = (base_budget.get("target_pages") or 0)
    base_density = (base_budget.get("chars_per_page") or 425.0)
    base_pages = base_budget_pages or sum(c.get("est_pages", 0) for c in base_chapters) or 1
    user_scale = 1.0
    if args.target_pages:
        user_scale = args.target_pages / base_pages
        log.info(f"用户指定目标页数 {args.target_pages}(基准 {base_pages:.0f}),"
                 f"全局缩放 ×{user_scale:.2f}")

    try:
        client = LLMClient.load(log=log)
    except Exception as e:  # noqa: BLE001
        log.error(f"LLM 配置加载失败({e}),变体标题差异化与 H2 切分中止")
        return 2
    rewrite_model = "deepseek_chat"
    _models_cfg = client.config.get("models", {}) or {}
    if (_models_cfg.get(rewrite_model, {}).get("disabled")
            or rewrite_model not in _models_cfg):
        # 模型停用/缺失时回退 default_model;仍不可用则取池内第一个可用模型
        rewrite_model = client.config.get("default_model", "deepseek")
        if _models_cfg.get(rewrite_model, {}).get("disabled") or rewrite_model not in _models_cfg:
            _usable = [k for k, v in _models_cfg.items() if not v.get("disabled")]
            if _usable:
                rewrite_model = _usable[0]
            else:
                log.error("无可用 LLM 模型,变体 H2 差异化生成中止")
                return 2

    log.info(f"开始为 {n_variants} 个变体生成差异化二级标题"
             f"(一级标题锁定评标表原文,模型={rewrite_model})...")

    # 1. 每变体:章节 = 基准(不变换),只缩放字数;并分配切分维度
    variant_structures: list[tuple[dict, list[dict], tuple[str, str]]] = []
    _used_strategies: set = set()  # 同批变体切分维度互异(避开碰撞)
    # 合格制目录差异化:已生成的标题序列(供后一家避用同一措辞)
    _pf_h1_taken: list[list[str]] = []
    # ★ 避撞清单必须**按章分桶**:同名子项(如「编制依据」)会出现在不同章里,
    #   旧的扁平 list 让 style_passfail_titles 把所有章的历史标题并成一个 occupied 集合,
    #   于是第 2 章的同名项被判"撞车"→ 丢弃 LLM 改写、退回「原题（补充说明）」,
    #   同一份目录反复出现该后缀(实测 V03 出现 7 次),既是机器痕迹又让标题不再等于大纲。
    _pf_h2_taken: dict[str, list[list[str]]] = {}
    if args.variant:
        # 定向重生成:把其他变体既有计划中的切分维度记为已占,防重种后撞轴
        #           ——同时把其他家已落盘的章/子项标题纳入避撞清单,否则重刷后
        #              本变体可能与其他家标题字面完全一致(反串标避撞失效)
        try:
            _raw_vs = json.loads(variants_path.read_text(encoding="utf-8")) if variants_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            _raw_vs = {}
        if isinstance(_raw_vs, dict):
            _raw_vs = _raw_vs.get("variants", [])
        for _v in (_raw_vs if isinstance(_raw_vs, list) else []):
            _vid = str(_v.get("id", "") or "")
            if not _vid or _vid.upper() == args.variant.upper():
                continue
            _vp = output / _vid / "chapter_plan_variant.md"
            if _vp.exists():
                _vt = _vp.read_text(encoding="utf-8", errors="ignore")
                _mm = re.search(r"切分维度【([^】]+)】", _vt)
                if _mm:
                    _used_strategies.add(_mm.group(1))
                _pf_h1_taken.append([x.strip() for x in re.findall(
                    r"^\| \d{2} \| [^|]+ \| ([^|]+) \|", _vt, re.M)])
                for _cm in re.finditer(
                        r"^###\s+(\d{2})\s[\s\S]*?(?=^###\s+\d{2}\s|\Z)", _vt, re.M):
                    _cnum = _cm.group(1)
                    _pf_h2_taken.setdefault(_cnum, []).append([
                        strip_inner_number(m.group(1)) for m in re.finditer(
                            r"^-\s+(?:\d+\.\d+|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）)\s*(.+?):\s*[\d,]+\s*字",
                            _cm.group(0), re.M)])
    for variant in variants:
        i = _orig_index.get(str(variant.get("id", "")), 0)
        # per-variant 目标页数(variant.target_pages 设了则按它大幅缩放,
        # 支持同项目多家单位不同厚度方案;未设则沿用原 ≤基准 反串标微差逻辑)
        vp = variant.get("target_pages")
        if vp:
            v_scale = vp / base_pages
        else:
            # 前 5 家用固定微差；第 6 家起叠加 variant 哈希抖动，破周期碰撞
            # (历史: 固定 [i%5] 使第 6/11 家与第 1 家同偏移，>7 家时同维度+同缩放雷同)
            _off = [0.0, -0.05, -0.02, -0.07, -0.04][i % 5]
            if i >= 5:
                _h = int(hashlib.md5(str(variant.get("id", i)).encode()).hexdigest()[:4], 16)
                _off -= 0.002 * (_h % 9)  # 0~-1.8% 变体专属抖动
            v_scale = user_scale * (1.0 + _off)
        chapters = build_variant_chapters(
            base_chapters, variant, v_scale, chars_per_page=base_density
        )
        if passfail_lock:
            chapters = style_passfail_chapter_titles(
                chapters, i, _pf_h1_taken, client, rewrite_model, log)
            _pf_h1_taken.append([c["title"] for c in chapters])
        try:
            verify_scoring_coverage(chapters, base_score_items, log)
        except RuntimeError as e:
            # 评分制下"变体丢了评分项覆盖"= 该变体必丢分,不能用 warning 放行
            # (评分覆盖是评分级硬闸);合格制项目无评分项可丢,保持容错。
            if passfail_lock:
                log.warning(f"  评分覆盖校验异常({e}),合格制项目降级继续")
            else:
                log.error(f"  评分覆盖校验失败({e}) —— 评分制下该变体将丢分,中止变体计划生成")
                return 1
        strategy = assign_strategy(
            i, variant_id=str(variant.get("id", "")),
            seed=variant.get("section_order_seed"),
            taken=_used_strategies)
        if passfail_lock:
            strategy = ("大纲锁定",
                        "合格制:一/二/三级节标题均逐字沿招标第七章大纲,不按维度重组;"
                        "变体差异体现在正文组织、编号体系与措辞")
        log.info(f"  {variant.get('id', '')}: 切分维度【{strategy[0]}】, 字数 ×{v_scale:.2f}, "
                 f"{len(chapters)} 章(同基准)")
        variant_structures.append((variant, chapters, strategy))

    # 2. 并行 LLM 子项分解(每变体一次调用,按各自维度切分)
    # 合格制:子项 = 基准大纲二/三级节标题(1:1),不调 LLM 重组(见 is_passfail_lock)
    def _decompose(item):
        variant, chapters, strategy = item
        if passfail_lock:
            return variant.get("id", ""), {}
        return variant.get("id", ""), decompose_variant(
            client, rewrite_model, variant.get("id", ""), strategy, chapters,
            criteria_map, project_brief, log,
        )

    decomposed: dict[str, dict[str, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(4, n_variants))) as ex:
        for vid, result in ex.map(_decompose, variant_structures):
            decomposed[vid] = result

    # 3. 落子项 + 渲染 + 图表分配
    for variant, chapters, strategy in variant_structures:
        vid = variant.get("id", "")
        sub_map = decomposed.get(vid, {})
        for c in chapters:
            att_subs = _attachment_subitems(c, attachments) if _is_attachment_chapter(c["title"]) else None
            if att_subs is not None:
                subs = att_subs  # 附图章:强制 = 大纲附表清单,绕开切分维度
            elif passfail_lock:
                # 合格制:大纲二/三级节标题集合 1:1 沿用基准(不套切分维度),
                # 但措辞按变体差异化 —— 三家目录字面完全一致会被评委判"投标文件异常一致"
                # (2) 剥掉基准的 "N.M " 前缀,与 LLM / _fallback_subitems 两路同口径
                base_subs = [
                    {"title": strip_inner_number(
                        re.sub(r"^\d+(?:\.\d+)*[、.\s]*", "", str(s.get("title", ""))).strip()),
                     "chars": int(s.get("chars") or 0)}
                    for s in (base_subitems.get(c["num"]) or [])
                ]
                if not base_subs:
                    base_subs = _fallback_subitems(c)
                subs = style_passfail_titles(
                    base_subs, _orig_index.get(str(variant.get("id", "")), 0),
                    _pf_h2_taken.get(c["num"], []), client, rewrite_model, log,
                    chapter_num=c["num"])
                # 避撞清单存**去编号后的基准标题**,否则带前缀(一、/（一）)的
                # 已用标题与基准标题比不上 → 第 3 家起避撞静默失效
                _pf_h2_taken.setdefault(c["num"], []).append(
                    [strip_inner_number(s["title"]) for s in subs])
            else:
                subs = sub_map.get(c["num"]) or _fallback_subitems(c)
            # 附表章不参与字数地板合并(附表一/二/…是招标硬性清单);
            # 合格制大纲子项同为招标硬清单,同样不合并(合并即丢评审锚点)
            c["subitems"] = _normalize_subitems(
                subs, c["target_chars"],
                merge_floor=(att_subs is None and not passfail_lock))

        v_dir = output / vid
        v_dir.mkdir(parents=True, exist_ok=True)
        (v_dir / "chapter_plan_variant.md").write_text(
            render_variant_plan(vid, strategy, chapters), encoding="utf-8"
        )
        chart_assign = _assign_charts_for_variant(
            variant, base_chart_specs, org_templates, chapters, work_labels, log=log
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
