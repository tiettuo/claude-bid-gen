#!/usr/bin/env python3
"""bid_format_audit.py — 实测层格式审计

打开 bid_assemble 生成的 DOCX，遍历每个 run / style 的字号、字体，对比
disqualification_rules.md 中解析出的硬性规则，输出违规清单。

设计目的：catch bid_assemble.py 里被绕过 fmt 直接硬编码的尺寸/字体
（典型如封面 26/18 这种"凭行业惯例硬编码"的违规）。

退出码：
  0 - 全部符合
  1 - 仅封面/标题区偏离（gray area，需人工确认）
  2 - 正文/表格区偏离（违反招标格式硬约束，废标风险）

用法：
  python bid_format_audit.py <tender_dir>
  python bid_format_audit.py <tender_dir> --docx output/xxx.docx
"""

import argparse
import json
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

# 复用 bid_assemble 的 fmt 解析（保证规则解析与组装一致）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bid_assemble  # noqa: E402


def _half_pt_to_pt(val: str) -> float:
    try:
        return int(val) / 2.0
    except (ValueError, TypeError):
        return 0.0


def _scan_runs(doc):
    """遍历 doc 全部 runs，返回 [(idx, style, text_snippet, size_pt, east_asia, ascii_font, in_table)]。"""
    rows = []
    body = doc.element.body
    para_idx = 0

    def _iter_paragraphs(container, in_table):
        nonlocal para_idx
        for p in container.paragraphs:
            style_name = p.style.name if p.style is not None else ""
            for r in p.runs:
                rPr = r._element.find(qn("w:rPr"))
                size_pt = None
                east_asia = None
                ascii_font = None
                if rPr is not None:
                    sz = rPr.find(qn("w:sz"))
                    if sz is not None:
                        size_pt = _half_pt_to_pt(sz.get(qn("w:val")))
                    rFonts = rPr.find(qn("w:rFonts"))
                    if rFonts is not None:
                        east_asia = rFonts.get(qn("w:eastAsia"))
                        ascii_font = rFonts.get(qn("w:ascii"))
                txt = (r.text or "").strip()
                snippet = txt[:30]
                rows.append({
                    "para_idx": para_idx,
                    "style": style_name,
                    "snippet": snippet,
                    "size_pt": size_pt,
                    "east_asia": east_asia,
                    "ascii": ascii_font,
                    "in_table": in_table,
                })
            para_idx += 1

    _iter_paragraphs(doc, in_table=False)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                _iter_paragraphs(cell, in_table=True)
    return rows


def _scan_styles(doc):
    """遍历样式表，返回 {style_name: (size_pt, east_asia)}"""
    out = {}
    for s in doc.styles:
        try:
            size_pt = float(s.font.size.pt) if s.font.size else None
        except Exception:
            size_pt = None
        east_asia = None
        try:
            rFonts = s.element.find(qn("w:rPr"))
            if rFonts is not None:
                rf = rFonts.find(qn("w:rFonts"))
                if rf is not None:
                    east_asia = rf.get(qn("w:eastAsia"))
        except Exception:
            pass
        out[s.name] = {"size_pt": size_pt, "east_asia": east_asia}
    return out


def _classify(row, first_heading_para_idx):
    """根据段落位置/样式归类 run 所属 region。"""
    style = row["style"] or ""
    if row["in_table"]:
        return "table"
    if first_heading_para_idx is not None and row["para_idx"] < first_heading_para_idx:
        return "cover_or_toc"
    if style.startswith("Heading 1"):
        return "h1"
    if style.startswith("Heading"):
        return "h2plus"
    if style == "Title":
        return "title_style"
    if style in ("TOC 1", "TOC 2", "TOC 3", "目录"):
        return "toc"
    return "body"


def _expected_size(region, fmt):
    """每个 region 期望的字号（None 表示不强制 / 跟随 body）。"""
    body = fmt.get("font_size_body")
    if region == "body":
        return body
    if region == "table":
        return fmt.get("font_size_table") or body
    if region == "h1":
        return fmt.get("font_size_h1") or body
    if region == "h2plus":
        return body
    if region == "title_style":
        return fmt.get("font_size_title_style") or body
    if region == "cover_or_toc":
        # 封面字号区间：[cover_meta, cover_title]，超出视为违规
        title = fmt.get("font_size_cover_title") or body
        meta = fmt.get("font_size_cover_meta") or body
        # C 版封面目录英文副标题(Contents)按设计比正文小 2pt(反串标差异化),
        # 区间下限放宽到 body-2,避免把设计内差异误判为违规。
        lo = min(meta, body, (body or 14) - 2)
        return ("range", lo, max(title, body))
    return None


def audit(tender_dir: Path, docx_path: Path):
    # Phase 3:优先 authoring_rules.md(方案规则),回退 disqualification_rules.md
    from bid_common import canonical_rules_path
    # 格式审计以 canonical(disqualification_rules.md)为唯一真源：
    # preferred( authoring_rules.md)的 format 块是嵌套 format: 结构,
    # parse_format_rules 解析不到 heading_h1_size_pt 等键 → h1 期望回退正文 12pt,
    # 与 assemble/verify 应用的 16pt 不一致,产生误报(实测历史: 12 段 H1 假违规)。
    rules_md = canonical_rules_path(tender_dir / "extracted")
    if not rules_md.exists():
        print(f"[error] 找不到 {rules_md}", file=sys.stderr)
        return 2, {}

    import logging
    log = logging.getLogger("bid_format_audit")
    log.addHandler(logging.NullHandler())
    fmt = bid_assemble.parse_format_rules(rules_md, log)

    # 变体感知:docx 属某变体时套用其 format_set,使审计期望 = 该变体实际排版。
    # 否则三变体各用独立字体(宋体/仿宋/楷体)会被单一期望误判为海量违规。
    import re as _re, json as _json
    _vm = _re.search(r"V(\d+)", docx_path.name)
    if _vm:
        _vj = tender_dir / "extracted" / "variants.json"
        _dq = tender_dir / "extracted" / "disqualification_rules.md"
        if _vj.exists() and _dq.exists():
            try:
                _vdata = _json.loads(_vj.read_text(encoding="utf-8"))
                _vlist = _vdata.get("variants", _vdata) if isinstance(_vdata, dict) else _vdata
                _vid = f"V{int(_vm.group(1)):02d}"
                _vrec = next((v for v in _vlist if v.get("id") == _vid), None)
                _fskey = (_vrec or {}).get("format_set")
                if _fskey:
                    _fsets = bid_assemble._parse_format_sets(_dq.read_text(encoding="utf-8"))
                    if str(_fskey).upper() in _fsets:
                        fmt = bid_assemble._apply_format_set(fmt, _fsets[str(_fskey).upper()], log)
            except Exception:
                pass

    # 补 default cover sizes（与 bid_assemble 同款 fallback，保证审计期望与组装期望一致）
    body = fmt.get("font_size_body", 14)
    fmt.setdefault("font_size_cover_title", body)
    fmt.setdefault("font_size_cover_project", body)
    fmt.setdefault("font_size_cover_meta", body)
    fmt.setdefault("font_size_title_style", body)

    doc = Document(str(docx_path))
    rows = _scan_runs(doc)
    styles = _scan_styles(doc)

    # 找第一个 Heading 1 段的 para_idx（封面/目录与正文的分界）
    first_h1 = None
    for r in rows:
        if (r["style"] or "").startswith("Heading 1"):
            first_h1 = r["para_idx"]
            break

    expected_cn_font = fmt.get("font_name_cn")
    violations = []
    summary = {"body": 0, "table": 0, "h1": 0, "h2plus": 0, "title_style": 0,
               "cover_or_toc": 0, "toc": 0}

    for r in rows:
        if not r["snippet"]:
            continue  # 空 run
        region = _classify(r, first_h1)
        summary[region] = summary.get(region, 0) + 1

        # 字号检查（仅当 run 有显式 sz 时；style-继承的不算违规，已通过样式扫描覆盖）
        actual_size = r["size_pt"]
        if actual_size is not None:
            exp = _expected_size(region, fmt)
            if exp is None:
                pass
            elif isinstance(exp, tuple) and exp[0] == "range":
                lo, hi = exp[1], exp[2]
                if actual_size < lo - 0.1 or actual_size > hi + 0.1:
                    violations.append({
                        "kind": "size",
                        "region": region,
                        "para_idx": r["para_idx"],
                        "style": r["style"],
                        "snippet": r["snippet"],
                        "actual": actual_size,
                        "expected": f"[{lo},{hi}]pt",
                    })
            elif abs(actual_size - exp) > 0.1:
                violations.append({
                    "kind": "size",
                    "region": region,
                    "para_idx": r["para_idx"],
                    "style": r["style"],
                    "snippet": r["snippet"],
                    "actual": actual_size,
                    "expected": exp,
                })

        # 字体检查
        actual_cn = r["east_asia"]
        # H1 区域按 H1 字体期望(标题惯用黑体),其余区域按正文字体
        exp_font = fmt.get("font_name_h1") if region == "h1" else expected_cn_font
        if actual_cn and exp_font and actual_cn != exp_font:
            violations.append({
                "kind": "font",
                "region": region,
                "para_idx": r["para_idx"],
                "style": r["style"],
                "snippet": r["snippet"],
                "actual": actual_cn,
                "expected": exp_font,
            })

    # 样式表层面的检查（Normal / Heading 1 / Table Grid 等）
    style_violations = []
    style_expected = {
        "Normal": fmt.get("font_size_body"),
        "Heading 1": fmt.get("font_size_h1") or fmt.get("font_size_body"),
        "Heading 2": fmt.get("font_size_body"),
        "Heading 3": fmt.get("font_size_body"),
        "Heading 4": fmt.get("font_size_body"),
        "Heading 5": fmt.get("font_size_body"),
        "Title": fmt.get("font_size_title_style") or fmt.get("font_size_body"),
        "Table Grid": fmt.get("font_size_table") or fmt.get("font_size_body"),
    }
    for sname, exp in style_expected.items():
        if exp is None or sname not in styles:
            continue
        info = styles[sname]
        if info["size_pt"] is not None and abs(info["size_pt"] - exp) > 0.1:
            style_violations.append({
                "style": sname, "actual": info["size_pt"], "expected": exp,
            })
        exp_sfont = (fmt.get("font_name_h1") if sname in ("Heading 1", "Title")
                     else expected_cn_font)
        if info["east_asia"] and exp_sfont and info["east_asia"] != exp_sfont:
            style_violations.append({
                "style": sname, "actual_font": info["east_asia"], "expected_font": exp_sfont,
            })

    # 决定退出码
    severe = any(v["region"] in ("body", "table", "h1", "h2plus", "toc") for v in violations) or bool(style_violations)
    soft = any(v["region"] in ("cover_or_toc", "title_style") for v in violations)
    code = 0 if not (severe or soft) else (2 if severe else 1)

    return code, {
        "docx": str(docx_path),
        "expected": {
            "font_name_cn": expected_cn_font,
            "font_size_body": fmt.get("font_size_body"),
            "font_size_table": fmt.get("font_size_table"),
            "font_size_h1": fmt.get("font_size_h1"),
            "font_size_cover_title": fmt.get("font_size_cover_title"),
            "font_size_cover_project": fmt.get("font_size_cover_project"),
            "font_size_cover_meta": fmt.get("font_size_cover_meta"),
            "font_size_title_style": fmt.get("font_size_title_style"),
        },
        "summary": summary,
        "violations": violations,
        "style_violations": style_violations,
        "exit_code": code,
    }


def write_report(out_dir: Path, result: dict):
    (out_dir / "format_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 格式审计报告（实测层）", ""]
    lines.append(f"**DOCX**：`{result['docx']}`")
    lines.append("")
    lines.append("## 期望参数")
    for k, v in result["expected"].items():
        lines.append(f"- `{k}` = {v}")
    lines.append("")
    lines.append("## Run 分布")
    for k, v in result["summary"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 段落字号违规")
    if not result["violations"]:
        lines.append("无 ✓")
    else:
        lines.append("| 区域 | 段号 | 样式 | 文本 | 实际 | 期望 |")
        lines.append("|------|------|------|------|------|------|")
        for v in result["violations"][:50]:
            lines.append(f"| {v.get('region','')} | {v['para_idx']} | {v.get('style','')} | "
                         f"{v['snippet']} | {v['actual']} | {v['expected']} |")
        if len(result["violations"]) > 50:
            lines.append(f"\n（仅显示前 50 条，共 {len(result['violations'])} 条）")
    lines.append("")
    lines.append("## 样式表违规")
    if not result["style_violations"]:
        lines.append("无 ✓")
    else:
        for sv in result["style_violations"]:
            lines.append(f"- {sv}")
    lines.append("")
    lines.append(f"**审计结论**：退出码 {result['exit_code']} "
                 f"({'通过' if result['exit_code']==0 else 'gray-area' if result['exit_code']==1 else '违规'})")

    (out_dir / "format_audit.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tender_dir")
    ap.add_argument("--docx", default=None, help="指定 .docx 路径；缺省时取 output/ 下首个 *.docx")
    args = ap.parse_args()

    tender = Path(args.tender_dir).resolve()
    if args.docx:
        docx_path = Path(args.docx).resolve()
    else:
        candidates = sorted((tender / "output").glob("*_施工组织设计.docx")) \
            + sorted((tender / "output").glob("*_监理服务方案.docx"))
        if not candidates:
            print(f"[error] {tender}/output 下未找到 *_施工组织设计.docx / *_监理服务方案.docx", file=sys.stderr)
            sys.exit(2)
        docx_path = candidates[0]

    code, result = audit(tender, docx_path)
    out_dir = tender / "output" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not result:
        # rules 缺失等硬失败路径: 无结果可写报告,直接退出(避免 write_report 对空 dict
        # 访问 result['docx'] 抛 KeyError,并把真实退出码透传给上层调用方)
        print(f"格式审计失败(无结果),退出码 {code}", file=sys.stderr)
        sys.exit(code)
    write_report(out_dir, result)
    print(f"格式审计 → output/reports/format_audit.md  退出码 {code}  违规 {len(result.get('violations', []))} 条")
    sys.exit(code)


if __name__ == "__main__":
    main()
