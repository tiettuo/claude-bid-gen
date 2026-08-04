import json

from bid_boq_extract import _parse_loose_boq_text, collect_boq
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


def test_collect_boq_deduplicates_overlapping_filename_globs(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "某工程量清单.txt").write_text(
        """
工程名称：某项目 【建筑工程】
| 1 | 010101001001 | 土方开挖 | 素土 | m³ | 12.5 |
""",
        encoding="utf-8",
    )

    payload = collect_boq(ext, _Log())

    assert payload["_meta"]["source_files"] == ["某工程量清单.txt"]
    assert sum(len(c["items"]) for c in payload["categories"]) == 1


def test_derive_prefers_explicit_altitude(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "清单.txt").write_text("工程建设地点：养殖类项目县日多乡，平均海拔高度为4328米。", encoding="utf-8")
    facts = {
        "location": {"value": "示例县示例乡"},
        "control_price": {"value": 3721200.0},
    }
    derived = derive(facts, ext)
    assert derived["altitude_m"] == 4328
    assert derived["altitude_source_type"] == "explicit_text"
    assert derived["size_class"] == "small"


def _gate_fixture(tmp_path, boq_txt: str):
    """构造 extracted/ 含 facts + 一个清单 txt + 空 boq.json，返回 gate 结果。"""
    from bid_run import generation_input_gate
    ext = tmp_path / "extracted"
    out = tmp_path / "output"
    ext.mkdir()
    out.mkdir()
    (ext / "project_facts.json").write_text(
        json.dumps({"facts": {"project_name": {"value": "某某人居环境整治项目"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (ext / "工程量清单.txt").write_text(boq_txt, encoding="utf-8")
    (ext / "boq.json").write_text(json.dumps({"categories": []}), encoding="utf-8")
    return generation_input_gate(tmp_path)


def test_generation_gate_blocks_empty_boq_on_extract_failure(tmp_path):
    """清单含明细表头却抽出 0 条 = 抽取失败，必须拦。"""
    ok, issues = _gate_fixture(tmp_path, "分部分项工程量清单\n序号 名称 单位 数量\n...")
    assert not ok
    assert any("boq.json 未解析出工程量" in i for i in issues)


def test_generation_gate_allows_empty_boq_in_summary_mode(tmp_path):
    """清单只有汇总表、无明细表头 = 汇总表模式，空 boq 合法，放行。"""
    ok, issues = _gate_fixture(tmp_path, "单位工程费汇总表\n招标控制价汇总\n合计 372 万元")
    assert ok, f"汇总表模式不应被拦，但有 issues: {issues}"
    assert not any("未解析出工程量" in i for i in issues)
