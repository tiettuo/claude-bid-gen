#!/usr/bin/env python3
"""
bid_coherence_pass.py - 工序C: 跨章勾兑(assemble 前的"茅台勾兑"工序)

为什么需要:
  工序A/B 让每章"知道自己的职责"和"引言回指主线",但章节正文之间仍缺乏横向连接:
  - 无「详见第X章」引用(读者无法在章 N 跳到章 M 的相关内容)
  - 多章可能在重复讲同一件事(如质量保证体系在质量章+安全章都大段展开)
  - 跨章数据矛盾(工期/吨位/人数在不同章不一致)

三个子任务,三次独立 LLM 调用(不混一个 prompt,控 context 稀释 C4):
  C-1 引用编织: 各章摘要 → 引用映射表 → 回写「详见第X章」到引言末/相关子节
  C-2 重复检测: 各章摘要 → 重复主题报告 → 对精简章节调 bid_concise 局部精简
  C-3 矛盾修复: 复用 expert_review.cross_chapter_check → 对矛盾章节提示重写

落点: bid_run.py STEPS.coherence_pass,assemble 前

用法:
  python bid_coherence_pass.py /path/to/tender/
  python bid_coherence_pass.py /path/to/tender/ --variant V01
  python bid_coherence_pass.py /path/to/tender/ --skip redundancy  # 跳过重复检测
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

# 每章摘要长度(控 C4:全章合 3-5K token,不读全文)
SUMMARY_CHARS = 400


# ─── C-1 引用编织 ─────────────────────────────────────────────────────

REFERENCE_SYSTEM = """你是投标文件结构编辑,负责在章节间编织「详见第X章」交叉引用。

好的施组各章应互相呼应:章 N 提到某个主题时,若该主题在章 M 有更详细论述,
应加「(详见第M章)」指引,让评委能快速定位、体现文档的整体性。

严格遵守:
1. 只输出 JSON,不要解释
2. 每条引用 = {章号, 插入位置锚点(该章某句话的前15字), 引用文本(如"(详见第3章施工方案)")"}
3. 引用必须真实相关(章 N 确实提到了章 M 详述的主题),禁止强行硬塞
4. 一章最多插入 2 条引用(避免引用泛滥)
5. 若章节间无明显交叉,输出空列表"""


REFERENCE_USER_TEMPLATE = """【各章内容摘要】
{chapters_summary}

【任务】
分析上述各章内容,找出应该加「详见第X章」交叉引用的位置。
只找真实相关的交叉点,每章最多 2 条。

输出 JSON:
{{
  "references": [
    {{
      "chapter": "目标章号(在此章插入引用)",
      "anchor_text": "该章正文中某句话的前15字(定位插入点)",
      "ref_text": "(详见第M章XXX)"
    }}
  ]
}}"""


def _build_chapters_summary(chapters_with_text: list[dict]) -> str:
    """构造各章摘要(每章 SUMMARY_CHARS 字)。"""
    parts = []
    for ch in chapters_with_text:
        num = ch["num"]
        title = ch.get("title", "")
        # 摘要:首段(去 json 块/标题)
        text = ch.get("summary", "")
        parts.append(f"【第{num}章 {title}】\n{text}")
    return "\n\n".join(parts)


def weave_references(client, chapters_dir: Path, chapters: list[dict],
                     chapters_text: dict, log) -> int:
    """C-1: 调 LLM 生成引用映射,回写到各章 md。返回插入的引用数。"""
    # 构造摘要输入
    ch_with_summary = []
    for ch in chapters:
        num = str(ch.get("num", "")).zfill(2)
        full = chapters_text.get(num, "")
        # 去掉 json 块和标题,取前 SUMMARY_CHARS 字
        clean = re.sub(r"```[\s\S]*?```", "", full)
        clean = re.sub(r"^#+\s.*$", "", clean, flags=re.MULTILINE).strip()
        ch_with_summary.append({
            "num": num,
            "title": ch.get("title", ""),
            "summary": clean[:SUMMARY_CHARS],
        })

    summary = _build_chapters_summary(ch_with_summary)
    prompt = REFERENCE_USER_TEMPLATE.format(chapters_summary=summary)

    try:
        resp = client.chat("deepseek_chat", REFERENCE_SYSTEM, prompt,
                           temperature=0.3, max_tokens=2000)
    except Exception as e:
        log.error(f"  C-1 引用编织 LLM 失败: {e}")
        return 0

    raw = resp.text.strip()
    raw = re.sub(r"^```\w*\s*\n", "", raw)
    raw = re.sub(r"\n```\s*$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"  C-1 引用编织 JSON 解析失败,跳过")
        return 0

    refs = data.get("references", []) or []
    inserted = 0
    md_by_num = {md.name[:2]: md for md in chapters_dir.glob("[0-9][0-9]_*.md")}

    for ref in refs:
        ch_num = str(ref.get("chapter", "")).zfill(2)
        anchor = (ref.get("anchor_text") or "")[:15]
        ref_text = ref.get("ref_text", "")
        if not (ch_num and anchor and ref_text):
            continue
        md_path = md_by_num.get(ch_num)
        if not md_path:
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
            # 在 anchor 句末插入引用(找第一个包含 anchor 的段落)
            if anchor in text and ref_text not in text:
                # 在 anchor 首次出现处的句号/。后插入
                idx = text.find(anchor)
                # 找该句的结束标点
                for end in ("。", "；", ";", "！"):
                    end_idx = text.find(end, idx)
                    if end_idx > 0:
                        new_text = text[:end_idx + 1] + ref_text + text[end_idx + 1:]
                        md_path.write_text(new_text, encoding="utf-8")
                        inserted += 1
                        break
        except Exception as e:
            log.warning(f"  C-1 回写 {ch_num} 失败: {e}")

    log.info(f"  C-1 引用编织: 插入 {inserted}/{len(refs)} 条「详见第X章」")
    return inserted


# ─── C-2 重复检测 ─────────────────────────────────────────────────────

REDUNDANCY_SYSTEM = """你是投标文件质量审核员,负责检测跨章节的内容重复。

施组各章应分工明确:同一主题(如质量保证体系、安全文明施工、应急预案)若在多章
大段重复展开,说明章节分工混乱,评委会认为"注水、不专业"。

严格遵守:
1. 只输出 JSON
2. 只报告"实质性重复"(同一主题在 2 章以上各有 >300 字展开),不报告正常的概念提及
3. 每条 = 重复主题 + 出现章号列表 + 建议保留章(该主题最该深入的章) + 建议精简章
4. 无重复时输出空列表"""


REDUNDANCY_USER_TEMPLATE = """【各章内容摘要】
{chapters_summary}

【任务】
检测跨章节的实质性内容重复(同一主题在多章 >300 字展开)。

输出 JSON:
{{
  "redundancies": [
    {{
      "topic": "重复主题(如质量保证体系)",
      "in_chapters": ["03", "07"],
      "keep_chapter": "03(该主题最该深入的章)",
      "trim_chapters": ["07(建议精简,只保留概述+详见第3章)"]
    }}
  ]
}}"""


def detect_redundancy(client, chapters: list[dict], chapters_text: dict, log) -> list[dict]:
    """C-2: 调 LLM 检测跨章重复。返回冗余清单。"""
    ch_with_summary = []
    for ch in chapters:
        num = str(ch.get("num", "")).zfill(2)
        full = chapters_text.get(num, "")
        clean = re.sub(r"```[\s\S]*?```", "", full)
        clean = re.sub(r"^#+\s.*$", "", clean, flags=re.MULTILINE).strip()
        ch_with_summary.append({
            "num": num, "title": ch.get("title", ""),
            "summary": clean[:SUMMARY_CHARS],
        })
    summary = _build_chapters_summary(ch_with_summary)
    prompt = REDUNDANCY_USER_TEMPLATE.format(chapters_summary=summary)

    try:
        resp = client.chat("deepseek_chat", REDUNDANCY_SYSTEM, prompt,
                           temperature=0.3, max_tokens=2000)
    except Exception as e:
        log.error(f"  C-2 重复检测 LLM 失败: {e}")
        return []

    raw = resp.text.strip()
    raw = re.sub(r"^```\w*\s*\n", "", raw)
    raw = re.sub(r"\n```\s*$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("  C-2 重复检测 JSON 解析失败,跳过")
        return []
    return data.get("redundancies", []) or []


def fix_redundancy(redundancies: list[dict], chapters_dir: Path,
                   variant_id: str, tender_dir: Path, client, log) -> int:
    """对重复主题的"建议精简"章节,调 bid_concise 局部精简。返回处理章数。"""
    if not redundancies:
        log.info("  C-2 重复检测: 无实质性重复")
        return 0

    # 收集需要精简的章号(去重)
    trim_nums: set[str] = set()
    for r in redundancies:
        for tc in r.get("trim_chapters", []):
            # trim_chapters 格式 "07(建议...)",取前2位数字
            m = re.match(r"(\d{2})", str(tc))
            if m:
                trim_nums.add(m.group(1))

    log.info(f"  C-2 重复检测: {len(redundancies)} 个重复主题,精简 {len(trim_nums)} 章")
    for r in redundancies:
        log.info(f"    · {r.get('topic', '?')}: 出现于 {r.get('in_chapters', [])}")

    # 调 bid_concise 对精简章做轻度压缩(ratio 0.9,只去冗余不伤筋骨)
    processed = 0
    for num in sorted(trim_nums):
        md_path = chapters_dir / next(
            (p.name for p in chapters_dir.glob(f"{num}_*.md")), ""
        )
        if not md_path.exists():
            continue
        try:
            from bid_concise import concise
            target = int(len(md_path.read_text(encoding="utf-8")) * 0.9)
            concise(client, md_path, target)
            processed += 1
            log.info(f"    精简 {md_path.name}(去冗余 10%)")
        except Exception as e:
            log.warning(f"    精简 {num} 失败: {e}")

    return processed


# ─── C-3 矛盾修复 ─────────────────────────────────────────────────────

def detect_and_fix_conflicts(client, chapters: list[dict], chapters_text: dict,
                             project_brief: str, chapters_dir: Path, log) -> int:
    """C-3: 复用 expert_review.cross_chapter_check 检测数据矛盾,输出报告。

    自动修复留给后续(需 writer 带 --fix-conflict,本轮只报告)。
    返回发现的矛盾数。
    """
    try:
        from bid_expert_review import cross_chapter_check, CROSS_CHAPTER_USER_TEMPLATE
    except ImportError:
        log.warning("  C-3 无法导入 bid_expert_review,跳过矛盾检测")
        return 0

    # 拼接全章(截断,控 token)
    all_md = "\n\n".join(
        f"=== 第{str(ch.get('num','')).zfill(2)}章 {ch.get('title','')} ===\n"
        + chapters_text.get(str(ch.get('num','')).zfill(2), '')[:2000]
        for ch in chapters
    )

    result = cross_chapter_check(client, "deepseek", all_md, project_brief, log)
    if not result:
        log.info("  C-3 矛盾检测: 无输出或调用失败")
        return 0

    defects = result.get("cross_defects", []) or []
    if not defects:
        log.info("  C-3 矛盾检测: ✓ 无跨章数据矛盾")
        return 0

    log.warning(f"  C-3 矛盾检测: 发现 {len(defects)} 处跨章数据矛盾:")
    for d in defects[:8]:
        log.warning(f"    · {d.get('param', '?')}: {d.get('reason', '')}")

    # 写报告(本轮只报告不自动修复,避免误改正确数据)
    report = {"cross_defects": defects, "auto_fixed": False,
              "note": "本轮只报告;fix_conflicts=true 时由人工或后续 writer --fix-conflict 处理"}
    report_path = chapters_dir / "reports" / "coherence_conflicts.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return len(defects)


# ─── 主流程 ───────────────────────────────────────────────────────────

def load_chapters_text(chapters_dir: Path) -> dict[str, str]:
    """读所有章节 md,返回 {章号: 全文}。"""
    out = {}
    for md in chapters_dir.glob("[0-9][0-9]_*.md"):
        out[md.name[:2]] = md.read_text(encoding="utf-8")
    return out


def process_variant(
    client: LLMClient, variant_id: str, variant_dir: Path,
    extracted: Path, tender_dir: Path, skips: set, log,
) -> dict:
    """处理单个变体(或单份)。返回统计。"""
    stats = {"refs": 0, "redundancy_fixed": 0, "conflicts": 0}

    # 章节计划
    plan_path = variant_dir / "chapter_plan_variant.md"
    if not plan_path.exists():
        plan_path = extracted / "chapter_plan.md"
    chapters = parse_chapter_plan(plan_path)
    if not chapters:
        log.warning(f"  [{variant_id}] 无可解析章节,跳过")
        return stats

    chapters_text = load_chapters_text(variant_dir)
    if not chapters_text:
        log.warning(f"  [{variant_id}] 无章节 md,跳过")
        return stats

    log.info(f"[{variant_id}] 处理 {len(chapters)} 章 ({variant_dir.name})")

    # project_brief(矛盾检测用)
    project_brief = ""
    facts_path = extracted / "project_facts.json"
    if facts_path.exists():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8")).get("facts", {})
            name = (facts.get("project_name") or {}).get("value", "")
            project_brief = str(name)[:200]
        except Exception:
            pass

    # C-1 引用编织
    if "references" not in skips:
        stats["refs"] = weave_references(client, variant_dir, chapters, chapters_text, log)

    # C-2 重复检测 + 精简
    if "redundancy" not in skips:
        reds = detect_redundancy(client, chapters, chapters_text, log)
        stats["redundancy_fixed"] = fix_redundancy(reds, variant_dir, variant_id, tender_dir, client, log)

    # C-3 矛盾检测
    if "conflicts" not in skips:
        stats["conflicts"] = detect_and_fix_conflicts(
            client, chapters, chapters_text, project_brief, variant_dir, log
        )

    client.flush_stats_to_json(tender_dir / "output" / "logs" / "llm_stats.json", "coherence_pass")
    return stats


def run(tender_dir: Path, variant_filter: str, skips: set, log) -> int:
    ext = tender_dir / "extracted"
    output = tender_dir / "output"
    if not output.is_dir():
        log.error("output/ 不存在")
        return 2

    # 前置依赖检查(chapter_narrative 是工序A产物,没有它勾兑意义打折但不阻断)
    narr_path = ext / "chapter_narrative.json"
    if not narr_path.exists():
        log.warning("chapter_narrative.json 不存在(工序A未跑),勾兑仍可进行但效果打折")

    client = LLMClient.load(log=log)

    # 决定处理哪些目录
    targets: list[tuple[str, Path]] = []
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

    total = {"refs": 0, "redundancy_fixed": 0, "conflicts": 0}
    for vid, vdir in targets:
        stats = process_variant(client, vid, vdir, ext, tender_dir, skips, log)
        for k in total:
            total[k] += stats[k]

    log.info("=" * 40)
    log.info(f"勾兑完成: 引用 {total['refs']} / 重复精简 {total['redundancy_fixed']} 章 / "
             f"矛盾 {total['conflicts']} 处")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--variant", default="", help="只处理指定变体")
    p.add_argument("--skip", nargs="*", default=[],
                   choices=["references", "redundancy", "conflicts"],
                   help="跳过指定子任务")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    output = tender_dir / "output"
    log = make_script_logger("bid_coherence_pass", output / "logs" / "coherence_pass.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_coherence_pass  目录: {tender_dir}")
    code = run(tender_dir, args.variant, set(args.skip), log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
