"""bid_writer 纯函数特征化测试 —— 锁定省级DB判定/authoring解析/子项级
计划构建行为(P3)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bid_writer as W


# ─── _guess_province_db ─────────────────────────────────────────
def test_guess_province_db_tibet():
    assert W._guess_province_db("西藏自治区拉萨市墨竹工卡县") == "DB54"


def test_guess_province_db_beijing():
    assert W._guess_province_db("北京市朝阳区") == "DB11"


def test_guess_province_db_unknown_empty():
    """识别不出省份返回空串(不瞎猜)。"""
    assert W._guess_province_db("某地某村") == ""


def test_guess_province_db_no_single_char_false_match():
    """单字'藏/京'不应误触发(feedback_province_db_single_char)。"""
    # 含'藏'但非西藏语境
    assert W._guess_province_db("收藏品仓库") != "DB54"


# ─── parse_authoring_yaml ───────────────────────────────────────
def test_parse_authoring_yaml_coerces_types():
    out = W.parse_authoring_yaml("```yaml\nbody_size_pt: 14\nblind_bid: false\n```")
    assert out["body_size_pt"] == 14
    assert out["blind_bid"] is False


def test_parse_authoring_yaml_empty_when_no_block():
    assert W.parse_authoring_yaml("没有 yaml 围栏") == {}


# ─── extract_project_type ───────────────────────────────────────
def test_extract_project_type_returns_nonempty():
    t = W.extract_project_type("本项目为养殖场建设工程,属建筑工程类")
    assert isinstance(t, str) and t


# ─── _build_auto_subitem_plan ───────────────────────────────────
def test_build_auto_subitem_plan_structure():
    chapter = {"num": "05", "title": "施工方案", "target_chars": 30000,
               "score_item": "技术#5", "score": 10}
    variant = {"id": "V01", "model": "deepseek", "temperature": 0.5}
    subs = [{"title": "总体部署", "chars": 10000},
            {"title": "分项工艺", "chars": 12000},
            {"title": "质量控制", "chars": 8000}]
    plan = W._build_auto_subitem_plan(chapter, variant, subs)
    assert plan["mode"] == "cross_model"
    assert len(plan["subitems"]) == 3
    assert [s["title"] for s in plan["subitems"]] == ["总体部署", "分项工艺", "质量控制"]


def test_build_auto_subitem_plan_subitem_chars_preserved():
    """各子项 target_chars 沿用传入的 chars。"""
    chapter = {"num": "01", "title": "X", "target_chars": 20000,
               "score_item": "技术#1", "score": 8}
    variant = {"id": "V02", "model": "deepseek", "temperature": 0.6}
    subs = [{"title": "a", "chars": 9000}, {"title": "b", "chars": 11000}]
    plan = W._build_auto_subitem_plan(chapter, variant, subs)
    assert [s["target_chars"] for s in plan["subitems"]] == [9000, 11000]


# ─── _build_writer_tasks（P3.3 拆 main 后的任务构造）─────────────
def test_build_writer_tasks_skip_and_collect(tmp_path):
    import logging
    from types import SimpleNamespace
    log = logging.getLogger("t_build"); log.setLevel(logging.ERROR)
    output = tmp_path / "output"; output.mkdir()
    variants = [{"id": "V01", "model": "deepseek"}, {"id": "V02", "model": "deepseek"}]
    chapters = [
        {"num": "01", "title": "工程概况", "filename": "01_工程概况.md"},
        {"num": "02", "title": "施工部署", "filename": "02_施工部署.md"},
    ]
    args = SimpleNamespace(chapter=None, force=False)
    # 预先在 V01 放一个已生成章节 → 应被 skip
    (output / "V01").mkdir()
    (output / "V01" / "01_工程概况.md").write_text("已生成", encoding="utf-8")

    tasks, skip = W._build_writer_tasks(variants, chapters, output, args, log)
    # 2 变体 × 2 章 = 4，减去 V01 的 01（已存在）→ 3 个任务 + 1 跳过
    assert skip == 1
    assert len(tasks) == 3
    assert ("V01", "02") in [(v["id"], ch["num"]) for v, ch, _ in tasks]
    assert ("V01", "01") not in [(v["id"], ch["num"]) for v, ch, _ in tasks]


def test_build_writer_tasks_chapter_filter(tmp_path):
    import logging
    from types import SimpleNamespace
    log = logging.getLogger("t_build2"); log.setLevel(logging.ERROR)
    output = tmp_path / "output"; output.mkdir()
    variants = [{"id": "V01", "model": "deepseek"}]
    chapters = [
        {"num": "01", "title": "x", "filename": "01_x.md"},
        {"num": "02", "title": "y", "filename": "02_y.md"},
    ]
    args = SimpleNamespace(chapter="2", force=False)  # 仅生成第 02 章
    tasks, skip = W._build_writer_tasks(variants, chapters, output, args, log)
    assert len(tasks) == 1 and tasks[0][1]["num"] == "02"
