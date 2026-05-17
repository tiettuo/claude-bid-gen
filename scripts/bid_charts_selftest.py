#!/usr/bin/env python3
"""bid_charts_selftest.py - 7 种渲染器的端到端体检

对每种 chart type 跑两个变体输入:
  1. example 标准形状(_RENDERER_SCHEMAS 里的最小 JSON)
  2. LLM 常见的宽松写法(测 adapter 兼容)

产出 PASS/FAIL 报告。可以被 bid_run.py --doctor 集成。

用法:
  python bid_charts_selftest.py                  # 运行所有
  python bid_charts_selftest.py --type schedule  # 只跑某种
  python bid_charts_selftest.py --keep-png       # 保留 PNG 不删
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_charts import (  # noqa: E402
    _CHART_ADAPTERS,
    _CHART_RENDERERS,
    _RENDERER_SCHEMAS,
    classify_json,
)


# ─── 每种 type 的"宽松写法"测例 ─────────────────────────────────────
# 测 adapter 能不能把 LLM 常见的错误形状自动归一化
_LOOSE_VARIANTS: dict[str, dict] = {
    "schedule": {
        # 用 items 而非 tasks,start/end 是 int days
        "type": "schedule",
        "items": [
            {"name": "准备", "start_day": 0, "end_day": 9, "phase": "P1"},
            {"name": "基础", "start_day": 10, "duration": 20, "phase": "P2"},
        ],
    },
    "org_chart": {
        # level_N 扁平结构
        "type": "org_chart",
        "level_1": ["项目经理"],
        "level_2": ["技术", "安全", "材料"],
        "level_3": ["质检员", "试验员", "文明员"],
    },
    "hierarchy_flow": {
        # layers list-of-list, connections list-of-list
        "type": "hierarchy_flow",
        "layers": [["项目经理"], ["技术", "安全"], ["施工A", "施工B"]],
        "connections": [["项目经理", "技术"], ["项目经理", "安全"],
                        ["技术", "施工A"], ["技术", "施工B"]],
    },
    "process_flow": {
        # nodes list-of-string, edges list-of-list
        "type": "process_flow",
        "nodes": ["材料进场", "抽检", "合格入库"],
        "edges": [["s0", "s1"], ["s1", "s2"]],
    },
    "resource_curve": {
        # series 是 dict
        "type": "resource_curve",
        "periods": ["6月", "7月", "8月", "9月"],
        "series": {"普工": [5, 15, 20, 8], "焊工": [0, 10, 15, 5]},
    },
    "aoa_network": {
        # edges 替代 activities;duration 缺省
        "type": "aoa_network",
        "edges": [
            ["A", "B", "施工准备", 9],
            ["B", "C", "基础", 21],
            ["C", "D", "主体", 31],
            ["D", "E", "竣工", 14],
        ],
    },
    "site_layout": {
        # zones 是 list-of-{name,x,y} 而非 dict
        "type": "site_layout",
        "main_building": "仓储",
        "zones": [
            {"name": "材料堆场", "x": 50, "y": 80},
            {"name": "办公区", "x": 50, "y": 10},
            {"name": "机械停放", "x": 90, "y": 50},
            {"name": "变压器", "x": 10, "y": 50},
        ],
    },
}


def _run_one(chart_type: str, data: dict, label: str, out_dir: Path,
             log: logging.Logger) -> tuple[bool, str, int]:
    """跑一次渲染,返回 (ok, msg, png_size)。"""
    out_png = out_dir / f"{chart_type}_{label}.png"
    try:
        adapter = _CHART_ADAPTERS.get(chart_type)
        rdata = adapter(data) if adapter else data
        renderer = _CHART_RENDERERS.get(chart_type)
        if not renderer:
            return False, f"无渲染器: {chart_type}", 0
        ok = renderer(rdata, out_png, f"{chart_type}-{label}", log, None)
        size = out_png.stat().st_size if out_png.exists() else 0
        if ok and size > 0:
            return True, f"PNG {size} bytes", size
        return False, f"renderer 返回 {ok},png_size={size}", size
    except Exception as e:
        return False, f"EXCEPTION: {type(e).__name__}: {e}", 0


def main() -> int:
    p = argparse.ArgumentParser(description="bid_charts 7 种渲染器自检")
    p.add_argument("--type", help="只跑某种 type")
    p.add_argument("--out", default="", help="PNG 输出目录(缺省临时目录)")
    p.add_argument("--keep-png", action="store_true", help="保留 PNG 不删")
    p.add_argument("--quiet", action="store_true", help="只输出最终汇总")
    args = p.parse_args()

    log = logging.getLogger("selftest")
    log.setLevel(logging.WARNING if args.quiet else logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="bid_chart_selftest_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    types = [args.type] if args.type else list(_RENDERER_SCHEMAS.keys())
    invalid = [t for t in types if t not in _RENDERER_SCHEMAS]
    if invalid:
        print(f"[ERROR] 未知 type: {invalid}", file=sys.stderr)
        return 2

    results = []
    for t in types:
        sch = _RENDERER_SCHEMAS[t]

        # 变体 1:标准 example
        ok1, msg1, sz1 = _run_one(t, dict(sch["example"]), "standard", out_dir, log)
        results.append((t, "standard", ok1, msg1, sz1))

        # 变体 2:宽松写法
        loose = _LOOSE_VARIANTS.get(t)
        if loose is not None:
            ok2, msg2, sz2 = _run_one(t, dict(loose), "loose", out_dir, log)
            results.append((t, "loose", ok2, msg2, sz2))
        else:
            results.append((t, "loose", None, "(无宽松测例)", 0))

    # 汇总
    print()
    print("=" * 70)
    print(f"{'TYPE':<18} {'VARIANT':<10} {'STATUS':<8} {'PNG':<10} {'NOTE':<20}")
    print("=" * 70)
    pass_n = fail_n = skip_n = 0
    for t, v, ok, msg, sz in results:
        if ok is None:
            status = "SKIP"
            skip_n += 1
        elif ok:
            status = "PASS"
            pass_n += 1
        else:
            status = "FAIL"
            fail_n += 1
        png = f"{sz:,}b" if sz else "—"
        print(f"{t:<18} {v:<10} {status:<8} {png:<10} {msg[:30]}")

    print("=" * 70)
    total = pass_n + fail_n + skip_n
    print(f"PASS={pass_n}  FAIL={fail_n}  SKIP={skip_n}  TOTAL={total}")
    print(f"PNG 输出目录: {out_dir}")

    # 清理 PNG
    if not args.keep_png and not args.out:
        shutil.rmtree(out_dir, ignore_errors=True)

    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
