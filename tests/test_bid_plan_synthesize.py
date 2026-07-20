from bid_plan_synthesize import (
    _score_item_subitem_count,
    repair_technical_criteria_alignment,
    score_item_subitems,
)


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
