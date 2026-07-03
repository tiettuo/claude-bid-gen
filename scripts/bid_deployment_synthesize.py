#!/usr/bin/env python3
"""
bid_deployment_synthesize.py - 合成「总体施工部署纲领」(连贯性主线锚点)

为什么需要这个脚本:
  施组散装的根因之一是没有"施工主线"。领域方法论(民航局规范/项目总工纲要)认为
  施工组织设计应以「总体施工部署」为纲领、以进度为主线串联各章节。本脚本产出
  一份部署纲领 JSON,供下游 plan_synthesize(章节叙事维度列)/ writer(Block1
  主线回指) / chapter_intro(引言回指) / coherence_pass(引用编织)消费。

输入:
  - extracted/difficulties.json (bid_difficulty_synthesize.py 产出,不重抽)
  - extracted/project_facts.json
  - extracted/mandatory_outline.json (章节锚点,用于难点→章节号映射)
  - 招标主文档全文

输出: extracted/deployment_outline.json
  {
    "deployment_statement": "一句话部署总思路(贯穿全文的'口号')",
    "deployment_dimensions": [5维部署摘要],
    "difficulty_to_dimension": [每难点→主维度+建议章节+切入角度],
    "_meta": {...}
  }

依赖顺序: 必须在 bid_difficulty_synthesize 之后、bid_plan_synthesize 之前运行。
  (difficulty 是其输入;plan 消费其输出做章节叙事维度列)

用法:
  python bid_deployment_synthesize.py /path/to/tender/
  python bid_deployment_synthesize.py /path/to/tender/ --force
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

# 固定的 5 维部署维度(民航局规范/项目总工纲要的标准框架)
DEPLOYMENT_DIMENSIONS = [
    "施工总目标",      # 进度/质量/安全/环境四目标量化承诺
    "施工区段划分",    # 按项目特点划分施工区段/流水段
    "施工总顺序",      # 先地下后地上/先主体后装修/关键工序逻辑
    "资源统筹原则",    # 劳动力/机械/材料的总体配置思路
    "组织机构框架",    # 项目部架构+关键岗位配置
]

OUTPUT_SCHEMA = {
    "deployment_statement": "一句话项目定位+部署总思路(50-80字,贯穿全文的纲领性陈述)",
    "deployment_dimensions": [
        {
            "dim": "施工总目标",
            "summary": "本维度部署要点(80-150字,含量化承诺:工期/质量/安全目标)",
        },
        {
            "dim": "施工区段划分",
            "summary": "本维度部署要点(说明划分依据和区段数)",
        },
        {
            "dim": "施工总顺序",
            "summary": "本维度部署要点(关键工序逻辑链)",
        },
        {
            "dim": "资源统筹原则",
            "summary": "本维度部署要点(人力/机械/材料配置思路)",
        },
        {
            "dim": "组织机构框架",
            "summary": "本维度部署要点(项目部架构)",
        },
    ],
    "difficulty_to_dimension": [
        {
            "difficulty": "<难点标题(从 difficulties.json 取,原样复制)>",
            "primary_dimension": "5维之一(此难点主要由哪个维度承载)",
            "angle": "本章应从什么角度回应此难点(一句话,供 writer 引用)",
        },
    ],
}


SYSTEM_PROMPT = (
    "你是有 20 年经验的投标施工组织设计专家,擅长为施工项目制定「总体施工部署」纲领。\n"
    "你深谙民航局规范/项目总工纲要的方法论:施组应以总体部署为纲领、以进度为主线串联各环节。\n\n"
    "硬性输出规则:\n"
    "1. 只输出 JSON,不要任何 markdown 标记/前言/解释\n"
    "2. deployment_statement 必须是项目独有的部署总思路(如'以派出所不停工为前提,分区流水作业')\n"
    "   禁止通用废话(如'科学组织、精心施工')\n"
    "3. deployment_dimensions 五维必须全部输出,每维 summary 要含本项目具体信息\n"
    "   (数字/地点/子工程名/工期节点),不要写任何项目都能用的套话\n"
    "4. difficulty_to_dimension:对输入的每条难点,指定它主要由哪个部署维度承载,\n"
    "   并给出'本章应从什么角度回应'的一句话指引(供下游 writer 注入 prompt)\n"
    "5. 输出严格的 JSON,字段名和层级严格遵守 schema"
)


def build_user_prompt(
    facts: dict,
    outline: dict,
    difficulties: dict,
    main_text: str,
) -> str:
    """构造 user prompt。"""
    facts_lines = []
    for k, v in facts.items():
        if isinstance(v, dict):
            val = v.get("value")
            if val is not None:
                facts_lines.append(f"- {k}: {val}")
    facts_block = "\n".join(facts_lines) if facts_lines else "(无)"

    outline_topics = outline.get("topics", []) if outline else []
    outline_lines = [
        f"- {t.get('title_original')}"
        for t in outline_topics
        if t.get("title_original")
    ]
    outline_block = "\n".join(outline_lines) if outline_lines else "(无显式大纲)"

    # 难点列表(从 difficulties.json 提取,原样给 LLM 做映射)
    diff_block = "(无 difficulties.json,请基于工程常识自行识别 3-5 条难点)"
    if difficulties:
        ds = difficulties.get("难点", [])
        zs = difficulties.get("重点", [])
        parts = []
        if zs:
            parts.append("技术重点:")
            for z in zs[:5]:
                t = z.get("title", "") if isinstance(z, dict) else str(z)
                if t:
                    parts.append(f"  · {t}")
        if ds:
            parts.append("施工难点(每条都要在 difficulty_to_dimension 中映射):")
            for d in ds[:5]:
                t = d.get("title", "") if isinstance(d, dict) else str(d)
                if t:
                    parts.append(f"  · {t}")
        diff_block = "\n".join(parts) if parts else diff_block

    # 截取主文档前 20K 字符(部署不需要全文,控 token)
    text_snippet = main_text[:20000] if len(main_text) > 20000 else main_text

    schema_str = json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2)

    return f"""# 项目基本信息(facts)
{facts_block}

# 招标方规定的章节大纲(部署纲领要能覆盖这些章节)
{outline_block}

# 已识别的重点难点(供 difficulty_to_dimension 映射)
{diff_block}

# 招标文件正文摘录(前 40 页)
{text_snippet}

---

请基于上述信息,输出本项目的「总体施工部署纲领」。

# 输出 JSON schema(严格遵守,数字字段不带引号)
{schema_str}

# 关键要求
- deployment_statement 是全文叙事主线,下游每章引言会回指它,必须精炼有力且项目独有
- 5 维部署是章节叙事职责的来源,每维 summary 要具体到本项目
- difficulty_to_dimension 必须覆盖上面列出的每条难点,difficulty 字段原样复制难点标题
- primary_dimension 必须是这 5 个之一: {"/".join(DEPLOYMENT_DIMENSIONS)}
- 输出严格的 JSON,不要 ```json ``` 包围,不要其他文字
"""


def _strip_markdown_fence(raw: str) -> str:
    """去掉 ```json ... ``` 包围。"""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


def synthesize(tender_dir: Path, force: bool = False, log=None) -> int:
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    out_path = ext / "deployment_outline.json"
    if out_path.exists() and not force:
        log.info(f"  deployment_outline.json 已存在,需 --force 覆盖")
        return 1

    # 输入:difficulties.json(必需,本脚本是难点→部署的映射)
    diff_path = ext / "difficulties.json"
    if not diff_path.exists():
        log.error(f"  缺少 difficulties.json — 请先跑 bid_difficulty_synthesize.py")
        return 2
    difficulties = json.loads(diff_path.read_text(encoding="utf-8"))

    # 输入:facts / outline
    facts_doc = json.loads((ext / "project_facts.json").read_text(encoding="utf-8"))
    facts = facts_doc.get("facts", {})

    outline_path = ext / "mandatory_outline.json"
    outline = (
        json.loads(outline_path.read_text(encoding="utf-8"))
        if outline_path.exists()
        else {}
    )

    # 主文档
    main_txt = pick_main_tender_text(ext)
    if not main_txt:
        log.error("找不到主文档")
        return 2
    main_text = main_txt.read_text(encoding="utf-8")
    log.info(f"  主文档: {main_txt.name} ({len(main_text):,} 字符)")

    user = build_user_prompt(facts, outline, difficulties, main_text)
    log.info(f"  prompt 长度: {len(user):,} 字符")

    # 调 DeepSeek(与 difficulty_synthesize 一致)
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

    raw = _strip_markdown_fence(resp.text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"  JSON 解析失败: {e}")
        log.debug(f"  原文前 500 字: {raw[:500]}")
        return 2

    # 校验 schema
    if "deployment_statement" not in data or "deployment_dimensions" not in data:
        log.error("  输出缺失 deployment_statement / deployment_dimensions")
        return 2

    dims = data.get("deployment_dimensions", [])
    if not isinstance(dims, list) or len(dims) < 3:
        log.warning(f"  deployment_dimensions 不足 5 维(实际 {len(dims)}),继续但质量待查")

    d2d = data.get("difficulty_to_dimension", []) or []
    n_diff_in = len(difficulties.get("难点", []))
    n_d2d = len(d2d)
    log.info(
        f"  ✓ DeepSeek 合成: 部署声明 {len(data['deployment_statement'])} 字 / "
        f"{len(dims)} 维部署 / 难点映射 {n_d2d}/{n_diff_in} 条"
    )
    if n_diff_in and n_d2d < n_diff_in:
        log.warning(
            f"  difficulty_to_dimension 覆盖不全({n_d2d}/{n_diff_in}),"
            f"下游 plan_synthesize 会对未映射难点降级处理"
        )

    # 校验 primary_dimension 合法性
    valid_dims = set(DEPLOYMENT_DIMENSIONS)
    bad = [
        d
        for d in d2d
        if isinstance(d, dict) and d.get("primary_dimension") not in valid_dims
    ]
    if bad:
        log.warning(
            f"  {len(bad)} 条映射的 primary_dimension 不在 5 维内,下游会做模糊匹配兜底"
        )

    data["_meta"] = {
        "source_file": main_txt.name,
        "extracted_by": "bid_deployment_synthesize.py",
        "model": "deepseek",
        "n_difficulties_input": n_diff_in,
        "n_dimensions": len(dims),
        "n_difficulty_mapped": n_d2d,
    }

    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"  ✓ 写入 {out_path.name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("tender_dir", help="投标根目录(含 extracted/)")
    p.add_argument("--force", action="store_true", help="覆盖已有 deployment_outline.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    log = make_script_logger(
        "bid_deployment_synthesize",
        ext / "skeleton_extract.log",
        verbose=args.verbose,
    )
    log.info("=" * 60)
    log.info(f"bid_deployment_synthesize  目录: {tender_dir}")
    code = synthesize(tender_dir, force=args.force, log=log)
    log.info("=" * 60)
    return code


if __name__ == "__main__":
    sys.exit(main())
