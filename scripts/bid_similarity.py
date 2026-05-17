#!/usr/bin/env python3
"""
bid_similarity.py - 批量投标交叉相似度检查

对 output/V*/ 下所有章节做两两相似度比对，确保雷同风险可控。

检查维度:
  1. 整体文字重复率（SimHash + jieba 中文分词）
  2. 段落级重复率（5-gram MinHash LSH）
  3. 数据指纹（提取所有数字做 Jaccard 相似度）
  4. 元数据独立性（DOCX core.xml）

依赖（可选，缺失则优雅降级到 difflib）:
  pip install simhash datasketch jieba

输出:
  output/相似度检查报告.md
  output/similarity_matrix.json

用法:
  python bid_similarity.py <tender_dir>
  python bid_similarity.py <tender_dir> --threshold-overall 0.4 --threshold-paragraph 0.5
"""

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bid_common import (  # noqa: E402
    make_script_logger,
    extract_headings,
)


# ─── 分词 ────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """尽量用 jieba，失败降级到字符级 bigram"""
    try:
        import jieba
        return [w for w in jieba.cut(text) if w.strip() and len(w) >= 1]
    except ImportError:
        # 字符级 bigram 近似
        text = re.sub(r"\s+", "", text)
        return [text[i:i+2] for i in range(len(text) - 1)] or [text]


# ─── 整体相似度（SimHash 优先，降级到 TF-IDF 余弦）──────────────────

def _text_similarity_simhash(a: str, b: str) -> float:
    """返回 0~1 的相似度"""
    try:
        from simhash import Simhash
    except ImportError:
        return _text_similarity_difflib(a, b)

    sa = Simhash(_tokenize(a))
    sb = Simhash(_tokenize(b))
    distance = sa.distance(sb)  # 0~64
    return 1 - distance / 64


def _text_similarity_difflib(a: str, b: str) -> float:
    """降级方案：difflib SequenceMatcher，慢但总能用"""
    from difflib import SequenceMatcher
    # 长文本截断以避免卡死
    max_chars = 5000
    return SequenceMatcher(None, a[:max_chars], b[:max_chars]).ratio()


def text_similarity(a: str, b: str) -> float:
    return _text_similarity_simhash(a, b)


# ─── 段落级（MinHash LSH 近似匹配，降级到 n-gram Jaccard）───────────

def _paragraphs(text: str, min_len: int = 50) -> list[str]:
    """按空行切段，过滤太短段落"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [p for p in paras if len(p) >= min_len]


def _ngram_set(text: str, n: int = 5) -> set[str]:
    tokens = _tokenize(text)
    if len(tokens) < n:
        return set([" ".join(tokens)]) if tokens else set()
    return {" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def paragraph_max_similarity(a_text: str, b_text: str) -> tuple[float, str, str]:
    """返回两份文本段落级最大 Jaccard 相似度 + 最相似的两段摘要"""
    a_paras = _paragraphs(a_text)
    b_paras = _paragraphs(b_text)
    if not a_paras or not b_paras:
        return 0.0, "", ""

    a_ngrams = [_ngram_set(p) for p in a_paras]
    b_ngrams = [_ngram_set(p) for p in b_paras]

    max_sim = 0.0
    max_a = max_b = ""
    for i, ag in enumerate(a_ngrams):
        for j, bg in enumerate(b_ngrams):
            s = _jaccard(ag, bg)
            if s > max_sim:
                max_sim = s
                max_a = a_paras[i][:80]
                max_b = b_paras[j][:80]
    return max_sim, max_a, max_b


# ─── 数据指纹相似度 ────────────────────────────────────────────────

def extract_numbers(text: str) -> set[str]:
    """抽取所有数字（整数 / 小数 / 百分比），归一化为字符串做集合比较"""
    nums = set()
    for m in re.finditer(r"\d+(?:\.\d+)?%?", text):
        nums.add(m.group())
    return nums


def data_fingerprint_similarity(a: str, b: str) -> float:
    return _jaccard(extract_numbers(a), extract_numbers(b))


# ─── 标题骨架相似度（反"一个妈生的"核心指标）──────────────────────

def _norm_heading(t: str) -> str:
    """归一化：去编号前缀、空白、标点，保留实质词。"""
    t = re.sub(r"^[\dIVXivx一二三四五六七八九十]+[\.、．]\s*", "", t)
    t = re.sub(r"^[（(【][^）)】]*[）)】]\s*", "", t)
    t = re.sub(r"\s+", "", t)
    return t.strip(" 【】（）()·…")


def _edit_distance(a: list[str], b: list[str]) -> int:
    """序列级 Levenshtein（元素为字符串，整体比较）。"""
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return max(la, lb)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i-1] == b[j-1] else 1
            curr[j] = min(prev[j] + 1, curr[j-1] + 1, prev[j-1] + cost)
        prev = curr
    return prev[lb]


def _extract_h2_seq(text: str) -> list[str]:
    return [_norm_heading(t) for lvl, t in extract_headings(text, levels=(2,)) if _norm_heading(t)]


def heading_similarity(a_text: str, b_text: str) -> tuple[float, float, list[str], list[str]]:
    """返回 (order_sim, set_sim, a_h2_seq, b_h2_seq)

    - order_sim: 1 - edit_distance / max(len) — 顺序敏感
    - set_sim : Jaccard(set(a), set(b)) — 顺序无关
    """
    a_seq = _extract_h2_seq(a_text)
    b_seq = _extract_h2_seq(b_text)
    if not a_seq or not b_seq:
        return 0.0, 0.0, a_seq, b_seq
    ed = _edit_distance(a_seq, b_seq)
    order_sim = 1 - ed / max(len(a_seq), len(b_seq))
    set_sim = _jaccard(set(a_seq), set(b_seq))
    return round(order_sim, 3), round(set_sim, 3), a_seq, b_seq


# ─── 元数据独立性 ──────────────────────────────────────────────────

def read_docx_metadata(docx_path: Path) -> dict:
    """读取 DOCX 的 core properties"""
    try:
        from docx import Document
        doc = Document(str(docx_path))
        cp = doc.core_properties
        return {
            "author": cp.author or "",
            "last_modified_by": cp.last_modified_by or "",
            "created": str(cp.created) if cp.created else "",
            "modified": str(cp.modified) if cp.modified else "",
            "revision": cp.revision or 0,
        }
    except Exception:
        return {}


# ─── 主流程 ────────────────────────────────────────────────────────

def collect_variant_texts(output_dir: Path) -> dict[str, dict]:
    """返回 {variant_id: {text: 全文拼接, chapters: {ch_num: text}, docx: path}}"""
    result = {}
    for v_dir in sorted(output_dir.iterdir()):
        if not (v_dir.is_dir() and re.match(r"^V\d+$", v_dir.name)):
            continue
        chapters = {}
        all_text_parts = []
        for md in sorted(v_dir.glob("*.md")):
            if md.name in ("review.md",):
                continue
            if not re.match(r"^\d", md.stem):
                continue
            t = md.read_text(encoding="utf-8")
            chapters[md.stem.split("_")[0]] = t
            all_text_parts.append(t)
        if chapters:
            # 找 DOCX（可能在 output 根目录，文件名以 VXX_ 开头）
            docx = None
            for cand in output_dir.glob(f"{v_dir.name}_*.docx"):
                docx = cand
                break
            result[v_dir.name] = {
                "text": "\n\n".join(all_text_parts),
                "chapters": chapters,
                "docx": docx,
            }
    return result


def _precompute(variants_data: dict[str, dict], log: logging.Logger) -> dict:
    """预计算每个 variant 的 SimHash / 数字集合 / 每章段落 ngram 缓存。"""
    from bid_common import safe_import
    simhash_mod = safe_import("simhash", log=log)
    has_simhash = simhash_mod is not None
    if not has_simhash:
        log.info("simhash 未安装,整体相似度降级到 difflib")
    Simhash = simhash_mod.Simhash if has_simhash else None

    cache = {}
    for vid, data in variants_data.items():
        per_ch = {}
        for ch_num, text in data["chapters"].items():
            paras = _paragraphs(text)
            per_ch[ch_num] = {
                "paras": paras,
                "ngrams": [_ngram_set(p) for p in paras],
            }
        entry = {
            "text": data["text"],
            "numbers": extract_numbers(data["text"]),
            "chapters": per_ch,
            "docx": data["docx"],
            "h2_seq": _extract_h2_seq(data["text"]),
        }
        if has_simhash:
            entry["simhash"] = Simhash(_tokenize(data["text"]))
        cache[vid] = entry
    return cache


def build_matrix(variants_data: dict[str, dict], log: logging.Logger) -> dict:
    """两两计算相似度（使用预计算缓存）"""
    ids = sorted(variants_data.keys())
    n = len(ids)
    log.info(f"预计算 {n} 个 variant 的 ngram/SimHash 缓存…")
    cache = _precompute(variants_data, log)

    overall = defaultdict(dict)
    paragraph = defaultdict(dict)
    data_fp = defaultdict(dict)
    heading_order = defaultdict(dict)
    heading_set = defaultdict(dict)
    metadata_same = defaultdict(dict)
    worst_pairs = []

    for i in range(n):
        for j in range(i + 1, n):
            a_id, b_id = ids[i], ids[j]
            a_c, b_c = cache[a_id], cache[b_id]
            log.info(f"  对比 {a_id} vs {b_id}")

            # 整体：SimHash 缓存对比或 difflib 降级
            if "simhash" in a_c and "simhash" in b_c:
                s_overall = 1 - a_c["simhash"].distance(b_c["simhash"]) / 64
            else:
                s_overall = _text_similarity_difflib(a_c["text"], b_c["text"])
            overall[a_id][b_id] = round(s_overall, 3)
            overall[b_id][a_id] = round(s_overall, 3)

            # 段落级：用缓存的 ngram 集合，不再重复分词
            max_para = 0.0
            worst_ch = ""
            worst_a = worst_b = ""
            for ch_num in a_c["chapters"]:
                if ch_num not in b_c["chapters"]:
                    continue
                a_info = a_c["chapters"][ch_num]
                b_info = b_c["chapters"][ch_num]
                for ia, ag in enumerate(a_info["ngrams"]):
                    for jb, bg in enumerate(b_info["ngrams"]):
                        s = _jaccard(ag, bg)
                        if s > max_para:
                            max_para = s
                            worst_ch = ch_num
                            worst_a = a_info["paras"][ia][:80]
                            worst_b = b_info["paras"][jb][:80]
            paragraph[a_id][b_id] = round(max_para, 3)
            paragraph[b_id][a_id] = round(max_para, 3)

            # 数据指纹：用缓存的数字集合
            s_data = _jaccard(a_c["numbers"], b_c["numbers"])
            data_fp[a_id][b_id] = round(s_data, 3)
            data_fp[b_id][a_id] = round(s_data, 3)

            # 标题骨架：顺序敏感 + 集合
            a_seq, b_seq = a_c["h2_seq"], b_c["h2_seq"]
            if a_seq and b_seq:
                ed = _edit_distance(a_seq, b_seq)
                h_order = round(1 - ed / max(len(a_seq), len(b_seq)), 3)
                h_set = round(_jaccard(set(a_seq), set(b_seq)), 3)
            else:
                h_order = h_set = 0.0
            heading_order[a_id][b_id] = heading_order[b_id][a_id] = h_order
            heading_set[a_id][b_id] = heading_set[b_id][a_id] = h_set

            # 元数据
            a_meta = read_docx_metadata(a_c["docx"]) if a_c["docx"] else {}
            b_meta = read_docx_metadata(b_c["docx"]) if b_c["docx"] else {}
            same_fields = []
            for k in ("author", "last_modified_by", "created"):
                if a_meta.get(k) and a_meta.get(k) == b_meta.get(k):
                    same_fields.append(k)
            metadata_same[a_id][b_id] = same_fields
            metadata_same[b_id][a_id] = same_fields

            worst_pairs.append({
                "a": a_id, "b": b_id,
                "overall": overall[a_id][b_id],
                "paragraph_max": paragraph[a_id][b_id],
                "worst_chapter": worst_ch,
                "worst_para_a": worst_a,
                "worst_para_b": worst_b,
                "data_fp": data_fp[a_id][b_id],
                "heading_order": heading_order[a_id][b_id],
                "heading_set": heading_set[a_id][b_id],
                "metadata_same": same_fields,
            })

    worst_pairs.sort(key=lambda x: x["paragraph_max"], reverse=True)
    return {
        "ids": ids,
        "overall": dict(overall),
        "paragraph": dict(paragraph),
        "data_fp": dict(data_fp),
        "heading_order": dict(heading_order),
        "heading_set": dict(heading_set),
        "metadata_same": dict(metadata_same),
        "pairs": worst_pairs,
    }


def write_report(matrix: dict, thresholds: dict, report_path: Path) -> int:
    """写人读报告，返回超阈值对数"""
    ids = matrix["ids"]
    th_overall = thresholds["overall"]
    th_para = thresholds["paragraph"]
    th_h_order = thresholds.get("heading_order", 0.70)
    th_h_set = thresholds.get("heading_set", 0.85)

    lines = [
        "# 交叉相似度检查报告",
        "",
        f"**阈值：** 整体 < {th_overall:.0%}，段落 < {th_para:.0%}，"
        f"标题顺序 < {th_h_order:.0%}，标题集合 < {th_h_set:.0%}",
        f"**共 {len(ids)} 份技术标，{len(ids) * (len(ids) - 1) // 2} 对两两比对**",
        "",
    ]

    # 整体矩阵
    lines.extend(["## 一、整体文字重复率矩阵", "",
                  "| | " + " | ".join(ids) + " |",
                  "|" + "---|" * (len(ids) + 1)])
    for a in ids:
        row = [a]
        for b in ids:
            if a == b:
                row.append("—")
            else:
                v = matrix["overall"].get(a, {}).get(b, 0)
                mark = "❌" if v > th_overall else "✓"
                row.append(f"{v:.2f} {mark}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 段落最大重复率矩阵
    lines.extend(["## 二、段落级最大重复率矩阵", "",
                  "| | " + " | ".join(ids) + " |",
                  "|" + "---|" * (len(ids) + 1)])
    for a in ids:
        row = [a]
        for b in ids:
            if a == b:
                row.append("—")
            else:
                v = matrix["paragraph"].get(a, {}).get(b, 0)
                mark = "❌" if v > th_para else "✓"
                row.append(f"{v:.2f} {mark}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 标题骨架矩阵（核心反"一个妈生的"指标）
    lines.extend(["## 三、标题骨架相似度矩阵（H2 顺序敏感）", "",
                  "| | " + " | ".join(ids) + " |",
                  "|" + "---|" * (len(ids) + 1)])
    for a in ids:
        row = [a]
        for b in ids:
            if a == b:
                row.append("—")
            else:
                vo = matrix["heading_order"].get(a, {}).get(b, 0)
                vs = matrix["heading_set"].get(a, {}).get(b, 0)
                mark = "❌" if vo >= th_h_order else ("⚠" if vs >= th_h_set else "✓")
                row.append(f"{vo:.2f}/{vs:.2f} {mark}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("> 单元格格式 `顺序/集合`；顺序 ≥ 阈值标 ❌（结构收敛，须重写）；集合 ≥ 阈值标 ⚠（子节雷同，建议复核）。")
    lines.append("")

    # 高危对
    violations = [p for p in matrix["pairs"]
                  if p["overall"] > th_overall or p["paragraph_max"] > th_para
                  or p["heading_order"] >= th_h_order
                  or p["metadata_same"]]
    lines.append(f"## 四、超阈值对（{len(violations)} 对）")
    lines.append("")
    if not violations:
        lines.append("✅ 所有对比均在阈值内")
    else:
        for p in violations:
            lines.append(f"### {p['a']} ↔ {p['b']}")
            lines.append(f"- 整体重复率：**{p['overall']:.2%}**" + ("  ❌" if p["overall"] > th_overall else ""))
            lines.append(f"- 段落最大重复率：**{p['paragraph_max']:.2%}**（章节 {p['worst_chapter']}）" +
                         ("  ❌" if p["paragraph_max"] > th_para else ""))
            lines.append(f"- 数据指纹重复率：{p['data_fp']:.2%}")
            lines.append(f"- 标题骨架顺序相似度：**{p['heading_order']:.2%}**"
                         + ("  ❌" if p["heading_order"] >= th_h_order else ""))
            lines.append(f"- 标题骨架集合相似度：{p['heading_set']:.2%}"
                         + ("  ⚠" if p["heading_set"] >= th_h_set else ""))
            if p["metadata_same"]:
                lines.append(f"- 元数据冲突：{', '.join(p['metadata_same'])} ❌")
            if p["paragraph_max"] > th_para:
                lines.append(f"- 示例段（A）：`{p['worst_para_a']}...`")
                lines.append(f"- 示例段（B）：`{p['worst_para_b']}...`")
                lines.append(f"- **修复建议：** 重跑 `bid_writer.py --variant {p['b']} --chapter {p['worst_chapter']} --force`（或换更高 temperature）")
            if p["heading_order"] >= th_h_order:
                lines.append(f"- **标题收敛修复建议：** 先给 {p['b']} 重种 section_order_seed 再全量重写，"
                             f"命令：`bid_run.py --reseed {p['b']} --from write --to similarity`")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return len(violations)


def main() -> int:
    p = argparse.ArgumentParser(description="批量投标交叉相似度检查")
    p.add_argument("tender_dir", help="投标根目录")
    p.add_argument("--threshold-overall", type=float, default=None)
    p.add_argument("--threshold-paragraph", type=float, default=None)
    p.add_argument("--threshold-heading-order", type=float, default=None)
    p.add_argument("--threshold-heading-set", type=float, default=None)
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    output = tender_dir / "output"
    extracted = tender_dir / "extracted"

    log = make_script_logger("bid_similarity", output / "similarity.log")
    log.info("=" * 60)
    log.info(f"bid_similarity 启动  目录: {tender_dir}")

    # 读阈值（variants.json 优先，CLI 覆盖）
    th = {"overall": 0.40, "paragraph": 0.50, "heading_order": 0.70, "heading_set": 0.85}
    variants_path = extracted / "variants.json"
    if variants_path.exists():
        payload = json.loads(variants_path.read_text(encoding="utf-8"))
        th.update(payload.get("similarity_threshold", {}))
    if args.threshold_overall is not None:
        th["overall"] = args.threshold_overall
    if args.threshold_paragraph is not None:
        th["paragraph"] = args.threshold_paragraph
    if args.threshold_heading_order is not None:
        th["heading_order"] = args.threshold_heading_order
    if args.threshold_heading_set is not None:
        th["heading_set"] = args.threshold_heading_set

    variants_data = collect_variant_texts(output)
    if len(variants_data) < 2:
        log.error(f"至少需要 2 个 variant 才能做交叉检查，当前发现 {len(variants_data)} 个")
        return 2

    log.info(f"开始两两比对，共 {len(variants_data)} × {len(variants_data)} 矩阵")
    matrix = build_matrix(variants_data, log)

    (output / "similarity_matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_path = output / "相似度检查报告.md"
    violations = write_report(matrix, th, report_path)
    log.info(f"报告已写入: {report_path}")
    log.info(f"超阈值对: {violations}")

    return 0 if violations == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
