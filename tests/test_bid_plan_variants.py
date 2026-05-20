import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bid_plan_variants import _variant_subitem_counts


def test_variant_subitem_counts_keep_first_variant_on_base_count():
    assert _variant_subitem_counts(7, 3) == [7, 6, 8]
    assert _variant_subitem_counts(5, 3) == [5, 4, 6]


def test_variant_subitem_counts_do_not_drop_below_three():
    assert _variant_subitem_counts(3, 3) == [3, 3, 4]
