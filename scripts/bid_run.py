#!/usr/bin/env python3
"""
bid_run.py - /bid-gen 主编排器

一键驱动 Step 1 → Step 6 的完整链路，支持：
  - 自动判断单份 / 批量模式（看 extracted/variants.json 是否存在）
  - Checkpoint（output/.bid_run.state.json），失败可 --resume
  - --from STEP / --to STEP 指定执行区间
  - --dry-run 只打印计划
  - --doctor 运行配置健康检查
  - --force-continue 忽略质量门禁
  - 质量门禁：review 出现 major → 自动重试 2 次；similarity 超阈值 → 自动重写

STEP 名:
  extract fingerprint variants write review assemble compliance similarity summary

Step 2（brain/plan/rules）由 Claude 在对话里完成，bid_run 检测到缺失会停下。

用法:
  python bid_run.py <tender_dir>                      # 自动全流程
  python bid_run.py <tender_dir> --resume             # 从上次失败处继续
  python bid_run.py <tender_dir> --from review        # 从指定步开始
  python bid_run.py <tender_dir> --dry-run
  python bid_run.py --doctor [<tender_dir>]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import setup_logger as _setup_logger  # noqa: E402
from bid_common import project_fingerprint as _project_fingerprint  # noqa: E402


STEPS = [
    "extract",       # bid_extract.py
    "template",      # bid_template_match.py (Tier 0 范本指纹比对)
    "facts",         # bid_facts_extract.py (Tier 1 13 字段正则抽)
    "derive",        # bid_derive.py (Tier 2 派生:海拔/季节/规模)
    "skeleton",      # bid_skeleton_extract.py (v2 骨架:3 份 JSON)
    "special",       # bid_special_extract.py (Tier 4 项目特殊条款)
    "input_assemble",# bid_input_assemble.py (合并 → project_input.json,与 docx assemble 区分)
    "fingerprint",   # bid_fingerprint.py (可选)
    "variants",      # bid_variants.py (批量专用)
    "plan_variants", # bid_plan_variants.py (每变体独立标题计划+图表分配)
    "chart_specs",   # bid_chart_specs.py (从 chapter_plan 派生 chart_specs.json)
    "write",         # bid_writer.py
    "review",        # bid_reviewer.py
    "assemble",      # bid_assemble.py (--batch 批量 / 单份)
    "verify",        # bid_verify.py (组装产物验证门:封面/三家排版差异/页数硬顶)
    "compliance",    # bid_compliance.py (--all-variants 批量 / 单份)
    "similarity",    # bid_similarity.py (批量专用)
    "summary",       # 写项目总览.md + compliance_summary.md + review_summary.md
]


# ─── checkpoint ──────────────────────────────────────────────────────

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"steps": {}, "runs": []}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_step(state: dict, step: str, status: str, **extra) -> None:
    s = state["steps"].setdefault(step, {})
    s["status"] = status
    if status == "running":
        s["started_at"] = datetime.now().isoformat(timespec="seconds")
    if status in ("ok", "fail"):
        s["finished_at"] = datetime.now().isoformat(timespec="seconds")
    s.update(extra)


def repair_stale_running_steps(state: dict, max_age_hours: float = 2.0) -> list[str]:
    """把上次异常中断遗留的 running 标记修成 interrupted。"""
    repaired = []
    now = datetime.now()
    for step, info in state.get("steps", {}).items():
        if info.get("status") != "running":
            continue
        started = info.get("started_at")
        try:
            started_at = datetime.fromisoformat(started) if started else None
        except ValueError:
            started_at = None
        if not started_at or (now - started_at).total_seconds() >= max_age_hours * 3600:
            info["status"] = "interrupted"
            info["finished_at"] = now.isoformat(timespec="seconds")
            info["error"] = "stale running state repaired"
            repaired.append(step)
    return repaired


# ─── subprocess runner ─────────────────────────────────────────────────

def _resolve_log_dir(args: list[str]) -> Path | None:
    """从 args 中找到 tender_dir,返回 output/logs/ 路径(创建之)。"""
    for a in args:
        try:
            p = Path(a).expanduser()
            if p.is_dir():
                log_dir = p / "output" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                return log_dir
        except (ValueError, OSError):
            continue
    return None


def run_script(script: str, args: list[str], log) -> int:
    """运行子脚本并捕获 stdout/stderr 到 output/logs/{script}_{ts}.log。

    实时双写:子进程输出按行 tee 到终端 + 日志文件,失败时 stderr 末尾 30 行
    显式打印,方便 troubleshoot。
    """
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    log.info(f"$ {' '.join(cmd)}")

    log_dir = _resolve_log_dir(args)
    log_path: Path | None = None
    if log_dir:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"{script.replace('.py', '')}_{ts}.log"

    if log_path:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"# CMD: {' '.join(cmd)}\n")
            logf.write(f"# START: {datetime.now().isoformat(timespec='seconds')}\n\n")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                logf.write(line)
            proc.wait()
            logf.write(f"\n# END: {datetime.now().isoformat(timespec='seconds')} exit={proc.returncode}\n")
        if proc.returncode != 0:
            log.error(f"  ✗ exit={proc.returncode}, 完整日志: {log_path}")
            # 打印末尾 30 行用于快速定位
            try:
                tail = log_path.read_text(encoding="utf-8").splitlines()[-30:]
                log.error("  --- 末尾 30 行 ---")
                for line in tail:
                    log.error(f"    {line}")
                log.error("  -------------------")
            except OSError:
                pass
        return proc.returncode
    else:
        # 无 tender_dir 时回退到原行为(如 doctor 模式)
        proc = subprocess.run(cmd)
        return proc.returncode


# ─── 模式判定 ─────────────────────────────────────────────────────────

def detect_mode(tender_dir: Path) -> str:
    """返回 'batch' 或 'single'"""
    return "batch" if (tender_dir / "extracted" / "variants.json").exists() else "single"


def check_step2_ready(tender_dir: Path) -> tuple[bool, list[str]]:
    """Step 2 三份文件是否齐备 + 机器校验（总分/废标来源）。

    返回 (ok, issues)。issues 为空表示完全通过；
    非空但 ok=True 表示仅有警告（允许继续，列给用户看）；
    ok=False 表示硬性失败。
    """
    ext = tender_dir / "extracted"
    required = ["project_brain.md", "chapter_plan.md", "disqualification_rules.md"]
    missing = [f for f in required if not (ext / f).exists()]
    if missing:
        return (False, [f"缺失: {m}" for m in missing])

    try:
        from bid_plan_verify import verify as _verify
        code = _verify(tender_dir)
    except Exception as e:  # noqa: BLE001
        return (False, [f"bid_plan_verify 异常: {e}"])

    if code == 0:
        return (True, [])
    if code == 1:
        return (True, ["有警告（未标注来源的废标条款），详见终端输出"])
    return (False, ["硬性失败（总分不符或结构缺失），详见终端输出"])


_BAD_PROJECT_NAME_RE = re.compile(
    r"^(?:见)?(?:投标人须知前附表|须知前附表|前附表|项目名称|工程名称|本项目|本工程|/|无)$"
)
_STALE_ORG_WORDS = ("棚圈", "牧道", "桥涵", "暖棚")


def generation_input_gate(tender_dir: Path) -> tuple[bool, list[str]]:
    """生成前硬门禁：拦住会显著污染正文的前置数据问题。"""
    ext = tender_dir / "extracted"
    output = tender_dir / "output"
    issues: list[str] = []

    facts_path = ext / "project_facts.json"
    if facts_path.exists():
        try:
            facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
            facts = facts_payload.get("facts", facts_payload)
            project_name = str((facts.get("project_name") or {}).get("value") or "").strip()
            if _BAD_PROJECT_NAME_RE.search(project_name) or "见投标人须知" in project_name:
                issues.append("项目名称仍是模板占位值，请重跑 facts（建议加 --force）")
        except Exception as e:  # noqa: BLE001
            issues.append(f"project_facts.json 读取失败: {e}")
    else:
        issues.append("缺少 project_facts.json")

    has_boq_txt = any(ext.glob("*清单*.txt")) or any(ext.glob("*工程量*.txt")) or any(ext.glob("*BoQ*.txt"))
    boq_path = ext / "boq.json"
    if has_boq_txt:
        if not boq_path.exists():
            issues.append("存在清单文本但缺少 boq.json")
        else:
            try:
                boq = json.loads(boq_path.read_text(encoding="utf-8"))
                total_items = sum(len(c.get("items", []) or []) for c in boq.get("categories", []) or [])
                if total_items == 0:
                    issues.append("存在清单文本但 boq.json 未解析出工程量，需修复清单抽取后再生成")
            except Exception as e:  # noqa: BLE001
                issues.append(f"boq.json 读取失败: {e}")

    for assign_path in output.glob("V*/chart_assignment.json"):
        try:
            text = assign_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if any(w in text for w in _STALE_ORG_WORDS):
            issues.append(f"{assign_path.relative_to(tender_dir)} 存在非本项目作业队模板残留")

    return (not issues, issues)


# ─── 质量门禁 ─────────────────────────────────────────────────────────

def read_review_summary(output: Path) -> dict | None:
    p = output / "review_summary.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _scan_majors(summary: dict | None) -> list[tuple[str, str]]:
    """从 review_summary.json 提取所有 (variant_id, chapter_num) 处于 major 状态的对。"""
    if not summary:
        return []
    majors: list[tuple[str, str]] = []
    for v in summary.get("variants", []):
        for c in v.get("chapters", []):
            if c.get("severity") == "major":
                majors.append((v["variant_id"], c["chapter_num"]))
    return majors


def enforce_review_gate(tender_dir: Path, state: dict, log, max_retries: int = 2) -> bool:
    """扫描 review_summary.json 的 major 章节,自动 --force 重写最多 max_retries 次。

    返回 True = 门禁通过(无 major);False = 已达重试上限仍有 major。

    修复(2026-05-11 Phase 2.2):
    - 改 for 循环上限明确,避免无意死循环
    - 每轮重写后重新全局扫(不仅扫当前 variant),覆盖跨 variant 的 major
    - return 前再做一次最终全局验证,避免"假成功"
    """
    output = tender_dir / "output"
    summary = read_review_summary(output)
    initial_majors = _scan_majors(summary)
    if not initial_majors:
        log.info("review gate ✓ 无 major")
        return True

    log.warning(f"review gate: 发现 {len(initial_majors)} 个 major 章节,开始自动重写")
    decisions = state["steps"].setdefault("review", {}).setdefault("gate_decisions", [])

    # 每个 (vid, cn) 的重写次数上限独立计算
    retry_count: dict[tuple[str, str], int] = {}

    for round_idx in range(1, max_retries + 1):
        summary = read_review_summary(output)
        current_majors = _scan_majors(summary)
        if not current_majors:
            log.info(f"review gate ✓ 第 {round_idx-1} 轮后已清空 major")
            break

        # 过滤已达上限的对(不再重试)
        actionable = [
            (vid, cn) for vid, cn in current_majors
            if retry_count.get((vid, cn), 0) < max_retries
        ]
        if not actionable:
            log.error(f"review gate: 全部剩余 major 已达 {max_retries} 次重写上限,放弃")
            break

        log.warning(f"review gate 第 {round_idx}/{max_retries} 轮:{len(actionable)} 个 major 待重写")
        for vid, cn in actionable:
            retry_count[(vid, cn)] = retry_count.get((vid, cn), 0) + 1
            n = retry_count[(vid, cn)]
            log.warning(f"  重写 {vid} 章 {cn}(第 {n}/{max_retries} 次)")
            code = run_script("bid_writer.py", [
                str(tender_dir), "--variant", vid, "--chapter", cn, "--force"
            ], log)
            decisions.append({
                "variant": vid, "chapter": cn, "attempt": n, "exit_code": code,
                "round": round_idx,
                "at": datetime.now().isoformat(timespec="seconds")
            })

        # 触及的 variants 全部重审,然后下一轮再全局扫
        touched_vids = {vid for vid, _ in actionable}
        for vid in touched_vids:
            run_script("bid_reviewer.py", [str(tender_dir), "--variant", vid], log)

    # 终态全局验证(防止假成功)
    final = read_review_summary(output)
    remaining_majors = _scan_majors(final)
    if remaining_majors:
        log.error(f"review gate ✗ 终态仍有 {len(remaining_majors)} 个 major:")
        for vid, cn in remaining_majors[:10]:
            log.error(f"    - {vid} 章 {cn}")
        return False
    log.info("review gate ✓ 终态全局验证通过")
    return True


def enforce_similarity_gate(tender_dir: Path, state: dict, log, max_retries: int = 2) -> bool:
    """相似度超阈值对 → 对"后者"variant 的最差章节 --force 重写。"""
    output = tender_dir / "output"
    matrix_path = output / "similarity_matrix.json"
    if not matrix_path.exists():
        return True
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))

    # 阈值（从 variants.json 读）
    th = {"overall": 0.40, "paragraph": 0.50, "heading_order": 0.70, "heading_set": 0.85}
    vj = tender_dir / "extracted" / "variants.json"
    if vj.exists():
        th.update(json.loads(vj.read_text(encoding="utf-8")).get("similarity_threshold", {}))

    def is_violation(p):
        return (p["overall"] > th["overall"]
                or p["paragraph_max"] > th["paragraph"]
                or p.get("heading_order", 0) >= th["heading_order"])

    def is_heading_only(p):
        return (p.get("heading_order", 0) >= th["heading_order"]
                and p["overall"] <= th["overall"]
                and p["paragraph_max"] <= th["paragraph"])

    violations = [p for p in matrix.get("pairs", []) if is_violation(p)]
    if not violations:
        log.info("similarity gate ✓ 无超阈值对")
        return True

    log.warning(f"similarity gate: 超阈值 {len(violations)} 对，开始自动重写")
    decisions = state["steps"].setdefault("similarity", {}).setdefault("gate_decisions", [])

    retry_count: dict[str, int] = {}  # 按 variant 计数（heading 重写是 variant 全量）
    for attempt in range(max_retries):
        any_rewrite = False
        reseeded: set[str] = set()
        for p in violations:
            vid = p["b"]
            cn = p["worst_chapter"] or ""

            # 分支 A：仅标题收敛 → 重种 seed 并全量重写该 variant
            if is_heading_only(p):
                if vid in reseeded:
                    continue
                n = retry_count.get(vid, 0) + 1
                if n > max_retries:
                    continue
                retry_count[vid] = n
                any_rewrite = True
                reseeded.add(vid)
                reseed_variant(vj, vid, log)
                log.warning(f"  [heading] {vid} 重种 seed 后全量重写（第 {n}/{max_retries}）")
                code = run_script("bid_writer.py", [
                    str(tender_dir), "--variant", vid, "--force"
                ], log)
                decisions.append({
                    "pair": f"{p['a']}↔{p['b']}", "variant": vid, "reason": "heading",
                    "attempt": n, "exit_code": code,
                    "at": datetime.now().isoformat(timespec="seconds")
                })
                run_script("bid_reviewer.py", [str(tender_dir), "--variant", vid], log)
                run_script("bid_assemble.py", [str(tender_dir), "--variant", vid], log)
                continue

            # 分支 B：段落/整体超阈 → 只重写最差章节
            if not cn:
                continue
            key = f"{vid}/{cn}"
            n = retry_count.get(key, 0) + 1
            if n > max_retries:
                continue
            retry_count[key] = n
            any_rewrite = True
            log.warning(f"  [content] 重写 {vid} 章 {cn}（第 {n}/{max_retries}）")
            code = run_script("bid_writer.py", [
                str(tender_dir), "--variant", vid, "--chapter", cn, "--force"
            ], log)
            decisions.append({
                "pair": f"{p['a']}↔{p['b']}", "variant": vid, "chapter": cn,
                "reason": "content", "attempt": n, "exit_code": code,
                "at": datetime.now().isoformat(timespec="seconds")
            })
            run_script("bid_assemble.py", [str(tender_dir), "--variant", vid], log)

        if not any_rewrite:
            break
        run_script("bid_similarity.py", [str(tender_dir)], log)
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        violations = [p for p in matrix.get("pairs", []) if is_violation(p)]
        if not violations:
            return True
    return not violations


def reseed_variant(variants_json: Path, variant_id: str, log) -> None:
    """把指定 variant 的 section_order_seed 与常量异或，持久化回 JSON。"""
    if not variants_json.exists():
        return
    payload = json.loads(variants_json.read_text(encoding="utf-8"))
    for v in payload.get("variants", []):
        if v.get("id") == variant_id:
            old = int(v.get("section_order_seed", 0))
            v["section_order_seed"] = old ^ 0xDEADBEEF
            log.info(f"  reseed {variant_id}: {old} → {v['section_order_seed']}")
            break
    variants_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── 汇总报告 ────────────────────────────────────────────────────────

def _write_review_summary_md(output: Path, log) -> None:
    rs = read_review_summary(output)
    if not rs:
        return
    lines = [
        "# 审校汇总", "",
        f"- 总 variant：{rs.get('total_variants', 0)}",
        f"- 全部通过：{rs.get('all_ok', 0)}",
        f"- 含 major：{rs.get('has_major', 0)}",
        f"- 含 moderate：{rs.get('has_moderate', 0)}",
        f"- 含 minor：{rs.get('has_minor', 0)}",
        "", "## 每家最严重章节", "",
        "| Variant | 公司 | 模型 | 总体 | 最严重章节 | 严重性 |",
        "|---------|------|------|------|-----------|--------|",
    ]
    sev_rank = {"major": 3, "moderate": 2, "minor": 1, "ok": 0}
    for v in rs.get("variants", []):
        worst = max(v.get("chapters", [{}]), key=lambda c: sev_rank.get(c.get("severity", "ok"), 0), default=None)
        w_num = worst["chapter_num"] if worst else "-"
        w_sev = worst["severity"] if worst else "-"
        lines.append(
            f"| {v['variant_id']} | {v.get('company_name','')} | {v.get('model','')} | "
            f"{v['overall_severity']} | {w_num} | {w_sev} |"
        )
    (output / "review_summary.md").write_text("\n".join(lines), encoding="utf-8")
    log.info(f"写入: {output / 'review_summary.md'}")


def _write_compliance_summary_md(output: Path, log) -> None:
    rows = []
    for v_dir in sorted(output.iterdir()):
        if not (v_dir.is_dir() and v_dir.name.startswith("V")):
            continue
        scan = v_dir / "compliance_scan.json"
        if not scan.exists():
            continue
        data = json.loads(scan.read_text(encoding="utf-8"))
        rows.append({
            "variant": v_dir.name,
            "blind": len(data.get("blind_violations", [])),
            "resp_miss": sum(1 for r in data.get("response_check", []) if r.get("required") and not r.get("present")),
            "chars_short": sum(1 for a in data.get("plan_alignment", []) if not a.get("chars_meet_target")),
            "align_bad": sum(1 for a in data.get("plan_alignment", []) if not a.get("file_exists") or not a.get("title_match")),
        })
    if not rows:
        return
    mk = lambda n: "✓" if n == 0 else f"❌ {n}"
    lines = [
        "# 批量合规汇总", "",
        "| Variant | 暗标违规 | 响应词缺失 | 字数短缺章节 | 对齐违规 |",
        "|---------|---------|-----------|-------------|---------|",
    ]
    for r in rows:
        lines.append(f"| {r['variant']} | {mk(r['blind'])} | {mk(r['resp_miss'])} | "
                     f"{mk(r['chars_short'])} | {mk(r['align_bad'])} |")
    (output / "compliance_summary.md").write_text("\n".join(lines), encoding="utf-8")
    log.info(f"写入: {output / 'compliance_summary.md'}")


def _write_overview_md(tender_dir: Path, output: Path, ext: Path, mode: str, log) -> None:
    vs = None
    vj = ext / "variants.json"
    if vj.exists():
        vs = json.loads(vj.read_text(encoding="utf-8"))
    lines = [
        f"# 项目总览 - {vs.get('project_name') if vs else tender_dir.name}", "",
        f"**模式：** {'批量' if mode == 'batch' else '单份'}  ",
        f"**生成时间：** {datetime.now().isoformat(timespec='seconds')}  ", "",
    ]
    if mode == "batch" and vs:
        sim_path = output / "similarity_matrix.json"
        worst_pair = None
        if sim_path.exists():
            mat = json.loads(sim_path.read_text(encoding="utf-8"))
            pairs = mat.get("pairs", [])
            if pairs:
                worst_pair = max(pairs, key=lambda p: max(p["overall"], p["paragraph_max"]))
        lines.extend([
            f"**单位数：** {vs['variant_count']}  ",
            f"**模型池：** {', '.join(vs['model_pool'])}  ",
            "", "## 各家概况", "",
            "| Variant | 公司 | 模型 | 字数 | Review | 合规 | DOCX |",
            "|---------|------|------|------|--------|------|------|",
        ])
        rev = read_review_summary(output) or {}
        rev_map = {v["variant_id"]: v for v in rev.get("variants", [])}
        for v in vs["variants"]:
            vid = v["id"]
            v_dir = output / vid
            total_chars = 0
            if v_dir.is_dir():
                for md in v_dir.glob("*.md"):
                    if md.name not in ("review.md",):
                        total_chars += sum(1 for c in md.read_text(encoding="utf-8") if "\u4e00" <= c <= "\u9fff")
            rev_sev = rev_map.get(vid, {}).get("overall_severity", "-")
            scan = v_dir / "compliance_scan.json"
            if scan.exists():
                d = json.loads(scan.read_text(encoding="utf-8"))
                comp = "✓" if not d.get("blind_violations") and all(
                    r.get("present") for r in d.get("response_check", []) if r.get("required")
                ) else "❌"
            else:
                comp = "-"
            docx = next(output.glob(f"{vid}_*.docx"), None)
            lines.append(
                f"| {vid} | {v['company_name']} | {v['model']} | {total_chars:,} | {rev_sev} | {comp} | "
                f"{docx.name if docx else '-'} |"
            )
        if worst_pair:
            lines.extend([
                "",
                f"**最高相似度对：** {worst_pair['a']} ↔ {worst_pair['b']}  "
                f"整体 {worst_pair['overall']:.1%}  段落 {worst_pair['paragraph_max']:.1%}",
            ])
    else:
        docx = next((output.glob("*.docx")), None)
        total_chars = sum(
            sum(1 for c in md.read_text(encoding="utf-8") if "\u4e00" <= c <= "\u9fff")
            for md in output.glob("*.md")
            if md.name not in ("review.md", "compliance_scan.md", "项目总览.md",
                              "review_summary.md", "compliance_summary.md",
                              "相似度检查报告.md")
        )
        lines.extend([
            f"**总字数：** {total_chars:,}  ",
            f"**DOCX：** {docx.name if docx else '-'}  ",
        ])
    (output / "项目总览.md").write_text("\n".join(lines), encoding="utf-8")
    log.info(f"写入: {output / '项目总览.md'}")


def write_summary_reports(tender_dir: Path, mode: str, log) -> None:
    """生成 compliance_summary.md / review_summary.md / 项目总览.md"""
    output = tender_dir / "output"
    ext = tender_dir / "extracted"
    _write_review_summary_md(output, log)
    if mode == "batch":
        _write_compliance_summary_md(output, log)
    _write_overview_md(tender_dir, output, ext, mode, log)


# ─── 健康检查 ────────────────────────────────────────────────────────

REQUIRED_PKGS = ["pdfplumber", "openpyxl", "docx", "openai"]
OPTIONAL_PKGS = ["simhash", "datasketch", "jieba", "olefile", "fitz"]


def doctor(tender_dir: Path | None) -> int:
    use_color = sys.stdout.isatty()
    OK = "\033[32m✓\033[0m" if use_color else "✓"
    FAIL = "\033[31m✗\033[0m" if use_color else "✗"
    WARN = "\033[33m⚠\033[0m" if use_color else "⚠"

    issues = []

    print("── Python 依赖 ──")
    for pkg in REQUIRED_PKGS:
        try:
            importlib.import_module(pkg)
            print(f"  {OK} {pkg}")
        except ImportError:
            print(f"  {FAIL} {pkg}  (required)")
            issues.append(f"pip install {pkg}")
    for pkg in OPTIONAL_PKGS:
        try:
            importlib.import_module(pkg)
            print(f"  {OK} {pkg}  (optional)")
        except ImportError:
            print(f"  {WARN} {pkg}  (optional — 缺失将降级)")

    print("\n── 脚本完整性 ──")
    for s in ["bid_common.py", "bid_extract.py",
              "bid_template_match.py", "bid_facts_extract.py", "bid_derive.py",
              "bid_skeleton_extract.py", "bid_special_extract.py", "bid_input_assemble.py",
              "bid_fingerprint.py", "bid_variants.py",
              "bid_writer.py", "bid_reviewer.py", "bid_assemble.py", "bid_compliance.py",
              "bid_similarity.py", "bid_llm_client.py", "bid_run.py"]:
        p = SCRIPTS / s
        if p.exists():
            print(f"  {OK} {s}")
        else:
            print(f"  {FAIL} {s}")
            issues.append(f"缺失脚本 {s}")

    print("\n── LLM 配置 ──")
    try:
        from bid_llm_client import _load_env as _bid_load_env
        _bid_load_env()
    except Exception:
        pass
    from bid_common import get_bid_gen_home as _ggh
    cfg_path = _ggh() / "configs" / "bid_llm_config.json"
    if not cfg_path.exists():
        print(f"  {FAIL} {cfg_path} 不存在")
        issues.append(f"运行: python bid_llm_client.py --init")
    else:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            print(f"  {OK} {cfg_path}")
            models = cfg.get("models", {})
            for name, m in models.items():
                if m.get("disabled"):
                    print(f"  ⊘ {name}  (disabled in config)")
                    continue
                env = m.get("api_key_env", "")
                if env and not os.environ.get(env):
                    print(f"  {FAIL} {name}: 环境变量 {env} 未设置")
                    issues.append(f"export {env}=...")
                else:
                    print(f"  {OK} {name}  (api_key_env={env})")
        except json.JSONDecodeError as e:
            print(f"  {FAIL} 配置 JSON 非法: {e}")
            issues.append("修复 bid_llm_config.json")

    # ─── 系统依赖(pandoc / 字体 / 磁盘)──
    print("\n── 系统依赖 ──")
    # 1. pandoc
    try:
        import shutil as _shutil
        pandoc_exe = _shutil.which("pandoc")
        if pandoc_exe:
            ver_proc = subprocess.run([pandoc_exe, "--version"],
                                      capture_output=True, text=True, timeout=10)
            ver = ver_proc.stdout.splitlines()[0] if ver_proc.returncode == 0 else "(unknown)"
            print(f"  {OK} pandoc — {ver}")
        else:
            print(f"  {FAIL} pandoc 未安装(docx 装配会失败)")
            issues.append("macOS: brew install pandoc / Ubuntu: apt-get install pandoc")
    except Exception as e:
        print(f"  {WARN} pandoc 检测异常: {e}")

    # 2. 中文字体(fc-list)
    try:
        fc_proc = subprocess.run(["fc-list", ":lang=zh-cn"],
                                 capture_output=True, text=True, timeout=10)
        if fc_proc.returncode == 0 and fc_proc.stdout:
            fonts = fc_proc.stdout.splitlines()
            needed = {"宋体": False, "黑体": False, "仿宋": False}
            for line in fonts:
                low = line
                if "SimSun" in low or "宋体" in low or "Song" in low or "Noto Serif CJK" in low:
                    needed["宋体"] = True
                if "SimHei" in low or "黑体" in low or "Hei" in low or "Noto Sans CJK" in low:
                    needed["黑体"] = True
                if "FangSong" in low or "仿宋" in low:
                    needed["仿宋"] = True
            for f, has in needed.items():
                if has:
                    print(f"  {OK} 字体: {f}")
                else:
                    print(f"  {WARN} 字体未找到: {f}(部分章节字体会回退)")
        else:
            print(f"  {WARN} fc-list 不可用,跳过字体检查(macOS 缺 fontconfig 时常见)")
    except FileNotFoundError:
        print(f"  {WARN} fc-list 未安装,跳过字体检查")
    except Exception as e:
        print(f"  {WARN} 字体检测异常: {e}")

    # 3. 磁盘空间(目标:tender_dir 所在卷剩余 ≥ 500MB)
    if tender_dir and tender_dir.is_dir():
        try:
            stat = os.statvfs(tender_dir)
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb >= 500:
                print(f"  {OK} 磁盘剩余 {free_mb:,.0f} MB")
            else:
                print(f"  {FAIL} 磁盘剩余 {free_mb:,.0f} MB < 500 MB(可能不足以装配)")
                issues.append("清理磁盘空间 ≥ 500MB")
        except Exception as e:
            print(f"  {WARN} 磁盘检测异常: {e}")

    if tender_dir and tender_dir.is_dir():
        print(f"\n── 投标目录 {tender_dir.name} ──")
        ext = tender_dir / "extracted"
        for f in ("project_brain.md", "chapter_plan.md", "disqualification_rules.md"):
            p = ext / f
            print(f"  {OK if p.exists() else WARN} {f}")
        try:
            from bid_common import parse_chapter_plan
            rows = parse_chapter_plan(ext / "chapter_plan.md")
            if rows:
                print(f"  {OK} chapter_plan 解析到 {len(rows)} 章")
            else:
                print(f"  {WARN} chapter_plan 无可解析章节")
        except Exception as e:
            print(f"  {FAIL} chapter_plan 解析异常: {e}")
            issues.append("修复 chapter_plan.md 格式")

    print("\n" + ("=" * 40))
    if issues:
        print(f"{FAIL} 发现 {len(issues)} 个问题，修复建议：")
        for s in issues:
            print(f"  - {s}")
        return 1
    print(f"{OK} 全部检查通过")
    return 0


# ─── 主流程 ────────────────────────────────────────────────────────

def run_pipeline(tender_dir: Path, args, log) -> int:
    ext = tender_dir / "extracted"
    output = tender_dir / "output"
    output.mkdir(exist_ok=True)
    state_path = output / ".bid_run.state.json"
    state = load_state(state_path)
    repaired = repair_stale_running_steps(state)
    if repaired:
        log.warning(f"已修复上次异常遗留的 running 状态: {', '.join(repaired)}")
        save_state(state_path, state)

    # ★ v3: 项目指纹校验 — 防跨项目数据串用
    try:
        fp = _project_fingerprint(ext)
    except Exception:
        fp = {}
    if fp.get("hash"):
        stored_hash = state.get("_project_hash")
        if stored_hash and stored_hash != fp["hash"]:
            log.error(f"⚠ 项目指纹不匹配！当前={fp['name'][:20]}... ≠ 历史={stored_hash}")
            log.error("  这说明 output 目录的 project_brain.md 被替换成了另一个项目的数据")
            log.error("  请确认 tender_dir 是否正确，或清空 output/ 重跑")
            return 2
        state["_project_hash"] = fp["hash"]
        state["_project_name"] = fp.get("name", "")[:60]
        log.info(f"项目指纹: {fp.get('name','')[:40]} (hash={fp['hash']})")

    state["runs"].append({
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "cwd": str(tender_dir),
        "args": vars(args),
    })
    save_state(state_path, state)

    # 决定要跑哪些步骤
    mode = detect_mode(tender_dir) if (ext / "variants.json").exists() or args.batch else "single"
    # --batch 强制批量（需要先 variants）
    if args.batch:
        mode = "batch"

    all_steps = STEPS[:]
    # 单份模式跳过 variants / similarity
    if mode == "single":
        all_steps = [s for s in all_steps if s not in ("variants", "similarity")]

    # --from / --to 切片
    def idx(step: str) -> int:
        try:
            return all_steps.index(step)
        except ValueError:
            return -1

    start_i = idx(args.from_step) if args.from_step else 0
    end_i = idx(args.to_step) + 1 if args.to_step else len(all_steps)
    if args.resume:
        # 从第一个未完成的步骤开始(状态 != "ok",含 skip/fail/running/未跑过)
        # skip 状态可能是"主动跳过(成功路径)"或"被迫跳过(上游缺数据)";默认两种都重跑,
        # 用 --skip-resume-passed-skips 跳过那些标记了 note="downgrade" 的可接受 skip。
        for i, s in enumerate(all_steps):
            step_state = state["steps"].get(s, {})
            status = step_state.get("status")
            if status == "ok":
                continue
            # 可接受的 skip(如 template 失配/special 未抽到):resume 时跳过这些保留 skip 状态
            if (status == "skip" and getattr(args, "skip_resume_passed_skips", False)
                    and step_state.get("note", "").startswith(("no_template_match", "no_facts"))):
                continue
            start_i = max(start_i, i)
            log.info(f"resume: 从 step={s} 开始(上次状态={status or 'unrun'})")
            break

    plan = all_steps[start_i:end_i]
    # P0.7: 如果 assemble 在计划中，强制追加 compliance（不可跳过）
    if "assemble" in plan and "compliance" not in plan:
        ci = all_steps.index("compliance")
        if ci > all_steps.index("assemble"):
            plan = list(plan) + ["compliance"]
            log.warning("compliance 被 --to 截断，已强制追加（P0.7）")
    log.info(f"模式: {mode}  计划执行: {' → '.join(plan)}")

    if args.dry_run:
        for s in plan:
            log.info(f"  [dry-run] {s}")
        return 0

    # Step 1 前后 Claude 手工步骤检查
    # Step 2 在 extract 之后、variants/write 之前必须齐备
    # 2026-05-13 升级:默认自动调 synthesize 链生成,--manual-step2 走旧手工路径
    def ensure_step2():
        ext = tender_dir / "extracted"
        has_brain = (ext / "project_brain.md").exists()
        has_plan = (ext / "chapter_plan.md").exists()
        has_rules = (ext / "disqualification_rules.md").exists()

        if not (has_brain and has_plan and has_rules):
            if getattr(args, "manual_step2", False):
                missing = []
                if not has_brain: missing.append("project_brain.md")
                if not has_plan:  missing.append("chapter_plan.md")
                if not has_rules: missing.append("disqualification_rules.md")
                log.error(f"Step 2 未完成 (--manual-step2)：缺少 {missing}")
                log.error("请让 Claude 生成这些文件后再 --resume")
                return False

            log.info("Step 2 三件套缺失,自动调 synthesize 链生成...")
            # 0. boq_extract (解析清单 xlsx;失败不阻断,brain 第 2 节走旧逻辑)
            code = run_script("bid_boq_extract.py",
                              [str(tender_dir), "--force"], log)
            if code not in (0, 1):
                log.warning(f"  boq_extract 失败 code={code},brain 第 2 节将空白")

            # 1. difficulty_synthesize (DeepSeek 合成第 3/4 节,可降级)
            code = run_script("bid_difficulty_synthesize.py",
                              [str(tender_dir), "--force"], log)
            if code not in (0, 1):
                log.warning(f"  difficulty_synthesize 失败 code={code},"
                            f"brain 第 3/4 节将走旧逻辑")

            # 2. brain_translate
            code = run_script("bid_brain_translate.py",
                              [str(tender_dir), "--force"], log)
            if code != 0:
                log.error(f"  brain_translate 失败 code={code}")
                return False

            # 3. rules_synthesize (先,因 plan 要读字号)
            code = run_script("bid_rules_synthesize.py",
                              [str(tender_dir), "--force"], log)
            if code != 0:
                log.error(f"  rules_synthesize 失败 code={code}")
                return False

            # 4. plan_synthesize (读 rules 的字号/行距)
            ps_cmd = [str(tender_dir), "--force"]
            if getattr(args, "target_pages", None):
                ps_cmd += ["--target-pages", str(args.target_pages)]
            code = run_script("bid_plan_synthesize.py", ps_cmd, log)
            if code != 0:
                log.error(f"  plan_synthesize 失败 code={code}")
                return False
            log.info(f"  ✓ Step 2 三件套自动生成完成")

        ready, issues = check_step2_ready(tender_dir)
        if not ready:
            log.error(f"Step 2 校验失败: {issues}")
            log.error("如规则化输出不达标,可用 --manual-step2 走 Claude 手工路径")
            return False
        return True

    for step in plan:
        mark_step(state, step, "running")
        save_state(state_path, state)
        t0 = time.time()

        try:
            if step == "extract":
                code = run_script("bid_extract.py", [str(tender_dir)], log)
            elif step == "template":
                # Tier 0 范本指纹比对
                if (ext / "template_match.json").exists() and not args.force:
                    log.info("  template_match.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_template_match.py", cmd, log)
                # 范本未命中也不阻断,只标记
                if code != 0:
                    log.warning(f"  template 未命中(code={code}),下游需完整跑 LLM")
                    mark_step(state, step, "skip", note=f"no_template_match")
                    continue
            elif step == "facts":
                if (ext / "project_facts.json").exists() and not args.force:
                    log.info("  project_facts.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_facts_extract.py", cmd, log)
                # facts exit=1 仅表示有 miss 字段(招标本身未明示),非硬失败
                if code == 1:
                    log.info("  facts 有 miss 字段(招标本身未明示该项),继续")
                    code = 0
            elif step == "derive":
                if (ext / "project_derived.json").exists() and not args.force:
                    log.info("  project_derived.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    continue
                if not (ext / "project_facts.json").exists():
                    log.warning("  project_facts.json 不存在,跳过 derive")
                    mark_step(state, step, "skip", note="facts missing")
                    continue
                code = run_script("bid_derive.py", [str(tender_dir)], log)
            elif step == "special":
                if (ext / "special_clauses.json").exists() and not args.force:
                    log.info("  special_clauses.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_special_extract.py", cmd, log)
                # special 抽取失败不阻断,Step 2 仍可继续
                if code != 0:
                    log.warning(f"  special 抽取失败(code={code}),继续往下走")
                    mark_step(state, step, "skip", note=f"special exit={code}")
                    continue
            elif step == "input_assemble":
                if (ext / "project_input.json").exists() and not args.force:
                    log.info("  project_input.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_input_assemble.py", cmd, log)
                if code not in (0, 1):  # 0=完整 1=部分
                    log.warning(f"  input_assemble 失败(code={code})")
            elif step == "skeleton":
                # Step 1.5: 骨架优先提取(三份 JSON)
                # 已存在 + 非 --force 则跳过;LLM 不可用时降级跳过(Step 2 仍可手工)
                ext_files = [
                    ext / "evaluation_method.json",
                    ext / "mandatory_outline.json",
                    ext / "disqualification.json",
                ]
                if all(f.exists() for f in ext_files) and not args.force:
                    log.info("  三份骨架 JSON 已存在,跳过(--force 重抽)")
                    mark_step(state, step, "skip")
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_skeleton_extract.py", cmd, log)
                # 骨架提取失败 ≠ 流程中止;只警告,Step 2 校验时再提示
                if code != 0:
                    log.warning(
                        f"  skeleton 提取退出码 {code}(警告级,继续往下走)。"
                        f"Step 2 会再次校验缺失项。"
                    )
                    mark_step(state, step, "skip", note=f"exit={code} downgraded")
                    continue
            elif step == "fingerprint":
                if not args.fingerprint:
                    log.info("  fingerprint 未显式启用（加 --fingerprint），跳过")
                    mark_step(state, step, "skip")
                    continue
                code = run_script("bid_fingerprint.py", [str(tender_dir)], log)
            elif step == "variants":
                # 已有则跳过；否则要求用户提供 --companies
                if (ext / "variants.json").exists():
                    log.info("  variants.json 已存在，跳过")
                    mark_step(state, step, "skip")
                    continue
                if not args.companies:
                    log.error("批量模式但 variants.json 不存在，且未提供 --companies")
                    mark_step(state, step, "fail", error="missing --companies")
                    save_state(state_path, state)
                    return 2
                code = run_script("bid_variants.py", [
                    str(tender_dir), "--companies", args.companies
                ], log)
            elif step == "chart_specs":
                # 从 chapter_plan.md 派生 chart_specs.json,可选,失败也不阻断
                if not (ext / "chapter_plan.md").exists():
                    log.info("  chapter_plan.md 不存在,跳过 chart_specs")
                    mark_step(state, step, "skip")
                    continue
                if (ext / "chart_specs.json").exists() and not args.force:
                    log.info("  chart_specs.json 已存在,跳过(--force 重派生)")
                    mark_step(state, step, "skip")
                    continue
                code = run_script("bid_chart_specs.py", [str(tender_dir)], log)
                # 派生失败仅警告,不阻断
            elif step == "plan_variants":
                # 为每个变体生成独立的标题计划 + 图表分配
                if mode != "batch":
                    log.info("  单份模式，跳过 plan_variants")
                    mark_step(state, step, "skip")
                    continue
                # 加载 variants 列表
                _vj = ext / "variants.json"
                _vlist = json.loads(_vj.read_text(encoding="utf-8")).get("variants", []) if _vj.exists() else []
                # 检查是否所有变体都已有
                all_done = True
                for v in _vlist:
                    vp = output / v["id"] / "chapter_plan_variant.md"
                    if not vp.exists():
                        all_done = False
                        break
                if all_done and not args.force:
                    log.info("  所有变体标题计划已存在，跳过")
                    mark_step(state, step, "skip")
                    continue
                code = run_script("bid_plan_variants.py", [str(tender_dir)], log)
                # 派生失败仅警告,不阻断
                if code != 0:
                    log.warning(f"  chart_specs 派生失败 code={code},降级为旧 prompt 流程")
                    mark_step(state, step, "skip", note=f"derive failed code={code}")
                    continue
            elif step == "write":
                if not ensure_step2():
                    mark_step(state, step, "fail", error="step2 not ready")
                    save_state(state_path, state)
                    return 2
                gate_ok, gate_issues = generation_input_gate(tender_dir)
                if not gate_ok:
                    for issue in gate_issues:
                        log.error(f"生成前门禁失败: {issue}")
                    mark_step(state, step, "fail", error="generation input gate failed", issues=gate_issues)
                    save_state(state_path, state)
                    return 2
                # writer 强制要 variants.json;single 模式自动生成 1 家默认
                if not (ext / "variants.json").exists():
                    log.info("  variants.json 缺失,自动生成单份配置(投标单位名称占位)")
                    code = run_script("bid_variants.py", [
                        str(tender_dir), "--companies", "投标单位名称",
                        "--model-pool", "deepseek_chat",
                    ], log)
                    if code != 0:
                        log.error(f"  自动生成 variants.json 失败 code={code}")
                        mark_step(state, step, "fail", error="variants gen failed")
                        save_state(state_path, state)
                        return 2
                cmd = [str(tender_dir), f"--workers={args.workers}"]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_writer.py", cmd, log)
                # write 阶段自动生成 variants.json 会导致 mode 从 single 漂到 batch
                # 重新检测，让 review/assemble/compliance 按正确路径找文件
                new_mode = detect_mode(tender_dir)
                if new_mode != mode:
                    log.info(f"  mode 由 {mode} → {new_mode}（variants.json 已生成）")
                    mode = new_mode
                # 写完后自动修 H1 字面(feedback_writer_h1_drift)
                if code == 0:
                    log.info("  post-writer: 自动修正 H1 字面...")
                    run_script("bid_h1_fix.py", [str(tender_dir)], log)
            elif step == "review":
                cmd = [str(tender_dir)]
                if mode == "single":
                    cmd.append("--single")
                code = run_script("bid_reviewer.py", cmd, log)
                # 质量门禁
                if mode == "batch" and not args.force_continue:
                    if not enforce_review_gate(tender_dir, state, log):
                        log.error("review gate 未通过，abort（可加 --force-continue 跳过）")
                        mark_step(state, step, "fail", error="review gate failed")
                        save_state(state_path, state)
                        return 1
            elif step == "assemble":
                cmd = [str(tender_dir / "output")]
                if mode == "batch":
                    cmd.append("--batch")
                code = run_script("bid_assemble.py", cmd, log)
            elif step == "verify":
                # 组装产物验证门:exit 2=硬失败(封面缺失/三家排版雷同/超硬顶)→ abort
                vcode = run_script("bid_verify.py", [str(tender_dir)], log)
                if vcode == 2 and not args.force_continue:
                    log.error("verify 门禁:产物硬失败,abort(详见 output/verify_report.md)")
                    mark_step(state, step, "fail", exit_code=2, error="verify gate failed")
                    save_state(state_path, state)
                    return 1
                if vcode == 1:
                    log.warning("verify 门禁:有警告,已记入 output/verify_report.md,继续")
                code = 0  # warn 不阻断;hard fail 已在上面 return
            elif step == "compliance":
                cmd = [str(tender_dir)]
                if mode == "batch":
                    cmd.append("--all-variants")
                code = run_script("bid_compliance.py", cmd, log)
            elif step == "similarity":
                code = run_script("bid_similarity.py", [str(tender_dir)], log)
                if not args.force_continue:
                    if not enforce_similarity_gate(tender_dir, state, log):
                        log.error("similarity gate 未通过，abort")
                        mark_step(state, step, "fail", error="similarity gate failed")
                        save_state(state_path, state)
                        return 1
            elif step == "summary":
                write_summary_reports(tender_dir, mode, log)
                code = 0
            else:
                log.warning(f"未知步骤 {step}，跳过")
                mark_step(state, step, "skip")
                continue

            elapsed = time.time() - t0
            if code == 0:
                mark_step(state, step, "ok", exit_code=0, seconds=round(elapsed, 1))
                log.info(f"✓ {step} ok ({elapsed:.1f}s)")
            else:
                mark_step(state, step, "fail", exit_code=code, seconds=round(elapsed, 1))
                log.error(f"✗ {step} fail (exit={code})")
                save_state(state_path, state)
                return code
        except Exception as e:
            mark_step(state, step, "fail", error=str(e))
            log.exception(f"步骤 {step} 异常: {e}")
            save_state(state_path, state)
            return 1
        save_state(state_path, state)

    log.info("=" * 60)
    log.info("pipeline 完成 ✓")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="/bid-gen 主编排器")
    p.add_argument("tender_dir", nargs="?", help="投标根目录")
    p.add_argument("--resume", action="store_true", help="从上次失败步骤继续(skip 也会重跑)")
    p.add_argument("--skip-resume-passed-skips", action="store_true",
                   help="resume 时跳过 note='no_template_match'/'no_facts' 类合理 skip,只重跑 fail 和未跑过的步")
    p.add_argument("--from", dest="from_step", help="起始步骤")
    p.add_argument("--to", dest="to_step", help="结束步骤")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--batch", action="store_true", help="强制批量模式")
    p.add_argument("--companies", help="批量模式必要时传入（逗号分隔）")
    p.add_argument("--fingerprint", action="store_true", help="启用 Step 1 指纹预检")
    p.add_argument("--force", action="store_true", help="写入阶段覆盖已存在章节")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force-continue", action="store_true", help="忽略 review / similarity 门禁")
    p.add_argument("--manual-step2", action="store_true",
                   help="不自动生成 Step 2 三件套,等 Claude 手工生成后 --resume")
    p.add_argument("--target-pages", type=int, default=None,
                   help="覆盖 plan_synthesize 自动判档的目标页数")
    p.add_argument("--doctor", action="store_true", help="运行配置健康检查后退出")
    args = p.parse_args()

    if args.doctor:
        td = Path(args.tender_dir).expanduser().resolve() if args.tender_dir else None
        return doctor(td)

    if not args.tender_dir:
        p.error("tender_dir 必填（除非 --doctor）")

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}", file=sys.stderr)
        return 2

    output = tender_dir / "output"
    output.mkdir(exist_ok=True)
    log = _setup_logger(
        "bid_run",
        output / "logs" / "run.log",
        root_log_path=output / "logs" / "bid.log",
    )
    log.info("=" * 60)
    log.info(f"bid_run 启动  目录: {tender_dir}")

    return run_pipeline(tender_dir, args, log)


if __name__ == "__main__":
    sys.exit(main())
