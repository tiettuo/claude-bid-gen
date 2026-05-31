#!/usr/bin/env python3
"""
bid_extract.py - 投标文件全量提取工具
将目录下所有 PDF/Excel/DOCX/TXT 文件提取为纯文本，生成 manifest.txt
运行日志保存至 extracted/run.log

用法: python bid_extract.py /path/to/tender/
"""
from __future__ import annotations

import hashlib
import json
import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import make_script_logger  # noqa: E402
import bid_vision  # noqa: E402

# 增量提取缓存版本。提取逻辑(OCR/解析)有实质改动时 +1,使旧缓存失效。
_EXTRACT_CACHE_VERSION = 1


def _file_md5(path: Path) -> str:
    """文件内容 md5(分块读,大 PDF 也快)。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_env():
    """自动加载 .env 文件中的环境变量（GLM_API_KEY 等）"""
    import os
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # 按优先级依次尝试加载
    env_paths = [
        Path(os.environ.get("LLM_ENV_FILE", "")),  # 显式指定
        Path.home() / ".env",                       # 用户主目录
        Path(__file__).resolve().parent.parent.parent / "bid-doc-generator" / ".env",  # 项目目录
    ]
    for p in env_paths:
        if p and p.is_file():
            load_dotenv(p, override=False)
            return


_load_env()


def _cid_corrupted(text: str, min_count: int = 10, min_ratio: float = 0.2) -> bool:
    """字体 ToUnicode 表缺失时 pdfplumber/fitz 会回退成 (cid:xxxx) 伪字符。
    功能上等同扫描页：有'字符'但不可读，必须走视觉 OCR。
    """
    cid_count = text.count("(cid:")
    if cid_count < min_count:
        return False
    return (cid_count * 8) / max(len(text), 1) >= min_ratio


def _is_scanned_page_pdfplumber(page, text: str, min_chars: int = 20) -> bool:
    """检测 pdfplumber 页面是否为扫描图片页（含字体损坏的伪字符页）"""
    if _cid_corrupted(text):
        return True
    if len(text.strip()) >= min_chars:
        return False
    if hasattr(page, "images") and page.images:
        return True
    return False


def _is_scanned_page_fitz(fitz_page, text: str, min_chars: int = 20) -> bool:
    """检测 PyMuPDF 页面是否为扫描图片页（含字体损坏的伪字符页）"""
    if _cid_corrupted(text):
        return True
    if len(text.strip()) >= min_chars:
        return False
    img_list = fitz_page.get_images(full=True)
    return len(img_list) > 0


def _page_to_image_bytes(fitz_page, dpi: int = 300) -> bytes:
    """将 PyMuPDF 页面转为 PNG 图片字节"""
    import fitz
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = fitz_page.get_pixmap(matrix=mat)
    return pix.tobytes("png")


def _ocr_paddle(image_bytes: bytes, log: logging.Logger) -> list[tuple[str, float]]:
    """PaddleOCR 本地识别，返回 [(text, confidence), ...]"""
    import tempfile, os
    try:
        from paddleocr import PaddleOCR
        if not hasattr(_ocr_paddle, "_engine"):
            _ocr_paddle._engine = PaddleOCR(
                use_angle_cls=True, lang="ch", show_log=False
            )
        engine = _ocr_paddle._engine
        # PaddleOCR 需要文件路径
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        try:
            result = engine.ocr(tmp_path, cls=True)
        finally:
            os.unlink(tmp_path)
        if not result or not result[0]:
            return []
        lines = []
        for line in result[0]:
            text = line[1][0]
            conf = line[1][1]
            lines.append((text, conf))
        return lines
    except ImportError:
        log.warning("  PaddleOCR 未安装，跳过本地 OCR")
        return []
    except Exception as e:
        log.warning(f"  PaddleOCR 识别异常: {e}")
        return []


def _assess_ocr_quality(lines: list[tuple[str, float]],
                        page_threshold: float = 0.80,
                        line_threshold: float = 0.85) -> str:
    """评估 OCR 质量，返回 'good' / 'page_retry' / 'partial_retry'"""
    if not lines:
        return "page_retry"
    avg_conf = sum(c for _, c in lines) / len(lines)
    low_lines = [(t, c) for t, c in lines if c < line_threshold]
    if avg_conf < page_threshold:
        return "page_retry"
    if low_lines:
        return "partial_retry"
    return "good"


def _ocr_precise_api(image_bytes: bytes, log: logging.Logger) -> str:
    """调用 GLM-OCR 精准识别 API（layout_parsing 端点，支持 base64 输入）"""
    import base64, os

    api_key = os.environ.get("GLM_API_KEY", "") or os.environ.get("LLM_ZHIPU_KEY", "")
    if not api_key:
        log.warning("  GLM-OCR API 未配置（GLM_API_KEY 或 LLM_ZHIPU_KEY），跳过精准识别")
        return ""

    try:
        from zai import ZhipuAiClient

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/png;base64,{b64}"
        client = ZhipuAiClient(api_key=api_key)
        response = client.layout_parsing.create(
            model="glm-ocr",
            file=data_uri,
        )
        md_text = response.md_results or ""
        if md_text.strip():
            log.info(f"  GLM-OCR 精准识别成功 ({len(md_text)} 字符)")
            return md_text
        return ""
    except Exception as e:
        log.warning(f"  GLM-OCR 精准识别失败: {e}")
        return ""


_BOQ_FILENAME_KEYWORDS = [
    "清单", "工程量", "控制价", "招标控制价", "工程造价",
    "报价单", "计价表", "费用汇总",
]
_BOQ_SUMMARY_KEYWORDS = [
    "汇总表", "汇总", "工程费汇总", "单项工程费", "单位工程费",
    "招标控制价汇总", "工程造价汇总", "费用汇总",
]
_BOQ_DETAIL_HEADER = [
    "分部分项工程量清单", "单价措施项目", "措施项目清单",
    "其他项目清单", "暂估价", "暂列金额", "计日工",
    "材料（工程设备）暂估单价", "专业工程暂估价",
]


def _is_boq_file(file_path: Path) -> bool:
    """判断 PDF 是否为工程量清单/控制价文件（按文件名关键词）。"""
    name = file_path.stem.lower()
    return any(kw in file_path.stem for kw in _BOQ_FILENAME_KEYWORDS)


def _filter_boq_summary_pages(page_meta: list[dict], log: logging.Logger) -> list[dict]:
    """清单 PDF 只保留汇总表相关页，跳过分部分项明细行。

    策略：
    - 全页均为扫描页（无文字）→ 只取前 15 页（封面+汇总表通常在此范围）
    - 有文字的页：包含汇总关键词或前 5 页 → 保留；命中明细表头且无汇总词 → 跳过
    """
    all_scanned = all(meta.get("is_scanned", False) or not meta.get("text", "").strip()
                      for meta in page_meta)
    if all_scanned:
        # 全扫描清单：先只送前 25 页进 vision OCR，vision 完成后在阶段4再按"汇总表"过滤
        kept = [m for m in page_meta if m["idx"] < 30]
        log.info(f"  清单文件（全扫描）：送前 30 页做 Vision OCR，跳过 {len(page_meta) - len(kept)} 页明细，"
                 f"阶段4将按'汇总表'关键词二次过滤")
        return kept

    kept = []
    for meta in page_meta:
        idx = meta["idx"]
        text = meta.get("text", "")
        is_summary = any(kw in text for kw in _BOQ_SUMMARY_KEYWORDS)
        is_detail_only = (
            any(kw in text for kw in _BOQ_DETAIL_HEADER)
            and not is_summary
        )
        if idx < 5 or is_summary:
            kept.append(meta)
        elif is_detail_only:
            pass  # 跳过明细页
        else:
            kept.append(meta)
    skipped = len(page_meta) - len(kept)
    if skipped:
        log.info(f"  清单文件：保留 {len(kept)}/{len(page_meta)} 页（跳过 {skipped} 页明细）")
    return kept


def extract_pdf(file_path: Path, log: logging.Logger, vision_report: dict | None = None) -> str:
    """提取 PDF 文本，分层策略：
    - 文字页 + 无关键词命中 → pdfplumber 原样输出
    - 文字页 + 强关键词命中（废标/暗标/评分）→ 视觉模型强制重抽，表格结构可靠
    - 扫描页 → 视觉模型（qwen-vl 主力，glm-ocr 兜底），失败再降级 PaddleOCR

    vision_report：可选 dict，运行后会注入 {"pages":[{idx,reason,model,categories,hits}], "total_pages":int}
    供 main() 写入 manifest
    """
    text_parts: list[str] = []
    # 每页元数据：idx(0基), text(pdfplumber/fitz 原文), is_scanned, classification
    page_meta: list[dict] = []

    try:
        vision_cfg = bid_vision.load_config()
    except Exception as e:
        log.warning(f"  视觉配置加载失败，退化为无视觉模式: {e}")
        vision_cfg = None

    # === 阶段 1：pdfplumber 提取文字 + 表格 + 关键词分类 ===
    pdfplumber_ok = False
    try:
        import pdfplumber
        log.debug(f"  使用 pdfplumber 提取: {file_path.name}")
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                is_scanned = _is_scanned_page_pdfplumber(page, page_text)

                # 拼装该页原文（含表格）
                blocks = []
                if not is_scanned and page_text.strip():
                    blocks.append(page_text)
                    tables = page.extract_tables() or []
                    for table in tables:
                        if not table:
                            continue
                        rows = [" | ".join(str(c or "").strip() for c in row) for row in table]
                        blocks.append("[表格]\n" + "\n".join(rows))
                raw_text = "\n".join(blocks)

                # 关键词分类（扫描页也扫，能命中就更好，扫不中也没关系）
                if vision_cfg:
                    classification = bid_vision.classify_page(raw_text, vision_cfg)
                else:
                    classification = bid_vision.PageClassification(set(), {})

                page_meta.append({
                    "idx": i,
                    "text": raw_text,
                    "is_scanned": is_scanned,
                    "classification": classification,
                })
            pdfplumber_ok = True
            log.info(
                f"  pdfplumber 扫描完成: {file_path.name} ({total_pages} 页，"
                f"扫描页 {sum(1 for m in page_meta if m['is_scanned'])} 个)"
            )
    except ImportError:
        log.warning("  pdfplumber 未安装，改走 PyMuPDF")
    except Exception as e:
        log.warning(f"  pdfplumber 提取失败: {e}，改走 PyMuPDF")

    # === 阶段 2：PyMuPDF 兜底 + 渲染视觉页 ===
    fitz_doc = None
    try:
        import fitz
        fitz_doc = fitz.open(file_path)

        if not pdfplumber_ok:
            page_meta = []
            for i, page in enumerate(fitz_doc):
                page_text = page.get_text() or ""
                is_scanned = _is_scanned_page_fitz(page, page_text)
                if vision_cfg:
                    classification = bid_vision.classify_page(page_text, vision_cfg)
                else:
                    classification = bid_vision.PageClassification(set(), {})
                page_meta.append({
                    "idx": i,
                    "text": "" if is_scanned else page_text,
                    "is_scanned": is_scanned,
                    "classification": classification,
                })
            log.info(f"  PyMuPDF 扫描完成: {file_path.name} ({len(page_meta)} 页)")

        # 清单文件：在触发 vision 前先过滤，只保留汇总表页
        if _is_boq_file(file_path):
            page_meta = _filter_boq_summary_pages(page_meta, log)

        # === 阶段 3：决定哪些页走视觉 ===
        vision_page_records: list[dict] = []
        vision_targets: list[tuple[int, str]] = []  # (idx, reason)
        if vision_cfg:
            for meta in page_meta:
                trigger, reason = bid_vision.should_use_vision(
                    meta["classification"], meta["is_scanned"], vision_cfg
                )
                if trigger:
                    vision_targets.append((meta["idx"], reason))

        if vision_targets:
            log.info(
                f"  视觉 OCR 触发 {len(vision_targets)} 页（扫描页 "
                f"{sum(1 for m in page_meta if m['is_scanned'])}，"
                f"强关键词 {len(vision_targets) - sum(1 for m in page_meta if m['is_scanned'])}）"
            )
            # 阶段 3a：收集所有视觉页的图片 + 批量分流
            batch_pages: list[tuple[int, bytes, set[str] | None]] = []
            for idx, reason in vision_targets:
                if idx >= len(fitz_doc):
                    continue
                fitz_page = fitz_doc[idx]
                image_bytes = _page_to_image_bytes(
                    fitz_page,
                    dpi=int(vision_cfg.get("render_dpi", 300)),
                )
                cats = page_meta[idx]["classification"].categories
                batch_pages.append((idx, image_bytes, cats))

            batch_results = bid_vision.vision_extract_batch(
                batch_pages, log, config=vision_cfg
            )

            # 阶段 3b：处理每页结果 + 失败降级
            for idx, reason in vision_targets:
                if idx >= len(fitz_doc):
                    continue
                vision_text, model_used = batch_results.get(idx, ("", ""))
                image_bytes = next(
                    (b for i, b, _ in batch_pages if i == idx), None
                )

                if not vision_text:
                    if page_meta[idx]["is_scanned"] and image_bytes:
                        paddle_lines = _ocr_paddle(image_bytes, log)
                        if paddle_lines:
                            vision_text = "\n".join(t for t, _ in paddle_lines)
                            model_used = "paddle_fallback"
                            log.warning(f"    第{idx+1}页: 视觉失败，PaddleOCR 兜底")
                        else:
                            vision_text = page_meta[idx]["text"] or "[OCR 识别失败]"
                            model_used = "none"
                    else:
                        vision_text = page_meta[idx]["text"]
                        model_used = "none"
                        log.warning(f"    第{idx+1}页: 视觉失败，回退 pdfplumber 原文")

                page_meta[idx]["vision_text"] = vision_text
                page_meta[idx]["vision_reason"] = reason
                page_meta[idx]["vision_model"] = model_used
                hits = page_meta[idx]["classification"].hits
                vision_page_records.append({
                    "page": idx + 1,
                    "reason": reason,
                    "model": model_used,
                    "categories": sorted(page_meta[idx]["classification"].categories),
                    "hits": {k: v[:5] for k, v in hits.items()},
                    "vision_text": vision_text,
                })
                log.info(f"    第{idx+1}页 [{reason}] 模型={model_used} 字符={len(vision_text)}")

        # === 阶段 4：汇总输出文本 ===
        # 清单全扫描文件：vision 完成后按"汇总表"二次过滤
        if _is_boq_file(file_path):
            filtered_meta = []
            for meta in page_meta:
                final_text = meta.get("vision_text") or meta.get("text", "")
                if "汇总表" in final_text:
                    filtered_meta.append(meta)
            if filtered_meta:
                log.info(f"  清单文件：二次过滤后保留 {len(filtered_meta)}/{len(page_meta)} 页（含'汇总表'）")
                page_meta = filtered_meta
            else:
                log.warning(f"  清单文件：二次过滤无命中，保留全部 {len(page_meta)} 页（'汇总表'未识别到）")

        for meta in page_meta:
            i = meta["idx"]
            if "vision_text" in meta:
                header = f"[第{i+1}页 - 视觉OCR:{meta['vision_model']}]"
                text_parts.append(f"{header}\n{meta['vision_text']}")
            elif meta["is_scanned"]:
                text_parts.append(f"[第{i+1}页：扫描页，无可用 OCR 结果]")
            elif meta["text"].strip():
                text_parts.append(f"[第{i+1}页]\n{meta['text']}")

        # 回填报告
        if vision_report is not None:
            vision_report["total_pages"] = len(page_meta)
            vision_report["vision_pages"] = vision_page_records

    except ImportError:
        log.warning("  PyMuPDF (fitz) 未安装，视觉 OCR 链路降级")
    except Exception as e:
        log.error(f"  PDF 处理异常: {e}", exc_info=True)
    finally:
        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except OSError:
                pass

    if text_parts:
        return "\n\n".join(text_parts)

    msg = f"[无法提取文本，文件可能为扫描图片或加密: {file_path.name}]"
    log.warning(f"  {msg}")
    return msg


def extract_excel(file_path: Path, log: logging.Logger) -> str:
    """提取 Excel 工程量清单为可读文本，正确处理合并单元格"""
    text_parts = []

    try:
        import openpyxl
        log.debug(f"  使用 openpyxl 提取: {file_path.name}")
        wb = openpyxl.load_workbook(file_path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            # 构建合并单元格映射：记录每个合并区域的起始值
            merge_map = {}  # (row, col) -> value，非起始位置为 ""
            for merge_range in ws.merged_cells.ranges:
                min_row, min_col = merge_range.min_row, merge_range.min_col
                value = ws.cell(min_row, min_col).value
                for row in range(merge_range.min_row, merge_range.max_row + 1):
                    for col in range(merge_range.min_col, merge_range.max_col + 1):
                        if row == min_row and col == min_col:
                            merge_map[(row, col)] = str(value or "").strip()
                        else:
                            merge_map[(row, col)] = None  # 标记为合并占位

            rows = []
            for row_cells in ws.iter_rows():
                cells = []
                for cell in row_cells:
                    pos = (cell.row, cell.column)
                    if pos in merge_map:
                        val = merge_map[pos]
                        if val is not None:
                            cells.append(val)
                        # None 表示合并占位，跳过
                    else:
                        cells.append(str(cell.value).strip() if cell.value is not None else "")
                if any(c for c in cells):
                    rows.append(" | ".join(cells))
            if rows:
                text_parts.append(f"【工作表: {sheet_name}】\n" + "\n".join(rows))

        # xlsx 嵌入图片走视觉 OCR（评分表截图常见嵌在 sheet 里）
        embedded = _extract_embedded_images(file_path, ("xl/media/",), log)
        if embedded:
            text_parts.append("【嵌入图片（视觉 OCR）】")
            for name, text, model in embedded:
                text_parts.append(f"\n--- 嵌入图: {name} [模型:{model}] ---\n{text}")

        log.info(
            f"  openpyxl 提取成功: {file_path.name} "
            f"({len(wb.sheetnames)} 个工作表，嵌入图 {len(embedded)} 张走视觉)"
        )
        return "\n\n".join(text_parts)
    except ImportError:
        log.warning("  openpyxl 未安装，跳过")
    except Exception as e:
        log.warning(f"  openpyxl 提取失败: {e}，尝试 xlrd...")

    try:
        import xlrd
        log.debug(f"  使用 xlrd 提取: {file_path.name}")
        wb = xlrd.open_workbook(file_path)
        for sheet in wb.sheets():
            rows = []
            for rx in range(sheet.nrows):
                row = [str(sheet.cell_value(rx, cx)).strip() for cx in range(sheet.ncols)]
                if any(c for c in row):
                    rows.append(" | ".join(row))
            if rows:
                text_parts.append(f"【工作表: {sheet.name}】\n" + "\n".join(rows))
        log.info(f"  xlrd 提取成功: {file_path.name} ({wb.nsheets} 个工作表)")
        return "\n\n".join(text_parts)
    except ImportError:
        log.warning("  xlrd 未安装，跳过")
    except Exception as e:
        log.error(f"  xlrd 提取失败: {e}")

    msg = f"[无法提取 Excel 文件: {file_path.name}]"
    log.warning(f"  {msg}")
    return msg


def extract_doc_legacy(file_path: Path, log: logging.Logger) -> str:
    """提取老式 .doc 文件文本（OLE 格式）"""
    import subprocess

    # 方法1: macOS textutil
    try:
        log.debug(f"  尝试 textutil 转换: {file_path.name}")
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(file_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            log.info(f"  textutil 提取成功: {file_path.name}")
            return result.stdout
        else:
            log.warning(f"  textutil 失败 (returncode={result.returncode}): {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log.warning(f"  textutil 超时: {file_path.name}")
    except FileNotFoundError:
        log.warning("  textutil 不可用（非 macOS 环境）")

    # 方法2: olefile 读取 WordDocument 流
    try:
        import olefile
        log.debug(f"  尝试 olefile 提取: {file_path.name}")
        ole = olefile.OleFileIO(str(file_path))
        if ole.exists("WordDocument"):
            stream = ole.openstream("WordDocument")
            data = stream.read()
            log.debug(f"  WordDocument 流大小: {len(data):,} 字节")
            chars = []
            i = 0
            while i < len(data) - 1:
                try:
                    c = data[i:i+2].decode("utf-16-le")
                    if "\u4e00" <= c <= "\u9fff":
                        chars.append(c)
                    elif c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                        chars.append(c)
                    elif c in "，。、；：""''（）【】！？—…·×+-=/% \t":
                        chars.append(c)
                    elif c in "\r\n":
                        chars.append("\n")
                except Exception:
                    pass
                i += 2
            raw = "".join(chars)
            lines = raw.split("\n")
            clean_lines = []
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                total = len(stripped)
                cn_count = sum(1 for c in stripped if "\u4e00" <= c <= "\u9fff")
                printable_count = sum(1 for c in stripped if c.isprintable() and not ("\ue000" <= c <= "\ufffd"))
                if total > 0:
                    cn_ratio = cn_count / total
                    if cn_ratio >= 0.4 or (cn_count == 0 and printable_count == total and total < 100):
                        clean_lines.append(stripped)
            text = "\n".join(clean_lines)
            log.debug(f"  olefile 原始行数: {len(lines)}，过滤后: {len(clean_lines)} 行，字符数: {len(text):,}")
            if len(text) > 100:
                log.info(f"  olefile 提取成功: {file_path.name}，有效字符: {len(text):,}")
                return text
            else:
                log.warning(f"  olefile 提取内容过少 ({len(text)} 字符)，视为失败")
        else:
            log.warning(f"  olefile: 未找到 WordDocument 流: {file_path.name}")
    except ImportError:
        log.warning("  olefile 未安装，跳过")
    except Exception as e:
        log.error(f"  olefile 提取异常: {e}", exc_info=True)

    msg = f"[.doc 文件无法提取，建议手动另存为 .docx: {file_path.name}]"
    log.warning(f"  {msg}")
    return msg


def extract_docx(file_path: Path, log: logging.Logger) -> str:
    """提取 DOCX/DOC 文本，保留标题层级、表格结构、页眉页脚"""
    if file_path.suffix.lower() == ".doc":
        return extract_doc_legacy(file_path, log)

    try:
        from docx import Document
        log.debug(f"  使用 python-docx 提取: {file_path.name}")
        doc = Document(file_path)
        parts = []

        # 提取页眉页脚
        for section in doc.sections:
            header = section.header
            if header and not header.is_linked_to_previous:
                header_text = " ".join(
                    p.text for p in header.paragraphs if p.text.strip()
                ).strip()
                if header_text:
                    parts.append(f"[页眉: {header_text}]")
            footer = section.footer
            if footer and not footer.is_linked_to_previous:
                footer_text = " ".join(
                    p.text for p in footer.paragraphs if p.text.strip()
                ).strip()
                if footer_text:
                    parts.append(f"[页脚: {footer_text}]")

        # 提取段落（保留标题层级）
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = (para.style.name or "").lower() if para.style else ""
            if "title" in style_name or "heading 1" in style_name:
                parts.append(f"\n# {text}\n")
            elif "heading 2" in style_name:
                parts.append(f"\n## {text}\n")
            elif "heading 3" in style_name:
                parts.append(f"\n### {text}\n")
            elif "heading 4" in style_name:
                parts.append(f"\n#### {text}\n")
            elif "heading 5" in style_name:
                parts.append(f"\n##### {text}\n")
            else:
                parts.append(text)

        # 提取表格（检测合并单元格）
        for table in doc.tables:
            table_parts = []
            for row_idx, row in enumerate(table.rows):
                cells_text = []
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    cells_text.append(cell_text)
                if any(cells_text):
                    # 去重相邻重复单元格（合并单元格的副作用）
                    deduped = []
                    prev = None
                    for ct in cells_text:
                        if ct != prev or not ct:
                            deduped.append(ct)
                        prev = ct
                    table_parts.append(" | ".join(deduped))
            if table_parts:
                parts.append("\n[表格]")
                parts.extend(table_parts)
                parts.append("[/表格]\n")

        # 嵌入图片走视觉 OCR（评分表截图、废标条款图片、签章等常见场景）
        embedded = _extract_embedded_images(file_path, ("word/media/",), log)
        if embedded:
            parts.append("\n[嵌入图片（视觉 OCR）]")
            for name, text, model in embedded:
                parts.append(f"\n--- 嵌入图: {name} [模型:{model}] ---\n{text}")
            parts.append("[/嵌入图片]\n")

        log.info(
            f"  python-docx 提取成功: {file_path.name} "
            f"({len(parts)} 段落/行，嵌入图 {len(embedded)} 张走视觉)"
        )
        return "\n".join(parts)
    except ImportError:
        msg = f"[python-docx 未安装，无法提取: {file_path.name}]"
        log.warning(f"  {msg}")
        return msg
    except Exception as e:
        msg = f"[提取失败: {e}]"
        log.error(f"  python-docx 提取异常: {file_path.name}: {e}", exc_info=True)
        return msg


def _classify_heading(size: float, is_bold: bool, body_size: float) -> str:
    """根据字号和粗体判定标题层级，返回 Markdown 前缀"""
    if size >= 18 or (size >= 14 and is_bold):
        return "## "
    if size >= body_size + 2 or is_bold:
        return "### "
    return ""


def extract_pdf_structured(file_path: Path, log: logging.Logger) -> str:
    """提取 PDF 文本并保留标题层级结构（使用 PyMuPDF 字体信息）"""
    import fitz

    doc = fitz.open(file_path)
    parts = []

    # 第一遍：统计正文字号（出现频率最高的字号）
    from collections import Counter
    size_counter = Counter()
    for page in doc:
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        size_counter[round(span["size"], 1)] += 1
    body_size = size_counter.most_common(1)[0][0] if size_counter else 12.0

    # 第二遍：提取文本并标注标题
    for page_num, page in enumerate(doc):
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                line_text = ""
                max_size = 0
                is_bold = False
                for span in line["spans"]:
                    line_text += span["text"]
                    max_size = max(max_size, span["size"])
                    font_name = span.get("font", "").lower()
                    if "bold" in font_name or "黑体" in font_name:
                        is_bold = True
                line_text = line_text.strip()
                if not line_text:
                    continue
                prefix = _classify_heading(max_size, is_bold, body_size)
                if prefix:
                    parts.append(f"\n{prefix}{line_text}\n")
                else:
                    parts.append(line_text)
        parts.append(f"\n--- 第{page_num + 1}页 ---\n")
    doc.close()

    if parts:
        log.info(f"  结构化提取成功: {file_path.name}（正文字号={body_size}）")
        return "\n".join(parts)

    msg = f"[无法提取文本，文件可能为扫描图片或加密: {file_path.name}]"
    log.warning(f"  {msg}")
    return msg


def extract_txt(file_path: Path, log: logging.Logger) -> str:
    """读取纯文本文件"""
    for encoding in ["utf-8", "gbk", "gb2312", "utf-16"]:
        try:
            text = file_path.read_text(encoding=encoding)
            log.info(f"  读取成功: {file_path.name} (编码: {encoding})")
            return text
        except (UnicodeDecodeError, LookupError):
            continue
    msg = f"[无法读取文件编码: {file_path.name}]"
    log.warning(f"  {msg}")
    return msg


def _extract_image_bytes_via_vision(
    image_bytes: bytes, label: str, log: logging.Logger
) -> tuple[str, str]:
    """将任意图片字节送 bid_vision.vision_extract，返回 (markdown, model_used)

    label 仅用于日志，区分独立图片 / docx 嵌入 / xlsx 嵌入 等来源
    """
    try:
        text, model = bid_vision.vision_extract(image_bytes, log)
        if text:
            log.info(f"  视觉识别[{label}] 成功 模型={model} 字符={len(text)}")
        else:
            log.warning(f"  视觉识别[{label}] 全部失败")
        return text, model
    except Exception as e:
        log.warning(f"  视觉识别[{label}] 异常: {e}")
        return "", ""


def _extract_image_file(file_path: Path, log: logging.Logger) -> str:
    """独立图片文件：走 bid_vision.vision_extract（Qwen-VL 主力，GLM-OCR 兜底）"""
    try:
        img_bytes = file_path.read_bytes()
    except OSError as e:
        log.warning(f"  图片读取失败: {file_path.name}: {e}")
        return f"[图片读取失败: {file_path.name}]"

    text, model = _extract_image_bytes_via_vision(img_bytes, file_path.name, log)
    if text:
        return f"[图片 - 视觉OCR:{model}]\n{text}"
    return f"[图片文件视觉识别失败: {file_path.name}]"


def _extract_embedded_images(
    file_path: Path, media_prefixes: tuple[str, ...], log: logging.Logger
) -> list[tuple[str, str, str]]:
    """从 docx/xlsx 等 ZIP 容器中抽取嵌入图片并做视觉 OCR

    返回 [(image_name, markdown, model_used), ...]
    """
    import zipfile

    results: list[tuple[str, str, str]] = []
    image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            media = [
                n for n in zf.namelist()
                if n.startswith(media_prefixes)
                and Path(n).suffix.lower() in image_exts
            ]
            if not media:
                return results
            log.info(f"  发现 {len(media)} 张嵌入图片，开始视觉 OCR...")
            for name in media:
                try:
                    img_bytes = zf.read(name)
                except OSError as e:
                    log.warning(f"    读取嵌入图 {name} 失败: {e}")
                    continue
                if len(img_bytes) < 2048:
                    # 过小的图片多半是装饰 icon / 公司 logo 碎片，跳过
                    log.debug(f"    跳过过小嵌入图: {name} ({len(img_bytes)} 字节)")
                    continue
                text, model = _extract_image_bytes_via_vision(
                    img_bytes, f"{file_path.name}:{Path(name).name}", log
                )
                if text:
                    results.append((Path(name).name, text, model))
    except zipfile.BadZipFile:
        log.warning(f"  {file_path.name} 不是有效 ZIP，跳过嵌入图抽取")
    except Exception as e:
        log.warning(f"  嵌入图抽取异常 {file_path.name}: {e}")
    return results


def _extract_dxf(file_path: Path, log: logging.Logger) -> str:
    """提取 DXF 文件中的文本实体"""
    try:
        import ezdxf
        doc = ezdxf.readfile(file_path)
        parts = []
        msp = doc.modelspace()
        for entity in msp:
            if entity.dxftype() == "TEXT":
                text = entity.dxf.text.strip()
                if text:
                    parts.append(text)
            elif entity.dxftype() == "MTEXT":
                text = entity.text.strip()
                if text:
                    parts.append(text)
        if parts:
            log.info(f"  ezdxf 提取成功: {file_path.name} ({len(parts)} 个文本实体)")
            return "\n".join(parts)
        return f"[DXF文件无可提取文本实体: {file_path.name}]"
    except ImportError:
        log.warning("  ezdxf 未安装，跳过 DXF 提取")
        return f"[DXF文件提取需要 ezdxf: {file_path.name}]"
    except Exception as e:
        log.warning(f"  ezdxf 提取失败: {file_path.name}: {e}")
        return f"[DXF文件提取失败: {file_path.name}]"


def _extract_dwg(file_path: Path, log: logging.Logger) -> str:
    """尝试将 DWG 转为 DXF 后提取文本"""
    import subprocess, tempfile
    try:
        result = subprocess.run(
            ["dwg2dxf", str(file_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            dxf_path = file_path.with_suffix(".dxf")
            if dxf_path.exists():
                text = _extract_dxf(dxf_path, log)
                return text
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"[DWG文件无法直接提取，建议转换为DXF: {file_path.name}]"


def get_file_brief(text: str) -> str:
    clean = text.replace("[第", "").replace("页]", "").strip()
    return clean[:200].replace("\n", " ")


def _zip_safe_extract(zf, dest: Path, log) -> list[Path]:
    """解一个 zip 到 dest,文件名按 cp437→gbk/utf-8 兜底解码,带 zip-slip 防护。"""
    out: list[Path] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        raw = info.filename
        for enc in ("gbk", "utf-8"):
            try:
                name = raw.encode("cp437").decode(enc)
                break
            except Exception:
                name = raw
        if ".." in Path(name).parts or Path(name).is_absolute():
            continue  # zip-slip 防护
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
        except Exception as e:
            log.warning(f"    解出 {name} 失败: {e}")
    return out


def unpack_archives(input_dir: Path, log: logging.Logger) -> None:
    """解开 .XZZ / .zip 招标包,内容落到 _unpacked/ 供后续扫描。

    西藏等地电子标书是 .XZZ(标准 zip),常见嵌套 GEFile.zip 内含
    「招标项目/招标文件.pdf」等真正的招标正文——不解包就只能拿到招标公告。
    幂等:_unpacked/<stem>/ 已存在则跳过。
    """
    import zipfile as _zip
    unpacked_root = input_dir / "_unpacked"
    archives = sorted(
        f for f in input_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in (".xzz", ".zip")
        and "_unpacked" not in f.relative_to(input_dir).parts
        and "extracted" not in f.relative_to(input_dir).parts
        and "output" not in f.relative_to(input_dir).parts
    )
    for arc in archives:
        dest = unpacked_root / arc.stem
        if dest.exists():
            log.info(f"  归档已解包,跳过: {arc.name}")
            continue
        try:
            with _zip.ZipFile(arc) as zf:
                files = _zip_safe_extract(zf, dest, log)
            log.info(f"  解包归档 {arc.name} → {len(files)} 个文件")
        except _zip.BadZipFile:
            log.warning(f"  {arc.name} 非有效 zip,跳过")
            continue
        except Exception as e:
            log.warning(f"  解包 {arc.name} 失败: {e}")
            continue
        # 递归解嵌套 zip(如 .XZZ 内的 GEFile.zip)
        for f in list(files):
            if f.suffix.lower() == ".zip":
                try:
                    with _zip.ZipFile(f) as zf2:
                        n2 = _zip_safe_extract(zf2, f.parent / f.stem, log)
                    log.info(f"    解包嵌套 {f.name} → {len(n2)} 个文件")
                except Exception as e:
                    log.warning(f"    嵌套 zip {f.name} 解包失败: {e}")


def main():
    if len(sys.argv) < 2:
        print("用法: python bid_extract.py /path/to/tender/")
        sys.exit(1)

    input_dir = Path(sys.argv[1]).resolve()
    if not input_dir.exists():
        print(f"目录不存在: {input_dir}")
        sys.exit(1)

    extracted_dir = input_dir / "extracted"
    extracted_dir.mkdir(exist_ok=True)

    log_path = extracted_dir / "run.log"
    log = make_script_logger("bid_extract", log_path)

    log.info("=" * 60)
    log.info(f"bid_extract 启动  输入目录: {input_dir}")
    log.info("=" * 60)

    pdf_vision_reports: dict[str, dict] = {}  # file_name -> report

    def _extract_pdf_with_report(p: Path) -> str:
        report: dict = {}
        text = extract_pdf(p, log, vision_report=report)
        pdf_vision_reports[p.name] = report
        return text

    supported = {
        ".pdf":  _extract_pdf_with_report,
        ".xlsx": lambda p: extract_excel(p, log),
        ".xls":  lambda p: extract_excel(p, log),
        ".docx": lambda p: extract_docx(p, log),
        ".doc":  lambda p: extract_docx(p, log),
        ".txt":  lambda p: extract_txt(p, log),
        ".md":   lambda p: extract_txt(p, log),
    }

    image_types = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    cad_types = {".dwg", ".dxf", ".dwf"}

    manifest_lines = [
        "# 文件清单",
        f"提取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"源目录: {input_dir}",
        "",
        "## 已提取文件",
        "",
    ]

    extracted_count = 0
    failed_count = 0

    # 先解开 .XZZ / zip 招标包(内含真正的招标正文,如 GEFile.zip 里的招标文件.pdf)
    unpack_archives(input_dir, log)

    # 递归扫描所有子目录；跳过已生成的 extracted/ 和 output/ 避免自吞
    skip_dirs = {"extracted", "output"}
    all_files = sorted([
        f for f in input_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in supported
        and not any(part in skip_dirs for part in f.relative_to(input_dir).parts)
    ])

    if not all_files:
        log.error(f"未找到可提取的文件（支持: {', '.join(supported.keys())}）")
        sys.exit(1)

    log.info(f"找到 {len(all_files)} 个文件，开始提取...")

    # 增量提取缓存:源文件 md5 未变且 txt 已在 → 跳过重提取(--force 全量重提)
    force = "--force" in sys.argv
    cache_path = extracted_dir / ".extract_cache.json"
    cache_files: dict[str, str] = {}
    if not force and cache_path.exists():
        try:
            cdata = json.loads(cache_path.read_text(encoding="utf-8"))
            if cdata.get("_version") == _EXTRACT_CACHE_VERSION:
                cache_files = cdata.get("files", {})
        except Exception:
            cache_files = {}
    new_cache_files: dict[str, str] = {}
    skipped_count = 0

    used_names = set()
    for file_path in all_files:
        suffix = file_path.suffix.lower()
        extractor = supported.get(suffix)
        if not extractor:
            continue

        rel = file_path.relative_to(input_dir)
        # 子目录中的文件：用 "子目录__文件名.txt" 避免同名冲突
        if len(rel.parts) > 1:
            flat_stem = "__".join(rel.with_suffix("").parts)
        else:
            flat_stem = file_path.stem
        # 再保险一次去重
        out_stem = flat_stem
        n = 1
        while out_stem in used_names:
            n += 1
            out_stem = f"{flat_stem}_{n}"
        used_names.add(out_stem)
        out_path = extracted_dir / f"{out_stem}.txt"

        # 增量:源文件未变且 txt 仍在 → 复用,跳过重提取
        cur_md5 = _file_md5(file_path)
        if (not force and cache_files.get(str(rel)) == cur_md5
                and out_path.exists()):
            char_count = len(out_path.read_text(encoding="utf-8", errors="ignore"))
            manifest_lines.append(f"- **{rel}** → `{out_path.name}`(增量复用,源未变更)")
            manifest_lines.append(f"  - 字符数: {char_count:,}")
            manifest_lines.append("")
            new_cache_files[str(rel)] = cur_md5
            extracted_count += 1
            skipped_count += 1
            log.info(f"  增量跳过(未变更): {rel}")
            continue

        log.info(f"提取: {rel}")
        try:
            text = extractor(file_path)
            out_path.write_text(text, encoding="utf-8")
            new_cache_files[str(rel)] = cur_md5

            char_count = len(text)
            brief = get_file_brief(text)
            manifest_lines.append(f"- **{rel}** → `{out_path.name}`")
            manifest_lines.append(f"  - 字符数: {char_count:,}")
            manifest_lines.append(f"  - 内容预览: {brief}...")
            # PDF 视觉 OCR 报告
            vreport = pdf_vision_reports.get(file_path.name)
            if vreport and vreport.get("vision_pages"):
                vp = vreport["vision_pages"]
                page_nums = ",".join(str(r["page"]) for r in vp[:20])
                more = "" if len(vp) <= 20 else f"...(共{len(vp)}页)"
                cat_agg: dict[str, int] = {}
                for r in vp:
                    for c in r["categories"]:
                        cat_agg[c] = cat_agg.get(c, 0) + 1
                cats_str = ", ".join(f"{k}×{v}" for k, v in sorted(cat_agg.items()))
                manifest_lines.append(
                    f"  - 视觉OCR: {len(vp)}/{vreport.get('total_pages', '?')} 页，触发类别: {cats_str or '扫描页'}"
                )
                manifest_lines.append(f"  - 视觉页码: {page_nums}{more}")
            manifest_lines.append("")
            log.info(f"  已保存: {out_path.name}  字符数: {char_count:,}")
            extracted_count += 1
        except Exception as e:
            log.error(f"  提取失败: {rel}: {e}", exc_info=True)
            manifest_lines.append(f"- **{rel}** ⚠ 提取失败: {e}")
            manifest_lines.append("")
            failed_count += 1

    # 写增量缓存(仅含本轮成功处理的文件)
    try:
        cache_path.write_text(
            json.dumps({"_version": _EXTRACT_CACHE_VERSION, "files": new_cache_files},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"  增量缓存写入失败: {e}")
    if skipped_count:
        log.info(f"  增量:{skipped_count}/{len(all_files)} 个文件未变更已跳过"
                 f"(--force 可强制全量重提)")

    # === 提取图片文件（使用 Vision LLM） ===
    image_files = [
        f for f in input_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in image_types
        and not any(part in skip_dirs for part in f.relative_to(input_dir).parts)
    ]

    if image_files:
        manifest_lines.extend(["## 图片文件（Vision LLM 提取）", ""])
        for f in sorted(image_files):
            rel = f.relative_to(input_dir)
            log.info(f"提取图片: {rel}")
            try:
                text = _extract_image_file(f, log)
                # 生成输出文件名
                if len(rel.parts) > 1:
                    flat_stem = "__".join(rel.with_suffix("").parts)
                else:
                    flat_stem = f.stem
                out_stem = flat_stem
                n = 1
                while out_stem in used_names:
                    n += 1
                    out_stem = f"{flat_stem}_{n}"
                used_names.add(out_stem)

                out_path = extracted_dir / f"{out_stem}.txt"
                out_path.write_text(text, encoding="utf-8")
                char_count = len(text)
                brief = get_file_brief(text)
                manifest_lines.append(f"- **{rel}** → `{out_path.name}`")
                manifest_lines.append(f"  - 提取方式: Vision LLM")
                manifest_lines.append(f"  - 字符数: {char_count:,}")
                manifest_lines.append(f"  - 内容预览: {brief}...")
                manifest_lines.append("")
                log.info(f"  已保存: {out_path.name}  字符数: {char_count:,}")
                extracted_count += 1
            except Exception as e:
                log.error(f"  图片提取失败: {rel}: {e}", exc_info=True)
                manifest_lines.append(f"- **{rel}** ⚠ 提取失败: {e}")
                manifest_lines.append("")
                failed_count += 1

    # === 提取 CAD 文件 ===
    cad_files = [
        f for f in input_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in cad_types
        and not any(part in skip_dirs for part in f.relative_to(input_dir).parts)
    ]

    if cad_files:
        manifest_lines.extend(["## CAD 文件", ""])
        for f in sorted(cad_files):
            rel = f.relative_to(input_dir)
            log.info(f"提取 CAD: {rel}")
            try:
                if f.suffix.lower() == ".dxf":
                    text = _extract_dxf(f, log)
                elif f.suffix.lower() == ".dwg":
                    text = _extract_dwg(f, log)
                else:
                    text = f"[不支持的 CAD 格式: {file_path.name}]"

                # 生成输出文件名
                if len(rel.parts) > 1:
                    flat_stem = "__".join(rel.with_suffix("").parts)
                else:
                    flat_stem = f.stem
                out_stem = flat_stem
                n = 1
                while out_stem in used_names:
                    n += 1
                    out_stem = f"{flat_stem}_{n}"
                used_names.add(out_stem)

                out_path = extracted_dir / f"{out_stem}.txt"
                out_path.write_text(text, encoding="utf-8")
                char_count = len(text)
                manifest_lines.append(f"- **{rel}** → `{out_path.name}`")
                manifest_lines.append(f"  - 字符数: {char_count:,}")
                manifest_lines.append("")
                log.info(f"  已保存: {out_path.name}  字符数: {char_count:,}")
                extracted_count += 1
            except Exception as e:
                log.error(f"  CAD 提取失败: {rel}: {e}", exc_info=True)
                manifest_lines.append(f"- **{rel}** ⚠ 提取失败: {e}")
                manifest_lines.append("")
                failed_count += 1

    # 汇总 PDF 视觉 OCR 统计
    total_vision_pages = sum(len(r.get("vision_pages", [])) for r in pdf_vision_reports.values())
    all_cats: dict[str, int] = {}
    for r in pdf_vision_reports.values():
        for vp in r.get("vision_pages", []):
            for c in vp["categories"]:
                all_cats[c] = all_cats.get(c, 0) + 1

    manifest_lines.extend([
        "## 提取统计",
        f"- 成功提取: {extracted_count} 个文件",
        f"- 提取失败: {failed_count} 个文件",
        f"- 图片文件（Vision LLM）: {len(image_files)} 个",
        f"- CAD 文件: {len(cad_files)} 个",
        f"- 视觉 OCR 触发页数: {total_vision_pages}",
    ])
    if all_cats:
        cat_str = ", ".join(f"{k}×{v}" for k, v in sorted(all_cats.items(), key=lambda x: -x[1]))
        manifest_lines.append(f"- 视觉 OCR 命中类别: {cat_str}")
    manifest_lines.extend([
        "",
        "---",
        "提取完成，请读取上述各 .txt 文件建立项目认知。",
        "视觉 OCR 页面内容比 pdfplumber 更可信（尤其是评分表、废标条款、暗标格式要求），",
        "Step 2 分析时应加权信任带 [视觉OCR:...] 标记的段落。",
    ])

    manifest_path = extracted_dir / "manifest.txt"
    manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")

    # 同时输出结构化视觉报告（供调试/后续分析）
    if pdf_vision_reports:
        import json as _json
        vision_json_path = extracted_dir / "vision_report.json"
        vision_json_path.write_text(
            _json.dumps(pdf_vision_reports, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"视觉报告: {vision_json_path}")

    log.info("=" * 60)
    log.info(
        f"提取完成  成功: {extracted_count}  失败: {failed_count}  "
        f"图片: {len(image_files)}  CAD: {len(cad_files)}  视觉OCR页: {total_vision_pages}"
    )
    log.info(f"日志文件: {log_path}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
