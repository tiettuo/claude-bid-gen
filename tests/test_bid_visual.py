"""OfficeCLI 视觉编排层：规则锁、命令编译、感知比较和流水线顺序。"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from docx import Document

import bid_officecli_style as style
import bid_run
from bid_visual_rules import compile_visual_rules, profile_key_for_variant
from bid_visual_similarity import compare


def _make_tender(tmp_path: Path) -> Path:
    tender = tmp_path / "tender"
    ext = tender / "extracted"
    out = tender / "output"
    ext.mkdir(parents=True)
    out.mkdir()
    (ext / "disqualification_rules.md").write_text(
        "# 规则\n```yaml\nformat:\n  blind_bid: true\n"
        "  no_color: true\n  body_size_pt: 12\n  font_chinese: 宋体\n"
        "  line_spacing_value: 1.5\n  margin_cm: {top: 2.5, bottom: 2.5, left: 2.5, right: 2.5}\n"
        "  cover_page: false\n```\n",
        encoding="utf-8",
    )
    (ext / "variants.json").write_text(json.dumps({"variants": [
        {"id": "V01", "format_set": "A", "company_name": ""},
        {"id": "V02", "format_set": "B", "company_name": ""},
    ]}, ensure_ascii=False), encoding="utf-8")
    return tender


def _make_docx(path: Path) -> None:
    doc = Document()
    doc.add_heading("第一章 总体部署", 1)
    doc.add_heading("1.1 总体目标", 2)
    doc.add_paragraph("正文内容保持不变。")
    doc.add_heading("1.1.1 管理原则", 3)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "事项"
    table.cell(0, 1).text = "要求"
    table.cell(1, 0).text = "检查"
    table.cell(1, 1).text = "闭环"
    doc.save(path)


def test_profile_selection_prefers_explicit_then_format_set():
    cfg = {"default_profile": "A", "profiles": {"A": {}, "B": {}, "C": {}}}
    assert profile_key_for_variant("V01", {"visual_profile": "C", "format_set": "B"}, cfg) == "C"
    assert profile_key_for_variant("V01", {"format_set": "B"}, cfg) == "B"
    assert profile_key_for_variant("V03", {}, cfg) == "C"


def test_compile_rules_preserves_hard_format_and_allows_visual_fields(tmp_path: Path):
    tender = _make_tender(tmp_path)
    rules = compile_visual_rules(tender, "V02")
    assert rules["profile"] == "B"
    assert "font_size_body" in rules["locked_fields"]
    assert "margin_left_cm" in rules["locked_fields"]
    assert "table_grammar" in rules["allowed"]
    assert "h2_align" in rules["allowed"]
    assert not any(x.startswith("font_") or x.startswith("margin_") for x in rules["allowed"])


def test_cell_border_grammars_are_structurally_distinct():
    border = "single;4;000000"
    full = style._cell_border_props("full_grid", border, 1, 1, 3, 3)
    horizontal = style._cell_border_props("horizontal_rules", border, 2, 2, 3, 3)
    frame = style._cell_border_props("outer_frame", border, 2, 1, 3, 3)
    assert full == {"border.all": border}
    assert horizontal["border.bottom"] == border
    assert "border.left" not in horizontal
    assert frame["border.left"] == border
    assert "border.bottom" not in frame


def test_build_commands_skips_image_paragraphs_inside_table_cells(monkeypatch, tmp_path: Path):
    docx = tmp_path / "legacy.docx"
    _make_docx(docx)

    def fake_query(_officecli, _docx, selector):
        if selector == "cell":
            return []
        return [
            "/body/p[1]/r[1]/drawing[1]/image[1]",
            "/body/tbl[1]/tr[1]/tc[1]/p[1]/r[1]/drawing[1]/image[1]",
        ]

    monkeypatch.setattr(style, "_query_results", lambda *_args: [])
    monkeypatch.setattr(style, "_query_paths", fake_query)
    rules = {
        "allowed": {"image_align": "center"},
        "format": {"line_thickness_pt": 0.5, "line_color": "000000"},
    }
    commands = style.build_commands("officecli", docx, rules)
    paths = [command["path"] for command in commands]
    assert "/body/p[1]" in paths
    assert not any("/tc[" in path for path in paths)


def test_build_commands_resolves_legacy_heading_style_ids(monkeypatch, tmp_path: Path):
    docx = tmp_path / "legacy-styles.docx"
    _make_docx(docx)
    monkeypatch.setattr(style, "_query_results", lambda *_args: [
        {"path": "/styles/21", "text": "heading 2",
         "format": {"id": "21", "name": "heading 2"}},
        {"path": "/styles/31", "text": "heading 3",
         "format": {"id": "31", "name": "heading 3"}},
    ])
    monkeypatch.setattr(style, "_query_paths", lambda *_args: [])
    rules = {
        "allowed": {"h2_align": "center", "h3_align": "left"},
        "format": {"line_thickness_pt": 0.5, "line_color": "000000"},
    }
    commands = style.build_commands("officecli", docx, rules)
    assert [command["path"] for command in commands] == ["/styles/21", "/styles/31"]


def test_validation_delta_uses_error_kinds_when_officecli_is_capped():
    before = {"count": 1000, "kinds": {"bad-ind", "bad-color"},
              "signatures": {"bad-ind|old-path"}}
    reordered = {"count": 1000, "kinds": {"bad-ind", "bad-color"},
                 "signatures": {"bad-ind|new-path"}}
    new_kind = {"count": 1000, "kinds": {"bad-ind", "bad-color", "bad-border"},
                "signatures": set()}
    assert style._validation_delta(before, reordered) == []
    assert style._validation_delta(before, new_kind) == ["bad-border"]


def test_pipeline_places_visual_steps_between_assemble_and_verify():
    a = bid_run.STEPS.index("assemble")
    assert bid_run.STEPS[a + 1:a + 5] == [
        "visual_style", "visual_render", "visual_verify", "verify"
    ]


@pytest.mark.skipif(not (shutil.which("officecli") or (Path.home() / ".local/bin/officecli").exists()),
                    reason="OfficeCLI not installed")
def test_officecli_apply_validates_and_keeps_h1(tmp_path: Path):
    tender = _make_tender(tmp_path)
    docx = tender / "output" / "V01_测试_施工组织设计.docx"
    _make_docx(docx)
    officecli = shutil.which("officecli") or str(Path.home() / ".local/bin/officecli")
    before = Document(docx).paragraphs[0].text
    result = style.apply_one(tender, "V01", docx, officecli, False,
                             logging.getLogger("test_visual"))
    assert result["status"] == "ok"
    assert Document(docx).paragraphs[0].text == before
    proc = subprocess.run([officecli, "validate", str(docx), "--json"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_visual_compare_detects_layout_change(tmp_path: Path):
    from PIL import Image, ImageDraw
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    ia = Image.new("L", (200, 200), "white")
    ib = Image.new("L", (200, 200), "white")
    ImageDraw.Draw(ia).rectangle((15, 20, 185, 50), fill="black")
    ImageDraw.Draw(ib).rectangle((15, 140, 185, 170), fill="black")
    ia.save(a)
    ib.save(b)
    metrics = compare(a, b)
    assert metrics["average_hash_distance"] > 0
    assert metrics["edge_similarity"] < 1.0


def test_blind_visual_pass_clears_identity_metadata(tmp_path: Path):
    tender = _make_tender(tmp_path)
    docx = tender / "output" / "V01_测试_施工组织设计.docx"
    _make_docx(docx)
    from bid_assemble import _apply_docx_metadata, parse_format_rules
    fmt = parse_format_rules(tender / "extracted" / "disqualification_rules.md",
                             logging.getLogger("test_blind_metadata"))
    _apply_docx_metadata(docx, {"company_name": "不得写入的单位名",
                         "metadata": {"author": "不得写入的作者"}},
                         logging.getLogger("test_blind_metadata"), fmt=fmt)
    cp = Document(docx).core_properties
    assert cp.author == ""
    assert cp.last_modified_by == ""
    assert cp.subject == ""


def test_metadata_only_mode_does_not_reapply_document_fonts(monkeypatch, tmp_path: Path):
    tender = _make_tender(tmp_path)
    docx = tender / "output" / "V01_测试_施工组织设计.docx"
    _make_docx(docx)
    import bid_assemble

    def unexpected(*_args, **_kwargs):
        raise AssertionError("metadata-only mode must not touch document fonts")

    monkeypatch.setattr(bid_assemble, "_reapply_heading_fonts", unexpected)
    monkeypatch.setattr(bid_assemble, "_reapply_h1_run_fonts", unexpected)
    monkeypatch.setattr(bid_assemble, "_reapply_table_fonts", unexpected)
    bid_assemble._apply_docx_metadata(
        docx, {"company_name": "不得写入"}, logging.getLogger("metadata_only"),
        fmt={"blind_bid": True}, reapply_fonts=False,
    )
    cp = Document(docx).core_properties
    assert cp.author == ""
    assert cp.subject == ""
