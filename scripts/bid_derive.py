#!/usr/bin/env python3
"""
bid_derive.py - Tier 2 派生层

从 Tier 1 抽到的事实字段(project_facts.json)派生项目特征,产出
project_derived.json,直接驱动 brain.md / chapter_plan.md / Step 3 写作。

派生字段:
  - altitude_m / altitude_class:海拔(米)+ 等级(low/middle/high/very_high/extreme)
  - season_set / spans_winter:有效施工季节集合 + 是否跨冬施工
  - size_class:项目规模等级(small/small_medium/medium/large/extra_large)

派生靠纯 Python 函数 + 内置数据库,**不调 LLM**。海拔库覆盖西藏全部
74 个县,其他省份留作 TODO。

用法:
  python bid_derive.py /path/to/tender/
  python bid_derive.py --location 类乌齐县 --duration 110 --start 2026-05-31 --price 2025201
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# 1. 西藏海拔库(74 县,主要城镇/驻地海拔,单位:米)
# ────────────────────────────────────────────────────────────────────

# 数据来源:西藏自治区统计年鉴 + 国家测绘局公开数据 + 各县政府门户
# 海拔取县城驻地海拔(并非全县最高/最低)。施工现场实际海拔可能略有差异,
# 但用于 altitude_class 分级足够准确。
TIBET_ALTITUDE_DB: dict[str, int] = {
    # 拉萨市
    "拉萨市": 3658, "城关区": 3658, "堆龙德庆区": 3640, "达孜区": 3680,
    "林周县": 3860, "当雄县": 4200, "尼木县": 3810, "曲水县": 3650, "墨竹工卡县": 3820,
    # 日喀则市
    "日喀则市": 3836, "桑珠孜区": 3836,
    "南木林县": 4000, "江孜县": 4040, "定日县": 4300, "萨迦县": 4316,
    "拉孜县": 4012, "昂仁县": 4500, "谢通门县": 4050, "白朗县": 3950,
    "仁布县": 3950, "康马县": 4310, "定结县": 4350, "仲巴县": 4772,
    "亚东县": 2850, "吉隆县": 2700, "聂拉木县": 3810, "萨嘎县": 4500, "岗巴县": 4540,
    # 昌都市
    "昌都市": 3240, "卡若区": 3240,
    "江达县": 3680, "贡觉县": 3760, "类乌齐县": 3766, "丁青县": 3873,
    "察雅县": 3580, "八宿县": 3260, "左贡县": 3780, "芒康县": 3870, "洛隆县": 3700, "边坝县": 3500,
    # 林芝市
    "林芝市": 2900, "巴宜区": 2900,
    "工布江达县": 3460, "米林县": 2950, "墨脱县": 1100, "波密县": 2725,
    "察隅县": 2300, "朗县": 3100,
    # 山南市
    "山南市": 3551, "乃东区": 3551,
    "扎囊县": 3580, "贡嘎县": 3570, "桑日县": 3580, "琼结县": 3870, "曲松县": 3950,
    "措美县": 4100, "洛扎县": 3820, "加查县": 3290, "隆子县": 3860, "错那县": 4380, "浪卡子县": 4500,
    # 那曲市
    "那曲市": 4513, "色尼区": 4513,
    "嘉黎县": 4500, "比如县": 4000, "聂荣县": 4700, "安多县": 4800, "申扎县": 4700,
    "索县": 4100, "班戈县": 4750, "巴青县": 4500, "尼玛县": 4500, "双湖县": 5000,
    # 阿里地区
    "阿里地区": 4500, "噶尔县": 4350, "普兰县": 3800, "札达县": 3700,
    "日土县": 4400, "革吉县": 4800, "改则县": 4700, "措勤县": 4670,
}


# ────────────────────────────────────────────────────────────────────
# 2. 派生函数
# ────────────────────────────────────────────────────────────────────

def altitude_class(altitude_m: int | None) -> str:
    """海拔等级。
    < 1500           low           平原
    1500-2500        middle        中海拔
    2500-3500        high          高海拔(常规高反)
    3500-4000        very_high     极高海拔(必须高原专项)
    >= 4000          extreme       极高海拔 4000+(强制氧气/医疗保障)
    None             unknown
    """
    if altitude_m is None:
        return "unknown"
    if altitude_m < 1500:
        return "low"
    if altitude_m < 2500:
        return "middle"
    if altitude_m < 3500:
        return "high"
    if altitude_m < 4000:
        return "very_high"
    return "extreme"


def lookup_altitude(location: str | None) -> int | None:
    """从地名抽出海拔。优先级:县/区(最具体) > 市 > 地区。

    例:'西藏自治区日喀则市昂仁县' 应命中昂仁县(4500),不应命中日喀则市(3836)。
    """
    if not location:
        return None
    # 按后缀分级排序:县/区/乡级最优先,然后市级,最后地区级/省级
    def priority(k: str) -> int:
        if k.endswith("县") or k.endswith("区"):
            return 0
        if k.endswith("市"):
            return 1
        return 2
    sorted_keys = sorted(TIBET_ALTITUDE_DB.keys(), key=priority)
    for key in sorted_keys:
        if key in location:
            return TIBET_ALTITUDE_DB[key]
    return None


def extract_explicit_altitude(extracted_dir: Path | None) -> tuple[int | None, str | None]:
    """从招标/清单原文中读取明确海拔，优先于地名库推断。"""
    if not extracted_dir or not extracted_dir.exists():
        return None, None
    patterns = [
        r"平均海拔(?:高度)?(?:为|约|：|:)?\s*(\d{3,5})\s*米",
        r"海拔(?:高度)?(?:为|约|：|:)?\s*(\d{3,5})\s*米",
        r"(\d{3,5})\s*米(?:以上)?(?:的)?高海拔",
    ]
    for txt in sorted(extracted_dir.glob("*.txt"), key=lambda p: ("清单" not in p.name, p.name)):
        try:
            text = txt.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in patterns:
            m = re.search(pat, text[:200000])
            if not m:
                continue
            val = int(m.group(1))
            if 500 <= val <= 6000:
                return val, txt.name
    return None, None


def parse_date(s: str | None) -> date | None:
    """解析日期字符串。支持 YYYY-MM-DD / YYYY年M月D日 / YYYY/MM/DD。"""
    if not s:
        return None
    s = s.strip()
    patterns = [
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?",
    ]
    for pat in patterns:
        m = re.match(pat, s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def season_set(start: date | None, end: date | None) -> list[str]:
    """有效施工季节集合(西藏施工气候分区,非天文季节)。
    西藏 4500m 海拔以下:11-3 月停工区,9-10 月秋季,6-8 月夏季,4-5 月春季。
    """
    if not start or not end or end < start:
        return []
    seasons: set[str] = set()
    cur = start
    while cur <= end:
        m = cur.month
        if 4 <= m <= 5:
            seasons.add("春")
        elif 6 <= m <= 8:
            seasons.add("夏")
        elif 9 <= m <= 10:
            seasons.add("秋")
        else:  # 11-12, 1-3
            seasons.add("冬")
        # 跳到下个月
        if m == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, m + 1, 1)
    return sorted(seasons, key=lambda s: ["春", "夏", "秋", "冬"].index(s))


def spans_winter(start: date | None, end: date | None) -> bool:
    """工期是否跨冬季(西藏 11-3 月停工区)。"""
    return "冬" in season_set(start, end)


# ────────────────────────────────────────────────────────────────────
# 字数密度查表(Word 实测口径,2026-05-04 起统一)
# ────────────────────────────────────────────────────────────────────
# 渲染口径:Microsoft Word(macOS 上 osascript 调 Word.app compute statistics 直读页数)
# 早期使用 LibreOffice 估算导致密度偏小 ~17%(系数 0.83),已统一为 Word 口径。
#
# 实测参考(A4, 页边距 2.5cm/2cm/2cm/2cm 类典型, 普通正文段落):
#   字体        字号    行距          实测 Word 字/页
#   宋体小五号  10pt    1.5 倍         1100-1300
#   宋体五号    11pt    1.5 倍         950-1150
#   宋体小四    12pt    1.5 倍         850-950
#   宋体四号    14pt    1.5 倍         550-700      ← 西藏简易综合评估法常用
#   仿宋三号    16pt    固定 28 磅     700-800
#   宋体三号    16pt    1.5 倍         450-580
#
# 关键警示:14pt+1.5 倍行距下,**满版理论 980 字/页是过度乐观**,
#         Word 实测仅 550-700,差距 ~30%,这是历史"目标 47K 字写完 53K 仍超页"的根因。
# ────────────────────────────────────────────────────────────────────

# (font_pt, line_spacing_kind, line_spacing_value) → (lo, hi)
# line_spacing_kind: "multiple"(倍数) | "fixed_pt"(固定磅)
_CHARS_PER_PAGE_TABLE: dict[tuple[int, str, float], tuple[int, int]] = {
    (10, "multiple", 1.5):  (1100, 1300),
    (11, "multiple", 1.5):  (950,  1150),
    (12, "multiple", 1.5):  (850,  950),
    (14, "multiple", 1.5):  (550,  700),
    (14, "multiple", 1.25): (700,  850),
    (14, "fixed_pt", 28.0): (550,  700),  # 28 磅 ≈ 1.5×14pt 的固定行距
    (16, "fixed_pt", 28.0): (700,  800),
    (16, "multiple", 1.5):  (450,  580),
}


# Phase 3.2(2026-05-11): 已收敛到 bid_common.CN_FONT_SIZE_MAP
from bid_common import CN_FONT_SIZE_MAP as _CN_SIZE_MAP  # noqa: E402


def resolve_font_size_pt(val) -> float | None:
    """将字号值转换为 pt 数值。支持中文名(四号/小四)和阿拉伯数字(12/14pt)。"""
    if val is None:
        return None
    val_str = str(val).strip()
    if val_str in _CN_SIZE_MAP:
        return _CN_SIZE_MAP[val_str]
    # 按 name 长度倒序 + "小"前缀优先,避免"小四"被"四号"先命中
    names_sorted = sorted(_CN_SIZE_MAP.keys(),
                          key=lambda n: (-len(n), 0 if n.startswith("小") else 1))
    for name in names_sorted:
        if name in val_str:
            return _CN_SIZE_MAP[name]
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:pt|磅)?", val_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def parse_format_spec_from_rules(rules_text: str) -> dict:
    """从 disqualification_rules.md 的 yaml 块解析字号/行距。

    返回 {font_size_pt, line_spacing_kind, line_spacing_value};未找到的为 None。
    """
    result = {
        "font_size_pt": None,
        "line_spacing_kind": "multiple",
        "line_spacing_value": None,
    }
    # 在围栏代码块或正文里找 key: value
    body = re.search(r"font_size_body\s*[:：]\s*([^\n#]+)", rules_text)
    if body:
        result["font_size_pt"] = resolve_font_size_pt(body.group(1).strip())

    ls_rule = re.search(r"line_spacing_rule\s*[:：]\s*([^\n#]+)", rules_text)
    if ls_rule:
        v = ls_rule.group(1).strip().lower()
        if "fixed" in v or "固定" in v or "magn" in v or "magnetic" in v:
            result["line_spacing_kind"] = "fixed_pt"

    ls = re.search(r"line_spacing\s*[:：]\s*([^\n#]+)", rules_text)
    if ls:
        v = ls.group(1).strip()
        # "1.5倍" or "1.5" or "28磅"
        if "磅" in v or "pt" in v.lower():
            result["line_spacing_kind"] = "fixed_pt"
        m = re.search(r"(\d+(?:\.\d+)?)", v)
        if m:
            try:
                result["line_spacing_value"] = float(m.group(1))
            except ValueError:
                pass

    return result


def chars_per_page(font_size_pt: float, line_spacing_kind: str,
                   line_spacing_value: float) -> tuple[int, int]:
    """根据字号/行距查 Word 实测字数密度区间 (lo, hi)。

    未命中时退到最接近的字号,差异 1-2pt 内取相同档位;否则给保守估值。
    """
    key = (int(round(font_size_pt)), line_spacing_kind, float(line_spacing_value))
    if key in _CHARS_PER_PAGE_TABLE:
        return _CHARS_PER_PAGE_TABLE[key]

    # 退化匹配:同字号 + 同 kind, 最近的 value
    candidates = [
        (k, v) for k, v in _CHARS_PER_PAGE_TABLE.items()
        if k[0] == int(round(font_size_pt)) and k[1] == line_spacing_kind
    ]
    if candidates:
        candidates.sort(key=lambda kv: abs(kv[0][2] - line_spacing_value))
        return candidates[0][1]

    # 退化匹配:同字号
    candidates = [
        (k, v) for k, v in _CHARS_PER_PAGE_TABLE.items()
        if k[0] == int(round(font_size_pt))
    ]
    if candidates:
        return candidates[0][1]

    # 全未命中:按线性插值(14pt → 625, 12pt → 900, 16pt → 540 中点)
    return (500, 700)


def recommend_target_chars(target_pages: int, font_size_pt: float,
                           line_spacing_kind: str, line_spacing_value: float,
                           figure_ratio: float = 0.25) -> dict:
    """根据目标页数 + 字号行距推荐目标总字数。

    返回 {density_lo, density_hi, density_mid, text_pages, target_chars_lo,
          target_chars_hi, target_chars_mid}。
    """
    lo, hi = chars_per_page(font_size_pt, line_spacing_kind, line_spacing_value)
    mid = (lo + hi) // 2
    text_pages = int(round(target_pages * (1 - figure_ratio)))
    return {
        "density_lo": lo,
        "density_hi": hi,
        "density_mid": mid,
        "text_pages": text_pages,
        "figure_pages": target_pages - text_pages,
        "target_chars_lo": text_pages * lo,
        "target_chars_hi": text_pages * hi,
        "target_chars_mid": text_pages * mid,
    }


def size_class(control_price: float | None) -> str:
    """项目规模等级(基于招标控制价,单位元)。
    < 500 万         small               极小型
    500 万 - 3000 万 small_medium        中小型
    3000 万 - 1 亿   medium              中型
    1 亿 - 5 亿      large               大型
    >= 5 亿          extra_large         特大型
    None             unknown
    """
    if control_price is None or control_price <= 0:
        return "unknown"
    if control_price < 5_000_000:
        return "small"
    if control_price < 30_000_000:
        return "small_medium"
    if control_price < 100_000_000:
        return "medium"
    if control_price < 500_000_000:
        return "large"
    return "extra_large"


# ────────────────────────────────────────────────────────────────────
# 3. 主流程
# ────────────────────────────────────────────────────────────────────

def derive(facts: dict, extracted_dir: Path | None = None) -> dict:
    """从 project_facts.json 字段派生 project_derived.json。

    facts 字段格式:每个 key 下有 {"value": ..., "source": {...}}
    """
    def _val(key: str):
        v = facts.get(key)
        if isinstance(v, dict):
            return v.get("value")
        return v

    location = _val("location")
    duration_days = _val("duration_days")
    start_date_s = _val("start_date")
    end_date_s = _val("end_date")
    control_price = _val("control_price")

    explicit_altitude, explicit_source = extract_explicit_altitude(extracted_dir)
    altitude = explicit_altitude if explicit_altitude is not None else lookup_altitude(location)
    start = parse_date(start_date_s)
    end = parse_date(end_date_s)

    # 若有 duration 但缺 end_date,用 start + duration 推导
    if start and not end and duration_days:
        try:
            end = start + timedelta(days=int(duration_days) - 1)
        except (ValueError, TypeError):
            pass

    derived = {
        "altitude_m": altitude,
        "altitude_class": altitude_class(altitude),
        "altitude_lookup_source": location,
        "altitude_source": explicit_source or "location_lookup",
        "altitude_source_type": "explicit_text" if explicit_source else "location_lookup",
        "season_set": season_set(start, end),
        "spans_winter": spans_winter(start, end),
        "season_inferred_from": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "size_class": size_class(float(control_price) if control_price else None),
        "size_inferred_from_price": float(control_price) if control_price else None,
    }

    # drives 字段:派生项驱动哪些章节
    derived["drives"] = {
        "altitude_class":  ["brain.section3", "chapter07.高原专项", "chapter10.医疗保障", "chapter12.冬期"],
        "season_set":      ["chapter12.季节性施工措施"],
        "spans_winter":    ["chapter12.冬期施工", "chapter05.进度计划"],
        "size_class":      ["chapter_plan.target_pages", "chapter01.项目概况"],
    }
    return derived


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", nargs="?", help="投标根目录(读 extracted/project_facts.json)")
    p.add_argument("--location", help="手动指定地点(单元测试用)")
    p.add_argument("--duration", type=int, help="工期日历天")
    p.add_argument("--start", help="开工日期")
    p.add_argument("--end", help="竣工日期")
    p.add_argument("--price", type=float, help="控制价(元)")
    p.add_argument("--recommend-chars", action="store_true",
                   help="查 Word 实测字数密度表 + 推荐目标总字数")
    p.add_argument("--pages", type=int, help="目标页数(与 --recommend-chars 配合)")
    p.add_argument("--font", type=float, default=14, help="字号磅(默认 14)")
    p.add_argument("--line", type=float, default=1.5, help="行距值(倍数或磅,默认 1.5)")
    p.add_argument("--line-kind", choices=["multiple", "fixed_pt"], default="multiple",
                   help="行距类型:multiple 倍数 / fixed_pt 固定磅(默认 multiple)")
    p.add_argument("--figure-ratio", type=float, default=0.25,
                   help="图表页占比(默认 0.25)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    # --recommend-chars 子模式
    if args.recommend_chars:
        if not args.pages:
            p.error("--recommend-chars 需要 --pages")
        rec = recommend_target_chars(args.pages, args.font, args.line_kind,
                                     args.line, args.figure_ratio)
        print(json.dumps({
            "input": {
                "target_pages": args.pages,
                "font_size_pt": args.font,
                "line_spacing": f"{args.line}{'倍' if args.line_kind=='multiple' else '磅'}",
                "figure_ratio": args.figure_ratio,
            },
            "density_word_chars_per_page": f"{rec['density_lo']}-{rec['density_hi']} (中位 {rec['density_mid']})",
            "text_pages": rec["text_pages"],
            "figure_pages": rec["figure_pages"],
            "target_chars": {
                "conservative": rec["target_chars_lo"],
                "mid": rec["target_chars_mid"],
                "max_safe": rec["target_chars_hi"],
            },
            "note": "推荐用 mid 作目标,max_safe 作超页风险底线;LibreOffice 渲染约为 Word 的 1.2 倍页数",
        }, ensure_ascii=False, indent=2))
        return 0

    # 命令行直接传参模式(单元测试)
    if args.location or args.duration or args.start or args.price:
        facts = {
            "location": {"value": args.location} if args.location else None,
            "duration_days": {"value": args.duration} if args.duration else None,
            "start_date": {"value": args.start} if args.start else None,
            "end_date": {"value": args.end} if args.end else None,
            "control_price": {"value": args.price} if args.price else None,
        }
        derived = derive(facts)
        print(json.dumps(derived, ensure_ascii=False, indent=2))
        return 0

    if not args.tender_dir:
        p.error("tender_dir 必填(除非用 --location/--duration/...)")

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    facts_path = ext / "project_facts.json"

    log = make_script_logger("bid_derive", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_derive  目录: {tender_dir}")

    if not facts_path.exists():
        log.error(f"未找到 {facts_path.name},先跑 bid_facts_extract.py")
        return 2

    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    derived = derive(facts.get("facts", facts), ext)
    derived["_meta"] = {
        "extracted_by": "bid_derive.py",
        "facts_source": facts_path.name,
    }

    out = ext / "project_derived.json"
    out.write_text(json.dumps(derived, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  ✓ 写入 {out.name}")
    log.info(f"    altitude={derived['altitude_m']}m / class={derived['altitude_class']}")
    log.info(f"    seasons={derived['season_set']} / spans_winter={derived['spans_winter']}")
    log.info(f"    size_class={derived['size_class']}")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
