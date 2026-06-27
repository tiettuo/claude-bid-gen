#!/usr/bin/env python3
"""web/dashboard.py — project-dir 导向的检视器只读后端（Flask Blueprint）。

13 个只读端点 + 1 个入口页（tests/test_web_dashboard.py 契约的只读部分）：
  GET  /dashboard                                  入口页（带 project_dir → 检视器）
  GET  /api/dashboard/inspect | state | step2 | compliance | file | asset
  GET  /api/dashboard/budget | loop | gates | similarity | verify | review | charts

POST /api/dashboard/control（唯一变更端点）与 _pid_alive 放在 app.py——
测试 `import app as web_app` 后 monkeypatch `web_app._pid_alive` /
`web_app.subprocess.Popen`，control 必须与这两者同处 app 命名空间才能命中。

数据聚合逻辑全部在 _readers.py；本模块只做 HTTP 适配。
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, render_template

import _readers
from _readers import (resolve_tender, read_text_safe, read_asset_safe,
                      read_state_summary, read_step2, aggregate_compliance,
                      compute_budget, read_loop, read_gates, read_similarity,
                      read_verify, read_review, read_charts,
                      read_log_tail, read_step_log)
from bid_run import STEPS  # _readers 已把 scripts/ 加入 sys.path

dashboard_bp = Blueprint("dashboard", __name__)


def _render_console(project_dir: str = ""):
    """渲染统一运行控制台页（/ 与 /dashboard 共用）。"""
    tender = resolve_tender(project_dir) if project_dir else None
    return render_template("console.html",
                           project_dir=str(tender) if tender else "",
                           project_name=tender.name if tender else "",
                           steps=list(STEPS))


def _project():
    """从 query 取 project_dir 并校验目录；无效 → (None, 400 response)。"""
    pd = request.args.get("project_dir", "").strip()
    p = resolve_tender(pd)
    if p is None:
        return None, (jsonify({"error": "project_dir 缺失或非目录"}), 400)
    return p, None


# ─── 入口页 ──────────────────────────────────────────────────────────────

@dashboard_bp.route("/dashboard")
def dashboard_page():
    """统一运行控制台（GitHub Actions 风格单页）。带 project_dir 直接定位该项目。"""
    return _render_console(request.args.get("project_dir", "").strip())


@dashboard_bp.route("/api/dashboard/log")
def api_log():
    p, err = _project()
    if err:
        return err
    return jsonify(read_log_tail(p))


@dashboard_bp.route("/api/dashboard/step_log")
def api_step_log():
    p, err = _project()
    if err:
        return err
    return jsonify(read_step_log(p, request.args.get("step", "").strip()))


# ─── 契约只读端点 ────────────────────────────────────────────────────────

@dashboard_bp.route("/api/dashboard/inspect")
def api_inspect():
    p, err = _project()
    if err:
        return err
    return jsonify({"ok": True, "project_dir": str(p), "name": p.name})


@dashboard_bp.route("/api/dashboard/state")
def api_state():
    p, err = _project()
    if err:
        return err
    return jsonify(read_state_summary(p))


@dashboard_bp.route("/api/dashboard/step2")
def api_step2():
    p, err = _project()
    if err:
        return err
    return jsonify(read_step2(p))


@dashboard_bp.route("/api/dashboard/compliance")
def api_compliance():
    p, err = _project()
    if err:
        return err
    return jsonify(aggregate_compliance(p))


@dashboard_bp.route("/api/dashboard/file")
def api_file():
    p, err = _project()
    if err:
        return err
    target = request.args.get("path", "")
    status, body = read_text_safe(str(p), target)
    return jsonify(body), status


@dashboard_bp.route("/api/dashboard/asset")
def api_asset():
    p, err = _project()
    if err:
        return err
    target = request.args.get("path", "")
    real = read_asset_safe(str(p), target)
    if real is None:
        return jsonify({"error": "forbidden or unsupported"}), 403
    return send_file(real)


# ─── 结果审查端点（只读） ───────────────────────────────────────────────

@dashboard_bp.route("/api/dashboard/budget")
def api_budget():
    p, err = _project()
    if err:
        return err
    return jsonify(compute_budget(p))


@dashboard_bp.route("/api/dashboard/loop")
def api_loop():
    p, err = _project()
    if err:
        return err
    return jsonify(read_loop(p))


@dashboard_bp.route("/api/dashboard/gates")
def api_gates():
    p, err = _project()
    if err:
        return err
    return jsonify(read_gates(p))


@dashboard_bp.route("/api/dashboard/similarity")
def api_similarity():
    p, err = _project()
    if err:
        return err
    return jsonify(read_similarity(p))


@dashboard_bp.route("/api/dashboard/verify")
def api_verify():
    p, err = _project()
    if err:
        return err
    return jsonify(read_verify(p))


@dashboard_bp.route("/api/dashboard/review")
def api_review():
    p, err = _project()
    if err:
        return err
    return jsonify(read_review(p))


@dashboard_bp.route("/api/dashboard/charts")
def api_charts():
    p, err = _project()
    if err:
        return err
    return jsonify(read_charts(p))
