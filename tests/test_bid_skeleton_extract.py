"""测试 bid_skeleton_extract 的关键路径(B3 grep fallback / auto_detect_hybrid)。"""
import pytest

from bid_skeleton_extract import (
    _grep_implicit_constraints,
    auto_detect_hybrid,
    validate_b1,
    validate_b3,
)


# ─── B3 grep fallback ───

def test_b3_grep_finds_page_limit():
    text = """[第5页]
本项目为中小型项目,投标文件总页数不得超过 70 页。
"""
    out = _grep_implicit_constraints(text, set(), None)
    assert any(c["constraint_type"] == "page_limit" for c in out)
    assert any("70" in c["text_original"] for c in out)


def test_b3_grep_finds_deadline():
    text = """[第20页]
投标有效期不得少于 90 日历天。
"""
    out = _grep_implicit_constraints(text, set(), None)
    assert any(c["constraint_type"] == "deadline" for c in out)


def test_b3_grep_finds_qualification():
    text = """[第30页]
项目经理须为本单位注册一级建造师,具有市政工程一级资质。
"""
    out = _grep_implicit_constraints(text, set(), None)
    assert any(c["constraint_type"] == "qualification" for c in out)


def test_b3_grep_dedup_same_line():
    """同一行被多 pattern 命中时,只取最具体的一条(避免重复)"""
    text = """[第5页]
本项目为中小型项目,投标文件总页数不得超过 70 页。
"""
    out = _grep_implicit_constraints(text, set(), None)
    # 一行只应出 1 条 page_limit
    page_limit_hits = [c for c in out if c["constraint_type"] == "page_limit"]
    assert len(page_limit_hits) == 1


def test_b3_grep_skip_existing():
    """existing_keys 已有的不重复添加。"""
    text = "[第5页]\n总页数不得超过 70 页。"
    existing = {"总页数不得超过 70 页|5"}
    out = _grep_implicit_constraints(text, existing, None)
    assert len(out) == 0


def test_validate_b3_warns_no_constraint_type():
    """B3 全部 clauses 都没 constraint_type 时应警告。"""
    payload = {
        "clauses": [
            {"category": "报价", "constraint_type": None,
             "text_original": "x" * 10, "source_page": 1},
        ] * 6,
    }
    ok, warns = validate_b3(payload, 100)
    assert ok
    assert any("constraint_type" in w for w in warns)


# ─── auto_detect_hybrid ───

def test_hybrid_explicit_tech_passfail():
    """信号 1: tech_score_is_passfail=true + 商务标合计 > 0"""
    payload = {
        "evaluation_method": "score",
        "tech_score_is_passfail": True,
        "scoring_items": [
            {"title_original": "投标报价", "score": 70},
            {"title_original": "类似业绩", "score": 30},
        ],
    }
    triggered, msgs = auto_detect_hybrid(payload)
    assert triggered
    assert payload["evaluation_method"] == "passfail"
    assert payload["_hybrid_mode"] is True


def test_hybrid_implicit_tech_zero_score():
    """信号 2: 施工组织设计 score=0 + 其他项 score>0"""
    payload = {
        "evaluation_method": "score",
        "tech_score_is_passfail": False,
        "scoring_items": [
            {"title_original": "施工组织设计", "score": 0},
            {"title_original": "投标报价", "score": 70},
            {"title_original": "业绩", "score": 30},
        ],
    }
    triggered, msgs = auto_detect_hybrid(payload)
    assert triggered
    assert payload["evaluation_method"] == "passfail"


def test_hybrid_method_chinese_signal():
    """信号 3: method_chinese 含 合格制 / 不计分"""
    payload = {
        "evaluation_method": "score",
        "method_chinese": "施工组织设计合格制+商务评分",
        "tech_score_is_passfail": False,
        "scoring_items": [
            {"title_original": "投标报价", "score": 60},
        ],
    }
    triggered, msgs = auto_detect_hybrid(payload)
    assert triggered


def test_hybrid_pure_score_not_triggered():
    """纯评分制(技术标有分,商务标无分)不触发混合制。"""
    payload = {
        "evaluation_method": "score",
        "tech_score_is_passfail": False,
        "scoring_items": [
            {"title_original": "施工组织设计", "score": 60},
            {"title_original": "总平面布置", "score": 20},
            {"title_original": "进度计划", "score": 20},
        ],
    }
    triggered, msgs = auto_detect_hybrid(payload)
    assert not triggered
    assert payload.get("_hybrid_mode") is None


def test_validate_b1_chains_hybrid_detection():
    """validate_b1 内部调用 auto_detect_hybrid,仓储类项目B场景能自动归 passfail。"""
    payload = {
        "evaluation_method": "score",
        "method_chinese": "综合评估法",
        "total_tech_score": 100,
        "tech_score_is_passfail": True,
        "scoring_items": [
            {"title_original": "投标报价", "score": 50},
            {"title_original": "信用得分", "score": 25},
            {"title_original": "类似业绩", "score": 25},
        ],
        "veto_items": [{"text_original": "x", "source_page": 1}],
    }
    ok, warns = validate_b1(payload)
    assert ok
    assert payload["evaluation_method"] == "passfail"
    assert payload.get("_hybrid_mode") is True
