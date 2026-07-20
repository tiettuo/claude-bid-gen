#!/usr/bin/env python3
"""OfficeCLI 视觉层规则编译：只放行未被招标硬规则锁定的视觉属性。"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from bid_common import canonical_rules_path, get_bid_gen_home
from bid_assemble import parse_format_rules


PROFILE_FIELDS = {
    "h2_align": ("align_h2",),
    "h3_align": ("align_h3",),
    "h2_space_before_pt": ("space_before_h2", "heading_h2_space_before_pt"),
    "h2_space_after_pt": ("space_after_h2", "heading_h2_space_after_pt"),
    "h3_space_before_pt": ("space_before_h3", "heading_h3_space_before_pt"),
    "h3_space_after_pt": ("space_after_h3", "heading_h3_space_after_pt"),
    "table_grammar": ("table_grammar", "table_border_style"),
    "table_padding_twips": ("table_padding", "table_cell_padding"),
    "image_align": ("image_alignment", "chart_alignment"),
}


def normalize_tender_dir(path: Path) -> Path:
    path = path.expanduser().resolve()
    if (path / "output").is_dir() or (path / "extracted").is_dir():
        return path
    if path.name == "output" and (path.parent / "extracted").is_dir():
        return path.parent
    raise ValueError(f"无法识别 tender/output 目录: {path}")


def load_profiles(config_path: Path | None = None) -> dict:
    if config_path is None:
        home = Path(os.environ.get("BID_GEN_HOME", str(get_bid_gen_home())))
        config_path = home / "configs" / "bid_visual_profiles.json"
    return json.loads(config_path.read_text(encoding="utf-8"))


def load_variants(tender_dir: Path) -> list[dict]:
    path = tender_dir / "extracted" / "variants.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [v for v in data.get("variants", []) if isinstance(v, dict)]


def profile_key_for_variant(variant_id: str, variant: dict | None, cfg: dict) -> str:
    profiles = cfg.get("profiles", {})
    explicit = str((variant or {}).get("visual_profile") or "").upper()
    format_set = str((variant or {}).get("format_set") or "").upper()
    if explicit in profiles:
        return explicit
    if format_set in profiles:
        return format_set
    match = re.search(r"(\d+)$", variant_id or "")
    keys = sorted(profiles)
    if match and keys:
        return keys[(int(match.group(1)) - 1) % len(keys)]
    default = str(cfg.get("default_profile", "A")).upper()
    return default if default in profiles else keys[0]


def compile_visual_rules(tender_dir: Path, variant_id: str = "") -> dict:
    tender_dir = normalize_tender_dir(tender_dir)
    cfg = load_profiles()
    variants = load_variants(tender_dir)
    variant = next((v for v in variants if v.get("id") == variant_id), None)
    key = profile_key_for_variant(variant_id, variant, cfg)
    profile = dict(cfg["profiles"][key])

    class _Log:
        def info(self, *_a, **_k): pass
        def warning(self, *_a, **_k): pass
        def error(self, *_a, **_k): pass

    rules_path = canonical_rules_path(tender_dir / "extracted")
    fmt = parse_format_rules(rules_path, _Log())
    specified = set(fmt.get("_specified_keys", set()))
    allowed, rejected = {}, {}
    for field, value in profile.items():
        if field == "description":
            continue
        locks = PROFILE_FIELDS.get(field, ())
        hit = sorted(set(locks) & specified)
        if hit:
            rejected[field] = {"value": value, "reason": f"招标硬格式已锁定: {', '.join(hit)}"}
        else:
            allowed[field] = value

    return {
        "schema_version": cfg.get("schema_version", 1),
        "variant_id": variant_id or "single",
        "profile": key,
        "description": profile.get("description", ""),
        "allowed": allowed,
        "rejected": rejected,
        "locked_fields": sorted(specified),
        "format": {
            "line_thickness_pt": float(fmt.get("line_thickness_pt", 0.5)),
            "line_color": "000000" if fmt.get("no_color") else str(fmt.get("line_color", "000000")).lstrip("#").upper(),
            "blind_bid": bool(fmt.get("blind_bid") or fmt.get("no_company_info")),
            "no_color": bool(fmt.get("no_color")),
        },
        "variant": variant,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tender_dir")
    parser.add_argument("--variant", default="")
    args = parser.parse_args()
    print(json.dumps(compile_visual_rules(Path(args.tender_dir), args.variant.upper()), ensure_ascii=False, indent=2))
