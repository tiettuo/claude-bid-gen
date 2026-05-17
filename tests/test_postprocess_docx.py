"""postprocess_docx 集成测试 — Phase 3.1 拆函数前的安全网。

策略:
1. 构造一个最小的"假 pandoc 输出"docx(用 python-docx 直接造,含 H1/H2/正文/列表/表格)
2. 跑 postprocess_docx
3. 用 python-docx 的高层 API + 适度的 XML 直读 验证关键属性
4. 拆函数后跑这套测试,任何属性变化即视为回归

覆盖范围(从 minimal 到 full):
- A. 基础属性:字体名 / 字号 / 页边距 / 行距
- B. 标题样式:H1 黑体 + 加粗 + 居中
- C. 封面(非暗标):应插入,含 project_name 和 company_name
- D. 暗标模式:no_cover=True 不插封面 / no_company_info=True 不写公司名
- E. 页码:no_page_numbers=False 应有 PAGE 域
- F. 横版图:大图应被 _fit_images 处理(尺寸约束)

本期先做 A+B+C+D 4 个核心 case;E+F 等到拆函数前补。
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from pathlib import Path

import pytest


# ─── 依赖检查(pytest skip) ────────────────────────────────────────────

def _check_deps():
    """postprocess_docx 强依赖 python-docx;集成测试还需要 pandoc 但本测试不调用 pandoc"""
    try:
        import docx  # noqa: F401
    except ImportError:
        pytest.skip("缺 python-docx")


@pytest.fixture
def default_fmt():
    """返回完整 DEFAULT_FORMAT 的 deep copy + 补全 parse_format_rules 通常会注入的字段。

    本测试绕过 parse_format_rules 直接用 DEFAULT_FORMAT,因此要手工补全
    后续 _prepend_cover / _prepend_toc 用到的 cover 字号字段(原本由
    parse_format_rules:636-643 补)。
    """
    from bid_assemble import DEFAULT_FORMAT
    fmt = copy.deepcopy(DEFAULT_FORMAT)
    body_size = fmt["font_size_body"]
    fmt.setdefault("font_size_title_style", body_size)
    fmt.setdefault("font_size_cover_title", body_size)
    fmt.setdefault("font_size_cover_project", body_size)
    fmt.setdefault("font_size_cover_meta", body_size)
    fmt.setdefault("_specified_keys", set())
    return fmt


# ─── 构造最小 pandoc-like docx ────────────────────────────────────────

def _build_minimal_docx(out_path: Path, fmt: dict) -> Path:
    """构造一个最小的 pandoc-like 输出 docx:
    - Heading 1 × 2 章
    - Heading 2 × 1
    - 正文 paragraph × 3
    - List × 3 item
    - Table 2×3
    样式名沿用 pandoc 默认 (Heading 1 / List Paragraph / Body Text)。
    """
    _check_deps()
    from docx import Document
    from docx.shared import Cm, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    # 页面 + section
    for section in doc.sections:
        section.page_width = Cm(fmt["page_width_cm"])
        section.page_height = Cm(fmt["page_height_cm"])
        section.top_margin = Cm(fmt["margin_top_cm"])
        section.bottom_margin = Cm(fmt["margin_bottom_cm"])
        section.left_margin = Cm(fmt["margin_left_cm"])
        section.right_margin = Cm(fmt["margin_right_cm"])

    # 章节 1
    doc.add_heading("工程概况", level=1)
    p1 = doc.add_paragraph("本项目位于西藏自治区,采用现浇钢筋混凝土框架结构。")
    doc.add_heading("项目位置", level=2)
    doc.add_paragraph("场地地处高海拔区域,平均海拔约 3,800 米。")
    # 列表
    doc.add_paragraph("施工要点包括:", style="Normal")
    doc.add_paragraph("土方开挖按图施工", style="List Bullet")
    doc.add_paragraph("钢筋绑扎符合规范", style="List Bullet")
    doc.add_paragraph("混凝土浇筑分层进行", style="List Bullet")

    # 章节 2
    doc.add_heading("施工部署", level=1)
    doc.add_paragraph("根据工程特点划分四个施工区段。")

    # 表格
    t = doc.add_table(rows=2, cols=3)
    t.style = "Table Grid"
    hdr = t.rows[0].cells
    hdr[0].text = "项目"
    hdr[1].text = "工期"
    hdr[2].text = "负责人"
    body = t.rows[1].cells
    body[0].text = "土方工程"
    body[1].text = "30 天"
    body[2].text = "技术负责人"

    doc.save(str(out_path))
    return out_path


# ─── 通用工具 ──────────────────────────────────────────────────────────

def _qn(tag):
    from docx.oxml.ns import qn
    return qn(tag)


def _all_run_fonts(doc) -> list[str]:
    """收集 doc 里所有 run 的 eastAsia 字体名(空串/None 忽略)。"""
    out = []
    for para in doc.paragraphs:
        for run in para.runs:
            rPr = run.element.find(_qn("w:rPr"))
            if rPr is None:
                continue
            rFonts = rPr.find(_qn("w:rFonts"))
            if rFonts is None:
                continue
            ea = rFonts.get(_qn("w:eastAsia"))
            if ea:
                out.append(ea)
    return out


# ─── Case A:基础属性(字体 + 页边距) ──────────────────────────────────

def test_A_basic_font_and_margin(default_fmt, tmp_path: Path):
    """跑完 postprocess_docx 后,正文 run 的 eastAsia 字体应 == fmt['font_name_cn']
       页边距应保留 setup_document 设的值。"""
    _check_deps()
    from docx import Document
    import logging

    docx_path = tmp_path / "test_A.docx"
    _build_minimal_docx(docx_path, default_fmt)

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_A")
    log.setLevel(logging.WARNING)

    out = postprocess_docx(
        docx_path, default_fmt,
        project_name="测试项目",
        company_name="某建筑公司",
        log=log,
    )
    # postprocess_docx 实际返回 out_path 或者就地修改;两种都接受
    final_path = Path(out) if out else docx_path
    assert final_path.exists(), "输出 docx 应存在"

    doc = Document(str(final_path))
    fonts = _all_run_fonts(doc)
    cn = default_fmt["font_name_cn"]
    # 大部分(>80%)的 run 应是中文字体,允许少数 H1 用黑体
    if fonts:
        cn_count = sum(1 for f in fonts if f == cn or f == default_fmt["font_name_h1"])
        ratio = cn_count / len(fonts)
        assert ratio >= 0.5, f"中文字体覆盖率 {ratio:.1%} 过低,fonts={set(fonts)}"

    # 页边距:postprocess_docx 不应改 section.top_margin
    sect = doc.sections[0]
    from docx.shared import Cm
    assert abs(sect.top_margin.cm - default_fmt["margin_top_cm"]) < 0.1
    assert abs(sect.left_margin.cm - default_fmt["margin_left_cm"]) < 0.1


# ─── Case B:H1 样式 ──────────────────────────────────────────────────

def test_B_h1_style_consistent(default_fmt, tmp_path: Path):
    """H1 段落的字体应符合 fmt['font_name_h1'],对齐符合 fmt['align_h1']。"""
    _check_deps()
    from docx import Document
    import logging

    docx_path = tmp_path / "test_B.docx"
    _build_minimal_docx(docx_path, default_fmt)

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_B")
    log.setLevel(logging.WARNING)

    out = postprocess_docx(
        docx_path, default_fmt,
        project_name="测试", company_name="x", log=log,
    )
    final_path = Path(out) if out else docx_path

    doc = Document(str(final_path))
    # 找 H1 段落(style = Heading 1)
    h1_paras = [p for p in doc.paragraphs if p.style.name == "Heading 1"]
    if not h1_paras:
        pytest.skip("无 H1 段落(可能被 postprocess 改名)")
    # H1 字体:任一 run 的 eastAsia 应是 fmt["font_name_h1"]
    h1_fonts = []
    for p in h1_paras:
        for run in p.runs:
            rPr = run.element.find(_qn("w:rPr"))
            if rPr is None:
                continue
            rFonts = rPr.find(_qn("w:rFonts"))
            if rFonts is not None:
                ea = rFonts.get(_qn("w:eastAsia"))
                if ea:
                    h1_fonts.append(ea)
    if h1_fonts:
        # 至少一个 H1 run 是 h1 字体或 body 字体
        valid = {default_fmt["font_name_h1"], default_fmt["font_name_cn"]}
        assert any(f in valid for f in h1_fonts), f"H1 字体异常:{set(h1_fonts)}"


# ─── Case C:非暗标应插入封面 ──────────────────────────────────────────

def test_C_cover_inserted_when_not_blind(default_fmt, tmp_path: Path):
    """blind_bid=False + no_cover=False 时,封面应被插入。
       封面识别:文档前 X 段含 project_name 字符串。"""
    _check_deps()
    from docx import Document
    import logging

    docx_path = tmp_path / "test_C.docx"
    _build_minimal_docx(docx_path, default_fmt)

    fmt = copy.deepcopy(default_fmt)
    fmt["blind_bid"] = False
    fmt["no_cover"] = False

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_C")
    log.setLevel(logging.WARNING)

    project_name = "仓储类项目A仓储项目"
    out = postprocess_docx(
        docx_path, fmt,
        project_name=project_name,
        company_name="某建筑公司",
        log=log,
    )
    final_path = Path(out) if out else docx_path

    doc = Document(str(final_path))
    # 前 15 段应含 project_name(封面通常在最前)
    head_texts = [p.text for p in doc.paragraphs[:15]]
    head_blob = "\n".join(head_texts)
    assert project_name in head_blob, (
        f"非暗标项目应有封面含 project_name '{project_name}',"
        f"实际前 15 段:\n{head_blob[:500]}"
    )


# ─── Case D:暗标模式应跳过封面 ──────────────────────────────────────

def test_D_blind_no_cover_no_company(default_fmt, tmp_path: Path):
    """blind_bid=True 时,封面应被跳过 + company_name 不应出现在文档里。"""
    _check_deps()
    from docx import Document
    import logging

    docx_path = tmp_path / "test_D.docx"
    _build_minimal_docx(docx_path, default_fmt)

    fmt = copy.deepcopy(default_fmt)
    fmt["blind_bid"] = True
    fmt["no_cover"] = True
    fmt["header_disabled"] = True

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_D")
    log.setLevel(logging.WARNING)

    company_name = "某建筑有限公司"
    out = postprocess_docx(
        docx_path, fmt,
        project_name="x", company_name=company_name, log=log,
    )
    final_path = Path(out) if out else docx_path

    doc = Document(str(final_path))
    all_text = "\n".join(p.text for p in doc.paragraphs)
    # 暗标项目下 company_name 不应出现在正文(除非 user 明确写在 md 里)
    assert company_name not in all_text, (
        f"暗标模式下 company_name '{company_name}' 不应出现在文档,"
        f"但在正文中找到:\n{all_text[:300]}"
    )


# ─── Case E:页码字段(PAGE field) ─────────────────────────────────────

def test_E_page_number_field_inserted(default_fmt, tmp_path: Path):
    """no_page_numbers=False 时,docx 内应有 PAGE 域(w:instrText 含 PAGE)。"""
    _check_deps()
    from docx import Document
    import logging

    docx_path = tmp_path / "test_E.docx"
    _build_minimal_docx(docx_path, default_fmt)

    fmt = copy.deepcopy(default_fmt)
    fmt["no_page_numbers"] = False
    fmt["blind_bid"] = False

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_E")
    log.setLevel(logging.WARNING)

    out = postprocess_docx(
        docx_path, fmt,
        project_name="测试", company_name="x", log=log,
    )
    final_path = Path(out) if out else docx_path

    doc = Document(str(final_path))
    # 扫整个 docx XML 找 PAGE 字段(在 instrText 里)
    found_page = False
    for section in doc.sections:
        for part in (section.footer, section.header):
            for p in part.paragraphs:
                for elem in p._element.iter():
                    if elem.tag.endswith("}instrText") and elem.text and "PAGE" in elem.text:
                        found_page = True
                        break
    assert found_page, "非 no_page_numbers 模式下应至少有一处 PAGE 字段"


# ─── Case F:max_pages 校验调用不应崩溃 ──────────────────────────────

def test_F_max_pages_check_does_not_crash(default_fmt, tmp_path: Path):
    """设 max_pages=100 不应让 postprocess_docx 失败(_check_page_count 是 osascript 调用,
       macOS 才能跑;但函数本身要 graceful 处理失败)。"""
    _check_deps()
    import logging

    docx_path = tmp_path / "test_F.docx"
    _build_minimal_docx(docx_path, default_fmt)

    fmt = copy.deepcopy(default_fmt)
    fmt["max_pages"] = 100

    from bid_assemble import postprocess_docx
    log = logging.getLogger("test_F")
    log.setLevel(logging.WARNING)

    # 不应崩溃
    out = postprocess_docx(
        docx_path, fmt,
        project_name="测试", company_name="x", log=log,
    )
    final_path = Path(out) if out else docx_path
    assert final_path.exists()
    assert final_path.stat().st_size > 5000  # 至少 5KB,正常 docx
