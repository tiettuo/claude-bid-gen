#!/usr/bin/env python3
"""
bid_assemble.py - 投标文件 DOCX 组装工具
将 output/ 目录下的 .md 章节文件组装为 Word 文档
自动读取 disqualification_rules.md 中的格式要求
运行日志保存至 output/run.log

用法: python bid_assemble.py /path/to/tender/output/
"""
from __future__ import annotations

import copy
import sys
import re
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    make_script_logger,
    needs_blind_scan,
    BLIND_TEXT_REGEX,
    collect_chapter_files,
    read_project_name as _read_project_name,
    sanitize_decorative_chars,
    strip_prompt_leaks,
    scan_blind_bid_violations,
)
from bid_charts import preprocess_chapter_dir  # noqa: E402


def _find_pandoc() -> str:
    """定位 pandoc 可执行文件；优先 PATH，其次 ~/.local/bin。"""
    exe = shutil.which("pandoc")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / "pandoc"
    if fallback.exists() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise FileNotFoundError(
        "pandoc 未找到。请先安装：\n"
        "  方式1：从 https://github.com/jgm/pandoc/releases 下载对应平台二进制放入 ~/.local/bin/\n"
        "  方式2：brew install pandoc（如有 Homebrew）"
    )


# ─── 默认格式配置（当废标条款未指定时使用）─────────────────────────
DEFAULT_FORMAT = {
    "font_name_cn": "宋体",
    "font_name_en": "Times New Roman",
    "font_size_body": 12,       # 小四 = 12pt
    "font_size_h1": 16,         # 三号（H1 字号，可被招标文件覆盖）
    "font_name_h1": "黑体",      # H1 字体（可被招标文件覆盖；H2-H5 永远跟 body）
    "bold_h1": True,            # H1 是否加粗
    "align_h1": "center",       # H1 对齐：center / left / right
    "heading_chapter_prefix": True,  # H1 是否加"第N章 "前缀
    "font_size_h2": 14,         # 四号（保留以兼容老配置，新版 H2-H5 一律 = body_size）
    "font_size_h3": 12,         # 小四
    "line_spacing": 28,                  # multiple 时为倍数；exact/atLeast 时为 pt
    "line_spacing_rule": "exact",        # multiple / exact / atLeast / single
    "margin_top_cm": 2.6,
    "margin_bottom_cm": 2.5,
    "margin_left_cm": 2.8,              # 左留装订余量
    "margin_right_cm": 2.4,
    "page_size": "A4",          # A4 / A3 / Letter
    "page_width_cm": 21.0,      # A4 宽
    "page_height_cm": 29.7,     # A4 高
    "blind_bid": False,
    "no_color": False,
    "no_cover": False,
    "no_page_numbers": False,            # 显式"无页码/不编页"才置 True
    "no_company_info": False,
    "header_disabled": False,            # 暗标常见：禁止页眉
    "footer_page_number": False,         # 显式"页脚仅页码"才置 True
    "page_number_restart_after_toc": False,  # "从目录后第1页起编"
    "page_number_position": "center",    # center / right —— 变体反串标可改
    "page_number_format": "{n}",         # 页码模板,{n} 为页号占位(如 "第 {n} 页")
    "max_pages": 0,             # 0 表示不限
    "force_portrait": True,     # 禁止自动切横版
    "company_name": "",
    "forbidden_terms": [],      # 暗标禁用词（自动扫描）
}

# Phase 3.2(2026-05-11): PAGE_SIZE_CM 与 CN_SIZE_MAP 已收敛到 bid_common.TYPOGRAPHY
# 此处保留模块级引用以兼容旧代码,新代码请从 bid_common 引用
from bid_common import PAGE_SIZE_CM, CN_FONT_SIZE_MAP as CN_SIZE_MAP  # noqa: E402, F401


def _parse_structured_block(text: str) -> dict:
    """解析围栏代码块中的 key: value 参数（YAML-like）

    支持:
    - 普通 `key: value`
    - inline-dict `margin_cm: {top: 2.5, bottom: 2.5, left: 2.5, right: 2.5}`
      自动展开为 margin_top_cm / margin_bottom_cm / margin_left_cm / margin_right_cm
    - skip `_source` 后缀键(authoring_rules.md 的来源标注,不是真正配置)
    """
    params = {}
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[：:]\s*(.+)", line)
            if not m:
                continue
            key = m.group(1).strip()
            val_raw = m.group(2).strip().rstrip(",;")

            # 保留 _source 后缀键供 parse_format_rules 判断参数来源（如 heading_h1_size_pt_source: default）

            # inline-dict 处理: {top: 2.5, bottom: 2.5, left: 2.5, right: 2.5}
            if val_raw.startswith("{") and val_raw.endswith("}"):
                inner = val_raw[1:-1]
                # 简易解析 key: value 对,逗号分隔
                # 把 margin_cm: {top: 2.5,...} 展开成 margin_top_cm: 2.5 等
                pairs = re.findall(r"(\w+)\s*[：:]\s*([\w.\-]+)", inner)
                if pairs:
                    for inner_k, inner_v in pairs:
                        # margin_cm + top → margin_top_cm; margin_cm + bottom → margin_bottom_cm
                        if key.endswith("_cm"):
                            stem = key[:-3]  # margin
                            new_key = f"{stem}_{inner_k}_cm"
                        else:
                            new_key = f"{key}_{inner_k}"
                        params[new_key] = inner_v
                continue

            # 普通标量
            val = re.sub(r"\s*[（(#].*$", "", val_raw).strip()
            params[key] = val
    return params


def _coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "yes", "y", "是", "1", "on")


def _coerce_number(val, default=None):
    try:
        return float(re.search(r"-?\d+\.?\d*", str(val)).group())
    except Exception:
        return default


def _resolve_font_size(val, default=None):
    """将字号值转换为 pt 数值。支持中文名（四号/小四等）和阿拉伯数字（12/14pt 等）。"""
    val_str = str(val).strip()
    # 1) 精确匹配中文字号名
    if val_str in CN_SIZE_MAP:
        return CN_SIZE_MAP[val_str]
    # 2) 从含中文/混合文本中提取（如"四号正文" → 四号 → 14pt）
    for name, pt in CN_SIZE_MAP.items():
        if name in val_str:
            return pt
    # 3) 提取阿拉伯数字（如"14pt" → 14）
    return _coerce_number(val, default)


def _extract_margins(text: str, params: dict, fmt: dict, log, specified: set | None = None) -> None:
    """边距抽取，兼容多种写法：
    - "上边距：2.5cm / 下边距：2cm"  分体写法
    - "页边距：上 2.5 cm，其余 2 cm" 上+其余
    - "页边距：上下 2.5 cm，左右 2 cm"
    - "页边距：上 2.5 cm，下 2 cm，左 2 cm，右 2 cm"
    - "页边距：上下左右 2 cm"
    """
    keys_set = set()  # 已被本函数显式赋值的 key

    def _set(k, v):
        if k not in params and k not in keys_set:
            fmt[k] = float(v)
            keys_set.add(k)
            if specified is not None:
                specified.add(k)

    # 1) 分体单字段（兼容老写法）
    for key, pat in [
        ("margin_top_cm",    r"上边距[：:]\s*(\d+\.?\d*)\s*cm"),
        ("margin_bottom_cm", r"下边距[：:]\s*(\d+\.?\d*)\s*cm"),
        ("margin_left_cm",   r"左边距[：:]\s*(\d+\.?\d*)\s*cm"),
        ("margin_right_cm",  r"右边距[：:]\s*(\d+\.?\d*)\s*cm"),
    ]:
        mo = re.search(pat, text)
        if mo:
            _set(key, mo.group(1))

    # 2) "页边距：" 段落集中抽取（取第一处）
    seg_mo = re.search(r"页边距[：:]\s*([^\n。；;]+)", text)
    if seg_mo:
        seg = seg_mo.group(0)

        def grab(label):
            m = re.search(rf"{label}\s*(\d+\.?\d*)\s*cm", seg)
            return m.group(1) if m else None

        v_top = grab("上")
        v_bot = grab("下")
        v_left = grab("左")
        v_right = grab("右")
        v_lr = grab("左右")
        v_tb = grab("上下")
        v_ulrd = grab("上下左右") or grab("四周")
        v_other = grab("其余") or grab("其它")

        if v_ulrd:
            for k in ("margin_top_cm", "margin_bottom_cm", "margin_left_cm", "margin_right_cm"):
                _set(k, v_ulrd)
        if v_tb:
            _set("margin_top_cm", v_tb); _set("margin_bottom_cm", v_tb)
        if v_lr:
            _set("margin_left_cm", v_lr); _set("margin_right_cm", v_lr)
        if v_top:    _set("margin_top_cm", v_top)
        if v_bot:    _set("margin_bottom_cm", v_bot)
        if v_left:   _set("margin_left_cm", v_left)
        if v_right:  _set("margin_right_cm", v_right)
        if v_other:
            for k in ("margin_top_cm", "margin_bottom_cm", "margin_left_cm", "margin_right_cm"):
                _set(k, v_other)  # _set 内部已做"未赋值才设"判断，所以"其余"只填上明确条之外的

    if keys_set:
        log.info(
            f"  [文本] 边距 上{fmt['margin_top_cm']}/下{fmt['margin_bottom_cm']}"
            f"/左{fmt['margin_left_cm']}/右{fmt['margin_right_cm']} cm"
        )


def _apply_line_spacing(pf, fmt: dict) -> None:
    """把 fmt 里的行距（含 rule）应用到一个 paragraph_format。"""
    from docx.shared import Pt as _Pt
    from docx.enum.text import WD_LINE_SPACING
    rule = fmt.get("line_spacing_rule", "multiple")
    val = fmt["line_spacing"]
    # 防御：yaml 只给 line_spacing: 1.5 而忘写 _rule 时，常见意图是"1.5 倍行距"
    # 但默认 rule="exact" 会把 1.5 当 1.5pt 固定行高 → 字符堆叠鬼影。
    # 启发式：rule="exact" 但值 <= 4 时自动判定为倍数。
    if rule == "exact" and float(val) <= 4.0:
        rule = "multiple"
    if rule == "exact":
        pf.line_spacing = _Pt(val)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    elif rule == "atLeast":
        pf.line_spacing = _Pt(val)
        pf.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
    elif rule == "single":
        pf.line_spacing = 1.0
        pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    else:  # multiple
        pf.line_spacing = float(val)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE


def _extract_line_spacing(text: str, params: dict, fmt: dict, log, specified: set | None = None) -> None:
    """行距抽取，支持：
    - 倍数：'1.5 倍行距' / '行距：1.5' / '单倍'(=1) / '1.5倍' / '双倍'(=2) / '多倍：1.25'
    - 固定值：'行距：20 磅' / '固定值 22 磅' / '20pt'
    - 最小值：'最小值 18 磅'
    """
    if "line_spacing" in params or "line_spacing_rule" in params:
        return

    def _mark(*keys):
        if specified is not None:
            specified.update(keys)

    # 1) 关键词倍数
    if re.search(r"单倍\s*行距|行距\s*[：:]?\s*单倍", text):
        fmt["line_spacing"] = 1.0; fmt["line_spacing_rule"] = "single"
        _mark("line_spacing", "line_spacing_rule")
        log.info("  [文本] line_spacing = 单倍 (1.0)")
        return
    if re.search(r"双倍\s*行距|行距\s*[：:]?\s*双倍|2\s*倍\s*行距", text):
        fmt["line_spacing"] = 2.0; fmt["line_spacing_rule"] = "multiple"
        _mark("line_spacing", "line_spacing_rule")
        log.info("  [文本] line_spacing = 双倍 (2.0)")
        return

    # 2) 行距固定值/最小值（带"磅"或 pt）—— 优先匹配，避免被倍数规则误吞
    m = re.search(r"行距[^。\n]{0,12}?固定值?\s*[：:]?\s*(\d+\.?\d*)\s*(?:磅|pt)", text, re.I)
    if m:
        fmt["line_spacing"] = float(m.group(1)); fmt["line_spacing_rule"] = "exact"
        _mark("line_spacing", "line_spacing_rule")
        log.info(f"  [文本] line_spacing = 固定 {m.group(1)} 磅")
        return
    m = re.search(r"行距[^。\n]{0,12}?最小值?\s*[：:]?\s*(\d+\.?\d*)\s*(?:磅|pt)", text, re.I)
    if m:
        fmt["line_spacing"] = float(m.group(1)); fmt["line_spacing_rule"] = "atLeast"
        _mark("line_spacing", "line_spacing_rule")
        log.info(f"  [文本] line_spacing = 最小值 {m.group(1)} 磅")
        return

    # 3) 倍数（数字 + 倍 / 行距：1.5 / 行距 1.5）
    m = re.search(r"(\d+\.?\d*)\s*倍\s*行距|行距\s*[：:]?\s*(\d+\.?\d*)\s*倍?", text)
    if m:
        val = float(m.group(1) or m.group(2))
        if 0.5 <= val <= 4.0:  # sanity
            fmt["line_spacing"] = val; fmt["line_spacing_rule"] = "multiple"
            _mark("line_spacing", "line_spacing_rule")
            log.info(f"  [文本] line_spacing = {val} 倍")


def _extract_h1_style(text: str, params: dict, fmt: dict, log, specified: set | None = None) -> None:
    """章节标题（H1）样式抽取。
    匹配形如：
      章节标题：三号黑体加粗居中
      标题：四号 仿宋 加粗 居左
    每一项独立匹配，未命中保留默认。
    """
    # 抓"章节标题：...""标题：..."后面到句号/换行的整段
    seg = None
    for pat in (
        r"章节?标题[：:]\s*([^\n。；;]+)",
        r"(?<![一-龥])标题[：:]\s*([^\n。；;]+)",
    ):
        m = re.search(pat, text)
        if m:
            seg = m.group(1)
            break
    if not seg:
        return

    # 字号
    if "font_size_h1" not in params:
        for name, pt in CN_SIZE_MAP.items():
            if name in seg:
                fmt["font_size_h1"] = pt
                if specified is not None:
                    specified.add("font_size_h1")
                break

    # 字体
    if "font_name_h1" not in params:
        fm = re.search(r"(仿宋|宋体|黑体|楷体|微软雅黑|等线|方正小标宋)", seg)
        if fm:
            fmt["font_name_h1"] = fm.group(1)
            if specified is not None:
                specified.add("font_name_h1")

    # 加粗（否定优先，避免"不加粗"匹配到"加粗"）
    if "bold_h1" not in params:
        if re.search(r"不加粗|不粗体|非加粗|禁止加粗", seg):
            fmt["bold_h1"] = False
            if specified is not None:
                specified.add("bold_h1")
        elif re.search(r"加粗|粗体|bold", seg, re.I):
            fmt["bold_h1"] = True
            if specified is not None:
                specified.add("bold_h1")

    # 对齐
    if "align_h1" not in params:
        if re.search(r"居中|剧中|置中", seg):
            fmt["align_h1"] = "center"
            if specified is not None:
                specified.add("align_h1")
        elif re.search(r"居左|靠左|left", seg, re.I):
            fmt["align_h1"] = "left"
            if specified is not None:
                specified.add("align_h1")
        elif re.search(r"居右|靠右|right", seg, re.I):
            fmt["align_h1"] = "right"
            if specified is not None:
                specified.add("align_h1")

    log.info(
        f"  [文本] H1 样式: {fmt['font_name_h1']} {fmt['font_size_h1']}pt  "
        f"bold={fmt['bold_h1']} align={fmt['align_h1']}"
    )


def _extract_header_footer(text: str, params: dict, fmt: dict, log, specified: set | None = None) -> None:
    """页眉/页脚/页码 三类规则抽取。"""
    # 页眉禁止（兼容"页眉禁止" / "不得设置页眉" / "禁止添加页眉" 等双向语序）
    if "header_disabled" not in params:
        if (re.search(r"页眉[：:]?\s*(禁止|不[设允]|无|不得|不准)", text)
                or re.search(r"(禁止|不得|不准|不[设允置加])[^\n。]{0,6}页眉", text)):
            fmt["header_disabled"] = True
            if specified is not None:
                specified.add("header_disabled")
            log.info("  [文本] header_disabled = True (招标文件禁页眉)")

    # 页码 / 页脚仅页码
    if "no_page_numbers" not in params:
        if re.search(r"(不编页码|无页码|不[设标]页码|页码[：:]?\s*(禁止|无|不得))", text):
            fmt["no_page_numbers"] = True
            if specified is not None:
                specified.add("no_page_numbers")
            log.info("  [文本] no_page_numbers = True (招标文件禁页码)")

    if "footer_page_number" not in params:
        if re.search(r"(页脚.{0,8}(仅|只).{0,2}页码|页脚[：:][^\n。]*页码)", text):
            fmt["footer_page_number"] = True
            if specified is not None:
                specified.add("footer_page_number")
            log.info("  [文本] footer_page_number = True (页脚仅页码)")

    # 页码起始位置
    if "page_number_restart_after_toc" not in params:
        if re.search(r"(目录|目次).{0,6}(后|之后).{0,8}第\s*1\s*页|从.{0,6}第\s*1\s*页.{0,8}(开始|起).{0,8}编", text):
            fmt["page_number_restart_after_toc"] = True
            if specified is not None:
                specified.add("page_number_restart_after_toc")
            log.info("  [文本] page_number_restart_after_toc = True (页码从目录后重起)")


def _parse_format_sets(rules_text: str) -> dict[str, dict]:
    """从 disqualification_rules.md 解析 format_sets YAML 块。"""
    import yaml as _yaml_mod
    m = re.search(r"```yaml\s*\nformat_sets:(.*?)```", rules_text, re.DOTALL)
    if not m:
        return {}
    try:
        data = _yaml_mod.safe_load("format_sets:" + m.group(1))
        sets = data.get("format_sets", {}) if isinstance(data, dict) else {}
        return {str(k).upper(): v for k, v in sets.items() if isinstance(v, dict)}
    except Exception:
        return {}


def _apply_format_set(fmt: dict, fs: dict, log) -> dict:
    """将 format_set 的键值覆盖到 fmt 上。"""
    for key, val in fs.items():
        if key == "description":
            continue
        if key == "font_size_body":
            n = _resolve_font_size(str(val))
            if n: fmt["font_size_body"] = n; log.info(f"  [format_set] font_size_body={val}→{n}pt")
        elif key == "font_size_h1":
            n = _resolve_font_size(str(val))
            if n: fmt["font_size_h1"] = n; log.info(f"  [format_set] font_size_h1={val}→{n}pt")
        elif key == "line_spacing_rule":
            v = str(val).strip().lower()
            if v in ("multiple","exact","atleast","single"):
                fmt["line_spacing_rule"] = "atLeast" if v == "atleast" else v
        elif key == "line_spacing":
            fmt["line_spacing"] = float(val)
        elif key in ("margin_top_cm","margin_bottom_cm","margin_left_cm","margin_right_cm"):
            fmt[key] = float(val)
        elif key in ("font_name_cn","font_name_h1","page_size","bold_h1","align_h1","first_line_indent_chars"):
            fmt[key] = val
        elif key == "page_number_position":
            v = str(val).strip().lower()
            if v in ("center", "right"):
                fmt["page_number_position"] = v
        elif key == "page_number_format":
            fmt["page_number_format"] = str(val)
        elif key == "paper":
            ps = str(val).upper().strip()
            if ps in PAGE_SIZE_CM:
                fmt["page_size"] = ps
                fmt["page_width_cm"], fmt["page_height_cm"] = PAGE_SIZE_CM[ps]
    return fmt


def _normalize_rule_param_keys(params: dict, log: logging.Logger) -> None:
    """把招标 yaml 的通俗键名/正向布尔归一到脚本内部键名(就地修改 params)。

    ① 别名映射(font_family→font_name_cn 等);② 语义翻转(page_numbers:false
    → no_page_numbers:true,脚本内部统一用 no_* 反向布尔)。
    P3.3 从 parse_format_rules 抽出,行为不变。
    """
    # YAML 别名映射：招标 rules 常用通俗键名，这里统一映射到脚本内部键
    _ALIASES = {
        "font_family": "font_name_cn",
        "font_body": "font_name_cn",
        "font_chinese": "font_name_cn",          # Phase 2 authoring 用
        "font_size_pt": "font_size_body",
        "font_body_size": "font_size_body",
        "body_size_pt": "font_size_body",
        "body_size_name": "font_size_body",      # Phase 2 authoring "四号" 字号名,走 _resolve_font_size
        "line_spacing_pt": "line_spacing",
        "line_spacing_value": "line_spacing",    # Phase 2 authoring 用
        "paper_size": "page_size",
        "h1_font": "font_name_h1",
        "h1_size_pt": "font_size_h1",
        "h1_bold": "bold_h1",
        "h1_align": "align_h1",
        "heading_h1_size_pt": "font_size_h1",    # Phase 2 authoring 用
        "heading_h1_size_pt_source": "font_size_h1_source",
        "heading_h2_size_pt": "font_size_h2",
        "heading_h3_size_pt": "font_size_h3",
        "margin_top": "margin_top_cm",
        "margin_bottom": "margin_bottom_cm",
        "margin_left": "margin_left_cm",
        "margin_right": "margin_right_cm",
        # yaml 自然短名（margins 节下常见写法）
        "top_cm": "margin_top_cm",
        "bottom_cm": "margin_bottom_cm",
        "left_cm": "margin_left_cm",
        "right_cm": "margin_right_cm",
        "font_table_size": "font_size_table",
        "table_size_pt": "font_size_table",
        "table_font_size_pt": "font_size_table",  # Phase 2 authoring 用
        "first_line_indent": "first_line_indent_chars",
        "first_line_indent_chars": "first_line_indent_chars",  # 显式映射
        "paragraph_first_line_indent_cm": "first_line_indent_cm",
        "page_hard_cap": "max_pages",            # Phase 2 authoring 页数硬顶
    }
    for src, dst in _ALIASES.items():
        if src in params and dst not in params:
            params[dst] = params[src]
            log.info(f"  [别名] {src} → {dst} = {params[src]}")

    # 语义翻转键映射：招标 yaml 常写正向能力，脚本内部用 "no_*" 反向布尔
    # page_numbers: false  → no_page_numbers: True（不得编制页码）
    # cover_page: false    → no_cover: True
    # color: black_only    → no_color: True
    # color_allowed: false → no_color: True
    if "page_numbers" in params and "no_page_numbers" not in params:
        params["no_page_numbers"] = not _coerce_bool(params["page_numbers"])
    if "cover_page" in params and "no_cover" not in params:
        params["no_cover"] = not _coerce_bool(params["cover_page"])
    if "color_allowed" in params and "no_color" not in params:
        params["no_color"] = not _coerce_bool(params["color_allowed"])
    if "header_footer_company_info" in params and "no_company_info" not in params:
        params["no_company_info"] = not _coerce_bool(params["header_footer_company_info"])
    if "color" in params and "no_color" not in params:
        v = str(params["color"]).strip().lower()
        if v in ("black_only", "black", "bw", "monochrome", "黑白"):
            params["no_color"] = True
    if "company_identity" in params and "no_company_info" not in params:
        v = str(params["company_identity"]).strip().lower()
        if v in ("forbidden", "禁止", "no", "false"):
            params["no_company_info"] = True


def parse_format_rules(rules_path: Path, log: logging.Logger) -> dict:
    """从 disqualification_rules.md 解析格式要求

    优先级：结构化参数块（```key: value```）> 文本模糊匹配 > 默认值
    """
    fmt = DEFAULT_FORMAT.copy()
    specified = set()  # 由规则显式指定的 key，软层不得覆盖
    if not rules_path.exists():
        log.warning(f"废标条款文件不存在: {rules_path}，使用默认格式")
        fmt["_specified_keys"] = specified
        return fmt

    log.info(f"读取废标条款: {rules_path}")
    text = rules_path.read_text(encoding="utf-8")

    # ─── 第一轮：结构化参数块（权威来源） ─────────────────────
    params = _parse_structured_block(text)
    if params:
        log.info(f"  解析到结构化参数 {len(params)} 项: {list(params.keys())}")

    _normalize_rule_param_keys(params, log)

    bool_keys = ("blind_bid", "no_color", "no_cover", "no_page_numbers", "no_company_info",
                 "header_disabled", "footer_page_number", "page_number_restart_after_toc",
                 "bold_h1", "heading_chapter_prefix")
    for k in bool_keys:
        if k in params:
            fmt[k] = _coerce_bool(params[k])
            specified.add(k)
            log.info(f"  [结构化] {k} = {fmt[k]}")

    if "font_name_cn" in params:
        fmt["font_name_cn"] = params["font_name_cn"]
        specified.add("font_name_cn")
        log.info(f"  [结构化] font_name_cn = {fmt['font_name_cn']}")

    if "font_size_body" in params:
        n = _resolve_font_size(params["font_size_body"])
        if n:
            fmt["font_size_body"] = n
            specified.add("font_size_body")
            log.info(f"  [结构化] font_size_body = {params['font_size_body']} → {n}pt")

    if "font_size_table" in params:
        n = _resolve_font_size(params["font_size_table"])
        if n:
            fmt["font_size_table"] = n
            specified.add("font_size_table")
            log.info(f"  [结构化] font_size_table = {params['font_size_table']} → {n}pt")

    if "page_size" in params:
        ps = params["page_size"].upper().strip()
        if ps in PAGE_SIZE_CM:
            fmt["page_size"] = ps
            fmt["page_width_cm"], fmt["page_height_cm"] = PAGE_SIZE_CM[ps]
            specified.update(("page_size", "page_width_cm", "page_height_cm"))
            log.info(f"  [结构化] page_size = {ps} ({fmt['page_width_cm']}×{fmt['page_height_cm']}cm)")

    if "max_pages" in params:
        n = _coerce_number(params["max_pages"], 0)
        fmt["max_pages"] = int(n or 0)
        specified.add("max_pages")

    if "line_spacing" in params:
        raw_ls = str(params["line_spacing"])
        n = _coerce_number(raw_ls)
        if n is not None:
            fmt["line_spacing"] = n
            specified.add("line_spacing")
            if "line_spacing_rule" not in params and "倍" in raw_ls:
                fmt["line_spacing_rule"] = "multiple"
                specified.add("line_spacing_rule")
                log.info(f"  [自动] line_spacing 含'倍' → rule=multiple")
    if "line_spacing_rule" in params:
        v = params["line_spacing_rule"].strip().lower()
        if v in ("multiple", "exact", "atleast", "single"):
            fmt["line_spacing_rule"] = "atLeast" if v == "atleast" else v
            specified.add("line_spacing_rule")

    if "font_name_h1" in params:
        fmt["font_name_h1"] = params["font_name_h1"].strip()
        # _source: default 时标记为未指定，让后续 fallback 覆盖
        if params.get("font_name_h1_source", "").lower() != "default":
            specified.add("font_name_h1")
    if "font_size_h1" in params:
        n = _resolve_font_size(params["font_size_h1"])
        if n:
            fmt["font_size_h1"] = n
            # _source: default 时标记为未指定，让后续 fallback 覆盖
            if params.get("font_size_h1_source", "").lower() != "default":
                specified.add("font_size_h1")
    if "align_h1" in params:
        v = params["align_h1"].strip().lower()
        if v in ("center", "left", "right"):
            fmt["align_h1"] = v
            specified.add("align_h1")

    for pkey in ("margin_top_cm", "margin_bottom_cm", "margin_left_cm", "margin_right_cm"):
        if pkey in params:
            n = _coerce_number(params[pkey])
            if n is not None:
                fmt[pkey] = n
                specified.add(pkey)

    # 表格/边框线条:thickness 单位是 pt(招标常写 0.5/0.75/1.0)
    if "line_thickness_pt" in params:
        n = _coerce_number(params["line_thickness_pt"])
        if n is not None:
            fmt["line_thickness_pt"] = float(n)
            specified.add("line_thickness_pt")
            log.info(f"  [结构化] line_thickness_pt = {n}pt")

    # 颜色:支持中文颜色名 / 十六进制 / 关键词
    _COLOR_CN_TO_HEX = {
        "黑": "000000", "黑色": "000000",
        "白": "FFFFFF", "白色": "FFFFFF",
        "红": "FF0000", "红色": "FF0000",
        "蓝": "0000FF", "蓝色": "0000FF",
        "灰": "808080", "灰色": "808080",
        "深灰": "595959",
        "auto": "auto",
    }
    for ckey in ("line_color", "font_color"):
        if ckey in params:
            v = str(params[ckey]).strip()
            hex_val = _COLOR_CN_TO_HEX.get(v.lower(), _COLOR_CN_TO_HEX.get(v, None))
            if hex_val is None:
                # 看是否已是 hex
                if re.fullmatch(r"[0-9A-Fa-f]{6}", v):
                    hex_val = v.upper()
                elif v.startswith("#") and re.fullmatch(r"[0-9A-Fa-f]{6}", v[1:]):
                    hex_val = v[1:].upper()
            if hex_val:
                fmt[ckey] = hex_val
                specified.add(ckey)
                log.info(f"  [结构化] {ckey} = {v} → {hex_val}")

    # no_color_content / no_color 同义
    if "no_color_content" in params and "no_color" not in params:
        fmt["no_color"] = _coerce_bool(params["no_color_content"])
        specified.add("no_color")

    # ─── 第二轮：文本兜底匹配（仅对未显式设置的字段） ─────────────
    if "blind_bid" not in params:
        if re.search(BLIND_TEXT_REGEX, text):
            fmt["blind_bid"] = True
            specified.add("blind_bid")
            log.info("  [文本] 检测到暗标表述 → blind_bid=True")

    if "font_name_cn" not in params:
        m = re.search(r"(?:正文|文字|字体).{0,10}?(仿宋|宋体|黑体|楷体|微软雅黑)", text)
        if not m:
            m = re.search(r"(仿宋|宋体|黑体|楷体|微软雅黑)", text)
        if m:
            fmt["font_name_cn"] = m.group(1)
            specified.add("font_name_cn")
            log.info(f"  [文本] font_name_cn = {fmt['font_name_cn']}")

    if "font_size_body" not in params:
        ctx = re.search(
            r"(?:正文|文字|字号).{0,12}?(初号|小初|一号|小一|二号|小二|三号|小三|四号|小四|五号|小五)",
            text,
        )
        chosen = ctx.group(1) if ctx else None
        if not chosen:
            earliest = (None, 10**9)
            for name in CN_SIZE_MAP:
                idx = text.find(name)
                if 0 <= idx < earliest[1]:
                    earliest = (name, idx)
            chosen = earliest[0]
        if chosen:
            fmt["font_size_body"] = CN_SIZE_MAP[chosen]
            specified.add("font_size_body")
            log.info(f"  [文本] font_size_body = {chosen} = {CN_SIZE_MAP[chosen]}pt")

    if "page_size" not in params:
        if re.search(r"\bA4\b", text, re.I):
            fmt["page_size"] = "A4"
            fmt["page_width_cm"], fmt["page_height_cm"] = PAGE_SIZE_CM["A4"]
            specified.update(("page_size", "page_width_cm", "page_height_cm"))
        elif re.search(r"\bA3\b", text, re.I):
            fmt["page_size"] = "A3"
            fmt["page_width_cm"], fmt["page_height_cm"] = PAGE_SIZE_CM["A3"]
            specified.update(("page_size", "page_width_cm", "page_height_cm"))
        log.info(f"  [文本] page_size = {fmt['page_size']}")

    # 边距抽取（多语法兼容）
    _extract_margins(text, params, fmt, log, specified)

    # 行距抽取（倍数 / 固定值 / 最小值）
    _extract_line_spacing(text, params, fmt, log, specified)

    # H1 章节标题样式抽取（字体/字号/加粗/对齐）
    _extract_h1_style(text, params, fmt, log, specified)

    # 页眉/页脚/页码规则抽取
    _extract_header_footer(text, params, fmt, log, specified)

    # 招标文件未单独指定标题字号时，H1/H2/H3 跟正文一致
    if "font_size_h1" not in specified and "font_size_body" in specified:
        fmt["font_size_h1"] = fmt["font_size_body"]
        fmt["font_name_h1"] = fmt["font_name_cn"]
        log.info(f"  [自动] 未指定标题字号，H1 跟随正文 = {fmt['font_size_body']}pt {fmt['font_name_cn']}")

    # 招标文件指定了标题字号但未指定标题字体时，H1 字体回退到正文字体
    if "font_name_h1" not in specified and "font_name_cn" in specified and fmt.get("font_name_h1", "") != fmt["font_name_cn"]:
        old = fmt.get("font_name_h1", "黑体")
        fmt["font_name_h1"] = fmt["font_name_cn"]
        log.info(f"  [自动] 未指定标题字体，H1 字体回退到正文字体: {old} → {fmt['font_name_cn']}")

    # 封面字号——招标文件未显式指定时一律回落到 font_size_body
    # 防止"凭行业惯例硬编码大字号"违反招标格式硬约束（如本项目 10.16 条要求"全文宋体四号"）
    body_size = fmt.get("font_size_body", 14)
    if "font_size_cover_title" not in fmt:
        fmt["font_size_cover_title"] = body_size
    if "font_size_cover_project" not in fmt:
        fmt["font_size_cover_project"] = body_size
    if "font_size_cover_meta" not in fmt:
        fmt["font_size_cover_meta"] = body_size
    if "font_size_title_style" not in fmt:
        fmt["font_size_title_style"] = body_size

    # 暗标时自动启用关联项（如果未显式设置）—— 仅 no_cover / no_company_info / header_disabled
    # no_page_numbers 不再跟随 blind_bid 自动开（很多暗标恰恰要求"页脚仅页码"）
    if fmt["blind_bid"]:
        for k in ("no_cover", "no_company_info"):
            if k not in params:
                fmt[k] = True
                specified.add(k)
        if "header_disabled" not in params and not re.search(r"页眉.{0,8}(允许|保留|有)", text):
            fmt["header_disabled"] = True
            specified.add("header_disabled")

    fmt["_specified_keys"] = specified
    log.info(
        f"  格式汇总: font={fmt['font_name_cn']} {fmt['font_size_body']}pt  "
        f"page={fmt['page_size']}  blind={fmt['blind_bid']}  "
        f"no_cover={fmt['no_cover']}  no_page={fmt['no_page_numbers']}  "
        f"header_off={fmt['header_disabled']}  "
        f"foot_pgnum={fmt['footer_page_number']}  pgnum_after_toc={fmt['page_number_restart_after_toc']}  "
        f"specified_keys={sorted(specified)}"
    )
    return fmt


def read_tender_info(output_dir: Path, log: logging.Logger,
                     extracted_dir: Path | None = None) -> dict:
    """从 project_brain.md 读取项目信息（项目名走 bid_common，公司名本地解析）

    extracted_dir 显式传入时使用之;不传时按 `output_dir.parent / 'extracted'`
    回退(适合单份模式 output_dir=.../output/)。
    批量模式下 chapter_dir=.../output/V01/,parent 路径错误,必须显式传 extracted_dir。
    """
    if extracted_dir is None:
        extracted_dir = output_dir.parent / "extracted"
    info = {
        "project_name": _read_project_name(extracted_dir),
        "company_name": "",
    }
    log.info(f"  项目名称: {info['project_name']}")

    brain_path = extracted_dir / "project_brain.md"
    if brain_path.exists():
        text = brain_path.read_text(encoding="utf-8")
        info_section = re.search(
            r"##\s*一、?\s*项目基本信息[^\n]*\n(.*?)(?=\n##\s|$)",
            text, flags=re.DOTALL,
        )
        scope = info_section.group(1) if info_section else text[:1500]
        # 兼容 markdown 加粗写法 - **公司名称**:xxx
        m = re.search(
            r"^[\s\-\*]*\*{0,2}\s*公司名称\s*\*{0,2}\s*[：:]\s*(.+?)\s*$",
            scope, flags=re.MULTILINE,
        )
        if m:
            company = re.sub(r"\*{1,2}$", "", m.group(1).strip()).strip()
            info["company_name"] = company
            log.debug(f"  公司名称: {info['company_name']}")
    else:
        log.warning(f"project_brain.md 不存在: {brain_path}")

    return info


def setup_document(fmt: dict, log: logging.Logger):
    """创建并配置 Word 文档"""
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn

    log.debug("创建 Word 文档对象")
    doc = Document()

    for section in doc.sections:
        # 页面尺寸（默认 A4；A3 用于图表页等可单独切换）
        section.page_width = Cm(fmt["page_width_cm"])
        section.page_height = Cm(fmt["page_height_cm"])
        section.top_margin = Cm(fmt["margin_top_cm"])
        section.bottom_margin = Cm(fmt["margin_bottom_cm"])
        section.left_margin = Cm(fmt["margin_left_cm"])
        section.right_margin = Cm(fmt["margin_right_cm"])
    log.info(
        f"  页面: {fmt['page_size']} ({fmt['page_width_cm']}×{fmt['page_height_cm']}cm)  "
        f"边距 上{fmt['margin_top_cm']}/下{fmt['margin_bottom_cm']}/左{fmt['margin_left_cm']}/右{fmt['margin_right_cm']} cm"
    )

    style = doc.styles["Normal"]
    font = style.font
    font.name = fmt["font_name_en"]
    font.size = Pt(fmt["font_size_body"])
    style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), fmt["font_name_cn"])
    # 行距应用到 Normal 段落格式（按 rule）
    _apply_line_spacing(style.paragraph_format, fmt)
    log.debug(
        f"  默认字体: {fmt['font_name_cn']} {fmt['font_size_body']}pt  "
        f"行距: {fmt['line_spacing']} ({fmt['line_spacing_rule']})"
    )

    # 配置 Heading 1~4 样式字体：暗标时所有级别统一用正文字体，
    # 否则沿用默认（加粗由行内 run 控制）。东亚字体必须显式设置才能生效。
    heading_sizes = {
        "Heading 1": fmt["font_size_h1"],
        "Heading 2": fmt["font_size_h2"],
        "Heading 3": fmt["font_size_h3"],
        "Heading 4": fmt["font_size_body"],
    }
    for sname, sz in heading_sizes.items():
        try:
            s = doc.styles[sname]
            s.font.name = fmt["font_name_en"]
            s.font.size = Pt(sz)
            # 暗标要求统一字体（如仿宋），否则 Word 默认用黑体/等线
            cn_font = fmt["font_name_cn"] if fmt["blind_bid"] else fmt["font_name_cn"]
            s.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), cn_font)
            _apply_line_spacing(s.paragraph_format, fmt)
        except KeyError:
            log.debug(f"  样式不存在，跳过: {sname}")

    return doc


def _set_cn_font(run, font_name: str):
    """安全设置中文字体"""
    from docx.oxml.ns import qn
    rpr = run.element.get_or_add_rPr()
    rFonts = rpr.get_or_add_rFonts()
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hint"), "eastAsia")


def add_cover(doc, project_name: str, company_name: str, fmt: dict, log: logging.Logger):
    """添加封面"""
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    log.info("添加封面页")
    # 封面字号——一律读 fmt（默认 = font_size_body），禁止硬编码
    title_size = fmt["font_size_cover_title"]
    project_size = fmt["font_size_cover_project"]
    meta_size = fmt["font_size_cover_meta"]
    cover_title_font = fmt.get("font_name_cover_title") or fmt["font_name_cn"]

    for _ in range(8):
        doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("施  工  组  织  设  计")
    run.font.size = Pt(title_size)
    run.font.bold = True
    run.font.name = cover_title_font
    _set_cn_font(run, cover_title_font)

    doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(project_name)
    run.font.size = Pt(project_size)
    run.font.name = fmt["font_name_en"]
    _set_cn_font(run, fmt["font_name_cn"])

    for _ in range(6):
        doc.add_paragraph()

    if not fmt["blind_bid"] and company_name:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"投 标 人：{company_name}")
        run.font.size = Pt(meta_size)
        _set_cn_font(run, fmt["font_name_cn"])

    from datetime import datetime
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"编制日期：{datetime.now().strftime('%Y年%m月')}")
    run.font.size = Pt(meta_size)
    _set_cn_font(run, fmt["font_name_cn"])

    doc.add_page_break()
    log.debug("封面添加完成")


def build_reference_doc(fmt: dict, log: logging.Logger) -> Path:
    """构造 pandoc --reference-doc 用的样式模板 DOCX。
    所有样式（Normal/Heading 1-5/Title/List/Table）预先定义好中文字体、字号、
    行距、对齐、段前段后。pandoc 会在转换时套用这些样式。
    """
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    # 页面
    for section in doc.sections:
        section.page_width = Cm(fmt["page_width_cm"])
        section.page_height = Cm(fmt["page_height_cm"])
        section.top_margin = Cm(fmt["margin_top_cm"])
        section.bottom_margin = Cm(fmt["margin_bottom_cm"])
        section.left_margin = Cm(fmt["margin_left_cm"])
        section.right_margin = Cm(fmt["margin_right_cm"])

    def _style(name, cn_font, size_pt, bold=False, align=None, space_before=0, space_after=0):
        try:
            s = doc.styles[name]
        except KeyError:
            return
        s.font.name = fmt["font_name_en"]
        s.font.size = Pt(size_pt)
        s.font.bold = bold
        rPr = s.element.get_or_add_rPr()
        rFonts = rPr.get_or_add_rFonts()
        rFonts.set(qn("w:eastAsia"), cn_font)
        rFonts.set(qn("w:ascii"), fmt["font_name_en"])
        rFonts.set(qn("w:hAnsi"), fmt["font_name_en"])
        pf = s.paragraph_format
        _apply_line_spacing(pf, fmt)
        if align is not None:
            pf.alignment = align
        if space_before:
            pf.space_before = Pt(space_before)
        if space_after:
            pf.space_after = Pt(space_after)

    body_size = fmt["font_size_body"]  # 通常 14pt（四号）
    # Normal：正文—宋体四号，首行缩进 2 字
    _style("Normal", fmt["font_name_cn"], body_size, bold=False)
    try:
        doc.styles["Normal"].paragraph_format.first_line_indent = Pt(body_size * 2)
    except Exception:
        pass

    # 标题：H1 完全由 fmt 驱动（招标文件抽取）；H2-H5 永远跟正文，不读招标子标题字段
    align_map = {"center": WD_ALIGN_PARAGRAPH.CENTER, "left": WD_ALIGN_PARAGRAPH.LEFT, "right": WD_ALIGN_PARAGRAPH.RIGHT}
    _style("Heading 1", fmt["font_name_h1"], fmt["font_size_h1"], bold=fmt["bold_h1"],
           align=align_map.get(fmt["align_h1"], WD_ALIGN_PARAGRAPH.CENTER),
           space_before=12, space_after=6)
    _style("Heading 2", fmt["font_name_cn"], body_size, bold=False, space_before=6, space_after=3)
    _style("Heading 3", fmt["font_name_cn"], body_size, bold=False, space_before=3, space_after=0)
    _style("Heading 4", fmt["font_name_cn"], body_size, bold=False, space_before=0, space_after=0)
    _style("Heading 5", fmt["font_name_cn"], body_size, bold=False)
    _style("Title", fmt["font_name_h1"], fmt["font_size_title_style"], bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    # 表格样式基线
    try:
        ts = doc.styles["Table Grid"]
        ts.font.name = fmt["font_name_en"]
        ts.font.size = Pt(fmt.get("font_size_table") or body_size - 1)
        rPr = ts.element.get_or_add_rPr() if ts.element is not None else None
        if rPr is not None:
            rFonts = rPr.get_or_add_rFonts()
            rFonts.set(qn("w:eastAsia"), fmt["font_name_cn"])
    except KeyError:
        pass

    ref_path = Path(tempfile.mkstemp(suffix="_ref.docx", prefix="bid_")[1])
    doc.save(ref_path)
    log.info(f"  样式参考模板: {ref_path.name}")
    return ref_path


def run_pandoc(md_paths: list, ref_docx: Path, out_docx: Path,
               pandoc_exe: str, log: logging.Logger) -> None:
    """调用 pandoc 将多个 MD 合并转换为 DOCX。"""
    # 合并 MD，每章之间插入明确的分隔，使 pandoc 识别为独立一级结构
    merged = []
    for p in md_paths:
        merged.append(p.read_text(encoding="utf-8").rstrip())
        merged.append("")  # 空行分隔

    # 合并后的 md 写到第一个 md 的同目录（通常是 .assemble_tmp/），
    # 这样图片相对路径 ../charts/xxx.png 才能被 pandoc 正确解析
    if md_paths:
        merged_md = md_paths[0].parent / "_merged.md"
    else:
        merged_md = Path(tempfile.mkstemp(suffix=".md", prefix="bid_merged_")[1])
    merged_md.write_text("\n\n".join(merged), encoding="utf-8")

    # resource-path 覆盖 tmp 目录、章节目录（含 charts/）、当前目录
    resource_paths = [
        str(merged_md.parent),
        str(merged_md.parent.parent),
        ".",
    ]
    sep = ";" if sys.platform == "win32" else ":"

    cmd = [
        pandoc_exe,
        str(merged_md),
        "-f", "markdown+pipe_tables+grid_tables+raw_html-subscript-superscript",
        "-t", "docx",
        "--reference-doc", str(ref_docx),
        "--resource-path", sep.join(resource_paths),
        "-o", str(out_docx),
    ]
    log.info(f"  pandoc: {' '.join(str(c) for c in cmd[:4])} ... -o {out_docx.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"pandoc 失败: {result.stderr}")
        raise RuntimeError(f"pandoc 转换失败: {result.stderr[:500]}")
    try:
        merged_md.unlink()
    except Exception:
        pass


def _apply_doc_soft_layer(doc, fmt: dict, vs: dict, log) -> None:
    """对 DOCX 应用 variant_style 中的 doc_* 软层微调。
    仅修改 rules 未显式指定的参数（_specified_keys 外的部分）。
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt, Twips

    specified = fmt.get("_specified_keys", set())
    touched = []

    # ── 正文段落：首行缩进 / 段前段后 / 行距微调 ──
    indent_chars = vs.get("doc_first_line_indent_chars")
    sp_before = vs.get("doc_para_spacing_before_pt")
    sp_after = vs.get("doc_para_spacing_after_pt")
    ls_offset = vs.get("doc_line_spacing_offset", 0)

    line_spacing_locked = "line_spacing" in specified
    body_size = fmt["font_size_body"]

    HEADING_STYLES = {"Heading 1", "Heading 2", "Heading 3", "Heading 4",
                      "Heading 5", "Heading 6", "Title", "Subtitle"}

    for p in doc.paragraphs:
        sname = p.style.name if p.style else ""
        if sname in HEADING_STYLES:
            continue
        # 跳过表格内段落（已有独立格式）
        if p._p.getparent().tag == qn("w:tc"):
            continue
        # 跳过含图片的段落（行距已由 _fit_images 设为自动，不能覆盖）
        if p._p.findall(f".//{qn('wp:inline')}"):
            continue

        pPr = p._p.find(qn("w:pPr"))
        if pPr is None:
            pPr = OxmlElement("w:pPr")
            p._p.insert(0, pPr)

        # 首行缩进微调
        if indent_chars is not None:
            ind = pPr.find(qn("w:ind"))
            if ind is None:
                ind = OxmlElement("w:ind")
                pPr.append(ind)
            twips = int(indent_chars * body_size * 20)
            ind.set(qn("w:firstLine"), str(twips))
            ind.set(qn("w:firstLineChars"), str(int(indent_chars * 100)))

        # 段前段后
        spacing = pPr.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            pPr.append(spacing)
        if sp_before is not None:
            spacing.set(qn("w:before"), str(int(sp_before * 20)))
        if sp_after is not None:
            spacing.set(qn("w:after"), str(int(sp_after * 20)))

        # 行距微调（仅 rules 未锁定时）
        if not line_spacing_locked and ls_offset:
            rule = fmt.get("line_spacing_rule", "exact")
            base = fmt["line_spacing"]
            if rule == "multiple":
                adjusted = round(base + ls_offset, 3)
                spacing.set(qn("w:line"), str(int(adjusted * 240)))
                spacing.set(qn("w:lineRule"), "auto")
            elif rule in ("exact", "atLeast"):
                adjusted = round(base + ls_offset * 20, 1)
                spacing.set(qn("w:line"), str(int(adjusted * 20)))
                spacing.set(qn("w:lineRule"), rule if rule == "exact" else "atLeast")

    if indent_chars is not None:
        touched.append(f"indent={indent_chars:.2f}字")
    if sp_before is not None:
        touched.append(f"sp_before={sp_before}pt")
    if sp_after is not None:
        touched.append(f"sp_after={sp_after}pt")
    if not line_spacing_locked and ls_offset:
        touched.append(f"ls_offset={ls_offset:+.3f}")

    # ── H2/H3 段前间距 ──
    h2_sp = vs.get("doc_h2_space_before_pt")
    h3_sp = vs.get("doc_h3_space_before_pt")
    for p in doc.paragraphs:
        sname = p.style.name if p.style else ""
        sp_val = None
        if sname == "Heading 2" and h2_sp is not None:
            sp_val = h2_sp
        elif sname == "Heading 3" and h3_sp is not None:
            sp_val = h3_sp
        if sp_val is not None:
            pPr = p._p.find(qn("w:pPr"))
            if pPr is None:
                pPr = OxmlElement("w:pPr")
                p._p.insert(0, pPr)
            spacing = pPr.find(qn("w:spacing"))
            if spacing is None:
                spacing = OxmlElement("w:spacing")
                pPr.append(spacing)
            spacing.set(qn("w:before"), str(int(sp_val * 20)))
    if h2_sp is not None:
        touched.append(f"h2_sp={h2_sp}pt")
    if h3_sp is not None:
        touched.append(f"h3_sp={h3_sp}pt")

    # ── 表格：单元格内边距 / 边框粗细 ──
    cell_pad = vs.get("doc_table_cell_padding_twips")
    border_pt = vs.get("doc_table_border_pt")
    for tbl in doc.tables:
        if cell_pad is not None:
            tblPr = tbl._tbl.find(qn("w:tblPr"))
            if tblPr is not None:
                cm = tblPr.find(qn("w:tblCellMar"))
                if cm is None:
                    cm = OxmlElement("w:tblCellMar")
                    tblPr.append(cm)
                for edge in ("top", "bottom", "start", "end"):
                    el = cm.find(qn(f"w:{edge}"))
                    if el is None:
                        el = OxmlElement(f"w:{edge}")
                        cm.append(el)
                    el.set(qn("w:w"), str(cell_pad))
                    el.set(qn("w:type"), "dxa")

        if border_pt is not None:
            tblPr = tbl._tbl.find(qn("w:tblPr"))
            if tblPr is not None:
                borders = tblPr.find(qn("w:tblBorders"))
                if borders is not None:
                    sz_val = str(int(border_pt * 8))
                    for child in borders:
                        child.set(qn("w:sz"), sz_val)
    if cell_pad is not None:
        touched.append(f"cell_pad={cell_pad}twips")
    if border_pt is not None:
        touched.append(f"border={border_pt}pt")

    if touched:
        log.info(f"  文档软层微调: {', '.join(touched)}")


def _fit_images(doc, fmt: dict, log) -> None:
    """扫描文档中的图片，约束尺寸并为大图切换横版。

    规则：
    1. 图片宽度不超过版心宽度，等比缩放
    2. 缩放后高度仍超过版心高度 85% → 切横版（landscape）
    3. 横版结束后自动恢复竖版
    """
    from docx.shared import Cm, Emu
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    import copy

    page_w_cm = fmt.get("page_width_cm", 21.0)
    page_h_cm = fmt.get("page_height_cm", 29.7)
    ml = fmt.get("margin_left_cm", 2.8)
    mr = fmt.get("margin_right_cm", 2.4)
    mt = fmt.get("margin_top_cm", 2.6)
    mb = fmt.get("margin_bottom_cm", 2.5)

    content_w_portrait = Cm(page_w_cm - ml - mr)
    content_h_portrait = Cm(page_h_cm - mt - mb)
    content_w_landscape = Cm(page_h_cm - ml - mr)   # 横版：高变宽
    content_h_landscape = Cm(page_w_cm - mt - mb)    # 横版：宽变高

    threshold = 0.85
    img_count = 0
    landscape_count = 0

    body = doc.element.body

    # 先把所有 wp:anchor（浮动图片）转为 wp:inline（嵌入式），防止图片飘到文字后面
    for para in doc.paragraphs:
        for anchor in para._element.findall(f".//{qn('wp:anchor')}"):
            inline = OxmlElement("wp:inline")
            for child in list(anchor):
                anchor.remove(child)
                inline.append(child)
            for attr in ("cx", "cy"):
                extent_el = anchor.find(qn("wp:extent"))
                if extent_el is not None:
                    break
            extent_el = inline.find(qn("wp:extent"))
            if extent_el is None:
                extent_el = OxmlElement("wp:extent")
                extent_el.set("cx", anchor.get("cx", "0"))
                extent_el.set("cy", anchor.get("cy", "0"))
                inline.insert(0, extent_el)
            anchor.getparent().replace(anchor, inline)
            log.info("  浮动图片 → 嵌入式（防止文字遮盖）")

    paragraphs_with_images = []
    for para in doc.paragraphs:
        drawings = para._element.findall(f".//{qn('wp:inline')}")
        if drawings:
            for drawing in drawings:
                paragraphs_with_images.append((para, drawing))

    for para, drawing in paragraphs_with_images:
        extent = drawing.find(qn("wp:extent"))
        if extent is None:
            continue

        orig_w = int(extent.get("cx", 0))
        orig_h = int(extent.get("cy", 0))
        if orig_w == 0 or orig_h == 0:
            continue

        aspect = orig_h / orig_w
        img_count += 1

        # 先尝试竖版缩放
        new_w = min(orig_w, int(content_w_portrait))
        new_h = int(new_w * aspect)

        need_landscape = (not fmt.get("force_portrait")) and (new_h > int(content_h_portrait * threshold))

        if need_landscape:
            # 横版缩放
            new_w = min(orig_w, int(content_w_landscape))
            new_h = int(new_w * aspect)
            if new_h > int(content_h_landscape):
                new_h = int(content_h_landscape)
                new_w = int(new_h / aspect)

            # 在图片段落前插入分节符（连续型），设为横版
            para_elem = para._element

            # 创建横版的 sectPr
            sect_landscape = OxmlElement("w:sectPr")
            pgsz = OxmlElement("w:pgSz")
            pgsz.set(qn("w:w"), str(int(Cm(page_h_cm).twips)))   # 横版：高→宽
            pgsz.set(qn("w:h"), str(int(Cm(page_w_cm).twips)))   # 横版：宽→高
            pgsz.set(qn("w:orient"), "landscape")
            sect_landscape.append(pgsz)
            # 横版的边距（左右对调因为旋转）
            pgmar = OxmlElement("w:pgMar")
            pgmar.set(qn("w:top"), str(int(Cm(mt).twips)))
            pgmar.set(qn("w:bottom"), str(int(Cm(mb).twips)))
            pgmar.set(qn("w:left"), str(int(Cm(ml).twips)))
            pgmar.set(qn("w:right"), str(int(Cm(mr).twips)))
            sect_landscape.append(pgmar)

            # 在图片段落前插入一个空段落，把竖版→横版的分节符放在这个空段里
            pre_break = OxmlElement("w:p")
            pre_pPr = OxmlElement("w:pPr")
            pre_pPr.append(copy.deepcopy(sect_landscape))
            pre_break.append(pre_pPr)
            body.insert(list(body).index(para_elem), pre_break)

            # 在图片段落后插入一个空段落，把横版→竖版的分节符放在里面
            sect_portrait = OxmlElement("w:sectPr")
            pgsz2 = OxmlElement("w:pgSz")
            pgsz2.set(qn("w:w"), str(int(Cm(page_w_cm).twips)))
            pgsz2.set(qn("w:h"), str(int(Cm(page_h_cm).twips)))
            sect_portrait.append(pgsz2)
            pgmar2 = OxmlElement("w:pgMar")
            pgmar2.set(qn("w:top"), str(int(Cm(mt).twips)))
            pgmar2.set(qn("w:bottom"), str(int(Cm(mb).twips)))
            pgmar2.set(qn("w:left"), str(int(Cm(ml).twips)))
            pgmar2.set(qn("w:right"), str(int(Cm(mr).twips)))
            sect_portrait.append(pgmar2)

            post_break = OxmlElement("w:p")
            post_pPr = OxmlElement("w:pPr")
            post_pPr.append(sect_portrait)
            post_break.append(post_pPr)
            para_idx = list(body).index(para_elem)
            if para_idx + 1 < len(list(body)):
                body.insert(para_idx + 1, post_break)
            else:
                body.append(post_break)

            landscape_count += 1
        else:
            # 竖版：超宽则缩放
            if new_h > int(content_h_portrait):
                new_h = int(content_h_portrait)
                new_w = int(new_h / aspect)

        # 更新图片尺寸
        extent.set("cx", str(new_w))
        extent.set("cy", str(new_h))

        # 同步更新 a:xfrm 里的 ext（如果有）
        for ext_elem in drawing.findall(f".//{qn('a:ext')}"):
            if int(ext_elem.get("cx", 0)) > 0:
                ext_elem.set("cx", str(new_w))
                ext_elem.set("cy", str(new_h))

        # 图片段落：彻底脱离 Normal 样式（exact 28pt 会裁切图片）
        pPr = para._element.find(qn("w:pPr"))
        if pPr is None:
            pPr = OxmlElement("w:pPr")
            para._element.insert(0, pPr)

        # 删除 pStyle（CaptionedFigure 等 pandoc 样式在文档中未定义，
        # Word 回退到 Normal 的 exact 行距会裁切图片）
        old_pstyle = pPr.find(qn("w:pStyle"))
        if old_pstyle is not None:
            pPr.remove(old_pstyle)

        # 行距：删旧重建，最小值=图片高度，确保段落撑开
        old_sp = pPr.find(qn("w:spacing"))
        if old_sp is not None:
            pPr.remove(old_sp)
        img_h_twips = int(new_h / 914400 * 1440)  # EMU → twips
        spacing = OxmlElement("w:spacing")
        spacing.set(qn("w:line"), str(max(img_h_twips, 240)))
        spacing.set(qn("w:lineRule"), "atLeast")
        spacing.set(qn("w:before"), "120")
        spacing.set(qn("w:after"), "120")
        pPr.append(spacing)

        # 首行缩进归零（覆盖 Normal 样式的 firstLine=560）
        ind = pPr.find(qn("w:ind"))
        if ind is None:
            ind = OxmlElement("w:ind")
            pPr.append(ind)
        ind.set(qn("w:firstLine"), "0")
        ind.set(qn("w:firstLineChars"), "0")
        ind.set(qn("w:left"), "0")
        ind.set(qn("w:right"), "0")

        # 居中
        jc = pPr.find(qn("w:jc"))
        if jc is None:
            jc = OxmlElement("w:jc")
            pPr.append(jc)
        jc.set(qn("w:val"), "center")

    if img_count:
        log.info(f"  图片缩放: {img_count} 张, 其中 {landscape_count} 张切换为横版")


def _postproc_normalize_tables(doc, fmt: dict, body_size, log,
                                force_cn_font):
    """规范化 docx 内所有表格(列宽/边框/表头/单元格字号/对齐/跨页重复)。

    2026-05-11 Phase 3.1:从 postprocess_docx 内部抽出。
    force_cn_font 是 postprocess_docx 闭包内的字体规范函数,通过回调传入。
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    # 版心宽度 = 页宽 - 左右页边距
    page_w = fmt.get("page_width_cm", 21.0)
    margin_l = fmt.get("margin_left_cm", 3.17)
    margin_r = fmt.get("margin_right_cm", 3.17)
    usable_cm = max(page_w - margin_l - margin_r, 10.0)
    # OOXML dxa 单位:1 cm ≈ 567 dxa
    usable_dxa = int(usable_cm * 567)
    tbl_size = fmt.get("font_size_table") or max(body_size - 2, 10)

    def _set_or_add(parent, tag):
        el = parent.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            parent.append(el)
        return el

    # 边框 sz 是"八分之一磅"单位,fmt 里是 pt
    line_pt = float(fmt.get("line_thickness_pt", 0.5))
    line_sz = max(1, round(line_pt * 8))  # 0.5pt→4 / 0.75pt→6 / 1.0pt→8
    line_color_hex = str(fmt.get("line_color", "595959")).upper()

    def _set_table_borders(tbl):
        tblPr = tbl._tbl.find(qn("w:tblPr"))
        if tblPr is None:
            return
        borders = _set_or_add(tblPr, "w:tblBorders")
        for child in list(borders):
            borders.remove(child)
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            b = OxmlElement(f"w:{edge}")
            b.set(qn("w:val"), "single")
            b.set(qn("w:sz"), str(line_sz))
            b.set(qn("w:space"), "0")
            b.set(qn("w:color"), line_color_hex)
            borders.append(b)

    def _calc_col_widths(tbl, total_dxa):
        """按内容字符数比例分配列宽(中文算 2 宽度)。"""
        rows = tbl.rows
        if not rows:
            return []
        n_cols = len(rows[0].cells)
        if n_cols == 0:
            return []
        min_dxa = 800
        max_chars = [0] * n_cols
        for row in rows:
            for i, cell in enumerate(row.cells):
                if i >= n_cols:
                    break
                text = cell.text.strip()
                w = sum(2 if '一' <= c <= '鿿' or c in '，。、；：""''（）《》【】' else 1 for c in text)
                max_chars[i] = max(max_chars[i], w)
        floor = 4
        weights = [max(c, floor) for c in max_chars]
        total_w = sum(weights)
        widths = [max(min_dxa, int(total_dxa * w / total_w)) for w in weights]
        remainder = total_dxa - sum(widths)
        if remainder != 0 and widths:
            widths[widths.index(max(widths))] += remainder
        return widths

    def _set_col_widths(tbl, total_dxa):
        widths = _calc_col_widths(tbl, total_dxa)
        if not widths:
            return
        tblPr = tbl._tbl.find(qn("w:tblPr"))
        if tblPr is not None:
            tblW = _set_or_add(tblPr, "w:tblW")
            tblW.set(qn("w:w"), str(total_dxa))
            tblW.set(qn("w:type"), "dxa")
            layout = _set_or_add(tblPr, "w:tblLayout")
            layout.set(qn("w:type"), "fixed")
        tblGrid = tbl._tbl.find(qn("w:tblGrid"))
        if tblGrid is not None:
            for child in list(tblGrid):
                tblGrid.remove(child)
            for w in widths:
                gc = OxmlElement("w:gridCol")
                gc.set(qn("w:w"), str(w))
                tblGrid.append(gc)
        for row in tbl.rows:
            for i, cell in enumerate(row.cells):
                tcPr = cell._tc.get_or_add_tcPr()
                tcW = _set_or_add(tcPr, "w:tcW")
                tcW.set(qn("w:w"), str(widths[i] if i < len(widths) else widths[-1]))
                tcW.set(qn("w:type"), "dxa")

    def _mark_header_row(row):
        trPr = row._tr.find(qn("w:trPr"))
        if trPr is None:
            trPr = OxmlElement("w:trPr")
            row._tr.insert(0, trPr)
        _set_or_add(trPr, "w:tblHeader")

    tbl_count = 0
    wide_tables = 0
    for tbl in doc.tables:
        tbl_count += 1

        # 表格整体居中
        tblPr = tbl._tbl.find(qn("w:tblPr"))
        if tblPr is not None:
            jc = _set_or_add(tblPr, "w:jc")
            jc.set(qn("w:val"), "center")

        # 边框 + 列宽
        _set_table_borders(tbl)
        n_cols = len(tbl.rows[0].cells) if tbl.rows else 0
        _set_col_widths(tbl, usable_dxa)
        if n_cols >= 7:
            wide_tables += 1
            log.warning(f"  ⚠ 表 #{tbl_count} 列数 {n_cols} ≥ 7,可能溢出页面(建议拆表)")

        # 表头行:仅居中 + 跨页重复(暗标铁律:除 H1 外不得有装饰区分,加粗 = 做标记)
        if tbl.rows:
            header = tbl.rows[0]
            _mark_header_row(header)
            for cell in header.cells:
                tcPr = cell._tc.get_or_add_tcPr()
                for shd in tcPr.findall(qn("w:shd")):
                    tcPr.remove(shd)
                for para in cell.paragraphs:
                    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for r in para.runs:
                        r.bold = False
                        rPr = r._r.find(qn("w:rPr"))
                        if rPr is not None:
                            for tag in ("w:b", "w:bCs"):
                                for node in rPr.findall(qn(tag)):
                                    rPr.remove(node)

        # 所有单元格:字号/段距/去首行缩进/水平居中/垂直居中
        for row in tbl.rows:
            for cell in row.cells:
                tcPr = cell._tc.get_or_add_tcPr()
                vAlign = _set_or_add(tcPr, "w:vAlign")
                vAlign.set(qn("w:val"), "center")
                for para in cell.paragraphs:
                    pPr = para._p.find(qn("w:pPr"))
                    if pPr is None:
                        pPr = OxmlElement("w:pPr")
                        para._p.insert(0, pPr)
                    # 剥掉 pandoc 注入的 <w:pStyle w:val="Compact"/>(0 高度风险)
                    for ps in pPr.findall(qn("w:pStyle")):
                        val = ps.get(qn("w:val"))
                        if val in ("Compact", "Table", "TableParagraph"):
                            pPr.remove(ps)
                    # 强制首行缩进 0(覆盖 Normal 样式的 2 字缩进)
                    ind = _set_or_add(pPr, "w:ind")
                    ind.set(qn("w:firstLine"), "0")
                    ind.set(qn("w:firstLineChars"), "0")
                    ind.set(qn("w:left"), "0")
                    ind.set(qn("w:leftChars"), "0")
                    ind.set(qn("w:right"), "0")
                    ind.set(qn("w:rightChars"), "0")
                    spacing = _set_or_add(pPr, "w:spacing")
                    spacing.set(qn("w:before"), "0")
                    spacing.set(qn("w:after"), "0")
                    spacing.set(qn("w:line"), "260")
                    spacing.set(qn("w:lineRule"), "auto")
                    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for r in para.runs:
                        force_cn_font(r, size_pt=tbl_size, strip_bold=True)

    log.info(f"  规范表格: {tbl_count} 张(其中 {wide_tables} 张 ≥7 列,已警告)")
    return tbl_count


def postprocess_docx(docx_path: Path, fmt: dict, project_name: str,
                     company_name: str, log: logging.Logger,
                     variant_style: dict | None = None) -> None:
    """pandoc 产出的 DOCX 后处理：
    1. 去除列表项小黑点（剥离 numPr），改为首行缩进
    2. 强制所有 run 使用中文字体（防止 pandoc 默认等线/Calibri）
    3. 表格居中、单元格字体规范
    4. 封面（非暗标）/目录/页码插入
    """
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document(str(docx_path))
    body_size = fmt["font_size_body"]

    def _force_rfonts(rPr, cn_font, en_font):
        """在给定 rPr 上设置/重建 w:rFonts，四套值全同步，清掉旧残留。"""
        # 先删掉已有 rFonts（防止旧属性如 asciiTheme/hAnsiTheme/eastAsiaTheme 残留干扰）
        for old in rPr.findall(qn("w:rFonts")):
            rPr.remove(old)
        rFonts = OxmlElement("w:rFonts")
        rFonts.set(qn("w:ascii"), en_font)
        rFonts.set(qn("w:hAnsi"), en_font)
        rFonts.set(qn("w:eastAsia"), cn_font)
        rFonts.set(qn("w:cs"), en_font)
        # 清掉 theme 引用（Word 主题字体会覆盖显式字体名）
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            rFonts.attrib.pop(qn(f"w:{attr}"), None)
        # rFonts 必须插在 rPr 开头附近，W3C 顺序要求
        rPr.insert(0, rFonts)

    def _force_cn_font(run, size_pt=None, cn_font=None, strip_bold=False):
        if size_pt is not None:
            run.font.size = Pt(size_pt)
        run.font.name = fmt["font_name_en"]
        rPr = run.element.get_or_add_rPr()
        _force_rfonts(rPr, cn_font or fmt["font_name_cn"], fmt["font_name_en"])
        # 暗标规则：除 H1/Title/Subtitle 外其余按正文处理 → 正文不能有粗体
        if strip_bold:
            for tag in ("w:b", "w:bCs"):
                for node in rPr.findall(qn(tag)):
                    rPr.remove(node)
        # 强制黑色：清掉 pandoc / Word 默认的标题蓝色、链接蓝色、代码褐色
        color = rPr.find(qn("w:color"))
        if color is None:
            color = OxmlElement("w:color")
            rPr.append(color)
        color.set(qn("w:val"), "000000")
        # 清掉字符底纹（反引号代码的灰底）
        shd = rPr.find(qn("w:shd"))
        if shd is not None:
            rPr.remove(shd)
        # 清掉荧光笔高亮
        hl = rPr.find(qn("w:highlight"))
        if hl is not None:
            rPr.remove(hl)
        # 清掉字符样式 rStyle（Hyperlink/Verbatim Char 等会带默认字体/颜色）
        rStyle = rPr.find(qn("w:rStyle"))
        if rStyle is not None:
            rPr.remove(rStyle)
        # 强制取消斜体（pandoc 把 Markdown *text* / _text_ 转成斜体；评委可能判"做标记"）
        for tag in ("w:i", "w:iCs"):
            for node in rPr.findall(qn(tag)):
                rPr.remove(node)

    def _normalize_style(style_name, cn_font=None):
        """样式级：强制黑色 + 同步四套字体 + 清底纹/高亮。"""
        try:
            s = doc.styles[style_name]
        except KeyError:
            return
        el = s.element
        rPr = el.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            el.append(rPr)
        _force_rfonts(rPr, cn_font or fmt["font_name_cn"], fmt["font_name_en"])
        color = rPr.find(qn("w:color"))
        if color is None:
            color = OxmlElement("w:color")
            rPr.append(color)
        color.set(qn("w:val"), "000000")
        for tag in ("w:shd", "w:highlight", "w:i", "w:iCs"):
            for node in rPr.findall(qn(tag)):
                rPr.remove(node)

    # ─── 0. 样式级规范化（统一字体 + 黑色 + 清底色 + 清斜体） ─────
    #   body 字体用中文正文字体；标题用黑体（与 build_reference_doc 的 _style 一致）
    body_cn = fmt["font_name_cn"]
    for sn in ("Normal", "Body Text", "List Paragraph", "Table Paragraph",
               "Hyperlink", "FollowedHyperlink",
               "Verbatim Char", "Source Code", "Code"):
        _normalize_style(sn, cn_font=body_cn)
    # 暗标规则仅 H1/Title 用黑体三号；H2-H6 用正文字体
    h1_cn = fmt.get("font_name_h1", "黑体")
    for sn in ("Heading 1", "Title", "Subtitle"):
        _normalize_style(sn, cn_font=h1_cn)
    for sn in ("Heading 2", "Heading 3", "Heading 4", "Heading 5", "Heading 6"):
        _normalize_style(sn, cn_font=body_cn)

    # 暗标 FS-7 防线：所有残留样式（pandoc 注入的 pygments token 样式、Heading7-9、
    # HeadingXChar 等）一律把 rPr 下的 w:color 强制改 000000、剥底纹/斜体，
    # 即使未被 document.xml 引用也要清掉 — 避免评委解压扫色时命中
    for s in list(doc.styles):
        try:
            el = s.element
        except Exception:
            continue
        rPr = el.find(qn("w:rPr"))
        if rPr is None:
            continue
        color = rPr.find(qn("w:color"))
        if color is not None:
            color.set(qn("w:val"), "000000")
        for tag in ("w:shd", "w:highlight", "w:i", "w:iCs"):
            for node in rPr.findall(qn(tag)):
                rPr.remove(node)

    def _clean_paragraph_rPr(paragraph):
        """段落级 w:pPr > w:rPr 的脏字体/颜色清理（pandoc 可能在此塞字体）。"""
        pPr = paragraph._p.find(qn("w:pPr"))
        if pPr is None:
            return
        pRPr = pPr.find(qn("w:rPr"))
        if pRPr is None:
            return
        # 段落级 rFonts/color 直接删除，让它跟随样式/run
        for tag in ("w:rFonts", "w:color", "w:shd", "w:highlight", "w:rStyle"):
            for node in pRPr.findall(qn(tag)):
                pRPr.remove(node)

    # 包装：遍历段落的所有 run（含 hyperlink 子 run）
    from docx.text.run import Run

    def _iter_all_runs(paragraph):
        # paragraph._p 是 w:p；w:r 可能直接在 w:p 下，也可能在 w:hyperlink 下
        for r_el in paragraph._p.iter(qn("w:r")):
            yield Run(r_el, paragraph)

    # 段落样式 → 该用哪个中文字体（H1/Title/Subtitle 用 fmt["font_name_h1"]，其余用 body）
    HEADING_H1_LIKE = {"Heading 1", "Heading1", "Title", "Subtitle"}
    h1_cn = fmt.get("font_name_h1", "黑体")

    def _is_h1_like(p):
        try:
            sname = p.style.name if p.style is not None else ""
        except Exception:
            sname = ""
        return sname in HEADING_H1_LIKE

    def _para_cn_font(p):
        return h1_cn if _is_h1_like(p) else body_cn

    # ─── 1. 去列表黑点 & 字体规范 ─────────────────────────
    list_stripped = 0
    for p in doc.paragraphs:
        pPr = p._p.find(qn("w:pPr"))
        if pPr is not None:
            numPr = pPr.find(qn("w:numPr"))
            if numPr is not None:
                pPr.remove(numPr)
                list_stripped += 1
                # 列表项改为首行缩进 2 字
                ind = pPr.find(qn("w:ind"))
                if ind is None:
                    ind = OxmlElement("w:ind")
                    pPr.append(ind)
                ind.set(qn("w:firstLineChars"), "200")
                ind.set(qn("w:firstLine"), str(int(body_size * 20 * 2)))
                # 清除 List Paragraph 样式可能带的左缩进
                ind.set(qn("w:left"), "0")
                ind.set(qn("w:leftChars"), "0")
        # 段落级 rPr 清理（清掉 pandoc 在 pPr 里塞的字体）
        _clean_paragraph_rPr(p)
        # 规范 run 字体（含超链接内）—— 按段落样式分流：H1/Title/Subtitle 走 h1_cn，其余走 body
        # 非 H1 段落同时剥掉 Markdown **强调** 转出来的 <w:b/>，保证正文无粗体
        para_font = _para_cn_font(p)
        h1_like = _is_h1_like(p)
        for r in _iter_all_runs(p):
            _force_cn_font(r, cn_font=para_font, strip_bold=not h1_like)

    log.info(f"  去除列表项目符号: {list_stripped} 处")

    # ─── 2. 表格规范化（列宽/边框/表头/字号/行距/跨页重复）─────────
    # 2026-05-11 Phase 3.1: 抽到 _postproc_normalize_tables() 模块级函数
    _postproc_normalize_tables(doc, fmt, body_size, log, _force_cn_font)

    # ─── 3. 插入封面 ─────────────────────────
    # 封面由 no_cover 单独控制（_prepend_cover 已是暗标安全的：投标人留空下划线、不印公司名）。
    # 暗标默认 no_cover=True（line ~760 自动置位），故既有暗标项目行为不变；
    # 仅当招标要求封面而显式设 no_cover=false 时，暗标项目才插入这张身份无关的封面。
    if not fmt["no_cover"]:
        _prepend_cover(doc, project_name, company_name, fmt, log)

    # ─── 4. 插入目录（所有情况都插，暗标也要目录） ─────────────
    _prepend_toc(doc, fmt, log)

    # ─── 5. 页眉禁止（暗标 / 显式禁用） ─────────────────────────
    if fmt["header_disabled"]:
        _disable_headers(doc, log)

    # ─── 6. 页码 ─────────────────────────
    if not fmt["no_page_numbers"]:
        _set_page_numbers(doc, log, restart_after_toc=fmt["page_number_restart_after_toc"],
                          position=fmt.get("page_number_position", "center"),
                          number_format=fmt.get("page_number_format", "{n}"))
    else:
        log.info("  跳过页码 (no_page_numbers=True)")

    # ─── 7. 文档软层微调（视觉指纹） ─────────────────────────
    if variant_style:
        _apply_doc_soft_layer(doc, fmt, variant_style, log)

    # ─── 8. 图片尺寸约束 + 大图横版（必须最后执行，防止被覆盖）──
    _fit_images(doc, fmt, log)

    # ─── 9. settings.xml 修正：东亚语言 ja-JP → zh-CN + 隐藏拼写/语法标记 ──
    _fix_settings_lang_and_proofing(doc, log)

    doc.save(str(docx_path))


def _fix_settings_lang_and_proofing(doc, log):
    """修 python-docx 模板默认的 themeFontLang.eastAsia="ja-JP"（→ zh-CN），
    并加 hideSpellingErrors + hideGrammaticalErrors 让 Word 不显示红色波浪线。
    根因：默认模板把东亚字典预置为日语，Word 用日语字典查中文 → 全篇标拼写错误。
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    settings = doc.settings.element

    lang = settings.find(qn("w:themeFontLang"))
    if lang is not None:
        old = lang.get(qn("w:eastAsia"))
        lang.set(qn("w:eastAsia"), "zh-CN")
        if old != "zh-CN":
            log.info(f"  themeFontLang.eastAsia: {old} → zh-CN")
    else:
        lang = OxmlElement("w:themeFontLang")
        lang.set(qn("w:val"), "en-US")
        lang.set(qn("w:eastAsia"), "zh-CN")
        settings.append(lang)
        log.info("  themeFontLang 字段缺失，已补入 eastAsia=zh-CN")

    for tag in ("w:hideSpellingErrors", "w:hideGrammaticalErrors"):
        if settings.find(qn(tag)) is None:
            settings.append(OxmlElement(tag))
    log.info("  已隐藏拼写/语法标记 (hideSpellingErrors + hideGrammaticalErrors)")

    # 让 Word 打开文档时自动更新所有字段（含 TOC）→ 用户无需按 F9
    update_fields = settings.find(qn("w:updateFields"))
    if update_fields is None:
        update_fields = OxmlElement("w:updateFields")
        update_fields.set(qn("w:val"), "true")
        settings.append(update_fields)
        log.info("  已设置 updateFields=true (Word 打开时自动更新目录字段)")
    else:
        update_fields.set(qn("w:val"), "true")


def _prepend_cover(doc, project_name: str, company_name: str, fmt: dict, log):
    """在文档最前插入封面页。"""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from datetime import datetime

    body = doc.element.body
    # pandoc 生成的首个段落是 sectPr 之前的第一个内容元素
    first_child = None
    for child in body.iterchildren():
        if child.tag == qn("w:p") or child.tag == qn("w:tbl"):
            first_child = child
            break

    def _insert_para(text="", size_pt=None, bold=False, cn_font=None,
                     align="center", underline=False, rule=False):
        size_pt = size_pt if size_pt is not None else fmt["font_size_cover_meta"]
        p = OxmlElement("w:p")
        pPr = OxmlElement("w:pPr")
        jc = OxmlElement("w:jc")
        jc.set(qn("w:val"), align)
        pPr.append(jc)
        if rule:  # 段落下边框 = 一条横线
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
            pPr.append(pBdr)
        p.append(pPr)
        if text:
            r = OxmlElement("w:r")
            rPr = OxmlElement("w:rPr")
            rFonts = OxmlElement("w:rFonts")
            rFonts.set(qn("w:eastAsia"), cn_font or fmt["font_name_cn"])
            rFonts.set(qn("w:ascii"), fmt["font_name_en"])
            rFonts.set(qn("w:hAnsi"), fmt["font_name_en"])
            rFonts.set(qn("w:hint"), "eastAsia")
            rPr.append(rFonts)
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), str(int(size_pt * 2)))
            rPr.append(sz)
            szCs = OxmlElement("w:szCs")
            szCs.set(qn("w:val"), str(int(size_pt * 2)))
            rPr.append(szCs)
            if bold:
                rPr.append(OxmlElement("w:b"))
                rPr.append(OxmlElement("w:bCs"))
            if underline:
                u = OxmlElement("w:u"); u.set(qn("w:val"), "single")
                rPr.append(u)
            r.append(rPr)
            t = OxmlElement("w:t")
            t.text = text
            t.set(qn("xml:space"), "preserve")
            r.append(t)
            p.append(r)
        if first_child is not None:
            first_child.addprevious(p)
        else:
            body.append(p)
        return p

    # 字号一律读 fmt（默认回落到 font_size_body），不改字号避免封面字号审计违规;
    # 三变体封面靠版式（留白/横线/对齐/加粗）+ 字体（format_set 已差异化）区分。
    tf = fmt.get("font_name_cover_title") or fmt["font_name_cn"]
    ts = fmt["font_size_cover_title"]
    ps = fmt["font_size_cover_project"]
    ms = fmt["font_size_cover_meta"]
    style = str(fmt.get("cover_style") or "A").upper()
    bidder = "投 标 人：" + "_" * 22          # 占位公司名去掉,留填写下划线
    date_line = f"编 制 日 期：{datetime.now().strftime('%Y 年 %m 月')}"

    if style == "B":   # 横线分隔式
        for _ in range(7): _insert_para()
        _insert_para(rule=True)
        _insert_para("施 工 组 织 设 计", ts, bold=True, cn_font=tf)
        _insert_para(rule=True)
        for _ in range(3): _insert_para()
        _insert_para(project_name, ps)
        for _ in range(7): _insert_para()
        _insert_para(bidder, ms)
        _insert_para(date_line, ms)
    elif style == "C":  # 项目名突出 + 左对齐落款
        for _ in range(5): _insert_para()
        _insert_para("施工组织设计", ts, cn_font=tf)
        for _ in range(4): _insert_para()
        _insert_para(project_name, ps, bold=True)
        for _ in range(9): _insert_para()
        _insert_para(bidder, ms, align="left")
        _insert_para(date_line, ms, align="left")
    else:               # A 居中经典
        for _ in range(8): _insert_para()
        _insert_para("施  工  组  织  设  计", ts, bold=True, cn_font=tf)
        _insert_para()
        _insert_para(project_name, ps)
        for _ in range(6): _insert_para()
        _insert_para(bidder, ms)
        _insert_para(date_line, ms)

    # 分页符
    p_break = OxmlElement("w:p")
    r = OxmlElement("w:r")
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    r.append(br)
    p_break.append(r)
    if first_child is not None:
        first_child.addprevious(p_break)
    else:
        body.append(p_break)

    log.info(f"  封面已插入(样式 {style})：{project_name}")


def _find_toc_anchor(body):
    """找封面分页符之后的插入锚点，返回 (anchor_element, cover_present)。"""
    from docx.oxml.ns import qn
    anchor = None
    cover_present = False
    for child in body.iterchildren():
        if child.tag != qn("w:p"):
            if anchor is None:
                anchor = child
            continue
        for br in child.iter(qn("w:br")):
            if br.get(qn("w:type")) == "page":
                cover_present = True
                return child.getnext(), cover_present
    for child in body.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl")):
            return child, cover_present
    return None, cover_present


def _build_toc_title(fmt: dict):
    """构造目录标题段落（按 H1 规则渲染，字号/字体用正文规格避免格式审计报警）。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    p = OxmlElement("w:p")
    pPr = OxmlElement("w:pPr")
    align = {"center": "center", "left": "left", "right": "right"}.get(fmt.get("align_h1", "center"), "center")
    jc = OxmlElement("w:jc"); jc.set(qn("w:val"), align); pPr.append(jc)
    p.append(pPr)
    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rFonts = OxmlElement("w:rFonts")
    toc_font = fmt.get("font_name_cn", "宋体")
    toc_size = fmt.get("font_size_body", 14)
    rFonts.set(qn("w:eastAsia"), toc_font)
    rFonts.set(qn("w:ascii"), fmt["font_name_en"])
    rFonts.set(qn("w:hAnsi"), fmt["font_name_en"])
    rFonts.set(qn("w:cs"), fmt["font_name_en"])
    rPr.append(rFonts)
    sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(toc_size * 2))); rPr.append(sz)
    r.append(rPr)
    t = OxmlElement("w:t"); t.text = "目    录"
    r.append(t)
    p.append(r)
    return p


def _build_toc_field():
    """构造 TOC 域段落。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    fld1 = OxmlElement("w:fldChar")
    fld1.set(qn("w:fldCharType"), "begin")
    fld1.set(qn("w:dirty"), "true")
    r.append(fld1)
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'
    r.append(instr)
    fld2 = OxmlElement("w:fldChar"); fld2.set(qn("w:fldCharType"), "separate"); r.append(fld2)
    t = OxmlElement("w:t"); t.text = "（请在 Word 中按 F9 更新目录）"; r.append(t)
    fld3 = OxmlElement("w:fldChar"); fld3.set(qn("w:fldCharType"), "end"); r.append(fld3)
    p.append(r)
    return p


def _build_section_break(body):
    """构造分节符（nextPage），克隆 body sectPr 的页面设置。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    p = OxmlElement("w:p")
    pPr = OxmlElement("w:pPr")
    sectPr = OxmlElement("w:sectPr")
    body_sectPr = body.find(qn("w:sectPr"))
    if body_sectPr is not None:
        for child in body_sectPr:
            sectPr.append(copy.deepcopy(child))
    for old in sectPr.findall(qn("w:type")):
        sectPr.remove(old)
    st = OxmlElement("w:type"); st.set(qn("w:val"), "nextPage"); sectPr.append(st)
    pPr.append(sectPr)
    p.append(pPr)
    return p


def _prepend_toc(doc, fmt: dict, log):
    """在文档最前（封面后）插入目录。"""
    body = doc.element.body
    anchor, cover_present = _find_toc_anchor(body)

    def _insert(elem):
        if anchor is not None:
            anchor.addprevious(elem)
        else:
            body.append(elem)

    _insert(_build_toc_title(fmt))
    _insert(_build_toc_field())
    _insert(_build_section_break(body))
    log.info(f"  目录已插入（封面存在={cover_present}），已插入分节符 → 正文独立 section")


def _disable_headers(doc, log):
    """暗标/显式禁用：清空所有 section 的页眉文字 + 把 header reference 标为空。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    cleared = 0
    for section in doc.sections:
        section.different_first_page_header_footer = False
        try:
            header = section.header
            header.is_linked_to_previous = False
            for p in list(header.paragraphs):
                for r in list(p.runs):
                    r.text = ""
                # 同时把段落本身留空
            cleared += 1
        except Exception:
            pass
        # OOXML 层兜底：移除 sectPr 里的 headerReference
        sectPr = section._sectPr
        for hr in sectPr.findall(qn("w:headerReference")):
            sectPr.remove(hr)
    log.info(f"  页眉已禁止：清空 {cleared} 个 section 的 header")


def _set_page_numbers(doc, log, restart_after_toc: bool = False,
                      position: str = "center", number_format: str = "{n}"):
    """页脚页码。位置(center/right)与格式模板({n} 为页号)按变体可变,用于反串标。

    number_format 例: "{n}" → 1 / "第 {n} 页" → 第 1 页 / "— {n} —" → — 1 —

    restart_after_toc=True 且文档有 ≥2 个 section 时：
      - section 0（TOC/前置）：footer 留空，不显示页码
      - section 1+（正文）：阿拉伯数字，pgNumType start=1 重起编号
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    align = (WD_ALIGN_PARAGRAPH.RIGHT if str(position).lower() == "right"
             else WD_ALIGN_PARAGRAPH.CENTER)
    tpl = number_format if "{n}" in (number_format or "") else "{n}"
    prefix, suffix = tpl.split("{n}", 1)

    def _affix_run(p, text):
        """添加前/后缀文本 run,并设中文字体,避免页码缀字渲染异常。"""
        if not text:
            return
        run = p.add_run(text)
        rpr = run._r.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), "宋体")

    sections = list(doc.sections)
    multi_section_restart = restart_after_toc and len(sections) >= 2

    for idx, section in enumerate(sections):
        footer = section.footer
        footer.is_linked_to_previous = False
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.clear()

        # TOC section（idx=0）在 multi-section 模式下不显示页码
        if multi_section_restart and idx == 0:
            continue

        p.alignment = align
        _affix_run(p, prefix)
        r1 = p.add_run()
        fld1 = OxmlElement("w:fldChar"); fld1.set(qn("w:fldCharType"), "begin")
        r1._r.append(fld1)
        r2 = p.add_run()
        instr = OxmlElement("w:instrText"); instr.text = "PAGE"
        instr.set(qn("xml:space"), "preserve")
        r2._r.append(instr)
        r3 = p.add_run()
        fld2 = OxmlElement("w:fldChar"); fld2.set(qn("w:fldCharType"), "end")
        r3._r.append(fld2)
        _affix_run(p, suffix)

        # 正文 section（idx>=1）：start=1 重起；同时显式声明阿拉伯数字
        if multi_section_restart and idx >= 1:
            sectPr = section._sectPr
            for old in sectPr.findall(qn("w:pgNumType")):
                sectPr.remove(old)
            pgNum = OxmlElement("w:pgNumType")
            pgNum.set(qn("w:start"), "1")
            pgNum.set(qn("w:fmt"), "decimal")
            sectPr.append(pgNum)
    note = "（TOC 不编号，正文从1起）" if multi_section_restart else ""
    pos_cn = "右对齐" if align == WD_ALIGN_PARAGRAPH.RIGHT else "居中"
    log.info(f"  页码已设置（页脚{pos_cn},格式「{tpl}」{note}），section 数={len(sections)}")


def _reapply_table_fonts(doc: "Document", fmt: dict, log: logging.Logger) -> None:
    """Word 打开保存后会把 w:sz=21(10.5pt) 升调为 w:sz=22(11pt)，重新压回目标字号。"""
    from docx.shared import Pt
    tbl_size = fmt.get("font_size_table")
    if not tbl_size:
        return
    fixed = 0
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for r in para.runs:
                        if r.font.size != Pt(tbl_size):
                            r.font.size = Pt(tbl_size)
                            fixed += 1
    if fixed:
        log.info(f"  Word 后表格字号已修复: {fixed} 处 → {tbl_size}pt")


def _reapply_h1_run_fonts(doc: "Document", fmt: dict, log: logging.Logger) -> None:
    """Word save strips w:rFonts from H1 paragraph runs; reapply font+size at run level."""
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    h1_cn = fmt.get("font_name_h1") or fmt.get("font_name_cn", "宋体")
    en_font = fmt.get("font_name_en", "Times New Roman")
    h1_size = fmt.get("font_size_h1") or fmt.get("font_size_body")
    H1_STYLES = {"Heading 1", "Heading1", "标题 1", "标题1"}
    fixed = 0
    for p in doc.paragraphs:
        sname = p.style.name if p.style else ""
        if sname not in H1_STYLES:
            continue
        for r in p.runs:
            if h1_size:
                r.font.size = Pt(h1_size)
            rPr = r._element.get_or_add_rPr()
            for old in rPr.findall(qn("w:rFonts")):
                rPr.remove(old)
            rFonts = OxmlElement("w:rFonts")
            rFonts.set(qn("w:eastAsia"), h1_cn)
            rFonts.set(qn("w:ascii"), en_font)
            rFonts.set(qn("w:hAnsi"), en_font)
            rFonts.set(qn("w:hint"), "eastAsia")
            rPr.insert(0, rFonts)
            fixed += 1
    if fixed:
        log.info(f"  Word 后 H1 段落 run 字体已修复: {fixed} 处 → {h1_cn} {h1_size}pt")


def _reapply_heading_fonts(doc: "Document", fmt: dict, log: logging.Logger) -> None:
    """Word 在页数检查时会剥离 H1/Title 的 w:rFonts，重新注入。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    h1_cn = fmt.get("font_name_h1") or fmt.get("font_name_cn", "宋体")
    en_font = fmt.get("font_name_en", "Times New Roman")
    for sn in ("Heading 1", "Title", "Subtitle"):
        try:
            s = doc.styles[sn]
        except KeyError:
            continue
        el = s.element
        rPr = el.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            el.append(rPr)
        for old in rPr.findall(qn("w:rFonts")):
            rPr.remove(old)
        rFonts = OxmlElement("w:rFonts")
        rFonts.set(qn("w:eastAsia"), h1_cn)
        rFonts.set(qn("w:ascii"), en_font)
        rFonts.set(qn("w:hAnsi"), en_font)
        rFonts.set(qn("w:cs"), en_font)
        rPr.insert(0, rFonts)
        color = rPr.find(qn("w:color"))
        if color is not None:
            color.set(qn("w:val"), "000000")
    log.info(f"  Word 后 H1/Title 字体已修复: {h1_cn}")


def _apply_docx_metadata(docx_path: Path, variant: dict | None, log: logging.Logger,
                         fmt: dict | None = None) -> None:
    """清理/注入 DOCX 元数据。

    - 批量模式（variant 非空）：按 variant 配置注入 author/last_modified_by/timestamps
    - 单份模式（variant=None）：一律置空（防止 Word 注册用户名 / Office 标识泄露）

    必须在 Word 页数检查之后调用——Word 打开文档会写 lastModifiedBy。
    """
    try:
        from docx import Document
    except ImportError:
        return
    try:
        from datetime import datetime, timedelta
        doc = Document(str(docx_path))

        # Word 在页数检查时会剥离 H1 样式的 w:rFonts，且把表格 10.5pt→11pt，此处修复
        if fmt:
            _reapply_heading_fonts(doc, fmt, log)
            _reapply_h1_run_fonts(doc, fmt, log)
            _reapply_table_fonts(doc, fmt, log)
        cp = doc.core_properties

        if variant:
            md = variant.get("metadata", {}) or {}
            author = md.get("author") or variant.get("company_name", "")
            last_by = md.get("last_modified_by") or author
            now = datetime.now()
            created = now + timedelta(hours=int(md.get("create_time_offset_hours", 0)))
            modified = now + timedelta(hours=int(md.get("modified_time_offset_hours", 0)))
            subject = variant.get("company_name", "")
        else:
            # 单份模式：一律清空，避免 Office 注册用户名（如 t36393）泄露
            author = ""
            last_by = ""
            now = datetime.now()
            created = now
            modified = now
            subject = ""

        cp.author = author
        cp.last_modified_by = last_by
        cp.created = created
        cp.modified = modified
        cp.revision = 1
        cp.comments = ""
        cp.keywords = ""
        cp.subject = subject
        cp.title = ""

        doc.save(str(docx_path))
        if variant:
            log.info(f"  元数据已注入: author={author}  created={created.isoformat(timespec='minutes')}")
        else:
            log.info("  元数据已清空（单份模式：author/last_modified_by/title 全空）")
    except Exception as e:
        log.warning(f"  元数据处理失败（非致命）: {e}")

    # python-docx 不暴露 app.xml / settings.xml / media，需直接 zip 操作
    _strip_docx_fingerprints(docx_path, log)


def _strip_docx_fingerprints(docx_path: Path, log: logging.Logger) -> None:
    """一次性清理 docx 的多层指纹通道（zip 一次进出，避免重复读写）：

    1. docProps/app.xml — Application 中性化为 'Microsoft Office Word'
    2. word/settings.xml — 删 <w:rsids>...</w:rsids> 段、<w:embedSystemFonts/> 标签
    3. word/document.xml — 删段内所有 w:rsid* 属性（rsidR/rsidRPr/rsidSect 等）
    4. word/media/*.png — PIL 重写抹去 EXIF Software/dpi/comment 等元数据

    rsid 是多文档同源指纹通道（批量投标时尤其敏感）；embedSystemFonts 可能在某些
    Word 版本下嵌入含本机用户名的字体子集；PNG EXIF 暴露生成工具（如 Matplotlib 版本）。
    必须在 _apply_docx_metadata 之后调用——python-docx 保存会重写 zip 抹掉这些清理。
    """
    import zipfile
    import re as _re
    from io import BytesIO
    try:
        with zipfile.ZipFile(str(docx_path), "r") as zin:
            items = {n: zin.read(n) for n in zin.namelist()}

        changes = []

        # 1) app.xml: 清洗所有可暴露身份/版本/时间的字段
        #    Mac Word 保存后会写入: Application/AppVersion/TotalTime/Template/Company/Manager
        if "docProps/app.xml" in items:
            app_xml = items["docProps/app.xml"].decode("utf-8")
            orig = app_xml
            # Application: 中性化
            app_xml = _re.sub(
                r"<Application>[^<]*</Application>",
                "<Application>Microsoft Office Word</Application>",
                app_xml, count=1)
            # AppVersion: 删除（暴露 Word 具体版本如 16.95.0000）
            app_xml = _re.sub(r"<AppVersion>[^<]*</AppVersion>", "", app_xml)
            # TotalTime: 0（编辑总分钟数）
            app_xml = _re.sub(r"<TotalTime>[^<]*</TotalTime>",
                              "<TotalTime>0</TotalTime>", app_xml)
            # Template: 中性化为 Normal.dotm
            app_xml = _re.sub(r"<Template>[^<]*</Template>",
                              "<Template>Normal.dotm</Template>", app_xml)
            # Company / Manager: 置空
            app_xml = _re.sub(r"<Company>[^<]*</Company>", "<Company></Company>", app_xml)
            app_xml = _re.sub(r"<Manager>[^<]*</Manager>", "<Manager></Manager>", app_xml)
            if app_xml != orig:
                items["docProps/app.xml"] = app_xml.encode("utf-8")
                changes.append("app.full")

        # 2) settings.xml: 删 rsids + embedSystemFonts
        if "word/settings.xml" in items:
            s_xml = items["word/settings.xml"].decode("utf-8")
            orig = s_xml
            s_xml = _re.sub(r"<w:rsids>.*?</w:rsids>", "", s_xml, flags=_re.DOTALL)
            s_xml = _re.sub(r"<w:embedSystemFonts\s*/>", "", s_xml)
            s_xml = _re.sub(r"<w:embedSystemFonts\s*>.*?</w:embedSystemFonts>", "", s_xml, flags=_re.DOTALL)
            if s_xml != orig:
                items["word/settings.xml"] = s_xml.encode("utf-8")
                changes.append("settings.rsids+embedFonts")

        # 3) document.xml: 删段内 rsid* 属性
        if "word/document.xml" in items:
            d_xml = items["word/document.xml"].decode("utf-8")
            orig = d_xml
            d_xml = _re.sub(r'\s+w:rsid[A-Z]\w*="[A-Fa-f0-9]+"', "", d_xml)
            if d_xml != orig:
                items["word/document.xml"] = d_xml.encode("utf-8")
                changes.append("document.rsid_attrs")

        # 4) PNG EXIF 抹除：PIL 重写不带 metadata
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            png_n = 0
            for name in list(items.keys()):
                if name.startswith("word/media/") and name.lower().endswith(".png"):
                    bio_in = BytesIO(items[name])
                    im = Image.open(bio_in)
                    im.load()
                    bio_out = BytesIO()
                    # 不传 pnginfo 参数 → 不写 tEXt / iTXt / pHYs 等 chunk
                    im.save(bio_out, format="PNG", optimize=True)
                    items[name] = bio_out.getvalue()
                    png_n += 1
            if png_n:
                changes.append(f"media.png×{png_n}")

        if not changes:
            return

        tmp = docx_path.with_suffix(docx_path.suffix + ".tmp")
        with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as zout:
            for n, data in items.items():
                zout.writestr(n, data)
        tmp.replace(docx_path)
        log.info(f"  指纹清理: {' / '.join(changes)}")
    except Exception as e:
        log.warning(f"  指纹清理失败（非致命）: {e}")


# 用户传 --no-word 时置 True,完全跳过 Word 自动化(目录预渲染 + 页数检查)。
_SKIP_WORD = False

# 探测 Word 是否被占用:不经 application tell 直接问 System Events,不会启动 Word。
# 返回 "notrunning"(未运行,可放心新开) / 整数字符串(已打开的文档数) / 出错。
_WORD_BUSY_APPLESCRIPT = r'''
tell application "System Events"
    if not (exists process "Microsoft Word") then return "notrunning"
end tell
tell application "Microsoft Word" to return (count of documents) as string
'''

# 合并:一次开关完成「更新字段(含 TOC)+ 保存 + 读页数」,避免开关 Word 两遍。
_WORD_RENDER_COUNT_APPLESCRIPT = r'''
tell application "Microsoft Word"
    set d to open file name (POSIX file "{path}" as string)
    delay 3
    with timeout of 420 seconds
        try
            update field of every field of d
        end try
        delay 1
        save d
        set pg to (compute statistics d statistic (statistic pages))
    end timeout
    close d saving no
    return (pg as string)
end tell
'''


def _word_busy_state(log: logging.Logger, timeout: int = 8) -> str:
    """快速探测 Word 可用性。返回 'free'(可用) 或 'skip'(占用/无响应,应跳过)。

    Word 已开着文档时,AppleScript 自动化会与用户抢占连接,旧实现因此每份 docx
    干等到 90s 超时。这里先 8s 内问清状态,占用就立即跳过,不再硬等。
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", _WORD_BUSY_APPLESCRIPT],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("  Word 探测: 无响应(>8s),跳过目录预渲染与页数检查")
        return "skip"
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        log.warning(f"  Word 探测: 失败({result.stderr.strip()[:80]}),跳过 Word 步骤")
        return "skip"
    if out == "notrunning":
        return "free"   # 未运行 → 自动化会新开一个干净实例,无抢占
    try:
        ndocs = int(out)
    except ValueError:
        return "skip"
    if ndocs > 0:
        log.info(f"  Word 有 {ndocs} 个文档打开 → 跳过目录预渲染与页数检查"
                 "(docx 已保存;打开后按 F9 更新目录页码)")
        return "skip"
    return "free"


def _check_page_count(docx_path: Path, fmt: dict, log: logging.Logger) -> int | None:
    """用 Microsoft Word 一次性完成目录预渲染 + 真实页数读取,与 max_pages 比对。

    Word 渲染 = 评委实际看到的页数。三道快速短路,避免旧实现的 90s×2 干等:
      1. --no-word / 非 macOS / 未装 Word → 跳过
      2. Word 已开着文档(被用户占用)→ 跳过(8s 内探明)
      3. 合并为一次开关,AppleScript 内 with timeout 420s + 子进程 480s
         (大文档[~350页]分页耗时长,旧 60s 上限会误判超时)
    跳过不影响 docx 可用:文档已带 updateFields,用户打开按 F9 即更新目录。
    """
    if _SKIP_WORD:
        log.info("  页数检查: 跳过(--no-word)")
        return None
    if sys.platform != "darwin":
        log.info("  页数检查: 跳过(仅 macOS 支持 Word AppleScript)")
        return None
    if not Path("/Applications/Microsoft Word.app").exists():
        log.info("  页数检查: 跳过(未找到 Microsoft Word.app)")
        return None
    if _word_busy_state(log) == "skip":
        return None

    script = _WORD_RENDER_COUNT_APPLESCRIPT.format(path=str(docx_path))
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=480,
        )
    except subprocess.TimeoutExpired:
        log.warning("  页数检查: Word 超时(>480s),跳过(docx 已保存;目录可在 Word 中按 F9 更新)")
        return None
    if result.returncode != 0:
        log.warning(f"  页数检查: Word 失败 — {result.stderr.strip()[:120]}")
        return None
    out = result.stdout.strip()
    try:
        pages = int(out)
    except ValueError:
        log.warning(f"  页数检查: Word 返回非整数 — {out!r}")
        return None
    if pages <= 0:
        log.warning(f"  页数检查: Word 返回 {pages} 页(分页未完成)")
        return None

    log.info("  ✓ 目录预渲染完成 (Word 中无需再按 F9)")
    max_pages = fmt.get("max_pages", 0)
    if max_pages > 0:
        status = "✅" if pages <= max_pages else "❌ 超页"
        log.info(f"  页数检查: {status}  实际 {pages} 页 / 上限 {max_pages} 页 (Microsoft Word 渲染)")
    else:
        log.info(f"  页数检查: {pages} 页 (Microsoft Word 渲染)")
    return pages


def _make_chart_style(variant_id: str, variant: dict = None) -> dict:
    """按 variant 生成不同的图表视觉风格，防串标。

    优先从 variant["chart_style"] 读取完整样式（由 bid_variants.py 的
    generate_variant_style() 生成，包含 org_direction/box_shape/gantt 等
    全部差异化参数）。若不存在则回退到 V01/V02/V03 硬编码样式。
    """
    # 优先使用 variant.chart_style 的完整字段
    if variant and isinstance(variant.get("chart_style"), dict):
        cs = variant["chart_style"]
        style = {}
        # 方向
        style["flow_direction"] = cs.get("flow_direction", cs.get("org_direction", "TB"))
        # 字号/线宽/间距
        style["font_size_offset"] = cs.get("font_size_offset", 0)
        style["line_width"] = cs.get("line_width", 0.8)
        style["flow_corner_radius"] = cs.get("flow_corner_radius", 4)
        style["spacing_factor"] = cs.get("spacing_factor", 1.0)
        style["chart_scale"] = cs.get("chart_scale", 1.0)
        # 甘特图
        style["gantt_grays"] = cs.get("gantt_grays", ["#4A4A4A", "#6E6E6E", "#929292", "#B6B6B6", "#8A8A8A"])
        style["gantt_bar_hatch"] = cs.get("gantt_bar_hatch", "///")
        style["gantt_bar_height"] = cs.get("gantt_bar_height", 0.55)
        style["gantt_grid"] = cs.get("gantt_grid", "dashed")
        _ms = cs.get("gantt_milestone_style", ":")
        _VALID_LS = {"-", "--", "-.", ":", "None", " ", "", "solid", "dashed", "dashdot", "dotted", "none"}
        style["gantt_milestone_style"] = _ms if _ms in _VALID_LS else ":"
        style["gantt_date_fmt"] = cs.get("gantt_date_fmt", "%m月")
        # org chart
        style["org_direction"] = cs.get("org_direction", "TB")
        style["org_box_shape"] = cs.get("org_box_shape", "box")
        # 资源曲线/网络图
        style["resource_bar_width"] = cs.get("resource_bar_width", 0.6)
        style["resource_hatches"] = cs.get("resource_hatches", ["", "//", "\\\\", "xx"])
        style["network_node_radius"] = cs.get("network_node_radius", 0.3)
        style["network_label_pos"] = cs.get("network_label_pos", "above")
        # 流程图视觉主题（反串标：颜色/填充/形状/连线）
        for k in ("chart_accent", "chart_node_fill", "chart_node_filled",
                  "chart_node_rounded", "chart_edge_spline"):
            if k in cs:
                style[k] = cs[k]
        return style

    # 兜底：V01/V02/V03 硬编码
    styles = {
        "V01": {
            "flow_direction": "TB",
            "font_size_offset": 0,
            "line_width": 0.8,
            "flow_corner_radius": 4,
            "spacing_factor": 1.0,
            "chart_scale": 1.0,
            "gantt_grays": ["#4A4A4A", "#6E6E6E", "#929292", "#B6B6B6", "#8A8A8A"],
            "gantt_bar_hatch": "///",
            "gantt_bar_height": 0.55,
            "gantt_grid": "dashed",
            "gantt_milestone_style": ":",
            "gantt_date_fmt": "%m月",
        },
        "V02": {
            "flow_direction": "TB",
            "font_size_offset": 2,
            "line_width": 1.5,
            "chart_scale": 1.3,
            "flow_corner_radius": 0,
            "spacing_factor": 0.80,
            "gantt_grays": ["#808080", "#A0A0A0", "#C0C0C0", "#595959", "#9E9E9E"],
            "gantt_bar_hatch": "",
            "gantt_bar_height": 0.65,
            "gantt_grid": "solid",
            "gantt_milestone_style": "-.",
            "gantt_date_fmt": "%Y-%m-%d",
        },
        "V03": {
            "flow_direction": "BT",
            "font_size_offset": -2,
            "chart_scale": 1.6,
            "line_width": 0.5,
            "flow_corner_radius": 10,
            "spacing_factor": 1.20,
            "gantt_grays": ["#999999", "#BBBBBB", "#777777", "#DDDDDD", "#888888"],
            "gantt_bar_hatch": "\\\\\\",
            "gantt_bar_height": 0.45,
            "gantt_grid": "dotted",
            "gantt_milestone_style": "--",
            "gantt_date_fmt": "%Y-%m",
        },
    }
    return styles.get(str(variant_id).upper(), styles["V01"])


def _assemble_one(
    chapter_dir: Path, extracted_dir: Path,
    output_dir: Path, log: logging.Logger,
    variant: dict = None,
) -> Path:
    """组装单个技术标 DOCX。
    chapter_dir: 含章节 md 的目录（单份模式= output/；批量模式= output/V01/）
    extracted_dir: 含 disqualification_rules.md / project_brain.md 的目录
    output_dir: 最终 DOCX 保存目录
    variant: 批量模式下的 variant 配置；单份模式传 None
    """
    # 格式/暗标/密度/format_sets 一律以 disqualification_rules.md 为唯一真源,
    # 不走 preferred_rules_path(authoring) —— 避免双轨 desync(养殖类项目踩坑)。
    from bid_common import canonical_rules_path
    rules_path = canonical_rules_path(extracted_dir)
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    fmt = parse_format_rules(rules_path, log)

    # ★ v3: 按 variant.format_set 覆盖格式参数（防串标-3套独立格式）
    if variant and variant.get("format_set"):
        # format_sets 写在 disqualification_rules.md 中，单独读取
        dq_path = extracted_dir / "disqualification_rules.md"
        dq_text = dq_path.read_text(encoding="utf-8") if dq_path.exists() else ""
        format_sets = _parse_format_sets(dq_text)
        fs_key = variant["format_set"].upper()
        if fs_key in format_sets:
            fs = format_sets[fs_key]
            log.info(f"  [format_set] 应用 {fs_key}: {fs.get('description', '')}")
            fmt = _apply_format_set(fmt, fs, log)
            fmt["cover_style"] = fs_key   # 封面版式按变体区分(反串标)
        else:
            # 变体引用了不存在的 format_set 是真错(三家排版会退化成完全相同)
            # —— 报 ERROR 并抛出,不静默放行
            avail = ", ".join(sorted(format_sets)) or "(空)"
            log.error(
                f"  [format_set] {fs_key} 在 disqualification_rules.md 中未定义"
                f"(已定义: {avail})——三家排版将无法差异化"
            )
            raise ValueError(
                f"variant {variant.get('id', '?')} 引用的 format_set '{fs_key}' "
                f"未在 disqualification_rules.md 的 format_sets 块中定义"
            )

    info = read_tender_info(chapter_dir, log, extracted_dir=extracted_dir)
    if variant:
        info["company_name"] = variant.get("company_name", info.get("company_name", ""))

    log.info(f"创建 Word 文档: {variant['id'] if variant else '单份'}  公司: {info.get('company_name', '-')}")

    md_files = collect_chapter_files(chapter_dir)
    if not md_files:
        raise FileNotFoundError(f"{chapter_dir} 下未找到章节 .md 文件")

    fmt["company_name"] = info.get("company_name", "")
    is_blind = fmt.get("blind_bid") or fmt.get("no_company_info")
    # 装饰字符清洗（所有项目默认执行；不限暗标）：
    #   - 暗标项目：避免【】★◆✓等被判"做标记"→ 废标
    #   - 非暗标项目：装饰符同样显不专业，一并清洗
    sanitize_decorative_chars(md_files, log)
    strip_prompt_leaks(md_files, log)
    violations = scan_blind_bid_violations(
        md_files,
        company_name=fmt.get("company_name", ""),
        custom_terms=fmt.get("forbidden_terms") or (),
    ) if is_blind else []
    if violations:
        log.error(f"暗标合规扫描：命中 {len(violations)} 处禁用信息（将在报告中列出，但仍继续组装）")
        for h in violations[:20]:
            log.error(f"  {h['file']}:{h['line']}  「{h['match']}」 上下文: …{h['context']}…")
        suffix = f"_{variant['id']}" if variant else ""
        report_path = output_dir / f"暗标扫描警告{suffix}.md"
        lines = ["# 暗标合规扫描警告", "",
                 f"共发现 {len(violations)} 处疑似禁用信息，请人工复核并修改章节后重新组装。", ""]
        for h in violations:
            lines.append(f"- **{h['file']}** 第 {h['line']} 行 ｜ 命中 `{h['match']}` ｜ 上下文：{h['context']}")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        log.error(f"警告文件已写入: {report_path}")
    elif is_blind:
        log.info("暗标合规扫描：未发现公司名/签名等禁用信息 ✓")

    safe_name = re.sub(r'[\\/:*?"<>|]', "_", info["project_name"])[:30]
    if variant:
        # 占位公司名已去除,文件名用 variant id + 项目名区分三份
        safe_company = re.sub(r'[\\/:*?"<>|]', "_", variant.get("company_name", "") or "")[:20]
        seg = f"{safe_company}_" if safe_company else ""
        out_path = output_dir / f"{variant['id']}_{seg}{safe_name}_施工组织设计.docx"
    else:
        out_path = output_dir / f"{safe_name}_施工组织设计.docx"

    # ─── 图表预处理：剥离 JSON/mermaid，渲染进度甘特图 PNG ──
    variant_id = variant["id"] if variant else ""
    chart_style = _make_chart_style(variant_id, variant=variant) if variant else None
    try:
        tmp_md_files = preprocess_chapter_dir(
            chapter_dir, md_files, log, variant_id,
            add_chapter_prefix=fmt.get("heading_chapter_prefix", True),
            chart_style=chart_style,
        )
    except Exception as e:
        log.warning(f"图表预处理失败，回退到原 md：{e}")
        tmp_md_files = md_files

    # ─── pandoc 管线 ─────────────────────────────────
    pandoc_exe = _find_pandoc()
    ref_docx = build_reference_doc(fmt, log)
    log.info(f"pandoc 转换 {len(tmp_md_files)} 个章节 → {out_path.name}")
    try:
        run_pandoc(tmp_md_files, ref_docx, out_path, pandoc_exe, log)
    finally:
        try:
            ref_docx.unlink()
        except Exception:
            pass

    # ─── 后处理：字体/去黑点/封面/目录/页码 ─────────────
    vstyle = variant.get("chart_style") if variant else None
    postprocess_docx(out_path, fmt, info["project_name"], info["company_name"], log,
                     variant_style=vstyle)

    log.info(f"  文档已保存: {out_path.name}  大小: {out_path.stat().st_size:,} 字节")

    # ★ 顺序硬性要求：先页数检查（Word 打开会写 lastModifiedBy），再元数据清理
    _check_page_count(out_path, fmt, log)
    _apply_docx_metadata(out_path, variant, log, fmt=fmt)

    return out_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="投标文件 DOCX 组装工具")
    parser.add_argument("output_dir", help="output 目录路径")
    parser.add_argument("--batch", action="store_true",
                        help="批量模式：扫描 output/V*/ 为每家单位生成独立 DOCX")
    parser.add_argument("--no-word", action="store_true",
                        help="跳过 Word 自动化(目录预渲染+页数检查);docx 仍正常生成,"
                             "打开后按 F9 更新目录。Word 开着文档时本就会自动跳过。")
    args = parser.parse_args()

    global _SKIP_WORD
    _SKIP_WORD = args.no_word

    try:
        from docx import Document  # noqa: F401
    except ImportError:
        print("错误: 请先安装 python-docx: pip install python-docx")
        sys.exit(1)

    output_dir = Path(args.output_dir).resolve()
    if not output_dir.exists():
        print(f"目录不存在: {output_dir}")
        sys.exit(1)

    (output_dir / "logs").mkdir(exist_ok=True)
    log_path = output_dir / "logs" / "run.log"
    log = make_script_logger("bid_assemble", log_path)

    log.info("=" * 60)
    log.info(f"bid_assemble 启动  输出目录: {output_dir}  批量={args.batch}")
    log.info("=" * 60)

    extracted_dir = output_dir.parent / "extracted"

    if not args.batch:
        # 单份模式（保持原行为）
        try:
            _assemble_one(output_dir, extracted_dir, output_dir, log)
        except FileNotFoundError as e:
            log.error(str(e))
            sys.exit(1)
        log.info("=" * 60)
        log.info(f"bid_assemble 完成  日志文件: {log_path}")
        log.info("=" * 60)
        return

    # ── 批量模式 ─────────────────────────────────────────────────────
    variants_path = extracted_dir / "variants.json"
    if not variants_path.exists():
        log.error(f"批量模式需要 {variants_path}")
        sys.exit(2)
    variants_payload = json.loads(variants_path.read_text(encoding="utf-8"))

    variant_dirs = sorted([d for d in output_dir.iterdir() if d.is_dir() and re.match(r"^V\d+$", d.name)])
    if not variant_dirs:
        log.error(f"未发现 V01/ V02/ ... 子目录。请先运行 bid_writer.py 生成各 variant 章节。")
        sys.exit(2)

    variants_by_id = {v["id"]: v for v in variants_payload["variants"]}

    success = []
    failed = []
    for v_dir in variant_dirs:
        variant = variants_by_id.get(v_dir.name)
        if not variant:
            log.warning(f"目录 {v_dir.name} 在 variants.json 中无对应配置，跳过")
            continue
        log.info(f"\n━━━ 组装 {v_dir.name} ({variant['company_name']}) ━━━")
        try:
            out_path = _assemble_one(v_dir, extracted_dir, output_dir, log, variant=variant)
            success.append(out_path)
        except Exception as e:
            log.error(f"{v_dir.name} 组装失败: {e}", exc_info=True)
            failed.append(v_dir.name)

    log.info("=" * 60)
    log.info(f"批量组装完成  成功: {len(success)}  失败: {len(failed)}")
    for p in success:
        log.info(f"  ✓ {p.name}")
    for f in failed:
        log.info(f"  ✗ {f}")
    log.info("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
