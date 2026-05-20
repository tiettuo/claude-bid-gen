import logging
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_plan_variants import (
    _is_tier_a,
    _pick_title,
    _seeded_subitem_count,
    verify_scoring_coverage,
)

_LOG = logging.getLogger("test_plan_variants")


def test_seeded_subitem_count_never_below_three():
    for base in range(0, 12):
        for seed in range(20):
            assert _seeded_subitem_count(base, random.Random(seed)) >= 3


def test_seeded_subitem_count_stays_near_base():
    # base=8 → 落在 [base-2, base+2]
    for seed in range(50):
        n = _seeded_subitem_count(8, random.Random(seed))
        assert 6 <= n <= 10


def test_pick_title_distinct_across_variants():
    """同一章在 3 个变体里拿到的同义标题必须互不相同(打破标题雷同)。"""
    titles = {_pick_title("质量管理体系与措施", i) for i in range(3)}
    assert len(titles) == 3
    # 评分锚定章标题必须保留主题关键词,确保评委能定位评分项
    for i in range(3):
        assert "质量" in _pick_title("质量管理体系与措施", i)


def test_pick_title_unknown_chapter_unchanged():
    assert _pick_title("某个没有同义池的章节", 0) == "某个没有同义池的章节"


def test_is_tier_a_by_score_item():
    assert _is_tier_a({"score_item": "2.2.4(1)-1", "score": 3})
    assert not _is_tier_a({"score_item": "—", "score": 0})
    assert not _is_tier_a({"score_item": "", "score": 0})


def test_verify_scoring_coverage_raises_on_missing():
    base = {"2.2.4(1)-1", "2.2.4(1)-2"}
    verify_scoring_coverage(
        [{"score_item": "2.2.4(1)-1"}, {"score_item": "2.2.4(1)-2"}], base, _LOG
    )
    with pytest.raises(RuntimeError):
        verify_scoring_coverage([{"score_item": "2.2.4(1)-1"}], base, _LOG)
