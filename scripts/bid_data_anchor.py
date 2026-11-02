#!/usr/bin/env python3
"""
bid_data_anchor.py - 生成统一数据锚点(unified_data_anchor.md)

读 facts / derived / boq,产出 extracted/unified_data_anchor.md,供 bid_writer 在
Block3(本章任务)注入,强制全文各章硬数据一致、禁止 LLM 编造。

历史:该锚点原为手工 hook —— Step2 确认 chapter_plan 后由人工(或 Agent)手写
extracted/unified_data_anchor.md,没人写就靠 LLM 自觉不漂移。现改为自动合成,
bid_run 在 write 前自动生成,单跑 bid_writer 前也可手动调本脚本。

用法:
  python bid_data_anchor.py <tender_dir>
  python bid_data_anchor.py <tender_dir> --force   # 覆盖已有
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


def _load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _as_dict(v) -> dict:
    """旧提取器形状(裸数组/None) → {}，避免后续 .get 抛 AttributeError。"""
    return v if isinstance(v, dict) else {}


def _val(facts: dict, key: str):
    v = facts.get(key)
    if isinstance(v, dict):
        return v.get("value")
    return v


# 派生枚举 → 中文标签(锚点注入 LLM prompt,用中文更自然)
_ALT_CLASS_ZH = {
    "low": "平原", "middle": "中海拔", "high": "高海拔",
    "very_high": "极高海拔(须高原专项)", "extreme": "极高海拔4000+(强制氧气/医疗保障)",
    "unknown": "未知",
}
_SIZE_CLASS_ZH = {
    "small": "极小型", "small_medium": "中小型", "medium": "中型",
    "large": "大型", "extra_large": "特大型", "unknown": "未知",
}


def _fmt_price(cp) -> str:
    """控制价格式化为 'x,xxx,xxx.xx 元（约 xxx.xx 万元）'。"""
    try:
        f = float(cp)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return f"{f:,.2f} 元（约 {f/10000:,.2f} 万元）"


_MANUAL_BEGIN = "<!-- MANUAL:BEGIN -->"
_MANUAL_END = "<!-- MANUAL:END -->"


def _extract_manual_block(path: Path) -> str:
    """取回上一份锚点文件里人工补充的内容(MANUAL 标记之间)。

    bid_run 每次 write 前 `bid_data_anchor.py --force` 重生成本文件 → 不保留人工段
    就会把 Agent/人工补的硬数据静默覆盖。
    """
    try:
        text = Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return ""
    m = re.search(re.escape(_MANUAL_BEGIN) + r"(.*?)" + re.escape(_MANUAL_END),
                  text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _boq_brief(boq: dict | None, max_categories: int = 6,
               max_items_per: int = 8) -> list[str]:
    """把 boq.json 压成「类目: item qty unit; ...」的紧凑行列表。"""
    # 旧提取器把 boq.json 写成裸数组(无 categories 外壳) → 不能直接 .get
    if not isinstance(boq, dict) or not boq.get("categories"):
        return []
    out = []
    for cat in boq["categories"][:max_categories]:
        if not isinstance(cat, dict):
            continue
        name = (cat.get("name") or "").strip()
        items = cat.get("items") or []
        if not name or not items:
            continue
        briefs = []
        for it in items[:max_items_per]:
            if not isinstance(it, dict):
                continue
            it_name = (it.get("name") or "").strip()
            if not it_name:
                continue
            qty = it.get("qty")
            unit = it.get("unit") or ""
            # qty 缺失时旧写法渲染成字面量 "None"(「钢筋 Nonem³」)
            # 缺数量时明确标注,避免 LLM 把「钢筋 t」当成"数量已给出"照抄
            briefs.append(f"{it_name} {qty}{unit}".strip() if qty is not None
                          else f"{it_name}（数量未提供）")
        if briefs:
            out.append(f"- {name}：{'；'.join(briefs)}")
    return out


def build_data_anchor(ext: Path, out_path: Path | None = None) -> str:
    """组装 unified_data_anchor.md 正文。缺失字段自动省略(不写 null)。"""
    facts = _as_dict(_load(ext / "project_facts.json"))
    facts = _as_dict(facts.get("facts", facts))
    derived = _as_dict(_load(ext / "project_derived.json"))
    boq = _load(ext / "boq.json")

    name = _val(facts, "project_name")
    location = _val(facts, "location")
    cp = _fmt_price(_val(facts, "control_price"))
    duration = _val(facts, "duration_days")
    quality = _val(facts, "quality_target")
    qualification = _val(facts, "qualification")

    start = derived.get("schedule_start_date")
    end = derived.get("schedule_end_date")
    _sc = _as_dict(derived.get("schedule_constraints"))
    earliest = _sc.get("earliest_start_date")
    min_rule = _sc.get("min_start_rule")
    altitude = derived.get("altitude_m")
    alt_class = derived.get("altitude_class")
    seasons = derived.get("season_set") or []
    winter = derived.get("spans_winter")
    size = derived.get("size_class")

    lines = [
        "> ★ 统一数据锚点：以下为本项目全文各章必须一致使用的硬数据。",
        "> 任何章节出现与下列数字/表述不一致的内容，均视为编造，必须改正。",
        "",
    ]

    body: list[str] = []
    if name:
        body.append(f"- 项目名称：{name}")
    if location:
        body.append(f"- 建设地点：{location}")
    if cp:
        body.append(f"- 招标控制价：{cp}")
    if duration:
        body.append(f"- 总工期：{duration} 日历天")
    if start or end:
        # ★ 防“锚点日期被吸入正文”陷阱:锚点会注入每章 prompt,而 compliance 把正文
        # 散文里的日历日期判为日期泄漏(只允许出现在进度计划章的 json 数据块内)。
        # 地点即规则:把日期放在“JSON 专用”小标题下,避免 LLM 当成正文可引用事实。
        body.append("- **进度计划 JSON 基准（★ 仅供进度计划章的 ```json 数据块使用）**：")
        if start:
            rule = f"，{min_rule}" if min_rule else ""
            body.append(f"  - 计划开工：{start}{rule}")
        if end:
            body.append(f"  - 计划竣工：{end}")
        body.append("  - 正文严禁出现上述具体日期（含“开工日期为 X 日”类表述），一律用"
                    "「总工期 N 日历天 / 开工后第 N 天 / 第 N 阶段」相对表达；"
                    "正文出现任何日历日期都会触发合规门禁并导致章节重写。")
    if quality:
        body.append(f"- 质量目标：{quality}")
    if qualification:
        body.append(f"- 资质要求：{qualification}")
    if altitude:
        cls = _ALT_CLASS_ZH.get(alt_class, alt_class or "")
        body.append(f"- 海拔：{altitude} m（{cls}）" if cls else f"- 海拔：{altitude} m")
    if seasons:
        winter_note = "，跨冬施工" if winter else ""
        body.append(f"- 有效施工季节：{'、'.join(seasons)}{winter_note}")
    if size and size != "unknown":
        body.append(f"- 项目规模：{_SIZE_CLASS_ZH.get(size, size)}")

    # 工期绝对基准(供进度图/正文相对表达换算)
    if earliest and not start:
        body.append(f"- 计划开工基准（最早）：{earliest}（{min_rule or '投标截止后第5个工作日'}）")

    if body:
        lines.extend(body)

    boq_lines = _boq_brief(boq)
    if boq_lines:
        lines.append("")
        lines.append("**主要工程量（清单驱动，正文引用数字不得偏离）：**")
        lines.extend(boq_lines)

    # 全空兜底:仍产出锚点骨架,避免 writer 读到空文件
    if not body and not boq_lines:
        lines.append("（无可用事实/清单数据，请先确认 facts/boq/derived 已生成）")

    # ★ 人工补充段保留:bid_run 每次 write 前都会 --force 重生成本文件,
    # 若直接覆盖,人工补的硬数据会被静默丢弃。约定用 MANUAL 标记包住人工内容。
    manual = _extract_manual_block(out_path)
    if manual:
        lines.append("")
        lines.append(_MANUAL_BEGIN)
        lines.append(manual)
        lines.append(_MANUAL_END)

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--force", action="store_true", help="覆盖已有 unified_data_anchor.md")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_data_anchor", ext / "data_anchor.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_data_anchor  目录: {tender_dir}")

    out_path = ext / "unified_data_anchor.md"
    if out_path.exists() and not args.force:
        log.info(f"  {out_path.name} 已存在,跳过(--force 覆盖)")
        return 0

    content = build_data_anchor(ext, out_path)
    out_path.write_text(content, encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name} ({len(content)} 字符)")
    for ln in content.splitlines():
        if ln.startswith("- "):
            log.info(f"    {ln}")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
