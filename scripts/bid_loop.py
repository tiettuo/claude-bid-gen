#!/usr/bin/env python3
"""循环编排器:包装 bid_run.py --loop-mode。

职责:
  策略加载 → LoopState → 子进程跑管道(重试一律 --resume,绝不全量重跑)
  → 退出码分类 → 失败进 JSONL 台账(bid_failures)→ 飞书告警(bid_escalation)
  → 写 LoopState。

bid_run 退出码约定:
  0 成功 / 2 环境硬失败(指纹不匹配/目录错,不重试) / 3 预算熔断(不重试)
  其余非零 = 步骤失败,可按 --max-cycles 重试。

用法:
  python bid_loop.py <tender_dir> [--policy PATH] [--max-cycles 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from bid_common import make_script_logger, get_bid_gen_home  # noqa: E402
from bid_escalation import notify  # noqa: E402
from bid_failures import append_failure  # noqa: E402

STATE_REL = Path("output") / ".bid_loop.state.json"
NO_RETRY_EXITS = {2: "hard_fail", 3: "budget_exceeded"}


def load_policy(path: Path | None = None) -> dict:
    p = path or (get_bid_gen_home() / "configs" / "bid_loop_policy.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class LoopState:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {"loop_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
                         "cycles": [], "last_result": None}

    def record(self, cycle: int, exit_code: int, seconds: float) -> None:
        self.data["cycles"].append({
            "cycle": cycle, "exit_code": exit_code, "seconds": round(seconds, 1),
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        self.data["last_result"] = "ok" if exit_code == 0 else f"exit_{exit_code}"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _failed_steps(tender_dir: Path) -> list[str]:
    p = tender_dir / "output" / ".bid_run.state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [k for k, v in (st.get("steps") or {}).items()
            if isinstance(v, dict) and v.get("status") == "fail"]


def run_once(tender_dir: Path, resume: bool, extra: list[str], log) -> int:
    cmd = [sys.executable, str(SCRIPTS / "bid_run.py"), str(tender_dir), "--loop-mode"]
    if resume:
        cmd.append("--resume")
    cmd += extra
    log.info("$ " + " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description="bid-gen 循环编排器")
    ap.add_argument("tender_dir", help="投标根目录")
    ap.add_argument("--policy", type=Path, default=None,
                    help="策略文件(默认 configs/bid_loop_policy.json)")
    ap.add_argument("--max-cycles", type=int, default=3,
                    help="管道级最大重试轮数(含首轮,默认 3)")
    ap.add_argument("--dry-run", action="store_true", help="只打印策略与状态,不跑子进程")
    args, extra = ap.parse_known_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    if not tender_dir.is_dir():
        print(f"目录不存在: {tender_dir}", file=sys.stderr)
        return 2
    output = tender_dir / "output"
    output.mkdir(exist_ok=True)
    log = make_script_logger("bid_loop", output / "logs" / "loop.log")

    policy = load_policy(args.policy)
    ledger_cfg = policy.get("failure_ledger") or {}
    lookback = int(ledger_cfg.get("dedup_lookback_days", 30))
    state = LoopState(tender_dir / STATE_REL)
    project = tender_dir.name

    if args.dry_run:
        log.info(f"[dry-run] policy keys: {sorted(policy.keys())}")
        log.info(f"[dry-run] loop_id={state.data['loop_id']} "
                 f"历史 cycles={len(state.data['cycles'])} last={state.data['last_result']}")
        log.info(f"[dry-run] 计划命令: bid_run.py {tender_dir} --loop-mode "
                 + " ".join(extra))
        return 0

    log.info("=" * 60)
    log.info(f"bid_loop 启动  项目: {project}  max_cycles={args.max_cycles}")
    notify(policy, "start", f"bid-gen 循环启动: {project}",
           f"max_cycles={args.max_cycles}", "info", log=log)

    rc = 1
    for cycle in range(1, args.max_cycles + 1):
        t0 = time.time()
        # 重试一律 --resume:只重跑失败步骤,绝不全管道重跑
        rc = run_once(tender_dir, resume=(cycle > 1), extra=extra, log=log)
        state.record(cycle, rc, time.time() - t0)
        state.save()

        if rc == 0:
            log.info(f"✓ 第 {cycle} 轮成功")
            notify(policy, "success", f"bid-gen 完成: {project}",
                   f"第 {cycle} 轮成功", "success", log=log)
            return 0

        failed = _failed_steps(tender_dir)
        detail = f"exit={rc} 失败步骤: {', '.join(failed) or '未知'}"
        log.error(f"✗ 第 {cycle} 轮失败 {detail}")
        if ledger_cfg.get("enabled", True):
            append_failure(tender_dir, {
                "failure_class": NO_RETRY_EXITS.get(rc, "step_fail"),
                "step": failed[0] if failed else "run",
                "detail": detail, "exit_code": rc,
            }, lookback_days=lookback)

        if rc in NO_RETRY_EXITS:
            reason = "预算熔断" if rc == 3 else "环境硬失败"
            log.error(f"{reason}(exit={rc}),不重试,转人工")
            notify(policy, "failure", f"bid-gen {reason}: {project}", detail,
                   "error", log=log)
            return rc

        if cycle < args.max_cycles:
            log.warning(f"第 {cycle + 1} 轮将以 --resume 重试失败步骤")

    log.error(f"循环耗尽({args.max_cycles} 轮)仍未成功")
    notify(policy, "loop_exhausted", f"bid-gen 循环耗尽: {project}",
           f"{args.max_cycles} 轮后仍失败,需人工介入", "error", log=log)
    return rc


if __name__ == "__main__":
    sys.exit(main())
