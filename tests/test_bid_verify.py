import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_verify import _normal_style_block, _is_lang_code, detect_page_cap


def test_is_lang_code():
    assert _is_lang_code("zh-CN")
    assert _is_lang_code("ja-JP")
    assert not _is_lang_code("宋体")
    assert not _is_lang_code("仿宋")
    assert not _is_lang_code("Times New Roman")


def test_normal_style_block_by_name_when_styleid_renumbered():
    """Word 重存 docx 会把 styleId 改成数字,但 <w:name w:val="Normal"> 不变。"""
    xml = (
        '<w:styles>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="1">'
        '<w:name w:val="Normal"/>'
        '<w:pPr><w:spacing w:line="560" w:lineRule="exact"/></w:pPr>'
        '<w:rPr><w:rFonts w:eastAsia="仿宋"/></w:rPr>'
        '</w:style>'
        '<w:style w:type="paragraph" w:styleId="2">'
        '<w:name w:val="Heading1"/></w:style>'
        '</w:styles>'
    )
    blk = _normal_style_block(xml)
    assert 'w:eastAsia="仿宋"' in blk
    assert "Heading1" not in blk


def test_normal_style_block_default_fallback():
    """没有 name=Normal 时,退到 w:default=1 的段落样式。"""
    xml = (
        '<w:styles>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="x">'
        '<w:rPr><w:rFonts w:eastAsia="楷体"/></w:rPr></w:style>'
        '</w:styles>'
    )
    assert 'w:eastAsia="楷体"' in _normal_style_block(xml)


def test_normal_style_block_missing():
    assert _normal_style_block("<w:styles></w:styles>") == ""


def test_detect_page_cap(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "chapter_plan.md").write_text(
        "招标硬顶:技术部分页数 ≤ 400 页", encoding="utf-8")
    assert detect_page_cap(ext) == 400


def test_detect_page_cap_chaoguo(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "disqualification_rules.md").write_text(
        "技术部分页数不得超过 300 页", encoding="utf-8")
    assert detect_page_cap(ext) == 300


def test_detect_page_cap_none(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "chapter_plan.md").write_text("无明确页数限制", encoding="utf-8")
    assert detect_page_cap(ext) is None
