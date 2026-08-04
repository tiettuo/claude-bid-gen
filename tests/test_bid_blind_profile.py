"""测试 bid_blind_profile — 地区专项技术暗标 profile（重庆）。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import bid_blind_profile as bbp
from bid_blind_profile import (
    detect_blind_profile,
    load_profile,
    apply_profile_to_fmt,
    check_bold_runs,
    check_color_runs,
    check_page_numbers,
    check_color_images,
    check_blank_and_rotated_pages,
    check_advisory_page_limit,
    grayscale_png_dir,
)
from bid_verify import pages_hard_cap_enforced

LOG = logging.getLogger("test_blind_profile")


# ─── fixtures / helpers ─────────────────────────────────────────

def _make_extracted(tmp_path: Path, rules_text: str = "", facts: dict | None = None) -> Path:
    ext = tmp_path / "extracted"
    ext.mkdir(parents=True, exist_ok=True)
    if rules_text:
        (ext / "disqualification_rules.md").write_text(rules_text, encoding="utf-8")
    if facts is not None:
        (ext / "project_facts.json").write_text(
            json.dumps({"facts": facts}, ensure_ascii=False), encoding="utf-8")
    return ext


BLIND_RULES = "```yaml\nblind_bid: true\n```\n"


def _chongqing_profile() -> dict:
    profile = load_profile("chongqing")
    assert profile is not None, "configs/bid_blind_profiles.json 缺少 chongqing profile"
    return profile


# ─── profile 检测 ───────────────────────────────────────────────

def test_detect_region_hit_via_facts(tmp_path):
    ext = _make_extracted(tmp_path, BLIND_RULES,
                          facts={"location": {"value": "重庆市渝北区某街道"}})
    assert detect_blind_profile(ext) == "chongqing"


def test_detect_region_hit_via_rules_text(tmp_path):
    ext = _make_extracted(tmp_path, BLIND_RULES + "\n建设地点：重庆市江北区\n")
    assert detect_blind_profile(ext) == "chongqing"


def test_detect_rules_explicit(tmp_path):
    ext = _make_extracted(
        tmp_path, "```yaml\nblind_bid: true\nblind_profile: chongqing\n```\n")
    assert detect_blind_profile(ext) == "chongqing"


def test_detect_non_blind_inactive(tmp_path):
    """非暗标项目即使地区命中也一律不激活。"""
    ext = _make_extracted(
        tmp_path,
        "```yaml\nblind_bid: false\nno_company_info: false\n```\n建设地点：重庆市\n",
        facts={"location": {"value": "重庆市"}})
    assert detect_blind_profile(ext) is None


def test_detect_explicit_none_disables(tmp_path):
    """rules 显式 blind_profile: none → 即使地区命中也关闭。"""
    ext = _make_extracted(
        tmp_path,
        "```yaml\nblind_bid: true\nblind_profile: none\n```\n建设地点：重庆市\n",
        facts={"location": {"value": "重庆市"}})
    assert detect_blind_profile(ext) is None


def test_detect_other_region_none(tmp_path):
    ext = _make_extracted(tmp_path, BLIND_RULES,
                          facts={"location": {"value": "北京市海淀区"}})
    assert detect_blind_profile(ext) is None


def test_load_profile_unknown_returns_none():
    assert load_profile("nonexistent_region") is None
    assert load_profile("") is None
    assert load_profile(None) is None


# ─── fmt 覆盖 ───────────────────────────────────────────────────

def test_apply_profile_to_fmt_overrides():
    from bid_assemble import DEFAULT_FORMAT
    fmt = DEFAULT_FORMAT.copy()
    specified = set()
    apply_profile_to_fmt(fmt, specified, _chongqing_profile(), LOG)

    # 四号仿宋正文
    assert fmt["font_name_cn"] == "仿宋"
    assert fmt["font_size_body"] == 14
    # 暗标关联硬性格式
    assert fmt["no_cover"] is True
    assert fmt["no_page_numbers"] is True
    assert fmt["no_color"] is True
    assert fmt["header_disabled"] is True
    assert fmt["page_size"] == "A4"
    # headings_follow_body：H1-H3 全部跟随正文、不加粗
    assert fmt["font_size_h1"] == 14
    assert fmt["font_size_h2"] == 14
    assert fmt["font_size_h3"] == 14
    assert fmt["font_name_h1"] == "仿宋"
    assert fmt["bold_h1"] is False
    # all_text_no_bold 标记
    assert fmt["_all_text_no_bold"] is True
    # 全部进 specified（招标硬性，软层不得覆盖）
    for key in ("no_cover", "no_page_numbers", "no_color", "header_disabled",
                "font_name_cn", "font_size_body", "bold_h1", "page_size",
                "page_width_cm", "page_height_cm",
                "font_size_h1", "font_size_h2", "font_size_h3", "font_name_h1"):
        assert key in specified, f"{key} 未计入 specified"


# ─── DOCX 合成检测（python-docx）─────────────────────────────────

def _make_docx(path: Path, runs_spec: list[tuple[str, dict]], with_page_field: bool = False):
    """runs_spec: [(text, {bold: True/False/None, color: (r,g,b)/None}), ...]"""
    from docx import Document
    from docx.shared import RGBColor
    doc = Document()
    p = doc.add_paragraph()
    for text, opts in runs_spec:
        r = p.add_run(text)
        if opts.get("bold") is not None:
            r.font.bold = opts["bold"]
        if opts.get("color") is not None:
            r.font.color.rgb = RGBColor(*opts["color"])
    if with_page_field:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        footer = doc.sections[0].footer
        footer.is_linked_to_previous = False
        fr = footer.paragraphs[0].add_run()
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE "
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        fr._r.append(fld_begin)
        fr._r.append(instr)
        fr._r.append(fld_end)
    doc.save(str(path))
    return path


def test_check_bold_runs_positive(tmp_path):
    dx = _make_docx(tmp_path / "bold.docx",
                    [("本章为施工总体部署。", {"bold": True})])
    hits = check_bold_runs(dx)
    assert len(hits) == 1
    assert hits[0]["rule"] == "all_text_no_bold"
    assert "施工总体部署" in hits[0]["location"]


def test_check_bold_runs_negative(tmp_path):
    dx = _make_docx(tmp_path / "clean.docx",
                    [("普通正文无修饰。", {}),
                     ("显式关闭粗体。", {"bold": False})])
    assert check_bold_runs(dx) == []


def test_check_color_runs_positive(tmp_path):
    dx = _make_docx(tmp_path / "color.docx",
                    [("重点提示文字。", {"color": (0xFF, 0, 0)})])
    hits = check_color_runs(dx)
    assert len(hits) == 1
    assert hits[0]["rule"] == "no_color"
    assert "FF0000" in hits[0]["detail"]


def test_check_color_runs_negative(tmp_path):
    dx = _make_docx(tmp_path / "black.docx",
                    [("黑色文字。", {"color": (0, 0, 0)}), ("默认色。", {})])
    assert check_color_runs(dx) == []


def test_check_page_numbers_positive(tmp_path):
    dx = _make_docx(tmp_path / "pg.docx", [("正文。", {})], with_page_field=True)
    hits = check_page_numbers(dx)
    assert len(hits) == 1
    assert hits[0]["rule"] == "no_page_numbers"


def test_check_page_numbers_negative(tmp_path):
    dx = _make_docx(tmp_path / "nopg.docx", [("正文。", {})])
    assert check_page_numbers(dx) == []


# ─── 图片彩色检测（PIL 合成）─────────────────────────────────────

pytestmark_pil = pytest.importorskip("PIL.Image", reason="PIL 不可用")


def _make_png(path: Path, mode: str, color):
    from PIL import Image
    Image.new(mode, (64, 64), color).save(path)


def test_check_color_images_dir(tmp_path):
    _make_png(tmp_path / "red.png", "RGB", (220, 30, 30))
    _make_png(tmp_path / "gray.png", "L", 128)
    hits = check_color_images(tmp_path)
    assert len(hits) == 1
    assert hits[0]["rule"] == "no_color_images"
    assert hits[0]["location"] == "red.png"


def test_check_color_images_docx_media(tmp_path):
    red = tmp_path / "red.png"
    _make_png(red, "RGB", (30, 30, 220))
    from docx import Document
    doc = Document()
    doc.add_paragraph("配图章节。")
    doc.add_picture(str(red))
    dx = tmp_path / "with_img.docx"
    doc.save(str(dx))
    hits = check_color_images(dx)
    assert len(hits) == 1
    assert hits[0]["rule"] == "no_color_images"


def test_grayscale_png_dir_fixes_color(tmp_path):
    _make_png(tmp_path / "chart.png", "RGB", (200, 40, 40))
    n = grayscale_png_dir(tmp_path)
    assert n == 1
    assert check_color_images(tmp_path) == []


# ─── 白页/倒页检测（PyMuPDF）─────────────────────────────────────

def test_check_blank_and_rotated_pages():
    fitz = pytest.importorskip("fitz", reason="PyMuPDF 不可用")
    import tempfile
    doc = fitz.open()
    doc.new_page()  # 白页
    p2 = doc.new_page()
    p2.insert_text((72, 72), "正常页面")
    p3 = doc.new_page()
    p3.insert_text((72, 72), "被旋转的页面")
    p3.set_rotation(90)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        pdf_path = Path(tf.name)
    doc.save(str(pdf_path))
    doc.close()

    hits = check_blank_and_rotated_pages(pdf_path)
    rules = {h["rule"] for h in hits}
    assert "no_blank_pages" in rules
    assert "no_rotated_pages" in rules
    blank = next(h for h in hits if h["rule"] == "no_blank_pages")
    assert blank["location"] == "page 1"
    pdf_path.unlink()


def test_check_blank_and_rotated_graceful_without_fitz(tmp_path, monkeypatch):
    """PyMuPDF 缺失时优雅跳过：返回空表、不抛异常。"""
    monkeypatch.setattr(bbp, "_fitz", None)
    assert check_blank_and_rotated_pages(tmp_path / "any.pdf") == []


def test_check_blank_and_rotated_missing_pdf(tmp_path):
    assert check_blank_and_rotated_pages(tmp_path / "nope.pdf") == []


# ─── 200 页 advisory（只 WARN 不改退出码）────────────────────────

def test_advisory_page_limit():
    profile = _chongqing_profile()
    assert profile["advisory_max_pages"] == 200
    assert profile["pages_not_eval_factor"] is True
    hits = check_advisory_page_limit(250, profile)
    assert len(hits) == 1
    assert hits[0]["severity"] == "warn"
    assert check_advisory_page_limit(200, profile) == []
    assert check_advisory_page_limit(None, profile) == []


def test_pages_hard_cap_enforced_chongqing(tmp_path):
    """重庆暗标：页数硬顶降级为 warn（不 hard-fail）。"""
    ext = _make_extracted(tmp_path, BLIND_RULES,
                          facts={"location": {"value": "重庆市"}})
    assert pages_hard_cap_enforced(ext) is False


def test_pages_hard_cap_enforced_other_projects_unchanged(tmp_path):
    """非重庆/非暗标项目：页数硬顶逻辑零变化（仍 hard-fail）。"""
    ext_nonblind = _make_extracted(
        tmp_path / "a", "```yaml\nblind_bid: false\nno_company_info: false\n```\n")
    assert pages_hard_cap_enforced(ext_nonblind) is True
    ext_blind_other = _make_extracted(
        tmp_path / "b", BLIND_RULES, facts={"location": {"value": "北京市"}})
    assert pages_hard_cap_enforced(ext_blind_other) is True
