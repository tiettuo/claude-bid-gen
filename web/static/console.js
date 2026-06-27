/* bid-gen 运行控制台（GitHub Actions 风格单页）：rail + 步骤清单 + 流式日志 + 问题 + 结果面板 */
"use strict";
let currentDir = window.PROJECT_DIR || "";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmtInt = (n) => Number(n || 0).toLocaleString();
const api = (name, extra = "") => `/api/dashboard/${name}?project_dir=${encodeURIComponent(currentDir)}${extra}`;
async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }

let lastState = null, lastComp = null, lastVerify = null, lastSim = null;

// ─── 项目切换 ────────────────────────────────────────────────────────────
async function loadProject(dir) {
  currentDir = dir || "";
  if (dir) history.replaceState(null, "", `/?project_dir=${encodeURIComponent(dir)}`);
  else history.replaceState(null, "", "/");
  if (dir) {
    try { const j = await getJSON(api("inspect")); $("#proj-name").textContent = j.name || dir; }
    catch (e) { $("#proj-name").textContent = dir; }
  } else {
    $("#proj-name").textContent = "未选择项目";
  }
  $("#proj-dir").textContent = dir || "（左侧选一个运行，或粘贴路径打开）";
  $$(".run-item").forEach((it) => it.classList.toggle("active", it.dataset.dir === dir));
  // 立即拉取所有
  pullJobs(); pullState(); pullLog();
  ["budget", "compliance", "verify", "similarity", "review", "step2", "charts", "loop", "gates"].forEach(pullPanel);
}

// ─── rail：运行列表 ──────────────────────────────────────────────────────
async function pullJobs() {
  let jobs = [];
  try { jobs = await getJSON("/jobs"); } catch (e) { setTimeout(pullJobs, 5000); return; }
  const ul = $("#run-list");
  if (!jobs.length) { ul.innerHTML = `<li class="empty">(暂无运行)</li>`; setTimeout(pullJobs, 10000); return; }
  ul.innerHTML = jobs.map((j) => {
    const st = j.finished ? "ok" : j.alive ? "running" : "fail";
    const color = st === "ok" ? "var(--ok)" : st === "running" ? "var(--running)" : "var(--fail)";
    return `<li class="run-item ${j.tender_dir === currentDir ? "active" : ""}" data-dir="${esc(j.tender_dir || "")}">
      <span class="ri-dot" style="background:${color}"></span>
      <span class="ri-name">${esc(j.tender_name || "(无名)")}</span>
      <span class="muted">${j.progress != null ? j.progress + "/" + (j.total || 18) : ""}</span></li>`;
  }).join("");
  $$(".run-item").forEach((it) => it.addEventListener("click", () => loadProject(it.dataset.dir)));
  setTimeout(pullJobs, 10000);
}

// ─── 顶部状态 + 步骤清单（/state） ───────────────────────────────────────
async function pullState() {
  if (!currentDir) { setTimeout(pullState, 3000); return; }
  let d; try { d = await getJSON(api("state")); } catch (e) { setTimeout(pullState, 3000); return; }
  lastState = d; renderState(d); renderProblems(); setTimeout(pullState, 2000);
}

function renderState(d) {
  const steps = d.steps || [];
  const running = steps.some((s) => s.status === "running");
  const b = $("#alive-badge");
  if (running) { b.className = "badge badge-running"; b.textContent = "运行中"; }
  else if (d.summary && d.summary.done) { b.className = "badge badge-ok"; b.textContent = "已完成 ✓"; }
  else if (d.summary && d.summary.first_failed) { b.className = "badge badge-fail"; b.textContent = "有失败 ✗"; }
  else { b.className = "badge badge-idle"; b.textContent = "空闲"; }
  $("#runtime").textContent = d.runtime != null ? `⏱ ${Math.floor(d.runtime / 60)}m${Math.round(d.runtime % 60)}s` : "";
  $("#btn-start").disabled = running || !currentDir;
  $("#btn-stop").disabled = !running;
  $("#step-progress").textContent = `进度 ${d.summary.progress}/${d.summary.total}` +
    (d.summary.first_failed ? ` · 首个失败 ${d.summary.first_failed}` : "");
  $("#step-list").innerHTML = steps.map((s) => {
    const icon = s.status === "ok" ? "✓" : s.status === "fail" ? "✗"
      : s.status === "running" ? `<span class="spin">⟳</span>` : "·";
    const meta = [s.seconds != null ? `${s.seconds}s` : "", s.exit_code != null ? `exit=${s.exit_code}` : ""]
      .filter(Boolean).join(" · ");
    return `<li class="step st-${esc(s.status)}">
      <div class="step-row" data-step="${esc(s.name)}"><span class="step-icon">${icon}</span>
        <span class="step-name">${esc(s.name)}</span><span class="step-meta">${esc(meta || s.status)}</span></div>
      ${s.error ? `<div class="step-error">⚠ ${esc(s.error)}</div>` : ""}
      <div class="step-log" hidden><pre>加载中…</pre></div></li>`;
  }).join("");
  $$(".step-row").forEach((row) => row.addEventListener("click", () => toggleStepLog(row)));
}

async function toggleStepLog(row) {
  const box = row.closest(".step").querySelector(".step-log");
  box.hidden = !box.hidden;
  if (!box.hidden && !box.dataset.loaded) {
    try { const j = await getJSON(api("step_log", "&step=" + encodeURIComponent(row.dataset.step)));
      box.querySelector("pre").textContent = j.tail || "(该步无独立日志，看上方全局日志)"; }
    catch (e) { box.querySelector("pre").textContent = "(读取失败)"; }
    box.dataset.loaded = "1";
  }
}

// ─── 流式日志终端（/log） ────────────────────────────────────────────────
async function pullLog() {
  if (!currentDir) { $("#log-term").textContent = ""; setTimeout(pullLog, 3000); return; }
  let d; try { d = await getJSON(api("log")); } catch (e) { setTimeout(pullLog, 3000); return; }
  const term = $("#log-term");
  const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 80;
  term.textContent = d.tail || "(暂无日志)";
  $("#log-path").textContent = d.path ? `← ${d.path}` : "";
  if (atBottom) term.scrollTop = term.scrollHeight;
  setTimeout(pullLog, 2000);
}

// ─── 问题标注（聚合 state/compliance/verify/similarity） ─────────────────
function renderProblems() {
  const ps = [];
  if (lastState) (lastState.steps || []).filter((s) => s.status === "fail")
    .forEach((s) => ps.push({ sev: "error", t: `步骤 ${s.name} 失败`, d: s.error || "" }));
  if (lastComp) (lastComp.variants || []).filter((v) => v.risk_level === "red")
    .forEach((v) => ps.push({ sev: "error", t: `合规 ${v.variant} 高风险`,
      d: v.counts ? Object.entries(v.counts).filter(([, n]) => n).map(([k, n]) => `${k}=${n}`).join(" · ") : "" }));
  if (lastVerify) (lastVerify.findings || []).filter((f) => f.severity === "fail")
    .forEach((f) => ps.push({ sev: "error", t: `验证 ${f.check} (${f.variant || ""})`, d: f.detail || "" }));
  if (lastSim) (lastSim.over || [])
    .forEach((p) => ps.push({ sev: "warn", t: `相似度 ${p.a}↔${p.b} 超阈`,
      d: `整体 ${(p.overall * 100).toFixed(0)}% · 段落 ${(p.paragraph_max * 100).toFixed(0)}%` }));
  const box = $("#problems");
  if (!ps.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="annot error"><span class="a-title">⚠ 发现 ${ps.length} 个问题</span></div>` +
    ps.slice(0, 15).map((p) => `<div class="annot ${p.sev}"><span class="a-title">${esc(p.t)}</span><span class="a-det">${esc((p.d || "").slice(0, 140))}</span></div>`).join("");
}

// ─── 结果面板（轮询 + 渲染） ─────────────────────────────────────────────
async function pullPanel(name) {
  if (!currentDir) { setTimeout(() => pullPanel(name), 5000); return; }
  let d; try { d = await getJSON(api(name)); } catch (e) { setTimeout(() => pullPanel(name), 8000); return; }
  if (name === "compliance") lastComp = d;
  if (name === "verify") lastVerify = d;
  if (name === "similarity") lastSim = d;
  try { RENDERS[name](d); } catch (e) { console.error("render", name, e); }
  renderProblems();
  setTimeout(() => pullPanel(name), 15000);
}
function bodyOf(name) { const el = $(`[data-panel="${name}"]`); return el || null; }

const RENDERS = {};
RENDERS.budget = (d) => {
  const pol = d.policy || {}, u = d.used || {};
  const bar = (label, used, cap) => {
    if (cap == null) return `<div class="muted">${esc(label)}: ${fmtInt(used)}</div>`;
    const pct = Math.min(100, (used / cap) * 100);
    return `<div>${esc(label)}: ${fmtInt(used)} / ${fmtInt(cap)} (${pct.toFixed(0)}%)<div class="bar"><span style="width:${pct}%;background:${pct > 95 ? "var(--fail)" : pct > 80 ? "var(--warn)" : "var(--ok)"}"></span></div></div>`;
  };
  bodyOf("budget").innerHTML = bar("Tokens", u.tokens, pol.max_tokens_per_run) + bar("费用 ¥", u.cost_yuan, pol.max_cost_yuan) +
    bar("耗时 min", u.wall_minutes, pol.max_wall_clock_minutes) +
    (d.over && d.over.length ? `<p class="fail">⚠ ${d.over.map(esc).join("；")}</p>` : `<p class="ok">✓ 未超预算</p>`);
};
RENDERS.compliance = (d) => {
  const vs = d.variants || [];
  bodyOf("compliance").innerHTML = `<table class="mini-table"><tr><th>variant</th><th>风险</th><th>违规计数</th></tr>` +
    vs.map((v) => `<tr><td><b>${esc(v.variant)}</b></td><td><span class="risk risk-${esc(v.risk_level)}">${esc(v.risk_level)}</span></td>` +
      `<td class="muted">${v.counts ? Object.entries(v.counts).filter(([, n]) => n).map(([k, n]) => `${k}=${n}`).join(" · ") || "全清" : "-"}</td></tr>` +
      (v.issues && v.issues.length ? `<tr><td colspan="3">${v.issues.slice(0, 8).map((i) => `<div class="issue">[${esc(i.class)}] ${esc(i.detail)}</div>`).join("")}</td></tr>` : "")).join("") + `</table>`;
};
RENDERS.verify = (d) => {
  if (!d || !d.findings) { bodyOf("verify").innerHTML = `<p class="empty">未运行 verify</p>`; return; }
  bodyOf("verify").innerHTML = `<p>状态: <b class="${d.status === "fail" ? "fail" : "ok"}">${esc(d.status || "-")}</b> · fail ${d.n_fail || 0} / warn ${d.n_warn || 0} · 页数硬顶 ${esc(d.page_cap)}</p>` +
    `<table class="mini-table"><tr><th>严重度</th><th>检查</th><th>variant</th><th>详情</th></tr>` +
    (d.findings || []).slice(0, 30).map((x) => `<tr><td class="${x.severity === "fail" ? "fail" : ""}">${esc(x.severity)}</td><td>${esc(x.check)}</td><td>${esc(x.variant || "")}</td><td class="muted">${esc((x.detail || "").slice(0, 80))}</td></tr>`).join("") + `</table>`;
};
RENDERS.similarity = (d) => {
  const over = d.over || [], pairs = d.pairs || [], th = d.threshold || {};
  let html = `<p>阈值 整体>${th.overall} / 段落>${th.paragraph} · 超阈 <b class="${over.length ? "fail" : "ok"}">${over.length}</b></p>`;
  if (pairs.length) html += `<table class="mini-table pairs"><tr><th>对</th><th>整体</th><th>段落</th><th>最差章</th></tr>` +
    pairs.map((p) => { const bad = p.overall > th.overall || p.paragraph_max > th.paragraph;
      return `<tr><td>${esc(p.a)}↔${esc(p.b)}</td><td class="${bad ? "over" : ""}">${(p.overall * 100).toFixed(0)}%</td><td class="${bad ? "over" : ""}">${(p.paragraph_max * 100).toFixed(0)}%</td><td class="muted">${esc(p.worst_chapter || "")}</td></tr>`; }).join("") + `</table>`;
  else html += `<p class="empty">无相似度数据</p>`;
  bodyOf("similarity").innerHTML = html;
};
RENDERS.review = (d) => {
  const vs = d.variants || [];
  if (!vs.length) { bodyOf("review").innerHTML = `<p class="empty">无审校数据</p>`; return; }
  bodyOf("review").innerHTML = vs.map((v) => `<p><b>${esc(v.id)}</b> 总体: <b class="${v.overall_severity === "major" ? "fail" : "ok"}">${esc(v.overall_severity || "-")}</b></p>` +
    (v.per_chapter && v.per_chapter.length ? `<table class="mini-table"><tr><th>章</th><th>严重性</th></tr>${v.per_chapter.slice(0, 8).map((c) => `<tr><td>${esc(c.chapter || c.num || "")}</td><td>${esc(c.severity || "")}</td></tr>`).join("")}</table>` : "")).join("");
};
RENDERS.step2 = (d) => {
  const sk = d.skeleton || {};
  const order = ["project_brain", "chapter_plan", "disqualification_rules", "evaluation_method", "mandatory_outline", "disqualification"];
  const label = { project_brain: "brain", chapter_plan: "plan", disqualification_rules: "rules", evaluation_method: "eval_method", mandatory_outline: "outline", disqualification: "disqual" };
  bodyOf("step2").innerHTML = `<div class="chips">${order.map((k) => { const it = sk[k] || {};
    return `<span class="chip ${it.exists ? "on" : "off"}" data-path="${esc(it.path || "")}" data-exists="${it.exists}">${it.exists ? "✓" : "✗"} ${esc(label[k] || k)}</span>`; }).join("")}</div>` +
    (d.warnings || []).map((w) => `<div class="fail">⚠ ${esc(w)}</div>`).join("");
  $$("#step2 .chip, [data-panel='step2'] .chip").forEach((c) => c.addEventListener("click", () => { if (c.dataset.exists === "true") previewFile(c.dataset.path); }));
};
RENDERS.charts = (d) => {
  const items = d.rendered || [];
  if (!items.length) { bodyOf("charts").innerHTML = `<p class="empty">无图表</p>`; return; }
  bodyOf("charts").innerHTML = `<div class="gallery">${items.map((c) => `<img src="${esc(c.png_url)}" title="${esc(c.variant || "")} ${esc(c.name)}" loading="lazy">`).join("")}</div>`;
};
RENDERS.loop = (d) => {
  const cyc = d.cycles || [], fails = d.failures || [];
  let html = `<p>last_result: <b>${esc(d.last_result || "-")}</b> · 循环 ${cyc.length} 次</p>`;
  if (cyc.length) html += `<table class="mini-table"><tr><th>#</th><th>exit</th><th>耗时</th><th>at</th></tr>` +
    cyc.slice(-8).reverse().map((c) => `<tr><td>${c.cycle}</td><td class="${c.exit_code === 0 ? "ok" : "fail"}">${c.exit_code}</td><td>${c.seconds}s</td><td class="muted">${esc(c.at || "")}</td></tr>`).join("") + `</table>`;
  html += fails.length ? `<h3>失败台账(近 ${fails.length})</h3>` + fails.slice(-8).reverse().map((f) => `<div class="issue"><b>${esc(f.failure_class)}</b> · ${esc(f.step)} ×${f.count} <span class="muted">${esc((f.detail || "").slice(0, 90))}</span></div>`).join("") : `<p class="empty">无失败记录</p>`;
  bodyOf("loop").innerHTML = html;
};
RENDERS.gates = (d) => {
  const gates = (d.gates || []).filter((g) => (g.decisions || []).length);
  bodyOf("gates").innerHTML = gates.length ? gates.map((g) => `<details><summary><b>${esc(g.step)}</b> · ${g.decisions.length} 次</summary>${g.decisions.map((dec) => `<div class="issue">${esc(JSON.stringify(dec))}</div>`).join("")}</details>`).join("") : `<p class="empty">无门禁自动修复记录</p>`;
};

// ─── 控制 + 模态 + rail 交互 ─────────────────────────────────────────────
async function control(action, extra = {}) {
  if (!currentDir) { alert("请先在左侧选择一个项目"); return; }
  const r = await fetch(api("control"), { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_dir: currentDir, action, ...extra }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { alert("控制失败: " + (j.error || r.status)); return; }
  pullState();
}
async function previewFile(rel) {
  $("#modal").hidden = false; $("#modal-title").textContent = rel; $("#modal-body").textContent = "加载中…";
  const r = await fetch(api("file", "&path=" + encodeURIComponent(rel)));
  const j = await r.json().catch(() => ({}));
  $("#modal-body").textContent = r.ok ? (j.content || "(空)") : ("错误 " + r.status + ": " + (j.error || ""));
}

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-start").onclick = () => control("start", { resume: true });
  $("#btn-stop").onclick = () => control("stop");
  $("#btn-refresh").onclick = () => { pullJobs(); pullState(); pullLog(); Object.keys(RENDERS).forEach(pullPanel); };
  $("#modal-close").onclick = () => ($("#modal").hidden = true);
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };
  $("#toggle-upload").onclick = () => { const f = $("#upload-form"); f.hidden = !f.hidden; };
  $("#open-form").onsubmit = async (e) => {
    e.preventDefault();
    const dir = $("#path-input").value.trim();
    const err = $("#open-err");
    err.hidden = true;
    if (!dir) return;
    try { const r = await fetch(`/api/dashboard/inspect?project_dir=${encodeURIComponent(dir)}`);
      if (r.ok) loadProject(dir); else { err.textContent = "目录无效或不存在"; err.hidden = false; } }
    catch (x) { err.textContent = "请求失败"; err.hidden = false; }
  };
  loadProject(currentDir);
});
