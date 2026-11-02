"""工期合规门的绝对日期基准测试。"""

import logging
from datetime import date

from bid_compliance import check_schedule_duration


def test_schedule_date_before_earliest_start_is_violation(tmp_path):
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        """# 施工进度计划

```json
{
  "type": "schedule",
  "tasks": [
    {"name": "施工准备", "start": "2026-07-01", "end": "2026-07-03"}
  ]
}
```
""",
        encoding="utf-8",
    )
    result = check_schedule_duration(
        [md], tender_days=60, log=logging.getLogger("test"),
        earliest_start=date(2026, 7, 23),
    )
    assert result["violations"]
    assert result["violations"][0]["kind"] == "计划开工基准"


def test_prose_abs_date_is_violation(tmp_path):
    """正文散文出现具体日历日期（工期上下文）→ 正文日期泄漏。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本工程计划于2026年8月1日开工，2026年10月31日竣工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    kinds = [v["kind"] for v in result["violations"]]
    assert "正文日期泄漏" in kinds


def test_prose_relative_expression_ok(tmp_path):
    """相对表达（日历天 / 第 N 天）不触发日期泄漏。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n总工期90日历天，开工后第45天完成基础工程。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_prose_reference_context_date_exempt(tmp_path):
    """引用类上下文（招标公告发布日期等）豁免，不误报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n招标公告发布日期为2026年4月1日，详见招标文件。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_factual_context_date_exempt(tmp_path):
    """日期紧邻检测/送检类流程词（已发生事实）豁免，不误报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n混凝土试块于2026年8月5日送检，检测报告编号JC-2026-088。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_report_word_in_same_line_not_exempt(tmp_path):
    """同行含豁免宽词（报告）但日期是工期语义 → 必须报，不允许整行漏报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n开工并形成开工报告，计划于2026年8月1日开工，2026年10月31日竣工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_cert_license_line_not_exempt(tmp_path):
    """「持证上岗」含证书类宽词但日期是进场（工期）语义 → 必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n持证上岗人员于2026年8月1日进场施工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_guiding_regulation_line_not_exempt(tmp_path):
    """「按规定实施降水」是施工语义（无「自…起」结构、无规范类词）→ 必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n按规定于2026年8月1日实施降水作业。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_standard_implement_date_exempt(tmp_path):
    """规范类词 + 「自…起实施」结构（规范生效日期）豁免，不误报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本规范自2024年8月1日起实施，本工程施工执行现行版本。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_standard_word_far_from_date_not_exempt(tmp_path):
    """规范类词远离日期（如「执行《验收统一标准》，本工程自…实施降水」）→ 真实开工日期必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n执行《建筑工程施工质量验收统一标准》，本工程自2026年8月1日起实施降水作业。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_mixed_line_only_std_date_exempt(tmp_path):
    """混排行：规范生效日期豁免，同行的真实开工日期必须照报（禁止整行豁免）。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本规范自2024-08-01起实施。计划2026-09-01开工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    leaked = [v["value"] for v in result["violations"] if v["kind"] == "正文日期泄漏"]
    assert "2026-09-01" in leaked       # 真实开工日期必须报
    assert "2024-08-01" not in leaked   # 规范生效日期豁免


def test_project_team_establishment_not_exempt(tmp_path):
    """「成立项目部」是排期里程碑，不是公司成立历史事实 → 必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n于2026-08-01成立项目部并进场。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_start_order_issue_date_not_exempt(tmp_path):
    """「开工令发布」是工期语义（不含公告/发布引用结构）→ 必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n开工令于2026-08-01发布后组织进场。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_plan_implement_date_not_exempt(tmp_path):
    """不含规范类词的方案实施日期（编造的开工日期）必须报，不被「自…实施」误豁免。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本施工方案自2026年8月1日起实施。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_historical_award_date_exempt(tmp_path):
    """历史事实类日期（获奖/成立等，必然发生在过去）豁免。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本公司2020年12月31日获省级优质工程奖，2023年通过ISO9001认证。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_json_block_dates_not_prose_leak(tmp_path):
    """进度图 JSON 块内的日期不报正文日期泄漏（仍走计划开工基准检查）。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n```json\n"
        "{\"type\":\"schedule\",\"tasks\":[{\"name\":\"施工准备\","
        "\"start\":\"2026-07-01\",\"end\":\"2026-07-03\"}]}\n```\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=60, log=logging.getLogger("test"))
    assert not any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_json_block_and_prose_mixed(tmp_path):
    """同一文件里 json 块合规 + 正文泄漏，两类检查互不干扰。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n计划于2026年9月1日竣工。\n\n```json\n"
        "{\"type\":\"schedule\",\"tasks\":[{\"name\":\"施工准备\","
        "\"start\":\"2026-08-01\",\"end\":\"2026-08-03\"}]}\n```\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=60, log=logging.getLogger("test"))
    kinds = [v["kind"] for v in result["violations"]]
    assert "正文日期泄漏" in kinds
    assert "计划开工基准" not in kinds  # json 块日期 2026-08-01 不早于基准时不再误报


def test_bidder_after_phrase_not_exempt(tmp_path):
    """「投标后于X日开工」是工期语义，不能因「投标」二字被豁免 → 必须报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n本工程投标后于2026年8月1日开工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_release_time_reference_date_exempt(tmp_path):
    """「招标文件发布时间为X」是引用来源日期（含「发布时间」）→ 豁免，不误报。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n招标文件发布时间为2026年4月1日，详见招标文件。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert not result["violations"]


def test_reference_word_after_date_not_exempt(tmp_path):
    """「X日开工，详见公告」——引用词在日期右侧是工期语义，不得豁免。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n计划于2026年8月1日开工，详见公告。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_trial_section_construction_not_exempt(tmp_path):
    """「试验段施工」是排期里程碑（非已发生试验事实），不得豁免。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n于2026年8月1日进行试验段施工。\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=90, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])


def test_malformed_json_block_dates_are_prose_leak(tmp_path):
    """解析失败的 ```json 块不豁免：块内日期按正文泄漏报，不能逃过两项检查。"""
    md = tmp_path / "09_施工进度计划.md"
    md.write_text(
        "# 施工进度计划\n\n```json\n"
        "{not valid json 2026-08-01 2026-09-01}\n```\n",
        encoding="utf-8",
    )
    result = check_schedule_duration([md], tender_days=60, log=logging.getLogger("test"))
    assert any(v["kind"] == "正文日期泄漏" for v in result["violations"])



# ─── 关键响应词：工期口径必须来自 schedule_milestones，且不得被投标有效期污染 ───

def _response(rules_text, body):
    from bid_compliance import check_response
    return check_response(rules_text, body, logging.getLogger("test"))


def test_duration_keyword_from_structured_field():
    """规则文件里同时有「投标有效期：90 日历天」时，工期响应词必须是 450 而不是 90。"""
    rules = (
        "## 否决投标关键节点\n"
        "- 投标有效期：90 日历天（从投标截止之日起计算）\n\n"
        "schedule_milestones:\n  total_duration_days: 450\n"
    )
    res = _response(rules, "本项目总工期 450 日历天，质量目标合格。")
    dur = [r for r in res if r["label"] == "工期响应"][0]
    assert dur["keyword"] == "450 日历天"
    assert dur["present"] is True


def test_duration_keyword_requires_unit_not_bare_number():
    """无结构化字段时，回退正则也必须带「日历天」单位，否则正文任意同号数字都算已响应。"""
    res = _response("计划工期为450日历天，工程质量合格。", "工程量清单第 450 条编号")
    dur = [r for r in res if r["label"] == "工期响应"][0]
    assert dur["keyword"] == "450 日历天"
    assert dur["present"] is False


def test_standard_book_title_effect_date_exempt():
    """「《X条例》自…起施行」是法规生效日期（合法引用），必须豁免为日期泄漏。"""
    from bid_compliance import _prose_abs_dates
    assert _prose_abs_dates("依《建设工程质量管理条例》自2000年1月30日起施行。", []) == []
    # 反向守旧：实词插入的标准名后日期仍是排期日期，不得借「标准」二字蒙混过关
    assert _prose_abs_dates("执行《验收统一标准》，本工程自2026年8月1日起实施降水。", [])


def test_process_word_exemption_requires_adjacency_same_clause():
    """送检/检测类豁免必须与日期紧邻且在同一小句内,否则放行真实排期日期。"""
    from bid_compliance import _prose_abs_dates

    # 隔句的「送检」不得豁免真实开工日期
    assert _prose_abs_dates("本工程计划于2026年8月1日开工。混凝土试块送检合格后方可浇筑。\n", [])
    # 紧邻的流程事实仍豁免
    assert _prose_abs_dates("混凝土试块已于2025年5月1日送检合格。\n", []) == []
