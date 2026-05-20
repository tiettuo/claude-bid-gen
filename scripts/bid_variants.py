#!/usr/bin/env python3
"""
bid_variants.py - 批量投标配置生成器

根据用户提供的单位名列表 + 模型池，生成 extracted/variants.json。
包含数据指纹（工期/劳动力/机械偏移）、元数据（作者/时间错位）、模型分配。

分配策略:
  - ≤ 6 家:      每家分配一个不同模型（模型池一一对应）
  - 7~12 家:    模型轮转 + 不同 temperature (0.5/0.7/0.9) 组合
  - 13~15 家:   上述 + 数据指纹大幅度偏移

用法:
  python bid_variants.py <tender_dir> --companies 甲建设,乙集团,丙公司 [--seed 42]
  python bid_variants.py <tender_dir> --companies-file companies.txt
"""

import argparse
import hashlib
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    needs_blind_scan,
    read_project_name,
    parse_chapter_plan,
    parse_plan_subitems,
)


# ─── 默认配置 ────────────────────────────────────────────────────────

# 仅在读不到 bid_llm_config.json 时的兜底池(正常走 build_model_pool 动态构建)
DEFAULT_MODEL_POOL = ["glm", "deepseek_chat", "kimi"]
DEFAULT_TEMPERATURES = [0.5, 0.7, 0.9]
DEFAULT_SIMILARITY = {"overall": 0.40, "paragraph": 0.50}

# 交叉多模型：同章温度浮动带宽（主温度 ±TEMP_BAND）
TEMP_BAND = 0.1
# 某模型 output_char_cap 读不到时的保守兜底
DEFAULT_OUTPUT_CHAR_CAP = 5000

# 数据指纹偏移范围
SCHEDULE_OFFSET_RANGE = (-7, 7)          # 日历天，在招标要求内调整
LABOR_FACTOR_RANGE = (0.92, 1.10)        # 劳动力系数
EQUIP_DELTA_ITEMS = ["挖掘机", "装载机", "压路机", "推土机", "自卸车", "搅拌车"]
EQUIP_DELTA_RANGE = (-2, 2)
STANDARD_YEARS = ["2017", "2020", "2023"]


def _load_companies(arg_companies: str, companies_file: Path) -> list[str]:
    names: list[str] = []
    if arg_companies:
        names.extend([x.strip() for x in arg_companies.split(",") if x.strip()])
    if companies_file and companies_file.is_file():
        for line in companies_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                names.append(s)
    # 去重但保留顺序
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


# 模型家族(用于变体间最大化文风差异:同家族文风相近,排序时打散)
_MODEL_FAMILIES = ["deepseek", "qwen", "glm", "kimi", "minimax", "doubao", "llama", "yi", "baichuan"]


def _model_family(key: str, cfg: dict) -> str:
    blob = (key + " " + str(cfg.get("model_name", ""))).lower()
    for fam in _MODEL_FAMILIES:
        if fam in blob:
            return fam
    return "other"


def build_model_pool(models_cfg: dict) -> list[str]:
    """从 LLM config 动态构建变体主模型池,按家族轮转排序。

    纳入条件:未停用(disabled) + 非纯兜底(writer_role != system_only,
    那些模型只做兜底不当主写手)。
    排序:家族轮转 —— 前 N 个变体尽量覆盖 N 个不同家族,文风差异最大化。
    """
    usable = [
        k for k, v in models_cfg.items()
        if not v.get("disabled") and v.get("writer_role") != "system_only"
    ]
    by_fam: dict[str, list[str]] = {}
    for k in usable:
        by_fam.setdefault(_model_family(k, models_cfg[k]), []).append(k)
    ordered: list[str] = []
    groups = list(by_fam.values())
    while any(groups):
        for grp in groups:
            if grp:
                ordered.append(grp.pop(0))
    return ordered


def _assign_model(idx: int, total: int, pool: list[str],
                  models_cfg=None) -> tuple[str, float]:
    """为第 idx 家分配模型与 temperature。

    idx < 模型池大小 → 每家一个不同模型(N 份 N 模型,文风各异);
    超出模型池 → 循环复用,用 temperature 区分。
    force_temperature 模型(如 kimi-k2.5 固定 0.6)用其强制值。
    """
    if not pool:
        raise ValueError("模型池为空")
    if idx < len(pool):
        model = pool[idx]
        temp = 0.6
    else:
        model = pool[idx % len(pool)]
        temp = DEFAULT_TEMPERATURES[(idx // len(pool)) % len(DEFAULT_TEMPERATURES)]
    if models_cfg and "force_temperature" in models_cfg.get(model, {}):
        temp = float(models_cfg[model]["force_temperature"])
    return model, temp


def _data_fingerprint(company: str, idx: int, total: int, rng: random.Random) -> dict:
    """为一家单位生成数据指纹"""
    amplify = 1.0 if total <= 12 else 1.5

    schedule_offset = rng.randint(int(SCHEDULE_OFFSET_RANGE[0] * amplify),
                                    int(SCHEDULE_OFFSET_RANGE[1] * amplify))
    labor_factor = round(rng.uniform(LABOR_FACTOR_RANGE[0], LABOR_FACTOR_RANGE[1]), 2)

    n_equip = rng.randint(2, 4)
    equip_delta = {
        rng.choice(EQUIP_DELTA_ITEMS): rng.randint(*EQUIP_DELTA_RANGE)
        for _ in range(n_equip)
    }

    standard_year = rng.choice(STANDARD_YEARS)

    return {
        "schedule_offset_days": schedule_offset,
        "labor_factor": labor_factor,
        "equip_delta": equip_delta,
        "standard_year_preference": standard_year,
    }


def generate_chart_style(variant_id: str, seed: int = 0) -> dict:
    """向后兼容别名。"""
    return generate_variant_style(variant_id, seed)


NUMBERING_SCHEMES = [
    {"name": "A", "level1": "1.  2.  3.", "level2": "(1)  (2)  (3)", "level3": "①  ②  ③"},
    {"name": "B", "level1": "一、二、三、", "level2": "1.  2.  3.", "level3": "(1)  (2)  (3)"},
    {"name": "C", "level1": "（一）（二）（三）", "level2": "1、2、3、", "level3": "①  ②  ③"},
    {"name": "D", "level1": "1、2、3、", "level2": "(1)  (2)  (3)", "level3": "①  ②  ③"},
    {"name": "E", "level1": "（一）（二）（三）", "level2": "1.  2.  3.", "level3": "a)  b)  c)"},
    {"name": "F", "level1": "1.  2.  3.", "level2": "1)  2)  3)", "level3": "(1)  (2)  (3)"},
]


def generate_variant_style(variant_id: str, seed: int = 0) -> dict:
    """用 variant_id 的 hash 确定性生成视觉风格参数（图表 + 文档软层）。"""
    h = int(hashlib.md5(f"{seed}_{variant_id}".encode()).hexdigest()[:8], 16)
    rng = random.Random(h)
    return {
        # ── 序号方案 ──
        "numbering_scheme": rng.choice(NUMBERING_SCHEMES),
        # ── 图表层（原 chart_style） ──
        "org_direction": rng.choice(["TB", "LR"]),
        "org_box_shape": rng.choice(["box", "rounded", "doubleframe"]),
        "flow_direction": rng.choice(["TB", "LR"]),
        "flow_corner_radius": rng.choice([0, 0, 8]),
        "gantt_bar_height": round(rng.uniform(0.42, 0.58), 2),
        "gantt_date_fmt": rng.choice(["%Y-%m", "%m月%d日", "%Y/%m"]),
        "gantt_grid": rng.choice(["dashed", "dotted", "none"]),
        "gantt_grays": rng.choice([
            ["#000000", "#333333", "#555555", "#777777", "#999999", "#BBBBBB"],
            ["#1A1A1A", "#3D3D3D", "#5C5C5C", "#7A7A7A", "#969696", "#B0B0B0"],
            ["#0D0D0D", "#2B2B2B", "#4A4A4A", "#6B6B6B", "#8C8C8C", "#ADADAD"],
            ["#000000", "#404040", "#606060", "#808080", "#A0A0A0", "#C0C0C0"],
            ["#1A1A1A", "#383838", "#565656", "#747474", "#929292", "#B8B8B8"],
            ["#0A0A0A", "#303030", "#505050", "#707070", "#909090", "#B5B5B5"],
        ]),
        "gantt_bar_hatch": rng.choice(["", "", "///", "...", "\\\\\\", "|||", "xxx", "+++"]),
        "gantt_milestone_style": rng.choice([":", "--", "-."]),
        "resource_bar_width": round(rng.uniform(0.5, 0.75), 2),
        "resource_hatches": rng.sample(
            ["///", "...", "|||", "xxx", "\\\\\\", "+++", "---", "ooo"], 4),
        "network_node_radius": rng.randint(14, 20),
        "network_label_pos": rng.choice(["above", "below"]),
        "line_width": round(rng.uniform(0.6, 1.2), 1),
        "font_size_offset": rng.choice([-1, 0, 0, +1]),
        "spacing_factor": round(rng.uniform(0.9, 1.15), 2),
        "sibling_shuffle_seed": h,
        # ── 文档软层（仅对 rules 未指定的参数生效） ──
        "doc_first_line_indent_chars": round(rng.uniform(1.92, 2.08), 2),
        "doc_para_spacing_before_pt": round(rng.uniform(0, 2.5), 1),
        "doc_para_spacing_after_pt": round(rng.uniform(0, 2.5), 1),
        "doc_line_spacing_offset": round(rng.uniform(-0.02, 0.02), 3),
        "doc_table_cell_padding_twips": rng.randint(40, 80),
        "doc_h2_space_before_pt": rng.randint(12, 18),
        "doc_h3_space_before_pt": rng.randint(8, 14),
        "doc_table_border_pt": round(rng.uniform(0.5, 1.0), 1),
        "doc_page_num_font_size_pt": round(rng.uniform(9.0, 10.5), 1),
        "doc_toc_indent_per_level_twips": rng.randint(300, 450),
    }


def _metadata(company: str, idx: int, rng: random.Random) -> dict:
    """DOCX 元数据错位"""
    return {
        "author": company,
        "last_modified_by": company,
        "create_time_offset_hours": rng.randint(-72, 0),
        "modified_time_offset_hours": rng.randint(-48, 0),
    }


def _pick_by_hash(pool: list[str], hash_hex: str) -> str:
    """按哈希前 8 位稳定选家（pool 先排序保证跨机器可复现）"""
    pool_sorted = sorted(pool)
    idx = int(hash_hex[:8], 16) % len(pool_sorted)
    return pool_sorted[idx]


def _hash_temp_in_band(hash_hex: str, lo: float, hi: float) -> float:
    """把哈希后 8 位映射到温度带内 [lo, hi]"""
    seed = int(hash_hex[8:16], 16)
    return round(lo + (hi - lo) * ((seed % 10000) / 10000.0), 2)


def _plan_chapter_models(
    variant: dict,
    chapters: list[dict],
    subitems_map: dict,
    models_cfg: dict,
) -> dict:
    """为该 variant 的每章分配"子项模型序列"

    返回: {
      chapter_num: {
        "mode": "cross_model" | "single_model_fallback",
        "polish_model": <variant 主模型>,
        "polish_temperature": <variant 主温度>,
        "subitems": [{idx, title, target_chars, model, temperature}, ...]
      }
    }

    分配规则:
    - 子项目标字数 > 某模型的 output_char_cap 时，该家被排除
    - 所有模型均参与子项分配（含 force_temperature 模型）
    - 池内用 hash(variant_id|chapter_num|subitem_idx) 决定具体家
    - 温度：variant 主温度 ± TEMP_BAND 带内哈希浮动；force_temperature 模型用固定值
    """
    main_model = variant["model"]
    main_temp = variant["temperature"]
    vid = variant["id"]

    temp_lo = max(0.0, main_temp - TEMP_BAND)
    temp_hi = min(1.5, main_temp + TEMP_BAND)

    plan: dict[str, dict] = {}

    for ch in chapters:
        ch_num = ch["num"]
        ch_subitems = subitems_map.get(ch_num, [])

        if not ch_subitems:
            # 没有子项分配 → 退化为单模型整章模式（writer 走老流程）
            plan[ch_num] = {
                "mode": "single_model_fallback",
                "polish_model": main_model,
                "polish_temperature": main_temp,
                "subitems": [],
            }
            continue

        subitem_assignments = []

        # 构建本章可用模型池（按字数上限 + writer_role 过滤）
        is_system_ch = any(
            kw in ch.get("title", "")
            for kw in ("质量", "安全", "环保", "绿色", "工期", "文明", "民工", "进度")
        )
        max_target = max(int(s["chars"]) for s in ch_subitems)
        chapter_pool = []
        for mk, mc in models_cfg.items():
            cap = int(mc.get("output_char_cap", DEFAULT_OUTPUT_CHAR_CAP))
            if max_target > cap:
                continue
            role = mc.get("writer_role", "")
            if role == "system_only" and not is_system_ch:
                continue
            chapter_pool.append(mk)
        if not chapter_pool:
            chapter_pool = [main_model]
        chapter_pool.sort()

        # 章内轮转分配：先用 hash 决定起始偏移，然后轮转保证同章内尽量不重复
        # 5 个模型 8 个子项 → 前 5 个各不同，第 6-8 个进入第二轮
        ch_hash = hashlib.md5(f"{vid}|{ch_num}".encode()).hexdigest()
        start_offset = int(ch_hash[:8], 16) % len(chapter_pool)
        # 按哈希洗牌池顺序，保证不同章/不同 variant 的轮转起点不同
        ch_rng = random.Random(int(ch_hash[:16], 16))
        shuffled_pool = chapter_pool[:]
        ch_rng.shuffle(shuffled_pool)

        for idx, sub in enumerate(ch_subitems):
            target_chars = int(sub["chars"])
            chosen = shuffled_pool[idx % len(shuffled_pool)]

            hash_hex = hashlib.md5(f"{vid}|{ch_num}|{idx}".encode()).hexdigest()
            if "force_temperature" in models_cfg.get(chosen, {}):
                temp = float(models_cfg[chosen]["force_temperature"])
            else:
                temp = _hash_temp_in_band(hash_hex, temp_lo, temp_hi)

            subitem_assignments.append({
                "idx": idx,
                "title": sub["title"],
                "target_chars": target_chars,
                "model": chosen,
                "temperature": temp,
            })

        plan[ch_num] = {
            "mode": "cross_model",
            "polish_model": main_model,
            "polish_temperature": main_temp,
            "subitems": subitem_assignments,
        }

    return plan


def build_variants(
    companies: list[str],
    project_name: str,
    model_pool: list[str],
    seed: int = 0,
    blind: bool = False,
    chapters=None,
    subitems_map=None,
    models_cfg=None,
) -> dict:
    if not companies:
        raise ValueError("公司列表为空")
    if len(companies) > 20:
        raise ValueError(f"一次最多支持 20 家，收到 {len(companies)}")

    # seed 为 0 时按项目名+公司组合做稳定哈希，保证可复现
    base_seed = seed or int(hashlib.md5(project_name.encode("utf-8")).hexdigest()[:8], 16)
    total = len(companies)

    # 过滤掉 disabled 模型（如豆包没充值），主模型分配池和子项分配池都排除
    if models_cfg:
        disabled_keys = {k for k, v in models_cfg.items() if v.get("disabled")}
        if disabled_keys:
            model_pool = [m for m in model_pool if m not in disabled_keys]
            if not model_pool:
                raise ValueError("过滤掉 disabled 模型后 model_pool 为空")

    # 交叉多模型分配所需的外部上下文（三者缺一则跳过子项分配）
    can_cross_assign = bool(chapters and subitems_map and models_cfg)
    # 仅保留 model_pool 里确实存在于 models_cfg 且未停用的模型用于子项分配
    cross_models_cfg = (
        {k: v for k, v in (models_cfg or {}).items()
         if k in model_pool and not v.get("disabled")}
        if can_cross_assign else {}
    )

    variants = []
    for idx, company in enumerate(companies):
        rng = random.Random(base_seed + idx * 17)
        model, temperature = _assign_model(idx, total, model_pool, models_cfg)
        fp = _data_fingerprint(company, idx, total, rng)
        md = _metadata(company, idx, rng)

        vid = f"V{idx + 1:02d}"
        vstyle = generate_variant_style(vid, seed=base_seed)

        v_entry = {
            "id": vid,
            "company_name": company,
            "model": model,
            "temperature": temperature,
            "blind": blind,
            **fp,
            "metadata": md,
            "section_order_seed": base_seed + idx * 101,
            "chart_style": vstyle,
        }

        if can_cross_assign:
            v_entry["chapter_model_plan"] = _plan_chapter_models(
                v_entry, chapters, subitems_map, cross_models_cfg
            )

        variants.append(v_entry)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": project_name,
        "variant_count": total,
        "model_pool": model_pool,
        "similarity_threshold": DEFAULT_SIMILARITY,
        "blind_bid": blind,
        "seed": base_seed,
        "cross_model_enabled": can_cross_assign,
        "variants": variants,
    }


def print_table(payload: dict) -> None:
    print(f"\n项目: {payload['project_name']}")
    print(f"单位数: {payload['variant_count']}   模型池: {', '.join(payload['model_pool'])}")
    print("=" * 110)
    print(f"{'ID':<5}{'公司':<20}{'模型':<12}{'温度':<6}{'工期偏移':<10}{'劳动力系数':<12}{'标准年份':<10}{'设备差异':<25}")
    print("-" * 110)
    for v in payload["variants"]:
        equip_str = ", ".join(f"{k}{v_:+d}" for k, v_ in v["equip_delta"].items())
        print(
            f"{v['id']:<5}{v['company_name'][:18]:<20}{v['model']:<12}"
            f"{v['temperature']:<6}{v['schedule_offset_days']:+d}{'':<6}"
            f"{v['labor_factor']:<12}{v['standard_year_preference']:<10}{equip_str[:23]:<25}"
        )
    print("=" * 110)


def main() -> int:
    p = argparse.ArgumentParser(description="生成批量投标的 variants.json 配置")
    p.add_argument("tender_dir", help="投标根目录（含 extracted/）")
    p.add_argument("--companies", help="逗号分隔的单位名列表")
    p.add_argument("--companies-file", type=Path, help="每行一个单位名的文本文件")
    p.add_argument("--project-name", default="", help="项目名称（默认从 project_brain.md 提取）")
    p.add_argument("--model-pool", help="自定义模型池，逗号分隔（默认 6 家全部）")
    p.add_argument("--seed", type=int, default=0, help="随机种子（默认由项目名哈希生成）")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    p.add_argument("--cross-model", action="store_true",
                   help="启用旧 cross_model 模式（每章 4 子项并行 + 主笔润色）。默认走"
                        "单作者整章模式（1 模型 = 1 写手，每章 1 次调用，无润色）")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    extracted_dir = tender_dir / "extracted"
    if not extracted_dir.is_dir():
        print(f"extracted/ 目录不存在: {extracted_dir}", file=sys.stderr)
        return 2

    companies = _load_companies(args.companies or "", args.companies_file)
    if not companies:
        print("未提供单位名。使用 --companies 或 --companies-file", file=sys.stderr)
        return 2

    project_name = args.project_name or read_project_name(extracted_dir, fallback=tender_dir.name)

    # 加载 LLM 配置(模型池来源)
    from bid_common import get_bid_gen_home as _ggh
    llm_config_path = _ggh() / "configs" / "bid_llm_config.json"
    models_cfg: dict = {}
    if llm_config_path.is_file():
        try:
            models_cfg = json.loads(llm_config_path.read_text(encoding="utf-8")).get("models", {})
        except Exception as e:
            print(f"⚠ bid_llm_config.json 读取失败: {e}")

    # 模型池:显式 --model-pool 优先;否则从 config 动态构建(家族轮转,
    # 前 N 份尽量分到 N 个不同模型 → 份数即模型数)
    if args.model_pool:
        pool = [x.strip() for x in args.model_pool.split(",") if x.strip()]
    elif models_cfg:
        pool = build_model_pool(models_cfg)
    else:
        pool = DEFAULT_MODEL_POOL
    print(f"模型池({len(pool)} 个,家族轮转): {', '.join(pool)}")
    if len(companies) <= len(pool):
        print(f"  → {len(companies)} 份各分一个不同模型")
    else:
        print(f"  ⚠ 份数 {len(companies)} > 模型池 {len(pool)},超出部分复用模型"
              f"(靠章节结构差异化兜底)")

    blind = needs_blind_scan(extracted_dir)
    if blind:
        print("检测到暗标场景（blind_bid=true 或 no_company_info=true 或正文暗标表述），每家 variant 将设置 blind=true")

    # 模式选择:--cross-model 才走旧 4 子项并行+主笔润色;默认走单作者整章模式
    chapters = None
    subitems_map = None
    if args.cross_model:
        chapter_plan_path = extracted_dir / "chapter_plan.md"
        if chapter_plan_path.is_file() and models_cfg:
            try:
                chapters = parse_chapter_plan(chapter_plan_path)
                subitems_map = parse_plan_subitems(chapter_plan_path)
                if chapters and subitems_map:
                    n_sub = sum(len(v) for v in subitems_map.values())
                    print(f"★ --cross-model 启用:{len(chapters)} 章 × {n_sub} 子项并行 + 主笔润色")
                else:
                    print("⚠ --cross-model 已要求但 chapter_plan 无子项,退化为单作者模式")
                    chapters = subitems_map = None
            except Exception as e:
                print(f"⚠ --cross-model 上下文加载失败,退化为单作者模式: {e}")
                chapters = subitems_map = None
        else:
            print("⚠ --cross-model 已要求但 chapter_plan.md / bid_llm_config.json 不存在,退化为单作者模式")
    else:
        print(f"★ 单作者模式(1 模型 = 1 写手,{len(companies)} 家各一个模型)")

    payload = build_variants(
        companies, project_name, pool,
        seed=args.seed, blind=blind,
        chapters=chapters, subitems_map=subitems_map, models_cfg=models_cfg,
    )
    print_table(payload)

    if args.dry_run:
        print("\n(--dry-run 未写入文件)")
        return 0

    out_path = extracted_dir / "variants.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
