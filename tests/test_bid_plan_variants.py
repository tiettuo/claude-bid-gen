import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_plan_variants import (
    _DECOMP_STRATEGIES,
    _fallback_subitems,
    _is_tier_a,
    _normalize_subitems,
    assign_strategy,
    build_variant_chapters,
    verify_scoring_coverage,
)

_LOG = logging.getLogger("test_plan_variants")


def test_assign_strategy_distinct_across_variants():
    """前 N(≤维度池大小)个变体拿到的切分维度必须互不相同。"""
    names = {assign_strategy(i)[0] for i in range(3)}
    assert len(names) == 3
    # 越界按维度池大小回绕
    assert assign_strategy(0) == assign_strategy(len(_DECOMP_STRATEGIES))


def test_build_variant_chapters_keeps_h1_titles():
    """★ 核心:一级标题逐字沿用基准,变体绝不改写。"""
    base = [
        {"num": "01", "title": "采购组织、供货计划", "score_item": "技术评分项 #1",
         "score": 10, "target_chars": 34200, "chart_count": 6, "_subitems": []},
        {"num": "07", "title": "保证措施（现场文明施工、施工安全、环境保护）",
         "score_item": "技术评分项 #7", "score": 4, "target_chars": 13680,
         "chart_count": 5, "_subitems": []},
    ]
    variant = {"id": "V01", "model": "deepseek", "temperature": 0.5,
               "section_order_seed": 123}
    chapters = build_variant_chapters(base, variant, scale=1.0)
    # 标题、章号、数量、顺序均与基准一致
    assert [c["title"] for c in chapters] == [b["title"] for b in base]
    assert [c["num"] for c in chapters] == ["01", "07"]
    assert len(chapters) == len(base)


def test_build_variant_chapters_scales_chars():
    """字数按缩放因子 + ±8% jitter 重设。"""
    base = [{"num": "01", "title": "X", "score_item": "s", "score": 10,
             "target_chars": 30000, "chart_count": 0, "_subitems": []}]
    v = {"id": "V02", "model": "deepseek", "temperature": 0.7,
         "section_order_seed": 7}
    scaled = build_variant_chapters(base, v, scale=0.9)[0]["target_chars"]
    assert 30000 * 0.9 * 0.92 <= scaled <= 30000 * 0.9 * 1.08


def test_is_tier_a_by_score_item():
    assert _is_tier_a({"score_item": "技术评分项 #1", "score": 10})
    assert not _is_tier_a({"score_item": "—", "score": 0})
    assert not _is_tier_a({"score_item": "", "score": 0})


def test_verify_scoring_coverage_raises_on_missing():
    base = {"技术评分项 #1", "技术评分项 #2"}
    verify_scoring_coverage(
        [{"score_item": "技术评分项 #1"}, {"score_item": "技术评分项 #2"}], base, _LOG
    )
    with pytest.raises(RuntimeError):
        verify_scoring_coverage([{"score_item": "技术评分项 #1"}], base, _LOG)


def test_normalize_subitems_sums_to_target():
    subs = [{"title": "a", "chars": 1000}, {"title": "b", "chars": 2000},
            {"title": "c", "chars": 1500}]
    out = _normalize_subitems(subs, 9000)
    assert sum(s["chars"] for s in out) == 9000


def test_fallback_subitems_uses_scope():
    ch = {"title": "采购组织", "target_chars": 12000,
          "_scope": ["范围界定", "组织机构", "供应商管理"]}
    subs = _fallback_subitems(ch)
    assert [s["title"] for s in subs] == ["范围界定", "组织机构", "供应商管理"]
    assert len(subs) == 3
