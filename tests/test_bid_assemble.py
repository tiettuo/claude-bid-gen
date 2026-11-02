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


# ─── _normalize_rule_param_keys (P3.3 抽出) ─────────────────────
def test_normalize_rule_param_keys_alias():
    """通俗键名映射到脚本内部键名。"""
    params = {"font_chinese": "仿宋", "heading_h1_size_pt": "16", "page_hard_cap": "400"}
    A._normalize_rule_param_keys(params, _LOG)
    assert params["font_name_cn"] == "仿宋"
    assert params["font_size_h1"] == "16"
    assert params["max_pages"] == "400"


def test_normalize_rule_param_keys_semantic_flip():
    """正向布尔翻转成脚本内部 no_* 反向布尔。"""
    params = {"page_numbers": "false", "cover_page": "false", "color_allowed": "false"}
    A._normalize_rule_param_keys(params, _LOG)
    assert params["no_page_numbers"] is True
    assert params["no_cover"] is True
    assert params["no_color"] is True


# ─── parse_format_rules 端到端(特征化)────────────────────────────
def test_parse_format_rules_end_to_end(tmp_path):
    rules = tmp_path / "disqualification_rules.md"
    rules.write_text(
        "## 排版格式硬约束\n```yaml\nformat:\n"
        "  body_size_pt: 14\n  font_chinese: 仿宋\n  line_spacing_value: 1.5\n"
        "  heading_h1_size_pt: 16\n  page_numbers: false\n  cover_page: false\n"
        "  page_hard_cap: 400\n  blind_bid: false\n```\n",
        encoding="utf-8")
    fmt = A.parse_format_rules(rules, _LOG)
    assert fmt["font_name_cn"] == "仿宋"
    assert fmt["font_size_body"] == 14.0
    assert fmt["font_size_h1"] == 16.0
    assert fmt["line_spacing"] == 1.5
    assert fmt["max_pages"] == 400
    assert fmt["no_cover"] is True
    assert fmt["no_page_numbers"] is True
    assert fmt["blind_bid"] is False


def test_parse_format_rules_missing_file_defaults(tmp_path):
    """规则文件不存在 → 返回默认格式,不崩。"""
    fmt = A.parse_format_rules(tmp_path / "不存在.md", _LOG)
    assert isinstance(fmt, dict)
    assert "font_size_body" in fmt


def _cover_fmt(**overrides):
    base = {
        "font_name_cn": "宋体", "font_name_en": "Times New Roman",
        "font_size_cover_title": 22, "font_size_cover_project": 16,
        "font_size_cover_meta": 14, "blind_bid": False, "no_company_info": False,
    }
    base.update(overrides)
    return base


def _cover_xml(fmt, company="西藏天路建设工程有限公司"):
    from docx import Document
    doc = Document()
    A._prepend_cover(doc, "某工程项目", company, fmt, logging.getLogger("test"))
    return doc.element.body.xml


def test_prepend_cover_no_company_info_hides_name():
    """no_company_info=true（即使非盲标）→ 封面不印公司名，留空下划线。"""
    xml = _cover_xml(_cover_fmt(no_company_info=True))
    assert "西藏天路建设工程有限公司" not in xml
    assert "投 标 人：" in xml


def test_prepend_cover_non_blind_prints_name():
    """非盲标且未禁公司名 → 封面印 company_name。"""
    xml = _cover_xml(_cover_fmt())
    assert "投 标 人：西藏天路建设工程有限公司" in xml


def test_prepend_cover_placeholder_name_is_not_printed():
    """占位公司名(甲公司/XX公司/待定…)不得当真名印上封面——应留填空线供人工填写。"""
    for ph in ("甲公司", "乙公司", "XX公司", "待定", "投标单位名称"):
        xml = _cover_xml(_cover_fmt(), company=ph)
        assert ph not in xml, f"占位名被印上封面: {ph}"
        assert "投 标 人：" in xml
