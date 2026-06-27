#!/usr/bin/env python3
"""
bid-gen Flask 服务
==================

两部分：
  1. job 上传流程（既有）：
       GET  /             首页(上传表单 + 任务列表 + 仪表盘入口)
       POST /upload       上传 zip → 解压 → 启动 bid_run.py 后台进程 → 返回 job_id
       GET  /status/<id>  返回 JSON {step, progress, log_tail}（+ ?json=1）
       GET  /download/<id>  下载 docx (单/多)的 zip 打包
       GET  /jobs         列出最近任务
  2. dashboard 检视器（见 dashboard.py / _readers.py）：
       GET  /dashboard    项目选择页 / 检视器单页（blueprint）
       GET  /api/dashboard/*  只读聚合（blueprint）
       POST /api/dashboard/control  Start/Stop（本文件，与 _pid_alive 同处 app 命名空间
                                     便于 tests monkeypatch）

无 Celery/Redis 依赖，subprocess.Popen + 文件系统状态。

用法:
  python web/app.py            # localhost:5000
  python web/app.py --port 8080
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess  # 模块级：tests monkeypatch web_app.subprocess.Popen
import sys
import time
import uuid
import zipfile
from pathlib import Path

from flask import (
    Flask, request, jsonify, send_file, abort, redirect, url_for, render_template,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
JOBS_DIR = Path.home() / ".bid_gen_jobs"
JOBS_DIR.mkdir(exist_ok=True)

# 18 步单一真值取自 bid_run（修正原先陈旧的 16 步硬编码）
from bid_run import STEPS  # noqa: E402
STEP_LIST = STEPS

from _readers import resolve_tender  # noqa: E402
from dashboard import dashboard_bp, _render_console  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.register_blueprint(dashboard_bp)


def _pid_alive(pid) -> bool:
    """进程是否存活（os.kill 0 探测）。模块级：tests monkeypatch web_app._pid_alive 目标。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


# ─── Job 状态管理 ───────────────────────────────────────────────────────

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def write_meta(job_id: str, **fields):
    p = job_dir(job_id) / "meta.json"
    meta = json.loads(p.read_text()) if p.exists() else {}
    meta.update(fields)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def read_meta(job_id: str) -> dict:
    p = job_dir(job_id) / "meta.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def list_jobs(limit: int = 20) -> list[dict]:
    items = []
    for jp in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not jp.is_dir():
            continue
        meta = json.loads((jp / "meta.json").read_text()) if (jp / "meta.json").exists() else {}
        meta["job_id"] = jp.name
        items.append(meta)
        if len(items) >= limit:
            break
    return items


def current_progress(job_id: str) -> dict:
    """从 .bid_run.state.json 推断进度;补 log 尾部。"""
    meta = read_meta(job_id)
    tender = Path(meta.get("tender_dir", ""))
    state_p = tender / "output" / ".bid_run.state.json"
    log_p = tender / "output" / "run.log"

    state = {}
    current_step = None
    progress_n = 0
    if state_p.exists():
        try:
            state = json.loads(state_p.read_text())
            for i, sname in enumerate(STEP_LIST):
                s = state.get("steps", {}).get(sname, {})
                if s.get("status") in ("ok", "skip"):
                    progress_n = i + 1
                elif s.get("status") == "running":
                    current_step = sname
                    break
                elif s.get("status") == "fail":
                    current_step = sname
                    break
        except Exception:
            pass

    # 优先 bid_run 自己的 run.log(更结构化);fallback 到 Popen 重定向的日志
    log_tail = ""
    for cand in [log_p, job_dir(job_id) / "run.log"]:
        if cand.exists():
            try:
                txt = cand.read_text(encoding="utf-8", errors="ignore")
                if txt.strip():
                    log_tail = txt[-3000:]
                    break
            except Exception:
                pass

    pid = meta.get("pid")
    alive = _pid_alive(pid)

    # 终态:有 summary 或所有 step ok
    finished = False
    if state.get("steps", {}).get("summary", {}).get("status") == "ok":
        finished = True
    elif state.get("steps", {}).get("assemble", {}).get("status") == "ok":
        # 用户可能 --to assemble 提前停
        finished = not alive
    if not alive and progress_n >= len(STEP_LIST) - 1:
        finished = True

    return {
        "job_id": job_id,
        "tender_name": meta.get("tender_name", ""),
        "current_step": current_step,
        "progress": progress_n,
        "total": len(STEP_LIST),
        "alive": alive,
        "finished": finished,
        "log_tail": log_tail,
        "started_at": meta.get("started_at"),
        "exit_code": meta.get("exit_code"),
    }


# ─── 路由：job 上传流程 ─────────────────────────────────────────────────

@app.route("/")
def index():
    # 统一运行控制台（GitHub Actions 风格）；无 project_dir 时默认定位到最近 job
    pd = request.args.get("project_dir", "").strip()
    if not pd:
        for j in list_jobs(1):
            td = j.get("tender_dir")
            if td and Path(td).is_dir():
                pd = td
                break
    return _render_console(pd)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("zipfile")
    if not f or not f.filename:
        return "no file", 400
    if not f.filename.endswith(".zip"):
        return "only .zip accepted", 400

    job_id = uuid.uuid4().hex[:12]
    jd = job_dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    tender_dir = jd / "tender"
    tender_dir.mkdir(exist_ok=True)

    # 解压到 tender_dir
    zbytes = f.read()
    try:
        with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
            zf.extractall(tender_dir)
    except zipfile.BadZipFile:
        return "bad zip", 400

    # 若 zip 内只有一个顶层目录,把它的内容上提
    sub = list(tender_dir.iterdir())
    if len(sub) == 1 and sub[0].is_dir():
        inner = sub[0]
        for p in inner.iterdir():
            shutil.move(str(p), str(tender_dir / p.name))
        inner.rmdir()

    tender_name = Path(f.filename).stem

    # 启动 bid_run
    cmd = [sys.executable, str(SCRIPTS / "bid_run.py"), str(tender_dir),
           "--force-continue"]
    target_pages = request.form.get("target_pages", "").strip()
    if target_pages:
        cmd += ["--target-pages", target_pages]
    if request.form.get("manual_step2"):
        cmd.append("--manual-step2")

    log_p = jd / "run.log"
    proc = subprocess.Popen(cmd, stdout=open(log_p, "w"), stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONUNBUFFERED": "1"})

    write_meta(job_id,
               tender_dir=str(tender_dir),
               tender_name=tender_name,
               company_name=request.form.get("company_name", "").strip() or "投标单位名称",
               started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
               pid=proc.pid,
               cmd=cmd)

    return redirect(f"/?project_dir={tender_dir}")


@app.route("/status/<job_id>")
def status(job_id):
    meta = read_meta(job_id)
    # ?json=1 保留为程序化轮询契约；否则重定向到统一控制台
    if request.args.get("json"):
        if not job_dir(job_id).exists():
            abort(404)
        return jsonify(current_progress(job_id))
    td = meta.get("tender_dir", "")
    if td and Path(td).is_dir():
        return redirect(f"/?project_dir={td}")
    abort(404)


@app.route("/download/<job_id>")
def download(job_id):
    meta = read_meta(job_id)
    tender = Path(meta.get("tender_dir", ""))
    if not tender.exists():
        abort(404)
    output = tender / "output"
    docxs = list(output.glob("*.docx"))
    if not docxs:
        return "no docx generated yet", 404
    if len(docxs) == 1:
        return send_file(docxs[0], as_attachment=True)
    # 多份打包
    zip_path = job_dir(job_id) / "output.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for d in docxs:
            zf.write(d, d.name)
        # 附带 brain/plan/rules + 合规报告
        for extra in [
            tender / "extracted" / "project_brain.md",
            tender / "extracted" / "chapter_plan.md",
            tender / "extracted" / "disqualification_rules.md",
        ]:
            if extra.exists():
                zf.write(extra, f"extracted/{extra.name}")
        for v in output.glob("V*/reports/compliance_scan.md"):
            zf.write(v, f"compliance/{v.parent.parent.name}_compliance.md")
    return send_file(zip_path, as_attachment=True, download_name=f"{meta.get('tender_name', 'bid')}_output.zip")


@app.route("/jobs")
def jobs():
    items = list_jobs()
    for j in items:  # 补 progress/finished/alive，供控制台左侧运行列表用
        try:
            j.update(current_progress(j["job_id"]))
        except Exception:
            pass
    return jsonify(items)


# ─── 路由：dashboard 控制（唯一变更端点，与 _pid_alive 同处 app 命名空间） ───

@app.route("/api/dashboard/control", methods=["POST"])
def dashboard_control():
    """Start / Stop 一个 project_dir 的 bid_run。tests monkeypatch _pid_alive/subprocess.Popen。"""
    body = request.get_json(silent=True) or {}
    project_dir = (body.get("project_dir") or "").strip()
    p = resolve_tender(project_dir)
    if p is None:
        return jsonify({"error": "project_dir 缺失或非目录"}), 400
    action = body.get("action")
    if action not in ("start", "stop"):
        return jsonify({"error": f"unknown action: {action}"}), 400

    output = p / "output"
    output.mkdir(parents=True, exist_ok=True)
    control_path = output / ".dashboard_control.json"

    if action == "start":
        # 校验 from/to step 必须在 STEPS 内（防注入）
        for key in ("from_step", "to_step"):
            val = body.get(key)
            if val and val not in STEPS:
                return jsonify({"error": f"invalid {key}: {val}"}), 400
        cmd = [sys.executable, str(SCRIPTS / "bid_run.py"), str(p)]
        if body.get("resume", True):
            cmd.append("--resume")
        if body.get("from_step"):
            cmd += ["--from", body["from_step"]]
        if body.get("to_step"):
            cmd += ["--to", body["to_step"]]
        if body.get("force_continue"):
            cmd.append("--force-continue")

        log_p = output / "logs" / "dashboard_control.log"
        log_p.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(cmd, stdout=open(log_p, "w"),
                                stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        existing = {}
        if control_path.exists():
            try:
                existing = json.loads(control_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        runs = existing.get("runs", [])
        runs.append({"at": now, "pid": proc.pid, "action": "start", "cmd": cmd})
        control = {"pid": proc.pid, "status": "running", "cmd": cmd, "runs": runs}
        control_path.write_text(json.dumps(control, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify({"alive": _pid_alive(proc.pid), "status": "running",
                        "pid": proc.pid, "cmd": cmd})

    # action == "stop"：MVP 仅标记 stopped（不真发 SIGTERM），返回探测结果
    existing = {}
    if control_path.exists():
        try:
            existing = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    pid = existing.get("pid")
    alive = _pid_alive(pid)
    existing["status"] = "stopped"
    existing["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    control_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"alive": alive, "status": "stopped"})


# ─── 入口 ──────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print(f"bid-gen Web 启动: http://{args.host}:{args.port}")
    print(f"Job 状态目录: {JOBS_DIR}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
