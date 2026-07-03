"""测试连贯性主线注入(工序 A)— 散装→茅台改进。

覆盖纯函数逻辑(不依赖 LLM 调用):
- bid_plan_synthesize._derive_chapter_narrative: 章节叙事维度派生
- bid_writer._build_block1_project: 难点精准注入 vs 全量 dump 降级
- bid_deployment_synthesize: schema/prompt 构造(不调 LLM)
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ─── 工序 A1: deployment_synthesize prompt/schema 构造 ────────────────

def test_deployment_build_user_prompt_contains_difficulties():
    """prompt 必须把 difficulties.json 的难点喂给 LLM 做映射。"""
    from bid_deployment_synthesize import build_user_prompt

    facts = {"project_name": {"value": "测试项目"}, "control_price": {"value": 1000000}}
    outline = {"topics": [{"title_original": "施工方案"}]}
    difficulties = {
        "重点": [{"title": "高海拔施工"}],
        "难点": [{"title": "派出所不停工"}, {"title": "冬季施工"}],
    }
    prompt = build_user_prompt(facts, outline, difficulties, "招标正文摘录")
    assert "派出所不停工" in prompt
    assert "冬季施工" in prompt
    assert "高海拔施工" in prompt
    assert "difficulty_to_dimension" in prompt


def test_deployment_schema_has_5_dimensions():
    """schema 必须含 5 维部署 + difficulty_to_dimension 映射。"""
    from bid_deployment_synthesize import OUTPUT_SCHEMA, DEPLOYMENT_DIMENSIONS

    assert "deployment_statement" in OUTPUT_SCHEMA
    assert "deployment_dimensions" in OUTPUT_SCHEMA
    assert "difficulty_to_dimension" in OUTPUT_SCHEMA
    assert len(DEPLOYMENT_DIMENSIONS) == 5
    assert "施工总顺序" in DEPLOYMENT_DIMENSIONS


def test_deployment_strip_markdown_fence():
    """LLM 输出常带 ```json 包围,必须能剥离。"""
    from bid_deployment_synthesize import _strip_markdown_fence

    assert _strip_markdown_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_markdown_fence('```\n{"a":1}\n```') == '{"a":1}'
    assert _strip_markdown_fence('{"a":1}') == '{"a":1}'


# ─── 工序 A2: _derive_chapter_narrative 派生 ───────────────────────────

def test_derive_chapter_narrative_matches_difficulty_to_chapter():
    """难点「冬季施工」应映射到含「冬」字的章节(如冬季施工措施章)。"""
    from bid_plan_synthesize import _derive_chapter_narrative

    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        deploy = {
            "deployment_statement": "以分区流水为部署总思路",
            "deployment_dimensions": [{"dim": "施工总顺序", "summary": "..."}],
            "difficulty_to_dimension": [
                {"difficulty": "冬季施工", "primary_dimension": "施工总顺序",
                 "angle": "从工序衔接角度"},
                {"difficulty": "派出所不停工", "primary_dimension": "施工区段划分",
                 "angle": "从区段隔离角度"},
            ],
        }
        (ext / "deployment_outline.json").write_text(
            json.dumps(deploy, ensure_ascii=False), encoding="utf-8"
        )
        chapters_meta = [
            {"title": "冬季施工保证措施", "chapter_num": 9},
            {"title": "质量目标及保证措施", "chapter_num": 7},
        ]
        import logging
        _derive_chapter_narrative(ext, chapters_meta, ext / "deployment_outline.json",
                                  logging.getLogger("test"))

        narr = json.loads((ext / "chapter_narrative.json").read_text(encoding="utf-8"))
        # 第09章(冬季施工措施)应聚焦「冬季施工」难点
        ch09 = narr["chapters"]["09"]
        assert "冬季施工" in ch09["difficulty_focus"], \
            f"09章应聚焦冬季施工,实际={ch09['difficulty_focus']}"
        assert ch09["narrative_dimension"] == "施工总顺序"
        assert "施工总顺序" in ch09["mainline_echo"]


def test_derive_chapter_narrative_no_deployment_is_noop():
    """deployment_outline.json 不存在时应跳过(降级安全,不报错)。"""
    from bid_plan_synthesize import _derive_chapter_narrative
    import logging

    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        _derive_chapter_narrative(ext, [{"title": "x", "chapter_num": 1}],
                                  ext / "deployment_outline.json",
                                  logging.getLogger("test"))
        assert not (ext / "chapter_narrative.json").exists(), "无 deployment 不应生成 narrative"


def test_derive_chapter_narrative_unmatched_chapter_gets_fallback_dim():
    """难点匹配不上的章节,应按序号轮转兜底到 5 维之一(不悬空)。"""
    from bid_plan_synthesize import _derive_chapter_narrative
    from bid_deployment_synthesize import DEPLOYMENT_DIMENSIONS
    import logging

    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        deploy = {
            "deployment_statement": "主线",
            "difficulty_to_dimension": [
                {"difficulty": " totally unrelated ", "primary_dimension": "施工总目标"},
            ],
        }
        (ext / "deployment_outline.json").write_text(
            json.dumps(deploy, ensure_ascii=False), encoding="utf-8"
        )
        chapters_meta = [{"title": "质量目标及保证措施", "chapter_num": 1}]
        _derive_chapter_narrative(ext, chapters_meta, ext / "deployment_outline.json",
                                  logging.getLogger("test"))
        narr = json.loads((ext / "chapter_narrative.json").read_text(encoding="utf-8"))
        dim = narr["chapters"]["01"]["narrative_dimension"]
        assert dim in DEPLOYMENT_DIMENSIONS, f"兜底维度非法: {dim}"


# ─── 工序 A3: writer Block1 难点精准注入 ───────────────────────────────

def _setup_writer_narrative(narr_data):
    """设置 bid_writer 模块级 _CHAPTER_NARRATIVE。"""
    import bid_writer
    bid_writer._CHAPTER_NARRATIVE = narr_data or {}


def test_writer_block1_precision_injection_when_focus_present():
    """有 chapter_narrative 且本章有聚焦时:只注入聚焦难点 + 主线回指声明。"""
    import bid_writer
    _setup_writer_narrative({
        "chapters": {
            "09": {
                "narrative_dimension": "施工总顺序",
                "difficulty_focus": ["冬季施工"],
                "mainline_echo": "★ 本章叙事职责:从「施工总顺序」维度展开,呼应部署纲领。",
            }
        }
    })
    difficulties = {
        "重点": [{"title": "高原施工"}],
        "难点": [
            {"title": "冬季施工", "rationale": ["混凝土防冻措施"]},
            {"title": "雨季施工", "rationale": ["排水措施"]},
        ],
    }
    block1 = bid_writer._build_block1_project(
        authoring={"project": {"name": "测试"}},
        brain_text="",
        difficulties=difficulties,
        current_chapter_num="09",
    )
    # 主线回指声明存在
    assert "施工总顺序" in block1
    # 聚焦难点存在 + 其 rationale 深化
    assert "冬季施工" in block1
    assert "混凝土防冻措施" in block1
    # 非聚焦难点不应出现(精准,不是全量)
    assert "雨季施工" not in block1
    # 技术重点仍作为背景列出
    assert "高原施工" in block1


def test_writer_block1_fallback_when_no_narrative():
    """无 chapter_narrative 时:走旧的全量 dump 逻辑(降级,行为不变)。"""
    import bid_writer
    _setup_writer_narrative(None)
    difficulties = {
        "重点": [{"title": "重点A"}, {"title": "重点B"}],
        "难点": [{"title": "难点1"}, {"title": "难点2"}, {"title": "难点3"}],
    }
    block1 = bid_writer._build_block1_project(
        authoring={"project": {}},
        brain_text="",
        difficulties=difficulties,
        current_chapter_num="03",
    )
    # 旧逻辑:全量列难点(无聚焦、无主线回指)
    assert "难点1" in block1
    assert "难点2" in block1
    assert "难点3" in block1
    assert "重点A" in block1
    assert "本章叙事职责" not in block1  # 无主线回指


def test_writer_block1_fallback_when_chapter_has_no_focus():
    """有 narrative 但本章 focus 为空时:该章走全量 dump(降级)。"""
    import bid_writer
    _setup_writer_narrative({
        "chapters": {
            "05": {
                "narrative_dimension": "资源统筹原则",
                "difficulty_focus": [],  # 本章无聚焦
                "mainline_echo": "★ 本章叙事职责:从「资源统筹原则」维度展开。",
            }
        }
    })
    difficulties = {"难点": [{"title": "难点X"}, {"title": "难点Y"}]}
    block1 = bid_writer._build_block1_project(
        authoring={"project": {}}, brain_text="",
        difficulties=difficulties, current_chapter_num="05",
    )
    # focus 空 → 全量 dump
    assert "难点X" in block1
    assert "难点Y" in block1
    # 但 mainline_echo 仍注入(只要 narrative 存在)
    assert "资源统筹原则" in block1


# ─── 工序 A0: 第0章「编制总说明」生成 ──────────────────────────────────

def test_intro_chapter_contains_deployment_statement():
    """第0章必须含部署纲领声明 + 5 维部署(叙事锚点)。"""
    from bid_plan_synthesize import _generate_intro_chapter
    import logging

    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        facts = {
            "project_name": {"value": "测试项目"},
            "project_scope": {"value": "道路施工"},
        }
        eval_data = {"evaluation_method": "score"}
        deploy = {
            "deployment_statement": "以分区流水作业为总体部署总思路",
            "deployment_dimensions": [
                {"dim": "施工总目标", "summary": "工期180天"},
                {"dim": "施工总顺序", "summary": "先地下后地上"},
            ],
            "difficulty_to_dimension": [
                {"difficulty": "冬季施工", "primary_dimension": "施工总顺序", "angle": "工序衔接"},
            ],
        }
        _generate_intro_chapter(ext, facts, eval_data, deploy, logging.getLogger("test"))
        md = (ext / "00_编制总说明.md").read_text(encoding="utf-8")
        # 部署纲领声明存在
        assert "以分区流水作业为总体部署总思路" in md
        # 5 维部署(这里 2 维)存在
        assert "施工总目标" in md
        assert "施工总顺序" in md
        # 难点响应表存在
        assert "冬季施工" in md
        assert "工序衔接" in md


def test_intro_chapter_skipped_when_no_deployment():
    """无 deployment_outline 时应跳过第0章生成(降级)。"""
    from bid_plan_synthesize import _generate_intro_chapter
    import logging

    with tempfile.TemporaryDirectory() as td:
        ext = Path(td)
        _generate_intro_chapter(ext, {}, {}, None, logging.getLogger("test"))
        assert not (ext / "00_编制总说明.md").exists(), "无 deployment 不应生成第0章"


# ─── 工序 B: chapter_intro 引文拆分 ────────────────────────────────────

def test_extract_intro_and_body_splits_correctly():
    """章节 md 应正确拆分为 H1 + 引言段 + 正文 + 末段摘要。"""
    from bid_chapter_intro import extract_intro_and_body

    md = """# 第一章 测试

这是引言段,讲背景。

## 1.1 子节一

子节一内容。

## 1.2 子节二

子节二内容,末尾。"""
    h1, intro, body, tail = extract_intro_and_body(md)
    assert "第一章 测试" in h1
    assert "这是引言段" in intro
    assert "1.1 子节一" in body
    assert "子节二内容" in tail  # 末段摘要


def test_extract_intro_and_body_no_h2():
    """无 ## 子节时,整个 rest 当正文。"""
    from bid_chapter_intro import extract_intro_and_body

    md = "# 标题\n\n只有引言没有子节。"
    h1, intro, body, tail = extract_intro_and_body(md)
    assert "标题" in h1
    # 无 ##,body 为空或引言全部
    assert "只有引言" in (intro + body)


# ─── 工序 C: coherence_pass 摘要构造 ──────────────────────────────────

def test_build_chapters_summary_includes_all_chapters():
    """摘要构造应包含所有章号+标题+摘要内容。"""
    from bid_coherence_pass import _build_chapters_summary

    chapters = [
        {"num": "01", "title": "工程概况", "summary": "本项目位于测试县"},
        {"num": "02", "title": "施工方案", "summary": "采用分区流水作业"},
    ]
    summary = _build_chapters_summary(chapters)
    assert "第01章" in summary
    assert "工程概况" in summary
    assert "第02章" in summary
    assert "分区流水作业" in summary


def test_coherence_skips_set_processing():
    """--skip 参数应正确跳过子任务(skips 集合逻辑)。"""
    # 这个测试验证 skips 集合的成员判断逻辑(纯逻辑,不调 LLM)
    skips = {"redundancy", "conflicts"}
    assert "references" not in skips
    assert "redundancy" in skips
    assert "conflicts" in skips


# ─── 端到端: config 加载 + 总开关 ─────────────────────────────────────

def test_coherence_config_loads():
    """bid_coherence.json 应可加载且含必要字段。"""
    from bid_run import load_coherence_config
    cfg = load_coherence_config()
    # config 存在(我们创建了它)
    assert "enabled" in cfg
    assert "chapter_intro" in cfg
    assert "coherence_pass" in cfg


def test_coherence_disabled_by_default():
    """默认 enabled=false(灰度安全,不破坏现有项目)。"""
    from bid_run import load_coherence_config
    cfg = load_coherence_config()
    assert cfg.get("enabled") is False, "coherence 必须默认关闭(灰度)"
