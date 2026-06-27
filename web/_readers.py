#!/usr/bin/env python3
"""web/_readers.py — dashboard 只读数据聚合（纯函数，不含 Flask）。

集中实现所有 /api/dashboard/* 端点背后的数据读取与聚合逻辑：
  - 路径围栏 resolve_within / 文本安全读 read_text_safe
  - json / jsonl 加载器
  - 进度态 read_state_summary（18 步，STEPS 取自 bid_run 单一真值）
  - Step2 三件套 read_step2
  - 合规聚合 aggregate_compliance（复用 bid_run._read_compliance_scans 的 glob，
    风险分级按 tests/test_web_dashboard.py 契约独立实现）
  - 预算 compute_budget（内联复制 bid_run._check_budget 的成本数学）
  - 循环 / 门禁 / 相似度 / 验证 / 图表 / 审校 聚合

不重构 scripts/ 管道脚本；bid_run 仅作只读常量与加载器导入（其顶层无副作用）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 把 scripts/ 加入 sys.path，以便复用 bid_run 的只读常量/加载器（一次性、幂等）
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# bid_run 顶层仅有 if __name__=="__main__" 守卫，import 安全
from bid_run import STEPS, _read_compliance_scans, _read_verify_report  # noqa: E402

BID_GEN_HOME = Path(os.environ.get("BID_GEN_HOME", str(ROOT)))
CONFIGS = BID_GEN_HOME / "configs"

# 风险分级依据（与 tests/test_web_dashboard.py 契约一致，单一真值）
_TEXT_SUFFIXES = {".md", ".json", ".txt", ".log", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".py"}
_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".json"}
_MAX_TEXT_BYTES = 256 * 1024  # /file 单文件 256 KiB 截断

_SIM_DEFAULT_TH = {"overall": 0.40, "paragraph": 0.50}


# ─── 基础加载器 ──────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    """json 加载；缺失或解析失败返回 {}。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def detect_mode(tender_dir: Path) -> str:
    """batch / single——以 extracted/variants.json 是否存在为准（与 bid_status 一致）。"""
    return "batch" if (tender_dir / "extracted" / "variants.json").exists() else "single"


# ─── 路径围栏 ────────────────────────────────────────────────────────────

def resolve_within(project_dir: str, target: str) -> Path | None:
    """把 target 解析到 project_dir 内（realpath 围栏，跟随符号链接）。

    返回绝对 realpath；越界 / base 不存在 → None。调用方据此返回 403/404。
    """
    base = Path(project_dir).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        return None
    t = Path(target).expanduser()
    t = t if t.is_absolute() else (base / t)
    try:
        real = t.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if real != base and base not in real.parents:
        return None  # 越界（含符号链接逃逸）
    return real


def read_text_safe(project_dir: str, target: str) -> tuple[int, dict]:
    """/file 的安全读取。返回 (http_status, body)。

    流程：① .env 名义拒绝 ② 围栏 ③ 存在性 ④ 非目录 ⑤ 文本白名单 ⑥ 截断读取。
    """
    p = Path(target)
    if p.name == ".env" or p.name.endswith(".env"):
        return 403, {"error": ".env forbidden"}
    real = resolve_within(project_dir, target)
    if real is None:
        # base 缺失归 404，否则越界归 403
        base_ok = Path(project_dir).expanduser().resolve().is_dir()
        return (404 if not base_ok else 403), {"error": "outside project"}
    if not real.exists():
        return 404, {"error": "not found"}
    if real.is_dir():
        return 400, {"error": "is a directory"}
    if real.suffix.lower() not in _TEXT_SUFFIXES:
        return 415, {"error": f"unsupported suffix {real.suffix}"}
    try:
        raw = real.read_bytes()
    except OSError:
        return 404, {"error": "unreadable"}
    if b"\x00" in raw[:4096]:  # 二进制兜底
        return 415, {"error": "binary content"}
    text = raw.decode("utf-8", errors="replace")
    truncated = False
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        text = text[:_MAX_TEXT_BYTES]
        truncated = True
    return 200, {"content": text, "truncated": truncated, "path": str(real)}


def read_asset_safe(project_dir: str, target: str) -> Path | None:
    """/asset 的围栏校验：返回可 send_file 的绝对路径，或 None（→403/415）。"""
    p = Path(target)
    if p.name == ".env" or p.name.endswith(".env"):
        return None
    real = resolve_within(project_dir, target)
    if real is None or not real.exists() or real.is_dir():
        return None
    if real.suffix.lower() not in _ASSET_SUFFIXES:
        return None
    return real


# ─── 进度态 /state ───────────────────────────────────────────────────────

def _iso_to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _runtime_seconds(steps: dict) -> float | None:
    """最早 started_at → 最晚 finished_at（秒）；无时间戳返回 None。"""
    starts, ends = [], []
    for info in steps.values():
        s, e = _iso_to_dt(info.get("started_at")), _iso_to_dt(info.get("finished_at"))
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts:
        return None
    end = max(ends) if ends else datetime.now()
    return max(0.0, (end - min(starts)).total_seconds())


def read_state_summary(tender_dir: Path) -> dict:
    """{steps:[{name,status,...}], runtime, summary:{progress,total,first_failed},
    interventions:[{from_step,...}]}。无 state 文件 → 全 not_started + progress 0。"""
    state = load_json(tender_dir / "output" / ".bid_run.state.json")
    steps_map = state.get("steps", {}) if isinstance(state, dict) else {}
    out_steps = []
    progress = 0
    first_failed = None
    for name in STEPS:
        info = steps_map.get(name, {}) or {}
        status = info.get("status") or "not_started"
        out_steps.append({
            "name": name,
            "status": status,
            "exit_code": info.get("exit_code"),
            "seconds": info.get("seconds"),
            "error": info.get("error"),
            "note": info.get("note"),
            "started_at": info.get("started_at"),
            "finished_at": info.get("finished_at"),
        })
        if status in ("ok", "skip"):
            progress += 1
        elif status == "fail" and first_failed is None:
            first_failed = name

    interventions = []
    if first_failed:
        interventions.append({
            "from_step": first_failed,
            "to_step": STEPS[-1],
            "suggested_cmd": f"bid_run.py <dir> --resume --from {first_failed}",
        })
    else:
        # 永远非空（契约要求）：无失败时建议从头或确认完成
        interventions.append({
            "from_step": "extract" if progress == 0 else STEPS[-1],
            "to_step": STEPS[-1],
            "suggested_cmd": "bid_run.py <dir> --resume" if progress else "bid_run.py <dir>",
        })

    runtime = _runtime_seconds(steps_map)
    return {
        "steps": out_steps,
        "runtime": runtime,
        "summary": {
            "progress": progress,
            "total": len(STEPS),
            "first_failed": first_failed,
            "done": progress >= len(STEPS),
        },
        "interventions": interventions,
    }


# ─── Step2 三件套 /step2 ────────────────────────────────────────────────

_STEP2_FILES = {
    "project_brain": "extracted/project_brain.md",
    "chapter_plan": "extracted/chapter_plan.md",
    "disqualification_rules": "extracted/disqualification_rules.md",
    "evaluation_method": "extracted/evaluation_method.json",
    "mandatory_outline": "extracted/mandatory_outline.json",
    "disqualification": "extracted/disqualification.json",
}


def read_step2(tender_dir: Path) -> dict:
    skeleton: dict[str, dict] = {}
    warnings: list[str] = []
    for key, rel in _STEP2_FILES.items():
        p = tender_dir / rel
        exists = p.exists()
        skeleton[key] = {
            "exists": exists,
            "size": p.stat().st_size if exists else 0,
            "path": rel,
        }
        if not exists:
            warnings.append(f"缺失 {rel}")
    return {"skeleton": skeleton, "warnings": warnings}


# ─── 合规 /compliance ───────────────────────────────────────────────────

def _counts(scan: dict) -> dict:
    blind = scan.get("blind_violations") or []
    resp_missing = [r for r in (scan.get("response_check") or []) if r.get("required") and not r.get("present")]
    outline_missing = ((scan.get("outline_coverage") or {}).get("missing")) or []
    chart_missing = ((scan.get("chart_coverage") or {}).get("missing")) or []
    fmt_viol = ((scan.get("format_audit") or {}).get("violations")) or []
    fabricated = int((scan.get("standards_check") or {}).get("fabricated", 0) or 0)
    return {
        "blind_violations": len(blind),
        "response_missing": len(resp_missing),
        "outline_missing": len(outline_missing),
        "chart_missing": len(chart_missing),
        "format_violations": len(fmt_viol),
        "standard_fabrications": fabricated,
    }


def _risk_level(counts: dict) -> str:
    """红=任一硬风险；绿=否则。黄仅用于'报告缺失'（见 aggregate_compliance）。"""
    if (counts["blind_violations"] or counts["response_missing"] or counts["outline_missing"]
            or counts["chart_missing"] or counts["format_violations"]
            or counts["standard_fabrications"]):
        return "red"
    return "green"


def aggregate_compliance(tender_dir: Path) -> dict:
    """复用 bid_run._read_compliance_scans 做 glob（4 模式防御）；风险按契约分级。"""
    output = tender_dir / "output"
    scans = _read_compliance_scans(output, detect_mode(tender_dir))
    if not scans:
        # 契约：报告缺失 → 合成单条黄色
        zero = {k: 0 for k in ("blind_violations", "response_missing", "outline_missing",
                                "chart_missing", "format_violations", "standard_fabrications")}
        return {"variants": [{"variant": "单份", "risk_level": "yellow", "counts": zero, "issues": []}]}
    variants = []
    for vid, scan in scans.items():
        counts = _counts(scan)
        variants.append({
            "variant": vid,
            "risk_level": _risk_level(counts),
            "counts": counts,
            "issues": _classify_issues(scan),
        })
    variants.sort(key=lambda v: (0, v["variant"]) if v["variant"] == "单份" else (1, v["variant"]))
    return {"variants": variants}


def _classify_issues(scan: dict) -> list[dict]:
    """把 scan 关键违规点提炼成 UI 可展开的 issue 列表（人读）。"""
    issues = []
    for b in scan.get("blind_violations") or []:
        issues.append({"class": "blind_violation", "detail": str(b)[:120]})
    for r in scan.get("response_check") or []:
        if r.get("required") and not r.get("present"):
            issues.append({"class": "response_keyword_miss",
                           "detail": f"{r.get('label','')}:{r.get('keyword','')}"})
    for m in ((scan.get("outline_coverage") or {}).get("missing")) or []:
        issues.append({"class": "outline_missing", "detail": str(m)[:120]})
    for m in ((scan.get("chart_coverage") or {}).get("missing")) or []:
        issues.append({"class": "chart_missing", "detail": str(m)[:120]})
    for v in ((scan.get("format_audit") or {}).get("violations")) or []:
        issues.append({"class": "format_violation", "detail": str(v)[:120]})
    if (scan.get("standards_check") or {}).get("fabricated", 0):
        issues.append({"class": "standards_fabricated",
                       "detail": f"杜撰标准 {scan['standards_check']['fabricated']} 处"})
    return issues[:50]


# ─── 预算 /budget（内联复制 bid_run._check_budget 成本数学）─────────────

def _match_price(model: str, pricing: dict) -> dict:
    default_pr = pricing.get("default") or {"in": 4.0, "out": 12.0}
    return next((v for k, v in pricing.items() if k != "default" and k in model), default_pr)


def compute_budget(tender_dir: Path) -> dict:
    output = tender_dir / "output"
    policy_all = load_json(CONFIGS / "bid_loop_policy.json")
    bp = policy_all.get("budget_policy") or {}
    pricing = bp.get("pricing_yuan_per_mtok") or {}
    total_tokens = 0
    total_cost = 0.0
    by_bucket: dict[str, dict] = {}
    data = load_json(output / "logs" / "llm_stats.json")
    for bname, s in (data.get("buckets") or {}).items():
        bk_tokens = 0
        bk_cost = 0.0
        for model, d in (s.get("by_model") or {}).items():
            pi, po = int(d.get("prompt", 0)), int(d.get("completion", 0))
            pr = _match_price(model, pricing)
            c = pi / 1e6 * float(pr.get("in", 4.0)) + po / 1e6 * float(pr.get("out", 12.0))
            total_tokens += pi + po
            total_cost += c
            bk_tokens += pi + po
            bk_cost += c
        by_bucket[bname] = {
            "tokens": bk_tokens, "cost_yuan": round(bk_cost, 3),
            "calls": int(s.get("total_calls", 0)), "seconds": float(s.get("total_seconds", 0) or 0),
        }

    state = load_json(output / ".bid_run.state.json")
    wall_sec = _runtime_seconds(state.get("steps", {})) or 0.0
    wall_min = wall_sec / 60.0

    over = []
    if bp.get("max_tokens_per_run") and total_tokens > int(bp["max_tokens_per_run"]):
        over.append(f"tokens {total_tokens:,} > {int(bp['max_tokens_per_run']):,}")
    if bp.get("max_cost_yuan") and total_cost > float(bp["max_cost_yuan"]):
        over.append(f"费用 ¥{total_cost:.1f} > ¥{bp['max_cost_yuan']}")
    if bp.get("max_wall_clock_minutes") and wall_min > float(bp["max_wall_clock_minutes"]):
        over.append(f"耗时 {wall_min:.0f}min > {bp['max_wall_clock_minutes']}min")

    return {
        "policy": {
            "max_tokens_per_run": bp.get("max_tokens_per_run"),
            "max_cost_yuan": bp.get("max_cost_yuan"),
            "max_wall_clock_minutes": bp.get("max_wall_clock_minutes"),
            "action_on_exceed": bp.get("action_on_exceed", "abort"),
        },
        "used": {
            "tokens": total_tokens,
            "cost_yuan": round(total_cost, 3),
            "wall_minutes": round(wall_min, 1),
            "by_bucket": by_bucket,
        },
        "over": over,
    }


# ─── 循环 /loop ──────────────────────────────────────────────────────────

def read_loop(tender_dir: Path) -> dict:
    output = tender_dir / "output"
    state = load_json(output / ".bid_loop.state.json")
    failures = load_jsonl(output / "logs" / "loop_failures.jsonl")
    # 近 20 条，按 key 去重保留最后
    seen: dict[str, dict] = {}
    for rec in failures:
        seen[rec.get("key", "")] = rec
    recent = list(seen.values())[-20:]
    return {
        "loop_id": state.get("loop_id"),
        "last_result": state.get("last_result"),
        "cycles": state.get("cycles", []),
        "failures": recent,
    }


# ─── 门禁 /gates ─────────────────────────────────────────────────────────

def read_gates(tender_dir: Path) -> dict:
    state = load_json(tender_dir / "output" / ".bid_run.state.json")
    steps = state.get("steps", {}) if isinstance(state, dict) else {}
    gates = []
    for step in ("review", "similarity", "verify", "compliance", "assemble"):
        info = steps.get(step, {}) or {}
        decisions = info.get("gate_decisions") or []
        if decisions or info.get("status"):
            gates.append({"step": step, "status": info.get("status"), "decisions": decisions})
    return {"gates": gates}


# ─── 相似度 /similarity ─────────────────────────────────────────────────

def read_similarity(tender_dir: Path) -> dict:
    output = tender_dir / "output"
    mat = load_json(output / "similarity_matrix.json")
    th = dict(_SIM_DEFAULT_TH)
    vj = load_json(tender_dir / "extracted" / "variants.json")
    th.update(vj.get("similarity_threshold") or {})
    pairs = mat.get("pairs", []) if mat else []
    over = [p for p in pairs
            if p.get("overall", 0) > th["overall"] or p.get("paragraph_max", 0) > th["paragraph"]]
    return {
        "threshold": th,
        "pairs": pairs,
        "over": over,
        "ids": mat.get("ids", []) if mat else [],
    }


# ─── 验证 /verify ────────────────────────────────────────────────────────

def read_verify(tender_dir: Path) -> dict:
    return _read_verify_report(tender_dir / "output") or {}


# ─── 审校 /review ────────────────────────────────────────────────────────

def read_review(tender_dir: Path) -> dict:
    output = tender_dir / "output"
    summary = load_json(output / "review_summary.json")
    variants = []
    # 单份 review.json 可能在 output/ 根；批量在 output/V*/review.json
    candidates = sorted(output.glob("V*/review.json"))
    if not candidates:
        single = output / "review.json"
        if single.exists():
            candidates = [single]
    for rp in candidates:
        d = load_json(rp)
        if not d:
            continue
        vid = rp.parent.name if rp.parent.name.startswith("V") else "单份"
        variants.append({
            "id": vid,
            "overall_severity": d.get("overall_severity"),
            "per_chapter": d.get("chapters") or d.get("per_chapter") or [],
        })
    return {"summary": summary, "variants": variants}


# ─── 图表 /charts ────────────────────────────────────────────────────────

def read_charts(tender_dir: Path) -> dict:
    output = tender_dir / "output"
    specs = load_json(tender_dir / "extracted" / "chart_specs.json")
    rendered = []
    charts_dir = output / "charts"
    if charts_dir.is_dir():
        for png in sorted(charts_dir.glob("*.png")):
            rendered.append({
                "name": png.name,
                "rel": str(png.relative_to(tender_dir)),
                "png_url": f"/api/dashboard/asset?project_dir={tender_dir}&path={png}",
            })
    # per-variant charts (output/V*/charts/*.png)
    for vdir in sorted(output.glob("V*/charts/*.png")):
        rendered.append({
            "variant": vdir.parent.parent.name,
            "name": vdir.name,
            "rel": str(vdir.relative_to(tender_dir)),
            "png_url": f"/api/dashboard/asset?project_dir={tender_dir}&path={vdir}",
        })
    return {"specs": specs.get("specs", []) if specs else [], "rendered": rendered}


def resolve_tender(project_dir: str) -> Path | None:
    """规范化 project_dir 并校验为目录；无效返回 None。"""
    p = Path(project_dir).expanduser()
    try:
        p = p.resolve(strict=True)
    except OSError:
        return None
    return p if p.is_dir() else None


# ─── 实时日志 /log + /step_log ──────────────────────────────────────────

def _tail(path: Path, max_chars: int = 8000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_chars * 3:  # 粗略按字节跳到尾部
        data = data[-(max_chars * 3):]
    return data.decode("utf-8", errors="ignore")[-max_chars:]


def read_log_tail(tender_dir: Path) -> dict:
    """全局运行日志尾部（优先 output/logs/bid.log，回退 run.log）。"""
    output = tender_dir / "output"
    for rel in ("logs/bid.log", "run.log"):
        p = output / rel
        if p.exists():
            return {"path": rel, "tail": _tail(p)}
    return {"path": None, "tail": ""}


def read_step_log(tender_dir: Path, step: str) -> dict:
    """某一步的子进程日志尾部：glob output/logs/*{step}*.log 取最新。"""
    if not step or not re.fullmatch(r"[A-Za-z0-9_]+", step):
        return {"step": step, "path": None, "tail": ""}
    logs_dir = tender_dir / "output" / "logs"
    candidates = sorted(logs_dir.glob(f"*{step}*.log"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if logs_dir.is_dir() else []
    if not candidates:
        return {"step": step, "path": None, "tail": ""}
    top = candidates[0]
    return {"step": step, "path": str(top.relative_to(tender_dir)), "tail": _tail(top)}
