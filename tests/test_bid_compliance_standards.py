"""Tests for bid_compliance standard truth matching."""

import logging
from pathlib import Path

from bid_compliance import (
    check_standards_fabrication,
    normalize_standard_key,
    standard_lookup_candidates,
)


def test_normalize_standard_key_handles_spacing_and_slash():
    assert normalize_standard_key("GB / T", "50502") == "GB/T50502"
    assert normalize_standard_key("JGJ", "59") == "JGJ59"
    assert normalize_standard_key("DB54 / T", "0001") == "DB54/T0001"


def test_standard_lookup_candidates_try_gbt_variant():
    assert standard_lookup_candidates("GB", "50502") == ["GB50502", "GB/T50502"]
    assert standard_lookup_candidates("GB/T", "50502") == ["GB/T50502", "GB50502"]


def test_check_standards_fabrication_detects_wrong_year_for_gbt_written_as_gb(tmp_path: Path):
    md = tmp_path / "01_test.md"
    md.write_text("施工组织设计按 GB 50502-2023 和 JGJ 59-2020 执行。", encoding="utf-8")

    result = check_standards_fabrication(
        [md],
        {"GB/T50502": "2009", "JGJ59": "2011"},
        logging.getLogger("test"),
    )

    assert result["total"] == 2
    assert result["fabricated"] == 2
    assert {d["standard"] for d in result["details"]} == {"GB/T50502", "JGJ59"}
