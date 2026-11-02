"""bid_rules_synthesize 两个生产回归:

1. detect_blind_bid 误报 —— 西藏前附表固定问「施工组织设计是否采用“暗标”评审方式： | ☑不采用」,
   关键词粗扫见到"暗标"二字即判 true,导致 writer 全文改"本公司"、compliance 全篇误报。
   (2026-09 某西藏村居暖圈项目实测)
2. 页数硬顶漏抽 —— 「总页数控制在500页内（含500页）」不被"不得超过/限N页"两条 pattern 命中,
   页数硬顶整条丢失 → 目标页数回落默认值。
"""

import logging

import bid_rules_synthesize as R

LOG = logging.getLogger("test")


# ─── 暗标判定 ────────────────────────────────────────────────────────

def test_blind_not_adopted_checkbox():
    text = "| 9.2 | 施工组织设计是否采用“暗标”评审方式： | ☑不采用。  施工组织设计编制要求：……"
    blind, no_company, scope = R.detect_blind_bid({}, text)
    assert blind is False
    assert "不采用" in scope


def test_blind_no_blind_review_line():
    text = "评标参数  评标办法：综合评估法  是否有暗标评审：无  资格审查方式：资格后审"
    assert R.detect_blind_bid({}, text)[0] is False


def test_blind_adopted_checkbox_still_true():
    text = "施工组织设计是否采用“暗标”评审方式： | ☑采用。投标人应严格按暗标要求编制。"
    assert R.detect_blind_bid({}, text)[0] is True


def test_blind_bare_keyword_still_true():
    assert R.detect_blind_bid({}, "本项目技术标采用暗标评审。")[0] is True


def test_blind_no_company_only():
    blind, no_company, _ = R.detect_blind_bid({}, "投标文件不得出现公司名称。")
    assert (blind, no_company) == (False, True)


def test_blind_conditional_outline_not_misfire():
    """大纲章的"若…采用技术暗标…则…"条件句不得让"无暗标"项目判成暗标。"""
    text = (
        "| 9.2 | 施工组织设计是否采用“暗标”评审方式： | ☑不采用。\n"
        "2. 若投标人须知规定施工组织设计采用技术“暗标”方式评审，则施工组织设计的编制和装订应按附表七编制。\n"
    )
    assert R.detect_blind_bid({}, text)[0] is False


# ─── 页数硬顶 ────────────────────────────────────────────────────────

def test_page_cap_controlled_within():
    text = "整体施工组织设计文件的总页数控制在500页内（含500页），未按要求编制施工组织设计的投标人施工组织设计评分得0分。"
    got = R.extract_authoring_format_spec(text, LOG, project_size="small_medium")
    assert got["page_hard_cap"]["value"] == 500


def test_page_cap_legacy_phrasings():
    for text, want in [
        ("中小型项目总页数不得超过70页。", 70),
        ("本项目总页数不得超过100页。", 100),
        ("施工组织设计篇幅限300页。", 300),
        ("技术标总页数不超过80页。", 80),
    ]:
        got = R.extract_authoring_format_spec(text, LOG, project_size="small_medium")
        assert got.get("page_hard_cap", {}).get("value") == want, (text, got)
