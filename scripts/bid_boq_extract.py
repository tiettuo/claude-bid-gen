#!/usr/bin/env python3
"""
bid_boq_extract.py - 解析工程量清单(BoQ)

从 extracted/ 下的 *清单*.txt 文件解析分部分项工程项目,
输出 extracted/boq.json,供 brain_translate 第 2 节"工程构成"自动填表。

清单 txt 结构(bid_extract.py 从 xlsx 生成):
  【工作表: 表-09 分部分项工程项目清单计价表【土建部分】】
  表-09
  分部分项工程项目清单计价表
  ...
  序号 | 项目编码 | 项目名称 | 项目特征 | 计量单位 | 工程量 | ...
  1 | 010101001001 | 拆除女儿墙 | ... | m3 | 0.33 | ...

输出 boq.json 结构:
  {
    "categories": [
      {"name": "土建-拆除", "items": [{"name": "拆除女儿墙", "qty": 0.33, "unit": "m3"}, ...]},
      {"name": "土建-新建", "items": [...]},
      {"name": "安装-电气", "items": [...]}
    ],
    "_meta": {...}
  }

用法:
  python bid_boq_extract.py /path/to/tender/
  python bid_boq_extract.py /path/to/tender/ --force
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"

# 单位白名单(防止误把"项目特征"中的数字当工程量)
VALID_UNITS = {"m", "m2", "m3", "m²", "m³", "t", "kg", "套", "个", "组",
               "台", "件", "块", "根", "条", "项", "处", "次", "对", "支",
               "桶", "袋", "卷", "盘", "百米", "千米", "公里", "工日",
               "座", "部", "具", "系统", "樘", "扇"}


def parse_boq_file(txt_path: Path, log) -> list[dict]:
    """解析单个 BoQ txt 文件 → [{"category": ..., "items": [...]}]

    按"工作表: 表-09 xxx【YY部分】"分组(YY 是专业类目如"土建部分""安装工程")。
    """
    text = txt_path.read_text(encoding="utf-8")
    # 切割工作表
    sheet_pattern = re.compile(r"【工作表:\s*([^】]+)】")
    parts = sheet_pattern.split(text)
    # parts = [前言, 工作表名1, 表内容1, 工作表名2, 表内容2, ...]

    results: list[dict] = []
    cur_section_name = None  # 如"土建部分""安装工程"

    for i in range(1, len(parts), 2):
        sheet_name = parts[i]
        sheet_content = parts[i + 1] if i + 1 < len(parts) else ""
        # 识别专业类目(从工作表名 / 表头里抽"【XX部分】"或"XX工程")
        # 注意:sheet_name 被外层正则切割,末尾的 】 已被消耗,所以 sub regex 不要求结尾 】
        m = re.search(
            r"【([一-鿿]{2,10}部分)|【([一-鿿]{2,10}工程)|"
            r"工程名称[:：]\s*([^\s|]+)",
            sheet_name,
        )
        if m:
            cur_section_name = m.group(1) or m.group(2) or m.group(3)

        # 只解析"分部分项工程项目清单"表(表-09 / 类似)
        if "分部分项工程项目清单" not in sheet_name and "分部分项工程" not in sheet_name:
            continue

        items = _parse_table_items(sheet_content)
        if not items:
            continue

        # 按"A 拆除部分 / B 新建部分 / C 安装部分"等小节再分组
        for sub_name, sub_items in _split_by_subsection(items, cur_section_name).items():
            results.append({
                "category": f"{cur_section_name or '未分类'}-{sub_name}" if sub_name else (cur_section_name or "未分类"),
                "items": sub_items,
            })

    if results:
        return results

    # PDF/OCR 转出的清单常常没有【工作表】标记，而是 HTML table / Markdown table
    # 直接按全文逐行解析，保持工程名称【专业】上下文。
    return _parse_loose_boq_text(text)


_UNITS_RE = "|".join(re.escape(u) for u in sorted(VALID_UNITS, key=len, reverse=True))
# 匹配 `序号 | 项目编码 | 名称 | [项目特征]` 起始行
_ITEM_START_RE = re.compile(
    r"(?:^|\n)(?P<idx>\d+)\s*\|\s*(?P<code>\d{10,12}(?:\.\d+)?)\s*\|\s*(?P<name>[^\n|]+?)\s*\|",
    re.MULTILINE,
)
# 匹配条目块尾部的 `单位 | 数量` 列(数量为正数)
_QTY_RE = re.compile(rf"\|\s*({_UNITS_RE})\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|")


def _parse_table_items(text: str) -> list[dict]:
    """从分部分项工程清单提取工程量条目。

    条目结构(铜溪/西藏项目通用):
      `1 | 011601001001 | 拆除女儿墙 | [项目特征]\\n...多行...\\n| m3 | 0.33 | 单价 | ...`

    解析策略:用 ITEM_START 正则定位每条起始,然后在起始到下一条之间找 unit+qty。
    """
    items = []
    matches = list(_ITEM_START_RE.finditer(text))
    for i, m in enumerate(matches):
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), block_start + 2000)
        block = text[block_start:block_end]

        name = m.group("name").strip()
        # 跳过表头行
        if name in ("项目名称", "") or len(name) > 60:
            continue

        # 找第一个 unit+qty 命中(后续可能还有单价但不需要)
        qm = _QTY_RE.search(block)
        if not qm:
            continue
        unit = qm.group(1)
        try:
            qty = float(qm.group(2))
        except ValueError:
            continue
        if qty <= 0:
            continue

        items.append({
            "name": name,
            "qty": qty,
            "unit": unit,
            "code": m.group("code"),
        })
    return items


def _strip_cell(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\r\n|")


def _parse_html_rows(line: str) -> list[list[str]]:
    rows = []
    for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", line, flags=re.I | re.S):
        row_html = rm.group(1)
        cells = [
            _strip_cell(cm.group(1))
            for cm in re.finditer(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def _parse_markdown_row(line: str) -> list[str] | None:
    if "|" not in line:
        return None
    raw = line.strip()
    if not raw.startswith("|"):
        return None
    cells = [_strip_cell(c) for c in raw.strip("|").split("|")]
    if len(cells) < 4:
        return None
    if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
        return None
    return cells


def _number(s: str) -> float | None:
    s = (s or "").replace(",", "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _category_from_heading(line: str) -> str | None:
    m = re.search(r"工程名称[:：].*?【([^】]{2,20})】", line)
    if m:
        return _strip_cell(m.group(1))
    return None


def _item_from_cells(cells: list[str], current_section: str | None) -> tuple[dict | None, str | None]:
    """解析一行清单。返回 (item, new_subsection)。"""
    # 分部标题行: ['', '0101', '土石方工程', ...] 或 ['1.1', '绿化工程', ...]
    if re.fullmatch(r"\d{2,6}(?:\.\d+)?", cells[1] if len(cells) > 1 else ""):
        maybe_name = cells[2] if len(cells) > 2 else ""
        if maybe_name and not re.fullmatch(r"\d{10,12}", cells[1]) and not cells[0].isdigit():
            return None, maybe_name[:40]

    if len(cells) < 6:
        return None, None

    if not cells[0].isdigit():
        return None, None
    code = cells[1] if len(cells) > 1 else ""
    if not re.fullmatch(r"\d{9,12}(?:\d{3})?", code):
        return None, None
    name = cells[2] if len(cells) > 2 else ""
    unit = cells[4] if len(cells) > 4 else ""
    qty = _number(cells[5] if len(cells) > 5 else "")
    if not name or unit not in VALID_UNITS or qty is None or qty <= 0:
        return None, None

    return {
        "name": name[:80],
        "qty": qty,
        "unit": unit,
        "code": code,
        "section": current_section,
    }, None


def _parse_loose_boq_text(text: str) -> list[dict]:
    """解析无工作表标记的 HTML/Markdown 清单全文。"""
    grouped: dict[str, list[dict]] = {}
    current_section = "未分类"
    current_subsection = ""

    def add_item(item: dict) -> None:
        category = current_section
        if current_subsection and current_subsection not in category:
            category = f"{category}-{current_subsection}"
        item.pop("section", None)
        grouped.setdefault(category, []).append(item)

    for line in text.splitlines():
        heading = _category_from_heading(line)
        if heading:
            current_section = heading
            current_subsection = ""

        rows = _parse_html_rows(line)
        md_row = _parse_markdown_row(line)
        if md_row:
            rows.append(md_row)

        for cells in rows:
            item, subsection = _item_from_cells(cells, current_section)
            if subsection:
                current_subsection = subsection
            if item:
                add_item(item)

    return [
        {"category": cat, "items": items}
        for cat, items in grouped.items()
        if items
    ]


def _split_by_subsection(items: list[dict], section_name: str | None) -> dict[str, list[dict]]:
    """简单分组:全部归到一个"主体"组(可后续细化)。"""
    # 当前简化:不按子节分,所有 items 归一组
    return {"": items}


def _looks_like_boq_text(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:20000]
    except OSError:
        return False
    return any(k in sample for k in (
        "分部分项工程项目清单", "工程量清单", "项目编码", "项目名称 | 项目特征",
        "综合单价", "措施项目清单", "单位工程"
    ))


def collect_boq(ext: Path, log) -> dict:
    """扫 extracted/ 下清单文本,合并解析。

    文件名优先匹配 *清单* / *工程量*;若用户文件只叫"项目.xlsx",
    兜底扫描 txt 内容里的清单表头。
    """
    txt_files = (
        list(ext.glob("*清单*.txt"))
        + list(ext.glob("*BoQ*.txt"))
        + list(ext.glob("*工程量*.txt"))
    )
    if not txt_files:
        txt_files = [p for p in ext.glob("*.txt") if _looks_like_boq_text(p)]
    if not txt_files:
        log.warning(f"  {WARN} 未找到清单 txt 文件")
        return {"categories": [], "_meta": {"source_files": []}}

    log.info(f"  发现 {len(txt_files)} 个清单文件")
    all_categories: list[dict] = []
    for tf in txt_files:
        log.info(f"  解析: {tf.name}")
        cats = parse_boq_file(tf, log)
        all_categories.extend(cats)

    # 合并同类目
    merged: dict[str, list[dict]] = {}
    for c in all_categories:
        merged.setdefault(c["category"], []).extend(c["items"])

    total_items = sum(len(v) for v in merged.values())
    log.info(f"  共 {len(merged)} 个类目 / {total_items} 条工程量")
    for cat, items in merged.items():
        log.info(f"    [{cat}] {len(items)} 条")

    return {
        "categories": [{"name": k, "items": v} for k, v in merged.items()],
        "_meta": {
            "source_files": [tf.name for tf in txt_files],
            "extracted_by": "bid_boq_extract.py",
        },
    }


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

    out_path = ext / "boq.json"
    if out_path.exists() and not args.force:
        return 1

    log = make_script_logger("bid_boq_extract", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_boq_extract  目录: {tender_dir}")

    payload = collect_boq(ext, log)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name}")
    log.info("=" * 60)
    return 0 if payload["categories"] else 1


if __name__ == "__main__":
    sys.exit(main())
