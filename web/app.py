#!/usr/bin/env python3
"""
bid-gen Flask MVP
=================

单文件 Web 服务,提供:
  GET  /             首页(上传表单 + 任务列表)
  POST /upload       上传 zip → 解压 → 启动 bid_run.py 后台进程 → 返回 job_id
  GET  /status/<id>  返回 JSON {step, progress, log_tail}
  GET  /download/<id>  下载 docx (单/多)的 zip 打包
  GET  /jobs         列出最近任务

无 Celery/Redis 依赖,subprocess.Popen + 文件系统状态。

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
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

from flask import (
    Flask, request, jsonify, send_file, abort, redirect, url_for, render_template_string,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
JOBS_DIR = Path.home() / ".bid_gen_jobs"
JOBS_DIR.mkdir(exist_ok=True)

STEP_LIST = [
    "extract", "template", "facts", "derive", "skeleton", "special",
    "input_assemble", "fingerprint", "variants", "chart_specs",
    "write", "review", "assemble", "compliance", "similarity", "summary",
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB


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
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False

    # 终态:有 summary 或所有 step ok
    finished = False
    if state.get("steps", {}).get("summary", {}).get("status") == "ok":
        finished = True
    elif state.get("steps", {}).get("assemble", {}).get("status") == "ok":
        # 用户可能 --to assemble 提前停
        finished = not alive
    if not alive and progress_n >= 13:
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


# ─── 路由 ──────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>bid-gen</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 880px; margin: 30px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 22px; border-bottom: 1px solid #ddd; padding-bottom: 8px; }
  .card { border: 1px solid #ddd; border-radius: 6px; padding: 16px; margin: 16px 0; }
  input[type=file], input[type=text], input[type=number] { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 4px; }
  button { padding: 10px 18px; border: none; border-radius: 4px; background: #2563eb; color: white; cursor: pointer; font-size: 14px; }
  button:hover { background: #1d4ed8; }
  label { display: block; margin: 8px 0 4px; font-weight: 500; font-size: 13px; color: #555; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid #eee; }
  th { background: #f7f7f7; font-weight: 500; }
  .ok { color: #16a34a; }
  .fail { color: #dc2626; }
  .running { color: #2563eb; }
  small { color: #888; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>bid-gen — 投标技术文件生成</h1>

<div class="card">
  <form method="post" action="/upload" enctype="multipart/form-data">
    <label>招标文件压缩包 (.zip)</label>
    <input type="file" name="zipfile" accept=".zip" required>
    <label>投标公司名(可选;空则用占位符,后期替换)</label>
    <input type="text" name="company_name" placeholder="如:某某建筑工程有限公司">
    <label>目标页数(可选;空则按规模自动判档)</label>
    <input type="number" name="target_pages" placeholder="留空 → 自动判档(35/70/120 etc.)" min="20" max="300">
    <label style="margin-top: 16px;">
      <input type="checkbox" name="manual_step2"> 走人工 Step 2(等 Claude 手工生成 brain/plan/rules)
    </label>
    <p style="margin-top: 16px;">
      <button type="submit">开始生成</button>
      <small style="margin-left: 12px;">将启动后台进程,完成约 8-15 分钟</small>
    </p>
  </form>
</div>

<div class="card">
  <h3 style="margin-top: 0;">最近任务</h3>
  {% if jobs %}
  <table>
    <thead><tr><th>项目</th><th>进度</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>
    {% for j in jobs %}
      <tr>
        <td>{{ j.tender_name }}</td>
        <td>{{ j.progress|default(0) }}/16</td>
        <td>
          {% if j.finished %}<span class="ok">完成</span>
          {% elif j.alive %}<span class="running">运行中</span>
          {% else %}<span class="fail">中断</span>
          {% endif %}
        </td>
        <td>
          <a href="/status/{{ j.job_id }}">详情</a>
          {% if j.finished %}| <a href="/download/{{ j.job_id }}">下载</a>{% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p><small>(暂无任务)</small></p>
  {% endif %}
</div>
</body>
</html>"""

STATUS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{{ data.tender_name }} — bid-gen</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1000px; margin: 30px auto; padding: 0 20px; }
  .progress { background: #eee; height: 24px; border-radius: 4px; overflow: hidden; margin: 10px 0; }
  .progress-bar { height: 100%; background: #2563eb; transition: width 0.3s; color: white; text-align: center; line-height: 24px; font-size: 12px; }
  pre { background: #1e293b; color: #f1f5f9; padding: 16px; border-radius: 6px; overflow-x: auto; font-size: 12px; line-height: 1.4; max-height: 500px; overflow-y: auto; }
  a { color: #2563eb; }
  .ok { color: #16a34a; font-weight: bold; }
  .fail { color: #dc2626; font-weight: bold; }
</style>
</head>
<body>
<a href="/">← 返回</a>
<h2>{{ data.tender_name }}</h2>
<p><small>Job: {{ data.job_id }}</small></p>

<div class="progress">
  <div class="progress-bar" id="pbar" style="width: {{ (data.progress/data.total*100)|round(0) }}%;">
    {{ data.progress }}/{{ data.total }}
  </div>
</div>

<p>
  当前步骤:<b id="step">{{ data.current_step or "-" }}</b>
  | 状态:
  {% if data.finished %}<span class="ok" id="state">完成 ✓</span>
  {% elif data.alive %}<span id="state">运行中</span>
  {% else %}<span class="fail" id="state">已中断</span>
  {% endif %}
</p>

{% if data.finished %}
<p><a href="/download/{{ data.job_id }}"><button style="padding: 10px 20px;">下载 DOCX</button></a></p>
{% endif %}

<h3>实时日志</h3>
<pre id="log">{{ data.log_tail }}</pre>

<script>
const jobId = "{{ data.job_id }}";
let finished = {{ "true" if data.finished else "false" }};
function poll() {
  if (finished) return;
  fetch(`/status/${jobId}?json=1`).then(r => r.json()).then(d => {
    document.getElementById("pbar").style.width = (d.progress/d.total*100).toFixed(0) + "%";
    document.getElementById("pbar").textContent = d.progress + "/" + d.total;
    document.getElementById("step").textContent = d.current_step || "-";
    document.getElementById("log").textContent = d.log_tail;
    document.getElementById("log").scrollTop = document.getElementById("log").scrollHeight;
    if (d.finished) {
      document.getElementById("state").textContent = "完成 ✓";
      document.getElementById("state").className = "ok";
      setTimeout(() => location.reload(), 1500);
      finished = true;
    } else if (!d.alive) {
      document.getElementById("state").textContent = "已中断";
      document.getElementById("state").className = "fail";
      finished = true;
    }
  });
}
setInterval(poll, 3000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    jobs = list_jobs()
    # 补 alive/finished/progress(每次列表刷新)
    for j in jobs:
        try:
            p = current_progress(j["job_id"])
            j.update(p)
        except Exception:
            pass
    return render_template_string(INDEX_HTML, jobs=jobs)


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

    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
def status(job_id):
    if not job_dir(job_id).exists():
        abort(404)
    data = current_progress(job_id)
    if request.args.get("json"):
        return jsonify(data)
    return render_template_string(STATUS_HTML, data=data)


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
    return jsonify(list_jobs())


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
