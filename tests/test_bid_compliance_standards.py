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


def test_law_name_effect_date_is_not_reported_as_date_leak():
    """《安全生产法》自2021年9月1日起施行 是法规引用,不是工期日期泄漏。"""
    from bid_compliance import _prose_abs_dates
    text = (
        "本工程施工依据《中华人民共和国安全生产法》自2021年9月1日起施行。\n"
        "《建设工程质量管理条例》(国务院令第279号)自2000年1月30日起施行。\n"
    )
    assert _prose_abs_dates(text, []) == []


def test_planned_calendar_date_is_still_reported():
    from bid_compliance import _prose_abs_dates
    hits = _prose_abs_dates("计划于2026年10月2日开工。\n", [])
    assert hits, "真实工期日期必须仍被报出"


def test_reference_words_must_be_adjacent_to_date():
    """引用来源类豁免要求引用词紧邻日期:夹实词(+)不得豁免真实排期日期。"""
    from bid_compliance import _prose_abs_dates

    assert _prose_abs_dates("获取开工许可后于2026年8月1日开工。\n", [])
    assert _prose_abs_dates("信息价采集完成后于2026年8月1日进场施工。\n", [])
    assert _prose_abs_dates("投标截止时间为2026年9月16日。\n", []) == []
    assert _prose_abs_dates("造价信息发布日期为2026年8月1日。\n", []) == []


def test_certification_dates_are_exempt_but_milestone_dates_are_not():
    """体系认证类日期是既成事实(豁免);开工/竣工/许可日期是计划工期(必须报出)。"""
    from bid_compliance import _prose_abs_dates

    assert _prose_abs_dates("本公司于2022年6月30日通过ISO9001认证。\n", []) == []
    assert _prose_abs_dates("试验室于2025年3月1日取得计量认证。\n", []) == []
    assert _prose_abs_dates("本工程于2027年10月1日竣工。\n", [])
    assert _prose_abs_dates("于2026年10月2日取得施工许可。\n", [])


def test_pre_baseline_date_within_one_year_is_still_reported():
    """基准前 1 年内的日期仍可能是写错的计划日期(必须报);超 1 年当历史事实(豁免)。"""
    from datetime import date

    from bid_compliance import _prose_abs_dates

    baseline = date(2026, 10, 2)
    assert _prose_abs_dates("本工程计划于2026年7月1日开工。\n", [], baseline=baseline)
    assert _prose_abs_dates("2020年8月10日竣工验收的类似工程。\n", [], baseline=baseline) == []
