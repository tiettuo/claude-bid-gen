#!/usr/bin/env python3
"""
bid_special_extract.py - Tier 4 项目特殊条款抽取

范本(Tier 0)已覆盖通用 16 章主题/B1.x 31 条/1.4.3 24 项/10.16 格式;
v2 骨架(bid_skeleton_extract)已抓评分办法/必含大纲/废标条款;
本步专门抽取**项目独有的特殊条款**——10.17 条 + 各行业专属约束。

例:
  - 邮政类:邮政业务合作要求、廉政承诺、邮政员工持股回避
  - 政府投资类:农民工工资专户、20% 人工费拨付
  - 高原类:海拔特殊技术要求、应急医疗、氧气保障
  - 教育类:校园安全/师生通行/施工时段限制
  - 区外企业:在藏备案、属地化管理

为何 LLM 而非关键词:这些条款常埋在表格单元格里(如"10.17 其他条件"),
关键词命不中,且每个项目个性化强。需要让 LLM 阅读全文后抽出。

输出:extracted/special_clauses.json
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
from bid_skeleton_extract import (  # noqa: E402
    pick_main_tender_text,
    build_page_index,
    _strip_json_fence,
    _parse_json_with_retry,
    SYSTEM_PROMPT,
)


SPECIAL_USER_TEMPLATE = """以下是某招标文件全文。请抽取**项目特殊条款**——
即范本通用条款(16 章主题、B1.x 否决条款、1.4.3 24 项限制投标、10.16 格式)
**之外**的、本项目独有或行业特定的实质性约束。

# 重点关注的特殊条款类别

1. **邮政类专属**:邮政业务合作要求、廉政承诺、邮政员工持股回避、邮政线路保护、
   既有邮政设施保护
2. **政府投资类**:农民工工资专户、人工费按合同金额比例拨付(常见 20%)、
   重庆/广东等援藏资金管理要求
3. **高原专属**:海拔技术要求(冷库、钢结构、混凝土等)、应急医疗、氧气供给、
   高原型设备适配、电机降容
4. **教育/校园类**:师生通行隔离、教学时段施工限制、校园安全管理
5. **区外企业**:在藏备案、属地化分包、藏区材料采购优先
6. **行业特定**:冷库深化设计要求、钢结构第三方检测、装配式构件运输要求等
7. **10.17 / 10.X 其他条件**:招标文件投标人须知前附表 10.X 系列(超过 10.16 之后的)
   常出现项目个性条款

# 输出 JSON schema

{{
  "clauses": [
    {{
      "category": "邮政|政府|高原|教育|区外|行业|其他",
      "topic": "简短主题(8-15 字),如'邮政业务合作 / 廉政承诺 / 人工费 20% 拨付'",
      "text_original": "原文条款(20-200 字,逐字抄)",
      "drives_chapters": ["brain.section4", "chapter04.项目组织管理机构"],
      "is_mandatory": true,
      "source_page": 25,
      "source_page_method": "ocr | text"
    }}
  ],
  "summary": {{
    "total_count": 0,
    "by_category": {{}}
  }}
}}

# 抽取规则

- **逐字抄**原文,不概括/改写。category 按上面 7 类归档,7 类都没命中归"其他"。
- **drives_chapters** 标注本条款影响哪一章节(看 16 章主题对照),让下游 brain.md 知道
  在哪一章响应这条特殊要求。
- 范本已覆盖的内容**不要重复抽**:
  * 不抽 16 章主题清单(范本已有)
  * 不抽 B1.1-B1.31(范本已有)
  * 不抽 1.4.3 的 24 项(范本已有)
  * 不抽 10.16 格式约束(范本已有)
- **优先关注 10.17 / 10.18 等条款**——这些常常是项目独有,关键词不一定命中。

# 招标文件原文

{tender_text}

只输出 JSON。"""


def extract_special(tender_text: str, client: LLMClient, model: str, log) -> dict:
    log.info("[Tier 4] 抽取项目特殊条款...")
    user = SPECIAL_USER_TEMPLATE.format(tender_text=tender_text)
    payload = _parse_json_with_retry(client, model, SYSTEM_PROMPT, user, log=log)
    if not payload:
        raise RuntimeError("Tier 4 特殊条款抽取失败")
    return payload


def validate_special(payload: dict) -> tuple[bool, list[str]]:
    warnings = []
    clauses = payload.get("clauses") or []
    if len(clauses) < 3:
        warnings.append(
            f"特殊条款数量偏少 ({len(clauses)} < 3),邮政/政府类项目通常 ≥ 5 条,请人工核对"
        )
    no_page = sum(1 for c in clauses if c.get("source_page") is None)
    if no_page:
        warnings.append(f"{no_page} 条特殊条款无 source_page,需补全")
    return True, warnings


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tender_dir")
    p.add_argument("--model")
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    tender_dir = Path(args.tender_dir).expanduser().resolve()
    ext = tender_dir / "extracted"
    if not ext.is_dir():
        print(f"extracted/ 不存在: {ext}", file=sys.stderr)
        return 2

    log = make_script_logger("bid_special_extract", ext / "skeleton_extract.log",
                             verbose=args.verbose)
    log.info("=" * 60)
    log.info(f"bid_special_extract  目录: {tender_dir}")

    out_path = ext / "special_clauses.json"
    if out_path.exists() and not args.force:
        log.info(f"  special_clauses.json 已存在,跳过(--force 重抽)")
        return 0

    main_txt = pick_main_tender_text(ext, log)
    if not main_txt:
        return 2
    full_text = main_txt.read_text(encoding="utf-8")
    pages = build_page_index(full_text)
    log.info(f"  主文档: {main_txt.name} ({len(full_text):,} 字符,{len(pages)} 页)")

    try:
        client = LLMClient.load(log=log)
    except Exception as e:
        log.error(f"LLM 配置加载失败: {e}")
        return 2

    model = args.model or client.config.get("default_model", "glm")
    log.info(f"  使用模型: {model}")

    try:
        payload = extract_special(full_text, client, model, log)
    except Exception as e:
        log.error(f"  ✗ 抽取失败: {e}")
        return 2

    ok, warns = validate_special(payload)
    if not ok:
        log.error(f"  ✗ 校验失败: {warns}")
        return 2

    payload["_meta"] = {
        "source_file": main_txt.name,
        "extracted_by": "bid_special_extract.py",
        "model": model,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    n = len(payload.get("clauses", []))
    log.info(f"  ✓ 抽到 {n} 条特殊条款,写入 {out_path.name}")
    for c in payload.get("clauses", [])[:5]:
        log.info(f"    [{c.get('category')}] {c.get('topic')}  "
                 f"[p{c.get('source_page')}]")
    for w in warns:
        log.warning(f"    ⚠ {w}")
    log.info("=" * 60)
    return 0 if not warns else 1


if __name__ == "__main__":
    sys.exit(main())
