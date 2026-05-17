"""测试 bid_run.py 编排层 — checkpoint 恢复 + 质量门禁 scan。"""
import json
import tempfile
from pathlib import Path

import pytest

import bid_run


# ─── _scan_majors(2.2 修复:跨 variant 扫) ───

def test_scan_majors_cross_variant():
    """V1 章 1 + V2 章 2 同时 major — 旧 bug 会漏 V2。"""
    summary = {"variants": [
        {"variant_id": "V01", "chapters": [
            {"chapter_num": "01", "severity": "major"},
            {"chapter_num": "02", "severity": "pass"},
        ]},
        {"variant_id": "V02", "chapters": [
            {"chapter_num": "01", "severity": "pass"},
            {"chapter_num": "02", "severity": "major"},
        ]},
    ]}
    result = bid_run._scan_majors(summary)
    assert set(result) == {("V01", "01"), ("V02", "02")}


def test_scan_majors_empty():
    assert bid_run._scan_majors(None) == []
    assert bid_run._scan_majors({"variants": []}) == []


def test_scan_majors_no_major():
    summary = {"variants": [
        {"variant_id": "V01", "chapters": [
            {"chapter_num": "01", "severity": "pass"},
            {"chapter_num": "02", "severity": "minor"},
        ]},
    ]}
    assert bid_run._scan_majors(summary) == []


# ─── checkpoint state 持久化 ───

def test_save_load_state_roundtrip(tmp_path: Path):
    state_path = tmp_path / ".bid_run.state.json"
    s = bid_run.load_state(state_path)
    assert s == {"steps": {}, "runs": []}

    bid_run.mark_step(s, "extract", "ok", chars=12345)
    bid_run.save_state(state_path, s)

    s2 = bid_run.load_state(state_path)
    assert s2["steps"]["extract"]["status"] == "ok"
    assert s2["steps"]["extract"]["chars"] == 12345
    assert "finished_at" in s2["steps"]["extract"]


def test_mark_step_running_no_finished(tmp_path: Path):
    state_path = tmp_path / ".bid_run.state.json"
    s = bid_run.load_state(state_path)
    bid_run.mark_step(s, "extract", "running")
    assert s["steps"]["extract"]["status"] == "running"
    assert "started_at" in s["steps"]["extract"]
    assert "finished_at" not in s["steps"]["extract"]


def test_mark_step_skip_keeps_note(tmp_path: Path):
    """skip 状态记录 note,resume 时可判断是否合理 skip。"""
    state_path = tmp_path / ".bid_run.state.json"
    s = bid_run.load_state(state_path)
    bid_run.mark_step(s, "template_match", "skip", note="no_template_match")
    assert s["steps"]["template_match"]["status"] == "skip"
    assert s["steps"]["template_match"]["note"] == "no_template_match"
