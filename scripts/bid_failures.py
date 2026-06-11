#!/usr/bin/env python3
"""结构化失败台账(JSONL)——循环工程的"经验沉淀"降级实现。

脚本只记结构化事件到 output/logs/loop_failures.jsonl;
认知教训(为什么/怎么防)由会话复盘产出,不自动写 memory(根因优先于防御层)。

去重: key = md5(failure_class|step|detail前80字)[:12];
lookback 窗口内同 key 只累加 count + 更新 last_seen。

用法:
  python bid_failures.py <tender_dir> --list
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

LEDGER_REL = Path("output") / "logs" / "loop_failures.jsonl"

_EVENT_FIELDS = ("failure_class", "step", "detail", "variant", "exit_code")


def _dedup_key(event: dict) -> str:
    basis = "|".join([
        str(event.get("failure_class", "")),
        str(event.get("step", "")),
        str(event.get("detail", ""))[:80],
    ])
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:12]


def load_ledger(tender_dir: Path) -> list[dict]:
    path = tender_dir / LEDGER_REL
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def append_failure(tender_dir: Path, event: dict, lookback_days: int = 30) -> dict:
    """记一条失败事件;窗口内同 key 累加 count。返回写入后的记录。"""
    path = tender_dir / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    records = load_ledger(tender_dir)
    key = _dedup_key(event)
    now = datetime.now().isoformat(timespec="seconds")
    cutoff = datetime.now() - timedelta(days=lookback_days)

    hit = None
    for r in records:
        if r.get("key") != key:
            continue
        try:
            last = datetime.fromisoformat(r.get("last_seen", ""))
        except ValueError:
            continue
        if last >= cutoff:
            hit = r
            break

    if hit:
        hit["count"] = int(hit.get("count", 1)) + 1
        hit["last_seen"] = now
    else:
        hit = {"key": key, "first_seen": now, "last_seen": now, "count": 1}
        hit.update({k: event[k] for k in _EVENT_FIELDS if k in event})
        records.append(hit)

    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return hit


def main() -> int:
    ap = argparse.ArgumentParser(description="循环失败台账查询")
    ap.add_argument("tender_dir")
    ap.add_argument("--list", action="store_true", help="列出全部失败记录")
    args = ap.parse_args()

    records = load_ledger(Path(args.tender_dir).expanduser().resolve())
    if not records:
        print("台账为空")
        return 0
    if args.list:
        for r in sorted(records, key=lambda x: x.get("last_seen", ""), reverse=True):
            print(f"[{r.get('last_seen')}] ×{r.get('count', 1)} "
                  f"{r.get('failure_class', '?')}/{r.get('step', '?')}: "
                  f"{str(r.get('detail', ''))[:100]}")
    else:
        print(f"共 {len(records)} 类失败,合计 {sum(r.get('count', 1) for r in records)} 次"
              f"(--list 看明细)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
