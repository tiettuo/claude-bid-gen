#!/usr/bin/env python3
"""对 OfficeCLI 联系表做轻量感知相似度检查（首期 warning-only）。"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from bid_common import make_script_logger
from bid_visual_rules import load_profiles, normalize_tender_dir


def _normalized(path: Path, size=(256, 256)) -> Image.Image:
    return Image.open(path).convert("L").resize(size)


def average_hash(img: Image.Image, size=16) -> str:
    small = img.resize((size, size))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return "".join("1" if p >= avg else "0" for p in px)


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def rms_similarity(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a, b)
    rms = math.sqrt(sum(v * v for v in ImageStat.Stat(diff).rms) / len(ImageStat.Stat(diff).rms))
    return max(0.0, 1.0 - rms / 255.0)


def edge_similarity(a: Image.Image, b: Image.Image) -> float:
    return rms_similarity(a.filter(ImageFilter.FIND_EDGES), b.filter(ImageFilter.FIND_EDGES))


def compare(a: Path, b: Path) -> dict:
    ia, ib = _normalized(a), _normalized(b)
    return {
        "rms_similarity": round(rms_similarity(ia, ib), 4),
        "edge_similarity": round(edge_similarity(ia, ib), 4),
        "average_hash_distance": hamming(average_hash(ia), average_hash(ib)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tender_dir")
    args = parser.parse_args()
    tender = normalize_tender_dir(Path(args.tender_dir))
    log = make_script_logger("visual_similarity", tender / "output" / "logs" / "visual_similarity.log")
    root = tender / "output" / "reports" / "visual"
    shots = sorted(root.glob("*/contact_sheet.png"))
    cfg = load_profiles().get("similarity", {})
    sim_warn = float(cfg.get("contact_sheet_similarity_warn", 0.90))
    hash_warn = int(cfg.get("layout_hash_distance_warn", 8))
    pairs = []
    warnings = []
    for a, b in itertools.combinations(shots, 2):
        metrics = compare(a, b)
        item = {"a": a.parent.name, "b": b.parent.name, **metrics}
        if metrics["edge_similarity"] >= sim_warn and metrics["average_hash_distance"] <= hash_warn:
            item["severity"] = "warn"
            warnings.append(item)
        else:
            item["severity"] = "pass"
        pairs.append(item)
    report = {"mode": "warning", "thresholds": {"edge_similarity": sim_warn,
              "average_hash_distance": hash_warn}, "pairs": pairs, "warnings": warnings}
    reports = tender / "output" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "visual_similarity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 视觉相似度报告", "", "> 首期为 warning-only，不阻断投标流水线。", "",
             "| 变体 A | 变体 B | 边缘相似度 | aHash 距离 | 结论 |", "|---|---|---:|---:|---|"]
    for p in pairs:
        lines.append(f"| {p['a']} | {p['b']} | {p['edge_similarity']:.4f} | {p['average_hash_distance']} | {p['severity']} |")
    if not pairs:
        lines.append("| - | - | - | - | 可比较联系表不足 2 份 |")
    (reports / "visual_similarity_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if warnings:
        log.warning(f"发现 {len(warnings)} 对视觉相似变体（warning-only）")
        return 1
    log.info("视觉相似度检查完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
