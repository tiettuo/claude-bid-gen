
"""测试 v3 新增功能"""
import sys, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from bid_common import detect_boilerplate, project_fingerprint, check_boq_chapter_coverage
import bid_rules_synthesize as R
import bid_writer as W
import bid_data_anchor as A

def test_boilerplate_on_template_text():
    text = "建立健全质量管理体系，成立质量管理领导小组，严格执行国家现行标准规范，加强施工过程质量监督，确保工程质量达到合格标准。制定专项施工方案，坚持预防为主安全第一的方针，做到责任到人任务到岗。实行三级管理制度，完善质量管理网络。加强人员教育培训，制定应急预案，确保施工安全。坚持质量第一以人为本。PDCA循环管理，三检制，三级教育，三级交底，四不放过原则。"
    result = detect_boilerplate(text)
    assert result["ratio"] > 0.3, f"ratio={result['ratio']}"
    assert result["verdict"] in ("review", "rewrite")

def test_boilerplate_specific_text():
    text = "本项目位于示例县示例乡，海拔3820米，控制价372.12万元。施工区域2.5万平方米。总工期180日历天，2026年5月31日至2026年11月26日。C25混凝土，HRB400钢材，18mm胶合板。开挖深度3.2m，地下水位-4.5m，井点降水。"
    result = detect_boilerplate(text)
    assert result["ratio"] < 0.25, f"ratio={result['ratio']}"

def test_boilerplate_empty():
    assert detect_boilerplate("")["ratio"] == 0.0
    assert detect_boilerplate("abc")["ratio"] == 0.0

def test_fingerprint_different():
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a" / "extracted"
        a.mkdir(parents=True)
        (a / "project_brain.md").write_text("项目名称：测试项目甲\n控制价：31514782.47 元", encoding="utf-8")
        fp_a = project_fingerprint(a)
        b = Path(td) / "b" / "extracted"
        b.mkdir(parents=True)
        (b / "project_brain.md").write_text("项目名称：测试项目乙\n控制价：3721200.00 元", encoding="utf-8")
        fp_b = project_fingerprint(b)
        assert fp_a["hash"] != fp_b["hash"]

def test_fingerprint_no_brain():
    with tempfile.TemporaryDirectory() as td:
        assert project_fingerprint(Path(td)) == {}

def test_boq_full_coverage():
    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        (ext / "boq.json").write_text(json.dumps({"categories": [{"name":"土石方"},{"name":"混凝土"},{"name":"砌筑"}]}), encoding="utf-8")
        titles = ["施工方案（含土石方、混凝土、砌筑）"]
        result = check_boq_chapter_coverage(ext, titles)
        assert result["ratio"] == 1.0

def test_boq_partial():
    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        (ext / "boq.json").write_text(json.dumps({"categories": [{"name":"土石方"},{"name":"矿山井巷"},{"name":"园林绿化"}]}), encoding="utf-8")
        result = check_boq_chapter_coverage(ext, ["施工方案（含土石方）"])
        assert result["ratio"] < 1.0

def test_boq_uncategorized_covered_by_technical_container():
    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        (ext / "boq.json").write_text(json.dumps({"categories": [{"name":"未分类"}]}), encoding="utf-8")
        result = check_boq_chapter_coverage(ext, ["施工方案与技术措施"])
        assert result["missing"] == []
        assert result["ratio"] == 1.0

def test_boq_no_file():
    with tempfile.TemporaryDirectory() as td:
        result = check_boq_chapter_coverage(Path(td), ["施工方案"])
        assert result["ratio"] == 1.0


# ─── P1: 工程量清单(boq.json) → authoring work_structure → writer Block1 ───

def test_extract_work_structure_from_boq():
    boq = {"categories": [
        {"name": "土建-拆除", "items": [
            {"name": "拆除女儿墙", "qty": 0.33, "unit": "m3", "code": "010101001001"}]},
        {"name": "土建-新建", "items": [
            {"name": "C30 现浇混凝土", "qty": 45.0, "unit": "m3"}]},
        {"name": "无条目类目", "items": []},
    ]}
    ws = R.extract_work_structure_from_boq(boq)
    assert [w["sub_project"] for w in ws] == ["土建-拆除", "土建-新建"]
    assert ws[0]["boq_items"][0]["qty"] == 0.33
    assert ws[1]["nature"] == "清单类目"


def test_extract_work_structure_from_boq_empty():
    assert R.extract_work_structure_from_boq(None) == []
    assert R.extract_work_structure_from_boq({"categories": []}) == []


def test_authoring_rules_boq_and_blind_roundtrip():
    """authoring_rules.md 渲染→parse:boq 工程量明细 + blind 块都必须存活。"""
    ws = [{"sub_project": "土建-新建", "nature": "清单类目",
           "boq_items": [{"name": "C30 现浇混凝土", "qty": 45.0, "unit": "m3"}]}]
    md = R.render_authoring_rules_md(
        project={"name": "测试项目"}, evaluation={"evaluation_method": "passfail", "total_tech_score": 0},
        blind={"blind_bid": True}, format_cfg={}, format_sources={}, density={}, must_include={},
        chapter_topics=[], attachments=[], work_structure=ws, key_personnel={}, project_specific=[],
        technical_red_lines=[], audit_warnings=[],
    )
    parsed = W.parse_authoring_yaml(md)
    assert parsed.get("blind", {}).get("blind_bid") is True  # blind 块不被丢弃
    assert isinstance(parsed["blind"].get("authoring_constraints"), list)
    assert parsed["work_structure"][0]["boq_items"][0]["qty"] == 45.0


def test_build_block1_renders_boq_items():
    W._AUTHORING_CTX = {
        "project": {"name": "某项目", "duration_days": 120},
        "work_structure": [
            {"sub_project": "土建-新建", "nature": "清单类目",
             "boq_items": [{"name": "钢筋", "qty": 6.8, "unit": "t"}]},
            {"sub_project": "新建宿舍", "area_m2": 893.64, "unit": "㎡"},
        ],
        "chapter_anchors": [{"n": "07", "title": "施工方案与技术措施"}],
    }
    W._BRAIN_TEXT = "项目类型：房建工程"
    W._DIFFICULTIES_DATA = {"重点": [], "难点": []}
    W._CHAPTER_NARRATIVE = {}
    b1 = W._build_block1_project(W._AUTHORING_CTX, W._BRAIN_TEXT, W._DIFFICULTIES_DATA, current_chapter_num="07")
    assert "钢筋 6.8t" in b1
    assert "新建宿舍 893.64㎡" in b1
    assert "1:1" in b1  # 施工方案章强提醒保留


# ─── P3: 现场条件约束注入 Block1 ──────────────────────────────────

def test_build_block1_injects_site_constraints():
    W._AUTHORING_CTX = {"project": {"name": "某项目"}, "work_structure": [],
                        "project_specific_constraints": [], "chapter_anchors": []}
    W._BRAIN_TEXT = "项目类型：房建工程"
    W._DIFFICULTIES_DATA = {
        "重点": [], "难点": [],
        "约束": [
            {"category": "场地交通", "content": "场地狭小，仅东侧一条 4m 宽临时便道"},
            {"category": "周边环境", "content": "北侧紧邻在营业网点，需不停业施工"},
            {"category": "材料设备", "content": "招标文件未明示,常规执行"},  # 应跳过
        ],
    }
    W._CHAPTER_NARRATIVE = {}
    b1 = W._build_block1_project(W._AUTHORING_CTX, W._BRAIN_TEXT, W._DIFFICULTIES_DATA, current_chapter_num="01")
    assert "现场施工条件约束" in b1
    assert "4m 宽临时便道" in b1
    assert "不停业施工" in b1
    assert "招标文件未明示" not in b1  # 空约束不灌套话


# ─── P4: 统一数据锚点自动合成 ─────────────────────────────────────

def test_build_data_anchor_includes_hard_data():
    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        facts = {"facts": {
            "project_name": {"value": "某邮政改造项目"},
            "location": {"value": "西藏日喀则市"},
            "control_price": {"value": 3721200.0},
            "duration_days": {"value": 180},
            "quality_target": {"value": "合格"},
        }}
        derived = {
            "schedule_start_date": "2026-05-31", "schedule_end_date": "2026-11-26",
            "schedule_constraints": {"earliest_start_date": "2026-05-26",
                                     "min_start_rule": "投标截止后第5个工作日(周一至周五口径)"},
            "altitude_m": 3820, "altitude_class": "very_high",
            "season_set": ["春", "夏", "秋"], "spans_winter": False,
            "size_class": "small_medium",
        }
        boq = {"categories": [
            {"name": "土建-拆除", "items": [{"name": "拆除女儿墙", "qty": 0.33, "unit": "m3"}]},
            {"name": "土建-新建", "items": [{"name": "C30 现浇混凝土", "qty": 45.0, "unit": "m3"}]},
        ]}
        (ext / "project_facts.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        (ext / "project_derived.json").write_text(json.dumps(derived, ensure_ascii=False), encoding="utf-8")
        (ext / "boq.json").write_text(json.dumps(boq, ensure_ascii=False), encoding="utf-8")
        anchor = A.build_data_anchor(ext)
        assert "某邮政改造项目" in anchor
        assert "3,721,200.00 元" in anchor
        assert "180 日历天" in anchor
        assert "3820 m" in anchor
        assert "拆除女儿墙 0.33m3" in anchor
        assert "C30 现浇混凝土 45.0m3" in anchor


def test_build_data_anchor_empty_sources():
    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)  # 无 facts/derived/boq
        anchor = A.build_data_anchor(ext)
        assert "统一数据锚点" in anchor  # 骨架仍产出，避免 writer 读到空文件
        assert "无可用事实" in anchor

print("ALL V3 TESTS PASSED")
