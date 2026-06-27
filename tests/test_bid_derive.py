"""测试 bid_derive 的字数密度查表 + 字号解析。"""
import pytest

from bid_derive import (
    chars_per_page,
    recommend_target_chars,
    resolve_font_size_pt,
    parse_format_spec_from_rules,
)


def test_font_size_explicit_cn():
    assert resolve_font_size_pt("四号") == 14
    assert resolve_font_size_pt("小四") == 12
    assert resolve_font_size_pt("五号") == 10.5


def test_font_size_xiao_priority():
    """小 X 号优先于 X 号(避免'小四号'被'四号'先命中)"""
    assert resolve_font_size_pt("小四号正文") == 12
    assert resolve_font_size_pt("小三号") == 15
    assert resolve_font_size_pt("小四号") == 12


def test_font_size_numeric():
    assert resolve_font_size_pt("14pt") == 14.0
    assert resolve_font_size_pt("12") == 12.0


def test_chars_per_page_14pt_15():
    """14pt + 1.5倍 → 400-450(全文档有效密度,聂荣实测415;用户指定 2026-06-11)"""
    lo, hi = chars_per_page(14, "multiple", 1.5)
    assert lo == 400
    assert hi == 450


def test_chars_per_page_12pt_15():
    """12pt + 1.5倍 → 850-950(老配置常用密度)"""
    lo, hi = chars_per_page(12, "multiple", 1.5)
    assert (lo, hi) == (850, 950)


def test_chars_per_page_fallback():
    """未命中查表时返回保守区间。"""
    lo, hi = chars_per_page(20, "multiple", 1.5)
    # 20pt 没在表里,应退化到 16pt 项或保守默认
    assert lo > 0 and hi > lo


def test_recommend_target_chars_14pt_70p():
    """14pt + 1.5倍 + 70 页(西藏 70 页硬顶常见场景)"""
    rec = recommend_target_chars(70, 14, "multiple", 1.5, figure_ratio=0.25)
    assert rec["density_lo"] == 400
    assert rec["density_hi"] == 450
    assert rec["density_mid"] == 425
    # 70 × 0.75 文字页 × 425 中位 ≈ 22K
    assert 18000 < rec["target_chars_mid"] < 26000


def test_parse_format_spec_yaml_style():
    rules_text = """
# 字体设置
font_size_body: 四号
font_size_table: 五号
line_spacing: 1.5
line_spacing_rule: multiple
"""
    spec = parse_format_spec_from_rules(rules_text)
    assert spec["font_size_pt"] == 14
    assert spec["line_spacing_kind"] == "multiple"
    assert spec["line_spacing_value"] == 1.5


def test_parse_format_spec_chinese_colon():
    rules_text = """
font_size_body:四号
line_spacing:1.5倍
"""
    spec = parse_format_spec_from_rules(rules_text)
    assert spec["font_size_pt"] == 14
    assert spec["line_spacing_value"] == 1.5


def test_parse_format_spec_fixed_pt():
    rules_text = """
font_size_body: 三号
line_spacing: 28磅
"""
    spec = parse_format_spec_from_rules(rules_text)
    assert spec["font_size_pt"] == 16
    assert spec["line_spacing_kind"] == "fixed_pt"
    assert spec["line_spacing_value"] == 28
