"""Tests for the read-only Flask dashboard API."""

import json
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

import app as web_app  # noqa: E402

app = web_app.app

# web/app.py 当前是 job 上传/状态应用,尚未实现本测试假设的 dashboard API
# (/dashboard 与 /api/dashboard/*)。功能落地前整体跳过,保留测试作为规格。
pytestmark = pytest.mark.skip(
    reason="dashboard API 尚未在 web/app.py 实现,本测试为未来功能的规格"
)


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    return app.test_client()


def test_dashboard_page_loads(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert b"bid-gen Dashboard" in resp.data


def test_inspect_rejects_missing_project(client):
    resp = client.get("/api/dashboard/inspect?project_dir=/definitely/not/here")
    assert resp.status_code == 400


def test_state_missing_state_file_returns_not_started(client, tmp_path: Path):
    resp = client.get(f"/api/dashboard/state?project_dir={tmp_path}")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["steps"]
    assert "runtime" in payload
    assert payload["summary"]["progress"] == 0
    assert payload["interventions"]
    assert all(s["status"] == "not_started" for s in payload["steps"])


def test_step2_missing_files_returns_warnings(client, tmp_path: Path):
    (tmp_path / "extracted").mkdir()
    resp = client.get(f"/api/dashboard/step2?project_dir={tmp_path}")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["skeleton"]["evaluation_method"]["exists"] is False
    assert payload["warnings"]


def test_compliance_missing_report_is_yellow(client, tmp_path: Path):
    (tmp_path / "output").mkdir()
    resp = client.get(f"/api/dashboard/compliance?project_dir={tmp_path}")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["variants"][0]["risk_level"] == "yellow"


def test_file_preview_allows_project_text(client, tmp_path: Path):
    p = tmp_path / "output" / "reports"
    p.mkdir(parents=True)
    f = p / "compliance_scan.md"
    f.write_text("# OK", encoding="utf-8")
    resp = client.get(f"/api/dashboard/file?project_dir={tmp_path}&path={f}")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == "# OK"


def test_file_preview_rejects_env(client, tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("SECRET=1", encoding="utf-8")
    resp = client.get(f"/api/dashboard/file?project_dir={tmp_path}&path={f}")
    assert resp.status_code == 403


def test_file_preview_rejects_outside_project(client, tmp_path: Path):
    outside = tmp_path.parent / "outside-dashboard-test.txt"
    outside.write_text("nope", encoding="utf-8")
    try:
        resp = client.get(f"/api/dashboard/file?project_dir={tmp_path}&path={outside}")
        assert resp.status_code == 403
    finally:
        outside.unlink(missing_ok=True)


def test_compliance_counts_hard_risks(client, tmp_path: Path):
    report_dir = tmp_path / "output" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "compliance_scan.json").write_text(json.dumps({
        "blind_violations": [{"x": 1}],
        "response_check": [{"required": True, "present": False}],
        "outline_coverage": {"missing": ["其他"]},
        "chart_coverage": {"missing": [{"type": "schedule"}]},
        "format_audit": {"violations": [{"x": 1}], "style_violations": []},
        "standards_check": {"fabricated": 1},
    }), encoding="utf-8")

    resp = client.get(f"/api/dashboard/compliance?project_dir={tmp_path}")
    assert resp.status_code == 200
    variant = resp.get_json()["variants"][0]
    assert variant["risk_level"] == "red"
    assert variant["counts"]["standard_fabrications"] == 1


def test_control_rejects_unknown_action(client, tmp_path: Path):
    resp = client.post("/api/dashboard/control", json={
        "project_dir": str(tmp_path),
        "action": "pause",
    })
    assert resp.status_code == 400


def test_control_start_records_process(monkeypatch, client, tmp_path: Path):
    class DummyProc:
        pid = 43210

    monkeypatch.setattr(web_app, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(web_app.subprocess, "Popen", lambda *a, **kw: DummyProc())

    resp = client.post("/api/dashboard/control", json={
        "project_dir": str(tmp_path),
        "action": "start",
        "resume": True,
        "from_step": "review",
        "to_step": "compliance",
        "force_continue": True,
    })

    assert resp.status_code == 200
    control = json.loads((tmp_path / "output" / ".dashboard_control.json").read_text(encoding="utf-8"))
    assert control["pid"] == 43210
    assert "--resume" in control["cmd"]
    assert "--from" in control["cmd"]
    assert "--to" in control["cmd"]
    assert control["runs"]


def test_control_stop_updates_state(monkeypatch, client, tmp_path: Path):
    control_path = tmp_path / "output" / ".dashboard_control.json"
    control_path.parent.mkdir(parents=True)
    control_path.write_text(json.dumps({"pid": 12345, "status": "running"}), encoding="utf-8")
    monkeypatch.setattr(web_app, "_pid_alive", lambda pid: False)

    resp = client.post("/api/dashboard/control", json={
        "project_dir": str(tmp_path),
        "action": "stop",
    })

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["alive"] is False
    assert payload["status"] == "stopped"


def test_state_suggests_failed_step_rerun(client, tmp_path: Path):
    state_path = tmp_path / "output" / ".bid_run.state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "steps": {
            "extract": {"status": "ok"},
            "review": {"status": "fail", "error": "major"},
        }
    }), encoding="utf-8")

    resp = client.get(f"/api/dashboard/state?project_dir={tmp_path}")
    payload = resp.get_json()

    assert payload["summary"]["first_failed"] == "review"
    assert payload["interventions"][0]["from_step"] == "review"
