#!/usr/bin/env python3
"""
bid_chapter_intro.py - 工序B: post-writer 重写每章引言段,回指施工主线

为什么需要:
  现有 writer 的 POLISH 步骤写的章首引言是"章内视角"(本章背景+编制依据+子节概要),
  不跨章。导致整本施组读起来像 N 篇独立文章拼接("散装")。本脚本把每章引言升级为
  "叙事视角":回指总体施工部署纲领 + 承接前一章 + 点明本章叙事职责。

设计(控 context 稀释 C4):
  - 每章是一次独立短 LLM 调用,输入 ~600 字(部署声明 + 该章摘要 + 前章末段),输出 ~300 字
  - 不读全文,只取该章首段 + 末段做摘要
  - 只重写 H1 后第一个引言段,H1 标题/## 子节标题/正文原封不动(同 POLISH 铁律)

输入:
  - extracted/deployment_outline.json (主线声明)
  - extracted/chapter_narrative.json (每章叙事维度/聚焦难点)
  - extracted/chapter_plan.md (章节顺序)
  - output/[VXX/]NN_*.md (各章正文)

输出: 原地重写各章 md 的引言段(只动 H1 后第一段)

落点: bid_run.py write 步骤后(post-writer 钩子,与 bid_h1_fix.py 并列)

用法:
  python bid_chapter_intro.py /path/to/tender/
  python bid_chapter_intro.py /path/to/tender/ --variant V01
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger, parse_chapter_plan  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"

# 摘要长度(控 C4):只取章节首段+末段,不读全文
SUMMARY_HEAD_CHARS = 300
SUMMARY_TAIL_CHARS = 200
TARGET_INTRO_CHARS = 300


SYSTEM_PROMPT = """你是资深投标文件主笔编辑,负责为施工组织设计的各章撰写"叙事型引言"。

好的章首引言不是孤立的本章简介,而是整个文档叙事链条的一环:
1. 回指总体施工部署纲领(让读者感到各章都服从同一部署总思路)
2. 承接前一章(承上:紧接上一章所述,本章进一步...)
3. 点明本章叙事职责(启下:本章将从XX维度展开,重点解决YY)
4. 简述本章编制依据与内容概要(原有功能)

严格遵守:
1. 只输出引言段落(200-400字),不要 H1 标题(标题已存在),不要 ## 子标题
2. 引言是 H1 下的正文段落,不设独立小标题
3. 不得出现【】〖〗 ★ ◆ ■ ● ◎ ▲ △ ▼ ▽ ◇ □ ☆ ※ • ➤ ✓ ✅ 等装饰字符,强调只用粗体
4. 不得出现公司名/人名(暗标),主体用"本项目部"
5. 不要"综上所述/本章小结/以下章节将"等元语句
6. 引言要有信息量,引用具体的项目独有信息(数字/地点/子工程名),禁止通用套话"""


def build_intro_prompt(
    chapter_num: str,
    chapter_title: str,
    deployment_statement: str,
    narrative_dimension: str,
    difficulty_focus: list,
    prev_title: str,
    prev_tail: str,
    this_head: str,
    this_tail: str,
    variant_angle: str,
) -> str:
    """构造单章引言重写的 user prompt。"""
    focus_str = "、".join(difficulty_focus) if difficulty_focus else "(本章无强制聚焦难点,按维度自然展开)"
    prev_block = f"上一章「{prev_title}」末段:\n{prev_tail}" if prev_tail else "(本章为第一章,无前章)"

    return f"""【总体施工部署纲领(全文叙事主线)】
{deployment_statement}

【本章信息】
章节号:第 {chapter_num} 章
章节标题:{chapter_title}
本章叙事维度:{narrative_dimension}
本章须回应的难点:{focus_str}
{variant_angle}

【上下文摘要(供撰写衔接,不要照抄)】
{prev_block}

本章现有正文首段:
{this_head}

本章现有正文末段:
{this_tail}

【任务】
为「第 {chapter_num} 章 {chapter_title}」撰写一段叙事型引言(200-400字),要求:
1. 开头回指总体施工部署纲领(本项目以...为总体部署...)
2. 承接上一章(紧接第{prev_title.split("章")[-1] if prev_title else "一"}章所述...),无前章则从项目全局引入
3. 点明本章从「{narrative_dimension}」维度展开,呼应部署纲领
4. 简述本章编制依据与内容概要
直接输出引言段落正文,不要标题,不要解释。"""


def extract_intro_and_body(md_text: str) -> tuple[str, str, str, str]:
    """拆分章节 md:返回 (h1_line, 原引言段, 正文剩余, 末段摘要)。

    结构约定:
      第1行 = `# 标题`
      第1行后到第一个 ## 之间 = 引言段(可能为空)
      第一个 ## 起 = 正文
    """
    lines = md_text.split("\n")
    if not lines or not lines[0].startswith("# "):
        return ("", "", md_text, "")

    h1 = lines[0]
    rest = lines[1:] if len(lines) > 1 else []

    # 找第一个 ## 子节的位置
    h2_idx = -1
    for i, ln in enumerate(rest):
        if ln.startswith("## "):
            h2_idx = i
            break

    if h2_idx > 0:
        intro_block = "\n".join(rest[:h2_idx]).strip()
        body = "\n".join(rest[h2_idx:])
    else:
        # 无 ## 子节,整个 rest 当正文
        intro_block = "\n".join(rest).strip()
        body = ""

    # 末段摘要:取正文最后 SUMMARY_TAIL_CHARS 字符的纯文本
    tail_text = re.sub(r"```json\s*\n.*?\n```", "", body, flags=re.DOTALL)
    tail_text = re.sub(r"```[\s\S]*?```", "", tail_text)
    tail = tail_text.strip()[-SUMMARY_TAIL_CHARS:] if tail_text.strip() else ""

    # 首段摘要:引言+正文开头
    head_src = (intro_block + "\n" + body).strip()
    head_src = re.sub(r"```[\s\S]*?```", "", head_src)
    head = head_src[:SUMMARY_HEAD_CHARS]

    return (h1, intro_block, body, tail)


def rewrite_chapter_intro(
    client: LLMClient,
    md_path: Path,
    chapter: dict,
    deployment_statement: str,
    prev_chapter: dict,
    variant_angle: str,
    log,
) -> bool:
    """重写单章引言。返回是否改动。"""
    md_text = md_path.read_text(encoding="utf-8")
    h1, intro_block, body, this_tail = extract_intro_and_body(md_text)
    if not h1:
        log.warning(f"  {md_path.name}: 无 H1 标题,跳过")
        return False

    this_head = intro_block[:SUMMARY_HEAD_CHARS] if intro_block else ""
    prev_tail = prev_chapter.get("tail", "") if prev_chapter else ""
    prev_title = prev_chapter.get("title", "") if prev_chapter else ""

    prompt = build_intro_prompt(
        chapter_num=chapter["num"],
        chapter_title=chapter["title"],
        deployment_statement=deployment_statement,
        narrative_dimension=chapter.get("narrative_dimension", ""),
        difficulty_focus=chapter.get("difficulty_focus", []),
        prev_title=prev_title,
        prev_tail=prev_tail,
        this_head=this_head,
        this_tail=this_tail,
        variant_angle=variant_angle,
    )

    try:
        resp = client.chat(
            "deepseek_chat",
            SYSTEM_PROMPT,
            prompt,
            temperature=0.5,
            max_tokens=800,
        )
    except Exception as e:
        log.error(f"  {md_path.name}: LLM 调用失败 {e}")
        return False

    new_intro = resp.text.strip()
    # 去除可能的代码围栏/标题前缀
    new_intro = re.sub(r"^```\w*\s*\n", "", new_intro)
    new_intro = re.sub(r"\n```\s*$", "", new_intro)
    # 如果模型多嘴加了 # 标题,去掉
    new_intro = re.sub(r"^#+\s.*\n", "", new_intro).strip()
    if not new_intro or len(new_intro) < 50:
        log.warning(f"  {md_path.name}: 引言过短({len(new_intro)}字),跳过")
        return False

    # 重组:H1 + 空行 + 新引言 + 空行 + 正文(若有)
    parts = [h1, "", new_intro]
    if body:
        parts.append("")
        parts.append(body)
    new_text = "\n".join(parts)
    md_path.write_text(new_text, encoding="utf-8")
    return True


def process_directory(
    client: LLMClient,
    chapter_dir: Path,
    chapters_plan: list[dict],
    narrative: dict,
    deployment_statement: str,
    variant_angle: str,
    log,
) -> int:
    """处理一个目录(单份 output/ 或 output/VXX/)的所有章节,按顺序重写引言。"""
    # 建立 章号 -> 该章 narrative 信息
    narr_map = narrative.get("chapters", {}) if narrative else {}

    # 章号 -> md 文件
    md_by_num: dict[str, Path] = {}
    for md in sorted(chapter_dir.glob("[0-9][0-9]_*.md")):
        md_by_num[md.name[:2]] = md

    # 按 plan 顺序处理(保证 prev_chapter 正确)
    prev_chapter_info: dict = {}
    rewritten = 0
    for ch in chapters_plan:
        num = str(ch.get("num", "")).zfill(2)
        md_path = md_by_num.get(num)
        if not md_path:
            continue
        narr_ch = narr_map.get(num, {})
        # 注入 narrative 字段
        ch_with_narr = dict(ch)
        ch_with_narr["narrative_dimension"] = narr_ch.get("narrative_dimension", "")
        ch_with_narr["difficulty_focus"] = narr_ch.get("difficulty_focus", [])

        changed = rewrite_chapter_intro(
            client, md_path, ch_with_narr, deployment_statement,
            prev_chapter_info, variant_angle, log,
        )
        if changed:
            rewritten += 1
            log.info(f"  ✓ {md_path.name} 引言重写({len(narr_ch.get('difficulty_focus', []))} 聚焦难点)")

        # 记录本章末段,供下一章承接(必须读文件拿最新 tail)
        if md_path.exists():
            _, _, _, tail = extract_intro_and_body(md_path.read_text(encoding="utf-8"))
            prev_chapter_info = {"title": ch.get("title", ""), "tail": tail}

    return rewritten


def load_coherence_config() -> dict:
    """读 configs/bid_coherence.json。"""
    from bid_common import get_bid_gen_home
    p = get_bid_gen_home() / "configs" / "bid_coherence.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run(tender_dir: Path, variant_filter: str, log) -> int:
    ext = tender_dir / "extracted"
    output = tender_dir / "output"
    if not output.is_dir():
        log.error("output/ 不存在")
        return 2

    # 必需输入:deployment_outline + chapter_narrative
    deploy_path = ext / "deployment_outline.json"
    narr_path = ext / "chapter_narrative.json"
    if not deploy_path.exists() or not narr_path.exists():
        log.error("缺少 deployment_outline.json 或 chapter_narrative.json,先跑工序A(deployment_synthesize + plan)")
        return 2

    deploy = json.loads(deploy_path.read_text(encoding="utf-8"))
    narrative = json.loads(narr_path.read_text(encoding="utf-8"))
    deployment_statement = deploy.get("deployment_statement", "")
    if not deployment_statement:
        log.warning("deployment_statement 为空,引言将缺少主线回指")

    cfg = load_coherence_config()
    intro_cfg = cfg.get("chapter_intro", {}) or {}
    global SUMMARY_HEAD_CHARS, SUMMARY_TAIL_CHARS, TARGET_INTRO_CHARS
    SUMMARY_HEAD_CHARS = int(intro_cfg.get("summary_head_chars", SUMMARY_HEAD_CHARS))
    SUMMARY_TAIL_CHARS = int(intro_cfg.get("summary_tail_chars", SUMMARY_TAIL_CHARS))

    # 章节顺序(基准 plan)
    chapters_plan = parse_chapter_plan(ext / "chapter_plan.md")
    if not chapters_plan:
        log.error("chapter_plan.md 无可解析章节")
        return 2

    client = LLMClient.load(log=log)

    # 决定处理哪些目录
    targets: list[tuple[str, Path]] = []  # (variant_id_or_"单份", dir)
    variant_dirs = sorted(output.glob("V[0-9][0-9]"))
    if variant_filter:
        vd = output / variant_filter.upper()
        if not vd.is_dir():
            log.error(f"变体目录不存在: {vd}")
            return 2
        targets.append((variant_filter.upper(), vd))
    elif variant_dirs:
        for vd in variant_dirs:
            targets.append((vd.name, vd))
    else:
        targets.append(("单份", output))

    total = 0
    for vid, ch_dir in targets:
        # 变体叙事角度(C2 差异化):不同变体用不同角度描述部署,避免引言趋同
        variant_angle = ""
        try:
            from bid_plan_variants import assign_strategy
            idx = int(re.sub(r"\D", "", vid)) if vid != "单份" else 0
            sname, sdesc = assign_strategy(idx)
            variant_angle = f"【本份叙事角度(反串标):{sname}】{sdesc}"
        except Exception:
            pass

        log.info(f"[{vid}] 处理 {ch_dir} (角度={'有' if variant_angle else '无'})")
        n = process_directory(
            client, ch_dir, chapters_plan, narrative,
            deployment_statement, variant_angle, log,
        )
        total += n
        client.flush_stats_to_json(output / "logs" / "llm_stats.json", "chapter_intro")

    log.info(f"完成: 重写 {total} 章引言")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--variant", default="", help="只处理指定变体(如 V01);缺省处理全部")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    output = tender_dir / "output"
    log = make_script_logger("bid_chapter_intro", output / "logs" / "chapter_intro.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_chapter_intro  目录: {tender_dir}")
    code = run(tender_dir, args.variant, log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
