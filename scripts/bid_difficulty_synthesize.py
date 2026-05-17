#!/usr/bin/env python3
"""
bid_difficulty_synthesize.py - 合成 brain.md 第 3/4 节

读 project_facts.json + mandatory_outline.json + special_clauses.json (若有)
+ 招标主文档全文,调 DeepSeek 一次 prompt 出:
  - 3-5 条技术重点(重点)
  - 3-5 条施工难点(难点)
  - 5-6 类施工约束(约束)

输出 extracted/difficulties.json (固定 schema)。
bid_brain_translate.py 后续读取此文件填第 3/4 节,替代旧的"从旧 brain 提取"逻辑。

用法:
  python bid_difficulty_synthesize.py /path/to/tender/
  python bid_difficulty_synthesize.py /path/to/tender/ --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from bid_common import make_script_logger  # noqa: E402
from bid_llm_client import LLMClient  # noqa: E402
from bid_facts_extract import pick_main_tender_text  # noqa: E402

OK = "✓"
WARN = "⚠"
FAIL = "✗"

OUTPUT_SCHEMA = {
    "重点": [
        {
            "title": "重点标题(简洁,15-25字,体现项目独有性)",
            "rationale": [
                "为什么是重点(从工程量/招标特殊条款/单位性质等角度,2-3句)",
                "应对措施方向(具体技术/管理动作,2-3句)"
            ],
            "evidence": "引用招标文件段落或工程量数字(如 401.45㎡夹芯板/第二篇九)"
        }
    ],
    "难点": [
        {
            "title": "难点标题",
            "rationale": ["难度来源", "应对方向"],
            "evidence": "招标依据或量化指标"
        }
    ],
    "约束": [
        {"category": "工期节点", "content": "..."},
        {"category": "季节气候", "content": "..."},
        {"category": "场地交通", "content": "..."},
        {"category": "周边环境", "content": "..."},
        {"category": "材料设备", "content": "..."},
        {"category": "其他约束", "content": "..."}
    ]
}


SYSTEM_PROMPT = (
    "你是有 20 年经验的投标施工组织设计专家。擅长从招标文件读出项目独有的"
    "技术重点、施工难点、约束条件,而不是套用通用模板。\n\n"
    "硬性输出规则:\n"
    "1. 只输出 JSON,不要任何 markdown 标记/前言/解释\n"
    "2. 必须引用招标文件具体条款或工程量数字(evidence 字段)\n"
    "3. 重点/难点标题必须体现项目独有性(如'派出所不停工施工'优于'安全生产')\n"
    "4. 不要写通用废话(如'加强组织管理'、'确保工程质量'这类无信息量句子)\n"
    "5. 约束条件 6 大类必填,确无相关信息时 content 写 '招标文件未明示,常规执行'\n"
    "6. 重点 3-5 条,难点 3-5 条(可少不可多,聚焦核心)"
)


def build_user_prompt(facts: dict, outline: dict, special: dict | None,
                      main_text: str) -> str:
    """构造 user prompt。"""
    facts_lines = []
    for k, v in facts.items():
        if isinstance(v, dict):
            val = v.get("value")
            if val is not None:
                facts_lines.append(f"- {k}: {val}")

    facts_block = "\n".join(facts_lines) if facts_lines else "(无)"

    outline_topics = outline.get("topics", []) if outline else []
    outline_lines = [f"- {t.get('title_original')}"
                     for t in outline_topics if t.get("title_original")]
    outline_block = "\n".join(outline_lines) if outline_lines else "(无显式大纲)"

    special_block = ""
    if special:
        special_clauses = special.get("clauses", [])
        if special_clauses:
            special_block = "\n## 特殊条款摘录\n" + "\n".join(
                f"- {c.get('text', '')[:200]}" for c in special_clauses[:10]
            )
        boq = special.get("boq_summary")
        if boq:
            special_block += f"\n\n## 工程量清单摘要\n{json.dumps(boq, ensure_ascii=False, indent=2)[:2000]}"

    # 截取主文档前 30K 字符(约 60 页)
    text_snippet = main_text[:30000] if len(main_text) > 30000 else main_text

    schema_str = json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2)

    return f"""# 项目基本信息(facts)
{facts_block}

# 招标方规定的必含大纲(outline)
{outline_block}
{special_block}

# 招标文件正文摘录(前 60 页或全文)
{text_snippet}

---

请基于上述信息,输出本项目的"技术重点+施工难点+施工约束"分析。

# 输出 JSON schema(严格遵守字段名和层级,数字字段不带引号)
{schema_str}

# 关键要求
- 重点/难点必须基于本项目特殊性(工程量/单位性质/工期/地理)
- 每条 evidence 字段必须引用具体招标条款或工程量数字
- 约束的 6 大类(工期/季节/场地/环境/材料/其他)必须全部输出
- 输出严格的 JSON,不要 ```json ``` 包围,不要其他文字
"""


def synthesize(tender_dir: Path, force: bool = False, log=None) -> int:
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    out_path = ext / "difficulties.json"
    if out_path.exists() and not force:
        log.info(f"  difficulties.json 已存在,需 --force 覆盖")
        return 1

    # 加载输入
    facts_doc = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))
    facts = facts_doc.get("facts", {})

    outline_path = ext / "mandatory_outline.json"
    outline = json.loads(outline_path.read_text(encoding="utf-8")) if outline_path.exists() else {}

    special_path = ext / "special_clauses.json"
    special = json.loads(special_path.read_text(encoding="utf-8")) if special_path.exists() else None

    main_txt = pick_main_tender_text(ext)
    if not main_txt:
        log.error("找不到主文档")
        return 2
    main_text = main_txt.read_text(encoding="utf-8")
    log.info(f"  主文档: {main_txt.name} ({len(main_text):,} 字符)")

    user = build_user_prompt(facts, outline, special, main_text)
    log.info(f"  prompt 长度: {len(user):,} 字符")

    # 调 DeepSeek
    try:
        client = LLMClient.load(log=log)
        resp = client.chat(
            model_key="deepseek",
            system=SYSTEM_PROMPT,
            user=user,
            temperature=0.3,
            max_tokens=4000,
        )
    except Exception as e:
        log.error(f"  DeepSeek 调用失败: {e}")
        return 2

    raw = resp.text.strip()
    # 去 markdown 包围
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"  JSON 解析失败: {e}")
        log.debug(f"  原文前 500 字: {raw[:500]}")
        return 2

    # 校验 schema
    required_keys = ["重点", "难点", "约束"]
    missing = [k for k in required_keys if k not in data]
    if missing:
        log.error(f"  输出缺失关键字段: {missing}")
        return 2

    n_emphasis = len(data.get("重点", []))
    n_difficulty = len(data.get("难点", []))
    n_constraint = len(data.get("约束", []))
    log.info(f"  ✓ DeepSeek 合成: 重点 {n_emphasis} 条 / 难点 {n_difficulty} 条 / 约束 {n_constraint} 类")

    # 添加 meta
    data["_meta"] = {
        "source_file": main_txt.name,
        "extracted_by": "bid_difficulty_synthesize.py",
        "model": "deepseek",
        "facts_hit": sum(1 for v in facts.values() if isinstance(v, dict) and v.get("value") is not None),
        "has_special": special is not None,
    }

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info(f"  ✓ 写入 {out_path.name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--force", action="store_true", help="覆盖已有 difficulties.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    log = make_script_logger("bid_difficulty_synthesize", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_difficulty_synthesize  目录: {tender_dir}")
    code = synthesize(tender_dir, force=args.force, log=log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
