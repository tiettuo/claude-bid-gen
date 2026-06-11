"""循环工程(--loop-mode)特征化测试:verify 失败分类/结构化报告/统计 flush/
预算熔断/门禁自动修复决策/失败台账去重/编排器状态。"""
import json
import logging
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bid_verify as V
import bid_run as R
import bid_failures as F
import bid_loop as BL
import bid_escalation as E
from bid_llm_client import LLMClient, LLMStats, LLMUsage

_LOG = logging.getLogger("test_loop")
_LOG.addHandler(logging.NullHandler())

POLICY = json.loads(
    (Path(__file__).resolve().parents[1] / "configs" / "bid_loop_policy.json")
    .read_text(encoding="utf-8"))


def _state():
    return {"steps": {}}


# ─── bid_verify 分类 + 结构化报告 ───────────────────────────────────

def test_classify_finding():
    assert V.classify_finding("V01 封面", "fail") == "cover_missing"
    assert V.classify_finding("V02 页数", "fail") == "page_over_cap"
    assert V.classify_finding("V02 页数", "warn") == "page_count_unknown"
    assert V.classify_finding("反串标-正文字体", "fail") == "format_identical"
    assert V.classify_finding("反串标-图表", "warn") == "format_similarity_warn"
    assert V.classify_finding("强制格式-行距", "warn") == "mandated_format_drift"
    assert V.classify_finding("产物存在性", "fail") == "missing_artifact"
    assert V.classify_finding("组装日志", "warn") == "assemble_log_danger"


def test_finding_variant():
    assert V._finding_variant("V01 封面") == "V01"
    assert V._finding_variant("单份 页数") == "单份"
    assert V._finding_variant("反串标-正文字体") == ""


def test_write_report_json(tmp_path):
    findings = [("fail", "V01 封面", "开头未检出封面"), ("warn", "组装日志", "1 条危险行")]
    per = {"V01": {"cover": False, "font": "宋体", "spacing": "1.5",
                   "margin": "2.5/2.5", "pages": 75}}
    V._write_report(tmp_path, findings, _LOG, per=per, cap=70)
    data = json.loads((tmp_path / "verify_report.json").read_text(encoding="utf-8"))
    assert data["status"] == "fail"
    assert data["n_fail"] == 1 and data["n_warn"] == 1
    assert data["page_cap"] == 70
    f0 = data["findings"][0]
    assert f0["class"] == "cover_missing" and f0["variant"] == "V01"
    assert data["per_variant"]["V01"]["pages"] == 75
    assert (tmp_path / "verify_report.md").exists()


# ─── LLM 统计 flush(预算追踪数据源)─────────────────────────────────

def test_flush_stats_merge_and_idempotent(tmp_path):
    c = LLMClient.__new__(LLMClient)
    c.stats = LLMStats()
    c.stats.add(LLMUsage(model="deepseek_chat", prompt_tokens=100,
                         completion_tokens=50, total_tokens=150))
    p = tmp_path / "llm_stats.json"
    c.flush_stats_to_json(p, "writer")
    c.stats.add(LLMUsage(model="deepseek_chat", prompt_tokens=10,
                         completion_tokens=5, total_tokens=15))
    c.flush_stats_to_json(p, "writer")  # 同 label 幂等覆盖累计值

    c2 = LLMClient.__new__(LLMClient)
    c2.stats = LLMStats()
    c2.stats.add(LLMUsage(model="qwen_plus", prompt_tokens=7,
                          completion_tokens=3, total_tokens=10))
    c2.flush_stats_to_json(p, "concise")  # 不同 label 分桶累加

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["buckets"]["writer"]["total_tokens"] == 165
    assert data["buckets"]["concise"]["total_tokens"] == 10


# ─── 预算熔断 ───────────────────────────────────────────────────────

def _write_stats(output: Path, prompt: int, completion: int, model="deepseek_chat"):
    p = output / "logs" / "llm_stats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"buckets": {"writer": {"by_model": {
        model: {"prompt": prompt, "completion": completion}}}}}), encoding="utf-8")


def test_budget_tokens_abort(tmp_path):
    _write_stats(tmp_path, 900_000, 200_000)
    pol = {"budget_policy": {"max_tokens_per_run": 1_000_000, "action_on_exceed": "abort"}}
    assert R._check_budget(tmp_path, pol, time.time(), _LOG) is False


def test_budget_warn_continues(tmp_path):
    _write_stats(tmp_path, 900_000, 200_000)
    pol = {"budget_policy": {"max_tokens_per_run": 1_000_000, "action_on_exceed": "warn"}}
    assert R._check_budget(tmp_path, pol, time.time(), _LOG) is True


def test_budget_cost_pricing(tmp_path):
    # deepseek 2元/8元 per Mtok: 1M in + 1M out = 10 元 > 5 元上限
    _write_stats(tmp_path, 1_000_000, 1_000_000, model="deepseek_chat")
    pol = {"budget_policy": {"max_cost_yuan": 5.0, "action_on_exceed": "abort",
                             "pricing_yuan_per_mtok": {
                                 "deepseek": {"in": 2.0, "out": 8.0},
                                 "default": {"in": 4.0, "out": 12.0}}}}
    assert R._check_budget(tmp_path, pol, time.time(), _LOG) is False


def test_budget_wall_clock(tmp_path):
    pol = {"budget_policy": {"max_wall_clock_minutes": 60, "action_on_exceed": "abort"}}
    assert R._check_budget(tmp_path, pol, time.time() - 7200, _LOG) is False


def test_budget_no_stats_passes(tmp_path):
    pol = {"budget_policy": {"max_tokens_per_run": 1000, "action_on_exceed": "abort"}}
    assert R._check_budget(tmp_path, pol, time.time(), _LOG) is True


# ─── verify 门禁 ───────────────────────────────────────────────────

def test_verify_gate_requires_human(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir(parents=True)
    report = {"findings": [{"severity": "fail", "check": "反串标-正文字体",
                            "detail": "三家相同", "class": "format_identical",
                            "variant": ""}]}
    (output / "verify_report.json").write_text(json.dumps(report), encoding="utf-8")
    calls = []
    monkeypatch.setattr(R, "run_script", lambda s, a, log: calls.append(s) or 0)
    assert R.enforce_verify_gate(tmp_path, _state(), _LOG, POLICY) is False
    assert calls == []  # 人工类不应触发任何修复脚本


def test_verify_gate_cover_autofix(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir(parents=True)
    bad = {"page_cap": None, "per_variant": {},
           "findings": [{"severity": "fail", "check": "V01 封面", "detail": "缺",
                         "class": "cover_missing", "variant": "V01"}]}
    (output / "verify_report.json").write_text(json.dumps(bad), encoding="utf-8")
    calls = []

    def fake_run(script, cmd, log):
        calls.append((script, list(cmd)))
        if script == "bid_verify.py":
            (output / "verify_report.json").write_text(
                json.dumps({"findings": []}), encoding="utf-8")
        return 0

    monkeypatch.setattr(R, "run_script", fake_run)
    st = _state()
    assert R.enforce_verify_gate(tmp_path, st, _LOG, POLICY) is True
    assemble = next(c for s, c in calls if s == "bid_assemble.py")
    assert "--variant" in assemble and "V01" in assemble
    assert any(s == "bid_verify.py" for s, _ in calls)  # 修复后必须复验
    assert st["steps"]["verify"]["gate_decisions"]


def test_verify_gate_page_fix_uses_concise(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir(parents=True)
    bad = {"page_cap": 70, "per_variant": {"V02": {"pages": 100}},
           "findings": [{"severity": "fail", "check": "V02 页数",
                         "detail": "100 页 > 招标硬顶 70 页",
                         "class": "page_over_cap", "variant": "V02"}]}
    (output / "verify_report.json").write_text(json.dumps(bad), encoding="utf-8")
    calls = []

    def fake_run(script, cmd, log):
        calls.append((script, list(cmd)))
        if script == "bid_verify.py":
            (output / "verify_report.json").write_text(
                json.dumps({"findings": []}), encoding="utf-8")
        return 0

    monkeypatch.setattr(R, "run_script", fake_run)
    assert R.enforce_verify_gate(tmp_path, _state(), _LOG, POLICY) is True
    concise = next(c for s, c in calls if s == "bid_concise.py")
    ratio = float(concise[concise.index("--ratio") + 1])
    assert 0.6 <= ratio <= 0.7  # 70/100 * 0.95 留余量
    assert "V02" in concise
    # 精简后必须重装(docx 已 stale)
    assert any(s == "bid_assemble.py" for s, _ in calls)


# ─── compliance 分类 + 门禁 ────────────────────────────────────────

def _scan(**overrides):
    base = {
        "blind_violations": [],
        "response_check": [],
        "plan_alignment": [],
        "chart_coverage": {"loaded": False},
        "format_audit": {"exit_code": 0},
        "standards_check": {"fabricated": 0},
        "schedule_check": {"violations": []},
    }
    base.update(overrides)
    return base


def test_classify_compliance():
    scan = _scan(
        response_check=[
            {"label": "质保期", "keyword": "二年", "required": True, "present": False},
            {"label": "工期", "keyword": "100", "required": True, "present": True}],
        chart_coverage={"loaded": True, "missing": [
            {"chapter": "05", "section": "2", "type": "gantt"}]})
    issues = R._classify_compliance(scan)
    classes = sorted(i["class"] for i in issues)
    assert classes == ["chart_missing", "response_keyword_miss"]
    chart = next(i for i in issues if i["class"] == "chart_missing")
    assert chart["chapter"] == "05"


def test_classify_compliance_human_classes():
    scan = _scan(blind_violations=[{"file": "01.md", "hit": "某某公司"}],
                 format_audit={"exit_code": 2},
                 schedule_check={"violations": [{"detail": "工期超硬顶"}]})
    classes = {i["class"] for i in R._classify_compliance(scan)}
    assert classes == {"blind_violation", "format_severe", "schedule_violation"}


def test_compliance_gate_blind_requires_human(tmp_path, monkeypatch):
    v_reports = tmp_path / "output" / "V01" / "reports"
    v_reports.mkdir(parents=True)
    (v_reports / "compliance_scan.json").write_text(
        json.dumps(_scan(blind_violations=[{"hit": "公司名"}])), encoding="utf-8")
    calls = []
    monkeypatch.setattr(R, "run_script", lambda s, a, log: calls.append(s) or 0)
    assert R.enforce_compliance_gate(tmp_path, "batch", _state(), _LOG, POLICY) is False
    assert calls == []


def test_compliance_gate_chart_autofix(tmp_path, monkeypatch):
    v_reports = tmp_path / "output" / "V01" / "reports"
    v_reports.mkdir(parents=True)
    scan_path = v_reports / "compliance_scan.json"
    scan_path.write_text(json.dumps(_scan(
        chart_coverage={"loaded": True, "missing": [
            {"chapter": "05", "section": "1", "type": "gantt"}]})), encoding="utf-8")
    calls = []

    def fake_run(script, cmd, log):
        calls.append((script, list(cmd)))
        if script == "bid_compliance.py":
            scan_path.write_text(json.dumps(_scan()), encoding="utf-8")
        return 0

    monkeypatch.setattr(R, "run_script", fake_run)
    st = _state()
    assert R.enforce_compliance_gate(tmp_path, "batch", st, _LOG, POLICY) is True
    writer = next(c for s, c in calls if s == "bid_writer.py")
    assert "--chapter" in writer and "05" in writer and "--force" in writer
    scripts = [s for s, _ in calls]
    # 重写后必须 reviewer 复扫 + 重装 + compliance 复查
    assert "bid_reviewer.py" in scripts
    assert "bid_assemble.py" in scripts
    assert "bid_compliance.py" in scripts
    assert st["steps"]["compliance"]["gate_decisions"]


# ─── 失败台账 ───────────────────────────────────────────────────────

def test_ledger_dedup(tmp_path):
    e = {"failure_class": "step_fail", "step": "verify", "detail": "exit=1 失败步骤: verify"}
    F.append_failure(tmp_path, e)
    F.append_failure(tmp_path, e)
    recs = F.load_ledger(tmp_path)
    assert len(recs) == 1 and recs[0]["count"] == 2

    F.append_failure(tmp_path, {"failure_class": "budget_exceeded",
                                "step": "run", "detail": "exit=3"})
    assert len(F.load_ledger(tmp_path)) == 2


# ─── 编排器 + 告警 ─────────────────────────────────────────────────

def test_loop_state_roundtrip(tmp_path):
    st = BL.LoopState(tmp_path / "s.json")
    st.record(1, 1, 12.3)
    st.save()
    st2 = BL.LoopState(tmp_path / "s.json")
    assert st2.data["cycles"][0]["exit_code"] == 1
    assert st2.data["last_result"] == "exit_1"


def test_load_policy_default():
    pol = BL.load_policy()
    assert pol["retry_policy"]["verify"]["auto_fixable"]["page_over_cap"]["max"] == 1
    assert pol["retry_policy"]["compliance"]["max_retries"] == 1
    assert pol["budget_policy"]["action_on_exceed"] == "abort"
    assert "blind_violation" in pol["retry_policy"]["compliance"]["requires_human"]


def test_notify_filters_events(monkeypatch):
    sent = []
    monkeypatch.setattr(E, "send_feishu_message",
                        lambda *a, **k: sent.append(a) or True)
    pol = {"escalation_policy": {"feishu_chat_ids": ["oc_x"],
                                 "notify_on": ["failure", "loop_exhausted"]}}
    assert E.notify(pol, "start", "t", "c") == 0      # 未订阅事件不发
    assert E.notify(pol, "failure", "t", "c") == 1
    assert E.notify({}, "failure", "t", "c") == 0     # 空策略静默


def test_notify_empty_chat_ids():
    pol = {"escalation_policy": {"feishu_chat_ids": [], "notify_on": ["failure"]}}
    assert E.notify(pol, "failure", "t", "c") == 0
