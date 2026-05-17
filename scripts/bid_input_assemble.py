#!/usr/bin/env python3
"""
bid_input_assemble.py - 组装 project_input.json(单一真相源)

合并 Step 1 + Step 1.5 + Step 1.5+ 全部产物为一份 JSON,作为 Step 2 写
brain.md/chapter_plan/disqualification_rules 的唯一输入(契约化)。

输入(可缺失,缺失字段填 null):
  - extracted/template_match.json     (Tier 0 范本)
  - extracted/project_facts.json      (Tier 1 项目事实 13 字段)
  - extracted/project_derived.json    (Tier 2 派生:海拔/季节/规模)
  - extracted/special_clauses.json    (Tier 4 项目特殊条款)
  - extracted/evaluation_method.json  (v2 骨架:评标办法)
  - extracted/mandatory_outline.json  (v2 骨架:必含大纲)
  - extracted/disqualification.json   (v2 骨架:废标条款全表)

输出:
  extracted/project_input.json
    + tier0..tier4 字段
    + skeleton(v2) 字段
    + rejection_lines:从 tier0 + tier1 + tier4 派生的扁平避雷清单
    + drives_index:每个字段驱动哪些章节(供 Step 2/3 调度)

退出码:
  0  全部输入文件齐全
  1  软警告(部分文件缺失,但有可用内容)
  2  硬失败(必备文件全缺)

用法:
  python bid_input_assemble.py /path/to/tender/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"


def _load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def derive_rejection_lines(tier0: dict | None, tier1: dict | None,
                           tier4: dict | None, skeleton_disq: dict | None) -> list[dict]:
    """生成扁平的避雷清单,供 Step 5 合规检查直接使用。
    每条:{rule_id, source, type, threshold, text, drives_compliance_check}
    """
    lines: list[dict] = []

    # 1. 来自 tier1 的硬约束(项目独有数值)
    if tier1:
        facts = tier1.get("facts", {})
        cp = facts.get("control_price", {}).get("value")
        if cp:
            lines.append({
                "rule_id": "PRICE_CEILING",
                "source": "tier1.control_price",
                "type": "max_value",
                "field": "投标报价",
                "threshold": cp,
                "unit": "元",
                "text": f"投标报价不得超过招标控制价 {cp:,.2f} 元(B1.10)",
                "drives_compliance_check": ["bid_compliance.price_check"],
            })
        dur = facts.get("duration_days", {}).get("value")
        if dur:
            lines.append({
                "rule_id": "DURATION_CEILING",
                "source": "tier1.duration_days",
                "type": "max_value",
                "field": "工期",
                "threshold": dur,
                "unit": "日历天",
                "text": f"投标文件载明的工期不得超过 {dur} 日历天(B1.16)",
                "drives_compliance_check": ["bid_compliance.duration_check"],
            })
        qual = facts.get("qualification", {}).get("value")
        if qual:
            lines.append({
                "rule_id": "QUALIFICATION",
                "source": "tier1.qualification",
                "type": "min_qualification",
                "field": "资质",
                "threshold": qual,
                "text": f"投标人资质须达 {qual} 及以上",
                "drives_compliance_check": ["bid_compliance.qualification_check"],
            })

    # 2. 来自 tier0 范本的 B1.x 31 条
    if tier0 and tier0.get("hit") and tier0.get("template"):
        b1 = tier0["template"].get("rejection_clauses_b1", {}).get("clauses", [])
        for c in b1:
            lines.append({
                "rule_id": c.get("id"),
                "source": "tier0.rejection_clauses_b1",
                "type": "binary_condition",
                "category": c.get("category"),
                "topic": c.get("topic"),
                "text": c.get("text_original"),
                "drives_compliance_check": ["bid_compliance.b1_check"],
            })

    # 3. 来自 tier4 的项目特殊条款(is_mandatory=true)
    if tier4:
        for c in tier4.get("clauses", []):
            if c.get("is_mandatory"):
                lines.append({
                    "rule_id": f"SPECIAL_{c.get('topic', '')[:10]}",
                    "source": "tier4.special_clauses",
                    "type": "binary_condition",
                    "category": c.get("category"),
                    "topic": c.get("topic"),
                    "text": c.get("text_original"),
                    "source_page": c.get("source_page"),
                    "drives_compliance_check": ["bid_compliance.special_check"],
                })

    # 4. v2 skeleton 的 disqualification.json(项目特定废标全表)
    # 与 tier0 B1.x 重复时跳过(B1 编号一致)
    if skeleton_disq:
        b1_ids = {c.get("id") for c in (
            tier0.get("template", {}).get("rejection_clauses_b1", {}).get("clauses", [])
            if tier0 and tier0.get("hit") else []
        )}
        for c in skeleton_disq.get("clauses", []):
            hint = c.get("source_file_hint") or ""
            if hint in b1_ids:
                continue  # 范本已有
            lines.append({
                "rule_id": hint or f"DISQ_{c.get('source_page')}_{c.get('category')}",
                "source": "skeleton.disqualification",
                "type": "binary_condition",
                "category": c.get("category"),
                "text": c.get("text_original"),
                "consequence": c.get("consequence"),
                "source_page": c.get("source_page"),
                "drives_compliance_check": ["bid_compliance.skeleton_check"],
            })

    return lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_input_assemble", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_input_assemble  目录: {tender_dir}")

    out_path = ext / "project_input.json"
    if out_path.exists() and not args.force:
        log.info(f"  project_input.json 已存在,跳过(--force 重组装)")
        return 0

    # 加载所有输入
    sources = {
        "tier0_template_match":   _load(ext / "template_match.json"),
        "tier1_project_facts":    _load(ext / "project_facts.json"),
        "tier2_project_derived":  _load(ext / "project_derived.json"),
        "tier4_special_clauses":  _load(ext / "special_clauses.json"),
        "skeleton_evaluation":    _load(ext / "evaluation_method.json"),
        "skeleton_outline":       _load(ext / "mandatory_outline.json"),
        "skeleton_disqualification": _load(ext / "disqualification.json"),
        "document_map_present":   (ext / "document_map.md").exists(),
    }

    available = {k: v is not None and v is not False for k, v in sources.items()}
    n_have = sum(1 for v in available.values() if v)
    log.info(f"  输入完整度: {n_have}/{len(available)}")
    for k, v in available.items():
        log.info(f"    {OK if v else WARN} {k}")

    if n_have == 0:
        log.error(f"{FAIL} 没有任何可用输入,请先跑 bid_extract / bid_skeleton_extract / 等")
        return 2

    # 派生 rejection_lines
    rejection_lines = derive_rejection_lines(
        sources["tier0_template_match"],
        sources["tier1_project_facts"],
        sources["tier4_special_clauses"],
        sources["skeleton_disqualification"],
    )
    log.info(f"  派生 rejection_lines: {len(rejection_lines)} 条")

    # 组装
    payload = {
        "project_id": (
            sources["tier1_project_facts"].get("facts", {})
            .get("project_code", {}).get("value")
            if sources["tier1_project_facts"] else None
        ),
        "project_name": (
            sources["tier1_project_facts"].get("facts", {})
            .get("project_name", {}).get("value")
            if sources["tier1_project_facts"] else None
        ),
        "tier0_template": sources["tier0_template_match"],
        "tier1_facts": sources["tier1_project_facts"],
        "tier2_derived": sources["tier2_project_derived"],
        "tier4_special": sources["tier4_special_clauses"],
        "skeleton": {
            "evaluation_method": sources["skeleton_evaluation"],
            "mandatory_outline": sources["skeleton_outline"],
            "disqualification": sources["skeleton_disqualification"],
        },
        "rejection_lines": rejection_lines,
        "_completeness": available,
        "_meta": {
            "assembled_by": "bid_input_assemble.py",
            "tender_dir": str(tender_dir),
        },
    }

    # 写文件(美观:rejection_lines 单独换行,其余一并)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"  ✓ 写入 {out_path.name} "
             f"({out_path.stat().st_size:,} 字节)")
    log.info("=" * 60)

    return 0 if n_have >= 5 else 1


if __name__ == "__main__":
    sys.exit(main())
