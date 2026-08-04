#!/usr/bin/env python3
"""bid_chart_specs.py - 从 chapter_plan.md 派生 chart_specs.json

职责:
  1. 解析 chapter_plan 各章节(chart_count、章节标题)
  2. 解析"### 子项分配"段拿到每章的子节标题
  3. 用关键词启发式给每张图推 type(schedule / site_layout / org_chart / ...)
  4. 招标硬性要求的图(总平面图、进度图)标 required=true
  5. 输出 extracted/chart_specs.json,后续供 bid_writer 注入 prompt、
     bid_compliance 做覆盖检查

每张图的 spec 字段:
  - chapter: 章节号 "01" "02" ...
  - section: 子节号 "5.4" 或 ""(不知具体子节)
  - type: schedule | site_layout | org_chart | hierarchy_flow |
          process_flow | resource_curve | aoa_network
  - required: bool(招标硬性要求,缺失即合规阻断)
  - data_hint: 给 LLM 的内容提示(从章节/子节标题派生)
  - title: 图标题(给 markdown alt-text)

用户/Claude 在 Step 2 阶段可以**手动 override** 这个 JSON 文件
(改 type 或 required),作为人工修正层。

用法:
  python bid_chart_specs.py <tender_dir>
  python bid_chart_specs.py <tender_dir> --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import make_script_logger, parse_chapter_plan  # noqa: E402


# ─── 关键词 → chart type 映射 ──────────────────────────────────────────
# 顺序很重要:更具体的优先(进度+网络优先于进度)
_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("aoa_network",   ["施工进度网络图", "双代号网络", "网络计划", "关键线路图"]),
    ("schedule",      ["进度横道", "横道图", "甘特", "施工进度", "进度计划",
                       "工期计划", "进度安排"]),
    ("site_layout",   ["总平面图", "平面布置图", "总平面布置", "施工总平面",
                       "现场布置图", "临设布置"]),
    ("org_chart",     ["组织机构", "组织框图", "管理机构", "组织架构",
                       "项目组织", "管理组织", "框图"]),
    ("hierarchy_flow", ["管理体系", "体系图", "保证体系", "管理网络",
                        "管理框图"]),
    ("process_flow",  ["工艺流程", "流程图", "施工流程", "工序流程",
                       "验收流程", "检验流程"]),
    ("resource_curve", ["劳动力曲线", "投入曲线", "资源曲线", "资金曲线",
                        "材料进退场曲线", "用工曲线"]),
]


# ─── 招标硬性要求的图(必填) ────────────────────────────────────────────
# 这些关键词命中即 required=true
_REQUIRED_HINTS = [
    "施工总平面图", "总平面布置图",          # 招标第七章附表五
    "施工进度网络图", "进度横道图", "网络计划图",  # 招标第七章附表四
]


def classify_by_title(title: str) -> str | None:
    """根据章节/子节标题推 chart type。命中第一个规则即返回。"""
    if not title:
        return None
    for ctype, kws in _KEYWORD_RULES:
        for kw in kws:
            if kw in title:
                return ctype
    return None


def is_pure_table_section(title: str) -> bool:
    """子节标题是否是"纯表格"(非图)。已用 markdown 表的子节不需要派生图 spec。

    例:'拟投入本标段的主要施工设备表(附表一)' / '劳动力计划表'
    判定:含"表"且不含"图"。
    """
    if not title:
        return False
    return ("表" in title) and ("图" not in title)


def is_required_chart(title: str) -> bool:
    """招标硬性要求的图。"""
    return any(h in (title or "") for h in _REQUIRED_HINTS)


# ─── chapter_plan 子项分配段解析 ──────────────────────────────────────

# 匹配 "### 01 工程概况(1800 字,1 图,2.4 页)" 之类的章块
_CHAPTER_BLOCK_RE = re.compile(
    r"###\s+(\d{1,3})\s+([^\n（(]+)[（(].*?[)）].*?(?=\n###\s+\d|\n##\s|\Z)",
    re.DOTALL,
)
# 匹配 "- 5.4 主要资源配置计划概述:450 字"
_SUBITEM_RE = re.compile(
    r"^\s*[-•]\s+(\d+\.\d+)\s+(.+?)(?=[:：]\s*[\d,，]+\s*字)",
    re.MULTILINE,
)


def parse_subitems(plan_text: str) -> dict[str, list[tuple[str, str]]]:
    """从 chapter_plan.md 的"### 子项分配"段抓每章的子节列表。

    返回 {chapter_num: [(section_id, section_title), ...]}
    例: {"05": [("5.1", "编制依据..."), ("5.2", "施工阶段...")]}
    """
    result: dict[str, list[tuple[str, str]]] = {}
    for m in _CHAPTER_BLOCK_RE.finditer(plan_text):
        ch_num = m.group(1).zfill(2)
        block = m.group(0)
        items = []
        for sm in _SUBITEM_RE.finditer(block):
            sec_id = sm.group(1).strip()
            sec_title = sm.group(2).strip()
            items.append((sec_id, sec_title))
        if items:
            result[ch_num] = items
    return result


# ─── 主流程 ───────────────────────────────────────────────────────────

def derive_chart_specs(plan_path: Path, log: logging.Logger) -> list[dict]:
    """从 chapter_plan.md 派生 chart specs 列表。"""
    rows = parse_chapter_plan(plan_path)
    if not rows:
        log.warning("chapter_plan 解析为空")
        return []

    plan_text = plan_path.read_text(encoding="utf-8")
    subitems = parse_subitems(plan_text)

    specs: list[dict] = []
    for row in rows:
        ch_num = row["num"].zfill(2)
        ch_title = row["title"]
        chart_count = int(row.get("chart_count") or 0)
        if chart_count <= 0:
            continue

        # 拿子节列表
        sub_list = subitems.get(ch_num, [])

        # 给本章拼 chart_count 张图。策略:
        # 1. 先扫子节,标题命中 type 关键词的子节,直接落一张图
        # 2. 余下张数按章节标题推 type
        # 3. 若全部子节都没命中,平均落到前 N 个子节
        used_subs: set[str] = set()
        ch_specs: list[dict] = []

        # 第一遍:子节标题命中(跳过纯表格子节)
        for sec_id, sec_title in sub_list:
            if len(ch_specs) >= chart_count:
                break
            if is_pure_table_section(sec_title):
                used_subs.add(sec_id)  # 标记为已占用,避免第二遍兜底再用
                continue
            ctype = classify_by_title(sec_title)
            if ctype:
                full_title = f"{ch_title} - {sec_title}"
                ch_specs.append({
                    "chapter": ch_num,
                    "section": sec_id,
                    "type": ctype,
                    "required": is_required_chart(sec_title) or is_required_chart(ch_title),
                    "data_hint": sec_title,
                    "title": full_title,
                })
                used_subs.add(sec_id)

        # 第二遍:用章节标题推剩余张数的 type
        remaining = chart_count - len(ch_specs)
        if remaining > 0:
            ctype_fallback = classify_by_title(ch_title) or "process_flow"
            free_subs = [(sid, st) for sid, st in sub_list
                         if sid not in used_subs and not is_pure_table_section(st)]
            for i in range(remaining):
                if i < len(free_subs):
                    sec_id, sec_title = free_subs[i]
                    hint = sec_title
                else:
                    sec_id = ""
                    hint = ch_title
                full_title = f"{ch_title}{' - ' + hint if hint != ch_title else ''}"
                ch_specs.append({
                    "chapter": ch_num,
                    "section": sec_id,
                    "type": ctype_fallback,
                    "required": is_required_chart(ch_title),
                    "data_hint": hint,
                    "title": full_title,
                })

        specs.extend(ch_specs)

    return specs


def main() -> int:
    p = argparse.ArgumentParser(description="从 chapter_plan.md 派生 chart_specs.json")
    p.add_argument("tender_dir", help="投标目录(含 extracted/chapter_plan.md)")
    p.add_argument("--dry-run", action="store_true", help="只打印 specs,不写盘")
    p.add_argument("--out", default="", help="覆盖默认输出路径")
    args = p.parse_args()

    tender = Path(args.tender_dir).expanduser().resolve()
    ext = tender / "extracted"
    plan = ext / "chapter_plan.md"
    log = make_script_logger("bid_chart_specs", ext / "chart_specs.log")

    if not plan.exists():
        log.error(f"未找到 {plan}")
        return 2

    specs = derive_chart_specs(plan, log)
    if not specs:
        log.warning("未派生出任何 chart spec(chapter_plan 中所有章节图表数都是 0?)")

    out = Path(args.out) if args.out else ext / "chart_specs.json"

    payload = {
        "_doc": "由 bid_chart_specs.py 派生,可手动 override type/required/data_hint",
        "_source": str(plan.relative_to(tender)),
        "specs": specs,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    log.info("=" * 60)
    log.info(f"派生 {len(specs)} 个 chart spec:")
    for s in specs:
        flag = "★" if s["required"] else " "
        log.info(f"  {flag} {s['chapter']}.{s['section'] or '?'} {s['type']:15s}  {s['data_hint'][:30]}")
    log.info("=" * 60)
    log.info(f"required(招标硬性): {sum(1 for s in specs if s['required'])} / {len(specs)}")

    if args.dry_run:
        log.info(f"[DRY-RUN] 不写盘。预览:\n{text[:500]}...")
        return 0

    out.write_text(text, encoding="utf-8")
    log.info(f"输出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
