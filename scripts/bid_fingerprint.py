#!/usr/bin/env python3
"""
bid_fingerprint.py - PDF 文件指纹采集工具

为目录下每个 PDF 采集结构特征（页数、尺寸、字符密度、表格、元数据、首页关键词），
输出 fingerprints.json 供 Claude 分类使用。

用法: python bid_fingerprint.py /path/to/tender/
输出:
  - <dir>/extracted/fingerprints.json
  - <dir>/extracted/fingerprint.log
  - 终端汇总表
"""

import sys
import re
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import make_script_logger  # noqa: E402


# 分类启发式使用的关键词
KEYWORDS = {
    "tender_main": ["招标文件", "投标人须知", "评标办法", "合同条款"],
    "tender_notice": ["招标公告", "资格预审公告"],
    "boq": ["工程量清单", "最高限价", "预算表", "综合单价", "定额编号"],
    "drawing": ["图册", "设计图", "单体图", "平面图", "总平面", "剖面图", "图纸目录", "图号"],
    "spec": ["技术规范", "实施方案", "技术要求", "附表"],
}

DRAWING_CREATORS = ["autocad", "天正", "revit", "microstation", "dwg"]


def digit_ratio(text: str) -> float:
    if not text:
        return 0.0
    digits = sum(1 for c in text if c.isdigit())
    non_space = sum(1 for c in text if not c.isspace())
    return digits / non_space if non_space else 0.0


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_space = sum(1 for c in text if not c.isspace())
    return cjk / non_space if non_space else 0.0


def match_keywords(text: str, first_page_text: str, filename: str) -> dict:
    """返回每个类别在首页 + 文件名中的关键词命中数"""
    hay = (first_page_text[:1500] + " " + filename).lower()
    hits = {}
    for cat, words in KEYWORDS.items():
        hits[cat] = sum(1 for w in words if w.lower() in hay)
    return hits


def fingerprint_pdf(path: Path, log: logging.Logger) -> dict:
    """采集单个 PDF 指纹"""
    fp = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "ok": False,
    }

    try:
        import pdfplumber
    except ImportError:
        log.error("未安装 pdfplumber，跳过")
        fp["error"] = "pdfplumber not installed"
        return fp

    try:
        with pdfplumber.open(path) as pdf:
            pages = pdf.pages
            n_pages = len(pages)

            # 尺寸采样：最多取前 5 页的中位数
            sample_idx = list(range(min(5, n_pages)))
            widths = [float(pages[i].width) for i in sample_idx]
            heights = [float(pages[i].height) for i in sample_idx]
            # 1 pt = 0.3528 mm
            avg_w_mm = round(sum(widths) / len(widths) * 0.3528, 1)
            avg_h_mm = round(sum(heights) / len(heights) * 0.3528, 1)
            landscape = avg_w_mm > avg_h_mm
            long_side = max(avg_w_mm, avg_h_mm)

            # 判定纸张
            if long_side > 800:
                paper = "A1+"
            elif long_side > 550:
                paper = "A2"
            elif long_side > 380:
                paper = "A3"
            elif long_side > 280:
                paper = "A4"
            else:
                paper = "small"

            # 文字采样：最多前 20 页 + 末 3 页
            text_sample_idx = set(range(min(20, n_pages)))
            if n_pages > 20:
                text_sample_idx.update(range(max(0, n_pages - 3), n_pages))
            total_chars = 0
            first_page_text = ""
            sampled_pages = 0
            for i in sorted(text_sample_idx):
                t = pages[i].extract_text() or ""
                total_chars += len(t)
                sampled_pages += 1
                if i == 0:
                    first_page_text = t
            avg_chars_per_page = round(total_chars / sampled_pages, 1) if sampled_pages else 0

            # 表格检测：采前 3 页
            table_count = 0
            for i in range(min(3, n_pages)):
                try:
                    tables = pages[i].find_tables()
                    table_count += len(tables)
                except Exception:
                    pass

            # 书签
            try:
                outline = pdf.outline or []
                has_outline = len(outline) > 0
                outline_depth = len(outline)
            except Exception:
                has_outline = False
                outline_depth = 0

            # 元数据
            meta = pdf.metadata or {}
            creator = str(meta.get("Creator", "")) + " " + str(meta.get("Producer", ""))
            is_cad = any(k in creator.lower() for k in DRAWING_CREATORS)

            # 关键词
            kw_hits = match_keywords("", first_page_text, path.name)

            fp.update({
                "ok": True,
                "pages": n_pages,
                "avg_width_mm": avg_w_mm,
                "avg_height_mm": avg_h_mm,
                "landscape": landscape,
                "paper": paper,
                "avg_chars_per_page": avg_chars_per_page,
                "digit_ratio": round(digit_ratio(first_page_text), 3),
                "cjk_ratio": round(cjk_ratio(first_page_text), 3),
                "table_count_top3": table_count,
                "has_outline": has_outline,
                "outline_depth": outline_depth,
                "creator": creator.strip(),
                "is_cad_software": is_cad,
                "keyword_hits": kw_hits,
                "first_page_excerpt": re.sub(r"\s+", " ", first_page_text)[:300],
            })

            # 启发式初判（Claude 会覆盖这个）
            fp["heuristic_guess"] = heuristic_classify(fp)

    except Exception as e:
        log.error(f"  解析失败 {path.name}: {e}")
        fp["error"] = str(e)

    return fp


def heuristic_classify(fp: dict) -> str:
    """基于指纹做初步分类，作为 Claude 的参考"""
    hits = fp.get("keyword_hits", {})
    best_cat = max(hits, key=hits.get) if hits else None
    best_score = hits.get(best_cat, 0) if best_cat else 0

    paper = fp.get("paper", "")
    cpp = fp.get("avg_chars_per_page", 0)
    is_cad = fp.get("is_cad_software", False)
    digits = fp.get("digit_ratio", 0)

    # 图纸强信号：大纸张 + 低字符密度 或 CAD 软件创建
    if is_cad or (paper in ("A1+", "A2") and cpp < 800):
        return "drawing"
    # 图纸次级信号：A3 横向 + 数字多 + 关键词命中
    if paper == "A3" and fp.get("landscape") and digits > 0.25:
        return "drawing"
    # 关键词主导
    if best_score >= 2:
        return best_cat
    if best_score == 1 and best_cat:
        return best_cat + "?"
    # 字符密度判断
    if cpp > 1500:
        return "tender_main?"
    if cpp < 200:
        return "drawing?"
    return "unknown"


def fingerprint_dir(tender_dir: Path, log: logging.Logger) -> list[dict]:
    pdfs = sorted(tender_dir.glob("*.pdf"))
    log.info(f"发现 {len(pdfs)} 个 PDF 文件")
    results = []
    for pdf in pdfs:
        log.info(f"采集指纹: {pdf.name}")
        fp = fingerprint_pdf(pdf, log)
        if fp.get("ok"):
            log.info(
                f"  页数={fp['pages']} 纸张={fp['paper']} 字/页={fp['avg_chars_per_page']} "
                f"表格={fp['table_count_top3']} 书签={fp['has_outline']} "
                f"初判={fp['heuristic_guess']}"
            )
        results.append(fp)
    return results


def print_summary(results: list[dict]) -> None:
    print("\n" + "=" * 100)
    print(f"{'文件名':<50} {'页数':>5} {'纸张':>5} {'字/页':>8} {'表':>4} {'书签':>5} {'初判':<16}")
    print("-" * 100)
    for r in results:
        if not r.get("ok"):
            print(f"{r['filename'][:48]:<50} {'ERR':>5}")
            continue
        name = r["filename"]
        name_disp = name if len(name) <= 48 else name[:45] + "..."
        print(
            f"{name_disp:<50} {r['pages']:>5} {r['paper']:>5} {r['avg_chars_per_page']:>8.0f} "
            f"{r['table_count_top3']:>4} {str(r['has_outline']):>5} {r['heuristic_guess']:<16}"
        )
    print("=" * 100)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python bid_fingerprint.py /path/to/tender/")
        return 2

    tender_dir = Path(sys.argv[1]).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}")
        return 2

    extracted_dir = tender_dir / "extracted"
    extracted_dir.mkdir(exist_ok=True)
    log_path = extracted_dir / "fingerprint.log"
    out_path = extracted_dir / "fingerprints.json"

    log = make_script_logger("bid_fingerprint", log_path)
    log.info("=" * 60)
    log.info(f"bid_fingerprint 启动  目录: {tender_dir}")
    log.info("=" * 60)

    results = fingerprint_dir(tender_dir, log)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": str(tender_dir),
        "pdf_count": len(results),
        "fingerprints": results,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"指纹已写入: {out_path}")
    log.info("=" * 60)

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
