#!/usr/bin/env python3
"""
bid_template_match.py - Step 0 范本指纹比对

在 bid_extract.py 之后、bid_skeleton_extract.py 之前运行。
扫描招标主文档前 N 页(默认 50 页)的关键词,命中范本规则即加载常量,
让下游 bid_skeleton_extract.py 可跳过 LLM 调用,直接复用范本里的:
  - 16 章必含主题(对应 mandatory_outline.json)
  - 31 条 B1.x 否决条款(对应 disqualification.json 的范本部分)
  - 24 项 1.4.3 限制投标
  - 10.16 格式约束
  - 评分公式骨架

降级策略:
  - 命中 ≥ min_match → 加载范本,template_match 标记 hit
  - 命中 1 → 警告 + 仍加载范本 + 标 drift_warning,人工核对差异
  - 0 命中 → 不加载范本,template_match.hit=false,下游必须完整跑 LLM

输出:
  extracted/template_match.json — 比对结果 + 命中范本完整 dump

用法:
  python bid_template_match.py /path/to/tender/
  python bid_template_match.py /path/to/tender/ --template tibet_gov_simplified
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

TEMPLATES_DIR = SCRIPTS / "templates"

OK = "✓"
WARN = "⚠"
FAIL = "✗"


# ────────────────────────────────────────────────────────────────────
# 1. 主文档识别(沿用 bid_skeleton_extract 的策略)
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
        name = p.name
        if any(re.search(pat, name) for pat in _AUX_PATTERNS):
            return False
        return any(re.search(pat, name) for pat in _MAIN_PATTERNS)

    mains = [p for p in candidates if is_main(p)]
    if mains:
        mains.sort(key=lambda p: p.stat().st_size, reverse=True)
        return mains[0]
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]


# ────────────────────────────────────────────────────────────────────
# 2. 截前 N 页文本
# ────────────────────────────────────────────────────────────────────

_PAGE_HEADER_RE = re.compile(r"^\[第(\d+)页(?:\s*-\s*视觉OCR:[^\]]+)?[^\]]*\]\s*$", re.M)


def text_in_first_n_pages(full_text: str, n: int) -> str:
    """截取前 n 页内容(用 [第N页] 标记切)。无标记时返回前 n*3000 字符兜底。"""
    matches = list(_PAGE_HEADER_RE.finditer(full_text))
    if not matches:
        return full_text[: n * 3000]
    cutoff = None
    for m in matches:
        page_num = int(m.group(1))
        if page_num > n:
            cutoff = m.start()
            break
    if cutoff is None:
        return full_text
    return full_text[:cutoff]


# ────────────────────────────────────────────────────────────────────
# 3. 范本加载与匹配
# ────────────────────────────────────────────────────────────────────

def load_all_templates() -> list[dict]:
    """加载 templates/ 目录下所有 .json 范本。"""
    if not TEMPLATES_DIR.is_dir():
        return []
    templates = []
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            t["_template_path"] = str(p)
            templates.append(t)
        except json.JSONDecodeError:
            continue
    return templates


def match_template(template: dict, scoped_text: str) -> dict:
    """对单个范本计算匹配度。返回 {hit, hit_count, matched_keywords, code_match, drift_warning}"""
    rules = template.get("match_rules", {})
    any_of = rules.get("any_of", [])
    code_pattern = rules.get("code_pattern", "")
    min_match = int(rules.get("min_match", 2))

    matched_keywords = [kw for kw in any_of if kw in scoped_text]
    code_match = False
    if code_pattern:
        code_match = bool(re.search(code_pattern, scoped_text, re.M))

    hit_count = len(matched_keywords) + (1 if code_match else 0)
    hit = hit_count >= min_match
    drift = (hit_count == 1)  # 临界命中,标记需人工核对

    return {
        "template_id": template.get("template_id"),
        "display_name": template.get("display_name"),
        "hit": hit,
        "hit_count": hit_count,
        "min_match": min_match,
        "matched_keywords": matched_keywords,
        "code_match": code_match,
        "drift_warning": drift,
    }


# ────────────────────────────────────────────────────────────────────
# 4. 主流程
# ────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--template", help="强制使用指定 template_id(跳过指纹比对)")
    p.add_argument("--force", action="store_true",
                   help="覆盖已有 template_match.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}", file=sys.stderr)
        return 2

    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_template_match", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_template_match  目录: {tender_dir}")

    out_path = ext / "template_match.json"
    if out_path.exists() and not args.force:
        log.info(f"  template_match.json 已存在,跳过(--force 重抽)")
        return 0

    # 1. 主文档
    main_txt = pick_main_tender_text(ext)
    if not main_txt:
        log.error("未找到任何 .txt,先跑 bid_extract.py")
        return 2
    full_text = main_txt.read_text(encoding="utf-8")
    log.info(f"  主文档: {main_txt.name} ({len(full_text):,} 字符)")

    # 2. 加载所有范本
    templates = load_all_templates()
    if not templates:
        log.warning(f"  templates/ 目录为空: {TEMPLATES_DIR}")
        result = {
            "hit": False,
            "reason": "no_templates_available",
            "template": None,
            "candidates": [],
        }
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        log.info(f"  写入 {out_path.name}(无范本可匹配)")
        return 1

    log.info(f"  加载 {len(templates)} 个范本: "
             f"{', '.join(t.get('template_id', '?') for t in templates)}")

    # 3. 强制指定范本时,跳过比对
    if args.template:
        target = next((t for t in templates if t.get("template_id") == args.template), None)
        if not target:
            log.error(f"  未找到 template_id={args.template!r}")
            return 2
        log.info(f"  --template 强制使用: {args.template}")
        result = {
            "hit": True,
            "forced": True,
            "template": target,
            "match_info": {"template_id": args.template, "hit_count": -1,
                           "matched_keywords": [], "code_match": False,
                           "drift_warning": False},
            "candidates": [],
        }
    else:
        # 4. 比对每个范本
        scoped_text = text_in_first_n_pages(
            full_text, max(t.get("match_rules", {}).get("scope_pages", 50)
                           for t in templates)
        )
        log.info(f"  扫描前 {scoped_text.count(chr(10))} 行 ({len(scoped_text):,} 字符)")

        candidates = [match_template(t, scoped_text) for t in templates]
        candidates.sort(key=lambda c: c["hit_count"], reverse=True)

        # 5. 选最高分
        best_match = candidates[0]
        best_template = next((t for t in templates
                              if t.get("template_id") == best_match["template_id"]), None)

        for c in candidates:
            log.info(f"    [{c['template_id']}] 命中 {c['hit_count']}/{c['min_match']} "
                     f"keywords={c['matched_keywords']} code={c['code_match']}")

        if best_match["hit"]:
            log.info(f"  {OK} 命中范本: {best_match['template_id']} "
                     f"({best_match['hit_count']} 项)")
            if best_match["drift_warning"]:
                log.warning(f"  {WARN} 临界命中(只 1 项),建议人工核对范本是否适用")
            result = {
                "hit": True,
                "forced": False,
                "template": best_template,
                "match_info": best_match,
                "candidates": candidates,
            }
        else:
            log.warning(f"  {WARN} 无范本命中(最高 {best_match['hit_count']}/"
                        f"{best_match['min_match']}),下游需完整跑 LLM")
            result = {
                "hit": False,
                "forced": False,
                "template": None,
                "match_info": best_match,
                "candidates": candidates,
            }

    # 6. 写文件
    result["_meta"] = {
        "source_file": main_txt.name,
        "extracted_by": "bid_template_match.py",
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info(f"  写入 {out_path}")

    log.info("=" * 60)
    return 0 if result["hit"] else 1


if __name__ == "__main__":
    sys.exit(main())
