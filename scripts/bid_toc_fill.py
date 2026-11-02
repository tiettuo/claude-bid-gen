#!/usr/bin/env python3
"""回填静态目录的真实页码。

`bid_assemble.py` 在封面后插入静态目录（每条形如
``第一章 工程概况 …… @@TOCPG1@@``）。本脚本把 ``@@TOCPGn@@`` 占位符
换成真实页码。

为什么要静态目录而不是 TOC 域：
Word 对带 ``w:updateFields=true`` 的文档，打开时**必定**弹模态框
「该文档包含的域可能引用了其他文件。是否更新该文档中的这些域？」。
该提示是安全提示，**没有可关闭的 Word 偏好项**；它会让 bid_verify 的
AppleScript 页数实测卡住（2026-09 某西藏施组项目实测弹窗 + -609 连接无效）。
改成静态目录后：打开零弹窗、零人工 F9。

页码来源：用 Word 把 docx 导出为 PDF，再用 PyMuPDF 定位每个标题所在页。
占位符与最终页码同在右对齐制表位后，长度变化只影响点线、不增减行 →
回填不会改变分页，因此无需二次测量页数。

非 macOS / 无 Word / 无 PyMuPDF 时优雅降级：占位符替换为 "—" 并告警，
目录仍可读，只是没有页码。

用法:
  python3 bid_toc_fill.py <tender_dir>
  python3 bid_toc_fill.py <tender_dir> --variant V01      # 仅某变体
  python3 bid_toc_fill.py <tender_dir> --dry-run          # 只报告不落盘
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bid_common import make_script_logger  # noqa: E402

TOKEN_RE = re.compile(r"@@TOCPG(\d+)@@")
_P_START_RE = re.compile(r"<w:p(?:\s[^>]*)?>")


# ── docx 读写 ────────────────────────────────────────────────────────
def read_docx_xml(docx: Path) -> str:
    with zipfile.ZipFile(docx) as z:
        return z.read("word/document.xml").decode("utf-8")


def rewrite_docx(docx: Path, new_doc_xml: str) -> None:
    """就地替换 word/document.xml，其余条目原样保留。"""
    tmp = docx.with_suffix(".tocfill.tmp")
    with zipfile.ZipFile(docx) as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in zin.infolist():
            data = zin.read(it.filename)
            if it.filename == "word/document.xml":
                data = new_doc_xml.encode("utf-8")
            zout.writestr(it, data)
    shutil.move(str(tmp), str(docx))


def toc_entries_in_order(doc_xml: str) -> list[str]:
    """按出现顺序取出目录条目文本（制表符前的那一段 w:t）。"""
    # ★ 必须匹配 `<w:p>` 与 `<w:p ...>` 两种形态。旧写法 rfind("<w:p ") 只认带属性的
    #   段落,而 python-docx 生成的普通段落是 `<w:p>`,于是 rfind 一路回看到文档里
    #   最后一个带属性的段落(往往是节属性段),把从封面到该条目的**整篇目录粘成一条**
    #   → 112 条 entry 全部匹配失败 → locate_pages 返回全 None → 目录页码全落「—」。
    #   2026-09 某藏北乡镇派出所项目实测。
    p_starts = [m.start() for m in _P_START_RE.finditer(doc_xml)]
    entries = []
    for m in TOKEN_RE.finditer(doc_xml):
        # 从上一个段落开始回看，取该段落里除页码外的文本
        i = bisect.bisect_right(p_starts, m.start()) - 1
        seg_start = p_starts[i] if i >= 0 else max(0, m.start() - 4000)
        seg = doc_xml[seg_start:m.start()]
        texts = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", seg)
        entries.append(" ".join(t for t in texts if t.strip()))
    return entries


# ── Word → PDF ───────────────────────────────────────────────────────
def word_available() -> bool:
    return sys.platform == "darwin" and Path("/Applications/Microsoft Word.app").exists()



def _word_stage_paths(docx: Path) -> tuple[Path, Path]:
    """返回 (喂给 Word 的临时 docx, 期望的 PDF 输出路径)。

    两个路径必须落在**不同**目录,原因是 Word 沙盒的两条不对称约束(实测):
      - 读:只读例外覆盖整个磁盘,但**打不开自家容器 Data/ 根目录下的 docx**
        (报 -2700 "Word 未能打开文档"),所以临时 docx 要放回项目目录;
      - 写:只能写自家容器(powerbox 之外),所以 PDF 输出必须落容器,否则弹
        「授予文件访问权限」模态框并卡死后续 AppleScript。

    ★ 临时 docx 不能带点号前缀:Word 拒绝打开隐藏文件
      (`.bid_tocfill_xxx.docx` 报 -2700 "Word 未能打开文档",实测)。
      用 `_tocstage_<pid>.docx` 普通名,并在 finally 里删除(本步结束时它已不存在,
      不会影响后续 bid_verify 的 *.docx 扫描)。
    """
    out_dir = _word_writable_dir()
    tag = f"bid_tocfill_{os.getpid()}"
    return docx.parent / f"_tocstage_{os.getpid()}.docx", out_dir / f"{tag}.pdf"


def _word_writable_dir() -> Path:
    """给 Word 一个**它自己沙盒里**的 PDF 输出路径。

    Microsoft Word 是 App Sandbox 应用(entitlements 里 com.apple.security.app-sandbox
    + 绝对路径仅 temporary-exception read-only)。`save as ... format PDF` 写到
    /var/folders/.../T/ 或项目目录时,macOS 会弹 powerbox 模态框
    「授予文件访问权限 — 需要额外的权限才能访问下列文件: ...」。
    该弹窗没有 Word 偏好项可关,而且在人工点掉之前会**卡死后续所有 AppleScript**:
    `open` 静默失败 → bid_verify 页数实测 -1728、目录页码全部落成「—」。

    唯一不触发弹窗的可写位置是 Word 自己的容器目录(2026-09 某藏北乡镇派出所项目实测)。
    Python 侧不受沙盒限制,可直接读容器里的 PDF 做页码定位,用完删除。
    """
    container = Path.home() / "Library" / "Containers" / "com.microsoft.Word" / "Data"
    if container.is_dir():
        return container
    # 非 sandbox 版 Word / 非 macOS:退回系统临时目录(此时不会弹窗)
    import tempfile as _tf

    return Path(_tf.gettempdir())


def export_pdf(docx: Path, pdf: Path, log) -> bool:
    """用 Word 把 docx 另存为 PDF。返回是否成功。"""
    esc = str(docx.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    out = str(pdf).replace("\\", "\\\\").replace('"', '\\"')
    name = docx.name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Microsoft Word"
    set display alerts to none
    open POSIX file "{esc}"
    -- ★ 轮询等待文档真正就绪。固定 delay 10 在冷启动/文档较大时不够:
    --   此时 `document "{name}"` 抛 -1728 "The object you are trying to access
    --   does not exist",export_pdf 误判失败,目录页码整体落空。
    -- ★ 用布尔标志判断,不要写 `if d is missing value`:
    --   对文档对象引用做 `is not missing value` 比较会走对象自身的相等判定,
    --   稳定返回 false → 明明已定位到文档却报 "Word 未能打开文档",整步失败(实测)。
    set found to false
    repeat 90 times
        delay 1
        try
            set d to document "{name}"
            set found to true
            exit repeat
        end try
    end repeat
    if found is false then error "Word 未能打开文档: {name}"
    with timeout of 600 seconds
        save as d file name "{out}" file format format PDF
    end timeout
    -- ★ 关闭是 best-effort:Word for Mac 16.110 不暴露 `close` 命令
    --   (`close d saving no` / `tell d to close` 都报 -1708 "document 不理解 close")。
    --   导出已成功时 close 失败不得判为失败。
    try
        close d saving no
    end try
    return "SAVED"
end tell
'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=780)
    except subprocess.TimeoutExpired:
        log.warning(f"  PDF 导出超时: {docx.name}")
        return False
    if r.returncode != 0 or not pdf.exists():
        log.warning(f"  PDF 导出失败: {r.stderr.strip()[:160]}")
        return False
    return True


# ── PDF 页码定位 ─────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def locate_pages(pdf: Path, entries: list[str], log) -> list[int | None]:
    """返回每个目录条目在 PDF 中的页码（1-based）；定位不到为 None。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("  PyMuPDF 未安装，无法定位页码（pip install pymupdf）")
        return [None] * len(entries)

    doc = fitz.open(str(pdf))
    try:
        pages = [_norm(p.get_text()) for p in doc]
    finally:
        doc.close()

    # 目录页 = 含占位符的页；正文从最后一页目录之后开始
    toc_pages = [i for i, t in enumerate(pages) if "@@TOCPG" in t]
    body_start = (max(toc_pages) + 1) if toc_pages else 0

    out: list[int | None] = []
    cursor = body_start
    for raw in entries:
        key = _norm(raw)
        found = None
        if key:
            for i in range(cursor, len(pages)):
                if key in pages[i]:
                    found = i
                    break
        if found is None:  # 顺序搜索失败 → 全局兜底（仍按顺序优先）
            for i in range(body_start, len(pages)):
                if key and key in pages[i]:
                    found = i
                    break
        if found is not None:
            cursor = found  # 标题按文档顺序递增，锁住游标防误配
            out.append(found + 1)  # 转 1-based
        else:
            out.append(None)
    return out


def fill_one(docx: Path, log, dry_run: bool = False,
             restart_after_toc: bool = False) -> bool:
    doc_xml = read_docx_xml(docx)
    if not TOKEN_RE.search(doc_xml):
        log.info(f"  {docx.name}: 无目录页码占位符，跳过")
        return True
    entries = toc_entries_in_order(doc_xml)
    log.info(f"  {docx.name}: {len(entries)} 条目录待回填页码")

    pages: list[int | None] = [None] * len(entries)
    if word_available():
        # ★ 必须喂给 Word 一份**唯一命名**的副本。Word 里若已开着同名文档,
        #   `open POSIX file` 是 no-op → Word 继续用内存里的旧副本 → 导出的 PDF
        #   是过期内容,新增/改名的标题定位不到,目录页码落「—」
        #   (2026-09 某藏北乡镇派出所项目实测:新加的 10.2/13.4/13.5 三条全部落空)。
        #   唯一命名可保证每次都是全新打开,且无需依赖 AppleScript `close`
        #   (Word 16.110 不暴露 close 命令)。
        stage_docx, pdf = _word_stage_paths(docx)
        try:
            shutil.copy2(docx, stage_docx)
            if export_pdf(stage_docx, pdf, log):
                pages = locate_pages(pdf, entries, log)
        finally:
            for f in (stage_docx, pdf):
                try:
                    f.unlink()
                except OSError:
                    pass
    else:
        log.warning("  非 macOS 或未安装 Microsoft Word，跳过页码定位")

    if restart_after_toc:
        # 页码从目录后重起：页脚显示值 = pdf 页序 - 正文起始序
        base = min([p for p in pages if p], default=1) or 1
        pages = [None if p is None else p - base + 1 for p in pages]

    missing = sum(1 for p in pages if p is None)
    if missing == len(entries) and entries:
        # 一条都没定位到 = PDF 导出/定位整体失败(常见于 Word 沙盒弹窗或屏幕锁定)。
        # 此时**不要**把占位符改写成「—」:保留 @@TOCPGn@@ 才能修好根因后直接重跑本步,
        # 否则必须整篇重新 bid_assemble 才能拿回占位符。
        log.warning("    全部未定位,保留 @@TOCPGn@@ 占位符不落盘(修好 Word 后可直接重跑本步)")
        return False
    if missing:
        log.warning(f"    {missing}/{len(entries)} 条未定位到页码，以「—」占位")

    counter = {"i": 0}

    def sub(m):
        i = int(m.group(1)) - 1
        counter["i"] = i + 1
        p = pages[i] if 0 <= i < len(pages) else None
        return str(p) if p else "—"

    new_xml = TOKEN_RE.sub(sub, doc_xml)
    if dry_run:
        log.info("    --dry-run：不落盘")
        return True
    rewrite_docx(docx, new_xml)
    log.info(f"    页码已回填（缺 {missing} 条）")
    return True


def find_docx(output_dir: Path, variant: str | None) -> list[Path]:
    if not output_dir.is_dir():
        return []
    files = sorted(p for p in output_dir.glob("*.docx")
                   if not p.name.startswith("~$"))
    if variant:
        files = [p for p in files if p.name.upper().startswith(variant.upper())]
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="回填静态目录的真实页码")
    ap.add_argument("tender_dir")
    ap.add_argument("--variant", help="仅处理该变体（如 V01）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tender = Path(args.tender_dir).resolve()
    if not (tender / "extracted").is_dir():
        print(f"[fail] 不是 tender 目录（缺 extracted/）: {tender}")
        return 2
    out_dir = tender / "output"
    log = make_script_logger("toc_fill", out_dir / "logs" / "bid_toc_fill.log")
    log.info("=" * 60)
    log.info(f"bid_toc_fill 目录: {tender}")

    targets = find_docx(out_dir, args.variant)
    if not targets:
        log.info("  未找到 DOCX，跳过")
        return 0

    # 页码是否从目录后重起（决定是否要做页序↔显示值换算）
    restart = False
    rules = tender / "extracted" / "disqualification_rules.md"
    if rules.exists():
        m = re.search(r"page_number_restart_after_toc:\s*(true|false)",
                      rules.read_text(encoding="utf-8"))
        restart = bool(m and m.group(1) == "true")

    ok = 0
    for d in targets:
        try:
            if fill_one(d, log, dry_run=args.dry_run, restart_after_toc=restart):
                ok += 1
        except Exception as e:  # noqa: BLE001
            log.warning(f"  {d.name} 回填失败（不阻断）: {e}")
    log.info(f"完成：{ok}/{len(targets)} 份" + ("（dry-run）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
