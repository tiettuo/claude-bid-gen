from bid_plan_synthesize import (
    _figure_count,
    _score_item_subitem_count,
    assign_weights,
    llm_plan_subitems,
    repair_technical_criteria_alignment,
    score_item_subitems,
)


class _Log:
    def info(self, *args, **kwargs):
        pass


def test_passfail_outline_subsections_are_never_merged():
    original = [f"原文子节{i}" for i in range(1, 15)]

    subs = llm_plan_subitems("绿色施工管理体系与措施", original, 1249, 11, "", _Log())

    assert len(subs) == len(original)
    assert [s["covers"] for s in subs] == [[s] for s in original]
    assert sum(s["chars"] for s in subs) == 1249


def test_standard_passfail_outline_prioritizes_construction_plan():
    topics = [
        "工程概况", "编制依据", "总体施工部署", "项目组织管理机构",
        "施工总进度计划", "施工总平面布置", "施工方案与技术措施",
        "质量管理体系与措施", "工期保证技术组织措施",
        "安全文明施工体系与措施", "绿色施工管理体系与措施",
        "季节性施工措施", "地下管线及其他地上地下设施的保护加固措施",
        "民工工资保证措施", "附图及附表", "其他",
    ]

    weights = assign_weights(topics)

    assert sum(weights.values()) == 100
    assert weights["施工方案与技术措施"] == 27
    assert weights["施工方案与技术措施"] > weights["附图及附表"]


def test_required_attachment_charts_are_budgeted_in_attachment_chapter():
    assert _figure_count("施工总进度计划") == 0
    assert _figure_count("施工总平面布置") == 0
    assert _figure_count("附图及附表") == 2


def test_score_item_subitems_expands_construction_plan_for_complex_work():
    item = {
        "title_original": "施工方案与技术措施",
        "score": 3,
        "criteria_original": "对工程重点、难点的认识，解决方案是否科学、系统、安全、经济，可操作性，保障措施是否得力。",
    }
    subs = score_item_subitems(item, 4200, 1, "人居环境整治", work_complexity=4)
    assert len(subs) >= 6
    assert any("园林绿化" in s["title"] for s in subs)


def test_score_item_subitems_keeps_warranty_compact():
    item = {
        "title_original": "工程竣工后的保修措施",
        "score": 2,
        "criteria_original": "工程竣工后的保修措施是否合理，保修措施是否具体。",
    }
    subs = score_item_subitems(item, 2400, 7, "人居环境整治", work_complexity=4)
    assert len(subs) == 3


def test_score_item_count_for_resource_plan_has_minimum_five():
    count = _score_item_subitem_count(
        "劳动力投入计划及保证措施",
        "劳动力的投入能否满足施工需要；机械设备的投入能否满足施工需要；调配计划是否合理；保证措施是否具体。",
        3,
        work_complexity=2,
    )
    assert count == 5


def test_repair_technical_criteria_alignment_semantically_reorders_shifted_rows():
    items = [
        {
            "section": "技术",
            "title_original": "施工方案与技术措施",
            "score": 3,
            "criteria_original": "工程质量保证措施计划是否全面细致、承诺工程质量标准。",
        },
        {
            "section": "技术",
            "title_original": "质量管理体系与措施",
            "score": 3,
            "criteria_original": "安全文明施工保证计划是否全面周到。",
        },
        {
            "section": "技术",
            "title_original": "工程进度计划与措施",
            "score": 3,
            "criteria_original": "对工程重点、难点的认识，解决方案是否科学，关键技术是否到位。",
        },
    ]
    repaired = repair_technical_criteria_alignment(items)

    by_title = {i["title_original"]: i["criteria_original"] for i in repaired}
    assert "关键技术" in by_title["施工方案与技术措施"]
    assert "质量" in by_title["质量管理体系与措施"]


def test_repair_keeps_generic_completeness_criteria_locked():
    completeness = "对本项目总体认识深刻，方案完整，进度、质量、安全、环保及文明施工措施具有针对性。"
    safety = "建立专职安全监督机构，有完整可操作的安全生产管理制度和劳动保护措施。"
    items = [
        {
            "section": "技术",
            "title_original": "内容完整性和编制水平",
            "score": 4,
            "criteria_original": completeness,
        },
        {
            "section": "技术",
            "title_original": "安全管理体系与措施",
            "score": 4,
            "criteria_original": safety,
        },
        {
            "section": "技术",
            "title_original": "质量管理体系与措施",
            "score": 4,
            "criteria_original": "质量目标明确，对工程、材料、设备和成品质量制定控制措施。",
        },
    ]

    repaired = repair_technical_criteria_alignment(items)
    by_title = {i["title_original"]: i["criteria_original"] for i in repaired}
    assert by_title["内容完整性和编制水平"] == completeness
    assert by_title["安全管理体系与措施"] == safety


# ── 规模判档矩阵 (memory/feedback_bid_size_judge_matrix.md) ──────────────
from bid_plan_synthesize import (  # noqa: E402
    _CHECKBOX_PAGE_CAP_RE,
    _read_page_cap_from_rules,
    detect_project_type,
    judge_size_matrix,
)


def test_size_matrix_small_medium_when_only_duration_is_large():
    """周转房(1113m²/365 天/混凝土 957m³):仅工期判大型 → 平局取保守中小型 70 页。"""
    m = judge_size_matrix(None, 365, "building", "美巴切勤乡1#周转房24套1113.76m²及附属工程", 957.0, 869.0)
    assert m["size_class"] == "small_medium"
    assert m["small"] >= 2 and m["large"] == 1
    verdicts = {dim: v for dim, v, _ in m["rows"]}
    assert verdicts["工程类型"] == "中小型"
    assert verdicts["单专业体量"] == "中小型"
    assert verdicts["招标控制价"] == "未明示"


def test_size_matrix_large_when_three_dims_large():
    m = judge_size_matrix(2e8, 900, "building", "超高层综合体项目", 80000.0, 12000.0)
    assert m["size_class"] == "large"
    assert m["large"] >= 3


def test_size_matrix_pipe_length_ignored_for_building():
    """房建的电气配管动辄数公里,不得据此判大型(市政口径才看管线)。"""
    m = judge_size_matrix(None, 200, "building", "周转房", 900.0, 9999.0)
    assert {d: v for d, v, _ in m["rows"]}["单专业体量"] == "中小型"


def test_detect_project_type_recognizes_turnover_housing():
    facts = {"project_name": {"value": "谢通门县乡镇周转房建设项目 二次"},
             "project_scope": {"value": "7标段为美巴切勤乡周转房：24套1113.76m²及附属工程"}}
    assert detect_project_type(facts, []) == "building"


def test_checkbox_page_cap_detects_dual_option():
    tender = "大型复杂项目（如高层建筑、桥梁等）总页数不得超过100页。中小型项目（如普通住宅、厂房等）总页数不得超过70页。"
    assert _CHECKBOX_PAGE_CAP_RE.search(tender)
    assert not _CHECKBOX_PAGE_CAP_RE.search("总页数不得超过 300 页。")


def test_read_page_cap_from_rules_ignores_source_suffix(tmp_path):
    p = tmp_path / "authoring_rules.md"
    p.write_text("format:\n  page_hard_cap: 70\n  page_hard_cap_source: tender_regex(unknown)\n",
                 encoding="utf-8")
    assert _read_page_cap_from_rules(p) == 70
    p.write_text("format:\n  page_hard_cap_source: tender_regex\n", encoding="utf-8")
    assert _read_page_cap_from_rules(p) is None
    assert _read_page_cap_from_rules(tmp_path / "missing.md") is None


def test_boq_volume_stats_survives_array_shaped_boq(tmp_path):
    """旧提取器把 boq.json 写成裸数组 → 不得 AttributeError 掀翻 Step 2。"""
    import json as _json

    from bid_plan_synthesize import _boq_volume_stats
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "boq.json").write_text(_json.dumps([{"name": "x"}]), encoding="utf-8")
    assert _boq_volume_stats(ext) == {"concrete_m3": 0.0, "pipe_m": 0.0}
    (ext / "boq.json").write_text(_json.dumps({"categories": [None, 42]}), encoding="utf-8")
    assert _boq_volume_stats(ext) == {"concrete_m3": 0.0, "pipe_m": 0.0}


def test_h2_h3_font_follow_body_when_rules_default(tmp_path):
    """招标"全文宋体四号"项目:rules 里 H2/H3 带 source=default 时不得让 H3 停在小四 12pt。"""
    import logging

    from bid_assemble import parse_format_rules
    rules = tmp_path / "disqualification_rules.md"
    rules.write_text(
        "```yaml\nformat:\n"
        "  font_name_cn: 宋体\n"
        "  font_size_body: 14\n"
        "  font_size_body_source: tender_regex\n"
        "  heading_h2_size_pt: 14\n"
        "  heading_h2_size_pt_source: default\n"
        "  heading_h3_size_pt: 12\n"
        "  heading_h3_size_pt_source: default\n"
        "```\n",
        encoding="utf-8",
    )
    fmt = parse_format_rules(rules, logging.getLogger(__name__))
    assert fmt["font_size_body"] == 14
    assert fmt["font_size_h3"] == 14, "H3 必须跟随正文四号,不得停在小四"
    assert fmt["font_size_h2"] == 14
