#!/usr/bin/env python3
"""bid_verify.py — 组装产物验证门(Step 4.5)。

在 bid_assemble 之后运行,对最终 DOCX 做硬核对——"脚本成功 N 份" ≠ 功能正确,
本步独立打开产物核对肉眼可验特征,不通过就 fail loud。

检查项:
  单份  — 封面是否存在;正文字体/字号;页数 ≤ 硬顶。
  多变体 — 三家正文字体/行距/页边距/图表 PNG 是否真差异化(反串标);
           全相同 = 反串标失效。
  日志  — 扫 assemble 日志的危险 WARNING/ERROR(format_set 未定义/渲染失败/KeyError)。

退出码:0 通过 / 1 警告(人工复核)/ 2 硬失败(必须修)。
产物:output/verify_report.md

用法:
  python bid_verify.py <tender_dir>            # 自动判定单份/多变体
  python bid_verify.py <tender_dir> --no-pages # 跳过 Word 页数实测(快)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bid_common import make_script_logger

# 危险日志关键词:出现即说明某步半失败(产出了但可能是错的)
_DANGER_LOG_PATTERNS = (
    "format_set", "未定义", "渲染失败", "已剥离", "KeyError",
    "Traceback", "JSON 解析失败", "回退到原 md",
)


# ─── docx 内核读取 ────────────────────────────────────────────────

def _docx_member(docx: Path, member: str) -> str:
    """读 docx(zip)内某 xml 成员,失败返回空串。"""
    try:
        with zipfile.ZipFile(docx) as z:
            return z.read(member).decode("utf-8", "ignore")
    except Exception:
        return ""


_COVER_TITLE_KW = ("施工组织设计", "监理服务方案", "监理大纲", "监理规划",
                   "服务方案", "技术标", "技术方案", "投标文件")


def cover_present(docx: Path, project_kw: str = "") -> bool:
    """文档开头是否有封面(前若干文本含文档类型标题关键词,兼容监理/服务等非施工标)。"""
    x = _docx_member(docx, "word/document.xml")
    texts = [t for t in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", x) if t.strip()]
    head = "".join(texts[:10]).replace(" ", "")
    return any(kw in head for kw in _COVER_TITLE_KW)


def _normal_style_block(styles_xml: str) -> str:
    """返回正文(Normal / 默认段落)样式 xml 块。

    不靠 w:styleId="Normal" —— Word 重存 docx 会把 styleId 改成数字编号。
    改按 <w:name w:val="Normal"> 定位;再退到 w:default="1" 的段落样式。
    """
    blocks = [m.group(0) for m in
              re.finditer(r'<w:style\b[^>]*>.*?</w:style>', styles_xml, re.DOTALL)]
    for blk in blocks:
        if 'w:type="paragraph"' in blk and re.search(r'<w:name w:val="Normal"', blk):
            return blk
    for blk in blocks:
        if 'w:type="paragraph"' in blk and 'w:default="1"' in blk:
            return blk
    return ""


def _is_lang_code(v: str) -> bool:
    """zh-CN / ja-JP / en-US 等是 themeFontLang 的语言码,不是字体名。"""
    return bool(re.match(r"^[a-z]{2}-[A-Z]{2}$", v))


def body_font(docx: Path) -> str:
    """正文(Normal 样式)中文字体。"""
    blk = _normal_style_block(_docx_member(docx, "word/styles.xml"))
    if blk:
        ea = re.search(r'<w:rFonts[^>]*w:eastAsia="([^"]+)"', blk)
        if ea:
            return ea.group(1)
    # 兜底:document.xml 里出现最多的 eastAsia(排除语言码)
    x = _docx_member(docx, "word/document.xml")
    c = Counter(v for v in re.findall(r'w:eastAsia="([^"]+)"', x)
                if not _is_lang_code(v))
    return c.most_common(1)[0][0] if c else "?"


def line_spacing(docx: Path) -> str:
    """正文行距(Normal 样式 spacing 的 line/lineRule)。"""
    blk = _normal_style_block(_docx_member(docx, "word/styles.xml"))
    if blk:
        sp = re.search(r'<w:spacing ([^/]+)/>', blk)
        if sp:
            ln = re.search(r'w:line="(\d+)"', sp.group(1))
            rule = re.search(r'w:lineRule="(\w+)"', sp.group(1))
            return f"{ln.group(1) if ln else '?'}/{rule.group(1) if rule else '?'}"
    return "?"


def margins(docx: Path) -> str:
    """页边距(sectPr pgMar,单位 twip)。"""
    x = _docx_member(docx, "word/document.xml")
    m = re.search(r"<w:pgMar ([^/]+)/>", x)
    if not m:
        return "?"
    vals = [re.search(rf'{k}="(\d+)"', m.group(1)) for k in ("top", "bottom", "left", "right")]
    return "/".join(v.group(1) if v else "?" for v in vals)


def page_count(docx: Path, log) -> int | None:
    """用 Microsoft Word 实测页数(AppleScript with timeout,等分页完成)。"""
    if sys.platform != "darwin" or not Path("/Applications/Microsoft Word.app").exists():
        return None
    script = f'''
tell application "Microsoft Word"
    open file name (POSIX file "{docx}" as string)
    delay 4
    set d to active document
    with timeout of 420 seconds
        set pg to (compute statistics d statistic (statistic pages))
    end timeout
    close d saving no
    return (pg as string)
end tell
'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=480)
    except subprocess.TimeoutExpired:
        log.warning(f"  页数实测超时: {docx.name}")
        return None
    if r.returncode != 0:
        log.warning(f"  页数实测失败: {r.stderr.strip()[:100]}")
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def chart_md5s(variant_dir: Path) -> dict[str, str]:
    """变体 charts 目录下 {图表名: md5}。"""
    out: dict[str, str] = {}
    cdir = variant_dir / "charts"
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.png")):
            out[p.name] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


# ─── 硬顶页数检测 ─────────────────────────────────────────────────

def detect_page_cap(extracted: Path) -> int | None:
    """从 chapter_plan.md / disqualification_rules.md 探测页数硬顶。

    优先取 disqualification_rules.md 的 page_hard_cap 字段(由 Step 2 据招标判档写入);
    否则正文正则兜底。正则匹配 2-3 位页数(招标常见'70页'/'100页'),同一文件命中多个时
    取最小值——招标常同时列大型(100)/中小型(70)两档,绑定约束是更严的那个。"""
    # 1. 优先读结构化字段 page_hard_cap(最可靠,已判档)
    dq = extracted / "disqualification_rules.md"
    if dq.exists():
        m = re.search(r"page_hard_cap:\s*(\d{2,3})", dq.read_text(encoding="utf-8"))
        if m:
            return int(m.group(1))
    # 2. 正文正则兜底:取最小(最严)的页数硬顶
    for name in ("chapter_plan.md", "disqualification_rules.md", "project_brain.md"):
        f = extracted / name
        if not f.exists():
            continue
        txt = f.read_text(encoding="utf-8")
        caps = [int(x) for x in re.findall(r"(?:硬顶|超过|控制在|≤|≦|不得超过)\s*(\d{2,3})\s*页", txt)]
        if caps:
            return min(caps)
    return None


def detect_cover_required(extracted: Path) -> bool:
    """从 disqualification_rules.md 读取 cover_required 设置，默认 True。"""
    dq = extracted / "disqualification_rules.md"
    if dq.exists():
        txt = dq.read_text(encoding="utf-8")
        m = re.search(r"cover_required:\s*(true|false)", txt)
        if m:
            return m.group(1) == "true"
    return True


def detect_mandated_format(extracted: Path) -> bool:
    """判断招标是否强制规定统一排版格式(字体/字号/行距/页边距/颜色)。

    强制格式项目下,三家变体的字体/行距/页边距【必须一致】(差异化会直接违反招标
    强制格式→废标),反串标改由内容(H2结构/模型/数据)+图表承担。此时不能再把
    '三家格式相同'判为反串标失效(那是误判)。

    判定信号(满足其一即视为强制格式):
    - disqualification_rules.md 的 format_sets 块只有 ≤1 个排版套(Step 2 因招标强制
      已收敛为单一套,三家共用);
    - format 块声明 color_allowed: false(招标连颜色都管死,属强制格式典型特征)。"""
    dq = extracted / "disqualification_rules.md"
    if not dq.exists():
        return False
    txt = dq.read_text(encoding="utf-8")
    m = re.search(r"```yaml\s*\nformat_sets:(.*?)```", txt, re.DOTALL)
    if m:
        # 顶格两空格的排版套 key(如 "  A:")计数
        set_keys = re.findall(r"^  (\w+):\s*$", m.group(1), re.MULTILINE)
        if len(set_keys) <= 1:
            return True
    if re.search(r"color_allowed:\s*false|no_color:\s*true", txt):
        return True
    return False


# ─── 验证主体 ────────────────────────────────────────────────────

def verify(tender_dir: Path, check_pages: bool, log) -> int:
    extracted = tender_dir / "extracted"
    output = tender_dir / "output"
    findings: list[tuple[str, str, str]] = []  # (severity, check, detail)

    def add(sev, check, detail):
        findings.append((sev, check, detail))

    # 1. 定位 DOCX
    batch_docx = sorted(output.glob("V[0-9][0-9]_*.docx"))
    # 单份产物:接受任意文档类型后缀(施工组织设计/监理服务方案/服务方案等),
    # 仅排除批量变体(V##_)与临时锁文件(~$)。
    single_docx = [p for p in output.glob("*.docx")
                   if not p.name.startswith("~$")
                   and not re.match(r"V\d{2}_", p.name)]
    docxs = batch_docx or single_docx
    if not docxs:
        add("fail", "产物存在性", f"{output} 下未找到任何组装 docx(*.docx,排除 V##_ 变体与临时文件)")
        _write_report(output, findings, log)
        return 2
    is_batch = len(batch_docx) >= 2
    log.info(f"发现 {len(docxs)} 份 DOCX(模式: {'多变体' if is_batch else '单份'})")

    # 2. 单份逐份检查
    cap = detect_page_cap(extracted)
    cover_required = detect_cover_required(extracted)
    per: dict[str, dict] = {}
    for dx in docxs:
        vid = dx.name.split("_")[0] if is_batch else "单份"
        info = {
            "cover": cover_present(dx),
            "font": body_font(dx),
            "spacing": line_spacing(dx),
            "margin": margins(dx),
            "pages": None,
        }
        if cover_required and not info["cover"]:
            add("fail", f"{vid} 封面", f"{dx.name} 开头未检出封面")
        elif not cover_required and not info["cover"]:
            pass  # 招标不要求封面，跳过
        if info["font"] in ("?", ""):
            add("warn", f"{vid} 字体", f"{dx.name} 正文字体无法识别")
        if check_pages:
            pg = page_count(dx, log)
            info["pages"] = pg
            if pg is None:
                add("warn", f"{vid} 页数", "Word 页数实测未取到")
            elif cap and pg > cap:
                add("fail", f"{vid} 页数", f"{pg} 页 > 招标硬顶 {cap} 页")
            elif cap:
                log.info(f"  {vid} 页数 {pg} ≤ 硬顶 {cap} ✓")
            else:
                log.info(f"  {vid} 页数 {pg}(未检出硬顶,仅记录)")
        per[vid] = info

    # 3. 多变体交叉:字体/行距/页边距/图表 是否真差异化
    if is_batch:
        mandated_fmt = detect_mandated_format(extracted)
        if mandated_fmt:
            log.info("  招标强制统一格式(format_sets≤1 或 color_allowed:false):"
                     "三家字体/行距/页边距【应当一致】,反串标改看内容+图表(见 bid_similarity)")
        for dim, key in (("正文字体", "font"), ("行距", "spacing"), ("页边距", "margin")):
            vals = {v: per[v][key] for v in per}
            uniq = set(vals.values())
            if mandated_fmt:
                # 强制格式下:一致=符合招标(正确);反而不一致才是格式违规风险
                if len(uniq) == 1:
                    log.info(f"  反串标-{dim}: 三家一致({next(iter(uniq))}) ✓ 符合招标强制格式")
                else:
                    add("warn", f"强制格式-{dim}",
                        f"招标强制统一格式,但三家{dim}不一致:{vals}——可能违反招标格式规定")
                continue
            if len(uniq) == 1:
                add("fail", f"反串标-{dim}", f"三家{dim}完全相同({uniq.pop()})——差异化失效")
            elif len(uniq) < len(vals):
                add("warn", f"反串标-{dim}",
                    f"{dim}有重复:{vals}")
            else:
                log.info(f"  反串标-{dim}: {vals} ✓ 三家不同")
        # 图表 PNG 跨变体 md5 碰撞
        md5_by_v = {v.name.split("_")[0]: chart_md5s(output / v.name.split("_")[0])
                    for v in batch_docx}
        collisions = []
        names = set()
        for mp in md5_by_v.values():
            names |= set(mp.keys())
        for nm in sorted(names):
            seen = [mp[nm] for mp in md5_by_v.values() if nm in mp]
            if len(seen) >= 2 and len(set(seen)) == 1:
                collisions.append(nm)
        if collisions:
            add("warn", "反串标-图表",
                f"{len(collisions)} 张同名图表跨变体 md5 完全相同: "
                + ", ".join(collisions[:5]))
        else:
            log.info(f"  反串标-图表: 跨变体无 md5 碰撞 ✓")

    # 4. 扫 assemble 日志的危险行
    log_files = list((output / "logs").glob("*assemble*.log")) if (output / "logs").is_dir() else []
    log_files += list(output.glob("*assemble*.log"))
    danger = []
    for lf in log_files:
        try:
            for ln in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ("WARNING" in ln or "ERROR" in ln) and \
                        any(p in ln for p in _DANGER_LOG_PATTERNS):
                    danger.append(ln.strip()[:140])
        except Exception:
            pass
    if danger:
        add("warn", "组装日志", f"{len(danger)} 条危险 WARNING/ERROR(样例): " + danger[0])

    _write_report(output, findings, log, per=per, cap=cap)

    n_fail = sum(1 for s, _, _ in findings if s == "fail")
    n_warn = sum(1 for s, _, _ in findings if s == "warn")
    log.info("=" * 60)
    if n_fail:
        log.error(f"验证门:硬失败 {n_fail} 项 / 警告 {n_warn} 项 — 退出码 2")
        return 2
    if n_warn:
        log.warning(f"验证门:警告 {n_warn} 项,需人工复核 — 退出码 1")
        return 1
    log.info("验证门:全部通过 ✓ — 退出码 0")
    return 0


def classify_finding(check: str, sev: str) -> str:
    """check 名 → 机器可读失败类别(供 --loop-mode 门禁分类,见 configs/bid_loop_policy.json)。"""
    if "封面" in check:
        return "cover_missing"
    if "页数" in check:
        return "page_over_cap" if sev == "fail" else "page_count_unknown"
    if check.startswith("强制格式"):
        return "mandated_format_drift"
    if check.startswith("反串标"):
        return "format_identical" if sev == "fail" else "format_similarity_warn"
    if check == "产物存在性":
        return "missing_artifact"
    if check == "组装日志":
        return "assemble_log_danger"
    return "other"


def _finding_variant(check: str) -> str:
    m = re.match(r"(V\d{2}|单份)\s", check)
    return m.group(1) if m else ""


def _write_report(output: Path, findings, log, per=None, cap=None) -> None:
    lines = ["# 组装产物验证报告", ""]
    n_fail = sum(1 for s, _, _ in findings if s == "fail")
    n_warn = sum(1 for s, _, _ in findings if s == "warn")
    payload = {
        "status": "fail" if n_fail else ("warn" if n_warn else "ok"),
        "n_fail": n_fail,
        "n_warn": n_warn,
        "page_cap": cap,
        "findings": [
            {"severity": s, "check": c, "detail": d,
             "class": classify_finding(c, s), "variant": _finding_variant(c)}
            for s, c, d in findings
        ],
        "per_variant": per or {},
    }
    (output / "verify_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    status = "❌ 硬失败" if n_fail else ("⚠ 有警告" if n_warn else "✅ 通过")
    lines += [f"**结论:** {status}(硬失败 {n_fail} / 警告 {n_warn})", ""]
    if per:
        lines += ["## 各变体核对", "",
                  "| 变体 | 封面 | 正文字体 | 行距 | 页边距 | 页数 |",
                  "|---|---|---|---|---|---|"]
        for vid, info in per.items():
            lines.append(
                f"| {vid} | {'✓' if info['cover'] else '✗'} | {info['font']} | "
                f"{info['spacing']} | {info['margin']} | {info['pages'] or '-'} |")
        if cap:
            lines.append(f"\n> 招标页数硬顶:{cap} 页")
        lines.append("")
    lines += ["## 发现项", ""]
    if findings:
        for sev, check, detail in findings:
            mark = {"fail": "❌", "warn": "⚠", "ok": "✓"}[sev]
            lines.append(f"- {mark} **{check}**:{detail}")
    else:
        lines.append("- 无")
    (output / "verify_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"报告: {output / 'verify_report.md'}")


def main() -> int:
    p = argparse.ArgumentParser(description="组装产物验证门")
    p.add_argument("tender_dir", help="投标根目录(含 output/)")
    p.add_argument("--no-pages", action="store_true",
                   help="跳过 Word 页数实测(快,但不校验是否超硬顶)")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir)
    log = make_script_logger("verify", tender_dir / "output" / "logs" / "verify.log")
    log.info("=" * 60)
    log.info(f"bid_verify  目录: {tender_dir}")
    log.info("=" * 60)
    return verify(tender_dir, check_pages=not args.no_pages, log=log)


if __name__ == "__main__":
    sys.exit(main())
