"""补充测试：dashboard 结果审查端点 + 契约未覆盖的边界（glob/二进制/符号链接/注入/数学）。"""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

import app as web_app  # noqa: E402
import _readers  # noqa: E402

app = web_app.app


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    return app.test_client()


# ─── 批量合规 glob：V01 reports/ + V02 平铺，标签正确 ──────────────────

def test_compliance_batch_glob_both_layouts(client, tmp_path: Path):
    (tmp_path / "extracted").mkdir()
    (tmp_path / "extracted" / "variants.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "output"
    v1 = out / "V01" / "reports"
    v1.mkdir(parents=True)
    (v1 / "compliance_scan.json").write_text(json.dumps({
        "blind_violations": [], "response_check": [], "outline_coverage": {"missing": []},
        "chart_coverage": {"missing": []}, "format_audit": {"violations": []},
        "standards_check": {"fabricated": 0},
    }), encoding="utf-8")
    v2 = out / "V02"  # 旧平铺路径
    v2.mkdir(parents=True)
    (v2 / "compliance_scan.json").write_text(json.dumps({
        "blind_violations": [{"x": 1}], "response_check": [], "outline_coverage": {"missing": []},
        "chart_coverage": {"missing": []}, "format_audit": {"violations": []},
        "standards_check": {"fabricated": 0},
    }), encoding="utf-8")

    resp = client.get(f"/api/dashboard/compliance?project_dir={tmp_path}")
    assert resp.status_code == 200
    vs = resp.get_json()["variants"]
    assert [v["variant"] for v in vs] == ["V01", "V02"]
    assert vs[0]["risk_level"] == "green"
    assert vs[1]["risk_level"] == "red"
    assert vs[1]["counts"]["blind_violations"] == 1


# ─── /file：二进制 .docx 拒绝 ──────────────────────────────────────────

def test_file_rejects_binary_docx(client, tmp_path: Path):
    out = tmp_path / "output"
    out.mkdir()
    f = out / "x.docx"
    f.write_bytes(b"PK\x03\x04\x00\x00\x00binary")  # docx 是 zip 二进制
    resp = client.get(f"/api/dashboard/file?project_dir={tmp_path}&path={f}")
    assert resp.status_code in (403, 415)


# ─── /file：符号链接逃逸 → 403 ─────────────────────────────────────────

def test_file_rejects_symlink_escape(client, tmp_path: Path):
    outside = tmp_path.parent / "outside-symlink-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "leak.txt"
    try:
        os.symlink(outside, link)
        resp = client.get(f"/api/dashboard/file?project_dir={tmp_path}&path={link}")
        assert resp.status_code == 403
    finally:
        outside.unlink(missing_ok=True)


# ─── /control：非法 from_step → 400（防注入，且不触发 Popen）─────────

def test_control_rejects_invalid_from_step(client, tmp_path: Path):
    resp = client.post("/api/dashboard/control", json={
        "project_dir": str(tmp_path), "action": "start",
        "from_step": "; rm -rf /",
    })
    assert resp.status_code == 400


# ─── /state：running 步不计入 progress，runtime 有值 ──────────────────

def test_state_running_excluded_from_progress(client, tmp_path: Path):
    sp = tmp_path / "output" / ".bid_run.state.json"
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"steps": {
        "extract": {"status": "ok", "started_at": "2026-06-23T10:00:00",
                     "finished_at": "2026-06-23T10:01:00"},
        "skeleton": {"status": "running", "started_at": "2026-06-23T10:01:00"},
    }}), encoding="utf-8")
    resp = client.get(f"/api/dashboard/state?project_dir={tmp_path}")
    payload = resp.get_json()
    assert payload["summary"]["progress"] == 1  # 只有 extract(ok)
    assert any(s["status"] == "running" for s in payload["steps"])
    assert payload["runtime"] is not None and payload["runtime"] > 0


# ─── /budget：已知输入的成本数学 ──────────────────────────────────────

def test_budget_cost_math(client, tmp_path: Path, monkeypatch):
    out = tmp_path / "output"
    (out / "logs").mkdir(parents=True)
    (out / "logs" / "llm_stats.json").write_text(json.dumps({"buckets": {
        "writer": {"total_calls": 1, "total_seconds": 10, "by_model": {
            "mymodel": {"calls": 1, "prompt": 500000, "completion": 250000, "total": 750000},
        }},
    }}), encoding="utf-8")
    # 把 CONFIGS 指向 tmp，写入已知 pricing 策略
    monkeypatch.setattr(_readers, "CONFIGS", tmp_path)
    (tmp_path / "bid_loop_policy.json").write_text(json.dumps({"budget_policy": {
        "max_tokens_per_run": 2000000, "max_cost_yuan": 50.0,
        "pricing_yuan_per_mtok": {"default": {"in": 4.0, "out": 12.0}},
    }}), encoding="utf-8")

    resp = client.get(f"/api/dashboard/budget?project_dir={tmp_path}")
    u = resp.get_json()["used"]
    assert u["tokens"] == 750000
    # 500000/1e6*4 + 250000/1e6*12 = 2 + 3 = 5
    assert abs(u["cost_yuan"] - 5.0) < 0.01


# ─── /similarity：阈值覆盖生效 ────────────────────────────────────────

def test_similarity_threshold_override(client, tmp_path: Path):
    out = tmp_path / "output"
    (tmp_path / "extracted").mkdir()
    out.mkdir()
    (out / "similarity_matrix.json").write_text(json.dumps({
        "ids": ["V01", "V02"],
        "pairs": [{"a": "V01", "b": "V02", "overall": 0.20, "paragraph_max": 0.10}],
    }), encoding="utf-8")
    # 默认阈值 0.40 → 0.20 不超；覆盖为 0.1 → 超阈
    (tmp_path / "extracted" / "variants.json").write_text(json.dumps(
        {"similarity_threshold": {"overall": 0.1}}), encoding="utf-8")

    resp = client.get(f"/api/dashboard/similarity?project_dir={tmp_path}")
    payload = resp.get_json()
    assert payload["threshold"]["overall"] == 0.1
    assert len(payload["over"]) == 1


# ─── /asset：越界 403 / 合法 png 200 ───────────────────────────────────

def test_asset_boundaries(client, tmp_path: Path):
    out = tmp_path / "output"
    out.mkdir()
    png = out / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n fake png")  # PNG 魔数
    # 合法
    r1 = client.get(f"/api/dashboard/asset?project_dir={tmp_path}&path={png}")
    assert r1.status_code == 200
    assert r1.mimetype == "image/png"
    # 越界
    outside = tmp_path.parent / "outside-asset.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        r2 = client.get(f"/api/dashboard/asset?project_dir={tmp_path}&path={outside}")
        assert r2.status_code == 403
    finally:
        outside.unlink(missing_ok=True)
