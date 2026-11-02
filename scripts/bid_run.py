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
from bid_common import get_config_dir, setup_logger as _setup_logger  # noqa: E402
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
    "coherence_pass",# bid_coherence_pass.py (连贯性工序C:跨章引用编织+重复检测+矛盾修复,assemble 前)
    "regroup",       # bid_regroup.py (合格制批量:二级标题数量/层级差异化 —— 目录指纹族第三层)
    "assemble",      # bid_assemble.py (--batch 批量 / 单份)
    "visual_style",  # bid_officecli_style.py (OfficeCLI 可重放视觉编排)
    "visual_render", # bid_visual_render.py (结构快照 + 全篇联系表)
    "visual_verify", # bid_visual_similarity.py (感知相似度;首期 warning-only)
    "toc_fill",      # bid_toc_fill.py (静态目录回填真实页码;导出 PDF 定位标题页)
    "verify",        # bid_verify.py (组装产物验证门:封面/三家排版差异/页数硬顶)
    "compliance",    # bid_compliance.py (--all-variants 批量 / 单份)
    "similarity",    # bid_similarity.py (批量专用)
    "expert_review", # bid_expert_review.py (评标专家:对照评分表打分,只出报告不阻断)
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
                # 先看 tender 根标记 extracted/(防招标目录本身叫 output 被误判);
                # assemble/verify 等步骤传 <tender>/output 目录,避免嵌套 output/output/logs
                if (p / "extracted").is_dir() or (p / "output" / "extracted").is_dir():
                    log_dir = p / "output" / "logs"
                elif (p.name == "output" and not (p / "extracted").exists()
                      and (p.parent / "extracted").exists()):
                    # 名为 output 且自身无 extracted/:两义(真实 output / 招标根恰叫 output)。
                    # 判别:父级已有 extracted/ → 本目录就是 <tender>/output → 日志落 <tender>/output/logs;
                    # 否则视为招标根目录恰叫 output → 日志落 <root>/logs。
                    log_dir = p / "logs"
                else:
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
            # 非数字取值(如 BID_STEP_TIMEOUT=abc)不得抛 ValueError 崩掉整条 pipeline
            _to_raw = os.environ.get("BID_STEP_TIMEOUT", "0")
            try:
                _use_wd = int(_to_raw or 0) > 0
            except (TypeError, ValueError):
                log.warning(f"  BID_STEP_TIMEOUT={_to_raw!r} 非数字，已按不启用看门狗处理")
                _use_wd = False
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                # 独立进程组仅在看门狗启用时(否则 Ctrl-C 无法终止子进程)
                start_new_session=_use_wd,
            )
            assert proc.stdout is not None
            # 步骤级看门狗(默认关):BID_STEP_TIMEOUT=7200 防 LLM 子进程挂起阻塞整链
            # (_use_wd 已在 Popen 前求值;start_new_session 与看门狗同步开关)
            _step_timeout = int(_to_raw) if _use_wd else 0
            if _step_timeout > 0:
                import threading as _th
                def _kill():
                    import signal as _sig
                    import os as _os
                    if proc.poll() is None:
                        try:  # 杀整个进程组(连带 pandoc/officecli 等派生子进程)
                            _os.killpg(_os.getpgid(proc.pid), _sig.SIGKILL)
                        except (OSError, PermissionError, ProcessLookupError):
                            try:
                                proc.kill()
                            except (OSError, ProcessLookupError):
                                pass  # 进程已退出
                _timer = _th.Timer(_step_timeout, _kill)
                _timer.start()
                try:
                    for line in proc.stdout:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        logf.write(line)
                    proc.wait()
                finally:
                    _timer.cancel()
            else:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    logf.write(line)
                proc.wait()
            logf.write(f"\n# END: {datetime.now().isoformat(timespec='seconds')} exit={proc.returncode}\n")
            logf.flush()  # 确保末尾 30 行回读时缓冲区已落盘
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


def run_visual_style(tender_dir: Path, log, variant: str = "") -> int:
    """重装 DOCX 后立即重放视觉 profile，避免自动修复把视觉层覆盖掉。"""
    cmd = [str(tender_dir)]
    if variant and variant != "单份":
        cmd += ["--variant", variant]
    return run_script("bid_officecli_style.py", cmd, log)


# ─── 模式判定 ─────────────────────────────────────────────────────────

def detect_mode(tender_dir: Path) -> str:
    """返回 'batch' 或 'single'"""
    return "batch" if (tender_dir / "extracted" / "variants.json").exists() else "single"


def variant_count(tender_dir: Path) -> int:
    """读取 variants.json 中的有效变体数量；单变体无需相似度交叉检查。"""
    vj = tender_dir / "extracted" / "variants.json"
    if not vj.exists():
        return 0
    try:
        payload = json.loads(vj.read_text(encoding="utf-8"))
    except Exception:
        return 0
    variants = payload.get("variants", payload) if isinstance(payload, dict) else payload
    return len(variants) if isinstance(variants, list) else 0


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
                    # 区分两种"空 boq"：
                    #   汇总表模式（清单只有汇总表、无明细）= 合法空 → 放行；
                    #   抽取失败（清单含明细表头却没抽出条目）= 仍拦，防 [[feedback_boq_must_read]] 那类漏抽。
                    try:
                        from bid_extract import _BOQ_DETAIL_HEADER as _detail_kw
                    except Exception:
                        _detail_kw = ["分部分项工程量清单", "单价措施项目", "措施项目清单", "其他项目清单"]
                    _blob = ""
                    for _tp in (list(ext.glob("*清单*.txt")) + list(ext.glob("*工程量*.txt"))
                                + list(ext.glob("*BoQ*.txt"))):
                        try:
                            _blob += _tp.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            pass
                    if any(kw in _blob for kw in _detail_kw):
                        issues.append("清单含明细表头但 boq.json 未解析出工程量，疑似抽取失败，需修复清单抽取后再生成")
                    # 否则=汇总表模式（只有汇总表、无明细），合法空，放行
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
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # reviewer 被中途 kill/写一半时文件损坏 → 返回 None,
        # 让门禁走“保持不通过”的干净失败路径(而非抛 JSONDecodeError 上冒)
        return None


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


# 软性 major:重写也难立即根治的风格指标(pg_low/boilerplate/chars_short/keyword_coverage_low),
# 与硬伤(暗标违规/占位符/标题不匹配)区分;重试耗尽且仅剩软性 major 时降级警告效行,不阻断整链。
# ★ 日志文案必须由本集合派生:_majors_all_soft 判定用的是集合全量,写死
# “仅 pg_low/boilerplate”会让字短/关键词漏的章节被放行时看不出真实原因。
_SOFT_MAJOR_TYPES = {"pg_low", "boilerplate", "chars_short", "keyword_coverage_low"}


def _reviewer_full_cmd(tender_dir: Path) -> list[str]:
    """全量汇总复原的 reviewer 命令。

    单份模式(无 variants.json)必须带 --single,否则 reviewer 走批量路径找不到
    variants.json 直接 return 2 → 被门禁当成"运行失败"abort。
    (write 步会自动生成单份 variants.json,所以以“文件在不在”为准而非 mode 变量)
    """
    cmd = [str(tender_dir)]
    if not (tender_dir / "extracted" / "variants.json").exists():
        cmd.append("--single")
    return cmd


def _soft_issue_labels(summary: dict | None, majors: list[tuple[str, str]]) -> str:
    """列出给定 major 对实际命中的软性 issue 类型(供降级日志如实说明)。"""
    pairs = set(majors)
    seen: list[str] = []
    for v in (summary or {}).get("variants", []):
        for c in v.get("chapters", []):
            if c.get("severity") != "major":
                continue
            if (v.get("variant_id"), c.get("chapter_num")) not in pairs:
                continue
            for it in (c.get("issues") or []):
                t = it.get("type") if isinstance(it, dict) else str(it)
                if t and t not in seen:
                    seen.append(t)
    return "/".join(sorted(seen)) or "未标注类型"


def _majors_all_soft(summary: dict | None, majors: list[tuple[str, str]]) -> bool:
    """判断给定 major 对是否全部只含软性 issue(用于门禁降级判定)。

    P/G 极低(<0.3,几乎全套话)视为硬伤不降级——与 reviewer 收紧口径一致,
    防近全套话章节借"重写耗尽"放行,写作质量门禁失效。"""
    if not summary or not majors:
        return False
    pairs = set(majors)
    for v in summary.get("variants", []):
        for c in v.get("chapters", []):
            if c.get("severity") != "major":
                continue
            if (v.get("variant_id"), c.get("chapter_num")) not in pairs:
                continue
            issues = c.get("issues") or []
            if not issues:
                return False  # 无 issue 明细的 major 视为硬伤(不冒险放行)
            for i in issues:
                if not isinstance(i, dict):
                    return False  # 旧格式/手改的字符串 issue → 视为硬伤,不冒险放行
                if i.get("type") not in _SOFT_MAJOR_TYPES:
                    return False
                if i.get("type") == "pg_low":
                    try:
                        _pr = float(c.get("pg_ratio"))
                    except (TypeError, ValueError):
                        _pr = 0.0  # 缺失/畸形按最差处理(不默认放行)
                    if _pr < 0.3:
                        return False  # 极端套话=硬伤
                if i.get("type") == "chars_short":
                    # 与 pg_low 对称:字数只是"严重不足"(<50% 目标)时不降级——
                    # 否则 review 把 <70% 升格要求重写的口径被降级放行直接推翻。
                    try:
                        _cr = (int(c.get("actual_chars") or 0)
                               / max(int(c.get("target_chars") or 1), 1))
                    except (TypeError, ValueError):
                        _cr = 0.0
                    if _cr < 0.5:
                        return False
    return True


def _writer_variant_id(tender_dir: Path, vid: str) -> str | None:
    """review 的 variant_id → bid_writer --variant 参数。

    单份模式下 bid_reviewer --single 会把 variant_id 写成 "single",而 bid_writer 只认
    variants.json 里的 id(如 V01) → 直接透传 "--variant single" 必然“未找到 variant” exit2,
    审核门禁的自动重写完全失效、任何 major 都直接 abort。
    返回 None 表示不应带 --variant(writer 自行遍历唯一变体)。
    """
    if vid and vid != "single":
        return vid
    try:
        payload = json.loads(
            (tender_dir / "extracted" / "variants.json").read_text(encoding="utf-8"))
        vlist = payload.get("variants", []) if isinstance(payload, dict) else payload
        if isinstance(vlist, list) and len(vlist) == 1:
            return (vlist[0] or {}).get("id")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


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
    if summary is None:
        log.error("review gate: review_summary.json 缺失(reviewer 未成功运行?),门禁不通过")
        return False
    # ★ 零变体被审 = 什么都没审:reviewer 在变体目录全缺时只 warning 后 continue,
    #   以 total_variants=0 / has_major=0 / exit 0 收尾 → 门禁会误判"无 major"放行。
    if not summary.get("variants"):
        log.error("review gate: review_summary 无任何变体记录(变体目录缺失?)→ 门禁不通过")
        return False
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
        if summary is None:
            log.error("review gate: 轮内 summary 读取失败,保持不通过")
            return False
        current_majors = _scan_majors(summary)
        if not current_majors:
            log.info(f"review gate ✓ 第 {round_idx} 轮重写后已清空 major")
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
            _wid = _writer_variant_id(tender_dir, vid)
            _cmd = [str(tender_dir), "--chapter", cn, "--force"]
            if _wid:
                _cmd = [str(tender_dir), "--variant", _wid, "--chapter", cn, "--force"]
            code = run_script("bid_writer.py", _cmd, log)
            if code != 0:
                # 执行失败(瞬态 LLM 抖动)不消耗重试预算——回滚本轮预递增
                retry_count[(vid, cn)] = n - 1
                log.error(f"  重写失败 {vid} 章 {cn} (exit={code}),本轮跳过(不耗预算)")
                decisions.append({
                    "variant": vid, "chapter": cn, "attempt": n, "exit_code": code,
                    "note": "rewrite failed (no budget used)", "at": datetime.now().isoformat(timespec="seconds")})
                continue
            decisions.append({
                "variant": vid, "chapter": cn, "attempt": n, "exit_code": code,
                "round": round_idx,
                "at": datetime.now().isoformat(timespec="seconds")
            })

        # 触及的 variants 全部重审,然后下一轮再全局扫
        touched_vids = {vid for vid, _ in actionable}
        for vid in touched_vids:
            _rcmd = [str(tender_dir)]
            if vid == "single":
                _rcmd.append("--single")
            else:
                _rcmd += ["--variant", vid]
            run_script("bid_reviewer.py", _rcmd, log)
        # ⚠ --variant 模式会把 review_summary.json 覆盖成仅含该变体,
        # 下一轮全局扫/终态验证前必须刷一次全量汇总(否则其余变体 major 漏检→门禁假通过)
        # 注意:reviewer 退出码 1 = "仍有 major"(本轮已重写但未清零是预期中间态,由下一轮
        # 循环或终态验证裁决);退出码 ≥2 = 运行失败,此时汇总可能被覆盖 → 保持不通过防假成功
        if run_script("bid_reviewer.py", _reviewer_full_cmd(tender_dir), log) >= 2:
            log.error("review gate: 全量 reviewer 复原运行失败,保持门禁不通过")
            return False

    # 终态全局验证(防止假成功)
    final = read_review_summary(output)
    if final is None:
        log.error("review gate: 终态 summary 缺失(reviewer 运行失败?),门禁不通过")
        return False
    remaining_majors = _scan_majors(final)
    if remaining_majors:
        if _majors_all_soft(final, remaining_majors):
            # 降级效行:仅剩风格类指标(pg/套话)且重写已耗尽——硬伤零容忍,软指标不阻断整链
            log.warning(
                f"review gate △ 终态剩 {len(remaining_majors)} 个软性 major"
                f"({_soft_issue_labels(final, remaining_majors)}，重写已达上限),降级效行不阻断；"
                f"建议人工润色: " + ", ".join(f"{v}章{c}" for v, c in remaining_majors[:6])
            )
            state["steps"].setdefault("review", {})["note"] = \
                f"soft-major degraded: {len(remaining_majors)}"
            return True
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
        # 矩阵缺失 = bid_similarity 未成功运行(历史:静默 return True 跳过反串标交叉检查)
        log.error("similarity gate: similarity_matrix.json 不存在(bid_similarity 未成功运行?)")
        return False
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"similarity gate: 矩阵解析失败({e})")
        return False

    # 阈值（从 variants.json 读）
    th = {"overall": 0.40, "paragraph": 0.50, "heading_order": 0.70, "heading_set": 0.85}
    vj = tender_dir / "extracted" / "variants.json"
    if vj.exists():
        try:
            _vjs = json.loads(vj.read_text(encoding="utf-8"))
            if isinstance(_vjs, dict):
                th.update(_vjs.get("similarity_threshold", {}))
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            log.warning(f"variants.json 读取失败({e}),阈值用默认")

    # ★ 合格制(含混合制)项目:H1 章节锚点与 H2 子项标题是招标大纲 1:1 锁定的
    # (memory/feedback_bid_pass_fail_anchor.md),所有变体标题必然几乎完全相同
    # (heading_set/heading_order ≈ 1.0)。若仍把它们当串标判据,门禁会命中所有配对,
    # 而"重种 seed + 全量重写"分支只能产出同一套锁定标题 → 永不收敛、重试耗尽 abort。
    # 此类项目里标题相似度是合规结果,不是串标证据 → 只以正文相似度(overall/paragraph)判违规。
    try:
        from bid_plan_variants import is_passfail_lock
        _passfail_lock = is_passfail_lock(tender_dir / "extracted", log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"similarity gate: 合格制判定失败({e}),按评分制口径（标题计入判据）")
        _passfail_lock = False
    if _passfail_lock:
        log.info("similarity gate: 合格制项目 → 标题相似度不计入违规判据"
                 "（大纲 H1/H2 标题按招标要求 1:1 锁定,天然一致）")

    def is_violation(p):
        if ((p.get("overall") or 0) > th["overall"]
                or (p.get("paragraph_max") or 0) > th["paragraph"]):
            return True
        if _passfail_lock:
            return False
        return ((p.get("heading_order") or 0) >= th["heading_order"]
                or (p.get("heading_set") or 0) >= th["heading_set"])  # 之前定义未消费=门禁虚设

    def is_heading_only(p):
        # heading_set 与 heading_order 同为“标题维度”违规,二者任一超阈且内容维度未超
        # → 只能靠重种 seed + 全量重写收敛;漏判会把这类对推给“只改最差单章”分支
        # (改正文改不动标题集合) → 门禁空转到 abort。
        return ((p.get("heading_order", 0) >= th["heading_order"]
                 or p.get("heading_set", 0) >= th["heading_set"])
                and (p.get("overall") or 0) <= th["overall"]
                and (p.get("paragraph_max") or 0) <= th["paragraph"])

    violations = [p for p in matrix.get("pairs", [])
                  if isinstance(p, dict) and is_violation(p)]
    if not violations:
        log.info("similarity gate ✓ 无超阈值对")
        return True

    log.warning(f"similarity gate: 超阈值 {len(violations)} 对，开始自动重写")
    decisions = state["steps"].setdefault("similarity", {}).setdefault("gate_decisions", [])

    retry_count: dict[str, int] = {}  # 按 variant 计数（heading 重写是 variant 全量）
    for attempt in range(max_retries):
        any_rewrite = False
        reseeded: set[str] = set()
        _seen_vid_heading: set = set()
        for p in violations:
            if not isinstance(p, dict) or not p.get("b"):
                log.warning(f"  忽略畸形相似度对: {str(p)[:80]}")
                continue
            vid = p["b"]
            cn = p.get("worst_chapter") or ""

            # 分支 A：仅标题收敛 → 重种 seed 并全量重写该 variant
            # (同 vid 多对违规只重写一次——全量重写已覆盖该变体所有章节)
            if is_heading_only(p):
                if vid in _seen_vid_heading:
                    continue
                _seen_vid_heading.add(vid)
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
                # 先刷新该变体的 H2 标题计划(否则 writer 仍读旧 chapter_plan_variant.md,
                # 重种的 seed 无人消费,标题结构不变,收敛空转到重试上限)
                pv_code = run_script("bid_plan_variants.py", [str(tender_dir), "--variant", vid], log)
                if pv_code != 0:
                    log.error(f"  [heading] {vid} H2 计划刷新失败(code={pv_code}),本轮回写作废")
                    # 与分支 B 同口径:执行失败不耗重试预算
                    retry_count[vid] = n - 1
                    continue
                code = run_script("bid_writer.py", [
                    str(tender_dir), "--variant", vid, "--force"
                ], log)
                decisions.append({
                    "pair": f"{p['a']}↔{p['b']}", "variant": vid, "reason": "heading",
                    "attempt": n, "exit_code": code,
                    "at": datetime.now().isoformat(timespec="seconds")
                })
                if code != 0:
                    # 与分支 B 同口径:重写失败(含部分变体未命中 exit4)不跑 reviewer/
                    # assemble(陈旧章节复测=白耗轮次),且回滚预算不占次数
                    log.error(f"  [heading] {vid} 全量重写失败 exit={code},跳过本轮复测")
                    retry_count[vid] = n - 1
                    continue
                run_script("bid_reviewer.py", [str(tender_dir), "--variant", vid], log)
                acode = run_script("bid_assemble.py", [str(tender_dir / "output"), "--variant", vid], log)
                if acode == 0:
                    run_visual_style(tender_dir, log, vid)
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
            if code != 0:
                # 重写失败(含 exit4 部分未命中):跳过 assemble/复测(陈旧章节复测=白耗轮次)
                log.error(f"  重写失败 exit={code},跳过本轮 assemble/复测")
                retry_count[key] = n - 1  # 执行失败不耗预算
                continue
            acode = run_script("bid_assemble.py", [str(tender_dir / "output"), "--variant", vid], log)
            if acode == 0:
                run_visual_style(tender_dir, log, vid)

        if not any_rewrite:
            break
        # 上面按 --variant 复扫覆盖了汇总,补一次全量复原(与 review/compliance gate 同);
        # 退出码 1 = 正常 major 态(交给 review gate 裁决),≥2 = 运行失败 → 保持违规判定
        if run_script("bid_reviewer.py", [str(tender_dir)], log) >= 2:
            log.error("similarity gate: 全量 reviewer 复原运行失败,保持违规判定")
            return False
        run_script("bid_similarity.py", [str(tender_dir)], log)
        # 复测矩阵缺失/损坏时保持违规判定(而非抛异常被上层吞掉、掩盖门禁真实原因)
        try:
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.error(f"similarity gate: 复测矩阵读取失败({e})，保持违规判定")
            return False
        violations = [p for p in matrix.get("pairs", []) if isinstance(p, dict) and is_violation(p)]
        if not violations:
            return True
    return not violations


# ─── loop-mode 门禁(--loop-mode 启用;策略 configs/bid_loop_policy.json)──────

def load_loop_policy() -> dict:
    from bid_common import get_bid_gen_home
    p = get_config_dir() / "bid_loop_policy.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_coherence_config() -> dict:
    """读 configs/bid_coherence.json(连贯性改进总开关)。

    enabled=false 时所有工序不触发,管道行为与改动前完全一致(灰度安全)。
    """
    from bid_common import get_bid_gen_home
    p = get_config_dir() / "bid_coherence.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_verify_report(output: Path) -> dict:
    try:
        return json.loads((output / "verify_report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def enforce_verify_gate(tender_dir: Path, state: dict, log, policy: dict) -> bool:
    """verify 硬失败的自动修复环(--loop-mode)。

    读 verify_report.json(bid_verify 落盘的结构化报告):
      cover_missing  → bid_assemble 重装该变体(内部含完整后处理链)
      page_over_cap  → bid_concise 一轮精简(实测一轮饱和)→ 重装
      requires_human 类(format_identical/缺产物等)→ 直接 False 转人工
    每轮修复后 bid_verify 复验;决策记入 state.steps.verify.gate_decisions。
    """
    output = tender_dir / "output"
    vp = (policy.get("retry_policy") or {}).get("verify") or {}
    fixable = vp.get("auto_fixable") or {}
    max_rounds = int(vp.get("max_retries", 2))
    decisions = state["steps"].setdefault("verify", {}).setdefault("gate_decisions", [])
    attempts: dict[tuple, int] = {}

    def _reassemble(vid: str) -> int:
        if vid and vid != "单份":
            code = run_script("bid_assemble.py", [str(output), "--variant", vid], log)
        else:
            code = run_script("bid_assemble.py", [str(output)], log)
        if code == 0:
            vcode = run_visual_style(tender_dir, log, vid)
            if vcode != 0:
                log.warning(f"[{vid or '单份'}] 视觉编排重放失败，保留重装产物继续")
        return code

    for round_idx in range(1, max_rounds + 1):
        report = _read_verify_report(output)
        if not report:
            log.error("verify gate: verify_report.json 缺失,无法自动处理")
            return False
        fails = [f for f in report.get("findings", []) if f.get("severity") == "fail"]
        if not fails:
            log.info("verify gate ✓ 无硬失败")
            return True
        humans = [f for f in fails if f.get("class") not in fixable]
        if humans:
            for f in humans[:5]:
                log.error(f"  verify 需人工: [{f.get('class')}] {f.get('check')}: "
                          f"{str(f.get('detail'))[:100]}")
            return False

        acted = False
        for f in fails:
            cls = f.get("class")
            vid = f.get("variant") or "单份"
            key = (cls, vid)
            if attempts.get(key, 0) >= int((fixable.get(cls) or {}).get("max", 1)):
                continue
            attempts[key] = attempts.get(key, 0) + 1
            acted = True
            log.warning(f"verify gate: 修复 [{cls}] {vid}(第 {attempts[key]} 次)")
            if cls == "cover_missing":
                code = _reassemble(vid)
            elif cls == "page_over_cap":
                # 精简比例 = 硬顶/实测页数,留 5% 余量;精简后 docx 已 stale,必须重装
                ratio = 0.85
                cap = report.get("page_cap")
                pages = ((report.get("per_variant") or {}).get(vid) or {}).get("pages")
                if cap and pages:
                    ratio = max(0.5, round(cap / pages * 0.95, 2))
                cmd = [str(tender_dir), "--ratio", str(ratio)]
                if vid != "单份":
                    cmd += ["--variant", vid]
                code = run_script("bid_concise.py", cmd, log)
                if code == 0:
                    code = _reassemble(vid)
            else:
                continue
            decisions.append({"gate": "verify", "class": cls, "variant": vid,
                              "attempt": attempts[key], "exit_code": code,
                              "at": datetime.now().isoformat(timespec="seconds")})
        if not acted:
            log.error("verify gate: 剩余硬失败均已达修复上限,放弃")
            return False
        run_script("bid_verify.py", [str(tender_dir)], log)

    report = _read_verify_report(output)
    fails = [f for f in report.get("findings", []) if f.get("severity") == "fail"]
    if fails:
        log.error(f"verify gate ✗ 终态仍有 {len(fails)} 项硬失败")
        return False
    log.info("verify gate ✓ 终态复验通过")
    return True


def _read_compliance_scans(output: Path, mode: str) -> dict[str, dict]:
    """返回 {variant_id|'单份': scan}。扫描固定在 <章节目录>/reports/compliance_scan.json
    (旧项目兼容根目录平铺路径)。"""
    scans: dict[str, dict] = {}

    def _load(p_new: Path, p_old: Path) -> dict | None:
        for p in (p_new, p_old):
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
        return None

    if mode == "batch":
        for v_dir in sorted(output.iterdir()):
            if not (v_dir.is_dir() and re.fullmatch(r"V\d{2}", v_dir.name)):
                continue
            scan = _load(v_dir / "reports" / "compliance_scan.json",
                         v_dir / "compliance_scan.json")
            if scan is not None:
                scans[v_dir.name] = scan
    else:
        scan = _load(output / "reports" / "compliance_scan.json",
                     output / "compliance_scan.json")
        if scan is not None:
            scans["单份"] = scan
    return scans


def _classify_compliance(scan: dict) -> list[dict]:
    """scan → [{class, detail, ...}];分支与 bid_compliance 的 has_violation 一一对应。"""
    issues: list[dict] = []
    for b in scan.get("blind_violations") or []:
        issues.append({"class": "blind_violation", "detail": str(b)[:120]})
    for r in scan.get("response_check") or []:
        if r.get("required") and not r.get("present"):
            issues.append({"class": "response_keyword_miss",
                           "detail": f"{r.get('label')}:{r.get('keyword')}",
                           "label": str(r.get("label") or ""),
                           "keyword": str(r.get("keyword") or "")})
    for a in scan.get("plan_alignment") or []:
        if not a.get("file_exists") or not a.get("title_match"):
            issues.append({"class": "alignment_failure",
                           "detail": str(a.get("file") or a)[:120]})
    cc = scan.get("chart_coverage") or {}
    if cc.get("loaded"):
        for m in cc.get("missing") or []:
            issues.append({"class": "chart_missing",
                           "chapter": str(m.get("chapter") or ""),
                           "detail": f"章{m.get('chapter')}.{m.get('section')} "
                                     f"type={m.get('type')}"})
    if (scan.get("format_audit") or {}).get("exit_code", 0) >= 2:
        issues.append({"class": "format_severe", "detail": "DOCX 格式实测层硬违规"})
    if (scan.get("standards_check") or {}).get("fabricated", 0) > 0:
        issues.append({"class": "standards_fabricated", "detail": "标准编号疑似杜撰"})
    for v in (scan.get("schedule_check") or {}).get("violations") or []:
        detail = str(v.get("detail") if isinstance(v, dict) else v)[:120]
        kind = v.get("kind") if isinstance(v, dict) else ""
        if kind == "正文日期泄漏":
            # 正文日期泄漏可由章节 --force 重写自动修复（与 response_keyword_miss 同类），
            # 单独成类避免被 schedule_violation 一刀切转人工。
            fname = str(v.get("file") or "")
            m = re.match(r"^(\d{1,2})_", fname)
            issues.append({"class": "schedule_date_leak",
                           "chapter": m.group(1) if m else "",
                           "detail": detail})
        else:
            issues.append({"class": "schedule_violation", "detail": detail})
    return issues


def _pick_chapter_for_keyword(tender_dir: Path, vid: str, label: str, keyword: str) -> str:
    """response_check 是全文级检查,无章节定位 → 按标题 bigram 重合度选最相关章节。"""
    from bid_common import parse_chapter_plan
    rows = []
    if vid and vid != "单份":
        vp = tender_dir / "output" / vid / "chapter_plan_variant.md"
        if vp.exists():
            rows = parse_chapter_plan(vp)
    if not rows:
        rows = parse_chapter_plan(tender_dir / "extracted" / "chapter_plan.md")
    if not rows:
        return ""
    probe = "".join(c for c in f"{label}{keyword}" if "一" <= c <= "鿿")

    def score(row) -> int:
        title = row.get("title", "")
        return sum(1 for i in range(len(probe) - 1) if probe[i:i + 2] in title)

    best = max(rows, key=score)
    if score(best) <= 0:
        return ""  # 无 bigram 命中:返回空让调用方跳过重写(误写第1章只会空耗重试)
    return best["num"]


def enforce_compliance_gate(tender_dir: Path, mode: str, state: dict, log, policy: dict) -> bool:
    """compliance 违规的自动修复环(--loop-mode)。

    response_keyword_miss / chart_missing → 定位目标章节 --force 重写(默认 1 次,
    重写本身是占位/泄漏源)→ bid_reviewer 复扫 → 重装该变体(docx 已 stale)→ 复查。
    blind/alignment/format/standards/schedule → False 转人工。
    """
    output = tender_dir / "output"
    cp = (policy.get("retry_policy") or {}).get("compliance") or {}
    fixable = cp.get("auto_fixable") or {}
    max_rounds = int(cp.get("max_retries", 1))
    decisions = state["steps"].setdefault("compliance", {}).setdefault("gate_decisions", [])
    attempts: dict[tuple, int] = {}

    def _all_issues() -> list[tuple[str, dict]]:
        scans = _read_compliance_scans(output, mode)
        return [(vid, i) for vid, scan in scans.items() for i in _classify_compliance(scan)]

    for round_idx in range(1, max_rounds + 1):
        issues = _all_issues()
        if not issues:
            log.info("compliance gate ✓ 无违规")
            return True
        humans = [(v, i) for v, i in issues if i["class"] not in fixable]
        if humans:
            for v, i in humans[:8]:
                log.error(f"  compliance 需人工: [{i['class']}] {v}: {i['detail']}")
            return False

        acted = False
        unlocatable: list[str] = []
        for vid, issue in issues:
            cls = issue["class"]
            cn = issue.get("chapter") or _pick_chapter_for_keyword(
                tender_dir, vid, issue.get("label", ""), issue.get("keyword", ""))
            if not cn:
                # 无 bigram 命中→定位不到章节:跳过本项(不是整轮门禁失败)。
                # 旧代码直接 return False,会让一个定位不着的关键词杀掉整轮——
                # 同轮其它可重写项(如 chart_missing)连带被放弃。
                log.warning(f"  compliance: [{cls}] {vid} 无法定位目标章节,跳过本项")
                unlocatable.append(f"[{cls}] {vid}: {issue.get('detail', '')[:60]}")
                continue
            cn = cn.zfill(2)
            key = (cls, vid, cn)
            if attempts.get(key, 0) >= int((fixable.get(cls) or {}).get("max", 1)):
                continue
            attempts[key] = attempts.get(key, 0) + 1
            acted = True
            log.warning(f"compliance gate: [{cls}] {vid} 章 {cn} --force 重写"
                        f"(第 {attempts[key]} 次)")
            wcmd = [str(tender_dir), "--chapter", cn, "--force"]
            rcmd = [str(tender_dir)]
            if vid != "单份":
                wcmd += ["--variant", vid]
                rcmd += ["--variant", vid]
            code = run_script("bid_writer.py", wcmd, log)
            if code != 0:
                # 与 review gate 同口径:执行失败(瞬态 LLM 抖动)不耗重试预算,
                # 否则单次抖动就吃光唯一一次重试预算 → 门禁直接 abort
                attempts[key] = attempts[key] - 1
                log.error(f"  [{cls}] {vid} 章 {cn} 重写失败 exit={code},本轮跳过(不耗预算)")
                continue
            # 重写后必须复扫(占位符/参数泄漏历史上正是重写引入的)+ 重装(docx 已 stale)
            # 单份模式补 --single(与管线 review 步一致,否则批量语义扫不到章节、覆盖空汇总)
            if mode == "single" and "--variant" not in rcmd:
                rcmd = rcmd + ["--single"]
            run_script("bid_reviewer.py", rcmd, log)
            if vid != "单份":
                acode = run_script("bid_assemble.py", [str(output), "--variant", vid], log)
            else:
                acode = run_script("bid_assemble.py", [str(output)], log)
            if acode == 0:
                run_visual_style(tender_dir, log, vid)
            decisions.append({"gate": "compliance", "class": cls, "variant": vid,
                              "chapter": cn, "attempt": attempts[key], "exit_code": code,
                              "at": datetime.now().isoformat(timespec="seconds")})
        if not acted:
            if unlocatable:
                log.error(f"compliance gate: {len(unlocatable)} 项无法定位目标章节且无可重写项,转人工: "
                          + "; ".join(unlocatable[:5]))
            else:
                log.error("compliance gate: 违规均已达重写上限,放弃")
            return False
        # 上面按 --variant 复扫会把 review_summary.json 覆盖成单变体,补一次全量
        # reviewer 复原汇总(summary 步骤的项目总览/review_summary.md 才不缺变体)
        _rcmd = [str(tender_dir)] + (["--single"] if mode == "single" else [])
        if run_script("bid_reviewer.py", _rcmd, log) >= 2:
            log.error("compliance gate: 全量 reviewer 复原运行失败,保持违规判定")
            return False
        cmd = [str(tender_dir)]
        if mode == "batch":
            cmd.append("--all-variants")
        run_script("bid_compliance.py", cmd, log)

    remaining = _all_issues()
    if remaining:
        log.error(f"compliance gate ✗ 终态仍有 {len(remaining)} 项违规")
        return False
    log.info("compliance gate ✓ 终态复查通过")
    return True


def enforce_assemble_gate(tender_dir: Path, assemble_cmd: list[str],
                          state: dict, log, policy: dict) -> bool:
    """assemble 非零退出(pandoc 偶发/超时)→ backoff 后原命令重跑。"""
    ap = (policy.get("retry_policy") or {}).get("assemble") or {}
    max_retries = int(ap.get("max_retries", 2))
    backoff = int(ap.get("backoff_seconds", 15))
    decisions = state["steps"].setdefault("assemble", {}).setdefault("gate_decisions", [])
    for n in range(1, max_retries + 1):
        log.warning(f"assemble gate: {backoff}s 后重试(第 {n}/{max_retries} 次)")
        time.sleep(backoff)
        code = run_script("bid_assemble.py", assemble_cmd, log)
        decisions.append({"gate": "assemble", "attempt": n, "exit_code": code,
                          "at": datetime.now().isoformat(timespec="seconds")})
        if code == 0:
            return True
    return False


def _check_budget(output: Path, policy: dict, run_t0: float, log) -> bool:
    """--loop-mode 每步后调用。读 output/logs/llm_stats.json(writer/concise 分桶 flush),
    超 caps 且 action_on_exceed=abort → False(run_pipeline 返回 3 熔断)。
    覆盖范围:writer/concise 为成本大头;synthesize 链小调用未计(policy.coverage_note)。
    """
    bp = (policy or {}).get("budget_policy") or {}
    if not bp:
        return True
    pricing = bp.get("pricing_yuan_per_mtok") or {}
    default_pr = pricing.get("default") or {"in": 4.0, "out": 12.0}
    total = 0
    cost = 0.0
    p = output / "logs" / "llm_stats.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for s in (data.get("buckets") or {}).values():
            for model, d in (s.get("by_model") or {}).items():
                pi, po = int(d.get("prompt", 0)), int(d.get("completion", 0))
                total += pi + po
                pr = next((v for k, v in pricing.items()
                           if k != "default" and k in model), default_pr)
                cost += pi / 1e6 * float(pr.get("in", 4.0)) + po / 1e6 * float(pr.get("out", 12.0))
    wall_min = (time.time() - run_t0) / 60
    over = []
    if bp.get("max_tokens_per_run") and total > int(bp["max_tokens_per_run"]):
        over.append(f"tokens {total:,} > {int(bp['max_tokens_per_run']):,}")
    if bp.get("max_cost_yuan") and cost > float(bp["max_cost_yuan"]):
        over.append(f"费用 ¥{cost:.1f} > ¥{bp['max_cost_yuan']}")
    if bp.get("max_wall_clock_minutes") and wall_min > float(bp["max_wall_clock_minutes"]):
        over.append(f"耗时 {wall_min:.0f}min > {bp['max_wall_clock_minutes']}min")
    if not over:
        return True
    msg = "预算超限: " + "; ".join(over)
    if (bp.get("action_on_exceed") or "abort") == "abort":
        log.error(f"⛔ {msg} — 熔断(exit 3)")
        return False
    log.warning(f"⚠ {msg}(action=warn,继续)")
    return True


def reseed_variant(variants_json: Path, variant_id: str, log) -> None:
    """把指定 variant 的 section_order_seed 与常量异或，持久化回 JSON。"""
    if not variants_json.exists():
        return
    payload = json.loads(variants_json.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        vlist = payload.get("variants") or []
    elif isinstance(payload, list):  # 兼容裸数组形式
        vlist, payload = payload, {"variants": payload}
    else:
        vlist = []
    for v in vlist:
        if v.get("id") == variant_id:
            try:
                old = int(v.get("section_order_seed", 0) or 0)
            except (TypeError, ValueError):
                old = 0
            # 累加而非自异或(XOR 再跑一次会翻回原值,重试≥3次结构复现,相似度永不收敛)
            v["section_order_seed"] = old + 0x9E3779B9
            v.setdefault("_reseed_count", 0)
            v["_reseed_count"] += 1
            log.info(f"  reseed {variant_id}: {old} → {v['section_order_seed']}(第{v['_reseed_count']}次)")
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
        _chs = [c for c in (v.get("chapters") or []) if isinstance(c, dict)]
        worst = max(_chs, key=lambda c: sev_rank.get(c.get("severity", "ok"), 0), default=None)
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
        scan = v_dir / "reports" / "compliance_scan.json"
        if not scan.exists():
            scan = v_dir / "compliance_scan.json"  # 旧项目兼容
        if not scan.exists():
            continue
        try:
            data = json.loads(scan.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"  {scan.name} 解析失败,跳过该变体汇总: {e}")
            continue
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
        try:
            vs = json.loads(vj.read_text(encoding="utf-8"))
            if isinstance(vs, list):
                vs = {"variants": vs, "project_name": None, "variant_count": len(vs)}
        except (json.JSONDecodeError, OSError):
            vs = None
    lines = [
        f"# 项目总览 - {vs.get('project_name') if vs else tender_dir.name}", "",
        f"**模式：** {'批量' if mode == 'batch' else '单份'}  ",
        f"**生成时间：** {datetime.now().isoformat(timespec='seconds')}  ", "",
    ]
    # 清单锚点体检(2026-08-19 写作质量整改):无锚点项目历史 P/G 普遍<0.5
    _anchor_warns: list[str] = []
    _ar = ext / "authoring_rules.md"
    if _ar.exists() and "工程构成自动派生失败" in _ar.read_text(errors="ignore"):
        _anchor_warns.append("⚠ 工程构成自动派生失败（boq.json 缺失或为空）——方案写作缺少清单锚点，请人工核查 authoring_rules.md ## 6 节")
    if not (ext / "boq.json").exists():
        _anchor_warns.append("⚠ extracted/boq.json 不存在——历史低质项目多源于此；建议检查 Step 1 清单提取")
    if _anchor_warns:
        lines.extend(["## ⚠ 写作锚点体检", ""] + [w + "  " for w in _anchor_warns] + [""])
    if mode == "batch" and vs:
        sim_path = output / "similarity_matrix.json"
        worst_pair = None
        if sim_path.exists():
            mat = json.loads(sim_path.read_text(encoding="utf-8"))
            pairs = mat.get("pairs", [])
            if pairs:
                _dpairs = [p for p in pairs if isinstance(p, dict)]
                worst_pair = max(_dpairs, key=lambda p: max(p.get("overall") or 0, p.get("paragraph_max") or 0)) if _dpairs else None
        lines.extend([
            f"**单位数：** {vs.get('variant_count') or len(vs.get('variants', []))}  ",
            f"**模型池：** {', '.join(vs.get('model_pool', []) or ['-'])}  ",
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
                    # 只统计正文章节。review/expert_review 是评审报告,chapter_plan_variant 是计划,
                    # 都不是投标正文——混进来会让总览字数虚高(实测 V01 31,216 → 真实 25,779,
                    # 虚高 5,437 字全部来自 expert_review.md),直接误导页数预算判断。
                    if md.name in ("review.md", "chapter_plan_variant.md", "expert_review.md"):
                        continue
                    total_chars += sum(1 for c in md.read_text(encoding="utf-8") if "\u4e00" <= c <= "\u9fff")
            rev_sev = rev_map.get(vid, {}).get("overall_severity", "-")
            scan = v_dir / "reports" / "compliance_scan.json"
            if not scan.exists():
                scan = v_dir / "compliance_scan.json"  # 旧项目兼容
            comp = "-"
            if scan.exists():
                try:
                    d = json.loads(scan.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    d = None
                if not isinstance(d, dict):
                    comp = "-(损坏)"  # 解析失败:如实标注,防误显示✓
                else:
                    comp = "✓" if not d.get("blind_violations") and all(
                        r.get("present") for r in d.get("response_check", []) if r.get("required")
                    ) else "❌"
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

REQUIRED_PKGS = ["pdfplumber", "openpyxl", "docx", "openai", "PIL"]
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
              "bid_writer.py", "bid_data_anchor.py", "bid_reviewer.py", "bid_assemble.py", "bid_compliance.py",
              "bid_officecli_style.py", "bid_visual_render.py", "bid_visual_similarity.py",
              "bid_visual_rules.py", "bid_similarity.py", "bid_expert_review.py",
              "bid_llm_client.py", "bid_run.py",
              # 管线实际调用的其余脚本(历史缺失 → doctor 假阴性)
              "bid_boq_extract.py", "bid_rules_synthesize.py", "bid_plan_synthesize.py",
              "bid_plan_variants.py", "bid_plan_verify.py", "bid_chart_specs.py",
              "bid_verify.py", "bid_concise.py", "bid_h1_fix.py",
              "bid_chapter_intro.py", "bid_coherence_pass.py", "bid_difficulty_synthesize.py",
              "bid_deployment_synthesize.py", "bid_brain_translate.py", "bid_status.py"]:
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
    cfg_path = get_config_dir() / "bid_llm_config.json"
    if not cfg_path.exists():
        print(f"  {FAIL} {cfg_path} 不存在")
        issues.append("运行: python bid_llm_client.py --init")
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

    # 1.5 OfficeCLI（装配后视觉编排的硬依赖）
    try:
        officecli = shutil.which("officecli")
        fallback = Path.home() / ".local" / "bin" / "officecli"
        if not officecli and fallback.exists():
            officecli = str(fallback)
        if officecli:
            proc = subprocess.run([officecli, "--version"], capture_output=True,
                                  text=True, timeout=10)
            version = proc.stdout.strip() if proc.returncode == 0 else "unknown"
            print(f"  {OK} officecli — {version}")
        else:
            print(f"  {FAIL} officecli 未安装(视觉编排会失败)")
            issues.append("安装 OfficeCLI: curl -fsSL https://d.officecli.ai/install.sh | bash")
    except Exception as e:
        print(f"  {FAIL} officecli 检测异常: {e}")
        issues.append("修复 OfficeCLI 安装或 PATH")

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

    run_t0 = time.time()
    loop_mode = bool(getattr(args, "loop_mode", False))
    loop_policy = load_loop_policy() if loop_mode else {}
    if loop_mode:
        log.info("loop-mode 启用: verify/compliance/assemble 门禁自动重试 + 预算熔断")

    # 连贯性主线注入(散装→茅台改进):总开关 + 各工序开关
    coh_cfg = load_coherence_config()
    if coh_cfg.get("enabled"):
        log.info("coherence 启用: 工序A(deployment/plan/writer) + 工序B(chapter_intro) + 工序C(coherence_pass)")

    # 决定要跑哪些步骤
    mode = detect_mode(tender_dir) if (ext / "variants.json").exists() or args.batch else "single"
    # --batch 强制批量（需要先 variants）
    if args.batch:
        mode = "batch"

    all_steps = STEPS[:]
    # 单份模式跳过 variants / similarity
    if mode == "single":
        # single 剔除仅批量有意义的步(write 漂移 batch 时按 STEPS 原位补回)
        all_steps = [s for s in all_steps if s not in ("variants", "plan_variants", "similarity")]

    # --from / --to 切片
    def idx(step: str) -> int:
        try:
            return all_steps.index(step)
        except ValueError:
            return -1

    start_i = idx(args.from_step) if args.from_step else 0
    end_i = idx(args.to_step) + 1 if args.to_step else len(all_steps)
    # 步骤名校验(idx 返回 -1 时切片会静默只跑尾部,误导排障)
    if args.from_step and start_i < 0:
        log.error(f"--from 步骤名不存在: {args.from_step}  合法: {', '.join(all_steps)}")
        return 2
    if args.to_step and idx(args.to_step) < 0:
        log.error(f"--to 步骤名不存在: {args.to_step}  合法: {', '.join(all_steps)}")
        return 2
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
            # 用户显式 --from 优先(不与 resume 失败步取 max,保区间语义)
            start_i = start_i if args.from_step else max(start_i, i)
            log.info(f"resume: 从 step={s} 开始(上次状态={status or 'unrun'}"
                     + (f";用户指定 --from {args.from_step},以其为准" if args.from_step else "") + ")")
            break
        else:
            # 全部 ok:无显式区间 → 收工;有 --from/--to → 尊重用户指定的重跑区间
            if args.from_step or args.to_step:
                start_i = idx(args.from_step) if args.from_step else 0
                log.info(f"resume: 状态全 ok 但指定了区间 → 重跑 {args.from_step or '开头'}..{args.to_step or '结尾'}")
            else:
                log.info("resume: 所有步骤状态均为 ok，无需重跑(用 --force 或 --from 指定单步重跑)")
                return 0

    plan = all_steps[start_i:end_i]
    # assemble 之后视觉层与 compliance 均不可被 --to assemble 截断。
    if "assemble" in plan:
        required_after_assemble = ["visual_style", "visual_render", "visual_verify", "verify", "compliance"]
        missing = [s for s in required_after_assemble if s not in plan]
        if missing:
            plan = list(plan) + missing
            log.warning(f"assemble 后置步骤被 --to 截断，已强制追加: {', '.join(missing)}")
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
        has_authoring = (ext / "authoring_rules.md").exists()

        if not (has_brain and has_plan and has_rules and has_authoring):
            if getattr(args, "manual_step2", False):
                missing = []
                if not has_brain: missing.append("project_brain.md")
                if not has_plan:  missing.append("chapter_plan.md")
                if not has_rules: missing.append("disqualification_rules.md")
                # ★ authoring_rules.md 是规则化产物,手工模式不强制(旧项目的“手工三件套”
                #   仍能跑;writer 会回退到 disqualification_rules.md 路径)。
                #   若把它算进 missing,既有手工项目会直接 exit 2。
                if missing:
                    log.error(f"Step 2 未完成 (--manual-step2)：缺少 {missing}")
                    log.error("请让 Claude 生成这些文件后再 --resume")
                    return False
                if not has_authoring:
                    # 手工三件套齐备、只缺规则化产物 → 直接接受。
                    # ★ 必须在这里 return:否则会落进下面的自动 synthesize 链,
                    #   以 --force 覆盖用户手写的 brain/plan/rules(与"手工三件套仍能跑"矛盾)
                    log.warning("  authoring_rules.md 缺失(--manual-step2 不阻断,不触发自动合成),"
                                "writer/reviewer 将回退到 disqualification_rules.md")
                    return True

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

            # 1.5 deployment_synthesize (连贯性主线注入工序A1;依赖 difficulty,被 plan 消费)
            # config bid_coherence.json enabled=false 时跳过(灰度)
            coh_cfg = load_coherence_config()
            if coh_cfg.get("enabled"):
                dcode = run_script("bid_deployment_synthesize.py",
                                   [str(tender_dir), "--force"], log)
                if dcode not in (0, 1):
                    log.warning(f"  deployment_synthesize 失败 code={dcode},"
                                f"连贯性主线降级(plan/writer 走旧逻辑)")
            else:
                log.info("  coherence 未启用(bid_coherence.json enabled=false),跳过 deployment_synthesize")

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
            log.info("  ✓ Step 2 三件套自动生成完成")

        ready, issues = check_step2_ready(tender_dir)
        if not ready:
            log.error(f"Step 2 校验失败: {issues}")
            log.error("如规则化输出不达标,可用 --manual-step2 走 Claude 手工路径")
            return False
        return True

    # single→batch 漂移补步队列(不在迭代中改 plan,防排序错位/重复执行)
    _pending_steps: list[str] = []
    _step_queue: list[str] = list(plan)
    while _step_queue or _pending_steps:
        # ★ 补步正常在循环顶部按 STEPS 原位插入 queue;若此刻 queue 已空
        #   (如 --to write / --from write --to write 走到末步),pending 步会被静默
        #   丢弃——既不执行也不写状态。此处兜底交给 queue 直接执行。
        if not _step_queue and _pending_steps:
            # 尊重 --to 边界:超出用户指定终止步的补步不得执行
            _tail = _pending_steps[0]
            if args.to_step and _tail in STEPS and args.to_step in STEPS \
                    and STEPS.index(_tail) > STEPS.index(args.to_step):
                _pending_steps.pop(0)
                continue
            _step_queue.append(_pending_steps.pop(0))
        step = _step_queue.pop(0)
        if _pending_steps and _pending_steps[0] == step:
            _pending_steps.pop(0)
        # 漂移补步:pending 步按 STEPS 原位插入 queue 头部之后的正确位置,
        # 保证 similarity 在 summary 前执行(总览需要相似度数据);
        # 尊重 --to 边界(end_i 相对当前 all_steps)
        if _pending_steps:
            # --to 边界按 STEPS 全序(end_i 相对初始 all_steps;漂移 append 不影响)
            if args.to_step:
                _to_name = args.to_step
                _allow = {s for s in _pending_steps
                          if s in STEPS and _to_name in STEPS
                          and STEPS.index(s) <= STEPS.index(_to_name)}
            else:
                _allow = set(_pending_steps)
            _ready = [s for s in _pending_steps if s in _allow]
            _pending_steps = [s for s in _pending_steps if s not in _ready]
            if _ready:
                for s in sorted(_ready, key=lambda x: STEPS.index(x) if x in STEPS else 99):
                    _pos = len(_step_queue)
                    for _qi, _qs in enumerate(_step_queue):
                        if _qs in STEPS and s in STEPS and STEPS.index(s) < STEPS.index(_qs):
                            _pos = _qi
                            break
                    _step_queue.insert(_pos, s)
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
                    save_state(state_path, state)
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_template_match.py", cmd, log)
                # 范本未命中也不阻断,只标记
                if code != 0:
                    log.warning(f"  template 未命中(code={code}),下游需完整跑 LLM")
                    mark_step(state, step, "skip", note="no_template_match")
                    save_state(state_path, state)
                    continue
            elif step == "facts":
                if (ext / "project_facts.json").exists() and not args.force:
                    log.info("  project_facts.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
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
                    save_state(state_path, state)
                    continue
                if not (ext / "project_facts.json").exists():
                    log.warning("  project_facts.json 不存在,跳过 derive")
                    mark_step(state, step, "skip", note="no_facts")
                    save_state(state_path, state)
                    continue
                code = run_script("bid_derive.py", [str(tender_dir)], log)
            elif step == "special":
                if (ext / "special_clauses.json").exists() and not args.force:
                    log.info("  special_clauses.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_special_extract.py", cmd, log)
                # special 抽取失败不阻断,Step 2 仍可继续
                if code != 0:
                    log.warning(f"  special 抽取失败(code={code}),继续往下走")
                    mark_step(state, step, "skip", note=f"special exit={code}")
                    save_state(state_path, state)
                    continue
            elif step == "input_assemble":
                if (ext / "project_input.json").exists() and not args.force:
                    log.info("  project_input.json 已存在,跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
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
                    save_state(state_path, state)
                    continue
                cmd = [str(tender_dir)]
                if args.force:
                    cmd.append("--force")
                code = run_script("bid_skeleton_extract.py", cmd, log)
                # bid_skeleton_extract.py: 0=通过, 1=已产出但有警告, 2=硬失败。
                if code == 1:
                    log.warning(
                        "  skeleton 提取完成但有警告，继续往下走。"
                        "请结合 extracted/skeleton_extract.log 人工核对。"
                    )
                    mark_step(state, step, "ok", exit_code=1, note="warnings")
                    save_state(state_path, state)
                    continue
                # 骨架硬失败 ≠ 流程中止;只警告,Step 2 校验时再提示
                if code != 0:
                    log.warning(
                        f"  skeleton 提取退出码 {code}(警告级,继续往下走)。"
                        f"Step 2 会再次校验缺失项。"
                    )
                    mark_step(state, step, "skip", note=f"exit={code} downgraded")
                    save_state(state_path, state)
                    continue
            elif step == "fingerprint":
                if not args.fingerprint:
                    log.info("  fingerprint 未显式启用（加 --fingerprint），跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                code = run_script("bid_fingerprint.py", [str(tender_dir)], log)
            elif step == "variants":
                # 已有则跳过；否则要求用户提供 --companies
                if (ext / "variants.json").exists():
                    log.info("  variants.json 已存在，跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                if not args.companies:
                    log.error("批量模式但 variants.json 不存在，且未提供 --companies")
                    mark_step(state, step, "fail", error="missing --companies")
                    save_state(state_path, state)
                    return 2
                # ★ variants 必须读 rules:blind 由 needs_blind_scan(rules) 判定,
                # format_sets 也要从 rules 探测。首跑时 rules 只在后面的 write 步
                # ensure_step2() 才生成 → 此处 rules 缺失会让 needs_blind_scan 直接返回
                # False(暗标项目变体 blind=false → writer 不脱敏公司名 = A1.13 级废标),
                # 且 format_sets 探测为空 → 仅回退 A/B/C。先补齐 Step 2 再生成变体。
                if not ensure_step2():
                    mark_step(state, step, "fail", error="step2 not ready")
                    save_state(state_path, state)
                    return 2
                code = run_script("bid_variants.py", [
                    str(tender_dir), "--companies", args.companies
                ], log)
            elif step == "chart_specs":
                # 从 chapter_plan.md 派生 chart_specs.json,可选,失败也不阻断
                if not (ext / "chapter_plan.md").exists() and not getattr(args, "manual_step2", False):
                    # 首跑自动链:chapter_plan.md 只在 write 步 ensure_step2 才生成——
                    # 这里先补合成,防 chart_specs 静默 skip → 首跑无精确图表规格且已写章节无法回填
                    # ★ 必须走完整 ensure_step2()(difficulty→brain_translate→rules→plan),
                    # 不能只调 plan_synthesize:后者读不到 rules 里的字号/行距,会退回 12pt 默认值
                    # (本类项目招标实测宋体四号 14pt)→ 字数密度按 850-950 估而非 400-450,
                    # 目标字数近乎翻倍 → 写完必超页废标。谢通门周转房(2026-09)踩过此坑。
                    if not ensure_step2() or not (ext / "chapter_plan.md").exists():
                        log.info("  chapter_plan.md 仍缺失,跳过 chart_specs")
                        mark_step(state, step, "skip")
                        save_state(state_path, state)
                        continue
                elif not (ext / "chapter_plan.md").exists():
                    log.info("  chapter_plan.md 不存在(--manual-step2),跳过 chart_specs")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                if (ext / "chart_specs.json").exists() and not args.force:
                    log.info("  chart_specs.json 已存在,跳过(--force 重派生)")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                code = run_script("bid_chart_specs.py", [str(tender_dir)], log)
                # 派生失败仅警告,不阻断
            elif step == "plan_variants":
                # 为每个变体生成独立的标题计划 + 图表分配
                if mode != "batch":
                    log.info("  单份模式，跳过 plan_variants")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                # ★ plan_variants 依赖 chapter_plan.md —— 全新自动合成 run 上它只在
                # 后面的 write 步 ensure_step2() 里才生成;不提前保证则本步必失败且被
                # 降级 skip 掩盖(某县案例:变体差异化 H2 从未生效的直接根因)
                # 瞬态失败(如 LLM 抖动)不永久中止:标记 fail 交由 --resume/loop 重试,
                # 仅在硬缺失(手工模式下缺文件)时 return 2
                if not (ext / "chapter_plan.md").exists() and not getattr(args, "manual_step2", False):
                    # 同上:必须走完整 ensure_step2(),只调 plan_synthesize 会丢 brain/rules
                    _s2 = 0 if ensure_step2() else 1
                    if _s2 != 0:
                        log.warning("  plan_variants: Step 2 自动合成失败,降级为共享基准计划"
                                    "(变体 H2 差异化待 --resume 重试)")
                        mark_step(state, step, "skip", note="step2 synth failed")
                        save_state(state_path, state)
                        continue
                elif getattr(args, "manual_step2", False) and not (ext / "chapter_plan.md").exists():
                    log.error("  plan_variants: --manual-step2 且 chapter_plan.md 缺失")
                    mark_step(state, step, "fail", error="step2 not ready")
                    save_state(state_path, state)
                    return 2
                # 加载 variants 列表(裸数组形状兼容,与 variant_count/reseed 同口径)
                _vj = ext / "variants.json"
                if _vj.exists():
                    _vp = json.loads(_vj.read_text(encoding="utf-8"))
                    _vlist = _vp.get("variants", []) if isinstance(_vp, dict) else (_vp if isinstance(_vp, list) else [])
                else:
                    _vlist = []
                if not _vlist:
                    log.error("  variants.json 无变体条目(空/损坏)——先重跑 bid_variants.py")
                    mark_step(state, step, "fail", error="empty variants list")
                    save_state(state_path, state)
                    return 2
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
                    save_state(state_path, state)
                    continue
                code = run_script("bid_plan_variants.py", [str(tender_dir)], log)
                # 派生失败仅警告,不阻断
                if code != 0:
                    log.warning(f"  plan_variants 生成失败 code={code}，降级为共享基准计划（变体 H2 差异化失效，建议检查 chapter_plan/LLM 配置）")
                    mark_step(state, step, "skip", note=f"derive failed code={code}")
                    save_state(state_path, state)
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
                # 统一数据锚点:write 前自动合成(工期/规模/质量/子工程量/控制价/海拔),
                # 强制全文硬数据一致、禁止 LLM 编造(原为手工 hook,现自动化)。
                # ★ 必传 --force:锚点是 facts/derived/boq 的派生结果,而三者在 extract/
                # synthesize 重跑后会变;不带 --force 则旧锚点长期降落,却被当成
                # “硬数据禁止不一致”注入每章 prompt → 全员按陈旧数字写。
                code = run_script("bid_data_anchor.py", [str(tender_dir), "--force"], log)
                if code != 0:
                    log.warning(f"  data_anchor 生成失败 code={code},writer 降级为无锚点(不阻断)")

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
                    # mode 漂到 batch 后补回被 single 剔除的 similarity 步
                    # (否则批量反串标相似度交叉检查被静默跳过;for 迭代器能看到 append)
                    if "similarity" not in plan and "similarity" not in all_steps:
                        _pending_steps.append("similarity")
                        if "similarity" not in all_steps:
                            all_steps.append("similarity")
                        log.info("  plan 已补回 similarity 步（single→batch 漂移）")
                    # plan_variants 同样被 single 剔除——write 步刚写完(章节已存在会被
                    # writer 跳过),故先刷新 H2 计划再 --force 重写本次章节,让差异化
                    # 对当次生成立即生效,而非等 queue 耗尽后的"下一轮"。
                    # ★ 但仅 1 家时无差异化可言:H2 差异化是反串标手段,单份项目重写整批
                    #   章节 = 写作成本/耗时翻倍且首遍产物全丢,重写失败还会把已成功的
                    #   write 步标 fail 并 abort。少于 2 家直接跳过漂移重写。
                    #   注:漂移只可能由单份模式自动生成的单变体 variants.json 触发,
                    #   故下面 _n_var>=2 的分支实际只在“用户手工补入了多份 variants.json”
                    #   时才会走到——保留它作为该场景的兜底,而非死代码。
                    _n_var = variant_count(tender_dir)
                    if _n_var < 2:
                        log.info(f"  仅 {_n_var} 家投标,无反串标差异化需求,跳过 single→batch 漂移重写")
                    elif "plan_variants" not in plan and "plan_variants" not in all_steps:
                        _pv_code = run_script("bid_plan_variants.py", [str(tender_dir)], log)
                        if _pv_code == 0:
                            # 已生成章节覆盖重写(带上新差异化 H2);失败即中止保状态一致
                            _w_code = run_script("bid_writer.py", [str(tender_dir), "--force"], log)
                            if _w_code == 0:
                                log.info("  plan_variants 已即时补跑并重写章节（single→batch 漂移）")
                            else:
                                log.error(f"  漂移重写失败 exit={_w_code},回滚为跳过(下轮 --resume)")
                                mark_step(state, "write", "fail", error=f"drift rewrite exit={_w_code}")
                                save_state(state_path, state)
                                return 1
                            if "plan_variants" not in all_steps:
                                all_steps.append("plan_variants")
                            if ("plan_variants" not in _step_queue
                                    and "plan_variants" not in _pending_steps):
                                _step_queue.append("plan_variants")
                        else:
                            log.warning("  plan_variants 即时补跑失败,变体 H2 差异化待下轮")
                            state["steps"].setdefault("plan_variants", {})["note"] = \
                                "drift rerun failed (single→batch); H2 差异化未生效,人工核验"
                            save_state(state_path, state)
                # 写完后自动修 H1 字面(feedback_writer_h1_drift)
                if code == 0:
                    log.info("  post-writer: 自动修正 H1 字面...")
                    run_script("bid_h1_fix.py", [str(tender_dir)], log)
                    # 连贯性工序B:重写每章引言回指主线(coherence config 控制)
                    if coh_cfg.get("enabled") and (coh_cfg.get("chapter_intro", {}) or {}).get("enabled", True):
                        log.info("  post-writer: 连贯性工序B 章首引言重写...")
                        run_script("bid_chapter_intro.py", [str(tender_dir)], log)
            elif step == "review":
                cmd = [str(tender_dir)]
                if mode == "single":
                    cmd.append("--single")
                code = run_script("bid_reviewer.py", cmd, log)
                # 质量门禁(single 与 batch 同权:major 自动重写,--force-continue 跳过)
                if args.force_continue:
                    # --force-continue 语义=跳过门禁重试;退出码1(major)降级继续,≥2(运行失败)仍拦
                    if code == 1:
                        log.warning("review 有 major 但 --force-continue:跳过门禁继续(风险自负)")
                        code = 0
                elif not enforce_review_gate(tender_dir, state, log):
                    log.error("review gate 未通过，abort（可加 --force-continue 跳过）")
                    mark_step(state, step, "fail", error="review gate failed")
                    save_state(state_path, state)
                    return 1
                else:
                    # 门禁通过 → moderate/minor 不阻断，强制 code=0 继续
                    code = 0
            elif step == "coherence_pass":
                # 连贯性工序C:assemble 前跨章勾兑(引用编织+重复检测+矛盾修复)
                # coherence config 未启用时直接 skip(灰度)
                if not coh_cfg.get("enabled") or not (coh_cfg.get("coherence_pass", {}) or {}).get("enabled", True):
                    log.info("  coherence_pass 未启用,跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                code = run_script("bid_coherence_pass.py", [str(tender_dir)], log)
                # 工序C 失败不阻断 assemble(只警告,降级为"未勾兑")
                if code != 0:
                    log.warning(f"  coherence_pass 退出码 {code},降级跳过(不阻断 assemble)")
                    code = 0
            elif step == "regroup":
                # 合格制批量:二级标题数量/层级差异化(目录指纹族第三层;
                # memory/feedback_bid_page_budget_and_fingerprint.md)
                # 单份模式无意义;批量下失败不阻断 assemble(只告警)。
                if mode != "batch":
                    log.info("  单份模式,regroup 跳过")
                    mark_step(state, step, "skip")
                    save_state(state_path, state)
                    continue
                code = run_script("bid_regroup.py", [str(tender_dir)], log)
                if code != 0:
                    log.warning(f"  regroup 退出码 {code},降级跳过(目录未做层级差异化)")
                    code = 0
            elif step == "assemble":
                cmd = [str(tender_dir / "output")]
                if mode == "batch":
                    cmd.append("--batch")
                code = run_script("bid_assemble.py", cmd, log)
                if code != 0 and loop_mode and not args.force_continue:
                    if enforce_assemble_gate(tender_dir, cmd, state, log, loop_policy):
                        code = 0
            elif step == "visual_style":
                code = run_script("bid_officecli_style.py", [str(tender_dir)], log)
                if code != 0:
                    # 视觉层失败(如 officecli 未装)不阻断管道,与 visual_render/
                    # visual_verify 的降级哲学一致——DOCX 本身仍有效,只是没套视觉 profile
                    log.warning("视觉编排未应用（officecli 不可用或执行失败），保留组装产物继续")
                    code = 0
            elif step == "visual_render":
                code = run_script("bid_visual_render.py", [str(tender_dir)], log)
                if code != 0:
                    log.warning("视觉联系表生成不完整，保留结构校验结果并继续")
                    code = 0
            elif step == "visual_verify":
                vcode = run_script("bid_visual_similarity.py", [str(tender_dir)], log)
                if vcode == 1:
                    log.warning("视觉相似度有告警（首期 warning-only），继续流水线")
                    code = 0
                elif vcode >= 2:
                    # 硬失败:记 fail 不阻断;continue 前仍执行预算熔断(与其他失败路径一致)
                    log.error(f"视觉相似度运行失败 exit={vcode}(warning-only 首期,记 fail 不 abort)")
                    mark_step(state, step, "fail", error=f"exit={vcode}")
                    save_state(state_path, state)
                    if loop_mode and not _check_budget(output, loop_policy, run_t0, log):
                        mark_step(state, step, "fail", error="budget exceeded")
                        save_state(state_path, state)
                        return 3
                    continue
                else:
                    code = 0
            elif step == "toc_fill":
                # 静态目录回填真实页码。失败/无 Word 不阻断(toc_fill 内部已降级为“—”)。
                code = run_script("bid_toc_fill.py", [str(tender_dir)], log)
                if code != 0:
                    log.warning(f"  toc_fill 退出码 {code}，目录页码未回填（目录仍可读）")
                    code = 0
            elif step == "verify":
                # 组装产物验证门:exit 2=硬失败(封面缺失/三家排版雷同/超硬顶)→ abort
                vcode = run_script("bid_verify.py", [str(tender_dir)], log)
                if vcode == 2 and loop_mode and not args.force_continue:
                    if enforce_verify_gate(tender_dir, state, log, loop_policy):
                        vcode = 1  # 自动修复通过,余项降级为警告继续
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
                # exit 1=合规违规(可分类处理);exit 2=用法/结构错误,不进门禁
                if code == 1 and loop_mode and not args.force_continue:
                    if enforce_compliance_gate(tender_dir, mode, state, log, loop_policy):
                        code = 0
            elif step == "similarity":
                n_variants = variant_count(tender_dir)
                if n_variants < 2:
                    log.info(f"  仅 {n_variants} 个 variant，跳过 similarity 交叉检查")
                    code = 0
                else:
                    code = run_script("bid_similarity.py", [str(tender_dir)], log)
                    if args.force_continue:
                        log.warning("--force-continue: 跳过 similarity 门禁(风险自负)")
                        code = 0
                    elif not enforce_similarity_gate(tender_dir, state, log):
                        log.error("similarity gate 未通过，abort")
                        mark_step(state, step, "fail", error="similarity gate failed")
                        save_state(state_path, state)
                        return 1
                    else:
                        # 门禁已自动重写并复测通过 → 归零,否则 bid_similarity 的 exit1
                        # (仍有超阈对,已修)会把步骤记 fail → 下面 return code 直接 abort 整链
                        code = 0
            elif step == "expert_review":
                # 评标专家：对照评分表逐项瑕疵制打分 + 符合性/跨章一致性，只出报告，恒不阻断
                code = run_script("bid_expert_review.py", [str(tender_dir)], log)
                if code != 0:
                    log.warning(f"expert_review 退出码 {code}，但不阻断管道")
                    code = 0
            elif step == "summary":
                write_summary_reports(tender_dir, mode, log)
                code = 0
            else:
                log.warning(f"未知步骤 {step}，跳过")
                mark_step(state, step, "skip")
                save_state(state_path, state)
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

        if loop_mode and not _check_budget(output, loop_policy, run_t0, log):
            mark_step(state, "budget", "fail", error="budget exceeded")
            save_state(state_path, state)
            return 3

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
    p.add_argument("--loop-mode", action="store_true",
                   help="启用 verify/compliance/assemble 门禁自动重试 + 预算熔断"
                        "(策略: configs/bid_loop_policy.json)")
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
