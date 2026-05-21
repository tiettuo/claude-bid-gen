"""bid_assemble 纯函数特征化测试 —— 锁定格式参数解析/字号换算/format_sets
行为(P3)。覆盖养殖类项目踩过的 format_set / 字号 相关路径。"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bid_assemble as A

_LOG = logging.getLogger("test_assemble")
_LOG.addHandler(logging.NullHandler())


# ─── _coerce_bool ───────────────────────────────────────────────
def test_coerce_bool_truthy():
    for v in ("true", "yes", "是", "1", "on", True):
        assert A._coerce_bool(v) is True


def test_coerce_bool_falsy():
    for v in ("false", "no", "否", "0", "", False):
        assert A._coerce_bool(v) is False


# ─── _coerce_number ─────────────────────────────────────────────
def test_coerce_number_extracts_digits():
    assert A._coerce_number("14pt") == 14.0
    assert A._coerce_number("2.5cm") == 2.5


def test_coerce_number_default_on_garbage():
    assert A._coerce_number("无数字", 9) == 9


# ─── _resolve_font_size ─────────────────────────────────────────
def test_resolve_font_size_chinese_name():
    assert A._resolve_font_size("小四") == 12
    assert A._resolve_font_size("四号") == 14


def test_resolve_font_size_mixed_text():
    """含中文字号名的混合文本也能解析。"""
    assert A._resolve_font_size("三号正文") == 16


def test_resolve_font_size_arabic():
    assert A._resolve_font_size("14pt") == 14.0


# ─── _parse_structured_block ────────────────────────────────────
def test_parse_structured_block_scalar():
    out = A._parse_structured_block("```yaml\nbody_size_pt: 14\n```")
    assert out["body_size_pt"] == "14"


def test_parse_structured_block_inline_dict_expands():
    """margin_cm: {top: 2.5, bottom: 2.6} → 展开成 margin_top_cm / margin_bottom_cm。"""
    out = A._parse_structured_block(
        "```yaml\nmargin_cm: {top: 2.5, bottom: 2.6}\n```")
    assert out["margin_top_cm"] == "2.5"
    assert out["margin_bottom_cm"] == "2.6"


def test_parse_structured_block_skips_comments():
    out = A._parse_structured_block("```yaml\n# 注释行\nbody_size_pt: 14\n```")
    assert "body_size_pt" in out
    assert len(out) == 1


# ─── _parse_format_sets ─────────────────────────────────────────
def test_parse_format_sets():
    text = ("```yaml\nformat_sets:\n  A:\n    font_name_cn: 宋体\n"
            "    line_spacing: 1.5\n  B:\n    font_name_cn: 仿宋\n```")
    fs = A._parse_format_sets(text)
    assert set(fs.keys()) == {"A", "B"}
    assert fs["A"]["font_name_cn"] == "宋体"
    assert fs["A"]["line_spacing"] == 1.5


def test_parse_format_sets_absent_yields_empty():
    assert A._parse_format_sets("没有 format_sets 块的文本") == {}


# ─── _apply_format_set ──────────────────────────────────────────
def test_apply_format_set_overrides():
    """format_set 的字段覆盖 fmt,未涉及字段保留。"""
    out = A._apply_format_set(
        {"font_name_cn": "仿宋", "keep": 1}, {"font_name_cn": "楷体"}, _LOG)
    assert out["font_name_cn"] == "楷体"
    assert out["keep"] == 1
