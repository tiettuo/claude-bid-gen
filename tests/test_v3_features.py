
"""测试 v3 新增功能"""
import sys, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from bid_common import detect_boilerplate, project_fingerprint, check_boq_chapter_coverage

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
        (a / "project_brain.md").write_text("项目名称：民房类项目\n控制价：31514782.47 元", encoding="utf-8")
        fp_a = project_fingerprint(a)
        b = Path(td) / "b" / "extracted"
        b.mkdir(parents=True)
        (b / "project_brain.md").write_text("项目名称：环境整治类项目\n控制价：3721200.00 元", encoding="utf-8")
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

print("ALL V3 TESTS PASSED")
