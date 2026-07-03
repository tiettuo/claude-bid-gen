"""bid_expert_review 评标专家测试：纯函数 + monkeypatch LLM 的集成验证。"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bid_expert_review as E

_LOG = logging.getLogger("test_expert")
_LOG.addHandler(logging.NullHandler())


# ── 纯函数 ────────────────────────────────────────────────────────────
def test_pair_chapters_scoring_zips_scored_only():
    chapters = [
        {"num": 1, "filename": "01_a.md", "score": 12, "score_item": "技术评分项 #1"},
        {"num": 2, "filename": "02_b.md", "score": 12, "score_item": "技术评分项 #2"},
        {"num": 3, "filename": "03_c.md", "score": 0, "score_item": "—"},  # 非评分章
    ]
    items = [{"title_original": "施工方案", "score": 12},
             {"title_original": "质量", "score": 12}]
    pairs = E.pair_chapters_scoring(chapters, items)
    assert len(pairs) == 2
    assert pairs[0][0]["num"] == 1 and pairs[0][1]["title_original"] == "施工方案"
    assert pairs[1][0]["num"] == 2 and pairs[1][1]["title_original"] == "质量"


def test_scan_amount_mismatch_detects_wrong_value():
    truth = {"safe_civilization_fee": 11842.51, "provisional_sum": 2500.0}
    text = "安全文明施工费按照 4101.6 元填报，暂列金额按 2500 元。"
    hits = E.scan_amount_mismatch(text, truth)
    # 安全文明施工费 4101.6 ≠ 11842.51 → 命中；暂列金额 2500=2500 → 不命中
    assert any("安全文明施工费" in h and "4101.6" in h for h in hits)
    assert not any("暂列金额" in h for h in hits)


def test_scan_amount_mismatch_no_truth_skips():
    assert E.scan_amount_mismatch("安全文明施工费 100 元", {}) == []


def test_scan_duration_flags_mismatch():
    hits = E.scan_duration("总工期 60 日历天完成", 45)
    assert hits and "60" in hits[0]


def test_scan_duration_ignores_close_value():
    # 偏差 ≤2 天不算矛盾
    assert E.scan_duration("总工期 45 日历天", 45) == []
    assert E.scan_duration("总工期 46 日历天", 45) == []


def test_scan_duration_ignores_stage_duration():
    # 阶段工期/工序工期不得误报为总工期矛盾（巴蜀案回归）
    text = ("第一阶段工期 12 日历天；钢结构工期 18 日历天；"
            "收尾阶段约 5 天；本项目总工期 45 日历天。")
    assert E.scan_duration(text, 45) == []
    # 真正的总工期矛盾仍要抓
    assert E.scan_duration("本项目总工期 60 日历天", 45) != []


def test_detect_variants_batch_and_single(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    (out / "V01").mkdir()
    (out / "V02").mkdir()
    vs = E.detect_variants(out)
    assert [n for n, _ in vs] == ["V01", "V02"]

    out2 = tmp_path / "out2"
    out2.mkdir()
    assert E.detect_variants(out2) == [("single", out2)]


def test_load_scoring_items_filters_technical(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "evaluation_method.json").write_text(json.dumps({
        "scoring_items": [
            {"section": "技术", "title_original": "施工方案", "score": 12, "criteria_original": "c1"},
            {"section": "投标报价", "title_original": "报价", "score": 30, "criteria_original": "c2"},
            {"section": "技术", "title_original": "质量", "score": 12, "criteria_original": "c3"},
        ]
    }), encoding="utf-8")
    items = E.load_scoring_items(ext, _LOG)
    titles = [it["title_original"] for it in items]
    assert titles == ["施工方案", "质量"]  # 报价项被过滤


def test_load_scoring_items_missing_file(tmp_path):
    assert E.load_scoring_items(tmp_path, _LOG) == []


def test_extract_amount_truth(tmp_path):
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "工程量清单控制价.txt").write_text(
        "其中：安全文明施工费 |  |  | 11842.51\n"
        "1 | 暂列金额 | 项 | 2500.00\n", encoding="utf-8")
    truth = E.extract_amount_truth(ext, _LOG)
    assert truth.get("safe_civilization_fee") == 11842.51
    assert truth.get("provisional_sum") == 2500.0


# ── 集成：monkeypatch LLM 跑 review_variant + write_summary ──────────
def _make_project(tmp_path, n_variants=1):
    """建临时项目：extracted/ + output/VXX/。"""
    ext = tmp_path / "extracted"
    ext.mkdir()
    (ext / "evaluation_method.json").write_text(json.dumps({
        "scoring_items": [
            {"section": "技术", "title_original": "施工方案与技术措施", "score": 12,
             "criteria_original": "方案内容不存在瑕疵，得12分；存在1处瑕疵，得8分；2处6分；3处2分；4处及以上0分。"},
            {"section": "技术", "title_original": "质量管理方案", "score": 12,
             "criteria_original": "方案内容不存在瑕疵，得12分；存在1处瑕疵，得8分；"},
        ]
    }), encoding="utf-8")
    (ext / "project_facts.json").write_text(json.dumps({"facts": {
        "project_name": {"value": "测试防坠长廊"},
        "duration_days": {"value": 45},
        "control_price": {"value": 350000},
    }}), encoding="utf-8")
    (ext / "工程量清单控制价.txt").write_text("安全文明施工费 11842.51\n暂列金额 2500\n", encoding="utf-8")
    (ext / "chapter_plan.md").write_text(
        "# 章节规划表\n## 大章节分配\n\n"
        "| 章节号 | 文件名 | 章节标题 | 对应评分项 | 分值 | 目标字数 |\n"
        "|---|---|---|---|---|---|\n"
        "| 01 | 01_施工方案与技术措施.md | 施工方案与技术措施 | 技术评分项 #1 | 12 | 1000 |\n"
        "| 02 | 02_质量管理方案.md | 质量管理方案 | 技术评分项 #2 | 12 | 1000 |\n",
        encoding="utf-8")

    out = tmp_path / "output"
    out.mkdir()
    results = []
    for i in range(1, n_variants + 1):
        vd = out / f"V{i:02d}"
        vd.mkdir()
        (vd / "01_施工方案与技术措施.md").write_text(
            "防坠长廊钢结构施工。监护员李四在岗。安全文明施工费 4101.6 元。\n", encoding="utf-8")
        (vd / "02_质量管理方案.md").write_text("质量管理。钢柱 60 根。\n", encoding="utf-8")
        results.append(vd)
    return ext, out, results


def _fake_parse(client, model, system, user, log=None, max_tokens=12000):
    """模拟 LLM：评分项返回 1 瑕疵 8 分；跨章返回矛盾。"""
    if "跨章节" in user:
        return {"cross_defects": [
            {"param": "钢柱数量",
             "conflicts": [{"chapter": "01", "value": "60 根"}, {"chapter": "02", "value": "80 根"}],
             "reason": "同方案钢柱数量矛盾"}]}
    return {
        "element_coverage": {"施工方法": True},
        "defects": [{"type": "5", "quote": "李四", "anchor": "01", "reason": "占位人名"}],
        "defect_count": 1, "score": 8, "summary": "有 1 处瑕疵",
    }


def test_review_variant_with_mocked_llm(tmp_path, monkeypatch):
    ext, out, vdirs = _make_project(tmp_path, n_variants=1)
    monkeypatch.setattr(E, "_parse_json_with_retry", _fake_parse)
    items = E.load_scoring_items(ext, _LOG)
    truth = E.extract_amount_truth(ext, _LOG)
    facts = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))["facts"]
    truth_str = E.format_amount_truth(truth, facts)
    brief = E.load_project_brief(ext)

    result = E.review_variant("V01", vdirs[0], ext, items, None, "deepseek",
                              brief, truth_str, truth, 45, _LOG)

    # 打分汇总：2 项 × 8 分 = 16，满分 24
    assert result["tech_score"] == 16
    assert result["tech_max"] == 24
    assert len(result["item_results"]) == 2
    # 跨章矛盾被检出
    assert result["cross_defects"] and result["cross_defects"][0]["param"] == "钢柱数量"
    # 符合性：占位人名「李四」+ 金额失实 4101.6 都应命中
    details = " ".join(c["detail"] for c in result["compliance"])
    assert "李四" in details or "占位" in details
    assert "4101.6" in details
    # 致命瑕疵计数（占位符 + 金额失实）
    assert result["fatal_count"] >= 1
    # 报告文件落地
    assert (vdirs[0] / "expert_review.json").exists()
    assert (vdirs[0] / "expert_review.md").exists()


def _fake_perfect(client, model, system, user, log=None, max_tokens=12000):
    """模拟一个无瑕疵满分方案。"""
    if "跨章节" in user:
        return {"cross_defects": []}
    return {"element_coverage": {}, "defects": [], "defect_count": 0, "score": 12, "summary": "满分"}


def test_write_summary_ranks_by_score(tmp_path, monkeypatch):
    ext, out, vdirs = _make_project(tmp_path, n_variants=2)
    items = E.load_scoring_items(ext, _LOG)
    truth = E.extract_amount_truth(ext, _LOG)
    facts = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))["facts"]

    results = []
    for i, vd in enumerate(vdirs, 1):
        # V01 用 8 分 fake；V02 用满分 fake
        monkeypatch.setattr(E, "_parse_json_with_retry",
                            _fake_perfect if i == 2 else _fake_parse)
        r = E.review_variant(f"V{i:02d}", vd, ext, items, None, "deepseek",
                             E.load_project_brief(ext), E.format_amount_truth(truth, facts),
                             truth, 45, _LOG)
        results.append(r)

    E.write_summary(out, results, _LOG)
    summ = json.loads((out / "expert_review_summary.json").read_text(encoding="utf-8"))
    # 排名按 tech_score 降序：V02(24) 应在 V01(16) 前
    assert summ["ranking"][0]["tech_score"] >= summ["ranking"][1]["tech_score"]
    assert (out / "reports" / "评标专家评分报告.md").exists()


def test_main_exits_zero_even_without_scoring_items(tmp_path, monkeypatch):
    """无 evaluation_method.json 时仍 exit 0（不阻断），降级产出。"""
    (tmp_path / "output").mkdir()
    # scoring_items 空 → client 不加载；patch 跨章避免任何真实 LLM 调用
    monkeypatch.setattr(E, "_parse_json_with_retry", _fake_parse)
    orig = sys.argv
    sys.argv = ["bid_expert_review.py", str(tmp_path)]
    try:
        code = E.main()
    finally:
        sys.argv = orig
    assert code == 0
