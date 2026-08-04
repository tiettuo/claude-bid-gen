"""bid_charts 纯函数特征化测试 —— 锁定围栏解析/类型判定/编号/适配器行为,
为后续重构提供安全网(P3)。"""
import sys
import hashlib
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bid_charts as C
from bid_chart_specs import classify_by_title, parse_subitems


def test_chart_spec_subitem_parser_keeps_attachment_title_after_colon():
    plan = """
### 15 附图及附表(382 字,2 图表,2.6 页)
- 15.4 附表四：计划开、竣工日期和施工进度网络图:76 字
- 15.5 附表五：施工总平面图:78 字
"""

    parsed = parse_subitems(plan)

    assert parsed["15"][0][1] == "附表四：计划开、竣工日期和施工进度网络图"
    assert parsed["15"][1][1] == "附表五：施工总平面图"


def test_required_progress_network_uses_aoa_renderer():
    assert classify_by_title("附表四：计划开、竣工日期和施工进度网络图") == "aoa_network"


def test_schedule_layouts_render_structurally_distinct_pngs(tmp_path):
    data = {
        "title": "测试横道图",
        "tasks": [
            {"name": "施工准备", "start": 1, "end": 5, "phase": "准备"},
            {"name": "钢结构安装", "start": 6, "end": 18, "phase": "主体"},
            {"name": "收尾验收", "start": 19, "end": 25, "phase": "验收"},
        ],
        "milestones": [{"name": "主体完成", "date": 18}],
        "project": {"start_date": "2026-01-01", "total_duration_days": 25},
    }
    hashes = set()
    for layout in ("classic_table", "phase_swimlane", "calendar_matrix"):
        path = tmp_path / f"{layout}.png"
        assert C.render_schedule_png(data, path, data["title"], logging.getLogger(__name__),
                                     {"gantt_layout": layout})
        assert path.stat().st_size > 10_000
        hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(hashes) == 3


def test_schedule_accepts_chinese_day_labels_and_duration(tmp_path):
    data = {
        "tasks": [
            {"name": "准备", "start": "第1天", "end": "第3天", "phase": "准备"},
            {"name": "安装", "start": 4, "duration": 5, "phase": "主体"},
        ],
        "project": {"start_date": "2026-01-01"},
    }
    out = tmp_path / "schedule.png"
    assert C.render_schedule_png(data, out, "测试", logging.getLogger(__name__),
                                 {"gantt_layout": "phase_swimlane"})
    assert out.stat().st_size > 10_000


# ─── iter_fences ────────────────────────────────────────────────
def test_iter_fences_extracts_lang_and_content():
    blocks = list(C.iter_fences("前文\n```json\n{\"a\":1}\n```\n后文"))
    assert len(blocks) == 1
    assert blocks[0].lang == "json"
    assert blocks[0].content.strip() == '{"a":1}'


def test_iter_fences_none_when_no_fence():
    assert list(C.iter_fences("纯文本没有围栏")) == []


# ─── _unwrap_data ───────────────────────────────────────────────
def test_unwrap_data_merges_inner():
    assert C._unwrap_data({"data": {"a": 1}, "b": 2}) == {"a": 1, "b": 2}


def test_unwrap_data_passthrough_when_no_data_key():
    assert C._unwrap_data({"a": 1}) == {"a": 1}


# ─── classify_json ──────────────────────────────────────────────
def test_classify_json_english_alias():
    assert C.classify_json({"type": "gantt"}) == "schedule"
    assert C.classify_json({"type": "site_layout"}) == "site_layout"


def test_classify_json_chinese_keyword():
    assert C.classify_json({"type": "组织机构图"}) == "org_chart"


def test_classify_json_by_structure():
    assert C.classify_json({"tasks": [{"name": "x", "start": 1}]}) == "schedule"


def test_classify_json_non_dict_is_other():
    assert C.classify_json("foo") == "other"
    assert C.classify_json([1, 2]) == "other"


# ─── 中文数字 ────────────────────────────────────────────────────
def test_cn_num():
    assert C._cn_num(1) == "一"
    assert C._cn_num(10) == "十"
    assert C._cn_num(12) == "十二"
    assert C._cn_num(25) == "二十五"


# ─── _prepend_chapter_number ────────────────────────────────────
def test_prepend_chapter_number_adds_prefix():
    assert C._prepend_chapter_number("# 采购方案", 3) == "# 第三章 采购方案"


def test_prepend_chapter_number_idempotent():
    once = C._prepend_chapter_number("# 采购方案", 3)
    assert C._prepend_chapter_number(once, 3) == once


def test_prepend_chapter_number_idx0_noop():
    assert C._prepend_chapter_number("# 采购方案", 0) == "# 采购方案"


# ─── _normalize_heading_numbers ─────────────────────────────────
def test_normalize_heading_numbers_styleB():
    out = C._normalize_heading_numbers("## 施工方案\n### 细节", 7, "B")
    assert out == "## 7.1 施工方案\n### 7.1.1 细节"


def test_normalize_heading_numbers_styleB_no_leading_zero():
    out = C._normalize_heading_numbers("## 施工方案\n### 细节", 1, "B")
    assert out == "## 1.1 施工方案\n### 1.1.1 细节"
    assert "01." not in out


def test_normalize_heading_numbers_does_not_create_zero_section():
    out = C._normalize_heading_numbers("### 工期控制中的文明施工协同要求\n## 进度计划", 9, "B")
    assert "9.0.1" not in out
    assert out.splitlines()[0] == "### 工期控制中的文明施工协同要求"
    assert out.splitlines()[1] == "## 9.1 进度计划"


def test_normalize_heading_numbers_strips_existing_number():
    """LLM 已带序号时先剥离再重编,不叠加。"""
    out = C._normalize_heading_numbers("## 3.2 施工方案", 7, "B")
    assert "施工方案" in out
    assert "3.2" not in out


# ─── _normalize_md_lists ────────────────────────────────────────
def test_normalize_md_lists_flattens_indented_bullets():
    out = C._normalize_md_lists("段落\n    *   **要点A**\n    *   要点B")
    assert "- **要点A**" in out
    assert "- 要点B" in out
    assert "*   " not in out


# ─── _adapt_site_layout_data ────────────────────────────────────
def test_adapt_site_layout_from_elements():
    """elements 矩形列表 → 四方位 zones(P0/P2 期修过的关键路径)。"""
    data = {
        "type": "site_layout",
        "elements": [
            {"label": "鸡舍", "x": 100, "y": 100, "width": 200, "height": 200},
            {"label": "办公区", "x": 400, "y": 100, "width": 100, "height": 80},
            {"label": "材料堆场", "x": 100, "y": 400, "width": 100, "height": 80},
        ],
    }
    out = C._adapt_site_layout_data(data)
    zones = out["zones"]
    assert set(zones.keys()) == {"north", "south", "east", "west"}
    placed = [v for vs in zones.values() for v in vs]
    assert "办公区" in placed and "材料堆场" in placed


def test_adapt_site_layout_empty_yields_empty_zones():
    out = C._adapt_site_layout_data({"type": "site_layout"})
    assert all(out["zones"][d] == [] for d in ("north", "south", "east", "west"))


# ─── validate_chart_data (P3.2) ─────────────────────────────────
def test_validate_chart_data_schedule_ok():
    ok, _ = C.validate_chart_data("schedule", {"tasks": [{"name": "t"}]})
    assert ok


def test_validate_chart_data_schedule_missing():
    ok, reason = C.validate_chart_data("schedule", {})
    assert not ok and "tasks" in reason


def test_validate_chart_data_site_layout_empty_rejected():
    """zones 全空 + 无 elements → 判不可渲染(养殖类项目踩过的空图)。"""
    empty = {"zones": {"north": [], "south": [], "east": [], "west": []}}
    ok, reason = C.validate_chart_data("site_layout", empty)
    assert not ok


def test_validate_chart_data_site_layout_with_facilities_ok():
    ok, _ = C.validate_chart_data(
        "site_layout", {"zones": {"north": ["鸡舍"], "south": [], "east": [], "west": []}})
    assert ok


def test_validate_chart_data_unknown_type_passes():
    """未登记类型不拦(保守:不误杀)。"""
    ok, _ = C.validate_chart_data("table", {"anything": 1})
    assert ok


def test_schedule_project_timing_sets_relative_baseline():
    data = {"type": "schedule", "tasks": [{"name": "施工准备", "start": 1, "end": 3}]}
    out = C._inject_schedule_project_timing(
        data,
        {"schedule_start_date": "2026-07-23", "duration_days": 60},
    )

    assert out["project"]["start_date"] == "2026-07-23"
    assert out["project"]["total_duration_days"] == 60
    assert C._resolve_baseline(out).strftime("%Y-%m-%d") == "2026-07-23"


def test_adapt_schedule_duration_is_inclusive():
    out = C._adapt_schedule_data({
        "type": "schedule",
        "items": [{"name": "完工确认", "start": 60, "duration": 1}],
    })

    assert out["tasks"][0]["start"] == 60
    assert out["tasks"][0]["end"] == 60
