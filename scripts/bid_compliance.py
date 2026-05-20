#!/usr/bin/env python3
"""
bid_compliance.py - 投标技术文件自动合规扫描
扫描 output/ 下所有 .md 章节文件，交叉比对 disqualification_rules.md 中的要求，
输出机械可检项的结果（供 Claude 做 Step 5 合规检查时引用）。

检查项：
  1. 暗标禁用信息（公司名/具名岗位/盖章动作等）
  2. 关键响应词是否出现（工期日历天、质量标准、强制响应项）
  3. 章节字数统计（对照 chapter_plan 目标字数）
  4. 章节-评分表对齐（章节数量/标题/分值与 chapter_plan.md 一致）

用法: python bid_compliance.py /path/to/tender/
运行日志: extracted/run.log
"""

from __future__ import annotations

import sys
import re
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    make_script_logger,
    parse_chapter_plan,
    collect_chapter_files,
    scan_blind_bid_violations,
    needs_blind_scan,
    chapter_chars_floor,
    count_chinese_chars,
)
from bid_plan_verify import topic_covered  # noqa: E402  复用语义匹配

# 关键响应词提取规则：从 disqualification_rules.md 自动挖出数字+单位组合
RESPONSE_RULES = [
    # (label, 正则-从规则文件中提取, 是否必须在 output 中出现)
    ("工期响应", r"(\d{2,4}\s*日历天)", True),
    ("暗标响应", r"(技术暗标|暗标格式)", False),
    ("双代号网络图", r"(双代号网络图)", False),
]



# ─── 标准编造检测（P0.6）───────────────────────────────────────────────
_STANDARD_RE = re.compile(
    r"(GB(?:\s*/\s*T)?|JGJ(?:\s*/\s*T)?|CJJ|DB54(?:\s*/\s*T)?)\s*(\d+)(?:[.\-](\d{4}))?"
)

def load_standards_truth(config_dir: Path) -> dict:
    """加载标准版本真值表"""
    truth_path = config_dir / "standards_truth.json"
    if truth_path.exists():
        with open(truth_path) as f:
            data = json.load(f)
        return data.get("standards", {})
    return {}


def normalize_standard_key(prefix: str, num: str) -> str:
    """归一化标准编号键:去空格后拼接前缀与编号。如 ("GB / T","50502")→"GB/T50502"。"""
    return f"{(prefix or '').replace(' ', '')}{(num or '').strip()}"


def standard_lookup_candidates(prefix: str, num: str) -> list[str]:
    """返回标准在真值表中的候选键:精确写法优先,再补 GB↔GB/T 变体。

    招标方案常把 GB/T 标准误写成 GB(或反之),真值表只存一种写法,
    故两种写法都要查。
    """
    key = normalize_standard_key(prefix, num)
    if "/T" in key:
        return [key, key.replace("/T", "")]
    variant = re.sub(r"^(GB|JGJ|CJJ|DB54)", r"\1/T", key)
    return [key, variant] if variant != key else [key]


def check_standards_fabrication(md_files: list[Path], truth: dict, log: logging.Logger) -> dict:
    """扫描章节文件中的标准引用，检测编造的年份版本。
    
    Returns: {"total": int, "fabricated": int, "details": list[dict]}
    """
    total = 0
    fabricated = 0
    details = []
    
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        for m in _STANDARD_RE.finditer(text):
            prefix = m.group(1)
            num = m.group(2)
            year = m.group(3)
            total += 1

            true_year = None
            matched_key = None
            for k in standard_lookup_candidates(prefix, num):
                if k in truth:
                    true_year = truth[k]
                    matched_key = k
                    break

            if true_year and year and year != true_year:
                fabricated += 1
                details.append({
                    "file": f.name,
                    "standard": matched_key,
                    "cited_year": year,
                    "true_year": true_year,
                    "text": m.group(0),
                })
    result = {"total": total, "fabricated": fabricated, "details": details}
    if fabricated:
        log.warning(f"标准编造检测: {fabricated}/{total} 条年份编造")
    else:
        log.info(f"标准编造检测: {total} 条引用，无编造")
    return result

def scan_blind(md_files: list[Path], is_blind: bool, log: logging.Logger) -> list[dict]:
    """扫描章节文件中的暗标禁用信息（薄封装：委托 bid_common + 日志）

    仅在 is_blind=True 时执行；非暗标项目公司名合法，扫描会产生误报。
    """
    if not is_blind:
        log.info("暗标扫描 — 非暗标项目，已跳过")
        return []
    hits = scan_blind_bid_violations(md_files)
    if hits:
        log.warning(f"暗标扫描命中 {len(hits)} 处")
    else:
        log.info("暗标扫描 ✓ 无禁用信息")
    return hits


def check_response(rules_text: str, all_md_text: str, log: logging.Logger) -> list[dict]:
    """检查关键响应词是否在章节中出现"""
    results = []
    for label, pat, required in RESPONSE_RULES:
        m_rule = re.search(pat, rules_text)
        if not m_rule:
            continue
        keyword = m_rule.group(1)
        present = keyword in all_md_text
        results.append({
            "label": label, "keyword": keyword,
            "required": required, "present": present,
        })
        status = "✓" if present else ("✗" if required else "⚠")
        log.info(f"  [{status}] {label}: 规则要求 '{keyword}' — {'已响应' if present else '未出现'}")
    return results


def count_words(md_files: list[Path]) -> dict[str, int]:
    """统计各章节汉字字数（委托 bid_common.count_chinese_chars，与 reviewer 同口径）"""
    stats = {}
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        stats[md.name] = count_chinese_chars(text)
    return stats


def parse_must_include_sections(rules_text: str) -> dict:
    """从 disqualification_rules.md 解析 must_include_sections yaml 块（regex 实现，零依赖）。

    返回 {"source": str, "enforcement": str, "sections": [str, ...], "required_attachments": [str, ...]}.
    若字段不存在或解析不到返回各字段默认值（空字符串/空数组）。
    """
    result = {"source": "", "enforcement": "", "sections": [], "required_attachments": []}

    # 找到 must_include_sections: 起点（行首允许有空白，兼容嵌入 yaml fence）
    m_start = re.search(r"^\s*must_include_sections\s*:\s*$", rules_text, re.M)
    if not m_start:
        return result
    start_pos = m_start.end()

    # 截取后续缩进块（缩进 >= 2 空格的连续行；遇到顶级 key 或 fence 结束）
    tail = rules_text[start_pos:]
    block_lines: list[str] = []
    for line in tail.splitlines():
        if not line.strip():
            block_lines.append(line)
            continue
        if line.startswith("```"):
            break
        # 顶级 key（无前导空白且含冒号）→ 块结束
        if re.match(r"^[A-Za-z_]\w*\s*:", line):
            break
        block_lines.append(line)
    block = "\n".join(block_lines)

    # 标量字段
    m_src = re.search(r'^\s*source\s*:\s*"?(.*?)"?\s*$', block, re.M)
    if m_src:
        result["source"] = m_src.group(1).strip()
    m_enf = re.search(r'^\s*enforcement\s*:\s*"?(.*?)"?\s*$', block, re.M)
    if m_enf:
        result["enforcement"] = m_enf.group(1).strip()

    # 列表字段：sections / required_attachments
    def collect_list(key: str) -> list[str]:
        items: list[str] = []
        m_k = re.search(rf"^\s*{key}\s*:\s*$", block, re.M)
        if not m_k:
            # 也兼容 inline 写法 sections: []
            m_inline = re.search(rf"^\s*{key}\s*:\s*\[(.*?)\]\s*$", block, re.M)
            if m_inline:
                raw = m_inline.group(1).strip()
                if raw:
                    items = [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()]
            return items
        sub_start = m_k.end()
        for line in block[sub_start:].splitlines():
            if not line.strip():
                continue
            m_item = re.match(r"^\s+-\s+(.+?)\s*$", line)
            if m_item:
                items.append(m_item.group(1).strip().strip('"').strip("'"))
                continue
            # 同级或更外层的 key → 列表结束
            if re.match(r"^\s*[A-Za-z_]\w*\s*:", line):
                break
        return items

    result["sections"] = collect_list("sections")
    result["required_attachments"] = collect_list("required_attachments")
    return result


def check_chart_coverage(
    extracted: Path, chapter_dir: Path, md_files: list[Path],
    log: logging.Logger,
) -> dict:
    """检查 chart_specs.json 中 required=true 的图是否在章节中落地。

    每个 spec 命中规则(任一即视为有图):
      1. 章节 .md 含 ```json``` 块且 type 字段命中 spec.type(或 classify_json 推回该 type)
      2. 章节 .md 含 ![](../charts/...png) 引用
      3. charts/ 目录有 `<chapter>_<...>_<type>.png` 文件

    返回 {
      "loaded": bool, "total_required": int, "satisfied": int,
      "missing": list[dict], "advisory": list[dict]
    }
    """
    out = {
        "loaded": False,
        "total_required": 0,
        "satisfied": 0,
        "missing": [],
        "advisory": [],
    }
    spec_path = extracted / "chart_specs.json"
    if not spec_path.exists():
        log.info("chart_specs.json 不存在 — 跳过图表覆盖检查(向后兼容)")
        return out
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"chart_specs.json 解析失败: {e}")
        return out

    out["loaded"] = True
    specs = payload.get("specs", [])
    if not specs:
        log.info("chart_specs.json 为空 — 跳过图表覆盖检查")
        return out

    # 索引 md_files 按 chapter num
    md_by_chapter: dict[str, Path] = {}
    for md in md_files:
        m = re.match(r"^(\d{1,3})[_-]", md.name)
        if m:
            md_by_chapter[m.group(1).zfill(2)] = md

    charts_dir = chapter_dir / "charts"
    chart_pngs = list(charts_dir.glob("*.png")) if charts_dir.exists() else []

    # 延迟导入分类器
    classify = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from bid_charts import classify_json
        classify = classify_json
    except ImportError:
        pass

    fence_re = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

    for spec in specs:
        ch = str(spec.get("chapter", "")).zfill(2)
        ctype = spec.get("type", "")
        required = bool(spec.get("required", False))
        section = spec.get("section", "")
        hint = spec.get("data_hint", "")

        present = False
        evidence = ""

        # 1. 检查 md 中的 ```json``` 块
        md = md_by_chapter.get(ch)
        if md and md.exists():
            text = md.read_text(encoding="utf-8")
            for blk in fence_re.findall(text):
                try:
                    data = json.loads(blk)
                except Exception:
                    continue
                if isinstance(data, dict):
                    declared = str(data.get("type", "")).lower()
                    if ctype in declared or (classify and classify(data) == ctype):
                        present = True
                        evidence = f"md JSON 块 type={declared or ctype}"
                        break

            # 2. 检查图片引用
            if not present and re.search(rf"!\[[^\]]*\]\([^)]*charts/[^)]*\.png", text):
                present = True
                evidence = "md 含 charts/ PNG 引用"

        # 3. 检查 charts/ 目录有匹配 PNG
        if not present:
            for png in chart_pngs:
                if png.stem.startswith(f"{ch}_") and ctype in png.stem:
                    present = True
                    evidence = f"PNG: {png.name}"
                    break

        # 4. required 图全变体兜底:chart_specs 用基准章号,变体章节数≠基准时
        #    指定章号可能不存在,招标硬性图只要全文任一章命中即视为已落地。
        if not present and required:
            for any_md in md_files:
                try:
                    atext = any_md.read_text(encoding="utf-8")
                except Exception:
                    continue
                for blk in fence_re.findall(atext):
                    try:
                        data = json.loads(blk)
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        declared = str(data.get("type", "")).lower()
                        if ctype in declared or (classify and classify(data) == ctype):
                            present = True
                            evidence = f"全文扫描命中 {any_md.name} type={declared or ctype}"
                            break
                if present:
                    break

        item = {
            "chapter": ch,
            "section": section,
            "type": ctype,
            "required": required,
            "data_hint": hint,
            "present": present,
            "evidence": evidence,
        }
        if required:
            out["total_required"] += 1
            if present:
                out["satisfied"] += 1
                log.info(f"  ✓ 图 [{ch}.{section} {ctype}] 已落地: {evidence}")
            else:
                out["missing"].append(item)
                log.warning(f"  ✗ 图 [{ch}.{section} {ctype}] ★招标硬性要求 — 未在章节中找到")
        else:
            out["advisory"].append(item)

    log.info(f"图表覆盖: {out['satisfied']}/{out['total_required']} 张 required 图已落地;"
             f" {sum(1 for a in out['advisory'] if a['present'])}/{len(out['advisory'])} 张 advisory 已落地")
    return out


def check_outline_coverage(
    must: dict, md_files: list[Path], log: logging.Logger,
) -> dict:
    """检查 output 章节文件是否覆盖 must_include_sections.sections。

    返回 {"sections": [...], "covered": [...], "missing": [...], "attachments_referenced": {...}}.
    匹配口径：章节文件的一级标题 (#) + 二级标题 (##)，与 sections 做 topic_covered 语义匹配。
    """
    sections = must.get("sections", [])
    attachments = must.get("required_attachments", [])
    out = {
        "sections": sections,
        "covered": [],
        "missing": [],
        "attachments_referenced": {},
    }
    if not sections and not attachments:
        log.info("must_include_sections 为空，跳过章节锚点核查")
        return out

    # 收集所有章节标题（一级 + 二级）
    titles: list[str] = []
    full_text = ""
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        full_text += "\n" + text
        for m in re.finditer(r"^(#{1,2})\s+(.+?)\s*$", text, re.M):
            titles.append(m.group(2).strip())

    log.info(f"章节锚点核查 — 大纲主题 {len(sections)} 项，附表 {len(attachments)} 项")
    for topic in sections:
        if topic_covered(topic, titles):
            out["covered"].append(topic)
        else:
            out["missing"].append(topic)
    if out["missing"]:
        log.warning(f"  缺失大纲主题 {len(out['missing'])} 项: {out['missing']}")
    else:
        log.info("  ✓ 大纲主题全部命中")

    # 附表只做软核查（出现于正文任意位置即算命中）
    for att in attachments:
        # 取关键词前 4 个汉字做包含搜索
        key_chars = "".join(c for c in att if "一" <= c <= "鿿")[:4]
        if key_chars and key_chars in full_text:
            out["attachments_referenced"][att] = True
        else:
            out["attachments_referenced"][att] = False
            log.warning(f"  附表未在章节中引用: {att}")
    return out


def check_plan_alignment(
    plan_rows: list[dict], md_files: list[Path],
    word_stats: dict[str, int], log: logging.Logger,
) -> list[dict]:
    """核查实际章节文件与 chapter_plan 的对齐情况。

    逐项检查：文件是否存在、一级标题是否与 plan 标题一致、字数是否达标。
    """
    md_map = {md.name: md for md in md_files}
    results = []
    for row in plan_rows:
        fname = row["filename"]
        md = md_map.get(fname)
        item = {
            "num": row["num"],
            "expected_filename": fname,
            "expected_title": row["title"],
            "score": row["score"],
            "target_chars": row["target_chars"],
            "file_exists": md is not None,
            "title_match": False,
            "actual_title": "",
            "actual_chars": word_stats.get(fname, 0),
            "chars_meet_target": False,
        }
        if md is not None:
            text = md.read_text(encoding="utf-8")
            m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
            if m:
                item["actual_title"] = m.group(1).strip()
                # 字面一致判断：去掉空白后比较
                norm_a = re.sub(r"\s+", "", item["actual_title"])
                norm_b = re.sub(r"\s+", "", row["title"])
                item["title_match"] = (norm_a == norm_b)
            # 字数下限：共享 bid_common.chapter_chars_floor（圈层理论 ±15%）
            threshold = chapter_chars_floor(row["target_chars"])
            item["chars_threshold"] = threshold
            item["chars_meet_target"] = (
                item["actual_chars"] >= threshold if row["target_chars"] else True
            )
        results.append(item)

        if not item["file_exists"]:
            log.error(f"  ✗ 章节 {row['num']} 缺失：{fname}")
        elif not item["title_match"]:
            log.warning(
                f"  ⚠ 章节 {row['num']} 标题不一致：期望「{row['title']}」实际「{item['actual_title']}」"
            )
        elif not item["chars_meet_target"]:
            log.warning(
                f"  ⚠ 章节 {row['num']} 字数不足：{item['actual_chars']} < {item['chars_threshold']}"
                f"（目标 {row['target_chars']}，下限 85%）"
            )
        else:
            log.info(f"  ✓ 章节 {row['num']} 对齐 ({item['actual_chars']}字)")
    # 多余章节（不在 plan 中）
    planned = {r["filename"] for r in plan_rows}
    extras = [md.name for md in md_files if md.name not in planned]
    if extras:
        log.warning(f"发现计划外章节文件: {extras}")
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="投标文件合规扫描")
    parser.add_argument("tender_dir", help="投标根目录")
    parser.add_argument("--variant", help="仅扫描指定 variant 子目录（如 V01）")
    parser.add_argument("--all-variants", action="store_true",
                        help="批量扫描 output/V*/ 所有 variant")
    args = parser.parse_args()

    root = Path(args.tender_dir).resolve()
    if not root.exists():
        print(f"目录不存在: {root}")
        sys.exit(1)

    extracted = root / "extracted"
    output = root / "output"
    if not output.exists():
        print(f"output 目录不存在: {output}")
        sys.exit(1)

    # ── 批量模式：对所有 variant 独立扫描 ──
    if args.all_variants:
        variant_dirs = sorted([d for d in output.iterdir()
                                if d.is_dir() and re.match(r"^V\d+$", d.name)])
        if not variant_dirs:
            print("未发现 V01/ V02/ ... 子目录")
            sys.exit(2)
        failed = []
        for v_dir in variant_dirs:
            print(f"\n━━━ 合规扫描 {v_dir.name} ━━━")
            code = _run_single(root, v_dir, v_dir.name)
            if code != 0:
                failed.append(v_dir.name)
        print(f"\n汇总：{len(variant_dirs) - len(failed)}/{len(variant_dirs)} 通过")
        sys.exit(0 if not failed else 1)

    # ── 单 variant 模式 ──
    if args.variant:
        chapter_dir = output / args.variant
        if not chapter_dir.is_dir():
            print(f"variant 目录不存在: {chapter_dir}")
            sys.exit(2)
        sys.exit(_run_single(root, chapter_dir, args.variant))

    # ── 默认：扫描 output/ 根目录（单份模式）──
    sys.exit(_run_single(root, output, ""))


def _run_single(root: Path, chapter_dir: Path, variant_id: str) -> int:
    """对某个章节目录做合规扫描。chapter_dir 是章节 md 所在位置。"""
    extracted = root / "extracted"
    # 报告固定写到 chapter_dir（单份=output/、批量=output/VXX/）
    output = chapter_dir

    (output / "logs").mkdir(exist_ok=True)
    (output / "reports").mkdir(exist_ok=True)
    log = make_script_logger("bid_compliance", output / "logs" / "compliance.log")
    log.info("=" * 60)
    log.info(f"bid_compliance 启动  章节目录: {chapter_dir}  variant={variant_id or '单份'}")
    log.info("=" * 60)

    rules_path = extracted / "disqualification_rules.md"
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    if not rules_text:
        log.warning("disqualification_rules.md 不存在或为空，响应词检查将跳过")

    md_files = collect_chapter_files(chapter_dir)
    if not md_files:
        log.error("未找到章节 .md 文件")
        return 1
    log.info(f"扫描章节文件 {len(md_files)} 个")

    all_text = "\n".join(md.read_text(encoding="utf-8") for md in md_files)

    is_blind = needs_blind_scan(extracted)
    log.info(f"暗标判定: is_blind={is_blind}  (来源：bid_common.needs_blind_scan)")
    blind_hits = scan_blind(md_files, is_blind, log)
    log.info("关键响应词检查:")
    responses = check_response(rules_text, all_text, log)
    word_stats = count_words(md_files)

    log.info("章节字数统计:")
    for fname, cn in word_stats.items():
        log.info(f"  {fname}: {cn:,} 汉字")
    log.info(f"  合计: {sum(word_stats.values()):,} 汉字")

    log.info("章节-评分表对齐核查:")
    plan_path = extracted / "chapter_plan.md"
    v_plan = chapter_dir / "chapter_plan_variant.md"
    if variant_id and v_plan.exists() and parse_chapter_plan(v_plan):
        plan_path = v_plan
        log.info(f"批量模式:改用变体计划 {v_plan.name}(结构独立)")
    plan_rows = parse_chapter_plan(plan_path)
    if not plan_rows and not plan_path.exists():
        log.warning(f"chapter_plan.md 不存在: {plan_path}")
    else:
        log.info(f"chapter_plan 解析到 {len(plan_rows)} 章")
    alignment = check_plan_alignment(plan_rows, md_files, word_stats, log) if plan_rows else []
    planned_names = {r["filename"] for r in plan_rows}
    extras = [md.name for md in md_files if md.name not in planned_names] if plan_rows else []

    log.info("章节锚点-施组大纲覆盖核查:")
    must = parse_must_include_sections(rules_text)
    outline_coverage = check_outline_coverage(must, md_files, log)

    log.info("图表覆盖核查(chart_specs.json):")
    chart_coverage = check_chart_coverage(extracted, chapter_dir, md_files, log)

    log.info("DOCX 格式实测层审计:")
    format_audit_result = _run_format_audit(root, output, log)

    log.info("标准引用编造检测:")
    config_dir = Path(__file__).resolve().parent.parent / "configs"
    truth = load_standards_truth(config_dir)
    std_result = check_standards_fabrication(md_files, truth, log)

    # 生成机读报告（JSON，供 Claude 读取）+ 人读报告（MD）
    reports_dir = output / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_json = reports_dir / "compliance_scan.json"
    report_json.write_text(json.dumps({
        "is_blind": is_blind,
        "blind_violations": blind_hits,
        "response_check": responses,
        "word_stats": word_stats,
        "total_cn_chars": sum(word_stats.values()),
        "plan_alignment": alignment,
        "extra_files": extras,
        "plan_loaded": bool(plan_rows),
        "outline_coverage": outline_coverage,
        "must_include_sections": must,
        "chart_coverage": chart_coverage,
        "format_audit": format_audit_result,
        "standards_check": std_result,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"机读报告: {report_json}")

    md_lines = [
        "# 自动合规扫描结果",
        "",
        "> 本文件由 `bid_compliance.py` 自动生成，供 Claude 做废标合规检查时参考。",
        "",
        f"**项目类型**：{'暗标' if is_blind else '非暗标'}（判定来源：bid_common.needs_blind_scan）",
        "",
        "## 1. 暗标禁用信息扫描",
        "",
    ]
    if not is_blind:
        md_lines.append("⏭ 非暗标项目，已跳过（公司名/签章等信息合法，扫描不适用）")
    elif blind_hits:
        md_lines.append(f"❌ 命中 {len(blind_hits)} 处，请逐条核查：")
        md_lines.append("")
        md_lines.append("| 文件 | 行 | 类别 | 命中 | 上下文 |")
        md_lines.append("|------|----|------|------|--------|")
        for h in blind_hits:
            md_lines.append(f"| {h['file']} | {h['line']} | {h['kind']} | `{h['match']}` | …{h['context']}… |")
    else:
        md_lines.append("✓ 未发现禁用信息")
    md_lines.append("")

    md_lines.append("## 2. 关键响应词检查")
    md_lines.append("")
    md_lines.append("| 项 | 规则关键词 | 是否响应 | 强制 |")
    md_lines.append("|----|-----------|---------|------|")
    for r in responses:
        md_lines.append(f"| {r['label']} | `{r['keyword']}` | {'✓' if r['present'] else '✗'} | {'是' if r['required'] else '否'} |")
    md_lines.append("")

    md_lines.append("## 3. 章节字数")
    md_lines.append("")
    md_lines.append("| 章节 | 汉字数 |")
    md_lines.append("|------|--------|")
    for fname, cn in word_stats.items():
        md_lines.append(f"| {fname} | {cn:,} |")
    md_lines.append(f"| **合计** | **{sum(word_stats.values()):,}** |")
    md_lines.append("")

    md_lines.append("## 4. 章节-评分表对齐核查")
    md_lines.append("")
    if not plan_rows:
        md_lines.append("⚠ 未找到 `extracted/chapter_plan.md`，跳过对齐核查。请在 Step 2 生成该文件。")
    else:
        md_lines.append("| 章 | 期望文件 | 期望标题 | 分值 | 实际标题 | 文件存在 | 标题一致 | 实际字数/目标 | 达标 |")
        md_lines.append("|----|---------|---------|------|---------|---------|---------|---------------|------|")
        for a in alignment:
            md_lines.append(
                f"| {a['num']} | {a['expected_filename']} | {a['expected_title']} | {a['score']} | "
                f"{a['actual_title'] or '—'} | {'✓' if a['file_exists'] else '✗'} | "
                f"{'✓' if a['title_match'] else '✗'} | "
                f"{a['actual_chars']}/{a['target_chars']} | "
                f"{'✓' if a['chars_meet_target'] else '✗'} |"
            )
        if extras:
            md_lines.append("")
            md_lines.append(f"⚠ 计划外的章节文件：{', '.join(extras)}")
    md_lines.append("")

    md_lines.append("## 5. 章节锚点-施组编制大纲覆盖核查")
    md_lines.append("")
    if not must.get("sections") and not must.get("required_attachments"):
        md_lines.append("⏭ `disqualification_rules.md` 未声明 `must_include_sections.sections`，跳过此核查。")
        md_lines.append("（合格制项目 / 招标文件含施组大纲时建议在 brain.md 第七节同步该字段。）")
    else:
        if must.get("source"):
            md_lines.append(f"**来源**：{must['source']}")
            md_lines.append("")
        if must.get("sections"):
            covered = outline_coverage["covered"]
            missing = outline_coverage["missing"]
            md_lines.append(f"大纲一级主题 {len(must['sections'])} 项，已覆盖 {len(covered)}，缺失 {len(missing)}")
            md_lines.append("")
            md_lines.append("| 主题 | 状态 |")
            md_lines.append("|------|------|")
            for topic in must["sections"]:
                ok = topic in covered
                md_lines.append(f"| {topic} | {'✓ 已覆盖' if ok else '✗ 缺失'} |")
            md_lines.append("")
        if must.get("required_attachments"):
            md_lines.append("### 附图附表引用核查（关键词搜索口径，仅供参考）")
            md_lines.append("")
            md_lines.append("| 附表名 | 是否在章节正文中提及 |")
            md_lines.append("|--------|-------------------|")
            for att, hit in outline_coverage["attachments_referenced"].items():
                md_lines.append(f"| {att} | {'✓' if hit else '✗'} |")
    md_lines.append("")

    report_md = reports_dir / "compliance_scan.md"
    report_md.write_text("\n".join(md_lines), encoding="utf-8")
    log.info(f"人读报告: {report_md}")

    # 退出码：暗标违规 / 强制响应未命中 / 章节缺失或标题不一致 / 格式硬违规 → 1
    has_align_violation = any(
        (not a["file_exists"]) or (not a["title_match"])
        for a in alignment
    )
    has_chart_violation = chart_coverage.get("loaded") and bool(chart_coverage.get("missing"))
    has_format_severe = format_audit_result.get("exit_code", 0) >= 2
    has_std_violation = std_result.get("fabricated", 0) > 0
    has_violation = (
        bool(blind_hits)
        or any(r["required"] and not r["present"] for r in responses)
        or has_align_violation
        or has_chart_violation
        or has_format_severe
        or has_std_violation
    )
    log.info("=" * 60)
    if has_violation:
        if has_chart_violation:
            n_miss = len(chart_coverage["missing"])
            log.error(f"招标硬性要求的 {n_miss} 张图未落地:")
            for m in chart_coverage["missing"]:
                log.error(f"    - 章 {m['chapter']}.{m['section']} type={m['type']} ({m['data_hint']})")
        if has_format_severe:
            n_v = len(format_audit_result.get("violations", []))
            n_sv = len(format_audit_result.get("style_violations", []))
            log.error(f"DOCX 格式实测层硬违规：{n_v} 段落字号/字体偏离 + {n_sv} 样式偏离")
        log.error("存在合规风险，退出码 1")
        sys.exit(1)
    log.info("合规扫描通过 ✓")
    return 0


def _run_format_audit(root, chapter_dir, log):
    """调用 bid_format_audit 实测 DOCX；缺 DOCX 时返回 skipped。"""
    # 单份模式 chapter_dir=output/(docx 同目录);批量模式 chapter_dir=output/V01/(docx 在父级)
    candidates = sorted(chapter_dir.glob("*_施工组织设计.docx"))
    if not candidates and chapter_dir.name.startswith("V"):
        # 批量模式:docx 在 output/ 顶层,文件名含 variant id
        candidates = sorted(chapter_dir.parent.glob(f"{chapter_dir.name}_*_施工组织设计.docx"))
    if not candidates:
        log.info("  跳过：未找到 *_施工组织设计.docx（先跑 bid_assemble）")
        return {"skipped": True, "reason": "no docx"}
    try:
        import bid_format_audit
        code, result = bid_format_audit.audit(root, candidates[0])
        v = len(result.get("violations", []))
        sv = len(result.get("style_violations", []))
        log.info(f"  实测违规 {v} 段落 + {sv} 样式  退出码 {code}")
        if v or sv:
            for vio in result.get("violations", [])[:5]:
                log.warning(f"  - [{vio.get('region','?')}] 段{vio['para_idx']} 「{vio['snippet']}」"
                            f" 实际 {vio.get('actual')} 期望 {vio.get('expected')}")
        return result
    except Exception as e:
        log.warning(f"  格式审计调用失败：{e}")
        return {"error": str(e), "skipped": True}


if __name__ == "__main__":
    main()
