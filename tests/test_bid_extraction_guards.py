import json

from bid_boq_extract import _parse_loose_boq_text
from bid_derive import derive
from bid_facts_extract import extract_facts


class _Log:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def test_project_name_repairs_placeholder():
    text = """
人居环境整治类项目
项 目 名  称：人居环境整治类项目
1.1.4 本招标项目名称：见投标人须知前附表。
"""
    facts = extract_facts(text, "招标文件.txt", _Log())
    from bid_facts_extract import _repair_project_name

    _repair_project_name(facts, text, "招标文件.txt", _Log())
    assert facts["project_name"]["value"] == "人居环境整治类项目"
    assert facts["project_name"]["source"]["method"].startswith("candidate_repair")


def test_loose_boq_html_table_extracts_items():
    text = """
工程名称：人居环境整治类项目 【园林绿化】
<table><tr><td></td><td>0501</td><td colspan="2">绿化工程</td><td></td><td></td></tr>
<tr><td>1</td><td>050101010001</td><td>整理绿化用地</td><td>回填土</td><td>m²</td><td>11810.70</td></tr>
<tr><td>2</td><td>050102013001</td><td>鼠茅草 + 高山嵩草混播</td><td>养护期12个月</td><td>m²</td><td>9084.20</td></tr></table>
"""
    cats = _parse_loose_boq_text(text)
    assert cats[0]["category"] == "园林绿化-绿化工程"
    assert [i["name"] for i in cats[0]["items"]] == ["整理绿化用地", "鼠茅草 + 高山嵩草混播"]


def test_derive_prefers_explicit_altitude(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "清单.txt").write_text("工程建设地点：示例县示例乡，平均海拔高度为4328米。", encoding="utf-8")
    facts = {
        "location": {"value": "示例县示例乡"},
        "control_price": {"value": 3721200.0},
    }
    derived = derive(facts, ext)
    assert derived["altitude_m"] == 4328
    assert derived["altitude_source_type"] == "explicit_text"
    assert derived["size_class"] == "small"


def test_generation_gate_blocks_empty_boq(tmp_path):
    from bid_run import generation_input_gate

    ext = tmp_path / "extracted"
    out = tmp_path / "output"
    ext.mkdir()
    out.mkdir()
    (ext / "project_facts.json").write_text(
        json.dumps({"facts": {"project_name": {"value": "某某人居环境整治项目"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (ext / "工程量清单.txt").write_text("清单文本", encoding="utf-8")
    (ext / "boq.json").write_text(json.dumps({"categories": []}), encoding="utf-8")
    ok, issues = generation_input_gate(tmp_path)
    assert not ok
    assert any("boq.json 未解析出工程量" in i for i in issues)
